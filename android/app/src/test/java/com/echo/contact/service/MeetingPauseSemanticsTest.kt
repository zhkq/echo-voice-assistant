package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.audio.WavWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * **暂停语义**（用户 2026-10-16 深夜推翻旧口径，并选了"手机与电脑统一"）。
 *
 * 用户原话：*"暂停区间就是空白录音，不影响会议总时长，不用剃掉"*。落地成三条判据
 * （与电脑侧 `tests/test_meeting_pause.py` 同一组判据）：
 *
 *  1. **时长**：一场里暂停 N 秒，**音频总时长 ≈ 挂钟时长**（不多不少），而且段数与不暂停时
 *     按**同一刻度**（"第 N 段 = 挂钟第 N 个段长"）；
 *  2. **内容是零**：暂停区间对应的样本**全为零**，而**前后仍是真音频**（别把整段写零）；
 *  3. **段边界**：含暂停时，第 N 段覆盖的仍是挂钟的第 N 个段长区间。
 *
 * 为什么这组必须跑**端到端**（采集 → 封段 → 编码 → 落盘）而不是只测纯函数：
 * 纯函数对了、但 `runLoop` 里少写一次 `seal` 或忘了把零帧计入缓冲，端到端就仍然会短 ——
 * 而"手机那场的时间轴在暂停期间变短"正是这一轮要修的那个 bug（旧代码在暂停时 `continue`）。
 */
class MeetingPauseSemanticsTest {

    @get:Rule val tmp = TemporaryFolder()

    private val rate = 16_000
    private val frameSamples = 1_600          // 100ms，与 `MeetingMicService.FRAME_SAMPLES` 一致
    private val segmentSamples = 16_000       // 1 秒一段（真机默认 30 秒，这里只把刻度调小）
    private val bytesPerSample = 2            // PCM 16-bit
    private val startIso = "2026-10-18T09:30:00"
    private val startMs: Long = MeetingDraftStore.epochOf(startIso) ?: error("用例前置：开始时间要能解析")

    /**
     * 一个"无损"的假编码器：把喂进来的 PCM 原样记下来（外面套个 WAV 头好让文件名/格式都对），
     * 于是用例能在字节层面直接验"**暂停区间对应的样本是不是全零**"。
     * 真机走的是 `FlacSegmentEncoder`（无损），这段语义与编码器无关。
     */
    private class FakePcmEncoder : SegmentAudioEncoder {
        val frames = ArrayList<ByteArray>()

        override fun encode(pcm: ByteArray): EncodedAudio {
            frames.add(pcm.copyOf())
            return EncodedAudio(WavWriter.wrap(pcm, 16_000, 1, 16), SegmentFormat.WAV)
        }
    }

    /** 采集循环的一次迭代：喂一帧 → 够一段就封（**与 `MeetingMicService.runLoop` 同一形状**）。 */
    private class Driver(
        private val session: MeetingRecordSession,
        private val samplesPerSegment: Int,
    ) {
        private val buffer = ArrayList<Short>(samplesPerSegment)
        private val capture = MicCaptureLoop(samplesPerSegment)

        /** 已经封出去的段（按顺序）—— 用来验时间轴与段边界。 */
        val sealed = ArrayList<DraftSegment>()

        fun feed(frame: ShortArray, n: Int, paused: Boolean) {
            if (capture.feed(buffer, frame, n, active = !paused).sealed) {
                val seal = session.seal(toPcmBytes(buffer))
                val d = seal.draft
                if (d != null) sealed.add(d.segments.last())
                buffer.clear()
            }
        }

        /** 录完了（走 `MeetingRecordSession.finish`；`session` 与 `store` 都是同一次运行的那份）。 */
        fun finish(): MeetingDraft? = session.finish()
    }

    /**
     * 一场录音：`totalFrames` 帧，其中第 [pauseFromFrame] 帧到第 [pauseToFrame] 帧（**不含**）
     * 是暂停 —— 与 `runLoop` 一样，暂停时喂进去的是**真实帧（长度一样）**，
     * 由 `MicCaptureLoop` 决定写零。
     *
     * 时钟每帧走一个帧长（100ms）——真机就是"100ms 采一帧"，所以挂钟与样本数**同刻度**。
     */
    private class Scenario(val driver: Driver, val clientId: String, val wallSeconds: Double)

    private fun recordScenario(
        store: MeetingDraftStore,
        encoder: FakePcmEncoder,
        totalFrames: Int,
        pauseFromFrame: Int = -1,
        pauseToFrame: Int = -1,
        clientId: String = "phone-20261018093000-abcd",
    ): Scenario {
        var now = startMs
        val session = MeetingRecordSession(
            store = store,
            encoder = encoder,
            sampleRate = rate,
            channelCount = 1,
            clock = { now },
        )
        session.start(clientId, startIso)
        val driver = Driver(session, segmentSamples)
        try {
            for (k in 0 until totalFrames) {
                // ① 暂停/恢复**先记**（在挂钟刻度上）：用户按下的那一刻就是一个确定的时刻，
                //    与"这一帧采完了没有"无关（真机上它是异步的，一个动作就落一个时间点）。
                val paused = k in pauseFromFrame until pauseToFrame
                if (paused) {
                    if (!session.paused) session.beginPause()
                } else {
                    if (session.paused) session.endPause()
                }
                // ② 再走一个帧长：真机上 `ar.read` 阻塞 100ms，所以**封段那一刻**
                //    （`seal()` 里读的时钟）与段末在同一个刻度上 —— 段边界才不会整场偏 100ms。
                now += frameSamples * 1_000L / rate
                // ③ 这一帧：暂停时内容变零，但它照样计数、照样占这 100ms 的挂钟
                driver.feed(ShortArray(frameSamples) { 1_000 }, frameSamples, paused)
            }
        } finally {
            ActiveDraft.clear()
        }
        return Scenario(driver, clientId, (now - startMs) / 1000.0)
    }

    /**
     * 暂停区间在**字节**里的窗口：第 2 秒到第 4 秒。
     *
     * ⚠️ 这里必须乘 [bytesPerSample]（一个样本 2 字节）—— 我第一版写成"样本数"直接当字节用，
     * 于是窗口整体前移一半，报出"窗口里第 16000 个样本非零"这种**看着像生产代码有问题**的假红。
     */
    private fun silenceWindowBytes(): IntRange {
        val from = rate * 2 * bytesPerSample      // 第 2 秒起
        val to = rate * 4 * bytesPerSample        // 到第 4 秒（不含）
        return from until to
    }

    private fun pcmOf(encoder: FakePcmEncoder): ByteArray {
        val out = ByteArray(encoder.frames.sumOf { it.size })
        var at = 0
        encoder.frames.forEach { f ->
            System.arraycopy(f, 0, out, at, f.size)
            at += f.size
        }
        return out
    }

    private fun shortAt(pcm: ByteArray, byteOffset: Int): Short =
        ByteBuffer.wrap(pcm, byteOffset, 2).order(ByteOrder.LITTLE_ENDIAN).short

    /** [fromByte, toByte) 这一段字节里的样本是不是**全是零**（空白）。 */
    private fun pcmRangeIsSilent(pcm: ByteArray, fromByte: Int, toByte: Int): Boolean {
        var i = fromByte
        while (i < toByte) {
            if (shortAt(pcm, i) != 0.toShort()) return false
            i += 2
        }
        return true
    }

    /** [fromByte, toByte) 这一段字节里**有没有**非零样本（= 有真音频）。 */
    private fun pcmRangeHasAudio(pcm: ByteArray, fromByte: Int, toByte: Int): Boolean {
        var i = fromByte
        while (i < toByte) {
            if (shortAt(pcm, i) != 0.toShort()) return true
            i += 2
        }
        return false
    }

    // ------------------------------------------------------------------ ① 时长：不多不少

    @Test
    fun `暂停不缩短时长：音频总时长等于挂钟时长`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val encoder = FakePcmEncoder()
        val totalFrames = 60                        // 6 秒
        val pauseFrom = 20                          // 第 2 秒起暂停
        val pauseTo = 40                            // 第 4 秒恢复

        val run = recordScenario(store, encoder, totalFrames, pauseFrom, pauseTo)
        val driver = run.driver

        assertEquals("60 帧 × 100ms = 6 秒挂钟", 6.0, run.wallSeconds, 1e-9)
        assertEquals("暂停 2 秒，音频样本一个都不许少（旧的'丢帧'会让这一段整体缩短）",
            6.0, driver.sealed.sumOf { it.durationSeconds }, 1e-6)
        assertEquals("一共 6 段", 6, driver.sealed.size)
        assertEquals("段长仍是同一个刻度", 1.0, driver.sealed.first().durationSeconds, 1e-9)
        val draft = store.load(run.clientId)!!
        assertEquals("清单里的暂停区间只有一条，且落盘了", 1, draft.pauses.size)
        assertEquals(2.0, draft.pauses.single().from, 1e-6)
        assertEquals(4.0, draft.pauses.single().to, 1e-6)
        ActiveDraft.clear()
    }

    @Test
    fun `有没有暂停，段数按同一刻度`() {
        val plain = FakePcmEncoder()
        val paused = FakePcmEncoder()
        val d1 = recordScenario(MeetingDraftStore(tmp.newFolder("plain")), plain, 60).driver
        val d2 = recordScenario(
            MeetingDraftStore(tmp.newFolder("paused")), paused, 60, 20, 40).driver

        assertEquals("同样 6 秒挂钟：不暂停", 6, d1.sealed.size)
        assertEquals("暂停中照样封段 —— 段数必须与不暂停时**同一个刻度**", d1.sealed.size, d2.sealed.size)
        assertEquals(d1.sealed.map { it.seq }, d2.sealed.map { it.seq })
        ActiveDraft.clear()
    }

    // ------------------------------------------------------------------ ② 内容是零、前后是真音频

    /**
     * 这一场：第 2 秒按暂停、第 4 秒恢复 —— 两个时刻都正好落在段边界上，
     * 于是暂停 = **两个整段**（第 3、4 段全零），零帧只出现在暂停区间内。
     *
     * ⚠️ 我第一版把"过渡段"写成第 4 段（以为暂停一个段长），于是拿"整段全零的第 4 段"
     * 去断言"后半段有真音频" → 一条**假红**（生产代码与磁盘上的音频当时就是对的）。
     * 段是 1 秒一段，所以"暂停 2 秒 = 暂停两个整段"必须先算清楚再写断言。
     * 恢复点落在段中间的那种场由下一条用例覆盖（那才是真正的过渡段）。
     */
    @Test
    fun `暂停区间的样本全为零而前后仍是真音频`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val encoder = FakePcmEncoder()
        val driver = recordScenario(store, encoder, 60, 20, 40).driver
        val pcm = pcmOf(encoder)
        val window = silenceWindowBytes()
        val perSegment = segmentSamples * bytesPerSample

        assertEquals("6 秒 × 16kHz × 2 字节", 6 * rate * bytesPerSample, pcm.size)
        assertTrue("暂停那 2 秒（第 2~4 秒）必须全是零样本",
            pcmRangeIsSilent(pcm, window.first, window.last + 1))
        assertTrue("暂停**之前**必须还是真音频（别把整段写零）",
            pcmRangeHasAudio(pcm, 0, window.first))
        assertTrue("暂停**之后**必须还是真音频（恢复就要接着录真的）",
            pcmRangeHasAudio(pcm, window.last + 1, pcm.size))

        // 逐段看（每段 1 秒）：第 1、2 段全真；第 3、4 段整段落在暂停里（全零）；第 5、6 段全真
        assertTrue("第 1 段", pcmRangeHasAudio(pcm, 0, perSegment))
        assertTrue("第 2 段", pcmRangeHasAudio(pcm, perSegment, perSegment * 2))
        assertTrue("第 3 段整段都在暂停里，必须是全零",
            pcmRangeIsSilent(pcm, perSegment * 2, perSegment * 3))
        assertTrue("第 4 段也整段在暂停里（暂停 2 秒 = 两个整段），必须是全零",
            pcmRangeIsSilent(pcm, perSegment * 3, perSegment * 4))
        assertTrue("第 5 段已经恢复，必须是真音频（零帧只出现在暂停区间内）",
            pcmRangeHasAudio(pcm, perSegment * 4, perSegment * 4 + perSegment / 2))
        assertEquals("六段都要真的封出来（含暂停那两段）", 6, driver.sealed.size)
    }

    /**
     * 恢复点落在**段中间**的那种场（用户任意时刻按恢复，段边界不跟着动）：
     * 第 2.5 秒暂停、第 4.5 秒恢复 → 第 3 段全是零、第 4 段前一半零后一半真、
     * 第 5 段前一半零后一半真。这条挡的是"把过渡段整段写零"或"整段写真"两种错法。
     */
    @Test
    fun `恢复点落在段中间时那一段前一半零后一半真`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val encoder = FakePcmEncoder()
        // 第 2.5 秒 = 第 25 帧；第 4.5 秒 = 第 45 帧
        val driver = recordScenario(store, encoder, 60, 25, 45).driver
        val pcm = pcmOf(encoder)
        val perSegment = segmentSamples * bytesPerSample

        assertTrue("第 3 段（2s~3s）：前半真、后半零",
            pcmRangeHasAudio(pcm, perSegment * 2, perSegment * 2 + perSegment / 2))
        assertTrue("第 3 段后半段（2.5s 起）是暂停，必须是零",
            pcmRangeIsSilent(pcm, perSegment * 2 + perSegment / 2, perSegment * 3))
        assertTrue("第 4 段（3s~4s）整段都在暂停里",
            pcmRangeIsSilent(pcm, perSegment * 3, perSegment * 4))
        assertTrue("第 5 段前半段（4s~4.5s）还在暂停，必须是零",
            pcmRangeIsSilent(pcm, perSegment * 4, perSegment * 4 + perSegment / 2))
        assertTrue("第 5 段后半段（4.5s 起）已经恢复，必须是真音频",
            pcmRangeHasAudio(pcm, perSegment * 4 + perSegment / 2, perSegment * 5))
        assertEquals("暂停不改变段数", 6, driver.sealed.size)
        ActiveDraft.clear()
    }

    // ------------------------------------------------------------------ ③ 段边界钉在挂钟刻度上

    @Test
    fun `含暂停时第 N 段覆盖的仍是挂钟的第 N 个段长区间`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val encoder = FakePcmEncoder()
        val run = recordScenario(store, encoder, 60, 20, 40)

        // 段是按累计样本数切的，所以第 N 段的起点必然是 (N-1) × 段长、终点必然是 N × 段长
        // —— 这正是电脑侧那条判据（"第 N 段 = 挂钟第 N 个 10 分钟"），只是刻度不同
        run.driver.sealed.forEachIndexed { i, seg ->
            assertEquals("第 ${i + 1} 段的起点", i.toDouble(), seg.startOffsetSeconds, 1e-6)
            assertEquals("第 ${i + 1} 段的终点", (i + 1).toDouble(),
                seg.startOffsetSeconds + seg.durationSeconds, 1e-6)
        }
        assertEquals("第 3 段（暂停里那段）仍要落在 2s~3s",
            2.0, run.driver.sealed[2].startOffsetSeconds, 1e-6)

        // 录完：总时长仍与挂钟一致（6 秒），音频时长不多不少
        val done = run.driver.finish()!!
        assertEquals("清单总时长 = 挂钟（6 秒）", 6.0, done.durationSeconds, 1e-6)
        assertEquals("清单总时长 = 音频总时长", 6.0, run.driver.sealed.sumOf { it.durationSeconds }, 1e-6)
        assertFalse("结束之后就不再是'还在录'", done.recording)
        assertEquals("落盘的也是 6 秒", 6.0, store.load(run.clientId)!!.durationSeconds, 1e-6)
        ActiveDraft.clear()
    }

    // ------------------------------------------------------------------ 边界（如实说明）

    @Test
    fun `整场都在暂停时也照样按挂钟封段`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val encoder = FakePcmEncoder()
        val driver = recordScenario(store, encoder, 30, 0, 30).driver
        val pcm = pcmOf(encoder)

        assertEquals("3 秒挂钟 = 3 段（一段都不许少）", 3, driver.sealed.size)
        assertTrue("整场暂停 = 整场空白（这不是 bug，是用户的口径）",
            pcmRangeIsSilent(pcm, 0, pcm.size))
        ActiveDraft.clear()
    }

    @Test
    fun `零帧那一场编码器也会被真的调用（不是被跳过）`() {
        // 旧代码在暂停时 `continue`：编码器根本不会被调到，段也不会产生。
        // 这条钉的就是"暂停的段也走同一条落盘路"（全零音频编出来还是全零，绝不能少一段）。
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val encoder = FakePcmEncoder()
        val run = recordScenario(store, encoder, 30, 0, 30)
        val driver = run.driver

        assertEquals("3 段都封了，编码器就被调了 3 次", 3, encoder.frames.size)
        assertEquals(3, driver.sealed.size)
        driver.sealed.forEach {
            val f = store.audioFileOf(run.clientId, it)
            assertTrue("第 ${it.seq} 段的音频必须真的落在磁盘上", f.isFile)
            assertEquals("落盘的是 WAV 头 + 那一段全零 PCM",
                WavWriter.HEADER_SIZE + segmentSamples * bytesPerSample, f.readBytes().size)
        }
        ActiveDraft.clear()
    }
}
