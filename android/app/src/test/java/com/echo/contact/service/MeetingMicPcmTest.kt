package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「手机当麦克风」那条路的两个纯函数（`toPcmBytes` / `rms`）。
 *
 * 它们值一组用例，是因为**错了会静默**：
 *  * 字节序写反 → 传上去的音频在电脑里是噪声/静音，而手机这边一切"正常"；
 *  * RMS 忘了夹住 0..1 → 电平条会画出屏幕（或恒为满格）。
 */
class MeetingMicPcmTest {

    @Test
    fun `PCM 是小端两字节`() {
        // 0x0102 = 258：小端在文件里是 0x02 0x01
        val b = toPcmBytes(listOf(0x0102.toShort()))
        assertEquals(2, b.size)
        assertEquals(0x02.toByte(), b[0])
        assertEquals(0x01.toByte(), b[1])
    }

    @Test
    fun `负数采样按补码写`() {
        val b = toPcmBytes(listOf((-2).toShort()))          // 0xFFFE
        assertEquals(0xFE.toByte(), b[0])
        assertEquals(0xFF.toByte(), b[1])
    }

    @Test
    fun `长度是采样数的两倍`() {
        assertEquals(200, toPcmBytes(List(100) { 0 }).size)
        assertEquals(0, toPcmBytes(emptyList()).size)
    }

    @Test
    fun `静音的电平是 0`() {
        assertEquals(0f, rms(ShortArray(1600), 1600), 0.0001f)
    }

    @Test
    fun `满量程的电平夹在 1 以内`() {
        val loud = ShortArray(1600) { Short.MAX_VALUE }
        val v = rms(loud, 1600)
        assertTrue("越界了：$v", v in 0f..1f)
        assertTrue("满量程该很接近 1：$v", v > 0.9f)
    }

    @Test
    fun `n 为 0 时不该炸`() {
        assertEquals(0f, rms(ShortArray(10), 0), 0.0001f)
    }

    // ---- 通知里那一行字（"录音到底有没有丢东西"的唯一出口）----

    @Test
    fun `待重传的段数要出现在通知里`() {
        val s = notificationText(MicState(active = true, sent = 4, pending = 2))
        assertTrue("用户不在界面里时，通知是唯一能看到'还有段没送出去'的地方：$s",
            s.contains("2 段待重传"))
        assertTrue("已送段数也要在：$s", s.contains("已送 4 段"))
    }

    @Test
    fun `错误优先于静音与待重传`() {
        // "有一段缓存不下来，可能真的丢了"绝不能被"在录 · 已送 3 段"盖掉 ——
        // 那是真的丢音频，优先级写错就等于在最该说话的时候沉默
        val s = notificationText(
            MicState(active = true, paused = true, sent = 3, pending = 5, error = "有一段缓存不下来"))
        assertEquals("有一段缓存不下来", s)
    }

    @Test
    fun `静音优先于待重传`() {
        val s = notificationText(MicState(active = true, paused = true, pending = 5))
        assertTrue("静音这件事更要紧（这段时间本来就不会录进会议）：$s", s.contains("已静音"))
        assertTrue("但也不能把这个状态说成'在录'：$s", !s.contains("在录"))
    }

    @Test
    fun `一切正常时只报已送段数`() {
        val s = notificationText(MicState(active = true, sent = 7))
        assertTrue(s, s.contains("在录"))
        assertTrue(s, s.contains("已送 7 段"))
        assertTrue("没有欠的段就不该出现'待重传'：$s", !s.contains("待重传"))
    }
}
