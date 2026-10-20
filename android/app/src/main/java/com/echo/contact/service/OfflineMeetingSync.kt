package com.echo.contact.service

import com.echo.contact.audio.SegmentFormat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * **本地先写 + 机会性上传**的编排（纯逻辑：jvm 单测里注入一个假的 [OfflineTransport]
 * 就能把顺序 / 续传 / 退避 / 对账 / 不抢占 / 上报暂停 全钉住）。
 *
 * ## 它为什么不能直接调 Retrofit
 *
 * 这条路上的错法全在**顺序与判定**上（先建会再传段、跳过已确认的、等对账通过才敢说成功、
 * 别人在录时不动手、还在录的那一场绝不能被收尾）。把这些塞进 `MeetingMicService`
 * 会让它们只能靠真机验，而真机上"到底传没传上去"本来就难看出来。注入一个
 * [OfflineTransport] 之后，这一整个流程能在 JVM 里跑（`OfflineMeetingSyncTest`）。
 *
 * ## 只有一条路了（用户 2026-10-17）
 *
 * 以前这里只管"离线那一场"的补传，而联网那一场由 `MeetingMicService` 自己在循环里传 ——
 * 两条路两套账。现在两件事合成一件：**每一段都先落本地**（`MeetingRecordSession`），
 * 而"传上去"这件事**只有这一处**在管（[SyncScope.INCREMENTAL] 一边录一边机会性传，
 * [SyncScope.FINALIZE] 录完之后补齐 → 对账 → stop）。于是不会再有
 * "离线那套以为传完了、在线那套还在传同一段"这种两套账的错法。
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
        /**
         * 正在录的那一份的 id（`null` = 这台手机此刻没在录）。
         *
         * ⚠️ 2026-10-20 起它**不再**用来判"ECHO 在录的是不是我自己那场"（那份判据在
         * `service/MeetingOwnership.kt`，看 `clientId` 在不在**本地全部清单**里）。
         * 它只剩一个用途：区分"我这场还在录"与"我这场录完还在传"那一档文案
         * （[meetingOwnership] 的 `localRecording`）。别拿它去判归属 ——
         * 手机 id 每次开录都会变，App 重开之后它根本认不出"上一次启动留下的那场"。
         */
        activeClientId: String? = null,
        /** 只跑这一份（用户对着某一场按「现在同步」）；null = 所有待传的。 */
        onlyClientId: String? = null,
        /** 这一次是不是用户按的（只影响文案）。 */
        manual: Boolean = false,
        /** 用户按了「现在同步」：**不受退避与自动次数限制**（但顺序/对账口径一个都不松）。 */
        force: Boolean = manual,
        /**
         * 这一轮的口径：还在录（[SyncScope.INCREMENTAL]，只传段）还是录完了
         * （[SyncScope.FINALIZE]，补齐 → 对账 → stop）。见 [SyncScope] 的说明。
         *
         * 默认 [SyncScope.FINALIZE]：后台 tick 与「现在同步」都是"把这场传完"的语义，
         * 而**还在录的那一份会被逐份降级成 INCREMENTAL**（判据在 [syncOne] 的第一行，
         * 不靠调用方记得传对）。
         */
        scope: SyncScope = SyncScope.FINALIZE,
        /**
         * ③ 对账开始那一刻的回调（用例钉"调用序列"用；`null` = 不关心）。
         *
         * 生产代码**不传**它。为什么要有：修①的判据是"stop 在**对账之后**"，
         * 而对账不一定是网络调用（段响应里带了段数时就不会再问一次），
         * 所以注入的假 transport 记不出这个时刻 —— 见 [syncOne] 的同名参数。
         */
        reconcileAt: ((MeetingDraft) -> Unit)? = null,
        /**
         * **逐段**的进度回调（`null` = 不关心；只影响界面显示，不参与任何判定）。
         *
         * 为什么必须有它：一轮同步可能要传好几分钟（一段 10 分钟 ≈19 MB，走中继更慢），
         * 而"已传 M / 共 N 段"原本只在**整轮结束**之后才第一次算得出来 —— 中间那几分钟
         * 界面上什么都没变，用户刚按了「现在同步」，看到的就是"按了没反应"。
         * 这个回调在每段**开始上传之前**报一次：已确认几段、正在传第几段。
         */
        onProgress: ((SyncProgress) -> Unit)? = null,
        /**
         * **每一段真的被服务端收下之后**的回调（`null` = 不关心；只影响展示）。
         *
         * 为什么"段开始"那一报还不够（用户 2026-10-19：*"传了一段，但是后面好像还是不行"*）：
         * 一段 18 MB 的 WAV 传完可能要几分钟，而"已传 M/T 段"这个数字原本只在
         * **下一段开始**之前才更新 —— 也就是**最后一段传完到整轮结束**（对账 + stop）
         * 那段时间里，界面上的数字还是旧的（"已传 2/3"，可 3 段其实都到了）。
         * 更糟的是"只有一段"的情况：传完的那一刻界面上**一次都不动**，
         * 用户看到的就是"传了一段之后停住了"。
         * 于是这一段成功之后**立刻**报一次真数（`justConfirmed` = 刚被收下的段序号）。
         *
         * ⚠️ 与 `onProgress` 一样**只用于展示**：任何判定都不许读它。
         */
        onSegmentDone: ((SyncProgress) -> Unit)? = null,
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
        //
        // 判"服务端那场是不是我自己那一份清单的会"**只有一处实现**：`meetingOwnership`
        // （会议页的归属显示读的是同一个函数）。这里原来自己写了一套"只认当前会话 id"
        // 的比较，而手机 id **每次开录都是新的**（`MeetingDraftStore.newClientId()`）——
        // App 重开之后那套比较必然认不出"我上一次启动留下的那场"，于是补传自己把自己挡住
        // （一直"等这场录完再传"，而那一场就是它自己），2026-10-20 真机撞到的就是这个。
        val active = transport.activeMeeting()
            ?: return offline(OfflinePhase.UNREACHABLE, drafts, manual, "读不到 ECHO 的会议状态（不算可达）")
        // `localRecording` 只影响"我是在录 / 还在传"那一档（闸只问 `isMine`，不看它）：
        // `activeClientId` 非空 = 这台手机此刻正录着那一份清单。
        val owner = meetingOwnership(
            active = active.recording,
            source = active.source,
            clientId = active.clientId,
            serverMeetingId = active.meetingId,
            local = drafts.map { LocalDraftRef(it.clientId, it.serverMeetingId) },
            localRecording = !activeClientId.isNullOrBlank(),
        )
        if (echoBusyElsewhere(active.recording, owner.isMine)) {
            return offline(OfflinePhase.WAITING_MEETING_END, drafts, manual,
                "ECHO 正在录另一场（${active.note.ifBlank { "没给名字" }}）：等它录完再传，不抢占")
        }

        // ---- ③ 一份一份地传（按开始时间升序 = 会议的自然顺序）----
        var confirmed = 0
        var total = 0
        var pending = 0
        var anyUploaded = false
        var anyStopped = false
        var anyFinalized = false
        /** 「暂停区间没报上去」那句（如实记下来，最后附在 note 上 —— 见 withPauseNote）。 */
        var pauseNote = ""
        // 进度用的总量与"前面几份已经确认了几段"（**只用于展示**，判定一个都不依赖它）
        val totalAll = drafts.sumOf { it.segments.size }
        var confirmedBefore = 0
        for (draft in drafts) {
            val baseConfirmed = confirmedBefore
            val stage = syncOne(transport, store, draft, force, scope, reconcileAt,
                onSegment = if (onProgress == null) null else { seq, bytes ->
                    // 这一段**正要开始传**：它自己还没被确认，所以只数到它前面那一小段。
                    // （原先写成 `seq - acked` 会把"正在传的这段"提前算成已传 —— 界面会
                    //  显示 1/3 却其实一段都还没到服务端，那正是"谎报进度"。）
                    val inDraft = (seq - 1 - draft.acknowledgedSegments).coerceAtLeast(0)
                    val done = (baseConfirmed + draft.confirmedSegments + inDraft)
                        .coerceIn(0, totalAll)
                    onProgress(SyncProgress(
                        drafts = drafts.size,
                        totalSegments = totalAll,
                        confirmedSegments = done,
                        pendingSegments = (totalAll - done).coerceAtLeast(0),
                        currentSeq = seq,
                        // 这一段有多大：前台通知要写"第 3 段 12.4 MB"（用户点名的那句），
                        // 而只有这里知道"正在传的是哪一段、它多少字节"。
                        currentBytes = bytes,
                    ))
                },
                onSegmentOk = if (onSegmentDone == null) null else { seq ->
                    // 这一段**已经**被服务端收下了：数它本身进来（与"开始传"那一报的区别就在这里）。
                    // 用 `seq <= seq` 去数清单里真有的段，而不是算 `seq - acked` ——
                    // 清单万一有洞（老版本截断过），减法会算出一个不存在的"已传段数"。
                    val inDraft = draft.segments.count { it.seq <= seq }
                    val done = (baseConfirmed + inDraft).coerceIn(0, totalAll)
                    onSegmentDone(SyncProgress(
                        drafts = drafts.size,
                        totalSegments = totalAll,
                        confirmedSegments = done,
                        pendingSegments = (totalAll - done).coerceAtLeast(0),
                        currentSeq = seq,
                        justConfirmed = seq,
                    ))
                })
            val d = stage.draft ?: draft
            confirmedBefore += d.confirmedSegments
            anyUploaded = anyUploaded || d.uploaded
            anyStopped = anyStopped || stage.stopped
            anyFinalized = anyFinalized || stage.finalized
            pauseNote = stage.pauseNote.ifBlank { pauseNote }
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
                    note = withPauseNote(withPauseNote(stage.result.note, pauseNote),
                        inferredEndNote(drafts)),
                )
            }
        }

        // ⚠️ 还在录的那一份不算"完"：它的段可以一次不剩地传上去（pending=0），但这一场
        // **还没收尾**（没有 stop 就没有转写）。所以"done"要求三件事同时成立：
        // 没有一场还在录、至少有一份真的走到了收尾、而且没有欠着的段。
        val anyRecording = drafts.any { it.recording }
        val done = !anyRecording && ((pending == 0 && anyFinalized) || drafts.all { it.uploaded })
        // `stopped` 只在**全部完成**时才敢说是：还有一份没传完时，说"已经开始转写"就是谎报
        // （那一场服务端还没收到 stop）。
        val stopped = done && anyStopped
        val baseNote = when {
            done -> ""
            pending > 0 -> "还有 $pending 段没传完，留着下次接着传"
            // 还在录：段可以一段不剩地传上去，但**这一场还没收尾**（收尾要等用户按结束）
            anyRecording -> "这一场还在录：已经录到的段都在服务端了，收尾（开始转写）要等你按结束"
            else -> "段都传上去了，只差收尾（POST /api/meeting/stop）：还没开始转写"
        }
        return OfflineSyncResult(
            phase = if (done) OfflinePhase.DONE else OfflinePhase.WAITING_UPLOAD,
            drafts = drafts.size,
            confirmedSegments = confirmed,
            totalSegments = total,
            pendingSegments = pending,
            stopped = stopped,
            note = withPauseNote(withPauseNote(baseNote, pauseNote), inferredEndNote(drafts)),
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
     * 一份清单的同步：**先建会**（带 `clientId` 幂等）→ **上报暂停区间** → **按序传段**（可续传）
     * →（只有"已经录完"才继续）**对账** → **stop**。
     *
     * 顺序**不许颠倒**（每一条都对应一次真实的静默错法）：
     *  * stop 在对账**之后**：段还没到齐就收尾，服务端会拿一份缺段的音频去转写；
     *  * stop 在标记完成**之前**：先标 DONE 就会"看着成了、其实永远不会出文字"
     *    （**没有 stop 就没有转写** —— 见文件头与 `OfflinePhase` 的说明）；
     *  * stop 在 `markUploaded` 之前：它失败时这一份必须**留在待办里**（退避后重试）。
     *
     * ⚠️ **还在录的那一份（[MeetingDraft.recording]）到此为止**：只做"建会 + 报暂停 + 传段"，
     * **绝不**对账、**绝不** stop、**绝不**标已上传。理由不是"省点事"，而是：
     *  * 提前 stop = 服务端立刻把这一场落盘开始转写，而麦克风还在录 → **后面的段再也传不上去**
     *    （服务端那份已经收尾，本地这份还会被标成"已完成"）；
     *  * 提前对账也毫无意义（段数当然还没到齐），只会产生一条假的"对账不符"。
     *  这个降级**不看调用方传了什么 scope**：后台 tick 用的就是 FINALIZE，而它完全可能
     *  在用户还在录的时候被调到 —— 判据必须长在这里。
     *
     * [StageResult.draft] = 更新后的清单（**已经落盘**）；
     * [StageResult.result] = 该停下时的那份结果（null = 这一份成了）；
     * [StageResult.stopped] = 这一份的 stop 是不是真的成功了（成功路径上必为 true）；
     * [StageResult.finalized] = 这一份**走完了**收尾那一半（INCREMENTAL / 还在录 = false）。
     */
    private suspend fun syncOne(
        transport: OfflineTransport,
        store: MeetingDraftStore,
        draft: MeetingDraft,
        force: Boolean,
        /** 这一轮的口径（[SyncScope]）；还在录的那一份会被就地降级成 INCREMENTAL。 */
        scope: SyncScope,
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
        /** 每一段**开始上传之前**报一次（[run] 的 `onProgress` 用；`null` = 不关心）。
         *  第二个参数是**这一段多少字节**（前台通知那句"第 3 段 12.4 MB"就靠它；
         *  读不到大小就给 0 —— 通知里只报段号，**绝不编一个大小**）。 */
        onSegment: ((Int, Long) -> Unit)? = null,
        /** 每一段**被服务端收下之后**报一次（[run] 的 `onSegmentDone` 用；`null` = 不关心）。 */
        onSegmentOk: ((Int) -> Unit)? = null,
    ): StageResult {
        var d = draft
        val finalize = scope == SyncScope.FINALIZE && !d.recording

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

        // ①' ③ 上报暂停区间（**start 成功之后**；用户 2026-10-17 的第③件事）。
        //
        // 为什么放在 start 之后：服务端得先有这一场（按 clientId 幂等建出来的那一场），
        // 这些区间才有地方落；而"按 clientId 幂等 + 服务端合并去重"意味着**整份重复报也没关系**
        // ——这正是"每新增一段暂停区间后再报一次"能这么简单的原因。
        //
        // ⚠️ **它成不成都不许影响补传**：老服务端根本没有这个端点（另一条线在同时实现），
        // 而"端点不存在"跟"音频有问题"是两件毫不相干的事 —— 把它当失败会让用户
        // 因为一个可有可无的上报而永远传不上音频。所以这里只把结论**如实记进清单**
        // （`pauseNote`，界面那条 syncNote 会带上它），然后**照常往下走**。
        var pauseNote = d.pauseNote
        if (d.pauses.isNotEmpty()) {
            val pauseOutcome = try {
                transport.reportPauses(PauseRequest(clientId = d.clientId, pauses = d.pauses))
            } catch (e: Exception) {
                // 补传是后台动作：不许被一个异常整条打断（真 transport 自己会吞，这一层也不假设）
                PauseOutcome.Failed(SyncFailure.UNREACHABLE,
                    "上报暂停区间时抛了异常：${e.javaClass.simpleName}")
            }
            pauseNote = when (pauseOutcome) {
                is PauseOutcome.Ok -> ""          // 报上去了：那条"没报上去"的旧话要撤掉
                is PauseOutcome.Unsupported -> pauseOutcome.note
                is PauseOutcome.Failed -> pauseOutcome.note
            }
            if (pauseNote != d.pauseNote) {
                d = d.copy(pauseNote = pauseNote)
                store.save(d)
            }
        }

        // ② 按序传段：一段失败就停在这（顺序不许破），已成功的**立刻推进水位**（可续传）
        //
        // ⚠️ **成功后必须继续下一段**（用户 2026-10-19 的"传完第 1 段就停住"）：
        // 这个循环里**没有** `break`、也没有"传一段就跑下一件事"的分支 —— 只有
        // ① 本地缺文件、② 服务端没收下，这两种情况才 `return`（而且都保留水位、不删音频）。
        // 循环跑完之后才允许进对账与 `stop`（见 ③④ 的说明：**段没传完绝不 stop**）。
        var serverCount: Int? = null
        for (seg in segmentsToUploadOf(d)) {
            // **按清单里记的名字**找音频：见 audioFileOf —— 按 seq 拼就得先猜一个格式
            // （FLAC 还是 WAV），猜错就会得到"文件不存在"，于是补传永远卡在第一段
            val file = store.audioFileOf(d.clientId, seg)
            if (!file.isFile) {
                // 本地没有这个文件：**不许跳过去传后面的**（顺序就是内容），也不许编空段。
                return StageResult(d, which(OfflinePhase.FAILED, d,
                    "第 ${seg.seq} 段的音频不在手机里（${seg.file}）：先别传后面的，免得顺序乱", force))
            }
            // 这一段马上要发了：先报一次进度（"正在传第 N 段"）—— 一个 10 分钟段的上传
            // 可能要几十秒到几分钟，没有这一步界面在这段时间里一动不动（= 看着像卡住）。
            // 顺手把这**一段多大**报出去（前台通知那句"第 3 段 12.4 MB"要的是真数，
            // 不是估算）；`length()` 读不到就给 0，通知里就只报段号。
            onSegment?.invoke(seg.seq, runCatching { file.length() }.getOrDefault(0L))
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
                    // ⚠️ Content-Type 用**这一段清单里记的**格式（绝不按"我们想要 FLAC"发）
                    format = seg.format,
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
                    // 这一段**真的到服务端了**：立刻把真数报给界面（见 `onSegmentDone` 的说明），
                    // 然后**继续下一段** —— 循环不 break、不 return，直到清单里该传的段都传完。
                    onSegmentOk?.invoke(seg.seq)
                }
                is SegmentOutcome.Failed -> {
                    val phase = if (out.retryable) OfflinePhase.WAITING_UPLOAD else OfflinePhase.FAILED
                    // 段层面失败：**已经推进的水位保留**（那几段是真被认下的），音频一律不删
                    store.save(d.copy(uploaded = false))
                    // ## 为什么失败这句话里必须带上"已传过的段不会重传、下次从第 N 段接着传"
                    //
                    // 用户 2026-10-19 的原话是"传了一段，但是后面好像还是不行"。他真正怕的是
                    // **又要从第 1 段重来**（那是 18 MB 白传一遍）。而这条路的真相恰恰相反：
                    // 水位（`acknowledgedSegments`）在每段成功后**立刻落盘**，
                    // 下一轮 `segmentsToUploadOf` 就从前几段之后开始 —— 所以这句话不是安慰，
                    // 是这条链路本来就有的行为，只是以前**没有告诉用户**。
                    // 混在"服务端拒了"那种原因里更不行：那时"重试"根本没意义，得说清是两回事。
                    val resume = if (d.confirmedSegments > 0) {
                        "；已传过的 ${d.confirmedSegments} 段不会重传，下次从第 ${seg.seq} 段接着传"
                    } else {
                        "；一段都还没传上去，下次从第 ${seg.seq} 段开始"
                    }
                    return StageResult(d, which(phase, d, out.note + resume, force))
                }
            }
        }

        // ---- 还在录（或这一轮只说"机会性上传"）：到这里就够了，绝不收尾 ----
        if (!finalize) {
            return StageResult(d, null, finalized = false, pauseNote = pauseNote)
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
                return StageResult(done, null, stopped = true, finalized = true, pauseNote = pauseNote)
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
                return StageResult(d, which(phase, d, withPauseNote(note, pauseNote), force),
                    finalized = true, pauseNote = pauseNote)
            }
        }
    }

    /**
     * 把"暂停区间没报上去"那句附在正文后面（**不覆盖真实原因** —— 两句都要在）。
     *
     * 为什么要单独一个拼法：这句话是**附加信息**，任何时候都不该把它顶掉真正的原因
     * （"服务端拒了收尾"比"暂停没报上去"要紧得多）；而它又必须能被用户看到
     * —— 用户点名要求"如实记一句"。两句都在 = 两种事实都在。
     */
    internal fun withPauseNote(note: String, pauseNote: String): String =
        listOf(note, pauseNote).filter { it.isNotBlank() }.joinToString(" · ")

    /**
     * 「这一场的结束时间是**推**出来的」那句如实的话（空 = 没有这种清单）。
     *
     * 什么时候会有：App 在录音时被系统杀掉，下一次开录时那条清单的 `ended_at` 只能
     * 按最后一段推（`endedAtInferred = true`）。它不是"暂停没报上去"那种附加信息，
     * 而是**这场会的时间轴有一处是推断的** —— 用户与排障的人都该看到。
     */
    internal fun inferredEndNote(drafts: List<MeetingDraft>): String {
        val n = drafts.count { it.endedAtInferred }
        return if (n == 0) "" else
            "有 $n 场是被进程打断的（结束时间按最后一段推的，不是当时按的结束；" +
                // ⚠️ 进程被杀时跑不到"补尾段"那一步：缓冲里不足一段的样本是真的丢了。
                // 这句话必须带上 —— 只说"结束时间是推的"会让人以为音频是完整的。
                "进程被杀时补不了尾，最后不足一段的缓冲可能没封存）"
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

    // ⚠️ 「服务端正在录的那一场是不是**我自己**的」不再有第二个实现：
    // 判据已经搬到 `MeetingOwnership.kt::meetingOwnership()`（会议页显示读的是同一个函数）。
    // 旧的那一份 `mineRecording(active, activeClientId, drafts)` 只认"**这一刻正在录**的那份清单"，
    // 而手机的会话 id **每次开录都是新的** → App 重开之后认不出"上一次启动留下的那场"，
    // 表现就是补传自己把自己挡在门外（2026-10-20 真机：手机以为自己没在录、说电脑在录）。
    // 想看当初为什么 clientId 那一级要先判（2026-10-17 合并两条路时改的），见那个文件的类注释。

    /** 一份清单跑完之后的东西（`result != null` = 该停下了）。 */
    private data class StageResult(
        val draft: MeetingDraft?,
        val result: OfflineSyncResult?,
        /** 这一份的 `stop` 是不是真的成功了（**唯一**敢说"开始转写"的判据）。 */
        val stopped: Boolean = false,
        /** 这一份**走完了**收尾那一半（还在录 / INCREMENTAL = false，见 [SyncScope]）。 */
        val finalized: Boolean = false,
        /** 「暂停区间没报上去」那句如实的话（空 = 报上去了，或这一场没有暂停区间）。 */
        val pauseNote: String = "",
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

/**
 * 一轮同步**进行中**的进度（给界面看；[OfflineMeetingSync.run] 的 `onProgress` 每段报一次）。
 *
 * 为什么要单独一档、而不是等 [OfflineSyncResult]：那一份是**整轮结束**才有的结论，而
 * 一轮可能要几分钟（10 分钟的一段 ≈19 MB，走中继更慢）。中间那几分钟界面上必须有东西在动，
 * 否则用户刚按完「现在同步」看到的就是"按了没反应"。
 *
 * ⚠️ 它**只用于展示**：任何判定（顺序/续传/对账/收尾）都不许读它 —— 那些只看
 * `OfflineMeetingSync` 内部的清单与 transport 的返回值。
 */
data class SyncProgress(
    val drafts: Int,
    /** 这一轮要处理的总段数（界面 `已传 M / 共 N 段` 的 N）。 */
    val totalSegments: Int,
    /** 已经确认的段数（含前面几份清单里早就确认过的）。 */
    val confirmedSegments: Int,
    val pendingSegments: Int,
    /** **正在传**的段序号（1 起）。 */
    val currentSeq: Int,
    /**
     * 刚刚被服务端收下的段序号（`0` = 这是"某段**开始**传"那一次回调）。
     *
     * 为什么这一格必须有：一次"段开始"与一次"段结束"的 `currentSeq` 是**同一个数字**
     * （第 3 段开始时的"正在传第 3 段"、与它传完时的"刚传完第 3 段"），
     * 界面若只靠 `currentSeq` 变化去区分，就会把传完那一下**当成没变化**而漏掉刷新
     * —— 那正是"传完最后一段之后界面停住"的形状。
     */
    val justConfirmed: Int = 0,
    /**
     * 正在传的那一段**有多少字节**（`0` = 不知道）。
     *
     * 补传的前台通知要写"已传 2/5 段（第 3 段 12.4 MB）"（用户 2026-10-19 深夜点名的那句），
     * 而这个数只有"正在读那一段文件"的地方知道 —— 界面那边没有清单、也读不到文件。
     * 0 就**只报段号**，绝不编一个大小出来。
     */
    val currentBytes: Long = 0,
)

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
    /**
     * **这一段真的是哪种格式**（来自清单里 `DraftSegment.format`）。
     *
     * ⚠️ 传输层**必须**用它拼 `Content-Type`（`audio/flac` / `audio/wav`）：
     * 服务端要按它 + `fLaC` 魔数认格式。写死成 `audio/wav` 的话，FLAC 那些段会被
     * 当成 wav 收下 —— 那正是"谎报格式"（用户明确说不要），而且转写那边解不出来。
     *
     * 默认 WAV：老清单里没写格式的段确实就是 WAV（见 [SegmentFormat.DEFAULT]）。
     */
    val format: SegmentFormat = SegmentFormat.WAV,
) {
    // ByteArray 在 data class 里必须自己写 equals/hashCode（否则用例里的"传了哪几段"
    // 断言会永远不等 —— 那可是真的会让人误判"没传"）。
    override fun equals(other: Any?): Boolean =
        other is UploadRequest && other.clientId == clientId && other.seq == seq &&
            other.startedAt == startedAt && other.source == source &&
            other.format == format && other.wav.contentEquals(wav)

    override fun hashCode(): Int =
        ((((clientId.hashCode() * 31 + seq) * 31 + startedAt.hashCode()) * 31 +
            source.hashCode()) * 31 + format.hashCode()) * 31 + wav.contentHashCode()
}

/**
 * **上报暂停区间**的请求（`POST /api/meeting/pauses`，用户 2026-10-17 的第③件事）。
 *
 * 契约（两边一致）：body 是
 * ```json
 * {"clientId": "...", "pauses": [{"from": 12.5, "to": 31.0}]}
 * ```
 * * `from`/`to` 是**相对这一场开始时间**的秒数（与电脑侧 `meta["pauses"]` 同一个语义：
 *   暂停 = 不写盘、不产生音频）；
 * * **按 `clientId` 幂等**：整份重复报也没关系（服务端会合并去重）—— 所以"每新增一条
 *   暂停区间就再报一次"这件事不必先算差集，直接把当前的整份交上去就行。
 *
 * ⚠️ 为什么不发 `by`（`local`/`remote`）：用户给的契约里只有 `from`/`to`。
 * 多发的字段会被服务端静默忽略（等于没发），而"以为它在起作用"是最难查的错法 ——
 * 契约上没有的东西，客户端就不发明。
 */
data class PauseRequest(
    val clientId: String,
    val pauses: List<DraftPause>,
)

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
 * 同步需要的**全部**服务端能力（一个接口，方便测试注入 + 让"服务端要支持什么"一目了然）。
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

    /** 传一段（**裸音频**：`Content-Type` 按 [UploadRequest.format]；带上 `clientId`/`seq` 供服务端幂等）。 */
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

    /**
     * **上报暂停区间**（`POST /api/meeting/pauses`）—— 用户 2026-10-17 的第③件事。
     *
     * 为什么它是端口上**唯一**一个有默认实现的能力（其余几个都没有）：它是**附加**信息，
     * 而且老服务端**根本没有这个端点**（另一条线在同时实现）。用默认实现表达
     * "没有这个能力"是最诚实的做法：
     *  * 不实现它的注入式 transport（用例里的假服务端、将来的别的实现）自动得到
     *    [PauseOutcome.Unsupported] —— **这次没报上去**，而**不影响补传**；
     *  * 真实现（[com.echo.contact.service.OfflineMeetingTransport]）覆盖它去发请求，
     *    并把 404/405 翻成同一档 [PauseOutcome.Unsupported]。
     */
    suspend fun reportPauses(req: PauseRequest): PauseOutcome =
        PauseOutcome.Unsupported("这台服务端没有上报暂停区间的接口（这次没报上去）")
}
