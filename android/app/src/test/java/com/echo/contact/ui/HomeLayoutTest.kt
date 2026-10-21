package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 首页重设计的判据（用户 2026-10-18 傍晚的原话）：
 *
 * > *"首页的样式要按照 app 来设计，不要出现滚动条，要合理利用空间，包括现在顶部那个空条全面设计"*
 *
 * 这三件事里有两件能被 JVM 门禁直接钉住，因为它们本来就是**数字**：
 *
 *  ① **一屏放得下**：`homeLayoutPlanForScreen(...)` 的各区块高度之和 ≤ 可用高度 —— 而且用的是
 *     **真实机型尺寸**（360×640 = 最小的常见手机、360×800、393×851），不是我自己挑的数；
 *  ② **顶部不再有空条**：`homeTopBar(首页)` 必须是 `None`（整块不存在），
 *     其余页必须是**非空**的 `Title`（不是"标题恰好有字"，是断言出来的）。
 *
 * 另外钉住"缩小的是留白、不是按钮"（主按钮/按住说话 48dp、在听细条 44dp）、
 * 三张功能卡与底部 5 页导航都还在，以及"太矮的屏预算会**如实**说放不下"
 * —— 那时界面才需要滚动兜底（滚动条不画，内容不截断）。
 */
class HomeLayoutTest {

    /** 三个**真实**尺寸：360×640（小屏底线）、360×800、393×851。 */
    private val phones = listOf(
        Triple("360×640", 360f, 640f),
        Triple("360×800", 360f, 800f),
        Triple("393×851", 393f, 851f),
    )

    @Test
    fun `360x640 一屏放得下（小屏底线）`() {
        val content = homeContentHeightDp(640f)
        // 屏高 640 − 状态栏 24 − 系统导航 48 − 底部导航 98（含凸起 38）= 470dp
        assertEquals("可用内容高算错了", 470f, content, 0.01f)
        val plan = homeLayoutPlan(360f, content)
        assertTrue("360×640 放不下：内容 ${plan.contentHeightDp} > 可用 ${plan.availableHeightDp}", plan.fits)
        assertTrue("内容总高必须 ≤ 可用高度", plan.contentHeightDp <= plan.availableHeightDp + 0.01f)
        assertTrue("预算写错了：连最小档都超过可用高度", plan.spareHeightDp >= 0f)
    }

    @Test
    fun `360x640 各块的 dp 明细（写死期望值，防止悄悄膨胀）`() {
        val plan = homeLayoutPlanForScreen(360f, 640f)
        // 顶栏 = 0：用户点名的"空条"整块不存在（不是留一条空白的 64dp）
        assertEquals("首页顶栏必须不占高度", 0f, plan.sections.first { it.name == "顶栏" }.heightDp, 0.01f)
        assertEquals("合并卡", 84f, plan.topCardDp, 0.01f)
        assertEquals("在听细条", 44f, plan.listenRowDp, 0.01f)
        assertEquals("「主要功能」标题", 16f, plan.sectionLabelDp, 0.01f)
        assertEquals("指令卡", 134f, plan.commandCardDp, 0.01f)
        assertEquals("会议/回顾卡", 134f, plan.sideCardDp, 0.01f)
        // 合计 = 6 + 84 + 6*4 + 44 + 16 + 134 + 134 + 6 = 448 ≤ 470
        assertEquals("合计高度", 448f, plan.contentHeightDp, 0.01f)
        assertEquals("剩多少（这几十 dp 是字体度量差异的缓冲）", 22f, plan.spareHeightDp, 0.01f)
    }

    @Test
    fun `360x800 与 393x851 也一屏放得下`() {
        for ((name, w, h) in phones) {
            val plan = homeLayoutPlanForScreen(w, h)
            assertTrue("$name 放不下：内容 ${plan.contentHeightDp} > 可用 ${plan.availableHeightDp}", plan.fits)
            assertTrue("$name 的间距被压成 0 了（说明预算算错了）", plan.gapDp > 0f)
        }
    }

    @Test
    fun `预算的每一块都算进合计里 没有漏算`() {
        for ((name, w, h) in phones) {
            val plan = homeLayoutPlanForScreen(w, h)
            val sections = plan.sections.sumOf { it.heightDp.toDouble() }.toFloat()
            val want = plan.pageTopDp + plan.pageBottomDp + plan.gapDp * HomeLayoutSpec.GAP_COUNT + sections
            assertEquals("$name 的合计与各块对不上", want, plan.contentHeightDp, 0.01f)
            assertTrue("$name 少了某个区块", plan.sections.count { it.heightDp > 0f } >= 5)
        }
    }

    @Test
    fun `屏更高时间距与卡片按比例松开 而不是硬撑一大片空白`() {
        val short = homeLayoutPlanForScreen(360f, 640f)
        val tall = homeLayoutPlanForScreen(360f, 800f)
        assertTrue("大屏的间距该更大：${short.gapDp} vs ${tall.gapDp}", tall.gapDp > short.gapDp)
        assertTrue("大屏的卡片该更高", tall.commandCardDp > short.commandCardDp)
        assertTrue("大屏的卡片也不许无限长", tall.commandCardDp <= HomeLayoutSpec.COMMAND_CARD_MAX_DP)
        assertTrue("大屏的剩余空间不该超过一屏的三成（否则就是没利用空间）",
            tall.spareHeightDp <= tall.availableHeightDp * 0.3f)
    }

    @Test
    fun `关键元素的高度底线 48 与 44`() {
        for ((name, w, h) in phones) {
            val plan = homeLayoutPlanForScreen(w, h)
            assertTrue(
                "$name 有触摸目标小于底线：${plan.undersizedTouchTargets.map { it.name + "=" + it.heightDp }}",
                plan.undersizedTouchTargets.isEmpty(),
            )
            // 主按钮与"按住说话"必须是 48（Material 的最小可点尺寸），细条至少 44
            assertEquals(HomeLayoutSpec.MIN_TOUCH_DP, plan.touchTargets.first { it.name.startsWith("发送") }.heightDp, 0.01f)
            assertTrue(plan.touchTargets.any { it.name.startsWith("开始录音") && it.heightDp >= 48f })
            assertTrue(plan.touchTargets.any { it.name.startsWith("在听") && it.heightDp >= 44f })
        }
    }

    @Test
    fun `360 宽下 并列两卡与打字框都还够宽`() {
        val plan = homeLayoutPlanForScreen(360f, 640f)
        // 会议/回顾各半宽：360 − 14*2 − 6 = 163dp，扣掉卡内边距还有 143dp 放按钮
        assertTrue("并列两卡太窄：${plan.sideCardWidthDp}", plan.sideCardWidthDp >= 150f)
        // 指令卡：去掉语音圆钮(48) + 发送按钮(48) + 三道 8dp 缝之后仍要能打一行字
        assertTrue("打字框太窄：${plan.composerFieldWidthDp}", plan.composerFieldWidthDp >= 120f)
    }

    @Test
    fun `顶栏不再为空：首页整块不存在 其余页必须有页名`() {
        // ① 首页：没有顶栏（整块不存在）—— 不是"画一条空的"
        assertEquals(HomeTopBar.None, homeTopBar(EchoTab.ASSISTANT))
        assertTrue("首页顶栏高度必须是 0", homeLayoutPlan(360f, 470f).sections.first { it.name == "顶栏" }.heightDp == 0f)

        // ② 其余四页：必须有**非空**页名（空条的定义就是"有栏没字"）
        ECHO_TABS.filter { it != START_TAB }.forEach { tab ->
            val bar = homeTopBar(tab)
            assertTrue("${tab.title} 页的顶栏不许是空的", bar is HomeTopBar.Title)
            assertTrue("${tab.title} 页的顶栏标题不许是空白", (bar as HomeTopBar.Title).text.isNotBlank())
            // 页名里不许带应用名（应用名只在合并卡那一处）
            assertFalse("${tab.title} 页的顶栏又画应用名了", bar.text.contains("ECHO"))
        }

        // ③ 源码级：外壳只在有标题时才画 TopAppBar（否则又是一条空条）
        val shell = moduleText("src/main/java/com/echo/contact/ui/HomeScreen.kt")
        assertTrue("外壳没有按 homeTopBar 决定画不画顶栏", shell.contains("homeTopBar(currentTab)"))
        assertTrue("首页那支必须真的不画（HomeTopBar.None -> Unit）", shell.contains("HomeTopBar.None -> Unit"))
    }

    @Test
    fun `三张功能卡与底部五页导航都在`() {
        val cards = homeCards(
            micRecording = false,
            reviewActive = false,
            reviewRecording = false,
            reviewStarting = false,
        )
        assertEquals("三张功能卡", 3, cards.size)
        assertEquals(
            listOf(HomeCardKind.COMMAND, HomeCardKind.MEETING, HomeCardKind.REVIEW),
            cards.map { it.kind },
        )
        // 每张卡都有能直接干活的按钮（缩小 ≠ 砍功能；判据的另一半在 HomeCardsTest）
        cards.forEach { card ->
            assertTrue("${card.title} 卡没有按钮了", card.buttons.isNotEmpty())
            assertTrue("${card.title} 卡的按钮不许切页", card.buttons.none { it.action.switchesTab })
        }

        // 底部导航：5 页都在，而且它的高度是从内容里扣掉的（不会盖住内容）
        assertEquals("底部导航 5 页", 5, ECHO_TABS.size)
        val plan = homeLayoutPlanForScreen(360f, 640f)
        assertEquals("底部导航高度（含中间凸起）", EchoNavSpec.TOTAL_HEIGHT_DP, plan.bottomNavHeightDp, 0.01f)
        assertTrue("360 宽下 5 项要放得下", navFitsWidth(360f))

        // 源码级：这三种卡都真的挂在首页上（不是只剩数据）
        val home = moduleText("src/main/java/com/echo/contact/ui/SuperAssistantTab.kt")
        assertTrue("首页没有那张合并卡了", home.contains("HomeTopCard(vm)"))
        assertTrue("首页没有指令卡", home.contains("CommandCardBody("))
        assertTrue("首页没有会议卡", home.contains("MeetingCardBody("))
        assertTrue("首页没有回顾卡", home.contains("ReviewCardBody("))
        assertTrue("首页没有在听细条", home.contains("ListenRow("))
        // 滚动只是兜底：不许出现可见滚动条（Compose 的 verticalScroll 本身不画）
        assertFalse("有人加了可见滚动条", home.contains("VerticalScrollbar"))
    }

    @Test
    fun `太矮的屏预算会如实说放不下（那时才滚动兜底）`() {
        // 360×480：不是目标机型，但预算不许在这儿撒谎说"放得下"
        val plan = homeLayoutPlanForScreen(360f, 480f)
        assertFalse("480dp 高的屏不可能一屏放下：${plan.contentHeightDp}", plan.fits)
        assertTrue("放不下时要能说出差多少", plan.spareHeightDp < 0f)
        // 而 640 那条必须还是放得下（底线不许被这条带歪）
        assertTrue(homeLayoutPlanForScreen(360f, 640f).fits)
        assertNotEquals("预算不该是常量：不同高度要给出不同结果",
            homeLayoutPlanForScreen(360f, 640f).gapDp,
            homeLayoutPlanForScreen(360f, 800f).gapDp)
    }
}
