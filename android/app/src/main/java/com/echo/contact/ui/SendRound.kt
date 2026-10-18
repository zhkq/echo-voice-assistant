package com.echo.contact.ui

import com.echo.contact.data.dto.CommandItem

/**
 * 「这一次到底发出去了没有；没出去要说哪句话；这一轮的结果凭什么算**这一轮**的」
 * —— 全部收敛成**纯函数**，所以 JVM 门禁里能逐条钉住（`SendRoundTest`）。
 *
 * ## 为什么这一轮必须做这件事（2026-10-18 真机 bug）
 *
 * 用户原话：*"刚才我测试从手机发指令，结果执行的不是我的语音内容而是测试"*。
 *
 * 服务端取证是硬结论：13:00 之后**一条 mobile 指令都没有**、`echo-server.log` 里
 * **13:00 之后没有任何一条 POST** —— 也就是说手机的"发送"**根本没出网**。
 * 而它同时**没有报错**：那几个失败分支只把原因写进 `_speakNote`，而 `speakNote`
 * 在整个 App 里**没有任何界面在读**（`MainViewModel.kt:282` 声明、`:283` 暴露，
 * 界面文件一次都没引它）→ 用户看到的只有"点了没反应"。
 *
 * 更糟的是"没反应"之后界面**没有清掉上一轮**：`_submit` 还是旧的那一轮
 * （或者干脆是 `Idle` → 「这一轮」卡片整块不画），眼前只剩「最近的指令」历史列表，
 * 而那份历史**只在 init / 手动刷新时拉过**。于是**旧数据顶替**了这次的结果。
 *
 * ## 三条不许破的规矩（下面每个函数都是它们的落点）
 *
 * 1. **发失败必须说**。任何一个"不发"的出口都要带着一句人话回到界面上；
 *    绝不 `return` 了事。
 * 2. **绝不许拿旧数据顶替**。失败态一律 `reply = ""`（[failedRound]），
 *    而且界面只在 [showsReply] 为真时显示回复 —— 只有 `Done` 才算"这一行是我这条"。
 * 3. **语音没听清 ≠ 发了"测试"**。电脑回"我受理了"但识别结果为空
 *    （[voiceHearNote]）时**既不发也不认领**，就地让用户再说一次。
 */

// ---------------------------------------------------------------------------------------------
// 一、发送前的闸门：发不发得出去（发不出去就**必须**给一句人话）
// ---------------------------------------------------------------------------------------------

/** 一次点击的结果：要么真去发，要么带着原因挡下来。 */
internal sealed interface SendGate {
    /** 可以发。 */
    data object Go : SendGate

    /**
     * 这次点击**没有**发出任何请求；[reason] 必须就地显示在卡片上。
     *
     * [keepRound] = true 表示"上一轮还在这张卡上，别把它擦掉"：只在"上一条仍在提交中"
     * 这一种情况下为真 —— 那条还在等电脑回话，把它整块换成失败态会让用户以为它丢了。
     * 其它情况（没配对 / 没文字）都换成一个新的失败态，顺手把上一条回复清掉，
     * 免得旧回复留在那儿被当成这次的答复。
     */
    data class Blocked(val reason: String, val keepRound: Boolean) : SendGate
}

/** 输入框是空的（按钮虽然 disabled，但别指望调用方都记得判）。 */
internal const val NOTE_EMPTY_TEXT = "没有文字可发：先打一句，或者点「说一句」"

/** 没配对 / 没有电脑地址。 */
internal const val NOTE_NOT_PAIRED =
    "还没连上电脑（没配对）：去「设置 → 连接」填电脑地址并配对，这条没发出去"

/** 上一条还在提交中（**不是**静默丢掉，要如实说）。 */
internal const val NOTE_SEND_BUSY =
    "上一条还在等电脑回话，这条没发出去（等它回来再发，或点右上角刷新）"

/**
 * 发文字指令之前的判断。
 *
 * @param text  用户输入（已 trim 或没 trim 都行，这里自己判空白）
 * @param phase 当前这一轮的阶段（`Sending` = 上一条还在路上）
 * @param paired 是否已经有电脑地址（`MainViewModel._baseUrl` 非空）
 */
internal fun sendGate(text: String, phase: SubmitPhase, paired: Boolean): SendGate = when {
    text.isBlank() -> SendGate.Blocked(NOTE_EMPTY_TEXT, keepRound = false)
    !paired -> SendGate.Blocked(NOTE_NOT_PAIRED, keepRound = false)
    phase == SubmitPhase.Sending -> SendGate.Blocked(NOTE_SEND_BUSY, keepRound = true)
    else -> SendGate.Go
}

// ---------------------------------------------------------------------------------------------
// 二、语音那条路的失败文案（以前全写进没人读的 speakNote）
// ---------------------------------------------------------------------------------------------

/** 开不了麦：这条路一个请求都发不出去，必须说出来。 */
internal const val NOTE_MIC_UNAVAILABLE =
    "开不了麦克风（可能被别的应用占着，或权限没给）：这条没发出去"

/** 电脑"受理了"但识别结果为空 —— 没听清，**不发也不认领**。 */
internal const val NOTE_NO_HEARING = "没听清（电脑没识别出文字）：这条没发出去，再说一次"

/** 录音太短 / 没采到数据时那句话（带实际时长，用户才知道是不是自己按快了）。 */
internal fun recordingTooShortNote(recordedMs: Long): String {
    val secs = (recordedMs.coerceAtLeast(0L) / 100.0).let { "%.1f".format(it) }
    // 文案对「指令」的按住说话与回顾的口述都成立（两处共用同一句，不各写一份）
    return "没录到声音（只录到 $secs 秒）：这条没发出去，请再说一遍（说完点「说完了」）"
}

/**
 * 电脑回"受理了"之后，它到底**听成了什么**。
 *
 * 返回值：空串 = 有可用文本，可以继续等结果；非空 = 直接当失败原因显示（[NOTE_NO_HEARING]）。
 *
 * 为什么空白要**返回原因**而不是"发个空串"：`awaitResult` 会拿这个文本去认领新行，
 * 空串会匹配到"文本为空"的任何一条（包括历史），这正是"旧数据顶替"的一个入口。
 */
internal fun voiceHearNote(recognized: String?): String =
    if (recognized.isNullOrBlank()) NOTE_NO_HEARING else ""

// ---------------------------------------------------------------------------------------------
// 三、这一轮的身份：谁能写界面
// ---------------------------------------------------------------------------------------------

/**
 * 这次异步回调还该不该写界面 —— **只认序号**。
 *
 * 为什么需要：发送失败/超时之后那条迟到回包（甚至上一轮的 `awaitResult`）会在
 * 几秒后才回来。它要是照样写 `_submit`，就会把**新一轮**（或刚显示的失败原因）覆盖成
 * 上一轮的回复 —— 表现正是"显示/念出来的不是这次的"。
 */
internal fun ownsRound(current: SubmitState, roundId: Long): Boolean = current.roundId == roundId

/** 按序号把关的一次状态更新：不归我就不动（返回原值）。 */
internal fun applyRound(
    current: SubmitState,
    roundId: Long,
    next: (SubmitState) -> SubmitState,
): SubmitState = if (ownsRound(current, roundId)) next(current) else current

/**
 * 开一轮新的（发送 / 说完了）。
 *
 * 关键点：[SubmitState.reply] 从空开始 —— 所以"这一轮"永远不会带着上一轮的答复出现。
 */
internal fun startingRound(
    roundId: Long,
    text: String,
    phase: SubmitPhase,
    message: String,
    startedAt: Long = 0L,
): SubmitState = SubmitState(
    text = text,
    phase = phase,
    message = message,
    reply = "",
    speak = "",
    error = "",
    roundId = roundId,
    startedAt = startedAt,
)

/**
 * 一次**没发出去**的尝试：失败态 + 原因 + **空回复**。
 *
 * `reply` 显式写空是这条规矩的落点：失败的时候界面上一个字都不许来自旧数据。
 */
internal fun failedRound(
    roundId: Long,
    text: String,
    reason: String,
    startedAt: Long = 0L,
): SubmitState = SubmitState(
    text = text,
    phase = SubmitPhase.Failed,
    message = reason,
    reply = "",
    speak = "",
    error = reason,
    roundId = roundId,
    startedAt = startedAt,
)

/** 给**当前这一轮**加一句"刚才那次点击没发出去"的提示（不擦掉还在路上的那一轮）。 */
internal fun withBlockedNote(state: SubmitState, reason: String): SubmitState =
    state.copy(blocked = reason)

/**
 * 卡片上能不能显示回复。
 *
 * **只有 `Done` 可以**：`Done` 只可能是 `awaitResult` 认领到"这一轮自己那一行"
 * （`id > 发送前的最新 id`）之后写出来的，所以那时 `reply` 一定是本条的。
 * 其它阶段（尤其 `Failed`）即使 `reply` 里还留着什么，也一个字都不许摆出来。
 */
internal fun showsReply(phase: SubmitPhase): Boolean = phase == SubmitPhase.Done

/** 卡片上"这一轮"那一行：把**这次发的原话**摆出来，用户才能确认没发错。 */
internal fun roundHeadline(state: SubmitState): String =
    state.text.ifBlank { "（语音）" }

// ---------------------------------------------------------------------------------------------
// 四、认领下界：别把旧指令当成本次结果
// ---------------------------------------------------------------------------------------------

/**
 * 一组指令里最大的 id（`0` = 一条都没有）。
 *
 * 它就是"发送前的最新一条"，也就是 [pickSubmitted] 的下界来源。
 * 为什么不直接用本地列表的最大值：本地列表**可能很旧**（自动刷新不刷指令列表，
 * 见 `MainViewModel.autoRefreshTick`），旧下界会让"发送前就存在、但比本地列表新"的
 * 历史行也通过 `id > maxIdBefore` 这一关 —— 那正是"认领到旧记录"。
 * 所以调用方要取 `max(本地最大值, 发送前现拉的头部最大值)`，见
 * `MainViewModel.claimFloorId`。
 */
internal fun newestCommandId(items: List<CommandItem>): Int =
    items.maxOfOrNull { it.id ?: 0 } ?: 0
