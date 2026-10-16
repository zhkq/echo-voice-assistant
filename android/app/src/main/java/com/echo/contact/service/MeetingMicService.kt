package com.echo.contact.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.echo.contact.MainActivity
import com.echo.contact.R
import com.echo.contact.audio.FlacSegmentEncoder
import com.echo.contact.audio.InputDevices
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.data.ApiClient
import com.echo.contact.data.PairStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.sqrt

/**
 * **手机当麦克风**的会议录音（用户 2026-10-15 的口径：*"手机发起的指令，手机收，
 * 电脑是配合执行和存储"*）。
 *
 * 于是"从手机发起录音"时采集的是**手机这支麦**（手机在会议室、电脑可能合着盖），
 * 电脑只负责落盘、转写、存储 —— 音频按段 POST 给 `/api/meeting/segment`。
 *
 * ## 只有一条路：**本地先写 + 机会性上传**（用户 2026-10-17 拍板）
 *
 * 用户原话：*"离线录音和在线录音就没什么本质区别了"*。所以这一版**删掉了**
 * `RecordPlan.Online` / `RecordPlan.Offline` 那条分叉，只留一套：
 *
 * ```
 * 点录音 → 本地写段（不受任何服务端状态影响）
 *         → 每到一段封存：顺手尝试上传（网通 + 没冲突就传；否则留着）
 *         → 结束：补齐剩余段 → 对账 → stop(clientId) → DONE
 * ```
 *
 * **为什么本地先写**（而不是"网通则边录边传、本地不留"）：
 * 会议录音是**不可重录**的 —— 网络的每一次抖动都不该让一段音频变成"只存在于网络上"。
 * 先落本地之后，任何时刻断电都有完整的一份在手，上传只是一个**幂等的可重试动作**
 * （`(clientId, seq)` 幂等），失败也不影响录音本身。
 *
 * **为什么单路比双路安全**：双路时"在线那条"的失败段放在 `cacheDir/meeting-mic`、
 * "离线那一场"放在 `filesDir/offline-meetings`，两套账、两个恢复路径、两处判据
 * —— "录到一半网断了"正好落在那两套账的缝里。现在只有一份音频、一条补传路
 * （`OfflineMeetingSync`），判据只有一处。
 *
 * **在线时的行为不变差**：网通就**边录边传**（最多滞后一段），面板/列表照旧显示
 * "已缓存 N 段 / 已传 M 段"；面板与通知里那句"还没传上去"仍然一个字都不许含糊。
 *
 * ## 段的格式：FLAC，编不出来退回 WAV（用户 2026-10-17 的第①件事）
 *
 * 每段封存时用 `MediaCodec` 的 `audio/flac` 编（16 kHz 单声道，与原来那份 PCM 同参数），
 * 落成 `seg-%04d.flac`，并在清单里记下**这一段真的**格式；上传时 `Content-Type` 用那一份。
 * 编码器不可用/编码异常 → **这一段退回 WAV**（见 `SegmentEncoding`）：绝不丢音频、
 * 绝不谎报格式，界面/通知里如实写"第 N 段用 WAV（FLAC 编码器不可用）"。
 *
 * ## 三个刻意的取舍
 *
 *  1. **按段（默认 30 秒）传**，不做实时流：一段 ≈1 MB（16 kHz 单声道 ≈ 32 KB/s），
 *     走中继（单次上限 12 MB）很稳；断了只重传这一段，不会把整场拖垮。
 *     改成 FLAC 之后大约只要一半体积。
 *  2. **发不出去就留着**：所有段都在 `filesDir/offline-meetings/`，
 *     **确认对账通过（且服务端收尾）才敢说已上传**，而且**默认不删**
 *     （宁可多留一会儿也别丢音频）。状态里如实写"还有 N 段没传上去"。
 *  3. **静音（暂停）= 照常录、写进去的是全零**（用户 2026-10-16 深夜推翻旧的"不写盘"口径，
 *     并选了"手机与电脑统一"；电脑侧已改完）：这一帧**照样计数、照样封段**，只是内容是零样本。
 *     为什么必须逐字连续见 [MicCaptureLoop]；暂停区间照旧上报 `POST /api/meeting/pauses`
 *     （用户 2026-10-17 的第③件事），而那一条**不能拖垮补传**（老服务端没这个端点时只如实记一句）。
 *
 * 前台服务（麦克风型）+ 常驻通知：Android 14 要求录音必须在可见的前台服务里做。
 */
class MeetingMicService : Service() {

    companion object {
        const val ACTION_START = "com.echo.contact.MEETING_MIC_START"
        const val ACTION_STOP = "com.echo.contact.MEETING_MIC_STOP"
        const val ACTION_PAUSE = "com.echo.contact.MEETING_MIC_PAUSE"
        const val ACTION_RESUME = "com.echo.contact.MEETING_MIC_RESUME"

        /**
         * 「现在同步」（需求点名要的那个明确入口）：把**本地还欠着的段**传上去。
         *
         * ⚠️ 用户 2026-10-17 合并两条路之后，这里**只有一个**补传入口了：
         * 原来那个「立即重传」（补 `cacheDir/meeting-mic` 里在线失败的段）**退役了** ——
         * 那正是"双路"留下的第二套账，而现在**所有**段都在本地清单里，一次同步就够了。
         */
        const val ACTION_SYNC = "com.echo.contact.MEETING_MIC_SYNC"

        private const val CHANNEL_ID = "echo_meeting_mic"
        private const val NOTIF_ID = 4211

        /** 日志标签（**段的格式退回 WAV 这类"如实的事"也要落进日志** —— 真机排障只能靠它）。 */
        private const val TAG = "MeetingMic"
        const val SAMPLE_RATE = 16000
        const val CHANNELS = 1
        private const val FRAME_SAMPLES = 1600                     // 100ms

        /**
         * 一段多长，**由偏好决定**（设置页那三个 15/30/60）。
         *
         * 为什么**默认 30 秒**：一段 ≈1 MB（16kHz 单声道 16bit ≈ 32 KB/s；FLAC 之后约一半），
         * 走中继（单次上限 12 MB）很稳；再长一点，一次失败要重传的就多；
         * 再短一点，段数太多、每段都有一次往返开销。改偏好**下次开录生效**
         * （正在录的这一场按开录时读到的档，中途换档不会把已缓冲的那半段切碎）。
         */
        private fun segmentSeconds(context: Context): Int = AppPrefs.segmentSeconds(context)

        fun start(context: Context) {
            val i = Intent(context, MeetingMicService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(i)
            else context.startService(i)
        }

        fun stop(context: Context) {
            context.startService(Intent(context, MeetingMicService::class.java).setAction(ACTION_STOP))
        }

        fun setPaused(context: Context, paused: Boolean) {
            val action = if (paused) ACTION_PAUSE else ACTION_RESUME
            context.startService(Intent(context, MeetingMicService::class.java).setAction(action))
        }

        /**
         * 「现在同步」：让服务把**本地清单**里欠的段传上去（用户明确按的那一下）。
         *
         * 为什么先在界面上说一句再让服务去干：`startService` 是异步的，而"按了没反应"
         * 是这类按钮最糟的体验。这里**不受退避限制**（用户说了"现在"）。
         */
        fun syncNow(context: Context) {
            val app = context.applicationContext
            val store = MeetingDraftStore(File(app.filesDir, OFFLINE_DIR))
            val waits = store.pending()
            _state.value = _state.value.copy(
                syncNote = if (waits.isEmpty()) "现在没有要传的录音"
                else "正在同步 ${waits.size} 场录音（${waits.sumOf { it.pendingSegments }} 段）…")
            runCatching {
                app.startService(
                    Intent(app, MeetingMicService::class.java).setAction(ACTION_SYNC))
            }
        }

        /**
         * 后台补传的入口（App 在**前台**的状态轮询里每几秒调一次；还有听写服务那边）。
         *
         * 为什么不做成"起一个服务"：补传是**顺带**的事（有一份清单欠着、而且离上次尝试
         * 够久了才真发请求），专门起一个服务只会多一份要管的东西。这里直接起一个
         * 短命的作用域干活；服务要是没活着，本地清单照样在，下次任何一次 tick 都会接着试。
         *
         * ⚠️ 与 `MeetingMicService` 里的那条路**共用** [syncGate]（进程级），
         * 所以"前台轮询"和"服务自己"不会同时传同一段（同一段被存两遍就是转写里出现两遍）。
         *
         * ⚠️ 这里走的是 [SyncScope.FINALIZE]（"把这场传完"），而**还在录的那一份会被
         * 自动降级**成"只传段、不收尾"（判据在 `OfflineMeetingSync.syncOne` 里）——
         * 前台轮询完全可能在用户还在录的时候被调到。
         */
        fun tickFromForeground(context: Context) {
            val app = context.applicationContext
            val store = MeetingDraftStore(File(app.filesDir, OFFLINE_DIR))
            val waits = store.pending()
            if (waits.isEmpty()) {
                // 没有欠的东西：把界面上那条"等联网上传"收掉（别留一条假的）
                if (_state.value.syncPhase == OfflinePhase.WAITING_UPLOAD ||
                    _state.value.syncPhase == OfflinePhase.UNREACHABLE ||
                    _state.value.syncPhase == OfflinePhase.DONE
                ) {
                    _state.value = _state.value.copy(
                        syncPhase = if (_state.value.active) OfflinePhase.RECORDING_LOCAL
                        else OfflinePhase.IDLE,
                        syncNote = "")
                    notifyIfRunning()
                }
                return
            }
            if (syncGate.running) return
            // 这一轮是**顺带**试：App 正在前台跑，所以按"此刻可达"算
            // （不可达时第一件事就是探活失败 → 退避，所以不会白转）
            if (!offlineSyncDue(reachableNow = true)) return
            CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
                runOfflineSync(app, store = store, manual = false, scope = SyncScope.FINALIZE)
            }
        }

        /**
         * 服务实例还活着时才刷通知（进程级那条补传路**可能**在服务没起来时跑 ——
         * 那时没有前台服务，也就没有通知可刷）。
         *
         * 为什么要单独一个静态入口：`notifyState()` 是实例方法（它要 `NotificationManager`），
         * 而补传的入口（`tickFromForeground`）是进程级的。**不**为了这一点把服务实例
         * 存成静态引用 —— 那是最经典的泄漏（服务没了还留着一个引用，里面还挂着麦）。
         */
        @Volatile private var liveService: MeetingMicService? = null

        private fun notifyIfRunning() {
            runCatching { liveService?.notifyStatePublic() }
        }

        /**
         * 补传的**不可重入守卫**（进程级，纯逻辑在 `RetryGuard` 里）。
         *
         * 为什么必须是进程级：补传有两个入口（服务自己、前台轮询），
         * 而"同一段传两次"在服务端看来就是两段音频（转写里同一句出现两遍），
         * 手机这边完全静默。守卫放进 `runOfflineSync` 里就是"谁跑谁占"。
         */
        private val syncGate = RetryGuard()

        // ---- 自动重试的节流（进程级；进程重启后退避从头开始，这是刻意的）----

        @Volatile private var syncAttempts = 0
        @Volatile private var syncNextAtMs = 0L

        /** 上一次探测是不是"不可达"（用来判"网络刚通"→ 立刻试 + 计数清零）。 */
        @Volatile private var syncWasUnreachable = false

        /**
         * 现在该不该再自动试一次。
         *
         * 判据：
         *  * 还欠着东西才试（调用方已经先查了 `pending()`）；
         *  * **可达性刚恢复** → 立刻试（`syncWasUnreachable` 就是"上一轮探活是连不上"）；
         *  * 否则等退避时间到。
         *
         * ⚠️ 这里**不**把 `reachable` 传成常量 true —— 那会让每一轮都命中"刚恢复"
         * 这条捷径、退避形同虚设（一分钟发几十次探活）。真相是：这一轮**还没探**，
         * 上一次探的结果是 [syncWasUnreachable]。
         */
        private fun offlineSyncDue(
            nowMs: Long = System.currentTimeMillis(),
            reachableNow: Boolean = false,
        ): Boolean = shouldRetryNow(
            nowMs = nowMs,
            attempts = syncAttempts,
            nextAtMs = if (syncNextAtMs <= 0) null else syncNextAtMs,
            wasUnreachable = syncWasUnreachable,
            reachable = reachableNow,
        )

        /**
         * 一轮结束之后更新退避状态（**单点**，别在各处散着改这三个字段）。
         *
         * [phase] 是 `OfflineMeetingSync` 给的结论：
         *  * [OfflinePhase.UNREACHABLE]：连不上 → 退避变长，并记下"上一轮不可达"
         *    （网络一恢复会命中"立刻试"那条捷径）；
         *  * [OfflinePhase.SERVER_BUSY] / [OfflinePhase.WAITING_MEETING_END]：**不是错**
         *    （服务端正录着别的会）→ 计数**清零**，但仍要等一段时间（别一秒问十次）；
         *  * 其余（含 DONE）：算一轮正常的推进 → 计数清零。
         */
        private fun syncAfterRound(phase: OfflinePhase) {
            when (phase) {
                OfflinePhase.UNREACHABLE -> {
                    syncAttempts += 1
                    syncNextAtMs = System.currentTimeMillis() + BackoffPolicy.delayMs(syncAttempts)
                    syncWasUnreachable = true
                }
                OfflinePhase.WAITING_MEETING_END -> {
                    syncAttempts = 0
                    syncNextAtMs = System.currentTimeMillis() + MEETING_END_POLL_MS
                    syncWasUnreachable = false
                }
                else -> {
                    syncAttempts = 0
                    syncNextAtMs = 0L
                    syncWasUnreachable = false
                }
            }
        }

        /**
         * 「ECHO 正录着别的会」时的复查间隔（1 分钟）。
         *
         * 为什么不按退避翻倍：那不是失败，只是"还没轮到我们"。而**等太久**是很糟的体验
         * —— 用户在电脑上按了「结束」，手机上却还要再等五分钟才开始传（而那一刻
         * 他正盯着手机）。一分钟探一次很便宜（一个 GET），也足够"录完就接着传"。
         */
        private const val MEETING_END_POLL_MS = 60_000L

        /**
         * 跑一轮同步（**唯一**的实现：服务的 [ACTION_SYNC]、前台 tick、以及
         * "每封一段顺手试一次"和"结束前的那一次"都走它）。
         *
         * 结果**如实**写进 `MicState.syncNote`/`syncPhase`：连不上、服务端拒绝、
         * 服务端正忙（等它录完再传）各说各的 —— 绝不能都说成"同步失败"。
         *
         * [scope] 决定这一轮做到哪一步（见 [SyncScope]）：录音中只做机会性上传，
         * 录完了才补齐 → 对账 → stop。
         */
        internal suspend fun runOfflineSync(
            app: Context,
            store: MeetingDraftStore = MeetingDraftStore(File(app.filesDir, OFFLINE_DIR)),
            manual: Boolean = false,
            onlyClientId: String? = null,
            scope: SyncScope = SyncScope.FINALIZE,
            /**
             * 占守卫时最多等多久（毫秒）。**只有"结束录音"那一下传非零值**：
             * 那一刻很可能正有一轮机会性上传在跑（每封一段就顺手试一次），
             * 而"收尾"是**不能等下一次 tick** 的（用户以为已经结束了，服务端却还没收到 stop）。
             * 等待是**有界**的：等不到就如实说"这一次没跑"（见 [stopRetryNote]），绝不假装传完。
             */
            waitForGateMs: Long = 0L,
        ): RetryOutcome {
            val baseUrl = PairStore.baseUrl(app)
            if (!PairStore.isPaired(app) || baseUrl.isNullOrBlank()) {
                // 没配对也能**录**（本地先写不依赖服务端），但传不上去 —— 如实说
                _state.value = _state.value.copy(
                    syncPhase = OfflinePhase.WAITING_UPLOAD,
                    syncNote = "还没配对：音频都在手机里，配对之后点「现在同步」就能传")
                return RetryOutcome.Ran
            }
            val block: suspend () -> Unit = {
                val transport = OfflineMeetingTransport(ApiClient.create(baseUrl))
                val active = ActiveDraft.current
                val result = OfflineMeetingSync.run(
                    transport = transport,
                    store = store,
                    activeClientId = active?.clientId,
                    onlyClientId = onlyClientId,
                    manual = manual,
                    force = manual,
                    scope = scope,
                )
                // 界面上那一档：**还在录**的时候一律显示"本机录音中"（此刻最要紧的事实），
                // 而"网通不通、传上去没传上去"由 note 与那几个数字说清（见 offlineStatusLine）。
                // 这一档替换**只影响展示**：退避仍然按真实的 `result.phase` 算（见 syncAfterRound）。
                val shown = if (_state.value.active) {
                    result.copy(phase = OfflinePhase.RECORDING_LOCAL)
                } else {
                    result
                }
                // 界面上那句"本地还留着多久"（**如实**：绝不早删，但也不会无限攒）
                val days = AppPrefs.audioRetentionDays(app)
                OfflineMeetingSync.publish(
                    shown.toState(
                        active = active,
                        retentionNote = OfflineMeetingRetention.retentionNote(days)))
                syncAfterRound(result.phase)
                val st = OfflineMeetingSync.state.value
                _state.value = _state.value.copy(
                    syncPhase = st.phase,
                    syncNote = offlineStatusLine(st).ifBlank { st.note },
                    syncPending = result.pendingSegments,
                    syncConfirmed = result.confirmedSegments,
                    syncDrafts = result.drafts,
                    sent = maxOf(_state.value.sent, result.confirmedSegments),
                    error = "",
                )
                // 同步这一轮过去了：**顺手**按有界保留策略清一次（只清"已对账 + 已收尾 + 到期"的）
                runCatching { purgeExpiredAudio(app, store) }
                notifyIfRunning()
            }
            val outcome = if (waitForGateMs > 0) {
                syncGate.runExclusiveWaiting(waitForGateMs) { block() }
            } else {
                syncGate.runExclusive { block() }
            }
            if (outcome == RetryOutcome.SkippedBusy) {
                // 如实：这一轮**没跑**（另一轮正占着），别让人以为"传完了"
                _state.value = _state.value.copy(
                    syncNote = if (manual) "上一次同步还在跑，这一次没跑（等它完再按）"
                    else _state.value.syncNote)
            }
            return outcome
        }

        /**
         * 「结束录音时那次收尾」等待守卫的上限（毫秒）。
         *
         * 为什么是 10 秒：那一刻在跑的通常是**一轮机会性上传**（最多传一小把段，秒级），
         * 10 秒足够它跑完；再长就让用户按了"结束"之后干等（那是更糟的体验）。
         * 等不到不是失败：会如实说"这次没跑、还有 N 段留着，下次会接着试"
         * （见 [stopRetryNote]），而且下一轮 tick 仍会把它传完。
         */
        const val FINALIZE_WAIT_MS = 10_000L

        /** 离线清单放在哪（**app 私有目录**：不依赖服务端，也不要外置存储权限）。 */
        const val OFFLINE_DIR = "offline-meetings"

        // ---------------------------------------------------------------- 有界保留（修②）

        /** 本地音频占用与场数（设置页那条"当前占用"；**纯读取**，不改任何东西）。 */
        data class LocalAudioUsage(
            /** 手上还有本地音频的场数。 */
            val drafts: Int,
            /** 这些音频一共多少字节。 */
            val bytes: Long,
            /** 已经对账通过（服务端收尾过）的场数 —— 这些才是保留策略管得到的。 */
            val uploadedDrafts: Int,
            /** 现在能立刻清掉的场数（已上传 + 已到期）。 */
            val expiredDrafts: Int,
            val retentionDays: Int,
        ) {
            val retentionLabel: String get() = OfflineMeetingRetention.label(retentionDays)
            val bytesText: String get() = RetentionPlan.humanBytes(bytes)
        }

        /** 这一次清理干了什么（**如实**：删了几场/几个文件/多少字节）。 */
        data class PurgeResult(
            val drafts: Int,
            val files: Int,
            val bytes: Long,
            /** 有清单被选中、但一个文件都没删掉时的原因（null = 都删掉了）。 */
            val failed: Int = 0,
        ) {
            val note: String
                get() = when {
                    drafts == 0 -> "没有到期的本地音频（未上传的一律不动）"
                    failed > 0 -> "清了 $drafts 场（$files 个文件，" +
                        RetentionPlan.humanBytes(bytes) + "），有 $failed 个文件没删掉"
                    else -> "已清理 $drafts 场已上传的本地音频（$files 个文件，" +
                        RetentionPlan.humanBytes(bytes) + "）"
                }
        }

        /** 共用一份清单目录口径（设置页那个"删除已上传的本地音频"入口也走它）。 */
        fun storeOf(context: Context): MeetingDraftStore =
            MeetingDraftStore(File(context.applicationContext.filesDir, OFFLINE_DIR))

        /** 设置页读它（占用大小 + 场数 + 保留档位）。 */
        fun localAudioUsage(context: Context): LocalAudioUsage {
            val store = storeOf(context)
            val drafts = runCatching { store.withAudio() }.getOrDefault(emptyList())
            val days = AppPrefs.audioRetentionDays(context)
            val plan = OfflineMeetingRetention.plan(
                drafts = drafts,
                nowMs = System.currentTimeMillis(),
                days = days,
                sizeOf = { OfflineMeetingRetention.audioBytes(store, it) },
            )
            return LocalAudioUsage(
                drafts = drafts.size,
                bytes = OfflineMeetingRetention.totalAudioBytes(store, drafts),
                uploadedDrafts = drafts.count { it.uploaded },
                expiredDrafts = plan.count,
                retentionDays = days,
            )
        }

        /**
         * 清掉"已经过期"的本地音频（**自动**那一路：只在补传成功之后顺手跑）。
         *
         * ⚠️ 判据一个都不在这里：该删哪些由 [OfflineMeetingRetention.plan] 算
         * （"绝不早删"：未上传的一律不动）。这里只做两件事 —— 算 + 删 + 如实回报。
         */
        internal fun purgeExpiredAudio(
            context: Context,
            store: MeetingDraftStore = storeOf(context),
        ): PurgeResult {
            val days = AppPrefs.audioRetentionDays(context)
            val drafts = runCatching { store.withAudio() }.getOrDefault(emptyList())
            val plan = OfflineMeetingRetention.plan(
                drafts = drafts,
                nowMs = System.currentTimeMillis(),
                days = days,
                sizeOf = { OfflineMeetingRetention.audioBytes(store, it) },
            )
            return purgePlan(store, plan)
        }

        /**
         * 用户按「删除已上传的本地音频」那一下（**手动**入口）。
         *
         * 与自动那一路**共用**同一个 plan 与同一个删除动作（[purgePlan]）：
         * 手动入口**不放松**判据 —— 它只删"已上传、已到期"的那些。
         * 为什么不给"立刻全删"的按钮：那等于把"绝不早删"这条铁律交给一个误触。
         */
        fun purgeUploadedNow(context: Context): PurgeResult {
            val store = storeOf(context)
            val days = AppPrefs.audioRetentionDays(context)
            val drafts = runCatching { store.withAudio() }.getOrDefault(emptyList())
            val plan = OfflineMeetingRetention.plan(
                drafts = drafts,
                nowMs = System.currentTimeMillis(),
                days = days,
                sizeOf = { OfflineMeetingRetention.audioBytes(store, it) },
            )
            return purgePlan(store, plan)
        }

        /** 真删（**唯一**的删除动作；plan 决定删哪些）。 */
        private fun purgePlan(store: MeetingDraftStore, plan: RetentionPlan): PurgeResult {
            var files = 0
            var failed = 0
            var freed = 0L
            plan.items.forEach { item ->
                val before = item.draft.segments.count {
                    store.segmentFile(item.draft.clientId, it.seq).isFile
                }
                val out = store.purgeAudio(item.draft)
                files += out.files
                // 释放量按**真的删掉的那几个文件**算（不是 plan 估的那个数）——
                // 估的数里可能包含已经不在的文件，报出去就是虚的
                freed += out.bytes
                failed += (before - out.files)
            }
            return PurgeResult(drafts = plan.count, files = files, bytes = freed, failed = failed)
        }

        /**
         * 界面读这个（**不需要 bind** —— 全屏录音界面在"手机麦"这条路上显示**本地**电平，
         * 比电脑那条隔一跳网络的顺得多）。
         */
        private val _state = MutableStateFlow(MicState())
        val state: StateFlow<MicState> = _state.asStateFlow()
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var record: AudioRecord? = null

    // ---- 这一场录音的现场（**进程被杀就没了；事实都在清单里**）----

    /**
     * 这一场的**本地**那一半（编码 + 落盘 + 清单）。
     *
     * 为什么它是这条路的本体：用户 2026-10-17 把在线/离线合并成一条之后，
     * "录下来"这件事只有一个实现 —— 先写本地（[MeetingRecordSession]），
     * 传上去是后面的事。于是这里没有"这一场是联网还是离线"这种字段了：
     * 网络只影响"顺手传的那一下"成不成。
     */
    @Volatile private var session: MeetingRecordSession? = null

    /** 上一次"谁结束了录音"（调试与文案上下文用，见 [stopEverything]）。 */
    @Volatile private var lastStopReason: String = ""

    /** 这一场已经有多少段退回了 WAV（界面/通知要如实说"不是每段都是 FLAC"）。 */
    @Volatile private var wavSegments = 0

    private val draftStore: MeetingDraftStore by lazy {
        MeetingDraftStore(File(filesDir, OFFLINE_DIR))
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // 进程级补传（前台 tick / 听写服务那条）要能刷通知 —— 只在服务活着时有意义。
        // 用 onCreate/onDestroy 成对维护，**不**用静态强引用兜着（见 notifyIfRunning 的说明）。
        liveService = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            ACTION_STOP -> stopEverything("用户结束了手机录音")
            ACTION_PAUSE -> {
                session?.beginPause()
                _state.value = _state.value.copy(paused = true)
                notifyState()
            }
            ACTION_RESUME -> {
                // 恢复：收一条暂停区间，并**顺手上报**（用户 2026-10-17 的第③件事：
                // "每新增一段暂停区间后上报一次"）。上报走同步那一轮（它天然带 start + 幂等）。
                session?.endPause()
                _state.value = _state.value.copy(paused = false)
                notifyState()
                reportPausesSoon()
            }
            ACTION_SYNC -> scope.launch {
                runOfflineSync(this@MeetingMicService, manual = true, scope = SyncScope.FINALIZE)
            }
            else -> {
                // 被系统重启带回来的 null intent：**不自动开麦**（与听写服务同一条纪律）
                stopSelf()
                return START_NOT_STICKY
            }
        }
        return START_STICKY
    }

    /**
     * 开始录音（用户 2026-10-17 合并两条路之后的形状）。
     *
     * ## 为什么这里**不探活、不建会、不看服务端脸色**
     *
     * 用户原话：*"离线录音和在线录音就没什么本质区别了"*。于是"开始录音"只做一件事：
     * **在本地把这一场立起来**（清单 + 第一段以后逐段落盘）。
     * 好处不只是"断网也能录"（那是 2026-10-16 就有的），更是**行为一致**：
     * 网络好不好都是同一条代码路径，没有"探活超时 1.5 秒才决定走哪条路"这种分叉，
     * 也没有"联网那条先在电脑上开一场会、失败了再退到离线"这种会留下半场会的中间态。
     *
     * 唯一还能让"开始录音"失败的东西是**麦克风权限**（那是真的不能录）。
     */
    private fun startRecording() {
        if (job != null) return
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            failAndStop("没有麦克风权限：先在 App 里给它")
            return
        }
        // 上一条**被进程杀掉**留下的清单：按最后一段的时间收尾（如实标出这是推断），
        // 它接下来会被后台补传捡走 —— 别让它永远停在"还在录"（那种清单谁都不敢收尾）
        closeInterruptedDrafts()

        val now = System.currentTimeMillis()
        val s = MeetingRecordSession(
            store = draftStore,
            encoder = FlacSegmentEncoder(SAMPLE_RATE, CHANNELS),
            sampleRate = SAMPLE_RATE,
            channelCount = CHANNELS,
        )
        val started = s.start(MeetingDraftStore.newClientId(), MeetingDraftStore.isoOf(now))
        session = s
        wavSegments = 0
        if (started.note.isNotBlank()) syncNote(started.note)
        startForegroundCompat()
        _state.value = MicState(
            active = true,
            syncPhase = OfflinePhase.RECORDING_LOCAL,
            syncNote = offlineStatusLine(OfflineSyncState(
                phase = OfflinePhase.RECORDING_LOCAL,
                clientId = started.draft.clientId,
                totalSegments = started.draft.segments.size,
                pendingSegments = started.draft.pendingSegments,
                confirmedSegments = started.draft.confirmedSegments,
            )),
            startedAt = started.draft.startedAt,
        )
        notifyState()
        job = scope.launch { runLoop() }
    }

    /**
     * 把**上一条被进程杀掉**的清单收尾：结束时间按最后一段推（**如实标出这是推断**）。
     *
     * 为什么要在这里做：那种清单的 `ended_at` 是空的 → 它会被这条路的规则判成"还在录"
     * → **永远不许收尾**（见 `SyncScope`）→ 音频永远传不到服务端、永远不出文字。
     * 而进程已经被杀过一次，那场录音**确实已经结束**了（结束时刻只能推断）。
     */
    private fun closeInterruptedDrafts() {
        val unfinished = runCatching {
            draftStore.list().filter { it.recording && it.segments.isNotEmpty() }
        }.getOrDefault(emptyList())
        unfinished.forEach { d ->
            val last = d.segments.maxByOrNull { it.seq } ?: return@forEach
            val endIso = MeetingDraftStore.isoOf(
                (MeetingDraftStore.epochOf(last.startedAt) ?: System.currentTimeMillis()) +
                    (last.durationSeconds * 1000).toLong())
            // 结束时间**是按最后一段推出来的**（不是当时真的按了结束）—— 用**结构化**的
            // `endedAtInferred` 记下来（而不是塞一句备注）：它必须能被每一次同步如实说出来
            draftStore.save(d.copy(endedAt = endIso, endedAtInferred = true))
            syncNote("上一条录音（${d.segments.size} 段）是被打断的：结束时间按最后一段推的" +
                "（不是当时按的结束），剩下的段会继续传")
        }
    }

    private suspend fun runLoop() {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return failAndStop("这台设备给不出可用的录音缓冲")
        val ar = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, minBuf * 2)
        } catch (e: Exception) {
            return failAndStop("打不开麦克风：${e.message}")
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            runCatching { ar.release() }
            return failAndStop("麦克风初始化失败：可能被别的应用占着")
        }
        runCatching { InputDevices.preferredInput(this)?.let { ar.setPreferredDevice(it) } }
        record = ar

        val frame = ShortArray(FRAME_SAMPLES)
        // 每段多长**读偏好**（设置页那三个 15/30/60）；开录时读一次，
        // 中途改偏好不会把已经在缓冲的那半段切碎（界面文案写着"改完下次开录生效"）
        val samplesPerSegment = SAMPLE_RATE * segmentSeconds(this)
        // 缓冲的容量按"装得下一整段"给（`ArrayList` 自己会增长，这个初值只是免掉几次扩容）；
        // 段长由硬件帧长整除（100ms 帧 vs 15/30/60 秒），所以一个缓冲里通常只有一段。
        val buffer = ArrayList<Short>(samplesPerSegment)
        val capture = MicCaptureLoop(samplesPerSegment)
        try {
            ar.startRecording()
            notifyState()
            while (currentCoroutineContext().isActive) {
                val n = ar.read(frame, 0, frame.size)
                if (n <= 0) {
                    delay(20)
                    continue
                }
                val paused = _state.value.paused
                // 电量条：暂停时**不动它**（上一帧的值留着往下走），而不是每 100ms 报一次 0
                // —— 零帧也要照常计数/封段，但界面不必跟着抖。
                if (!paused) _state.value = _state.value.copy(level = rms(frame, n))
                // 采集语义全在这一句里（纯逻辑在 [MicCaptureLoop]）：暂停时它塞进去的是
                // **与这一帧同长度、同格式的全零样本**，而"够一段就封段"那件事照样发生
                // —— 所以段边界仍落在挂钟刻度上、总时长照常累计。
                // 于是"第 N 段 = 挂钟第 N 个段长（默认 30 秒）"这条对齐关系在暂停前后都成立。
                val feed = capture.feed(buffer, frame, n, active = !paused)
                if (feed.sealed) {
                    val pcm = toPcmBytes(buffer)
                    // ① **先落本地**（编码 + 写盘 + 记清单）—— 这一步不碰网络，
                    //    所以它绝不会因为"网断了"而失败；它失败只有一种原因：手机写不进去。
                    //    ⚠️ 暂停期间的段也会走到这里（里面是零样本），照常编码/落盘：全零音频
                    //    编出来还是全零，而"少一段"会让整条时间轴前移（那正是要避免的）。
                    val seal = session?.seal(pcm)
                    if (seal != null) publishSeal(seal)
                    // ② 顺手试一次上传（**异步、绝不阻塞采音**：`ar.read` 是同一个协程，
                    //    一次卡住的网络请求会把麦克风缓冲顶掉 —— 那是**真的丢音频**）
                    triggerOpportunisticUpload()
                    buffer.clear()
                }
            }
        } catch (e: Exception) {
            _state.value = _state.value.copy(error = "采集出错：${e.message}")
        } finally {
            runCatching { ar.stop() }
            runCatching { ar.release() }
            record = null
        }
    }

    /**
     * 一段封存之后的**如实发布**（界面/通知那一行）。
     *
     * 三件事都要说清楚（每一件都对应一次"看着没事"的错法）：
     *  1. **有几段还没传上去**（`syncPending`）—— 这是"音频会不会丢"的唯一出口；
     *  2. **这一段真的用了什么格式**：退回 WAV 时必须说"第 N 段用 WAV（FLAC 编码器不可用）"
     *     （用户 2026-10-17 点名要求），而编成 FLAC 时**什么都不说**（别用套话占住那一行）；
     *  3. **真的可能丢音频时以 error 优先**（"有一段缓存不下来"绝不能被"已缓存 3 段"盖掉）。
     */
    private fun publishSeal(seal: SegmentSeal) {
        val prev = _state.value
        _state.value = prev.copy(
            syncPhase = OfflinePhase.RECORDING_LOCAL,
            syncPending = seal.draft?.pendingSegments ?: prev.syncPending,
            syncConfirmed = seal.draft?.confirmedSegments ?: prev.syncConfirmed,
            sent = maxOf(prev.sent, seal.draft?.confirmedSegments ?: 0),
            formatNote = seal.note.ifBlank { prev.formatNote },
            error = seal.error.ifBlank { prev.error },
            syncNote = if (prev.syncNote.isBlank()) {
                offlineStatusLine(OfflineSyncState(
                    phase = OfflinePhase.RECORDING_LOCAL,
                    totalSegments = seal.draft?.segments?.size ?: 0,
                    pendingSegments = seal.draft?.pendingSegments ?: 0,
                    confirmedSegments = seal.draft?.confirmedSegments ?: 0,
                ))
            } else prev.syncNote,
        )
        if (seal.error.isNotBlank()) {
            android.util.Log.e(TAG, "会议段封存失败：${seal.error}")
        }
        if (seal.note.isNotBlank()) {
            // 日志里也留一份（用户要求"界面/日志如实体现"）：真机排障时只能靠它
            android.util.Log.w(TAG, "会议段格式：${seal.note}")
        }
        notifyState()
    }

    /**
     * **顺手试一次上传**（每到一段封存之后调）。
     *
     * ## 为什么是"异步 + 有节流 + 不排队"
     *
     *  * **异步**：它在采集协程里被调用，而 `ar.read` 是同一个协程 —— 一次卡住的网络请求
     *    会把麦克风缓冲顶掉（**真的丢音频**）。所以只 `launch`，绝不 `await`；
     *  * **有节流**（[offlineSyncDue]）：连不上时按退避（2s → 4s → … → 5 分钟）再试，
     *    不会每一段都去撞一次墙；
     *  * **不排队**：上一轮还在传（`syncGate` 占着）就**直接跳过**这一次 ——
     *    段已经安全躺在磁盘上了，下一段封存时再试，或者用户按「现在同步」。
     *    排队等反而会让一串上传请求堆在录音协程后面（那是"边录边传"最不该有的形状）。
     */
    private fun triggerOpportunisticUpload() {
        if (syncGate.running) return
        if (!offlineSyncDue(reachableNow = true)) return
        scope.launch {
            runOfflineSync(this@MeetingMicService, store = draftStore,
                manual = false, scope = SyncScope.INCREMENTAL)
        }
    }

    /**
     * 「新增了一段暂停区间」之后上报一次（用户 2026-10-17 的第③件事）。
     *
     * 走的是同一轮同步（它天然带 `start`，而暂停上报就挂在 start 成功之后）——
     * 于是"按 clientId 幂等 + 服务端合并去重"这条契约只需要在一处实现。
     *
     * ⚠️ 用 [SyncScope.INCREMENTAL]：这时通常**还在录**，绝不能顺手把这一场收尾了。
     */
    private fun reportPausesSoon() {
        if (syncGate.running) return
        scope.launch {
            runOfflineSync(this@MeetingMicService, store = draftStore,
                manual = false, scope = SyncScope.INCREMENTAL)
        }
    }

    private fun stopEverything(reason: String) {
        // 为什么要把这个原因记下来：结束时还静音着、或补传没传完，这两件事都要
        // 写在状态里让人看懂 —— 而那两句话的**上下文**就是"谁结束了录音"
        // （用户按的 / 通知里的结束 / 服务被系统收掉）。别让它变成一个没人读的参数。
        lastStopReason = reason
        val j = job
        job = null
        scope.launch {
            if (j != null) j.cancelAndJoin()

            // ① **本地收尾立刻生效**：结束时间 + 总时长落盘（结束时还静音着的话，
            //    最后那段暂停也在这里收进清单 —— 否则会凭空多出一段"没人说话"的时长）。
            //    为什么先做这一步再传：用户的"结束"必须**当场**生效（本地不再采音、
            //    清单落盘），绝不能押在一次网络请求上（断网时那会变成"按了结束没反应"）。
            val done = runCatching { session?.finish() }.getOrNull()
            _state.value = _state.value.copy(
                active = false, level = 0f, paused = false,
                syncPhase = if (done != null && !done.uploaded) OfflinePhase.WAITING_UPLOAD
                else _state.value.syncPhase,
                syncPending = done?.pendingSegments ?: _state.value.syncPending,
                syncNote = offlineStatusLine(OfflineSyncState(
                    phase = if (done != null && !done.uploaded) OfflinePhase.WAITING_UPLOAD
                    else _state.value.syncPhase,
                    totalSegments = done?.segments?.size ?: 0,
                    pendingSegments = done?.pendingSegments ?: 0,
                    confirmedSegments = done?.confirmedSegments ?: 0,
                    note = "录完了，正在补齐剩下的段并让 ECHO 收尾",
                )),
            )
            notifyState()

            // ② **补齐剩余段 → 对账 → stop(clientId)**（这条路唯一的收尾动作）。
            //    为什么要**等**正在跑的那一轮（`waitForGateMs`）：每封一段都会顺手试一次上传，
            //    而"结束"很可能正好撞上它 —— 直接跳过的话，这一场就要等到下一次 tick
            //    才被收尾（用户以为已经结束了，服务端却还没收到 stop = **还没有转写**）。
            //    等待是**有界**的（见 FINALIZE_WAIT_MS）：等不到就如实说"这一次没跑"。
            val outcome = runCatching {
                runOfflineSync(this@MeetingMicService, store = draftStore,
                    manual = true, scope = SyncScope.FINALIZE,
                    waitForGateMs = FINALIZE_WAIT_MS)
            }.getOrDefault(RetryOutcome.Ran)
            if (outcome == RetryOutcome.SkippedBusy) {
                // 如实：这次收尾**没跑**（另一轮占着），还有几段留着 —— 绝不假装传完了
                val left = session?.draft?.pendingSegments ?: 0
                stopRetryNote(RetryOutcome.SkippedBusy, left).takeIf { it.isNotBlank() }?.let { note ->
                    _state.value = _state.value.copy(syncNote = note)
                    notifyState()
                }
            }
            stopForegroundCompat()
            stopSelf()
        }
    }

    /**
     * 「结束前那次收尾」等待守卫的上限（毫秒）。见 [MeetingMicService.Companion.FINALIZE_WAIT_MS]。
     */
    private fun failAndStop(msg: String) {
        _state.value = MicState(active = false, error = msg)
        notifyState()
        stopForegroundCompat()
        stopSelf()
    }

    /** 记一句这条路的原因（不覆盖 phase）。 */
    private fun syncNote(note: String) {
        _state.value = _state.value.copy(syncNote = note)
    }

    override fun onDestroy() {
        runCatching { record?.release() }
        record = null
        if (liveService === this) liveService = null
        // 服务正在消失：把守卫放掉（幂等）。被取消的那一轮即使来不及走 finally，
        // 也不会让下一场录音里这个守卫一直是"在跑"。
        syncGate.leave()
        // ⚠️ **刻意不清 [ActiveDraft]/[session]**：进程可能只是被系统回收，清单与音频都还在磁盘上，
        // 下一次 `tickFromForeground` / 开录时会重新读出来。清掉反而会让"接着传"失去判据。
        scope.cancel()
        super.onDestroy()
    }

    // ---- 通知（前台服务的"可见性"，Android 14 的硬要求）----

    private fun startForegroundCompat() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "会议录音（手机麦克风）",
                    NotificationManager.IMPORTANCE_LOW))
        }
        val n = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION") stopForeground(true)
        }
    }

    private fun notifyState() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
        runCatching { nm.notify(NOTIF_ID, buildNotification()) }
    }

    /** 进程级那条补传路刷通知的入口（**只**在服务活着时被调用）。 */
    internal fun notifyStatePublic() {
        notifyState()
    }

    private fun buildNotification(): Notification {
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        val pi = PendingIntent.getActivity(this, 0, intent, flags)
        val stopPi = PendingIntent.getService(
            this, 1,
            Intent(this, MeetingMicService::class.java).setAction(ACTION_STOP), flags)
        // 「现在同步」也放进通知：用户看到"等联网上传"时，唯一想做的就是让它立刻试一次
        val syncPi = PendingIntent.getService(
            this, 2,
            Intent(this, MeetingMicService::class.java).setAction(ACTION_SYNC), flags)
        val b = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("ECHO 会议录音")
            .setContentText(notificationText(_state.value))
            .setOngoing(true)
            .setContentIntent(pi)
        if (_state.value.syncPending > 0) b.addAction(0, "现在同步", syncPi)
        return b.addAction(0, "结束", stopPi).build()
    }
}

/**
 * 采集循环里那唯一一句语义判断（**纯逻辑，JVM 逐条可测**）：**暂停 = 照常计数，但写全零**。
 *
 * 用户 2026-10-16 深夜推翻了旧的"暂停不写盘"口径，并选了"手机与电脑统一"：
 * *"暂停区间就是空白录音，不影响会议总时长，不用剃掉"*（电脑侧 `app/audio/recorder.py`
 * 已经改完并验过：暂停时把这一块换成 `np.zeros(...)`、走**同一行写盘**、`meta["pauses"]`
 * 只是纯标注）。手机这一侧原来是"丢帧"（`continue`）—— 这里把它改成同一口径。
 *
 * ## 为什么必须逐字连续（这条最容易改错）
 *
 * 段是按**累计写进去的样本数**切的（`buffer.size >= samplesPerSegment`）。暂停要是"丢帧、
 * 不计数"，后面所有段的边界就**整体前移**：一场 40 分钟、中间暂停 5 分钟，音频只有 35 分钟，
 * "第 N 段 = 挂钟第 N 个段长"这条对齐关系当场失效（转写时间轴与音频就再也对不上）。
 * 所以暂停期间**一个样本都不能少**：拿全零补齐，样本数照常累积、`seal` 照常触发。
 *
 * ## 为什么是零，而不是"接着录真实声音"
 *
 * 暂停就是"这段时间不留内容"。录真实声音等于暂停白按了（用户要的是"这段时间不会录进会议"）。
 * 而**不做任何剪切/补偿**是用户明确说的"不用剃掉"—— 转写里那一段没有文字，是因为
 * **音频本身就是空白**，不是因为我们事后去剪了它。
 *
 * ## 为什么全零帧也要照常封段
 *
 * 少封一段 = 少一段音频 = 总时长变短、后面的段号与挂钟错位。全零音频编码出来还是全零
 * （FLAC/WAV 都无损、也都允许全零帧），所以"照常封段"不会在服务端造成任何异常输入。
 *
 * ## 帧长与段长是整除的
 *
 * 真机上帧长固定 100ms（1600 样本），而合法段长是 15/30/60 秒（240000/480000/960000 样本）
 * —— 全部整除。所以"攒够一段就封"落下去的每一段**恰好是 `samplesPerSegment` 个样本**，
 * 段边界与挂钟刻度一一对应（这条让判据能写成"第 N 段 = 挂钟第 N 个段长"）。
 */
internal class MicCaptureLoop(
    /** 这一场的每段样本数（采样率 × 段秒数；**暂停不改变它**）。 */
    private val samplesPerSegment: Int,
) {
    init {
        require(samplesPerSegment > 0) { "每段样本数必须为正：$samplesPerSegment" }
    }

    /**
     * 喂进**一帧**（`ar.read` 的一次结果），可能因此攒满一段。
     *
     * [active] = false（暂停）时：源不是麦克风那一帧，而是**与之同长度的零样本**
     * —— 长度与格式都不变（PCM 16-bit，零样本就是全零字节），只有内容变成了零。
     *
     * ⚠️ **满了也不清缓冲**：`sealed = true` 是"**现在去封这一段**"的指令，
     * 而封段的人（`runLoop`）必须先读 `buffer` 拿到那一段 PCM、封完再自己 `clear()`
     * （这里顺手清掉的话，那一段就永远丢了 —— 这正是"看着没事"的错法）。
     *
     * 帧是**整帧加进去**的（与旧代码同一形状，于是**一个样本都不丢**）：真机上 100ms 帧
     * （1600 样本）能整除每个合法段长（15/30/60 秒 = 240000/480000/960000 样本），
     * 所以"封下去的每一段恰好是 `samplesPerSegment` 个样本"始终成立。
     * 万一参数变成不整除（帧比一段长），行为仍与旧代码一致：样本照收，段长最多多出一帧的量。
     */
    fun feed(
        buffer: ArrayList<Short>,
        frame: ShortArray,
        n: Int,
        active: Boolean,
    ): MicFeed {
        var added = 0
        var sealed = false
        var left = n
        while (left > 0) {
            val take = minOf(left, samplesPerSegment)
            // 真音频就照抄那一段；暂停就塞**同样多个零样本** —— 帧长与格式一个都不变
            if (active) buffer.addAll(frame.asList().subList(n - left, n - left + take))
            else repeat(take) { buffer.add(0) }
            added += take
            left -= take
            if (buffer.size >= samplesPerSegment) {   // 攒够一段：**告诉调用方去封**（暂停期间也照样封）
                sealed = true
                break
            }
        }
        return MicFeed(added = added, sealed = sealed)
    }

    /** [feed] 的结局：这一帧贡献了几个样本、要不要现在就封一段。 */
    data class MicFeed(val added: Int, val sealed: Boolean)
}

/**
 * 通知里那一行字（**纯函数**，JVM 可测）。
 *
 * 为什么值得单独抽出来并有判据：这一行是"录音到底有没有丢东西"的**唯一**出口
 * （用户不在界面里时只看得到它）—— 优先级写错了就会在最该说话的时候沉默：
 *  * 有**错误**先说错误（"有一段缓存不下来，可能真的丢了"绝不能被"在录 · 已送 3 段"盖掉）；
 *  * **录音已经结束**时先说"还在往 ECHO 传"（那时最怕用户以为"关了就完了"）；
 *  * 静音其次（但**有段没传上去**比静音那句更要紧：那关系到音频会不会丢）；
 *  * 然后是"已缓存 N 段 · 等联网上传"（[MeetingMicService] 那条如实的同步状态）；
 *  * 再是"还有 N 段没传上去"；
 *  * 最后才是"一切正常"。
 *
 * ⚠️ 需求里点名的口径在这里落成一句话：**没传上去绝不说"已上传"**。
 * 所以还没对账的那些档只会出现"已缓存 / 等联网上传 / 还没传"，不出现"已上传"
 * （唯一说"已传/已上传"的地方是段被服务端认下之后，以及 DONE 那一档的真对账通过）。
 */
internal fun notificationText(st: MicState): String = when {
    st.error.isNotBlank() -> st.error
    // 录完了但还在传：这时最怕用户以为"已经完事了"（其实还没出文字）
    !st.active -> "录音已结束 · " + st.syncNote.ifBlank { "正在把剩下的段传给 ECHO…" }
    // 有段没传上去 + 有真实原因（连不上 / 服务端拒绝）：先说它（那关系到音频会不会丢）
    st.syncPending > 0 && st.syncNote.isNotBlank() -> st.syncNote
    st.paused -> "已静音（这段不会录进会议）"
    st.syncPending > 0 -> "本机录音中 · 已缓存 ${st.syncPending} 段 · 等联网上传"
    else -> "在录（手机麦克风）· 已传 ${st.sent} 段"
}

/** 手机录音那条路的状态（界面读它）。 */
data class MicState(
    val active: Boolean = false,
    val paused: Boolean = false,
    /** 本地电平（0..1）—— **这条路上电平是本地算的**，比电脑那条路顺得多。 */
    val level: Float = 0f,
    /** 服务端**已认下**的段数（界面那一行"已传 N 段"）。 */
    val sent: Int = 0,
    val error: String = "",

    // ---- 音频"传上去没有"（**唯一**的出口是 syncNote / offlineStatusLine）----

    /** 这条路现在到哪一档（本机录音中 / 等联网 / 在传 / 等服务端 / 对账完成…）。 */
    val syncPhase: OfflinePhase = OfflinePhase.IDLE,
    /** 要显示的那行字（**唯一**出口：`offlineStatusLine`）。 */
    val syncNote: String = "",
    /** 还有几段**本地音频没传上去**（>0 就该出现「现在同步」；这也是"会不会丢"的唯一判据）。 */
    val syncPending: Int = 0,
    /** 那几段里已经被服务端认下几段（界面用来说"已传 M 段"）。 */
    val syncConfirmed: Int = 0,
    /** 有几场录音在等（可能不止一场：连着录了两场，第一场还没传完）。 */
    val syncDrafts: Int = 0,

    /**
     * 这一场是什么时候开始的（ISO 本地时间；用它算大计时）。
     *
     * 为什么永远有值：合并成一条路之后，**开始时间永远是手机自己记的**
     * （`start` 那一刻的本地时间）—— 不再有"电脑给了 startedAt 才有值"那一档，
     * 所以大计时不会出现按哪个分支取值的分叉。
     */
    val startedAt: String = "",

    // ---- 段的格式（用户 2026-10-17 的第①件事：FLAC，编不出来退回 WAV）----

    /**
     * 「这一段用 WAV（FLAC 编码器不可用）」这类**如实**的一句话（空 = 一切按预期）。
     *
     * 为什么要有它：用户点名要求"在界面/日志如实体现"。只说"已缓存 3 段"是不够的
     * —— 用户会以为每段都是 FLAC（体积减半），而实际可能整场都退回了 WAV
     * （这句话里带着"本场已有 N 段用 WAV"的累计，见 `segmentFormatNote`）。
     */
    val formatNote: String = "",
) {
    /** 正在补传（界面据此把「现在同步」按钮换成"正在…"，别让人连点）。 */
    val syncBusy: Boolean get() = syncPhase == OfflinePhase.UPLOADING
}

/**
 * 一轮同步（[MeetingMicService.runOfflineSync] / `RetryGuard`）的结局。
 *
 * 为什么要有"没跑"这一档，而不是只回 true/false：调用方需要**如实**告诉用户
 * "这一次没跑，因为另一轮正在跑" —— 只说"没成功"会让人以为音频又丢了一段。
 */
internal enum class RetryOutcome {
    /** 真跑了（成没成看这份清单的 `pendingSegments`：>0 就是还有没送出去的段）。 */
    Ran,

    /** **没跑** —— 另一轮已经占着守卫（不排队、不等待）。 */
    SkippedBusy,
}

/**
 * 「这一轮同步是不是我在跑」的**唯一**判据（纯逻辑，JVM 可测）。
 *
 * 为什么值得单独一个类并有判据：同一段音频被传两次，电脑侧就多存一段
 * （转写里同一段出现两遍），而这件事在手机这边**完全静默** —— 界面照样显示"传完了"。
 * 所以"不可重入"必须是**一处**说了算的：谁跑谁占（[runExclusive]），
 * 并发的第二个调用者**立刻**拿到 [RetryOutcome.SkippedBusy]。
 *
 * 而[runExclusiveWaiting] 是同一个守卫的**有界等待**版本：只有"结束录音时那一次收尾"
 * 用它 —— 那一刻很可能正有一轮机会性上传在跑，而"收尾"是**不能等下一次 tick** 的
 * （用户以为已经结束了，服务端却还没收到 stop = 还没有转写）。等待有上界，
 * 等不到就如实回 [RetryOutcome.SkippedBusy]（绝不假装传完了）。
 */
internal class RetryGuard {

    /**
     * 用原子量而不是 `@Volatile Boolean`：`判断 + 占用`必须是**一步**（CAS），
     * 否则两个线程可以同时读到"没人占"然后都进去 —— 那正是要挡的那个竞态。
     */
    private val busy = AtomicBoolean(false)

    /** 现在有没有人占着。**只用来观察/写用例**，别拿它当"我要不要进去"的判据（那是 TOCTOU）。 */
    val running: Boolean get() = busy.get()

    /** 进得去回 true；已经有人占着就回 **false**（调用方据此如实说话）。 */
    fun tryEnter(): Boolean = busy.compareAndSet(false, true)

    /** 放掉。放掉一个没被占的守卫是无害的（幂等）。 */
    fun leave() {
        busy.set(false)
    }

    /**
     * 占着守卫跑一轮 [block]：进得去就 `Ran`，别人占着就**立刻** `SkippedBusy`（不排队）。
     *
     * `try/finally` 是硬要求：上传抛异常时也必须放掉 —— 否则一次失败就把同步**永久锁死**
     * （「现在同步」从此按了没反应，而待传段数一直不降，用户无路可走）。
     */
    suspend fun runExclusive(block: suspend () -> Unit): RetryOutcome {
        if (!tryEnter()) return RetryOutcome.SkippedBusy
        try {
            block()
            return RetryOutcome.Ran
        } finally {
            leave()
        }
    }

    /**
     * 等守卫空出来再跑（最多等 [timeoutMs]，每 [stepMs] 看一次）。
     *
     * 为什么要单独有这一档（只在"结束录音"那一次收尾用）：那一刻正跑着的那一轮是
     * **机会性上传**（每封一段都会顺手试一次），它总会结束（最坏就是这一轮的段都传完）；
     * 而如果直接 [runExclusive] 被挡，这一场的收尾就要等到下一次 tick ——
     * 用户以为已经结束了，服务端却还没收到 stop（= **还没有转写**）。
     * 等不到（超时）仍然如实回 [RetryOutcome.SkippedBusy]：宁可说"这次没跑、下次接着试"，
     * 也绝不假装传完了。
     */
    suspend fun runExclusiveWaiting(
        timeoutMs: Long,
        stepMs: Long = 100L,
        block: suspend () -> Unit,
    ): RetryOutcome {
        val deadline = System.currentTimeMillis() + maxOf(0L, timeoutMs)
        while (!tryEnter()) {
            if (System.currentTimeMillis() >= deadline) return RetryOutcome.SkippedBusy
            delay(stepMs)
        }
        try {
            block()
            return RetryOutcome.Ran
        } finally {
            leave()
        }
    }
}

/**
 * 「结束录音时那次收尾」的结局 → 如实的一句话（**纯函数**，JVM 可测）。
 *
 * 为什么单独抽出来：这一轮**可能被另一轮同步占着而根本没跑**（机会性上传正好在跑，
 * 而等待也有上界）。什么都不说的话，用户看到的是"我按了结束、待传的数字还在"，
 * 却分不清是"没跑"还是"传失败了"（说错原因会把人引去查网络）。
 * 返回空串 = 没有欠的段，没什么要说的（别用套话占住界面上那一行）。
 */
internal fun stopRetryNote(outcome: RetryOutcome, left: Int): String = when {
    left <= 0 -> ""
    outcome == RetryOutcome.SkippedBusy ->
        "结束前的补传被另一轮重传占着，这一次没跑：还有 $left 段留在手机里（下次开录会接着试）"
    else ->
        "结束前的补传没传完：还有 $left 段留在手机里（下次开录会接着试）"
}

/** 一段 PCM（短整型）→ 小端字节。纯函数，JVM 可测。 */
internal fun toPcmBytes(samples: List<Short>): ByteArray {
    val out = ByteArray(samples.size * 2)
    for (i in samples.indices) {
        val v = samples[i].toInt()
        out[i * 2] = (v and 0xFF).toByte()
        out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
    }
    return out
}

/** 这一帧的 RMS（0..1 的粗电平，用于界面动效）。纯函数，JVM 可测。 */
internal fun rms(frame: ShortArray, n: Int): Float {
    if (n <= 0) return 0f
    var sum = 0.0
    for (i in 0 until n) {
        val v = frame[i] / 32768.0
        sum += v * v
    }
    return (sqrt(sum / n) * 3.0).coerceIn(0.0, 1.0).toFloat()
}

/** 通知里那句时间戳（调试/对齐用）。 */
internal fun stamp(now: Long = System.currentTimeMillis()): String =
    SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(now))
