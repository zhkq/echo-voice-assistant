package com.echo.contact.ui

import com.echo.contact.data.dto.MeetingItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 通知 extra → 「打开哪场会议 / 落到列表 + 一句提示」的**决策**（需求判据 ②）。
 *
 * 为什么这段逻辑值得单测：它在真机上要复现的成本很高 ——
 * "会议被删了再点那条旧通知"这种场景，人得先删会议、再等一条几小时前的通知点下去。
 * 而这里的三条分支（无目标 / 找到 / 找不到）一旦写错，表现都是"点了没反应"，
 * 是最难被发现的体验问题。做成纯函数就一次钉死。
 */
class MeetingOpenPlanTest {

    private fun meeting(id: Int, name: String) = MeetingItem(id = id, name = name)

    @Test
    fun `没有目标就回首页而不是硬开一场会议`() {
        // 普通通知（指令有结果、回顾好了）本来就没有会议；点开只该到首页。
        // 而且**不该弹一句提示** —— 什么都没出错，何必解释。
        val plan = MeetingOpenPlan.decide(null, listOf(meeting(1, "A")))
        assertTrue("没有目标时必须是 Fallback：$plan", plan is MeetingOpenPlan.Fallback)
        assertNull("没有目标时不该有提示（这不是错误）", (plan as MeetingOpenPlan.Fallback).message)
    }

    @Test
    fun `空白目标等于没有目标`() {
        // Intent 里键在、值是空串也会发生（手工 putExtra("")）。
        // 当成"有目标"的话，用户点通知会看到一句莫名其妙的"没找到那场会议（）"。
        for (blank in listOf("", "   ", "\t")) {
            val plan = MeetingOpenPlan.decide(blank, listOf(meeting(1, "A")))
            assertTrue("空白目标要当成没目标：[$blank] → $plan", plan is MeetingOpenPlan.Fallback)
            assertNull((plan as MeetingOpenPlan.Fallback).message)
        }
    }

    @Test
    fun `能找到就打开那一场`() {
        val plan = MeetingOpenPlan.decide(
            "2026-10-16_09-00-00",
            listOf(meeting(7, "别的会"), meeting(8, "2026-10-16_09-00-00")),
        )
        assertTrue("该打开第二场：$plan", plan is MeetingOpenPlan.Open)
        assertEquals(8, (plan as MeetingOpenPlan.Open).id)
        assertEquals("2026-10-16_09-00-00", plan.name)
    }

    @Test
    fun `名字两侧的空白不该让人找不到会议`() {
        // extra 是人/事件流给的字符串，前后带空格完全可能；因为一个空格就"找不到会议"
        // 属于典型的"坏在看不见的地方"。
        val plan = MeetingOpenPlan.decide("  2026-10-16_09-00-00  ", listOf(meeting(8, "2026-10-16_09-00-00")))
        assertTrue(plan is MeetingOpenPlan.Open)
        assertEquals(8, (plan as MeetingOpenPlan.Open).id)
    }

    /** 需求判据 ③：会议被删了要**如实说**，不能静默什么都不发生。 */
    @Test
    fun `找不到那场会议要落回列表并说清楚`() {
        val plan = MeetingOpenPlan.decide("已经被删掉的那场", listOf(meeting(1, "还在的会")))
        assertTrue("找不到时必须是 Fallback（落回列表）：$plan", plan is MeetingOpenPlan.Fallback)
        val message = (plan as MeetingOpenPlan.Fallback).message
        assertTrue("必须有一句提示，不能静默：$message", !message.isNullOrBlank())
        assertTrue("提示要说清「没找到那场会议」：$message", message!!.contains("没找到"))
        assertTrue("提示里要带上找的是哪一场（用户才知道自己点的是什么）：$message",
            message.contains("已经被删掉的那场"))
    }

    @Test
    fun `列表为空时也是找不到而不是崩`() {
        // 第一次进 App、会议列表还没拉到就点通知：这是真实会发生的一刻。
        val plan = MeetingOpenPlan.decide("某场会", emptyList())
        assertTrue(plan is MeetingOpenPlan.Fallback)
        assertTrue((plan as MeetingOpenPlan.Fallback).message!!.contains("没找到"))
    }

    @Test
    fun `名字完全一样才算那一场`() {
        // 会议名是时间戳，允许前缀相近（同日不同时刻）。用 contains/startsWith 去认
        // 会认错会议 —— 打开的是别人的会议比"找不到"更糟。
        assertNull(
            "只有完全相等才算：前缀相近不该被认成同一场",
            MeetingOpenPlan.matchId(listOf(meeting(1, "2026-10-16_09-00")), "2026-10-16_09-00-00"),
        )
        assertNull(
            "子串关系也不该算",
            MeetingOpenPlan.matchId(listOf(meeting(1, "2026-10-16")), "2026-10-16_09-00-00"),
        )
        assertEquals(2, MeetingOpenPlan.matchId(listOf(meeting(1, "A"), meeting(2, "B")), "B"))
    }

    /**
     * 通知里的会议名可能是**列表里的旧名字**（改过名）或不同大小写。
     * 这里如实记录当前口径：**只认完全相等**，认不出就按"找不到"提示 ——
     * 宁可说一句"没找到"，也不要打开一场看起来对、其实不是那场会议。
     */
    @Test
    fun `大小写不同按找不到处理而不是猜`() {
        val plan = MeetingOpenPlan.decide("abc", listOf(meeting(1, "ABC")))
        assertTrue(plan is MeetingOpenPlan.Fallback)
    }
}
