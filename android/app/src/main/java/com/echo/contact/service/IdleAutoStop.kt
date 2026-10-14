package com.echo.contact.service

/**
 * 「一直没人说话就自己停下来」——**纯逻辑，可 JVM 单测**。
 *
 * 为什么必须有它：常驻听写开着的是**麦克风型前台服务**（`AudioRecord` 一直读 +
 * 前台通知），没人说话时它照样占着麦克风和一份常驻通知。放着不管就是：
 * 用户忘了关 → 一晚上耗电、第二天手机上还挂着一条"在听"。
 *
 * 口径（与 ECHO 那边"不能卡死"的取舍一致）：
 *   * **只有听到过一句话才算"有动静"** —— 环境底噪、风声、音乐都不算，
 *     否则开车放一路音乐就永远停不下来（[markActivity] 只由"真的送出一段"调用）；
 *   * 停下来时**必须说清原因**（"听了一刻钟也没听到你说话"），
 *     否则用户只会觉得"它自己断了"（这种静默失效正是最难查的）。
 */
class IdleAutoStop(private val idleLimitMs: Long = DEFAULT_IDLE_LIMIT_MS) {

    private var lastActivityMs: Long = 0
    private var started = false

    /** 开始监听（把计时器归零）。 */
    fun start(nowMs: Long) {
        started = true
        lastActivityMs = nowMs
    }

    /** 有动静了（送出去一段、或用户刚打开开关）。 */
    fun markActivity(nowMs: Long) {
        started = true
        lastActivityMs = nowMs
    }

    /** 已经静了多久（毫秒）；没开始返回 0。 */
    fun idleMs(nowMs: Long): Long =
        if (!started) 0 else (nowMs - lastActivityMs).coerceAtLeast(0)

    /** 该不该自己停。 */
    fun shouldStop(nowMs: Long): Boolean = started && idleMs(nowMs) >= idleLimitMs

    /** 停下来时给用户看的那句话（人话，带时长）。 */
    fun stopReason(nowMs: Long): String {
        val minutes = (idleMs(nowMs) / 60_000L).coerceAtLeast(1L)
        return "听了 $minutes 分钟也没听到你说话，先停了省电（要用再打开开关）"
    }

    companion object {
        /** 默认 20 分钟：开车时说话本来就断断续续，太短会烦人；再长就白耗电。 */
        const val DEFAULT_IDLE_LIMIT_MS = 20 * 60 * 1000L
    }
}
