package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「哪种事件值得打扰人」的判据（`EventNotify.summarize`，纯函数）。
 *
 * 用户 2026-10-15 的口径是"**手机收**" —— 但"收"不等于"什么都弹"：
 *  `command_received` / `command_sent` 是过程，弹出来只会吵；而"转写好了""纪要好
 *  了""有结果了"才是他等的那件事。这条边界写错了，用户会关掉整个通知权限。
 */
class EventNotifyTest {

    @Test
    fun `等的那几件事都要弹`() {
        for (t in listOf("meeting_transcribed", "meeting_summary_ready",
                         "meeting_stopped", "meeting_imported", "command_done")) {
            val s = EventNotify.summarize(t, emptyMap())
            assertTrue("$t 该有通知", s != null)
            assertTrue("$t 的标题不该为空", s!!.first.isNotBlank())
            assertTrue("$t 的正文不该为空", s.second.isNotBlank())
        }
    }

    @Test
    fun `过程类事件不打扰`() {
        for (t in listOf("command_received", "command_sent", "wake_fired", "whatever_new")) {
            assertNull("$t 不该弹通知", EventNotify.summarize(t, emptyMap()))
        }
    }

    @Test
    fun `带上会议名更像人话`() {
        val s = EventNotify.summarize("meeting_transcribed", mapOf("meeting" to "2026-10-15_15-36-48"))
        assertTrue(s!!.second, s.second.contains("2026-10-15_15-36-48"))
    }

    @Test
    fun `没有会议名也要能说清楚`() {
        val s = EventNotify.summarize("meeting_transcribed", emptyMap())
        assertEquals("会议转写好了", s!!.first)
        assertTrue(s.second.isNotBlank())
    }

    @Test
    fun `payload 里乱七八糟的值不该炸`() {
        val s = EventNotify.summarize("meeting_transcribed",
            mapOf("meeting" to 42, "name" to null, "id" to listOf(1, 2)))
        assertTrue(s != null)
        assertTrue("数字/空值都不该被当成会议名", !s!!.second.contains("42"))
    }
}
