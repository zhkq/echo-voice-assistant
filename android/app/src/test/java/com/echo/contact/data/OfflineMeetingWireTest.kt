package com.echo.contact.data

import com.echo.contact.audio.SegmentFormat
import com.echo.contact.service.ActiveMeeting
import com.echo.contact.service.CountOutcome
import com.echo.contact.service.DraftPause
import com.echo.contact.service.OfflineMeetingTransport
import com.echo.contact.service.PauseOutcome
import com.echo.contact.service.PauseRequest
import com.echo.contact.service.SegmentOutcome
import com.echo.contact.service.StartOutcome
import com.echo.contact.service.StartRequest
import com.echo.contact.service.StopOutcome
import com.echo.contact.service.StopRequest
import com.echo.contact.service.SyncFailure
import com.echo.contact.service.UploadRequest
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 离线补传那条路的**线路契约**（用一个真的本地 HTTP 服务钉住）：
 * 路径、`clientId`/`seq`/`startedAt` 这些参数到底怎么发的、以及**宽容解析**。
 *
 * 为什么这一层必须有用例：服务端这一轮**不许改**（另一条线在重建后端镜像），
 * 于是"客户端按什么契约发"就是这一轮唯一能钉住的东西 —— 而错了的方式**全都不会编译报错**：
 *  * 参数名写成 `client_id` 而不是 `clientId` → 服务端静默忽略 → 幂等失效
 *    （一次超时重发就在电脑上多出一场会）；
 *  * 段序号没带上 → 服务端没法做幂等 → 同一段存两遍（转写里同一句出现两遍）；
 *  * 回包字段名认错 → 对账永远"拿不到段数"（功能不算坏，但会**一直不许说已上传**）。
 */
class OfflineMeetingWireTest {

    private lateinit var server: MockWebServer

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

    private fun api() = ApiClient.create(server.url("/").toString())

    private fun json(body: String) = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(body)

    private fun query(req: RecordedRequest): Map<String, String> =
        req.requestUrl!!.queryParameterNames.associateWith { req.requestUrl!!.queryParameter(it)!! }

    // ---------------------------------------------------------------- 建会

    @Test
    fun `建会带上 clientId startedAt endedAt source=phone 且不打请求体`() {
        server.enqueue(json("""{"ok":true,"message":"开始了","meetingId":42}"""))

        val out = runBlocking {
            OfflineMeetingTransport(api()).startMeeting(
                StartRequest(
                    clientId = "phone-20261016203102-abcd",
                    startedAt = "2026-10-16T20:31:02",
                    endedAt = "2026-10-16T20:35:00",
                    segments = 8,
                )
            )
        }

        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/api/meeting/start", req.path!!.substringBefore('?'))
        assertEquals("必须带上幂等键（服务端据此只建一场会）",
            "phone-20261016203102-abcd", query(req)["clientId"])
        assertEquals("开始时间要用缓存的那一刻（不是补传那一刻）",
            "2026-10-16T20:31:02", query(req)["startedAt"])
        assertEquals("phone", query(req)["source"])
        assertEquals("补传时已经录完了", "2026-10-16T20:35:00", query(req)["endedAt"])
        // 与**服务端签名**逐字对齐（`app/api.py::meeting_start` 只收 source/clientId/
        // startedAt/endedAt）：多发的参数会被静默忽略，而"客户端以为它在起作用"是最难查的错法。
        assertEquals("发出的 query 只能是服务端签名里那四个",
            setOf("clientId", "startedAt", "endedAt", "source"), req.requestUrl!!.queryParameterNames)
        assertEquals("**不带请求体**：后端这条签名里只有 _auth，多一个 JSON body 只是白搭",
            0, req.bodySize)

        assertTrue(out is StartOutcome.Ok)
        assertEquals(42, (out as StartOutcome.Ok).meetingId)
    }

    @Test
    fun `后端恒回 200 时 成没成看 ok`() {
        server.enqueue(json("""{"ok":false,"message":"正在录另一场会议"}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).startMeeting(
                StartRequest(clientId = "c1", startedAt = "t"))
        }
        assertTrue("HTTP 200 但 ok=false 就是没成：$out", out is StartOutcome.Failed)
        out as StartOutcome.Failed
        assertTrue("要把服务端那句话带上：${out.note}", out.note.contains("正在录另一场会议"))
        assertTrue("'正在录'是可重试的（等它录完就行），不是'拒绝'", out.retryable)
    }

    @Test
    fun `服务端拒绝时不重试`() {
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail":"不允许"}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).startMeeting(
                StartRequest(clientId = "c1", startedAt = "t"))
        }
        out as StartOutcome.Failed
        assertTrue("403 是契约/权限问题，重试也不会好：${out.note}", !out.retryable)
    }

    @Test
    fun `连不上时不抛异常 而是可重试的失败`() {
        server.shutdown()      // 模拟"网断了/服务没起"
        val out = runBlocking {
            OfflineMeetingTransport(api()).startMeeting(
                StartRequest(clientId = "c1", startedAt = "t"))
        }
        assertTrue(out is StartOutcome.Failed)
        assertTrue("连不上当然可以重试", (out as StartOutcome.Failed).retryable)
    }

    // ---------------------------------------------------------------- 传段

    @Test
    fun `传段带上 clientId 与 seq 且是裸 wav`() {
        server.enqueue(json("""{"ok":true,"meetingId":42,"segmentCount":1}"""))

        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest(
                    clientId = "phone-1", seq = 3, startedAt = "2026-10-16T20:31:02",
                    source = "phone", wav = ByteArray(16) { 7 }))
        }

        val req = server.takeRequest()
        assertEquals("/api/meeting/segment", req.path!!.substringBefore('?'))
        assertEquals("幂等键（服务端按 (clientId, seq) 判重）", "phone-1", query(req)["clientId"])
        assertEquals("段序号（对账与续传的基准）", "3", query(req)["seq"])
        assertEquals("audio/wav；**不能**发 multipart（那条接口不收）",
            "audio/wav", req.getHeader("Content-Type"))
        assertEquals(16, req.bodySize)
        assertTrue(out is SegmentOutcome.Ok)
        assertEquals(1, (out as SegmentOutcome.Ok).serverCount)
    }

    @Test
    fun `传段用的是清单里记的那一份格式（FLAC 就发 audio flac）`() {
        // 用户 2026-10-17 的第①件事：段改成 FLAC，服务端按 `fLaC` 魔数识别 ——
        // 于是 `Content-Type` 必须跟着**清单里记的那一份**走。
        // 写死成 audio/wav 的话，FLAC 那些段会被当成 wav 收下（= 谎报格式，转写那边解不出来）。
        server.enqueue(json("""{"ok":true,"meetingId":42,"segmentCount":1}"""))

        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest(
                    clientId = "phone-1", seq = 1, startedAt = "2026-10-17T09:30:00",
                    source = "phone", wav = "fLaC".toByteArray() + ByteArray(8),
                    format = SegmentFormat.FLAC))
        }

        val req = server.takeRequest()
        assertEquals("这是契约的另一半（服务端按它 + 魔数认格式）",
            "audio/flac", req.getHeader("Content-Type"))
        assertTrue(out is SegmentOutcome.Ok)
    }

    @Test
    fun `退回 WAV 的那些段仍然发 audio wav（两条路都要有）`() {
        server.enqueue(json("""{"ok":true,"meetingId":42,"segmentCount":1}"""))
        runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("phone-1", 1, "2026-10-17T09:30:00", "phone",
                    ByteArray(4), format = SegmentFormat.WAV))
        }
        assertEquals("audio/wav", server.takeRequest().getHeader("Content-Type"))
    }

    @Test
    fun `段回包只给 segments 数组时也要能算段数`() {        // 既有 GET /api/meeting/status 那一族的形状（最可能被复用的那个）
        server.enqueue(json("""{"ok":true,"segments":[{"seq":1},{"seq":2}]}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("c", 2, "t", "phone", ByteArray(4)))
        }
        assertEquals(2, (out as SegmentOutcome.Ok).serverCount)
    }

    @Test
    fun `段回包给 seqCount 时也要认得`() {
        server.enqueue(json("""{"ok":true,"seqCount":5}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("c", 5, "t", "phone", ByteArray(4)))
        }
        assertEquals(5, (out as SegmentOutcome.Ok).serverCount)
    }

    @Test
    fun `段回包什么都没说时 serverCount 是 null（不许编一个数出来）`() {
        server.enqueue(json("""{"ok":true}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("c", 1, "t", "phone", ByteArray(4)))
        }
        assertNull("拿不到段数 = 没法对账（调用方据此**不**标已上传）",
            (out as SegmentOutcome.Ok).serverCount)
    }

    @Test
    fun `服务端说这段是重复的时算成功`() {
        server.enqueue(json("""{"ok":true,"duplicate":true,"segmentCount":3}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("c", 2, "t", "phone", ByteArray(4)))
        }
        assertTrue("幂等命中本来就是我们要的（重试不会存两遍）",
            (out as SegmentOutcome.Ok).duplicate)
    }

    @Test
    fun `服务端明确拒绝这一段时不重试`() {
        server.enqueue(json("""{"ok":false,"message":"这段音频格式不对"}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("c", 1, "t", "phone", ByteArray(4)))
        }
        out as SegmentOutcome.Failed
        assertTrue("要说清是第几段 + 服务端原话：${out.note}", out.note.contains("第 1 段"))
        assertTrue(out.note.contains("格式不对"))
        assertTrue("4xx 类拒绝：重试也不会好", !out.retryable)
    }

    @Test
    fun `服务端忙时说清楚是等一等（可重试）`() {
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"detail":"转写中"}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).uploadSegment(
                UploadRequest("c", 1, "t", "phone", ByteArray(4)))
        }
        out as SegmentOutcome.Failed
        assertTrue(out.retryable)
        assertEquals(com.echo.contact.service.SyncFailure.SERVER_BUSY, out.failure)
    }

    // ---------------------------------------------------------------- 状态（不抢占的判据）

    @Test
    fun `状态里没有 clientId 时也读得出来（老服务端）`() {
        server.enqueue(json("""{"active":true,"folder":"2026-10-16_20-00-00","segments":4,"level":0.1}"""))
        val active = runBlocking { OfflineMeetingTransport(api()).activeMeeting() }
        assertEquals(ActiveMeeting(recording = true, meetingId = 0, source = "",
            note = "2026-10-16_20-00-00", clientId = ""), active)
    }

    @Test
    fun `状态里有 clientId 与 meetingId 时读得出来（补传据此判是不是自己那一场）`() {
        server.enqueue(json("""{"active":true,"clientId":"phone-1","meetingId":42,"source":"phone","segments":2}"""))
        val active = runBlocking { OfflineMeetingTransport(api()).activeMeeting() }!!
        assertEquals("phone-1", active.clientId)
        assertEquals(42, active.meetingId)
        assertEquals("phone", active.source)
    }

    @Test
    fun `对账：认得出这一场时用状态里的段数`() {
        server.enqueue(json("""{"active":false,"clientId":"phone-1","meetingId":42,"segments":6}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).segmentCount("phone-1", 42)
        }
        assertEquals(6, (out as CountOutcome.Ok).count)
    }

    @Test
    fun `对账：认不出这一场时如实说没法对账（而不是拿别人的段数交差）`() {
        server.enqueue(json("""{"active":true,"clientId":"other","meetingId":7,"segments":9}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).segmentCount("phone-1", 42)
        }
        assertTrue("拿别的会的段数冒充对账 = 谎报已上传：$out", out is CountOutcome.Failed)
    }

    @Test
    fun `读不到状态时不算可达`() {
        server.enqueue(MockResponse().setResponseCode(500))
        val active = runBlocking { OfflineMeetingTransport(api()).activeMeeting() }
        assertNull("读不到状态就不敢往下走（调用方算不可达）", active)
    }

    // ---------------------------------------------------------------- 收尾（修①：没有 stop 就没有转写）

    @Test
    fun `收尾打的是 meeting stop 而且带上这一场的 clientId 不带请求体`() {
        server.enqueue(json("""{"ok":true,"message":"已开始转写"}"""))

        val out = runBlocking {
            OfflineMeetingTransport(api()).stopMeeting(
                StopRequest(clientId = "phone-20261016203102-abcd", meetingId = 42))
        }

        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/api/meeting/stop", req.path!!.substringBefore('?'))
        assertEquals("**必须**带上这一场的 clientId：服务端靠它拒绝'停错会'" +
            "（别人正在录的那场一个字节都不动）；不发的话护栏在真机流程里永远不生效",
            "phone-20261016203102-abcd", query(req)["clientId"])
        assertEquals("发出去的只能是这一场自己的 id（不是别的清单/当前活动那场的 id）",
            setOf("clientId"), req.requestUrl!!.queryParameterNames)
        assertEquals("`meetingId` 不发：服务端那条签名里没有它（发了也是静默被忽略）",
            null, query(req)["meetingId"])
        assertEquals("**不带请求体**", 0, req.bodySize)
        assertTrue("收下了就是成功：$out", out is StopOutcome.Ok)
        assertTrue(!(out as StopOutcome.Ok).alreadyStopped)
    }

    @Test
    fun `收尾时手上没有这一场的 id 就一个 query 都不发（退化成老行为）`() {
        server.enqueue(json("""{"ok":true,"message":"已开始转写"}"""))

        runBlocking { OfflineMeetingTransport(api()).stopMeeting(StopRequest()) }

        val req = server.takeRequest()
        assertEquals("空 id 不许发成 `clientId=`：那不叫'带上了 id'，" +
            "服务端契约上也只是白白多一个会被忽略的参数 —— 老行为要逐字节保持",
            0, req.requestUrl!!.querySize)
    }

    @Test
    fun `收尾时服务端说没有进行中的会议 算成功（幂等 不许卡死）`() {
        // 真服务端的回复（`app/meeting.py::stop_meeting` 第一行）：HTTP 200 + ok=false。
        // 这不是失败 —— 它是"这场之前就停过了"，卡在这一档会让清单**永远**留在待办里。
        server.enqueue(json("""{"ok":false,"message":"没有进行中的会议"}"""))

        val out = runBlocking { OfflineMeetingTransport(api()).stopMeeting(StopRequest("c", 1)) }

        assertTrue("幂等命中必须算成功：$out", out is StopOutcome.Ok)
        assertTrue((out as StopOutcome.Ok).alreadyStopped)
    }

    /**
     * 服务端护栏那句**拒绝**（`app/meeting.py::stop_meeting` 里 clientId 不符那一档）
     * 与"没有进行中的会议"**不是**一回事：它是**明确说"这场不是我录的那一场"**。
     *
     * 判错的后果是这一整轮修复里最坏的一种：一场**根本没传完/没收尾**的录音被标成
     * "已完成、已开始转写" —— 而那一场还占着服务端唯一那条录音槽、一个字都还没转。
     */
    @Test
    fun `收尾时服务端因 clientId 不符而拒绝 不算成功 可重试 缓存不删`() {
        // 真服务端那句话（`app/meeting.py::stop_meeting` 的护栏分支），逐字抄的
        server.enqueue(json("""{"ok":false,"message":"这场会议不是 clientId=phone-1 那一场""" +
            """（服务端正在录别的会），已拒绝收尾：不会停掉别人的会议"}"""))

        val out = runBlocking {
            OfflineMeetingTransport(api()).stopMeeting(StopRequest(clientId = "phone-1", meetingId = 1))
        }

        assertTrue("**绝不许**被当成'这场早就停过了'（那会把没收尾说成已转写）：$out",
            out is StopOutcome.Failed)
        out as StopOutcome.Failed
        assertTrue("这是'等那场录完再收尾'，不是契约问题 —— 别把用户引去查契约：$out", out.retryable)
        assertEquals(com.echo.contact.service.SyncFailure.SERVER_BUSY, out.failure)
        assertTrue("原因要照带服务端原话：${out.note}", out.note.contains("不是 clientId=phone-1"))
        assertTrue("要点明**还没有开始转写**（那才是用户关心的结果）：${out.note}",
            out.note.contains("还没有开始转写"))
        assertTrue("音频已经全传上去了，只是没收尾 —— 这一档要能分开说", out.alreadyUploadedHint)
    }

    @Test
    fun `收尾时服务端真拒绝 不算成功`() {
        server.enqueue(json("""{"ok":false,"message":"这台后端不认识 clientId"}"""))

        val out = runBlocking { OfflineMeetingTransport(api()).stopMeeting(StopRequest("c", 1)) }

        assertTrue("别的 ok=false **不许**被当成'已经好了'（那会把没收尾说成已转写）：$out",
            out is StopOutcome.Failed)
        out as StopOutcome.Failed
        assertTrue("这是契约/权限问题：重试也不会好", !out.retryable)
        assertTrue("音频已经传上去了，只是没收尾 —— 文案要分得开", out.alreadyUploadedHint)
        assertTrue("带上服务端原话：${out.note}", out.note.contains("clientId"))
    }

    @Test
    fun `收尾 5xx 时可重试（它自己坏了 不是网络不通）`() {
        server.enqueue(MockResponse().setResponseCode(500))

        val out = runBlocking { OfflineMeetingTransport(api()).stopMeeting(StopRequest("c", 1)) }

        out as StopOutcome.Failed
        assertTrue("5xx 要能重试（别把'它自己坏了'说成'网络不通'而放弃）", out.retryable)
        assertEquals(com.echo.contact.service.SyncFailure.SERVER_ERROR, out.failure)
    }

    @Test
    fun `收尾超时时可重试 而不是当成收下了`() {
        server.shutdown()

        val out = runBlocking { OfflineMeetingTransport(api()).stopMeeting(StopRequest("c", 1)) }

        out as StopOutcome.Failed
        assertTrue("连不上当然要重试：没收到确认就不许说已经转写", out.retryable)
        assertEquals(com.echo.contact.service.SyncFailure.UNREACHABLE, out.failure)
    }

    // ---------------------------------------------------------------- 上报暂停区间（第③件事）

    @Test
    fun `上报暂停区间打的是 meeting pauses 且 body 按契约`() {
        server.enqueue(json("""{"ok":true,"message":"收到了"}"""))

        val out = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(
                PauseRequest(clientId = "phone-20261017093000-abcd",
                    pauses = listOf(DraftPause(12.5, 31.0), DraftPause(60.0, 61.5))))
        }

        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/api/meeting/pauses", req.path!!.substringBefore('?'))
        assertTrue("必须是 JSON body（契约里就是这么写的）：${req.getHeader("Content-Type")}",
            req.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = req.body.readUtf8()
        assertTrue("要按 clientId 幂等：$body", body.contains("\"clientId\":\"phone-20261017093000-abcd\""))
        assertTrue(body, body.contains("\"from\":12.5"))
        assertTrue(body, body.contains("\"to\":31.0"))
        assertTrue("两条都要在：$body", body.contains("\"from\":60.0"))
        assertFalse("契约里没有 by，客户端就不发明：$body", body.contains("\"by\""))
        assertEquals("收下了就是成功", PauseOutcome.Ok(2), out)
    }

    @Test
    fun `空暂停区间一个请求都不发`() {
        val out = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(PauseRequest("c", emptyList()))
        }
        assertEquals(PauseOutcome.Ok(0), out)
        assertEquals("没东西可报就别白发一次请求", 0, server.requestCount)
    }

    @Test
    fun `老服务端没有这个端点时是'没报上去'而不是失败（404 405）`() {
        // 这一档的后果是"补传会不会被一个可有可无的上报拖垮"：端点不存在 ≠ 音频有问题
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"Not Found"}"""))
        val notFound = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(
                PauseRequest("c", listOf(DraftPause(1.0, 2.0))))
        }
        assertTrue("404 必须归'没这个接口'那一档：$notFound", notFound is PauseOutcome.Unsupported)

        server.enqueue(MockResponse().setResponseCode(405))
        val notAllowed = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(
                PauseRequest("c", listOf(DraftPause(1.0, 2.0))))
        }
        assertTrue("405 同理：$notAllowed", notAllowed is PauseOutcome.Unsupported)
    }

    @Test
    fun `暂停上报 5xx 与连不上都归'失败'那一档（原因要说准）`() {
        server.enqueue(MockResponse().setResponseCode(500))
        val broken = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(
                PauseRequest("c", listOf(DraftPause(1.0, 2.0))))
        }
        assertTrue("5xx 是'它自己坏了'，不是'没这个接口'：$broken", broken is PauseOutcome.Failed)
        assertEquals(SyncFailure.SERVER_ERROR, (broken as PauseOutcome.Failed).failure)

        server.shutdown()
        val offline = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(
                PauseRequest("c", listOf(DraftPause(1.0, 2.0))))
        }
        assertTrue(offline is PauseOutcome.Failed)
        assertEquals("连不上要说成连不上", SyncFailure.UNREACHABLE,
            (offline as PauseOutcome.Failed).failure)
    }

    @Test
    fun `服务端用 200+ok false 说'不支持'时也认得出来`() {
        server.enqueue(json("""{"ok":false,"message":"不支持的接口"}"""))
        val out = runBlocking {
            OfflineMeetingTransport(api()).reportPauses(
                PauseRequest("c", listOf(DraftPause(1.0, 2.0))))
        }
        assertTrue("有些实现会用 200 表达同一件事：$out", out is PauseOutcome.Unsupported)
    }

    // ---------------------------------------------------------------- 探活

    @Test
    fun `探活用 health 那条不要令牌的路径`() {
        server.enqueue(MockResponse().setResponseCode(200))
        val p = runBlocking { OfflineMeetingTransport(api()).reachable() }!!
        assertEquals("/health", server.takeRequest().path)
        assertEquals(200, p.code)
    }

    @Test
    fun `探活失败回 null 而不是抛`() {
        server.shutdown()
        val p = runBlocking { OfflineMeetingTransport(api()).reachable() }
        assertNull("连不上 = code 为 null（调用方据此进离线）", p!!.code)
    }
}
