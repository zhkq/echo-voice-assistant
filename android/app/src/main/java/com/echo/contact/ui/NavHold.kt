package com.echo.contact.ui

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/**
 * 底部中间那颗圆 logo 的**按住说话**（用户 2026-10-18 夜里原话：
 * *"app 下方中间的 logo 增加一个按住说话发指令的功能"*）。
 *
 * ## 为什么这颗圆要能按住说话
 *
 * 圆是**主入口**，而"说话"是这个 App 的主动作 —— 用户要的是"在任何一页都能按住中间那颗说一句"，
 * 不必先切到「指令」页、再找那颗「按住说话」。但**短按的语义一个字都不能改**：
 * 短按仍然是"回/切到超级助理页"（那是这颗圆本来就有的唯一动作）。
 *
 * ## 为什么把"手势 → 动作"做成纯状态机（[navHold]）
 *
 * Compose 的手势在 JVM 门禁里跑不起来（没有渲染、没有指针事件），可这一轮最容易改坏的地方
 * 恰恰是"短按会不会被长按吃掉"、"松手到底发不发"、"拖开到底取不取消"。所以把
 * **手势序列 → 该做哪件事** 抽成一张表：界面只负责把 [NavHoldKey] 喂进来、把
 * [NavHoldAction] 执行出去，判据（`NavHoldTest`）直接对着这张表逐条断言。
 *
 * ## 三条语义（用户点名的）
 *
 * | 手势 | 动作 | 为什么 |
 * | --- | --- | --- |
 * | 按下→**很快**松手（< [NavHoldSpec.LONG_PRESS_MS]） | [NavHoldAction.SWITCH_TO_CENTER_TAB] | 短按照旧切页，**不许**被长按吃掉 |
 * | 按住到 [NavHoldSpec.LONG_PRESS_MS] | [NavHoldAction.START_RECORDING] | 长按才开录 |
 * | 按住之后松手 | [NavHoldAction.FINISH_AND_SEND] | 松手就发（结束+发送是同一条既有路） |
 * | 按住之后手指拖开 / 上滑出界 | [NavHoldAction.CANCEL_RECORDING] | 与「指令」页那颗「按住说话」同语义 |
 *
 * ## ⚠️ 不新增第二条录音路
 *
 * 这里**一个录音 API 都没有**（没有 `MediaRecorder`、没有 `AudioRecord`、没有 `recorder.start`）：
 * 三个动作由 `HomeScreen` 直接接到 `MainViewModel.startRecording()` / `finishRecording()` /
 * `cancelRecording()` —— 就是「指令」页那颗「按住说话」与首页指令卡在用的**同三个方法**。
 * 判据除了这张状态表，还有一条**源码级**断言（`NavHoldTest`：全仓库只有一个
 * `fun startRecording(` 声明，本文件里不许出现任何录音实现）。
 */
internal object NavHoldSpec {

    /**
     * 按住多久算"长按"。
     *
     * 取 400ms：比系统的长按阈值（约 500ms）略短一点点 —— 用户按下去时的心理预期是
     * "按住就开始说"，短一点更跟手；又远高于误触（两次点击之间的抖动不会超过 200ms）。
     */
    const val LONG_PRESS_MS = 400L

    /** 按住时圆放大的倍数（反馈之一）。 */
    const val HOLD_SCALE = 1.18f

    /** 按住时外面那圈光晕的不透明度（反馈之二）。 */
    const val HOLD_GLOW_ALPHA = 0.30f

    /**
     * 按住时那行提示（放在圆的上方，**浮层**，不进布局）。
     *
     * 为什么必须说"上滑取消"：按住说话最怕的是"说错了但不知道怎么办"——
     * 手指一滑就能取消，比"松开再点取消"少一步，而且不会误发。
     */
    const val HOLD_HINT = "松手发送 · 上滑取消"

    /**
     * 那行提示相对触摸圈顶边的偏移（**负值 = 往上画到条外**）。
     *
     * 为什么用 `offset` 而不是在 Column 里加一行：加一行就会让这颗圆所在的列变高，
     * 底部导航整条跟着变形（用户点名叫的就是"别把底部导航挤变形"）。`offset` 只移动**绘制**位置，
     * 不参与测量 —— 导航条的总高 [EchoNavSpec.TOTAL_HEIGHT_DP] 一个 dp 都不动。
     */
    const val HINT_OFFSET_DP = 22f
}

/** 一次手势里发生的事（界面把指针事件翻译成它）。 */
internal enum class NavHoldKey {
    /** 手指按下。 */
    DOWN,

    /** 按满 [NavHoldSpec.LONG_PRESS_MS]（长按成立）。 */
    ELAPSED_LONG_PRESS,

    /** 松手，而且手势**没有被取消**（还在圆里）。 */
    UP,

    /** 手势被取消：手指拖开 / 上滑出界 / 被别的组件抢走。 */
    CANCEL,
}

/** 处在哪个相位。 */
internal enum class NavHoldPhase {
    IDLE,
    PRESSED,
    RECORDING,
}

/** 该做的那件事（界面负责执行，纯数据层不碰任何录音 API）。 */
internal enum class NavHoldAction {
    /** 短按：切到中间那颗对应的页（既有动作）。 */
    SWITCH_TO_CENTER_TAB,

    /** 长按：开始录一句指令（`MainViewModel.startRecording`）。 */
    START_RECORDING,

    /** 松手：结束并发送（`MainViewModel.finishRecording`）。 */
    FINISH_AND_SEND,

    /** 拖开：取消（`MainViewModel.cancelRecording`）。 */
    CANCEL_RECORDING,
}

/**
 * 状态机的状态：当前相位 + 这次转换**要执行的动作**。
 *
 * 动作跟着转换走（而不是另外去问"现在是什么相位"），界面就不需要自己再判一遍
 * "松手算发还是算切页" —— 那正是最容易和状态表漂成两套的地方。
 */
internal data class NavHoldState(
    val phase: NavHoldPhase = NavHoldPhase.IDLE,
    val actions: List<NavHoldAction> = emptyList(),
)

/**
 * 一次转换（**纯函数**，`NavHoldTest` 逐条钉住）。
 *
 * 两条"不许多做"的规矩：
 *  * 没按满 [NavHoldSpec.LONG_PRESS_MS] 就松手 → 只切页，**绝不**开录（短按不许被吃掉）；
 *  * 没进入录音相位就取消（手指在圆里抖了一下又出去）→ **什么都不做**
 *    （既不发、也不切页：手势被取消时用户并没有"点"这一页）。
 */
internal fun navHold(state: NavHoldState, key: NavHoldKey): NavHoldState = when (state.phase) {
    NavHoldPhase.IDLE -> when (key) {
        // 新的一次手势永远从 IDLE 起（也顺手把上一次没收尾的相位清掉）
        NavHoldKey.DOWN -> NavHoldState(NavHoldPhase.PRESSED)
        // 空闲时收到的迟到事件（松手/取消/长按）一律**什么都不做**，而且不带旧动作
        else -> NavHoldState(NavHoldPhase.IDLE)
    }

    NavHoldPhase.PRESSED -> when (key) {
        NavHoldKey.UP -> NavHoldState(NavHoldPhase.IDLE, listOf(NavHoldAction.SWITCH_TO_CENTER_TAB))
        NavHoldKey.ELAPSED_LONG_PRESS ->
            NavHoldState(NavHoldPhase.RECORDING, listOf(NavHoldAction.START_RECORDING))
        NavHoldKey.CANCEL -> NavHoldState(NavHoldPhase.IDLE)
        NavHoldKey.DOWN -> NavHoldState(NavHoldPhase.PRESSED)
    }

    NavHoldPhase.RECORDING -> when (key) {
        NavHoldKey.UP -> NavHoldState(NavHoldPhase.IDLE, listOf(NavHoldAction.FINISH_AND_SEND))
        NavHoldKey.CANCEL -> NavHoldState(NavHoldPhase.IDLE, listOf(NavHoldAction.CANCEL_RECORDING))
        // 长按之后再"按满"没有第二次：**动作表必须是空的**（别把同一个长按当两次开录）
        NavHoldKey.ELAPSED_LONG_PRESS -> NavHoldState(NavHoldPhase.RECORDING)
        NavHoldKey.DOWN -> NavHoldState(NavHoldPhase.RECORDING)
    }
}

/**
 * 状态机对象（Compose 侧只 `remember` 这一个；纯函数 [navHold] 才是唯一判据）。
 *
 * 为什么还要这个壳：界面要在两个**不同的回调**里推进同一个手势（`onPress` 与 `onLongPress`
 * 是 `detectTapGestures` 的两个独立 lambda），所以相位必须存在组合外的一个可变对象里，
 * 不能是形参。
 */
internal class NavHoldMachine {

    var state by mutableStateOf(NavHoldState())
        private set

    /** 喂一个手势事件，返回这次**要执行的动作**。 */
    fun dispatch(key: NavHoldKey): List<NavHoldAction> {
        state = navHold(state, key)
        return state.actions
    }

    /** 长按之后麦其实没开成（没权限 / 麦被占）：把相位清回 IDLE，松手时什么都不做。 */
    fun abandon() {
        state = NavHoldState()
    }
}

/** 现在是不是"按住录音中"（界面据此放大圆 + 出提示）。 */
internal fun navHoldRecording(state: NavHoldState): Boolean = state.phase == NavHoldPhase.RECORDING

/** 按住时圆该多大（倍数）；不在录就是 1.0（**不改变布局**，只是绘制缩放）。 */
internal fun navHoldScale(state: NavHoldState): Float =
    if (navHoldRecording(state)) NavHoldSpec.HOLD_SCALE else 1f

/** 按住时那行提示；不在录时返回 null（`EchoNavBarTest`/`NavHoldTest` 钉住文案）。 */
internal fun navHoldHint(state: NavHoldState): String? =
    if (navHoldRecording(state)) NavHoldSpec.HOLD_HINT else null
