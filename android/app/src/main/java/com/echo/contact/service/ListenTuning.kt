package com.echo.contact.service

/**
 * 常驻听写的**可调参数**（纯数据 + 钳制，可 JVM 单测）。
 *
 * 为什么要让用户能改：这套判据是在安静房间里定的，而它的真实战场是**车里**——
 * 发动机/风噪/音乐一上来，"多大的声音算有人在说话"与"静多久算说完"都可能要重定。
 * 定死在代码里的后果是：用户只能反馈一句"它老是自己在说话"，然后等我改一版。
 *
 * 三个旋钮（都能在「指令 → 后台听写 → 参数」里调）：
 *   * [silenceThreshold] 说话门限（RMS）：**调大**才能压住车内底噪；
 *   * [hangoverMs] 说完静音多久算一段（**调大**给思考停顿留余量，**调小**反应更快）；
 *   * [idleAutoStopMin] 一直没人说话多久自己停（省电）。
 *
 * [clamped] 是唯一的**边界口径**：界面、服务、未来别的地方都走它，
 * 免得出现"界面存了 0.5 的门限 → 服务里永远判定为有人在说"这种静默坏掉。
 */
data class ListenTuning(
    val silenceThreshold: Double = DEFAULT_SILENCE_THRESHOLD,
    val hangoverMs: Int = DEFAULT_HANGOVER_MS,
    val idleAutoStopMin: Int = DEFAULT_IDLE_AUTO_STOP_MIN,
) {

    fun clamped(): ListenTuning = ListenTuning(
        silenceThreshold = silenceThreshold.coerceIn(MIN_SILENCE_THRESHOLD, MAX_SILENCE_THRESHOLD),
        hangoverMs = hangoverMs.coerceIn(MIN_HANGOVER_MS, MAX_HANGOVER_MS),
        idleAutoStopMin = idleAutoStopMin.coerceIn(MIN_IDLE_AUTO_STOP_MIN, MAX_IDLE_AUTO_STOP_MIN),
    )

    val idleAutoStopMs: Long get() = idleAutoStopMin.coerceAtLeast(1) * 60_000L

    companion object {
        /** 与 ECHO 面板的 `silenceThreshold` 同口径（2026-10-09 对齐过）。 */
        const val DEFAULT_SILENCE_THRESHOLD = 0.012
        const val DEFAULT_HANGOVER_MS = 1200
        const val DEFAULT_IDLE_AUTO_STOP_MIN = 20

        /** 下限 0.004：再小的话普通房间的底噪就会被当成说话。 */
        const val MIN_SILENCE_THRESHOLD = 0.004

        /** 上限 0.08：再大就得贴着麦克风喊了。 */
        const val MAX_SILENCE_THRESHOLD = 0.08

        /** 0.6 秒：比这更短会把正常换气切成一堆碎片。 */
        const val MIN_HANGOVER_MS = 600

        /** 4 秒：比这更长，用户会以为"说完没反应"。 */
        const val MAX_HANGOVER_MS = 4000

        const val MIN_IDLE_AUTO_STOP_MIN = 1
        const val MAX_IDLE_AUTO_STOP_MIN = 120
    }
}
