package com.echo.contact.audio

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 逐字节钉住 wav 头。
 *
 * 为什么值得这么较真：这一环写错**不会报错** —— 表现是后端说"格式不认识"，
 * 或者干脆"没识别出文字"（422），而音频其实是好的。而它又是**手写的字节布局**
 * （小端、chunk 大小、byteRate、blockAlign），最容易被想当然。
 *
 * 下面那几条 `GOLDEN` 不是我自己算的：是用 **Python 标准库 `wave` 模块**
 * 对同样的参数写出来的前 44 字节（`wave` 是成熟的参考实现），
 * 所以这是在跟一个**独立实现**比对，而不是"我自己跟自己一致"。
 */
class WavTest {

    private fun hex(bytes: ByteArray): String =
        bytes.joinToString(" ") { "%02X".format(it.toInt() and 0xff) }

    private fun fromHex(s: String): ByteArray =
        s.trim().split(" ").filter { it.isNotBlank() }
            .map { it.toInt(16).toByte() }
            .toByteArray()

    /** Python: wave, 16000 Hz, 1ch, 16bit, 8 字节 PCM。 */
    private val GOLDEN_16K_MONO_8 =
        "52 49 46 46 2C 00 00 00 57 41 56 45 66 6D 74 20 10 00 00 00 01 00 01 00 " +
            "80 3E 00 00 00 7D 00 00 02 00 10 00 64 61 74 61 08 00 00 00"

    /** Python: wave, 16000 Hz, 1ch, 16bit, 32000 字节 PCM（正好 1 秒）。 */
    private val GOLDEN_16K_MONO_32000 =
        "52 49 46 46 24 7D 00 00 57 41 56 45 66 6D 74 20 10 00 00 00 01 00 01 00 " +
            "80 3E 00 00 00 7D 00 00 02 00 10 00 64 61 74 61 00 7D 00 00"

    /** Python: wave, 44100 Hz, 2ch, 16bit, 12 字节 PCM。 */
    private val GOLDEN_44K1_STEREO_12 =
        "52 49 46 46 30 00 00 00 57 41 56 45 66 6D 74 20 10 00 00 00 01 00 02 00 " +
            "44 AC 00 00 10 B1 02 00 04 00 10 00 64 61 74 61 0C 00 00 00"

    @Test
    fun `录音参数的 wav 头与 Python wave 模块逐字节一致`() {
        // 这正是手机录音用的参数：16 kHz 单声道
        val pcm = ByteArray(8)
        val out = WavWriter.wrap(pcm, 16000, 1, 16)

        assertEquals(44 + 8, out.size)
        assertEquals(GOLDEN_16K_MONO_8, hex(out.copyOfRange(0, 44)))
    }

    @Test
    fun `一秒音频的头也对（chunk 大小随长度走）`() {
        val pcm = ByteArray(32000)          // 16000 样本 × 2 字节
        val out = WavWriter.wrap(pcm, 16000, 1, 16)

        assertEquals(44 + 32000, out.size)
        assertEquals(GOLDEN_16K_MONO_32000, hex(out.copyOfRange(0, 44)))
    }

    @Test
    fun `立体声与大采样率也对（byteRate 与 blockAlign 不能写死）`() {
        val pcm = ByteArray(12)
        val out = WavWriter.wrap(pcm, 44100, 2, 16)

        assertEquals(56, out.size)
        assertEquals(GOLDEN_44K1_STEREO_12, hex(out.copyOfRange(0, 44)))
    }

    @Test
    fun `四个魔数字段都是 ASCII`() {
        val out = WavWriter.wrap(ByteArray(4), 16000, 1, 16)

        assertEquals("RIFF", String(out, 0, 4, Charsets.US_ASCII))
        assertEquals("WAVE", String(out, 8, 4, Charsets.US_ASCII))
        assertEquals("fmt ", String(out, 12, 4, Charsets.US_ASCII))
        assertEquals("data", String(out, 36, 4, Charsets.US_ASCII))
    }

    @Test
    fun `字段值逐个可读（出错时能一眼看出是哪一个）`() {
        val pcm = ByteArray(1000)
        val out = WavWriter.wrap(pcm, 16000, 1, 16)

        fun u32(off: Int) = (out[off].toInt() and 0xff) or
            ((out[off + 1].toInt() and 0xff) shl 8) or
            ((out[off + 2].toInt() and 0xff) shl 16) or
            ((out[off + 3].toInt() and 0xff) shl 24)

        fun u16(off: Int) = (out[off].toInt() and 0xff) or ((out[off + 1].toInt() and 0xff) shl 8)

        assertEquals(36 + 1000, u32(4))     // RIFF chunk 大小 = 36 + data
        assertEquals(16, u32(16))           // fmt chunk 长度
        assertEquals(1, u16(20))            // PCM
        assertEquals(1, u16(22))            // 单声道
        assertEquals(16000, u32(24))        // 采样率
        assertEquals(32000, u32(28))        // byteRate = 16000 × 1 × 16/8
        assertEquals(2, u16(32))            // blockAlign
        assertEquals(16, u16(34))           // 位深
        assertEquals(1000, u32(40))         // data 长度
    }

    @Test
    fun `PCM 原样跟在头后面 一个字节都不动`() {
        val pcm = byteArrayOf(1, -2, 3, -4, 127, -128)
        val out = WavWriter.wrap(pcm, 16000, 1, 16)

        assertArrayEquals(pcm, out.copyOfRange(44, out.size))
    }

    @Test
    fun `空 PCM 也能生成合法的头（不该崩）`() {
        val out = WavWriter.wrap(ByteArray(0), 16000, 1, 16)

        assertEquals(44, out.size)
        assertEquals("RIFF", String(out, 0, 4, Charsets.US_ASCII))
        assertEquals(36, (out[4].toInt() and 0xff))   // 36 + 0
        assertEquals(0, (out[40].toInt() and 0xff))   // data 长度 0
    }

    @Test
    fun `从十六进制还原的黄金头与我生成的一致（防抄错）`() {
        // 这条是"检查上面那几条 GOLDEN 常量本身没被抄错"：用同一个函数来回解一遍
        val mine = hex(WavWriter.wrap(ByteArray(8), 16000, 1, 16).copyOfRange(0, 44))
        assertEquals(fromHex(GOLDEN_16K_MONO_8).size, 44)
        assertEquals(GOLDEN_16K_MONO_8.replace("  ", " "), mine)
    }
}
