package com.echo.contact.audio

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import java.io.ByteArrayOutputStream

/**
 * **一段会议音频的格式**（用户 2026-10-17 拍板：段改成 FLAC —— 无损、体积减半）。
 *
 * ## 为什么格式必须是一个"被如实传递的事实"，而不是一个常量
 *
 * 这条路只有一份契约：**服务端按 `fLaC` 魔数识别 FLAC**
 * （两边同一条契约：客户端发 `Content-Type: audio/flac`，字节里也必须真的有那个魔数）。
 * 而 FLAC 编码器是**设备能力** —— `MediaCodec` 的 `audio/flac` 从 API 24 起才有，
 * 真机上还可能被厂商裁掉或编码失败。于是"这一段到底是什么格式"只能由**封段那一刻**
 * 回答，不能由"我们想要什么格式"回答：
 *
 *  * 想编 FLAC 但编不出来 → **这一段退回 WAV**（[WAV]）；
 *  * 清单里记下**真的**那一份（[com.echo.contact.service.DraftSegment.format]）；
 *  * 上传时 `Content-Type` 用**那一份**（[contentType]）。
 *
 * 两条铁律（用户原话）：
 *  1. **绝不丢音频**：编不出来就退回 WAV —— WAV 是纯字节拼接，恒可用，不需要任何系统能力；
 *  2. **绝不谎报格式**：清单里那份格式就是真的那份；宁可整场都是 WAV，也不许把
 *     "其实是 PCM 的字节"说成 FLAC（服务端按魔数识别，谎报只会当场露馅、还把音频废掉）。
 *
 * 权限不变：编码只写 app 私有目录，**不需要任何新权限**。
 */
enum class SegmentFormat(
    /** 清单里记的那个字符串（小写，稳定 —— 改它等于换契约）。 */
    val id: String,
    /** 文件后缀（`seg-0001.flac` / `seg-0001.wav`）。 */
    val extension: String,
    /** 上传时的 `Content-Type`（服务端按它 + 魔数认格式）。 */
    val contentType: String,
) {
    /** 无损压缩。默认档 —— 同样的音频只占一半体积（16 kHz 单声道语音实测约 50%）。 */
    FLAC("flac", "flac", "audio/flac"),

    /** 退路：裸 PCM 外面套 44 字节 WAV 头。**恒可用**（见 [SegmentEncoding.WAV_FALLBACK]）。 */
    WAV("wav", "wav", "audio/wav");

    companion object {
        /**
         * 清单里**没有**格式字段时的默认值。
         *
         * 为什么默认是 WAV 而不是 FLAC：这一版之前只写 WAV，所以老清单（来自旧版本 App、
         * 或写了一半掉电的那一份）里的音频**确实**是 WAV。默认成 FLAC 会让补传拿
         * `audio/flac` 去发一堆 PCM 字节 —— 那正是"谎报格式"，而且服务端会拒。
         */
        val DEFAULT = WAV

        /** 清单里的字符串 → 格式（认不出来按 [DEFAULT]，**不抛**：补传是后台动作）。 */
        fun ofId(raw: String?): SegmentFormat {
            val s = raw?.trim()?.lowercase().orEmpty()
            return entries.firstOrNull { it.id == s } ?: DEFAULT
        }
    }
}

/**
 * FLAC 流的**魔数**（`fLaC`，4 字节，位于流的最前面）。
 *
 * 为什么客户端也要判它：服务端就是按它识别的（两边同一条契约）。若编码器给回来的字节
 * 里没有这个魔数（真机上确实可能：某些实现只吐裸帧），那串字节在服务端眼里**不是 FLAC**
 * —— 与其把它标成 FLAC 发出去（谎报 + 白传一遍），不如**就地退回 WAV**。
 * 这条判据的代价只是"这一段没省下体积"，收益是"清单里那份格式永远是真的"。
 */
fun looksLikeFlac(bytes: ByteArray): Boolean =
    bytes.size >= 4 &&
        bytes[0] == 'f'.code.toByte() && bytes[1] == 'L'.code.toByte() &&
        bytes[2] == 'a'.code.toByte() && bytes[3] == 'C'.code.toByte()

/**
 * 编码结果：**真的**产出的字节 + **真的**格式 + 编不出 FLAC 时那句如实的话。
 *
 * [note] 只会在"退回了 WAV"时非空（[SegmentEncoding] 拼的），调用方要把它显示出来
 * —— 用户点名要求"在界面/日志如实体现'这段用 WAV（FLAC 编码器不可用）'"。
 */
class EncodedAudio(
    val bytes: ByteArray,
    val format: SegmentFormat,
    val note: String = "",
) {
    val fallback: Boolean get() = format == SegmentFormat.WAV && note.isNotBlank()
}

/**
 * 「一段 PCM → 一种格式的字节」。
 *
 * 它是**端口**（和 `OfflineTransport` 同一个套路）：真实现走 Android 的 `MediaCodec`
 * （[FlacSegmentEncoder]），而"编不出来该怎么办"这条判定在 [SegmentEncoding] 里，
 * 是**纯函数**—— 于是"退回 WAV 且如实记格式"这件事能在 JVM 里逐条钉住，不用真机。
 */
interface SegmentAudioEncoder {
    /** 这一段的字节与格式。**不许抛**（抛 = 这段音频没了；实现必须自己兜住）。 */
    fun encode(pcm: ByteArray): EncodedAudio
}

/**
 * 「试编 FLAC，编不出来**退回 WAV**」这条判定的**唯一**实现（纯函数，JVM 逐条可测）。
 *
 * 为什么把 flac/wav 两个动作做成参数（而不是直接 new 一个 MediaCodec）：
 * 这条判定的错法全是**静默**的 —— 退回了 WAV 却记成 FLAC、或者编码抛异常时整段丢掉。
 * 参数化之后，这两件事都能在 JVM 里用"编不出来"的假编码器当场钉住。
 *
 * 四种"编不出来"都走同一条退路（**绝不丢音频**）：
 *  1. 设备没有这个编码器（`createEncoderByType` 抛 / API < 24）；
 *  2. 编码过程出错（`configure`/`start`/`dequeue*` 抛）；
 *  3. 编回来了，但字节里**没有 `fLaC` 魔数**（服务端按魔数认，那种字节在它眼里不是 FLAC）；
 *  4. 编回来了、也有魔数，但 **STREAMINFO 里的总样本数回填不了**（[fixup] 失败 —— 见
 *     [FlacStreamInfo]：`total_samples = 0` 会让 libsndfile 把它读成 2^63-1、seek 崩，
 *     而**发一个 total_samples=0 的 FLAC 出去，等于把一段服务端解不出来的音频交上去**）。
 */
object SegmentEncoding {

    /** 这句是用户点名要看到的那半句（"这段用 WAV（**FLAC 编码器不可用**）"）。 */
    const val FLAC_UNAVAILABLE = "FLAC 编码器不可用"

    /** 编回来了但没有魔数（第 3 种）：说清是"不认"，不是"没有"。 */
    const val FLAC_NOT_RECOGNIZED = "FLAC 编码器给的字节里没有 fLaC 魔数"

    /** 回填失败（第 4 种）：**这是一句精确的话**，别用"编码器不可用"盖掉它。 */
    const val FLAC_HEADER_NOT_FIXED =
        "FLAC 头的总样本数回填不了（不敢发 total_samples=0 的段）"

    fun encode(
        /** 试编 FLAC：返回 null 或抛异常都算"这台设备编不出来"。 */
        flac: () -> ByteArray?,
        /** 兜底 WAV（恒可用）。 */
        wav: () -> ByteArray,
        /**
         * **封段收尾**：把 STREAMINFO 的 `total_samples` 回填成真的（见 [FlacHeaderBackfill]）。
         *
         * 默认 [FlacHeaderFixup.NONE]（不改字节）只给用例里那些"只验判定"的假 FLAC 用；
         * **生产路径必须**传 [FlacHeaderBackfill.forPcm] —— 真编码器（`FlacSegmentEncoder`）
         * 走的是 [encodeFlacSegment]，那里已经接好了。
         */
        fixup: FlacHeaderFixup = FlacHeaderFixup.NONE,
    ): EncodedAudio {
        val bytes = try {
            flac()
        } catch (e: Exception) {
            // 编码器抛异常 = 这一档不可用。**不许**让它冒出去：冒出去的表现是"这段音频没了"
            null
        }
        if (bytes == null) {
            return EncodedAudio(wav(), SegmentFormat.WAV, note = FLAC_UNAVAILABLE)
        }
        if (!looksLikeFlac(bytes)) {
            return EncodedAudio(wav(), SegmentFormat.WAV, note = FLAC_NOT_RECOGNIZED)
        }
        // 魔数有了还不够：头里那个长度必须是真的（回填失败就不许把它当 FLAC 发出去）。
        // fixup 自己也**不许**抛（抛出去 = 这段音频没了），所以这一层再兜一次。
        val fixed = try {
            fixup.fix(bytes)
        } catch (e: Exception) {
            FlacHeaderFix.Failed(FLAC_HEADER_NOT_FIXED)
        }
        return when (fixed) {
            is FlacHeaderFix.Ok -> EncodedAudio(fixed.bytes, SegmentFormat.FLAC)
            is FlacHeaderFix.Failed -> EncodedAudio(
                bytes = wav(),
                format = SegmentFormat.WAV,
                note = fixed.note.ifBlank { FLAC_HEADER_NOT_FIXED },
            )
        }
    }
}

/**
 * **生产路径的封段一步**（`MediaCodec` 之外的整条判定，JVM 里能把整条跑完）：
 * 试编 FLAC → **回填 STREAMINFO 的 `total_samples`** → 判魔数/自检 → 不行就整段退回 WAV。
 *
 * 为什么把它从 [FlacSegmentEncoder] 里拆出来：`MediaCodec` 在 JVM 单测里是桩，
 * 拆开之后"回填 → 自检 → 失败退回 WAV"这一整条能在 JVM 里用一份**假的原始 FLAC**
 * 逐条钉住（`MeetingSegmentAudioTest`），而 [FlacSegmentEncoder] 只剩
 * "用 MediaCodec 把 PCM 编成原始字节"这一件事。
 *
 * [raw] 返回编码器**原样吐出来**的字节 —— 真机上它的 `total_samples` 就是 0
 * （原因见 [FlacStreamInfo]），回填在这一层做，**原地覆盖、长度不变**。
 */
internal fun encodeFlacSegment(
    pcm: ByteArray,
    sampleRate: Int,
    channelCount: Int,
    raw: () -> ByteArray?,
): EncodedAudio = SegmentEncoding.encode(
    flac = raw,
    wav = { WavWriter.wrap(pcm, sampleRate, channelCount, 16) },
    // 总样本数从**真的 PCM 字节数**算（16 kHz 单声道 16 bit → pcm.size / 2），
    // 不是拿设置里的段长乘出来的：尾段是不足一段的，按段长算会写出一个比真实音频长的头
    fixup = FlacHeaderBackfill.forPcm(pcm, channelCount),
)

/** 只用 WAV 的编码器（用例与"想关掉 FLAC"的场合；生产走 [FlacSegmentEncoder]）。 */
class WavSegmentEncoder(
    private val sampleRate: Int = 16_000,
    private val channelCount: Int = 1,
    private val bitsPerSample: Int = 16,
) : SegmentAudioEncoder {
    override fun encode(pcm: ByteArray): EncodedAudio = EncodedAudio(
        WavWriter.wrap(pcm, sampleRate, channelCount, bitsPerSample),
        SegmentFormat.WAV,
        note = SegmentEncoding.FLAC_UNAVAILABLE,
    )
}

/**
 * **真**编码器：`MediaCodec` 的 `audio/flac`（API 24+ 可用），编不出来当场退回 WAV。
 *
 * 口径与现在这条路一致：**16 kHz / 单声道 / 16 bit**（与 `WavWriter` 那份 PCM 完全相同，
 * 只是换了个容器）。所以退回 WAV 时两边的音频内容逐字节一致 —— 退回这件事**不影响音质**。
 *
 * ⚠️ **真机行为（2026-10-17 已由 PC 侧取证定格，这里如实写下来）**：`MediaCodec`
 * 吐出来的字节**带** `fLaC` 魔数，但它的 STREAMINFO 里 **`total_samples = 0`、MD5 全零、
 * min/max framesize = 0** —— 因为**写头的那一刻它还不知道这一段总共有多少样本**。
 * 规范里 `total_samples = 0` 是"未知"（合法），可 `libsndfile` 把它读成 `SF_COUNT_MAX (2^63-1)`，
 * 于是服务端解码时 `Internal psf_fseek() failed`，整段音频**解不出来**。
 * 所以这里在编完之后**回填真的总样本数**（见 [FlacStreamInfo] / [FlacHeaderBackfill]）：
 * 头是定长的，回填是**原地覆盖**，文件长度一个字节都不变。
 *
 * 四层兜底（四条的**共同后果**都只是"这一段没省下体积"，**绝不会**是"丢音频/谎报格式"）：
 * ① 任何异常 → 退回 WAV；② 超时（编不完）→ 退回 WAV；③ 没有魔数 → 退回 WAV；
 * ④ 有魔数但**总样本数回填不了/自检没过** → 退回 WAV（见 [encodeFlacSegment]）。
 */
class FlacSegmentEncoder(
    private val sampleRate: Int = 16_000,
    private val channelCount: Int = 1,
    /** 压缩级 0..8（5 与常见 FLAC 工具的默认档一致）。设不进去也不影响能不能编。 */
    private val compressionLevel: Int = DEFAULT_COMPRESSION_LEVEL,
) : SegmentAudioEncoder {

    override fun encode(pcm: ByteArray): EncodedAudio =
        encodeFlacSegment(pcm, sampleRate, channelCount) { encodeFlac(pcm) }

    /** 编出 FLAC 字节；**任何**编不出来的情况都回 null（调用方据此退回 WAV）。 */
    private fun encodeFlac(pcm: ByteArray): ByteArray? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.N) return null   // audio/flac 从 API 24 起
        if (pcm.isEmpty()) return null
        var codec: MediaCodec? = null
        return try {
            val c = MediaCodec.createEncoderByType(FLAC_MIME)
            codec = c
            val fmt = MediaFormat.createAudioFormat(FLAC_MIME, sampleRate, channelCount)
            // 压缩级只是"想更小一点"：设不进去（有些设备不认这个键）也照样能编
            runCatching { fmt.setInteger(MediaFormat.KEY_FLAC_COMPRESSION_LEVEL, compressionLevel) }
            c.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.start()

            val out = ByteArrayOutputStream(pcm.size / 2 + 64)
            val info = MediaCodec.BufferInfo()
            val deadline = System.currentTimeMillis() + encodeTimeoutMsFor(pcm.size)
            var fed = 0
            var eosQueued = false
            var finished: ByteArray? = null
            while (finished == null) {
                // 硬超时：采集循环在等这一段（阻塞太久 = 麦克风缓冲被顶掉 = **真的丢音频**），
                // 所以宁可退回 WAV，也绝不在这里无限等一个不听话的编码器
                if (System.currentTimeMillis() > deadline) return null
                if (!eosQueued) {
                    val inIdx = c.dequeueInputBuffer(DEQUEUE_TIMEOUT_US)
                    if (inIdx >= 0) {
                        val buf = c.getInputBuffer(inIdx) ?: return null
                        buf.clear()
                        val chunk = minOf(buf.remaining(), pcm.size - fed)
                        if (chunk > 0) buf.put(pcm, fed, chunk)
                        val last = fed + chunk >= pcm.size
                        c.queueInputBuffer(inIdx, 0, chunk, ptsUsOf(fed),
                            if (last) MediaCodec.BUFFER_FLAG_END_OF_STREAM else 0)
                        fed += chunk
                        if (last) eosQueued = true
                    }
                }
                val outIdx = c.dequeueOutputBuffer(info, DEQUEUE_TIMEOUT_US)
                when {
                    outIdx == MediaCodec.INFO_TRY_AGAIN_LATER -> Unit
                    outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> Unit
                    outIdx >= 0 -> {
                        val ob = c.getOutputBuffer(outIdx)
                        if (ob != null && info.size > 0) {
                            ob.position(info.offset)
                            ob.limit(info.offset + info.size)
                            val b = ByteArray(info.size)
                            ob.get(b)
                            // FLAC 的流头（fLaC + STREAMINFO）是**代码配置**那一块输出缓冲，
                            // 它本来就在这个循环里，一起收下来 → 才有可能带魔数
                            out.write(b)
                        }
                        c.releaseOutputBuffer(outIdx, false)
                        if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                            finished = out.toByteArray()
                        }
                    }
                }
            }
            finished
        } catch (e: Exception) {
            // 没有这个编码器 / 这台设备编不了 / 编码中途出错：都归"这一档不可用"
            null
        } finally {
            if (codec != null) {
                runCatching { codec.stop() }
                runCatching { codec.release() }
            }
        }
    }

    private fun ptsUsOf(byteOffset: Int): Long =
        byteOffset.toLong() * 1_000_000L / (sampleRate.toLong() * channelCount * 2)

    companion object {
        const val FLAC_MIME = "audio/flac"
        const val DEFAULT_COMPRESSION_LEVEL = 5

        /** 编码硬超时的**底**（毫秒）。按输入大小再加（见 [encodeTimeoutMsFor]）。 */
        const val ENCODE_TIMEOUT_MS = 10_000L

        private const val DEQUEUE_TIMEOUT_US = 10_000L
    }
}

/**
 * 一段编码最多等多久（毫秒），**跟着输入数据量走**。
 *
 * 旧的固定 10 秒是按 30 秒段标定的（≈1 MB）。段长默认改成 600 秒后输入大 20 倍，
 * 固定 10 秒会把"还没编完"误判成"编不出来" → 无谓退回 WAV（10 分钟 ≈19 MB，
 * 走中继时超过 12 MB 上限就传不上去）。所以预算 = 10 秒底 + 每 MB 1 秒，封顶 90 秒
 * （真卡死也不能让封段线一直挂着）。短段的行为与旧版一致（10 秒底）。
 */
internal fun encodeTimeoutMsFor(pcmBytes: Int): Long {
    if (pcmBytes <= 0) return FlacSegmentEncoder.ENCODE_TIMEOUT_MS
    val mb = (pcmBytes + 1_048_575) / 1_048_576   // 向上取整到 MB
    return (FlacSegmentEncoder.ENCODE_TIMEOUT_MS + mb * 1_000L).coerceAtMost(90_000L)
}
