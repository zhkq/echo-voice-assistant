package com.echo.contact.data

import com.echo.contact.data.dto.CommandItem
import com.echo.contact.data.dto.CommandRequest
import com.echo.contact.data.dto.DailyReviewSubmitRequest
import com.echo.contact.data.dto.PairClaimRequest
import com.echo.contact.ui.pickSubmitted
import com.google.gson.Gson
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/**
 * 拿**真后端**（真 `app/api.py` + 真 SQLite + 真配对码 + 真后台线程）验 App 的 HTTP 层。
 *
 * 它补的是 MockWebServer 补不了的那一半：MockWebServer 只能证明
 * 「App 发出去的形状对」，证明不了「真后端收下这个形状、并吐回我们能认的 JSON」。
 *
 * **默认跳过**（`assumeTrue`），所以常规 `gradlew test` 完全自足、不依赖外部进程。
 * 要跑它，先用 `android/tools/real_backend_harness.py` 起插座，再：
 *
 *     gradlew :app:testDebugUnitTest -Decho.realBackend=<harness.json 的绝对路径>
 *
 * 插座用的是**临时数据根**（`ECHO_DATA`），不碰真实 `data/`。
 */
class EchoApiRealBackendTest {

    private data class Harness(
        val port: Int = 0,
        val code: String = "",
        val expectedText: String = "",
        val expectedReply: String = "",
        val expectedStatus: String = "done",
        val expectedId: Int = 0,
    )

    private fun base(port: Int) = "http://127.0.0.1:$port"

    // ------------------------------------------------------------ 跳过与共用

    private fun skipIfNoHarness(): Harness {
        val h = harness
        assumeTrue(
            "未提供真后端插座（用 -Decho.realBackend=<harness.json> 打开），跳过",
            h != null,
        )
        return h!!
    }

    // ------------------------------------------------------------ 用例

    @Test
    fun `真后端 status 可通`() {
        val h = skipIfNoHarness()
        ApiClient.attachTokenProvider { null }

        val resp = runBlocking { ApiClient.create(base(h.port)).status() }

        assertTrue("status 应 2xx，实际 HTTP ${resp.code()}", resp.isSuccessful)
        // 顺带钉住仪表盘真正要用的那几个字段解析得出来（真后端实测有 9 个组件）
        val body = resp.body()
        assertNotNull("status 应能被 StatusResponse 解析", body)
        assertTrue("组件列表不该是空的", (body!!.components.size) >= 5)
        assertTrue("组件里应有 server", body.components.any { it.name == "server" })
    }

    @Test
    fun `真后端 配对换到真令牌 且历史能被 DTO 解析`() {
        val h = skipIfNoHarness()
        val token = sharedToken
        assertNotNull("真后端应换到令牌（配对码是一次性的，整轮只兑换一次）", token)

        ApiClient.attachTokenProvider { token }
        val list = runBlocking { ApiClient.create(base(h.port)).listCommands(mapOf("limit" to "50")) }

        assertTrue("历史接口应 2xx，实际 HTTP ${list.code()}", list.isSuccessful)
        val seeded = list.body()?.items?.firstOrNull { it.id == h.expectedId }
        assertNotNull("应能按 id 找到插座预置的那条指令（id=${h.expectedId}）", seeded)

        // 下面这几个正是「字段名写错就永远是空」的那几个字段：
        // 写成 created_at / state / result 时，这里会拿到 null 而 Gson 一点错都不报。
        assertNotNull("ts 不该为空（写成 created_at 时这里就是 null）", seeded!!.ts)
        assertEquals(h.expectedStatus, seeded.status)
        assertEquals(h.expectedReply, seeded.reply)
        assertEquals(h.expectedText, seeded.text)
        assertEquals("mobile", seeded.source)
        assertEquals(1234, seeded.durationMs)
    }

    /**
     * **这个用例直接钉住"点完没反馈"那个 bug 的修法**，而且刻意做成对抗性的：
     *
     * 1. 发的文本**故意跟插座里那条已 `done` 的种子指令一模一样**
     *    —— 若认领规则少了 `id > maxIdBefore` 这一条，轮询会立刻"找到"那条旧记录，
     *    报出一个假的成功结果（这正是最误导的失败方式）；
     * 2. 插座里**没有智能体**，所以 `_dispatch` 必然把新那一行写成 `failed`
     *    （`error="DSH 未运行"`，app/assistant.py:883）—— 于是"错误也要能显示出来"也被验了；
     * 3. 断言必须**轮询**才拿得到结论：`POST` 只回 `{ok:true, message:"已提交"}`
     *    （app/assistant.py:793），活儿在后台线程里干（app/assistant.py:792）。
     */
    @Test
    fun `发指令要轮询才拿得到结论 且不认领上次那条同名旧记录`() {
        val h = skipIfNoHarness()
        val token = sharedToken
        assertNotNull("真后端应换到令牌", token)
        ApiClient.attachTokenProvider { token }
        val api = ApiClient.create(base(h.port))

        // 发送前先看清现状：种子里就有一条**文本完全相同**的 done 记录
        val before = runBlocking { api.listCommands(mapOf("limit" to "50")).body()?.items ?: emptyList() }
        val maxIdBefore = before.maxOfOrNull { it.id ?: 0 } ?: 0
        val oldSameText = before.firstOrNull { it.text == h.expectedText }
        assertNotNull("插座应预置一条同名旧记录（这个用例靠它验认领规则）", oldSameText)
        assertEquals("旧记录应是终态 done", "done", oldSameText!!.status)

        // 1) 发出去：只拿到"已提交"，没有任何结果
        val sent = runBlocking { api.sendCommand(CommandRequest(text = h.expectedText, source = "mobile")) }
        assertTrue("发送应 2xx，实际 HTTP ${sent.code()}", sent.isSuccessful)
        assertTrue("POST 应回 ok=true", sent.body()?.ok == true)
        assertEquals("后端应明确回'已提交'而不是结果", "已提交", sent.body()?.message)

        // 2) 轮询：只有比 maxIdBefore 更新的那条才算我的
        var hit: CommandItem? = null
        var rounds = 0
        runBlocking {
            val deadline = System.currentTimeMillis() + 30_000
            while (hit == null && System.currentTimeMillis() < deadline) {
                rounds++
                val items = api.listCommands(mapOf("limit" to "50")).body()?.items ?: emptyList()
                val found = pickSubmitted(items, h.expectedText, maxIdBefore)
                if (found?.status == "done" || found?.status == "failed") hit = found
                delay(300)
            }
        }

        assertNotNull("轮询应能认领到新那一行（认领规则要是漏了 id 判据就会认领到旧记录）", hit)
        val result = hit!!
        assertTrue("认领到的必须是新行，不是那条旧记录", (result.id ?: 0) > maxIdBefore)
        assertEquals("插座里没有智能体，所以这一轮应落到 failed", "failed", result.status)
        assertNotNull("失败原因要能拿到（界面要显示它）", result.error)
        assertTrue(
            "失败原因应说明 DSH 没在跑，实际=${result.error}",
            result.error!!.contains("DSH"),
        )
        println("[real-backend] 轮询 $rounds 轮拿到结论：status=${result.status} error=${result.error}")
    }

    /**
     * 回顾那三个端点：**路径写错就是 404**，所以必须拿真后端打一遍。
     * 插座的数据根是空的，所以这里不校验"有几轮"，只校验"路径与字段解析都对"。
     */
    @Test
    fun `真后端 回顾端点路径与解析都对`() {
        val h = skipIfNoHarness()
        ApiClient.attachTokenProvider { sharedToken }
        val api = ApiClient.create(base(h.port))

        val state = runBlocking { api.dailyReview() }
        assertTrue("GET /api/daily-review 应 2xx，实际 HTTP ${state.code()}", state.isSuccessful)
        assertNotNull("DailyReviewState 应解析得出来", state.body())

        val summary = runBlocking { api.dailyReviewSummary() }
        assertTrue("GET /api/daily-review/summary 应 2xx，实际 HTTP ${summary.code()}", summary.isSuccessful)
        val s = summary.body()
        assertNotNull("DailyReviewSummary 应解析得出来", s)
        // 空数据根：今天 0 轮、没有历史 —— 这本身就是断言
        assertEquals(0, s!!.todayTurns)
        // 实测（2026-10-09 逮到）：没有历史时后端给的是**空串**而不是 null，
        // 所以判据必须是 isNullOrBlank —— 只判 null 会漏。界面那边用的也是 isNullOrBlank。
        assertTrue("空数据根里应没有播报（空串或 null 都算没有）", s.lastBrief.isNullOrBlank())

        // 具体某天（坏日期后端是 200 + ok=false，不抛）
        val day = runBlocking { api.dailyReviewDay("2026-10-07") }
        assertTrue("GET /api/daily-review/history/{date} 应 2xx，实际 HTTP ${day.code()}", day.isSuccessful)
        assertNotNull(day.body())
    }

    /**
     * 新增的那几条读接口：**路径写错就是 404**，所以拿真后端打一遍。
     * 插座的数据根是空的，所以只校验"路径通 + 能解析"，不校验内容。
     */
    @Test
    fun `真后端 探活与会议与转写进度与回顾历史都能通`() {
        val h = skipIfNoHarness()
        ApiClient.attachTokenProvider { sharedToken }
        val api = ApiClient.create(base(h.port))

        // 探活：唯一无令牌、无 /api 前缀的一条
        val health = runBlocking { api.health() }
        assertTrue("GET /health 应 2xx，实际 HTTP ${health.code()}", health.isSuccessful)

        val meetings = runBlocking { api.meetings(limit = 20) }
        assertTrue("GET /api/meetings 应 2xx，实际 HTTP ${meetings.code()}", meetings.isSuccessful)
        assertNotNull("MeetingListResponse 应解析得出来", meetings.body())

        val progress = runBlocking { api.transcribeStatus() }
        assertTrue("GET /api/transcribe/status 应 2xx，实际 HTTP ${progress.code()}", progress.isSuccessful)
        assertNotNull(progress.body())

        val days = runBlocking { api.dailyReviewHistory(60) }
        assertTrue("GET /api/daily-review/history 应 2xx，实际 HTTP ${days.code()}", days.isSuccessful)
        assertNotNull("ReviewHistoryList 应解析得出来", days.body())
        // 空数据根：没有历史
        assertTrue((days.body()?.items ?: emptyList()).isEmpty())
    }

    /**
     * **"HTTP 200 但 ok=false"这条约定**必须钉住。
     *
     * `POST /api/daily-review/submit` 在"回顾没配好 / 没等到回复"时返回的是
     * **200 + `ok=false` + `error` + 一句 `spoken`**（api.py:879-883 明说这是刻意的：
     * "没等到回复不是客户端错误"）。只看状态码的客户端会把它当成成功。
     *
     * 插座的数据根是空的 → `ready()` 必然不过 → 正好稳定复现这一支。
     */
    @Test
    fun `真后端 回顾提交是 200 加 ok_false 而不是 4xx`() {
        val h = skipIfNoHarness()
        ApiClient.attachTokenProvider { sharedToken }
        val api = ApiClient.create(base(h.port))

        val resp = runBlocking { api.dailyReviewSubmit(DailyReviewSubmitRequest(text = "今天修好了后端")) }

        assertTrue("HTTP 该是 2xx（业务失败不用 4xx），实际 ${resp.code()}", resp.isSuccessful)
        val b = resp.body()
        assertNotNull("DailyReviewSubmitResponse 应解析得出来", b)
        val body = b!!
        assertFalse("空数据根里回顾没配好，ok 应为 false", body.ok)
        assertTrue("失败也要给一句人话原因", body.error.isNotBlank())
        // 三层兜底保证"车里总有一句可念的话"——这正是手机要念的东西
        assertTrue("失败时也该有 spoken 兜底", body.spoken.isNotBlank())
    }

    private companion object {
        /** 插座信息（整轮只读一次）。 */
        val harness: Harness? by lazy {
            val path = System.getProperty("echo.realBackend").orEmpty()
            if (path.isBlank() || !File(path).isFile) {
                null
            } else {
                Gson().fromJson(File(path).readText(Charsets.UTF_8), Harness::class.java)
            }
        }

        /**
         * 配对码是**一次性**的，所以整轮测试只兑换一次，各用例共用这个令牌。
         * （companion 的 `by lazy` 在整个测试 JVM 里只初始化一次，而 JUnit 每个用例都新建实例。）
         */
        val sharedToken: String? by lazy {
            val h = harness ?: return@lazy null
            ApiClient.clearCache()
            ApiClient.attachTokenProvider { null }
            val resp = runBlocking {
                ApiClient.create("http://127.0.0.1:${h.port}")
                    .pairClaim(PairClaimRequest(code = h.code, name = "mobile"))
            }
            if (!resp.isSuccessful) {
                fail("配对失败 HTTP ${resp.code()}：${resp.errorBody()?.string()}")
            }
            resp.body()?.token
        }
    }
}
