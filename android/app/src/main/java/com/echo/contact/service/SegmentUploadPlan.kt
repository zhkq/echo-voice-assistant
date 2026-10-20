package com.echo.contact.service

import com.echo.contact.data.ApiClient
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withTimeout
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * **一段音频的搬运**（读本地文件 + 发一次 `POST /api/meeting/segment`）。
 *
 * 为什么把它做成一个可注入的接口（而不是让 [SegmentUploadPlan] 直接调 transport）：
 * 并发这件事最容易错的地方是**调度**（谁先跑、几个同时在跑、一个失败会不会拖死别人），
 * 而"这一段的字节怎么送到电脑上"是另一件事。拆开之后，前面那件事能在 JVM 里用
 * 一个**假的搬运实现**逐条钉住（每段多久、哪段失败、哪段慢，全由用例说了算），
 * 真机上也不用为了验"到底并行没有"去抓包。
 *
 * 真实现就是 `OfflineMeetingSync.syncOne` 里那个 lambda（读文件 → `transport.uploadSegment`）。
 */
fun interface SegmentShipper {
    suspend fun ship(seg: DraftSegment): SegmentShipOutcome
}

/** [SegmentShipper.ship] 的结局（**只有补传的语义**，没有 HTTP/文件细节）。 */
sealed interface SegmentShipOutcome {

    data class Ok(
        val seq: Int,
        /** 服务端回过的会议 id（> 0 时记进清单）。 */
        val serverMeetingId: Int = 0,
        /** 服务端回的**累计段数**（`null` = 它没说，老服务端会这样）。 */
        val serverCount: Int? = null,
    ) : SegmentShipOutcome

    data class Failed(
        val seq: Int,
        val failure: SyncFailure,
        val note: String,
        val retryable: Boolean,
    ) : SegmentShipOutcome
}

/** 一段失败（[SegmentUploadRound] 里的一项）。 */
data class SegmentUploadFailure(
    val seq: Int,
    val failure: SyncFailure,
    val note: String,
    val retryable: Boolean,
)

/**
 * 一轮"把这几段传上去"的结论。
 *
 * ⚠️ [acknowledged] **不是** [succeeded]：它是**连续前缀**的水位（见
 * [contiguousAcknowledged]）。并发允许 `seq=3` 先到、`seq=2` 还在飞，但水位
 * **绝不能**因此越过 2 —— 否则下一轮会以为 2 已经传过、把它跳过，服务端那份会议
 * 就永远缺一段（转写里那句话就没了，而且对账永远过不了）。
 */
data class SegmentUploadRound(
    /** 这一轮真的被服务端收下的段（按 `seq` 升序）。 */
    val okSeqs: List<Int> = emptyList(),
    /** 这一轮失败的段（按 `seq` 升序；多段可以同时失败）。 */
    val failures: List<SegmentUploadFailure> = emptyList(),
    /** 这一轮之后**连续**确认到的段序号（水位；只会前进）。 */
    val acknowledged: Int = 0,
    /**
     * 因为"前面已经有一段失败"而**没发出去**的段（按 `seq` 升序）。
     *
     * 为什么要有它：失败之后还在飞的那几段**继续跑完**（它们已经在传了，取消就是白传），
     * 而**还没开始**的段不再开新的 —— 水位卡在缺口处，这一轮之后无论再传多少段都得
     * 从缺口重来（服务端虽然幂等，但那些字节是白传的）。省下来的是用户的时间。
     */
    val skippedSeqs: List<Int> = emptyList(),
) {
    /** 这一轮成功了几段。 */
    val succeeded: Int get() = okSeqs.size

    /** 这一轮失败了几段。 */
    val failed: Int get() = failures.size

    /** 第一处失败（按 `seq`；下一轮就从它开始补）。 */
    val firstFailure: SegmentUploadFailure? get() = failures.minByOrNull { it.seq }

    /** 计划里的段**全都**成了（没有失败、也没有被跳过的）。 */
    val allSucceeded: Boolean get() = failures.isEmpty() && skippedSeqs.isEmpty()
}

/**
 * **受控并发**的段上传调度（纯逻辑：不碰文件、不碰 HTTP，只决定"几段同时在飞"）。
 *
 * ## 为什么要有它（用户 2026-10-20：*"手机补传录音速度太慢了"*）
 *
 * 实测那一场：10 分钟一段的 FLAC ≈ 10 MB，单连接只有 **30–160 KB/s**，于是
 * 一段要几分钟、5 段串行就是十几分钟。瓶颈**不在段大小**（已经是 FLAC），而在
 * **单连接的吞吐** —— 每多一条同时在飞的连接，就能把链路剩下的余量吃进来。
 * 所以这里做的是"最多 N 段同时在飞"，而不是"把段切小 / 换格式"（那些都改不了单连接上限）。
 *
 * ## 三条铁律一条都没放松（并发只改"到达顺序"，不改"确认顺序"）
 *
 *  1. **不重传**：进入这里的段已经是 `seq > 水位` 的那几段（`segmentsToUploadOf`）；
 *     水位由 [contiguousAcknowledged] **只按连续前缀**推进 —— `seq=3` 成功而 `seq=2`
 *     还在飞时，水位**停在 2 之前**，下一轮仍然从 2 补。
 *  2. **不乱序**：到达可以是乱的（服务端按 `seq` 落盘、且同一段重复传是幂等的），
 *     但**确认**（水位）永远是顺序的、单调的。
 *  3. **失败只取消它自己**：一段失败**不许**把别的段一起带走（那些已经在传了，
 *     取消就是白传一遍）；结果是"这一轮成功 K 段 / 失败 M 段"，调用方据此在缺口处停下。
 *
 * ## 为什么并发度是 3（[DEFAULT_CONCURRENCY]，上限也是 3）
 *
 *  * 每段是**整段读进内存**再发（`file.readBytes()`）：10 分钟 FLAC ≈ 10 MB，
 *    3 路同时在飞 ≈ **30 MB** 瞬时占用，手机上可接受；再多就只是给内存和乱序加码，
 *    而单连接 30–160 KB/s 那种瓶颈在 3 路时已经吃得差不多了。
 *  * 5 段的会议：3 路 = 2 波（3 + 2），2 路 = 3 波 —— 3 路少一波。
 *  * 短会（1–2 段）不受影响：并发数由"实际要传的段数"决定，不会空等。
 *  * 上限刻意锁在 3（[MAX_CONCURRENCY]）：它不是为了"更快"，而是为了让
 *    **内存与乱序**有上界；配置成 1 就退回串行（排障/对照用）。
 *
 * ## 并发是**跑在调用方的上下文**里的
 *
 * 这里不自己切线程池（`launch` 继承调用方的 dispatcher）：生产那条路
 * （`MeetingMicService.runOfflineSync` → `scope` = `Dispatchers.IO`）本来就是多线程的；
 * 而单测里用默认的 `runBlocking` 时，假 transport 不挂起，几段会按顺序跑完 ——
 * 这让既有的"顺序"判据仍然确定，而"真的并行"由用例用 `Dispatchers.Default` +
 * 会挂起的假 transport 单独证明。
 */
object SegmentUploadPlan {

    /**
     * 默认并发段数（见类注释"为什么并发度是 3"）。
     *
     * 为什么把它做成常量 + 参数（而不是写死在循环里）：真机哪天链路的瓶颈变了
     * （例如中继按连接限速变严），要能只改一处就调到 2 或 1；用例也要能钉住"改了
     * 配置并发度真的跟着变"。
     */
    const val DEFAULT_CONCURRENCY = 3

    /** 并发上限（内存与乱序的上界；见类注释）。 */
    const val MAX_CONCURRENCY = 3

    /** 并发下限（1 = 串行；排障/对照用）。 */
    const val MIN_CONCURRENCY = 1

    /** 把配置值夹进 [MIN_CONCURRENCY]..[MAX_CONCURRENCY]（0/99 这类配置不许把内存打爆）。 */
    fun normalizedConcurrency(requested: Int): Int =
        requested.coerceIn(MIN_CONCURRENCY, MAX_CONCURRENCY)

    /**
     * 把 [segments] 用**受控并发**传上去。
     *
     * @param acknowledged 进入这一轮之前的水位（`seq <= 它` 的段已经在服务端了）。
     * @param concurrency 同时在飞的段数上限（[normalizedConcurrency] 会夹到 1..3）。
     * @param perSegmentBudgetMs **单段**的总预算（毫秒）。每一段**各自计时**：这是
     *   `ApiClient.UPLOAD_CALL_TIMEOUT_S` 在客户端这一层的第二次保证 —— OkHttp 的
     *   `callTimeout` 本来就是按**每个 call** 计的（并行时各算各的），但那一层要是
     *   哪天被人换掉了，"一段卡住"就会把整轮拖到无限期；这里再包一层，卡住的只是它自己。
     *   `<= 0` = 不设这一层（只用传输层自己的超时）。
     * @param onSucceeded 每段**被服务端收下**之后回调 `(该段结果, 新的连续水位)`。
     *   调用方在这里落盘清单与刷新界面 —— 它可能从**多个**上传协程里被调到，
     *   所以实现里要自己串起来（`OfflineMeetingSync` 用一把 `Mutex`）。
     */
    suspend fun run(
        segments: List<DraftSegment>,
        acknowledged: Int = 0,
        concurrency: Int = DEFAULT_CONCURRENCY,
        perSegmentBudgetMs: Long = ApiClient.UPLOAD_CALL_TIMEOUT_S * 1000L,
        shipper: SegmentShipper,
        onSucceeded: (suspend (SegmentShipOutcome.Ok, Int) -> Unit)? = null,
    ): SegmentUploadRound {
        val planned = segments.sortedBy { it.seq }
        if (planned.isEmpty()) return SegmentUploadRound(acknowledged = acknowledged)

        val plannedSeqs = planned.map { it.seq }
        val permits = Semaphore(normalizedConcurrency(concurrency))
        // 已成功的段（只有连续前缀会推水位；见 contiguousAcknowledged）
        val done = LinkedHashSet<Int>()
        val doneLock = Any()
        val okSeqs = java.util.Collections.synchronizedList(ArrayList<Int>())
        val failures = java.util.Collections.synchronizedList(ArrayList<SegmentUploadFailure>())
        val skipped = java.util.Collections.synchronizedList(ArrayList<Int>())
        val watermark = AtomicInteger(acknowledged)
        /** 已经出现过一处失败（之后**不再开新段**，但已经在飞的不取消）。 */
        val sawFailure = AtomicBoolean(false)

        coroutineScope {
            planned.forEach { seg ->
                launch {
                    // 受控并发：并发度由 Semaphore 兜住，**不是**把 async 全开
                    // （全开的话 5 段会同时读 5×10 MB 进内存，链路上也只是互相挤）。
                    permits.withPermit {
                        // 排队等许可的这一段时间里，前面可能已经有一段失败了：
                        // 这时**不再发新段**（省下的字节下一轮一定要重发，因为水位卡在缺口）。
                        // ⚠️ 已经在飞的段不取消 —— 取消就是白传一遍。
                        if (sawFailure.get()) {
                            skipped += seg.seq
                            return@withPermit
                        }
                        when (val out = shipSafely(seg, perSegmentBudgetMs, shipper)) {
                            is SegmentShipOutcome.Ok -> {
                                val next = synchronized(doneLock) {
                                    done += seg.seq
                                    // ⚠️ `updateAndGet`（不是 `getAndUpdate`）：要的是**新**水位 ——
                                    // 用旧的会让每一轮的水位都落后一段（`acknowledgedSegments`
                                    // 停在上一段，下一轮就重复传那一段）。
                                    watermark.updateAndGet { current ->
                                        contiguousAcknowledged(current, plannedSeqs, done)
                                    }
                                }
                                okSeqs += seg.seq
                                onSucceeded?.invoke(out, next)
                            }
                            is SegmentShipOutcome.Failed -> {
                                failures += SegmentUploadFailure(
                                    seq = seg.seq, failure = out.failure,
                                    note = out.note, retryable = out.retryable)
                                sawFailure.set(true)
                            }
                        }
                    }
                }
            }
        }

        return SegmentUploadRound(
            okSeqs = okSeqs.sorted(),
            failures = failures.sortedBy { it.seq },
            acknowledged = watermark.get(),
            skippedSeqs = skipped.sorted(),
        )
    }

    /**
     * 一段的搬运，**这一段的异常与超时都只落在它自己身上**。
     *
     * 为什么异常也要在这里吞掉：`launch` 里抛出去的异常会取消**父作用域**
     * —— 那正好是"一段失败把其他段一起拖死"，是本次要求明确不许的形状。
     * 而 `CancellationException` 要原样抛（那是整轮被取消，不是某一段失败）。
     */
    private suspend fun shipSafely(
        seg: DraftSegment,
        budgetMs: Long,
        shipper: SegmentShipper,
    ): SegmentShipOutcome = try {
        if (budgetMs > 0) withTimeout(budgetMs) { shipper.ship(seg) } else shipper.ship(seg)
    } catch (e: TimeoutCancellationException) {
        // 注意顺序：TimeoutCancellationException 是 CancellationException 的子类，
        // 必须在它**之前**接住 —— 否则"一段超时"会被当成"整轮被取消"重新抛出去。
        SegmentShipOutcome.Failed(
            seq = seg.seq,
            failure = SyncFailure.UNREACHABLE,
            note = "第 ${seg.seq} 段上传超时 —— 这段太大 / 网太慢（单段最多等 " +
                "${budgetMs / 1000} 秒还没传完）。重试中：已传过的段不会重传，" +
                "下次从第 ${seg.seq} 段接着传",
            retryable = true,
        )
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        SegmentShipOutcome.Failed(
            seq = seg.seq,
            failure = SyncFailure.UNREACHABLE,
            note = "第 ${seg.seq} 段上传时抛了异常：${e.javaClass.simpleName}" +
                (e.message?.let { "（$it）" } ?: ""),
            retryable = true,
        )
    }
}

/**
 * **连续前缀**上的确认水位（纯函数）。
 *
 * 从 [acknowledged] 开始往上数：只有"清单里下一段的序号正好是水位 + 1，而且它真的成功了"
 * 才继续推进，**遇到第一个缺口立刻停**。它同时处理两种洞：
 *  * **还没成功的段**（并发下 `seq=3` 先到、`seq=2` 还在飞）；
 *  * **清单里本来就没有的段**（升级上来的老清单可能只有 7、8 两段，水位是 6）。
 *
 * 为什么不能用 `max(水位, seq)`：并发下 `seq=3` 成功而 `seq=2` 失败时，`max` 会把水位
 * 推到 3 —— 下一轮就**跳过第 2 段**，服务端那份会议永远缺一段（转写里少一句话、
 * 对账永远不符），而用户看到的是"已经传完了"。这正是本次要防的静默错法。
 */
fun contiguousAcknowledged(
    acknowledged: Int,
    plannedSeqs: List<Int>,
    doneSeqs: Set<Int>,
): Int {
    var watermark = acknowledged
    for (seq in plannedSeqs.sorted()) {
        if (seq <= watermark) continue
        // 清单里跳过了一段（老清单）或者这一段还没成功：水位就停在这之前。
        if (seq != watermark + 1) break
        if (seq !in doneSeqs) break
        watermark = seq
    }
    return watermark
}
