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
}
