package com.echo.contact.audio

/**
 * 给裸 PCM 套一个 44 字节的标准 wav 头。
 *
 * 为什么单独抽成一个**不碰任何 Android API** 的纯对象：这是整条语音链路里
 * **最容易静默错**的一环 —— 头里任何一个字段（字节序、chunk 大小、byteRate、
 * blockAlign）写错，表现都不是"报错"，而是**后端说"格式不认识"或干脆没识别出文字**。
 * 抽出来之后就能用 JVM 单测逐字节比对（`WavTest`），不用真机。
 *
 * 布局（小端）：
 * ```
 *  0 "RIFF" | 4  chunkSize=36+len | 8 "WAVE"
 * 12 "fmt " | 16 16 | 20 audioFormat=1(PCM) | 22 channels
 * 24 sampleRate | 28 byteRate | 32 blockAlign | 34 bitsPerSample
 * 36 "data" | 40 dataLen | 44 PCM...
 * ```
 */
object WavWriter {

    /** wav 头固定 44 字节。 */
    const val HEADER_SIZE = 44

    fun wrap(pcm: ByteArray, sampleRate: Int, channels: Int, bitsPerSample: Int): ByteArray {
        val byteRate = sampleRate * channels * bitsPerSample / 8
        val blockAlign = channels * bitsPerSample / 8
        val out = ByteArray(HEADER_SIZE + pcm.size)

        fun putStr(off: Int, s: String) {
            for (i in s.indices) out[off + i] = s[i].code.toByte()
        }

        // 小端：低字节在前
        fun putInt(off: Int, v: Int) {
            out[off] = (v and 0xff).toByte()
            out[off + 1] = ((v shr 8) and 0xff).toByte()
            out[off + 2] = ((v shr 16) and 0xff).toByte()
            out[off + 3] = ((v shr 24) and 0xff).toByte()
        }

        fun putShort(off: Int, v: Int) {
            out[off] = (v and 0xff).toByte()
            out[off + 1] = ((v shr 8) and 0xff).toByte()
        }

        putStr(0, "RIFF")
        putInt(4, 36 + pcm.size)
        putStr(8, "WAVE")
        putStr(12, "fmt ")
        putInt(16, 16)                      // fmt chunk 长度（PCM 固定 16）
        putShort(20, 1)                     // audioFormat = 1 (PCM)
        putShort(22, channels)
        putInt(24, sampleRate)
        putInt(28, byteRate)
        putShort(32, blockAlign)
        putShort(34, bitsPerSample)
        putStr(36, "data")
        putInt(40, pcm.size)
        System.arraycopy(pcm, 0, out, HEADER_SIZE, pcm.size)
        return out
    }
}
