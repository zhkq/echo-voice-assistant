package com.echo.contact.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * 常驻听写的分段判据（`VoiceSegmenter`）—— **纯 JVM，不需要设备/麦克风**。
 *
 * 这一组盯的是"什么时候算一句话说完了"。判早了后端收到半句、判晚了要等两句说完
 * 才有回复，两种都是车里最难查的体验问题，所以每条规则都逐帧钉住。
 */
class VoiceSegmenterTest {

    private val frame = 1600                 // 100 ms @ 16 kHz
    private val hangoverFrames = 12          // 1200 ms 默认 hangover

    private fun silence(): ShortArray = ShortArray(frame)

    /** 有效语音：交替 ±8000 → RMS ≈ 0.244，远高于默认门限 0.012。 */
    private fun voice(amp: Int = 8000): ShortArray =
        ShortArray(frame) { if (it % 2 == 0) amp.toShort() else (-amp).toShort() }

    @Test
    fun `一直静音不产段`() {
        val seg = VoiceSegmenter()
        repeat(50) { assertNull(seg.feed(silence(), frame)) }
        assertEquals(VoiceSegmenter.State.IDLE, seg.state)
        assertEquals(0, seg.speechMs)
    }

    @Test
    fun `一句话加静音收尾`() {
        val seg = VoiceSegmenter()
        repeat(5) { assertNull(seg.feed(voice(), frame)) }        // 说 500 ms
        assertEquals(VoiceSegmenter.State.SPEAKING, seg.state)

        var out: ByteArray? = null
        repeat(hangoverFrames) { out = seg.feed(silence(), frame) }

        assertNotNull("静音超过 hangover 就该收尾", out)
        // 片段 = 说话那 5 帧 + 收尾那 12 帧静音（尾巴留着更自然）
        assertEquals((5 + hangoverFrames) * frame * 2, out!!.size)
        assertEquals(VoiceSegmenter.State.IDLE, seg.state)
    }

    @Test
    fun `前导静音只留预卷`() {
        val seg = VoiceSegmenter()                                 // preRoll 默认 400 ms = 4 帧
        repeat(30) { seg.feed(silence(), frame) }                  // 干等 3 秒
        repeat(5) { seg.feed(voice(), frame) }
        var out: ByteArray? = null
        repeat(hangoverFrames) { out = seg.feed(silence(), frame) }

        assertNotNull(out)
        // 只有 4 帧预卷进了 wav，而不是 30 帧静音
        assertEquals((4 + 5 + hangoverFrames) * frame * 2, out!!.size)
    }

    @Test
    fun `太短的一段当噪声丢掉`() {
        val seg = VoiceSegmenter()
        seg.feed(voice(), frame)                                   // 只有 100 ms < 300 ms
        var out: ByteArray? = null
        repeat(hangoverFrames) { out = seg.feed(silence(), frame) }
        assertNull("太短不该发给后端", out)

        // 丢掉之后要能正常接下一句。
        // 注意：上面那 12 帧静音是**上一段（被丢掉的噪声）的一部分**，不进预卷，
        // 所以这里就是干净的 5 帧说话 + 12 帧收尾静音。
        repeat(5) { seg.feed(voice(), frame) }
        repeat(hangoverFrames) { out = seg.feed(silence(), frame) }
        assertNotNull(out)
        assertEquals((5 + hangoverFrames) * frame * 2, out!!.size)
    }

    @Test
    fun `说太久强制切段`() {
        val seg = VoiceSegmenter(maxSegmentMs = 1000)
        var cuts = 0
        var size = 0
        repeat(15) {
            val out = seg.feed(voice(), frame)
            if (out != null) {
                cuts++
                size = out.size
            }
        }
        assertEquals("过 1 秒就该切一刀", 1, cuts)
        assertEquals(10 * frame * 2, size)                         // 正好 10 帧 = 1000 ms
    }

    @Test
    fun `小声不算说话`() {
        val seg = VoiceSegmenter()
        // 幅度 100 → RMS ≈ 0.003，低于默认门限 0.012：这是底噪，不是人在说
        repeat(30) { assertNull(seg.feed(voice(100), frame)) }
        assertEquals(VoiceSegmenter.State.IDLE, seg.state)
    }

    @Test
    fun `reset 之后干净重来`() {
        val seg = VoiceSegmenter()
        repeat(5) { seg.feed(voice(), frame) }
        assertEquals(VoiceSegmenter.State.SPEAKING, seg.state)

        seg.reset()
        assertEquals(VoiceSegmenter.State.IDLE, seg.state)
        assertEquals(0, seg.speechMs)
        assertEquals(0.0, seg.level, 0.0001)

        // 重新来一句：不该把 reset 之前那半句带进来
        repeat(5) { seg.feed(voice(), frame) }
        var out: ByteArray? = null
        repeat(hangoverFrames) { out = seg.feed(silence(), frame) }
        assertEquals((5 + hangoverFrames) * frame * 2, out!!.size)
    }

    @Test
    fun `PCM 走小端`() {
        // 0x0102 → 低字节 0x02 在前（ECHO 的 WavWriter 也按小端写头）
        val bytes = VoiceSegmenter.pcmToBytes(listOf(0x0102.toShort(), (-2).toShort()))
        assertEquals(4, bytes.size)
        assertEquals(0x02.toByte(), bytes[0])
        assertEquals(0x01.toByte(), bytes[1])
        assertEquals(0xFE.toByte(), bytes[2])
        assertEquals(0xFF.toByte(), bytes[3])
    }
}
