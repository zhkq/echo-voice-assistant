package com.echo.contact.ui

import com.echo.contact.service.MicState
import com.echo.contact.service.OfflinePhase
import com.echo.contact.service.PendingSyncSummary
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 会议页那块**常驻**的「待上传 N 段 · 现在同步」的判据（用户 2026-10-19 真机反馈）。
 *
 * 用户原话：补传 413 修好之后 *"没有上传的地方了"* —— 那不是数字错，是**入口消失**：
 *  * 全屏录音页一关，那颗「现在同步」就跟着没了；
 *  * 会议页原来那块补传进度只在 `MicState.syncPhase != IDLE` 时出现，而 `MicState`
 *    是内存态，App 重开之后是空的 → 磁盘上还有 3 段，界面上却一块都不画。
 *
 * 所以这一批用例钉的是"入口与原因都在、而且不依赖录音页也不依赖内存态"：
 *  1. 有 N 段 → 显示「待上传 N 段」+ 可点的按钮，点了**真的**调既有同步入口；
 *  2. 没有待传 → 不显示误导性内容（整块不画，"刚同步完"才留一句"全部已上传"）；
 *  3. 失败（413 / 没网 / 会议进行中 / 服务端拒收）→ 会议页有可见原因、且能再点重试；
 *  4. `WAITING_MEETING_END` → 如实说"等电脑那场结束"。
 */
class MeetingSyncEntryTest {

    private fun pending(
        drafts: Int = 1,
        pendingSegments: Int = 3,
        totalSegments: Int = 3,
        earliestStartedAt: String = "2026-10-19T14:03:07",
        recordingDrafts: Int = 0,
    ) = PendingSyncSummary(
        drafts = drafts,
        pendingSegments = pendingSegments,
        totalSegments = totalSegments,
        earliestStartedAt = earliestStartedAt,
        recordingDrafts = recordingDrafts,
    )

    // ---------------------------------------------------------------- ① 有 N 段就显示

    @Test
    fun `有 N 段待传时会议页显示待上传与可点的现在同步`() {
        val e = meetingSyncEntry(pending(), MicState())
        assertTrue("还有 3 段没传，这块必须出现", e.visible)
        assertEquals(3, e.pendingSegments)
        assertEquals("标题要如实说几段", "待上传 3 段", e.title)
        assertEquals("按钮该是可点的", MeetingSyncAction.SYNC_NOW, e.action)
        assertEquals("现在同步", e.actionLabel)
        // "可显示最早那场的时间"（用户要的）：补传按会议顺序排队，最早那场就是该看到的那一场
        assertEquals("最早一场的时间", "10-19 14:03", e.earliestLabel)
        assertFalse("没失败就别用红字/重试文案", e.failed)
    }

    /**
     * 点「现在同步」**真的**会调既有同步入口（不是另起一条路）。
     *
     * 这里用打桩 lambda 断言"调到了"；接到哪个入口由源码断言钉住
     * （`MeetingsTab` 那处必须是 `MeetingMicService.syncNow`）。
     */
    @Test
    fun `点现在同步真的调到既有同步入口`() {
        var calls = 0
        val e = meetingSyncEntry(pending(), MicState())
        assertTrue("这一次该发起同步", dispatchMeetingSync(e.action) { calls += 1 })
        assertEquals("必须恰好调一次既有入口", 1, calls)

        // 没有待传时那块根本没有按钮：不许调（"空点一下"不该发请求）
        var none = 0
        val empty = meetingSyncEntry(PendingSyncSummary(), MicState())
        assertFalse(dispatchMeetingSync(empty.action) { none += 1 })
        assertEquals(0, none)
    }

    /** 同步中：按钮置灰（连点只会拿到"上一次还在跑"），而且进度要说得清。 */
    @Test
    fun `同步中显示已传总数与当前段且按钮不可连点`() {
        val e = meetingSyncEntry(
            pending(pendingSegments = 3, totalSegments = 5),
            MicState(syncPhase = OfflinePhase.UPLOADING, syncPending = 3, syncConfirmed = 2),
        )
        assertTrue("同步中这块要在", e.visible)
        assertTrue("标题要给进度：${e.title}", e.title.contains("已传 2/5 段"))
        assertTrue("要说正在传第几段：${e.title}", e.title.contains("第 3 段"))
        assertTrue("进度那一行要独立可读：${e.progress}", e.progress.contains("已传 2 / 共 5 段"))
        assertEquals("同步中按钮置灰", MeetingSyncAction.SYNCING, e.action)
        var calls = 0
        assertFalse("同步中再点不许再发一次", dispatchMeetingSync(e.action) { calls += 1 })
        assertEquals(0, calls)
    }

    /**
     * 同步中的总数取**本轮进度**（`MicState.syncTotal`），而不是可能还在过期的磁盘摘要
     * —— 否则同步一开始那几秒会显示旧分母（例如 2，而这一轮其实有 5 段要传）。
     */
    @Test
    fun `同步中的总数来自本轮进度而不是过期的磁盘摘要`() {
        val e = meetingSyncEntry(
            PendingSyncSummary(drafts = 1, pendingSegments = 2, totalSegments = 2),
            MicState(
                syncPhase = OfflinePhase.UPLOADING,
                syncConfirmed = 3,
                syncPending = 2,
                syncTotal = 5,
            ),
        )
        assertTrue("总数要用本轮那 5 段：${e.title}", e.title.contains("已传 3/5 段"))
        assertTrue("当前段接着往后数：${e.title}", e.title.contains("第 4 段"))
    }

    // ---------------------------------------------------------------- ② 没有待传就收起来

    @Test
    fun `没有待传时不显示误导性内容`() {
        val e = meetingSyncEntry(PendingSyncSummary(), MicState())
        assertFalse("没有待传就不该占地方", e.visible)
        assertFalse("不许出现'待上传'这种会吓人的字", e.title.contains("待上传"))
        assertEquals("没有待传就没有按钮", MeetingSyncAction.NONE, e.action)
        assertEquals("没有待传就没有原因可说", "", e.note)
        assertEquals("没有待传就不显示时间", "", e.earliestLabel)
    }

    /** 刚同步完留一句"全部已上传"（确认感），但**不给按钮**（没东西可点）。 */
    @Test
    fun `同步完成后只留一句全部已上传`() {
        val e = meetingSyncEntry(
            PendingSyncSummary(),
            MicState(
                syncPhase = OfflinePhase.DONE,
                syncNote = "录音已补传并开始转写（3 段）· 手机上的副本还留着",
            ),
        )
        assertTrue("刚成功要给确认", e.visible)
        assertEquals("全部已上传", e.title)
        assertEquals(MeetingSyncAction.NONE, e.action)
        assertTrue("成功的那句原话要带上", e.note.contains("开始转写"))
    }

    // ---------------------------------------------------------------- ③ 失败必须看得见、能重试

    @Test
    fun `413 失败给人话原因且可以再点重试`() {
        // 真实文案来自 `OfflineMeetingTransport.uploadSegment`（HTTP 413 → 服务端拒绝）
        val e = meetingSyncEntry(
            pending(pendingSegments = 4, totalSegments = 6),
            MicState(
                syncPhase = OfflinePhase.FAILED,
                syncNote = "第 3 段没被收下（HTTP 413，服务端拒绝（重试也不会好，多半是契约/权限没对齐））",
                syncPending = 4,
                syncConfirmed = 2,
            ),
        )
        assertTrue("失败必须显出来", e.visible)
        assertTrue("原因要能看出是 413：${e.note}", e.note.contains("413"))
        assertTrue("要说清是哪一类问题（段太大）：${e.note}", e.note.contains("太大"))
        assertTrue("不许丢掉'音频还在手机里'这句（否则用户以为丢了）", e.note.contains("还在手机里"))
        assertEquals("失败后按钮该是'重试同步'", "重试同步", e.actionLabel)
        assertEquals("失败也必须能再点", MeetingSyncAction.SYNC_NOW, e.action)
        assertTrue("失败档要标出来（界面用它上红色）", e.failed)
    }

    @Test
    fun `没网失败给人话原因且可以再点重试`() {
        val e = meetingSyncEntry(
            pending(),
            MicState(
                syncPhase = OfflinePhase.UNREACHABLE,
                syncNote = "ECHO 连不上（HTTP 500，ECHO 自己出错了）",
                syncPending = 3,
            ),
        )
        assertTrue("原因要说是网络：${e.note}", e.note.contains("没网") || e.note.contains("连不上"))
        assertEquals(MeetingSyncAction.SYNC_NOW, e.action)
        assertEquals("重试同步", e.actionLabel)
    }

    @Test
    fun `服务端拒收说服务端而不说网络`() {
        val e = meetingSyncEntry(
            pending(),
            MicState(
                syncPhase = OfflinePhase.FAILED,
                syncNote = "第 1 段被服务端拒了：不支持这个契约",
                syncPending = 3,
            ),
        )
        assertTrue("要说是服务器拒收：${e.note}", e.note.contains("服务器拒收"))
        assertFalse("别把服务端拒绝说成没网（会把人引去查网线）",
            e.note.contains("没网"))
        assertEquals(MeetingSyncAction.SYNC_NOW, e.action)
    }

    /**
     * 电脑上那场还在录 → 同步在等（`WAITING_MEETING_END`），界面要**如实说在等**，
     * 不能看着像卡住；而且仍然允许手动再试一次。
     */
    @Test
    fun `电脑那场还在录时如实显示等它结束`() {
        val e = meetingSyncEntry(
            pending(pendingSegments = 2, totalSegments = 2),
            MicState(
                syncPhase = OfflinePhase.WAITING_MEETING_END,
                syncNote = "ECHO 正在录另一场（某场会）：等它录完再传，不抢占",
                syncPending = 2,
            ),
        )
        assertTrue("等也要让人看见", e.visible)
        assertTrue("标题要说在等电脑那场：${e.title}", e.title.contains("等电脑那场结束"))
        assertTrue("原因要说清是在等而不是卡住：${e.note}", e.note.contains("等它结束"))
        assertTrue("还要说不会抢占", e.note.contains("不会抢占"))
        assertTrue("等的时候也标出来", e.waitingMeetingEnd)
        assertEquals("等的时候也能手动再试", MeetingSyncAction.SYNC_NOW, e.action)
        assertFalse("这不是失败（别上红色）", e.failed)
    }

    /** **不许静默**：失败但没拿到原因时，也必须说一句"还没传上去"。 */
    @Test
    fun `失败但没拿到原因时也不许沉默`() {
        val e = meetingSyncEntry(
            pending(),
            MicState(syncPhase = OfflinePhase.FAILED, syncNote = "", syncPending = 3),
        )
        assertTrue("失败必须显出来", e.visible)
        assertTrue("原因不许为空：'${e.note}'", e.note.isNotBlank())
        assertTrue("要告诉用户还能再试", e.note.contains("重试"))
    }

    /** 录音中也要能看到待传段数，而且要说清"收尾要等你按结束"。 */
    @Test
    fun `还在录的那一场也显示待传并说清不会替你收尾`() {
        val e = meetingSyncEntry(
            pending(pendingSegments = 3, totalSegments = 3, recordingDrafts = 1),
            MicState(active = true, syncPhase = OfflinePhase.RECORDING_LOCAL, syncPending = 3),
        )
        assertTrue("录音中也要看得见", e.visible)
        assertEquals("待上传 3 段", e.title)
        assertTrue("要说清收尾要等用户按结束：${e.note}",
            e.note.contains("还在录") && e.note.contains("按结束"))
    }

    @Test
    fun `最早那场的时间读不懂就原样显示`() {
        assertEquals("", meetingStartLabel(""))
        assertEquals("2026-10-19T14:03:07", "10-19 14:03", meetingStartLabel("2026-10-19T14:03:07"))
        assertEquals("读不懂不编时间", "not-a-time", meetingStartLabel("not-a-time"))
    }

    // ---------------------------------------------------------------- ④ 触发点与落点（源码级）

    /**
     * **打开 App 就会尝试同步**：触发点必须挂在外壳上（五页都活着）。
     *
     * 为什么是源码级断言：Compose 在 JVM 门禁里渲染不了，而这条恰恰是"触发点挂错层"
     * 的错误（挂到只在某一页可见的卡上）—— 那种错误跑任何纯函数用例都发现不了。
     * 现场就是：首页那张状态卡只在「超级助理」页组合，用户录完停在会议页/从通知直达
     * 会议页 → 那条节拍一次都没跑。
     */
    @Test
    fun `打开 App 就会尝试同步且触发点在外壳上`() {
        val shell = moduleText("src/main/java/com/echo/contact/ui/HomeScreen.kt")
        assertTrue("外壳上必须先试一次（不等 5 秒节拍）：onAppOpened",
            shell.contains("MeetingMicService.onAppOpened"))
        assertTrue("外壳上还要周期性 tick", shell.contains("MeetingMicService.tickFromForeground"))
        assertTrue("节拍常量要在这儿", shell.contains("APP_AUTO_SYNC_TICK_MS"))
        // 旧触发点（只在首页那张卡可见时跑）不许再是唯一入口
        assertFalse("首页那张卡里不许再藏着自动同步的触发点",
            moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt")
                .contains(".tickFromForeground("))
        assertFalse("ViewModel 的那条节拍里也不许再藏着它",
            moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")
                .contains(".tickFromForeground("))
    }

    /** 自动同步检查的是**既有离线清单**（`MeetingDraftStore.pending()`），不是另一套存储。 */
    @Test
    fun `自动同步查的是既有离线清单目录`() {
        val svc = moduleText("src/main/java/com/echo/contact/service/MeetingMicService.kt")
        val body = functionBody(svc, "fun tickFromForeground(", "internal suspend fun runOfflineSync(")
        assertTrue("待传清单必须来自既有 store.pending()", body.contains("store.pending()"))
        assertTrue("同步走既有 runOfflineSync", body.contains("runOfflineSync"))
        assertTrue("顺手刷新会议页那块入口的数据源", body.contains("_pendingSummary.value = pendingSummaryOf(waits)"))
        // 不许为这件事另造一个目录/一份清单
        assertFalse("不许另造清单目录", body.contains("offline-meetings-v2"))
        assertTrue("目录口径只有一处（OFFLINE_DIR）", svc.contains("const val OFFLINE_DIR = \"offline-meetings\""))
    }

    /** 会议页那块入口**常驻**：它不依赖全屏录音页，也不依赖内存里的录音状态。 */
    @Test
    fun `会议页的待传入口常驻且不依赖录音页`() {
        val meetings = moduleText("src/main/java/com/echo/contact/ui/MeetingsTab.kt")
        assertTrue("会议页要有那块常驻入口", meetings.contains("MeetingSyncBlock("))
        assertTrue("数据来自磁盘清单的摘要", meetings.contains("pendingSummary"))
        assertTrue("要自己刷新摘要（不靠录音服务）", meetings.contains("refreshPendingSummary"))
        assertTrue("同步接既有入口", meetings.contains("MeetingMicService.syncNow"))
        // 那块入口在会议页只出现一次（两处画同一件事必然有一天分叉）
        assertEquals("入口只能有一处", 1, occurrencesOf(meetings, "MeetingSyncBlock("))
        // 它不许藏在"正在录音"的分支里：录音页自己那颗按钮仍可以留着，但这边必须独立
        val body = functionBody(meetings, "fun MeetingsTab(", "private fun MeetingDetailView(")
        assertTrue("入口必须在会议页的主体里（不是录音时才画）", body.contains("MeetingSyncBlock("))
        assertFalse("UI 层不许自己跑同步实现", body.contains("OfflineMeetingSync.run"))
    }
}
