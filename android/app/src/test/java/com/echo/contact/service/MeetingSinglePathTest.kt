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
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * **合并之后那一条路，端到端跑一遍**（真 HTTP，不是假 transport）。
 *
 * 用户 2026-10-17 的原话是*"离线录音和在线录音就没什么本质区别了"*，目标形态是：
 * ```
 * 点录音 → 本地写段（不受任何服务端状态影响）
 *         → 每到一段封存：顺手尝试上传（网通 + 没冲突就传；否则留着）
 *         → 结束：补齐剩余段 → 对账 → stop(clientId) → DONE
 * ```
 * 这一组用例就是照那个形态走：一段一段封存 → 一路同步 → 结束 → 收尾，
 * 并且**两条用例走的是同一个 `MeetingRecordSession` + `OfflineMeetingSync`**
 * （区别只有"一开始有没有网"）—— 这正是"合并成一条路"的判据。
 *
 * 为什么必须用真 HTTP（`MockWebServer`）而不是假的 transport：这一轮要钉的东西里有
 * **字节层面的契约**（`Content-Type: audio/flac`、`POST /api/meeting/pauses` 的 JSON body、
 * `clientId`/`seq` 到底怎么发的）。假 transport 里这些是**我自己**拼的，
 * 拼错了它照样通过 —— 只有真发一次请求才钉得住。
 */
class MeetingSinglePathTest {

    @get:Rule val tmp = TemporaryFolder()

    private lateinit var server: MockWebServer

    private val clientId = "phone-20261017093000-abcd"

    /**
     * 一个够真的假 ECHO：
     *  * `/health` 恒 200（探活）；
     *  * `/api/meeting/status` 按 [recording] / [statusClientId] 回报"现在在录哪一场"；
     *  * `/api/meeting/start` 按 `clientId` **幂等**（同一个 id 只算一场，id 固定 7）；
     *  * `/api/meeting/segment` 按 `(clientId, seq)` **幂等**，并回报**累计段数**（对账用它）；
     *  * `/api/meeting/pauses` 可以不认这个端点（[pausesCode]）—— 老服务端就是这样；
     *  * `/api/meeting/stop` 收尾。
     */
    private class EchoStub(
        /** `/api/meeting/pauses` 回什么码（404 = 老服务端没这个端点）。 */
        var pausesCode: Int = 200,
        /** 暂停上报"卡住/断开"（演超时那一档）。 */
        var pausesDrop: Boolean = false,
    ) : Dispatcher() {
        val starts = mutableListOf<String>()
        val segments = LinkedHashMap<String, MutableSet<Int>>()
        val pauseBodies = mutableListOf<String>()
        var stops = 0
        var recording = false
        var statusClientId = ""
        val statusBodies = mutableListOf<String>()
        private val lock = Any()

        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.path!!.substringBefore('?')
            val q = request.requestUrl!!
            synchronized(lock) {
                return when (path) {
                    "/health" -> MockResponse().setResponseCode(200)
                    "/api/meeting/status" -> {
                        val body = """{"active":$recording,"clientId":"$statusClientId",""" +
                            """"segments":${segments.values.sumOf { it.size }}}"""
                        statusBodies += body
                        json(body)
                    }
                    "/api/meeting/start" -> {
                        val cid = q.queryParameter("clientId").orEmpty()
                        starts += cid
                        recording = true
                        statusClientId = cid
                        json("""{"ok":true,"message":"开始了","meetingId":7}""")
                    }
                    "/api/meeting/segment" -> {
                        val cid = q.queryParameter("clientId").orEmpty()
                        val seq = q.queryParameter("seq")?.toIntOrNull() ?: -1
                        val set = segments.getOrPut(cid) { linkedSetOf() }
                        val dup = !set.add(seq)
                        json("""{"ok":true,"meetingId":7,"segmentCount":${set.size},""" +
                            """"duplicate":$dup}""")
                    }
                    "/api/meeting/pauses" -> {
                        pauseBodies += request.body.readUtf8()
                        when {
                            pausesDrop -> MockResponse().setSocketPolicy(
                                okhttp3.mockwebserver.SocketPolicy.DISCONNECT_AT_START)
                            pausesCode in 200..299 ->
                                json("""{"ok":true,"message":"收到了"}""")
                            else -> MockResponse().setResponseCode(pausesCode)
                                .setBody("""{"detail":"Not Found"}""")
                        }
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

    private lateinit var stub: EchoStub

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
    }

    /** 假 FLAC 编码器：吐一段带 `fLaC` 魔数的字节（真编码器的行为形状）。 */
    private class FakeFlac : SegmentAudioEncoder {
        override fun encode(pcm: ByteArray): EncodedAudio =
            EncodedAudio("fLaC".toByteArray() + ByteArray(32) { 5 }, SegmentFormat.FLAC)
    }

    private class FakeWav : SegmentAudioEncoder {
        override fun encode(pcm: ByteArray): EncodedAudio =
            EncodedAudio(WavWriter.wrap(pcm, 16_000, 1, 16), SegmentFormat.WAV,
                note = com.echo.contact.audio.SegmentEncoding.FLAC_UNAVAILABLE)
    }

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    private fun session(store: MeetingDraftStore, encoder: SegmentAudioEncoder = FakeFlac()) =
        MeetingRecordSession(
            store = store, encoder = encoder, sampleRate = 16_000, channelCount = 1)

    private fun pcm(seconds: Double) = ByteArray((16_000 * 2 * seconds).toInt()) { 1 }

    private fun transport() = OfflineMeetingTransport(ApiClient.create(server.url("/").toString()))

    /** 一个**连不上**的 transport（把 MockWebServer 关掉之后的那个端口）。 */
    private fun offlineTransport(): OfflineMeetingTransport {
        val dead = MockWebServer()
        dead.start()
        val url = dead.url("/").toString()
        dead.shutdown()                     // 端口立刻空出来 → connect refused
        return OfflineMeetingTransport(ApiClient.create(url))
    }

    private fun sync(
        store: MeetingDraftStore,
        t: OfflineTransport,
        scope: SyncScope,
        activeClientId: String? = null,
    ): OfflineSyncResult = runBlocking {
        OfflineMeetingSync.run(
            transport = t, store = store, scope = scope, activeClientId = activeClientId)
    }

    private fun drain(): List<RecordedRequest> {
        val out = mutableListOf<RecordedRequest>()
        while (true) out += server.takeRequest(200, java.util.concurrent.TimeUnit.MILLISECONDS)
            ?: break
        return out
    }

    // ---------------------------------------------------------------- ① 一开始不联网

    /**
     * 判据里那条主线：**点录音（不联网）→ 录两段 → 联网 → 自动补齐 → 对账 → stop 全绿**。
     */
    @Test
    fun `不联网录两段 联网后自动补齐到对账与收尾（真 HTTP）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T09:30:00")

        // ---- 不联网那两段：本地必须全都写下来 ----
        s.seal(pcm(1.0))
        s.seal(pcm(1.0))
        val off = sync(store, offlineTransport(), SyncScope.INCREMENTAL)
        assertEquals("连不上就走 UNREACHABLE 那一档（不假装在传）",
            OfflinePhase.UNREACHABLE, off.phase)
        assertEquals(2, off.pendingSegments)
        assertEquals("两段都要在本地（一个字节都没上去，那就一个字节都不许丢）",
            listOf("seg-0001.flac", "seg-0002.flac"),
            store.load(clientId)!!.segments.map { it.file })
        assertFalse("连不上时**不许**说已上传", store.load(clientId)!!.uploaded)

        // ---- 结束（本地立刻生效，不依赖服务端）----
        val done = s.finish()!!
        assertFalse("结束必须当场生效（不能等网络）", done.recording)
        assertEquals(2, done.segments.size)

        // ---- 联网：补齐 → 对账 → stop ----
        val r = sync(store, transport(), SyncScope.FINALIZE)

        assertEquals("对账通过并且服务端收尾了，才敢说完成：$r", OfflinePhase.DONE, r.phase)
        assertTrue("要能说'已开始转写'", r.stopped)
        assertTrue(store.load(clientId)!!.uploaded)
        assertTrue("确认之后音频仍然留着（宁可多留一会儿也别丢音频）",
            (1..2).all { store.segmentFile(clientId, it, SegmentFormat.FLAC).isFile })

        val reqs = drain()
        val paths = reqs.map { it.path!!.substringBefore('?') }
        assertEquals("顺序不许乱：探活 → 状态 → 建会 → 两段 → 收尾",
            listOf("/health", "/api/meeting/status", "/api/meeting/start",
                "/api/meeting/segment", "/api/meeting/segment", "/api/meeting/stop"), paths)

        // ---- 字节层面的契约 ----
        val segReqs = reqs.filter { it.path!!.startsWith("/api/meeting/segment") }
        assertEquals(listOf("1", "2"), segReqs.map { it.requestUrl!!.queryParameter("seq") })
        segReqs.forEach { req ->
            assertEquals("幂等键必须是同一个客户端 id",
                clientId, req.requestUrl!!.queryParameter("clientId"))
            assertEquals("FLAC 那些段必须按 audio/flac 发（服务端按 fLaC 魔数识别）",
                "audio/flac", req.getHeader("Content-Type"))
            assertTrue("发的就是本地那份 FLAC 字节（带魔数）",
                req.body.readUtf8().startsWith("fLaC"))
        }
        val startReq = reqs.first { it.path!!.startsWith("/api/meeting/start") }
        assertEquals("只用同一个 clientId 建一场会", clientId,
            startReq.requestUrl!!.queryParameter("clientId"))
        assertEquals("开始时间用清单里那一刻（不是补传那一刻）", "2026-10-17T09:30:00",
            startReq.requestUrl!!.queryParameter("startedAt"))
        val stopReq = reqs.first { it.path!!.startsWith("/api/meeting/stop") }
        assertEquals("收尾必须带上这一场自己的 id（服务端靠它拒绝停错会）",
            clientId, stopReq.requestUrl!!.queryParameter("clientId"))
        assertEquals("一场只收尾一次", 1, stub.stops)
    }

    // ---------------------------------------------------------------- ② 一开始就联网

    /**
     * **同一条路**在"一开始就联网"时也全绿 —— 而且是**边录边传**（最多滞后一段）。
     *
     * 判据里还藏着一条最重要的：**还在录的那一份绝不能被收尾**。
     * 收尾 = 服务端立刻落盘开始转写，而麦克风还在录 → 后面的段再也传不上去
     * （本地那份还会被标成"已完成"）。所以这里专门断言"第一段传完时一次 stop 都没有"。
     */
    @Test
    fun `一开始就联网时同一条路边录边传 且录音期间绝不收尾（真 HTTP）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T09:30:00")

        // ---- 第一段：封存完顺手传一次（这时还在录）----
        s.seal(pcm(1.0))
        val r1 = sync(store, transport(), SyncScope.INCREMENTAL, activeClientId = clientId)

        assertEquals("服务端已经认下第 1 段（最多滞后一段）", 1, r1.confirmedSegments)
        assertEquals(0, r1.pendingSegments)
        assertFalse("还在录 → **不许**说完成", r1.phase == OfflinePhase.DONE)
        assertFalse("**一次 stop 都不许有**（提前收尾会让后面的段永远传不上去）", r1.stopped)
        assertEquals(0, stub.stops)
        assertFalse("还在录的那一份不许被标已上传", store.load(clientId)!!.uploaded)

        // ---- 第二段：同样先落本地，再顺手传 ----
        s.seal(pcm(1.0))
        val r2 = sync(store, transport(), SyncScope.INCREMENTAL, activeClientId = clientId)
        assertEquals(2, r2.confirmedSegments)
        assertEquals(0, stub.stops)
        assertTrue(store.load(clientId)!!.recording)

        // ---- 结束：补齐剩余段 → 对账 → stop ----
        s.finish()
        val r3 = sync(store, transport(), SyncScope.FINALIZE, activeClientId = clientId)

        assertEquals("对账通过并且服务端收尾了才敢说完成：$r3（status=${stub.statusBodies}）",
            OfflinePhase.DONE, r3.phase)
        assertTrue("只有走到最后一步（stop）才敢说开始转写", r3.stopped)
        assertEquals("整场只收尾一次", 1, stub.stops)
        assertEquals("服务端手上这一段是 1、2（不重不漏）",
            listOf(1, 2), stub.segments[clientId]!!.toList())
        assertTrue(store.load(clientId)!!.uploaded)

        // 同一条路：请求形状与"一开始不联网"那条**完全一样**（这就是"没有本质区别"）。
        // 最后一轮多了一次 `/api/meeting/status`：这一轮没有新段要传，段数只能问状态
        // （对账的另一个来源）—— 拿不到它就**不许**说完成（见 reconcile 的说明）。
        val reqs = drain()
        assertEquals("两次顺手传 + 最后收尾，没有别的花样",
            listOf("/health", "/api/meeting/status", "/api/meeting/start", "/api/meeting/segment",
                "/health", "/api/meeting/status", "/api/meeting/start", "/api/meeting/segment",
                "/health", "/api/meeting/status", "/api/meeting/start", "/api/meeting/status",
                "/api/meeting/stop"),
            reqs.map { it.path!!.substringBefore('?') })
        reqs.filter { it.path!!.startsWith("/api/meeting/segment") }.forEach {
            assertEquals("audio/flac", it.getHeader("Content-Type"))
        }
    }

    /**
     * 编码器编不出来时的那条**真实**链路：清单记 WAV、上传用 `audio/wav`（两条路都要有判据）。
     */
    @Test
    fun `编码器不可用时同一条路照样走完 但发的是 audio wav（真 HTTP）`() {
        val store = store()
        val s = session(store, encoder = FakeWav())
        s.start(clientId, "2026-10-17T09:30:00")
        s.seal(pcm(1.0))
        s.seal(pcm(1.0))
        s.finish()

        val r = sync(store, transport(), SyncScope.FINALIZE)

        assertEquals(OfflinePhase.DONE, r.phase)
        val reqs = drain()
        reqs.filter { it.path!!.startsWith("/api/meeting/segment") }.forEach { req ->
            assertEquals("清单里记的是 WAV，发出去就必须是 audio/wav（绝不谎报）",
                "audio/wav", req.getHeader("Content-Type"))
            assertFalse("发出去的字节里没有 fLaC 魔数（它本来就不是 FLAC）",
                req.body.readUtf8().startsWith("fLaC"))
        }
        assertEquals("清单里如实记 WAV",
            listOf("seg-0001.wav", "seg-0002.wav"),
            store.load(clientId)!!.segments.map { it.file })
    }

    // ---------------------------------------------------------------- ③ 暂停上报

    @Test
    fun `start 之后上报暂停区间（按 clientId 幂等 整份重报）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T09:30:00")
        s.seal(pcm(1.0))
        s.beginPause()
        s.endPause()
        s.finish()

        sync(store, transport(), SyncScope.FINALIZE)

        assertEquals("暂停区间报上去了（一次）", 1, stub.pauseBodies.size)
        val body = stub.pauseBodies.single()
        assertTrue("按契约：clientId + pauses[{from,to}]：$body",
            body.contains("\"clientId\":\"$clientId\""))
        assertTrue(body.contains("\"pauses\""))
        assertTrue(body.contains("\"from\""))
        assertTrue(body.contains("\"to\""))
        assertTrue("契约里没有 by，客户端就不发明：$body", !body.contains("\"by\""))
        assertTrue("报的是**整份**当前暂停区间（服务端合并去重）",
            body.contains("0") || body.contains("0.0"))
        assertEquals("上报成功了就不该留一句'没报上去'",
            "", store.load(clientId)!!.pauseNote)
    }

    @Test
    fun `老服务端没有 pauses 端点（404）也不算失败 补传照常走完`() {
        stub.pausesCode = 404
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T09:30:00")
        s.seal(pcm(1.0))
        s.beginPause()
        s.endPause()
        s.finish()

        val r = sync(store, transport(), SyncScope.FINALIZE)

        assertEquals("**端点不存在绝不许拖垮补传**：这一段照常传完并收尾",
            OfflinePhase.DONE, r.phase)
        assertTrue(r.stopped)
        assertEquals(1, stub.stops)
        assertTrue("要如实记一句（用户点名要求）：${r.note}", r.note.contains("没报上去"))
        assertTrue("而且要能被下一次同步看到（落进清单）",
            store.load(clientId)!!.pauseNote.contains("没报上去"))
        assertTrue("音频一段不少", store.segmentFile(clientId, 1, SegmentFormat.FLAC).isFile)
    }

    @Test
    fun `暂停上报超时（连接断）也不算失败 补传照常走完`() {
        stub.pausesDrop = true
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T09:30:00")
        s.seal(pcm(1.0))
        s.beginPause()
        s.endPause()
        s.finish()

        val r = sync(store, transport(), SyncScope.FINALIZE)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue(r.stopped)
        assertTrue("原因要说准（这是没通，不是'端点不存在'）：${r.note}",
            r.note.contains("没报上去"))
        assertTrue(store.load(clientId)!!.uploaded)
    }
}
