package com.echo.contact.service

import com.echo.contact.data.ApiClient
import com.echo.contact.data.EchoApi
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 「一段语音发出去之后，结果从哪来」（`ListenFlow`）—— 用**真的 HTTP**（MockWebServer）钉住。
 *
 * 为什么这条链路必须在 JVM 里测：常驻听写没有界面，用户在车里能得到的只有"它有没有回话"。
 * 而这条路上有三个**必须区分开**的失败：后端 422（没识别出文字）/ 502（转写失败）/
 * 连不上（网络）—— 说错原因就会把人引去查错地方（与 ECHO 那边"说错原因比不说更坏"同一条纪律）。
 */
class ListenFlowTest {

    private lateinit var server: MockWebServer
    private lateinit var api: EchoApi

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        api = ApiClient.create(server.url("/").toString())
    }

    @After
    fun tearDown() {
        runCatching { server.shutdown() }
    }

    private fun json(body: String, code: Int = 200) {
        server.enqueue(
            MockResponse().setResponseCode(code)
                .setHeader("Content-Type", "application/json")
                .setBody(body)
        )
    }

    private fun flow(pollMs: Long = 0, timeoutMs: Long = 60_000) =
        ListenFlow(api, pollMs = pollMs, timeoutMs = timeoutMs, sleep = { })

    private val wav = ByteArray(320)          // 内容无关紧要，这条链路只看 HTTP 形状

    @Test
    fun `上传成功后回它听成什么`(): Unit = runBlocking {
        json("""{"ok":true,"message":"已提交","text":"开灯","commandId":7}""")
        val out = flow().submit(wav)
        assertTrue(out is ListenFlow.Outcome.Heard)
        out as ListenFlow.Outcome.Heard
        assertEquals("开灯", out.text)
        assertEquals(7, out.commandId)

        // 顺带钉住请求形状：裸 body + audio/wav（发成 multipart 后端会 422）
        val req = server.takeRequest()
        assertEquals("/api/assistant/voice-command", req.path)
        assertEquals("audio/wav", req.getHeader("Content-Type"))
    }

    @Test
    fun `没识别出文字与转写失败要分开说`(): Unit = runBlocking {
        json("""{"detail":"没有识别出文本"}""", code = 422)
        var out = flow().submit(wav)
        assertTrue(out is ListenFlow.Outcome.Failed)
        assertTrue((out as ListenFlow.Outcome.Failed).message.contains("没听清"))

        json("""{"detail":"转写失败"}""", code = 502)
        out = flow().submit(wav)
        assertTrue((out as ListenFlow.Outcome.Failed).message.contains("转写"))
    }

    @Test
    fun `连不上说的是连不上`(): Unit = runBlocking {
        server.shutdown()
        val out = flow().submit(wav)
        assertTrue(out is ListenFlow.Outcome.Failed)
        assertTrue((out as ListenFlow.Outcome.Failed).message.contains("连不上"))
    }

    @Test
    fun `拿到 brief 就念 brief 而不是整篇 reply`(): Unit = runBlocking {
        json("""{"ok":true,"message":"已提交","text":"开灯","commandId":7}""")
        assertEquals("开灯", (flow().submit(wav) as ListenFlow.Outcome.Heard).text)

        // 第一次还没跑完，第二次 done（brief 是要念的短句）
        json("""{"total":1,"items":[{"id":7,"status":"running","reply":null}]}""")
        json("""{"total":1,"items":[{"id":7,"status":"done","brief":"已经开好了。","reply":"## 详情\n一整篇"}]}""")
        assertEquals("已经开好了。", (flow().awaitReply(7) as ListenFlow.Outcome.Reply).text)
        server.takeRequest()
        server.takeRequest()
        server.takeRequest()
    }

    @Test
    fun `没有 brief 就退回 reply`(): Unit = runBlocking {
        json("""{"total":1,"items":[{"id":9,"status":"done","reply":"记好了。"}]}""")
        assertEquals("记好了。", (flow().awaitReply(9) as ListenFlow.Outcome.Reply).text)
    }

    @Test
    fun `失败那条要把原因带出来`(): Unit = runBlocking {
        json("""{"total":1,"items":[{"id":3,"status":"failed","error":"DSH 在超时内没有回复"}]}""")
        val out = flow().awaitReply(3)
        assertTrue(out is ListenFlow.Outcome.Failed)
        assertTrue((out as ListenFlow.Outcome.Failed).message.contains("DSH 在超时内没有回复"))
    }

    @Test
    fun `一直不 done 就说可能还在跑`(): Unit = runBlocking {
        repeat(50) { json("""{"total":1,"items":[{"id":5,"status":"running"}]}""") }
        val out = flow(timeoutMs = 1).awaitReply(5)
        assertTrue(out is ListenFlow.Outcome.Failed)
        assertTrue((out as ListenFlow.Outcome.Failed).message.contains("还在跑"))
    }

    @Test
    fun `没有编号就等不了`(): Unit = runBlocking {
        val out = flow().awaitReply(null)
        assertTrue((out as ListenFlow.Outcome.Failed).message.contains("编号"))
    }
}
