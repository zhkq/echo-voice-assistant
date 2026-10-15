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
import android.media.audiofx.AcousticEchoCanceler
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.os.Build
import android.os.IBinder
import android.view.KeyEvent
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.echo.contact.MainActivity
import com.echo.contact.R
import com.echo.contact.audio.EchoCancel
import com.echo.contact.audio.EchoTtsPlayer
import com.echo.contact.audio.InputDevices
import com.echo.contact.audio.SystemTts
import com.echo.contact.audio.VoiceSegmenter
import com.echo.contact.audio.WavWriter
import com.echo.contact.data.ApiClient
import com.echo.contact.data.EchoApi
import com.echo.contact.data.PairStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * **常驻听写**（阶段5）：前台服务里一直开着麦，听到"一句话 + 静音收尾"就
 * 把这段 wav 交给 ECHO（`POST /api/assistant/voice-command`，裸 body），
 * 拿回它的答复，用**手机自己的 TTS**念出来 —— 全程不用解锁、不用点屏幕。
 *
 * 为什么需要它（与界面上那个"按住说一句"的区别）
 * ============================================
 * 界面里那次录音是**前台临时录音**（`audio/Recorder.kt` + `MainViewModel`）：
 * 界面一走就没了。而真实场景是**开车**：手机放在支架上/兜里、接着蓝牙耳机，
 * 人要的正是"抬手说一句就行"。这要求：
 *   * 录音活在**前台服务**里（App 切走、锁屏都不被杀）；
 *   * 有**常驻通知**（系统对麦克风型前台服务的硬要求，也是用户唯一能一键停掉的地方）；
 *   * 自己解决"一句话说完没有"（[VoiceSegmenter]），不能让人去点按钮。
 *
 * 几个刻意的取舍（写清楚，免得后来者以为是漏了）
 * ============================================
 * 1. **处理/朗读期间不收音**（`paused`）：否则手机把自己念的回复又听一遍，
 *    会陷入"自问自答"的循环。代价是这几秒里你说的话会丢 —— 换来的是不会自激。
 *    要真正做到"边播边听"需要 AEC（回声消除），那是另一件事（见 README「还没做」）。
 * 2. **不自动重启开麦**：`START_STICKY` 只在系统回收后重建服务壳，
 *    重建时**不会**偷偷再开麦 —— 麦克风这种东西，"用户没点头就不该自己开"。
 *    真被回收了，由用户在通知/界面里再点一次。
 * 3. **连不上就如实说**：网络错误、后端 4xx、转写为空，都会写进通知正文与状态，
 *    不静默吞掉（"说完没反应"是最难查的体验）。
 * 4. **一直没人说话就自己停**（[IdleAutoStop]，默认 20 分钟）：开着的是麦克风型前台服务，
 *    用户忘了关就是一晚上耗电 + 第二天还挂着"在听"。**只有真的送出一段才算有动静**，
 *    免得放一路音乐/风声就永远停不下来；停下来时原因写到卡片上（别让它"自己断了"）。
 */
class ListenService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var tts: SystemTts? = null

    /** 「ECHO 念」那条路的播放器（懒建；与系统 TTS 并存，按设置二选一）。 */
    private var echoPlayer: EchoTtsPlayer? = null

    /** 处理 + 朗读期间置位：这期间读到的音频**不喂给分段器**（见类注释的取舍 1）。 */
    @Volatile private var paused = false

    /** 回声消除实例（全双工时才有）：拿不到就是 null，也就是半双工。 */
    private var aec: AcousticEchoCanceler? = null

    /** 本次是否跑在全双工（念回复时也收音）。半双工时 `handleSegment` 会置 `paused`。 */
    @Volatile private var fullDuplex = false

    /** 用户主动暂停（耳机键/通知按钮）：服务还活着，但不采音。与 [paused] 是两回事。 */
    @Volatile private var userPaused = false

    /** 媒体会话（耳机/车机键的入口），只在服务活着时存在。 */
    private var mediaSession: MediaSession? = null

    /** 省电计时器（`runLoop` 里建，`setUserPaused` 要用它把"刚继续"算成有动静）。 */
    private var idle: IdleAutoStop? = null

    /** 只在线程里改，避免每次读帧都刷通知。 */
    @Volatile private var lastText: String = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null) {
            // 进程被杀之后，`START_STICKY` 会带一个 **null intent** 把服务拉回来。
            // 这时**绝不能自动开麦**（见类注释取舍 2）：麦克风只能由用户点头开。
            ListenState.reset(ListenState.Phase.OFF, "服务被系统重启过，要用请再打开开关")
            stopSelf()
            return START_NOT_STICKY
        }
        when (intent.action) {
            ACTION_STOP -> {
                stopEverything("用户停掉了后台听写")
                return START_NOT_STICKY
            }
            // 通知里的「暂停 / 继续」：服务已经在跑才有意义；不在跑就把这次启动退掉。
            ACTION_PAUSE -> {
                if (job != null) setUserPaused(true) else stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RESUME -> {
                if (job != null) setUserPaused(false) else stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                // 先把前台通知挂上（系统给 startForegroundService 的窗口只有几秒）
                if (!ensureForeground()) return START_NOT_STICKY
                if (job == null) {
                    ListenState.reset(ListenState.Phase.STARTING)
                    job = scope.launch { runLoop() }
                }
            }
        }
        // 用户在开着听写的情况下把 App 划掉：服务继续听（这正是"后台"的意义），
        // 系统回收后也只重建服务壳 —— 开麦仍然只由用户的开关决定（见上面 null intent 那条）
        return START_STICKY
    }

    override fun onDestroy() {
        stopEverything("服务被停止")
        super.onDestroy()
    }

    // ------------------------------------------------------------------ 前台

    private fun ensureForeground(): Boolean {
        if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            ListenState.reset(ListenState.Phase.FAILED, "没有麦克风权限：请先在「指令」页给权限")
            stopSelf()
            return false
        }
        createChannel()
        val n = buildNotification(ListenState.Phase.STARTING, "", 0)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(NOTIFICATION_ID, n)
            }
        } catch (e: Exception) {
            // Android 14 对麦克风型前台服务查得严（权限/后台启动限制），这里必须响亮失败
            ListenState.reset(ListenState.Phase.FAILED, "开不了前台服务：${e.message}")
            stopSelf()
            return false
        }
        ensureMediaSession()
        return true
    }

    /**
     * 媒体会话：让**耳机线控键 / 车机媒体键**能控制听写（暂停 ↔ 继续、停止）。
     *
     * 为什么用框架自带的 `android.media.session.MediaSession` 而不是 androidx 的
     * `MediaSessionCompat`：这一条不需要引入新依赖（`androidx.media` 不在本项目的依赖里），
     * 而 API 21+ 的框架类足够用 —— 本项目 minSdk 26。
     *
     * ⚠️ 会话只在服务活着时存在：耳机键**不能**把没开过的听写叫起来（见 `ListenKeys` 的说明）。
     *
     * **能不能用是 App 自己的选项**（`AppPrefs.mediaKeys`，设置 → 按键与快捷方式）：
     * 关掉就**不建会话** —— 连"按一下有反应"都不该有。这条只在服务启动时读一次，
     * 所以设置页写着"改完下次开听写生效"（与听写参数同一个口径）。
     */
    private fun ensureMediaSession() {
        if (mediaSession != null) return
        if (!AppPrefs.mediaKeys(this)) return
        val session = MediaSession(this, MEDIA_SESSION_TAG)
        session.setFlags(
            MediaSession.FLAG_HANDLES_MEDIA_BUTTONS or
                MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS
        )
        session.setCallback(object : MediaSession.Callback() {
            override fun onPlay() {
                handleKey(KeyEvent.KEYCODE_MEDIA_PLAY)
            }

            override fun onPause() {
                handleKey(KeyEvent.KEYCODE_MEDIA_PAUSE)
            }

            override fun onStop() {
                handleKey(KeyEvent.KEYCODE_MEDIA_STOP)
            }

            // 有些车机只发裸 KeyEvent（不走 onPlay/onPause），这条兜住它
            override fun onMediaButtonEvent(mediaButtonIntent: Intent): Boolean {
                val ev = mediaButtonIntent.getParcelableExtra<KeyEvent>(Intent.EXTRA_KEY_EVENT)
                if (ev != null && ev.action == KeyEvent.ACTION_UP) {
                    handleKey(ev.keyCode)
                    return true
                }
                return super.onMediaButtonEvent(mediaButtonIntent)
            }
        })
        session.isActive = true
        mediaSession = session
        updateMediaSessionState()
    }

    /** 耳机/车机键来了：按 [ListenKeys] 的映射执行（没在跑就什么都不做）。 */
    private fun handleKey(keyCode: Int) {
        when (ListenKeys.actionFor(keyCode, job != null, userPaused)) {
            ListenKeys.Action.PAUSE -> setUserPaused(true)
            ListenKeys.Action.RESUME -> setUserPaused(false)
            ListenKeys.Action.STOP -> stopEverything("耳机键停掉了后台听写")
            null -> Unit
        }
    }

    /** 用户主动暂停/继续（耳机键或通知按钮）。**与"处理中暂时不收音"是两回事**。 */
    private fun setUserPaused(pausedNow: Boolean) {
        userPaused = pausedNow
        if (pausedNow) {
            ListenState.set(ListenState.Phase.PAUSED, "按了耳机键：先不听了，再按一下继续")
        } else {
            idle?.markActivity(System.currentTimeMillis()) // 别刚继续就判"没人说话"
            ListenState.set(ListenState.Phase.LISTENING, "")
        }
        updateMediaSessionState()
        val (phase, detail) = ListenState.phase.value to ListenState.detail.value
        refreshNotification(phase, detail)
    }

    private fun updateMediaSessionState() {
        val session = mediaSession ?: return
        val state = if (userPaused) PlaybackState.STATE_PAUSED else PlaybackState.STATE_PLAYING
        val actions = PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or
            PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_STOP
        runCatching {
            session.setPlaybackState(
                PlaybackState.Builder()
                    .setActions(actions)
                    .setState(state, PlaybackState.PLAYBACK_POSITION_UNKNOWN, 1f)
                    .build()
            )
        }
    }

    private fun createChannel() {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (mgr.getNotificationChannel(CHANNEL_ID) != null) return
        val ch = NotificationChannel(
            CHANNEL_ID,
            "后台听写",
            NotificationManager.IMPORTANCE_LOW,     // 不要响：它是个常驻状态，不是提醒
        ).apply {
            description = "一直听着，说完一句就交给 ECHO"
            setShowBadge(false)
        }
        mgr.createNotificationChannel(ch)
    }

    private fun buildNotification(phase: ListenState.Phase, detail: String, segments: Int): Notification {
        val (title, body) = ListenState.textsOf(phase, detail, segments)
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, ListenService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        // 暂停/继续：这是开车时最常用的那个键（耳机线控按一下 = 同一条）
        val togglePaused = PendingIntent.getService(
            this, 2,
            Intent(this, ListenService::class.java)
                .setAction(if (userPaused) ACTION_RESUME else ACTION_PAUSE),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_echo_mic)
            .setContentTitle(title)
            .setContentText(body)
            .setOngoing(true)
            .setSilent(true)
            .setShowWhen(false)
            .setContentIntent(open)
            .addAction(0, if (userPaused) "继续" else "暂停", togglePaused)
            .addAction(0, "停止听写", stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun refreshNotification(phase: ListenState.Phase, detail: String) {
        val key = phase.name + "|" + detail
        if (key == lastText) return
        lastText = key
        val n = buildNotification(phase, detail, ListenState.segments.value)
        runCatching {
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .notify(NOTIFICATION_ID, n)
        }
    }

    // ------------------------------------------------------------------ 主循环

    private suspend fun runLoop() {
        val baseUrl = PairStore.baseUrl(this)
        if (!PairStore.isPaired(this) || baseUrl.isNullOrBlank()) {
            return failAndStop("还没配对：先在 App 里配对一台 ECHO")
        }
        ApiClient.attachTokenProvider { PairStore.token(this@ListenService) }
        val api = ApiClient.create(baseUrl, ApiClient.LONG_READ_TIMEOUT_S)

        val engine = SystemTts(this).also { it.init() }
        tts = engine

        // ---- 采集源与回声消除（决定"念回复时能不能继续听"）----
        // 用户在参数里要全双工**且**这台设备有 AEC 才走全双工；否则半双工（念时不收音）。
        // 拿不到 AEC 时**如实降级并说出来** —— 悄悄降级会让用户以为"开了全双工"。
        val tuning = ListenPrefs.load(this)
        val wanted = EchoCancel.decide(tuning.keepListeningWhileSpeaking, EchoCancel.available())
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuf <= 0) return failAndStop("这台设备给不出可用的录音缓冲（16k 单声道）")
        val ar = try {
            AudioRecord(
                EchoCancel.sourceFor(wanted),
                SAMPLE_RATE, CHANNEL, ENCODING, minBuf * 2,
            )
        } catch (e: Exception) {
            return failAndStop("打不开麦克风：${e.message}")
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            runCatching { ar.release() }
            return failAndStop("麦克风初始化失败：可能被别的应用占着")
        }
        // 用户指定的那支麦（App 自己的选项，见 `InputDevices`）：**设置偏好**，并如实回报。
        // `setPreferredDevice` 的语义就是"偏好"——系统可以不听（蓝牙尤其会），所以这里不承诺
        // "一定用它"；选的那支不在了就回落系统默认，并把原因写进启动说明。
        val micNote = if (AppPrefs.inputDeviceId(this) == AppPrefs.FOLLOW_SYSTEM) {
            ""
        } else {
            val picked = InputDevices.preferredInput(this)
            if (picked == null) {
                "你选的那支麦克风不在了 —— 已回落到系统默认"
            } else {
                val ok = runCatching { ar.setPreferredDevice(picked) }.getOrDefault(false)
                if (ok) "麦克风：${InputDevices.labelOf(picked)}" else "系统没接受指定的麦克风，用了默认那支"
            }
        }
        aec = if (wanted == EchoCancel.Mode.FULL_DUPLEX) EchoCancel.attach(ar) else null
        fullDuplex = aec != null
        val startNote = when {
            fullDuplex -> "全双工：念回复时也听着（回声消除已开）"
            tuning.keepListeningWhileSpeaking -> "这台设备不支持回声消除，念回复时先不收音"
            else -> ""
        }
        val startNotes = listOf(startNote, micNote).filter { it.isNotBlank() }

        val segmenter = VoiceSegmenter(
            sampleRate = SAMPLE_RATE,
            silenceThreshold = tuning.silenceThreshold,
            hangoverMs = tuning.hangoverMs,
        )
        val idleTimer = IdleAutoStop(tuning.idleAutoStopMs)
        idleTimer.start(System.currentTimeMillis())
        idle = idleTimer
        val frame = ShortArray(FRAME_SAMPLES)
        try {
            ar.startRecording()
            // 两条启动说明合成一句（全双工 + 麦克风偏好可能都有），别一条盖掉另一条
            val note = startNotes.joinToString(" · ")
            ListenState.set(ListenState.Phase.LISTENING, note)
            refreshNotification(ListenState.Phase.LISTENING, note)
            updateMediaSessionState()
            while (currentCoroutineContext().isActive) {
                val n = try {
                    ar.read(frame, 0, frame.size)
                } catch (_: Exception) {
                    -1
                }
                if (n <= 0) {
                    delay(20)
                } else if (!paused && !userPaused) {
                    val pcm = segmenter.feed(frame, n)
                    if (pcm != null) {
                        idleTimer.markActivity(System.currentTimeMillis())
                        handleSegment(api, pcm, engine)
                    }
                }
                // 一直没人说话 → 自己停下省电（原因要写出来，别让它"自己断了"）。
                // 用户主动暂停时**不算**"没人说话"：那是他要的暂停，不该被自动停掉。
                if (!paused && !userPaused && idleTimer.shouldStop(System.currentTimeMillis())) {
                    stopWithReason(idleTimer.stopReason(System.currentTimeMillis()))
                    return
                }
            }
        } finally {
            runCatching { ar.stop() }
            runCatching { ar.release() }
            idle = null
        }
    }

    /** 一段说完了：上传 → 等回复 → 念出来。**不抛异常**（失败也要继续听下一句）。 */
    private suspend fun handleSegment(api: EchoApi, pcm: ByteArray, engine: SystemTts) {
        // 全双工（AEC 已开）时不暂停：念回复期间照样收音，用户可以插话打断；
        // 半双工必须暂停，否则会把手机自己念的又听一遍（自激）。
        paused = !fullDuplex
        val flow = ListenFlow(api)
        try {
            ListenState.set(ListenState.Phase.THINKING, "刚录到一句，正在让 ECHO 转写…")
            refreshNotification(ListenState.Phase.THINKING, "刚录到一句，正在让 ECHO 转写…")
            val wav = WavWriter.wrap(pcm, SAMPLE_RATE, 1, BITS)
            val heard = when (val first = flow.submit(wav)) {
                is ListenFlow.Outcome.Heard -> first
                is ListenFlow.Outcome.Failed -> return note("刚才那句没发出去：${first.message}")
                // 上传这一步不该回 Reply（Reply 只从 awaitReply 来）；写出来是为了穷尽分支
                is ListenFlow.Outcome.Reply -> return note("这次没拿到结果")
            }
            ListenState.countSegment()
            // 把"它听成什么"写出来：车里最常问的就是这句（听错了要立刻能看出来）
            if (heard.text.isNotBlank()) {
                val line = "听到了：${heard.text.take(NOTIFICATION_TEXT_MAX)}"
                ListenState.set(ListenState.Phase.THINKING, line)
                refreshNotification(ListenState.Phase.THINKING, line)
            } else {
                ListenState.set(ListenState.Phase.THINKING, "正在等 ECHO 回话…")
                refreshNotification(ListenState.Phase.THINKING, "正在等 ECHO 回话…")
            }
            when (val result = flow.awaitReply(heard.commandId)) {
                is ListenFlow.Outcome.Reply -> speak(engine, api, result.text)
                is ListenFlow.Outcome.Failed -> note(result.message)
                is ListenFlow.Outcome.Heard -> note("这次没拿到结果")
            }
        } catch (e: Exception) {
            note("刚才那句出错：${e.message ?: e.javaClass.simpleName}")
        } finally {
            paused = false
            // 用户在处理期间按了暂停：不要把状态改回"在听"
            if (ListenState.phase.value != ListenState.Phase.FAILED && !userPaused) {
                ListenState.set(ListenState.Phase.LISTENING)
                refreshNotification(ListenState.Phase.LISTENING, "")
            }
        }
    }

    private suspend fun speak(engine: SystemTts, api: EchoApi, text: String) {
        if (text.isBlank()) return
        ListenState.set(ListenState.Phase.SPEAKING, text.take(NOTIFICATION_TEXT_MAX))
        refreshNotification(ListenState.Phase.SPEAKING, text.take(NOTIFICATION_TEXT_MAX))

        // 「谁念回复」是 **App 自己的选项**（设置 → 语音）。以前这条路**写死**手机念，
        // 于是用户选了"ECHO 念"对开车听写根本不生效（2026-10-15 发现的窟窿）。
        // ECHO 念：`POST /api/tts` 拿字节再播；**失败如实回落手机念**并把原因写出来 ——
        // 静默回落 = "设置了没生效"，那是最难查的一类。
        if (SpeakPrefs.load(this) == SpeakPrefs.ENGINE_ECHO) {
            val bytes = runCatching {
                val resp = api.tts(com.echo.contact.data.dto.TtsRequest(text = text))
                if (resp.isSuccessful) resp.body()?.bytes() else null
            }.getOrNull()
            if (bytes != null && bytes.isNotEmpty()) {
                if (playEchoBytes(bytes)) return
            }
            note("ECHO 念没成，这句用手机念")
        }

        if (!engine.available) {
            return note("ECHO 回话了，但这台设备的系统朗读不可用（回复在通知里）")
        }
        withTimeoutOrNull(30_000) {
            suspendCancellableCoroutine { cont ->
                engine.speak(text) { if (cont.isActive) cont.resume(Unit) }
            }
        }
    }

    /** 播 ECHO 给的音频字节（audio/mpeg）。返回 false = 没播成（调用方回落到手机念）。 */
    private suspend fun playEchoBytes(bytes: ByteArray): Boolean {
        val player = echoPlayer ?: EchoTtsPlayer(this).also { echoPlayer = it }
        val file = runCatching {
            kotlinx.coroutines.withContext(Dispatchers.IO) { player.cache(bytes, "audio/mpeg") }
        }.getOrNull() ?: return false
        withTimeoutOrNull(30_000) {
            suspendCancellableCoroutine { cont ->
                runCatching { player.play(file) { if (cont.isActive) cont.resume(Unit) } }
                    .onFailure { if (cont.isActive) cont.resume(Unit) }
            }
        }
        return true
    }

    /** 失败但**继续听**：只把原因写进状态与通知（用户看得到，也不会因此丢掉整个服务）。 */
    private fun note(msg: String) {
        ListenState.set(ListenState.Phase.LISTENING, msg)
        refreshNotification(ListenState.Phase.LISTENING, msg)
    }

    private fun failAndStop(msg: String) {
        ListenState.reset(ListenState.Phase.FAILED, msg)
        refreshNotification(ListenState.Phase.FAILED, msg)
        stopEverything(msg, keepState = true)
    }

    /** **正常**收尾（不是出错）：原因留在状态里，界面回来能看到为什么自己停了。 */
    private fun stopWithReason(reason: String) {
        ListenState.reset(ListenState.Phase.OFF, reason)
        refreshNotification(ListenState.Phase.OFF, reason)
        stopEverything(reason, keepState = true)
    }

    private fun stopEverything(reason: String, keepState: Boolean = false) {
        val j = job
        job = null
        paused = false
        userPaused = false
        fullDuplex = false
        idle = null
        runCatching { mediaSession?.release() }
        mediaSession = null
        runCatching { aec?.release() }
        aec = null
        runCatching { tts?.shutdown() }
        tts = null
        runCatching { echoPlayer?.stop() }
        echoPlayer = null
        if (j != null) {
            scope.launch { j.cancelAndJoin() }
        }
        if (!keepState) ListenState.reset(ListenState.Phase.OFF, reason)
        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
        runCatching { stopSelf() }
    }

    companion object {
        const val ACTION_START = "com.echo.contact.action.LISTEN_START"
        const val ACTION_STOP = "com.echo.contact.action.LISTEN_STOP"

        /** 通知里的「暂停 / 继续」（耳机键走媒体会话，最终也落到这两个动作上）。 */
        const val ACTION_PAUSE = "com.echo.contact.action.LISTEN_PAUSE"
        const val ACTION_RESUME = "com.echo.contact.action.LISTEN_RESUME"

        const val CHANNEL_ID = "echo_listen"
        const val NOTIFICATION_ID = 0xEC40

        /** 媒体会话的 tag（系统日志/调试用；耳机键就是通过它转进来的）。 */
        private const val MEDIA_SESSION_TAG = "echo-listen"

        private const val SAMPLE_RATE = 16000
        private const val BITS = 16
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT

        /** 100 ms 一帧：分段按毫秒算，帧长只影响延迟与开销。 */
        private const val FRAME_SAMPLES = SAMPLE_RATE / 10
        private const val NOTIFICATION_TEXT_MAX = 80

        /** 开听写（调用方**必须**先拿到 RECORD_AUDIO 权限）。 */
        fun start(context: Context) {
            val i = Intent(context, ListenService::class.java).setAction(ACTION_START)
            ContextCompat.startForegroundService(context, i)
        }

        /** 停听写（界面上那个开关关掉时走这条；通知里的「停止听写」走 ACTION_STOP）。 */
        fun stop(context: Context) {
            context.stopService(Intent(context, ListenService::class.java))
        }
    }
}
