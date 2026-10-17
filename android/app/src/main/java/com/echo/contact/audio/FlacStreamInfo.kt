package com.echo.contact.audio

/**
 * **FLAC 流的定长头**（`fLaC` 魔数 + 第一个 metadata block），只做两件事：
 * **读头**（[parse]）与**原地改 `total_samples`**（[rewriteTotalSamplesInPlace]）。
 *
 * ## 为什么需要它（2026-10-17 真机暴露的格式问题）
 *
 * 手机用 `MediaCodec` 编 FLAC 时，**写头的那一刻它还不知道这一段总共有多少样本**
 * （音频是一个缓冲一个缓冲喂进去的，EOS 还没到）。于是它把 STREAMINFO 里的
 * `total_samples` 写成 **0**、MD5 写成**全零**、min/max framesize 也留 0。
 * 按 FLAC 规范，`total_samples = 0` 表示"**未知**"，这本身是**合法**的；
 * 但 `libsndfile` 把它读成 `SF_COUNT_MAX (2^63-1)`，于是它每读完一块都要
 * "记下位置、seek 回来"（它的读法），而那个长度根本不存在 →
 * `Internal psf_fseek() failed` → **整段音频解不出来**。
 * 服务端 meta 里那个 `durationSeconds = 1152921504606847.0` 就是 2^63/8000 的痕迹。
 * 文件本身没坏（ffmpeg 能完整解出 6.3 秒 / 100800 样本）—— 坏的只有头里那个 0。
 *
 * 所以封段收尾时把**真的总样本数**回填进去，是客户端这一侧最划算的根治：
 * 头是定长的，回填是**原地覆盖**，**文件长度一个字节都不变**。
 *
 * ## 字节偏移（改这里之前先把这一段读完；这是全项目唯一直接写 FLAC 头的地方）
 *
 * ```
 *  偏移 0..3    "fLaC"
 *  偏移 4       metadata block header：bit7 = 最后一个 block 的标志，
 *                                         bit0..6 = block 类型（0 = STREAMINFO）
 *  偏移 5..7    该 block 的长度，24 bit **大端**（STREAMINFO 恒为 34）
 *  偏移 8..9    STREAMINFO.min_block_size   （16 bit 大端）
 *  偏移 10..11  max_block_size
 *  偏移 12..14  min_frame_size              （24 bit 大端）
 *  偏移 15..17  max_frame_size
 *  偏移 18..25  8 个字节 = **一个 64 bit 大端的整体**：
 *                 sample_rate(20) | channels-1(3) | bits_per_sample-1(5) | total_samples(36)
 *  偏移 26..41  MD5（16 字节；MediaCodec 给的是全零 = 规范里的"没算"）
 * ```
 *
 * ## `total_samples` 的位序（最容易写错的一处）
 *
 * 它是上面那个 64 bit 整体的**最低 36 bit**，**跨 5 个字节**：
 *  * `偏移 21` 的**低半字节**（高半字节是 `bits_per_sample - 1` 的高 4 位）
 *    —— 也就是 36 bit 里最高的那 4 bit；
 *  * `偏移 22..25` 四个整字节（低 32 bit）。
 *
 * 所以**绝不能**按"偏移 22..25 的一个 32 位大端整数"去写：那样写会把 2^32 以上的
 * 位数**截掉**（30 秒 / 61 秒的语音段只有几十万个样本，看着不会露馅；但更长的段
 * 会写出一个**谎报的长度**）。这里一律把 18..25 当成一个 64 bit 数读出来、
 * 只替换低 36 bit、再原样写回去 —— 采样率/声道/位深因此**一个字都不会动**。
 *
 * ## 真实样本（2026-10-17 PC 侧取证那一份 MediaCodec 文件，前 42 字节）
 *
 * ```
 * 66 4C 61 43 00 00 00 22 10 00 10 00 00 00 00 00 00 00 03 E8 00 F0 00 00 00 00
 * 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
 * ```
 * 解出来：`fLaC`；block 类型 0、长度 34（**不是**最后一个 block —— 后面还有一个
 * Vorbis comment）；min/max block 4096；frame size 全 0；`03E8 00F0 00000000`
 * = 采样率 16000、单声道、16 bit、**total_samples = 0**；MD5 全零。
 */
object FlacStreamInfo {

    /** FLAC 流的魔数（4 字节，流最前面）。 */
    const val MAGIC = "fLaC"

    const val MAGIC_SIZE = 4

    /** 第一个 metadata block 的 header 从偏移 4 开始。 */
    const val BLOCK_HEADER_OFFSET = 4

    /** block 类型：STREAMINFO（规范要求它必须是第一个）。 */
    const val STREAMINFO_BLOCK_TYPE = 0

    /** STREAMINFO 的 body 长度（固定 34 字节）。 */
    const val STREAMINFO_LENGTH = 34

    /** STREAMINFO 的 body 起点 = 4 字节魔数 + 4 字节 block header。 */
    const val STREAMINFO_OFFSET = 8

    /** sample_rate/channels/bps/total_samples 那 8 个字节的起点。 */
    const val RATE_CH_BPS_TOTAL_OFFSET = 18

    /** MD5 的起点（16 字节）。 */
    const val MD5_OFFSET = 26

    /** 定长头总长 = 4 + 4 + 34 = **42 字节**（回填**不改**这个长度）。 */
    const val FIXED_HEADER_SIZE = 42

    /** `total_samples` 占 36 bit。 */
    const val TOTAL_SAMPLES_BITS = 36

    /** 36 bit 全 1 = 68719476735（规范上限；再大就写不进这个字段）。 */
    const val MAX_TOTAL_SAMPLES = 0xFFFF_FFFFFL

    /** 规范里 **0 = 未知**（合法，但 libsndfile 会读成 2^63-1 —— 见文件头）。 */
    const val TOTAL_SAMPLES_UNKNOWN = 0L

    /** 读出来的头（只读我们关心的那几项；用例拿它逐项断言）。 */
    class Info(
        /** 总样本数（每声道；0 = 未知）。 */
        val totalSamples: Long,
        val sampleRate: Int,
        val channels: Int,
        val bitsPerSample: Int,
        val minBlockSize: Int,
        val maxBlockSize: Int,
        val minFrameSize: Int,
        val maxFrameSize: Int,
        /** MD5，16 字节的十六进制小写（MediaCodec 给的是 32 个 `0`）。 */
        val md5Hex: String,
    ) {
        /** 这个头有没有**如实**写下长度（0 = 规范里的"未知"）。 */
        val totalSamplesKnown: Boolean get() = totalSamples != TOTAL_SAMPLES_UNKNOWN
    }

    /**
     * 读头。**不是一个完好的 FLAC 定长头就回 null**（调用方据此退回 WAV）。
     *
     * 判据刻意严：魔数对、第一个 block 就是 STREAMINFO（类型 0）、长度 34、文件够 42 字节。
     * 少一样就说明"我们认不出这个头"，那就**不许**往里写 —— 写错位置比不写更糟
     * （会毁掉后面的 metadata 或帧数据）。
     */
    fun parse(bytes: ByteArray): Info? {
        if (!hasFixedHeader(bytes)) return null
        val packed = longAt(bytes, RATE_CH_BPS_TOTAL_OFFSET)
        return Info(
            totalSamples = packed and MAX_TOTAL_SAMPLES,
            sampleRate = ((packed ushr 44) and 0xFFFFF).toInt(),
            channels = ((packed ushr 41) and 0x7).toInt() + 1,
            bitsPerSample = ((packed ushr 36) and 0x1F).toInt() + 1,
            minBlockSize = intAt(bytes, STREAMINFO_OFFSET, 2),
            maxBlockSize = intAt(bytes, STREAMINFO_OFFSET + 2, 2),
            minFrameSize = intAt(bytes, STREAMINFO_OFFSET + 4, 3),
            maxFrameSize = intAt(bytes, STREAMINFO_OFFSET + 7, 3),
            md5Hex = hexOf(bytes, MD5_OFFSET, 16),
        )
    }

    /**
     * **原地**把 `total_samples` 写成 [totalSamples]：长度不变、别的字段一个都不动。
     *
     * 返回 false = **没写**（头认不出 / 数越界）—— 一个字节都没动，调用方退回 WAV。
     * 注意：写成功之后仍然要**重新 [parse] 一遍自检**（[FlacHeaderBackfill.fix] 会做），
     * 别拿"我写了"当"写对了"。
     */
    fun rewriteTotalSamplesInPlace(bytes: ByteArray, totalSamples: Long): Boolean {
        if (totalSamples < 0 || totalSamples > MAX_TOTAL_SAMPLES) return false
        if (!hasFixedHeader(bytes)) return false
        val old = longAt(bytes, RATE_CH_BPS_TOTAL_OFFSET)
        // 只替换最低 36 bit：高 28 bit（采样率 20 / 声道 3 / 位深 5）原样保留
        val packed = (old and MAX_TOTAL_SAMPLES.inv()) or (totalSamples and MAX_TOTAL_SAMPLES)
        putLong(bytes, RATE_CH_BPS_TOTAL_OFFSET, packed)
        return true
    }

    /** 这个头我们认不认（魔数 + 第一个 block 是 STREAMINFO + 长度 34 + 够 42 字节）。 */
    fun hasFixedHeader(bytes: ByteArray): Boolean =
        bytes.size >= FIXED_HEADER_SIZE &&
            looksLikeFlac(bytes) &&
            (bytes[BLOCK_HEADER_OFFSET].toInt() and 0x7F) == STREAMINFO_BLOCK_TYPE &&
            intAt(bytes, BLOCK_HEADER_OFFSET + 1, 3) == STREAMINFO_LENGTH

    // ---- 大端读写（FLAC 头里所有整数都是大端；这也是最容易写反的一处）----

    /** 读 [size] 个字节的大端无符号整数（`size <= 4`）。 */
    private fun intAt(bytes: ByteArray, offset: Int, size: Int): Int {
        var v = 0
        for (i in 0 until size) v = (v shl 8) or (bytes[offset + i].toInt() and 0xFF)
        return v
    }

    /** 读 8 个字节的大端 64 bit 整数。 */
    private fun longAt(bytes: ByteArray, offset: Int): Long {
        var v = 0L
        for (i in 0 until 8) v = (v shl 8) or (bytes[offset + i].toLong() and 0xFFL)
        return v
    }

    /** 写 8 个字节的大端 64 bit 整数（**原地**，不增不删）。 */
    private fun putLong(bytes: ByteArray, offset: Int, value: Long) {
        for (i in 0 until 8) {
            bytes[offset + i] = ((value ushr ((7 - i) * 8)) and 0xFFL).toByte()
        }
    }

    private fun hexOf(bytes: ByteArray, offset: Int, size: Int): String = buildString {
        for (i in 0 until size) {
            val b = bytes[offset + i].toInt() and 0xFF
            append(HEX[b ushr 4]).append(HEX[b and 0xF])
        }
    }

    private const val HEX = "0123456789abcdef"
}

/**
 * 一次「回填 STREAMINFO 总样本数」的结局。
 *
 * 为什么不是"回一个 ByteArray?"：失败**必须能说出为什么** —— 界面/日志里那句
 * "这段用 WAV"要带上真实原因（头认不出？样本数算不出？自检没过？），
 * 否则排障的人只会看到一句"退回 WAV"却不知道该去查什么。
 */
sealed interface FlacHeaderFix {

    /** 回填成功。[bytes] 就是传进来那个数组（**原地覆盖，长度不变**）。 */
    class Ok(val bytes: ByteArray) : FlacHeaderFix

    /** 回填失败。[note] 是给界面/日志的那句如实话（**这一段要退回 WAV**）。 */
    class Failed(val note: String) : FlacHeaderFix
}

/**
 * 「封段收尾」这一步：把 FLAC 头里的 `total_samples` 回填成真的。
 *
 * 为什么做成一个可注入的接口：这一步的错法**全是静默的** ——
 *  * **不写**：libsndfile 把那 0 读成 2^63-1，seek 崩（真机就是这么暴露的）；
 *  * **写错位置/位序**：头就废了，而且长度看着像真的（更难发现）；
 *  * **写失败却照样标成 FLAC**：谎报格式（服务端按魔数认，一解就露馅）。
 *
 * 注入之后，"回填失败就整段退回 WAV"这条退路能在 JVM 里用假的失败实现逐条钉住。
 *
 * ⚠️ 生产路径**必须**用 [FlacHeaderBackfill.forPcm] 造出来的那一份
 * （见 [SegmentEncoding.encode] 与 `FlacSegmentEncoder`）。[NONE] 只留给用例里那些
 * "本来就不当 FLAC 用"的假字节，**别在生产里用**。
 */
fun interface FlacHeaderFixup {
    fun fix(bytes: ByteArray): FlacHeaderFix

    companion object {
        /** 不动字节（= "这份字节的头本来就是真的"）。**生产路径不许用它**。 */
        val NONE = FlacHeaderFixup { FlacHeaderFix.Ok(it) }
    }
}

/**
 * 「总样本数从哪来 + 怎么写回去」的**唯一**实现（纯逻辑，不碰 Android，JVM 可测）。
 *
 * ## 为什么样本数是**算**出来的，不是猜的
 *
 * 16 kHz / 单声道 / 16 bit 是这条路恒定的参数（与 `WavWriter.wrap` 的调用点同一份口径），
 * 所以样本数 = `pcm.size / 2`。**不能**拿"设置里的段长 × 采样率"当答案：
 * 尾段是不足一段的（`TailFlush` 会把剩下的几十毫秒也封成一段），按段长算会写出一个
 * **比真实音频长**的头 —— 那同样是谎报，而且 libsndfile 读到"不够的样本"会截断/报错。
 *
 * ## 为什么回填之后还要自检
 *
 * 写的动作只有 8 个字节，但错法有"偏移错一位""位序反了""把采样率踩掉了"三种，
 * 它们都不会抛异常。所以写完**重新按 [FlacStreamInfo.parse] 读一遍**，
 * 逐项比对（见 [fix]）—— 不对就退回 WAV：**宁可慢一半，也别丢音频/谎报格式**。
 */
object FlacHeaderBackfill {

    /** 这条路恒定 16 bit 采样（每个样本 2 字节，见 `WavWriter.wrap(pcm, …, 16)`）。 */
    const val BYTES_PER_SAMPLE = 2

    /** 头认不出（没有 `fLaC` / 第一个 block 不是 STREAMINFO / 长度不是 34）。 */
    const val NOTE_NO_HEADER =
        "FLAC 头的总样本数回填不了：认不出这个头（第一个 metadata block 不是 STREAMINFO）"

    /** PCM 不是整帧 —— "有多少样本"本身就说不准。 */
    const val NOTE_CANNOT_COUNT =
        "FLAC 头的总样本数回填不了：算不出这一段有多少样本（PCM 不是整帧）"

    /** 一个样本都没有：规范里的 0 是"未知"，写它等于没修。 */
    const val NOTE_EMPTY =
        "FLAC 头的总样本数回填不了：这一段一个样本都没有（0 在规范里是「未知」，写它等于没修）"

    /** 超过 36 bit 上限。 */
    const val NOTE_TOO_MANY =
        "FLAC 头的总样本数回填不了：样本数超过 36 bit 上限（写进去会被截断）"

    /** 写完自检没过（长度/魔数/别的字段被动过、或读回来的数不符）。 */
    const val NOTE_SELF_CHECK =
        "FLAC 头的总样本数回填不了：回填后自检没过" +
            "（total_samples 与真实样本数不符 / 文件长度变了）"

    /**
     * 这一段 PCM 有多少个**样本**（= 每声道多少个采样点）。
     *
     * 除不尽（PCM 不是整帧）就回 null：宁可整段退回 WAV（WAV 里那个字节数照样原样保留），
     * 也不往头里写一个凑出来的数。
     */
    fun samplesOf(
        pcm: ByteArray,
        channelCount: Int,
        bytesPerSample: Int = BYTES_PER_SAMPLE,
    ): Long? {
        val frameBytes = channelCount * bytesPerSample
        if (frameBytes <= 0) return null
        if (pcm.size % frameBytes != 0) return null
        return (pcm.size / frameBytes).toLong()
    }

    /**
     * 生产路径用的那一份：**封段那一刻**就把样本数算好，回填时直接用
     * （编完再算也没问题，但固定在同一处算，少一个"两边口径不一致"的机会）。
     */
    fun forPcm(
        pcm: ByteArray,
        channelCount: Int,
        bytesPerSample: Int = BYTES_PER_SAMPLE,
    ): FlacHeaderFixup =
        FlacHeaderFixup { bytes -> fix(bytes, samplesOf(pcm, channelCount, bytesPerSample)) }

    /**
     * 回填 + **自检**。自检不通过 = [FlacHeaderFix.Failed]，调用方**整段退回 WAV**。
     *
     * 自检四条（缺一条就不敢把它当 FLAC 发出去）：
     *  1. 回填后**重新解析** STREAMINFO，`total_samples` 必须**一字不差**等于要写的数；
     *  2. 字节数**一个都没变**（"原地覆盖"的强不变式 —— 变了说明写越界了）；
     *  3. `fLaC` 魔数还在（头没被踩坏）；
     *  4. 采样率/声道/位深/MD5/帧长/块长这些**别人家的字段**一个都没动
     *     （写 `total_samples` 时踩到它们 = 头被我们改坏了）。
     *
     * ⚠️ 失败时字节可能已经被改过一半（写与自检之间）—— 没关系：调用方拿到
     * [FlacHeaderFix.Failed] 之后用的是**另外**包出来的 WAV，这份字节会被丢掉。
     */
    fun fix(bytes: ByteArray, totalSamples: Long?): FlacHeaderFix {
        // 负数/null 都是"这个数根本算错了"，与"这一段没有样本"是两件事：原因不许混
        if (totalSamples == null || totalSamples < 0) return FlacHeaderFix.Failed(NOTE_CANNOT_COUNT)
        // 0 在规范里是"未知"，写它等于什么都没修（这正是要根治的那一档）
        if (totalSamples == 0L) return FlacHeaderFix.Failed(NOTE_EMPTY)
        if (totalSamples > FlacStreamInfo.MAX_TOTAL_SAMPLES) {
            return FlacHeaderFix.Failed(NOTE_TOO_MANY)
        }
        val before = FlacStreamInfo.parse(bytes) ?: return FlacHeaderFix.Failed(NOTE_NO_HEADER)
        val lengthBefore = bytes.size
        if (!FlacStreamInfo.rewriteTotalSamplesInPlace(bytes, totalSamples)) {
            return FlacHeaderFix.Failed(NOTE_NO_HEADER)
        }
        val after = FlacStreamInfo.parse(bytes) ?: return FlacHeaderFix.Failed(NOTE_SELF_CHECK)
        if (after.totalSamples != totalSamples) return FlacHeaderFix.Failed(NOTE_SELF_CHECK)
        if (bytes.size != lengthBefore) return FlacHeaderFix.Failed(NOTE_SELF_CHECK)
        if (!looksLikeFlac(bytes)) return FlacHeaderFix.Failed(NOTE_SELF_CHECK)
        if (after.sampleRate != before.sampleRate ||
            after.channels != before.channels ||
            after.bitsPerSample != before.bitsPerSample ||
            after.md5Hex != before.md5Hex ||
            after.minBlockSize != before.minBlockSize ||
            after.maxBlockSize != before.maxBlockSize ||
            after.minFrameSize != before.minFrameSize ||
            after.maxFrameSize != before.maxFrameSize
        ) {
            return FlacHeaderFix.Failed(NOTE_SELF_CHECK)
        }
        return FlacHeaderFix.Ok(bytes)
    }
}
