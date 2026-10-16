package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.charset.StandardCharsets
import java.time.LocalDateTime

/**
 * 离线录音的**清单**（manifest）：序列化往返 + 落盘 + 损坏时的降级。
 *
 * 它值一整组用例，是因为这份清单是**断网时唯一的事实来源**：
 *  * 读歪了 → 补传的顺序/时间轴全错（而"顺序就是内容"）；
 *  * 读崩了 → 整场录音失去定义（音频还在，但没人知道它属于哪一场、第几段）；
 *  * 读不动时**覆盖**掉 → 把本来还能救的音频连同定义一起毁掉（最糟）。
 */
class OfflineMeetingManifestTest {

    @get:Rule val tmp = TemporaryFolder()

    private fun seg(seq: Int, uploaded: Boolean = false) = DraftSegment(
        seq = seq,
        file = MeetingDraftStore.segmentName(seq),
        startedAt = "2026-10-16T20:3$seq:00",
        startOffsetSeconds = (seq - 1) * 30.0,
        durationSeconds = 30.0,
        uploaded = uploaded,
    )

    private fun draft(
        id: String = "phone-20261016203102-abcd",
        ended: String = "2026-10-16T20:33:02",
        segs: List<DraftSegment> = listOf(seg(1), seg(2)),
        paused: List<DraftPause> = listOf(DraftPause(30.0, 60.0)),
        acked: Int = 0,
        uploaded: Boolean = false,
    ) = MeetingDraft(
        clientId = id,
        startedAt = "2026-10-16T20:31:02",
        endedAt = ended,
        durationSeconds = 120.0,
        serverMeetingId = if (uploaded) 77 else 0,
        pauses = paused,
        segments = segs,
        acknowledgedSegments = acked,
        uploaded = uploaded,
    )

    // ---------------------------------------------------------------- 往返

    @Test
    fun `清单往返一字不差`() {
        val d = draft()
        val back = OfflineMeetingJson.decode(OfflineMeetingJson.encode(d))
        assertNotNull("编码出来的东西必须能读回来", back)
        assertEquals(d, back)
    }

    @Test
    fun `空清单也能往返`() {
        // 空清单是真实存在的：开始录音之后、第一段落盘之前的那一刻
        val d = draft(segs = emptyList(), paused = emptyList())
        assertEquals(d, OfflineMeetingJson.decode(OfflineMeetingJson.encode(d)))
    }

    @Test
    fun `中文与引号不会把 JSON 写坏`() {
        // clientId 是生成的、不会带中文，但**暂停来源/备注**这类字段以后可能带
        val d = draft(id = "phone-\"引号\"-反斜杠\\-换行\n", paused = listOf(
            DraftPause(1.0, 2.0, by = "本地静音（用户按的）")))
        val back = OfflineMeetingJson.decode(OfflineMeetingJson.encode(d))!!
        assertEquals("phone-\"引号\"-反斜杠\\-换行\n", back.clientId)
        assertEquals("本地静音（用户按的）", back.pauses[0].by)
    }

    @Test
    fun `小数秒不会被写成整数`() {
        val d = draft().copy(durationSeconds = 12.5,
            segments = listOf(seg(1).copy(startOffsetSeconds = 0.25, durationSeconds = 29.75)))
        val back = OfflineMeetingJson.decode(OfflineMeetingJson.encode(d))!!
        assertEquals(12.5, back.durationSeconds, 1e-9)
        assertEquals(0.25, back.segments[0].startOffsetSeconds, 1e-9)
        assertEquals(29.75, back.segments[0].durationSeconds, 1e-9)
    }

    // ---------------------------------------------------------------- 降级（不许崩）

    @Test
    fun `损坏的清单读成 null 而不是抛异常`() {
        // 补传是后台动作：读一份坏清单**不许**把整条补传链抛死（那会让所有清单都传不动）
        assertNull(OfflineMeetingJson.decode(""))
        assertNull(OfflineMeetingJson.decode("   "))
        assertNull(OfflineMeetingJson.decode("{"))
        assertNull(OfflineMeetingJson.decode("""{"format":1,"started_at":"2026-10-16T20:31:02"}"""))
        assertNull(OfflineMeetingJson.decode("这不是 JSON"))
        assertNull(OfflineMeetingJson.decode(null))
    }

    @Test
    fun `被截断的清单少字段也读得出来（按默认值收）`() {
        // 旧版本 App 写的清单没有后来的字段；"缺字段"不该等同"整份作废"
        val partial = """
            { "format": 1, "client_id": "phone-1", "started_at": "2026-10-16T20:31:02",
              "segments": [ { "seq": 1, "file": "seg-0001.wav" } ] }
        """.trimIndent()
        val d = OfflineMeetingJson.decode(partial)!!
        assertEquals("phone-1", d.clientId)
        assertEquals(1, d.segments.size)
        assertEquals(0.0, d.segments[0].durationSeconds, 1e-9)
        assertTrue("缺 ended_at = 还在录（这是有意义的默认值）", d.recording)
        assertFalse(d.uploaded)
    }

    @Test
    fun `缺文件名的段被丢掉（而不是留着一条空文件名）`() {
        val text = """
            { "client_id": "phone-1", "started_at": "T",
              "segments": [ {"seq":1,"file":"seg-0001.wav"}, {"seq":2}, {"file":"x.wav"} ] }
        """.trimIndent()
        val d = OfflineMeetingJson.decode(text)!!
        assertEquals("没有 seq/file 的条目不是段，留着只会让补传去读一个不存在的文件",
            1, d.segments.size)
        assertEquals(1, d.segments[0].seq)
    }

    // ---------------------------------------------------------------- 落盘

    @Test
    fun `存了再读回来是同一条`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val d = draft()
        assertTrue("写清单必须成功（写不进去就是真丢音频，调用方要如实报）", store.save(d))
        assertEquals(d, store.load(d.clientId))
        assertTrue(File(store.dirOf(d.clientId), MeetingDraftStore.MANIFEST_NAME).isFile)
    }

    @Test
    fun `没有清单时给 null 而不是伪造一份空的`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        assertNull("伪造空清单会让补传以为'这一场没有段'，从而把它标成已上传", store.load("nope"))
    }

    @Test
    fun `读不动的清单不影响别的清单`() {
        val root = tmp.newFolder("offline")
        val store = MeetingDraftStore(root)
        assertTrue(store.save(draft(id = "good-1")))
        // 手写一份坏清单（模拟写到一半掉电）
        val bad = File(root, "bad-1").apply { mkdirs() }
        File(bad, MeetingDraftStore.MANIFEST_NAME)
            .writeBytes("""{"client_id":"bad-1","segments":[{""".toByteArray(StandardCharsets.UTF_8))
        val list = store.list()
        assertEquals("坏的那份被跳过，好的那份照样在", listOf("good-1"), list.map { it.clientId })
    }

    @Test
    fun `清单按开始时间升序（补传顺序不能靠目录名）`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        store.save(draft(id = "b").copy(startedAt = "2026-10-16T10:00:00"))
        store.save(draft(id = "a").copy(startedAt = "2026-10-16T09:00:00"))
        store.save(draft(id = "c").copy(startedAt = "2026-10-16T11:00:00"))
        assertEquals(listOf("a", "b", "c"), store.list().map { it.clientId })
    }

    @Test
    fun `落盘是原子的（有临时文件也不会被当成清单）`() {
        val root = tmp.newFolder("offline")
        val store = MeetingDraftStore(root)
        store.save(draft(id = "x"))
        val dir = store.dirOf("x")
        // 留一个"写到一半"的临时文件：它**不是**清单，不该被读出来
        File(dir, "${MeetingDraftStore.MANIFEST_NAME}.tmp").writeText("{ 半截")
        assertEquals(1, store.list().size)
        assertNotNull(store.load("x"))
    }

    @Test
    fun `clip：只有真的传完并核对一致才标 uploaded`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val d = draft(segs = listOf(seg(1), seg(2)))
        val ok = store.markUploaded(d, serverMeetingId = 77)
        assertTrue(ok.uploaded)
        assertEquals(77, ok.serverMeetingId)
        assertEquals("标记已上传时水位也要推到顶（否则续传会以为还有欠的）",
            2, ok.acknowledgedSegments)
        assertTrue(ok.segments.all { it.uploaded })
        assertTrue("确认之后**仍然不删音频**：宁可多留一会儿也别丢", store.retainUploadedAudio())
    }

    // ---------------------------------------------------------------- 客户端 id

    @Test
    fun `客户端 id 稳定且带时间戳`() {
        val at = LocalDateTime.of(2026, 10, 16, 20, 31, 2)
        val id = MeetingDraftStore.newClientId(at, rnd = 0x1a2b)
        assertEquals("phone-20261016203102-1a2b", id)
        assertEquals("同一个输入必须给同一个 id（幂等键不许自己漂）", id,
            MeetingDraftStore.newClientId(at, rnd = 0x1a2b))
    }

    @Test
    fun `目录名里的非法字符会被换成下划线`() {
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        val d = draft(id = "phone/../坏:id")
        assertTrue(store.save(d))
        // 关键判据：**不许**跑出 root 之外（路径穿越）
        assertTrue(store.dirOf(d.clientId).canonicalPath
            .startsWith(tmp.root.canonicalPath))
        assertNotNull(store.load(d.clientId))
    }

    @Test
    fun `ISO 时间往返 且读不懂时回 null`() {
        val iso = MeetingDraftStore.isoOf(1_763_000_000_000L)
        assertTrue("形状要对（面板与电脑侧都是这个形状）：$iso",
            Regex("""\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}""").matches(iso))
        assertNotNull(MeetingDraftStore.epochOf(iso))
        assertNull("读不懂**不编一个 0**（那会显示 1970 年）", MeetingDraftStore.epochOf("下午三点"))
        assertNull(MeetingDraftStore.epochOf(""))
        assertNull(MeetingDraftStore.epochOf(null))
    }

    @Test
    fun `自动收尾：上一条没结束的清单能读出来（重启能接着传）`() {
        // "进程被杀之后 App 重开"这件事在这层的判据：清单**还在磁盘上**，而且
        // `recording=true` 能被认出来（服务据此收尾并接着传）。
        val store = MeetingDraftStore(tmp.newFolder("offline"))
        store.save(draft(id = "killed-1", ended = "", segs = listOf(seg(1))))
        val reopened = MeetingDraftStore(store.dirOf("killed-1").parentFile!!).load("killed-1")!!
        assertTrue("重启之后要能看出这一场还没结束", reopened.recording)
        assertEquals(1, reopened.segments.size)
        assertEquals(1, reopened.pendingSegments)
    }
}
