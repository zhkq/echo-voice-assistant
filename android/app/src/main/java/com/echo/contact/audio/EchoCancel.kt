package com.echo.contact.audio

import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler

/**
 * 免提自激的对策：**平台回声消除（AEC）**。
 *
 * 问题：常驻听写开着麦克风，而 ECHO 的回复是从**手机扬声器**念出来的 ——
 * 不处理的话麦克风会把念出去的话又听一遍，于是"自问自答"。
 * 之前只有一条兜底：**念的时候不收音**（半双工），代价是念的那几秒你插话会丢。
 *
 * 这一版加第二条路（全双工）：
 *   * 采集源改用 `VOICE_COMMUNICATION`（平台对这条源做回声消除与噪声抑制）；
 *   * 再显式挂一个 [AcousticEchoCanceler]（**设备支持才有**）；
 *   * 朗读期间**继续收音**，靠它把"自己念出去的那份"从麦克风信号里减掉。
 *
 * 为什么把判据拆成"纯函数 + 真机那几行"
 * =====================================
 * [decide] / [sourceFor] 是纯计算，能在 JVM 里逐条钉住（`EchoCancelTest`）；
 * [available] / [attach] 只能在真机上才有意义 —— 拿不到 AEC 时**如实降级**成半双工，
 * 而不是"设了标记就当它生效了"（那样用户会听到自激，还会以为是回声消除没做）。
 *
 * ⚠️ 有意**不去改** `AudioManager.mode` / 扬声器路由：`MODE_IN_COMMUNICATION` 会把声音
 * 默认走到听筒（车里等于听不见）。只用采集源 + AEC 实例，改动面最小。
 */
object EchoCancel {

    enum class Mode {
        /** 全双工：念回复时也收音（需要 AEC）。 */
        FULL_DUPLEX,

        /** 半双工：念回复时暂停收音（默认行为，任何设备都能跑）。 */
        HALF_DUPLEX,
    }

    /** 用哪种模式：用户想要全双工**且**这台设备支持 AEC，才给全双工。 */
    fun decide(keepListeningWhileSpeaking: Boolean, aecAvailable: Boolean): Mode =
        if (keepListeningWhileSpeaking && aecAvailable) Mode.FULL_DUPLEX else Mode.HALF_DUPLEX

    /** 采集源：全双工走 `VOICE_COMMUNICATION`（平台会做 AEC），否则用识别源。 */
    fun sourceFor(mode: Mode): Int =
        if (mode == Mode.FULL_DUPLEX) {
            MediaRecorder.AudioSource.VOICE_COMMUNICATION
        } else {
            MediaRecorder.AudioSource.VOICE_RECOGNITION
        }

    /** 这台设备支不支持 AEC（拿不到就当不支持）。 */
    fun available(): Boolean =
        runCatching { AcousticEchoCanceler.isAvailable() }.getOrDefault(false)

    /** 给这条录音会话挂上 AEC 并启用；拿不到/启用失败返回 null（调用方按半双工跑）。 */
    fun attach(record: AudioRecord): AcousticEchoCanceler? = runCatching {
        AcousticEchoCanceler.create(record.audioSessionId)?.also { it.enabled = true }
    }.getOrNull()
}
