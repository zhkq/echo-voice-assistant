package com.echo.contact.audio

import kotlin.math.sqrt

/**
 * 「说一句话 → 静音收尾」的分段状态机（**纯 Kotlin，不碰任何 Android API**）。
 *
 * 为什么要单独抽出来
 * ==================
 * 常驻听写（后台/锁屏录音）里最容易错的就是**什么时候算一句说完了**：
 *   * 判早了 → 一句话被切成两半，后端收到半句、答非所问；
 *   * 判晚了 → 两句并成一句，前一句的答复要等到第二句说完才来。
 * 抽成不依赖 Android 的纯逻辑之后，就能用 JVM 单测逐帧喂数据把它钉住
 * （`VoiceSegmenterTest`），不用真机、也不用真麦克风。
 *
 * 口径与 ECHO 侧一致（`app/audio/recorder.py` 的 `silence_threshold` /
 * `dailyReviewSilenceMs`）：
 *   * 门限用 **RMS**（默认 0.012，与面板那一项同口径），不是"有没有非零样本"；
 *   * 说完之后静音 **hangover**（默认 1200 ms）才收尾；
 *   * 一段太短（有声音的时长 < `minSpeechMs`）当噪声**丢弃**，不发给后端白跑一趟；
 *   * 说太久（> `maxSegmentMs`）**强制切段**，免得一句话把内存与上传都撑爆。
 *
 * 前导静音**不进片段**：只保留 `preRollMs`（默认 400 ms）的"预卷"。
 * 不这么做的话，用户打开开关之后干等的十几秒会原样进 wav（后端白转写一大段静音），
 * 而预卷又能保证**第一个字不被切掉**（音节起始常常比门限晚一两帧）。
 */
class VoiceSegmenter(
    private val sampleRate: Int = DEFAULT_SAMPLE_RATE,
    private val silenceThreshold: Double = DEFAULT_SILENCE_THRESHOLD,
    private val hangoverMs: Int = DEFAULT_HANGOVER_MS,
    private val minSpeechMs: Int = DEFAULT_MIN_SPEECH_MS,
    private val maxSegmentMs: Int = DEFAULT_MAX_SEGMENT_MS,
    private val preRollMs: Int = DEFAULT_PRE_ROLL_MS,
) {

    enum class State {
        /** 还没听到人说话（在攒预卷）。 */
        IDLE,

        /** 有人在说。 */
        SPEAKING,
    }

    /** 当前状态（界面/通知用它显示"在听 / 在说"）。 */
    var state: State = State.IDLE
        private set

    /** 最近一帧的 RMS（0..1）——给界面/通知画电平，不做判定用。 */
    var level: Double = 0.0
        private set

    /** 当前这段"有声音"的时长（毫秒）；IDLE 时是 0。 */
    var speechMs: Int = 0
        private set

    private val preRoll = ArrayDeque<ShortArray>()
    private var preRollSamples = 0
    private val preRollCap = sampleRate * preRollMs / 1000

    private var segment = ArrayList<Short>()
    private var segmentSpeechSamples = 0
    private var silenceRunSamples = 0

    /** 丢掉手上的一切（服务停止/重启时调）。 */
    fun reset() {
        state = State.IDLE
        level = 0.0
        speechMs = 0
        preRoll.clear()
        preRollSamples = 0
        segment = ArrayList()
        segmentSpeechSamples = 0
        silenceRunSamples = 0
    }

    /**
     * 喂一帧 PCM（[count] 是有效样本数；帧长由调用方决定，判据全按毫秒算）。
     *
     * 返回**这一段收尾出来的裸 PCM 字节**（没有就返回 null）；调用方负责套 wav 头。
     */
    fun feed(samples: ShortArray, count: Int): ByteArray? {
        if (count <= 0 || count > samples.size) return null
        level = rmsOf(samples, count)
        val voiced = level >= silenceThreshold

        if (voiced) {
            if (state == State.IDLE) {
                // 说话开始：预卷 + 这一帧一起成为这段的开头（保证第一个字不被切掉）
                state = State.SPEAKING
                segment = ArrayList(preRollSamples + count)
                for (frame in preRoll) for (i in frame.indices) segment.add(frame[i])
                preRoll.clear()
                preRollSamples = 0
                silenceRunSamples = 0
                segmentSpeechSamples = 0
            }
            for (i in 0 until count) segment.add(samples[i])
            segmentSpeechSamples += count
            speechMs = segmentSpeechSamples * 1000 / sampleRate
            silenceRunSamples = 0
            // 说太久：强制切（下一段从静音之后的预卷重新开始）
            return if (speechMs >= maxSegmentMs) cut() else null
        }

        if (state == State.SPEAKING) {
            // 尾巴那点静音也留着：听起来更自然，后端也不在乎多这一两百毫秒
            for (i in 0 until count) segment.add(samples[i])
            silenceRunSamples += count
            val silenceMs = silenceRunSamples * 1000 / sampleRate
            return if (silenceMs >= hangoverMs) cut() else null
        }

        // IDLE 里的静音：只维护预卷
        pushPreRoll(samples, count)
        return null
    }

    private fun cut(): ByteArray? {
        val data = segment
        val speech = segmentSpeechSamples
        state = State.IDLE
        speechMs = 0
        segment = ArrayList()
        segmentSpeechSamples = 0
        silenceRunSamples = 0
        preRoll.clear()
        preRollSamples = 0
        if (speech * 1000 / sampleRate < minSpeechMs) return null   // 太短 = 噪声
        return pcmToBytes(data)
    }

    private fun pushPreRoll(samples: ShortArray, count: Int) {
        preRoll.addLast(samples.copyOf(count))
        preRollSamples += count
        while (preRollSamples > preRollCap && preRoll.isNotEmpty()) {
            preRollSamples -= preRoll.removeFirst().size
        }
    }

    private fun rmsOf(samples: ShortArray, count: Int): Double {
        var sum = 0.0
        for (i in 0 until count) {
            val v = samples[i] / 32768.0
            sum += v * v
        }
        return sqrt(sum / count)
    }

    companion object {
        const val DEFAULT_SAMPLE_RATE = 16000
        const val DEFAULT_SILENCE_THRESHOLD = 0.012
        const val DEFAULT_HANGOVER_MS = 1200
        const val DEFAULT_MIN_SPEECH_MS = 300
        const val DEFAULT_MAX_SEGMENT_MS = 15000
        const val DEFAULT_PRE_ROLL_MS = 400

        /** 16-bit 小端（`AudioRecord` 给的就是本机字节序，ECHO 的 `WavWriter` 也认小端）。 */
        fun pcmToBytes(pcm: List<Short>): ByteArray {
            val out = ByteArray(pcm.size * 2)
            for (i in pcm.indices) {
                val v = pcm[i].toInt()
                out[i * 2] = (v and 0xff).toByte()
                out[i * 2 + 1] = ((v shr 8) and 0xff).toByte()
            }
            return out
        }
    }
}
