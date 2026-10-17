package com.echo.contact.ui

import com.echo.contact.service.AppPrefs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 设置页「每段多长」那几个档位的判据（用户 2026-10-18）。
 *
 * 默认 600 秒是**用户点名**的（"跟电脑一样 10 分钟一段就行"），而且它必须在界面上
 * 一眼看出是推荐档 —— 否则用户会按旧习惯选 30 秒，手机与电脑的段边界就对不上。
 */
class SettingsSegmentChoicesTest {

    @Test
    fun `600 秒标成与电脑一致并推荐`() {
        val label = segmentChoiceLabel(600)
        assertEquals("10 分钟 · 与电脑一致（推荐）", label)
        assertTrue(label.contains("与电脑一致"))
        assertTrue(label.contains("推荐"))
    }

    @Test
    fun `其它档位保留且标签可读`() {
        assertEquals("15 秒", segmentChoiceLabel(15))
        assertEquals("30 秒", segmentChoiceLabel(30))
        assertEquals("1 分钟", segmentChoiceLabel(60))
        assertEquals("5 分钟", segmentChoiceLabel(300))
        for (sec in AppPrefs.SEGMENT_SECONDS_CHOICES) {
            assertTrue("$sec 秒这一档没有标签", segmentChoiceLabel(sec).isNotBlank())
        }
        assertTrue("旧档位 15/30/60 不许被删掉", AppPrefs.SEGMENT_SECONDS_CHOICES.contains(30))
        assertTrue("默认档 600 必须在可选档里", AppPrefs.SEGMENT_SECONDS_CHOICES.contains(600))
    }

    @Test
    fun `段长说明按当前档位说`() {
        // 说明文字要准：用户点名的原话是"每 10 分钟封一段；结束时不足一段会补成尾段"
        assertEquals("10 分钟", segmentDurationText(600))
        assertEquals("5 分钟", segmentDurationText(300))
        assertEquals("1 分钟", segmentDurationText(60))
        assertEquals("30 秒", segmentDurationText(30))
        assertEquals("15 秒", segmentDurationText(15))
    }
}
