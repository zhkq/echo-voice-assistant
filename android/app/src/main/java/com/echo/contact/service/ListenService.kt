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
import androidx.core.content.ContextCompat
import com.echo.contact.MainActivity
import com.echo.contact.R
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
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
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

    /** 处理 + 朗读期间置位：这期间读到的音频**不喂给分段器**（见类注释的取舍 1）。 */
    @Volatile private var paused = false

    /** 只在线程里改，避免每次读帧都刷通知。 */
    @Volatile private var lastText: String = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopEverything("用户停掉了后台听写")
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
        // 系统回收后重建：只重建服务壳，**不自动开麦**（见类注释的取舍 2）
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
        return true
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
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_echo_mic)
            .setContentTitle(title)
            .setContentText(body)
            .setOngoing(true)
            .setSilent(true)
            .setShowWhen(false)
            .setContentIntent(open)
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

        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuf <= 0) return failAndStop("这台设备给不出可用的录音缓冲（16k 单声道）")
        val ar = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, CHANNEL, ENCODING, minBuf * 2,
            )
        } catch (e: Exception) {
            return failAndStop("打不开麦克风：${e.message}")
        }
        if (ar.state != AudioRecord.STATE_INITIALIZED) {
            runCatching { ar.release() }
            return failAndStop("麦克风初始化失败：可能被别的应用占着")
        }

        val segmenter = VoiceSegmenter(sampleRate = SAMPLE_RATE)
        val idle = IdleAutoStop()
        idle.start(System.currentTimeMillis())
        val frame = ShortArray(FRAME_SAMPLES)
        try {
            ar.startRecording()
            ListenState.set(ListenState.Phase.LISTENING)
            refreshNotification(ListenState.Phase.LISTENING, "")
            while (currentCoroutineContext().isActive) {
                val n = try {
                    ar.read(frame, 0, frame.size)
                } catch (_: Exception) {
                    -1
                }
                if (n <= 0) {
                    delay(20)
                } else if (!paused) {
                    val pcm = segmenter.feed(frame, n)
                    if (pcm != null) {
                        idle.markActivity(System.currentTimeMillis())
                        handleSegment(api, pcm, engine)
                    }
                }
                // 一直没人说话 → 自己停下省电（原因要写出来，别让它"自己断了"）
                if (!paused && idle.shouldStop(System.currentTimeMillis())) {
                    stopWithReason(idle.stopReason(System.currentTimeMillis()))
                    return
                }
            }
        } finally {
            runCatching { ar.stop() }
            runCatching { ar.release() }
        }
    }

    /** 一段说完了：上传 → 等回复 → 念出来。**不抛异常**（失败也要继续听下一句）。 */
    private suspend fun handleSegment(api: EchoApi, pcm: ByteArray, engine: SystemTts) {
        paused = true
        try {
            ListenState.set(ListenState.Phase.THINKING, "刚录到一句，正在让 ECHO 转写…")
            refreshNotification(ListenState.Phase.THINKING, "刚录到一句，正在让 ECHO 转写…")
            val wav = WavWriter.wrap(pcm, SAMPLE_RATE, 1, BITS)
            val resp = api.voiceCommand(wav.toRequestBody(WAV_MEDIA_TYPE.toMediaType()))
            val body = resp.body()
            if (!resp.isSuccessful || body?.ok != true) {
                val why = body?.message ?: "HTTP ${resp.code()}"
                return note("刚才那句没发出去：$why")
            }
            ListenState.countSegment()
            val reply = awaitReply(api, body.commandId)
            if (reply.isNullOrBlank()) {
                return note("ECHO 没在 90 秒内回话（那句可能还在跑）")
            }
            speak(engine, reply)
        } catch (e: Exception) {
            note("刚才那句出错：${e.message ?: e.javaClass.simpleName}")
        } finally {
            paused = false
            if (ListenState.phase.value != ListenState.Phase.FAILED) {
                ListenState.set(ListenState.Phase.LISTENING)
                refreshNotification(ListenState.Phase.LISTENING, "")
            }
        }
    }

    /**
     * 等那条指令的结果。
     *
     * 为什么要轮询而不是直接拿 `voice-command` 的返回：那条只回 `{ok, message, text, commandId}`，
     * 智能体的答复是**异步**落到 `commands` 表里的（与界面那条路同一个口径：
     * `MainViewModel.awaitResult`）。手机上没有推送通道，轮询是唯一稳的办法。
     */
    private suspend fun awaitReply(api: EchoApi, commandId: Int?, timeoutMs: Long = 90_000): String? {
        if (commandId == null) return null
        ListenState.set(ListenState.Phase.THINKING, "正在等 ECHO 回话…")
        refreshNotification(ListenState.Phase.THINKING, "正在等 ECHO 回话…")
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline && currentCoroutineContext().isActive) {
            val item = try {
                api.listCommands(mapOf("limit" to "5")).body()?.items?.firstOrNull { it.id == commandId }
            } catch (e: Exception) {
                null
            }
            if (item != null) {
                when (item.status) {
                    "done" -> return item.brief?.takeIf { it.isNotBlank() } ?: item.reply
                    "failed" -> return "ECHO 那边失败了：${item.error ?: "没给原因"}"
                }
            }
            delay(POLL_MS)
        }
        return null
    }

    private suspend fun speak(engine: SystemTts, text: String) {
        if (text.isBlank()) return
        if (!engine.available) {
            return note("ECHO 回话了，但这台设备的系统朗读不可用（回复在通知里）")
        }
        ListenState.set(ListenState.Phase.SPEAKING, text.take(NOTIFICATION_TEXT_MAX))
        refreshNotification(ListenState.Phase.SPEAKING, text.take(NOTIFICATION_TEXT_MAX))
        withTimeoutOrNull(30_000) {
            suspendCancellableCoroutine { cont ->
                engine.speak(text) { if (cont.isActive) cont.resume(Unit) }
            }
        }
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
        runCatching { tts?.shutdown() }
        tts = null
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
        const val CHANNEL_ID = "echo_listen"
        const val NOTIFICATION_ID = 0xEC40

        private const val SAMPLE_RATE = 16000
        private const val BITS = 16
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT

        /** 100 ms 一帧：分段按毫秒算，帧长只影响延迟与开销。 */
        private const val FRAME_SAMPLES = SAMPLE_RATE / 10
        private const val POLL_MS = 1500L
        private const val NOTIFICATION_TEXT_MAX = 80
        private const val WAV_MEDIA_TYPE = "audio/wav"

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
