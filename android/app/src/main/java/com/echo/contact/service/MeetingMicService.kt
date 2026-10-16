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
import com.echo.contact.audio.InputDevices
import com.echo.contact.audio.WavWriter
import com.echo.contact.data.ApiClient
import com.echo.contact.data.EchoApi
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
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
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
 * ## 两条路：联网那条，与**离线那条**（用户 2026-10-16 加的需求）
 *
 * 用户原话：*"手机如果连不上 echo 也可以进入离线状态，离线状态可以开始录音，结束录音，
 * 录音文件缓存在 app，网络通了就可以传上去"*。
 *
 *  * **联网**（[RecordPlan.Online]）：与改动前**完全一致** —— 先在电脑上开一场"手机麦"会议，
 *    再按段传；传不出去的段进"待重传"（`retryPending`）。
 *  * **离线**（[RecordPlan.Offline]）：**完全本地**。段写进 app 私有目录
 *    （`filesDir/offline-meetings/<clientId>/`）+ 一份清单（[MeetingDraft]）；
 *    结束录音也不碰服务端。网络恢复后由 [OfflineMeetingSync] 按序补传、对账。
 *
 * 为什么"连不上"不能是失败：会议室里没网是常态，而**会议录音一旦没录下来就永远补不回来**。
 * 所以采音这件事**绝不能依赖服务端** —— 这正是这一条路存在的全部理由。
 *
 * ## 三个刻意的取舍
 *
 *  1. **按段（默认 30 秒）传**，不做实时流：一段 ≈1 MB，走中继（单次上限 12 MB）很稳；
 *     断了只重传这一段，不会把整场拖垮。
 *  2. **发不出去就留着**：没传成的段留在 `cacheDir/meeting-mic/`，下一段之前重试；
 *     离线那些段留在 `filesDir/offline-meetings/`，**确认对账通过才敢说已上传**，
 *     而且**默认不删**（宁可多留一会儿也别丢音频）。状态里如实写"还有 N 段没送出去"。
 *  3. **静音 = 本地丢帧 + 不上传**（与电脑侧"暂停不写盘"同一个语义）；这段空白照样
 *     会在会议/清单里留下暂停区间（因为暂停/恢复也报了，见 `pause`）。
 *
 * 前台服务（麦克风型）+ 常驻通知：Android 14 要求录音必须在可见的前台服务里做。
 */
class MeetingMicService : Service() {

    companion object {
        const val ACTION_START = "com.echo.contact.MEETING_MIC_START"
        const val ACTION_STOP = "com.echo.contact.MEETING_MIC_STOP"
        const val ACTION_PAUSE = "com.echo.contact.MEETING_MIC_PAUSE"
        const val ACTION_RESUME = "com.echo.contact.MEETING_MIC_RESUME"

        /** 「立即重传」：界面上那个按钮走它（**复用** `retryPending`，不另写一份上传逻辑）。 */
        const val ACTION_RETRY = "com.echo.contact.MEETING_MIC_RETRY"

        /**
         * 「现在同步」（需求点名要的那个明确入口）：把**离线清单**里欠的段补传上去。
         *
         * 与 [ACTION_RETRY] 的区别：那个补的是"在线那条路发失败的段"（`cacheDir/meeting-mic/`），
         * 这个补的是"整场离线录音"（`filesDir/offline-meetings/`）。
         */
        const val ACTION_SYNC = "com.echo.contact.MEETING_MIC_SYNC"

        private const val CHANNEL_ID = "echo_meeting_mic"
        private const val NOTIF_ID = 4211
        private const val SAMPLE_RATE = 16000
        private const val FRAME_SAMPLES = 1600                     // 100ms

        /**
         * 开始录音前那次探活最多等多久。
         *
         * 为什么要有上限：探活是**用户按了「开始」之后**在主线程上等的一下
         * （见 [decidePlan]）。网络不通时 `connect` 可能要十几秒才放弃，
         * 而用户按下按钮后的等待超过两秒就已经像"卡住了"。超时 = 不可达 = 走离线，
         * 这正是我们要的分支（宁可多走一次离线，也别让"开始录音"变成一次等待）。
         */
        private const val PROBE_TIMEOUT_MS = 1_500L

        /**
         * 一段多长，**由偏好决定**（设置页那三个 15/30/60）。
         *
         * 为什么**默认 30 秒**：一段 ≈1 MB（16kHz 单声道 16bit ≈ 32 KB/s），
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
         * **立即重传**（界面上的按钮）。
         *
         * 内部与自动重传走**同一条路**（[retryPending]）—— 上传协议、重试顺序、状态口径都只有一份，
         * 另写一份迟早漂移。这里只多做两件界面要求的事：
         *  1. **立刻**给出"正在重传 N 段…"（`startService` 是异步的，不等它回来再说话）；
         *  2. 没有待传的段时说清楚"现在没有待重传的段"，而不是让按钮看着没反应。
         */
        fun retryPendingNow(context: Context) {
            val app = context.applicationContext
            val n = File(app.cacheDir, "meeting-mic")
                .listFiles { f -> f.name.endsWith(".wav") }?.size ?: 0
            if (n == 0) {
                _state.value = _state.value.copy(pending = 0, retryNote = "现在没有待重传的段")
            } else {
                _state.value = _state.value.copy(retryNote = "正在重传 $n 段…")
            }
            runCatching {
                app.startService(
                    Intent(app, MeetingMicService::class.java).setAction(ACTION_RETRY))
            }
        }

        /**
         * 「现在同步」：让服务把**离线清单**里欠的段传上去（用户明确按的那一下）。
         *
         * 与 [retryPendingNow] 同一个套路（先在界面上说一句、再让服务去干），
         * 因为这个按钮最怕的也是"按了没反应"。这里**不受退避限制**（用户说了"现在"）。
         */
        fun syncNow(context: Context) {
            val app = context.applicationContext
            val store = MeetingDraftStore(File(app.filesDir, OFFLINE_DIR))
            val waits = store.pending()
            _state.value = _state.value.copy(
                syncNote = if (waits.isEmpty()) "现在没有要传的离线录音"
                else "正在同步 ${waits.size} 场离线录音（${waits.sumOf { it.pendingSegments }} 段）…")
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
                        syncNote = if (_state.value.active)
                            "离线录音中（还没跟 ECHO 连上；音频先存在手机里）" else "")
                    notifyIfRunning()
                }
                return
            }
            if (syncGate.running) return
            // 这一轮是**顺带**试：App 正在前台跑，所以按"此刻可达"算
            // （不可达时第一件事就是探活失败 → 退避，所以不会白转）
            if (!offlineSyncDue(reachableNow = true)) return
            CoroutineScope(SupervisorJob() + Dispatchers.IO).launch {
                runOfflineSync(app, store = store, manual = false)
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
         * 跑一轮离线补传（**唯一**的实现：服务的 [ACTION_SYNC] 与前台 tick 都走它）。
         *
         * 结果**如实**写进 `MicState.syncNote`/`syncPhase`：连不上、服务端拒绝、
         * 服务端正忙（等它录完再传）各说各的 —— 绝不能都说成"同步失败"。
         */
        internal suspend fun runOfflineSync(
            app: Context,
            store: MeetingDraftStore = MeetingDraftStore(File(app.filesDir, OFFLINE_DIR)),
            manual: Boolean = false,
            onlyClientId: String? = null,
        ) {
            val baseUrl = PairStore.baseUrl(app)
            if (!PairStore.isPaired(app) || baseUrl.isNullOrBlank()) {
                // 没配对也能**录**（离线录音不依赖服务端），但传不上去 —— 如实说
                _state.value = _state.value.copy(
                    syncPhase = OfflinePhase.WAITING_UPLOAD,
                    syncNote = "还没配对：音频都在手机里，配对之后点「现在同步」就能传")
                return
            }
            val outcome = syncGate.runExclusive {
                val transport = OfflineMeetingTransport(ApiClient.create(baseUrl))
                val active = ActiveDraft.current
                val result = OfflineMeetingSync.run(
                    transport = transport,
                    store = store,
                    localRecording = _state.value.active &&
                        _state.value.syncPhase == OfflinePhase.RECORDING_LOCAL,
                    activeClientId = active?.clientId,
                    onlyClientId = onlyClientId,
                    manual = manual,
                    force = manual,
                )
                // 界面上那句"本地还留着多久"（**如实**：绝不早删，但也不会无限攒）
                val days = AppPrefs.audioRetentionDays(app)
                OfflineMeetingSync.publish(
                    result.toState(
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
                    offline = _state.value.offline || st.phase != OfflinePhase.IDLE,
                    error = "",
                )
                // 补传这一轮过去了：**顺手**按有界保留策略清一次（只清"已对账 + 已收尾 + 到期"的）
                runCatching { purgeExpiredAudio(app, store) }
                notifyIfRunning()
            }
            if (outcome == RetryOutcome.SkippedBusy) {
                // 如实：这一轮**没跑**（另一轮正占着），别让人以为"传完了"
                _state.value = _state.value.copy(
                    syncNote = if (manual) "上一次同步还在跑，这一次没跑（等它完再按）"
                    else _state.value.syncNote)
            }
        }

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

    /**
     * 手动重传（[ACTION_RETRY]）要用的作用域：**只在真的按下按钮时**才建。
     * 平时它不该存在 —— 服务被系统停掉之后，一个还活着的 scope 会继续发请求，
     * 那是最难查的一类"它为什么还在传"。
     */
    @Volatile private var retryScope: CoroutineScope? = null

    /**
     * 「这一轮重传是不是我在跑」—— **唯一**的不可重入判据，由 [retryPending] 自己进出。
     *
     * 为什么不能像上一版那样只在"手动"那一侧放个 `retryBusy`：补传有**两个**入口
     * （界面上的「立即重传」、以及 [stopEverything] 里结束前那次尽力补传），
     * 而两个入口都会调 [retryPending]。保护只写在其中一个上，另一个照样能同时进去
     * —— 同一段音频被传两次，电脑侧就多存一段（转写里同一段出现两遍）。
     * 判据放进 [retryPending] 里，就是"谁跑谁占"，与调用方是谁无关。
     */
    private val retryGate = RetryGuard()

    // ---- 离线那条路要用的现场（**进程被杀就没了；事实都在清单里**）----

    /** 这一次是联网录还是离线录（开始录音前由 [decidePlan] 定下来）。 */
    @Volatile private var offlineMode = false

    /** 当前这一份离线清单（离线模式下才有）。 */
    @Volatile private var activeDraft: MeetingDraft? = null

    /**
     * 在线那场会的 `startedAt`（电脑给的 ISO 时间）。
     *
     * 为什么在**联网**这条路上也要记它：网络可能中途断掉 —— 那时要**无缝转成离线录音**
     * （用户不该因为"录到第 3 段时网断了"而丢掉整场）。而离线清单里的开始时间必须是
     * **一开始那一刻**，不是转离线那一刻（否则整场的时间轴整体错位）。
     */
    @Volatile private var onlineStartedAt: String = ""

    /** 这一次录音的开始时刻（epoch ms）：算段偏移与时长用。 */
    @Volatile private var sessionStartMs = 0L

    /** 当前暂停区间的起点（相对开始时间的秒）；null = 没在暂停。 */
    @Volatile private var pauseStartedAtOffset: Double? = null

    /** 上一次"谁结束了录音"（调试与文案上下文用，见 [stopEverything]）。 */
    @Volatile private var lastStopReason: String = ""

    /** 已经写进清单的暂停区间（转离线/结束时补上最后一条用）。 */
    private val localPauses = ArrayList<DraftPause>()

    /** 已经落进清单的段数（离线模式下 = 清单里的段数；在线模式下 = 只是记账）。 */
    @Volatile private var recordedSegments = 0

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
                markPauseStart()
                _state.value = _state.value.copy(paused = true)
                notifyState()
            }
            ACTION_RESUME -> {
                markPauseEnd()
                _state.value = _state.value.copy(paused = false)
                notifyState()
            }
            ACTION_RETRY -> retryPendingNow()
            ACTION_SYNC -> scope.launch { runOfflineSync(this@MeetingMicService, manual = true) }
            else -> {
                // 被系统重启带回来的 null intent：**不自动开麦**（与听写服务同一条纪律）
                stopSelf()
                return START_NOT_STICKY
            }
        }
        return START_STICKY
    }

    private fun startRecording() {
        if (job != null) return
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            failAndStop("没有麦克风权限：先在 App 里给它")
            return
        }
        // 探活 + 决定走哪条路（**同步**做：界面紧接着要打开全屏录音界面，
        // 而它必须知道这一场是联网还是离线 —— 见 AboutToRecord 的说明）
        val plan = decidePlan()
        AboutToRecord.put(plan)
        offlineMode = plan is RecordPlan.Offline
        startForegroundCompat()
        job = scope.launch { runLoop(plan) }
    }

    /**
     * 决定这一次录音走哪条路（**唯一**的判据在 `startDecision` 里，纯函数 + 有用例）。
     *
     * 顺序有讲究：
     *  ① 先看有没有**上一条还在录**的清单（进程被杀过）—— 有就接着它录，
     *     开始时间与段序号都续上（"App 重开能接着录"就是这一步）；
     *  ② 否则探活：通 → 联网那条路；不通 → **离线**（这就是需求的核心）。
     *
     * 探活失败**绝不能**变成"开始录音失败"：会议室没网时那等于什么都没录。
     */
    private fun decidePlan(): RecordPlan {
        val paired = PairStore.isPaired(this)
        val baseUrl = PairStore.baseUrl(this)
        // ① 接着上一份还没结束的清单（进程被杀/被系统回收之后重开）
        val unfinished = runCatching {
            draftStore.list().lastOrNull { it.recording && it.segments.isNotEmpty() }
        }.getOrNull()
        if (unfinished != null) {
            // 上一场是被**杀掉**的：它的结束时间只能按最后一段推（**如实标出这是推断**）
            val last = unfinished.segments.maxByOrNull { it.seq }
            val endIso = last?.let {
                MeetingDraftStore.isoOf(
                    (MeetingDraftStore.epochOf(it.startedAt) ?: System.currentTimeMillis()) +
                        (it.durationSeconds * 1000).toLong())
            }.orEmpty()
            val closed = unfinished.copy(
                endedAt = endIso,
                pauses = unfinished.pauses,      // 暂停区间原样留着（那是真实发生过的）
            )
            draftStore.save(closed)
            syncNote("上一条离线录音（${closed.segments.size} 段）是被打断的，已经按最后一段的时间收了尾；" +
                "剩下的段会继续传")
        }
        // ② 探活：只有**明确 2xx** 才算可达（超时/拒绝连接都算连不上）
        val reachable = if (paired && !baseUrl.isNullOrBlank()) {
            runBlocking {
                withTimeoutOrNull(PROBE_TIMEOUT_MS) {
                    runCatching {
                        // 用 /health：它不带令牌，所以"令牌坏了"不会被误判成"服务不在"
                        ApiClient.create(baseUrl).health().isSuccessful
                    }.getOrDefault(false)
                } ?: false
            }
        } else {
            false
        }
        return when (val d = startDecision(paired, reachable, why = "ECHO 连不上（探活超时或拒绝连接）")) {
            is StartDecision.Online -> RecordPlan.Online()
            is StartDecision.Offline -> RecordPlan.Offline(
                clientId = MeetingDraftStore.newClientId(), reason = d.why)
            is StartDecision.Blocked -> RecordPlan.Offline(
                // 这一档在这一版里走不到（没配对也允许离线录），留着是为了将来
                // 真出现"不能录"的情况时有一个**不静默**的落点。
                clientId = MeetingDraftStore.newClientId(), reason = d.why)
        }
    }

    private suspend fun runLoop(plan: RecordPlan) {
        val api: EchoApi? = PairStore.baseUrl(this)?.takeIf { PairStore.isPaired(this) }
            ?.let { ApiClient.create(it) }

        // ---- 联网那条路：与改动前一致（先在电脑上开一场"手机麦"会议）----
        if (plan is RecordPlan.Online) {
            val started = try {
                val r = api!!.meetingStartPhone()
                if (r.isSuccessful && r.body()?.ok == true) true
                else {
                    // 探活通了但开会失败：**别把录音失败掉** —— 落到离线那条路
                    offlineMode = true
                    AboutToRecord.put(RecordPlan.Offline(
                        clientId = MeetingDraftStore.newClientId(),
                        reason = "电脑没能开始这场录音（${r.body()?.message ?: "HTTP ${r.code()}"}）"))
                    false
                }
            } catch (e: Exception) {
                offlineMode = true
                AboutToRecord.put(RecordPlan.Offline(
                    clientId = MeetingDraftStore.newClientId(),
                    reason = "联系不上电脑：${e.message}"))
                false
            }
            if (started) {
                onlineStartedAt = MeetingDraftStore.isoOf(System.currentTimeMillis())
            }
        }

        val startIso = onlineStartedAt.ifBlank { MeetingDraftStore.isoOf(System.currentTimeMillis()) }
        sessionStartMs = System.currentTimeMillis()
        // 离线的原因（"ECHO 连不上…"）要在清掉那次交接**之前**取出来 ——
        // 它正是界面上那句"ECHO 连不上，已在手机上离线录音"的一半（另一半是套话）。
        val offlineReason = (AboutToRecord.plan as? RecordPlan.Offline)?.reason.orEmpty()
        if (offlineMode) {
            // 离线：**先把清单立起来**（还没录到一段也先立 —— 这样进程立刻被杀也留得下"这一场开始了"）
            val p = AboutToRecord.plan
            val clientId = (p as? RecordPlan.Offline)?.clientId ?: MeetingDraftStore.newClientId()
            val load = draftStore.loadOrCreate(clientId, startIso)
            activeDraft = when (load) {
                is DraftLoad.Ok -> load.draft
                is DraftLoad.Recovered -> {
                    syncNote(load.note)
                    load.draft
                }
            }
            activeDraft?.let { ActiveDraft.set(it); draftStore.save(it) }
            recordedSegments = activeDraft?.segments?.size ?: 0
        }

        _state.value = _state.value.copy(
            active = true, error = "", sent = 0, pending = 0, retryNote = "",
            offline = offlineMode,
            offlineNote = offlineReason,
            syncPhase = if (offlineMode) OfflinePhase.RECORDING_LOCAL else OfflinePhase.IDLE,
            syncNote = if (offlineMode) "离线录音中（还没跟 ECHO 连上；音频先存在手机里）" else "",
            syncPending = 0,
            syncConfirmed = 0,
            // 大计时按**本地**开始时间走（离线时电脑不在场，没人给我们 startedAt）
            startedAt = if (offlineMode) startIso else "",
        )
        // 界面上"这一场"的信息（时长/模式）从这一刻起就有
        AboutToRecord.clear()

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
        var buffer = ArrayList<Short>(samplesPerSegment)
        try {
            ar.startRecording()
            notifyState()
            while (currentCoroutineContext().isActive) {
                val n = ar.read(frame, 0, frame.size)
                if (n <= 0) {
                    delay(20)
                    continue
                }
                val level = rms(frame, n)
                if (_state.value.paused) {
                    // 静音：**丢帧 + 不上传**（这段时间在会议里就是空白，meta/清单里会留区间）
                    _state.value = _state.value.copy(level = 0f)
                    continue
                }
                _state.value = _state.value.copy(level = level)
                for (i in 0 until n) buffer.add(frame[i])
                if (buffer.size >= samplesPerSegment) {
                    val bytes = toPcmBytes(buffer)
                    onSegmentReady(api, bytes)
                    buffer = ArrayList(samplesPerSegment)
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
     * 一段录满了：**离线就只写本地，联网就照旧传**。
     *
     * 这一处是两条路的分岔口，判据只有 [offlineMode] 一个 —— 免得出现
     * "离线了还在传"（白耗电、还可能把服务端那场会搞乱）这种半吊子状态。
     */
    private suspend fun onSegmentReady(api: EchoApi?, bytes: ByteArray) {
        if (offlineMode) {
            appendToDraft(bytes)
            return
        }
        if (api == null) {
            // 没有 api（配对信息丢了）：**转离线**，别让这一段丢掉
            switchToOffline("配对信息读不到了")
            appendToDraft(bytes)
            return
        }
        if (!uploadSegment(api, bytes)) {
            // 发不出去：先看看是不是**整个连不上** —— 是就转离线（后面不再逐段重试）
            if (probeUnreachable(api)) {
                switchToOffline("录音中途 ECHO 连不上了（探活失败）")
                appendToDraft(bytes)
            } else {
                // 只是这一段没成（服务端抖动/拒绝）：留着，下一段之前重试（行为与改动前一致）
                savePending(bytes)
            }
        }
    }

    /** 探活一次：**只有明确非 2xx 或抛异常**才说不可达（单次抖动不转离线）。 */
    private suspend fun probeUnreachable(api: EchoApi): Boolean = try {
        !api.health().isSuccessful
    } catch (e: Exception) {
        true
    }

    /**
     * 在线 → 离线**无缝切换**（录音中途断网）。
     *
     * 为什么值得单独一条路：会议可能开了一个小时，第 40 分钟才没网。这时
     * "剩下的都录不下来"是最糟的结果（前面 39 分钟白录），而"从这里转成本地录"
     * 只是多等一会儿补传。开始时间沿用**这一场开始的那一刻**（[onlineStartedAt]），
     * 所以补传上去之后时间轴是连续的，看不出中间切过一次。
     */
    private fun switchToOffline(why: String) {
        if (offlineMode) return
        offlineMode = true
        val clientId = MeetingDraftStore.newClientId()
        val startIso = onlineStartedAt.ifBlank { MeetingDraftStore.isoOf(sessionStartMs) }
        val load = draftStore.loadOrCreate(clientId, startIso)
        val draft = when (load) {
            is DraftLoad.Ok -> load.draft
            is DraftLoad.Recovered -> {
                syncNote(load.note)
                load.draft
            }
        }
        // 这一场已经报给电脑的那些段不在本地（它们已经在电脑上了）——
        // 所以水位要抬到"联网时已经送出去的那几段"，否则本地最大段序号只有 1、
        // 而服务端那一场已经有 N 段 → 对账永远不符（见 watermarkAfterHandoff）。
        val handoff = watermarkAfterHandoff(
            sentOnline = _state.value.sent,
            localSeqs = draft.segments.map { it.seq },
        )
        activeDraft = draft.copy(
            pauses = localPauses.toList(),
            acknowledgedSegments = maxOf(draft.acknowledgedSegments, handoff),
        )
        draftStore.save(activeDraft!!)
        ActiveDraft.set(activeDraft)
        recordedSegments = activeDraft?.segments?.size ?: 0
        _state.value = _state.value.copy(
            offline = true, offlineNote = why,
            syncPhase = OfflinePhase.RECORDING_LOCAL,
            syncNote = "录音中途 ECHO 连不上了：后面的段先存在手机里（$why）",
            syncPending = 0,
            syncConfirmed = activeDraft?.confirmedSegments ?: 0,
        )
        notifyState()
    }

    /** 把一段写进**离线清单 + app 私有目录**（离线录音的落盘动作，**不碰网络**）。 */
    private fun appendToDraft(pcm: ByteArray) {
        val d = activeDraft
        if (d == null) {
            _state.value = _state.value.copy(error = "离线清单丢了，这一段没地方存（真的可能丢音频）")
            return
        }
        // ⚠️ 段序号要接着**水位**往下排，而不是"本地最大序号 + 1"：中途从联网转离线时
        // 前面那几段（seq 1..watermark）在电脑上而不在本地，序号直接从 watermark+1 起
        // —— 否则新段会从 1 重新开始，把电脑上已有的段**覆盖/重号**（转写内容就乱了）。
        val seq = maxOf(d.segments.maxOfOrNull { it.seq } ?: 0, d.acknowledgedSegments) + 1
        val startedAtIso = MeetingDraftStore.isoOf(System.currentTimeMillis())
        val offset = offsetSeconds(d.startedAt, System.currentTimeMillis())
        val seconds = segmentSeconds(this)
        return try {
            val f = draftStore.segmentFile(d.clientId, seq)
            f.parentFile?.mkdirs()
            f.writeBytes(WavWriter.wrap(pcm, SAMPLE_RATE, 1, 16))
            val seg = DraftSegment(
                seq = seq,
                file = f.name,
                startedAt = startedAtIso,
                startOffsetSeconds = offset,
                durationSeconds = seconds.toDouble(),
            )
            val updated = d.copy(
                segments = d.segments + seg,
                pauses = localPauses.toList(),
                durationSeconds = offset + seconds,
                acknowledgedSegments = d.acknowledgedSegments,
            )
            activeDraft = updated
            ActiveDraft.set(updated)
            recordedSegments = updated.segments.size
            if (!draftStore.save(updated)) {
                _state.value = _state.value.copy(
                    error = "第 $seq 段存下来了，但清单没写进去（重启后可能对不上账）")
            }
            publishOfflineRecording(updated)
        } catch (e: Exception) {
            // 连缓存都写不进去：如实报出来（这是真的丢音频）
            _state.value = _state.value.copy(error = "有一段缓存不下来，可能真的丢了：${e.message}")
        }
    }

    /** 离线录音期间把"录到第几段/几段还没传"发布给界面（含通知）。 */
    private fun publishOfflineRecording(d: MeetingDraft) {
        val st = OfflineSyncState(
            phase = OfflinePhase.RECORDING_LOCAL,
            clientId = d.clientId,
            pendingSegments = d.pendingSegments,
            confirmedSegments = d.confirmedSegments,
            totalSegments = d.segments.size,
            drafts = 1,
            recordingSeconds = d.durationSeconds,
            note = _state.value.offlineNote,
        )
        OfflineMeetingSync.publish(st)
        _state.value = _state.value.copy(
            offline = true,
            syncPhase = OfflinePhase.RECORDING_LOCAL,
            syncPending = d.pendingSegments,
            syncConfirmed = d.confirmedSegments,
            syncNote = offlineStatusLine(st),
        )
        notifyState()
    }

    // ---- 暂停区间（离线录音要自己记账：电脑那边根本不知道手机静音了）----

    private fun markPauseStart() {
        if (pauseStartedAtOffset == null) {
            pauseStartedAtOffset = offsetSeconds(
                activeDraft?.startedAt ?: onlineStartedAt.ifBlank {
                    MeetingDraftStore.isoOf(sessionStartMs)
                },
                System.currentTimeMillis())
        }
    }

    /**
     * 暂停结束（**恢复录音**或**直接结束录音**都走这里）。
     *
     * 为什么"结束时还静音着"也要记一条：那段静音**确实是暂停**（没有采到音），
     * 不记的话会议里就会凭空多出一段"没人说话"的时长，而不是标着"被暂停过"。
     */
    private fun markPauseEnd(nowMs: Long = System.currentTimeMillis()) {
        val from = pauseStartedAtOffset ?: return
        pauseStartedAtOffset = null
        val baseIso = activeDraft?.startedAt ?: onlineStartedAt.ifBlank {
            MeetingDraftStore.isoOf(sessionStartMs)
        }
        val to = maxOf(from, offsetSeconds(baseIso, nowMs))
        localPauses += DraftPause(from = from, to = to)
        activeDraft?.let { d ->
            val updated = d.copy(pauses = localPauses.toList())
            activeDraft = updated
            ActiveDraft.set(updated)
            draftStore.save(updated)
        }
    }

    /** 传一段；成功返回 true。失败**不抛**（调用方决定留着重试）。 */
    private suspend fun uploadSegment(api: EchoApi, pcm: ByteArray): Boolean {
        retryPending(api)
        val wav = WavWriter.wrap(pcm, SAMPLE_RATE, 1, 16)
        return try {
            val r = api.meetingSegment(wav.toRequestBody("audio/wav".toMediaType()))
            if (r.isSuccessful) {
                _state.value = _state.value.copy(sent = _state.value.sent + 1, error = "")
                notifyState()
                true
            } else {
                _state.value = _state.value.copy(
                    error = "这一段电脑没收下（HTTP ${r.code()}），留着下轮重传")
                false
            }
        } catch (e: Exception) {
            _state.value = _state.value.copy(error = "这一段没发出去（${e.message}），留着下轮重传")
            false
        }
    }

    private fun pendingDir(): File = File(cacheDir, "meeting-mic").apply { mkdirs() }

    private fun savePending(pcm: ByteArray) {
        try {
            val f = File(pendingDir(), "seg-" + System.currentTimeMillis() + ".wav")
            f.writeBytes(WavWriter.wrap(pcm, SAMPLE_RATE, 1, 16))
            _state.value = _state.value.copy(pending = pendingFiles().size)
            notifyState()
        } catch (e: Exception) {
            // 连缓存都写不进去：如实报出来（这是真的丢音频）
            _state.value = _state.value.copy(error = "有一段缓存不下来，可能真的丢了：${e.message}")
        }
    }

    private fun pendingFiles(): List<File> =
        pendingDir().listFiles { f -> f.name.endsWith(".wav") }?.sortedBy { it.name } ?: emptyList()

    /**
     * 把欠的段按顺序再传一遍。
     *
     * **失败就停在这**（`break`）而不是跳过它继续传后面的：会议音频的**先后顺序**就是内容本身，
     * 跳过一段先把后面的送上去，转写出来就是乱的。留着下次再试，顺序永远不破。
     *
     * **不可重入**：判据在 [retryGate] 里（不是各个调用方自己的标志位）—— 「立即重传」与
     * [stopEverything] 里"结束前的补传"是**两个**入口，保护只写在其中一个上就等于没写
     * （两轮同时进去 → 同一段被传两次 → 电脑侧多存一段、转写里同一段出现两遍）。
     * 并发的第二次调用**立刻**返回 [RetryOutcome.SkippedBusy]，**不排队重跑**
     * （排队会让用户的"结束"被一次上传拖住，那是更糟的体验）。
     *
     * [note]：要不要把结果写进 `MicState.retryNote`。**只有用户按了「立即重传」才写** ——
     * 自动重传是后台的常事（每段上传前都会重试一轮），每次都往界面上写一句
     * "重传完成"，那一行就会被常驻的套话占住，反而看不出手动重传的真实结果。
     * 不论写不写 note，`pending` 段数**每次都如实更新**（那是界面与通知都在看的事实）。
     */
    private suspend fun retryPending(
        api: EchoApi,
        note: Boolean = false,
    ): RetryOutcome = retryGate.runExclusive { retryRound(api, note) }

    /** 真传的那一轮（**调用方必须已经占住 [retryGate]** —— 见 [retryPending]）。 */
    private suspend fun retryRound(api: EchoApi, note: Boolean) {
        var failed = false
        for (f in pendingFiles()) {
            try {
                val r = api.meetingSegment(f.readBytes().toRequestBody("audio/wav".toMediaType()))
                if (r.isSuccessful) {
                    f.delete()
                    _state.value = _state.value.copy(sent = _state.value.sent + 1)
                } else {
                    failed = true
                    break                       // 还是不行：留着，下次再试（保顺序）
                }
            } catch (e: Exception) {
                failed = true
                break
            }
        }
        val left = pendingFiles().size
        _state.value = _state.value.copy(
            pending = left,
            retryNote = if (!note) _state.value.retryNote
            else if (left == 0) "重传完成，都送出去了"
            else if (failed) "还有 $left 段没重传成功（还留着，下轮接着试）"
            else "还有 $left 段待重传",
        )
        notifyState()
    }

    /**
     * 界面上那个「立即重传」按下的入口（[ACTION_RETRY]）。
     *
     * 与自动重传**同一条路**（[retryPending]），这里只负责：配对没配好就说清楚，
     * 然后把这一次重传的**结果**如实写进 `retryNote`（别静默）。
     *
     * ⚠️ 这里**不再**自己置"忙"标志：上一版是先置 `retryBusy = true` 再去调 [retryPending]，
     * 而 [retryPending] 开头的判据一看标志是 true 就 `return` —— 于是按「立即重传」**什么都没传**，
     * 只把上一轮的 `retryNote` 清空了（看着像"按了没反应"）。现在"忙不忙"由守卫自己回答：
     * 进去了就真传，被挡了就如实说被挡（[RetryOutcome.SkippedBusy]）。
     */
    private fun retryPendingNow() {
        val baseUrl = PairStore.baseUrl(this)
        if (!PairStore.isPaired(this) || baseUrl.isNullOrBlank()) {
            _state.value = _state.value.copy(retryNote = "还没配对：先配对才能重传")
            notifyState()
            return
        }
        val api = ApiClient.create(baseUrl)
        // 上一次的结果先清掉：留着上一轮的"重传完成"会让人以为这次也完了（还没开始呢）
        _state.value = _state.value.copy(retryNote = "")
        ensureRetryScope().launch {
            try {
                when (retryPending(api, note = true)) {
                    // 结果（"重传完成，都送出去了" / "还有 N 段…"）已经由 retryPending 写进 retryNote
                    RetryOutcome.Ran -> Unit
                    RetryOutcome.SkippedBusy -> {
                        // 如实：这一次**没跑**（不是"传完了"）。不说清楚的话，用户看到的只是
                        // "按了按钮、那一行空着"，会以为按钮坏了或段丢了。
                        _state.value = _state.value.copy(
                            retryNote = "上一次重传还在跑，这一次没跑（等它完再按）")
                        notifyState()
                    }
                }
            } catch (e: Exception) {
                _state.value = _state.value.copy(retryNote = "重传出错：${e.message}")
                notifyState()
            }
        }
    }

    /** 手动重传的作用域（用完就在 [onDestroy] 里取消 —— 不留一个还在发请求的活作用域）。 */
    private fun ensureRetryScope(): CoroutineScope =
        retryScope ?: CoroutineScope(SupervisorJob() + Dispatchers.IO).also { retryScope = it }

    private fun stopEverything(reason: String) {
        // 为什么要把这个原因记下来：结束时还静音着、或补传没传完，这两件事都要
        // 写在状态里让人看懂 —— 而那两句话的**上下文**就是"谁结束了录音"
        // （用户按的 / 通知里的结束 / 服务被系统收掉）。别让它变成一个没人读的参数。
        lastStopReason = reason
        val j = job
        job = null
        scope.launch {
            if (j != null) j.cancelAndJoin()

            // 结束时还静音着：那段静音也要记成暂停区间（否则会凭空多出一段时长）
            if (pauseStartedAtOffset != null) markPauseEnd()

            // ---- 离线那一场：**完全本地结束**（不依赖服务端）----
            if (offlineMode) {
                finishOfflineDraft()
                // 结束之后**尽力补传一次**（网络可能刚好恢复了）；传不上去也照样留着
                runCatching { runOfflineSync(this@MeetingMicService, manual = true) }
                _state.value = _state.value.copy(active = false, level = 0f, paused = false)
                stopForegroundCompat()
                stopSelf()
                return@launch
            }

            // ---- 联网那一场：与改动前一致（先尽力补传欠的段，再让电脑结束这场会议）----
            try {
                val baseUrl = PairStore.baseUrl(this@MeetingMicService) ?: ""
                if (baseUrl.isNotBlank()) {
                    val api = ApiClient.create(baseUrl)
                    // 这一轮**可能被"手动重传"占着而根本没跑**（[retryPending] 不可重入）。
                    // 不排队等它 —— 用户的"结束"不该被一次上传拖住；但也**不许静默**：
                    // 把"这次没重传完、还有 N 段留着"如实写进 retryNote（见 [stopRetryNote]）。
                    val outcome = retryPending(api)
                    val left = pendingFiles().size
                    stopRetryNote(outcome, left).takeIf { it.isNotBlank() }?.let { note ->
                        _state.value = _state.value.copy(pending = left, retryNote = note)
                        notifyState()
                    }
                    api.meetingStop()
                }
            } catch (e: Exception) {
                // 停不下来就如实写在状态里；用户的"结束"已经生效（本地不再录）
            }
            _state.value = _state.value.copy(active = false, level = 0f, paused = false)
            stopForegroundCompat()
            stopSelf()
        }
    }

    /**
     * 离线那一场的收尾：写结束时间、算时长、**落盘**，然后交给补传。
     *
     * 为什么结束也必须落盘：`ended_at` 是补传时要交给服务端的（会议时长/时间轴靠它）。
     * 而"结束"这一下如果只改了内存，进程一被杀这场会就永远停在"还在录"。
     */
    private fun finishOfflineDraft() {
        val d = activeDraft ?: return
        val nowMs = System.currentTimeMillis()
        val endedAt = MeetingDraftStore.isoOf(nowMs)
        val duration = maxOf(d.durationSeconds, offsetSeconds(d.startedAt, nowMs))
        val done = d.copy(
            endedAt = endedAt,
            durationSeconds = duration,
            pauses = localPauses.toList(),
        )
        activeDraft = done
        ActiveDraft.set(done)
        draftStore.save(done)
        if (_state.value.syncPhase == OfflinePhase.RECORDING_LOCAL) {
            OfflineMeetingSync.publish(OfflineSyncState(
                phase = OfflinePhase.WAITING_UPLOAD,
                clientId = done.clientId,
                pendingSegments = done.pendingSegments,
                confirmedSegments = done.confirmedSegments,
                totalSegments = done.segments.size,
                drafts = 1,
                recordingSeconds = done.durationSeconds,
                note = "录完了，等联网上传",
            ))
        }
    }

    private fun failAndStop(msg: String) {
        _state.value = MicState(active = false, error = msg)
        notifyState()
        stopForegroundCompat()
        stopSelf()
    }

    /** 记一句离线那一路的原因（不覆盖 phase）。 */
    private fun syncNote(note: String) {
        _state.value = _state.value.copy(syncNote = note)
    }

    override fun onDestroy() {
        runCatching { record?.release() }
        record = null
        if (liveService === this) liveService = null
        // 手动重传那个作用域也要一起收掉：服务没了还留着它，就会"服务已停、请求还在飞"
        retryScope?.cancel()
        retryScope = null
        // 服务正在消失：把守卫放掉（幂等）。被取消的那一轮即使来不及走 finally，
        // 也不会让下一场录音里这个守卫一直是"在跑"。
        retryGate.leave()
        // ⚠️ **刻意不清 [ActiveDraft]**：进程可能只是被系统回收，清单与音频都还在磁盘上，
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
 * 通知里那一行字（**纯函数**，JVM 可测）。
 *
 * 为什么值得单独抽出来并有判据：这一行是"录音到底有没有丢东西"的**唯一**出口 ——
 * 优先级写错了就会在最该说话的时候沉默：
 *  * 有**错误**先说错误（"有一段缓存不下来，可能真的丢了"绝不能被"在录 · 已送 3 段"盖掉）；
 *  * 静音其次（但**离线**时"已缓存 N 段 · 等联网上传"比静音那句更要紧：那关系到音频会不会丢）；
 *  * 然后是**离线**那些段（"已缓存 N 段 · 等联网上传"）；
 *  * 再是待重传（这条也在界面上写着，见 `MeetingRecordingScreen`）；
 *  * 最后才是"一切正常"。
 *
 * ⚠️ 需求里点名的口径在这里落成一句话：**没传上去绝不说"已上传"**。
 * 所以离线那几档只会出现"已缓存 / 等联网上传 / 还没传"，不出现"已上传"
 * （唯一说"已上传"的地方是补传对账通过之后的 `DONE`，而那时是真的对上了）。
 */
internal fun notificationText(st: MicState): String = when {
    st.error.isNotBlank() -> st.error
    // 离线：先说"存在手机里、还没传上去"（这件事比静音要紧得多）
    st.offline && st.syncNote.isNotBlank() -> st.syncNote
    st.offline && st.syncPending > 0 -> "离线录音中 · 已缓存 ${st.syncPending} 段 · 等联网上传"
    st.offline -> "离线录音中 · 声音先存在这台手机里，等联网上传"
    st.paused -> "已静音（这段不会录进会议）"
    st.pending > 0 -> "在录（手机麦克风）· 已送 ${st.sent} 段 · ${st.pending} 段待重传"
    else -> "在录（手机麦克风）· 已送 ${st.sent} 段"
}

/** 手机录音那条路的状态（界面读它）。 */
data class MicState(
    val active: Boolean = false,
    val paused: Boolean = false,
    /** 本地电平（0..1）—— **这条路上电平是本地算的**，比电脑那条路顺得多。 */
    val level: Float = 0f,
    val sent: Int = 0,
    /** 还没送出去的段数（>0 就说明有音频躺在缓存里等重传）。 */
    val pending: Int = 0,
    val error: String = "",
    /**
     * 上一次重传的**结果**（"重传完成，都送出去了" / "还有 2 段没重传成功（还留着，下轮接着试）"）。
     *
     * 为什么要有它：界面上按了「立即重传」之后，如果只让 `pending` 变个数字，
     * 用户分不清"传成功了"和"按了没反应"。所以结果**明写在这一行**（别静默）。
     */
    val retryNote: String = "",

    // ---- 离线那条路（用户 2026-10-16 的需求）----

    /** 这一场是不是**离线录音**（界面据此把"声音从哪来"那句话换成离线口径）。 */
    val offline: Boolean = false,
    /** 为什么进了离线（"ECHO 连不上（探活超时或拒绝连接）"）：界面要**如实**说这句。 */
    val offlineNote: String = "",
    /** 离线那一路现在的状态（等联网 / 在传 / 等服务端 / 对账完成…）。 */
    val syncPhase: OfflinePhase = OfflinePhase.IDLE,
    /** 离线那一路要显示的那行字（**唯一**出口：`offlineStatusLine`）。 */
    val syncNote: String = "",
    /** 还有几段离线音频没传上去（>0 就该出现「现在同步」）。 */
    val syncPending: Int = 0,
    /** 离线那些段里已经被服务端认下几段（界面用来说"已传 M 段"）。 */
    val syncConfirmed: Int = 0,
    /** 有几种离线录音在等（可能不止一场）。 */
    val syncDrafts: Int = 0,
    /**
     * 这一场是什么时候开始的（ISO 本地时间；离线那一场用它算大计时）。
     *
     * 为什么离线要把它带上来：联网那条路的 `startedAt` 是**电脑**给的
     * （`GET /api/meeting/status`），而离线时电脑根本不在场 —— 大计时只能按
     * 本地清单里的开始时间算，否则它会永远停在 `--:--`。
     */
    val startedAt: String = "",
) {
    /** 正在补传（界面据此把「现在同步」按钮换成"正在…"，别让人连点）。 */
    val syncBusy: Boolean get() = syncPhase == OfflinePhase.UPLOADING
}

/**
 * 一轮重传（[MeetingMicService.retryPending]）的结局。
 *
 * 为什么要有"没跑"这一档，而不是只回 true/false：调用方需要**如实**告诉用户
 * "这一次没跑，因为另一轮正在跑" —— 只说"没成功"会让人以为音频又丢了一段。
 */
internal enum class RetryOutcome {
    /** 真跑了（成没成看 `MicState.pending`：>0 就是还有没送出去的段）。 */
    Ran,

    /** **没跑** —— 另一轮已经占着守卫（不排队、不等待）。 */
    SkippedBusy,
}

/**
 * 「这一轮重传是不是我在跑」的**唯一**判据（纯逻辑，JVM 可测）。
 *
 * 为什么值得单独一个类并有判据：同一段音频被传两次，电脑侧就多存一段
 * （转写里同一段出现两遍），而这件事在手机这边**完全静默** —— 界面照样显示"重传完成"。
 * 所以"不可重入"必须是**一处**说了算的：谁跑谁占（[runExclusive]），
 * 并发的第二个调用者**立刻**拿到 [RetryOutcome.SkippedBusy]（**不排队** ——
 * 排队会让用户的"结束"被一次上传拖住）。
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
     * `try/finally` 是硬要求：上传抛异常时也必须放掉 —— 否则一次失败就把重传**永久锁死**
     * （「立即重传」从此按了没反应，而 `pending` 一直不降，用户无路可走）。
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
}

/**
 * 「结束录音前那次补传」的结局 → 如实的一句话（**纯函数**，JVM 可测）。
 *
 * 为什么单独抽出来：这一轮**可能被"手动重传"占着而根本没跑**。什么都不说的话，
 * 用户看到的是"我按了结束、待重传的数字还在"，却分不清是"没跑"还是"传失败了"
 * （说错原因会把人引去查网络）。返回空串 = 没有欠的段，没什么要说的
 * （别用套话占住界面上那一行）。
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
