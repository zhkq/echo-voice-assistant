package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回顾页**每个入口点下去到底有没有做事**的判据（2026-10-18 复查"功能都正常不"）。
 *
 * 为什么要这些用例：Compose 的点击在 JVM 门禁里断言不了，但"点下去会发生什么"在这里
 * 全都是**纯数据**。这一轮修掉的三类问题各自对应一条：
 *
 *  1. **今日回顾卡只是重拉状态**（屏幕上什么都不变 = 空壳按钮）→ [reviewTodayTap]；
 *  2. **读不到摘要被说成"还没有回顾/播报"**（假空态）→ `reviewFeatureTiles(summaryLoaded=false)`；
 *  3. **"文件在"被说成"这一轮已写入"**（假成功）→ [reviewDaySubtitle] / [reviewTurnRegistryNote]。
 */
class ReviewEntryActionsTest {

    @Test
    fun `今日回顾有轮次时点下去打开当天详情`() {
        val tap = reviewTodayTap(today = "2026-10-18", summaryLoaded = true, todayTurns = 3)
        assertTrue("有轮次时必须真的进详情页，不能只重拉状态", tap is ReviewTodayTap.OpenDay)
        assertEquals("2026-10-18", (tap as ReviewTodayTap.OpenDay).date)
    }

    @Test
    fun `今日回顾没有轮次时如实说一句并告诉用户去哪儿开始`() {
        val tap = reviewTodayTap(today = "2026-10-18", summaryLoaded = true, todayTurns = 0)
        assertTrue(tap is ReviewTodayTap.Note)
        val msg = (tap as ReviewTodayTap.Note).message
        assertTrue("要告诉用户下一步点什么：$msg", msg.contains("开始回顾"))
        assertTrue("别只说'没有'：$msg", msg.length > 8)
    }

    @Test
    fun `摘要没读到时不许说今天还没回顾`() {
        val tap = reviewTodayTap(today = "", summaryLoaded = false, todayTurns = 0)
        assertTrue(tap is ReviewTodayTap.Note)
        val msg = (tap as ReviewTodayTap.Note).message
        assertTrue("读不到 ≠ 没有：$msg", msg.contains("没读到"))
        assertFalse("读不到时别说成'今天还没有回顾'", msg.contains("今天还没有回顾"))

        val tiles = reviewFeatureTiles(
            running = false, enabled = true, ready = true, todayTurns = 0,
            hasBrief = false, speaking = false, summaryLoaded = false,
        )
        val today = tiles.first { it.feature == ReviewFeature.TODAY }
        assertEquals("还没读到今天的状态", today.subtitle)
        val speak = tiles.first { it.feature == ReviewFeature.SPEAK }
        assertFalse("没读到播报就不能让用户点", speak.enabled)
        assertTrue("要如实说没读到，而不是'还没有可念的播报'：${speak.subtitle}",
            speak.subtitle.contains("没读到"))
    }

    @Test
    fun `失败轮数要出现在今日卡上`() {
        val tiles = reviewFeatureTiles(
            running = false, enabled = true, ready = true, todayTurns = 2,
            hasBrief = true, speaking = false, todayFailed = 2,
        )
        assertEquals(
            "今天已回顾 2 轮（2 轮失败）",
            tiles.first { it.feature == ReviewFeature.TODAY }.subtitle,
        )
        // 真实数据里 2026-10-14 就是"2 轮全失败"：不分开说会把失败说成成绩
        val ok = reviewFeatureTiles(
            running = false, enabled = true, ready = true, todayTurns = 2,
            hasBrief = true, speaking = false, todayFailed = 0,
        ).first { it.feature == ReviewFeature.TODAY }.subtitle
        assertEquals("今天已回顾 2 轮", ok)
    }

    @Test
    fun `详情页副标题不把文件在说成这一轮已写入`() {
        val exists = reviewDaySubtitle(count = 3, vaultNoteExists = true)
        assertTrue("要说的是日志文件在：$exists", exists.contains("已存在"))
        assertFalse("文件在 ≠ 这一轮写进去了：$exists", exists.contains("已写入"))
        assertTrue(exists.startsWith("3 轮"))

        val missing = reviewDaySubtitle(count = 0, vaultNoteExists = false)
        assertTrue("日志没有也要说：$missing", missing.contains("还没生成"))
    }

    @Test
    fun `某一轮没登记要说清楚而老记录不说话`() {
        val notRegistered = reviewTurnRegistryNote(false)
        assertTrue("没登记必须说出来：$notRegistered", notRegistered.contains("工作日志"))
        assertTrue("要给可能的原因：$notRegistered", notRegistered.contains("技能"))
        // null = 老记录（那时还没有核实）；true = 检测到更新。两种都不该冒出一句话
        assertEquals("", reviewTurnRegistryNote(null))
        assertEquals("", reviewTurnRegistryNote(true))
    }

    @Test
    fun `开不了麦与空的一天都有可操作的文案`() {
        assertTrue(REVIEW_MIC_BLOCKED.contains("麦克风"))
        assertTrue("要说清这次没开始录：$REVIEW_MIC_BLOCKED", REVIEW_MIC_BLOCKED.contains("没开始"))
        assertTrue("空的一天要告诉下一步：$REVIEW_DAY_EMPTY", REVIEW_DAY_EMPTY.contains("开始回顾"))
        // 历史空态与它们不是一回事（不能混用一句话）
        assertFalse(REVIEW_HISTORY_EMPTY == REVIEW_DAY_EMPTY)
    }
}
