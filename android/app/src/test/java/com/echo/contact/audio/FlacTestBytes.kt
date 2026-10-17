package com.echo.contact.audio

/**
 * 用例用的**假 FLAC 字节**：形状与真编码器给回来的完全一致
 * （`fLaC` + STREAMINFO(34) + 一段帧字节），默认 `total_samples = 0`
 * —— 真机上 `MediaCodec` 写下来的就是 0（原因见 [FlacStreamInfo] 的文件头）。
 *
 * 为什么单独放一个对象：头本身 / 封段 / 端到端三组用例都要造"编码器原始输出"，
 * 各写一份的话，**哪一份的头造错了，红的地方就不是坏的地方**（这正是"用例自己撒谎"）。
 * 这里也刻意带上真机那份文件的形状：STREAMINFO **不是**最后一个 metadata block
 * （后面还跟着 Vorbis comment），于是"回填不许碰后面的 metadata"这条能被钉住。
 */
internal object FlacTestBytes {

    /**
     * 造一段"编码器产出的 FLAC"。
     *
     * 参数默认值照真机那一份取证文件填（16 kHz / 单声道 / 16 bit / block 4096 /
     * frame size 全 0 / MD5 全零 / total_samples=0）。
     */
    fun stream(
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
        /** 这个 STREAMINFO 是不是最后一个 metadata block（真机那份**不是**）。 */
        lastBlock: Boolean = false,
        /** 假帧字节数（只为"长度可预测"，内容不重要）。 */
        frameBytes: Int = 64,
    ): ByteArray {
        // 真机那份头后面还跟着一个 VORBIS_COMMENT block（`84 00 00 28` + 40 字节内容），
        // 只要不是最后一个 block 就照那个形状补上 —— 好让"回填不许碰后面的 metadata"有东西可碰
        val tailBlocks = if (lastBlock) ByteArray(0) else {
            ByteArray(4 + 40).also { block ->
                block[0] = 0x84.toByte()             // 最后一个 block + 类型 4（VORBIS_COMMENT）
                putInt(block, 1, 40, 3)              // block 长度 = 40
                for (i in 4 until block.size) block[i] = (i * 13 + 5).toByte()
            }
        }
        val out = ByteArray(FlacStreamInfo.FIXED_HEADER_SIZE + tailBlocks.size + frameBytes)
        asciiAt(out, 0, FlacStreamInfo.MAGIC)
        out[4] = ((if (lastBlock) 0x80 else 0) or (blockType and 0x7F)).toByte()
        putInt(out, 5, blockLength, 3)
        putInt(out, FlacStreamInfo.STREAMINFO_OFFSET, minBlockSize, 2)
        putInt(out, FlacStreamInfo.STREAMINFO_OFFSET + 2, maxBlockSize, 2)
        putInt(out, FlacStreamInfo.STREAMINFO_OFFSET + 4, minFrameSize, 3)
        putInt(out, FlacStreamInfo.STREAMINFO_OFFSET + 7, maxFrameSize, 3)
        val packed = (sampleRate.toLong() shl 44) or
            ((channels - 1).toLong() shl 41) or
            ((bitsPerSample - 1).toLong() shl 36) or
            (totalSamples and FlacStreamInfo.MAX_TOTAL_SAMPLES)
        putLong(out, FlacStreamInfo.RATE_CH_BPS_TOTAL_OFFSET, packed)
        System.arraycopy(md5, 0, out, FlacStreamInfo.MD5_OFFSET, 16)
        System.arraycopy(tailBlocks, 0, out, FlacStreamInfo.FIXED_HEADER_SIZE, tailBlocks.size)
        val framesFrom = FlacStreamInfo.FIXED_HEADER_SIZE + tailBlocks.size
        for (i in 0 until frameBytes) out[framesFrom + i] = (i * 7 + 3).toByte()
        return out
    }

    /** 大端写 [size] 个字节的无符号整数（`size <= 4`）。 */
    fun putInt(bytes: ByteArray, offset: Int, value: Int, size: Int) {
        for (i in 0 until size) {
            bytes[offset + i] = ((value ushr ((size - 1 - i) * 8)) and 0xFF).toByte()
        }
    }

    /** 大端写 8 个字节。 */
    fun putLong(bytes: ByteArray, offset: Int, value: Long) {
        for (i in 0 until 8) {
            bytes[offset + i] = ((value ushr ((7 - i) * 8)) and 0xFFL).toByte()
        }
    }

    /** 把 ASCII 写进 [bytes]（`fLaC` 这种）。 */
    fun asciiAt(bytes: ByteArray, offset: Int, s: String) {
        for (i in s.indices) bytes[offset + i] = s[i].code.toByte()
    }

    /** 十六进制字符串 → 字节（"真机样本"那条用例要原样抄取证文件的前若干字节）。 */
    fun hex(s: String): ByteArray {
        val clean = s.filter { !it.isWhitespace() }
        require(clean.length % 2 == 0) { "十六进制字符串长度必须是偶数" }
        return ByteArray(clean.length / 2) { i ->
            clean.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        }
    }

    /** [bytes] 某一段的十六进制小写（断言"没被动过"用）。 */
    fun hexOf(bytes: ByteArray, offset: Int, size: Int): String = buildString {
        for (i in 0 until size) {
            val b = bytes[offset + i].toInt() and 0xFF
            append("0123456789abcdef"[b ushr 4]).append("0123456789abcdef"[b and 0xF])
        }
    }
}
