package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 首页三张**功能卡**的判据（用户 2026-10-18 中午："下方的主要功能，应该用卡片形式，
 * 里面要有功能按钮，不用跳转到对应功能页再开始使用功能"）。
 *
 * 抽成纯函数/纯数据来测的理由与 `ReviewTabSectionsTest` 一样：Compose 在 JVM 门禁里点不了，
 * 但"按钮到底直接干活还是切页"这件事本身就是**数据**（动作 + 会不会切页），
 * 所以能在 JVM 里逐条钉住 —— 不用等到真机上一个个点。
 *
 * 本轮钉住的四件事：
 *  ① 三张卡都在（指令 / 会议 / 回顾），每张都有能直接干活的按钮；
 *  ② 那些按钮的**动作是"直接执行"**（发送 / 开始录音 / 结束录音 / 口述），**不是**切页；
 *  ③ 点了按钮**不改变当前页签**（状态级断言：`tabAfterClick`）；
 *  ④ 录音态时会议卡显示「结束录音」+ 本次时长（读 `MicState` 那两个现成字段）。
 */
class HomeCardsTest {

    @Test
    fun `三张功能卡齐全且顺序是用户点名的`() {
        val cards = homeCards(micRecording = false, reviewDictating = false)
        assertEquals(3, cards.size)
        assertEquals(listOf("指令", "会议", "回顾"), cards.map { it.title })
        assertEquals(
            listOf(HomeCardKind.COMMAND, HomeCardKind.MEETING, HomeCardKind.REVIEW),
            cards.map { it.kind },
        )
        cards.forEach { card ->
            assertTrue("${card.title} 卡必须有能直接干活的按钮", card.buttons.isNotEmpty())
            assertTrue("${card.title} 卡少了那句说明", card.subtitle.isNotBlank())
            assertTrue("${card.title} 卡少了「去看」链接", card.linkLabel.isNotBlank())
            assertTrue("${card.title} 卡指向一个不存在的页：${card.tab}", card.tab in ECHO_TABS)
            assertEquals("卡片类型与页签要对得上", card.kind.title, card.title)
        }
    }

    @Test
    fun `每张卡的按钮对应的动作都是直接执行而不是切页`() {
        // 闲时与"正在录"两种状态都过一遍（录音态会换按钮，不能只测一种）
        val cards = homeCards(micRecording = false, reviewDictating = false) +
            homeCards(micRecording = true, reviewDictating = true)

        for (card in cards) {
            for (button in card.buttons) {
                assertFalse(
                    "${card.title} 卡的「${button.label}」会切页 —— 用户明确说" +
                        "不要「跳转到对应功能页再开始使用功能」",
                    button.action.switchesTab,
                )
                assertTrue(
                    "${card.title} 卡的「${button.label}」必须是直接动作（HomeEffect.Direct）",
                    effectOf(card, button) is HomeEffect.Direct,
                )
            }
        }
    }

    @Test
    fun `三张卡的动作各自对上既有入口（不许新造）`() {
        fun primary(recording: Boolean, dictating: Boolean, kind: HomeCardKind): HomeAction =
            homeCards(recording, dictating).first { it.kind == kind }.buttons.first().action

        // 指令卡 = MainViewModel.sendCommand（「指令」页那颗发送键同一个方法）
        assertEquals(HomeAction.SEND_COMMAND, primary(false, false, HomeCardKind.COMMAND))
        // 会议卡 = MainViewModel.startMeeting → MeetingMicService.start（唯一那条手机麦录音路）
        assertEquals(HomeAction.START_MEETING, primary(false, false, HomeCardKind.MEETING))
        // 录音中就换成一个位置上的「结束录音」= MeetingMicService.stop（既有收尾路）
        assertEquals(HomeAction.STOP_MEETING, primary(true, false, HomeCardKind.MEETING))
        // 回顾卡 = MainViewModel.startRecording（回顾页「车里口述」那同一个入口）
        assertEquals(HomeAction.DICTATE_REVIEW, primary(false, false, HomeCardKind.REVIEW))
    }

    @Test
    fun `点按钮不改变当前页签（状态级）`() {
        // 对**每一个**页签都试一遍，而不是只看"首页"：卡片在首页，但这条约束不该依赖这一点
        ECHO_TABS.forEachIndexed { current, tab ->
            for (card in homeCards(micRecording = false, reviewDictating = false)) {
                for (button in card.buttons) {
                    assertEquals(
                        "在第 ${current + 1} 页（${tab.title}）点了「${card.title}」卡的" +
                            "「${button.label}」，页签不该变",
                        current,
                        tabAfterClick(current, effectOf(card, button)),
                    )
                }
            }
        }
    }

    @Test
    fun `只有右下角那个小链接会切页且切到自己那一页`() {
        for (card in homeCards(micRecording = false, reviewDictating = false)) {
            val link = HomeCardButton(card.linkLabel, HomeAction.OPEN_TAB)
            val effect = effectOf(card, link)
            assertTrue("${card.title} 的小链接必须是切页动作", effect is HomeEffect.SwitchTab)
            assertEquals(
                "${card.title} 的小链接该切到它自己的页",
                card.tab.ordinal,
                tabAfterClick(EchoTab.ASSISTANT.ordinal, effect),
            )
        }
    }

    @Test
    fun `录音态时会议卡变成结束录音并显示本次时长`() {
        val idle = homeCards(micRecording = false, reviewDictating = false)
            .first { it.kind == HomeCardKind.MEETING }
        assertEquals(listOf("开始录音"), idle.buttons.map { it.label })
        assertEquals(HomeAction.START_MEETING, idle.buttons.single().action)

        val recording = homeCards(micRecording = true, reviewDictating = false)
            .first { it.kind == HomeCardKind.MEETING }
        assertEquals("同一个位置换文案", listOf("结束录音"), recording.buttons.map { it.label })
        assertEquals(HomeAction.STOP_MEETING, recording.buttons.single().action)

        // 时长读的是服务自己算的那个（`MicState.elapsedSeconds`），界面不另算一遍
        assertEquals("录音中 · 已录 03:12", meetingCardLine(recording = true, seconds = 192.4, paused = false))
        assertEquals(
            "静音区间照录（内容全零），但要说清在静音",
            "录音中（已静音）· 已录 03:12",
            meetingCardLine(recording = true, seconds = 192.4, paused = true),
        )
        assertEquals("没在录音就不显示时长", "没在录音", meetingCardLine(false, 0.0, false))
        assertFalse("没在录音时不该提时长", meetingCardLine(false, 192.0, false).contains("已录"))
    }

    @Test
    fun `时长排版 分秒 与 时分秒`() {
        assertEquals("00:00", elapsedClock(0.0))
        assertEquals("00:12", elapsedClock(12.9))
        assertEquals("03:12", elapsedClock(192.0))
        assertEquals("59:59", elapsedClock(3599.0))
        assertEquals("1:00:00", elapsedClock(3600.0))
        assertEquals("1:02:05", elapsedClock(3725.0))
        // 负数（不该出现，但别让它画出 "-1:-1"）
        assertEquals("00:00", elapsedClock(-5.0))
    }

    @Test
    fun `回顾卡口述中显示说完了与取消且都是直接动作`() {
        val idle = homeCards(micRecording = false, reviewDictating = false)
            .first { it.kind == HomeCardKind.REVIEW }
        assertEquals(listOf("口述回顾"), idle.buttons.map { it.label })

        val dictating = homeCards(micRecording = false, reviewDictating = true)
            .first { it.kind == HomeCardKind.REVIEW }
        assertEquals(listOf("说完了", "取消"), dictating.buttons.map { it.label })
        assertEquals(
            listOf(HomeAction.SUBMIT_REVIEW, HomeAction.CANCEL_REVIEW),
            dictating.buttons.map { it.action },
        )
        for (button in dictating.buttons) {
            assertFalse("口述中的按钮也不许切页：${button.label}", button.action.switchesTab)
        }
    }

    @Test
    fun `卡片上那条回复短的原样 长的截断并给去看详情`() {
        val short = replyPreview("好的，已经记下了。")
        assertEquals("好的，已经记下了。", short.text)
        assertFalse("短回复不需要「去看详情」", short.truncated)

        val long = replyPreview("这是一段很长的回复。".repeat(40))
        assertTrue("长回复必须截断（卡片不是详情页）", long.truncated)
        assertTrue("截断要看得出来：${long.text}", long.text.endsWith("…"))
        assertTrue("截断后的长度要收敛在限额附近（含省略号）",
            long.text.length <= HOME_REPLY_PREVIEW_CHARS + 1)

        assertEquals("空回复就是空的", "", replyPreview("   ").text)
        assertFalse(replyPreview("").truncated)

        // 边界：正好等于限额 → 不截断（不该多出一个省略号）
        val exact = "字".repeat(HOME_REPLY_PREVIEW_CHARS)
        assertEquals(exact, replyPreview(exact).text)
        assertFalse(replyPreview(exact).truncated)
    }

    @Test
    fun `首页顶栏不再画应用名与配对地址（只有顶部那张卡画）`() {
        // ① 首页顶栏留空：应用名 + 连接信息只出现在那张合并卡里
        assertEquals("首页顶栏必须留空（否则应用名又出现两处）", "", homeTopBarTitle(EchoTab.ASSISTANT))
        // 别的页照旧显示页名，但页名里不含应用名
        ECHO_TABS.forEach { tab ->
            assertFalse(
                "页名不该带应用名：${tab.title}",
                homeTopBarTitle(tab).contains("ECHO"),
            )
        }
        assertEquals("指令", homeTopBarTitle(EchoTab.COMMANDS))
        assertEquals("设置", homeTopBarTitle(EchoTab.SETTINGS))

        // ② 源码级：外壳（顶栏所在文件）既不画应用名，也不再画配对地址
        val shell = moduleText("src/main/java/com/echo/contact/ui/HomeScreen.kt")
        assertFalse("外壳还在画应用名（应用名只该在 HomeTopCard 一处）", shell.contains("APP_TITLE"))
        assertFalse("外壳还在画配对地址（旧的独立顶部行没删干净）", shell.contains("baseUrl"))
        assertFalse("外壳还画着旧名字", shell.contains("触点"))

        // ③ 三张卡自身也不画应用名（它们只是卡，不带标题栏的应用名）
        val cards = moduleText("src/main/java/com/echo/contact/ui/SuperAssistantTab.kt")
        assertFalse("三张卡那个文件还在画应用名", cards.contains("APP_TITLE"))
    }

    @Test
    fun `卡片标题仍可进对应页（卡片是快捷方式 不是替代）`() {
        // 点卡片标题 = 切到它对应的页签（底部 5 页导航照旧有效）
        for (card in homeCards(false, false)) {
            assertEquals(card.tab.ordinal, tabAfterClick(EchoTab.SETTINGS.ordinal, HomeEffect.SwitchTab(card.tab)))
        }
        // 而每个页签都还在（一个都没少）
        assertEquals(5, ECHO_TABS.size)
    }
}
