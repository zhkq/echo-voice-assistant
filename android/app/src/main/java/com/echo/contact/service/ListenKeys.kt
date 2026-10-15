package com.echo.contact.service

import android.view.KeyEvent

/**
 * 耳机/蓝牙媒体键 → 听写动作（**纯函数，可 JVM 单测**）。
 *
 * 为什么需要它：开车时人**不碰手机**，能按的只有耳机线上的键或车机上的媒体键。
 * 常驻听写已经开着麦，所以这几个键最自然的语义是"暂停/继续/停掉"，而不是"开始"。
 *
 * ⚠️ 一个刻意的边界：**媒体会话只在听写服务活着时存在**，所以耳机键**不能**把
 * 一个从没开过的听写"叫起来"（那需要常驻的媒体会话，会长期霸占别人的播放键）。
 * 键在"没在听"时按了什么都不做 —— 这比"莫名其妙自己开始录音"安全得多。
 *
 * 映射（与手机/车机上的习惯一致）：
 *   * 播放/暂停键、耳机线控键（HEADSETHOOK）：在"暂停 ↔ 继续"之间切；
 *   * 单独的暂停键：暂停；单独的播放键：继续；
 *   * 停止键：停掉整个听写（等于通知里的「停止听写」）。
 */
object ListenKeys {

    enum class Action { PAUSE, RESUME, STOP }

    /**
     * @param keyCode  `KeyEvent.KEYCODE_*`
     * @param running  听写服务是不是活着（没活着就什么都不做）
     * @param userPaused 是不是用户自己按了暂停（决定"切换"往哪边切）
     */
    fun actionFor(keyCode: Int, running: Boolean, userPaused: Boolean): Action? {
        if (!running) return null
        return when (keyCode) {
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_MEDIA_PLAY,      // 有些车机只发这一个键，不能不理
            KeyEvent.KEYCODE_MEDIA_PAUSE,
            -> if (userPaused) Action.RESUME else Action.PAUSE

            KeyEvent.KEYCODE_MEDIA_STOP -> Action.STOP
            else -> null
        }
    }
}
