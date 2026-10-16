package com.echo.contact.service

import com.echo.contact.audio.SegmentFormat
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.nio.charset.StandardCharsets
import java.time.LocalDateTime

/**
 * **本地音频的保留策略**（修②：有界保留，且**绝不早删**）。
 *
 * 这一组用例护着这条路上**唯一不可逆**的动作：删音频文件。
 * 判错的后果是静默的（用户几天后才发现某场会没有音频），所以每条口径都单独钉：
 *  * 未 DONE（没上传/没对账通过）的**一个字节都不许删**；
 *  * 只有超过保留天数才删；
 *  * **正好到期**那一下算到期（边界要写死，不能靠"感觉上差不多了"）；
 *  * 空清单不许炸（那是最容易被漏掉的一档：什么都没发生就该什么都不做）。
 */
class OfflineMeetingRetentionTest {

    @get:Rule val tmp = TemporaryFolder()

    /** 固定的"现在"：`2026-10-16T12:00:00`（本地时区）—— 判据不许跟着系统时钟漂。 */
    private val now = epochOf("2026-10-16T12:00:00")

    private val day = 24L * 60 * 60 * 1000

    private fun epochOf(iso: String): Long =
        MeetingDraftStore.epochOf(iso) ?: error("用例的 ISO 必须能解析：$iso")

    private fun seg(seq: Int) = DraftSegment(
        seq = seq,
        file = MeetingDraftStore.segmentName(seq),
        startedAt = "2026-10-16T10:00:00",
        startOffsetSeconds = (seq - 1) * 30.0,
        durationSeconds = 30.0,
    )

    /**
     * 一份清单。`endedIso` 决定它有多旧；[uploaded] / [stopped] 分开给 ——
     * "对账通过"与"服务端收尾"是两件事（见 `OfflineMeetingRetention.isEligible`）。
     */
    private fun draft(
        id: String = "phone-1",
        endedIso: String = "2026-10-16T11:00:00",
        uploaded: Boolean = false,
        segs: List<DraftSegment> = listOf(seg(1), seg(2)),
    ) = MeetingDraft(
        clientId = id,
        startedAt = "2026-10-16T10:00:00",
        endedAt = endedIso,
        durationSeconds = 3600.0,
        serverMeetingId = if (uploaded) 42 else 0,
        segments = segs,
        acknowledgedSegments = if (uploaded) segs.maxOfOrNull { it.seq } ?: 0 else 0,
        uploaded = uploaded,
    )

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    /** 在 store 里真写出段文件（`purgeAudio` 要真删东西才测得出）。 */
    private fun materialize(store: MeetingDraftStore, d: MeetingDraft): Long {
        val dir = store.dirOf(d.clientId).apply { mkdirs() }
        var bytes = 0L
        d.segments.forEach {
            val f = File(dir, MeetingDraftStore.segmentName(it.seq))
            val body = ByteArray(1000) { 3 }
            f.writeBytes(body)
            bytes += body.size
        }
        assertTrue("用例前置：清单要写得进去", store.save(d))
        return bytes
    }

    // ---------------------------------------------------------------- 绝不早删

    @Test
    fun `未上传的不管多旧都不删`() {
        // 这是"绝不早删"最要紧的一档：音频还没上去，删了就是**真丢**。
        // 时间再久也不行 —— "放了半年"不构成删它的理由，用户要的是那份录音。
        val old = draft(endedIso = "2025-01-01T10:00:00", uploaded = false)
        val plan = OfflineMeetingRetention.plan(listOf(old), now, days = 7)

        assertTrue("未上传的一律不动（哪怕放了半年）", plan.nothingToDo)
        assertEquals(0, plan.count)
        assertEquals(0L, plan.bytes)
        assertEquals("但要说清它还留着", 1, plan.kept)
    }

    @Test
    fun `已上传但还没到期的不删`() {
        val fresh = draft(endedIso = "2026-10-16T11:00:00", uploaded = true)
        val plan = OfflineMeetingRetention.plan(listOf(fresh), now, days = 7)

        assertTrue("1 小时前刚结束：远没到期，一个字节都不许动", plan.nothingToDo)
        assertEquals(1, plan.kept)
    }

    @Test
    fun `已经对账通过但服务端没收尾（没转写）的也不删`() {
        // 为什么单独钉：保留策略要求的是"上传成功 **且** 服务端收尾过"。
        // 拿不到收尾证据时**多留一份**永远比删错一份便宜（`stopped` 传 false 就是"不确定"）。
        val d = draft(endedIso = "2026-08-01T10:00:00", uploaded = true)
        val plan = OfflineMeetingRetention.plan(
            drafts = listOf(d), nowMs = now, days = 7, stopped = { false })

        assertTrue("没有'服务端已收尾'的证据 → 不清", plan.nothingToDo)
    }

    @Test
    fun `还在录的那一场绝不删`() {
        val recording = draft(endedIso = "", uploaded = true)
        assertFalse("还在录就没有'结束时间'，更不该被当成到期的",
            OfflineMeetingRetention.isEligible(recording, now, 7, stopped = true))
    }

    @Test
    fun `永不自动清理这一档什么都不清`() {
        val old = draft(endedIso = "2025-01-01T10:00:00", uploaded = true)
        assertTrue("0 的语义必须是'不清'（不是'立刻清' —— 搞反了会当场删光用户的音频）",
            OfflineMeetingRetention.plan(listOf(old), now, days = 0).nothingToDo)
        assertTrue(OfflineMeetingRetention.neverCleans(0))
        assertTrue(OfflineMeetingRetention.neverCleans(-3))
        assertFalse(OfflineMeetingRetention.neverCleans(1))
    }

    // ---------------------------------------------------------------- 超期才删

    @Test
    fun `超过保留天数的已上传清单才会被清`() {
        val stale = draft(id = "stale", endedIso = "2026-10-01T11:00:00", uploaded = true)
        val fresh = draft(id = "fresh", endedIso = "2026-10-15T11:00:00", uploaded = true)
        val plan = OfflineMeetingRetention.plan(listOf(stale, fresh), now, days = 7)

        assertEquals("只清超期那一场", listOf("stale"), plan.items.map { it.draft.clientId })
        assertEquals(1, plan.kept)
    }

    @Test
    fun `边界：正好到期那一刻算到期`() {
        // 判据写的是 `now >= ended + days`（含等号）—— 边界必须**写死**，
        // 否则同一条口径在两台机器上会因为一次毫秒抖动而不同（用户看到的是"有时清、有时不清"）。
        val days = 7
        val ended = "2026-10-09T12:00:00"
        val endMs = epochOf(ended)
        val exactly = draft(endedIso = ended, uploaded = true)

        assertTrue("正好 7×24 小时：算到期",
            OfflineMeetingRetention.isEligible(exactly, endMs + days * day, days, stopped = true))
        assertFalse("差 1 毫秒：不算（宁可多留一会儿）",
            OfflineMeetingRetention.isEligible(exactly, endMs + days * day - 1, days, stopped = true))
    }

    @Test
    fun `空清单不炸（没有清单 空清单 没有段的清单）`() {
        // 最容易被漏掉的一档：什么都没发生就该什么都不做，而且**不许抛**
        // （它跑在补传成功之后那条路上，抛出去会让"已经传上去了"这件事看着像失败）
        val emptyStore = store()
        assertTrue(OfflineMeetingRetention.plan(emptyList(), now, 7).nothingToDo)
        assertEquals(0L, OfflineMeetingRetention.totalAudioBytes(emptyStore, emptyList()))

        val noSegs = draft(uploaded = true, segs = emptyList())
        assertTrue("没有段的清单没什么可清（也不该被显示成一场）",
            OfflineMeetingRetention.plan(listOf(noSegs), now, 7).nothingToDo)

        // 真的空目录：`withAudio()` 与"占用"都要给 0，而不是抛
        assertEquals(0, emptyStore.withAudio().size)
        assertEquals(0L, OfflineMeetingRetention.totalAudioBytes(emptyStore, emptyStore.withAudio()))
    }

    // ---------------------------------------------------------------- 占用与"删的是什么"

    @Test
    fun `音频占用按清单里真有的段文件算`() {
        val store = store()
        val d = draft(id = "a", uploaded = true)
        val written = materialize(store, d)
        assertEquals("写进去多少就该报多少", written, OfflineMeetingRetention.audioBytes(store, d))
        assertEquals(written, OfflineMeetingRetention.totalAudioBytes(store, listOf(d)))
    }

    @Test
    fun `每条都说得出删的是哪一场（时间 段数 大小）`() {
        val store = store()
        val d = draft(id = "a", endedIso = "2026-10-01T11:00:00", uploaded = true)
        materialize(store, d)
        val plan = OfflineMeetingRetention.plan(
            listOf(d), now, days = 7, sizeOf = { OfflineMeetingRetention.audioBytes(store, it) })

        val item = plan.items.single()
        val label = item.label
        assertTrue("要能看出是哪一场（开始时间）：$label", label.contains("2026-10-16T10:00:00"))
        assertTrue("要能看出几段：$label", label.contains("2 段"))
        assertTrue("要能看出多大：$label", label.contains("KB") || label.contains("B"))
        assertTrue("要能看出为什么可以删：${item.why}", item.why.contains("保留 7 天"))
        assertTrue("原因里要有点出'已对账通过并已收尾'：${item.why}",
            item.why.contains("已对账通过"))
    }

    @Test
    fun `总量是各场之和（设置页那行'当前占用'）`() {
        val store = store()
        val a = draft(id = "a", endedIso = "2026-09-01T11:00:00", uploaded = true)
        val b = draft(id = "b", endedIso = "2026-09-02T11:00:00", uploaded = true)
        val bytes = materialize(store, a) + materialize(store, b)
        assertEquals(bytes, OfflineMeetingRetention.totalAudioBytes(store, listOf(a, b)))
    }

    @Test
    fun `读不懂结束时间的不清（不编一个 1970 出来）`() {
        // 编一个 0 出来会算出"已经过了 20000 天" → 直接放行删掉。
        // 读不懂 = 不清（"绝不早删"的方向永远是"多留"）。
        // 注意：这一份里清单自己的 started_at 也读不懂、段的时间也读不懂 ——
        // 三条线索**全都**没有时才允许回 null（有一条能算就得算出来，见下一条用例）。
        val broken = draft(endedIso = "下午三点", uploaded = true)
        assertNull("三条线索全没有 → 结论只能是 null",
            OfflineMeetingRetention.endedAtMs(broken.copy(
                startedAt = "??",
                segments = listOf(seg(1).copy(startedAt = "??"), seg(2).copy(startedAt = "??")))))
        assertTrue(OfflineMeetingRetention.plan(listOf(broken), now, 7).nothingToDo)
    }

    @Test
    fun `结束时间读不懂时退到最后一段的结束时刻`() {
        val d = draft(endedIso = "", uploaded = true, segs = listOf(seg(2)))
        // 最后一段 10:00:00 起 + 30 秒 = 10:00:30
        assertEquals(epochOf("2026-10-16T10:00:30"), OfflineMeetingRetention.endedAtMs(d))
    }

    // ---------------------------------------------------------------- 真删（只删选中那些）

    @Test
    fun `清理只删选中的那些 别的音频一个都不动`() {
        val store = store()
        val stale = draft(id = "stale", endedIso = "2026-10-01T11:00:00", uploaded = true)
        val fresh = draft(id = "fresh", endedIso = "2026-10-15T11:00:00", uploaded = true)
        val pending = draft(id = "pending", endedIso = "2026-10-01T11:00:00", uploaded = false)
        materialize(store, stale); materialize(store, fresh); materialize(store, pending)

        val plan = OfflineMeetingRetention.plan(listOf(stale, fresh, pending), now, days = 7)
        assertEquals(listOf("stale"), plan.items.map { it.draft.clientId })

        val out = store.purgeAudio(plan.items.single().draft)

        assertEquals("删掉两个段文件", 2, out.files)
        assertTrue("释放量要是**真的**删掉的那些字节", out.bytes > 0)
        assertTrue("到期那场的音频没了", !store.segmentFile("stale", 1).isFile)
        assertTrue("没到期的**一个字节都不许动**", store.segmentFile("fresh", 1).isFile)
        assertTrue("还没传上去的更不许动", store.segmentFile("pending", 1).isFile)
        assertTrue("清单本身留着（它是那一场会唯一的本地事实，才几 KB）",
            store.load("stale") != null)
    }

    @Test
    fun `FLAC 的段照样算占用、也照样能清（第①件事之后新增的那一档）`() {
        // 段改成 FLAC 之后最容易漏的一处：占用统计/清理如果还按 `seg-%04d.wav` 拼文件名，
        // 表现会是"占用永远是 0、到期了也清不掉" —— 用户只看到手机越来越满，却查不出原因。
        val store = store()
        val flacSegs = listOf(
            DraftSegment(1, "seg-0001.flac", "2026-10-16T10:00:00", 0.0, 30.0,
                format = SegmentFormat.FLAC),
            DraftSegment(2, "seg-0002.wav", "2026-10-16T10:00:30", 30.0, 30.0,
                format = SegmentFormat.WAV),
        )
        val d = draft(id = "mixed", endedIso = "2026-10-01T11:00:00", uploaded = true,
            segs = flacSegs)
        val dir = store.dirOf("mixed").apply { mkdirs() }
        File(dir, "seg-0001.flac").writeBytes(ByteArray(2000) { 1 })
        File(dir, "seg-0002.wav").writeBytes(ByteArray(3000) { 2 })
        assertTrue(store.save(d))

        assertEquals("两种格式都要算进占用（否则设置页显示的占用是假的）",
            5000L, OfflineMeetingRetention.audioBytes(store, d))
        val plan = OfflineMeetingRetention.plan(listOf(d), now, days = 7)
        assertEquals("到期了就该能清", 1, plan.count)
        val out = store.purgeAudio(d)
        assertEquals("两个文件都要删掉（`.flac` 那个最容易漏）", 2, out.files)
        assertEquals(5000L, out.bytes)
        assertTrue(!File(dir, "seg-0001.flac").isFile)
        assertTrue(!File(dir, "seg-0002.wav").isFile)
    }

    @Test
    fun `清理后占用立刻下来`() {
        val store = store()
        val stale = draft(id = "stale", endedIso = "2026-10-01T11:00:00", uploaded = true)
        materialize(store, stale)
        assertTrue(OfflineMeetingRetention.audioBytes(store, stale) > 0)
        store.purgeAudio(stale)
        assertEquals("删完占用要归零（否则设置页那行会一直显示旧的数）",
            0L, OfflineMeetingRetention.audioBytes(store, stale))
        assertEquals(0L, OfflineMeetingRetention.totalAudioBytes(store, listOf(stale)))
    }

    @Test
    fun `文件已经不在了也不许报成删掉了`() {
        val store = store()
        val d = draft(id = "x", uploaded = true)
        val dir = store.dirOf("x").apply { mkdirs() }
        assertTrue(store.save(d))
        // 目录里什么都没有（用户手工清过 / 上次删了一半）
        assertEquals(0, store.purgeAudio(d).files)
        assertTrue("一个文件都没删时不许说'清完了'", store.purgeAudio(d).nothingDeleted)
        assertTrue(dir.isDirectory)
    }

    // ---------------------------------------------------------------- 档位与文案

    @Test
    fun `保留天数只认那几档 且夹到最近的档`() {
        for (d in OfflineMeetingRetention.RETENTION_DAY_CHOICES) {
            assertEquals("$d 是界面上真实存在的档，必须原样留着",
                d, OfflineMeetingRetention.normalizeDays(d))
        }
        // 3 距 1 是 2、距 7 是 4 → 取 1（**不是**夹到下界 0 = 永不清，那与用户意图相反）
        assertEquals(1, OfflineMeetingRetention.normalizeDays(3))
        assertEquals(7, OfflineMeetingRetention.normalizeDays(6))
        assertEquals("负数落回最近那一档（0 = 永不清），绝不是'立刻清'",
            0, OfflineMeetingRetention.normalizeDays(-99))
        assertEquals(30, OfflineMeetingRetention.normalizeDays(9999))
    }

    @Test
    fun `默认是 7 天 而且在可选档里`() {
        assertEquals(7, OfflineMeetingRetention.DEFAULT_RETENTION_DAYS)
        assertTrue(OfflineMeetingRetention.RETENTION_DAY_CHOICES
            .contains(OfflineMeetingRetention.DEFAULT_RETENTION_DAYS))
    }

    @Test
    fun `夹取过的值再过一遍不变`() {
        for (raw in listOf(-99, 0, 1, 3, 6, 7, 14, 30, 9999)) {
            val once = OfflineMeetingRetention.normalizeDays(raw)
            assertEquals("raw=$raw 再夹一次变了", once, OfflineMeetingRetention.normalizeDays(once))
        }
    }

    @Test
    fun `文案如实说'本地还留着 N 天的备份'`() {
        val note = OfflineMeetingRetention.retentionNote(7)
        assertTrue("用户 2026-10-16 点名要的口径：$note", note.contains("本地还留着 7 天的备份"))
        assertTrue("要说明到期后会清：$note", note.contains("自动清"))
        val never = OfflineMeetingRetention.retentionNote(0)
        assertTrue("永不自动清理那一档不许说'到期后会自动清'：$never",
            never.contains("永不自动清理") && !never.contains("到期后会自动清"))
    }

    @Test
    fun `档位标签说人话`() {
        assertEquals("永不自动清理", OfflineMeetingRetention.label(0))
        assertEquals("保留 1 天", OfflineMeetingRetention.label(1))
        assertEquals("保留 7 天", OfflineMeetingRetention.label(7))
    }

    @Test
    fun `大小显示是人话`() {
        assertEquals("0 B", RetentionPlan.humanBytes(0))
        assertEquals("1023 B", RetentionPlan.humanBytes(1023))
        assertEquals("负数当 0（别显示 -1 B）", "0 B", RetentionPlan.humanBytes(-5))
    }

    @Test
    fun `大小显示在 KB MB GB 之间切换`() {
        assertEquals("1 KB", RetentionPlan.humanBytes(1024))
        assertEquals("1.0 MB", RetentionPlan.humanBytes(1024L * 1024))
        assertEquals("1.50 GB", RetentionPlan.humanBytes(1024L * 1024 * 1024 * 3 / 2))
        assertEquals("负数当 0（别显示 -1 B）", "0 B", RetentionPlan.humanBytes(-5))
    }

    @Test
    fun `DONE 的那行字要说出保留策略`() {
        // 修①+修②合起来的那句话：**已补传并开始转写** + 本地副本还能留多久
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.DONE, totalSegments = 3, stopped = true,
            retentionNote = OfflineMeetingRetention.retentionNote(7)))
        assertTrue("要说清已经开始转写了（没有 stop 就没有转写）：$line",
            line.contains("开始转写"))
        assertTrue("要如实说本地副本还留 7 天：$line", line.contains("本地还留着 7 天的备份"))
        assertFalse("**不许**再说'没删'那种含糊话", line.contains("没删"))
    }

    @Test
    fun `没收尾时那行字不许说开始转写`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.DONE, totalSegments = 3, stopped = false))
        assertFalse("没有 stop 的证据就绝不能说'开始转写'：$line", line.contains("开始转写"))
        assertTrue("要如实说还没收到收尾确认：$line", line.contains("收尾"))
    }

    @Test
    fun `清单损坏的目录不会被当成有音频的场`() {
        // `withAudio()` 走 `list()`，而 `list()` 会跳过读不动的清单 —— 设置页那条
        // "当前占用"不许因为一个坏文件而整条读不出来
        val root = tmp.newFolder("offline")
        val store = MeetingDraftStore(root)
        materialize(store, draft(id = "good"))
        val bad = File(root, "bad").apply { mkdirs() }
        File(bad, MeetingDraftStore.MANIFEST_NAME)
            .writeBytes("""{"client_id":"bad","segments":[{""".toByteArray(StandardCharsets.UTF_8))
        assertEquals(listOf("good"), store.withAudio().map { it.clientId })
    }

    @Test
    fun `可读的时间戳格式化得出来`() {
        val iso = MeetingDraftStore.isoOf(epochOf("2026-10-16T11:00:00"))
        assertTrue(OfflineMeetingRetention.readableAt(iso).contains("2026-10-16"))
        assertEquals("读不懂就原样回（别编一个时间出来）", "下午三点",
            OfflineMeetingRetention.readableAt("下午三点"))
    }

    @Test
    fun `plan 默认把 uploaded 当成'已收尾'（那是 syncOne 只在 stop 成功后才写上的标记）`() {
        val store = store()
        val stale = draft(id = "stale", endedIso = "2026-10-01T11:00:00", uploaded = true)
        materialize(store, stale)
        // 不传 stopped：默认用 draft.uploaded。而 uploaded **只在**
        // `OfflineMeetingSync` 的第 ④ 步（stop 成功）之后才被写上 —— 这就是那个契约。
        val plan = OfflineMeetingRetention.plan(listOf(stale), now, days = 7)
        assertEquals(1, plan.count)
        assertEquals("stale", plan.items[0].draft.clientId)
    }

    @Test
    fun `ISO 本地时间与 epoch 的互换在用例里是稳的`() {
        // 这条是给上面所有边界用的地基：`ended_at` 与 `now` 必须走**同一个**时区口径，
        // 否则"正好到期"那条用例会在别的时区上假红（而那是用例的问题，不是策略的问题）
        assertEquals(LocalDateTime.of(2026, 10, 16, 12, 0, 0),
            LocalDateTime.ofInstant(java.time.Instant.ofEpochMilli(now),
                java.time.ZoneId.systemDefault()))
    }
}
