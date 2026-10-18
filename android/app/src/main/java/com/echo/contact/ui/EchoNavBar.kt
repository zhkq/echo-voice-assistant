package com.echo.contact.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.waitForUpOrCancellation
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.echo.contact.ui.theme.EchoPalette

/**
 * 底部导航的**造型尺寸表**（用户 2026-10-18："可以考虑 echo 超级助理在中间，左右各两个页签的样式，
 * 可以设计个造型"）。
 *
 * ```
 *                      ╭──────╮   ← 中间那颗向上凸起（露在条外 34dp ≈ 底座的 61%）
 *   ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁╭──┤  ⬤  ├──╮▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁   ← 条本体 60dp
 *    ▂ 指令   ▂ 会议   ╰──┴──────┴──╯   回顾  ▂  设置 ▂
 * ```
 *
 * ## 为什么这些数字要在**纯常量/纯函数**里，而不是散在 `Modifier.size(...)` 里
 *
 * "中间那颗更大、更凸" 是这一轮的**判据**，不是随手调的观感：如果尺寸只写在 Compose 的
 * modifier 链里，JVM 单测就碰不到它，只能靠肉眼看真机 —— 而"看起来差不多大"恰恰是最容易
 * 悄悄退化的地方（下一轮谁把 `weight(1f)` 改成等分，中间那颗当场就没了）。
 * 所以：**尺寸/抬升/槽位预算全是纯数据 + 纯函数**，由 `EchoNavBarTest` 断言。
 *
 * ## 为什么凸起的高度也算进"导航条总高度"
 *
 * 凸起必须**向上露在条外**才看得出"这是主入口"，但它绝不能盖住页面内容（尤其别挡页面底部的按钮）。
 * 做法不是在内容上浮一层，而是让**底部栏自身的高度 = 条本体 + 凸起量**（[TOTAL_HEIGHT_DP]）——
 * `Scaffold` 拿这个高度当内容的底部 padding，于是内容永远停在凸起圆顶之上。
 *
 * ⚠️ 改这些数字时**先看 `EchoNavBarTest`**：`CENTER_TOUCH_DP <= 360/5` 这条是"窄屏不重叠"的
 * 底线（360dp 是这一轮定的窄屏基准）。
 */
internal object EchoNavSpec {

    /** 窄屏基准（dp）：这一轮要求 360dp 下 5 项不挤、不重叠。 */
    const val NARROW_SCREEN_WIDTH_DP = 360f

    /** 窄屏下每一项自己的槽位宽度（360 / 5 = 72dp）。 */
    const val SIDE_TAP_MIN_WIDTH_DP = 48f

    /** 导航条**本体**高度（不含露在条外的凸起、不含系统导航区）。 */
    const val BAR_HEIGHT_DP = 60f

    /** 条本体上沿那条 1dp 分隔线（跟面板的 `--border` 一致，不是阴影）。 */
    const val HAIRLINE_DP = 1f

    /** 中间那颗的**圆形底座**直径。 */
    const val CENTER_PILL_DP = 56f

    /**
     * 中间那颗的**触摸区**直径（比底座大一圈）。
     *
     * 为什么触摸区要单独一档：底座是圆的，手指落点却是个矩形；触摸区比视觉底座大一点，
     * 才既"看起来没变大"又好点中。同时它也是"中间那颗比其余四项大"的可测判据。
     */
    const val CENTER_TOUCH_DP = 64f

    /** 中间那颗的图标（比两侧大一档：30 vs 22）。 */
    const val CENTER_ICON_DP = 30f

    /** 两侧四项的图标。 */
    const val SIDE_ICON_DP = 22f

    /**
     * 两侧四项的触摸高度。
     *
     * 比条本体（60）矮一点、比无障碍最小值（48）高：既留出条上下沿的呼吸，
     * 又让"中间那颗的触摸区更大"成为一条真实可测的差异（64 > 56）。
     */
    const val SIDE_TAP_HEIGHT_DP = 56f

    /**
     * 中间那颗**露在条外**的高度（底座顶边高出条本体上沿多少）。
     *
     * 34 / 56 ≈ 61%：即"底座大约六成在条外"——用户要的是"一眼看出是主入口"，
     * 露得再少就变成"只是稍微大一点的页签"，露得再多就变成 FAB 挡住内容。
     */
    const val CENTER_LIFT_DP = 34f

    /**
     * 中间那颗**圆下那行可见文字**。
     *
     * 用户 2026-10-18："底部圆形 logo 下方可以写个 ECHO 或者超级助理，你自己排一下哪个好看都行"。
     * 定 **`ECHO`**：**圆下是品牌位** —— 56dp 的圆本来就当 logo 用，下面配 4 个字母最像品牌标；
     * 「超级助理」四个汉字要在同一宽度里（还得保持 9sp、不压圆、不挤左右邻居）只能压到 9sp 以下，
     * 又小又难认。
     *
     * ⚠️ **显示名与语义名故意分开**：可见文字是 `ECHO`，而**页签名 / 页面标题 / 无障碍仍是
     * [EchoTab.ASSISTANT.title]（"超级助理"）** —— 读屏与搜索还认得它。所以别把这条当成
     * "把页签改名"去做（改 `EchoTabs.kt` 会牵连页面标题与无障碍，且这里本来就不需要动它）。
     */
    const val CENTER_LABEL = "ECHO"

    /** 中间那颗底座下那行小字（[CENTER_LABEL] = "ECHO" 在 56dp 圆的下面一行）。 */
    const val CENTER_LABEL_SP = 9f

    /** 两侧四项的文字。 */
    const val SIDE_LABEL_SP = 10f

    /**
     * 触摸圈比底座大，是**居中**套着的：所以触摸圈的顶边要多抬 `(64-56)/2 = 4dp`，
     * 这样"底座露在条外 34dp"才和 [CENTER_LIFT_DP] 一字不差。
     */
    const val CENTER_TOUCH_PAD_DP = (CENTER_TOUCH_DP - CENTER_PILL_DP) / 2f

    /** 触摸圈的顶边相对条本体上沿的位置（负数 = 抬到条外）。 */
    const val CENTER_TOUCH_OFFSET_DP = CENTER_LIFT_DP + CENTER_TOUCH_PAD_DP

    /**
     * 底部栏**总高度** = 条本体 + 露在条外的那截（含触摸圈的 4dp 余量）。
     *
     * `Scaffold` 用这个高度给内容留底部 padding —— 所以凸起**不会盖住内容**，
     * 页面底部的按钮也不会被它压住。
     */
    const val TOTAL_HEIGHT_DP = BAR_HEIGHT_DP + CENTER_TOUCH_OFFSET_DP
}

/**
 * 中间那颗在 [tabs] 里的下标（纯函数：**正中间** = `size / 2`）。
 *
 * 为什么不是写死 `EchoTab.ASSISTANT`：顺序只由 `EchoTabs.kt` 的枚举决定（[ECHO_TABS]）。
 * 造型跟着"中间那一格"走，加/减页签时不需要再改一次导航条；同时 `EchoNavBarTest` 钉住
 * "当前顺序下中间那颗正好是超级助理"——两条一起，才既单一来源又不跑偏。
 */
internal fun echoCenterIndex(tabs: List<EchoTab>): Int = tabs.size / 2

/** 中间那颗**是哪一页**（由 [echoCenterIndex] 决定，不在任何别处写第二遍）。 */
internal fun echoCenterTab(tabs: List<EchoTab>): EchoTab = tabs[echoCenterIndex(tabs)]

/**
 * 导航条上某一格**可见**的文案（纯函数：用例不渲染 Compose 也能断言"这五格分别写什么"）。
 *
 *  * **中间那颗 = 品牌位**：圆下写 [EchoNavSpec.CENTER_LABEL]（`ECHO`），**不用**页签名
 *    「超级助理」—— 圆是 logo，下面配 4 个字母才排得开（见该常量的说明）。
 *  * **左右四项 = 页签名**（指令/会议/回顾/设置），一个字都没动。
 *
 * 为什么抽成纯函数而不是在 `Text(text = ...)` 里各写一遍：这一轮最容易被"顺手改掉"的
 * 恰恰是这行小字，而 Compose 在 JVM 门禁里渲染不了 —— 只有把它变成纯数据，
 * `EchoNavBarTest` 才能直接钉住"中间是 ECHO、左右还是那四个"。
 *
 * @param center 哪一格是中间那颗（默认按 [ECHO_TABS] 算，与 [echoCenterTab] 同一个来源）。
 */
internal fun navItemLabel(tab: EchoTab, center: EchoTab = echoCenterTab(ECHO_TABS)): String =
    if (tab == center) EchoNavSpec.CENTER_LABEL else tab.title

/**
 * 中间那颗的**无障碍 / 语义名**：仍是页签名「超级助理」。
 *
 * 为什么可见文字叫 `ECHO`、语义名却还是「超级助理」：**显示名与语义名故意分开** ——
 * 肉眼看到的是品牌标（圆下写 ECHO），而读屏、搜索、页面标题仍用"超级助理"，
 * 用户说"打开超级助理"时，读屏念出来的和搜到的都还是这四个字。
 */
internal fun centerNavSemanticLabel(tab: EchoTab): String = tab.title

/**
 * 底部导航要渲染的顺序：**恒等于 [ECHO_TABS]**，与"当前选中哪一页"无关。
 *
 * 为什么要抽成函数：选中态只该改变**颜色**，不该改变**位置**（"选中的那颗跳到中间/左边"
 * 这种设计会让用户每次切换都要重新找位置）。用例遍历所有页断言这一点。
 *
 * 形参 `current` 是**故意保留的**：它让"顺序与选中无关"这件事有地方可断言
 * （`navTabs(EchoTab.SETTINGS) == ECHO_TABS`），而实现里它确实不该参与排序 ——
 * 所以这里显式压掉"未使用"的告警，不是漏用了。
 */
@Suppress("UNUSED_PARAMETER")
internal fun navTabs(current: EchoTab): List<EchoTab> = ECHO_TABS

/** 每一格等分的宽度（5 项 = 屏宽 / 5）。 */
internal fun navSlotWidthDp(availableWidthDp: Float, slots: Int): Float = availableWidthDp / slots

/** 第 [index] 格的左边界（左闭右开）。 */
internal fun navSlotStartDp(index: Int, availableWidthDp: Float, slots: Int): Float =
    index * navSlotWidthDp(availableWidthDp, slots)

/** 第 [index] 格的右边界（左闭右开）。 */
internal fun navSlotEndDp(index: Int, availableWidthDp: Float, slots: Int): Float =
    (index + 1) * navSlotWidthDp(availableWidthDp, slots)

/**
 * 这个宽度下 5 项放得下吗（纯函数，专给"窄屏 360dp 不重叠"用）。
 *
 * 判据有两条，缺一不可：
 *  ① 每一项的槽位都够宽（>= 无障碍最小 48dp）；
 *  ② **中间那颗的触摸圈塞得进自己的槽位** —— 这是最容易漏的一条：凸起那颗比别的大，
 *     一旦它宽过槽位，就会压到左右两项的点击区（看起来"点会议却回到了首页"）。
 */
internal fun navFitsWidth(availableWidthDp: Float, slots: Int = ECHO_TABS.size): Boolean {
    val slot = navSlotWidthDp(availableWidthDp, slots)
    return slot >= EchoNavSpec.SIDE_TAP_MIN_WIDTH_DP && slot >= EchoNavSpec.CENTER_TOUCH_DP
}

/** 中间那颗"露在条外"的比例（相对它自己的底座直径）。 */
internal fun centerProtrudeFraction(): Float =
    EchoNavSpec.CENTER_LIFT_DP / EchoNavSpec.CENTER_PILL_DP

/**
 * 中间那颗**圆形底座**在给定屏宽下的左边界（左闭右开；横向居中于它的槽位）。
 *
 * 为什么连这个都要算出来：底座比其余四项宽（56 vs 22 的图标），它会不会**压到邻居的点击区**
 * 是"窄屏会不会点错"的核心问题。有了这个纯函数，用例就能在 360dp / 320dp 上直接算，
 * 而不是"真机上试试点会议会不会回首页"。
 */
internal fun centerPillStartDp(availableWidthDp: Float, slots: Int = ECHO_TABS.size): Float {
    val slot = navSlotWidthDp(availableWidthDp, slots)
    val slotCenter = navSlotStartDp(slots / 2, availableWidthDp, slots) + slot / 2f
    return slotCenter - EchoNavSpec.CENTER_PILL_DP / 2f
}

/** 中间那颗圆形底座的右边界（左闭右开）。 */
internal fun centerPillEndDp(availableWidthDp: Float, slots: Int = ECHO_TABS.size): Float =
    centerPillStartDp(availableWidthDp, slots) + EchoNavSpec.CENTER_PILL_DP

/** 页签 → 图标（只做外观映射；页签本身见 [EchoTab]）。 */
internal fun echoTabIcon(tab: EchoTab): ImageVector = when (tab) {
    EchoTab.ASSISTANT -> Icons.Default.Dashboard
    EchoTab.COMMANDS -> Icons.Default.Mic
    EchoTab.MEETINGS -> Icons.Default.EventNote
    EchoTab.REVIEW -> Icons.Default.History
    EchoTab.SETTINGS -> Icons.Default.Settings
}

/**
 * 底部导航：**中间一颗凸起的主入口，左右各两项**（顺序照 [ECHO_TABS]，不在这里排序）。
 *
 * 高亮规则（用户点名的"当前页高亮"）：
 *  * 中间那颗：在超级助理页时是**激活态**（主色实心 + 柔光），在别的页时是**常态**
 *    （深底 + 主色描边），但因为它更大、更高、更靠前，**任何页都比其余四项醒目**；
 *  * 两侧四项：选中的那颗图标与文字变成主色；其余更暗（`Muted`）。
 *
 * @param current 当前页（只决定颜色，不决定位置）。
 * @param onSelect 点了某一页（调用方接 `vm.selectTab(it.ordinal)`）。
 * @param onHoldStart 中间那颗**按住到长按阈值**时调（返回 false = 麦没开成，这一次不算录音）。
 *   调用方接的是 `MainViewModel.startRecording()` —— 与「指令」页那颗「按住说话」**同一个入口**。
 * @param onHoldRelease 按住之后松手（结束并发送，`MainViewModel.finishRecording()`）。
 * @param onHoldCancel 按住之后拖开/上滑出界（`MainViewModel.cancelRecording()`）。
 */
@Composable
internal fun EchoNavBar(
    current: EchoTab,
    onSelect: (EchoTab) -> Unit,
    onHoldStart: () -> Boolean,
    onHoldRelease: () -> Unit,
    onHoldCancel: () -> Unit,
) {
    val tabs = navTabs(current)
    val center = echoCenterTab(tabs)

    Column(modifier = Modifier.fillMaxWidth()) {
        // ① 凸起露在条外的那一截：**透明**（露出页面底色，才看得出"浮起来了"）。
        //    它照样算在底部栏高度里 —— 所以内容不会被盖住（见 EchoNavSpec.TOTAL_HEIGHT_DP）。
        Spacer(
            modifier = Modifier
                .fillMaxWidth()
                .height(EchoNavSpec.CENTER_TOUCH_OFFSET_DP.dp),
        )

        // ② 条本体
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(EchoNavSpec.BAR_HEIGHT_DP.dp)
                .background(EchoPalette.Card),
        ) {
            // 上沿 1dp 分隔线（凸起那颗会压过它 —— 这正是"浮在条上"的视觉线索）
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(EchoNavSpec.HAIRLINE_DP.dp)
                    .align(Alignment.TopCenter)
                    .background(EchoPalette.Border),
            )

            // 五个等宽的格子：左右四格画页签，中间那格**留空**（由下面的 overlay 画），
            // 但格子本身不画东西、也不可点 —— 点击区在凸起那颗自己的 Box 上。
            Row(
                modifier = Modifier.fillMaxSize(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                tabs.forEach { tab ->
                    Box(
                        modifier = Modifier
                            .weight(1f)
                            .fillMaxHeight(),
                        contentAlignment = Alignment.Center,
                    ) {
                        if (tab != center) {
                            SideNavItem(
                                tab = tab,
                                selected = tab == current,
                                onClick = { onSelect(tab) },
                            )
                        }
                    }
                }
            }

            // 中间那颗：横向居中、向上抬出条外。
            // `offset` 只移动绘制位置、不占布局 —— 抬出的那一截由第 ① 步的透明 Spacer 预留，
            // 所以**不会压到页面内容**（Scaffold 的底部 padding 已经是含凸起的总高度）。
            CenterNavItem(
                tab = center,
                selected = center == current,
                onClick = { onSelect(center) },
                onHoldStart = onHoldStart,
                onHoldRelease = onHoldRelease,
                onHoldCancel = onHoldCancel,
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .offset(y = -EchoNavSpec.CENTER_TOUCH_OFFSET_DP.dp),
            )
        }

        // ③ 系统导航区（手势条 / 三键）：也铺条底色，免得下面露出另一种颜色。
        //    不是 edge-to-edge（targetSdk 34 且没开 enableEdgeToEdge）时这份 inset 通常是 0。
        Spacer(
            modifier = Modifier
                .fillMaxWidth()
                .background(EchoPalette.Card)
                .navigationBarsPadding(),
        )
    }
}

/**
 * 中间那颗：**圆形底座 + 底座下面一行很小很小的字**（文字 = [EchoNavSpec.CENTER_LABEL]）。
 *
 * 为什么圆下写 `ECHO` 而不是页签名：**圆下是品牌位**（56dp 的圆就是这 App 的 logo），
 * 4 个字母放得开、像品牌标；「超级助理」四个汉字塞进这个宽度只能压到 9sp 以下。
 * 页签/页面标题/无障碍**仍是**「超级助理」（[centerNavSemanticLabel]）—— 显示名与语义名
 * 故意分开，读屏和搜索还认得它。
 *
 * 文字为什么不压进圆里：圆只有 56dp，图标已经占 30dp，再塞一行字，
 * 大字号（系统字体放大）时必定挤出圆外或压住图标。放在圆下面那一行，就永远不打架；
 * 代价是这行字比左右四项的文字**略高一点**（圆抬起来了，文字跟着在条的上半部），
 * 这是有意的取舍 —— 不为了对齐基线去把圆压回条里（那就没有了"凸起"）。
 *
 * ## 2026-10-18 夜里：这颗圆要能**按住说话**（用户原话："app 下方中间的 logo 增加一个
 * 按住说话发指令的功能"）
 *
 * 手势语义、状态机、以及"为什么不新增第二条录音路"全在 `NavHold.kt`（纯函数，`NavHoldTest` 钉住）。
 * 这里只做三件事：把指针事件翻译成 [NavHoldKey] 喂给 [NavHoldMachine]、执行它吐出来的
 * [NavHoldAction]、以及按住时的**手感反馈**。
 *
 * ⚠️ **短按不许被吃掉**：原来这颗圆是 `selectable(...)`（点一下就切页）。改成手工手势之后，
 * 点击语义要**自己补回来**（`semantics { onClick }`）—— 否则读屏/辅助功能就点不动它了；
 * 手势上也一样：没按满长按阈值就松手 → 走"切页"，绝不进录音。
 *
 * ⚠️ **反馈不许把导航条挤变形**：放大只发生在**绘制**（`Modifier.scale`，不占布局），
 * 提示那行是**浮层**（`offset` 负值画到条外，不进 Column 的测量）—— 于是
 * [EchoNavSpec.TOTAL_HEIGHT_DP] 一个 dp 都不用改（`HomeLayoutTest` 的一屏预算照旧）。
 */
@Composable
private fun CenterNavItem(
    tab: EchoTab,
    selected: Boolean,
    onClick: () -> Unit,
    onHoldStart: () -> Boolean,
    onHoldRelease: () -> Unit,
    onHoldCancel: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val machine = remember { NavHoldMachine() }
    val recording = navHoldRecording(machine.state)
    val scale = navHoldScale(machine.state)
    val hint = navHoldHint(machine.state)
    val click = onClick
    val isSelectedTab = selected

    Box(modifier = modifier.width(EchoNavSpec.CENTER_TOUCH_DP.dp)) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                // 圆角裁一下：默认的水波纹是方的，套在圆底座上很难看
                // （只裁这一列：上面那行提示是浮层，不能被裁掉，见函数说明）
                .clip(RoundedCornerShape(24.dp))
                // 点击语义自己补回来：读屏点它仍然等于"切到超级助理页"
                .semantics {
                    role = Role.Tab
                    this.selected = isSelectedTab
                    onClick(label = "打开${centerNavSemanticLabel(tab)}") {
                        click()
                        true
                    }
                }
                .pointerInput(Unit) {
                    // 为什么**不用** `detectTapGestures(onPress = …, onLongPress = …)`：
                    // 那个组合的内部时序是"长按到点就把 press 作用域 release 掉"，
                    // 于是 `tryAwaitRelease()` 会在**长按那一刻**返回 true —— 那就是"按住 400ms
                    // 就直接发出去"，用户还没松手。这个坑 JVM 门禁看不见（它不跑手势），
                    // 只有在真机上才会表现成"刚按住就把话说出去"。
                    //
                    // 所以这里用最原始的指针事件自己排时序，只依赖三个稳定 API：
                    // `awaitEachGesture` / `awaitFirstDown` / `waitForUpOrCancellation`。
                    awaitEachGesture {
                        val down = awaitFirstDown(requireUnconsumed = false)
                        down.consume()
                        machine.dispatch(NavHoldKey.DOWN)          // → PRESSED（什么都还没做）

                        // ① 长按阈值之内：抬起（短按）还是被取消（拖出去/被抢走）
                        val withinTimeout = withTimeoutOrNull(NavHoldSpec.LONG_PRESS_MS) {
                            // 用 Result 包一层，才能把"超时(null)"与"手势被取消(值为 null)"分开
                            Result.success(waitForUpOrCancellation())
                        }
                        val upEarly = withinTimeout?.getOrNull()

                        val acts = if (withinTimeout == null) {
                            // ② 阈值用完 = 长按成立
                            val started = machine.dispatch(NavHoldKey.ELAPSED_LONG_PRESS)
                            if (NavHoldAction.START_RECORDING in started &&
                                onHoldStart()      // 没权限 / 麦被占着 → false，这一次不算录
                            ) {
                                // ③ 真的在录：等手指**真的**抬起（悬停多久都不发）
                                val after = waitForUpOrCancellation()
                                machine.dispatch(
                                    if (after != null) NavHoldKey.UP else NavHoldKey.CANCEL
                                )
                            } else {
                                // 麦没开成：相位清回 IDLE，把这次手势吞掉（不许掉进"发空指令"）
                                machine.abandon()
                                waitForUpOrCancellation()
                                emptyList()
                            }
                        } else {
                            // 短按 = 抬起；取消 = 什么都不做（用户并没有"点"这一页）
                            if (upEarly != null) upEarly.consume()
                            machine.dispatch(
                                if (upEarly != null) NavHoldKey.UP else NavHoldKey.CANCEL
                            )
                        }

                        when {
                            // 短按：松手在长按阈值之前 → 切页（原来 selectable 的行为）
                            NavHoldAction.SWITCH_TO_CENTER_TAB in acts -> click()
                            // 按住之后松手 → 结束并发送（既有那条提交路）
                            NavHoldAction.FINISH_AND_SEND in acts -> onHoldRelease()
                            // 按住之后拖开/上滑出界 → 取消（既有 cancelRecording）
                            NavHoldAction.CANCEL_RECORDING in acts -> onHoldCancel()
                        }
                    }
                },
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Box(
                modifier = Modifier.size(EchoNavSpec.CENTER_TOUCH_DP.dp),
                contentAlignment = Alignment.Center,
            ) {
                // 柔光：激活时一直有；**按住说话时更亮一圈**（"它在听你说"）
                if (isSelectedTab || recording) {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .background(
                                EchoPalette.Accent.copy(
                                    alpha = if (recording) NavHoldSpec.HOLD_GLOW_ALPHA else 0.18f
                                ),
                                CircleShape,
                            ),
                    )
                }
                Box(
                    modifier = Modifier
                        .size(EchoNavSpec.CENTER_PILL_DP.dp)
                        // 按住时圆放大：**只改绘制，不占布局** —— 否则整条导航会被撑高、挤歪
                        // （用户点名叫的就是"别把底部导航挤变形"）。
                        .scale(scale)
                        .background(
                            color = when {
                                recording -> EchoPalette.Red
                                isSelectedTab -> EchoPalette.Accent
                                else -> EchoPalette.Card2
                            },
                            shape = CircleShape,
                        )
                        .border(
                            width = 1.dp,
                            color = when {
                                recording -> EchoPalette.Red
                                isSelectedTab -> EchoPalette.Accent
                                else -> EchoPalette.Accent.copy(alpha = 0.45f)
                            },
                            shape = CircleShape,
                        ),
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(
                        imageVector = echoTabIcon(tab),
                        // 语义名仍是页签名「超级助理」（可见文字是 ECHO，两者故意分开）
                        contentDescription = centerNavSemanticLabel(tab),
                        modifier = Modifier.size(EchoNavSpec.CENTER_ICON_DP.dp),
                        tint = if (isSelectedTab) Color.White else EchoPalette.Text,
                    )
                }
            }
            Text(
                // 圆下是品牌位：ECHO（不是页签名；页签名只用于标题/无障碍）
                text = navItemLabel(tab, center = tab),
                fontSize = EchoNavSpec.CENTER_LABEL_SP.sp,
                color = if (isSelectedTab) EchoPalette.Accent else EchoPalette.Muted,
                fontWeight = if (isSelectedTab) FontWeight.SemiBold else FontWeight.Normal,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }

        // 按住时那行提示："松手发送 · 上滑取消"。
        // 它是**浮层**：`offset` 负值只移动绘制位置、不参与 Column 的测量 —— 所以底部导航
        // 的高度/排版一个 dp 都不动（这正是"别把导航挤变形"这条要求的落点）。
        if (hint != null) {
            Text(
                hint,
                fontSize = 10.sp,
                color = Color.White,
                maxLines = 1,
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .offset(y = (-NavHoldSpec.HINT_OFFSET_DP).dp)
                    .background(EchoPalette.Accent, RoundedCornerShape(9.dp))
                    .padding(horizontal = 8.dp, vertical = 3.dp),
            )
        }
    }
}

/**
 * 两侧那四项：**等宽、更小、更暗**，选中的那颗变主色。
 *
 * 尺寸全部来自 [EchoNavSpec]（图标 22dp、触摸高度 56dp）；这里不再写死任何 dp，
 * 所以"中间那颗更大"这条判据只有一个来源。
 */
@Composable
private fun SideNavItem(
    tab: EchoTab,
    selected: Boolean,
    onClick: () -> Unit,
) {
    val tint = if (selected) EchoPalette.Accent else EchoPalette.Muted
    Column(
        modifier = Modifier
            // 横向铺满自己那一格（每一格的宽度由 Row 的 weight 等分决定，见 navFitsWidth）
            .fillMaxWidth()
            .height(EchoNavSpec.SIDE_TAP_HEIGHT_DP.dp)
            .clip(RoundedCornerShape(14.dp))
            .selectable(selected = selected, role = Role.Tab, onClick = onClick),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Icon(
            imageVector = echoTabIcon(tab),
            contentDescription = tab.title,
            modifier = Modifier.size(EchoNavSpec.SIDE_ICON_DP.dp),
            tint = tint,
        )
        Spacer(Modifier.height(2.dp))
        Text(
            // 左右四项就是页签名（指令/会议/回顾/设置），与中间那颗的品牌位无关
            text = navItemLabel(tab, center = echoCenterTab(ECHO_TABS)),
            fontSize = EchoNavSpec.SIDE_LABEL_SP.sp,
            style = MaterialTheme.typography.labelSmall,
            color = tint,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}
