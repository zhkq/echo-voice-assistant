package com.echo.contact.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.Role
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

    /** 中间那颗底座里图标下方那行小字（"超级助理"四个字在 56dp 圆的下面一行）。 */
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
 */
@Composable
internal fun EchoNavBar(
    current: EchoTab,
    onSelect: (EchoTab) -> Unit,
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
 * 中间那颗：**圆形底座 + 底座下面一行很小很小的字**。
 *
 * 文字为什么不压进圆里：圆只有 56dp，图标已经占 30dp，再塞一行 4 个汉字，
 * 大字号（系统字体放大）时必定挤出圆外或压住图标。放在圆下面那一行，就永远不打架；
 * 代价是这行字比左右四项的文字**略高一点**（圆抬起来了，文字跟着在条的上半部），
 * 这是有意的取舍 —— 不为了对齐基线去把圆压回条里（那就没有了"凸起"）。
 */
@Composable
private fun CenterNavItem(
    tab: EchoTab,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .width(EchoNavSpec.CENTER_TOUCH_DP.dp)
            // 圆角裁一下：默认的水波纹是方的，套在圆底座上很难看
            .clip(RoundedCornerShape(24.dp))
            .selectable(selected = selected, role = Role.Tab, onClick = onClick),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(
            modifier = Modifier.size(EchoNavSpec.CENTER_TOUCH_DP.dp),
            contentAlignment = Alignment.Center,
        ) {
            // 柔光：只在激活时出现（"这一页是主入口、而且你正在它上面"）
            if (selected) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(EchoPalette.Accent.copy(alpha = 0.18f), CircleShape),
                )
            }
            Box(
                modifier = Modifier
                    .size(EchoNavSpec.CENTER_PILL_DP.dp)
                    .background(
                        color = if (selected) EchoPalette.Accent else EchoPalette.Card2,
                        shape = CircleShape,
                    )
                    .border(
                        width = 1.dp,
                        color = if (selected) EchoPalette.Accent else EchoPalette.Accent.copy(alpha = 0.45f),
                        shape = CircleShape,
                    ),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    imageVector = echoTabIcon(tab),
                    contentDescription = tab.title,
                    modifier = Modifier.size(EchoNavSpec.CENTER_ICON_DP.dp),
                    tint = if (selected) Color.White else EchoPalette.Text,
                )
            }
        }
        Text(
            text = tab.title,
            fontSize = EchoNavSpec.CENTER_LABEL_SP.sp,
            color = if (selected) EchoPalette.Accent else EchoPalette.Muted,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
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
            text = tab.title,
            fontSize = EchoNavSpec.SIDE_LABEL_SP.sp,
            style = MaterialTheme.typography.labelSmall,
            color = tint,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}
