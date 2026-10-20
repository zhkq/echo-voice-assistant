package com.echo.contact.data

import com.echo.contact.data.dto.CommandRequest
import com.echo.contact.data.dto.DailyReviewSubmitRequest
import com.echo.contact.data.dto.PairClaimRequest
import com.google.gson.Gson
import kotlinx.coroutines.runBlocking
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 用一个**真的本地 HTTP 服务**（MockWebServer）钉住 App 发出去的线上行为：
 * 路径、请求体字段、鉴权头、以及 base URL 的归一化。
 *
 * 为什么必须单测这一层：这些错法**都不会编译报错**，而表现是 404 / 401 这种
 * 「看着像后端坏了」的现象。已经踩到过一次真问题 —— 后端是 `APIRouter(prefix="/api")`
 * （app/api.py:28），而配对回包里的 baseUrl 是**不带 `/api`** 的服务根
 * （`_phone_base_url()`，app/api.py:2462），所以路径必须自己带 `api/` 前缀。
 */
class EchoApiWireTest {

    private lateinit var server: MockWebServer
    private val gson = Gson()

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        ApiClient.attachTokenProvider { null }
    }

    @After
    fun tearDown() {
        ApiClient.attachTokenProvider { null }
        server.shutdown()
    }

    private fun json(body: String) = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(body)

    private fun api(base: String = server.url("/").toString()) = ApiClient.create(base)

    private fun bodyOf(req: okhttp3.mockwebserver.RecordedRequest): Map<*, *> =
        gson.fromJson(req.body.readUtf8(), Map::class.java)

    @Test
    fun `配对打到 api 前缀的路径 且只带 code 与 name 不带令牌`() {
        server.enqueue(
            json(
                """{"ok":true,"token":"tok-once","keyId":3,"name":"mobile",
                    "baseUrl":"http://192.168.1.5:8970","note":"令牌只出现这一次"}"""
            )
        )

        val resp = runBlocking { api().pairClaim(PairClaimRequest(code = "123456", name = "mobile")) }

        assertTrue(resp.isSuccessful)
        assertEquals("tok-once", resp.body()?.token)
        assertEquals("http://192.168.1.5:8970", resp.body()?.baseUrl)

        val req = server.takeRequest()
        assertEquals("/api/pair/phone/claim", req.path)
        assertEquals("POST", req.method)
        // 配对是**唯一**不带令牌的接口：这时候还没有令牌
        assertNull(req.getHeader("Authorization"))
        val sent = bodyOf(req)
        assertEquals("123456", sent["code"])
        assertEquals("mobile", sent["name"])
    }

    @Test
    fun `发指令带 Bearer 且路径与请求体都对`() {
        ApiClient.attachTokenProvider { "tok-123" }
        server.enqueue(json("""{"ok":true,"message":"已下发"}"""))

        val resp = runBlocking { api().sendCommand(CommandRequest(text = "把客厅灯打开", source = "mobile")) }

        assertTrue(resp.isSuccessful)
        assertEquals("已下发", resp.body()?.message)

        val req = server.takeRequest()
        assertEquals("/api/assistant/command", req.path)
        assertEquals("Bearer tok-123", req.getHeader("Authorization"))
        val sent = bodyOf(req)
        assertEquals("把客厅灯打开", sent["text"])
        assertEquals("mobile", sent["source"])
    }

    @Test
    fun `普通指令不带 context 与 requestId（老行为不变）`() {
        server.enqueue(json("""{"ok":true,"message":"已下发"}"""))

        runBlocking { api().sendCommand(CommandRequest(text = "普通指令", source = "mobile")) }

        val req = server.takeRequest()
        assertEquals("/api/assistant/command", req.path)
        assertNull("普通指令不该带 context", req.requestUrl!!.queryParameter("context"))
        assertNull("普通指令不该带 requestId", req.requestUrl!!.queryParameter("requestId"))
    }

    /**
     * **回顾那一场的一轮**：带 `context=review`（服务端据此记 `source='review'`，
     * 进回顾历史、不进指令历史）+ 同一轮的幂等键。
     */
    @Test
    fun `回顾轮次带 context review 与幂等键`() {
        server.enqueue(
            json("""{"ok":true,"message":"已提交","commandId":9,"source":"review"}""")
        )

        val resp = runBlocking {
            api().sendCommand(
                CommandRequest(text = "回顾这一轮", source = "mobile"),
                context = "review",
                requestId = "sess-1:1700000000000:7",
            )
        }
        assertTrue(resp.isSuccessful)
        assertEquals(9, resp.body()?.commandId)
        assertEquals("review", resp.body()?.source)

        val req = server.takeRequest()
        assertEquals("review", req.requestUrl!!.queryParameter("context"))
        assertEquals("sess-1:1700000000000:7", req.requestUrl!!.queryParameter("requestId"))
        // 请求体仍是既有形状（标签走 query，不改 body 契约）
        val sent = bodyOf(req)
        assertEquals("回顾这一轮", sent["text"])
        assertEquals("mobile", sent["source"])
    }

    @Test
    fun `回顾语音轮次同样带 context review`() {
        server.enqueue(
            json("""{"ok":true,"message":"已提交","text":"车上说的","commandId":10,"source":"review"}""")
        )

        runBlocking {
            api().voiceCommand(
                byteArrayOf(1, 2, 3, 4).toRequestBody("audio/wav".toMediaType()),
                context = "review",
                requestId = "sess-1:1700000000000:8",
            )
        }

        val req = server.takeRequest()
        assertEquals("review", req.requestUrl!!.queryParameter("context"))
        assertEquals("sess-1:1700000000000:8", req.requestUrl!!.queryParameter("requestId"))
        assertEquals("audio/wav", req.getHeader("Content-Type"))
    }

    @Test
    fun `按 id 取一条指令`() {
        server.enqueue(
            json("""{"id":42,"source":"review","text":"这一轮","status":"done",
                "reply":"好了","origin":"mobile","speakText":"好了"}""")
        )

        val resp = runBlocking { api().commandResult(42) }

        assertTrue(resp.isSuccessful)
        assertEquals(42, resp.body()?.id)
        assertEquals("review", resp.body()?.source)
        assertEquals("mobile", resp.body()?.origin)
        assertEquals("/api/commands/42", server.takeRequest().path)
    }

    @Test
    fun `空令牌时不带 Authorization 头`() {
        server.enqueue(json("""{"ok":true,"message":""}"""))

        runBlocking { api().sendCommand(CommandRequest(text = "hi", source = "mobile")) }

        assertNull(server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `历史走 api commands 且 limit 进查询串`() {
        server.enqueue(json("""{"total":0,"items":[]}"""))

        runBlocking { api().listCommands(mapOf("limit" to "30")) }

        assertEquals("/api/commands?limit=30", server.takeRequest().path)
    }

    @Test
    fun `不带的筛选参数不会出现在 URL 里`() {
        server.enqueue(json("""{"total":0,"items":[]}"""))

        runBlocking { api().listCommands(mapOf("limit" to "30", "q" to "天气")) }

        // 用 QueryMap 而不是逐个 @Query，就是为了"没传的键不出现"（空 offset=0&q= 会让路径难断言）
        val path = server.takeRequest().path
        assertEquals("/api/commands?limit=30&q=%E5%A4%A9%E6%B0%94", path)
    }

    /**
     * **探活走 `/health`，不是 `/api/status`**：前者无 `/api` 前缀、也不要令牌。
     * 拿 status 探活的话，令牌坏掉会得到 401，很容易被误判成"服务没起来"。
     */
    @Test
    fun `探活打的是不带 api 前缀的 health`() {
        server.enqueue(json("""{"ok":true}"""))

        val resp = runBlocking { api().health() }

        assertTrue(resp.isSuccessful)
        assertEquals("/health", server.takeRequest().path)
    }

    /**
     * 回顾提交：**收的是文本**，所以手机录完口述要先转写再提交到这里。
     */
    @Test
    fun `回顾提交把口述文本放进 body`() {
        server.enqueue(json("""{"ok":true,"spoken":"今天记了两件事。","source":"broadcast","seconds":12.5}"""))

        val resp = runBlocking { api().dailyReviewSubmit(DailyReviewSubmitRequest(text = "今天修好了后端")) }

        assertTrue(resp.isSuccessful)
        assertEquals("今天记了两件事。", resp.body()?.spoken)
        assertEquals("broadcast", resp.body()?.source)

        val req = server.takeRequest()
        assertEquals("/api/daily-review/submit", req.path)
        assertEquals("POST", req.method)
        val sent = bodyOf(req)
        assertEquals("今天修好了后端", sent["text"])
        assertEquals(false, sent["force_new"])
    }

    /**
     * 转写是 **multipart** —— 与 voice-command 的裸 body **正好相反**。
     * 这两条发错方向都会得到一个不像"我发错了"的错误
     * （裸 body 那条对 multipart 回 422 并明说"别用 multipart"）。
     */
    @Test
    fun `转写走 multipart 且带 file 字段`() {
        server.enqueue(json("""{"text":"今天开了个会","engine":"sensevoice","model":""}"""))

        val part = okhttp3.MultipartBody.Part.createFormData(
            "file", "dictation.wav",
            byteArrayOf(1, 2, 3).toRequestBody("audio/wav".toMediaType()),
        )
        val resp = runBlocking { api().transcribe(part) }

        assertTrue(resp.isSuccessful)
        assertEquals("今天开了个会", resp.body()?.text)

        val req = server.takeRequest()
        assertEquals("/api/stt/transcribe", req.path)
        val ctype = req.getHeader("Content-Type").orEmpty()
        assertTrue("必须是 multipart，实际=$ctype", ctype.startsWith("multipart/form-data"))
        val raw = req.body.readUtf8()
        assertTrue("要带 file 字段名", raw.contains("name=\"file\""))
        assertTrue("要带文件名", raw.contains("dictation.wav"))
    }

    @Test
    fun `不带 offset 时 URL 里没有 offset（第一页）`() {
        server.enqueue(json("""{"total":0,"items":[]}"""))

        runBlocking { api().listCommands(mapOf("limit" to "30")) }

        assertEquals("/api/commands?limit=30", server.takeRequest().path)
    }

    @Test
    fun `翻页时 offset 进查询串`() {
        server.enqueue(json("""{"total":0,"items":[]}"""))

        runBlocking { api().listCommands(mapOf("limit" to "30", "offset" to "30")) }

        assertEquals("/api/commands?limit=30&offset=30", server.takeRequest().path)
    }

    @Test
    fun `会议详情与产出文件的路径对得上`() {
        server.enqueue(json("""{"id":14,"title":"周会","speakers":[],"lines":[],"segment_count":0}"""))
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setHeader("Content-Type", "text/markdown; charset=utf-8")
                .setBody("# 纪要\n\n今天过了三件事。")
        )

        val detail = runBlocking { api().meetingDetail(14) }
        assertTrue(detail.isSuccessful)
        assertEquals("周会", detail.body()?.title)
        assertEquals("/api/meetings/14", server.takeRequest().path)

        val file = runBlocking { api().meetingFile(14, "summary") }
        assertTrue(file.isSuccessful)
        assertEquals("/api/meetings/14/file?kind=summary", server.takeRequest().path)
        // 正文当文本读（手机上直接显示）
        assertTrue(file.body()!!.string().contains("今天过了三件事"))
    }

    @Test
    fun `会议开始停止是不带 body 的 POST`() {
        server.enqueue(json("""{"ok":true,"message":"2026-10-09_22-00-00"}"""))
        server.enqueue(json("""{"ok":true,"message":"2026-10-09_22-00-00"}"""))

        // 建会只剩**一条路**（`source=phone` + 幂等键 + 开始时间）。无参数的 `meetingStart()`
        // 已经删掉 —— 2026-10-17 真机 bug 正是它引起的（服务端于是开电脑的麦）。
        runBlocking {
            api().meetingStartPhoneWithId(
                clientId = "c-1", startedAt = "2026-10-17T13:40:00")
        }
        runBlocking { api().meetingStop() }

        val a = server.takeRequest()
        assertEquals("/api/meeting/start", a.path!!.substringBefore('?'))
        assertEquals("POST", a.method)
        assertEquals("采集源写在 query 上（缺省就是手机）", "phone",
            a.requestUrl!!.queryParameter("source"))
        assertEquals("c-1", a.requestUrl!!.queryParameter("clientId"))
        assertEquals("2026-10-17T13:40:00", a.requestUrl!!.queryParameter("startedAt"))
        assertEquals("", a.body.readUtf8())        // 后端签名里没有 body，我们也不发
        val b = server.takeRequest()
        assertEquals("/api/meeting/stop", b.path)
        assertEquals("", b.body.readUtf8())
    }

    @Test
    fun `语音指令是裸 body 不是 multipart`() {
        server.enqueue(json("""{"ok":true,"message":"已提交","text":"你好","commandId":7,"source":"mobile"}"""))

        runBlocking {
            api().voiceCommand(byteArrayOf(1, 2, 3, 4).toRequestBody("audio/wav".toMediaType()))
        }

        val req = server.takeRequest()
        assertEquals("/api/assistant/voice-command", req.path)
        // 关键：Content-Type 必须是 audio/wav 本身，不能是 multipart/form-data
        //（后端对 multipart 会回 422 并明说"别用 multipart"）
        assertEquals("audio/wav", req.getHeader("Content-Type"))
        assertEquals(4, req.body.size)
    }

    @Test
    fun `会话路径把 id 放进路径里`() {
        server.enqueue(json("""{"ok":true,"session_id":"s1","messages":[],"exists":true,"running":false}"""))

        runBlocking { api().commandSession(42) }

        assertEquals("/api/commands/42/session", server.takeRequest().path)
    }

    @Test
    fun `base URL 归一化 带不带 api 与结尾斜杠都收`() {
        val root = server.url("/").toString().trimEnd('/') // http://127.0.0.1:<port>

        assertEquals("$root/", ApiClient.normalizeBaseUrl(root))
        assertEquals("$root/", ApiClient.normalizeBaseUrl("$root/"))
        assertEquals("$root/", ApiClient.normalizeBaseUrl("$root/api"))
        assertEquals("$root/", ApiClient.normalizeBaseUrl("$root/api/"))
        assertEquals("$root/", ApiClient.normalizeBaseUrl("  $root/  "))
    }

    @Test
    fun `用户粘了带 api 的地址也不会拼成 api api`() {
        server.enqueue(json("""{"ok":true,"message":""}"""))
        val baseWithApi = server.url("/").toString().trimEnd('/') + "/api"

        runBlocking { api(baseWithApi).sendCommand(CommandRequest(text = "hi", source = "mobile")) }

        // 若归一化失效，这里会是 /api/api/assistant/command → 真后端 404
        assertEquals("/api/assistant/command", server.takeRequest().path)
    }
}
