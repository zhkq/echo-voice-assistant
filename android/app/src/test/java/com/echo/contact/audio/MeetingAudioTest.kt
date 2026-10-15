package com.echo.contact.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 会议音频的分段导航（`MeetingAudio`）—— 纯 JVM。 */
class MeetingAudioTest {

    @Test
    fun `段号从 1 起且不能越界`() {
        assertTrue(MeetingAudio.valid(1, 12))
        assertTrue(MeetingAudio.valid(12, 12))
        assertFalse(MeetingAudio.valid(0, 12))
        assertFalse(MeetingAudio.valid(13, 12))
        assertFalse("没有分段时任何段号都不合法", MeetingAudio.valid(1, 0))
    }

    @Test
    fun `下一段到末段就夹住不循环`() {
        assertEquals(2, MeetingAudio.next(1, 12))
        assertEquals(12, MeetingAudio.next(12, 12))
        assertEquals(0, MeetingAudio.next(1, 0))
    }

    @Test
    fun `上一段到首段就夹住不循环`() {
        assertEquals(1, MeetingAudio.prev(2, 12))
        assertEquals(1, MeetingAudio.prev(1, 12))
        assertEquals(0, MeetingAudio.prev(1, 0))
    }

    @Test
    fun `标题说得清是第几段`() {
        assertEquals("第 3/12 段", MeetingAudio.label(3, 12))
        assertEquals("这场会议没有可播放的分段", MeetingAudio.label(1, 0))
        assertTrue(MeetingAudio.label(99, 12).contains("超出范围"))
    }

    @Test
    fun `缓存名按会议与段号分开`() {
        assertEquals("meeting-7-seg3.wav", MeetingAudio.cacheName(7, 3))
        assertTrue(MeetingAudio.cacheName(7, 3) != MeetingAudio.cacheName(7, 4))
        assertTrue(MeetingAudio.cacheName(7, 3) != MeetingAudio.cacheName(8, 3))
    }
}
