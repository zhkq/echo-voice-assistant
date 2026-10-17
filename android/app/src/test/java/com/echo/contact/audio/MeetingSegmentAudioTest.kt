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

    // ---------------------------------------------------------------- 封段收尾：回填 STREAMINFO

    /*
     * 下面这几条钉的是 2026-10-17 真机那个格式问题的**根治点**：
     * `MediaCodec` 写 FLAC 头时还不知道总长度 → `total_samples = 0`（规范里是"未知"，
     * 合法）→ `libsndfile` 读成 `SF_COUNT_MAX (2^63-1)` → 服务端 `psf_fseek` 崩、整段解不出来。
     *
     * 所以封段收尾必须**回填真的总样本数**；回填不了就**整段退回 WAV**（绝不发一个
     * total_samples=0 的 FLAC 出去，也绝不丢音频）。
     * 这一组走的是**生产路径** [encodeFlacSegment]（[FlacSegmentEncoder] 只是把
     * "用 MediaCodec 编出原始字节"那一步塞给它），所以除了 MediaCodec 本身，整条都验了。
     */

    /** 一段 1 秒的 PCM（16 kHz / 单声道 / 16 bit）。 */
    private fun oneSecondPcm(): ByteArray = ByteArray(16_000 * 2) { (it % 101).toByte() }

    @Test
    fun `封段时把 total_samples 回填成真实样本数（编码器给回来的是 0）`() {
        val pcm = oneSecondPcm()
        // MediaCodec 的原样输出形状：带 fLaC 魔数，但头里 total_samples = 0
        val raw = FlacTestBytes.stream(totalSamples = 0, frameBytes = pcm.size / 4)

        val out = encodeFlacSegment(pcm, 16_000, 1) { raw }

        assertEquals(SegmentFormat.FLAC, out.format)
        assertEquals("编成了就没什么要说的（别用套话占住界面那一行）", "", out.note)
        assertTrue(looksLikeFlac(out.bytes))
        assertEquals("头里必须写**真的**总样本数 —— 不是 0（0 就是服务端解不出来的那一档）",
            16_000L, FlacStreamInfo.parse(out.bytes)!!.totalSamples)
        assertEquals("回填是原地覆盖：长度一个字节都没变", raw.size, out.bytes.size)
        assertFalse("没有退回 WAV", out.fallback)
    }

    @Test
    fun `编码器自己猜的长度也会被真的样本数覆盖（写头时它不可能知道）`() {
        val pcm = oneSecondPcm()
        // 有些实现会往前猜一个数（这里故意写 999）：它不可能是对的 —— 写头时 EOS 还没到。
        // 我们的样本数来自**真的喂进去的 PCM 字节数**，比它准。
        val raw = FlacTestBytes.stream(totalSamples = 999, frameBytes = 32)

        val out = encodeFlacSegment(pcm, 16_000, 1) { raw }

        assertEquals(SegmentFormat.FLAC, out.format)
        assertEquals("头里必须是真实样本数（不是编码器猜的那个）",
            16_000L, FlacStreamInfo.parse(out.bytes)!!.totalSamples)
        assertEquals("长度不变", raw.size, out.bytes.size)
    }

    @Test
    fun `头认不出时整段退回 WAV，并如实说是回填失败（不是编码器不可用）`() {
        val pcm = oneSecondPcm()
        // 注入回填失败：编码器给回来的"FLAC"头不完整（真机上对应的情形：
        // 流里没有可认的 STREAMINFO —— 那种头里写什么都是猜的）
        val truncated = FlacTestBytes.stream(totalSamples = 0).copyOfRange(0, 20)

        val out = encodeFlacSegment(pcm, 16_000, 1) { truncated }

        assertEquals("回填不了就退回 WAV —— 绝不发一个 total_samples=0 的 FLAC", SegmentFormat.WAV, out.format)
        assertTrue("要说清是回填那一步失败：${out.note}", out.note.contains("回填"))
        assertFalse("不许拿'FLAC 编码器不可用'盖掉真实原因",
            out.note == SegmentEncoding.FLAC_UNAVAILABLE)
        // 退回只是换容器：PCM 逐字节一致（一个样本都不许丢）
        val payload = out.bytes.copyOfRange(WavWriter.HEADER_SIZE, out.bytes.size)
        assertTrue("PCM 必须一个字节不少", payload.contentEquals(pcm))
    }

    @Test
    fun `注入回填失败（fixup 直接失败）也退回 WAV，原因原样带出去`() {
        val out = SegmentEncoding.encode(
            flac = { FlacTestBytes.stream(totalSamples = 0) },
            wav = { WavWriter.wrap(pcm, 16_000, 1, 16) },
            fixup = FlacHeaderFixup { FlacHeaderFix.Failed("注入：回填失败") },
        )
        assertEquals(SegmentFormat.WAV, out.format)
        assertEquals("注入的那句原因要原样带出去（排障的人靠它找对地方）", "注入：回填失败", out.note)
    }

    @Test
    fun `回填的 fixup 抛异常也不许把异常冒出去（退回 WAV）`() {
        val out = SegmentEncoding.encode(
            flac = { FlacTestBytes.stream(totalSamples = 0) },
            wav = { WavWriter.wrap(pcm, 16_000, 1, 16) },
            fixup = FlacHeaderFixup { error("回填这一步炸了") },
        )
        assertEquals("抛异常 = 这一段编不成 —— 退回 WAV，绝不丢音频", SegmentFormat.WAV, out.format)
        assertEquals(SegmentEncoding.FLAC_HEADER_NOT_FIXED, out.note)
    }

    @Test
    fun `回填不了的那句话与'没有编码器'是两句不同的话`() {
        assertTrue(SegmentEncoding.FLAC_HEADER_NOT_FIXED.isNotBlank())
        assertFalse("两件事不许混成一句（混了就把排障的人指错方向）",
            SegmentEncoding.FLAC_HEADER_NOT_FIXED == SegmentEncoding.FLAC_UNAVAILABLE)
        assertFalse(SegmentEncoding.FLAC_HEADER_NOT_FIXED == SegmentEncoding.FLAC_NOT_RECOGNIZED)
    }

    // ---------------------------------------------------------------- 编码超时预算（段长 600 秒）

    /**
     * 编码超时必须**跟着数据量走**。
     *
     * 旧值固定 10 秒是按 30 秒段（≈1 MB）标定的。默认段长改成 600 秒后输入大 20 倍，
     * 固定 10 秒会把"还没编完"误判成"编不出来"→ 无谓退回 WAV（19 MB，走中继超过 12 MB
     * 上限就传不上去）。这条钉住"长段的预算更大、而且仍有上界"。
     */
    @Test
    fun `编码超时预算跟着数据量走且仍有上界`() {
        assertEquals("没有输入/空段：保持旧版的 10 秒底", 10_000L, encodeTimeoutMsFor(0))
        // 30 秒段 ≈ 960,000 字节 → 1 MB 档，只比旧值多一点
        assertTrue(encodeTimeoutMsFor(960_000) >= 10_000L)
        // 600 秒段 ≈ 19,200,000 字节：必须显著大于 10 秒，否则长段永远退回 WAV
        val long = encodeTimeoutMsFor(19_200_000)
        assertTrue("600 秒段的编码预算太小：$long", long >= 25_000L)
        assertTrue("预算必须有上界（卡死的编码器不能把封段线拖死）：$long", long <= 90_000L)
        assertEquals("超大输入封顶 90 秒", 90_000L, encodeTimeoutMsFor(200 * 1024 * 1024))
        // 单调：输入越大，预算不缩
        assertTrue(encodeTimeoutMsFor(19_200_000) >= encodeTimeoutMsFor(960_000))
    }
}
