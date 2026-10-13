package com.echo.contact.audio

import android.content.Context
import android.media.MediaPlayer
import java.io.File

/**
 * 播 ECHO 合成好的音频字节（`POST /api/tts` 的响应体）。
 *
 * 字节可能是 `audio/mpeg`（edge-tts）也可能是 `audio/wav`（离线引擎）—— MediaPlayer 两个都吃。
 * 先落一个临时文件再播，比实现 `MediaDataSource` 省事，而且 mp3 的随机定位也更稳。
 *
 * 用 `prepareAsync()` 不用 `prepare()`：后者会**阻塞调用线程**，在主线程上就是 ANR。
 * 回调会回到创建它的那个 Looper（主线程）上，所以 UI 回调是安全的。
 */
class EchoTtsPlayer(private val context: Context) {

    private var player: MediaPlayer? = null
    private var current: File? = null

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
            mp.setDataSource(file.absolutePath)
            mp.prepareAsync()
        } catch (e: Exception) {
            onFinished()
            stop()
        }
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
