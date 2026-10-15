package com.echo.contact.audio

import android.content.Context
import android.media.MediaPlayer
import java.io.File

/**
 * 播会议的一段音频（`MediaPlayer` 的薄封装）。
 *
 * 为什么用 `MediaPlayer` 而不是 ExoPlayer：这里只播**本地文件里的一段 wav**，
 * 不需要流式/自适应码率那一套；`MediaPlayer` 是系统自带，零依赖。
 * （`.flac` 那件事在服务端就解决掉了 —— `meeting_audio` 会用时解码成 wav。）
 *
 * 状态通过 [onState] 回调给出去：界面必须能显示"正在取/在放/放完了/出错了"，
 * 否则用户按了「播放」看不出发生了什么（这与整机其它地方的口径一致）。
 */
class MeetingAudioPlayer(context: Context) {

    enum class State { IDLE, LOADING, PLAYING, PAUSED, DONE, ERROR }

    /** 状态回调（在主线程被调用；`message` 只在 ERROR 时有值）。 */
    var onState: ((State, String) -> Unit)? = null

    private val appContext = context.applicationContext
    private var player: MediaPlayer? = null

    /** 当前正在放的段号（0 = 没在放）——界面据此高亮"第几段"。 */
    var currentSeg: Int = 0
        private set

    val isPlaying: Boolean
        get() = runCatching { player?.isPlaying == true }.getOrDefault(false)

    fun play(file: File, seg: Int = 0) {
        stopPlayer()
        currentSeg = seg
        onState?.invoke(State.LOADING, "")
        val mp = MediaPlayer()
        player = mp
        try {
            mp.setDataSource(appContext, android.net.Uri.fromFile(file))
            mp.setOnPreparedListener {
                runCatching { it.start() }
                onState?.invoke(State.PLAYING, "")
            }
            mp.setOnCompletionListener {
                onState?.invoke(State.DONE, "")
                stopPlayer(keepSeg = true)
            }
            mp.setOnErrorListener { _, what, extra ->
                onState?.invoke(State.ERROR, "播放器出错（$what/$extra）")
                stopPlayer(keepSeg = true)
                true
            }
            mp.prepareAsync()
        } catch (e: Exception) {
            onState?.invoke(State.ERROR, "打不开这段音频：${e.message ?: e.javaClass.simpleName}")
            stopPlayer(keepSeg = true)
        }
    }

    fun pause() {
        val mp = player ?: return
        if (runCatching { mp.isPlaying }.getOrDefault(false)) {
            runCatching { mp.pause() }
            onState?.invoke(State.PAUSED, "")
        }
    }

    fun resume() {
        val mp = player ?: return
        runCatching { mp.start() }
        onState?.invoke(State.PLAYING, "")
    }

    /** 停掉并释放；`keepSeg=true` 时界面还知道上次放的是哪一段。 */
    fun stop(keepSeg: Boolean = false) {
        stopPlayer(keepSeg)
    }

    private fun stopPlayer(keepSeg: Boolean = false) {
        val mp = player
        player = null
        runCatching { mp?.stop() }
        runCatching { mp?.release() }
        if (!keepSeg) currentSeg = 0
        onState?.invoke(State.IDLE, "")
    }

    fun release() = stopPlayer()
}
