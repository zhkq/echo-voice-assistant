package com.echo.contact.service

import com.echo.contact.data.ApiClient
import com.echo.contact.data.EchoApi
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun `409 与 503 是忙不是坏`(): Unit = runBlocking {
        // 2026-10-31：指令全走能力后端且同一人的请求串行 —— 409 client_busy 会真实出现。
        json("""{"detail":"我这边正忙着上一件事，等它完了再说一次。"}""", code = 409)
        var out = flow().submit(wav)
        assertTrue("409 必须单独判为 Busy：$out", out is ListenFlow.Outcome.Busy)
        assertTrue((out as ListenFlow.Outcome.Busy).message.contains("忙"))

        json("""{"detail":"能力后端这会儿不可用：检查配对/网络。"}""", code = 503)
        out = flow().submit(wav)
        assertTrue("503 也是稍后再试，不是失败：$out", out is ListenFlow.Outcome.Busy)
        assertTrue((out as ListenFlow.Outcome.Busy).message.contains("后端"))
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

    // --------------------------------------------- 用户 2026-10-17 真机：只念摘要、不念详情

    /**
     * 有 `speakText` 就念它 —— 这条是"单一出处"的**线上判据**：
     * 结果体里同时躺着整篇 detail，念出来的必须**恰好**是电脑给的那段。
     */
    @Test
    fun `有 speakText 就念它、整篇 reply 里的详情一句都不念`(): Unit = runBlocking {
        json(
            """{"total":1,"items":[{"id":11,"status":"done","source":"mobile",
               "speakText":"三件事都办好了。",
               "reply":"三件事都办好了。\n详情如下：\n1. 客厅灯已打开，亮度 60%\n2. 空调制冷二十六度，风速自动"}]}"""
        )
        val out = flow().awaitReply(11)
        assertTrue("该念 speakText 那段：$out", out is ListenFlow.Outcome.Reply)
        out as ListenFlow.Outcome.Reply
        assertEquals("念的必须恰好是 speakText", "三件事都办好了。", out.text)
        for (w in listOf("60%", "风速自动", "详情如下")) {
            assertFalse("详情是给人看的、不许念：${out.text}", out.text.contains(w))
        }
    }

    /** 老电脑没有 `speakText`：只念第一段，后面的详情一概不念。 */
    @Test
    fun `老电脑没有 speakText 时只念第一段摘要`(): Unit = runBlocking {
        json(
            """{"total":1,"items":[{"id":12,"status":"done","source":"mobile",
               "reply":"会议纪要整理好了。\n\n第一段讲进度：三个模块两个完成。\n第二段讲风险：接口还没对接完。"}]}"""
        )
        val out = flow().awaitReply(12) as ListenFlow.Outcome.Reply
        assertEquals("会议纪要整理好了。", out.text)
        for (w in listOf("讲进度", "讲风险", "接口")) {
            assertFalse("详情段落不许念：${out.text}", out.text.contains(w))
        }
    }

    /** 电脑发起的任务：手机一个字都不念，但要把"为什么不念"说出来（不是失败）。 */
    @Test
    fun `电脑发起的答复一个字都不念`(): Unit = runBlocking {
        json(
            """{"total":1,"items":[{"id":13,"status":"done","origin":"pc","source":"web",
               "reply":"第一段。\n\n详情在后"}]}"""
        )
        val out = flow().awaitReply(13)
        assertTrue("电脑的活儿不该返回要念的文字：$out", out is ListenFlow.Outcome.Silent)
        assertTrue("得说清为什么不念", (out as ListenFlow.Outcome.Silent).why.isNotBlank())
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
