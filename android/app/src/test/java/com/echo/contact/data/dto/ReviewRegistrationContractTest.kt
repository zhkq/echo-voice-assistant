package com.echo.contact.data.dto

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回顾**详情**里两条"诚实性"字段的契约（2026-10-18 复查加）：
 *
 *  - `turns[].registered`：这一轮有没有真的写进工作日志（服务端比的当天日志指纹）。
 *    三态：`true` / `false` / **缺席**（老记录）—— 界面靠"缺席"区分"没核实过"与"没登记"，
 *    所以 DTO 必须是 `Boolean?`，写成 `Boolean=false` 会把所有老记录都诬成没登记。
 *  - 详情级 `error`：`ok=false` 时 HTTP 仍是 **200**，没有这个字段界面就只能画成
 *    "这一天没有回顾记录"（假空态）。
 */
class ReviewRegistrationContractTest {

    private val gson = Gson()

    @Test
    fun `每一轮的登记核实是三态`() {
        val json = """
            {"ok": true, "date": "2026-10-18", "count": 2,
             "turns": [{"id": 1, "status": "done", "registered": true},
                       {"id": 2, "status": "done", "registered": false}],
             "vault": "C:\\笔记", "vaultNote": "C:\\笔记\\01-工作日志\\2026-10-18.md",
             "vaultNoteExists": true}
        """.trimIndent()

        val d = gson.fromJson(json, ReviewHistoryDetail::class.java)

        assertTrue(d.turns[0].registered == true)
        assertFalse("没登记的一轮必须能被界面看见", d.turns[1].registered != false)
        assertTrue(d.vaultNoteExists)
        assertEquals("", d.error)
    }

    @Test
    fun `老记录没有 registered 字段时是 null 而不是 false`() {
        val json = """
            {"ok": true, "date": "2026-10-07", "count": 1,
             "turns": [{"id": 38, "status": "done", "brief": "记好了。"}]}
        """.trimIndent()

        val d = gson.fromJson(json, ReviewHistoryDetail::class.java)

        assertNull("缺字段 = 没核实过，不许当成'没登记'", d.turns.single().registered)
    }

    @Test
    fun `坏日期时 ok 与 error 都能拿到`() {
        val json = """{"ok": false, "error": "日期格式要是 YYYY-MM-DD", "date": "2026-13-99"}"""

        val d = gson.fromJson(json, ReviewHistoryDetail::class.java)

        assertFalse(d.ok)
        assertTrue("没有 error 字段界面会把坏日期画成'这天没记录'", d.error.contains("YYYY-MM-DD"))
    }
}
