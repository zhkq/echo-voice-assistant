package com.echo.contact.service

import com.echo.contact.audio.SegmentFormat
import com.echo.contact.data.ApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.Collections
import java.util.concurrent.atomic.AtomicInteger
import kotlin.system.measureTimeMillis

/**
 * **手机补传改并行**的判据（用户 2026-10-20：*"手机补传录音速度太慢了，有什么可以优化"*）。
 *
 * ## 现场（用户核对过的实测数据）
 *
 * ```
 * 01.wav 到盘 23:02:29 → 02.wav 23:08:42   第 2 段 6 分 13 秒 ✗
 * 03.wav 23:08:46（4 秒 ✓）  04.wav 23:12:11（3 分 25 秒 ✗）
 * 手机传的是 FLAC ≈ 10 MB / 10 分钟段  →  平均约 30–160 KB/s
 * ```
 * 单段几分钟、串行 5 段就是十几分钟。瓶颈**不是段大小**（已是 FLAC），而是**单连接的吞吐** ——
 * 所以解法是"最多 3 段同时在飞"（[SegmentUploadPlan]），把单连接之外的链路余量吃进来。
 *
 * ## 本文件钉的东西（对应需求里点名的每一条）
 *
 *  1. **真的并行**：用"同时在飞的段数"的时间栅栏看峰值（不是看总时长猜）；
 *  2. **整轮时长**：假 transport 每段 300ms → 并行后总时长不到串行的 2/3；
 *  3. **一段失败不拖死别人**：其他两段照样完成，整轮报"成功 2 / 失败 1"；
 *  4. **水位只推进到连续前缀**：`seq=3` 先成功、`seq=2` 后成功时，过程中**不许**把水位推过 2；
 *  5. **停不提前**：段没全成 → 一次 `stop` 都没有（沿用既有的那条铁律）；
 *  6. **并发上限配置化** + **传段复用同一个上传客户端**（源码级 + `assertSame`）。
 *
 * ⚠️ 为什么这里要 `runBlocking(Dispatchers.Default)`：并发调度**不自己切线程池**
 * （`launch` 继承调用方上下文，生产那条路是 `Dispatchers.IO`）。用单线程的
 * `runBlocking {}` 跑，几段会按顺序走完 —— 那**证明不了**并行，也发现不了
 * "一段失败把父作用域取消掉"这种错法。所以凡是验并行的用例，都在多线程 dispatcher 上跑，
 * 且假的搬运实现会真的 `delay`（挂起 = 让出线程）。
 */
class ParallelSegmentUploadTest {

    @get:Rule val tmp = TemporaryFolder()

    private val clientId = "phone-20261020100000-abcd"

    @After
    fun tearDown() {
        // ApiClient 是全局缓存：这条用例会建"上传专用"客户端，别让它漏到别的用例里
        ApiClient.clearCache()
    }

    // ---------------------------------------------------------------- 假的东西

    /** 造一段（只用于调度层：不读文件）。 */
    private fun seg(seq: Int, format: SegmentFormat = SegmentFormat.FLAC) = DraftSegment(
        seq = seq,
        file = MeetingDraftStore.segmentName(seq, format),
        startedAt = "2026-10-20T10:00:00",
        startOffsetSeconds = (seq - 1) * 600.0,
        durationSeconds = 600.0,
        format = format,
    )

    /**
     * 假的搬运实现：**每段多久、哪段失败都由用例说**，并记下"同时在飞"的峰值。
     *
     * 为什么峰值要在这里数（而不是拿总时长去反推）：总时长受调度开销、dispatcher 线程数影响，
     * 只能说明"比串行快"；"真的同时在飞 N 段"只有这个进/出计数能证明。
     */
    private class FakeShipper(
        private val delays: Map<Int, Long>,
        private val failures: Set<Int> = emptySet(),
        private val defaultDelayMs: Long = 20L,
    ) : SegmentShipper {

        private val inFlight = AtomicInteger(0)
        val maxInFlight = AtomicInteger(0)
        val attempts = Collections.synchronizedList(mutableListOf<Int>())

        override suspend fun ship(seg: DraftSegment): SegmentShipOutcome {
            attempts += seg.seq
            val now = inFlight.incrementAndGet()
            maxInFlight.updateAndGet { maxOf(it, now) }
            try {
                delay(delays[seg.seq] ?: defaultDelayMs)
                return if (seg.seq in failures) {
                    SegmentShipOutcome.Failed(
                        seq = seg.seq,
                        failure = SyncFailure.UNREACHABLE,
                        note = "第 ${seg.seq} 段（假服务端）没发出去",
                        retryable = true,
                    )
                } else {
                    // serverCount 给序号只是让用例能看出"哪一段的回包"，对账另有判据
                    SegmentShipOutcome.Ok(seg.seq, serverMeetingId = 42, serverCount = seg.seq)
                }
            } finally {
                inFlight.decrementAndGet()
            }
        }
    }

    /**
     * 走**完整编排**（`OfflineMeetingSync.run`）时用的假 ECHO：每段的耗时/失败可控。
     *
     * 为什么要真的走编排而不是只测调度层：`stop` 的时机、失败后整轮的结果、水位落盘
     * 都是编排层的事 —— 只测调度层证明不了"段没全成时不收尾"。
     */
    private class DelayTransport(
        private val delayOf: Map<Int, Long> = emptyMap(),
        private val failSeqs: Set<Int> = emptySet(),
        /**
         * 每段**开始**上传那一刻被调一次（用例用它模拟"录音侧正好在这时封存了新的一段"）。
         *
         * 为什么放在"开始"而不是"回包之后"：放在回包之后，这次写入会与同步侧的落盘
         * 互相赛跑（那条用例会变成"有时红有时绿"）；放在开始，等上传真的回来时磁盘上
         * 已经稳稳是新的那份了 —— 判据只盯"同步侧会不会抹掉它"，不赌赛跑。
         */
        private val onAttempt: ((Int) -> Unit)? = null,
    ) : OfflineTransport {

        val attempts = Collections.synchronizedList(mutableListOf<Int>())
        val uploaded = Collections.synchronizedList(mutableListOf<Int>())
        val stops = Collections.synchronizedList(mutableListOf<StopRequest>())
        val stored: MutableSet<Int> = java.util.concurrent.ConcurrentHashMap.newKeySet()
        var startCalls = 0

        override suspend fun reachable(): ProbeResult? = ProbeResult(200)

        override suspend fun activeMeeting(): ActiveMeeting = ActiveMeeting(recording = false)

        override suspend fun startMeeting(req: StartRequest): StartOutcome {
            startCalls++
            for (s in 1..req.acknowledgedSegments) stored += s
            return StartOutcome.Ok(42)
        }

        override suspend fun uploadSegment(req: UploadRequest): SegmentOutcome {
            attempts += req.seq
            onAttempt?.invoke(req.seq)
            delay(delayOf[req.seq] ?: 10L)
            if (req.seq in failSeqs) {
                return SegmentOutcome.Failed(
                    SyncFailure.UNREACHABLE, "第 ${req.seq} 段（假服务端）没发出去", retryable = true)
            }
            stored += req.seq
            uploaded += req.seq
            return SegmentOutcome.Ok(serverMeetingId = 42, serverCount = stored.size)
        }

        override suspend fun segmentCount(clientId: String, meetingId: Int): CountOutcome =
            CountOutcome.Ok(stored.size)

        override suspend fun stopMeeting(req: StopRequest): StopOutcome {
            stops += req
            return StopOutcome.Ok(alreadyStopped = false, note = "收下了")
        }
    }

    private fun store() = MeetingDraftStore(tmp.newFolder("offline"))

    /** 造一份"录完了、还有段要传"的清单（音频**真的**写出来，很小：判据与大小无关）。 */
    private fun seed(store: MeetingDraftStore, seqs: List<Int> = listOf(1, 2, 3)): MeetingDraft {
        val dir = store.dirOf(clientId).apply { mkdirs() }
        seqs.forEach {
            File(dir, MeetingDraftStore.segmentName(it, SegmentFormat.FLAC))
                .writeBytes(ByteArray(32) { 1 })
        }
        val d = MeetingDraft(
            clientId = clientId,
            startedAt = "2026-10-20T10:00:00",
            endedAt = "2026-10-20T10:50:00",
            durationSeconds = 3_000.0,
            segments = seqs.map { seg(it) },
        )
        assertTrue("用例的前置条件：清单要写得进去", store.save(d))
        return d
    }

    // ---------------------------------------------------------------- ① 真的并行 / 上限

    /**
     * **3 段并行：同时在飞的峰值 = 3**（默认并发度就是 3）。
     *
     * 这条判的是"真的并行"，不是"总时长看起来快了一点"：每段都 `delay(200)`，
     * 串行的话峰值永远是 1；只有受控并发（Semaphore 放 3 个）才会出现 3。
     */
    @Test
    fun `三段并行：同时在飞的峰值等于 3（不是串行）`() {
        val shipper = FakeShipper((1..3).associateWith { 200L })
        runBlocking(Dispatchers.Default) {
            SegmentUploadPlan.run(segments = (1..3).map { seg(it) }, shipper = shipper)
        }
        assertEquals("默认并发度是 3（见 SegmentUploadPlan 的说明）",
            3, SegmentUploadPlan.DEFAULT_CONCURRENCY)
        assertEquals("三段必须真的同时在飞（时间栅栏看到的峰值）",
            3, shipper.maxInFlight.get())
        assertEquals("三段都要发出", listOf(1, 2, 3), shipper.attempts.sorted())
    }

    /** 并发度**可配置**：2 就是 2、1 退回串行、超上限被夹到 3（配置不许把内存打爆）。 */
    @Test
    fun `并发上限可配置：2 就是 2、1 退回串行、超上限被夹住`() {
        val two = FakeShipper((1..4).associateWith { 150L })
        runBlocking(Dispatchers.Default) {
            SegmentUploadPlan.run(
                segments = (1..4).map { seg(it) }, concurrency = 2, shipper = two)
        }
        assertEquals("配 2 就必须是 2（多一段也得等）", 2, two.maxInFlight.get())

        val serial = FakeShipper((1..3).associateWith { 30L })
        runBlocking(Dispatchers.Default) {
            SegmentUploadPlan.run(
                segments = (1..3).map { seg(it) }, concurrency = 1, shipper = serial)
        }
        assertEquals("1 = 串行（排障/对照用）", 1, serial.maxInFlight.get())

        assertEquals("配置 99 被夹到上限 3", 3, SegmentUploadPlan.normalizedConcurrency(99))
        assertEquals("配置 0 被夹到下限 1", 1, SegmentUploadPlan.normalizedConcurrency(0))
        assertEquals("负数也一样", 1, SegmentUploadPlan.normalizedConcurrency(-5))
    }

    /**
     * **整轮时长**：3 段各 300ms —— 串行 ≈900ms，并行后必须 < 串行的 2/3（即 <600ms）。
     *
     * 为什么把串行那一轮也**真的跑一遍**（而不是拿 3×300 算）：机器忙的时候两条都可能变慢，
     * 用同一台机器、同一时刻量出来的两个数比，才是"并行确实省了时间"的判据。
     */
    @Test
    fun `整轮时长：3 段各 300ms 时 并行总时长不到串行的 2 3`() {
        val serial = measureTimeMillis {
            runBlocking(Dispatchers.Default) {
                SegmentUploadPlan.run(
                    segments = (1..3).map { seg(it) }, concurrency = 1,
                    shipper = FakeShipper((1..3).associateWith { 300L }))
            }
        }
        val parallel = measureTimeMillis {
            runBlocking(Dispatchers.Default) {
                SegmentUploadPlan.run(
                    segments = (1..3).map { seg(it) }, concurrency = 3,
                    shipper = FakeShipper((1..3).associateWith { 300L }))
            }
        }
        assertTrue("串行那一轮至少要 900ms（实测 ${serial}ms）", serial >= 900L)
        assertTrue(
            "并行那一轮必须明显更短：${parallel}ms vs 串行 ${serial}ms（要求 < ${serial * 2 / 3}ms）",
            parallel < serial * 2 / 3,
        )
    }

    /**
     * **水位只按连续前缀推进**：`seq=3` 先成功、`seq=2` 后成功 → 最终水位 3，
     * 但**过程中**绝不许把水位推过 2（那是下一轮跳过缺口、永久丢段的入口）。
     *
     * 时间线（每段一个假耗时）：1 → 10ms、3 → 150ms、2 → 600ms（间隔拉大一点，
     * 免得机器一忙就把"谁先成功"抖乱 —— 这条判据要的是**水位**，不是赛跑名次）。
     * 期望的水位回调序列：`(1→1)`、`(3→1)`、`(2→3)` —— 第 3 段成功时水位**还是 1**。
     */
    @Test
    fun `seq=3 先成功、seq=2 后成功：过程中水位不许推过 2 最终才到 3`() {
        val updates = Collections.synchronizedList(mutableListOf<Pair<Int, Int>>())
        val shipper = FakeShipper(mapOf(1 to 10L, 2 to 600L, 3 to 150L))
        val round = runBlocking(Dispatchers.Default) {
            SegmentUploadPlan.run(
                segments = listOf(seg(1), seg(2), seg(3)),
                acknowledged = 0,
                concurrency = 3,
                shipper = shipper,
                onSucceeded = { ok, ack -> updates += ok.seq to ack },
            )
        }

        assertEquals("三段都成功了", 3, round.succeeded)
        assertEquals("整轮之后水位到 3", 3, round.acknowledged)
        assertEquals("水位回调的顺序与每次的值（第 3 段成功时**不许**把水位推到 3）",
            listOf(1 to 1, 3 to 1, 2 to 3), updates.toList())
        assertTrue(
            "第 3 段成功之前/当时都不许把水位推过 2：$updates",
            updates.filter { it.first == 3 }.all { it.second <= 2 },
        )
    }

    /** 连续水位是**纯函数**：缺口、老清单（前几段不在本地）、乱序成功都算对。 */
    @Test
    fun `连续水位：缺口不推 老清单按段序号推 永远不会退`() {
        assertEquals("2 还没成 → 只到 1", 1,
            contiguousAcknowledged(0, listOf(1, 2, 3), setOf(1, 3)))
        assertEquals("1、2 都成了 → 到 2", 2,
            contiguousAcknowledged(0, listOf(1, 2, 3), setOf(1, 2)))
        assertEquals("老清单只有 7、8（水位在 6）→ 两段都成了就到 8", 8,
            contiguousAcknowledged(6, listOf(7, 8), setOf(7, 8)))
        assertEquals("清单本身有洞（1、3）→ 只到 1（2 不在清单里，不能跳）", 1,
            contiguousAcknowledged(0, listOf(1, 3), setOf(1, 3)))
        assertEquals("水位只进不退：晚到的一段不许把它拉回去", 2,
            contiguousAcknowledged(2, listOf(3, 4), setOf(4)))
        assertEquals("一段都没成 → 原地不动", 0,
            contiguousAcknowledged(0, listOf(1, 2), emptySet()))
    }

    // ---------------------------------------------------------------- ② 失败只取消它自己

    /**
     * **一段失败 → 其他两段照样完成**，整轮如实报"成功 2 / 失败 1"，而且：
     *  * **不许 stop**（段没全成 = 这一场没收尾）；
     *  * 水位只推进到**连续前缀**（第 2 段失败 → 水位停在 1，绝不因为第 3 段到了就推到 3）。
     *
     * 走完整编排（`OfflineMeetingSync.run`），假的 ECHO 里第 2 段 250ms 后失败、
     * 第 1/3 段 30ms 就成功 —— 三段同时在飞，所以另外两段是**真的跑完**的，不是被取消。
     */
    @Test
    fun `一段失败：其他两段照样完成 整轮报成功 2 失败 1 且不 stop`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = DelayTransport(
            delayOf = mapOf(1 to 30L, 2 to 250L, 3 to 30L),
            failSeqs = setOf(2),
        )

        val r = runBlocking(Dispatchers.Default) {
            OfflineMeetingSync.run(transport = t, store = store, manual = false,
                scope = SyncScope.FINALIZE)
        }

        assertEquals("三段都要**真的发过**（失败不拖死别人）",
            listOf(1, 2, 3), t.attempts.sorted())
        assertEquals("到服务端的是 1 和 3（第 2 段失败）", listOf(1, 3), t.uploaded.sorted())
        assertEquals("整轮：成功 2 段", 2, r.roundSucceeded)
        assertEquals("整轮：失败 1 段", 1, r.roundFailed)
        assertEquals("水位只推进到连续前缀 1（不许因为第 3 段到了就推过 2）",
            1, r.confirmedSegments)
        assertEquals("清单里的水位也只到 1",
            1, store.load(clientId)!!.acknowledgedSegments)
        assertFalse("没标已上传（这一场还没传完/没收尾）", store.load(clientId)!!.uploaded)
        assertTrue("**段没全成，一次 stop 都不许有**：${t.stops}", t.stops.isEmpty())
        assertTrue("原因要报那句'成功 2 / 失败 1'：${r.note}",
            r.note.contains("成功 2 段") && r.note.contains("失败 1 段"))
        assertTrue("要指到第一处失败（第 2 段）：${r.note}", r.note.contains("第 2 段"))
        assertTrue("要说下一轮从缺口继续：${r.note}", r.note.contains("从第 2 段"))
        assertTrue("还要说已确认的段不会重传：${r.note}", r.note.contains("不会重传"))
        assertTrue("音频一个都不许删（宁可多留一会儿）",
            (1..3).all { store.segmentFile(clientId, it, SegmentFormat.FLAC).isFile })
    }

    /**
     * **失败之后不再开新段**（省下的一定要重发的字节），但**在飞的那几段继续跑完**。
     *
     * 这一条用并发度 1 才看得出来：第 1 段一失败，后面三段根本不该被发出去
     * （水位卡在 0，下一轮从第 1 段重来 —— 现在多发的那几段全是白传）。
     * 而"在飞的不取消"由上面那条用例证明（3 段同时起飞、一个失败、另外两个照样完成）。
     */
    @Test
    fun `失败之后不再开新段（没发出去的记在 skipped 里）`() {
        val shipper = object : SegmentShipper {
            val attempts = Collections.synchronizedList(mutableListOf<Int>())
            override suspend fun ship(seg: DraftSegment): SegmentShipOutcome {
                attempts += seg.seq
                return SegmentShipOutcome.Failed(
                    seg.seq, SyncFailure.UNREACHABLE, "第 ${seg.seq} 段（假服务端）没发出去",
                    retryable = true)
            }
        }
        val round = runBlocking(Dispatchers.Default) {
            SegmentUploadPlan.run(
                segments = (1..4).map { seg(it) }, concurrency = 1, shipper = shipper)
        }
        assertEquals("只发了第一段（其余的在排队时就被拦下）", 1, shipper.attempts.size)
        assertEquals("成功 0", 0, round.succeeded)
        assertEquals("失败 1", 1, round.failed)
        assertEquals("剩下 3 段记为'因为前面失败没发'", 3, round.skippedSeqs.size)
        assertEquals("水位原地不动", 0, round.acknowledged)
        assertFalse("这一轮不算成功", round.allSucceeded)
    }

    /**
     * **并发落盘不许抹掉录音侧刚追加的那一段**。
     *
     * 为什么这条跟并行有关：并发让"每段成功一次落盘"在几秒内连发三次，而录音那一侧
     * （`MeetingRecordSession`）在**同一时间**完全可能刚封存了新的一段（它每次都先重读磁盘）。
     * 同步这一侧如果拿"本轮开始时的快照"去 save，就会把刚追加的那一段从清单里抹掉 ——
     * 音频文件还在，但清单里没有它，那一段就**永远不会被补传**（真的丢内容，不是显示问题）。
     *
     * 时间线：三段各 50ms；第 1 段**刚开始上传**时，录音侧"封存"了第 4 段（真写进清单）。
     * 期望：这一轮结束之后第 4 段仍在清单里；水位只到 3（第 4 段还没传）；
     * 而且因为"本地最大段序号 4 > 服务端 3 段"，对账如实判不符、**不收尾**。
     */
    @Test
    fun `并发落盘不许抹掉录音侧刚追加的段`() {
        val store = store()
        seed(store, seqs = listOf(1, 2, 3))
        val t = DelayTransport(
            delayOf = mapOf(1 to 50L, 2 to 50L, 3 to 50L),
            onAttempt = { seq ->
                if (seq == 1) {
                    // 演录音侧：先落音频、再写清单（与 `MeetingRecordSession.seal` 同一个顺序）
                    val cur = store.load(clientId)!!
                    File(store.dirOf(clientId), MeetingDraftStore.segmentName(4, SegmentFormat.FLAC))
                        .writeBytes(ByteArray(32) { 2 })
                    assertTrue(store.save(cur.copy(segments = cur.segments + seg(4))))
                }
            },
        )

        val r = runBlocking(Dispatchers.Default) {
            OfflineMeetingSync.run(transport = t, store = store, scope = SyncScope.FINALIZE)
        }

        val d = store.load(clientId)!!
        assertTrue("刚追加的第 4 段必须还在清单里（并发落盘不许抹掉它）：${d.segments.map { it.seq }}",
            d.segments.any { it.seq == 4 })
        assertEquals("水位仍然只到 3（第 4 段还没传）", 3, d.acknowledgedSegments)
        assertFalse("第 4 段没传 → 不许标已上传", d.uploaded)
        assertTrue("段没全到齐，一次 stop 都不许有：${t.stops}", t.stops.isEmpty())
        assertEquals("对账如实判不符（本地到 4、服务端 3）",
            OfflinePhase.FAILED, r.phase)
        assertTrue("原因要说清段数不符：${r.note}", r.note.contains("只认下 3 段"))
    }

    /**
     * **单段超时只取消它自己**：每段各有各的单段预算（[ApiClient.UPLOAD_CALL_TIMEOUT_S]
     * 在客户端这一层的第二次保证），一段卡住不许把整轮拖住，也不许连累别的段。
     *
     * 这里把预算缩到 1 秒、让第 2 段假睡 1.5 秒：它自己超时失败，1 和 3 照常完成。
     */
    @Test
    fun `单段超时只取消它自己：其他段照常完成 水位不被推过缺口`() {
        val shipper = FakeShipper(mapOf(1 to 10L, 2 to 1_500L, 3 to 10L))
        val round = runBlocking(Dispatchers.Default) {
            SegmentUploadPlan.run(
                segments = listOf(seg(1), seg(2), seg(3)),
                concurrency = 3,
                perSegmentBudgetMs = 1_000L,
                shipper = shipper,
            )
        }
        assertEquals("其他两段照常完成", 2, round.succeeded)
        assertEquals("只有卡住的那段失败", 1, round.failed)
        assertEquals("超时的就是第 2 段", 2, round.firstFailure!!.seq)
        assertTrue("原因要说清单段预算：${round.firstFailure!!.note}",
            round.firstFailure!!.note.contains("单段最多等 1 秒"))
        assertEquals("水位停在缺口前（1）", 1, round.acknowledged)
    }

    // ---------------------------------------------------------------- ③ 复用同一个客户端

    /**
     * **并发要复用同一个 OkHttp 实例**（源码级 + 实例级）。
     *
     * 为什么这条必须钉：并发上传最诱人的错法是"每段 new 一个 client" —— 那等于每段
     * 一个新连接池 + 一套线程池（真的会泄漏），而且连接**不能复用**（每次都要重新握手，
     * 更慢）。生产代码只建一次 `ApiClient.createForUpload(baseUrl)`，而它按 baseUrl 缓存。
     */
    @Test
    fun `传段复用同一个上传客户端（不是每段新建）`() {
        val mic = source("src/main/java/com/echo/contact/service/MeetingMicService.kt")
        val transport = source("src/main/java/com/echo/contact/service/OfflineMeetingTransport.kt")
        val plan = source("src/main/java/com/echo/contact/service/SegmentUploadPlan.kt")
        val sync = source("src/main/java/com/echo/contact/service/OfflineMeetingSync.kt")

        assertEquals("上传专用客户端在整条路上只建一次（建在每段里 = 每次新连接池）",
            1, occurrencesOf(codeLinesOf(mic), "ApiClient.createForUpload(baseUrl)"))
        assertTrue("传段那个请求只走 uploadApi", transport.contains("uploadApi.meetingSegmentWithId("))

        assertTrue("并发必须是 Semaphore 限流（不是 async 全开）",
            plan.contains("Semaphore(") && plan.contains("withPermit"))
        assertTrue("水位只能由连续前缀算出来", plan.contains("contiguousAcknowledged("))
        assertFalse("旧的 max(水位, seq) 不许再用来推水位（那会把水位推过缺口）",
            sync.contains("acknowledgedAfterUpload("))

        // 实例级：同一个地址两次拿到的是**同一个**对象（连接池复用）；传段/普通仍是两个实例
        val base = "http://10.0.0.1:8970"
        assertSame("同一个地址必须复用同一个客户端（否则每段一个新连接池）",
            ApiClient.createForUpload(base), ApiClient.createForUpload(base))
        assertNotSame("传段那一档必须与普通接口分开（超时不同）",
            ApiClient.create(base), ApiClient.createForUpload(base))
        assertTrue("单段总预算要比'单次无进展'的上限长（慢链路要能过）",
            ApiClient.UPLOAD_CALL_TIMEOUT_S > ApiClient.UPLOAD_TIMEOUT_S)
    }

    // ---------------------------------------------------------------- 源码定位

    /** 读源码文本（**只读**；源码级判据用）。候选路径都列出来，找不到就报出找过哪些。 */
    private fun source(relative: String): String {
        val candidates = listOf(File(relative), File("app/$relative"), File("../$relative"))
        val f = candidates.firstOrNull { it.isFile }
        assertTrue("找不到源码 $relative（找过：${candidates.joinToString()}）", f != null)
        return f!!.readText()
    }

    private fun occurrencesOf(text: String, needle: String): Int {
        var n = 0
        var i = text.indexOf(needle)
        while (i >= 0) {
            n++
            i = text.indexOf(needle, i + needle.length)
        }
        return n
    }

    /**
     * 只看**代码行**（丢掉 `//` / `*` 开头的注释行）。
     *
     * 为什么源码级判据要这么数：注释里本来就会解释"这里为什么只建一次客户端"，
     * 按整段文本去数会把注释里的那次提到也算进去（这条用例第一次跑就是这样红的）。
     */
    private fun codeLinesOf(text: String): String =
        text.lines().filterNot { it.trim().startsWith("//") || it.trim().startsWith("*") }
            .joinToString("\n")
}
