package com.echo.contact.data.dto

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 钉住「ECHO 后端真实 JSON ↔ 本 App DTO」的字段契约。
 *
 * 为什么值得单测：Gson 反序列化**不认识的字段是静默丢弃**，字段名写错不会报错、
 * 只会在界面上显示成空白。所以这里用后端真会吐出来的 JSON 逐字段断言。
 *
 * 期望值来源（别改之前先回去看代码）：
 *  - 历史行 = `commands` 表的列（app/db.py:112）+ `_command_row` 摊平的 backend/intent（app/api.py:1071）
 *  - 配对回包 app/api.py:2541；错误体是 FastAPI 的 `{"detail": ...}`
 *  - 会话回包 app/api.py:1169，消息只有 role/seq/text（app/api.py:1168）
 */
class DtosContractTest {

    private val gson = Gson()

    @Test
    fun `commands 行的列名与 DTO 对得上`() {
        // 行取自 db.py:112 的真实列（含 _command_row 加的 backend/intent）
        val json = """
            {"total": 1, "items": [{
              "id": 42,
              "ts": "2026-10-09 18:30:01",
              "source": "mobile",
              "text": "把客厅灯打开",
              "status": "done",
              "session_id": "sess-abc",
              "reply": "已经打开了客厅的灯。",
              "brief": "已开灯",
              "duration_ms": 1234,
              "error": "",
              "meta": "{\"backend\": \"dsh\"}",
              "backend": "dsh",
              "intent": "command"
            }]}
        """.trimIndent()

        val parsed = gson.fromJson(json, CommandListResponse::class.java)

        assertEquals(1, parsed.total)
        val item = parsed.items.single()
        assertEquals(42, item.id)
        // 下面这几个就是曾经写错过的那批（created_at/state/result → ts/status/reply）
        assertEquals("2026-10-09 18:30:01", item.ts)
        assertEquals("mobile", item.source)
        assertEquals("把客厅灯打开", item.text)
        assertEquals("done", item.status)
        assertEquals("sess-abc", item.sessionId)
        assertEquals("已经打开了客厅的灯。", item.reply)
        assertEquals("已开灯", item.brief)
        assertEquals(1234, item.durationMs)
        assertEquals("dsh", item.backend)
        assertEquals("command", item.intent)
    }

    @Test
    fun `配对回包的字段与 DTO 对得上`() {
        val json = """
            {"ok": true, "token": "raw-token-once", "keyId": 7, "name": "mobile",
             "baseUrl": "http://192.168.1.5:8900",
             "note": "令牌只出现这一次，请设备侧立刻存进安全存储"}
        """.trimIndent()

        val parsed = gson.fromJson(json, PairClaimResponse::class.java)

        assertTrue(parsed.ok)
        assertEquals("raw-token-once", parsed.token)
        assertEquals("7", parsed.keyId)
        assertEquals("mobile", parsed.name)
        assertEquals("http://192.168.1.5:8900", parsed.baseUrl)
        assertNotNull(parsed.note)
    }

    @Test
    fun `配对失败时后端给的是 FastAPI 的 detail`() {
        val parsed = gson.fromJson("""{"detail": "配对码无效或已过期"}""", ApiErrorResponse::class.java)
        assertEquals("配对码无效或已过期", parsed.detail)
    }

    @Test
    fun `指令回包只有 ok 与 message`() {
        val parsed = gson.fromJson("""{"ok": true, "message": "已下发"}""", CommandResponse::class.java)
        assertTrue(parsed.ok)
        assertEquals("已下发", parsed.message)
    }

    @Test
    fun `会话回包的消息只有 role seq text`() {
        val json = """
            {"ok": true, "session_id": "sess-abc", "title": "标题", "cwd": "C:\\echo-dev",
             "exists": true, "running": false, "anchored": true, "error": "",
             "messages": [
               {"role": "user", "seq": 1, "text": "把客厅灯打开"},
               {"role": "assistant", "seq": 2, "text": "已经打开了。"}
             ]}
        """.trimIndent()

        val parsed = gson.fromJson(json, CommandSessionResponse::class.java)

        assertTrue(parsed.ok)
        assertEquals("sess-abc", parsed.sessionId)
        assertEquals("标题", parsed.title)
        assertTrue(parsed.exists)
        assertFalse(parsed.running)
        assertTrue(parsed.anchored)
        assertEquals(2, parsed.messages.size)
        assertEquals("user", parsed.messages[0].role)
        assertEquals(1, parsed.messages[0].seq)
        assertEquals("已经打开了。", parsed.messages[1].text)
    }

    @Test
    fun `没有关联会话时后端给的是人话说明而不是 error`() {
        val json = """
            {"ok": true, "session_id": "", "messages": [], "title": "", "cwd": "",
             "exists": false, "running": false,
             "message": "这条命令没有关联会话（发送失败，或当时没拿到会话）"}
        """.trimIndent()

        val parsed = gson.fromJson(json, CommandSessionResponse::class.java)

        assertFalse(parsed.exists)
        assertTrue(parsed.messages.isEmpty())
        assertEquals("这条命令没有关联会话（发送失败，或当时没拿到会话）", parsed.message)
    }
}
