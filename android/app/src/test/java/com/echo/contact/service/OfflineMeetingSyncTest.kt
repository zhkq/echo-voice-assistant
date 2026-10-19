package com.echo.contact.service

import com.echo.contact.data.ApiClient
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * **本地先写 + 机会性上传的编排**（`OfflineMeetingSync`）：顺序 / 续传 / 对账 / 退避触发 /
 * 不抢占 / 还在录不许收尾 / 上报暂停区间。
 *
 * 这一组用例用一个**假的 [OfflineTransport]**（下面 [FakeTransport]）把服务端的各种
 * 反应摆出来（连不上、忙、拒绝、少收一段、幂等命中…），于是那些"只能靠真机才能看出来"
 * 的口径在 JVM 里就能钉住 —— 而它们在真机上看不出来的原因正是：
 * **手机这边一切正常，错的是"谁先谁后"与"敢不敢说已上传"**。
 *
 * （"两条路合并成一条"那条主线在 `MeetingSinglePathTest` 里用**真 HTTP** 跑，
 * 这里管的是编排层的逐条判据。）
 */
class OfflineMeetingSyncTest {

    @get:Rule val tmp = TemporaryFolder()

    private val clientId = "phone-20261016203102-abcd"

    /**
     * **真的 HTTP**（仅给下面那两条"整条链"用例用）。
     *
     * 为什么单测里还要一条真线路：上面那个 [FakeTransport] 是我自己写的，
     * 它替我把"服务端那句拒绝"翻译成了 `StopOutcome.Failed` —— 于是"判混了就没人发现"
     * 这件事它证明不了。这一条走 `ApiClient` + `OfflineMeetingTransport`，
     * 让**服务端原话**一路流到编排层，钉的才是真机上的那条链：
     * 服务端拒绝 → 传输层分类 → 不许进 DONE、不许删缓存、原因如实。
     */
    private lateinit var server: MockWebServer

    @Before
    fun setUpServer() {
        server = MockWebServer()
        server.start()
        ApiClient.attachTokenProvider { null }
    }

    @After
    fun tearDownServer() {
        ApiClient.attachTokenProvider { null }
        server.shutdown()
    }

    private fun json(body: String) = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(body)

    // ---------------------------------------------------------------- 假的服务端

    /**
     * 一个够真的假服务端：
     *  * 记得每个 `clientId` 建过哪场会（**幂等**：同一个 id 只算一场）；
     *  * 记得每一场收了哪些 `seq`（**幂等**：重复的 seq 不重复存，只回 duplicate）；
     *  * 段数是**这一场的累计段数**（不是"这一轮收了几段"）—— 真服务端报的也是它，
     *    而对账比的就是这个数（见 `reconcile` 的说明）；
     *  * 能演"ECHO 正录着别的会""服务端拒绝""连不上""读不到状态"；
     *  * 能演"老服务端没有上报暂停区间的端点"（[reportPauses] 的默认实现就是那一档，
     *    这里显式记下来，供"上报不影响补传"的判据用）。
     */
    private class FakeTransport(
        var reachCode: Int? = 200,
        var active: ActiveMeeting = ActiveMeeting(recording = false),
        /** `ok=false` 的那句话（null = 正常收下）。 */
        var rejectStart: String? = null,
        var rejectSegmentAt: Int? = null,
        var rejectSegmentMessage: String = "服务端不收这一段",
        /** 段数里**故意**多加几段（演"服务端把同一段存了两遍"）。 */
        var countPenalty: Int = 0,
        /** 段响应里回不回段数（false = 演"老服务端什么都没说"）。 */
        var reportCount: Boolean = true,
        /** 只有这几段会被"真的存下来"（用来精确制造对账不符）。 */
        var dropSeqs: Set<Int> = emptySet(),
        /** 演"读不到会议状态"（**算不可达**，不敢往下走）。 */
        var statusUnreadable: Boolean = false,
        /** `POST /api/meeting/stop` 的结局：`ok=false` 的那句话（null = 正常收下）。 */
        var stopReject: String? = null,
        /** stop **没连上/超时**（`retryable = true` 那一档）。 */
        var stopFailure: SyncFailure? = null,
        /**
         * [stopFailure] 那一档要带的服务端原话（真传输层会把服务端 msg 照带；
         * 默认那句是"没连上"的风格，演"clientId 不符"时要换掉）。
         */
        var stopFailureMessage: String = "",
        /** stop 直接抛异常（演"收尾那个请求超时了"）。 */
        var stopThrows: Boolean = false,
        /** 演"这场早就停过了"（服务端的幂等回复）。 */
        var stopAlreadyStopped: Boolean = false,
        /** 演"老服务端没有上报暂停区间的端点"（`PauseOutcome.Unsupported`）。 */
        var pausesUnsupported: Boolean = false,
        /** 演"上报暂停区间时连不上/出错"。 */
        var pausesFailure: SyncFailure? = null,
    ) : OfflineTransport {

        val started = mutableListOf<StartRequest>()
        val uploaded = mutableListOf<Pair<String, Int>>()

        /** 上报过的暂停区间（每一轮一条 —— "整份重报"就该在这里看到重复的区间）。 */
        val pauseReports = mutableListOf<PauseRequest>()

        /**
         * **调用序列**（修①的核心判据：stop 必须在最后、且只有一次）。
         *
         * 为什么要有它，而不是只数 stop 的次数：次数对不代表顺序对 ——
         * "先 stop 再传段"会让服务端拿一份缺段的音频去转写，而用例若只数次数**全绿**。
         */
        val calls = mutableListOf<String>()

        val stops = mutableListOf<StopRequest>()

        /** `clientId` → 这一场服务端手上的段（`LinkedHashSet`：**重复的 seq 不重复存**）。 */
        val stored = LinkedHashMap<String, MutableSet<Int>>()
        var startCalls = 0

        override suspend fun reachable(): ProbeResult? = ProbeResult(reachCode)

        override suspend fun activeMeeting(): ActiveMeeting? =
            if (statusUnreadable) null else active

        override suspend fun reportPauses(req: PauseRequest): PauseOutcome {
            calls += "pauses:${req.pauses.size}"
            pauseReports += req
            pausesFailure?.let {
                return PauseOutcome.Failed(it, "暂停区间没报上去（${it.name}）：不影响音频补传")
            }
            if (pausesUnsupported) {
                return PauseOutcome.Unsupported("这台服务端没有上报暂停区间的接口：这次没报上去")
            }
            return PauseOutcome.Ok(req.pauses.size)
        }

        override suspend fun startMeeting(req: StartRequest): StartOutcome {
            startCalls++
            started += req
            calls += "start"
            rejectStart?.let { return StartOutcome.Failed(it, retryable = false) }
            // 幂等：同一个 clientId 只算一场会（id 固定 42）。
            // 续传时清单里已经有 `acknowledgedSegments` 段是**之前**传上去的，
            // 服务端手上当然也留着它们 —— 补进来之后，"段数"才是这一场的累计数
            // （真服务端的语义就是这个；少了这一步，续传用例会假红）。
            val set = stored.getOrPut(req.clientId) { linkedSetOf() }
            for (s in 1..req.acknowledgedSegments) set += s
            return StartOutcome.Ok(42)
        }

        override suspend fun uploadSegment(req: UploadRequest): SegmentOutcome {
            calls += "segment:${req.seq}"
            // 先判"拒"再记账：被拒的那一段**没有**进服务端（否则假服务端会替真服务端
            // 把一段不存在的音频算进去，用例就测不出"停在失败处"这件事）
            if (rejectSegmentAt == req.seq) {
                return SegmentOutcome.Failed(SyncFailure.REJECTED, rejectSegmentMessage,
                    retryable = false)
            }
            uploaded += req.clientId to req.seq
            val set = stored.getOrPut(req.clientId) { linkedSetOf() }
            val dup = set.contains(req.seq)
            if (req.seq !in dropSeqs) set += req.seq
            val count = (set.size + countPenalty).takeIf { reportCount }
            return SegmentOutcome.Ok(
                serverMeetingId = 42, serverCount = count, duplicate = dup)
        }

        override suspend fun segmentCount(clientId: String, meetingId: Int): CountOutcome {
            return if (!reportCount) {
                // 演"老服务端"/"读不到这一场的段数"：**不许**被当成"本地数了数 = 对账通过"
                CountOutcome.Failed(SyncFailure.SERVER_ERROR, "服务端没回段数")
            } else {
                CountOutcome.Ok(stored[clientId]?.size ?: 0)
            }
        }

        override suspend fun stopMeeting(req: StopRequest): StopOutcome {
            calls += "stop"
            stops += req
            if (stopThrows) throw java.io.IOException("连接超时")
            if (stopAlreadyStopped) {
                // 真服务端的原话（`app/meeting.py::stop_meeting` 第一行）
                return StopOutcome.Ok(alreadyStopped = true, note = "没有进行中的会议")
            }
            stopFailure?.let {
                return StopOutcome.Failed(it, stopFailureMessage.ifBlank { "收尾没成（${it.name}）" },
                    retryable = failureRetryable(it), alreadyUploadedHint = true)
            }
            stopReject?.let {
                return StopOutcome.Failed(SyncFailure.REJECTED, it, retryable = false,
                    alreadyUploadedHint = true)
            }
            return StopOutcome.Ok()
        }
    }

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    /**
     * 把"对账那一刻"记进假 transport 的调用序列（**同一个 list、同一个时间顺序**）。
     *
     * 为什么需要它：修① 的判据是"stop 在最后**且在对账之后**"，而对账不一定是一次网络调用
     * （段响应里带了段数时就不会再问一次）—— 只看 transport 记的那些，"先 stop 再传段"
     * 同样能让"stop 是最后一次调用"成立。所以由 `reconcileAt` 把那一刻**原地**追加进
     * 同一个序列，得到的才是真正的时间线（不需要事后猜位置）。
     */
    private fun markReconcile(t: FakeTransport): (MeetingDraft) -> Unit = { t.calls += "reconcile" }

    /** 在 store 里造一份"录完了、还有 N 段没传"的清单（音频文件**真的**写出来）。 */
    private fun seed(
        store: MeetingDraftStore,
        id: String = clientId,
        startedAt: String = "2026-10-16T20:31:02",
        seqs: List<Int> = listOf(1, 2, 3),
        acked: Int = 0,
        uploaded: Boolean = false,
        /** 还在录（没写结束时间）—— 那种清单**只许传段、绝不许收尾**。 */
        recording: Boolean = false,
        pauses: List<DraftPause> = emptyList(),
        format: com.echo.contact.audio.SegmentFormat = com.echo.contact.audio.SegmentFormat.WAV,
    ): MeetingDraft {
        val dir = store.dirOf(id).apply { mkdirs() }
        seqs.forEach {
            File(dir, MeetingDraftStore.segmentName(it, format)).writeBytes(ByteArray(64) { 1 })
        }
        val d = MeetingDraft(
            clientId = id,
            startedAt = startedAt,
            endedAt = if (recording) "" else "2026-10-16T20:33:02",
            durationSeconds = 90.0,
            pauses = pauses,
            segments = seqs.map {
                DraftSegment(seq = it, file = MeetingDraftStore.segmentName(it, format),
                    startedAt = startedAt, startOffsetSeconds = (it - 1) * 30.0,
                    durationSeconds = 30.0, format = format)
            },
            acknowledgedSegments = acked,
            uploaded = uploaded,
        )
        assertTrue("用例的前置条件：清单要写得进去", store.save(d))
        return d
    }

    private fun run(
        t: OfflineTransport,
        store: MeetingDraftStore,
        activeClientId: String? = null,
        manual: Boolean = false,
        scope: SyncScope = SyncScope.FINALIZE,
        reconcileAt: ((MeetingDraft) -> Unit)? = null,
    ): OfflineSyncResult = runBlocking {
        OfflineMeetingSync.run(
            transport = t, store = store, activeClientId = activeClientId,
            manual = manual, scope = scope, reconcileAt = reconcileAt)
    }

    // ---------------------------------------------------------------- ① 顺序 + 幂等建会

    @Test
    fun `按序上传 并且只用同一个客户端 id 建一场会`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals("必须按 1,2,3 的顺序传（音频的顺序就是内容）",
            listOf(1, 2, 3), t.uploaded.map { it.second })
        assertEquals("建会只该发生一次", 1, t.startCalls)
        assertEquals("幂等键必须是客户端生成的那一个", clientId, t.started[0].clientId)
        assertEquals("开始时间要用**缓存里**的（不是补传那一刻）",
            "2026-10-16T20:31:02", t.started[0].startedAt)
        assertEquals("来源要说清是手机麦", MeetingSource.PHONE, t.started[0].source)
    }

    @Test
    fun `反复调用只发同一个 id（客户端侧只发同一个 id）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport()
        run(t, store)
        // 第二、三轮：清单已经对账完成，pending() 里没有它了 —— 这正是"不会又建一场"
        seed(store, seqs = listOf(1), id = "other-1")
        run(t, store)
        assertTrue("所有建会请求里的 clientId 只能来自清单（客户端绝不自己造第二个）",
            t.started.all { it.clientId == clientId || it.clientId == "other-1" })
        assertEquals(2, t.startCalls)
        assertEquals("同一个 clientId 的两轮**不会**建出两场（假服务端按它幂等）",
            1, t.stored.keys.count { it == clientId })
        // 每一轮都必须收尾一次（收了尾，服务端才转写、才把那条唯一的录音槽放掉）
        assertEquals("两轮 = 两次收尾", 2, t.stops.size)
    }

    /**
     * 标记完成的清单**不会**被再传一遍 —— 于是也不会再收尾一次。
     *
     * ⚠️ **已知边界（如实写在这里，不美化）**：`uploaded=true` 在旧版本里表示
     * "对账通过"（那时还没有 stop 这一步）。升级上来的清单如果是那一档，
     * 它会因为 `pending()`/`needsSync` 判它"没有欠的段"而**永远不会被再走一遍**，
     * 于是那一场会**永远不会被收尾**（= 永远不会出文字，还占着服务端的录音槽）。
     * 这一条用例钉的是现在的行为（不重复传），**不是**在说那个边界已经解决 ——
     * 要真解决得让 `uploaded` 带上"收尾也过了"的语义（例如新增一个 `stopped_at` 字段），
     * 那属于清单格式的变更，超出本次修复的范围（已如实报给 Lead）。
     */
    @Test
    fun `已经标记完成的清单不会再传（也就不会再收尾）—— 已知边界`() {
        val store = store()
        seed(store, seqs = listOf(1, 2), acked = 2, uploaded = true)
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.IDLE, r.phase)
        assertTrue("不再重传任何一段", t.uploaded.isEmpty())
        assertEquals("也不会再收尾一次（stop 只跟着补传走）", 0, t.stops.size)
    }

    // ---------------------------------------------------------------- ② 续传

    @Test
    fun `续传：前 k 段已确认时只传剩下的 且顺序正确`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3, 4, 5), acked = 3)
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals("已经确认的 1..3 段不许再传（那会在电脑上存成两遍）",
            listOf(4, 5), t.uploaded.map { it.second })
        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue(store.load(clientId)!!.uploaded)
    }

    @Test
    fun `传了一半断了 水位停在最后成功的那一段`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        // 第 3 段被拒（4xx：重试也不会好）→ 这一轮停在第 2 段之后
        val t = FakeTransport(rejectSegmentAt = 3)

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertEquals(listOf(1, 2), t.uploaded.map { it.second })
        val d = store.load(clientId)!!
        assertEquals("水位要落在第 2 段（下次从第 3 段续）", 2, d.acknowledgedSegments)
        assertFalse("**没有对账通过 → 不许标已上传**", d.uploaded)
        assertTrue("音频一个都不许删", File(store.segmentFile(clientId, 3).path).isFile)
        assertTrue("原因要如实转达服务端那句原话（别换成一句笼统的'同步失败'）：${r.note}",
            r.note.contains("服务端不收这一段"))
    }

    @Test
    fun `本地缺了第 k 段时停在它前面 不许跳过去传后面的`() {
        val store = store()
        val d = seed(store, seqs = listOf(1, 2, 3))
        assertTrue(store.segmentFile(clientId, 2).delete())
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals("只传了第 1 段", listOf(1), t.uploaded.map { it.second })
        assertEquals(OfflinePhase.FAILED, r.phase)
        assertTrue("要说清为什么停：${r.note}", r.note.contains("第 2 段"))
        assertEquals(1, store.load(clientId)!!.acknowledgedSegments)
        assertNotNull(d)
    }

    @Test
    fun `本地只留了后面几段的清单也能对账通过（水位在前）`() {
        // ⚠️ 合并成一条路之后，"前几段只在电脑上"这种清单**不再由这条路产生**
        // （每一段都先写本地）。但它在升级上来的老清单里真实存在过，而且这条判据本身
        // 仍然重要：**对账比的是"本地最大段序号"，不是"本地有几段"** ——
        // 按"有几段"去比会把每一份续传清单都判成"不符"，于是那些清单**永远传不完**
        // （用户什么都没做错，却一直看到"还有 N 段没传上去"）。
        val store = store()
        val d = seed(store, seqs = listOf(7, 8))
        store.save(d.copy(acknowledgedSegments = 6))
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals("只传本地真有的那两段", listOf(7, 8), t.uploaded.map { it.second })
        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue(store.load(clientId)!!.uploaded)
    }

    // ---------------------------------------------------------------- ③ 对账

    @Test
    fun `对账不符时不删缓存 不标已上传 且如实报`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        // 服务端"少收"了第 3 段（count 只到 2）
        val t = FakeTransport(dropSeqs = setOf(3))

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertTrue("原因要说出段数不符：${r.note}", r.note.contains("只认下 2 段"))
        val d = store.load(clientId)!!
        assertFalse("**对账不过就不许说已上传**", d.uploaded)
        assertTrue("3 段音频全部留在手机里", (1..3).all { store.segmentFile(clientId, it).isFile })
    }

    @Test
    fun `服务端没回段数时不假装对账通过`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport(reportCount = false)

        val r = run(t, store)

        assertEquals("拿不到确切数字 → 不许标已上传，也不许说'传完了'",
            OfflinePhase.FAILED, r.phase)
        assertFalse(store.load(clientId)!!.uploaded)
        assertTrue(r.note, r.note.contains("没回段数"))
    }

    @Test
    fun `段数一致（服务端回段数）时才算完成`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals(2, r.confirmedSegments)
        assertEquals(0, r.pendingSegments)
        assertTrue(store.load(clientId)!!.uploaded)
        assertTrue("确认之后音频仍然留着（默认策略：确认后再删，而这一版连删都没做）",
            (1..2).all { store.segmentFile(clientId, it).isFile })
    }

    @Test
    fun `服务端多出段来（幂等可能没做好）也不删缓存`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport(countPenalty = 1)   // 服务端报 3 段

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertTrue(r.note, r.note.contains("两遍"))
        assertFalse(store.load(clientId)!!.uploaded)
    }

    // ---------------------------------------------------------------- ④ 不抢占 / 不可达

    @Test
    fun `ECHO 正在录另一场时只等 不传任何一段`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport(active = ActiveMeeting(recording = true, meetingId = 7, note = "2026-10-16_20-00"))

        val r = run(t, store)

        assertEquals(OfflinePhase.WAITING_MEETING_END, r.phase)
        assertTrue("一段都不许传（抢占会把电脑正在录的会打断）", t.uploaded.isEmpty())
        assertEquals("连建会都不该做（那会在别人录着的时候开新会）", 0, t.startCalls)
        assertTrue(r.note, r.note.contains("不抢占"))
        assertFalse(store.load(clientId)!!.uploaded)
    }

    @Test
    fun `ECHO 在录的就是我们这一份清单时不算抢占`() {
        val store = store()
        seed(store, seqs = listOf(1), acked = 0)
        // 服务端回了 clientId：这才是"我们自己那一场"的最强判据
        val t = FakeTransport(active = ActiveMeeting(recording = true, meetingId = 42, clientId = clientId))

        val r = run(t, store, activeClientId = clientId)

        assertEquals("自己那一场不该把自己挡住（否则会自己等自己）", OfflinePhase.DONE, r.phase)
        assertEquals(listOf(1), t.uploaded.map { it.second })
    }

    @Test
    fun `自己那一场没有 clientId 时按会议 id 判`() {
        val store = store()
        val d = seed(store, seqs = listOf(1))
        store.save(d.copy(serverMeetingId = 42))
        val t = FakeTransport(active = ActiveMeeting(recording = true, meetingId = 42))

        val r = run(t, store, activeClientId = clientId)

        assertEquals(OfflinePhase.DONE, r.phase)
    }

    @Test
    fun `认不出是不是自己那一场时宁可判不是（最多多等一会儿 不会打断别人的会）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        // 服务端没回 clientId，清单里也没记下会议 id → 认不出
        val t = FakeTransport(active = ActiveMeeting(recording = true, meetingId = 99))

        val r = run(t, store, activeClientId = clientId)

        assertEquals(OfflinePhase.WAITING_MEETING_END, r.phase)
        assertTrue(t.uploaded.isEmpty())
    }

    @Test
    fun `连不上时立刻停下 并把真实原因带回去`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport(reachCode = null)

        val r = run(t, store)

        assertEquals(OfflinePhase.UNREACHABLE, r.phase)
        assertTrue(t.uploaded.isEmpty())
        assertEquals("连探活都没过就不该去建会", 0, t.startCalls)
        assertEquals(2, r.pendingSegments)
    }

    @Test
    fun `读不到会议状态也算不可达（不敢在不知道电脑在干什么时灌段）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport(statusUnreadable = true)

        val r = run(t, store)

        assertEquals(OfflinePhase.UNREACHABLE, r.phase)
        assertTrue(t.uploaded.isEmpty())
    }

    @Test
    fun `服务端拒绝建会时不重试（4xx 重试也不会好）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport(rejectStart = "这台后端不认识 clientId")

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertTrue("要点出是什么被拒了：${r.note}", r.note.contains("clientId"))
        assertTrue(t.uploaded.isEmpty())
        assertFalse(store.load(clientId)!!.uploaded)
    }

    // ---------------------------------------------------------------- ⑤ 多份清单

    @Test
    fun `几场离线录音按开始时间顺序传`() {
        val store = store()
        seed(store, id = "later", startedAt = "2026-10-16T11:00:00", seqs = listOf(1))
        seed(store, id = "earlier", startedAt = "2026-10-16T09:00:00", seqs = listOf(1))
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals("先录的那场要先传（会议顺序）",
            listOf("earlier", "later"), t.uploaded.map { it.first })
        assertEquals("两场各自的会（各自的幂等键）", 2, t.startCalls)
        assertEquals(2, r.drafts)
    }

    @Test
    fun `一份停下时不再动后面的（先给用户第一个真实原因）`() {
        val store = store()
        seed(store, id = "a-earlier", startedAt = "2026-10-16T09:00:00", seqs = listOf(1, 2))
        seed(store, id = "b-later", startedAt = "2026-10-16T10:00:00", seqs = listOf(1))
        val t = FakeTransport(dropSeqs = setOf(2))    // a 的第 2 段对账不过

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertTrue("后面的那场**不该**被动（可能同一个毛病，而用户要看第一个真原因）",
            t.uploaded.none { it.first == "b-later" })
    }

    // ---------------------------------------------------------------- ⑦ 收尾 stop（修①）

    /**
     * 修①（2026-10-16）：补传的**最后一步**必须是 `POST /api/meeting/stop`。
     *
     * 根因（写在用例名里，免得以后有人"顺手简化掉"这一步）：
     * **没有 stop 就没有转写** —— 服务端只在 `stop_meeting()` 里收尾这场会并发起转写；
     * 只做 start + segment 的话，音频躺在电脑上**永远不会出文字**（用户要的就是"出文字"）；
     * 而且"手机麦"这场会会一直占着服务端**唯一**那条录音槽，之后谁都开不了新会议。
     */

    @Test
    fun `成功路径的调用序列是 start 到 segment 到 对账 到 stop（stop 在最后且只一次）`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = FakeTransport()

        val r = run(t, store, reconcileAt = markReconcile(t))

        assertEquals("没有 stop 就没有转写：这一场不许以'完成'收场（${t.calls}）",
            listOf("start", "segment:1", "segment:2", "segment:3", "reconcile", "stop"), t.calls)
        assertEquals("stop 只许调一次（多调一次就是多收尾一次）", 1, t.stops.size)
        assertEquals("stop 收到的是这一场的 id（只用于日志/文案）",
            clientId, t.stops[0].clientId)
        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue("界面上要能说'已补传并开始转写'", r.stopped)
        assertTrue(store.load(clientId)!!.uploaded)
    }

    @Test
    fun `对账不通过时不调 stop 也不进 DONE`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        // 服务端"少收"了第 3 段 → 对账不符（段还没到齐就收尾，等于让服务端拿缺段的音频去转写）
        val t = FakeTransport(dropSeqs = setOf(3))

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertTrue("对账没过就**不许**收尾：${t.calls}", t.calls.none { it == "stop" })
        assertEquals("一次 stop 都不该发生", 0, t.stops.size)
        assertFalse("没收尾 → 不许标已上传", store.load(clientId)!!.uploaded)
        assertFalse("没收尾 → 不许说'开始转写'", r.stopped)
        assertTrue("音频一个都不许删", (1..3).all { store.segmentFile(clientId, it).isFile })
    }

    @Test
    fun `stop 失败时状态不是 DONE 缓存不删 而且有可重试标记`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        // 收尾那一下**没连上/超时**（可重试的那一档：网络还会恢复）——
        // 不能拿 4xx 来测"可重试"，那是"重试也不会好"的另一档（见下一条用例）
        val t = FakeTransport(stopFailure = SyncFailure.UNREACHABLE)

        val r = run(t, store)

        assertNotEquals("stop 没成就不许说完成", OfflinePhase.DONE, r.phase)
        assertEquals("可重试的 stop 失败留在待办里等退避重试",
            OfflinePhase.WAITING_UPLOAD, r.phase)
        assertFalse("不许说'开始转写'", r.stopped)
        val d = store.load(clientId)!!
        assertFalse("**没收尾就不许标已上传**（标了就等于说已经出文字了）", d.uploaded)
        assertTrue("缓存一个字节都不许删", (1..2).all { store.segmentFile(clientId, it).isFile })
        assertTrue("下次还会再试（清单仍在待办里）", needsSync(d))
        assertTrue("原因要如实说清是收尾（stop）没成、还没开始转写：${r.note}",
            r.note.contains("stop") && r.note.contains("还没有开始转写"))
        assertTrue("要带上服务端那句原话：${r.note}", r.note.contains("收尾没成"))
    }

    @Test
    fun `stop 被服务端拒绝（4xx）时是可重试标记为假的那一档`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        // 4xx 是"重试也不会好"：这时**如实说成 FAILED**比无限退避更诚实
        // （两种都不是 DONE —— 铁律是"没收尾就不许说完成"，档位只是说清该不该再试）
        val t = FakeTransport(stopFailure = SyncFailure.REJECTED)

        val r = run(t, store)

        assertEquals(OfflinePhase.FAILED, r.phase)
        assertFalse(store.load(clientId)!!.uploaded)
        assertTrue("缓存一个字节都不许删", (1..2).all { store.segmentFile(clientId, it).isFile })
        assertTrue("原因要如实说：${r.note}", r.note.contains("还没有开始转写"))
    }

    @Test
    fun `stop 超时（抛异常）也留在可重试的那一档`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport(stopThrows = true)

        val r = run(t, store)

        assertEquals(OfflinePhase.WAITING_UPLOAD, r.phase)
        assertFalse(store.load(clientId)!!.uploaded)
        assertTrue("音频留着", store.segmentFile(clientId, 1).isFile)
        assertTrue("下次会重试", needsSync(store.load(clientId)!!))
    }

    @Test
    fun `stop 返回没有进行中的会议时当成成功（幂等 不许卡死）`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        // 这场之前已经停过了：服务端回 `{ok:false,message:"没有进行中的会议"}`
        // （`app/meeting.py::stop_meeting` 的第一行）—— 那不是失败，是"已经停过了"
        val t = FakeTransport(stopAlreadyStopped = true)

        val r = run(t, store)

        assertEquals("幂等命中**算成功**：卡在最后一步是最糟的（用户看到'差一点'却永远不动）",
            OfflinePhase.DONE, r.phase)
        assertTrue("已经停过 = 收尾已完成（转写那次就发起了）", r.stopped)
        assertTrue(store.load(clientId)!!.uploaded)
    }

    @Test
    fun `stop 的判定是纯函数 只认服务端那句'没有进行中的会议'`() {
        // 为什么单独钉这一条：这一句是**幂等**的全部依据，判宽了会把
        // "服务端真拒绝了"当成"已经好了"（那就把没转写说成了已转写）
        assertTrue(looksAlreadyStopped("没有进行中的会议"))
        assertTrue(looksAlreadyStopped("  没有进行中的会议  "))
        assertFalse("空串不是（服务端什么都没说 → 不许当成成功）", looksAlreadyStopped(""))
        assertFalse(looksAlreadyStopped("参数不对"))
        assertFalse(looksAlreadyStopped("这台后端不认识 clientId"))
    }

    /**
     * 服务端护栏那句（`app/meeting.py::stop_meeting` 里 clientId 不符那一档）：**逐字**抄的。
     *
     * 它与"没有进行中的会议"都在 `ok=false` 这一档上，但两句话在客户端眼里必须分得开：
     *  * "没有进行中的会议" = 这场之前就停过了 = **成功**（幂等）；
     *  * "这场会议不是 clientId=… 那一场" = 服务端在录**别人**的会、**没有**收尾我们这一场
     *    = **失败**（可重试），**不许**标 DONE、**不许**删缓存。
     * 判混的后果是这一整轮修复里最坏的一种：一场根本没传完的录音被标成"已完成、已开始转写"。
     */
    private val clientIdMismatch = "这场会议不是 clientId=$clientId 那一场" +
        "（服务端正在录别的会），已拒绝收尾：不会停掉别人的会议"

    @Test
    fun `stop 的拒绝话术与幂等话术必须分得开（纯函数）`() {
        assertTrue("服务端护栏那句要认得出来：$clientIdMismatch",
            looksClientIdMismatch(clientIdMismatch))
        assertFalse("它**绝不**能被当成'这场早就停过了'（那就把没收尾说成了已转写）",
            looksAlreadyStopped(clientIdMismatch))
        assertFalse("反过来也一样：幂等那句不是'clientId 不符'",
            looksClientIdMismatch("没有进行中的会议"))
        assertFalse("空串不是（服务端什么都没说 → 不许当成功、也不许编一个原因）",
            looksClientIdMismatch(""))
        assertFalse("别的拒绝不许被认成这一档（原因要说准，不许套模板）",
            looksClientIdMismatch("这台后端不认识 clientId"))
    }

    /**
     * 服务端因 `clientId` 不符拒绝收尾 → 这一档必须是**可重试的失败**：
     * 不是 DONE、不删缓存、原因如实带上服务端原话。
     *
     * 为什么值得单独钉：这一档的错法**不会报错**——把拒绝当成成功之后，界面上写的是
     * "离线录音已补传并开始转写"，而真相是那一场还占着服务端唯一那条录音槽、一个字都还没转。
     *
     * ⚠️ 这里用 [FakeTransport.stopFailure]（而不是 `stopReject`）是**故意的**：
     * 真 `OfflineMeetingTransport` 把这一档归成 `SERVER_BUSY` + **可重试**
     * （服务端只是在录别的会，等它录完就能收尾）—— 见 `OfflineMeetingWireTest` 与
     * `OfflineMeetingSyncTest` 里"整条链（真 HTTP）"那条。用 `stopReject`（`retryable=false`）
     * 会演成另一档（`FAILED`），那就测不到"可重试"这件事了。
     */
    @Test
    fun `stop 被服务端按 clientId 拒绝时不是 DONE 并可重试且缓存不删`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        // 演真服务端的护栏分支：`ok=false` + 那句"不是 clientId 那一场"，
        // 而传输层给它的分类是"服务端正忙（在录别的会）"+ 可重试。
        val t = FakeTransport(
            stopFailure = SyncFailure.SERVER_BUSY,
            stopFailureMessage = clientIdMismatch,
        )

        val r = run(t, store)

        assertNotEquals("**绝不许**算成功：$r", OfflinePhase.DONE, r.phase)
        assertEquals("服务端只是在录别的会，等它录完就能收尾 → 归可重试那一档",
            OfflinePhase.WAITING_UPLOAD, r.phase)
        assertFalse("没收尾就不许说'开始转写'", r.stopped)
        val d = store.load(clientId)!!
        assertFalse("不许标已上传（标了就等于说已经出文字了）", d.uploaded)
        assertTrue("缓存一个字节都不许删（这一场还没传完/没收尾）",
            (1..2).all { store.segmentFile(clientId, it).isFile })
        assertTrue("下次还会再试（留着待办里）", needsSync(d))
        assertTrue("原因要如实带上服务端原话：${r.note}", r.note.contains("不是 clientId=$clientId"))
        assertTrue("要点明**还没有开始转写**：${r.note}", r.note.contains("还没有开始转写"))
    }

    @Test
    fun `stop 时把这一场自己的 clientId 发出去（护栏才可能在真机生效）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport()

        run(t, store)

        assertEquals("收尾请求里必须是**这一份清单自己的** id（护栏的判据）",
            clientId, t.stops.single().clientId)
    }

    @Test
    fun `几场各带各自的 clientId 收尾（不许拿别人的 id）`() {
        val store = store()
        seed(store, id = "earlier", startedAt = "2026-10-16T09:00:00", seqs = listOf(1))
        seed(store, id = "later", startedAt = "2026-10-16T11:00:00", seqs = listOf(1))
        val t = FakeTransport()

        run(t, store)

        assertEquals("每一场收尾带的都是它自己的 id（拿别人的 id = 绕过护栏）",
            listOf("earlier", "later"), t.stops.map { it.clientId })
    }

    // ---------------------------------------------------------------- ⑨ 整条链（真 HTTP）

    /**
     * **整条链**走一遍真 HTTP：服务端因 `clientId` 不符拒绝收尾时，
     * 编排层必须停在"可重试的失败"，**不是** DONE。
     *
     * 为什么这条比假 transport 那条更强：假 transport 里"服务端那句拒绝"是**我**翻译成
     * `StopOutcome.Failed` 的，它证明不了传输层会把它翻对。这一条让**服务端原话**
     * （逐字抄 `app/meeting.py::stop_meeting` 的护栏分支）真的走一遍
     * `EchoApi` → `OfflineMeetingTransport` → `OfflineMeetingSync`。
     */
    @Test
    fun `整条链：服务端按 clientId 拒绝收尾时停在失败档（真 HTTP）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        server.enqueue(MockResponse().setResponseCode(200))                           // GET /health 探活
        server.enqueue(json("""{"active":false}"""))                                  // 会议状态（没人在录）
        server.enqueue(json("""{"ok":true,"meetingId":42}"""))                        // start
        server.enqueue(json("""{"ok":true,"meetingId":42,"segmentCount":1}"""))       // segment 1
        server.enqueue(json("""{"ok":false,"message":"这场会议不是 clientId=$clientId 那一场""" +
            """（服务端正在录别的会），已拒绝收尾：不会停掉别人的会议"}"""))            // stop
        val r = run(OfflineMeetingTransport(ApiClient.create(server.url("/").toString())), store)

        assertEquals("服务端拒了收尾 → **不是** DONE：$r", OfflinePhase.WAITING_UPLOAD, r.phase)
        assertFalse("不许说'开始转写'", r.stopped)
        val d = store.load(clientId)!!
        assertFalse("不许标已上传（那等于说已经出文字了）", d.uploaded)
        assertTrue("缓存不删", store.segmentFile(clientId, 1).isFile)
        assertTrue("原因要如实带上服务端原话：${r.note}", r.note.contains("不是 clientId=$clientId"))
        // 五发请求的路径与顺序也顺带钉住（那条拒绝必须是**最后**那一下，不是中间）
        val paths = (1..5).map { server.takeRequest().path!!.substringBefore('?') }
        assertEquals(listOf("/health", "/api/meeting/status", "/api/meeting/start",
            "/api/meeting/segment", "/api/meeting/stop"), paths)
    }

    /**
     * 反面对照：同一条链上"没有进行中的会议"**仍然**算成功（幂等不许被这次改动弄坏）。
     *
     * 两条用例必须**成对**存在：只钉"拒绝不算成功"很容易顺手把幂等也判死，
     * 而幂等判死的表现是那些已经停过的清单**永远卡在最后一步**（用户看到"差一点"却永远不动）。
     */
    @Test
    fun `整条链：没有进行中的会议仍然算成功（真 HTTP）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        server.enqueue(MockResponse().setResponseCode(200))                           // /health
        server.enqueue(json("""{"active":false}"""))                                  // 会议状态
        server.enqueue(json("""{"ok":true,"meetingId":42}"""))                        // start
        server.enqueue(json("""{"ok":true,"meetingId":42,"segmentCount":1}"""))       // segment 1
        server.enqueue(json("""{"ok":false,"message":"没有进行中的会议"}"""))           // stop

        val r = run(OfflineMeetingTransport(ApiClient.create(server.url("/").toString())), store)

        assertEquals("幂等命中**仍然**算成功：$r", OfflinePhase.DONE, r.phase)
        assertTrue("已经停过 = 收尾已完成（转写那次就发起了）", r.stopped)
        assertTrue(store.load(clientId)!!.uploaded)
    }

    @Test
    fun `续传场景（前几段已传）仍然走完 对账到stop`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3, 4, 5), acked = 3)
        val t = FakeTransport()

        val r = run(t, store, reconcileAt = markReconcile(t))

        assertEquals("已确认的段不重传", listOf(4, 5), t.uploaded.map { it.second })
        assertEquals("续传也必须走完最后那一步（否则这一场永远不出文字）",
            listOf("start", "segment:4", "segment:5", "reconcile", "stop"), t.calls)
        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue(r.stopped)
    }

    @Test
    fun `几场离线录音每场各收尾一次`() {
        val store = store()
        seed(store, id = "earlier", startedAt = "2026-10-16T09:00:00", seqs = listOf(1))
        seed(store, id = "later", startedAt = "2026-10-16T11:00:00", seqs = listOf(1))
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals("每一场都要收尾（漏一场 = 那一场永远不出文字，还占着录音槽）",
            listOf("earlier", "later"), t.stops.map { it.clientId })
        assertTrue(r.stopped)
    }

    // ---------------------------------------------------------------- ⑩ 合并成一条路（2026-10-17）

    /**
     * **还在录的那一份：只传段，绝不收尾**（合并两条路之后最容易写错的地方）。
     *
     * 为什么这条比什么都重要：`stop` 一到，服务端就落盘、写 meta、**发起转写**，
     * 而麦克风还在录 —— 之后的段再也传不上去（服务端那份已经收尾，本地这份还会被标成
     * "已完成"）。所以判据必须长在 `syncOne` 里，而不是靠调用方记得别传 FINALIZE：
     * 后台 tick（`tickFromForeground`）用的就是 FINALIZE，它完全可能在用户还在录时被调到。
     */
    @Test
    fun `还在录的那一份只传段 一次 stop 都不许有`() {
        val store = store()
        seed(store, seqs = listOf(1, 2), recording = true)
        val t = FakeTransport()

        val r = run(t, store)                       // 默认就是 FINALIZE（最"危险"的那一档）

        assertEquals("两段都传上去了（边录边传）", listOf(1, 2), t.uploaded.map { it.second })
        assertTrue("**一次 stop 都没有**：${t.calls}", t.calls.none { it == "stop" })
        assertEquals(0, t.stops.size)
        assertEquals("连对账都不做（段数当然还没到齐，那条'不符'是假的）",
            listOf("start", "segment:1", "segment:2"), t.calls)
        assertFalse("没收尾 → 不许标已上传", store.load(clientId)!!.uploaded)
        assertTrue("还在录", store.load(clientId)!!.recording)
        assertFalse("也不许说'开始转写'", r.stopped)
        assertTrue("原因要说清是'还在录、收尾等结束'：${r.note}",
            r.note.contains("还在录") && r.note.contains("按结束"))
    }

    /** [SyncScope.INCREMENTAL]：录完了的清单这一轮也只传段、不收尾（"顺手传一下"就是这个口径）。 */
    @Test
    fun `INCREMENTAL 只传段不收尾（录完了也一样）`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport()

        val r = run(t, store, scope = SyncScope.INCREMENTAL)

        assertEquals(listOf(1, 2), t.uploaded.map { it.second })
        assertEquals("机会性上传**永远不 stop**：${t.calls}",
            listOf("start", "segment:1", "segment:2"), t.calls)
        assertFalse(store.load(clientId)!!.uploaded)
        assertFalse(r.stopped)
        assertEquals("段都上去了，但还没收尾 —— 这不是 DONE", OfflinePhase.WAITING_UPLOAD, r.phase)
    }

    /** 同一个 `clientId` 自己那一场**不再挡自己**（合并成一条路之后必须这样）。 */
    @Test
    fun `自己那一场正在录时也不算抢占（合并成一条路之后）`() {
        val store = store()
        seed(store, seqs = listOf(1), recording = true)
        // 服务端回的 clientId 就是我们这一份 → 那"正在录"的就是我们自己
        val t = FakeTransport(active = ActiveMeeting(recording = true, meetingId = 7, clientId = clientId))

        val r = run(t, store, activeClientId = clientId, scope = SyncScope.INCREMENTAL)

        assertEquals("不许'自己等自己'（那条路线上每一段都会带 clientId 去 start）",
            OfflinePhase.WAITING_UPLOAD, r.phase)
        assertEquals(listOf(1), t.uploaded.map { it.second })
    }

    @Test
    fun `结束时间是推算出来的那一场 要在结果里如实说一句`() {
        val store = store()
        val d = seed(store, seqs = listOf(1))
        store.save(d.copy(endedAtInferred = true))
        val t = FakeTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue("这场会的时间轴有一处是推断的，必须说出来：${r.note}",
            r.note.contains("被进程打断"))
        assertTrue("而且要说清是'推的'：${r.note}", r.note.contains("推的"))
    }

    // ---- ③ 上报暂停区间（用户 2026-10-17 的第③件事）----

    @Test
    fun `start 成功之后上报暂停区间 而且是按 clientId 整份重报`() {
        val store = store()
        seed(store, seqs = listOf(1), pauses = listOf(DraftPause(10.0, 20.0)))
        val t = FakeTransport()

        run(t, store)
        // 第二轮：把 uploaded 清掉重跑一次（模拟"又新增了一条暂停区间"）
        store.save(store.load(clientId)!!.copy(uploaded = false, acknowledgedSegments = 0))
        run(t, store)

        assertEquals("两轮都报了一次（每新增一条就再报一次）", 2, t.pauseReports.size)
        assertTrue("按 id 幂等 —— 所以每次把**当前整份**交上去，服务端合并去重",
            t.pauseReports.all { it.clientId == clientId && it.pauses == listOf(DraftPause(10.0, 20.0)) })
        assertEquals("上报发生在 start **之后**（服务端得先有这一场）",
            listOf("start", "pauses:1", "segment:1", "stop",
                "start", "pauses:1", "segment:1", "stop"), t.calls)
    }

    @Test
    fun `没有暂停区间时一个请求都不发`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport()
        run(t, store)
        assertTrue("没东西可报就别白发一次请求（也别刷日志）：${t.calls}",
            t.calls.none { it.startsWith("pauses") })
    }

    @Test
    fun `老服务端没有 pauses 端点时不算失败 补传照常走到 DONE`() {
        val store = store()
        seed(store, seqs = listOf(1, 2), pauses = listOf(DraftPause(5.0, 9.0)))
        val t = FakeTransport(pausesUnsupported = true)

        val r = run(t, store)

        assertEquals("**端点不存在跟音频没关系**：补传照常完成", OfflinePhase.DONE, r.phase)
        assertTrue(r.stopped)
        assertTrue(store.load(clientId)!!.uploaded)
        assertTrue("要如实记一句：${r.note}", r.note.contains("没报上去"))
        assertTrue("而且记进清单（下次同步还看得到）",
            store.load(clientId)!!.pauseNote.contains("没报上去"))
    }

    @Test
    fun `暂停上报连不上时也照样传完 只是如实记一句`() {
        val store = store()
        seed(store, seqs = listOf(1), pauses = listOf(DraftPause(1.0, 2.0)))
        val t = FakeTransport(pausesFailure = SyncFailure.UNREACHABLE)

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals(listOf(1), t.uploaded.map { it.second })
        assertTrue("原因要说准（是没通，不是'端点不存在'）：${r.note}",
            r.note.contains("没报上去"))
    }

    @Test
    fun `暂停上报失败不许盖掉真正的原因（收尾那句最要紧）`() {
        val store = store()
        seed(store, seqs = listOf(1), pauses = listOf(DraftPause(1.0, 2.0)))
        val t = FakeTransport(stopFailure = SyncFailure.UNREACHABLE,
            pausesUnsupported = true)

        val r = run(t, store)

        assertTrue("收尾没成那句必须在（那是'还没有开始转写'）：${r.note}",
            r.note.contains("还没有开始转写"))
        assertTrue("暂停那条也要在（两句都是事实）：${r.note}", r.note.contains("没报上去"))
    }

    // ---------------------------------------------------------------- ⑧ 其它

    @Test
    fun `没有任何要传的清单时什么都不做`() {
        val t = FakeTransport()
        val r = run(t, store())
        assertEquals(OfflinePhase.IDLE, r.phase)
        assertEquals(0, t.startCalls)
    }

    @Test
    fun `已经对账完成的清单不会被再传一遍`() {
        val store = store()
        seed(store, seqs = listOf(1, 2), acked = 2, uploaded = true)
        val t = FakeTransport()
        val r = run(t, store)
        assertEquals(OfflinePhase.IDLE, r.phase)
        assertTrue(t.uploaded.isEmpty())
    }

    // ---------------------------------------------------------------- ⑥ 幂等输入（用例钉住"只发同一个 id"）

    @Test
    fun `同一个客户端 id 反复调用只当成一场（客户端侧只发同一个 id）`() {
        val store = store()
        seed(store, seqs = listOf(1))
        val t = FakeTransport()
        // 模拟"同一份清单被反复重试"：把 uploaded 清掉再跑一遍（像超时重发那样）
        run(t, store)
        val d = store.load(clientId)!!
        store.save(d.copy(uploaded = false, acknowledgedSegments = 0))
        run(t, store)

        assertEquals("三轮（含重复）都只该发同一个 clientId",
            2, t.started.size)
        assertTrue(t.started.all { it.clientId == clientId })
        assertEquals("服务端侧按 id 幂等：只有一场会", 1, t.stored.size)
        assertEquals("同一段被重复传也不该存两遍（幂等命中）",
            listOf(1), t.stored[clientId]!!.toList())
        assertTrue("重复的那一次要能被认出来（服务端回 duplicate 时客户端当成功）",
            t.uploaded.count { it.second == 1 } == 2)
    }

    @Test
    fun `进度统计与清单一致`() {
        val d = MeetingDraft(
            clientId = "c", startedAt = "t",
            segments = (1..3).map {
                DraftSegment(seq = it, file = "s$it", startedAt = "t",
                    startOffsetSeconds = 0.0, durationSeconds = 1.0,
                    uploaded = it <= 2)
            },
            acknowledgedSegments = 2,
        )
        assertEquals(2 to 3, draftProgress(d))
    }

    @Test
    fun `toState 在离线录音时用清单的实时数字（不是上一轮的汇总）`() {
        val active = MeetingDraft(
            clientId = "c", startedAt = "t",
            segments = listOf(
                DraftSegment(1, "s1", "t", 0.0, 1.0),
                DraftSegment(2, "s2", "t", 1.0, 1.0),
            ),
        )
        val st = OfflineSyncResult(phase = OfflinePhase.RECORDING_LOCAL, pendingSegments = 0)
            .toState(active = active)
        assertEquals(OfflinePhase.RECORDING_LOCAL, st.phase)
        assertEquals("录音中那一档的数字要来自清单（它才是实时事实）", 2, st.totalSegments)
        assertEquals(2, st.pendingSegments)
    }

    @Test
    fun `published state 能被界面直接读`() {
        OfflineMeetingSync.publish(OfflineSyncState(phase = OfflinePhase.UPLOADING, pendingSegments = 3))
        assertEquals(OfflinePhase.UPLOADING, OfflineMeetingSync.state.value.phase)
        OfflineMeetingSync.publishNote("服务端拒绝了", nextAttemptAt = null)
        assertEquals("服务端拒绝了", OfflineMeetingSync.state.value.note)
        assertEquals("补一句原因不该把 phase 冲掉",
            OfflinePhase.UPLOADING, OfflineMeetingSync.state.value.phase)
        OfflineMeetingSync.reset()
        assertEquals(OfflinePhase.IDLE, OfflineMeetingSync.state.value.phase)
    }

    @Test
    fun `一份都没有时 run 直接回 IDLE`() {
        val store = store()
        val t = FakeTransport()
        assertEquals(OfflinePhase.IDLE, run(t, store).phase)
    }

    // ---------------------------------------------------------------- ⑦ 逐段进度（用户 2026-10-19）

    /**
     * 一轮同步要**逐段**报进度：界面才有东西在动（否则按完「现在同步」几分钟没反应）。
     *
     * 三条判据：
     *  * 每段开始前报一次，`currentSeq` 依次是 1/2/3（不是整轮结束才一次）；
     *  * `confirmedSegments` 是**已经到服务端**的段数 —— 正在传的那一段**不许**提前算进去
     *    （算进去就是"谎报进度"：界面显示 1/3，其实一段都还没到）；
     *  * 总量 `totalSegments` 是这一轮要处理的总段数。
     */
    @Test
    fun `同步过程中逐段报进度（已传几段、正在传第几段）`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = FakeTransport()
        val seen = ArrayList<SyncProgress>()
        runBlocking {
            OfflineMeetingSync.run(transport = t, store = store, onProgress = { seen += it })
        }
        assertEquals("三段 = 三次进度回调（每段开始前一次）",
            listOf(1, 2, 3), seen.map { it.currentSeq })
        assertEquals("总量是这一轮要处理的段数", 3, seen[0].totalSegments)
        assertEquals("还没传之前已确认 0 段", 0, seen[0].confirmedSegments)
        assertEquals("第二段开始前只确认了 1 段", 1, seen[1].confirmedSegments)
        assertEquals("第三段开始前只确认了 2 段", 2, seen[2].confirmedSegments)
        assertEquals("待传随之减少（正在传的那段仍算待传）",
            listOf(3, 2, 1), seen.map { it.pendingSegments })
    }

    /** 不传 `onProgress` 时一切照旧（默认 null，别的入口不被这条进度牵动）。 */
    @Test
    fun `不关心进度时 run 的行为一个字节都不变`() {
        val store = store()
        seed(store, seqs = listOf(1, 2))
        val t = FakeTransport()
        val r = run(t, store)
        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals(listOf(1, 2), t.uploaded.map { it.second })
    }
}
