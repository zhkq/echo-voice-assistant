package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * 「打开 App 为什么没自动同步」这一半的判据（用户 2026-10-19）。
 *
 * 两件事分开钉：
 *  1. **待传摘要**（会议页那块入口的数据源）必须只从**既有清单**算出来，
 *     而且"已上传的 / 一段都没有的"不许算进去 —— 否则界面上会永远挂着"待上传 N 段"；
 *  2. **自动重试的退避**：试满 `MAX_AUTO_ATTEMPTS` 次之后 `shouldRetryNow` 在**本进程里**
 *     恒为 false（这就是"打开 App 等几分钟一条段都没发"的坑之一）；App 打开时必须清零。
 */
class MeetingAutoSyncTest {

    private fun draft(
        clientId: String,
        startedAt: String,
        endedAt: String = startedAt,
        segments: Int = 3,
        acknowledged: Int = 0,
        uploaded: Boolean = false,
        format: com.echo.contact.audio.SegmentFormat = com.echo.contact.audio.SegmentFormat.FLAC,
    ): MeetingDraft = MeetingDraft(
        clientId = clientId,
        startedAt = startedAt,
        endedAt = endedAt,
        acknowledgedSegments = acknowledged,
        uploaded = uploaded,
        segments = (1..segments).map { seq ->
            DraftSegment(
                seq = seq,
                file = MeetingDraftStore.segmentName(seq, format),
                startedAt = startedAt,
                startOffsetSeconds = (seq - 1) * 600.0,
                durationSeconds = 600.0,
                format = format,
            )
        },
    )

    // ---------------------------------------------------------------- 待传摘要（既有清单）

    @Test
    fun `待传摘要只算还没上传而且手里有段的清单`() {
        val waiting = draft("phone-a", "2026-10-19T10:00:00")
        val uploaded = draft("phone-b", "2026-10-19T09:00:00", uploaded = true)
        val empty = draft("phone-c", "2026-10-19T08:00:00", segments = 0)
        val s = pendingSummaryOf(listOf(waiting, uploaded, empty))
        assertEquals("只有那一场真的要传", 1, s.drafts)
        assertEquals("待传段数 = 3", 3, s.pendingSegments)
        assertEquals("总段数（进度那条的分母）", 3, s.totalSegments)
        assertEquals("最早一场就是它", "2026-10-19T10:00:00", s.earliestStartedAt)
        assertTrue(s.hasPending)
    }

    @Test
    fun `待传段数要扣掉服务端已经认下的水位`() {
        val s = pendingSummaryOf(listOf(draft("phone-a", "2026-10-19T10:00:00",
            segments = 4, acknowledged = 1)))
        assertEquals(4, s.totalSegments)
        assertEquals("已确认 1 段，还剩 3 段", 3, s.pendingSegments)
    }

    @Test
    fun `最早一场按开始时间取最小值而不是清单顺序`() {
        // `list()` 是按开始时间升序的，但摘要**不许**依赖调用方的顺序（那是隐式契约）
        val later = draft("phone-later", "2026-10-19T18:00:00")
        val earlier = draft("phone-earlier", "2026-10-19T08:30:00")
        val s = pendingSummaryOf(listOf(later, earlier))
        assertEquals(2, s.drafts)
        assertEquals("2026-10-19T08:30:00", s.earliestStartedAt)
    }

    @Test
    fun `还在录的那一场也算待传并单独标出来`() {
        // ended_at 空着 = 还在录：段照传，但收尾要等用户按结束（同步会降级成 INCREMENTAL）
        val recording = draft("phone-a", "2026-10-19T10:00:00", endedAt = "")
        val s = pendingSummaryOf(listOf(recording))
        assertEquals(1, s.drafts)
        assertEquals(1, s.recordingDrafts)
        assertEquals(3, s.pendingSegments)
    }

    @Test
    fun `没有待传时摘要全零`() {
        val s = pendingSummaryOf(emptyList())
        assertEquals(0, s.drafts)
        assertEquals(0, s.pendingSegments)
        assertEquals("", s.earliestStartedAt)
        assertFalse(s.hasPending)
    }

    /**
     * 摘要读的是**清单里记的文件名**，不是按 seq 再猜一遍格式。
     *
     * 为什么要钉：段可能退回 WAV（FLAC 编码器不可用），这时按 seq 拼成 `.flac` 就会
     * "文件不存在" —— 补传卡在第一段，而摘要还显示"待上传 3 段"（看着像在传）。
     */
    @Test
    fun `段文件名按清单里记的格式`() {
        val wav = draft("phone-a", "2026-10-19T10:00:00",
            format = com.echo.contact.audio.SegmentFormat.WAV)
        assertEquals("seg-0001.wav", wav.segments.first().file)
        val s = pendingSummaryOf(listOf(wav))
        assertEquals(3, s.pendingSegments)
    }

    // ---------------------------------------------------------------- 退避（坑④）

    /**
     * 自动重试**次数用尽**之后，本进程里 `shouldRetryNow` 恒 false ——
     * 这就是"打开 App 等几分钟、一条段都没发"的一条真实原因（8 次多半是早上在地铁/无网时用掉的）。
     */
    @Test
    fun `自动重试用尽后确实不再自动试（这就是那个坑）`() {
        val now = 1_000_000L
        val dead = shouldRetryNow(
            nowMs = now,
            attempts = BackoffPolicy.MAX_AUTO_ATTEMPTS,
            nextAtMs = now + 60_000,
            wasUnreachable = false,
            reachable = true,
        )
        assertFalse("试满 8 次之后 shouldRetryNow 必须为 false（否则这条用例没测到坑）", dead)
    }

    /** App 打开 = 用户明确的一次意图 → 退避清零，于是"打开 App 就会尝试同步"。 */
    @Test
    fun `App 打开会把自动重试的退避清零`() {
        val now = 1_000_000L
        val reset = backoffOnAppOpen()
        assertEquals(0, reset.attempts)
        assertEquals(null, reset.nextAtMs)
        assertFalse(reset.wasUnreachable)
        assertTrue(
            "清零之后必须立刻允许再试（否则打开 App 也没有任何自动入口）",
            shouldRetryNow(
                nowMs = now,
                attempts = reset.attempts,
                nextAtMs = reset.nextAtMs,
                wasUnreachable = reset.wasUnreachable,
                reachable = true,
            ),
        )
    }

    /** 退避清零是**进程级**的三个字段一起清（`MeetingMicService.onAppOpened` 用的就是它）。 */
    @Test
    fun `App 打开这次清零走的是同一个纯函数`() {
        val src = File(moduleRootForService(), "src/main/java/com/echo/contact/service/MeetingMicService.kt")
            .readText(Charsets.UTF_8)
        val body = src.substringAfter("fun onAppOpened(").substringBefore("fun tickFromForeground(")
        assertTrue("必须用 backoffOnAppOpen 的结论", body.contains("backoffOnAppOpen()"))
        assertTrue("三个字段一起清：attempts", body.contains("syncAttempts = reset.attempts"))
        assertTrue("三个字段一起清：下一次时刻", body.contains("syncNextAtMs = reset.nextAtMs"))
        assertTrue("三个字段一起清：上一轮是不是不可达", body.contains("syncWasUnreachable = reset.wasUnreachable"))
        assertTrue("先刷新摘要再 tick（界面立刻有数）",
            body.indexOf("refreshPendingSummary") < body.indexOf("tickFromForeground"))
    }

    /**
     * 用例自己的"模块根"（不依赖 ui 包里那个 internal 工具，service 包不跨包借）。
     * 逻辑与 `TestSources.moduleRoot` 一致：从 `user.dir` 往上找 `src/main/AndroidManifest.xml`。
     */
    private fun moduleRootForService(): File {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null) {
            if (File(dir, "src/main/AndroidManifest.xml").isFile) return dir
            if (File(dir, "app/src/main/AndroidManifest.xml").isFile) return File(dir, "app")
            if (File(dir, "android/app/src/main/AndroidManifest.xml").isFile) return File(dir, "android/app")
            dir = dir.parentFile
        }
        error("找不到 android/app 模块目录（user.dir = ${System.getProperty("user.dir")}）")
    }
}
