package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.audio.WavWriter
import com.echo.contact.data.ApiClient
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * **结束时补齐最后不足一段**（本次修的退步）+ 停止时的时长门槛。
 *
 * ## 现场（上一轮自己交代的既有缺口，这一轮成了显式退步）
 *
 * 段长是**用户设置**（默认 600 秒 = 10 分钟；这一组固定用 30 秒那一档，方便把段边界数清）。
 * 以前 `stopEverything`
 * 只做 `session.finish()`：还攒在缓冲里的样本**整段丢掉**。于是用户真机要验的
 * "**录 20 秒**看效果"既不建会、也不上传，那段录音直接消失；而旧版"电脑麦"那条路
 * 是能录的 —— 所以这是**真实的退步**，不是"功能没做"。
 *
 * 这一组用例照生产顺序走（真 HTTP，不是假 transport）：
 * ```
 * 采集（100ms 帧，30 秒一段）→ 结束：sealFinalTail（补尾段）→ session.finish()
 *   → OfflineMeetingSync.FINALIZE（建会 source=phone → 传段 → 对账 → stop）
 * ```
 *
 * ## 四条判据（对应任务里的四条）
 *
 *  1. 录 **3 秒**就结束 → 落成 1 段 + 建会（`source=phone`/`clientId`/`startedAt`）+ 上传；
 *  2. 录 **45 秒**（跨一段 + 余 15 秒）→ 2 段，第二段是补齐的尾段，边界/时长合理；
 *  3. **0 秒**（一按就停）→ **不建会**（一个请求都不发）且**如实说**"没有录到音频"；
 *  4. **暂停中结束** → 尾段照样落（零音频也算时长，暂停区间仍在清单里）。
 *
 * 另加两条护栏：**不足 0.5 秒也留下**（阈值只影响文案）、**尾段落盘失败时如实说
 * "可能真的丢了"**（绝不谎报"已上传"）。
 */
class MeetingTailFlushTest {

    @get:Rule val tmp = TemporaryFolder()

    private lateinit var server: MockWebServer
    private lateinit var stub: EchoStub

    private val clientId = "phone-20261018153000-abcd"
    private val startIso = "2026-10-18T15:30:00"
    private val startMs: Long =
        MeetingDraftStore.epochOf(startIso) ?: error("用例前置：开始时间要能解析")
    private val rate = 16_000
    private val frameSamples = 1_600                 // 100ms，与 MeetingMicService.FRAME_SAMPLES 一致
    private val defaultSegmentSeconds = 30            // 用户可选的 30 秒档（**默认是 600**，这里用短档跑用例）
    private var folderSeq = 0

    /**
     * 一个够真的假 ECHO（与 `MeetingSinglePathTest` 同一个形状）：
     *  * `/health` 探活；`/api/meeting/status` 回报"现在在录哪一场、几段"；
     *  * `/api/meeting/start` 把 query（`source`/`clientId`/`startedAt`）**原样记下来**；
     *  * `/api/meeting/segment` 按 `(clientId, seq)` 记段并回报累计段数（对账用）；
     *  * `/api/meeting/stop` 收尾。
     */
    private class EchoStub : Dispatcher() {
        /** 每一次建会请求的三个身份参数（**这一组判据的核心**就在这三样上）。 */
        val startQueries = mutableListOf<Map<String, String>>()
        val segments = LinkedHashMap<String, MutableSet<Int>>()
        var stops = 0
        var recording = false
        var statusClientId = ""
        private val lock = Any()

        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.path!!.substringBefore('?')
            val url = request.requestUrl!!
            synchronized(lock) {
                return when (path) {
                    "/health" -> MockResponse().setResponseCode(200)
                    "/api/meeting/status" -> json(
                        """{"active":$recording,"clientId":"$statusClientId",""" +
                            """"segments":${segments.values.sumOf { it.size }}}""")
                    "/api/meeting/start" -> {
                        startQueries += listOf("source", "clientId", "startedAt")
                            .associateWith { url.queryParameter(it).orEmpty() }
                        val cid = url.queryParameter("clientId").orEmpty()
                        recording = true
                        statusClientId = cid
                        json("""{"ok":true,"message":"开始了","meetingId":7}""")
                    }
                    "/api/meeting/segment" -> {
                        val cid = url.queryParameter("clientId").orEmpty()
                        val seq = url.queryParameter("seq")?.toIntOrNull() ?: -1
                        val set = segments.getOrPut(cid) { linkedSetOf() }
                        val dup = !set.add(seq)
                        json("""{"ok":true,"meetingId":7,"segmentCount":${set.size},""" +
                            """"duplicate":$dup}""")
                    }
                    "/api/meeting/stop" -> {
                        stops++
                        recording = false
                        json("""{"ok":true,"message":"已开始转写"}""")
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }

        private fun json(body: String) = MockResponse().setResponseCode(200)
            .setHeader("Content-Type", "application/json").setBody(body)
    }

    @Before
    fun setUp() {
        server = MockWebServer()
        stub = EchoStub()
        server.dispatcher = stub
        server.start()
        ApiClient.attachTokenProvider { null }
        ApiClient.clearCache()
    }

    @After
    fun tearDown() {
        ApiClient.attachTokenProvider { null }
        runCatching { server.shutdown() }
        ActiveDraft.clear()
    }

    /** 假 FLAC 编码器：吐一段带 `fLaC` 魔数的字节（真编码器的行为形状）。 */
    private class FakeFlac : SegmentAudioEncoder {
        override fun encode(pcm: ByteArray): EncodedAudio =
            EncodedAudio("fLaC".toByteArray() + ByteArray(32) { 5 }, SegmentFormat.FLAC)
    }

    /**
     * 一个**无损**的假编码器：把喂进来的 PCM 原样记下来（外面套 WAV 头让格式/文件名都对）。
     * 暂停那条用例要在字节层面验"暂停区间是不是全零"，所以必须能看到原始 PCM。
     */
    private class PcmRecordingEncoder : SegmentAudioEncoder {
        val chunks = ArrayList<ByteArray>()

        override fun encode(pcm: ByteArray): EncodedAudio {
            chunks += pcm.copyOf()
            return EncodedAudio(WavWriter.wrap(pcm, 16_000, 1, 16), SegmentFormat.WAV)
        }

        fun joined(): ByteArray {
            val out = ByteArray(chunks.sumOf { it.size })
            var at = 0
            chunks.forEach { chunk ->
                System.arraycopy(chunk, 0, out, at, chunk.size)
                at += chunk.size
            }
            return out
        }
    }

    /**
     * **与 `MeetingMicService.runLoop` 同形状**的采集驱动器：
     * 每 [tick] 走 100ms 挂钟（真机上 `ar.read` 一次就是 100ms）→ 喂一帧 → 攒满一段就封；
     * [stop] 时先 `sealFinalTail`（生产代码在 `finish()` 之前做的同一件事）再 `finish()`。
     *
     * 段长用 30 秒这一档（**用户可选**、默认是 600）：这一组要钉的正是"30 秒段长下，
     * 20 秒/45 秒录音会不会丢"，改小段长就等于把 bug 藏起来。
     */
    private inner class Rig(
        val encoder: SegmentAudioEncoder = FakeFlac(),
        segmentSeconds: Int = defaultSegmentSeconds,
    ) {
        val store = MeetingDraftStore(tmp.newFolder("offline-${folderSeq++}"))
        var now = startMs
        val session = MeetingRecordSession(
            store = store, encoder = encoder, sampleRate = rate, channelCount = 1,
            clock = { now })
        private val capture = MicCaptureLoop(rate * segmentSeconds)
        private val buffer = PcmBuffer(rate * segmentSeconds)

        /** 已经攒满一段封出去的段（**不含**结束时的尾段）。 */
        val sealed = ArrayList<DraftSegment>()

        fun start() {
            session.start(clientId, startIso)
        }

        /** 走一个 100ms 帧。[paused] = true 时内容全零（暂停口径，照常计数/封段）。 */
        fun tick(paused: Boolean = false) {
            now += frameSamples * 1_000L / rate
            val frame = ShortArray(frameSamples) { if (paused) 0 else 1_000 }
            if (capture.feed(buffer, frame, frameSamples, active = !paused).sealed) {
                val seal = session.seal(toPcmBytes(buffer))
                seal.draft?.let { sealed += it.segments.last() }
                buffer.clear()
            }
        }

        /** 结束：**先补尾段，再收尾**（顺序与 `stopEverything` 一致）。 */
        fun stop(): Pair<TailSealOutcome, MeetingDraft?> {
            val tail = sealFinalTail(session, buffer.toShortArray(), rate)
            if (tail.seal != null && tail.error.isBlank()) {
                tail.seal.draft?.let { sealed += it.segments.last() }
            }
            buffer.clear()
            return tail to session.finish()
        }
    }

    private fun sync(store: MeetingDraftStore): OfflineSyncResult = runBlocking {
        OfflineMeetingSync.run(
            transport = OfflineMeetingTransport(ApiClient.create(server.url("/").toString())),
            store = store,
            scope = SyncScope.FINALIZE,
        )
    }

    private fun drain(): List<RecordedRequest> {
        val out = mutableListOf<RecordedRequest>()
        while (true) {
            out += server.takeRequest(200, java.util.concurrent.TimeUnit.MILLISECONDS) ?: break
        }
        return out
    }

    // ------------------------------------------------------------------ ① 录 3 秒就结束

    /**
     * 判据 1：**录 3 秒就结束**（用户真机要验的"录 20 秒"的同类，只是更短）→
     * 落成 1 段、发出建会请求（`source=phone` + `clientId` + `startedAt`）、随后上传该段。
     *
     * 为什么必须走真 HTTP：建会那三个参数**只能**真发一次才钉得住（假 transport 是
     * 我自己拼的，拼错了它照样通过 —— 上一轮的退步正是"请求根本没发"）。
     */
    @Test
    fun `录 3 秒就结束 最后不足一段也封成 1 段并建会上传（真 HTTP）`() {
        val rig = Rig()
        rig.start()
        repeat(30) { rig.tick() }        // 3 秒
        assertEquals("30 秒段长下，3 秒还攒不满一段（这正是以前整段消失的那一档）",
            0, rig.sealed.size)

        val (tail, done) = rig.stop()

        // ---- 本地：尾段真的落成 1 段 ----
        assertEquals("最后那 3 秒必须封成第 1 段", 1, tail.seal?.seq ?: -1)
        assertEquals(3.0, tail.seconds, 1e-9)
        assertTrue("尾段落盘不该报错：${tail.error}", tail.error.isBlank())
        assertFalse("有音频就不许说'没有录到音频'", tail.noAudio)
        val d = done!!
        assertEquals("清单里就是这一段", 1, d.segments.size)
        assertEquals(3.0, d.segments.single().durationSeconds, 1e-6)
        assertEquals("尾段的起点仍是这一场的开头（不是停止那一刻）",
            0.0, d.segments.single().startOffsetSeconds, 1e-6)
        assertEquals("总时长 = 3 秒（由真的样本数算出来）", 3.0, d.durationSeconds, 1e-6)
        assertTrue("音频文件真的在手机上",
            rig.store.audioFileOf(clientId, d.segments.single()).isFile)

        // ---- 统一路：建会（带身份）→ 上传 → 对账 → stop ----
        val r = sync(rig.store)

        assertEquals("对账通过且服务端收尾了才敢说完成：$r", OfflinePhase.DONE, r.phase)
        assertTrue("服务端真的收尾（没有 stop 就没有转写）", r.stopped)
        assertEquals("一次录音只建一场会", 1, stub.startQueries.size)
        val q = stub.startQueries.single()
        assertEquals("采集源必须是**手机**", "phone", q["source"])
        assertEquals("幂等键就是本地清单里这一场的 id", clientId, q["clientId"])
        assertEquals("开始时间用本地清单记的那一刻", startIso, q["startedAt"])
        assertEquals("服务端手上就是这一段", setOf(1), stub.segments[clientId]!!.toSet())
        assertEquals("一场只收尾一次", 1, stub.stops)

        val reqs = drain()
        val paths = reqs.map { it.path!!.substringBefore('?') }
        assertTrue("建会必须在传段**之前**：$paths",
            paths.indexOf("/api/meeting/start") < paths.indexOf("/api/meeting/segment"))
        assertTrue("stop 必须在最后：$paths",
            paths.indexOf("/api/meeting/stop") > paths.indexOf("/api/meeting/segment"))
        val segReq = reqs.single { it.path!!.startsWith("/api/meeting/segment") }
        assertEquals("audio/flac", segReq.getHeader("Content-Type"))
        assertTrue("发出去的就是本地那份 FLAC 字节",
            segReq.body.readUtf8().startsWith("fLaC"))
    }

    // ------------------------------------------------------------------ ② 录 45 秒（跨一段 + 余 15 秒）

    /**
     * 判据 2：**录 45 秒就结束**（30 秒整段 + 15 秒尾巴）→ 落成 2 段，
     * 第二段就是**补齐的尾段**，段边界/时长都要合理。
     *
     * 为什么这条重要：它钉住"补齐尾段 **不等于** 改段长"—— 第一段仍然是完整的 30 秒、
     * 起点 0；第二段 15 秒、起点 30；总时长 45 秒。段长要是被改短，这里的 30 秒边界
     * 会立刻露馅。
     */
    @Test
    fun `录 45 秒就结束 落成 2 段且第二段是补齐的尾段（真 HTTP）`() {
        val rig = Rig()
        rig.start()
        repeat(450) { rig.tick() }       // 45 秒 = 30 秒整段 + 15 秒余量

        assertEquals("第 30 秒那一整段已经按用户设置的段长封出去了", 1, rig.sealed.size)

        val (tail, done) = rig.stop()

        assertEquals("尾巴补成第 2 段", 2, tail.seal?.seq ?: -1)
        assertEquals("尾巴就是剩下的 15 秒", 15.0, tail.seconds, 1e-9)
        val d = done!!
        assertEquals("一共 2 段", 2, d.segments.size)
        assertEquals("第一段仍是完整的 30 秒（**段长没被改短**）",
            listOf(30.0, 15.0), d.segments.map { it.durationSeconds })
        assertEquals("段边界：0 与 30（第 N 段覆盖挂钟第 N 个段长）",
            listOf(0.0, 30.0), d.segments.map { it.startOffsetSeconds })
        assertEquals("总时长 = 45 秒", 45.0, d.durationSeconds, 1e-6)
        assertEquals("两段音频都真的在手机上",
            2, d.segments.count { rig.store.audioFileOf(clientId, it).isFile })

        val r = sync(rig.store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue(r.stopped)
        assertEquals("服务端收到 1、2 两段（不重不漏）",
            setOf(1, 2), stub.segments[clientId]!!.toSet())
        assertEquals(1, stub.stops)

        val segReqs = drain().filter { it.path!!.startsWith("/api/meeting/segment") }
        assertEquals("上传顺序不许乱", listOf("1", "2"),
            segReqs.map { it.requestUrl!!.queryParameter("seq") })
        segReqs.forEach {
            assertEquals("两段都是 FLAC（清单里记的就是发出去的）",
                "audio/flac", it.getHeader("Content-Type"))
        }
    }

    // ------------------------------------------------------------------ ③ 0 秒

    /**
     * 判据 3：**0 秒**（一按就停）→ **不建会**（一个请求都不发），而且**如实说**
     * "这次没有录到音频" —— **绝不静默**。
     *
     * 为什么"一个请求都不发"是硬判据：服务端一场"手机麦"的空会会占着**唯一**那条
     * 录音槽，之后电脑/手机都开不了新会（用户看到的会是"开始录音失败"）。
     */
    @Test
    fun `0 秒一按就停 不建会一个请求都不发且如实说没有录到音频`() {
        val rig = Rig()
        rig.start()

        val (tail, done) = rig.stop()     // 一帧都没喂

        assertTrue("一个样本都没有", tail.noAudio)
        assertNull("没有音频就不许封段", tail.seal)
        assertTrue("原因要说出来：${tail.note}", tail.note.contains("没有录到音频"))
        assertEquals("清单里一段都没有", 0, done!!.segments.size)
        assertFalse("这一场已经收尾（不是永远停在'还在录'）", done.recording)

        val r = sync(rig.store)

        assertEquals("没有音频段 → 这一轮什么都不做", OfflinePhase.IDLE, r.phase)
        assertEquals("**一个请求都不发**（连探活都不发，服务端不会出现空会）",
            0, server.requestCount)

        // ---- 如实：结束那一刻那句话必须存在，且不许说成"已上传" ----
        val note = tailStopNote(tail, done.segments.size, hadSession = true)
        assertTrue("必须如实说没有录到音频：$note", note.contains("这次没有录到音频"))
        assertFalse("绝不许说成已上传：$note", note.contains("已上传"))
        assertTrue("这句话要能出现在界面上（MeetingsTab 读 stopNote），不能静默：$note",
            notificationText(MicState(active = false, stopNote = note))
                .contains("没有录到音频"))
    }

    // ------------------------------------------------------------------ ④ 暂停中结束

    /**
     * 判据 4：**暂停中结束** → 尾段照样落（用户 2026-10-16 深夜的口径：暂停是
     * **全零音频**、**照样算时长**、不做任何剪切）。
     *
     * 这一场：2 秒真音频 + 2 秒暂停（写零）→ 一段都没攒满（30 秒段长）→ 结束在暂停里。
     * 尾段必须是 4 秒，前 2 秒有声音、后 2 秒全零；清单里那一条暂停区间也要在，
     * 且结束点就是停止那一刻（不能因为"暂停"少算时长）。
     */
    @Test
    fun `暂停中结束 尾段照样落 零音频也算时长`() {
        val encoder = PcmRecordingEncoder()
        val rig = Rig(encoder = encoder)
        rig.start()

        repeat(20) { rig.tick() }                    // 2 秒真音频
        rig.session.beginPause()
        repeat(20) { rig.tick(paused = true) }       // 2 秒暂停：全零，但照样计数

        val (tail, done) = rig.stop()                // 结束时**还在暂停里**

        assertEquals("暂停中结束，尾巴也必须封成第 1 段", 1, tail.seal?.seq ?: -1)
        assertEquals("4 秒（含暂停那 2 秒，零音频也算时长）", 4.0, tail.seconds, 1e-9)
        val d = done!!
        assertEquals("暂停区间要收进清单（结束时还静音着也要记）", 1, d.pauses.size)
        assertEquals("暂停起点 = 第 2 秒", 2.0, d.pauses.single().from, 1e-6)
        assertEquals("暂停结束点 = 停止那一刻", 4.0, d.pauses.single().to, 1e-6)
        assertEquals("清单总时长 = 4 秒", 4.0, d.durationSeconds, 1e-6)
        assertEquals("那一段音频的时长 = 4 秒", 4.0, d.segments.single().durationSeconds, 1e-6)

        val pcm = encoder.joined()
        val perSecond = rate * 2                     // 16kHz × 16bit
        assertTrue("暂停之前必须仍是真音频", pcmRangeHasAudio(pcm, 0, 2 * perSecond))
        assertTrue("暂停那 2 秒必须是空白（零样本），不是真声音",
            pcmRangeIsSilent(pcm, 2 * perSecond, 4 * perSecond))
        assertTrue("尾段的音频文件真的在手机上",
            rig.store.audioFileOf(clientId, d.segments.single()).isFile)
    }

    // ------------------------------------------------------------------ 护栏：极短 / 落盘失败

    /**
     * 护栏：**不足 0.5 秒也留下**。
     *
     * 用户给的阈值是"≥ 某个极短阈值（比如 0.5 秒）就必须落成一段"；我们把它收紧到
     * "有样本就落"—— 因为 0.1 秒也是真的声音，扔掉它就违背了"有音频就必须留下"。
     * 0.5 秒只用来在文案里说一句"很短"。
     */
    @Test
    fun `不足 0 5 秒也留下 阈值只影响文案不是扔掉的借口`() {
        val rig = Rig()
        rig.start()
        rig.tick()                                   // 0.1 秒

        val (tail, done) = rig.stop()

        assertEquals("0.1 秒也要封成一段", 1, tail.seal?.seq ?: -1)
        assertEquals(0.1, tail.seconds, 1e-9)
        assertTrue("很短这件事可以说，但它确实留下了：${tail.note}",
            tail.note.contains("很短"))
        assertEquals(1, done!!.segments.size)
        assertEquals(0.1, done.segments.single().durationSeconds, 1e-9)
    }

    /**
     * 护栏：尾段**落盘失败**时如实说"可能真的丢了"，绝不谎报"已上传"。
     *
     * 这就是"宁可说难听的实话，也不给一句看着舒服的谎"：那一段没有真的落下来，
     * 就不能说它留下了。
     */
    @Test
    fun `尾段落盘失败时如实说可能真的丢了 绝不说已上传`() {
        // 真的写不进去：把清单 root 的位置先放一个文件（mkdirs 必失败）
        val root = File(tmp.root, "not-a-dir").apply { writeText("占住这个位置") }
        val store = MeetingDraftStore(root)
        val session = MeetingRecordSession(
            store = store, encoder = FakeFlac(), sampleRate = rate, channelCount = 1,
            clock = { startMs + 1_000 })
        session.start(clientId, startIso)

        val tail = sealFinalTail(session, List(rate) { 1.toShort() }, rate)   // 1 秒

        assertTrue("封段失败必须报出来：${tail.error}", tail.error.isNotBlank())
        assertEquals("失败时不许说'留下了'", "", tail.note)
        val note = tailStopNote(tail, 0, hadSession = true)
        assertTrue("原因要如实：$note", note.contains("可能真的丢了"))
        assertFalse("绝不能谎报已上传：$note", note.contains("已上传"))
        assertFalse("也不能说成'没有录到音频'（我们确实录到了，只是没存下来）：$note",
            note.contains("没有录到音频"))
    }

    /**
     * 护栏：**进程被杀时补不了尾** —— 这句事实必须被如实说出来。
     *
     * `sealFinalTail` 只在"用户按结束"那条路上跑；进程被系统杀掉时那不足一段的缓冲
     * 跟着进程一起没了。补传/收尾的文案里如果只说"结束时间是推的"，会让人以为音频
     * 是完整的 —— 所以必须点出"最后不足一段的缓冲可能没封存"。
     *
     * （真机进程被杀这件事在 JVM 里造不出来，但**要说的话**是纯函数，可以钉住。）
     */
    @Test
    fun `进程被杀时补不了尾 这句事实必须被如实说出来`() {
        val d = MeetingDraft(
            clientId = clientId, startedAt = startIso, endedAtInferred = true)
        val note = OfflineMeetingSync.inferredEndNote(listOf(d))
        assertTrue("打断这件事要说：$note", note.contains("被进程打断"))
        assertTrue("还要说清结束时间是推的：$note", note.contains("推的"))
        assertTrue("必须点出尾段补不了、可能丢了：$note", note.contains("不足一段"))
        assertTrue("不能把'可能'省掉（我们确实不知道丢了多少）：$note", note.contains("可能没封存"))
    }

    // ---- 字节层面的小工具（只看 PCM，不看容器头）----

    private fun shortAt(pcm: ByteArray, byteOffset: Int): Short =
        ByteBuffer.wrap(pcm, byteOffset, 2).order(ByteOrder.LITTLE_ENDIAN).short

    private fun pcmRangeIsSilent(pcm: ByteArray, fromByte: Int, toByte: Int): Boolean {
        var i = fromByte
        while (i < toByte) {
            if (shortAt(pcm, i) != 0.toShort()) return false
            i += 2
        }
        return true
    }

    private fun pcmRangeHasAudio(pcm: ByteArray, fromByte: Int, toByte: Int): Boolean {
        var i = fromByte
        while (i < toByte) {
            if (shortAt(pcm, i) != 0.toShort()) return true
            i += 2
        }
        return false
    }
}
