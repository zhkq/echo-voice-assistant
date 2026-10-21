package com.echo.contact.ui

import com.echo.contact.data.dto.ReviewDay
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回顾页"**顶部功能区 + 下面历史列表**"的判据（用户 2026-10-18）。
 *
 * 为什么抽纯函数：Compose 的界面在 JVM 门禁里没法断言，但这一页的骨架是**数据**：
 * 顶部有哪几张卡、每张卡什么状态、历史按什么顺序、空历史说什么。全都能在这里钉住 ——
 * 也就钉住了"不再是一整页纯列表"。
 */
class ReviewTabSectionsTest {

    @Test
    fun `顶部功能区有三张现有能力的卡`() {
        val tiles = reviewFeatureTiles(
            running = false, enabled = true, ready = true, todayTurns = 0,
            hasBrief = false, speaking = false,
        )
        assertEquals(3, tiles.size)
        assertEquals(
            listOf("今日回顾", "车里口述", "播报"),
            tiles.map { it.feature.title },
        )
        // 车里口述永远是手机自己能做的事（不等于"主机麦克风口述"那条）
        assertTrue(tiles.first { it.feature == ReviewFeature.DICTATE }.enabled)
        // 没有可念的播报：置灰 + 说明（而不是藏起来）
        val speak = tiles.first { it.feature == ReviewFeature.SPEAK }
        assertFalse("没有播报时不该可点", speak.enabled)
        assertEquals("还没有可念的播报", speak.subtitle)
    }

    @Test
    fun `今日回顾的状态如实`() {
        fun todayLabel(running: Boolean, enabled: Boolean, ready: Boolean, turns: Int): String =
            reviewFeatureTiles(running, enabled, ready, turns, false, false)
                .first { it.feature == ReviewFeature.TODAY }.subtitle

        assertEquals("进行中", todayLabel(running = true, enabled = true, ready = true, turns = 3))
        assertEquals("未启用", todayLabel(running = false, enabled = false, ready = false, turns = 0))
        assertEquals("未就绪", todayLabel(running = false, enabled = true, ready = false, turns = 0))
        assertEquals("今天已回顾 2 轮", todayLabel(false, true, true, 2))
        assertEquals("今天还没回顾", todayLabel(false, true, true, 0))
    }

    @Test
    fun `有播报时播报卡可点，正在念时置灰`() {
        val ready = reviewFeatureTiles(
            running = false, enabled = true, ready = true, todayTurns = 1,
            hasBrief = true, speaking = false,
        ).first { it.feature == ReviewFeature.SPEAK }
        assertTrue(ready.enabled)
        assertEquals("念今天 / 最近一次的播报", ready.subtitle)

        val speaking = reviewFeatureTiles(
            running = false, enabled = true, ready = true, todayTurns = 1,
            hasBrief = true, speaking = true,
        ).first { it.feature == ReviewFeature.SPEAK }
        assertFalse("正在念的时候不该再让人点一次", speaking.enabled)
    }

    @Test
    fun `历史按日期倒序`() {
        val days = listOf(
            ReviewDay(date = "2026-10-10"),
            ReviewDay(date = "2026-10-18"),
            ReviewDay(date = "2026-10-15"),
        )
        assertEquals(
            listOf("2026-10-18", "2026-10-15", "2026-10-10"),
            reviewDaysDescending(days).map { it.date },
        )
        assertEquals(emptyList<ReviewDay>(), reviewDaysDescending(emptyList()))
    }

    @Test
    fun `两个区块都在且空历史有文案`() {
        assertTrue(REVIEW_FEATURE_TITLE.isNotBlank())
        assertTrue(REVIEW_HISTORY_TITLE.isNotBlank())
        assertNotEquals("功能区与历史列表是两个区块", REVIEW_FEATURE_TITLE, REVIEW_HISTORY_TITLE)
        assertTrue("历史为空必须说话，不能空着", REVIEW_HISTORY_EMPTY.isNotBlank())
        // 空状态要告诉用户"怎么才会有"（不是一句干巴巴的"没有数据"）
        assertTrue(REVIEW_HISTORY_EMPTY.contains("开始回顾"))
    }
}
