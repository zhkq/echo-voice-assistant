package com.echo.contact.data.dto

import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 钉住会议与转写进度的字段契约。
 *
 * JSON 取自 2026-10-09 对活着的 ECHO 的**实测**（会议 7 场、`/api/transcribe/status` 实测 `{}`，
 * 非空形状来自源码 `meeting.py` 的 `_set_progress` 调用点）。
 *
 * 这里最值得钉的是几处**容易静默写错**的地方：
 *  - 列表用 `has_summary`（snake）+ `speakerNames`（字符串数组）；
 *    而**详情**用的是 `hasSummary`（camel）+ `speakers`（对象数组）—— 故意不一致，
 *    混用不会报错，只会永远拿到默认值；
 *  - 列表的 `segments` 是 **int（段数）**，详情的 `segments` 是 **array**；
 *  - `duration_seconds` 是 **float**；
 *  - `status` 实测会出现 **`transcribed`**（schema 注释里没写这个值）。
 */
class MeetingDtosContractTest {

    private val gson = Gson()

    @Test
    fun `会议列表一行全字段对得上`() {
        val json = """
            {"id": 14, "name": "2026-10-09_11-40-31", "title": "",
             "started_at": "2026-10-09T11:40:31", "ended_at": "2026-10-09T12:13:29",
             "duration_seconds": 1977.8, "status": "transcribed",
             "stt_model": "", "stt_device": "auto", "diarize": 1, "segments": 4,
             "audio_bytes": 0, "notes": "{}", "created_at": "2026-10-09 12:13:30",
             "error": "", "has_summary": true, "compression": null,
             "speakerNames": ["说话人1", "说话人2"], "transcribing": false,
             "timestamps": {"kinds": {"exact": 4}, "label": "精确（模型自己给的） × 4", "short": "精确 × 4"}}
        """.trimIndent()

        val m = gson.fromJson(json, MeetingItem::class.java)

        assertEquals(14, m.id)
        assertEquals("2026-10-09_11-40-31", m.name)
        assertEquals("", m.title)                       // 实测就是可能是空串
        assertEquals("2026-10-09T11:40:31", m.startedAt)
        assertEquals(1977.8, m.durationSeconds, 0.001)  // float，不是 int
        assertEquals("transcribed", m.status)           // 注释里没写、实测会出现的值
        assertEquals(4, m.segments)                     // 列表里是 int 段数
        assertTrue(m.hasSummary)                        // snake
        assertFalse(m.transcribing)
        assertEquals(listOf("说话人1", "说话人2"), m.speakerNames)   // 字符串数组
    }

    @Test
    fun `列表没有 total 字段也不会炸`() {
        // 实测 /api/meetings 只回 items（api.py:1531 没有 total）
        val parsed = gson.fromJson("""{"items": []}""", MeetingListResponse::class.java)
        assertTrue(parsed.items.isEmpty())
    }

    @Test
    fun `详情用 camel 的 hasSummary 与对象数组 speakers`() {
        val json = """
            {"id": 14, "name": "2026-10-09_11-40-31", "title": "周会",
             "folder": "C:\\echo-dev\\data\\meetings\\2026-10-09_11-40-31",
             "started_at": "2026-10-09T11:40:31", "ended_at": "2026-10-09T12:13:29",
             "duration_seconds": 1977.8, "status": "transcribed", "error": "",
             "speakers": [
               {"id": 1, "meeting_id": 14, "label": "S1", "name": "说话人1", "color": "#4f8cff"},
               {"id": 2, "meeting_id": 14, "label": "S2", "name": "张工", "color": "#3ecf8e"}],
             "lines": [
               {"id": 101, "meeting_id": 14, "seg_index": 0, "start": 1.25, "end": 3.5,
                "speaker_label": "S1", "text": "先过一下上周的进度。", "kind": "speech",
                "revised": 0, "updated_at": "2026-10-09 12:13:00"}],
             "segment_count": 4,
             "hasTranscript": true, "hasSummary": true, "hasSegments": true,
             "transcribing": false, "summaries": [], "compression": null,
             "segments": [
               {"index": 0, "duration": 600.0, "start": 0.0, "end": 600.0,
                "speakers": [{"label": "S1", "name": "说话人1", "count": 12}], "lines": []}],
             "capability": {"notes": []}}
        """.trimIndent()

        val d = gson.fromJson(json, MeetingDetail::class.java)

        assertEquals("周会", d.title)
        assertEquals(1977.8, d.durationSeconds, 0.001)
        // 详情是 camel —— 写成 has_summary 这里就永远是 false（且 Gson 一声不吭）
        assertTrue("hasSummary 是 camel", d.hasSummary)
        assertTrue("hasTranscript 是 camel", d.hasTranscript)
        assertTrue(d.hasSegments)
        assertEquals(4, d.segmentCount)          // 原段数在 segment_count

        // 说话人是**对象数组**（列表那边是字符串数组）
        assertEquals(2, d.speakers.size)
        assertEquals("张工", d.speakers[1].name)
        assertEquals("S1", d.speakers[0].label)

        // 正文行
        assertEquals(1, d.lines.size)
        assertEquals("S1", d.lines[0].speakerLabel)
        assertEquals(1.25, d.lines[0].start, 0.001)
        assertEquals("先过一下上周的进度。", d.lines[0].text)
    }

    @Test
    fun `详情里同名的 segments 是数组 也不会把解析搞崩`() {
        // 列表的 segments 是 int（段数），详情是 array —— 同一个名字两种类型。
        // 我们**故意不在 MeetingDetail 里声明 segments**，所以这里必须照样解析成功。
        val json = """
            {"id": 1, "title": "t", "segments": [{"index": 0}, {"index": 1}], "segment_count": 2}
        """.trimIndent()

        val d = gson.fromJson(json, MeetingDetail::class.java)

        assertEquals(1, d.id)
        assertEquals(2, d.segmentCount)
    }

    @Test
    fun `转写进度按源码形状解析 且 updated_at 是 epoch 浮点`() {
        val json = """
            {"14": {"phase": "转写中", "seg_index": 2, "seg_total": 4,
                    "percent": 50, "detail": "正在识别第 2 段", "updated_at": 1791547340.123}}
        """.trimIndent()

        val parsed = gson.fromJson(json, Map::class.java)
        assertNotNull(parsed)

        // 逐字段用真类型解一遍（键在 JSON 里必然是字符串）
        val one = gson.fromJson(
            """{"phase": "转写中", "seg_index": 2, "seg_total": 4, "percent": 50,
                "detail": "正在识别第 2 段", "updated_at": 1791547340.123}""",
            TranscribeProgress::class.java,
        )
        assertEquals("转写中", one.phase)
        assertEquals(2, one.segIndex)
        assertEquals(4, one.segTotal)
        assertEquals(50, one.percent)
        assertEquals(1791547340.123, one.updatedAt, 0.001)
    }

    @Test
    fun `空进度表是正常的（实测无任务时就返回空对象）`() {
        val parsed = gson.fromJson("{}", Map::class.java) as Map<*, *>
        assertTrue(parsed.isEmpty())
    }

    @Test
    fun `会话缺会话那条分支没有 error 与 anchored 也不会炸`() {
        // api.py:1124-1127：没有 session_id 时**没有 error、没有 anchored**，多一个 message
        val json = """
            {"ok": true, "session_id": "", "messages": [], "title": "", "cwd": "",
             "exists": false, "running": false,
             "message": "这条命令没有关联会话（发送失败，或当时没拿到会话）"}
        """.trimIndent()

        val s = gson.fromJson(json, CommandSessionResponse::class.java)

        assertTrue(s.ok)
        assertFalse(s.exists)
        assertTrue(s.messages.isEmpty())
        assertNull(s.error)          // 这一支没有它
        assertFalse(s.anchored)      // 默认 false（这一支没有它）
        assertEquals("这条命令没有关联会话（发送失败，或当时没拿到会话）", s.message)
    }
}
