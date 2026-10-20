package com.echo.contact.service

import com.echo.contact.data.ApiClient
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.InterruptedIOException
import java.net.SocketTimeoutException

/**
 * **"传完第 1 段就停住"的判据**（2026-10-19 真机，用户原话：*"传了一段，但是后面好像还是不行"*）。
 *
 * 现场是这样的（服务端访问日志 + 那场会议的 `meta.json`）：
 * ```
 * POST /api/meeting/segment?clientId=phone-…&seq=1&…  → 200   ✓ 落盘 01.wav（18.3 MB）
 * 之后：只有 POST /api/meeting/start?… 反复（3 次以上），**再没有 seq=2** ✗
 * meta.json：segments=['01.wav']、uploading=true、clientEndedAt=10:54:48
 * ```
 * 也就是：**第 1 段上去了、第 2 段永远没到、`stop` 永远没发**（于是那一场永远不出文字，
 * 而手机上"已传 1 段"就那么僵着）。这条链上有两处会各自造成同一个症状，本用例两类都钉住：
 *
 * ## ① 段循环（`OfflineMeetingSync.syncOne`）—— 结果：代码本身没问题，用例把它钉死
 *
 * 串行那一版是 `for (seg in segmentsToUploadOf(d))`：每段成功 → 推进水位（落盘）→
 * **继续下一段**，没有 `break`、也没有"传一段就跳到收尾"的分支；只有"本地缺文件"与
 * "服务端没收下"才 `return`（两者都不动水位、不删音频、**不 stop**）。
 *
 * ⚠️ 2026-10-20 起这一段改成了**受控并发**（`SegmentUploadPlan`，最多 3 段同时在飞）：
 * "顺序"从"一段一段发"变成了"**确认**只按连续前缀推进"（见 `ParallelSegmentUploadTest`）。
 * 这里那三条判据**一条都没丢**，只是盯的对象从"循环体"换成了"传段块"：
 * 每段都发、最后才 stop、段没传完绝不 stop。所以病根不在这里 —— 但"这里没病"
 * 本身必须是一条**能被反复验证**的判据（下面的序列断言与源码级断言），
 * 否则下次谁改一个 `break`（或把 `stop` 提到传段前面）进去，症状完全一样而没人看得出。
 *
 * ## ② 每段上传的 HTTP 超时 —— 这才是真机上的病根
 *
 * 一段 10 分钟的 **WAV** 是 ≈18.3 MB（手机 FLAC 编码失败会退回 WAV，真机正是这样），
 * 而且要走**中继**。原来传段和"查状态/建会"共用**同一个**客户端：
 * `ApiClient.build()` 里写死 `writeTimeout(60, SECONDS)`，读超时是 60 秒
 * （`ApiClient.DEFAULT_READ_TIMEOUT_S`），而 `MeetingMicService` 建 transport 时用的就是
 * `ApiClient.create(baseUrl)` —— **传段没有任何独立的超时**。
 *
 * 写超时抓的是"**整整 60 秒一个字节都没写出去**"（OkHttp 的写超时是**每次写操作**的上限，
 * 不是整个请求体的总预算）。在慢链路/中继上这属于常态，于是第 2 段被判死；
 * 而 body **不能续传**（`req.wav.toRequestBody(...)` 只能从头发），下一轮就从第 0 个字节重来
 * → **大段永远传不完 → `seq` 推不到 3 → `stop` 永远不发**。
 * 修法：`ApiClient.createForUpload()`（读/写都 960 秒 = `UPLOAD_TIMEOUT_S`，另加单段总预算
 * `UPLOAD_CALL_TIMEOUT_S`），**只**给传段用；其它接口仍然 60 秒（一个卡住的状态查询
 * 不许把整轮补传拖十几分钟）。
 *
 * 于是本文件三类判据：
 *  1. **3 段全成功**：依次 `seq=1,2,3` 每段都发、**最后才** `stop`（序列级）；
 *  2. **第 2 段超时**：已传的第 1 段**不重传**、重试**从第 2 段继续**、原因与进度**看得见**
 *     （"这段太大/网太慢、重试中、已传过的段不会重传"）；
 *  3. **段没传完绝不 `stop`**：序列级 + **源码级**（循环之后才可能走到 `stop`，循环体里没有 `break`）。
 *
 * （与 `OfflineMeetingSyncTest` 的分工：那边覆盖对账/收尾/抢占/暂停那些横向口径；
 * 这里只盯"段→段→…→stop"这条纵向的推进链，以及传段那一档的超时。）
 */
class OfflineMeetingSegmentLoopTest {

    @get:Rule val tmp = TemporaryFolder()

    private val clientId = "phone-20261019101109-aca9"

    @After
    fun tearDown() {
        // ApiClient 是全局缓存：这个类会建"上传专用"客户端，别让它漏到别的用例里
        ApiClient.clearCache()
    }

    // ---------------------------------------------------------------- 假的服务端

    /**
     * 只做三件事的假服务端：**记调用序列**、按 `clientId` 幂等地存段、能被指定某一段"超时"。
     *
     * 为什么"超时"那一档要真的经过 [uploadFailureNote]：真机上的那句话就是由它拼的
     * （`OfflineMeetingTransport.uploadSegment` 的 catch），用例从这里注入，
     * 一路流到 `OfflineSyncResult.note` —— 钉住的才是**用户会看到的那句话**，
     * 而不是用例自己编的一句。
     */
    private class LoopTransport(
        var reachCode: Int? = 200,
        var active: ActiveMeeting = ActiveMeeting(recording = false),
        /** 只有这一段会被判"上传超时"（`null` = 都正常）。 */
        var timeoutAt: Int? = null,
        /** 超时那一档实际抛的异常（默认 `SocketTimeoutException` = OkHttp 写超时的形状）。 */
        var timeoutError: Exception = SocketTimeoutException("timeout"),
    ) : OfflineTransport {

        /** **调用序列**（"最后才 stop"与"停止后不再传段"都靠它）。 */
        val calls = mutableListOf<String>()

        /** 服务端真的收下的段序号（累积，跨轮）。 */
        val uploaded = mutableListOf<Int>()

        /** 服务端手上这一段会的有哪些段（按 `clientId` 幂等，`LinkedHashSet` = 不重复存）。 */
        val stored = linkedSetOf<Int>()

        val stops = mutableListOf<StopRequest>()
        var startCalls = 0

        override suspend fun reachable(): ProbeResult? = ProbeResult(reachCode)

        override suspend fun activeMeeting(): ActiveMeeting? = active

        override suspend fun startMeeting(req: StartRequest): StartOutcome {
            startCalls++
            calls += "start"
            // 续传时清单里记着"前面几段已经传过" —— 服务端手上当然也有它们
            for (s in 1..req.acknowledgedSegments) stored += s
            return StartOutcome.Ok(42)
        }

        override suspend fun uploadSegment(req: UploadRequest): SegmentOutcome {
            calls += "segment:${req.seq}"
            if (req.seq == timeoutAt) {
                // 真 transport 就是这么翻的：一句"这段太大/网太慢、重试中" + 可重试
                return SegmentOutcome.Failed(
                    SyncFailure.UNREACHABLE,
                    uploadFailureNote(req.seq, req.wav.size, timeoutError),
                    retryable = true,
                )
            }
            uploaded += req.seq
            stored += req.seq
            return SegmentOutcome.Ok(serverMeetingId = 42, serverCount = stored.size)
        }

        override suspend fun segmentCount(clientId: String, meetingId: Int): CountOutcome =
            CountOutcome.Ok(stored.size)

        override suspend fun stopMeeting(req: StopRequest): StopOutcome {
            calls += "stop"
            stops += req
            return StopOutcome.Ok(alreadyStopped = false, note = "收下了")
        }

        /** 下一轮开始前清掉"这一轮的调用记录"，但**服务端手上的段要留着**（那才是续传的前提）。 */
        fun nextRound() {
            calls.clear()
            uploaded.clear()
        }
    }

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    /** 造一份"录完了、还有事要传"的清单（段音频**真的**写出来，大小按真机的 WAV 段给）。 */
    private fun seed(
        store: MeetingDraftStore,
        seqs: List<Int> = listOf(1, 2, 3),
        acked: Int = 0,
        /** 每段多少字节（默认 18.3 MB 那一档的一半，足够断言"MB"那句） */
        bytesPerSegment: Int = 1_000_000,
    ): MeetingDraft {
        val dir = store.dirOf(clientId).apply { mkdirs() }
        seqs.forEach {
            File(dir, MeetingDraftStore.segmentName(it)).writeBytes(ByteArray(bytesPerSegment) { 1 })
        }
        val d = MeetingDraft(
            clientId = clientId,
            startedAt = "2026-10-19T10:11:09",
            endedAt = "2026-10-19T10:54:48",
            durationSeconds = 2_619.0,
            segments = seqs.map {
                DraftSegment(seq = it, file = MeetingDraftStore.segmentName(it),
                    startedAt = "2026-10-19T10:11:09",
                    startOffsetSeconds = (it - 1) * 600.0, durationSeconds = 600.0)
            },
            acknowledgedSegments = acked,
        )
        assertTrue("用例的前置条件：清单要写得进去", store.save(d))
        return d
    }

    /** 跑一轮（`reconcileAt` 把"对账那一刻"原地记进同一个调用序列 —— 顺序判据才准）。 */
    private fun run(
        t: LoopTransport,
        store: MeetingDraftStore,
        onProgress: ((SyncProgress) -> Unit)? = null,
        onSegmentDone: ((SyncProgress) -> Unit)? = null,
        markReconcile: Boolean = false,
    ): OfflineSyncResult = runBlocking {
        OfflineMeetingSync.run(
            transport = t,
            store = store,
            manual = false,
            scope = SyncScope.FINALIZE,
            // 对账不一定是一次网络调用（段响应带了段数时就不会再问），所以"对账那一刻"
            // 只能由这个回调原地记进**同一个**调用序列 —— 顺序判据才准。
            reconcileAt = if (!markReconcile) null else { _: MeetingDraft -> t.calls += "reconcile" },
            onProgress = onProgress,
            onSegmentDone = onSegmentDone,
        )
    }

    // ---------------------------------------------------------------- ① 三段全成功

    /**
     * **3 段全成功**：依次 `seq=1,2,3`、**每段都真的发**、`stop` 在**最后**且只有一次。
     *
     * 这条是"传完第 1 段就停住"最直接的回归判据：真机上第 1 段之后就没有 `seq=2` 了，
     * 而这里要求 `1,2,3` 一段不落，且 `stop` 必须排在它们与对账之后。
     */
    @Test
    fun `三段全成功：依次发 seq=1 2 3 每段都发 最后才 stop`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = LoopTransport()

        val r = run(t, store, markReconcile = true)

        assertEquals("三段必须一段不落地发出去（真机就是在这里断的）",
            listOf(1, 2, 3), t.uploaded)
        assertEquals(
            "调用序列必须是 start → segment:1 → segment:2 → segment:3 → 对账 → stop",
            listOf("start", "segment:1", "segment:2", "segment:3", "reconcile", "stop"),
            t.calls,
        )
        assertEquals("stop 只许一次", 1, t.stops.size)
        assertEquals("收尾要带这一场自己的 clientId（护栏才生效）",
            clientId, t.stops[0].clientId)
        assertEquals(OfflinePhase.DONE, r.phase)
        assertTrue("stop 成了才敢说'开始转写'", r.stopped)
        assertEquals(3, r.confirmedSegments)
        assertEquals(0, r.pendingSegments)
        val d = store.load(clientId)!!
        assertTrue(d.uploaded)
        assertEquals("水位到头", 3, d.acknowledgedSegments)
        assertTrue("默认策略：确认之后音频仍然留着（宁可多留一会儿）",
            (1..3).all { store.segmentFile(clientId, it).isFile })
    }

    /**
     * **第 2 段超时**：`stop` 一次都不许有、对账也不许有（段没到齐去对账只会得到假的不符）；
     * 已传的第 1 段**不许重传**；重试**从第 2 段继续**；原因与进度都要看得见。
     *
     * 真机上写超时 60 秒就把第 2 段判死了 —— 现在修的是"这一段本来就能慢慢传过去"
     * （见 [ApiClient.UPLOAD_TIMEOUT_S]）；而**万一**它还是超时，这一条钉的是
     * "不许重头再来、也不许把它说成服务端出错"。
     */
    @Test
    fun `第 2 段超时：已传的第 1 段不重传 重试从第 2 段继续 进度与原因可见`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = LoopTransport(timeoutAt = 2)

        val seen = ArrayList<SyncProgress>()
        val done = ArrayList<SyncProgress>()
        val first = run(t, store, onProgress = { seen += it }, onSegmentDone = { done += it })

        assertEquals("只传上去第 1 段", listOf(1), t.uploaded)
        assertFalse("**段没传完，一次 stop 都不许有**（序列级）", t.calls.contains("stop"))
        assertFalse("段没到齐也不许去对账（那只会得到一条假的不符）",
            t.calls.contains("reconcile"))
        assertEquals("超时是**可重试**那一档（等会儿换个时刻就能过）",
            OfflinePhase.WAITING_UPLOAD, first.phase)
        val d = store.load(clientId)!!
        assertEquals("水位停在第 1 段（那一段是真被认下的）", 1, d.acknowledgedSegments)
        assertFalse("没传完就不许标已上传", d.uploaded)

        // 原因：这段太大/网太慢、重试中、已传过的段不会重传（**三句都要在**）
        assertTrue("要说这一段有多大：${first.note}", first.note.contains("第 2 段"))
        assertTrue("要说'这段太大/网太慢'：${first.note}",
            first.note.contains("太大") && first.note.contains("网太慢"))
        assertTrue("要说'重试中'：${first.note}", first.note.contains("重试"))
        assertTrue("要说清已传过的段不会重传：${first.note}", first.note.contains("不会重传"))
        assertTrue("也要说下次从第 2 段接着传：${first.note}", first.note.contains("从第 2 段"))

        // 进度：开始那一报是 1 与 2（第 2 段开始传了），结束那一报只有第 1 段
        assertEquals("开始传的段（第 2 段开始了但没那么顺利）", listOf(1, 2), seen.map { it.currentSeq })
        assertEquals("真的到服务端的只有第 1 段", listOf(1), done.map { it.justConfirmed })
        assertEquals("那一报里的已传段数是真数", 1, done[0].confirmedSegments)
        assertEquals(2, done[0].pendingSegments)

        // ---- 第二轮：链路好了（超时那一档没了）→ 从第 2 段继续，第 1 段不许重传 ----
        t.timeoutAt = null
        t.nextRound()
        val second = run(t, store)

        assertEquals("**第 1 段不许重传**，从第 2 段接着传", listOf(2, 3), t.uploaded)
        assertEquals(listOf("start", "segment:2", "segment:3", "stop"), t.calls)
        assertEquals("三段到齐之后才收尾", 1, t.stops.size)
        assertEquals(OfflinePhase.DONE, second.phase)
        assertTrue(second.stopped)
    }

    /** 段传了一半之后**再断一次**，水位仍然停在最后成功的那一段（续传不许回退）。 */
    @Test
    fun `每轮都从最后一个成功段之后继续（水位单调）`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3, 4), bytesPerSegment = 1024)
        val t = LoopTransport(timeoutAt = 3)

        run(t, store)
        assertEquals(listOf(1, 2), t.uploaded)
        assertEquals(2, store.load(clientId)!!.acknowledgedSegments)

        // 还是第 3 段超时：不许把 1、2 再传一遍，也不许把水位往前挪
        t.nextRound()
        val again = run(t, store)
        assertEquals("水位之前的两段不许重传", listOf<Int>(), t.uploaded)
        assertEquals(2, store.load(clientId)!!.acknowledgedSegments)
        assertEquals(OfflinePhase.WAITING_UPLOAD, again.phase)
        assertFalse(t.calls.contains("stop"))
    }

    // ---------------------------------------------------------------- ② 段没传完绝不 stop（源码级）

    /**
     * **源码级判据**：`stop` 只可能在"传段走完 + 对账通过"之后才被调到。
     *
     * 为什么序列级判据不够、还得看源码：假 transport 的调用序列只能证明"这一轮没调到 stop"，
     * 证明不了"结构上不可能调到" —— 例如有人把 `stop` 提到传段前面（"先收尾再补段"），
     * 在"段全成功"的用例里**照样全绿**（stop 还是只有一次、还是最后），
     * 而真机上那等于**拿一份缺段的音频去转写**。
     *
     * 三条检查（都对着源码文本，只读不改）：
     *  1. `transport.stopMeeting(` 的位置在**传段那一块**（`SegmentUploadPlan.run(`）**之后**；
     *  2. 对账（`reconcileAt?.invoke(d)`）在 `stop` **之前**；
     *  3. 传段那一块里**没有** `stopMeeting(`，而且段失败时就在块里 `return`（不往下走到收尾）。
     *
     * ⚠️ 2026-10-20 起段是**受控并发**传的（`SegmentUploadPlan`），所以这里盯的
     * "循环体"换成了"传段块"（`SegmentUploadPlan.run(` … `// ---- 还在录`）。判据本身
     * 一个都没放松：**段没全成绝不 stop**。
     */
    @Test
    fun `段没传完绝不 stop（源码级：stop 在传段与对账之后）`() {
        val src = source("src/main/java/com/echo/contact/service/OfflineMeetingSync.kt")
        val upload = src.indexOf("SegmentUploadPlan.run(")
        val stop = src.indexOf("transport.stopMeeting(")
        val reconcile = src.indexOf("reconcileAt?.invoke(d)")
        assertTrue("源码里找不到传段那一块（判据会失效，别当成通过）", upload > 0)
        assertTrue("源码里找不到 stop 调用", stop > 0)
        assertTrue("stop 必须在传段**之后**（否则就是拿一份缺段的音频去转写）", stop > upload)
        assertTrue("对账必须在 stop 之前", reconcile > 0 && reconcile < stop)

        // 传段块到"这一轮只传段、不收尾"那一档为止：里面不许 stop，失败要在块里 return
        val tail = src.indexOf("// ---- 还在录", upload)
        assertTrue("找不到传段块结束的位置（注释被改了？）", tail > upload)
        val block = src.substring(upload, tail)
        // ⚠️ 只看**代码行**（丢掉注释行）：这一段的注释里本来就会解释"为什么不在这里 stop"，
        // 按整段文本去搜会把自己写的那句注释当成调用（这条用例第一次就是这么红的）。
        val code = block.lines().filterNot { it.trim().startsWith("//") }.joinToString("\n")
        assertFalse("传段块里不许有 stop：那会跳出去在段没齐的时候收尾",
            code.contains("transport.stopMeeting("))
        assertTrue("段失败必须在块里就 return（不往下走到对账与收尾）",
            code.contains("return StageResult("))
    }

    /**
     * **源码级判据**：传段那一路必须用**上传专用**的客户端（读/写都是长超时）。
     *
     * 为什么这条要看源码：`OfflineMeetingTransport` 的第二个参数有默认值（`= api`），
     * 于是任何一处忘了传都能"编译通过、用例全绿"，而真机上又退回 60 秒写超时
     * —— 那个 bug 的症状（传完第 1 段就停住）与这一次一模一样，靠跑用例**看不出来**。
     */
    @Test
    fun `传段必须用上传专用客户端（源码级 + 常量级）`() {
        val mic = source("src/main/java/com/echo/contact/service/MeetingMicService.kt")
        assertTrue("生产代码必须建上传专用客户端（ApiClient.createForUpload）",
            mic.contains("ApiClient.createForUpload("))
        assertTrue("并且要作为 uploadApi 交给 OfflineMeetingTransport",
            mic.contains("uploadApi = ApiClient.createForUpload(baseUrl)"))

        val transport = source("src/main/java/com/echo/contact/service/OfflineMeetingTransport.kt")
        assertTrue("传段那个请求必须走 uploadApi（不是普通 api）",
            transport.contains("uploadApi.meetingSegmentWithId("))

        // 常量口径：写超时必须与读对齐（60 秒传不完 18.3 MB 的 WAV）
        assertEquals("写超时与读超时是同一个数（同一个预算的两半）",
            ApiClient.UPLOAD_TIMEOUT_S, ApiClient.LONG_READ_TIMEOUT_S)
        assertTrue("单段总预算必须比「单次无进展」的上限更长（18.3 MB 慢链路要能过）",
            ApiClient.UPLOAD_CALL_TIMEOUT_S > ApiClient.UPLOAD_TIMEOUT_S)
    }

    // ---------------------------------------------------------------- ③ 超时那句话

    /**
     * **超时要说"这段太大 / 网太慢，重试中"**（用户点名要求），并且带上"已传过的段不会重传"。
     *
     * 数字用的是真机那一段的大小（18.3 MB），所以这句话与真机上看到的是同一句。
     */
    @Test
    fun `超时那句要说这段太大网太慢而且在重试`() {
        val note = uploadFailureNote(2, 19_189_940, SocketTimeoutException("timeout"))

        assertTrue("要说清是哪一段：$note", note.contains("第 2 段"))
        assertTrue("要说清这一段有多大：$note", note.contains("18.3 MB"))
        assertTrue("要说'这段太大'：$note", note.contains("太大"))
        assertTrue("要说'网太慢'：$note", note.contains("网太慢"))
        assertTrue("要说清在下一次重试那一档（不是永久失败）：$note", note.contains("重试"))
        assertTrue("要说已传过的段不会重传：$note", note.contains("不会重传"))
        assertFalse("不许说成服务端拒绝/出错（那会把人引去查错的地方）",
            note.contains("服务端"))
    }

    /** 单段总预算用完同样是**超时**（"一直在动但永远传不完"那一档），不许说成服务端出错。 */
    @Test
    fun `单段总预算用完也算超时`() {
        val note = uploadFailureNote(3, 19_189_940, InterruptedIOException("timeout"))
        assertTrue(note, note.contains("太大"))
        assertTrue("要说清总预算是多少秒：$note",
            note.contains(ApiClient.UPLOAD_CALL_TIMEOUT_S.toString()))
        assertTrue(note.contains("不会重传"))
    }

    /** 非超时的异常**不许冒充超时**：原样带上异常类名与消息（排障的唯一线索）。 */
    @Test
    fun `不是超时的失败不许冒充超时`() {
        val note = uploadFailureNote(2, 2048, IllegalStateException("boom"))
        assertTrue("要说清没发出去：$note", note.contains("没发出去"))
        assertTrue("异常类名要留着：$note", note.contains("IllegalStateException"))
        assertTrue("异常消息要留着：$note", note.contains("boom"))
        assertFalse("不许冒充超时：$note", note.contains("太大") || note.contains("超时"))
    }

    /** 大小换算用点号（这句会被用例逐字比对，也跟着日志走）。 */
    @Test
    fun `大小换算`() {
        assertEquals("18.3 MB", megabytesText(19_189_940))
        assertEquals("640 KB", megabytesText(655_360))
        assertEquals("512 B", megabytesText(512))
        assertEquals("0 B", megabytesText(0))
    }

    // ---------------------------------------------------------------- ④ 每段开始/结束都报进度

    /**
     * **每段开始与每段结束各报一次**（"进度真实"）：
     *  * 开始那一报 `justConfirmed = 0`（这一段还没到）；
     *  * 结束那一报 `justConfirmed = 该段`、`confirmedSegments` 是**真的到了**的段数，
     *    `pendingSegments` 随之减少 —— 用户才不会看到"传完最后一段还写着 2/3"。
     */
    @Test
    fun `每段结束也报一次真进度（不只是开始）`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3), bytesPerSegment = 1024)
        val t = LoopTransport()
        val started = ArrayList<SyncProgress>()
        val done = ArrayList<SyncProgress>()

        run(t, store, onProgress = { started += it }, onSegmentDone = { done += it })

        assertEquals("每段开始前各一次", listOf(1, 2, 3), started.map { it.currentSeq })
        assertEquals("开始那一报里'刚确认'必须是 0（这一段还没到服务端）",
            listOf(0, 0, 0), started.map { it.justConfirmed })
        assertEquals("每段成功后各一次（段序号相同，但含义是'刚传完'）",
            listOf(1, 2, 3), done.map { it.justConfirmed })
        assertEquals("结束那一报的已传段数必须是真数",
            listOf(1, 2, 3), done.map { it.confirmedSegments })
        assertEquals("待传随之减少，最后一段传完是 0",
            listOf(2, 1, 0), done.map { it.pendingSegments })
        assertTrue("总量始终是这一轮要处理的 3 段", done.all { it.totalSegments == 3 })
    }

    /** 不传这两个回调时，`run` 的判定与之前**一个字节都不变**（其它入口不受影响）。 */
    @Test
    fun `不关心进度时行为不变`() {
        val store = store()
        seed(store, seqs = listOf(1, 2), bytesPerSegment = 1024)
        val t = LoopTransport()

        val r = run(t, store)

        assertEquals(OfflinePhase.DONE, r.phase)
        assertEquals(listOf(1, 2), t.uploaded)
        assertEquals(listOf("start", "segment:1", "segment:2", "stop"), t.calls)
    }

    /**
     * 上传专用客户端：与普通客户端**必须是两个实例**（否则要么传段被 60 秒判死，
     * 要么查状态也跟着等 16 分钟），同一个地址要**复用**（每次新建 = 漏连接池/线程池）。
     */
    @Test
    fun `上传客户端与普通客户端是两个实例 而且各自复用`() {
        val base = "http://10.0.0.1:8970"
        val plain = ApiClient.create(base)
        val upload = ApiClient.createForUpload(base)

        assertNotSame("传段必须用另一个客户端（读/写超时都不一样）", plain, upload)
        assertNotSame("也不能复用'长读超时'那个（它的写超时仍是 60 秒）",
            upload, ApiClient.create(base, ApiClient.LONG_READ_TIMEOUT_S))
        assertSame("同一个地址要复用（否则每段都新建连接池 = 泄漏）",
            upload, ApiClient.createForUpload(base))
        assertTrue("写超时至少要和读一样长（18.3 MB 的 WAV 走中继）",
            ApiClient.UPLOAD_TIMEOUT_S >= 900)
        assertTrue("普通接口仍然要快（一个卡住的状态查询不许拖住整轮补传）",
            ApiClient.DEFAULT_READ_TIMEOUT_S < ApiClient.UPLOAD_TIMEOUT_S)
        assertTrue(ApiClient.DEFAULT_WRITE_TIMEOUT_S < ApiClient.UPLOAD_TIMEOUT_S)
    }

    /**
     * **传段只走"上传专用"客户端**（两个假服务端，各记自己的请求）。
     *
     * 为什么这条要有：`OfflineMeetingTransport` 的第二个参数有默认值（`= api`），
     * 忘了传也编译得过、别的用例也全绿 —— 而真机上就又退回 60 秒写超时
     * （症状与这次一模一样：传完第 1 段就停住）。这条用**请求落在哪个服务端**来钉它：
     * 传段的那个 POST 必须出现在"上传专用"那一台，普通那一台**一个请求都不许收到**。
     */
    @Test
    fun `传段只走上传专用客户端（普通客户端一个请求都不许收到）`() {
        val plain = okhttp3.mockwebserver.MockWebServer()
        val uploadOnly = okhttp3.mockwebserver.MockWebServer()
        plain.start()
        uploadOnly.start()
        try {
            uploadOnly.enqueue(
                okhttp3.mockwebserver.MockResponse()
                    .setResponseCode(200)
                    .setHeader("Content-Type", "application/json")
                    .setBody("""{"ok":true,"meetingId":42,"segmentCount":2}""")
            )
            val out = runBlocking {
                OfflineMeetingTransport(
                    api = ApiClient.create(plain.url("/").toString()),
                    uploadApi = ApiClient.createForUpload(uploadOnly.url("/").toString()),
                ).uploadSegment(
                    UploadRequest(
                        clientId = clientId, seq = 2, startedAt = "2026-10-19T10:11:09",
                        source = MeetingSource.PHONE, wav = ByteArray(64) { 1 },
                    )
                )
            }

            assertTrue("这一段要被收下：$out", out is SegmentOutcome.Ok)
            assertEquals("传段必须落在上传专用那一台上", 1, uploadOnly.requestCount)
            assertEquals("普通客户端（60 秒写超时）一个请求都不许收到", 0, plain.requestCount)
            val req = uploadOnly.takeRequest()
            assertEquals("/api/meeting/segment", req.path!!.substringBefore('?'))
            assertTrue("段序号要带上（续传与对账的基准）：${req.path}", req.path!!.contains("seq=2"))
        } finally {
            plain.shutdown()
            uploadOnly.shutdown()
            ApiClient.clearCache()
        }
    }

    // ---------------------------------------------------------------- 源码定位

    /**
     * 读源码文本（**只读**；源码级判据用）。
     *
     * 为什么把候选路径列出来再断言：Gradle 单测的工作目录通常是模块目录（`app/`），
     * 但这不该是"猜对了才通过"的东西 —— 找不到就把找过哪些路径一起报出来，
     * 免得某天 CWD 变了，这几条源码级判据变成**悄悄不生效**。
     */
    private fun source(relative: String): String {
        val candidates = listOf(
            File(relative),          // CWD = app/
            File("app/$relative"),   // CWD = android/
            File("../$relative"),
        )
        val f = candidates.firstOrNull { it.isFile }
        assertNotNull("找不到源码 $relative（找过：${candidates.joinToString()}）", f)
        return f!!.readText()
    }
}
