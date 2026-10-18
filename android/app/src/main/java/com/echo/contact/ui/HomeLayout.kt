package com.echo.contact.ui

/**
 * 首页的**布局预算**（纯常量 + 纯函数，所以"一屏放得下"能在 JVM 门禁里断言）。
 *
 * ## 为什么必须先把尺寸算清楚，而不是"看着差不多就行"
 *
 * 用户 2026-10-18 傍晚的原话：
 *
 * > *"首页的样式要按照 app 来设计，不要出现滚动条，要合理利用空间，包括现在顶部那个空条全面设计"*
 *
 * "不要出现滚动条"的真意思不是"别让用户滚"，而是**内容必须一屏装下** ——
 * 只要内容比视口高，滚动就是必然的（哪怕不画滚动条，用户也会发现页面是浮的、拖得动）。
 * 所以做法是：先算清楚 360×640（最小的常见手机）上每一块占多少，再让界面按这份预算摆。
 *
 * ## 预算怎么来的（以 360×640 为例）
 *
 * 屏高 640 − 状态栏 24 − 系统导航 48 − 底部导航 98（含中间那颗凸起的 38）= **470dp** 内容高。
 * `homeLayoutPlan(360, 470)` 的要目：
 *
 * | 区块 | 360×640 下的高度 |
 * | --- | --- |
 * | 顶栏 | **0**（首页**没有**顶栏 —— 不是"空条"，是整块不存在） |
 * | 应用名/连接/组件合并卡 | 84 |
 * | 在听细条 | 44 |
 * | 「主要功能」标题 | 16 |
 * | 指令卡（打字 + 按住说话 + 一行状态） | 134 |
 * | 会议卡 / 回顾卡（并列，各 ≈163 宽） | 134 |
 * | 页间距 6×4 + 上下边距 6+6 | 36 |
 * | **合计** | **448 ≤ 470** ✓ |
 *
 * 屏更高时（360×800 → 630dp 内容高、393×851 → 681dp），间距与卡片高度按
 * min→max 线性收敛（[HomeLayoutSpec] 里每项一对），多出来的高度交给"垂直居中"，
 * 而不是把某张卡硬撑成一大片空白。
 *
 * ## 为什么"最小"那一档也要够用（而不是把字压到看不清）
 *
 * 卡里每个零件的尺寸都写在 [HomeLayoutSpec] 里，界面与预算**同源**：
 * 主按钮与"按住说话"48dp、在听细条 44dp（可点整行）、底部页签 56/64dp ——
 * 全部 ≥ 无障碍最小触摸尺寸，缩小的是**留白**，不是**按钮**。
 *
 * ## 字体放大怎么办（这是有意的取舍）
 *
 * 预算按默认字体（fontScale = 1.0）算。系统字体放大后内容会变高 —— 那时页面**允许滚动**，
 * 但 Compose 的 `verticalScroll` **不画滚动条**，而且内容一个字都不会被截断。
 * 宁可放大字体后要滚一下，也不要把字压小到看不清。
 */
internal object HomeLayoutSpec {

    // ---- 系统栏（只用于"屏高 → 可用内容高"的换算；预算本身只吃内容高）----

    /** 状态栏（常见值 24dp；360×640 那一档）。 */
    const val STATUS_BAR_DP = 24f

    /** 系统导航栏（按**三键导航**的 48dp 保守算；手势条更小，只会更宽松）。 */
    const val SYSTEM_NAV_BAR_DP = 48f

    // ---- 页面 ----

    /** 页面左右边距（卡片不贴边，才像 App）。 */
    const val PAGE_H_PADDING_DP = 14f

    const val PAGE_TOP_MIN_DP = 6f
    const val PAGE_TOP_MAX_DP = 14f
    const val PAGE_BOTTOM_MIN_DP = 6f
    const val PAGE_BOTTOM_MAX_DP = 14f

    /** 区块之间的间距（自适应：小屏收紧、大屏松开）。 */
    const val GAP_MIN_DP = 6f
    const val GAP_MAX_DP = 14f

    /** 首页有几个"块间缝"：合并卡 / 在听 / 标题 / 指令卡 / 并列两卡 = 4 条缝。 */
    const val GAP_COUNT = 4

    // ---- 区块高度（min → max，按可用高度线性收敛）----

    /**
     * 首页顶栏高度 = **0**。
     *
     * 用户点名的"顶部那个空条"就是它：原来首页顶栏只画一个留空的标题（`homeTopBarTitle`
     * 对首页返回空串），于是整整一条 64dp 的空白。这一版**整块删掉**（`homeTopBar` 返回
     * `None`），应用名与连接状态只出现在下面那张合并卡里（同一个事实不画两遍）。
     */
    const val TOP_BAR_DP = 0f

    const val TOP_CARD_MIN_DP = 84f
    const val TOP_CARD_MAX_DP = 112f

    /** 合并卡的内边距（比功能卡更紧：它有三行小字，是首页最密的一块）。 */
    const val TOP_CARD_PADDING_DP = 8f

    /** 在听细条：整行可点（回「指令」页），所以按 44dp 的可点行做。 */
    const val LISTEN_ROW_DP = 44f

    const val SECTION_LABEL_MIN_DP = 16f
    const val SECTION_LABEL_MAX_DP = 20f

    /** 指令卡：卡内边距 20 + 标题行 40 + 输入行 48 + 间距 + 一行状态 18。 */
    const val COMMAND_CARD_MIN_DP = 134f
    const val COMMAND_CARD_MAX_DP = 158f

    /** 会议卡 / 回顾卡（并列）：卡内边距 20 + 标题行 40 + 一行状态 18 + 主按钮 48 + 间距。 */
    const val SIDE_CARD_MIN_DP = 134f
    const val SIDE_CARD_MAX_DP = 158f

    // ---- 卡内零件（界面与预算**同源**：改这里，两边一起变）----

    const val CARD_PADDING_DP = 10f
    const val CARD_CORNER_DP = 12f

    /** 卡片标题行（图标 + 标题 + 一行说明 + 右箭头；整行可点 = 进对应页）。 */
    const val HEADER_ROW_DP = 40f

    /** 指令卡里"打字框 + 按住说话 + 发送"那一行。 */
    const val COMPOSER_ROW_DP = 48f

    /** 卡片上"一行状态"的高度（默认字体）。 */
    const val STATUS_LINE_DP = 18f

    /** 卡内小块之间的缝。 */
    const val CARD_GAP_DP = 4f

    /** 卡上主按钮（开始录音 / 口述回顾 …）。 */
    const val PRIMARY_BUTTON_DP = 48f

    // ---- 触摸目标（无障碍底线）----

    /** 所有**主**按钮的底线（Material 的最小可点尺寸）。 */
    const val MIN_TOUCH_DP = 48f

    /** 整行可点的辅助行（在听细条）的底线。 */
    const val MIN_ROW_TOUCH_DP = 44f

    // ---- 字号（按可用高度收敛一点点；别小到看不清）----

    const val CARD_TITLE_MIN_SP = 13f
    const val CARD_TITLE_MAX_SP = 14f
    const val SECTION_LABEL_MIN_SP = 11f
    const val SECTION_LABEL_MAX_SP = 13f

    /**
     * 预算里预留的余量。
     *
     * 为什么要留：不同 ROM 的字体度量/行高略有差异，卡片是"至少这么高"，
     * 差几个 dp 就会让 360×640 从"刚好一屏"变成"多一点要滚"。
     */
    const val RESERVE_DP = 24f
}

/** 一个区块（预算里的一行）。 */
internal data class HomeSection(val name: String, val heightDp: Float)

/** 一个触摸目标：实际高度 vs 无障碍底线（`HomeLayoutTest` 逐条断言）。 */
internal data class HomeTouchTarget(val name: String, val heightDp: Float, val minDp: Float) {
    val ok: Boolean get() = heightDp + 0.01f >= minDp
}

/**
 * 一份**算好的**首页布局预算。
 *
 * 界面从这里取间距/内边距/字号/卡片最小高度；用例从这里断言"一屏放得下"与"触摸目标够大"。
 * 两边读的是**同一份数字**，所以不会出现"用例说放得下、真机要滚"。
 */
internal data class HomeLayoutPlan(
    val widthDp: Float,
    val heightDp: Float,
    val pageTopDp: Float,
    val pageBottomDp: Float,
    val gapDp: Float,
    val topCardDp: Float,
    val listenRowDp: Float,
    val sectionLabelDp: Float,
    val commandCardDp: Float,
    val sideCardDp: Float,
    val cardPaddingDp: Float,
    val cardTitleSp: Float,
    val sectionLabelSp: Float,
) {

    /** 从上到下的区块（**含顶栏那一行 0**：首页没有顶栏，所以它不占任何高度）。 */
    val sections: List<HomeSection>
        get() = listOf(
            HomeSection("顶栏", HomeLayoutSpec.TOP_BAR_DP),
            HomeSection("应用名/状态合并卡", topCardDp),
            HomeSection("在听细条", listenRowDp),
            HomeSection("主要功能标题", sectionLabelDp),
            HomeSection("指令卡", commandCardDp),
            HomeSection("会议卡/回顾卡并列", sideCardDp),
        )

    /** 内容总高 = 所有区块 + 块间缝 + 上下边距。 */
    val contentHeightDp: Float
        get() {
            var sum = pageTopDp + pageBottomDp + gapDp * HomeLayoutSpec.GAP_COUNT
            sections.forEach { sum += it.heightDp }
            return sum
        }

    /** 可用内容高（Scaffold 给首页的那块地方）。 */
    val availableHeightDp: Float get() = heightDp

    /** 一屏放得下之后还剩多少（≥ 0 = 不用滚；大屏上它表现为"垂直居中"的留白）。 */
    val spareHeightDp: Float get() = availableHeightDp - contentHeightDp

    /** **判据**：内容 ≤ 可用高度（这才叫"一屏放得下"，而不是"允许滚"）。 */
    val fits: Boolean get() = spareHeightDp >= -0.01f

    /** 底部导航总高（含中间那颗露在条外的部分）—— 它必须在，且不占内容高度。 */
    val bottomNavHeightDp: Float get() = EchoNavSpec.TOTAL_HEIGHT_DP

    /** 并列那两张卡各自的宽度（360dp 下要还能放下「开始录音」四个字）。 */
    val sideCardWidthDp: Float
        get() = (widthDp - 2 * HomeLayoutSpec.PAGE_H_PADDING_DP - gapDp) / 2f

    /** 指令卡里"打字框"的宽度：扣掉语音圆钮、发送按钮与三道 8dp 的缝。 */
    val composerFieldWidthDp: Float
        get() = widthDp - 2 * HomeLayoutSpec.PAGE_H_PADDING_DP - 2 * cardPaddingDp -
            HomeLayoutSpec.COMPOSER_ROW_DP - HomeLayoutSpec.PRIMARY_BUTTON_DP - 3 * 8f

    /** 首页上所有**可点**东西的高度（主按钮 48 / 细条 44 / 底部页签 56 & 64）。 */
    val touchTargets: List<HomeTouchTarget>
        get() = listOf(
            HomeTouchTarget("发送 / 按住说话", HomeLayoutSpec.COMPOSER_ROW_DP, HomeLayoutSpec.MIN_TOUCH_DP),
            HomeTouchTarget("开始录音 / 结束录音", HomeLayoutSpec.PRIMARY_BUTTON_DP, HomeLayoutSpec.MIN_TOUCH_DP),
            HomeTouchTarget("口述回顾 / 说完了 / 取消", HomeLayoutSpec.PRIMARY_BUTTON_DP, HomeLayoutSpec.MIN_TOUCH_DP),
            HomeTouchTarget("在听细条（点它回指令页）", listenRowDp, HomeLayoutSpec.MIN_ROW_TOUCH_DP),
            HomeTouchTarget("底部导航两侧页签", EchoNavSpec.SIDE_TAP_HEIGHT_DP, HomeLayoutSpec.MIN_TOUCH_DP),
            HomeTouchTarget("底部导航中间那颗", EchoNavSpec.CENTER_TOUCH_DP, HomeLayoutSpec.MIN_TOUCH_DP),
        )

    /** 没达标的目标（用例断言它是空的）。 */
    val undersizedTouchTargets: List<HomeTouchTarget> get() = touchTargets.filter { !it.ok }
}

/**
 * 屏高 → 首页可用内容高（纯函数，把"系统栏 + 底部导航"一次扣干净）。
 *
 * 为什么要在用例里也用它：这样 `360×640 一屏放得下` 那条用的是**真实手机尺寸**，
 * 而不是我自己挑的一个顺眼的数字。
 */
internal fun homeContentHeightDp(screenHeightDp: Float): Float =
    screenHeightDp - HomeLayoutSpec.STATUS_BAR_DP - HomeLayoutSpec.SYSTEM_NAV_BAR_DP -
        EchoNavSpec.TOTAL_HEIGHT_DP

/**
 * 给定**手机屏幕尺寸**（dp），算出首页布局预算 —— 判据用例就用这个入口：
 * 传 360×640 / 360×800 / 393×851，而不是自己算好的"可用高度"
 * （那样等于把要证明的东西当条件）。
 *
 * 界面那一侧走的是 [homeLayoutPlan]：它拿 `BoxWithConstraints` 量到的**真实**可用宽高，
 * 不依赖"状态栏 24 / 系统导航 48"这些假设值 —— 换一台 ROM 也不会算错。
 * 两个入口是同一份预算逻辑，只是喂进去的数字来源不同。
 */
internal fun homeLayoutPlanForScreen(screenWidthDp: Float, screenHeightDp: Float): HomeLayoutPlan =
    homeLayoutPlan(screenWidthDp, homeContentHeightDp(screenHeightDp))

/**
 * 给定**可用内容宽高**，算出首页的布局预算（纯函数）。
 *
 * 收敛方式：小屏用各区块的 min 档（把留白压到最小，保证 360×640 一屏放得下）；
 * 屏越高，间距/卡片高度按同一比例 `grow ∈ [0,1]` 往 max 档走 —— 一屏里的东西**等比例**松开，
 * 不会出现"某一块特别空、另一块挤成一团"。
 *
 * 为什么先用 [HomeLayoutSpec.RESERVE_DP] 把可用高度扣掉一截再算 grow：
 * 这样 360×640 落在 grow≈0 的档位，剩下的十几 dp 就是"字体度量略有差异"的缓冲，
 * 而不是被卡片吃干抹净、多一行字就得滚。
 */
internal fun homeLayoutPlan(widthDp: Float, heightDp: Float): HomeLayoutPlan {
    val spec = HomeLayoutSpec
    val minTotal = spec.PAGE_TOP_MIN_DP + spec.TOP_BAR_DP + spec.TOP_CARD_MIN_DP +
        spec.GAP_MIN_DP * spec.GAP_COUNT + spec.LISTEN_ROW_DP + spec.SECTION_LABEL_MIN_DP +
        spec.COMMAND_CARD_MIN_DP + spec.SIDE_CARD_MIN_DP + spec.PAGE_BOTTOM_MIN_DP
    val maxTotal = spec.PAGE_TOP_MAX_DP + spec.TOP_BAR_DP + spec.TOP_CARD_MAX_DP +
        spec.GAP_MAX_DP * spec.GAP_COUNT + spec.LISTEN_ROW_DP + spec.SECTION_LABEL_MAX_DP +
        spec.COMMAND_CARD_MAX_DP + spec.SIDE_CARD_MAX_DP + spec.PAGE_BOTTOM_MAX_DP
    val room = maxTotal - minTotal
    val grow = if (room <= 0f) {
        0f
    } else {
        ((heightDp - spec.RESERVE_DP - minTotal) / room).coerceIn(0f, 1f)
    }

    fun at(min: Float, max: Float): Float = min + (max - min) * grow

    return HomeLayoutPlan(
        widthDp = widthDp,
        heightDp = heightDp,
        pageTopDp = at(spec.PAGE_TOP_MIN_DP, spec.PAGE_TOP_MAX_DP),
        pageBottomDp = at(spec.PAGE_BOTTOM_MIN_DP, spec.PAGE_BOTTOM_MAX_DP),
        gapDp = at(spec.GAP_MIN_DP, spec.GAP_MAX_DP),
        topCardDp = at(spec.TOP_CARD_MIN_DP, spec.TOP_CARD_MAX_DP),
        listenRowDp = spec.LISTEN_ROW_DP,
        sectionLabelDp = at(spec.SECTION_LABEL_MIN_DP, spec.SECTION_LABEL_MAX_DP),
        commandCardDp = at(spec.COMMAND_CARD_MIN_DP, spec.COMMAND_CARD_MAX_DP),
        sideCardDp = at(spec.SIDE_CARD_MIN_DP, spec.SIDE_CARD_MAX_DP),
        cardPaddingDp = spec.CARD_PADDING_DP,
        cardTitleSp = at(spec.CARD_TITLE_MIN_SP, spec.CARD_TITLE_MAX_SP),
        sectionLabelSp = at(spec.SECTION_LABEL_MIN_SP, spec.SECTION_LABEL_MAX_SP),
    )
}
