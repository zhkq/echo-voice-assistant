package com.echo.contact.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **段的格式与"编不出来该怎么办"**（用户 2026-10-17 的第①件事）。
 *
 * 这一组用例钉的全是**静默错法**（错了不报错，只是音频废了/体积白涨/服务端拒收）：
 *  * 想编 FLAC 但设备编不出来 → **这一段必须退回 WAV**（绝不丢音频）；
 *  * 退回 WAV 时清单/`Content-Type` 必须记 **WAV**（绝不谎报格式 —— 服务端按 `fLaC` 魔数验）；
 *  * FLAC 的判据是**魔数**，不是"我们想要 FLAC"（编码器吐出来的字节里没有 `fLaC` 就不算数）；
 *  * 退回的那份音频里**必须真的有 PCM**（否则"没丢音频"这句话是空的）。
 *
 * ⚠️ **真机上的 `MediaCodec` 行为不在这一组里**（`FlacSegmentEncoder.encodeFlac` 走的是
 * Android 的 `MediaCodec`，JVM 单测跑不了它）。这一组钉的是**它外面那层判定**
 * —— 也就是"编不出来时的退路"与"格式的如实传递"。真机未验这件事我已如实写在
 * `FlacSegmentEncoder` 的类注释里，并在交付说明里点出。
 */
class MeetingSegmentAudioTest {

    private val pcm = ByteArray(320) { (it % 251).toByte() }

    /** 一段长得像 FLAC 的假字节（真编码器给回来的东西至少要有这个魔数）。 */
    private fun fakeFlac(payload: ByteArray = ByteArray(64) { 9 }): ByteArray =
        "fLaC".toByteArray() + payload

    // ---------------------------------------------------------------- 格式本身

    @Test
    fun `两种格式的后缀与 Content-Type 是契约的一部分`() {
        assertEquals("flac", SegmentFormat.FLAC.id)
        assertEquals("flac", SegmentFormat.FLAC.extension)
        assertEquals("audio/flac", SegmentFormat.FLAC.contentType)
        assertEquals("wav", SegmentFormat.WAV.id)
        assertEquals("audio/wav", SegmentFormat.WAV.contentType)
        assertEquals("默认档是 WAV（老清单/老调用点的行为一个字都不变）",
            SegmentFormat.WAV, SegmentFormat.DEFAULT)
    }

    @Test
    fun `清单里没写格式的段按 WAV 收（老清单里那些真的是 WAV）`() {
        assertEquals(SegmentFormat.WAV, SegmentFormat.DEFAULT)
        assertEquals(SegmentFormat.WAV, SegmentFormat.ofId(null))
        assertEquals(SegmentFormat.WAV, SegmentFormat.ofId(""))
        assertEquals(SegmentFormat.WAV, SegmentFormat.ofId("  "))
        assertEquals("认不出来的也不许猜成 FLAC（那会把 PCM 谎报成 FLAC）",
            SegmentFormat.WAV, SegmentFormat.ofId("opus"))
        assertEquals(SegmentFormat.FLAC, SegmentFormat.ofId("flac"))
        assertEquals(SegmentFormat.FLAC, SegmentFormat.ofId(" FLAC "))
    }

    @Test
    fun `FLAC 的判据是魔数 fLaC`() {
        assertTrue(looksLikeFlac(fakeFlac()))
        assertFalse("空的不是", looksLikeFlac(ByteArray(0)))
        assertFalse("只有三个字节不算", looksLikeFlac("fLa".toByteArray()))
        assertFalse("RIFF（wav）不是 FLAC", looksLikeFlac("RIFF....WAVE".toByteArray()))
        assertFalse("字节对但顺序不对也不算", looksLikeFlac("CaLf".toByteArray()))
    }

    // ---------------------------------------------------------------- 编码的判定（纯函数）

    @Test
    fun `编得出 FLAC 就用 FLAC 且不啰嗦`() {
        val out = SegmentEncoding.encode(
            flac = { fakeFlac() },
            wav = { error("编出来了就不该退回 WAV") },
        )
        assertEquals(SegmentFormat.FLAC, out.format)
        assertTrue(looksLikeFlac(out.bytes))
        assertEquals("编成了就没什么要说的（别用套话占住界面那一行）", "", out.note)
        assertFalse(out.fallback)
    }

    @Test
    fun `设备没有 FLAC 编码器时退回 WAV 并如实说`() {
        // 三种"没有"里最常见的那种：createEncoderByType 抛 / API < 24 → 我们的实现回 null
        val out = SegmentEncoding.encode(
            flac = { null },
            wav = { WavWriter.wrap(pcm, 16_000, 1, 16) },
        )
        assertEquals("这一段用 WAV（清单里必须如实记）", SegmentFormat.WAV, out.format)
        assertEquals(SegmentEncoding.FLAC_UNAVAILABLE, out.note)
        assertTrue("退回的那份必须是完整 WAV（头 + PCM）",
            out.bytes.size == WavWriter.HEADER_SIZE + pcm.size)
        assertTrue(out.fallback)
    }

    @Test
    fun `编码抛异常时也退回 WAV（绝不把异常冒出去）`() {
        val out = SegmentEncoding.encode(
            flac = { throw IllegalStateException("这台设备不认 audio/flac") },
            wav = { WavWriter.wrap(pcm, 16_000, 1, 16) },
        )
        assertEquals(SegmentFormat.WAV, out.format)
        assertEquals(SegmentEncoding.FLAC_UNAVAILABLE, out.note)
    }

    @Test
    fun `编回来了但没有 fLaC 魔数时退回 WAV（绝不谎报格式）`() {
        // 有些实现只吐裸帧。服务端按魔数识别，那种字节在它眼里不是 FLAC ——
        // 与其标成 FLAC 发出去（谎报 + 白传一遍），不如就地退回 WAV
        val out = SegmentEncoding.encode(
            flac = { ByteArray(32) { 7 } },
            wav = { WavWriter.wrap(pcm, 16_000, 1, 16) },
        )
        assertEquals(SegmentFormat.WAV, out.format)
        assertEquals(SegmentEncoding.FLAC_NOT_RECOGNIZED, out.note)
    }

    @Test
    fun `退回 WAV 绝不丢音频：PCM 一个字节不少`() {
        val out = SegmentEncoding.encode(
            flac = { null },
            wav = { WavWriter.wrap(pcm, 16_000, 1, 16) },
        )
        assertTrue("得是 RIFF/WAVE 头", String(out.bytes, 0, 4) == "RIFF")
        // PCM 原样在头后面（退回只是换容器，音频内容逐字节一致）
        val payload = out.bytes.copyOfRange(WavWriter.HEADER_SIZE, out.bytes.size)
        assertTrue("PCM 必须一个字节不少", payload.contentEquals(pcm))
    }

    @Test
    fun `只用 WAV 的编码器也如实标 WAV`() {
        val out = WavSegmentEncoder(16_000, 1, 16).encode(pcm)
        assertEquals(SegmentFormat.WAV, out.format)
        assertTrue("它也要说清为什么不是 FLAC", out.note.isNotBlank())
        assertTrue(out.bytes.size == WavWriter.HEADER_SIZE + pcm.size)
    }

    @Test
    fun `真实 FLAC 编码器在 JVM 里编不出来时也必须退回 WAV（不许把异常冒出去）`() {
        // 这一条在 JVM 上跑的就是"Android 那几个类不可用"这一档（MediaCodec 在单测里是桩）
        val out = FlacSegmentEncoder(16_000, 1).encode(pcm)
        assertEquals("编不出来就退回 WAV —— 绝不丢音频", SegmentFormat.WAV, out.format)
        assertTrue("退回时那句如实的话必须在：${out.note}", out.note.isNotBlank())
        assertTrue(out.bytes.size == WavWriter.HEADER_SIZE + pcm.size)
    }
}
