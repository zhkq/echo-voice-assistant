package com.echo.contact.service

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * 常驻听写的**状态单一事实源**：服务写、界面与通知读。
 *
 * 为什么不把状态塞在 Service 里让界面去 bind：`ListenService` 是前台服务，
 * 界面可能根本不活着（锁屏、切走）——状态必须是**进程级**的，
 * 界面回来时（`collectAsState`）要能立刻显示"现在到底在干什么"。
 *
 * [textsOf] 刻意是**纯函数**：通知/界面上那两行字是全靠猜最容易写错的地方
 * （"在听"和"正在交给 ECHO"混了，用户就不知道为什么说完没反应），
 * 做成纯函数才能在 JVM 单测里逐档钉住（`ListenStateTest`）。
 */
object ListenState {

    enum class Phase {
        /** 没在跑。 */
        OFF,

        /** 在开麦（申请/打开 AudioRecord 的那几百毫秒）。 */
        STARTING,

        /** 在听（这一段还没有人说话，或正在说）。 */
        LISTENING,

        /** 已经把一段发给 ECHO，等它回话。 */
        THINKING,

        /** 正在念回复。 */
        SPEAKING,

        /** 用户按了暂停（耳机键或通知）：服务还活着，但麦克风不采了。 */
        PAUSED,

        /** 出错停了（原因在 detail 里）。 */
        FAILED,
    }

    private val _phase = MutableStateFlow(Phase.OFF)
    val phase: StateFlow<Phase> = _phase.asStateFlow()

    private val _detail = MutableStateFlow("")
    val detail: StateFlow<String> = _detail.asStateFlow()

    /** 本次开启一共送出去几段（通知里能看到"已经听了几句"）。 */
    private val _segments = MutableStateFlow(0)
    val segments: StateFlow<Int> = _segments.asStateFlow()

    /** 进程内"服务是否在跑"——界面据此决定开关的位置，不依赖通知是否被系统折叠。 */
    @Volatile
    var running: Boolean = false
        private set

    fun set(phase: Phase, detail: String = "") {
        _phase.value = phase
        _detail.value = detail
        running = phase != Phase.OFF && phase != Phase.FAILED
    }

    fun countSegment() {
        _segments.value = _segments.value + 1
    }

    fun reset(phase: Phase = Phase.OFF, detail: String = "") {
        _phase.value = phase
        _detail.value = detail
        _segments.value = 0
        running = phase != Phase.OFF && phase != Phase.FAILED
    }

    /** 通知标题 + 正文（纯函数）。[detail] 为空时按档位给默认话术。 */
    fun textsOf(phase: Phase, detail: String, segments: Int): Pair<String, String> {
        val title = when (phase) {
            Phase.OFF -> "后台听写已关闭"
            Phase.STARTING -> "ECHO 正在开麦…"
            Phase.LISTENING -> "在听：说完停一下我就发出去"
            Phase.THINKING -> "正在交给 ECHO…"
            Phase.SPEAKING -> "正在念回复"
            Phase.PAUSED -> "已暂停（按耳机键或通知里的「继续」）"
            Phase.FAILED -> "后台听写出错了"
        }
        val body = when {
            detail.isNotBlank() -> detail
            phase == Phase.LISTENING && segments > 0 -> "本次已经听出去 $segments 句"
            phase == Phase.THINKING -> "在等它回话，稍等"
            phase == Phase.STARTING -> "第一次开麦可能要一两秒"
            phase == Phase.PAUSED -> "麦克风没在采；再按一下耳机键就继续"
            phase == Phase.OFF -> "点开开关就开始听"
            else -> ""
        }
        return title to body
    }

    // ------------------------------------------------------------------ 首页那根常驻细条

    /**
     * 常驻细条要显示的两行字：[label] 是**相位词**，[text] 是最近一次的具体文案。
     *
     * 是纯数据（不带颜色）：颜色是界面的事（`StatusDot` 要 `Compose Color`），
     * 这里只管"说什么" —— 这样整个函数能在 JVM 单测里逐档钉住。
     */
    data class Strip(val label: String, val text: String)

    /**
     * 相位 → 首页细条文案（**纯函数**，`ListenStateTest` 钉住）。
     *
     * 返回 null = **不显示**（[Phase.OFF]）：首页不该常驻一条"什么都没在发生"的空条。
     *
     * 为什么不用 [textsOf] 的标题：那条是**通知标题**，措辞是通知语境
     * （"说完停一下我就发出去"、括号里写"通知里的「继续」"）——
     * 搬到首页细条上会指错地方。所以另给一组短词，只保留"在听/在等/在念/已暂停/出错"的区分。
     * ⚠️ 两套文案必须**逐档都分得开**（`ListenStateTest` 有一条专门盯着"别都说在听"）。
     *
     * [detail] 优先：暂停的原因（"按了耳机键：先不听了，再按一下继续"）来自服务，
     * 比任何默认话术都准 —— 用户要看的正是"为什么停了"。
     */
    fun stripOf(phase: Phase, detail: String, segments: Int): Strip? {
        if (phase == Phase.OFF) return null
        val label = when (phase) {
            Phase.STARTING -> "正在开麦"
            Phase.LISTENING -> "在听"
            Phase.THINKING -> "正在等 ECHO"
            Phase.SPEAKING -> "正在念"
            Phase.PAUSED -> "已暂停"
            Phase.FAILED -> "已停下"          // 出错停了：原因在 detail 里，这里说"停下了"
            Phase.OFF -> return null
        }
        val text = when {
            detail.isNotBlank() -> detail
            phase == Phase.LISTENING && segments > 0 -> "本次已经听出去 $segments 句"
            phase == Phase.LISTENING -> "说一句就行，我送出去"   // 还没送出过一句也要有话说，别留一条半截的细条
            phase == Phase.STARTING -> "第一次开麦可能要一两秒"
            phase == Phase.THINKING -> "在等它回话"
            phase == Phase.PAUSED -> "麦克风没在采音"
            phase == Phase.FAILED -> "出错了"
            else -> ""
        }
        return Strip(label, text)
    }
}
