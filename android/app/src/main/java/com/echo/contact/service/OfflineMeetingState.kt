package com.echo.contact.service

/**
 * **离线录音的状态机**（纯逻辑，JVM 逐档可测）。
 *
 * 用户 2026-10-16 的需求：*"手机如果连不上 echo 也可以进入离线状态，离线状态可以开始录音，
 * 结束录音，录音文件缓存在 app，网络通了就可以传上去"*。
 *
 * 这里把那条路上**所有会静默出错的判断**都抽成纯函数（状态迁移、退避、选段、对账、
 * 抢占判定、文案），因为它们的错法都是"看着没事"：
 *  * 状态说错 → 用户以为已经传上去了（"绝不把没传上去说成已上传"）；
 *  * 顺序错 → 转写里的话是乱的；
 *  * 退避没有上限 → 断网一晚上就把电池和流量烧光；
 *  * 抢占判定错 → **把电脑正在录的那场会打断**（用户明确说不要）。
 */

// --------------------------------------------------------------------------- 状态

/**
 * 离线录音的状态。
 *
 * 迁移（`OfflineMeetingStateTest` 逐条钉住）：
 * ```
 *  (没有清单) ──开始录音（ECHO 不可达）──▶ [RECORDING_LOCAL]
 *  [RECORDING_LOCAL] ──结束录音──▶ [WAITING_UPLOAD]
 *  [WAITING_UPLOAD] ──「现在同步」/ 自动重试──▶ [UPLOADING]
 *  [UPLOADING] ──对账通过 **且 stop 成功**──▶ [DONE]
 *  [UPLOADING] ──对账通过但 stop 失败/超时──▶ [WAITING_UPLOAD]（退避后再试；音频留着）
 *  [UPLOADING] ──ECHO 不可达──▶ [WAITING_UPLOAD]（退避后再试；音频留着）
 *  [UPLOADING] ──ECHO 正录另一场──▶ [WAITING_MEETING_END]（**不抢占**）
 *  [UPLOADING] ──对账不符/服务端拒绝──▶ [FAILED]（**不删缓存**、如实报原因）
 *  [FAILED] ──「现在同步」──▶ [UPLOADING]
 * ```
 * ⚠️ **DONE 的前一步必须是 `POST /api/meeting/stop`**（2026-10-16 补的收尾步骤）：
 * **没有 stop 就没有转写** —— 服务端只在 `stop_meeting()` 里把这场会收尾并发起转写，
 * 只做 start + segment 的话，音频躺在电脑上**永远不会出文字**；
 * 而且"手机麦"这场会在服务端一直占着**唯一**那条录音槽，之后电脑/手机都开不了新会。
 * 所以本文件里的 [StopOutcome] 与 `OfflineMeetingSync` 里的 stop 那一步不是"清理"，是**必需的收尾**。
 * ⚠️ **没有**从任何一档"自动删掉本地音频"的迁移：清理是**另一条**、有界保留的策略
 * （见 [OfflineMeetingRetention]：只有 DONE 之后、且超过保留天数才允许清）。
 */
enum class OfflinePhase {
    /** 没有离线录音、也没有待补传的段。 */
    IDLE,

    /** 在手机上离线录音（**完全本地**：不依赖服务端，也不需要服务端在场）。 */
    RECORDING_LOCAL,

    /** 录完了、还躺在手机上等联网（失败原因在 [OfflineSyncState.note] 里）。 */
    WAITING_UPLOAD,

    /** 正在补传（按序、可续传、传完要对账）。 */
    UPLOADING,

    /**
     * ECHO 不可达。
     *
     * 为什么与 [WAITING_UPLOAD] 分开：用户要看的正是"是网没通，还是服务端拒了"
     * —— 说错原因会让他去查错的地方（需求里点名要求"真实原因"）。
     */
    UNREACHABLE,

    /** ECHO **正在录另一场会**（例如电脑那边）：等它录完再传，**绝不抢占**。 */
    WAITING_MEETING_END,

    /** 对账不符或服务端明确拒绝：**不删缓存**，如实报出来，等用户/下次重试。 */
    FAILED,

    /**
     * 补传**全部完成**（对账通过 **且** `stop` 成功 —— 服务端已经收到收尾并开始转写；
     * 本地音频**仍然留着**，等有界保留策略到期才清）。
     */
    DONE,
}

// --------------------------------------------------------------------------- 可达性

/** ECHO 到底在不在。 */
enum class Reachability {
    /** 通了（拿到了 2xx）。 */
    REACHABLE,

    /** 连不上 / 超时 / 返回非 2xx。 */
    UNREACHABLE,
}

/**
 * 探活结果 → 可达性（**纯函数**）。
 *
 * [code] 为 null = 请求抛异常（超时、连接被拒、DNS 失败）—— 与"返回了非 2xx"一样，
 * 都算**不可达**：用户要的语义是"连不上 echo 也可以进入离线状态"，
 * 而"连得上但回错码"在录音这条路上没有任何区别（都开不了那场会）。
 */
fun reachabilityOf(code: Int?): Reachability =
    if (code != null && code in 200..299) Reachability.REACHABLE else Reachability.UNREACHABLE

/** 顶层别名，读起来像一句话。 */
internal fun isReachable(code: Int?): Boolean = reachabilityOf(code) == Reachability.REACHABLE

/**
 * 「ECHO 正忙（在录另一场）」—— 与"不可达"是**两件不同的事**。
 *
 * 判据：服务端明确说了它正在录（`meeting.active == true`），且那场**不是**我们这一份
 * 清单对应的会（`localRecording == false`）。这时**不许抢占**：
 * 用户的原话是"不要抢占电脑正在录的会议"，而抢占的后果是把别人正在录的会打断
 * （ECHO 会把它记成 `interrupted`，音频虽在但录音当场断了 —— 2026-09-23 真出过这次事故）。
 */
fun echoBusyElsewhere(echoRecording: Boolean, localRecording: Boolean): Boolean =
    echoRecording && !localRecording

/** 服务端侧的错误分类（**必须分得开**：它们该说的话完全不同）。 */
enum class SyncFailure {
    /** 连不上 / 超时（网络还没通）。 */
    UNREACHABLE,

    /** 服务端**拒绝**（4xx：没配对、不支持这个契约、参数不对）。 */
    REJECTED,

    /** 服务端**正忙**（409 / 503：多半是在录另一场，或转写占着）。 */
    SERVER_BUSY,

    /** 服务端 5xx（它自己出错了）：可以重试，但**不说成"网络不通"**。 */
    SERVER_ERROR,
}

/**
 * HTTP 状态码 → 错误分类（**纯函数**，`null` = 请求抛异常）。
 *
 * 分类的意义全在文案与重试策略上：
 *  * [REJECTED]（4xx）：**重试也不会好**（契约没对齐），要如实说"服务端拒绝了"；
 *  * [SERVER_BUSY]：不是错，是"等一下"，**不抢占**；
 *  * [UNREACHABLE]：网络问题，退避重试；
 *  * [SERVER_ERROR]：它自己坏，退避重试。
 */
fun failureOf(code: Int?): SyncFailure = when {
    code == null -> SyncFailure.UNREACHABLE
    code in 200..299 -> SyncFailure.UNREACHABLE      // 调用方不该走到这（成功不是失败）
    code in 400..499 -> if (code == 409) SyncFailure.SERVER_BUSY else SyncFailure.REJECTED
    code in 500..599 -> if (code == 503) SyncFailure.SERVER_BUSY else SyncFailure.SERVER_ERROR
    else -> SyncFailure.UNREACHABLE
}

/** 这类失败还值不值得再试（纯函数）。 */
fun failureRetryable(f: SyncFailure): Boolean = f != SyncFailure.REJECTED

/** 「开始那场会」的结局。 */
sealed interface StartOutcome {
    data class Ok(val meetingId: Int) : StartOutcome
    data class Busy(val note: String) : StartOutcome
    data class Failed(val note: String, val retryable: Boolean) : StartOutcome
}

/** 「传一段」的结局。 */
sealed interface SegmentOutcome {
    /** 服务端收下了（[serverMeetingId] > 0 时顺便把会议 id 记下来）。 */
    data class Ok(
        val serverMeetingId: Int = 0,
        /** 服务端回的**段数**（`null` = 它没说 —— 老服务端会这样）。 */
        val serverCount: Int? = null,
        /** 服务端明确说"这一段是它已经有的"（幂等命中，等同于收下）。 */
        val duplicate: Boolean = false,
    ) : SegmentOutcome

    data class Failed(val failure: SyncFailure, val note: String, val retryable: Boolean) :
        SegmentOutcome
}

/** 「问一下这一场服务端手上有几段」的结局（对账用）。 */
sealed interface CountOutcome {
    data class Ok(val count: Int) : CountOutcome
    data class Failed(val failure: SyncFailure, val note: String) : CountOutcome
}

/**
 * 「让服务端收尾这场会」的结局（**补传的最后一步**）。
 *
 * 为什么它必须是补传流程里的一档，而不是"顺手调一下、成没成都无所谓"：
 * **没有 stop 就没有转写** —— `app/meeting.py::stop_meeting()` 才是把这场会落盘、
 * 写 `meta`、并发起转写的那一个动作；只 start + segment 的话，音频在电脑上躺着，
 * 用户**永远等不到文字**。而且"手机麦"这场会在服务端占着唯一那条录音槽，
 * 不收尾就会让之后任何新会议都开不起来。
 */
sealed interface StopOutcome {
    /**
     * 服务端收下了收尾（**已经开始转写**）。
     *
     * [alreadyStopped] = 服务端说"没有进行中的会议"（幂等命中：这场之前就停过了）
     * —— **也算成功**。卡死在这里是最糟的：用户看到"差最后一步"却永远不动，
     * 而服务端那边其实一切都好（只是我们又问了一次）。
     */
    data class Ok(val alreadyStopped: Boolean = false, val note: String = "") : StopOutcome

    data class Failed(
        val failure: SyncFailure,
        val note: String,
        val retryable: Boolean,
        /**
         * 音频**已经全部传上去了**，只是"收尾"这一步没成。
         *
         * 为什么要把它跟段失败分开：段没传完时说"传完了、就差收尾"是谎报。
         * 这一档的文案必须点出"**还没有开始转写**"（那才是用户真正关心的结果）。
         */
        val alreadyUploadedHint: Boolean = false,
    ) : StopOutcome
}

/**
 * 服务端那句话是不是「这场会早就停过了」（**纯函数**）。
 *
 * 幂等的判据只能是**服务端原话**：`POST /api/meeting/stop` 在"没有进行中的会议"时
 * 回 `{ok: false, message: "没有进行中的会议"}`（`app/meeting.py::stop_meeting` 的第一行），
 * 而那**不是失败** —— 它正是"我们已经停过了"。
 *
 * ⚠️ 只在 [SyncFailure.REJECTED] 那一档上用它：`ok=false + 4xx` 才是"服务端明确说没得停"；
 * 别的失败（连不上、5xx）**绝不能**被这句话蒙过去 —— 那会把"没传上去"说成"已经好了"。
 *
 * ⚠️ 这里**只**认"没有进行中的会议"这一类原话，**绝不**认服务端"clientId 不符"的拒绝话术
 * （那个在 [looksClientIdMismatch] 里，走**可重试的失败**，不是成功）。
 * 两者混起来 = 把一场根本没传完的录音标成"已完成"✗（见那条的说明）。
 */
fun looksAlreadyStopped(message: String): Boolean {
    val m = message.trim()
    if (m.isEmpty()) return false
    return m.contains("没有进行中的会议") || m.contains("没有正在进行的会议") ||
        m.contains("当前没有会议") || m.contains("未在录音") || m.contains("没有在录音")
}

/**
 * 服务端那句话是不是「**这场不是 clientId 那一场**」的拒绝（**纯函数**）。
 *
 * 为什么它与 [looksAlreadyStopped] 必须是**两个**判据 —— 它们都在 `ok=false` 这一档上，
 * 但后果**正好相反**：
 *  * "没有进行中的会议" = 这场之前就停过了 = **成功**（幂等命中，卡住才是最糟的）；
 *  * "不是 clientId 那一场" = 服务端正在录**别的**会，**没有**收尾我们这一场 = **失败**，
 *    而且**不许**标 DONE、**不许**删缓存 —— 把它当成功就等于把一场**根本没传完/没收尾**
 *    的录音说成"已完成、已开始转写"（用户以为出了文字，其实那一场还占着录音槽）。
 *
 * 判据只认服务端那几句原话（`app/meeting.py::stop_meeting` 的护栏分支）：
 *  * `这场会议不是 clientId=<id> 那一场（服务端正在录别的会），已拒绝收尾：不会停掉别人的会议`
 *    —— 认前半句（`不是 clientId=`）或后半句（`正在录别的会`）；
 *  * `会停掉别人的会议` —— 那句"已拒绝收尾"的尾巴。
 *
 * 刻意**不**认「拒绝」/「不允许」这种宽词：那些太宽（服务端以后拿它们说别的毛病时，
 * 我们会把原因说歪），而这里要的只是"别把它当成成功"。
 */
fun looksClientIdMismatch(message: String): Boolean {
    val m = message.trim()
    if (m.isEmpty()) return false
    return m.contains("不是 clientId=") || m.contains("正在录别的会") ||
        m.contains("会停掉别人的会议")
}

// --------------------------------------------------------------------------- 退避

/**
 * 退避重试的**参数**（纯函数 + 有上限）。
 *
 * 为什么必须有上限：断网可能是一整夜。没有上限（或上限很大）的重试会把电池与流量烧光，
 * 而音频本来就在手机上躺着 —— 早传半小时没有任何价值。
 */
object BackoffPolicy {
    /** 第一次失败后等多久。 */
    const val BASE_MS = 2_000L

    /** 倍率（2 = 每失败一次翻倍）。 */
    const val FACTOR = 2

    /** 间隔上限：5 分钟。 */
    const val MAX_MS = 300_000L

    /**
     * 试到第几次就**不再自动试**（只留「现在同步」按钮给用户）。
     *
     * 为什么不无限自动试：自动重试的**唯一**目的是"网络一恢复就自己传上去"，
     * 而网络恢复这件事已经被 [shouldRetryNow] 单独判了（可达性一变就重置计数）。
     * 一直试下去只会在服务端拒绝（4xx）这种**重试也没用**的情况下空转。
     */
    const val MAX_AUTO_ATTEMPTS = 8

    /**
     * 第 [attempt] 次失败之后该等多久（毫秒）。[attempt] 从 1 起。
     *
     * 上界夹在 [MAX_MS]：翻倍会很快超过 5 分钟，而"等 5 分钟"与"等 4 小时"
     * 对用户体验没有区别（用户能感知的只有「现在同步」这一下）。
     * ⚠️ 夹取必须发生在**每一次翻倍之前**（先夹再乘），否则最后一次会越过上限
     * —— 那样"上限"就不是上限了，而用户会在一个夜里被 4 分钟的等待拖长成几小时。
     */
    fun delayMs(attempt: Int): Long {
        if (attempt <= 0) return 0L
        var d = BASE_MS
        repeat(attempt - 1) {
            if (d >= MAX_MS) return MAX_MS
            d *= FACTOR
        }
        return if (d > MAX_MS) MAX_MS else d
    }

    /** 还要不要自动重试（纯函数）。 */
    fun shouldAutoRetry(attempts: Int): Boolean = attempts < MAX_AUTO_ATTEMPTS

    /** 下一次自动重试的时刻（不会自动重试时给 null —— **别给一个永远不成立的时刻**）。 */
    fun nextAttemptAt(nowMs: Long, attempts: Int): Long? =
        if (!shouldAutoRetry(attempts)) null else nowMs + delayMs(attempts + 1)
}

/**
 * 此刻该不该再自动试一次（**纯函数**）。
 *
 * 三个条件缺一不可：
 *  1. 还有没传完的段；
 *  2. 还没到"自动重试次数用尽"；
 *  3. **要么**退避时间到了，**要么**可达性刚刚从"不可达"变成"可达"。
 *
 * 第 3 条的后半句是需求"网络通了就可以传上去"的落点：网络一恢复就**立刻**试，
 * 不等那 5 分钟 —— 用户刚走进 WiFi 就该看到它自己开始传。而且这时计数要**重置**
 * （见 [attemptsAfterReachableChange]），否则用户在地铁里进出隧道几轮就把次数用光了。
 */
fun shouldRetryNow(
    nowMs: Long,
    attempts: Int,
    nextAtMs: Long?,
    wasUnreachable: Boolean,
    reachable: Boolean,
): Boolean {
    if (!BackoffPolicy.shouldAutoRetry(attempts)) return false
    if (wasUnreachable && reachable) return true
    return nextAtMs == null || nowMs >= nextAtMs
}

/**
 * 可达性变化之后的重试计数（纯函数）。
 *
 * 从"不可达"变成"可达" = **网络刚通**：计数清零，退避重新开始
 * （否则用户进出隧道几轮就把 [BackoffPolicy.MAX_AUTO_ATTEMPTS] 用光了，
 * 到了真有网的时候反而"不再自动试"，只能手点 —— 那就违背了需求）。
 */
fun attemptsAfterReachableChange(wasUnreachable: Boolean, reachable: Boolean, attempts: Int): Int =
    if (wasUnreachable && reachable) 0 else attempts

/** 顶层别名（设置页/用例直接读它）。 */
fun backoffDelayMs(attempt: Int): Long = BackoffPolicy.delayMs(attempt)

// --------------------------------------------------------------------------- 段选择与应用

/**
 * 补传时**实际要传**的那几段（**顺序 + 续传**，纯函数）。
 *
 * `seq <= acknowledgedSegments` 的一律跳过（那些已被服务端认下 → **可续传**）；
 * 其余按 `seq` 升序（音频顺序就是内容本身，跳过一段先传后面的 = 转写里的话是乱的）。
 */
fun segmentsToUpload(sequences: List<Int>, acknowledged: Int): List<Int> =
    sequences.filter { it > acknowledged }.sorted()

/**
 * **录音中途从联网转离线**时，清单的确认水位该怎么起（纯函数）。
 *
 * 为什么需要这一步（不写会静默地永远传不完）：接上离线那一刻，前 [sentOnline] 段
 * **已经在电脑上了** —— 但清单里没有它们（本地一个字节都没留）。若水位还是 0：
 *  * 本地最大段序号 = 1（只有转离线之后那一段）；
 *  * 服务端那一场已经是 [sentOnline] + 1 段；
 *  * 对账永远"不符" → 永远不敢标记已上传（用户看到的是"一直没传完"）。
 *
 * 把水位抬到 [sentOnline]（但**不超过**清单里已有的最大序号）之后，那个等式就成立了：
 * 本地最大序号 = 服务端的手上的段数。而多出来的那几段**不需要**本地有文件
 * （续传只查 `seq > acknowledged` 的段，它们恰好被跳过）。
 */
fun watermarkAfterHandoff(sentOnline: Int, localSeqs: List<Int>): Int {
    val maxLocal = localSeqs.maxOrNull() ?: 0
    return maxOf(0, minOf(sentOnline, maxOf(0, maxLocal - 1)))
}

/**
 * 一段传成功之后，本地的确认水位（单调递增；纯函数）。
 *
 * 为什么是"逐段推水位"而不是"传完一起推"：中途断网时水位停在最后一段，
 * 下次续传就从那里接着走（这就是"可续传"的全部实现）。
 */
fun acknowledgedAfterUpload(acknowledged: Int, seq: Int): Int = maxOf(acknowledged, seq)

// --------------------------------------------------------------------------- 对账

/** 对账的结论。 */
sealed interface ReconcileVerdict {
    /** 服务端手上的段与本地清单一致 → **只有到这一步才敢标记"已上传"**。 */
    data class Match(val segments: Int) : ReconcileVerdict

    /**
     * 不符：`sent - server` = **差多少段**（>0：服务端少收了；
     * <0：服务端有多出来的段，可能是**幂等没做好**把同一段存了两遍）。
     *
     * 不论哪种，本地音频**都不许删**（需求："宁可多留一会儿也别丢音频"）。
     */
    data class Mismatch(val sent: Int, val server: Int, val note: String) : ReconcileVerdict
}

/**
 * 对账（**纯函数**，需求里点名的"服务端段数不符 → 不删缓存、如实报"）。
 *
 * ## 比的是什么（这一条最容易写错，写错了会"永远不许说已上传"）
 *
 * 比的是**服务端手上这一场的段数**与**本地清单里最大的段序号**（段序号 1 起、连续）。
 *
 * 为什么**不**用"本地已确认水位"去比：水位是"客户端自己认为传到第几段"（1 起，所以
 * 它是个**序号**而不是个数）。续传时这一轮只发后几段，服务端**不会**报出一个包含
 * 前几段的累计数（它只知道自己手上这一场总共存了几段）—— 拿水位当个数去比，
 * 每一轮续传都会判"不符"，于是功能看着没坏、却永远不敢说"已上传"。
 *
 * ## 本地为什么要自查"有没有洞"
 *
 * [localSeqs] 里必须是一段**连续的区间**（1..n 或 a..b）。有洞就说明清单被截断过
 * （例如写到一半掉电），那种"段数对上了"是假的。
 * ⚠️ 归零那一段**只查本地真有的段**：录音中途从联网转离线时，前面那几段
 * （1..水位）只在电脑上、本地一个字节都没留 —— 它们**不是洞**，是"本来就在那边"。
 *
 * ⚠️ 这一版服务端**还没改**（另一条线在重建后端镜像），所以 [server] 为 null 时
 * **不假装通过**：如实说"没法对账"（见 `OfflineMeetingSync` 的说明）。
 */
fun reconcile(localSeqs: List<Int>, server: Int?): ReconcileVerdict {
    val mine = localSeqs.filter { it > 0 }.sorted()
    val localCount = mine.maxOrNull() ?: 0
    if (mine.isEmpty()) {
        return ReconcileVerdict.Mismatch(0, server ?: -1, "本地一段都没有，没什么可对账的")
    }
    if (server == null) {
        return ReconcileVerdict.Mismatch(localCount, -1,
            "服务端没回段数，没法对账（本地到第 $localCount 段）")
    }
    // 本地自查：**本地这一段必须连续**（1..n 或 a..b）。有洞 = 清单被截断过，
    // 那种"段数对上了"是假的。注意只看本地真有的段 —— 中途转离线时前面的段
    // 本来就在电脑上（本地没有），那不是洞。
    val missing = ((mine.first())..localCount).filter { it !in mine }
    if (missing.isNotEmpty()) {
        return ReconcileVerdict.Mismatch(localCount, server,
            "本地清单里缺了第 ${missing.joinToString("、")} 段（清单可能被截断过）：缓存没删，别当已上传")
    }
    if (server == localCount) return ReconcileVerdict.Match(localCount)
    val note = if (server < localCount) {
        "服务端只认下 $server 段，本地传到第 $localCount 段" +
            "（少 ${localCount - server} 段，缓存**没删**，下次接着传）"
    } else {
        "服务端那边有 $server 段，本地只到第 $localCount 段" +
            "（多 ${server - localCount} 段：可能同一段被存了两遍；缓存没删）"
    }
    return ReconcileVerdict.Mismatch(localCount, server, note)
}

// --------------------------------------------------------------------------- 开始/结束录音的决策

/** 开始录音时的三种落点。 */
sealed interface StartDecision {
    /** 联网那条路（与以前一致）。 */
    data object Online : StartDecision

    /**
     * 离线那条路。`why` 是**真实原因**（"ECHO 连不上（连接被拒）"），
     * 界面上那句"ECHO 连不上，已在手机上离线录音"就是它 + 这句套话拼出来的。
     */
    data class Offline(val why: String) : StartDecision

    /** 真的不能录（没配对、没麦权限）：这时才该报失败。 */
    data class Blocked(val why: String) : StartDecision
}

/**
 * 「这次开始录音走哪条路」的**唯一**判据（纯函数）。
 *
 * 需求原话：*"开始/结束录音时若 ECHO 不可达，不要让录音失败，而是进入离线录音"*。
 * 所以：
 *  * 没配对 → [StartDecision.Offline]（**不是**失败！配对只影响"能不能传"，不影响"能不能录"）；
 *  * 不可达 → [StartDecision.Offline]（并把原因原文带上）；
 *  * 可达 → [StartDecision.Online]（联网那条路**一个字都不改**）。
 *
 * ⚠️ **没配对也允许离线录音**是有意的：用户在会议室里、手机还没跟电脑配过对，
 * 这时"能录下来"仍然远好于"报个错、什么都不录"。传不上去的原因由补传那一侧如实说。
 */
fun startDecision(paired: Boolean, reachable: Boolean, why: String = ""): StartDecision = when {
    reachable -> StartDecision.Online
    paired -> StartDecision.Offline(why.ifBlank { "ECHO 连不上" })
    else -> StartDecision.Offline("还没配对（先把这台手机跟 ECHO 配一次，音频会先留在手机里）")
}

/** 结束录音时的两种落点。 */
sealed interface StopDecision {
    /** 本地就结束（离线那一场）：**不依赖服务端**。 */
    data object FinishLocally : StopDecision

    /** 顺带告诉服务端"这场录完了"（联网那一场）。 */
    data object NotifyServer : StopDecision
}

/**
 * 「结束录音」走哪条路（纯函数）。
 *
 * 离线那一场**绝不能**把"结束"押在服务端身上：断网时 `meetingStop` 必然失败，
 * 而用户的"结束"必须立刻生效（本地不再采音、清单落盘）。服务端那场会等补传时
 * 用同一个 `clientId` 重新建起来 —— 结束时间是清单里的 `ended_at`，不是补传的时间。
 */
fun stopDecision(recordingOffline: Boolean): StopDecision =
    if (recordingOffline) StopDecision.FinishLocally else StopDecision.NotifyServer

/** 封面统计：一份清单里"已确认 / 总共"（纯函数，界面那一行用）。 */
fun draftProgress(draft: MeetingDraft): Pair<Int, Int> =
    draft.confirmedSegments to draft.segments.size

// --------------------------------------------------------------------------- 界面状态

/**
 * 界面/通知要显示的**离线那一路**的状态（纯数据）。
 *
 * 与 `MicState` 分开是刻意的：`MicState` 是"在线录音"的状态（已送 N 段 / 待重传 N 段），
 * 而离线这一路要说的东西是"**服务端根本不在场**"（已缓存 N 段 / 等联网上传）——
 * 混成一个字段组的话，那句最容易写错的文案（"已上传" vs "还没传上去"）就没有唯一出口了。
 */
data class OfflineSyncState(
    val phase: OfflinePhase = OfflinePhase.IDLE,
    val clientId: String = "",
    val pendingSegments: Int = 0,
    val confirmedSegments: Int = 0,
    val totalSegments: Int = 0,
    /** 有几个清单在等（可能不止一场：连着断网录了两场）。 */
    val drafts: Int = 0,
    /** 录音中的那一场已录多久（秒）。 */
    val recordingSeconds: Double = 0.0,
    /**
     * 服务端**已经收尾这场会**（`POST /api/meeting/stop` 成功，转写已经开始）。
     *
     * 为什么它必须是一个单独的事实（而不是"phase == DONE 就当然是"）：
     * 界面上那句 DONE 的文案要说清"**已经开始转写**"还是"只传上去了、还没收尾"——
     * 而"没有 stop 就没有转写"正是这次修复的根因，说错这句等于把根因又藏回去。
     */
    val stopped: Boolean = false,
    /**
     * 本地那份副本"还能留多久"的**如实**说法（如 `"已上传，本地还留着 7 天的备份"`）。
     *
     * 为什么要从外面传进来而不是在这里拼：保留天数是 `AppPrefs` 里的**用户选项**，
     * 而这一层（纯函数）不该去读 Android 的 SharedPreferences —— 否则这行文案就没法在 JVM 里钉住。
     */
    val retentionNote: String = "",
    /** 真实原因（连不上 / 服务端拒绝 / 服务端正忙）：**界面必须显示它**，不许吞。 */
    val note: String = "",
    /** 下次自动重试的时刻（epoch ms；null = 不会再自动试了）。 */
    val nextAttemptAt: Long? = null,
    /** 这一次同步是不是"用户按的"（决定文案是"正在同步…"还是"正在自动补传…"）。 */
    val manual: Boolean = false,
) {
    val busy: Boolean get() = phase == OfflinePhase.UPLOADING
    val hasWork: Boolean get() = phase != OfflinePhase.IDLE
}

/**
 * 离线状态 → 界面/通知那一行字（**纯函数**，`OfflineMeetingStateTest` 逐档钉住）。
 *
 * 三条铁律（每一条都对应一次真实的静默错法）：
 *  1. **没传上去绝不说"已上传"**：没对账通过之前，一律是"等联网上传 / 待同步"；
 *  2. **不抢占**：ECHO 正在录另一场时，说的是"等这场录完再传"，**不是**"正在传"；
 *  3. **真实原因优先**：有 `note`（连不上 / 服务端拒绝 / 服务端正忙）就先说它 ——
 *     套话（"等联网上传"）把人引去查网线，而真相可能是"服务端不支持这个契约"。
 */
fun offlineStatusLine(st: OfflineSyncState): String = when (st.phase) {
    OfflinePhase.IDLE -> ""

    OfflinePhase.RECORDING_LOCAL -> buildString {
        append("离线录音中 · 已缓存 ")
        append(if (st.totalSegments > 0) st.totalSegments else st.pendingSegments)
        append(" 段")
        if (st.pendingSegments > 0) append("（${st.pendingSegments} 段还没传）")
        append(" · 等联网上传")
    }

    OfflinePhase.WAITING_UPLOAD, OfflinePhase.UNREACHABLE, OfflinePhase.FAILED -> {
        val head = when (st.phase) {
            OfflinePhase.WAITING_UPLOAD -> "离线录音已保存"
            OfflinePhase.UNREACHABLE -> "ECHO 连不上"
            else -> "补传没成功"
        }
        buildString {
            append(head).append(" · 已缓存 ").append(st.pendingSegments).append(" 段")
            if (st.confirmedSegments > 0) append("（已传 ${st.confirmedSegments} 段）")
            append(" · ")
            if (st.note.isNotBlank()) append(st.note) else append("等联网上传")
        }
    }

    OfflinePhase.WAITING_MEETING_END ->
        "ECHO 正在录另一场 · 已缓存 ${st.pendingSegments} 段 · 等这场录完再传（不抢占）"

    OfflinePhase.UPLOADING -> buildString {
        append(if (st.manual) "正在同步…" else "正在自动补传…")
        append(" · 已传 ").append(st.confirmedSegments).append("/").append(st.totalSegments).append(" 段")
        if (st.note.isNotBlank()) append(" · ").append(st.note)
    }

    OfflinePhase.DONE -> buildString {
        // 两句话**必须分得开**（"没有 stop 就没有转写"是这次的根因）：
        //  * 收尾成功 → 才敢说"已开始转写"；
        //  * 没收尾（不该走到这一档，但状态可能是老版本/半路留下的）→ 不许说转写。
        if (st.stopped) append("离线录音已补传并开始转写（${st.totalSegments} 段）")
        else append("离线录音已上传并核对一致（${st.totalSegments} 段）· 还没收到服务端的收尾确认")
        append(" · 手机上的副本还留着，${st.retentionNote.ifBlank { "按保留策略到期后才会清" }}")
    }
}
