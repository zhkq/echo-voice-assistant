package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentEncoding
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.audio.WavSegmentEncoder
import com.echo.contact.audio.WavWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * **本地先写**那一半（`MeetingRecordSession`）：编码 → 落盘 → 记清单。
 *
 * 用户 2026-10-17 拍板"离线录音和在线录音就没什么本质区别了"，于是这条路只剩一个本体：
 * **每一段都先落在手机上**。这一组用例钉的就是这个本体的三条硬口径：
 *  1. **不依赖服务端**：开始录音只写清单（不探活、不建会）；封段只碰磁盘；
 *  2. **如实记格式**：编成 FLAC 就记 `flac`、退回了 WAV 就记 `wav`，而且磁盘上的文件名
 *     与清单里那一项**必须一致**（不然补传会去找一个不存在的文件）；
 *  3. **明明可能丢音频的事要报出来**：写不进去时 `error` 必须非空（那是真的丢音频）。
 */
class MeetingRecordSessionTest {

    @get:Rule val tmp = TemporaryFolder()

    private val clientId = "phone-20261017093000-abcd"
    private val startIso = "2026-10-17T09:30:00"

    /** 一个可控的假编码器：想让它"编成 FLAC"就编成 FLAC，想让它退回 WAV 就退回 WAV。 */
    private class FakeEncoder(private val flacOk: Boolean) : SegmentAudioEncoder {
        var calls = 0
            private set

        override fun encode(pcm: ByteArray): EncodedAudio {
            calls++
            return if (flacOk) {
                EncodedAudio("fLaC".toByteArray() + ByteArray(32) { 3 }, SegmentFormat.FLAC)
            } else {
                EncodedAudio(
                    WavWriter.wrap(pcm, 16_000, 1, 16), SegmentFormat.WAV,
                    note = SegmentEncoding.FLAC_UNAVAILABLE)
            }
        }
    }

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    private fun session(
        store: MeetingDraftStore,
        flacOk: Boolean = true,
        now: () -> Long = { startMs },
    ) = MeetingRecordSession(
        store = store,
        encoder = FakeEncoder(flacOk),
        sampleRate = 16_000,
        channelCount = 1,
        clock = now,
    )

    // 一段 1 秒的 16kHz 单声道 16bit PCM
    private fun pcmOfSeconds(seconds: Double): ByteArray = ByteArray((16_000 * 2 * seconds).toInt())

    private val startMs: Long =
        MeetingDraftStore.epochOf(startIso) ?: error("用例的前置条件：开始时间要能解析")

    // ---------------------------------------------------------------- 开始：只动本地

    @Test
    fun `开始录音只立本地清单（不碰服务端）`() {
        val store = store()
        val s = session(store)
        val started = s.start(clientId, startIso)

        assertEquals(clientId, started.draft.clientId)
        assertEquals(startIso, started.draft.startedAt)
        assertEquals("开始那一刻就该有清单（进程立刻被杀也留得下'这一场开始了'）",
            0, started.draft.segments.size)
        assertTrue("还在录（ended_at 空）", started.draft.recording)
        assertEquals("", started.note)
        assertNotNull("清单必须真的落盘", store.load(clientId))
        assertEquals(clientId, ActiveDraft.current?.clientId)
        ActiveDraft.clear()
    }

    @Test
    fun `清单自相矛盾时另起一份并把原因如实带出来`() {
        // 判据是 `MeetingDraftStore.loadOrCreate`：清单**读得动但自相矛盾**（段号 0 = 被截断过）
        // 时不许在它上面继续写 —— 那会把还能救的音频连同定义一起毁掉
        val store = store()
        store.dirOf(clientId).mkdirs()
        store.manifestFile(clientId).writeText(
            """{"client_id":"$clientId","started_at":"$startIso",""" +
                """"segments":[{"seq":0,"file":"x.wav"}]}""")
        val s = session(store)
        val started = s.start(clientId, startIso)
        assertTrue("要如实说清降级了：${started.note}", started.note.contains("读不懂"))
        assertFalse("新的 clientId 不该等于坏掉的那一份",
            started.draft.clientId == clientId)
        assertTrue("旧目录原样留着（不许删）", store.dirOf(clientId).isDirectory)
        ActiveDraft.clear()
    }

    // ---------------------------------------------------------------- 封段：格式如实 + 段号连续

    @Test
    fun `封段产出 FLAC 文件名与清单里的格式一致`() {
        val store = store()
        val s = session(store, flacOk = true)
        s.start(clientId, startIso)

        val seal = s.seal(pcmOfSeconds(2.0))

        assertEquals(1, seal.seq)
        assertEquals(SegmentFormat.FLAC, seal.format)
        assertEquals("编成 FLAC 时不该有那句 WAV 的说明", "", seal.note)
        assertEquals("", seal.error)
        val seg = seal.draft!!.segments.single()
        assertEquals("文件名按段号补零（目录里按名字排就是正确顺序）", "seg-0001.flac", seg.file)
        assertEquals("清单里记的格式必须是真的那一份", SegmentFormat.FLAC, seg.format)
        assertTrue("音频要真的落在磁盘上", store.audioFileOf(clientId, seg).isFile)
        assertTrue(store.audioFileOf(clientId, seg).readBytes().size > 4)
        assertTrue("FLAC 的字节里必须有那个魔数",
            store.audioFileOf(clientId, seg).readBytes().decodeToString().startsWith("fLaC"))
        ActiveDraft.clear()
    }

    @Test
    fun `编码器不可用时这一段退回 WAV 且清单如实记 WAV`() {
        val store = store()
        val s = session(store, flacOk = false)
        s.start(clientId, startIso)

        val seal = s.seal(pcmOfSeconds(2.0))

        assertEquals(SegmentFormat.WAV, seal.format)
        assertTrue("退回 WAV 时必须如实说清为什么：${seal.note}",
            seal.note.contains("WAV") && seal.note.contains(SegmentEncoding.FLAC_UNAVAILABLE))
        val seg = seal.draft!!.segments.single()
        assertEquals("seg-0001.wav", seg.file)
        assertEquals(SegmentFormat.WAV, seg.format)
        assertTrue("退回 WAV 也绝不丢音频（文件在、有头有 PCM）",
            store.audioFileOf(clientId, seg).readBytes().size == WavWriter.HEADER_SIZE +
                pcmOfSeconds(2.0).size)
        assertEquals("落盘正常时不该报错（退回 WAV 不是错误）", "", seal.error)
        ActiveDraft.clear()
    }

    @Test
    fun `同一场里可以一段 FLAC 两段 WAV（逐段记真的那一份）`() {
        val store = store()
        // 第一段编得出 FLAC，之后编码器"不可用"：真实世界里编码器被系统回收就是这样
        var first = true
        val s = MeetingRecordSession(
            store = store,
            encoder = object : SegmentAudioEncoder {
                override fun encode(pcm: ByteArray): EncodedAudio {
                    val ok = first
                    first = false
                    return if (ok) EncodedAudio("fLaC".toByteArray() + ByteArray(8), SegmentFormat.FLAC)
                    else EncodedAudio(WavWriter.wrap(pcm, 16_000, 1, 16), SegmentFormat.WAV,
                        note = SegmentEncoding.FLAC_UNAVAILABLE)
                }
            },
            sampleRate = 16_000,
            clock = { startMs },
        )
        s.start(clientId, startIso)
        s.seal(pcmOfSeconds(1.0))
        val second = s.seal(pcmOfSeconds(1.0))
        val third = s.seal(pcmOfSeconds(1.0))

        val segs = third.draft!!.segments
        assertEquals(listOf(1, 2, 3), segs.map { it.seq })
        assertEquals(listOf("seg-0001.flac", "seg-0002.wav", "seg-0003.wav"),
            segs.map { it.file })
        assertEquals(listOf(SegmentFormat.FLAC, SegmentFormat.WAV, SegmentFormat.WAV),
            segs.map { it.format })
        assertTrue("第二段那句如实的话要在：${second.note}", second.note.contains("第 2 段用 WAV"))
        assertTrue("第一段是 FLAC 时不该出现格式那句话（不许把 FLAC 说成退回）：${second.note}",
            !second.note.contains("第 1 段"))
        assertTrue("第三段要带上本场累计（用户要知道'这场不止一段退了'）：${third.note}",
            third.note.contains("第 3 段用 WAV") && third.note.contains("本场已有 2 段用 WAV"))
        // 磁盘上三份都在（一个都不许丢）
        segs.forEach { assertTrue(it.file, store.audioFileOf(clientId, it).isFile) }
        ActiveDraft.clear()
    }

    @Test
    fun `段号接着水位往下排（不会重号）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, startIso)
        // 模拟"前面几段已经被服务端认下"（续传场景）：水位 3
        val d = s.draft!!.copy(acknowledgedSegments = 3)
        store.save(d)
        val reopened = session(store)
        reopened.start(clientId, startIso)

        val seal = reopened.seal(pcmOfSeconds(1.0))

        assertEquals("水位 3 之后新段必须从 4 起（从 1 起会跟电脑上已有的段重号）",
            4, seal.seq)
        ActiveDraft.clear()
    }

    @Test
    fun `段的起点是封存那一刻减去它自己的长度（段边界对齐挂钟）`() {
        // 封存发生在这一段**录完之后**：第 1 段 30 秒在 09:30:30 封存，
        // 它的起点必须是 09:30:00（= 30 秒前），不是 09:30:30。
        // 这条一旦错，每一段的边界都晚一个段长 —— 而"第 N 段 = 挂钟第 N 个段长"
        // 正是转写时间轴与音频对齐的依据（用户 2026-10-16 深夜的口径也点在这一点上）。
        val store = store()
        var now = startMs
        val s = session(store, now = { now })
        s.start(clientId, startIso)

        now = startMs + 30_000
        val first = s.seal(pcmOfSeconds(30.0))
        assertEquals(0.0, first.draft!!.segments.single().startOffsetSeconds, 1e-6)
        assertEquals("总时长 = 段末（30 秒），不是 60 秒", 30.0, first.draft!!.durationSeconds, 1e-6)

        now = startMs + 60_000
        val second = s.seal(pcmOfSeconds(30.0))
        assertEquals(30.0, second.draft!!.segments.last().startOffsetSeconds, 1e-6)
        assertEquals(60.0, second.draft!!.durationSeconds, 1e-6)
        ActiveDraft.clear()
    }

    @Test
    fun `时长由真的字节数算出来（不拿设置里的段长当答案）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, startIso)

        val seal = s.seal(pcmOfSeconds(1.5))

        assertEquals("1.5 秒的 16kHz 单声道 PCM 就是 1.5 秒",
            1.5, seal.draft!!.segments.single().durationSeconds, 1e-6)
        assertEquals("总时长也要跟着（最后一段可能比别的段短）",
            1.5, seal.draft!!.durationSeconds, 1e-6)
        ActiveDraft.clear()
    }

    // ---------------------------------------------------------------- 暂停区间

    @Test
    fun `暂停区间按相对开始时间的秒数记进清单`() {
        val store = store()
        var now = startMs
        val s = session(store, now = { now })
        s.start(clientId, startIso)

        now = startMs + 10_000
        assertEquals(10.0, s.beginPause()!!, 1e-6)
        assertTrue(s.paused)
        now = startMs + 25_000
        val pause = s.endPause()!!

        assertEquals(10.0, pause.from, 1e-6)
        assertEquals(25.0, pause.to, 1e-6)
        assertFalse(s.paused)
        assertEquals(listOf(pause), s.draft!!.pauses)
        assertEquals("暂停区间必须落盘（崩了也留得下'这段时间被暂停过'）",
            listOf(pause), store.load(clientId)!!.pauses)
        ActiveDraft.clear()
    }

    @Test
    fun `连按两下静音不会记出两条区间（幂等）`() {
        val store = store()
        var now = startMs + 5_000
        val s = session(store, now = { now })
        s.start(clientId, startIso)

        assertEquals(5.0, s.beginPause()!!, 1e-6)
        now = startMs + 8_000
        assertEquals("已经在暂停中就还是那一个起点", 5.0, s.beginPause()!!, 1e-6)
        now = startMs + 9_000
        assertEquals(5.0, s.endPause()!!.from, 1e-6)
        assertEquals(1, s.draft!!.pauses.size)
        ActiveDraft.clear()
    }

    @Test
    fun `结束时还静音着也要把这段区间收进清单`() {
        // 不记的话会议里会凭空多出一段"没人说话"的时长，而不是标着"被暂停过"
        val store = store()
        var now = startMs + 20_000
        val s = session(store, now = { now })
        s.start(clientId, startIso)
        s.beginPause()
        now = startMs + 30_000

        val done = s.finish()!!

        assertEquals(1, done.pauses.size)
        assertEquals(30.0, done.pauses.single().to, 1e-6)
        assertFalse("结束之后就不再是'还在录'了", done.recording)
        ActiveDraft.clear()
    }

    // ---------------------------------------------------------------- 结束与真丢音频

    @Test
    fun `结束写结束时间与总时长并落盘`() {
        val store = store()
        var now = startMs
        val s = session(store, now = { now })
        s.start(clientId, startIso)
        now = startMs + 3_000
        val seal = s.seal(pcmOfSeconds(3.0))
        assertEquals(3.0, seal.draft!!.durationSeconds, 1e-6)

        now = startMs + 40_000
        val done = s.finish()!!

        assertEquals("2026-10-17T09:30:40", done.endedAt)
        assertEquals("总时长按真的过去的时间（40 秒）", 40.0, done.durationSeconds, 1e-6)
        assertFalse(done.recording)
        assertEquals("结束时间必须落盘（否则这一场永远停在'还在录'）",
            "2026-10-17T09:30:40", store.load(clientId)!!.endedAt)
        ActiveDraft.clear()
    }

    @Test
    fun `写不进去时如实报出'可能真的丢了'`() {
        // 造一个真的写不进去的清单目录：把 root 位置上先放一个**文件**（mkdirs 必失败）
        val root = File(tmp.root, "not-a-dir")
        root.writeText("占住这个位置")
        val store = MeetingDraftStore(root)
        val s = session(store)
        s.start(clientId, startIso)

        val seal = s.seal(pcmOfSeconds(1.0))

        assertTrue("这是真的可能丢音频，必须报出来：${seal.error}", seal.error.isNotBlank())
        ActiveDraft.clear()
    }

    @Test
    fun `被进程打断那种清单的'结束时间是推的'要落盘（结构化，不是一句话）`() {
        // 这条字段是给"每一次同步都要如实说"用的（见 OfflineMeetingSync.inferredEndNote）：
        // 只写在内存里的话，重启一次就没人知道那个结束时间是推断的了
        val store = store()
        val s = session(store)
        s.start(clientId, startIso)
        s.seal(pcmOfSeconds(1.0))
        store.save(store.load(clientId)!!.copy(
            endedAt = "2026-10-17T09:31:00", endedAtInferred = true))

        val back = store.load(clientId)!!
        assertTrue("结束时间是推的这件事必须能读回来", back.endedAtInferred)
        assertFalse("正常收尾的那一份不该带这个标记",
            OfflineMeetingJson.decode(OfflineMeetingJson.encode(
                back.copy(endedAtInferred = false)))!!.endedAtInferred)
        ActiveDraft.clear()
    }

    @Test
    fun `WAV 编码器的那条路（显式不用 FLAC）也照样落盘`() {
        val store = store()
        val s = MeetingRecordSession(
            store = store, encoder = WavSegmentEncoder(16_000, 1, 16),
            sampleRate = 16_000, clock = { startMs })
        s.start(clientId, startIso)

        val seal = s.seal(pcmOfSeconds(1.0))

        assertEquals(SegmentFormat.WAV, seal.format)
        assertEquals("seg-0001.wav", seal.draft!!.segments.single().file)
        assertTrue(store.audioFileOf(clientId, seal.draft!!.segments.single()).isFile)
        ActiveDraft.clear()
    }
}
