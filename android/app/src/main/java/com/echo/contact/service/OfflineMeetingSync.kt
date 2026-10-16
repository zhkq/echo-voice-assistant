package com.echo.contact.service

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * **离线补传的编排**（纯逻辑：jvm 单测里注入一个假的 [OfflineTransport] 就能把
 * 顺序 / 续传 / 退避 / 对账 / 不抢占 全钉住）。
 *
 * ## 它为什么不能直接调 Retrofit
 *
 * 这条路上的错法全在**顺序与判定**上（先建会再传段、跳过已确认的、等对账通过才敢说成功、
 * 别人在录时不动手）。把这些塞进 `MeetingMicService` 会让它们只能靠真机验，
 * 而真机上"到底传没传上去"本来就难看出来。注入一个 [OfflineTransport] 之后，
 * 这一整个流程能在 JVM 里跑（`OfflineMeetingSyncTest`）。
 *
 * ## 与在线那条路的关系
 *
 * **完全不重叠**：在线那场会由 `MeetingMicService` 直接在循环里传（行为与改动前一致），
 * 这里只处理"清单里还欠着的段"。两条路共用同一个 [MeetingDraftStore] 与同一份
 * `clientId` 口径，所以"断网录了一半、网络恢复"这种混合情形也不会出现两套账。
 */
object OfflineMeetingSync {

    /**
     * 同步一轮。
     *
     * ⚠️ **对账通过之后还要 `stop`，两步都成了才标记"已完成"**：
     *  * **没有 stop 就没有转写** —— 服务端只在 `stop_meeting()` 里收尾这场会并发起转写；
     *  * 不收尾，"手机麦"这场会在服务端一直占着**唯一**那条录音槽（之后谁都开不了新会）。
     *
     * ⚠️ **标记完成也不删音频**：标记只是说"可以按保留策略清了"，
     * 清理是另一件（有界保留，见 `OfflineMeetingRetention`）的事 ——
     * 需求里"宁可多留一会儿也别丢音频"。
     */
    suspend fun run(
        transport: OfflineTransport,
        store: MeetingDraftStore,
        /** 现在有没有人在录（App 自己开着离线录音时是 true）。 */
        localRecording: Boolean = false,
        /** 正在录的那一份的 id（用来判断"ECHO 在录的是不是它"）。 */
        activeClientId: String? = null,
        /** 只跑这一份（用户对着某一场按「现在同步」）；null = 所有待传的。 */
        onlyClientId: String? = null,
        /** 这一次是不是用户按的（只影响文案）。 */
        manual: Boolean = false,
        /** 用户按了「现在同步」：**不受退避与自动次数限制**（但顺序/对账口径一个都不松）。 */
        force: Boolean = manual,
        /**
         * ③ 对账开始那一刻的回调（用例钉"调用序列"用；`null` = 不关心）。
         *
         * 生产代码**不传**它。为什么要有：修①的判据是"stop 在**对账之后**"，
         * 而对账不一定是网络调用（段响应里带了段数时就不会再问一次），
         * 所以注入的假 transport 记不出这个时刻 —— 见 [syncOne] 的同名参数。
         */
        reconcileAt: ((MeetingDraft) -> Unit)? = null,
    ): OfflineSyncResult {
        val drafts = store.pending().let { list ->
            if (onlyClientId.isNullOrBlank()) list else list.filter { it.clientId == onlyClientId }
        }
        if (drafts.isEmpty()) {
            return OfflineSyncResult(phase = OfflinePhase.IDLE, manual = manual)
        }

        // ---- ① 可达性：不可达时**立刻**停下，并把真实原因带回去（别伪装成"在传"）----
        val reach = transport.reachable()
        if (reach == null) {
            return offline(OfflinePhase.UNREACHABLE, drafts, manual, "探活没结果（超时/连不上）")
        }
        if (!isReachable(reach.code)) {
            return offline(OfflinePhase.UNREACHABLE, drafts, manual,
                "ECHO 连不上（HTTP ${reach.code}${if (reach.detail.isBlank()) "" else "，" + reach.detail}）")
        }

        // ---- ② 抢占判定：ECHO 正在录**别的**会 → 等，绝不抢占 ----
        val active = transport.activeMeeting()
            ?: return offline(OfflinePhase.UNREACHABLE, drafts, manual, "读不到 ECHO 的会议状态（不算可达）")
        val mine = mineRecording(active, localRecording, activeClientId, drafts)
        if (echoBusyElsewhere(active.recording, mine)) {
            return offline(OfflinePhase.WAITING_MEETING_END, drafts, manual,
                "ECHO 正在录另一场（${active.note.ifBlank { "没给名字" }}）：等它录完再传，不抢占")
        }

        // ---- ③ 一份一份地传（按开始时间升序 = 会议的自然顺序）----
        var confirmed = 0
        var total = 0
        var pending = 0
        var anyUploaded = false
        var anyStopped = false
        for (draft in drafts) {
            val stage = syncOne(transport, store, draft, force, reconcileAt)
            val d = stage.draft ?: draft
            anyUploaded = anyUploaded || d.uploaded
            anyStopped = anyStopped || stage.stopped
            confirmed += d.confirmedSegments
            total += d.segments.size
            pending += d.pendingSegments
            if (stage.result != null) {
                // 这一份停下了（不可达 / 服务端拒绝 / 对账不符 / **stop 没成**）：**别继续下一份**
                // —— 后面那些多半会遇到同样的问题，而用户要看到的是**第一个真实原因**。
                return stage.result.copy(
                    drafts = drafts.size,
                    manual = manual,
                    confirmedSegments = confirmed,
                    totalSegments = total,
                    pendingSegments = pending,
                )
            }
        }

        val done = pending == 0 || drafts.all { it.uploaded } || anyUploaded
        // `stopped` 只在**全部完成**时才敢说是：还有一份没传完时，说"已经开始转写"就是谎报
        // （那一场服务端还没收到 stop）。
        val stopped = done && anyStopped
        return OfflineSyncResult(
            phase = if (done) OfflinePhase.DONE else OfflinePhase.WAITING_UPLOAD,
            drafts = drafts.size,
            confirmedSegments = confirmed,
            totalSegments = total,
            pendingSegments = pending,
            stopped = stopped,
            note = if (done) "" else "还有 $pending 段没传完，留着下次接着传",
            manual = manual,
        )
    }

    /** "停在某一档 + 真实原因"这一种返回（只在此处拼，免得各处字段口径漂）。 */
    private fun offline(
        phase: OfflinePhase,
        drafts: List<MeetingDraft>,
        manual: Boolean,
        note: String,
    ): OfflineSyncResult = OfflineSyncResult(
        phase = phase,
        drafts = drafts.size,
        totalSegments = drafts.sumOf { it.segments.size },
        pendingSegments = drafts.sumOf { it.pendingSegments },
        confirmedSegments = drafts.sumOf { it.confirmedSegments },
        note = note,
        manual = manual,
    )

    /**
     * 一份清单的补传：**先建会**（带 `clientId` 幂等）→ **按序传段**（可续传）→ **对账** → **stop**。
     *
     * 顺序**不许颠倒**（每一条都对应一次真实的静默错法）：
     *  * stop 在对账**之后**：段还没到齐就收尾，服务端会拿一份缺段的音频去转写；
     *  * stop 在标记完成**之前**：先标 DONE 就会"看着成了、其实永远不会出文字"
     *    （**没有 stop 就没有转写** —— 见文件头与 `OfflinePhase` 的说明）；
     *  * stop 在 `markUploaded` 之前：它失败时这一份必须**留在待办里**（退避后重试）。
     *
     * [StageResult.draft] = 更新后的清单（**已经落盘**）；
     * [StageResult.result] = 该停下时的那份结果（null = 这一份成了）；
     * [StageResult.stopped] = 这一份的 stop 是不是真的成功了（成功路径上必为 true）。
     */
    private suspend fun syncOne(
        transport: OfflineTransport,
        store: MeetingDraftStore,
        draft: MeetingDraft,
        force: Boolean,
        /**
         * ③ 对账开始那一刻的回调（`null` = 不关心）。
         *
         * 为什么值得留这个口子：修①的判据是**调用序列**——
         * `start → segment… → 对账 → stop`。而"对账"不是一个网络调用
         * （段响应里带了段数时根本不会再发请求），所以靠假的 transport 记不出来；
         * 没有它，用例只能验"stop 在最后"，验不了"stop 在**对账之后**"
         * （先 stop 再传段同样能让"stop 是最后一次调用"成立）。
         */
        reconcileAt: ((MeetingDraft) -> Unit)? = null,
    ): StageResult {
        var d = draft

        // ① 建会（**同一个 clientId 反复调只算一场**：幂等是服务端的责任，客户端只发同一个 id）
        val start = transport.startMeeting(
            StartRequest(
                clientId = d.clientId,
                startedAt = d.startedAt,
                source = d.source,
                endedAt = d.endedAt,
                segments = d.segments.size,
                acknowledgedSegments = d.acknowledgedSegments,
                durationSeconds = d.durationSeconds,
                pauses = d.pauses,
            )
        )
        when (start) {
            is StartOutcome.Ok -> {
                if (start.meetingId > 0 && start.meetingId != d.serverMeetingId) {
                    d = d.copy(serverMeetingId = start.meetingId)
                    store.save(d)
                }
            }
            is StartOutcome.Busy -> return StageResult(
                d, which(OfflinePhase.WAITING_MEETING_END, d, start.note, force))
            is StartOutcome.Failed -> {
                val phase = if (start.retryable) OfflinePhase.WAITING_UPLOAD else OfflinePhase.FAILED
                return StageResult(d, which(phase, d, start.note, force))
            }
        }

        // ② 按序传段：一段失败就停在这（顺序不许破），已成功的**立刻推进水位**（可续传）
        var serverCount: Int? = null
        for (seg in segmentsToUploadOf(d)) {
            val file = store.segmentFile(d.clientId, seg.seq)
            if (!file.isFile) {
                // 本地没有这个文件：**不许跳过去传后面的**（顺序就是内容），也不许编空段。
                return StageResult(d, which(OfflinePhase.FAILED, d,
                    "第 ${seg.seq} 段的音频不在手机里（${seg.file}）：先别传后面的，免得顺序乱", force))
            }
            val bytes = try {
                file.readBytes()
            } catch (e: Exception) {
                return StageResult(d, which(OfflinePhase.FAILED, d,
                    "读不到第 ${seg.seq} 段的音频：${e.message}", force))
            }

            when (val out = transport.uploadSegment(
                UploadRequest(
                    clientId = d.clientId, seq = seg.seq, startedAt = d.startedAt,
                    source = d.source, wav = bytes,
                )
            )) {
                is SegmentOutcome.Ok -> {
                    if (out.serverMeetingId > 0 && out.serverMeetingId != d.serverMeetingId) {
                        d = d.copy(serverMeetingId = out.serverMeetingId)
                    }
                    // **逐段推进确认水位**并落盘：中途断网时下一轮从这里续
                    d = store.markAcknowledged(
                        d, acknowledgedAfterUpload(d.acknowledgedSegments, seg.seq))
                    d = d.copy(uploaded = false)          // 只要还有没传的段，就还不是"已上传"
                    store.save(d)
                    ActiveDraft.current?.takeIf { it.clientId == d.clientId }?.let { ActiveDraft.set(d) }
                    out.serverCount?.let { serverCount = it }
                }
                is SegmentOutcome.Failed -> {
                    val phase = if (out.retryable) OfflinePhase.WAITING_UPLOAD else OfflinePhase.FAILED
                    // 段层面失败：**已经推进的水位保留**（那几段是真被认下的），音频一律不删
                    store.save(d.copy(uploaded = false))
                    return StageResult(d, which(phase, d, out.note, force))
                }
            }
        }

        // ③ 对账：服务端手上这一场的段数 == 本地最大段序号（段序号 1 起、连续）→ 才敢往下走
        reconcileAt?.invoke(d)
        val counted = serverCount
            ?: when (val c = transport.segmentCount(d.clientId, d.serverMeetingId)) {
                is CountOutcome.Ok -> c.count
                is CountOutcome.Failed -> null
            }
        when (val verdict = reconcile(d.segments.map { it.seq }, counted)) {
            is ReconcileVerdict.Mismatch -> {
                // **不删缓存**、不标记已上传；原因原文带回去（服务端没回段数那一档也在里面）
                store.save(d.copy(uploaded = false))
                return StageResult(d, which(OfflinePhase.FAILED, d, verdict.note, force))
            }
            is ReconcileVerdict.Match -> Unit      // 过了：接着走最后一步
        }

        // ④ **收尾：`POST /api/meeting/stop`**（补传的最后一步，不是可选项）
        //
        // 为什么必须有这一步（这是本次修复的根因）：
        //  * **没有 stop 就没有转写** —— 服务端只在 `stop_meeting()` 里落盘并发起转写；
        //    只做 start + segment，音频永远只是一堆躺在目录里的 wav，用户要的"出文字"不会发生；
        //  * "手机麦"这场会不收尾就**一直占着服务端唯一那条录音槽** → 之后电脑/手机
        //    都开不了新会议（用户看到的会是"开始录音失败"，而根因在几天前那场没收尾的会）。
        //
        // 顺序还是那条：**对账过了才 stop，stop 成了才进 DONE**。
        d = d.copy(uploaded = false)               // 万一下面失败：**留在待办里**，别让人以为成了
        // 这一步包 try/catch：**补传是后台动作，不许被一个异常整条打断**
        // （真 transport 自己会吞，但这一层不能假设所有实现都那么周到 ——
        //  抛出去的表现是"整轮同步炸了"，而真相只是"收尾那一下没成"）
        val stop = try {
            transport.stopMeeting(StopRequest(clientId = d.clientId, meetingId = d.serverMeetingId))
        } catch (e: Exception) {
            StopOutcome.Failed(SyncFailure.UNREACHABLE,
                "收尾那个请求抛了异常：${e.javaClass.simpleName}${e.message?.let { "（$it）" } ?: ""}",
                retryable = true, alreadyUploadedHint = true)
        }
        when (stop) {
            is StopOutcome.Ok -> {
                // 幂等命中（服务端说"没有进行中的会议"）**算成功** —— 这场之前就停过了，
                // 卡在这里是最糟的：用户看到"差最后一步"却永远不动，而服务端其实一切都好。
                val done = store.markUploaded(d)
                store.save(done)
                ActiveDraft.current?.takeIf { it.clientId == done.clientId }?.let { ActiveDraft.set(done) }
                return StageResult(done, null, stopped = true)
            }
            is StopOutcome.Failed -> {
                // stop 没成：**不进 DONE、不删缓存**，留在待办里等退避后重试。
                // ⚠️ 这里**不**把 uploaded 记成 true —— 记了就等于说"已经出文字了"，
                // 而真相是"音频传上去了，但服务端还没开始转写"。
                store.save(d)
                val note = if (stop.alreadyUploadedHint) {
                    "音频都传上去了，只是收尾那一步（POST /api/meeting/stop）没成：${stop.note}；" +
                        "**还没有开始转写**，缓存留着，下次接着试"
                } else {
                    "补传的收尾（POST /api/meeting/stop）没成：${stop.note}；" +
                        "**还没有开始转写**，缓存留着，下次接着试"
                }
                val phase = if (stop.retryable) OfflinePhase.WAITING_UPLOAD else OfflinePhase.FAILED
                return StageResult(d, which(phase, d, note, force))
            }
        }
    }

    /** 单份清单那一档结果（省得每处都手拼五个字段 —— 手拼必漂）。 */
    private fun which(
        phase: OfflinePhase,
        d: MeetingDraft,
        note: String,
        manual: Boolean,
        stopped: Boolean = false,
    ): OfflineSyncResult = OfflineSyncResult(
        phase = phase,
        confirmedSegments = d.confirmedSegments,
        totalSegments = d.segments.size,
        pendingSegments = d.pendingSegments,
        stopped = stopped,
        note = note,
        manual = manual,
    )

    /**
     * 「ECHO 正在录的那一场，是不是**我自己**这一份清单的会」。
     *
     * 判据刻意不用"会议名/文件夹"（那是服务端生成的时间戳，本地清单里根本没有），
     * 而是**两级**：
     *  1. 服务端回了 `clientId` 且与我们正在同步的清单相等 → 是（最强判据，也最不容易错）；
     *  2. 否则比会议 id（清单里记过 `serverMeetingId` 才算）。
     *
     * 判对的意义：判错成"是我的"会把**电脑正在录的会**当成自己的场子往里传段
     * （两路音频混进同一场 = 转写里两个人同时说话）；判错成"不是我的"最多多等一会儿。
     * 所以这里**宁可判不是**（只有确切相等才回 true）。
     */
    internal fun mineRecording(
        active: ActiveMeeting,
        localRecording: Boolean,
        activeClientId: String?,
        drafts: List<MeetingDraft>,
    ): Boolean {
        if (localRecording) return false
        val mine = drafts.filter { it.clientId == activeClientId }
        if (mine.isEmpty()) return false
        val aid = active.clientId.trim()
        if (aid.isNotEmpty()) return mine.any { it.clientId == aid }
        return active.meetingId > 0 && mine.any { it.serverMeetingId == active.meetingId }
    }

    /** 一份清单跑完之后的东西（`result != null` = 该停下了）。 */
    private data class StageResult(
        val draft: MeetingDraft?,
        val result: OfflineSyncResult?,
        /** 这一份的 `stop` 是不是真的成功了（**唯一**敢说"开始转写"的判据）。 */
        val stopped: Boolean = false,
    )

    // ---- 状态流（界面读它；与 MicState 同一个套路）----

    private val _state = MutableStateFlow(OfflineSyncState())
    val state: StateFlow<OfflineSyncState> = _state.asStateFlow()

    fun publish(st: OfflineSyncState) {
        _state.value = st
    }

    /** 只更新"原因/下次重试时刻"这类附加信息（别把 phase 覆盖掉）。 */
    fun publishNote(note: String, nextAttemptAt: Long? = null) {
        _state.value = _state.value.copy(note = note, nextAttemptAt = nextAttemptAt)
    }

    fun reset() {
        _state.value = OfflineSyncState()
    }
}

/** [OfflineMeetingSync.run] 的结局（同时也是给界面/通知的那份状态）。 */
data class OfflineSyncResult(
    val phase: OfflinePhase = OfflinePhase.IDLE,
    val drafts: Int = 0,
    val confirmedSegments: Int = 0,
    val totalSegments: Int = 0,
    val pendingSegments: Int = 0,
    /**
     * 服务端**已经收尾**（`POST /api/meeting/stop` 成功 → 转写已经开始）。
     *
     * 它跟 [phase] 是**两个**事实，刻意分开：`DONE` 说的是"本地这一轮做完了"，
     * 而这一项说的是"电脑那边真的开始转写了" —— 用户要的是后者（"传上去能出文字"），
     * 两者混成一个字段就再也没法如实说出"传上去了但没收尾"。
     */
    val stopped: Boolean = false,
    val note: String = "",
    val manual: Boolean = false,
) {
    /** 转成界面读的那份状态（**唯一**的转换处）。 */
    fun toState(
        active: MeetingDraft? = null,
        fallbackNote: String = "",
        /** 本地副本"还能留多久"的如实说法（来自 `AppPrefs` 的保留天数，见 `OfflineMeetingRetention`）。 */
        retentionNote: String = "",
    ): OfflineSyncState =
        OfflineSyncState(
            phase = phase,
            clientId = active?.clientId.orEmpty(),
            pendingSegments = if (phase == OfflinePhase.RECORDING_LOCAL)
                active?.pendingSegments ?: pendingSegments else pendingSegments,
            confirmedSegments = if (phase == OfflinePhase.RECORDING_LOCAL)
                active?.confirmedSegments ?: confirmedSegments else confirmedSegments,
            totalSegments = if (phase == OfflinePhase.RECORDING_LOCAL)
                active?.segments?.size ?: totalSegments else totalSegments,
            drafts = drafts,
            stopped = stopped,
            retentionNote = retentionNote,
            note = note.ifBlank { fallbackNote },
            manual = manual,
        )
}

// --------------------------------------------------------------------------- 端口（给测试注入）

/** 一次探活的结果（`code == null` = 连不上/超时）。 */
data class ProbeResult(val code: Int?, val detail: String = "")

/** ECHO 那一侧的会议现状（补传只关心这几件事）。 */
data class ActiveMeeting(
    val recording: Boolean,
    val meetingId: Int = 0,
    val source: String = "",
    val note: String = "",
    /**
     * 服务端认下的**客户端 id**（有的话，就是判"这场是不是我自己那一份清单"的最强判据）。
     *
     * 为什么它这么重要：没有它的时候只能按"会议 id 相等"判，而我们**自己刚建起来**
     * 那场会正好会从 `active` 里回来 —— 那时清单里的 `serverMeetingId` 可能还没写下来，
     * 于是判成"不是我的" → 退化成"等这场录完再传"，等着等着就永远在等（自己等自己）。
     * 所以拿得到 `clientId` 时必须用它。
     */
    val clientId: String = "",
)

/** 建会请求（**所有会建会的路都用它**，免得两处拼出两套字段）。 */
data class StartRequest(
    val clientId: String,
    val startedAt: String,
    val source: String = MeetingSource.PHONE,
    val endedAt: String = "",
    /** 清单里的总段数（服务端可据此核对，也可只用它做日志）。 */
    val segments: Int = 0,
    /**
     * 清单里**已经传上去过**几段（续传时 > 0）。
     *
     * 为什么要交上去：服务端那次是"接着一场已经存在的会继续收段"，它得知道
     * 客户端**认为**前面有几段已经到了（对账时它报的段数才有意义；也是它排查
     * "客户端说传过、我这里其实没有"的唯一线索）。
     */
    val acknowledgedSegments: Int = 0,
    val durationSeconds: Double = 0.0,
    val pauses: List<DraftPause> = emptyList(),
)

/** 传一段的请求。 */
data class UploadRequest(
    val clientId: String,
    val seq: Int,
    val startedAt: String,
    val source: String,
    val wav: ByteArray,
) {
    // ByteArray 在 data class 里必须自己写 equals/hashCode（否则用例里的"传了哪几段"
    // 断言会永远不等 —— 那可是真的会让人误判"没传"）。
    override fun equals(other: Any?): Boolean =
        other is UploadRequest && other.clientId == clientId && other.seq == seq &&
            other.startedAt == startedAt && other.source == source && other.wav.contentEquals(wav)

    override fun hashCode(): Int =
        (((clientId.hashCode() * 31 + seq) * 31 + startedAt.hashCode()) * 31 +
            source.hashCode()) * 31 + wav.contentHashCode()
}

/**
 * 收尾那一场的请求（`POST /api/meeting/stop`）。
 *
 * [clientId] **会**作为 query 发出去（服务端 2026-10-16 起支持，`app/api.py::meeting_stop`）——
 * 它是**防误停别人会议**的护栏：与服务端"当前正在录的那一场"不符时，服务端拒绝收尾且
 * **一个字节都不动**（否则一个错序/重试的补传就会把别人正在录的会停掉，那场音频当场就断）。
 * 所以这里发的必须是**这一份清单自己的** id。
 *
 * [meetingId] 只用于日志与文案（服务端那条签名里没有它）。
 * `clientId` 为空时一个 query 都不发（退化成改动前的行为，见
 * `OfflineMeetingTransport.stopMeeting()`）。
 */
data class StopRequest(
    val clientId: String = "",
    val meetingId: Int = 0,
)

/**
 * 补传需要的**全部**服务端能力（一个接口，方便测试注入 + 让"服务端要支持什么"一目了然）。
 *
 * 实现见 `OfflineMeetingTransport`（真 HTTP，走 `EchoApi`）。
 */
interface OfflineTransport {

    /** 探活（`null` = 连不上/超时）。 */
    suspend fun reachable(): ProbeResult?

    /** ECHO 那边会议现状；`null` = 读不到（**算不可达**，不敢往下走）。 */
    suspend fun activeMeeting(): ActiveMeeting?

    /** 用缓存的开始时间 + 客户端 id 建那场会（**同一个 id 反复调只该建一场**）。 */
    suspend fun startMeeting(req: StartRequest): StartOutcome

    /** 传一段（裸 wav；带上 `clientId` / `seq` 供服务端幂等）。 */
    suspend fun uploadSegment(req: UploadRequest): SegmentOutcome

    /** 问服务端：这一场它手上有几段（对账用）。读不到就 [CountOutcome.Failed]。 */
    suspend fun segmentCount(clientId: String, meetingId: Int): CountOutcome

    /**
     * **收尾这场会**（`POST /api/meeting/stop`）—— 补传的最后一步。
     *
     * 为什么它是端口上的一个**必需**能力（而不是可选/忽略返回值）：
     * 服务端只在 `stop_meeting()` 里发起转写，也只在它里面把那条唯一的录音槽放掉 ——
     * 少了这一步，**传上去的音频永远不会出文字**，而且之后谁都开不了新会议。
     */
    suspend fun stopMeeting(req: StopRequest): StopOutcome
}
