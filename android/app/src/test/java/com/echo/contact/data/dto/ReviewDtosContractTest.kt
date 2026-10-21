package com.echo.contact.data.dto

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 钉住每日回顾的字段契约。
 *
 * 下面的 JSON 全是 **2026-10-09 从活着的 ECHO 上真抓下来的**（`/api/daily-review`、
 * `/api/daily-review/summary`、`/api/daily-review/history/2026-10-07`），只把长文本截短了。
 * 为什么值得钉：Gson 对不认识的字段**静默丢弃**，名字写错不会报错 ——
 * 表现只是回顾卡上"最近一次播报"永远空白，而没有任何异常。
 */
class ReviewDtosContractTest {

    private val gson = Gson()

    @Test
    fun `回顾状态字段对得上`() {
        val json = """
            {"enabled": true, "ready": true, "reason": "", "agent": "harness",
             "workspace": "C:\\echo-dev\\review",
             "vault": "C:\\Users\\zhkq\\OneDrive\\笔记\\笔记-2026\\笔记-2026",
             "kind": "review:2026-10-09", "sessionId": "", "running": false}
        """.trimIndent()

        val s = gson.fromJson(json, DailyReviewState::class.java)

        assertTrue(s.enabled)
        assertTrue(s.ready)
        assertEquals("harness", s.agent)
        assertEquals("review:2026-10-09", s.kind)
        assertFalse(s.running)
        assertTrue(s.vault.contains("工作") || s.vault.isNotBlank())
    }

    @Test
    fun `summary 的播报字段是 lastBrief 不是别的名字`() {
        val json = """
            {"today": "2026-10-09", "todayTurns": 0, "todayFailed": 0,
             "lastAt": "2026-10-07 10:29:03",
             "lastBrief": "今天记了两件事：每日回顾界面完善了，后端启动那个毛病也修好了。",
             "lastStatus": "done",
             "lastText": "小测试试一下今天天的美丽每日回回国回顾我今天今天主要的工作"}
        """.trimIndent()

        val s = gson.fromJson(json, DailyReviewSummary::class.java)

        assertEquals("2026-10-09", s.today)
        assertEquals(0, s.todayTurns)
        assertEquals(0, s.todayFailed)
        assertEquals("2026-10-07 10:29:03", s.lastAt)
        // 要念的就是它（不是 lastText —— 那是口述转写原文，带噪音）
        assertNotNull("lastBrief 不该为空（写成 brief 时这里就是 null）", s.lastBrief)
        assertTrue(s.lastBrief!!.startsWith("今天记了两件事"))
        assertEquals("done", s.lastStatus)
        assertNotNull(s.lastText)
    }

    @Test
    fun `回顾一轮的 turn 响应字段对得上`() {
        val json = """
            {"ok": true, "text": "今天把回顾改成走回顾接口了",
             "session_id": "session-1", "workspace": "C:\\echo-dev\\review",
             "reply": "已登记", "spoken": "记好了，还有吗？", "source": "broadcast",
             "error": "", "seconds": 42.5, "registered": true}
        """.trimIndent()

        val t = gson.fromJson(json, DailyReviewTurnResponse::class.java)

        assertTrue(t.ok)
        assertEquals("今天把回顾改成走回顾接口了", t.text)
        // `session_id` 是蛇形（不是 sessionId）—— 写错就永远是空，排障时看不出
        assertEquals("session-1", t.sessionId)
        assertEquals("记好了，还有吗？", t.spoken)
        assertEquals("broadcast", t.source)
        assertEquals(42.5, t.seconds, 0.001)
        assertTrue("登记核实要原样读出来", t.registered == true)

        val legacy = gson.fromJson(
            """{"ok":true,"text":"x","spoken":"y"}""",
            DailyReviewTurnResponse::class.java,
        )
        assertNull("老服务端没有 registered → null（不许说成 false）", legacy.registered)
    }

    @Test
    fun `某天详情里每轮的 brief 与 seconds 对得上`() {
        val json = """
            {"ok": true, "date": "2026-10-07",
             "turns": [{"id": 38, "at": "2026-10-07 10:29:03",
                        "text": "小测试试一下今天天的美丽每日回回国回顾",
                        "reply": "已按今天的口述登记完毕……",
                        "brief": "今天记了两件事：每日回顾界面完善了。",
                        "status": "done", "error": "", "seconds": 122.4,
                        "sessionId": "session-44ba84f9-4e13-419d-88f3-80cdcbadd559"}],
             "count": 1, "vault": "C:\\笔记",
             "vaultNote": "C:\\笔记\\01-工作日志\\2026-10-07.md", "vaultNoteExists": true}
        """.trimIndent()

        val d = gson.fromJson(json, ReviewHistoryDetail::class.java)

        assertTrue(d.ok)
        assertEquals("2026-10-07", d.date)
        assertEquals(1, d.count)
        assertEquals(1, d.turns.size)
        val t = d.turns.single()
        assertEquals(38, t.id)
        assertEquals("done", t.status)
        assertEquals("今天记了两件事：每日回顾界面完善了。", t.brief)
        assertEquals(122.4, t.seconds, 0.001)
        // sessionId 是驼峰（不是 session_id），写错就永远是空
        assertEquals("session-44ba84f9-4e13-419d-88f3-80cdcbadd559", t.sessionId)
        assertTrue(d.vaultNoteExists)
        assertEquals("C:\\笔记\\01-工作日志\\2026-10-07.md", d.vaultNote)
    }
}
