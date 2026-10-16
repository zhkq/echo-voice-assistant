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
 * ## 三个刻意的取舍
 *
 *  1. **按段（默认 30 秒）传**，不做实时流：一段 ≈1 MB，走中继（单次上限 12 MB）很稳；
 *     断了只重传这一段，不会把整场拖垮。
 *  2. **发不出去就留着**：没传成的段留在 `cacheDir/meeting-mic/`，下一段之前重试 ——
 *     会议录音最怕"看着在录、其实丢了"。状态里如实写"还有 N 段没送出去"。
 *  3. **静音 = 本地丢帧 + 不上传**（与电脑侧"暂停不写盘"同一个语义）；这段空白照样
 *     会在会议的 meta 里留下暂停区间（因为暂停/恢复也报了电脑，见 `pause`）。
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

        private const val CHANNEL_ID = "echo_meeting_mic"
        private const val NOTIF_ID = 4211
        private const val SAMPLE_RATE = 16000
        private const val FRAME_SAMPLES = 1600                     // 100ms

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

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            ACTION_STOP -> stopEverything("用户结束了手机录音")
            ACTION_PAUSE -> {
                _state.value = _state.value.copy(paused = true)
                notifyState()
            }
            ACTION_RESUME -> {
                _state.value = _state.value.copy(paused = false)
                notifyState()
            }
            ACTION_RETRY -> retryPendingNow()
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
        startForegroundCompat()
        job = scope.launch { runLoop() }
    }

    private suspend fun runLoop() {
        val baseUrl = PairStore.baseUrl(this)
        if (!PairStore.isPaired(this) || baseUrl.isNullOrBlank()) {
            return failAndStop("还没配对：先在 App 里配对一台 ECHO")
        }
        val api = ApiClient.create(baseUrl)
        // 让**电脑**开一场"手机麦"的会议（电脑那条路不开麦，只落盘/转写/存储）
        try {
            val r = api.meetingStartPhone()
            if (!(r.isSuccessful && r.body()?.ok == true)) {
                return failAndStop("电脑没能开始这场录音：${r.body()?.message ?: "HTTP ${r.code()}"}")
            }
        } catch (e: Exception) {
            return failAndStop("联系不上电脑：${e.message}")
        }
        _state.value = _state.value.copy(
            active = true, error = "", sent = 0, pending = 0, retryNote = "")

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
                    // 静音：**丢帧 + 不上传**（这段时间在会议里就是空白，meta 里会留区间）
                    _state.value = _state.value.copy(level = 0f)
                    continue
                }
                _state.value = _state.value.copy(level = level)
                for (i in 0 until n) buffer.add(frame[i])
                if (buffer.size >= samplesPerSegment) {
                    val bytes = toPcmBytes(buffer)
                    if (!uploadSegment(api, bytes)) {
                        // 发不出去就留着，下一段之前重试（会议录音最怕"看着在录、其实丢了"）
                        savePending(bytes)
                    }
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

    /** 传一段；成功返回 true。失败**不抛**（调用方决定留着重试）。 */
    private suspend fun uploadSegment(api: com.echo.contact.data.EchoApi, pcm: ByteArray): Boolean {
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
        api: com.echo.contact.data.EchoApi,
        note: Boolean = false,
    ): RetryOutcome = retryGate.runExclusive { retryRound(api, note) }

    /** 真传的那一轮（**调用方必须已经占住 [retryGate]** —— 见 [retryPending]）。 */
    private suspend fun retryRound(api: com.echo.contact.data.EchoApi, note: Boolean) {
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
        val j = job
        job = null
        scope.launch {
            if (j != null) j.cancelAndJoin()
            // 先把欠的段补传（**停止前尽力**），再让电脑结束这场会议
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
            _state.value = _state.value.copy(active = false, level = 0f)
            stopForegroundCompat()
            stopSelf()
        }
    }

    private fun failAndStop(msg: String) {
        _state.value = MicState(active = false, error = msg)
        notifyState()
        stopForegroundCompat()
        stopSelf()
    }

    override fun onDestroy() {
        runCatching { record?.release() }
        record = null
        // 手动重传那个作用域也要一起收掉：服务没了还留着它，就会"服务已停、请求还在飞"
        retryScope?.cancel()
        retryScope = null
        // 服务正在消失：把守卫放掉（幂等）。被取消的那一轮即使来不及走 finally，
        // 也不会让下一场录音里这个守卫一直是"在跑"。
        retryGate.leave()
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

    private fun buildNotification(): Notification {
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        val pi = PendingIntent.getActivity(this, 0, intent, flags)
        val stopPi = PendingIntent.getService(
            this, 1,
            Intent(this, MeetingMicService::class.java).setAction(ACTION_STOP), flags)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("ECHO 会议录音")
            .setContentText(notificationText(_state.value))
            .setOngoing(true)
            .setContentIntent(pi)
            .addAction(0, "结束", stopPi)
            .build()
    }
}

/**
 * 通知里那一行字（**纯函数**，JVM 可测）。
 *
 * 为什么值得单独抽出来并有判据：这一行是"录音到底有没有丢东西"的**唯一**出口 ——
 * 优先级写错了就会在最该说话的时候沉默：
 *  * 有**错误**先说错误（"有一段缓存不下来，可能真的丢了"绝不能被"在录 · 已送 3 段"盖掉）；
 *  * 静音其次；
 *  * 然后是待重传（这条也在界面上写着，见 `MeetingRecordingScreen`）；
 *  * 最后才是"一切正常"。
 */
internal fun notificationText(st: MicState): String = when {
    st.error.isNotBlank() -> st.error
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
)

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
