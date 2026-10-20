package com.echo.contact.ui

/**
 * 手机回顾 = **PC 版那套交互**（开始 → 多轮对话 → 结束）的纯逻辑。
 *
 * ## 为什么要有这个文件（用户 2026-10-19 澄清的口径）
 *
 * 用户原话：*"我是说跟 pc 版的交互一样，开始回顾开始然后继续交互"*。
 * PC 版是"点开始 → 一轮轮说/打 → 结束"，不是"录一段长口述一次性交上去"。
 *
 * 所以这里描述的是**一场对话**：
 *  * [ReviewSession] = 一开场（`POST /api/daily-review/start` 成功后才有）；
 *  * 每一轮 = **两条消息**（我说的 + ECHO 回的 / 失败原因）；
 *  * 多轮天然是**追加**（[appendReviewRound] 只往 `messages` 后面接，绝不覆盖前面的）。
 *
 * ## 三条边界（与 ViewModel/界面里的注释是同一套，判据在 `ReviewConversationTest`）
 *
 *  1. **开始不碰麦**：手机点"开始"只建会话；真正开麦的是"按住说话"那一轮
 *     （走**唯一**那处 `startRecording`，用**手机的麦**）。
 *  2. **继续交互走既有指令链**：语音 `voice-command` / 文字 `assistant/command` ——
 *     回顾自己**没有**第二条录音路。但每一轮都带 [REVIEW_CONTEXT]：
 *     服务端据此把它记成 `commands.source='review'` —— **回顾轮次属于回顾**，
 *     于是它进「回顾历史」、**不再**在「指令历史」里重复一遍
 *     （用户 2026-10-20：*"回顾的历史对话，指令里也有能不重复吗？"*）。
 *  3. **标签不改执行**：带不带这个标记，指令照样发给智能体、结果照样回来；
 *     它只决定这一轮算哪段历史。老服务端不认这个 query → 行为退回原样
 *     （那些轮次落 `mobile`，也就仍出现在指令历史里）。
 *
 * 这个文件刻意**不碰 Android / ViewModel**：它是纯函数，JVM 门禁里能逐条钉住
 * （Compose 在 JVM 门禁里渲染不了，"两轮不覆盖"这种事只能靠纯逻辑断言）。
 */

// ---- 回顾轮次的上下文标记 / 幂等键 ---------------------------------------------------------

/**
 * 「回顾那一场的一轮」的**上下文标记**（发给服务端的 `?context=review`）。
 *
 * 为什么要有它：手机回顾走的是**既有指令链**，不打招呼的话服务端只能按默认记
 * `source='mobile'` —— 于是同一句话在「回顾页 + 指令历史」出现两次，而「回顾历史」
 * 里反倒没有（那边只读 `source='review'`）。这个标记让服务端把轮次记进回顾历史。
 *
 * ⚠️ 它**只是一个标签**：不带它的普通指令、一次性口述（`/daily-review/submit`）
 * 都不许带；带了也不改变执行（见 `EchoApi.voiceCommand` / `sendCommand` 的说明）。
 */
internal const val REVIEW_CONTEXT = "review"

/** 幂等键里不许出现的字符（服务端只收 `[A-Za-z0-9:_.-]`）。 */
private val REVIEW_ID_UNSAFE = Regex("[^A-Za-z0-9:_.-]")

/**
 * 这一轮的**幂等键**（`"<回顾会话 id>:<本场开始时刻>:<轮次>"`）。
 *
 * 为什么需要：万一同一轮被发两次（重试 / 界面重放），服务端按这个键认出"这轮已经
 * 记过了"，不会产生第二条回顾记录。键只由**这一场 + 这一轮**决定，所以两轮内容
 * 完全相同也仍是两个键（幂等的是"这一轮"，不是"这句话"）。
 *
 * ⚠️ **必须带 `startedAt`**：`round` 是 ViewModel 里的计数器，App 重启后从 1 重新数，
 * 而"今天那条回顾会话"的 `sessionId` 当天是复用的 —— 只写 `sessionId:round` 的话，
 * 重启后第一轮会撞上重启前某一轮的键，服务端会把它当成重复而**丢一轮真内容**。
 * 本场开始时刻（毫秒）把两场分开；同一场里它不变，所以重发仍是同一个键。
 *
 * 拿不到会话 / 开始时刻 / 轮次无效 → null（不发这个参数，老服务端与老通道行为不变）。
 */
internal fun reviewRequestId(sessionId: String, startedAt: Long, round: Long): String? {
    val sid = sessionId.replace(REVIEW_ID_UNSAFE, "").take(80)
    if (sid.isEmpty() || startedAt <= 0L || round <= 0L) return null
    return "$sid:$startedAt:$round"
}

// ---- 说话人 -------------------------------------------------------------------------------

/** 我说的那一句（语音转写出来的文本，或打字的内容）。 */
internal const val REVIEW_SPEAKER_ME = "我"

/** ECHO 回的那一句（成功=回复；失败=原因，见 [ReviewMessage.failed]）。 */
internal const val REVIEW_SPEAKER_ECHO = "ECHO"

/**
 * 一条消息。
 *
 * @property speaker [REVIEW_SPEAKER_ME] / [REVIEW_SPEAKER_ECHO]（用字符串而不是枚举：
 *   它只是"谁说的"这一个标签，界面直接画出来，不需要再映射一次）。
 * @property failed  **ECHO 那一条**专用：true = 这不是回复，而是**没做成的原因**。
 *   界面要把它画成"失败"的样子（红色），不能和正常回复混在一起。
 */
data class ReviewMessage(
    val speaker: String,
    val text: String,
    val failed: Boolean = false,
)
/**
 * 一场手机回顾。
 *
 * @property sessionId  电脑给的那条 DSH 会话（`/daily-review/start` 回的 `sessionId`）。
 * @property workspace  回顾工作区（电脑上的目录，界面上只用来"指向哪儿"，不参与判断）。
 * @property startedAt  本场开始时间（手机时钟；只用于显示/排障，不和服务端比时间）。
 * @property messages   **追加**的消息（一轮两条），顺序就是发生顺序。
 * @property rounds     本场已完成的轮数（= `messages.size / 2`，单独存一份是为了界面直接读）。
 * @property lastRoundId 最后一条被追加进对话的**轮次身份**（`SubmitState.roundId`）。
 *   它有两个用途：① 同一轮的结果被重复回调时**不重复追加**；② 开始这场之前那条
 *   还在路上的指令（id 更小）不会被算进这一场。
 */
data class ReviewSession(
    val sessionId: String = "",
    val workspace: String = "",
    val startedAt: Long = 0L,
    val messages: List<ReviewMessage> = emptyList(),
    val rounds: Int = 0,
    val lastRoundId: Long = 0L,
)

// ---- 各档原因（都必须是一句能显示的人话）--------------------------------------------------

/**
 * 没配对：连电脑地址都没有，**一个请求都不发**（发了只会变成一个超时，
 * 而用户需要知道的是"去设置里配对"）。
 */
internal const val REVIEW_NOT_PAIRED =
    "还没连上电脑（没配对）：去「设置 → 连接」填电脑地址并配对 —— 这次没开始回顾"

/** 已经开始过了，别重复开始（重复开始会把这一场的对话清掉，用户会以为它丢了）。 */
internal const val REVIEW_ALREADY_RUNNING =
    "这场回顾已经在进行了：按住下面的「按住说话」，或直接打字继续"

/** 已经结束了（再点一次"结束"不是失败，但也不能一声不吭）。 */
internal const val REVIEW_ALREADY_ENDED = "这场回顾已经结束了"

/** 没有进行中的回顾（结束/录一轮之前要先开始）。 */
internal const val REVIEW_NO_SESSION =
    "没有正在进行的回顾：先点「开始回顾」，再录这一轮"

/** 没在录音时点"说完了" —— 这次确实没提交，说清楚（别让点击像掉进黑洞）。 */
internal const val REVIEW_NOT_RECORDING =
    "没在录音：这次没有提交（按住「按住说话」说，或已经在提交了）"

/**
 * 还在录这一轮就想"结束回顾"：先说完这一轮。
 *
 * 为什么挡住：结束之后会议/口述卡会把这一轮当成"一次性口述"（另一条上传路），
 * 那段录音就没有"这一轮"的出口了 —— 用户会说"我说了一半，它什么都没记"。
 */
internal const val REVIEW_STILL_RECORDING =
    "还在录这一轮：先点「说完了」（这一轮会进这场对话），再结束回顾"

/** 开始那一步失败时的前缀（后面接服务端给的原因；`reviewFailureLine` 保证永不空话）。 */
internal const val REVIEW_START_FAILED = "开始回顾失败"

// ---- 界面文案 ----------------------------------------------------------------------------

/** 开始卡标题。 */
internal const val REVIEW_START_TITLE = "开始回顾"

/** 进行中标题（要求：顶部就写"回顾进行中"）。 */
internal const val REVIEW_RUNNING_TITLE = "回顾进行中"

/** 结束之后那张只读卡的标题。 */
internal const val REVIEW_LAST_TITLE = "刚才那一场"

/**
 * 开始卡上那句"这是 PC 版那套交互"。
 *
 * 为什么要点明"录的是这台手机的麦克风"：PC 版开始之后录的是**电脑**的麦；
 * 手机版必须用手机的（人在车里，电脑不在场），这是同一个交互、不同的采集源。
 */
internal const val REVIEW_START_HINT =
    "和 PC 版一样：点开始 → 一轮轮说或打字 → 结束。录的是这台手机的麦克风，不占电脑那支。"

/**
 * 「刚才那一场」卡片上的那句话（2026-10-20 起：这几轮**已经进回顾历史**）。
 *
 * 手机回顾的每一轮现在都带 `REVIEW_CONTEXT`，服务端记 `commands.source='review'`
 * —— 也就是说它们就在**下面的「历史回顾」**里（按天看得到原文与播报），
 * 不在「指令」历史里重复。所以这张卡只留一句**去向**，不再把整段对话重画一遍
 * （重画一遍等于同一个页面里又看了一次同样的内容）。
 */
internal const val REVIEW_LAST_ROUNDS_NOTE =
    "这几轮已记入回顾历史：在下面的「历史回顾」里按天可见（不再重复进「指令」历史）。"

/** 一次性口述入口的定位（降级为"退路"，不是主路径）。 */
internal const val REVIEW_DICTATE_FALLBACK =
    "退路：不想开一场回顾时，也可以用上面的「车里口述」录一段（一次性的，不进这场对话）。"

/** 失败/空回复时用于占位的两句话（绝不画一条空白消息）。 */
private const val REVIEW_EMPTY_MINE = "（这一轮没有可显示的原话）"
private const val REVIEW_EMPTY_REPLY = "（ECHO 没有回复文本）"
private const val REVIEW_EMPTY_FAILURE = "（这一轮失败了，但没拿到原因）"

// ---- 一轮 = 两条消息（追加，不覆盖）-------------------------------------------------------

/**
 * 把**一轮**追加进这场对话：我说的 + ECHO 回的（或失败原因）。
 *
 * 为什么一轮是两条而不是一条：PC 版看到的就是一来一回 —— 只有 ECHO 的回复时，
 * 用户不知道"它回的是我哪句话"；只有我的话时，又看不出这轮成没成。
 *
 * ⚠️ **追加**：[session.messages] 原样保留，只在后面接两条。这是"多轮对话"的字面要求，
 * 也是回归钉子（`ReviewConversationTest` 断言 2 轮的场次 = 4 条，不是 2 条）。
 *
 * @param roundId 这一轮的身份（`SubmitState.roundId`）—— 记进 [ReviewSession.lastRoundId]，
 *   用于挡住同一轮的重复回调。
 * @param mine    我说的（语音时是转写结果；拿不到文本时写"（语音）"这类占位）。
 * @param echo    ECHO 的回复；[failed]=true 时传**失败原因**。
 */
internal fun appendReviewRound(
    session: ReviewSession,
    roundId: Long,
    mine: String,
    echo: String,
    failed: Boolean = false,
): ReviewSession {
    val mineText = mine.trim().ifEmpty { REVIEW_EMPTY_MINE }
    val echoText = echo.trim().ifEmpty {
        if (failed) REVIEW_EMPTY_FAILURE else REVIEW_EMPTY_REPLY
    }
    return session.copy(
        messages = session.messages + listOf(
            ReviewMessage(speaker = REVIEW_SPEAKER_ME, text = mineText),
            ReviewMessage(speaker = REVIEW_SPEAKER_ECHO, text = echoText, failed = failed),
        ),
        rounds = session.rounds + 1,
        lastRoundId = roundId,
    )
}

/**
 * 这一轮的结论该不该追加进对话。
 *
 * 两条规矩：
 *  1. **同一轮不重复**：`roundId > lastRoundId` 才追加（`StateFlow` 会把同一个值再发一次，
 *     界面切换/重放也可能再看到一次；重复追加会让一轮变成四行）。
 *  2. **还在路上的不追加**：`Sending/Accepted/Running` 时回复还没到（`active=true`），
 *     这时把"（语音，正在转写…）"当成我的原话记下来，等真结果到了又追加一条 = 一轮两条变四条。
 *
 * 用"严格大于"还有一个好处：开始这场之前那条还在路上的指令（`roundId` 更小）
 * 不会被算进这一场（ViewModel 把 `lastRoundId` 初始化成开始那一刻的轮次）。
 */
internal fun reviewShouldAppend(
    session: ReviewSession?,
    roundId: Long,
    active: Boolean,
): Boolean {
    if (session == null) return false
    if (active) return false
    if (roundId <= 0L) return false
    return roundId > session.lastRoundId
}

// ---- 顶部那一行：今日轮数 + 本场轮数 -------------------------------------------------------

/**
 * 「回顾进行中」下面那一行。
 *
 * 为什么两个数都要：`todayRounds` 来自服务端的**历史回顾**（电脑那支麦做的），
 * `sessionRounds` 是**这一场**（手机的对话）。只显示一个都会误导：
 *  * 只显示本场 → 用户以为"今天才做了 1 轮"（其实电脑那边已经做过）；
 *  * 只显示今日 → 用户看不出"这一场进行到第几轮了"。
 */
internal fun reviewRunningLine(todayRounds: Int, sessionRounds: Int): String =
    "本场 $sessionRounds 轮 · 今日历史 $todayRounds 轮"

// ---- 结束：本地与主机是两件事 --------------------------------------------------------------

/**
 * 结束之后那句话：**本地**（这场对话到此为止、保留成只读卡）与**主机**（`/stop` 回的三态）
 * 分开说。
 *
 * 为什么必须分开：这是两条独立的事实 ——
 *  * 本地结束是手机自己的状态（一定成立）；
 *  * 主机那侧可能本来就没在跑（`stopped=false`），也可能拿不到 `stopped`（老服务端）。
 * 把两者合成一句"已停止"就是一次假成功（[reviewStopNote] 的三态就是为此存在的）。
 */
internal fun reviewEndedLine(rounds: Int, stopped: Boolean?): String =
    "这场回顾已结束：本场 $rounds 轮，已记入「回顾历史」" +
        "（电脑那边：${reviewStopNote(stopped)}）"

// ---- 录一轮之前的闸门（复用 dictationGate 的四档口径）--------------------------------------

/**
 * 「按住说话」（这一轮）按下去能不能开始。
 *
 * 先判"这场还在不在"（[REVIEW_NO_SESSION]），其余**整段复用** [dictationGate]：
 * 没给麦克风权限 / 已经在录 / 会议录音占着 / 「一直听着」占着 —— 四档原因与口述那条路
 * **逐字相同**（合成一句"开不了麦"等于什么都没说，用户不知道去关哪个）。
 *
 * ⚠️ 与 [dictationGate] 一样，这里**没有** `SubmitState` 这个入参：
 * "上一条指令还在路上"不该挡住"录这一轮"（那是两件事）。
 */
internal fun reviewRoundGate(
    inSession: Boolean,
    hasPermission: Boolean,
    alreadyRecording: Boolean,
    meetingRecording: Boolean,
    listening: Boolean,
): DictationGate {
    if (!inSession) return DictationGate.Blocked(REVIEW_NO_SESSION)
    return dictationGate(
        hasPermission = hasPermission,
        alreadyRecording = alreadyRecording,
        meetingRecording = meetingRecording,
        listening = listening,
    )
}
