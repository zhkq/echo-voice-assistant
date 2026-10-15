package com.echo.contact.audio

import android.content.Context
import android.media.MediaPlayer
import com.echo.contact.service.AppPrefs
import java.io.File

/**
 * 播 ECHO 合成好的音频字节（`POST /api/tts` 的响应体）。
 *
 * 字节可能是 `audio/mpeg`（edge-tts）也可能是 `audio/wav`（离线引擎）—— MediaPlayer 两个都吃。
 * 先落一个临时文件再播，比实现 `MediaDataSource` 省事，而且 mp3 的随机定位也更稳。
 *
 * 用 `prepareAsync()` 不用 `prepare()`：后者会**阻塞调用线程**，在主线程上就是 ANR。
 * 回调会回到创建它的那个 Looper（主线程）上，所以 UI 回调是安全的。
 *
 * ## 输出设备（用户 2026-10-15 拍板"只做实际能做到的"）
 *
 * 「ECHO 念」这条路上 `MediaPlayer.setPreferredDevice()` 是**真的可用**的，
 * 所以这里按 `AppPrefs.outputDeviceId` 设偏好。三条口径：
 *  1. **设失败不当错误**：它是**偏好**，系统可以不听（蓝牙档位切换时尤其常见）→
 *     只把真实结果记进 [lastRoute]，**绝不**因此不播（播不出声才是真错）；
 *  2. **不静默**：真实结果记在 [lastRoute] 里，谁要看都能看到"这一次系统怎么答的"
 *     （设置页上那行"偏好用哪支"是 `OutputDevices.describe()`，两者一起看才是全部事实）；
 *  3. 只在**播放前**设一次（播放中换设备要重开播放才生效 —— 与设置页那句"改完下次念生效"一致）。
 *
 * 手机念（`SystemTts`）**没有**这个接口，所以那条路永远跟随系统：限制写在设置页上。
 */
class EchoTtsPlayer(private val context: Context) {

    private var player: MediaPlayer? = null
    private var current: File? = null

    /**
     * 最近**一次**播放的路由结果（界面读它来如实说明"偏好有没有被系统收下"）。
     *
     * 只写主线程（[play] 从主线程调），读的也是界面，所以不加锁。
     */
    var lastRoute: TtsRoute? = null
        private set

    /** 把字节写盘（IO 线程调）→ 返回可播放的文件。 */
    fun cache(bytes: ByteArray, mime: String?): File {
        val ext = when {
            mime?.contains("wav") == true -> "wav"
            mime?.contains("mpeg") == true || mime?.contains("mp3") == true -> "mp3"
            else -> "bin"
        }
        val f = File(context.cacheDir, "echo-tts.$ext")
        f.writeBytes(bytes)
        return f
    }

    /** 主线程调：异步准备并开始播放。 */
    fun play(file: File, onFinished: () -> Unit) {
        stop()
        current = file
        val mp = MediaPlayer()
        player = mp
        mp.setOnPreparedListener { it.start() }
        mp.setOnCompletionListener {
            onFinished()
            stop()
        }
        mp.setOnErrorListener { _, _, _ ->
            onFinished()
            stop()
            true
        }
        try {
            // 偏好要在 prepare/start **之前**设：设晚了这一段就已经从默认设备出去了
            applyPreferredOutput(mp)
            mp.setDataSource(file.absolutePath)
            mp.prepareAsync()
        } catch (e: Exception) {
            // 走到这的都是 setDataSource / prepare 的问题（文件坏了、被删了、没有可用的输出）——
            // 路由偏好**不会**走到这：它自己在 applyPreferredOutput 里 try 过，失败也只是记一笔
            onFinished()
            stop()
        }
    }

    /**
     * 按偏好选设备并上报真实结果。**失败不算错误**（偏好而已，系统可以不听），
     * 但必须**不静默**：结果写进 [lastRoute]，界面照实显示。
     */
    private fun applyPreferredOutput(mp: MediaPlayer) {
        val pref = try {
            OutputDevices.preferredOutput(context)
        } catch (e: Exception) {                                   // 个别 ROM 的 getDevices 会抛
            null
        }
        // 「有没有偏好」与「偏好成没成」是两件事，都要说：没选设备时 outputDeviceId 是 -1，
        // 而设备列表查不到（拔了/没权限）时也回 null —— 两种都算"这次没设上"。
        val chose = AppPrefs.outputDeviceId(context) != AppPrefs.FOLLOW_SYSTEM
        val prefLabel = pref?.let { OutputDevices.labelOf(it) } ?: ""
        val applied = if (pref != null) {
            runCatching { mp.setPreferredDevice(pref) }.getOrDefault(false)
        } else {
            false
        }
        lastRoute = routeOutcome(preferred = chose && pref != null, label = prefLabel, applied = applied)
    }

    /** 正在播吗（用于让"停止"按钮只在需要时出现）。 */
    val isPlaying: Boolean
        get() = runCatching { player?.isPlaying == true }.getOrDefault(false)

    fun stop() {
        player?.let { mp ->
            runCatching { if (mp.isPlaying) mp.stop() }
            runCatching { mp.reset() }
            runCatching { mp.release() }
        }
        player = null
        current = null
    }
}
