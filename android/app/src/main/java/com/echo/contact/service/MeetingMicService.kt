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
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
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
 *  1. **按段（默认 600 秒 = 10 分钟，与电脑一致）传**，不做实时流：10 分钟 ≈ 19 MB 的 WAV
 *     （16 kHz 单声道 ≈ 32 KB/s；FLAC 之后约一半），走中继（单次上限 12 MB）——
 *     FLAC 时通常还在限内，**退回 WAV 时可能超限**（如实写在设置页，别当成"很稳"）；
 *     断了只重传这一段，不会把整场拖垮。
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

        /**
         * `ACTION_SYNC` 上带的一个布尔 extra：这一轮是**后台节拍自动**发起的（不是用户按的）。
         *
         * 为什么要分开：两者的"话"不一样 ——
         *  * 用户按的那一下：状态是"正在同步…"，而且**失败也要发一条通知**（他正等着结果）；
         *  * 后台节拍：状态是"正在自动补传…"，失败**不发通知**（每几分钟弹一次"补传没成"
         *    就是骚扰；原因在会议页那块入口里已经写着，用户点开就看得见）。
         */
        internal const val EXTRA_AUTO_SYNC = "com.echo.contact.EXTRA_AUTO_SYNC"

        private const val CHANNEL_ID = "echo_meeting_mic"

        /**
         * 录音那条通知的 id（`internal`：补传的前台实现要**沿用同一个 id** 才能"更新而不是
         * 多出一条" —— 录音中顺手补传时，前台那条通知仍然必须是录音那条，见 `SyncForegroundAndroid`）。
         */
        internal const val NOTIF_ID = 4211

        /** 日志标签（**段的格式退回 WAV 这类"如实的事"也要落进日志** —— 真机排障只能靠它）。 */
        private const val TAG = "MeetingMic"
        const val SAMPLE_RATE = 16000
        const val CHANNELS = 1
        private const val FRAME_SAMPLES = 1600                     // 100ms

        /**
         * 「结束录音」时等封段队列排空的上限（毫秒）。
         *
         * 为什么要有上界：10 分钟一段的编码可能要几秒，但**不能无限等** ——
         * 真撞上一个卡死的编码器，"结束"必须照样返回（尾段照常封，宁可顺序冒一点风险）。
         */
        private const val SEAL_DRAIN_TIMEOUT_MS = 60_000L

        /**
         * 一段多长，**由偏好决定**（设置页 15/30/60/300/600 秒）。
         *
         * 为什么**默认 600 秒（10 分钟）**：用户 2026-10-18 点名"跟电脑一样 10 分钟一段就行"
         * —— 段长与电脑一致，两边的段边界才有同一个含义。代价是边录边传最多滞后 10 分钟、
         * 且一次失败要重传的那段更大（中继单次上限 12 MB，FLAC 通常够、退回 WAV 可能超）。
         * 改偏好**下次开录生效**（正在录的这一场按开录时读到的档，中途换档不会把已缓冲的那半段切碎）。
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

        /**
         * 用**前台**方式起服务（O+ 必须 `startForegroundService`）。
         *
         * 为什么单独一个入口：`startForegroundService` 是一条**承诺** —— 服务必须在几秒内
         * 调 `startForeground`，否则系统按 ANR 处理（`ForegroundServiceDidNotStartInTimeException`）。
         * 所以每一个调用点都必须紧接着有一段"先挂前台"的代码（`ACTION_START` → `startRecording`，
         * `ACTION_SYNC` → `syncForeground.guard`）；这里只负责"怎么起"。
         */
        private fun startForegroundServiceCompat(context: Context, intent: Intent) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(intent)
            else context.startService(intent)
        }

        /**
         * 这些清单里**还要传的段一共多少字节**（只用来估锁的超时；读不到就按 0 算）。
         *
         * 为什么要按字节估：一段 10 分钟的 WAV ≈18 MB，锁的时间必须按"真的有多少要传"
         * 算，而不是拍一个 30 分钟（见 `uploadLockTimeoutMs`）。读不到文件大小只会让
         * 估算偏小（下一段会续锁），这是安全的方向。
         */
        private fun pendingBytesOf(store: MeetingDraftStore, drafts: List<MeetingDraft>): Long {
            var total = 0L
            drafts.forEach { d ->
                segmentsToUploadOf(d).forEach { seg ->
                    total += runCatching { store.audioFileOf(d.clientId, seg).length() }
                        .getOrDefault(0L)
                }
            }
            return total
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
         *
         * ## 为什么这里必须是 `startForegroundService`（用户 2026-10-19 深夜的根因）
         *
         * 旧实现是 `app.startService(...)` —— 一个**后台**服务。屏幕一关，进程被冻结/掐掉，
         * 正在传的那一段（18 MB，要几分钟）就断在那里，用户解锁后只能再按一次「重试」。
         * 换成 `startForegroundService` 之后，服务**必须**在几秒内 `startForeground`
         * （见 `onStartCommand` 的 `ACTION_SYNC`：那里先挂前台再发请求），
         * 于是补传期间进程是**前台**的 —— 锁屏不会把它当后台掐掉。
         *
         * ## 为什么"没有待传"就**连服务都不起**
         *
         * "用户没在传的时候零通知"是硬要求。先起服务再发现没事可干，至少会留下一条通知
         * （前台服务必须挂一条），而且会白白走一遍前台的生命周期。
         * 所以判据在这里就问清楚（`shouldEnterSyncForeground` 的同一个判据）。
         */
        fun syncNow(context: Context) {
            val app = context.applicationContext
            val store = MeetingDraftStore(File(app.filesDir, OFFLINE_DIR))
            val waits = store.pending()
            _state.value = _state.value.copy(
                syncNote = if (waits.isEmpty()) "现在没有要传的录音"
                else "正在同步 ${waits.size} 场录音（${waits.sumOf { it.pendingSegments }} 段）…")
            // 没东西要传：**不进前台、不起服务**（"零通知"那条判据）
            if (!shouldEnterSyncForeground(waits.size)) return
            runCatching {
                startForegroundServiceCompat(
                    app, Intent(app, MeetingMicService::class.java).setAction(ACTION_SYNC))
            }
        }

        // ---------------------------------------------------------------- 待传摘要（会议页那块入口）

        /**
         * 磁盘上**待补传清单**的摘要（会议页那块常驻入口读它；数据来自既有 `MeetingDraftStore`）。
         *
         * ## 为什么必须是磁盘的摘要、而不是只读 [MicState]
         *
         * [MicState] 是**内存**状态：App 重开 / 进程被系统杀掉之后它就是一张白纸
         * （默认 `syncPhase = IDLE`、`syncPending = 0`）。而音频与清单都还在
         * `filesDir/offline-meetings` 里 —— 只读内存的话，会议页那块入口会在
         * "还有 3 段没传"的时候**整块不画**。用户 2026-10-19 说的"没有上传的地方了"
         * 就是这件事：不是数字错了，是入口**消失了**。
         */
        private val _pendingSummary = MutableStateFlow(PendingSyncSummary())
        val pendingSummary: StateFlow<PendingSyncSummary> = _pendingSummary.asStateFlow()

        /**
         * 本机清单里那几场的**身份**（会议页判"服务器上那场是不是我的"读它）。
         *
         * 为什么界面需要它（2026-10-20 真机）：服务端只说"当前这场录音的 `clientId` 是
         * `phone-…`"，而"这个 id 是不是我建的"只有本地清单知道 —— 手机会话 id
         * **每次开录都会新生成**（`MeetingDraftStore.newClientId()`），所以拿"当前会话"去比
         * 永远认不出"上一次启动留下的那场"。这里把磁盘上**所有**清单的 id 交出去
         * （不是只交待传的那几场）：判身份看的是"我建过没有"，与"还欠几段"是两件事。
         *
         * ⚠️ `null` 是**有意义的**：刚打开 App、还没来得及读磁盘时是 `null`
         * （不是空表）—— 判归属那边要借它区分"我没有清单"与"我还不知道"。
         * 两种都当成"没有清单"的话，界面第一帧就会把"我自己那场"说成"另一台设备在录音"。
         */
        private val _localDrafts = MutableStateFlow<List<LocalDraftRef>?>(null)
        internal val localDrafts: StateFlow<List<LocalDraftRef>?> = _localDrafts.asStateFlow()

        /**
         * 从既有清单刷新那块摘要（[MeetingDraftStore.pending]）。
         *
         * 读不动就按"没有待传"算 —— 但**不抛**：这是界面/后台都会调的一条，
         * 抛出去的表现是"开会页崩了"，而真相只是某个清单目录读不了（补传自己会另算）。
         *
         * ⚠️ 2026-10-20 起**一次读盘喂两处**：①待传摘要（会议页那块入口）；
         * ②清单身份（[localDrafts]，判归属用）。分两次读会出现"摘要说有 3 段、
         * 身份表里却没有那一场"的瞬间分叉。
         */
        fun refreshPendingSummary(context: Context): PendingSyncSummary {
            val app = context.applicationContext
            val all = runCatching { storeOf(app).list() }.getOrDefault(emptyList())
            val summary = runCatching { pendingSummaryOf(all) }.getOrDefault(PendingSyncSummary())
            _pendingSummary.value = summary
            publishLocalDrafts(all)
            return summary
        }

        /** 把"本机建过哪些会话"公开给界面（**一处**：判归属用，见 [localDrafts]）。 */
        private fun publishLocalDrafts(drafts: List<MeetingDraft>) {
            _localDrafts.value = drafts.map { LocalDraftRef(it.clientId, it.serverMeetingId) }
        }

        /**
         * **App 一打开（进入外壳）就试一次自动同步**（用户 2026-10-19 的第二个问题）。
         *
         * 为什么不能只靠首页那张状态卡的节拍：那张卡只在「超级助理」页可见，而录完之后
         * 用户最常停在会议页（或从通知直达会议页）—— 那时旧的 tick **一次都不会跑**，
         * 一条 `/meeting/segment` 都不会发。所以触发点必须挂在**外壳**上（见 `HomeScreen`），
         * 而这里只有一件事：把"用户刚打开"这件事翻译成一次**允许尝试**的 tick。
         *
         * 三件事，缺一不可：
         *  1. 退避清零（[backoffOnAppOpen]）：否则几小时前断网攒满 8 次之后，
         *     本进程里 `shouldRetryNow` 恒 false —— 打开 App 也永远不再自动试；
         *  2. 先刷新待传摘要：界面那块入口要立刻有数（不等下一轮 tick）；
         *  3. 走**既有** [tickFromForeground]：不新造一条同步路（它内部还是
         *     `runOfflineSync(scope = FINALIZE)`，还在录的那份会被自动降级成 INCREMENTAL）。
         */
        fun onAppOpened(context: Context) {
            val app = context.applicationContext
            val reset = backoffOnAppOpen()
            syncAttempts = reset.attempts
            syncNextAtMs = reset.nextAtMs ?: 0L
            syncWasUnreachable = reset.wasUnreachable
            refreshPendingSummary(app)
            tickFromForeground(app)
        }

        /**
         * 后台补传的入口（**应用外壳**每几秒调一次；还有听写服务那边）。
         *
         * ⚠️ 2026-10-19 起这个触发点**必须是 App 级**的（`HomeScreen` 那个 `LaunchedEffect`），
         * 不能只挂在首页那张状态卡里 —— 那张卡只在「超级助理」页可见，而录完之后用户
         * 最常停在会议页/从通知直达会议页，那条节拍一次都不会跑（这就是"打开 App 等几分钟，
         * 一条 `/meeting/segment` 都没发"的根因）。见 [onAppOpened]。
         *
         * ## 为什么 2026-10-19 深夜改成"把活交给服务"（不再自己起协程）
         *
         * 原来是这里 `CoroutineScope(SupervisorJob() + Dispatchers.IO).launch { runOfflineSync(...) }`
         * —— 一个**挂在 App 进程里、没有任何前台服务兜着**的协程。App 进后台/锁屏之后
         * 进程被冻结，这个协程连同正在传的那一段一起停在那里，而且**没有任何东西会告诉用户
         * 它停了**（用户看到的就是"锁屏之后再解锁，得按一下重试"）。
         * 现在这里只做一件事：把"该同步了"翻译成一次 `ACTION_SYNC`（**前台**服务），
         * 于是补传期间进程是前台的、CPU/WiFi 有锁，锁屏也传得完。
         *
         * 三条判据一个都不能少（少了就会变成"每几秒拉一次前台服务"）：
         *  * 没有待传 → 直接 return（**不起服务、零通知**）；
         *  * 已经有一轮在跑（`syncGate`）→ return（不重复起）；
         *  * 没到退避时间 → return（断网时不会把电池和流量烧光）。
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
            // 顺手把会议页那块入口的数据源刷新一次：**它读的是磁盘上既有清单**，
            // 所以每一轮 tick 之后"待上传 N 段"都会跟着真实情况走（而不是等界面自己再算）。
            // 这里直接用刚读到的 `waits`（它本来就是 `needsSync` 过滤过的同一批），
            // 不再多扫一遍目录。
            _pendingSummary.value = pendingSummaryOf(waits)
            // 顺手也刷一次"本机建过哪些会话"：判"服务器上那场是不是我的"要用它
            // （`MeetingOwnership`）。**必须扫全量**（`list()`）而不是上面的 `waits`：
            // 身份看的是"我建过没有"，`waits` 只含"还欠段"的那几场 ——
            // 一场已经传完、只差收尾的会漏掉它，界面就会把它说成"另一台设备在录"。
            runCatching { publishLocalDrafts(store.list()) }
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
            runCatching {
                startForegroundServiceCompat(
                    app, Intent(app, MeetingMicService::class.java)
                        .setAction(ACTION_SYNC)
                        // 后台节拍发起的：失败不该弹通知（见 EXTRA_AUTO_SYNC 的说明）
                        .putExtra(EXTRA_AUTO_SYNC, true))
            }.onFailure { e ->
                // Android 12+ 不许**从后台**起前台服务（锁屏之后这条节拍可能就撞上它）。
                // 如实说一句：不许让用户以为"它还在传"。
                android.util.Log.w(TAG, "起补传前台服务被系统拒绝：${e.javaClass.simpleName} ${e.message}")
                _state.value = _state.value.copy(
                    syncNote = "系统不允许现在开始上传（后台/锁屏限制）：解锁后会接着传")
                notifyIfRunning()
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
         * 逐段进度 → 补传前台通知（**只在服务活着、而且真的在补传时**才有效果）。
         *
         * 为什么包一层：`runOfflineSync` 是伴生对象里的函数（它只拿到 `Context`），
         * 而"那条通知现在挂没挂"只有**活着的服务实例**知道。没有实例 = 没有前台 = 没有通知
         * 可刷 —— 那就什么都不做（`SyncForeground.progress` 里 `active == false` 时本来就是空操作，
         * 于是边录边传那种"没有补传前台"的路径不会凭空多出一条通知）。
         */
        private fun publishSyncProgress(p: SyncProgress) {
            runCatching { liveService?.onSyncProgress(p) }
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
                // ⚠️ **两个**客户端（2026-10-19 真机：传完第 1 段就停住）：
                //  * `ApiClient.create(baseUrl)` 读 60 秒 —— 查状态/建会/收尾这些**必须快**的接口用它；
                //  * `ApiClient.createForUpload(baseUrl)` 读/写都 960 秒 + 单段总预算 —— **只**传段。
                //    一段 10 分钟的 WAV 是 ≈18.3 MB、还要过中继；原来它和上面那些共用 60 秒写超时的
                //    客户端，第 2 段会被"60 秒没有进展"判死，而下一轮又从这一段的第 0 个字节重发
                //    （body 不能续传）→ 大段永远传不完，`seq` 推不到 3，`stop` 永远不发。
                val transport = OfflineMeetingTransport(
                    api = ApiClient.create(baseUrl),
                    uploadApi = ApiClient.createForUpload(baseUrl),
                )
                val active = ActiveDraft.current
                // 这一轮**真的开始了**：先翻到"同步中"。
                // 为什么必须在 `run` 之前：从用户按下列到第一段传完之间（探活 + 建会 + 读段
                // 都可能要好几秒），界面上那块入口一直显示"待上传 N 段" + 可点的按钮
                // —— 用户会以为没按上，于是再按一次（拿到"上一次还在跑"）。
                _state.value = _state.value.copy(
                    syncPhase = OfflinePhase.UPLOADING,
                    syncNote = if (manual) "正在同步…" else "正在自动补传…",
                )
                val result = OfflineMeetingSync.run(
                    transport = transport,
                    store = store,
                    activeClientId = active?.clientId,
                    onlyClientId = onlyClientId,
                    manual = manual,
                    force = manual,
                    scope = scope,
                    // 逐段进度（只影响展示）：一轮可能几分钟，界面必须看得到"已传 M/N、第 K 段"。
                    onProgress = { p ->
                        _state.value = _state.value.copy(
                            syncPhase = OfflinePhase.UPLOADING,
                            syncConfirmed = p.confirmedSegments,
                            syncTotal = p.totalSegments,
                            syncPending = p.pendingSegments,
                            syncDrafts = p.drafts,
                        )
                        notifyIfRunning()
                        // 补传那条**前台通知**每段动一次（锁屏时用户只看得到它）：
                        // 这一段开始传 → "已传 2/5 段（第 3 段 12.4 MB）"
                        publishSyncProgress(p)
                    },
                    // 每段**真的到服务端**之后立刻刷一次真数（用户 2026-10-19：*"传了一段，后面好像还是不行"*）
                    //
                    // 为什么"段开始"那一报不够：一段 18 MB 的 WAV 传完要几分钟，而数字原来只在
                    // **下一段开始**前才更新 —— 最后一段传完到整轮结束（对账 + stop）那段时间里，
                    // 界面上还是旧数（"已传 2/3"，可 3 段其实都到了）；只有一段时更是整轮都不动。
                    // 这里只改**数字**：文案仍然只有一处出口（`offlineStatusLine`，下一轮/结束时重写），
                    // 免得两处各说一套。
                    onSegmentDone = { p ->
                        _state.value = _state.value.copy(
                            syncPhase = OfflinePhase.UPLOADING,
                            syncConfirmed = p.confirmedSegments,
                            syncTotal = p.totalSegments,
                            syncPending = p.pendingSegments,
                            syncDrafts = p.drafts,
                        )
                        notifyIfRunning()
                        // 这一段**已经到服务端**了：通知里的"已传 M/N"当场就变（不是只在下一段开始才变）
                        publishSyncProgress(p)
                    },
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
                    syncTotal = result.totalSegments,
                    syncDrafts = result.drafts,
                    sent = maxOf(_state.value.sent, result.confirmedSegments),
                    error = "",
                )
                // 同步这一轮过去了：**顺手**按有界保留策略清一次（只清"已对账 + 已收尾 + 到期"的）
                runCatching { purgeExpiredAudio(app, store) }
                // 这一轮把水位推了/把一场传完了：会议页那块入口的"待上传 N 段"要立刻跟上
                // （只靠 tick 刷的话，用户点完「现在同步」会看到旧数字一会儿 —— 那很像"没反应"）。
                refreshPendingSummary(app)
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

    /**
     * 录音那条前台**是不是挂着**。
     *
     * 为什么补传要知道它：录音中顺手补传时，前台那条通知必须仍然是"录音"那条
     * （换成"正在上传会议录音"用户会以为录音停了），而且**收尾时不许撤**（撤了录音就没前台了）。
     * 见 `SyncForegroundAndroid.startForeground` / `SyncForeground.finish`。
     */
    @Volatile private var micForeground = false

    /**
     * 补传的"前台 + 锁"（`lazy`：服务只是被顺手拉起来做一次「现在同步」时，
     * 到真正要进前台那一刻才建 —— 没东西要传的那条路径上它一次都不会被创建）。
     */
    private val syncForeground: SyncForeground by lazy {
        SyncForeground(SyncForegroundAndroid(
            service = this,
            scope = scope,
            recording = { micForeground },
            recordingNotification = { buildNotification() },
            recordingNotifId = NOTIF_ID,
        ))
    }

    /**
     * 一轮补传的结局 → 收尾通知那一句（**唯一**的转换处）。
     *
     * 判据全部取自 `MicState` 的最终那一档（`runOfflineSync` 结束时刚写过它）：
     *  * `DONE` → 完成；`stopped` 才敢说"开始转写"（没有收尾就没有转写）；
     *  * `IDLE` / 已经没有欠的段 → 没什么可说的（**不发通知**）；
     *  * 其余 → 失败，原因用 `syncNote`（连不上 / 413 / 服务端拒收 / 对账不符，各说各的）。
     *
     * [quietFailure] = 后台节拍自动起的那一轮：**失败不发通知**（[SyncEnd.Quiet]）。
     * 理由：那种轮次每几分钟就会来一次，失败通知会变成骚扰；而原因已经写在会议页那块
     * 入口里（`syncReasonText` 分档说人话），用户点开就看得到。**成功照样要发**。
     */
    private fun syncEndOf(outcome: RetryOutcome, quietFailure: Boolean = false): SyncEnd {
        if (outcome == RetryOutcome.SkippedBusy) {
            return SyncEnd.Nothing("上一次同步还在跑，这一次没跑")
        }
        val st = _state.value
        return when {
            st.syncPhase == OfflinePhase.DONE -> SyncEnd.Done(
                segments = st.syncConfirmed,
                drafts = st.syncDrafts.coerceAtLeast(1),
                // `OfflineSyncState.stopped` 在 publish 时是 `OfflineSyncResult.stopped`
                transcribing = OfflineMeetingSync.state.value.stopped,
            )
            st.syncPhase == OfflinePhase.IDLE && st.syncPending == 0 ->
                SyncEnd.Nothing("现在没有要传的录音")
            quietFailure -> SyncEnd.Quiet(st.syncNote.ifBlank { "自动补传没成（原因见会议页）" })
            else -> SyncEnd.Failed(st.syncNote.ifBlank { "补传没成（这次没拿到原因）" })
        }
    }

    /**
     * 一次封段任务：PCM 已经从采集缓冲**拷出来**，编码/落盘交给单消费者按序做。
     *
     * [ack] 非空 = 这是一个"排空标记"（不是真段）：`stopEverything` 用它等前面
     * 排队的整段全部落完，再封尾段 —— 顺序反了尾段会排到整段前面（序号与时间轴都乱）。
     */
    private data class SealJob(
        val session: MeetingRecordSession?,
        val pcm: ByteArray,
        val ack: CompletableDeferred<Unit>? = null,
    )

    /**
     * 封段队列：**单消费者**保证段序（段序号不能乱），容量很小（正常时永远是空的）。
     *
     * 为什么编码必须搬出采集协程（2026-10-18 段长默认 600 秒时）：一段 10 分钟 ≈19 MB，
     * FLAC 编码要好几秒；而采集协程要每 100ms 调一次 `ar.read`，停那么久
     * = AudioRecord 的缓冲被顶掉 = **真的丢音频**（旧版 30 秒一段编码很快，
     * 放在一起没出事；600 秒把这个停顿放大了 20 倍）。网络那一下早就是异步的，
     * 编码这一下现在也挪开；采集循环只做"拷贝 + 入队"。
     */
    private val sealQueue = Channel<SealJob>(capacity = 2)
    private var sealWorker: Job? = null

    /**
     * 已经在收尾了（`stopEverything` 只该跑一次）。
     *
     * 为什么需要它：收尾现在会**把尾段缓冲封成一段**。`ACTION_STOP` 完全可能到两次
     * （通知里的「结束」+ 界面上的「结束」，或系统重投），第二次再封一遍就是
     * **同一段音频在清单里出现两遍**（转写里同一句话出现两遍）——手机这边完全静默。
     * 所以第一下占住它，后来的一律忽略；新的一场录音开始时才放开。
     */
    @Volatile private var stopping = false

    /**
     * 采集循环正在攒的、**还不到一段**的样本（结束时由 [sealFinalTail] 封成尾段）。
     *
     * 为什么它必须是服务的一个字段（而不是 `runLoop` 里的局部变量）：原来的形状是
     * "缓冲是局部的、一结束就没了" —— 而段长默认 600 秒（10 分钟），用户"**录 20 秒就停**"时
     * 缓冲里那 20 秒**一段都没攒满**，整段录音于是凭空消失（既不建会也不上传）。
     * 段长越长，这条尾巴越常见（默认 10 分钟时几乎每场短会都要靠它）。
     * 现在 `stopEverything` 在 `cancelAndJoin` 之后从这里把尾巴封出去。
     *
     * ⚠️ 只在**旧那一场跑完并 join 之后**读（那时采集协程已经不再写它）；重复收到
     * `stop` 时会把它置空，避免同一段音频被封两遍（转写里同一句话出现两次）。
     */
    @Volatile private var captureTail: PcmBuffer? = null

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
        // 封段单消费者：**串行**编码/落盘（见 [sealQueue] 的说明）。
        // 每个任务都包一层 runCatching：一个段编码抛异常不能把整条封段线弄死
        // （弄死之后后面的段永远不落盘，而界面只会显示"在录"）。
        sealWorker = scope.launch {
            for (job in sealQueue) {
                val ack = job.ack
                if (ack != null) {
                    ack.complete(Unit)
                    continue
                }
                runCatching {
                    val seal = job.session?.seal(job.pcm)
                    if (seal != null) publishSeal(seal)
                    // 每封一段顺手试一次上传（异步、绝不阻塞封段线）
                    triggerOpportunisticUpload()
                }.onFailure {
                    android.util.Log.e(TAG, "封段失败（编码/落盘）：${it.message}")
                }
            }
        }
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
                // ## 上传是**前台任务**（用户 2026-10-19 深夜："锁屏后，再解锁就要按一下重试"）
                //
                // 这条分支是补传的**唯一**入口（「现在同步」按钮、通知里那颗、以及后台节拍
                // 转过来的 `ACTION_SYNC`）。它原来只是"顺手起个后台服务"，于是：
                // 屏幕一关 → 进程进后台被冻结/掐掉 → 正在传的那一段断在那里 → 解锁只能手点重试。
                //
                // 现在的形状：**一开同步就进前台 + 拿锁**（`syncForeground.guard`），
                // 传完 / 失败 / 抛异常都在它的 `finally` 里撤干净（后台+前台一起收）。
                // 判据见 `SyncForegroundTest`。
                //
                // 三条"没东西可做"的出口都必须**在进前台之前**（否则会留下一条通知）：
                //  * 清单空了（可能就在这几毫秒里被另一条路传完了）→ 不起前台，直接收服务；
                //  * 已经有一轮在跑（`syncGate`）→ 不重复进前台，如实说一句"这次没跑"。
                val pending = runCatching { draftStore.pending() }.getOrDefault(emptyList())
                // 后台节拍发起的（不是用户按的）：文案与"失败要不要发通知"都要跟着变
                val auto = intent.getBooleanExtra(EXTRA_AUTO_SYNC, false)
                if (!shouldEnterSyncForeground(pending.size, PairStore.isPaired(this@MeetingMicService)) ||
                    syncGate.running
                ) {
                    if (syncGate.running) {
                        _state.value = _state.value.copy(
                            syncNote = "上一次同步还在跑，这一次没跑（等它完再按）")
                    }
                    if (job == null) stopSelf()
                    return@launch
                }
                try {
                    syncForeground.guard(
                        totalSegments = pending.sumOf { it.segments.size },
                        totalBytes = pendingBytesOf(draftStore, pending),
                        block = {
                            runOfflineSync(this@MeetingMicService, manual = !auto,
                                scope = SyncScope.FINALIZE)
                        },
                        endOf = { outcome ->
                            // 自动那一轮**失败不发通知**（每几分钟弹一条"补传没成"就是骚扰：
                            // 原因在会议页那块入口里已经写着）；成功仍然要告诉用户一声。
                            syncEndOf(outcome, quietFailure = auto)
                        },
                    )
                } catch (c: kotlinx.coroutines.CancellationException) {
                    // 服务被销毁/作用域被取消：**照原样往外抛**（不许把取消吞成"正常结束"）
                    throw c
                } catch (t: Throwable) {
                    // `guard` 会把异常照原样抛回来（它只负责放锁/收前台）。
                    // 这里**必须**接住：`scope.launch` 里漏出去的异常会走默认处理器 → **App 崩**，
                    // 而真相只是"这一轮补传没成"（清单与音频都在，下一轮接着传）。
                    android.util.Log.e(TAG, "补传这一轮抛了异常：${t.javaClass.simpleName} ${t.message}")
                    _state.value = _state.value.copy(
                        syncNote = "补传这一轮出错了（${t.javaClass.simpleName}）：音频都还在手机里，下次接着传")
                    notifyIfRunning()
                } finally {
                    // 「现在同步」可能在服务只是被顺手拉起来的时候发生（会议页那块常驻入口
                    // 在没有录音的时候也能点）。同步跑完就把服务收掉 —— 否则每点一次就留下一个
                    // 常驻的后台服务。⚠️ 还在录（job != null）时**绝不能停**。
                    // 收服务就顺带把前台撤了（`onDestroy` 里还有一道 releaseAll 兜底）。
                    if (job == null) stopSelf()
                }
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
     * ## 这是**唯一**的录音入口（手机发起 = 手机录）
     *
     * 界面上那颗「开始录音」（`MeetingsTab` → `MainViewModel.startMeeting()`）与旧版
     * 「用手机麦克风」那颗按钮**现在就是同一件事**：都只调 [MeetingMicService.start]。
     * 旧的 `MainViewModel.startMeeting()` 发的是无参数的 `POST /api/meeting/start`
     * （服务端于是开**电脑**那支麦）—— 真机上表现成"手机点了开始、电脑在录"，
     * 转写里混进电脑那边的人声。那条路已经退役，**别再**在这里或 ViewModel 里把它接回来。
     *
     * ## 为什么这里**不探活、不建会、不看服务端脸色**
     *
     * 用户原话：*"离线录音和在线录音就没什么本质区别了"*。于是"开始录音"只做一件事：
     * **在本地把这一场立起来**（清单 + 第一段以后逐段落盘）。
     * 好处不只是"断网也能录"（那是 2026-10-16 就有的），更是**行为一致**：
     * 网络好不好都是同一条代码路径，没有"探活超时 1.5 秒才决定走哪条路"这种分叉，
     * 也没有"联网那条先在电脑上开一场会、失败了再退到离线"这种会留下半场会的中间态。
     *
     * 而且**建会请求刻意延后到第一段音频真的落盘之后**（见 [triggerOpportunisticUpload]）：
     * 这一行的 `clientId` / `startedAt` 就是那条统一路要交上去的身份（`source=phone`）。
     * 先建会再录音的话，服务端会出现"一场手机麦的会，一个音频段都没有"—— 空会占着
     * 唯一那条录音槽，之后谁都开不了新会。**没有本地音频，就不该有这场会。**
     *
     * 唯一还能让"开始录音"失败的东西是**麦克风权限**（那是真的不能录）。
     */
    private fun startRecording() {
        if (job != null) return
        // 新的一场：上一次的"正在收尾"标记要放开（否则这一场按结束会被忽略）
        stopping = false
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            failAndStop("没有麦克风权限：先在 App 里给它")
            return
        }
        // 上一条**被进程杀掉**留下的清单：按最后一段的时间收尾（如实标出这是推断），
        // 它接下来会被后台补传捡走 —— 别让它永远停在"还在录"（那种清单谁都不敢收尾）
        val interruptedNote = closeInterruptedDrafts()

        val now = System.currentTimeMillis()
        val s = MeetingRecordSession(
            store = draftStore,
            encoder = FlacSegmentEncoder(SAMPLE_RATE, CHANNELS),
            sampleRate = SAMPLE_RATE,
            channelCount = CHANNELS,
        )
        val started = s.start(MeetingDraftStore.newClientId(), MeetingDraftStore.isoOf(now))
        session = s
        // **开录时读一次**段长（设置页 15/30/60/300/600 秒）：中途改偏好不会把已经在缓冲的那半段
        // 切碎，界面文案也写着"改完下次开录生效"。这一份同时交给 runLoop 与状态，
        // 于是"界面显示的本段进度"与"真正封段用的样本数"**永远是同一个段长**。
        val segSeconds = segmentSeconds(this)
        // 上一场已经结束：把尾段缓冲清掉（`runLoop` 马上会换上这一场自己的那一个）
        captureTail = null
        wavSegments = 0
        startForegroundCompat()
        // 这两句都是"必须让人看见"的降级说明：上一条被打断的录音、以及这一份清单读不懂
        // 另起了一份。它们不能被下面那行"本机录音中"的状态盖掉（以前就是被盖掉的）。
        val notices = listOf(interruptedNote, started.note).filter { it.isNotBlank() }
        _state.value = MicState(
            active = true,
            syncPhase = OfflinePhase.RECORDING_LOCAL,
            syncNote = (notices + offlineStatusLine(OfflineSyncState(
                phase = OfflinePhase.RECORDING_LOCAL,
                clientId = started.draft.clientId,
                totalSegments = started.draft.segments.size,
                pendingSegments = started.draft.pendingSegments,
                confirmedSegments = started.draft.confirmedSegments,
            ))).filter { it.isNotBlank() }.joinToString(" · "),
            startedAt = started.draft.startedAt,
            // 界面要显示"本段 mm:ss / mm:ss"：段长就是**开录这一刻**读到的这一档
            // （与 runLoop 里那一次必须同一个值，否则界面会按另一个段长报进度）
            segmentSeconds = segSeconds,
        )
        notifyState()
        // 把**开录那一刻**交下去：累计时长与清单里的 `started_at` 必须是同一把尺子
        job = scope.launch { runLoop(now, segSeconds) }
    }

    /**
     * 把**上一条被进程杀掉**的清单收尾：结束时间按最后一段推（**如实标出这是推断**）。
     *
     * 为什么要在这里做：那种清单的 `ended_at` 是空的 → 它会被这条路的规则判成"还在录"
     * → **永远不许收尾**（见 `SyncScope`）→ 音频永远传不到服务端、永远不出文字。
     * 而进程已经被杀过一次，那场录音**确实已经结束**了（结束时刻只能推断）。
     *
     * ⚠️ **进程被杀时补不了尾段**（`sealFinalTail` 只在用户按结束时跑）：那不足一段的
     * 缓冲在进程里，跟着进程一起没了。这件事**必须如实说**，绝不因为"清单收尾了"
     * 就让人以为整场都还在（见 [inferredEndNote] 与返回的提示）。
     *
     * 返回一句**要显示给人的话**（空 = 没有被丢弃的录音）。它会被 `startRecording`
     * 合并进这一场的状态里 —— 以前那句 `syncNote(...)` 紧接着就被新的 `MicState` 盖掉了，
     * 用户根本看不到（等于静默）。
     */
    private fun closeInterruptedDrafts(): String {
        val notes = ArrayList<String>()
        val unfinished = runCatching {
            draftStore.list().filter { it.recording }
        }.getOrDefault(emptyList())
        unfinished.forEach { d ->
            if (d.segments.isEmpty()) {
                // 进程在第一段攒满之前就被杀了：缓冲里的那一小段**没封存、也救不回来**。
                // 这一份清单必须收尾（否则永远停在"还在录"），而它**没有可上传的音频**，
                // 所以也不会建会 —— 但"这次什么都没留下"必须说出来。
                // ⚠️ 措辞不写死"进程被杀"：麦克风起不来（`failAndStop`）也会留下这种
                // 0 段的清单，而**原因我们此刻分不出来**（两种都说成被杀就是编原因）。
                draftStore.save(d.copy(endedAt = d.startedAt, endedAtInferred = true))
                notes += "上一条录音在第一段攒满之前就中断了：没有留下可上传的音频" +
                    "（不足一段的缓冲没来得及封存）"
                return@forEach
            }
            val last = d.segments.maxByOrNull { it.seq } ?: return@forEach
            val endIso = MeetingDraftStore.isoOf(
                (MeetingDraftStore.epochOf(last.startedAt) ?: System.currentTimeMillis()) +
                    (last.durationSeconds * 1000).toLong())
            // 结束时间**是按最后一段推出来的**（不是当时真的按了结束）—— 用**结构化**的
            // `endedAtInferred` 记下来（而不是塞一句备注）：它必须能被每一次同步如实说出来
            draftStore.save(d.copy(endedAt = endIso, endedAtInferred = true))
            notes += "上一条录音（${d.segments.size} 段）是被打断的：结束时间按最后一段推的" +
                "（不是当时按的结束），剩下的段会继续传；" +
                "**最后不足一段的缓冲没来得及封存**（进程被杀时补不了尾），那一小段可能丢了"
        }
        return notes.joinToString(" · ")
    }

    /**
     * 采集循环。
     *
     * [startedAtMs] 是**开录那一刻**的挂钟（`startRecording` 里与清单的 `started_at` 用同一个值）
     * —— 累计时长与清单里的时间轴必须是**同一把尺子**，否则界面上的计时会与会议实际时长对不上。
     *
     * [segSeconds] 是开录时读到的那一档段长（15/30/60/300/600）。**只读一次**：中途改设置不影响
     * 正在录的这一场，而且界面上的"本段进度"用的是同一个数，不会出现两边各说一套。
     */
    private suspend fun runLoop(startedAtMs: Long, segSeconds: Int) {
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
        // 段长由 `startRecording` 读好交下来（**只读一次**，见方法说明）
        val samplesPerSegment = SAMPLE_RATE * segSeconds
        // 缓冲的容量按"装得下一整段"给。**必须是原始 ShortArray 的 [PcmBuffer]**：
        // 默认 600 秒 = 960 万个样本，`ArrayList<Short>` 装箱后要 ≈190 MB（手机上直接 OOM），
        // 而 PcmBuffer 只占 19 MB —— 600 秒这个默认值让"装箱"从"慢一点"变成"录不下去"。
        // 段长由硬件帧长整除（100ms 帧 vs 各档段长 15/30/60/300/600 秒），所以一个缓冲里通常只有一段。
        val buffer = PcmBuffer(samplesPerSegment)
        // 把它挂到服务上：**结束时**要把这里剩下的（不足一段的）样本补封成尾段
        // —— 不挂出去的话，"录 20 秒就结束"那 20 秒会跟着局部变量一起消失。
        captureTail = buffer
        val capture = MicCaptureLoop(samplesPerSegment)
        // 这一场的**累计时长**（挂钟；纯逻辑在 [MeetingElapsed]）。
        // 为什么它必须与"暂停"完全无关：暂停 = 照常录音、写全零，时间轴逐字连续、
        // 总时长不受影响 —— 既然时间在走，界面上的计时就不能冻住（真机问题①）。
        val elapsed = MeetingElapsed(clock = { System.currentTimeMillis() })
        elapsed.start(startedAtMs)
        try {
            ar.startRecording()
            notifyState()
            while (currentCoroutineContext().isActive) {
                val n = ar.read(frame, 0, frame.size)
                // 读不到数据（被系统掐了一下、或刚起来）也要推进计时：这一格代表的是
                // **挂钟**，不是"采到了多少"，所以它不能因为这一帧缺席而停住。
                if (n <= 0) {
                    _state.value = _state.value.copy(
                        elapsedSeconds = elapsed.tick(),
                        segmentElapsedSeconds = progressOf(buffer, SAMPLE_RATE),
                    )
                    delay(20)
                    continue
                }
                val paused = _state.value.paused
                // 采集语义全在这一句里（纯逻辑在 [MicCaptureLoop]）：暂停时它塞进去的是
                // **与这一帧同长度、同格式的全零样本**，而"够一段就封段"那件事照样发生
                // —— 所以段边界仍落在挂钟刻度上、总时长照常累计。
                // 于是"第 N 段 = 挂钟第 N 个段长（默认 10 分钟）"这条对齐关系在暂停前后都成立。
                val feed = capture.feed(buffer, frame, n, active = !paused)
                if (feed.sealed) {
                    val pcm = toPcmBytes(buffer)
                    // ① **先落本地**（编码 + 写盘 + 记清单）—— 这一步不碰网络，
                    //    所以它绝不会因为"网断了"而失败；它失败只有一种原因：手机写不进去。
                    //    ⚠️ 暂停期间的段也会走到这里（里面是零样本），照常编码/落盘：全零音频
                    //    编出来还是全零，而"少一段"会让整条时间轴前移（那正是要避免的）。
                    //
                    // 编码**不在这条采集协程里**：600 秒一段编码要好几秒，停这么久不 `ar.read`
                    // = AudioRecord 缓冲被顶掉 = 真的丢音频（单消费者按序做，见 [sealQueue]）。
                    sealQueue.send(SealJob(session, pcm))
                    // ② 顺手试一次上传由封段线在落盘之后做（异步、绝不阻塞采音：`ar.read` 是同一个
                    //    协程，一次卡住的网络请求会把麦克风缓冲顶掉 —— 那是**真的丢音频**）。
                    buffer.clear()
                }
                // ③ 这一帧的"事实"一次刷新：**累计时长（挂钟）** + 本段进度 + 电平。
                //
                // 为什么这三个要在**同一处**更新：界面的重组只由状态变化驱动，而暂停时
                // 电平是**刻意不动**的（零帧不该让电平条抖）。旧版于是暂停期间一次都不重组
                // → 计时冻住（真机问题①）。现在时长与本段进度每帧都变，界面就一直在刷新，
                // 用户能看到"时间还在走、这一段还在攒" —— 也就不会再误判
                // "恢复后不到一分钟就封了第一段"（其实那一段早就按挂钟攒满了，见 [MicCaptureLoop]）。
                val cur = _state.value
                _state.value = cur.copy(
                    // 电平：暂停时保留上一帧的值（不每 100ms 报一次 0）
                    level = if (paused) cur.level else rms(frame, n),
                    elapsedSeconds = elapsed.tick(),
                    segmentElapsedSeconds = progressOf(buffer, SAMPLE_RATE),
                )
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
     *
     * [isTail] 只有"结束时补出来的那一段"（[sealFinalTail]）才为 true。它决定下面那条
     * **段长判据**要不要生效 —— 见 [segmentLengthProblem] 的说明（"只有尾段允许不足一段"）。
     */
    private fun publishSeal(seal: SegmentSeal, isTail: Boolean = false) {
        val prev = _state.value
        // 段长判据（**正常封段**）：封下去的必须**正好是一个段长**。
        //
        // 为什么要在生产代码里也判一次：段是按**累计样本数**切的（暂停的零样本照常计数），
        // 所以"封下来的每一段恰好是 samplesPerSegment 个样本"本应是恒等式。
        // 一旦有人在采集/缓冲那里动了手脚（例如恢复时把缓冲清了、或者暂停期间丢帧），
        // 这个恒等式就破了 —— 而破掉之后的症状**只在真机上**表现为"不到一个段长就封段了"，
        // JVM 用例（如果只测纯函数）看不出来。所以这里在真机上也留一条响亮的日志。
        // ⚠️ 唯一允许不足一段的是**结束时的尾段**（isTail = true）—— 用户明确要"录 20 秒就停
        //    也不能丢那 20 秒"，那一段本来就短，不能按段长判。
        if (!isTail && seal.error.isBlank()) {
            val seg = seal.draft?.segments?.firstOrNull { it.seq == seal.seq }
            if (seg != null) {
                val problem = segmentLengthProblem(
                    seq = seal.seq,
                    seconds = seg.durationSeconds,
                    segmentSeconds = prev.segmentSeconds,
                )
                if (problem.isNotBlank()) android.util.Log.e(TAG, problem)
            }
        }
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
        // 重复的「结束」（通知 + 界面各按了一次）不许把尾段封两遍 —— 见 [stopping]。
        if (stopping) return
        stopping = true
        // 为什么要把这个原因记下来：结束时还静音着、或补传没传完，这两件事都要
        // 写在状态里让人看懂 —— 而那两句话的**上下文**就是"谁结束了录音"
        // （用户按的 / 通知里的结束 / 服务被系统收掉）。别让它变成一个没人读的参数。
        lastStopReason = reason
        val j = job
        job = null
        // 这一场自己的对象与缓冲**先抓住**：下面那段协程跑完之前，用户完全可能马上
        // 又按一次「开始录音」—— 那时 `session`/`captureTail` 已经是下一场的了。
        val s = session
        val tailBuffer = captureTail
        scope.launch {
            if (j != null) j.cancelAndJoin()

            // ① 排队里的整段先落完（**必须在尾段之前**）：不排空就封尾，尾段会抢在
            //    整段前面拿到序号（时间轴与转写顺序都会乱）。
            awaitSealQueueIdle()

            // ①' **先补齐最后不足一段的音频**（本次修的退步）。
            //
            // 为什么必须在 `finish()` **之前**：`finish()` 要写 `ended_at` 与总时长，
            // 而总时长必须包含这一小段；顺序反了这一段的时长就落在会议之外。
            //
            // ⚠️ 这是"结束时代替攒满一段补出尾段"，**不是**把段长改短：段长仍是
            // 用户设置（设置页 15/30/60/300/600，默认 600 秒 = 10 分钟，与电脑一致），
            // 正常录音的段边界一个都没动。
            // 只有"停止时还差最后一点"这种情况才会多出这样一段尾巴。
            //
            // 判据见 [sealFinalTail]：**有样本就必须留下**；只有真的一个样本都没有
            // （一按就停 / 立刻失败）才允许不建会 —— 而那时也必须如实说（见 stopNote）。
            val tail = sealFinalTail(s, tailBuffer?.toShortArray() ?: ShortArray(0), SAMPLE_RATE)
            // 尾段也可能**退回 WAV** / **落盘失败**：走同一个如实发布（绝不谎报格式、绝不吞错误）
            // ⚠️ `isTail = true`：**只有这一段**允许不足一个段长（段长仍是用户设置，
            //    这里只是把"结束时还差的那一点"补成一段，见 [sealFinalTail]）。
            tail.seal?.let { publishSeal(it, isTail = true) }
            if (tail.note.isNotBlank()) android.util.Log.i(TAG, tail.note)
            if (tail.error.isNotBlank()) android.util.Log.e(TAG, tail.error)
            // 这一段的缓冲已经封走了：清掉引用，**重复收到 stop 也不会再封一次**
            // （同一段音频封成两段 = 转写里同一句话出现两遍）。
            if (captureTail === tailBuffer) captureTail = null

            // ① **本地收尾立刻生效**：结束时间 + 总时长落盘（结束时还静音着的话，
            //    最后那段暂停也在这里收进清单 —— 否则会凭空多出一段"没人说话"的时长）。
            //    为什么先做这一步再传：用户的"结束"必须**当场**生效（本地不再采音、
            //    清单落盘），绝不能押在一次网络请求上（断网时那会变成"按了结束没反应"）。
            val done = runCatching { s?.finish() }.getOrNull()
            val totalSegments = done?.segments?.size ?: 0
            // 一个样本都没录到（一按就停 / 立刻失败）：**不建会**，但必须如实说 ——
            // 静默是最糟的（用户会以为录上了，只是还没转写）。
            val stopNote = tailStopNote(tail, totalSegments, hadSession = s != null)
            // 用户可能已经按了"开始录音"（stop 是异步的，而且现在还要等封段队列）：
            // 这时**不许**把新一场的 active 覆盖成 false（否则界面显示"已结束"、
            // 麦克风却还在录）。只有这一场仍是当前场时才发布收尾状态。
            val stopState = _state.value.copy(
                active = false, level = 0f, paused = false,
                stopNote = stopNote,
                syncPhase = when {
                    totalSegments == 0 -> OfflinePhase.IDLE
                    done != null && !done.uploaded -> OfflinePhase.WAITING_UPLOAD
                    else -> _state.value.syncPhase
                },
                syncPending = if (totalSegments == 0) 0
                else done?.pendingSegments ?: _state.value.syncPending,
                syncNote = if (totalSegments == 0) "" else offlineStatusLine(OfflineSyncState(
                    phase = if (done != null && !done.uploaded) OfflinePhase.WAITING_UPLOAD
                    else _state.value.syncPhase,
                    totalSegments = totalSegments,
                    pendingSegments = done?.pendingSegments ?: 0,
                    confirmedSegments = done?.confirmedSegments ?: 0,
                    note = "录完了，正在补齐剩下的段并让 ECHO 收尾",
                )),
            )
            if (session === s) {
                _state.value = stopState
                notifyState()
            }

            // ② **补齐剩余段 → 对账 → stop(clientId)**（这条路唯一的收尾动作）。
            //    为什么要**等**正在跑的那一轮（`waitForGateMs`）：每封一段都会顺手试一次上传，
            //    而"结束"很可能正好撞上它 —— 直接跳过的话，这一场就要等到下一次 tick
            //    才被收尾（用户以为已经结束了，服务端却还没收到 stop = **还没有转写**）。
            //    等待是**有界**的（见 FINALIZE_WAIT_MS）：等不到就如实说"这一次没跑"。
            //    ⚠️ 一个样本都没有时也照跑这一轮：它只处理**别的**还没传完的清单
            //    （这一份没有音频段，`pending()` 不会选中它），所以**不会**建出空会。
            //
            //    ⚠️ 这一轮也走 `syncForeground.guard`：用户按完「结束」就把手机锁屏塞兜里
            //    是**最常见**的动作，而这一轮可能要传好几分钟（最后几段 + 对账 + stop）。
            //    录音那条前台此刻还挂着（`micForeground == true`），所以 `begin` 只是把
            //    `dataSync` 类型补上去、拿到锁，**不会**换掉那条通知；`finish` 也不会撤它
            //    （`foregroundOwned == false`），撤前台仍然是下面那句 `stopForegroundCompat()`。
            val pendingNow = runCatching { draftStore.pending() }.getOrDefault(emptyList())
            val outcome = runCatching {
                if (shouldEnterSyncForeground(pendingNow.size, PairStore.isPaired(this@MeetingMicService))) {
                    syncForeground.guard(
                        totalSegments = pendingNow.sumOf { it.segments.size },
                        totalBytes = pendingBytesOf(draftStore, pendingNow),
                        block = {
                            runOfflineSync(this@MeetingMicService, store = draftStore,
                                manual = true, scope = SyncScope.FINALIZE,
                                waitForGateMs = FINALIZE_WAIT_MS)
                        },
                        endOf = { syncEndOf(it) },
                    )
                } else {
                    // 没有待传的场：**不进前台**（"零通知"）。这一轮只是走一遍清单（几乎立刻返回）。
                    runOfflineSync(this@MeetingMicService, store = draftStore,
                        manual = true, scope = SyncScope.FINALIZE,
                        waitForGateMs = FINALIZE_WAIT_MS)
                }
            }.getOrDefault(RetryOutcome.Ran)
            if (outcome == RetryOutcome.SkippedBusy) {
                // 如实：这次收尾**没跑**（另一轮占着），还有几段留着 —— 绝不假装传完了
                val left = s?.draft?.pendingSegments ?: 0
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
     * 等封段队列排空（**只在 stopEverything 里调**）。
     *
     * 为什么用"排空标记 + ack"而不是 `delay`：编码/落盘耗时不确定（10 分钟一段可能几秒），
     * 睡一个固定值不是等太久就是没等够。标记会被单消费者**按序**处理，所以 ack 回到时，
     * 前面所有整段都已经落盘。
     *
     * 有上界（[SEAL_DRAIN_TIMEOUT_MS]）：真撞上一个卡死的编码器，也不能让"结束"永远不返回
     * —— 超时只记日志，随后照常封尾段（宁可顺序冒一点风险，也不能让用户以为没结束）。
     */
    private suspend fun awaitSealQueueIdle() {
        val ack = CompletableDeferred<Unit>()
        val done = withTimeoutOrNull(SEAL_DRAIN_TIMEOUT_MS) {
            sealQueue.send(SealJob(null, ByteArray(0), ack))
            ack.await()
            true
        } ?: false
        if (!done) {
            android.util.Log.w(TAG, "封段队列排空超时（${SEAL_DRAIN_TIMEOUT_MS}ms）：尾段照常封")
        }
    }

    /**
     * 「结束前那次收尾」等待守卫的上限（毫秒）。见 [MeetingMicService.Companion.FINALIZE_WAIT_MS]。
     */
    private fun failAndStop(msg: String) {
        _state.value = MicState(active = false, error = msg)
        notifyState()
        // 起不来就撤：锁/前台一个都不许留（`releaseAll` 幂等，没拿过就是空操作）
        syncForeground.releaseAll()
        stopForegroundCompat()
        stopSelf()
    }

    override fun onDestroy() {
        runCatching { record?.release() }
        record = null
        if (liveService === this) liveService = null
        // 服务正在消失：把守卫放掉（幂等）。被取消的那一轮即使来不及走 finally，
        // 也不会让下一场录音里这个守卫一直是"在跑"。
        syncGate.leave()
        // ⚠️ **锁一定要在这里再放一次**：`SyncForeground.guard` 的 finally 覆盖了正常路径，
        // 但"服务被系统直接收掉"（`stopSelf` 之外的那些路）不会走那段 finally ——
        // 一个没放掉的 PARTIAL_WAKE_LOCK 会让用户手机整夜不睡（比传不完糟得多）。
        syncForeground.releaseAll()
        micForeground = false
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
        // 记下来：补传进前台时要知道"录音那条还挂着"（否则会换掉通知、结束时还会去撤它）
        micForeground = true
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION") stopForeground(true)
        }
        micForeground = false
    }

    private fun notifyState() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
        runCatching { nm.notify(NOTIF_ID, buildNotification()) }
    }

    /** 进程级那条补传路刷通知的入口（**只**在服务活着时被调用）。 */
    internal fun notifyStatePublic() {
        notifyState()
    }

    /**
     * 逐段进度 → 补传那条**前台通知**（用户点名的那句"已传 2/5 段（第 3 段 12.4 MB）"）。
     *
     * 为什么要有这个实例入口：`runOfflineSync` 在**伴生对象**里（它拿的是 `Context`，
     * 不是服务实例），碰不到 `syncForeground`；而"刷哪条通知、要不要刷"只能由
     * **活着的那一个服务实例**决定（没有实例 = 没有前台 = 没有通知可刷，那就什么都不做）。
     *
     * ⚠️ 它**只**更新通知，不参与任何判定（进度数字的判定全在 `OfflineMeetingSync`）。
     */
    internal fun onSyncProgress(p: SyncProgress) {
        runCatching { syncForeground.progress(p) }
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
 * 一段 PCM 的缓冲：**原始 `ShortArray`**（不是 `ArrayList<Short>`）。
 *
 * ## 为什么必须换掉 `ArrayList<Short>`（2026-10-18 段长默认改 600 秒时发现的）
 *
 * 默认段长 600 秒 = 960 万个样本。`ArrayList<Short>` 每个元素都是**装箱**的
 * `java.lang.Short`（对象头 + 引用 ≈20 字节）→ 一段要 ≈190 MB，Android 上直接 OOM；
 * 而 `ShortArray` 只要 19 MB。30 秒时代（48 万样本 ≈10 MB）这个坑不明显，
 * 600 秒把它变成了"录到第一段就崩" —— 所以这里不是优化，是让 10 分钟段长**能跑**。
 *
 * ## 为什么实现 `List<Short>`
 *
 * 判据（`progressOf` / `toPcmBytes` / 测试）本来就是按"一串样本"写的。
 * 实现只读 `List` 让那些**小缓冲**的用法一行不用改；生产路径走 [toShortArray] /
 * [toPcmBytes]，**不装箱**。测试里 `buffer.toList()` / `isEmpty()` 也照旧能用。
 */
internal class PcmBuffer(initialCapacity: Int = 0) : AbstractList<Short>() {

    private var data = ShortArray(initialCapacity.coerceAtLeast(0))
    private var count = 0

    override val size: Int get() = count

    override fun get(index: Int): Short {
        if (index < 0 || index >= count) {
            throw IndexOutOfBoundsException("index=$index size=$count")
        }
        return data[index]
    }

    /** 追加一个样本（暂停的零样本也走它 —— 长度与格式都不变）。 */
    fun addSample(sample: Short) {
        ensureCapacity(count + 1)
        data[count++] = sample
    }

    /** 追加一帧的一部分（真音频；整帧照抄，一个样本都不丢）。 */
    fun addSamples(frame: ShortArray, from: Int, until: Int) {
        val n = until - from
        if (n <= 0) return
        ensureCapacity(count + n)
        System.arraycopy(frame, from, data, count, n)
        count += n
    }

    /** 追加 [n] 个零样本（暂停帧：与真实帧**同长度**，只是内容全零）。 */
    fun addZeros(n: Int) {
        if (n <= 0) return
        ensureCapacity(count + n)
        java.util.Arrays.fill(data, count, count + n, 0.toShort())
        count += n
    }

    /**
     * 清空。
     *
     * ⚠️ 只有**封段的人**（`runLoop` / 结束收尾）能调：`MicCaptureLoop.feed` 满了也不清，
     * 否则调用方拿到的是空缓冲，封下去就是一段全零（真的丢音频，但界面看着正常）。
     *
     * 注意：这里继承的是只读的 `AbstractList`（不是 `AbstractMutableList`），
     * 所以它是**本类自己的**方法（没有 `override`）。
     */
    fun clear() {
        count = 0
    }

    /** 拷贝出这一段（封段要用；之后 clear 不会改到它）。 */
    fun toShortArray(): ShortArray = data.copyOf(count)

    /** 直接写成 16-bit 小端 PCM（**不装箱**；10 分钟那段就靠它）。 */
    fun toPcmBytes(): ByteArray {
        val out = ByteArray(count * 2)
        for (i in 0 until count) {
            val v = data[i].toInt()
            out[i * 2] = (v and 0xFF).toByte()
            out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    private fun ensureCapacity(need: Int) {
        if (need <= data.size) return
        var cap = if (data.isEmpty()) 16 else data.size
        while (cap < need) cap *= 2
        data = data.copyOf(cap)
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
 * 真机上帧长固定 100ms（1600 样本），而合法段长是 15–600 秒（240000/480000/960000/
 * 4800000/9600000 样本）
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
     * （1600 样本）能整除每个合法段长（15/30/60/300/600 秒 = 240000/480000/960000/
     * 4800000/9600000 样本），
     * 所以"封下去的每一段恰好是 `samplesPerSegment` 个样本"始终成立。
     * 万一参数变成不整除（帧比一段长），行为仍与旧代码一致：样本照收，段长最多多出一帧的量。
     */
    fun feed(
        buffer: PcmBuffer,
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
            if (active) buffer.addSamples(frame, n - left, n - left + take)
            else buffer.addZeros(take)
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
 * 这一场**已经录了多久**（挂钟，纯逻辑，JVM 逐条可测）。
 *
 * ## 为什么需要它（用户 2026-10-17 真机问题①："暂停时计时器也不刷新时间了"）
 *
 * 暂停的口径是**照常录音、写进去的是全零**（AGENTS.md「暂停语义（2026-10-16 深夜）」）：
 * 时间轴逐字连续、**总时长不受影响**。既然时间在走，界面上的计时就必须跟着走 ——
 * 而旧代码里界面的计时是"按当前挂钟算一次"的：界面的重组只由状态变化驱动，暂停时
 * 电平是**刻意不刷新**的（零帧不该让电平条抖），于是整块界面在暂停期间一次都不重组，
 * 计时看上去就冻住了。
 *
 * ## 为什么用"开始时刻 + 现在"而不是"累计采样数"
 *
 * 采样数在真机上会**少于**挂钟（麦克风被系统掐一下、编码占住采集协程都会丢帧）。
 * 用户要看的是"这一场录了多久"，而会议的总时长（[MeetingRecordSession.finish] 里的
 * `durationSeconds`）与清单里的 `started_at` 也是挂钟 —— **同一把尺子**，
 * 用采样数算会让界面比会议短一截，反而制造新的"对不上"。
 *
 * ## 它与暂停无关（这条最容易改错）
 *
 * [tick] 由采集循环**每一帧**都调一次：暂停帧照常调、`ar.read` 没读到数据的那一帧也照常调。
 * 所以"暂停期间计时停住"这件事在逻辑层就不可能发生，界面只是如实显示它。
 */
internal class MeetingElapsed(
    private val clock: () -> Long = { System.currentTimeMillis() },
) {
    private var startedMs: Long? = null

    /** 开录那一刻（与清单 `started_at` 是同一个时刻）。 */
    fun start(atMs: Long = clock()) {
        startedMs = atMs
    }

    /** 现在累计了多少秒（每帧调一次；**暂停也调**）。没开始过就是 0。 */
    fun tick(nowMs: Long = clock()): Double {
        val start = startedMs ?: return 0.0
        return maxOf(0.0, (nowMs - start) / 1000.0)
    }
}

/**
 * 缓冲里已经攒了多少秒（"本段进度"那条线；纯函数，JVM 可测）。
 *
 * 为什么不夹到段长：调用方（界面）自己夹 —— 这里只是"缓冲里真的有多少"。
 * 段边界由 [MicCaptureLoop] 按累计样本数决定，**暂停的零样本也算在内**，
 * 所以这个进度在暂停期间照常增长（这正是要让用户看见的事实）。
 */
internal fun progressOf(buffer: List<Short>, sampleRate: Int): Double {
    if (sampleRate <= 0 || buffer.isEmpty()) return 0.0
    return buffer.size.toDouble() / sampleRate
}

/**
 * [PcmBuffer] 版本（生产路径走它）。
 *
 * 为什么单独一个重载：`PcmBuffer` 也是 `List<Short>`，但按 `List` 迭代会**逐样本装箱**
 * —— 采集循环每 100ms 就要算一次进度，600 秒段长下那会是每秒几十万次装箱。
 * 这个重载只看 `size`，一个对象都不分配。
 */
internal fun progressOf(buffer: PcmBuffer, sampleRate: Int): Double {
    if (sampleRate <= 0 || buffer.size == 0) return 0.0
    return buffer.size.toDouble() / sampleRate
}

/**
 * **正常封段**（不是结束时的尾段）的段长判据：必须**正好是一个段长**。
 *
 * 为什么这是一条判据而不是一句注释：段是按**累计样本数**切的（暂停的零样本照常计数），
 * 所以"每一段封下去都恰好是 `samplesPerSegment` 个样本"本应是恒等式。一旦有人在
 * 采集/缓冲那里动了手脚（恢复时把缓冲清了、暂停期间丢帧、段长取错档），这个恒等式就破 ——
 * 而症状只在**真机**上表现为"不到一个段长就封段了"（用户 2026-10-17 的问题②）。
 *
 * 唯一允许不足一段的是**结束时的尾段**（[sealFinalTail]）：用户要"录 20 秒就停也不能丢
 * 那 20 秒"，那一段本来就短，所以调用方（[MeetingMicService.publishSeal]）只对非尾段判这一条。
 *
 * 返回空串 = 没问题（段长为 0 表示"还没读到设置"，那时不判 —— 不编一个假问题出来）。
 */
internal fun segmentLengthProblem(
    seq: Int,
    seconds: Double,
    segmentSeconds: Int,
    /** 允许的偏差（秒）：100ms 帧的量级，正常封段是**精确相等**，这里只留一点余量。 */
    toleranceSeconds: Double = 0.25,
): String {
    if (segmentSeconds <= 0) return ""
    // 时长读不出来（0 秒 = 清单里没有这一段）时不判：**不编一个假问题出来**
    if (seconds <= 0.0) return ""
    val want = segmentSeconds.toDouble()
    if (kotlin.math.abs(seconds - want) <= toleranceSeconds) return ""
    return "第 $seq 段只有 ${"%.2f".format(java.util.Locale.US, seconds)} 秒" +
        "（段长设置 $segmentSeconds 秒）：正常封段必须刚好一个段长" +
        "（暂停的零样本也算；只有结束时的尾段允许不足一段）"
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
    // ⚠️ 没有补传进度可说时，**不能沉默**：结束那一刻的事实（例如"这次没有录到音频"）
    // 要顶上来 —— 否则通知里只剩一句套话，用户以为录上了只是还没转写。
    !st.active -> "录音已结束 · " + st.syncNote.ifBlank {
        st.stopNote.ifBlank { "正在把剩下的段传给 ECHO…" }
    }
    // 有段没传上去 + 有真实原因（连不上 / 服务端拒绝）：先说它（那关系到音频会不会丢）
    st.syncPending > 0 && st.syncNote.isNotBlank() -> st.syncNote
    // 暂停那句话说清两件事（用户 2026-10-17 的两个真机问题都出在这句话的语义上）：
    // 内容不留在会议里 ✓，但**时间照常累计** ✓ —— 后者不写出来，用户就会以为"暂停把时长也停了"。
    st.paused -> "已静音（这段写空白，时长仍在累计）"
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

    /**
     * 上一次「结束录音」时**必须说给人听的那句话**（空 = 没什么要说的）。
     *
     * 为什么单独一个字段（而不是塞在 [syncNote] 里）：`syncNote` 是**补传那一轮**的
     * 状态，前台 tick 每几秒就会按真实进度重写它；而"这次没有录到音频"/
     * "最后一段没封存下来"是**结束那一刻的事实**，不能被下一轮 tick 抹掉
     * （抹掉 = 静默，用户会以为录上了只是还没转写）。见 [tailStopNote]。
     */
    val stopNote: String = "",

    // ---- 音频"传上去没有"（**唯一**的出口是 syncNote / offlineStatusLine）----

    /** 这条路现在到哪一档（本机录音中 / 等联网 / 在传 / 等服务端 / 对账完成…）。 */
    val syncPhase: OfflinePhase = OfflinePhase.IDLE,
    /** 要显示的那行字（**唯一**出口：`offlineStatusLine`）。 */
    val syncNote: String = "",
    /** 还有几段**本地音频没传上去**（>0 就该出现「现在同步」；这也是"会不会丢"的唯一判据）。 */
    val syncPending: Int = 0,
    /** 那几段里已经被服务端认下几段（界面用来说"已传 M 段"）。 */
    val syncConfirmed: Int = 0,
    /**
     * 这一轮要处理的总段数（界面那条 `已传 M / 共 T 段` 里的 T）。
     *
     * 为什么它必须来自**本轮进度**（`SyncProgress` / `OfflineSyncResult`）而不是界面自己数：
     * 磁盘摘要每 3 秒才刷一次，而"这一轮要传几段"只有同步自己知道（它按清单挑段）。
     * 0 = 还没开始过一轮（界面回落到磁盘摘要的总段数）。
     */
    val syncTotal: Int = 0,
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

    // ---- 累计时长（用户 2026-10-17 真机问题①：暂停时计时器停了）----

    /**
     * 这一场**已经录了多久**（挂钟秒数，**暂停期间照样增长**）。
     *
     * 为什么它必须是状态里的一格、而不是"界面自己按当前时间算一次"：
     * 界面的重组是由状态变化驱动的。旧版只有 `level` 会变（每 100ms 一次），而暂停时
     * **电平是刻意不刷新的**（零帧不该让电平条抖），于是整块界面在暂停期间**一次都不重组**
     * —— 用户看到的就是"按了静音，计时也停了"（真机原话）。
     * 现在采集循环每来一帧（含暂停帧、含没读到数据的帧）都推进这一格，计时于是与暂停无关。
     *
     * 为什么暂停也要增长：暂停 = **照常录音、写进去的是全零**，时间轴逐字连续、
     * 总时长不受影响（AGENTS.md「暂停语义（2026-10-16 深夜）」）；既然音频在累计、
     * 会议总时长在累计，界面上的计时就**不能**冻住 —— 冻住会让用户误判"这段没算进时长"，
     * 进而误判"才录了 40 秒就封段了"（真机问题②就是这么被看出来的）。
     */
    val elapsedSeconds: Double = 0.0,

    /**
     * 用户设置的一段多长（秒；15/30/60/300/600，开录那一刻读到的档）。
     *
     * 界面用它显示"本段 mm:ss / mm:ss" —— 段边界只由**累计样本数**决定，
     * 让用户看见"这一段攒到哪儿了"，就不会再有"恢复后不到一分钟就封段了"的错觉
     * （暂停期间零样本照常计数，所以本段进度在暂停时也照常走）。
     */
    val segmentSeconds: Int = 0,

    /** 正在攒的这一段**已经攒了多久**（秒；暂停的零样本也算进去）。 */
    val segmentElapsedSeconds: Double = 0.0,

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

/**
 * [ShortArray] 版本：**不装箱**（封段/尾段都用它）。
 *
 * 为什么要有它：10 分钟一段是 960 万个样本，走 `List<Short>` 那条会装箱 960 万次
 * （≈150 MB 的临时对象），在手机上就是一次超长 GC 甚至 OOM。
 */
internal fun toPcmBytes(samples: ShortArray): ByteArray {
    val out = ByteArray(samples.size * 2)
    for (i in samples.indices) {
        val v = samples[i].toInt()
        out[i * 2] = (v and 0xFF).toByte()
        out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
    }
    return out
}

/** [PcmBuffer] 版本（生产封段路径）：直接写字节，一个样本都不装箱。 */
internal fun toPcmBytes(buffer: PcmBuffer): ByteArray = buffer.toPcmBytes()

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
