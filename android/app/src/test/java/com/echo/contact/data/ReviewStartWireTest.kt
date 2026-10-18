package com.echo.contact.data

import com.google.gson.Gson
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * `POST /api/daily-review/start` 与 `/stop` 的**线上形状**（用一个真的本地 HTTP 服务钉）。
 *
 * 为什么必须单测这一层：这两个错法都**不会编译报错**，而表现是"点了开始没反应"或
 * "以为结束了其实没结束"：
 *  * 路径写错 / 多带一个 body → 老服务端可能 404 或 422；
 *  * `/stop` 回的是 `{ok, stopped}`，不看 `stopped` 就会把"本来就没在跑"说成"已停止"。
 *
 * 服务端契约（逐行核过，别再猜）：
 *  * `POST /api/daily-review/start` → `daily_review.start()`（幂等），回
 *    `{ok, sessionId, workspaceId, workspace, access, accessOk, error}`，**不碰麦克风**；
 *  * `POST /api/daily-review/stop` → `{ok, stopped}`。
 *
 * ⛔ **另一条路不存在于接口面里**：`/api/daily-review/go` 会 `assistant.start_review()`
 * = 真的开**电脑**那支麦。手机这条线不调它（判据在 `ReviewConversationTest` 里）。
 */
class ReviewStartWireTest {

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

    @Test
    fun `开始打 start 无请求体 且字段对得上`() {
        server.enqueue(
            json(
                """{"ok":true,"sessionId":"session-1","workspaceId":"ws-9",
                    "workspace":"C:\\echo-dev\\review","access":"已允许全盘访问",
                    "accessOk":true,"error":""}"""
            )
        )

        val resp = runBlocking { api().dailyReviewStart() }

        assertTrue(resp.isSuccessful)
        val b = resp.body()!!
        assertTrue(b.ok)
        assertEquals("session-1", b.sessionId)
        assertEquals("ws-9", b.workspaceId)
        assertEquals("C:\\echo-dev\\review", b.workspace)
        assertEquals("已允许全盘访问", b.access)
        assertTrue("accessOk=true 要读成 true", b.accessOk == true)
        assertEquals("", b.error)

        val req = server.takeRequest()
        assertEquals("/api/daily-review/start", req.path)
        assertEquals("POST", req.method)
        // 后端签名里 body 是可选的，手机这条路**一个字节都不发**（多带字段会被老服务端 422）
        assertEquals("", req.body.readUtf8())
    }

    @Test
    fun `start 的 accessOk 是三态 拿不到就不猜`() {
        val missing = gson.fromJson(
            """{"ok":true,"sessionId":"s","workspaceId":"","workspace":"","access":"","error":""}""",
            com.echo.contact.data.dto.DailyReviewStartResponse::class.java,
        )
        assertNull("老服务端没这个字段 → null（不许说成 false = 假失败）", missing.accessOk)

        val notOk = gson.fromJson(
            """{"ok":false,"error":"回顾未启用","accessOk":false}""",
            com.echo.contact.data.dto.DailyReviewStartResponse::class.java,
        )
        assertTrue("明确 false 要读成 false", notOk.accessOk == false)
        assertEquals("回顾未启用", notOk.error)
        assertTrue("ok=false 时 sessionId 是空的", notOk.sessionId.isEmpty())
    }

    @Test
    fun `结束打 stop 且 stopped 字段读得出来`() {
        server.enqueue(json("""{"ok":true,"stopped":true}"""))
        server.enqueue(json("""{"ok":true,"stopped":false}"""))

        val stopped = runBlocking { api().dailyReviewStop() }
        assertTrue(stopped.isSuccessful)
        assertTrue("stopped=true 要原样读出来", stopped.body()?.get("stopped") == true)

        val notRunning = runBlocking { api().dailyReviewStop() }
        assertTrue(
            "stopped=false 是服务端如实说的'没有可停的'，不能当成 true",
            notRunning.body()?.get("stopped") == false,
        )

        val a = server.takeRequest()
        assertEquals("/api/daily-review/stop", a.path)
        assertEquals("POST", a.method)
        assertEquals("", a.body.readUtf8())
        val b = server.takeRequest()
        assertEquals("/api/daily-review/stop", b.path)
    }
}
