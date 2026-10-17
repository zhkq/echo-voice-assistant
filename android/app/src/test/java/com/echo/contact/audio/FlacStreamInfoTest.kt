package com.echo.contact.audio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **FLAC 定长头的回填**（2026-10-17 真机那个"服务端解码失败"的根治点）。
 *
 * 真机现场：手机 `MediaCodec` 写 FLAC 头时**还不知道总长度**，于是 STREAMINFO 里
 * `total_samples = 0`（规范里 = "未知"，合法）、MD5 全零。`libsndfile` 把那 0 读成
 * `SF_COUNT_MAX (2^63-1)` → 每块"读完记回位置"的 seek 必失败 →
 * `Internal psf_fseek() failed` → 服务端解不出音频（meta 里 `durationSeconds=2^63/8000`）。
 *
 * 这一组用例钉的是"回填"这件事本身：
 *  * 回填后**重新解析** STREAMINFO，`total_samples` == 真实样本数；
 *  * **文件长度一个字节都没变**（原地覆盖）；
 *  * `fLaC` 魔数还在、采样率/声道/位深/MD5/帧长这些**别人家的字段一个都没动**；
 *  * 头认不出 / 数越界 / 自检没过 → **拒绝写**（调用方整段退回 WAV，绝不谎报）。
 *
 * ⚠️ **这一组不碰 `MediaCodec`**（它在 JVM 单测里是桩）。真机行为那一半在
 * [FlacStreamInfo] 的类注释里如实写着（含取证文件前 42 字节的原样抄录），
 * 而"回填之后服务端能不能解"最终只能真机验 —— 这一点没有美化。
 */
class FlacStreamInfoTest {

    /** 造一段"编码器形状"的 FLAC（默认 `total_samples = 0`，与真机一致）。 */
    private fun flac(
        totalSamples: Long = 0,
        sampleRate: Int = 16_000,
        channels: Int = 1,
        bitsPerSample: Int = 16,
        minBlockSize: Int = 4096,
        maxBlockSize: Int = 4096,
        minFrameSize: Int = 0,
        maxFrameSize: Int = 0,
        md5: ByteArray = ByteArray(16),
        blockType: Int = FlacStreamInfo.STREAMINFO_BLOCK_TYPE,
        blockLength: Int = FlacStreamInfo.STREAMINFO_LENGTH,
        lastBlock: Boolean = false,
        frameBytes: Int = 64,
    ): ByteArray = FlacTestBytes.stream(
        totalSamples = totalSamples, sampleRate = sampleRate, channels = channels,
        bitsPerSample = bitsPerSample, minBlockSize = minBlockSize, maxBlockSize = maxBlockSize,
        minFrameSize = minFrameSize, maxFrameSize = maxFrameSize, md5 = md5,
        blockType = blockType, blockLength = blockLength, lastBlock = lastBlock,
        frameBytes = frameBytes,
    )

    /** [fix] 必须成功，返回那份字节。 */
    private fun okBytes(fix: FlacHeaderFix): ByteArray {
        assertTrue("这一步应当成功，实际：${(fix as? FlacHeaderFix.Failed)?.note}",
            fix is FlacHeaderFix.Ok)
        return (fix as FlacHeaderFix.Ok).bytes
    }

    /** [fix] 必须失败，返回那句如实话。 */
    private fun failNote(fix: FlacHeaderFix): String {
        assertTrue("这一步应当失败（失败就必须退回 WAV），实际拿到了 ${(fix as? FlacHeaderFix.Ok)?.bytes?.size} 字节",
            fix is FlacHeaderFix.Failed)
        return (fix as FlacHeaderFix.Failed).note
    }

    /** 一秒 16 kHz 单声道 16 bit 的 PCM 字节数。 */
    private fun pcmOf(seconds: Double): ByteArray = ByteArray((16_000 * 2 * seconds).toInt())

    // ---------------------------------------------------------------- 回填本身

    @Test
    fun `回填把 total_samples 写成真实样本数（0_5 秒 = 8000 个样本）`() {
        val pcm = pcmOf(0.5)
        val bytes = flac(totalSamples = 0)
        assertEquals("造出来的就是真机那一档：编码器写的是 0（未知）",
            0L, FlacStreamInfo.parse(bytes)!!.totalSamples)

        val out = okBytes(FlacHeaderBackfill.fix(bytes, FlacHeaderBackfill.samplesOf(pcm, 1)))

        val info = FlacStreamInfo.parse(out)!!
        assertEquals("回填后重新解析：total_samples 必须等于真实样本数",
            8_000L, info.totalSamples)
        assertTrue("这个头现在说的是真话", info.totalSamplesKnown)
        assertEquals("原地覆盖：文件长度一个字节都没变", bytes.size, out.size)
        assertTrue("fLaC 魔数还在", looksLikeFlac(out))
    }

    @Test
    fun `三种段长（0_5 与 30 与 61 秒）回填后 total_samples 都对`() {
        // 8000 = 0x1F40（2 字节）| 480000 = 0x75300（3 字节）| 976000 = 0xEE400（3 字节）
        listOf(0.5, 30.0, 61.0).forEach { seconds ->
            val pcm = pcmOf(seconds)
            val expected = (16_000.0 * seconds).toLong()
            assertEquals("用例的前置：$seconds 秒的 PCM 字节数要对",
                expected * 2, pcm.size.toLong())

            val bytes = flac(totalSamples = 0, frameBytes = 96)
            val out = okBytes(FlacHeaderBackfill.fix(bytes, FlacHeaderBackfill.samplesOf(pcm, 1)))

            assertEquals("$seconds 秒", expected, FlacStreamInfo.parse(out)!!.totalSamples)
            assertEquals("$seconds 秒：长度不变", bytes.size, out.size)
            assertTrue("$seconds 秒：魔数还在", looksLikeFlac(out))
        }
    }

    @Test
    fun `36 bit 跨 5 字节：2 的 32 次方与规范上限都写得进去（不会被截成 4 字节）`() {
        val cases = listOf(
            1L,
            0xFFFFL,                                   // 2 字节边界
            0x1_0000L,                                 // 刚过 2 字节
            0xFF_FFFFL,                                // 3 字节边界
            0x100_0000L,                               // 刚过 3 字节
            4_294_967_296L,                            // 2^32：低 32 bit 全 0，高位在偏移 21 的低半字节
            4_294_967_296L + 12_345L,                   // 2^32 + 一点
            FlacStreamInfo.MAX_TOTAL_SAMPLES,          // 36 bit 全 1 = 68719476735
        )
        cases.forEach { n ->
            val bytes = flac()
            val out = okBytes(FlacHeaderBackfill.fix(bytes, n))
            assertEquals("样本数 $n 必须原样写得进去（写成 4 字节就会被截断）",
                n, FlacStreamInfo.parse(out)!!.totalSamples)
            assertEquals("样本数 $n：长度不变", bytes.size, out.size)
        }

        // 位序的**逐字节**判据（照真机那份头：16000 = 0x3E80 / 单声道 / 16 bit / total=0）
        val pristine = flac()
        assertEquals("偏移 18..21 = 采样率(20bit) | 声道-1(3bit) | 位深-1(5bit) | total 的高 4 bit",
            "03e800f0", FlacTestBytes.hexOf(pristine, 18, 4))
        assertEquals("偏移 22..25 = total 的低 32 bit",
            "00000000", FlacTestBytes.hexOf(pristine, 22, 4))

        // 2^32 那一档：偏移 21 的低半字节必须变成 1（高半字节还是 bps-1 = 0xF），22..25 全 0。
        // 如果按"偏移 22..25 写一个 32 位整数"去做，这一档就会被写成 0（长度谎报成 0 = 崩溃）
        val b = flac()
        okBytes(FlacHeaderBackfill.fix(b, 4_294_967_296L))
        assertEquals("36 bit 里最高的 4 bit 落在偏移 21 的低半字节（高半字节是 bps-1）",
            0xF1, b[21].toInt() and 0xFF)
        assertEquals("偏移 22..25 = 低 32 bit（2^32 时全 0）",
            "00000000", FlacTestBytes.hexOf(b, 22, 4))
    }

    @Test
    fun `回填不碰别人家的字段（采样率 声道 位深 MD5 块长 帧长 原样）`() {
        val md5 = ByteArray(16) { (it + 1).toByte() }
        val bytes = flac(
            totalSamples = 0, minBlockSize = 1024, maxBlockSize = 4096,
            minFrameSize = 1234, maxFrameSize = 5678, md5 = md5,
        )
        val before = FlacStreamInfo.parse(bytes)!!

        val out = okBytes(FlacHeaderBackfill.fix(bytes, 480_000L))
        val after = FlacStreamInfo.parse(out)!!

        assertEquals("采样率", before.sampleRate, after.sampleRate)
        assertEquals("声道", before.channels, after.channels)
        assertEquals("位深", before.bitsPerSample, after.bitsPerSample)
        assertEquals("MD5（这一版**不**回填 MD5 —— 代价大，规范允许全零）", before.md5Hex, after.md5Hex)
        assertEquals("min block", before.minBlockSize, after.minBlockSize)
        assertEquals("max block", before.maxBlockSize, after.maxBlockSize)
        assertEquals("min frame", before.minFrameSize, after.minFrameSize)
        assertEquals("max frame（这一版保持 0 —— 规范允许）", before.maxFrameSize, after.maxFrameSize)
        assertEquals("真要改的只有那个总样本数", 480_000L, after.totalSamples)
    }

    @Test
    fun `回填是原地覆盖：同一份字节 长度不变 后面的 metadata 一个字节没动`() {
        val bytes = flac(totalSamples = 0, frameBytes = 40)
        val afterFortyTwo = FlacTestBytes.hexOf(bytes, FlacStreamInfo.FIXED_HEADER_SIZE, 4 + 40)
        val md5Before = FlacTestBytes.hexOf(bytes, FlacStreamInfo.MD5_OFFSET, 16)

        val out = okBytes(FlacHeaderBackfill.fix(bytes, 976_000L))

        assertSame("回填就是在传进来那份字节上原地覆盖（不新建、不拼接）", bytes, out)
        assertEquals("长度不变", bytes.size, out.size)
        assertEquals("偏移 42 之后的 metadata/帧字节一个都不许动",
            afterFortyTwo, FlacTestBytes.hexOf(out, FlacStreamInfo.FIXED_HEADER_SIZE, 4 + 40))
        assertEquals("MD5 没动", md5Before, FlacTestBytes.hexOf(out, FlacStreamInfo.MD5_OFFSET, 16))
        assertEquals("但 18..25 里那一项真的变了", 976_000L, FlacStreamInfo.parse(out)!!.totalSamples)
    }

    @Test
    fun `再回填一次是幂等的（同一份字节、同一个数）`() {
        val bytes = flac()
        okBytes(FlacHeaderBackfill.fix(bytes, 48_000L))
        val snapshot = bytes.copyOf()
        okBytes(FlacHeaderBackfill.fix(bytes, 48_000L))
        assertTrue("第二次不该改动任何字节", snapshot.contentEquals(bytes))
        assertEquals(48_000L, FlacStreamInfo.parse(bytes)!!.totalSamples)
    }

    // ---------------------------------------------------------------- 拒绝写（退回 WAV 的那一档）

    @Test
    fun `头认不出时拒绝写，一个字节都不动`() {
        val good = flac(totalSamples = 0)
        val broken = listOf(
            "太短（连定长头都不够）" to good.copyOfRange(0, 20),
            "魔数不对（RIFF 不是 FLAC）" to good.copyOf().also { FlacTestBytes.asciiAt(it, 0, "RIFF") },
            "第一个 block 不是 STREAMINFO" to flac(blockType = 4),
            "STREAMINFO 长度不是 34" to flac(blockLength = 40),
        )
        broken.forEach { (why, bytes) ->
            val snapshot = bytes.copyOf()
            val note = failNote(FlacHeaderBackfill.fix(bytes, 8_000L))
            assertEquals("$why：原因要说清是'认不出这个头'", FlacHeaderBackfill.NOTE_NO_HEADER, note)
            assertNull("$why：读头也要回 null", FlacStreamInfo.parse(bytes))
            assertTrue("$why：写不进去就一个字节都不许动", snapshot.contentEquals(bytes))
        }
    }

    @Test
    fun `超过 36 bit 上限时拒绝写（绝不写一个截断的数）`() {
        val bytes = flac()
        val snapshot = bytes.copyOf()
        assertEquals(FlacHeaderBackfill.NOTE_TOO_MANY,
            failNote(FlacHeaderBackfill.fix(bytes, FlacStreamInfo.MAX_TOTAL_SAMPLES + 1)))
        assertTrue("一个字节都不许动", snapshot.contentEquals(bytes))
        assertEquals("头里那个 0 还在（没被写成一个截断的数）",
            0L, FlacStreamInfo.parse(bytes)!!.totalSamples)
    }

    @Test
    fun `样本数算不出或为 0 时拒绝写`() {
        val bytes = flac()
        assertEquals("0 在规范里是「未知」，写它等于没修",
            FlacHeaderBackfill.NOTE_EMPTY, failNote(FlacHeaderBackfill.fix(bytes, 0L)))
        assertEquals("算不出（PCM 不是整帧）也不许编一个数",
            FlacHeaderBackfill.NOTE_CANNOT_COUNT, failNote(FlacHeaderBackfill.fix(bytes, null)))
        assertEquals("负数同样算'算不出'（不是'这一段没有样本'）",
            FlacHeaderBackfill.NOTE_CANNOT_COUNT, failNote(FlacHeaderBackfill.fix(bytes, -1L)))
    }

    // ---------------------------------------------------------------- 样本数怎么算

    @Test
    fun `样本数按真的字节数算（不是整帧就回 null）`() {
        assertEquals("0.5 秒单声道", 8_000L, FlacHeaderBackfill.samplesOf(pcmOf(0.5), 1))
        assertEquals("3 秒单声道", 48_000L, FlacHeaderBackfill.samplesOf(pcmOf(3.0), 1))
        assertEquals("立体声：每帧 4 字节", 8_000L,
            FlacHeaderBackfill.samplesOf(ByteArray(32_000), channelCount = 2))
        assertNull("奇数字节 = PCM 不是整帧（宁可不写，也不编一个数）",
            FlacHeaderBackfill.samplesOf(ByteArray(3), channelCount = 1))
        assertNull("立体声不是 4 的倍数",
            FlacHeaderBackfill.samplesOf(ByteArray(6), channelCount = 2))
        assertNull("声道数非法", FlacHeaderBackfill.samplesOf(ByteArray(10), channelCount = 0))
        assertEquals("0 个样本", 0L, FlacHeaderBackfill.samplesOf(ByteArray(0), channelCount = 1))
    }

    // ---------------------------------------------------------------- 真机样本（取证文件的前 46 字节）

    @Test
    fun `真机样本：MediaCodec 那份头（total_samples=0）能被认出来并回填`() {
        // 2026-10-17 PC 侧取证文件的**前 46 字节原样抄录**（前 42 = fLaC + STREAMINFO，
        // 后 4 = 紧随其后的 VORBIS_COMMENT block 头 `84 00 00 28`）。
        // 真机原话：ffmpeg 能完整解出 6.3 秒 = 100800 个样本，而 libsndfile 报
        // "Internal psf_fseek() failed"。
        val real = FlacTestBytes.hex(
            "664c6143" +            // fLaC
                "00000022" +        // block: 不是最后一个 / 类型 0（STREAMINFO）/ 长度 34
                "10001000" +        // min/max block size = 4096
                "000000" + "000000" +   // min/max frame size = 0（MediaCodec 不知道）
                "03e800f000000000" +    // 16000 Hz / 单声道 / 16 bit / total_samples = 0
                "00000000000000000000000000000000" +  // MD5 全零
                "84000028"          // 后面还跟着 VORBIS_COMMENT（回填不许碰它）
        )
        assertEquals("这 46 个字节就是取证文件的开头", 46, real.size)

        val info = FlacStreamInfo.parse(real)!!
        assertEquals("采样率 16000", 16_000, info.sampleRate)
        assertEquals("单声道", 1, info.channels)
        assertEquals("16 bit", 16, info.bitsPerSample)
        assertEquals("MediaCodec 写的是「未知」（这就是崩溃的根因）",
            FlacStreamInfo.TOTAL_SAMPLES_UNKNOWN, info.totalSamples)
        assertFalse("它自己不说长度", info.totalSamplesKnown)
        assertEquals("MD5 全零（规范里 = 没算）", "0".repeat(32), info.md5Hex)
        assertEquals("STREAMINFO 不是最后一个 block（后面还有 metadata）",
            0, real[4].toInt() and 0x80)

        val out = okBytes(FlacHeaderBackfill.fix(real, 100_800L))
        val after = FlacStreamInfo.parse(out)!!
        assertEquals("回填成真机上真解出来的那个样本数（6.3 秒）",
            100_800L, after.totalSamples)
        assertTrue(after.totalSamplesKnown)
        assertEquals("长度不变（原地覆盖）", 46, out.size)
        assertEquals("后面的 metadata block 头一个字节都不许动", 0x84, out[42].toInt() and 0xFF)
        assertEquals("100800 < 2^32 → 偏移 21 的高半字节仍是 bps-1，低半字节仍是 0",
            0xF0, out[21].toInt() and 0xFF)
        assertEquals("MD5 还是全零（这一版没回填 MD5）",
            info.md5Hex, after.md5Hex)
    }
}
