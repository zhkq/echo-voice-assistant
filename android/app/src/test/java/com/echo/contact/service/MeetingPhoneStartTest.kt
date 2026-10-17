package com.echo.contact.service

import com.echo.contact.audio.EncodedAudio
import com.echo.contact.audio.SegmentAudioEncoder
import com.echo.contact.audio.SegmentFormat
import com.echo.contact.data.ApiClient
import com.echo.contact.data.EchoApi
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
import retrofit2.http.POST
import retrofit2.http.Query

/**
 * 用户 2026-10-17 真机 bug 的客户端半边：**手机发起 = 手机录**。
 *
 * 现场是：手机上按「开始录音」→ 服务端收到的 `POST /api/meeting/start` **连一个 query 都没有**
 * （旧的 `EchoApi.meetingStart()`），于是电脑按**它自己那支麦**开了一场会；
 * 整个录音期间**零个** `/api/meeting/segment`（手机根本没录），转写里混进了电脑那边的人声。
 *
 * 这一组用例钉三件事（全部用真 HTTP，不是假 transport —— 参数形状只能真发一次才钉得住）：
 *
 *  1. 手机这一侧**真的**在本地录着（本地清单 + 音频段），建会请求带
 *     `source=phone` / `clientId` / `startedAt`，而且身份来自**本地录音清单**；
 *  2. **没有本地音频段就一个建会请求都不发** —— "建了一场手机麦的会、却一个音频段都没有"
 *     是另一种失败模式（空会占着服务端唯一那条录音槽，之后谁都开不了新会）；
 *  3. 接口面里**不再有**无参数的建会那条路（删掉它，而不是"留着不用"）。
 */
class MeetingPhoneStartTest {

    @get:Rule val tmp = TemporaryFolder()

    private lateinit var server: MockWebServer
    private lateinit var stub: EchoStub

    private val clientId = "phone-20261017134000-abcd"

    /**
     * 一个够真的假 ECHO（形状与真后端一致）：
     *  * `/api/meeting/start` 把 query **原样记下来**（`source`/`clientId`/`startedAt`）——
     *    这次 bug 的核心就是"这条请求上什么都没有"，所以断言必须落在它身上；
     *  * `/api/meeting/segment` 按 `(clientId, seq)` 记段并回报累计段数（对账用）；
     *  * `/api/meeting/status` 回报"现在在录哪一场"；
     *  * `/api/meeting/stop` 收尾。
     */
    private class EchoStub : Dispatcher() {
        /** 每一次建会请求的 query（**只记这三个**：契约里就这三样，别的都是猜）。 */
        val startQueries = mutableListOf<Map<String, String>>()

        /** 每一次建会请求的完整 path（断言"真的带了 query"）。 */
        val startPaths = mutableListOf<String>()

        val segments = linkedSetOf<Int>()
        var recording = false
        var statusClientId = ""
        var stops = 0

        override fun dispatch(request: RecordedRequest): MockResponse {
            val path = request.path!!.substringBefore('?')
            val url = request.requestUrl!!
            synchronized(this) {
                return when (path) {
                    "/health" -> MockResponse().setResponseCode(200)
                    "/api/meeting/status" -> json(
                        """{"active":$recording,"clientId":"$statusClientId",""" +
                            """"segments":${segments.size}}""")
                    "/api/meeting/start" -> {
                        startPaths += request.path!!
                        startQueries += listOf("source", "clientId", "startedAt")
                            .associateWith { url.queryParameter(it).orEmpty() }
                        val cid = url.queryParameter("clientId").orEmpty()
                        recording = true
                        statusClientId = cid
                        json("""{"ok":true,"message":"开始了","meetingId":7}""")
                    }
                    "/api/meeting/segment" -> {
                        val seq = url.queryParameter("seq")?.toIntOrNull() ?: -1
                        segments += seq
                        json("""{"ok":true,"meetingId":7,"segmentCount":${segments.size},""" +
                            """"duplicate":false}""")
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
    }

    /** 假 FLAC 编码器：吐一段带 `fLaC` 魔数的字节（真编码器的行为形状）。 */
    private class FakeFlac : SegmentAudioEncoder {
        override fun encode(pcm: ByteArray): EncodedAudio =
            EncodedAudio("fLaC".toByteArray() + ByteArray(32) { 5 }, SegmentFormat.FLAC)
    }

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    private fun session(store: MeetingDraftStore) = MeetingRecordSession(
        store = store, encoder = FakeFlac(), sampleRate = 16_000, channelCount = 1)

    private fun pcm(seconds: Double) = ByteArray((16_000 * 2 * seconds).toInt()) { 1 }

    private fun transport() =
        OfflineMeetingTransport(ApiClient.create(server.url("/").toString()))

    private fun api(): EchoApi = ApiClient.create(server.url("/").toString())

    private fun sync(store: MeetingDraftStore, scope: SyncScope): OfflineSyncResult = runBlocking {
        OfflineMeetingSync.run(
            transport = transport(), store = store, activeClientId = clientId, scope = scope)
    }

    // ---------------------------------------------------------------- ① 手机发起 = 手机录

    /**
     * 判据主线：**「开始录音」之后本地真的在录**，而与录音身份一起发出去的建会请求
     * 带的是 `source=phone` + 本地清单的 `clientId` / `startedAt`。
     *
     * 为什么断言 `startedAt` 等于清单里那一刻：这条请求的身份**只能**来自本地录音
     * （清单是 `MeetingRecordSession.start` 在开录那一刻写的）。时间来自"上传那一刻"
     * 就说明建会跟录音脱钩了 —— 那正是旧 bug 的形状。
     */
    @Test
    fun `手机发起就是手机录 建会请求带 source=phone 与本地清单身份（真 HTTP）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T13:40:00")

        // 点「开始录音」之后这一刻：**本地这一场已经立起来了**（清单在、录音中），
        // 但还没有任何音频段 —— 此时一个建会请求都不该有（见下面的用例②）。
        val local = store.load(clientId)!!
        assertTrue("本地这一场必须立起来（还在录）", local.recording)
        assertEquals("这时还没有音频段", 0, local.segments.size)

        // 第一段（直接用 1 秒 PCM；段长默认 600 秒，这里不靠攒满、手工封一段）→ 才有东西可传
        s.seal(pcm(1.0))

        val r = sync(store, SyncScope.INCREMENTAL)

        assertEquals("这一段被服务端认下了", 1, r.confirmedSegments)
        assertFalse("还在录 → **不许**收尾（收尾会让后面的段永远传不上去）", r.stopped)
        assertEquals("一次 stop 都不许有", 0, stub.stops)

        // ---- 判据：建会请求的每一个参数 ----
        assertEquals("一次录音只建一场会", 1, stub.startQueries.size)
        val q = stub.startQueries.single()
        assertEquals("采集源必须是**手机**（电脑侧不许被当成采集源）", "phone", q["source"])
        assertEquals("幂等键就是本地录音清单里这一场的 id", clientId, q["clientId"])
        assertEquals("开始时间用本地清单记的那一刻（不是上传那一刻）",
            "2026-10-17T13:40:00", q["startedAt"])
        assertTrue(
            "建会请求必须真的带 query —— 旧的无参数 meetingStart 一个都没有，那正是这次 bug：" +
                stub.startPaths.single(),
            stub.startPaths.single().contains("?"))
    }

    // ---------------------------------------------------------------- ② 两类动作同生同死

    /**
     * **绝不允许"建了手机麦会议但没在本地录音"**：本地一个音频段都没有时，
     * 这条统一路**一个请求都不发**（连探活都不发 —— `OfflineMeetingSync` 先看有没有待办）。
     *
     * 这条断言两个方向都要成立，所以用同一台假 ECHO 走两段：
     *  * 只有本地清单、没有音频 → 建会请求 = 0；
     *  * 写下一段音频之后 → 本地录音与建会请求**同时**成立（同一个 clientId）。
     *
     * 为什么这件事这么要紧：服务端一场"手机麦"的空会会占着**唯一**那条录音槽，
     * 之后电脑/手机都开不了新会（用户看到的会是"开始录音失败"，根因却在几天前）。
     */
    @Test
    fun `建会与本地录音同生同死 没有本地音频段就不许建会（真 HTTP）`() {
        val store = store()
        val s = session(store)
        s.start(clientId, "2026-10-17T13:40:00")

        // ---- 第一段：本地在录、但一段音频都还没有 ----
        val before = sync(store, SyncScope.FINALIZE)
        assertEquals("没有音频段 → 这一轮什么都不做", OfflinePhase.IDLE, before.phase)
        assertEquals("**一个请求都不发**（服务端不会出现一场空会）", 0, server.requestCount)
        assertTrue("更没有建会请求", stub.startQueries.isEmpty())
        assertTrue("但本地这一场**在**（录音中）", store.load(clientId)!!.recording)

        // ---- 第二段：写下一段音频，两件事同时成立 ----
        s.seal(pcm(1.0))
        sync(store, SyncScope.INCREMENTAL)

        assertEquals("有音频之后才建会（一次）", 1, stub.startQueries.size)
        assertEquals("建的就是本地这一场", clientId, stub.startQueries.single()["clientId"])
        val now = store.load(clientId)!!
        assertTrue("本地确实录到了这一段", now.segments.isNotEmpty())
        assertTrue("而且这场还在录（不是收尾）", now.recording)
        assertTrue("本地音频文件真的在（不是只记了清单）",
            store.audioFileOf(clientId, now.segments.first()).isFile)
    }

    // ---------------------------------------------------------------- ③ 接口面

    /**
     * **无参数的建会那条路必须不存在**（不是"留着不用"）。
     *
     * 这次 bug 的根因就是它有地方能调：类型里留着它，谁在 ViewModel 里顺手写一句
     * `api().meetingStart()`，同样的 bug 就回来了，而编译期一点提示都没有。
     * 删掉之后这里断言：
     *  * `POST api/meeting/start` 只有**一条**；
     *  * 这条必须带 `source` / `clientId` / `startedAt`（采集源与身份都写在请求上）；
     *  * 名为 `meetingStart` 的老方法不存在了。
     *
     * 剩下唯一"有地方能建会"的是 `OfflineMeetingSync` —— 而它只从**本地清单**出发
     * （没有本地录音就没有清单，没有清单就没有请求，见用例②）。
     */
    @Test
    fun `建会只剩一条路 无参数的 meetingStart 已经退役`() {
        val startMethods = EchoApi::class.java.declaredMethods.filter { m ->
            m.annotations.filterIsInstance<POST>().any { it.value == "api/meeting/start" }
        }
        assertEquals("POST api/meeting/start 只能有一条（无参数那条已删）",
            1, startMethods.size)
        val queries = startMethods.single().parameterAnnotations
            .flatMap { anns -> anns.filterIsInstance<Query>().map { it.value } }
        assertEquals("建会必须把采集源与身份写在请求上：$queries",
            listOf("source", "clientId", "startedAt", "endedAt"), queries)
        assertFalse("名叫 `meetingStart` 的无参数老方法必须已经不存在",
            EchoApi::class.java.declaredMethods.any { it.name == "meetingStart" })
    }

    /**
     * 不显式传 `source` 时**默认也是 `phone`**。
     *
     * 这条单独钉住"默认值"：`@Query` 的默认值写在接口上（`"phone"`），而它一旦被改成别的
     * （或者有人把这条参数删掉），所有建会请求都会悄悄退化成"电脑麦"—— 服务端那边不会再报错，
     * 只会安静地开电脑的麦。默认值读不出来，所以只能用真请求验。
     */
    @Test
    fun `不显式传 source 时默认也是 phone（不许退化成电脑麦）`() {
        // 这台假 ECHO 对 `/api/meeting/start` 恒回 `{ok:true}`（见 EchoStub），
        // 而且它会把**收到的** query 记下来 —— 正是这条用例要看的东西。
        runBlocking {
            api().meetingStartPhoneWithId(
                clientId = "c-1", startedAt = "2026-10-17T13:40:00")
        }

        val req = server.takeRequest()
        assertEquals("这次只该发建会那一条请求", "/api/meeting/start",
            req.path!!.substringBefore('?'))
        assertEquals("缺省采集源必须是手机", "phone", req.requestUrl!!.queryParameter("source"))
        assertEquals("后端签名里没有 body，我们也不发", "", req.body.readUtf8())
    }
}
