package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.audio.WavWriter
import com.echo.contact.ui.elapsedLabel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * **暂停期间的时间轴**（用户 2026-10-17 晚的两个真机问题）。
 *
 * 用户原话：
 *  1. *"手机录音暂停时，计时器也不刷新时间了"*；
 *  2. *"恢复录音后，不到一分钟的音频组成了第一个分段，并开始上传了"*。
 *
 * 现行口径（AGENTS.md「暂停语义（2026-10-16 深夜）」）：暂停 = **照常录音、写进去的是全零**、
 * 时间轴逐字连续、**总时长不受影响**、不做任何剪切。于是这两条都是**违反该口径**的：
 *
 *  * 问题①：既然还在录、时长还在累计，界面上的计时就**不能冻住**（冻住会让用户以为
 *    "暂停把这段时间抹掉了"，进而把后面封的那一段看成"提前封"）。
 *  * 问题②：段长只由**累计样本数**决定（暂停的零样本照常计数），所以"录 20 秒 → 暂停 60 秒
 *    → 恢复 → 录 40 秒"这一场（段长 60 秒）必须**正好 2 段**（60 + 60），
 *    **绝不是 1 段**（那是"暂停不计时长"的旧错法）；更不许"不到一个段长就封段"。
 *    **唯一**允许不足一段的是**结束时的尾段**（`MeetingTailFlush` / `sealFinalTail`）。
 *
 * 这一组用例走的是**与 `MeetingMicService.runLoop` 同形状**的驱动（喂帧 → 暂停 → 推进时钟 →
 * 攒满一段就封 → 每帧刷新"累计时长/本段进度"），因为"暂停时少算几个样本"这种错法
 * 在纯函数层看着都对，只有端到端（采集 → 缓冲 → 封段 → 清单/时长）才会露出来。
 */
class MeetingPauseTimelineTest {

    @get:Rule val tmp = TemporaryFolder()

    private val rate = 16_000
    private val frameSamples = 1_600          // 100ms，与 `MeetingMicService.FRAME_SAMPLES` 一致
    private val framesPerSecond = 10
    private val startIso = "2026-10-18T09:30:00"
    private val startMs: Long = MeetingDraftStore.epochOf(startIso) ?: error("用例前置：开始时间要能解析")
    private val clientId = "phone-20261018093000-abcd"
    private var folderSeq = 0

    /** 把喂进来的 PCM 原样记下来（外面套 WAV 头），于是能验"暂停那段是不是全零"。 */
    private class FakePcmEncoder : SegmentAudioEncoder {
        val frames = ArrayList<ByteArray>()
        override fun encode(pcm: ByteArray): EncodedAudio {
            frames.add(pcm.copyOf())
            return EncodedAudio(WavWriter.wrap(pcm, 16_000, 1, 16), SegmentFormat.WAV)
        }
        fun joined(): ByteArray {
            val out = ByteArray(frames.sumOf { it.size })
            var at = 0
            frames.forEach { f -> System.arraycopy(f, 0, out, at, f.size); at += f.size }
            return out
        }
    }

    /**
     * **与 `runLoop` 同形状**的采集驱动：每一帧推进 100ms 挂钟 → 记暂停/恢复 → 喂帧（暂停喂零）
     * → 攒满一段就封 → 最后刷新"累计时长"与"本段进度"（生产代码在同一个循环体里做这三件事）。
     */
    private inner class Loop(
        segmentSeconds: Int,
        withSession: Boolean = true,
    ) {
        val store = MeetingDraftStore(tmp.newFolder("offline-${folderSeq++}"))
        val encoder = FakePcmEncoder()
        var now = startMs
        val session: MeetingRecordSession? =
            if (withSession) MeetingRecordSession(
                store = store, encoder = encoder, sampleRate = rate, channelCount = 1,
                clock = { now }) else null

        private val capture = MicCaptureLoop(rate * segmentSeconds)
        private val buffer = PcmBuffer(rate * segmentSeconds)
        private val elapsed = MeetingElapsed(clock = { now })

        /** 采集循环每帧刷新出来的两格状态（与 `MicState` 同名，含义相同）。 */
        var elapsedSeconds = 0.0
            private set
        var segmentElapsedSeconds = 0.0
            private set

        /** 已经由"攒满一段"封出去的段（**不含**结束时的尾段）。 */
        val sealed = ArrayList<DraftSegment>()

        fun start() {
            session?.start(clientId, startIso)
            elapsed.start(startMs)     // 生产：startRecording 把"开录那一刻"交下去
        }

        /** 走一个 100ms 帧。[paused] = true 时这一段是暂停（内容全零，但**照常计数/封段**）。 */
        fun frame(paused: Boolean = false) {
            // ① 暂停/恢复先记在挂钟刻度上（用户按下的那一刻就是一个确定的时刻）
            val s = session
            if (s != null) {
                if (paused && !s.paused) s.beginPause()
                if (!paused && s.paused) s.endPause()
            }
            // ② 再走一个帧长（真机上 `ar.read` 阻塞 100ms），③ 这一帧可能封出一段
            now += frameSamples * 1_000L / rate
            if (capture.feed(buffer, ShortArray(frameSamples) { 1_000 }, frameSamples,
                    active = !paused).sealed) {
                val seal = s?.seal(toPcmBytes(buffer))
                seal?.draft?.let { sealed += it.segments.last() }
                buffer.clear()
            }
            // ④ 这一帧的"事实"（与 runLoop 的 ③ 同一处）：累计时长与暂停无关，必须每帧推进
            elapsedSeconds = elapsed.tick()
            segmentElapsedSeconds = progressOf(buffer, rate)
        }

        /** 走满一秒（10 帧）。 */
        fun second(paused: Boolean = false) {
            repeat(framesPerSecond) { frame(paused = paused) }
        }

        /** 结束：先补尾段再收尾（与 `stopEverything` 同一顺序）。 */
        fun stop(): Pair<TailSealOutcome, MeetingDraft?> {
            val tail = sealFinalTail(session, buffer.toShortArray(), rate)
            buffer.clear()
            return tail to session?.finish()
        }
    }

    /** "录 [before] 秒 → 暂停 [pause] 秒 → 恢复 → 录 [after] 秒"。 */
    private fun scenario(segmentSeconds: Int, before: Int, pause: Int, after: Int): Loop {
        val loop = Loop(segmentSeconds)
        loop.start()
        repeat(before) { loop.second(paused = false) }
        repeat(pause) { loop.second(paused = true) }
        repeat(after) { loop.second(paused = false) }
        return loop
    }

    private fun shortAt(pcm: ByteArray, byteOffset: Int): Short =
        ByteBuffer.wrap(pcm, byteOffset, 2).order(ByteOrder.LITTLE_ENDIAN).short

    private fun isSilent(pcm: ByteArray, fromByte: Int, toByte: Int): Boolean {
        var i = fromByte
        while (i < toByte) {
            if (shortAt(pcm, i) != 0.toShort()) return false
            i += 2
        }
        return true
    }

    private fun hasAudio(pcm: ByteArray, fromByte: Int, toByte: Int): Boolean {
        var i = fromByte
        while (i < toByte) {
            if (shortAt(pcm, i) != 0.toShort()) return true
            i += 2
        }
        return false
    }

    // ---------------------------------------------------- ① 计时：暂停期间照挂钟增长

    @Test
    fun `累计时长只由挂钟决定（与暂停无关）`() {
        // 纯函数那一层：开始前是 0；开始后 tick 就是"现在 - 开始时刻"，
        // 它**完全不认识"暂停"这个概念** —— 这正是"暂停也占时长"最不容易写错的形式。
        val e = MeetingElapsed(clock = { startMs })
        assertEquals("还没开始录：0 秒", 0.0, e.tick(), 1e-9)
        e.start(startMs)
        assertEquals(0.0, e.tick(startMs), 1e-9)
        assertEquals("挂钟走了 90 秒就是 90 秒（这里面有没有暂停都一样）",
            90.0, e.tick(startMs + 90_000), 1e-9)
        assertEquals("时钟倒退也不出负数", 0.0, e.tick(startMs - 5_000), 1e-9)
        // 本段进度：按样本数算，样本一个都不少（暂停写的就是零样本，也占长度）
        assertEquals("16000 个样本（16kHz）= 1 秒", 1.0, progressOf(List(16_000) { 0 }, 16_000), 1e-9)
        assertEquals("空缓冲 = 0 秒", 0.0, progressOf(emptyList(), 16_000), 1e-9)
        assertEquals("采样率读不出来时不除零", 0.0, progressOf(List(16_000) { 0 }, 0), 1e-9)
    }

    @Test
    fun `暂停期间累计时长照挂钟增长（计时不许冻住）`() {
        val loop = Loop(segmentSeconds = 60)
        loop.start()
        val marks = HashMap<Int, Double>()

        repeat(20) { loop.second(paused = false); marks[loop.elapsedSeconds.toInt()] = loop.elapsedSeconds }
        // 暂停 60 秒：这 60 秒里时间**一秒都不能少**（暂停 = 照常录音、写全零、总时长不受影响）
        repeat(60) { loop.second(paused = true); marks[loop.elapsedSeconds.toInt()] = loop.elapsedSeconds }
        repeat(40) { loop.second(paused = false); marks[loop.elapsedSeconds.toInt()] = loop.elapsedSeconds }

        try {
            // 暂停开始、暂停中段、暂停结束、结束：累计时长都等于挂钟
            listOf(20, 21, 40, 60, 80, 81, 100, 120).forEach { at ->
                assertEquals("第 $at 秒时的累计时长", at.toDouble(), marks[at] ?: -1.0, 1e-9)
            }
            // 暂停**期间**必须是**严格增长**的（冻住的话 21..79 会全是 20 秒）
            val duringPause = (21..79).map { marks[it] ?: -1.0 }
            assertTrue("暂停期间累计时长必须一直增长：$duringPause",
                duringPause.zipWithNext().all { (a, b) -> b > a })
            // 界面那一行（纯函数）也照挂钟走 —— 暂停中段显示的就是 01:00
            assertEquals("暂停中段的界面上计时", "01:00",
                elapsedLabel(marks[60] ?: -1.0, startIso, startMs + 60_000))
        } finally {
            ActiveDraft.clear()
        }
    }

    @Test
    fun `暂停期间本段进度也在攒（零样本照常计数）`() {
        // 段长 30 秒：录 20 秒 → 暂停。暂停到第 10 秒时本段正好攒满 30 秒（于是封段），
        // 之后本段重新从 0 开始 —— 这些数字在界面上直接可见（"本段 mm:ss / 00:30"）。
        val loop = Loop(segmentSeconds = 30)
        loop.start()
        repeat(20) { loop.second(paused = false) }
        assertEquals("录 20 秒：本段进度 20 秒", 20.0, loop.segmentElapsedSeconds, 1e-9)

        repeat(5) { loop.second(paused = true) }        // 暂停到 25 秒
        assertEquals("暂停中本段进度照常攒到 25 秒", 25.0, loop.segmentElapsedSeconds, 1e-9)
        assertEquals("还没封段", 0, loop.sealed.size)

        repeat(5) { loop.second(paused = true) }        // 暂停到 30 秒：这一帧攒满一段
        assertEquals("暂停期间攒满一段就封（段边界落在挂钟刻度上）", 1, loop.sealed.size)
        assertEquals("封下来的这一段仍是一个段长", 30.0, loop.sealed.single().durationSeconds, 1e-9)
        assertEquals("封完本段从 0 重新开始", 0.0, loop.segmentElapsedSeconds, 1e-9)
        ActiveDraft.clear()
    }

    // ---------------------------------------------------- ② 20 + 暂停 60 + 40：段数与总时长

    @Test
    fun `录 20 秒 + 暂停 60 秒 + 录 40 秒 段长 30 秒 恰好 4 段且总时长 120 秒`() {
        val loop = scenario(segmentSeconds = 30, before = 20, pause = 60, after = 40)
        val pcm = loop.encoder.joined()
        val draft = loop.store.load(clientId)!!

        try {
            assertEquals("段长 30 秒 + 挂钟 120 秒 → 正好 4 段（暂停那 60 秒也算时长）",
                4, loop.sealed.size)
            loop.sealed.forEach { seg ->
                assertEquals("第 ${seg.seq} 段必须恰好一个段长（暂停的零样本也算）",
                    30.0, seg.durationSeconds, 1e-9)
            }
            assertEquals("段号连续", listOf(1, 2, 3, 4), loop.sealed.map { it.seq })
            loop.sealed.forEachIndexed { i, seg ->
                assertEquals("第 ${i + 1} 段的起点 = 挂钟第 ${i + 1} 个段长",
                    (i * 30).toDouble(), seg.startOffsetSeconds, 1e-6)
                assertEquals("第 ${i + 1} 段的终点 = 挂钟第 ${i + 1} 个段长的末尾",
                    ((i + 1) * 30).toDouble(), seg.startOffsetSeconds + seg.durationSeconds, 1e-6)
            }
            assertEquals("音频总时长 = 挂钟 120 秒",
                120.0, loop.sealed.sumOf { it.durationSeconds }, 1e-6)
            assertEquals("清单里的累计时长 = 挂钟", 120.0, loop.elapsedSeconds, 1e-9)
            assertEquals("落盘的总时长也是挂钟", 120.0, draft.durationSeconds, 1e-6)

            // ③ 暂停区间全零、前后真音频（既有判据，别坏）
            val bytesPerSecond = rate * 2
            assertTrue("暂停那 60 秒（第 20~80 秒）必须全是零样本",
                isSilent(pcm, 20 * bytesPerSecond, 80 * bytesPerSecond))
            assertTrue("暂停**之前**必须还是真音频", hasAudio(pcm, 0, 20 * bytesPerSecond))
            assertTrue("暂停**之后**必须还是真音频", hasAudio(pcm, 80 * bytesPerSecond, pcm.size))
            assertEquals("暂停区间如实记进清单（纯标注，不参与剪切/补偿）",
                1, draft.pauses.size)
            assertEquals(20.0, draft.pauses.single().from, 1e-6)
            assertEquals(80.0, draft.pauses.single().to, 1e-6)
        } finally {
            ActiveDraft.clear()
        }
    }

    @Test
    fun `同一个场景 段长 60 秒 恰好 2 段（不是 1 段）`() {
        // ⚠️ 这条是"暂停不计时长"那个旧错法的直接判据：那种错法下 20 + 40 = 60 秒音频
        // 正好凑成 **1 段**（而挂钟是 120 秒）—— 用户看到的"第一个分段"于是就成了
        // "不到一分钟的音频"，而且它在恢复后不久就封了。
        val loop = scenario(segmentSeconds = 60, before = 20, pause = 60, after = 40)
        try {
            assertEquals("段长 60 秒 + 挂钟 120 秒 → 正好 2 段（不是 1 段）", 2, loop.sealed.size)
            assertEquals("两段都恰好一个段长", listOf(60.0, 60.0),
                loop.sealed.map { it.durationSeconds })
            assertEquals("第 2 段从挂钟第 60 秒起", 60.0, loop.sealed[1].startOffsetSeconds, 1e-6)
            assertEquals("音频总时长 = 挂钟 120 秒",
                120.0, loop.sealed.sumOf { it.durationSeconds }, 1e-6)
            assertEquals("累计时长 = 挂钟", 120.0, loop.elapsedSeconds, 1e-9)
        } finally {
            ActiveDraft.clear()
        }
    }

    @Test
    fun `恢复后不会提前封段：每一段都要攒满一个段长`() {
        // "恢复录音后，不到一分钟的音频组成了第一个分段"的判据化：
        // 暂停期间那一段一直在攒（段长 60 秒，挂钟第 60 秒就封了第 1 段），
        // 恢复时本段里已经有 20 秒，所以它**还要再攒 40 秒**才封 —— 不是恢复就封。
        val loop = Loop(segmentSeconds = 60)
        loop.start()
        repeat(20) { loop.second(paused = false) }      // 0~20s 真音频
        repeat(60) { loop.second(paused = true) }       // 20~80s 暂停
        assertEquals("暂停期间就攒满了：第 1 段在挂钟第 60 秒封的", 1, loop.sealed.size)
        assertEquals("第 1 段的终点 = 挂钟第 60 秒", 60.0,
            loop.sealed.single().startOffsetSeconds + loop.sealed.single().durationSeconds, 1e-6)

        repeat(39) { loop.second(paused = false) }      // 恢复后 39 秒
        assertEquals("恢复后还没攒满一整段：不许提前封", 1, loop.sealed.size)
        assertEquals("本段进度 = 20（暂停期间攒的零样本）+ 39 = 59 秒（不是从 0 重新开始）",
            59.0, loop.segmentElapsedSeconds, 1e-6)

        loop.second(paused = false)                     // 恢复后第 40 秒：攒满
        assertEquals("攒满一个段长才封", 2, loop.sealed.size)
        assertEquals("第 2 段也恰好是一个段长", 60.0, loop.sealed[1].durationSeconds, 1e-9)
        assertEquals("第 2 段的起点 = 挂钟第 60 秒", 60.0, loop.sealed[1].startOffsetSeconds, 1e-6)
        ActiveDraft.clear()
    }

    // ---------------------------------------------------- ③ 段长判据（只有尾段例外）

    @Test
    fun `正常封段必须刚好一个段长而尾段允许不足一段`() {
        // 正常封段（攒满一段才封）：每一段都精确等于段长
        assertEquals("30 秒段长、封下来 30 秒：没问题", "",
            segmentLengthProblem(seq = 1, seconds = 30.0, segmentSeconds = 30))
        assertEquals("60 秒段长、封下来 60 秒：没问题", "",
            segmentLengthProblem(seq = 2, seconds = 60.0, segmentSeconds = 60))
        // 不到一个段长就封：这是真机问题②的形状，必须被判出来（而不是"看着没事"）
        assertTrue("不到一个段长就封段必须报出来",
            segmentLengthProblem(seq = 1, seconds = 40.0, segmentSeconds = 60).isNotBlank())
        assertTrue("超出一个段长也要报出来",
            segmentLengthProblem(seq = 2, seconds = 75.0, segmentSeconds = 60).isNotBlank())
        assertEquals("还没读到设置（段长 0）时不编假问题", "",
            segmentLengthProblem(seq = 1, seconds = 5.0, segmentSeconds = 0))
        assertEquals("段长读不出来（0 秒）时也不判", "",
            segmentLengthProblem(seq = 1, seconds = 0.0, segmentSeconds = 60))

        // 尾段：录 20 秒就结束（段长 60 秒）→ 一段都没攒满，尾段只有 20 秒 —— **允许**
        val loop = Loop(segmentSeconds = 60)
        loop.start()
        repeat(20) { loop.second(paused = false) }
        assertEquals("20 秒还攒不满 60 秒的一段", 0, loop.sealed.size)
        val (tail, done) = loop.stop()
        try {
            assertTrue("有音频就必须留下（不许因为不够一段就丢）", tail.seal != null)
            assertEquals("尾段就是真的那 20 秒", 20.0, tail.seconds, 1e-9)
            assertTrue("尾段是结束时代替'攒满一段'补出来的那一段", tail.isTail)
            assertFalse("尾段不许报错", tail.error.isNotBlank())
            assertEquals("清单里就这一段（尾段）", 1, done!!.segments.size)
            assertEquals(20.0, done.segments.single().durationSeconds, 1e-6)
            // 同一条判据在尾段上会"报警" —— 这正是为什么调用方只对**非尾段**判它
            assertEquals("尾段本来就短（所以 publishSeal 用 isTail 豁免它）",
                true, segmentLengthProblem(1, tail.seconds, 60).isNotBlank())
        } finally {
            ActiveDraft.clear()
        }
    }

    @Test
    fun `暂停中结束时的尾段也照样封且内容是全零`() {
        // 暂停里按结束：那一段缓冲里全是零样本，但它**仍然是用户录到的时长**，
        // 不许因为"没有声音"就丢掉（AGENTS.md：暂停不影响总时长、不剪切）。
        val loop = Loop(segmentSeconds = 30)
        loop.start()
        repeat(20) { loop.second(paused = false) }      // 20 秒真音频
        repeat(5) { loop.second(paused = true) }        // 再暂停 5 秒
        val (tail, done) = loop.stop()
        try {
            assertEquals("20 + 5 = 25 秒，一段都没攒满", 0, loop.sealed.size)
            assertEquals("尾段 25 秒", 25.0, tail.seconds, 1e-9)
            assertFalse("尾段不许报错", tail.error.isNotBlank())
            val pcm = loop.encoder.joined()
            val bytesPerSecond = rate * 2
            assertTrue("前面 20 秒是真音频", hasAudio(pcm, 0, 20 * bytesPerSecond))
            assertTrue("最后 5 秒是暂停（全零）", isSilent(pcm, 20 * bytesPerSecond, pcm.size))
            assertEquals("总时长 = 挂钟 25 秒", 25.0, done!!.durationSeconds, 1e-6)
        } finally {
            ActiveDraft.clear()
        }
    }
}
