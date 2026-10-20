package com.echo.contact.service

import com.echo.contact.ui.moduleText
import com.echo.contact.ui.occurrencesOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * 「服务端那场"正在录"的会到底是谁的」—— 2026-10-20 真机 bug 的判据。
 *
 * ## 用户的原话与现场
 *
 * > "电脑上显示手机录音中，但是手机显示电脑在录音"
 *
 * 服务端实际是 `active=true, source=phone, clientId=phone-20261020094624-e1b6, segments=1`
 * —— 也就是**手机那场在补传**。归属其实对，错的是手机这一侧：
 *  * **显示**只看 `active` 就写「电脑在录音」✗；
 *  * **"别抢占"闸**只认"这一刻正在录的那份清单"，而手机的会话 id
 *    **每次开录都会新生成**（`MeetingDraftStore.newClientId()`），App 重开之后
 *    服务端上那场是**上一次启动**留下的 id → 认不出自己 → 自己把自己挡在门外。
 *
 * ## 这一批用例钉什么
 *
 *  1. `source=phone` + `clientId` **在我本地清单里** + `active` → **我的**（还在上传），
 *     **不是**"电脑在录"；
 *  2. 那个 `clientId` **不在我清单里** → **另一台手机**；
 *  3. `source=pc` → 电脑那支麦（既有行为，不许变）；
 *  4. `active=false` → 谁都没在录（不许出现任何"在录"）；
 *  5. **归属判定只有一处实现**，且"别抢占"闸与显示**共用**它（源码级断言）。
 *
 * 纯逻辑在前半段（逐档钉），编排层与源码级在后半段（钉"两处共用一处判据"）。
 */
class MeetingOwnershipTest {

    @get:Rule val tmp = TemporaryFolder()

    /** 这台手机上一次启动留下的那场（真机日志里那个 id 的形状）。 */
    private val oldId = "phone-20261020094624-e1b6"

    /** 这一次启动新建的那场。 */
    private val newId = "phone-20261020130500-7a3c"

    private fun local(vararg ids: String): List<LocalDraftRef> =
        ids.map { LocalDraftRef(it) }

    // ---------------------------------------------------------------- ① 我自己的（含历史）

    /**
     * **判据①**：`source=phone` + `clientId` 在我清单里 + `active` → 「我的·上传中」。
     *
     * 现场这一档就是 2026-10-20 的真机：手机以为"没在录"（本机麦克风确实关了），
     * 服务端那场却还是 `active` —— 因为段还在补传。它不是"电脑在录"。
     */
    @Test
    fun `服务器说手机在录且那个 id 在我清单里时是我的（在上传）`() {
        val owner = meetingOwnership(
            active = true,
            source = "phone",
            clientId = oldId,
            local = local(oldId),
        )

        assertEquals("是我自己那场（在补传），不是我之外的人", MeetingOwner.MINE_UPLOADING, owner)
        assertTrue("闸必须认它是我的（否则它会拒绝续传 = 自己等自己）", owner.isMine)
        // ⚠️ 这一次真机 bug 的正脸：措辞不许再把"我的上传"说成"电脑在录"
        assertEquals("手机上这场的录音还在上传", meetingOwnerTrailing(owner))
        assertFalse("不许说成电脑在录音", meetingOwnerTrailing(owner) == "电脑在录音")
        assertFalse("更不许说「录音中」——录音早就停了", meetingOwnerTrailing(owner).contains("录音中"))
    }

    /**
     * 这一次真机的**根因**：会话 id **每次开录都会变**，服务端上那场是**
     * 上一次启动**留下的 —— 所以"我的"必须按**本地清单里建过的那些 id** 判，
     * 不能按"当前会话 id"判（旧实现 `OfflineMeetingSync.mineRecording` 就是那样）。
     */
    @Test
    fun `上一次启动留下的会话 id 也算我的（App 重开之后不许说成电脑在录）`() {
        val owner = meetingOwnership(
            active = true,
            source = "phone",
            clientId = oldId,                       // 服务端手上是老 id
            local = local(oldId, newId),            // 本地清单里两场都在（新的一场也建了）
        )

        assertEquals("历史会话也是我建的，必须认", MeetingOwner.MINE_UPLOADING, owner)
        assertTrue(owner.isMine)
    }

    /** 这台手机此刻正录着那一份 → 「我的·在录」（与"录完还在传"分开两档文案）。 */
    @Test
    fun `本地正在录那一份时才说这台手机在录音`() {
        val recording = meetingOwnership(
            active = true, source = "phone", clientId = newId,
            local = local(oldId, newId), localRecording = true,
        )
        assertEquals(MeetingOwner.MINE_RECORDING, recording)
        assertEquals("这台手机在录音", meetingOwnerTrailing(recording))
        assertTrue(recording.isMine)

        val uploading = meetingOwnership(
            active = true, source = "phone", clientId = newId,
            local = local(oldId, newId), localRecording = false,
        )
        assertEquals("麦克风停了、段还在传 → 上传档", MeetingOwner.MINE_UPLOADING, uploading)
        assertEquals("手机上这场的录音还在上传", meetingOwnerTrailing(uploading))
    }

    // ---------------------------------------------------------------- ② 别人的

    /** **判据②**：`source=phone` + `clientId` **不在**我清单里 → 另一台手机。 */
    @Test
    fun `不在我清单里的手机 id 是另一台手机`() {
        val owner = meetingOwnership(
            active = true,
            source = "phone",
            clientId = "phone-20261020140000-ffff",
            local = local(oldId, newId),
        )

        assertEquals(MeetingOwner.OTHER_PHONE, owner)
        assertFalse("别人的会绝不许判成我的（判错会把段灌进别人的会）", owner.isMine)
        assertTrue("要说是另一台设备：${meetingOwnerTrailing(owner)}",
            meetingOwnerTrailing(owner).contains("另一台"))
        assertFalse(meetingOwnerTrailing(owner).contains("电脑"))
    }

    // ---------------------------------------------------------------- ③ 电脑那支麦

    /** **判据③**：`source=pc` → 电脑（既有行为，一个字都不许变）。 */
    @Test
    fun `source 是 pc 时判电脑在录音（既有行为）`() {
        val owner = meetingOwnership(
            active = true,
            source = "pc",
            clientId = "",                 // 电脑本机麦那条路服务端**不带** clientId
            local = local(oldId),
        )

        assertEquals(MeetingOwner.PC, owner)
        assertEquals("电脑在录音", meetingOwnerTrailing(owner))
        assertFalse("电脑那场不是我的（别把段灌进去）", owner.isMine)
    }

    // ---------------------------------------------------------------- ④ 没在录

    /** **判据④**：`active=false` → 谁都没在录，任何组合都不许说"在录"。 */
    @Test
    fun `服务端说没在录时谁也不在录`() {
        val cases = listOf(
            meetingOwnership(active = false, source = "phone", clientId = oldId, local = local(oldId)),
            meetingOwnership(active = false, source = "phone", clientId = "phone-x", local = local(oldId)),
            meetingOwnership(active = false, source = "pc", local = local(oldId)),
            meetingOwnership(active = false),
        )
        cases.forEach {
            assertEquals("active=false 只能是 NONE，实际 $it", MeetingOwner.NONE, it)
        }
        assertEquals("未在录音", meetingOwnerTrailing(MeetingOwner.NONE))
        assertEquals("没有在录就没什么要解释的", "", meetingOwnerNote(MeetingOwner.NONE))
    }

    // ---------------------------------------------------------------- 老服务端 / 认不出

    /**
     * 老服务端**不给** `clientId`：退到"会议 id 相等"（清单里真记过才算）。
     *
     * 这条退路是既有行为（`OfflineMeetingSyncTest` 里那条"按会议 id 判"钉着），
     * 搬进这一处之后**一个字都不许丢** —— 否则升级前后会多出一档"自己等自己"。
     */
    @Test
    fun `没有 clientId 时按会议 id 认自己 认不出就宁可判不是`() {
        val mine = meetingOwnership(
            active = true, source = "phone", serverMeetingId = 42,
            local = listOf(LocalDraftRef(oldId, serverMeetingId = 42)),
        )
        assertEquals("会议 id 对得上 → 是我那场", MeetingOwner.MINE_UPLOADING, mine)
        assertTrue(mine.isMine)

        // 对不上（97 不是我的）→ **宁可判不是**（最多多等一会儿，绝不抢占）
        val other = meetingOwnership(
            active = true, source = "phone", serverMeetingId = 97,
            local = listOf(LocalDraftRef(oldId, serverMeetingId = 42)),
        )
        assertEquals(MeetingOwner.OTHER_PHONE, other)
        assertFalse(other.isMine)

        // 服务端连 source 都没给（更老的后端）：认不出是谁的 → UNKNOWN，仍然不抢占
        val unknown = meetingOwnership(
            active = true, source = "", serverMeetingId = 97,
            local = listOf(LocalDraftRef(oldId, serverMeetingId = 42)),
        )
        assertEquals(MeetingOwner.UNKNOWN, unknown)
        assertFalse("认不出时绝不判成我的", unknown.isMine)
        assertFalse("也不许说成电脑在录音（服务端没说）",
            meetingOwnerTrailing(unknown) == "电脑在录音")
    }

    /**
     * 本机清单**还没读出来**（界面第一帧）时先别断言 —— 那一刻说成"另一台设备在录音"
     * 就是又编了一句话。`local = null` 与"空表"必须分得开：
     *  * `null` = 还不知道 → UNKNOWN；
     *  * 空表 = 真的一场都没建过 → 那才是另一台手机。
     */
    @Test
    fun `清单还没读出来时先别断言是谁的`() {
        val notLoaded = meetingOwnership(
            active = true, source = "phone", clientId = oldId, local = null)

        assertEquals(MeetingOwner.UNKNOWN, notLoaded)
        assertFalse(notLoaded.isMine)
        assertFalse("那半秒里不许说成另一台设备",
            meetingOwnerTrailing(notLoaded).contains("另一台"))

        val trulyMine = meetingOwnership(active = true, source = "phone", clientId = oldId, local = emptyList())
        assertEquals("读了、确实没有我建过的清单 → 那就是另一台手机",
            MeetingOwner.OTHER_PHONE, trulyMine)
    }

    // ---------------------------------------------------------------- 文案（措辞）    /** 上传档那句说明：要说清"已传几段"，而且**不编分母**。 */
    @Test
    fun `上传档的说明说清已传几段 不知道总数时不编分母`() {
        val withCount = meetingOwnerNote(MeetingOwner.MINE_UPLOADING, confirmedSegments = 1, totalSegments = 5)
        assertTrue("主句必须是这一句：$withCount", withCount.contains("手机上这场的录音还在上传"))
        assertTrue("要给进度：$withCount", withCount.contains("已传 1/5 段"))
        assertTrue("要说清录音已经停了（否则用户会去找「结束」）：$withCount",
            withCount.contains("录音已经停了"))
        assertFalse("不许说成还在录：$withCount", withCount.contains("录音中"))

        val noTotal = meetingOwnerNote(MeetingOwner.MINE_UPLOADING, confirmedSegments = 0, totalSegments = 0)
        assertFalse("不知道分母就不许编一个：$noTotal", noTotal.contains("已传"))

        // 越界的进度不许画出"已传 7/5"
        val clamp = meetingOwnerNote(MeetingOwner.MINE_UPLOADING, confirmedSegments = 7, totalSegments = 5)
        assertTrue("越界也要夹回分母：$clamp", clamp.contains("已传 5/5 段"))
    }

    /** 电脑/另一台手机/认不出：三档各说各的（都不许含糊成"有会议在录"）。 */
    @Test
    fun `三档别人的说法分得开`() {
        assertEquals("电脑在录音", meetingOwnerTrailing(MeetingOwner.PC))
        assertTrue(meetingOwnerNote(MeetingOwner.PC).contains("不是这台手机"))
        assertTrue(meetingOwnerNote(MeetingOwner.OTHER_PHONE).contains("另一台手机"))
        assertTrue("认不出就如实说认不出：${meetingOwnerNote(MeetingOwner.UNKNOWN)}",
            meetingOwnerNote(MeetingOwner.UNKNOWN).contains("认不出"))
    }

    /**
     * 首页那张状态卡的「会议」一格用的也是**同一处**判据：
     * 我那场已经录完、还在传时写「上传中」——**不许**写「录音中」（同一个 bug 的另一张脸）。
     */
    @Test
    fun `首页会议格用的是同一处判据（上传档不写录音中）`() {
        assertEquals("上传中", meetingOwnerCell(MeetingOwner.MINE_UPLOADING))
        assertFalse("不许说成还在录", meetingOwnerCell(MeetingOwner.MINE_UPLOADING).contains("录音中"))
        assertEquals("这台手机真的在录", "录音中", meetingOwnerCell(MeetingOwner.MINE_RECORDING))
        assertEquals("没在录", "未在录音", meetingOwnerCell(MeetingOwner.NONE))
        assertEquals("电脑那场确实在录", "录音中", meetingOwnerCell(MeetingOwner.PC))
    }

    // ---------------------------------------------------------------- ⑤ 编排层：闸真的用同一套判据

    /**
     * **"别抢占"闸**：服务端正在收的那一场**就是我上一次启动留下的那场** → 不许等。
     *
     * 这一条是真机 bug 的另一半：旧实现在 App 重开之后（`activeClientId == null`）
     * 认不出自己 → 返回 `WAITING_MEETING_END` → 一段都不传、界面还写着"等电脑那场结束"，
     * 于是用户永远等不到文字。修复后这里必须一路走到 DONE。
     */
    @Test
    fun `闸认自己的历史会话（App 重开之后不许自己等自己）`() {
        val store = MeetingDraftStore(tmp.newFolder("offline-own"))
        seed(store, oldId)

        val t = StubTransport(
            ActiveMeeting(recording = true, meetingId = 90, source = "phone", clientId = oldId))
        // ⚠️ **activeClientId 故意传 null**：这就是"App 重开之后"的状态
        val r = runBlocking {
            OfflineMeetingSync.run(transport = t, store = store, activeClientId = null)
        }

        assertEquals("自己那场绝不许把自己挡住（否则补传永远不动）", OfflinePhase.DONE, r.phase)
        assertEquals("段要真的传上去", listOf(1), t.uploaded)
        assertEquals("传完要收尾（没有 stop 就没有转写）", 1, t.stops)
    }

    /** 反过来：**另一台手机**正在录 → 只等，一段都不许传（抢占会把别人的会打断）。 */
    @Test
    fun `另一台手机在录时仍然只等 一段都不传`() {
        val store = MeetingDraftStore(tmp.newFolder("offline-other"))
        seed(store, oldId)

        val t = StubTransport(
            ActiveMeeting(recording = true, meetingId = 91, source = "phone",
                clientId = "phone-20261020140000-ffff"))
        val r = runBlocking {
            OfflineMeetingSync.run(transport = t, store = store, activeClientId = null)
        }

        assertEquals(OfflinePhase.WAITING_MEETING_END, r.phase)
        assertTrue("这一段都不许传", t.uploaded.isEmpty())
        assertEquals("更不许收尾", 0, t.stops)
        assertTrue(r.note, r.note.contains("不抢占"))
    }

    // ---------------------------------------------------------------- ⑥ 源码级：判据只有一处

    private val ownershipSource = "src/main/java/com/echo/contact/service/MeetingOwnership.kt"
    private val meetingsTab = "src/main/java/com/echo/contact/ui/MeetingsTab.kt"
    private val syncSource = "src/main/java/com/echo/contact/service/OfflineMeetingSync.kt"

    /**
     * **归属判定只有一处实现**，显示与"别抢占"闸**共用**它。
     *
     * 为什么这条必须是源码级：Compose 在 JVM 门禁里渲染不了，而"两处各写一遍判据"
     * 恰恰是这次 bug 的形状 —— 闸一套、显示一套，同一个事实在界面上能给出相反的结论
     * （闸说"不是我的"于是拒绝续传，显示说"电脑在录"）。
     * 这类错跑任何一条纯函数用例都发现不了，只有读源码才钉得住。
     */
    @Test
    fun `归属判定只有一处且显示与别抢占闸共用它`() {
        val own = moduleText(ownershipSource)
        assertEquals("归属函数只许有一个实现",
            1, occurrencesOf(own, "fun meetingOwnership("))
        assertEquals("文案也只有一个出口",
            1, occurrencesOf(own, "fun meetingOwnerTrailing("))

        val meetings = moduleText(meetingsTab)
        assertTrue("会议页必须用那一处判据判归属", meetings.contains("meetingOwnership("))
        assertTrue("文案也必须来自那一处", meetings.contains("meetingOwnerTrailing("))
        assertTrue(meetings.contains("meetingOwnerNote("))

        val sync = moduleText(syncSource)
        assertTrue("闸必须用同一处判据（isMine）", sync.contains("meetingOwnership("))
        assertTrue("闸问的必须是 owner.isMine", sync.contains("owner.isMine"))
        assertFalse("旧的第二份实现（mineRecording）不许回来",
            sync.contains("fun mineRecording("))

        // 「电脑在录音」这句只许出现在那一处实现的文案里 ——
        // 两侧各写一遍正是"同一个事实两个说法"的病根。
        assertEquals("这句话只许在归属那一处定义",
            1, occurrencesOf(own, "\"电脑在录音\""))
        assertEquals("会议页不许自己再写一遍", 0, occurrencesOf(meetings, "\"电脑在录音\""))
        assertEquals("同步闸不许自己再写一遍", 0, occurrencesOf(sync, "\"电脑在录音\""))

        // 首页那张状态卡「会议」那一格同理：它原来写死 `active -> "录音中"`，
        // 于是"我这场的录音还在上传"在首页又变成"录音中"。
        val statusCard = moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt")
        assertTrue("首页那格必须用同一处判据", statusCard.contains("meetingOwnerCell("))
        assertEquals("短文案也只许有一处实现", 1, occurrencesOf(own, "fun meetingOwnerCell("))
        assertEquals("首页不许再自己写一遍「录音中」", 0, occurrencesOf(statusCard, "\"录音中\""))
    }

    /**
     * 会议页的状态词**不再是"`active` → 电脑在录音"**那种只看 `active` 的写法。
     *
     * 源码级：`trailing` 必须来自 `meetingOwnerTrailing(owner)`，而且
     * 显示用的 `owner` 必须把服务端的 `source`/`clientId` 与本地清单一起交进去 ——
     * 少了 `clientId` 就又会变成"认不出自己"。
     */
    @Test
    fun `会议页的归属判据带上了 source 与 clientId 和本地清单`() {
        val meetings = moduleText(meetingsTab)
        assertTrue("要读服务端的 source", meetings.contains("meeting?.source"))
        assertTrue("要读服务端的 clientId", meetings.contains("meeting?.clientId"))
        assertTrue("要拿本地清单里的 id 去比", meetings.contains("local = localDrafts"))
        assertTrue("trailing 只能来自那一处",
            meetings.contains("trailing = meetingOwnerTrailing(owner)"))
        // 本地清单的身份来自既有 store（不许另造一份"我的 id 列表"）
        val svc = moduleText("src/main/java/com/echo/contact/service/MeetingMicService.kt")
        assertTrue("清单身份必须来自既有 store.list()", svc.contains("store.list()"))
        assertTrue("要公开给界面（判归属用）", svc.contains("val localDrafts"))
    }

    // ---------------------------------------------------------------- 用例脚手架

    /** 在 store 里造一份"录完了、还有 1 段没传"的清单（音频文件**真的**写出来）。 */
    private fun seed(store: MeetingDraftStore, id: String): MeetingDraft {
        val dir = store.dirOf(id).apply { mkdirs() }
        File(dir, MeetingDraftStore.segmentName(1)).writeBytes(ByteArray(64) { 1 })
        val d = MeetingDraft(
            clientId = id,
            startedAt = "2026-10-20T09:46:24",
            endedAt = "2026-10-20T09:56:24",
            durationSeconds = 600.0,
            segments = listOf(
                DraftSegment(
                    seq = 1,
                    file = MeetingDraftStore.segmentName(1),
                    startedAt = "2026-10-20T09:46:24",
                    startOffsetSeconds = 0.0,
                    durationSeconds = 600.0,
                ),
            ),
        )
        assertTrue("用例的前置条件：清单要写得进去", store.save(d))
        return d
    }

    /**
     * 够用就好的假服务端：**只为**钉"闸会不会把自己挡住"。
     *
     * 不重复 `OfflineMeetingSyncTest.FakeTransport` 那一大套（那边管的是顺序/对账/退避）——
     * 这里只有一件事要证明：`activeMeeting()` 报的是**我自己的**那场时，
     * 编排层会不会继续传段并收尾。
     */
    private class StubTransport(private val active: ActiveMeeting) : OfflineTransport {
        val uploaded = mutableListOf<Int>()
        var stops = 0

        override suspend fun reachable(): ProbeResult = ProbeResult(200)
        override suspend fun activeMeeting(): ActiveMeeting = active
        override suspend fun startMeeting(req: StartRequest): StartOutcome = StartOutcome.Ok(90)

        override suspend fun uploadSegment(req: UploadRequest): SegmentOutcome {
            uploaded += req.seq
            return SegmentOutcome.Ok(serverMeetingId = 90, serverCount = uploaded.size)
        }

        override suspend fun segmentCount(clientId: String, meetingId: Int): CountOutcome =
            CountOutcome.Ok(uploaded.size)

        override suspend fun stopMeeting(req: StopRequest): StopOutcome {
            stops += 1
            return StopOutcome.Ok()
        }
    }
}
