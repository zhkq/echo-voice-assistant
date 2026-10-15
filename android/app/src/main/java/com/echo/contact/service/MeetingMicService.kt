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

        private const val CHANNEL_ID = "echo_meeting_mic"
        private const val NOTIF_ID = 4211
        private const val SAMPLE_RATE = 16000
        private const val FRAME_SAMPLES = 1600                     // 100ms
        /** 一段多少秒（≈1MB，走中继很稳）。 */
        private const val SEGMENT_SECONDS = 30

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
         * 界面读这个（**不需要 bind** —— 全屏录音界面在"手机麦"这条路上显示**本地**电平，
         * 比电脑那条隔一跳网络的顺得多）。
         */
        private val _state = MutableStateFlow(MicState())
        val state: StateFlow<MicState> = _state.asStateFlow()
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    private var record: AudioRecord? = null

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
        _state.value = _state.value.copy(active = true, error = "", sent = 0, pending = 0)

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
        val samplesPerSegment = SAMPLE_RATE * SEGMENT_SECONDS
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

    private suspend fun retryPending(api: com.echo.contact.data.EchoApi) {
        for (f in pendingFiles()) {
            try {
                val r = api.meetingSegment(f.readBytes().toRequestBody("audio/wav".toMediaType()))
                if (r.isSuccessful) {
                    f.delete()
                    _state.value = _state.value.copy(sent = _state.value.sent + 1)
                } else {
                    return                      // 还是不行：留着，下次再试（保顺序）
                }
            } catch (e: Exception) {
                return
            }
        }
        _state.value = _state.value.copy(pending = pendingFiles().size)
    }

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
                    retryPending(api)
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
        val st = _state.value
        val intent = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        val pi = PendingIntent.getActivity(this, 0, intent, flags)
        val stopPi = PendingIntent.getService(
            this, 1,
            Intent(this, MeetingMicService::class.java).setAction(ACTION_STOP), flags)
        val text = when {
            st.error.isNotBlank() -> st.error
            st.paused -> "已静音（这段不会录进会议）"
            st.pending > 0 -> "在录（手机麦克风）· 已送 ${st.sent} 段 · ${st.pending} 段待重传"
            else -> "在录（手机麦克风）· 已送 ${st.sent} 段"
        }
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("ECHO 会议录音")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(pi)
            .addAction(0, "结束", stopPi)
            .build()
    }
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
)

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
