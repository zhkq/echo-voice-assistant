package com.echo.contact.ui

import com.echo.contact.service.ListenState

/**
 * 「口述回顾」这个动作**能不能开始**，以及**为什么不能**（纯逻辑，JVM 可测）。
 *
 * ## 为什么要有这个文件（2026-10-18 深夜真机 bug：回顾发不起来）
 *
 * 用户原话：*"我刚才用手机回顾，不管回顾页面上的按钮，还是界面上按钮都没能发起回顾"*。
 * 电脑侧取证是：`POST /api/stt/transcribe` **一次都没有**、`POST /api/daily-review/submit`
 * 也没有 —— 也就是说手机在**录音开始之前**就断了，连音频都没上传。
 *
 * 而当时的代码把"断在哪"这件事吞掉了：
 *  * 回顾页（`ReviewTab`）只在 `granted == true` 时试着开录 —— **权限没给就什么都不做**；
 *  * 首页回顾卡（`SuperAssistantTab`）把 `startRecording()` 的 `Boolean` 直接扔掉，
 *    开不了麦什么都不显示（而且 `startRecording()` 还会把这次失败写进 `_submit`，
 *    也就是**发指令**那一轮的卡片 —— 那是另一件事）。
 *
 * 所以这里把判定提成**一条纯函数**：两个入口都走它，任何"不开始"都带着一句能显示、
 * 能指导下一步的话（[DictationGate.Blocked.reason]）。界面只负责把这句话摆在
 * **按下的那颗按钮旁边**，不许判断、不许自己编文案。
 *
 * ## 回顾口述 ≠ 发指令（这条是这一轮最要紧的口径）
 *
 * 两者共用同一个录音器（`MainViewModel.startRecording`）与同一套「说完了」收尾，
 * 但它们是**两个动作**：
 *  * 发指令的那一轮结果属于 `_submit`（"正在提交…/已提交/回复"）；
 *  * 回顾口述属于 `_dictateNote` + `_reviewSubmittingNote`（"正在转写这段口述…"）。
 *
 * 所以判定里**没有** `SubmitState` 这个入参 —— "上一条指令还在发（blocked）"**不该**
 * 挡住回顾口述（那条路一个字节都不碰 `_submit`）。这条是回归钉子：见
 * `DictationEntryTest` 里"blocked 不该挡住回顾口述"。
 */
internal sealed class DictationGate {
    /** 可以开始：调用方接着走既有的 `MainViewModel.startRecording`。 */
    data object Go : DictationGate()

    /**
     * 这次**没有开始录**（或已经在录、不用再开），必须把 [reason] 就地显示出来。
     *
     * 为什么"已经在录"也算这里：用户再按一次时屏幕上什么都不变，看着就像按钮坏了 ——
     * 一句话"已经在录了，说完点「说完了」"就能把这条路指清楚。
     */
    data class Blocked(val reason: String) : DictationGate()
}

// ---- 五档原因：**互不相同**，每一档都告诉用户下一步做什么 -------------------------------
//
// 用户 2026-10-18 明确要求："没给麦克风权限 / 麦克风正被录音占用 / 开不了麦"要**分开说**。
// 合成一句"开不了麦克风（可能没权限也可能被占）"等于什么都没说 —— 用户不知道去关哪个。

/** 没给 `RECORD_AUDIO`：连试都不用试，直接说清去哪儿打开。 */
internal const val DICTATE_NO_PERMISSION =
    "没给麦克风权限：点「允许」或去系统设置打开它（这次没开始录）"

/** 已经在录（同一个录音器）：别再开一次，指路「说完了」。 */
internal const val DICTATE_ALREADY =
    "已经在录了：说完点「说完了」，再按不会重新开始"

/** 手机麦正被**会议录音**（`MeetingMicService`）占着 —— 那是本 App 自己的另一路录制。 */
internal const val DICTATE_MIC_MEETING =
    "麦克风正被会议录音占用：先结束会议录音（这次没开始录）"

/** 手机麦正被**常驻听写**（`ListenService`，"一直听着"）占着。 */
internal const val DICTATE_MIC_LISTEN =
    "麦克风正被「一直听着」占用：去「指令」页停掉它"

/** 别的应用占着麦 / 这台设备不给开（`AudioRecord` 建不起来）。 */
internal const val DICTATE_MIC_UNAVAILABLE =
    "开不了麦克风（被别的应用占着，或设备不给开）：这次没开始录"

/**
 * 麦**开起来了却一个字节都没读到**（`AudioRecord.startRecording()` 成功、读回 0 字节）。
 *
 * 这是"看着在录、其实什么都没录"的那一档：真机上最容易被当成"按钮没反应" ——
 * 计时一直 0.0 秒，按「说完了」只会得到"录得太短"。所以录满 `SILENT_MIC_MS`（约 3 秒）
 * 还没有任何字节就当场停掉并说这句话（别让用户对着一个假的"录音中"说话）。
 */
internal const val DICTATE_SILENT =
    "麦克风一个字都没收到（多半被占着）：这次没有录音，已停止"

/** 用户自己按了「取消」：不是失败，但也不能一声不吭。 */
internal const val DICTATE_CANCELED = "这次录音取消了，没有发出去"

/**
 * 常驻听写现在**是不是真的握着麦克风**。
 *
 * 只有 [ListenState.Phase.STARTING] / [ListenState.Phase.LISTENING] 在采音；
 * `THINKING`/`SPEAKING`/`PAUSED` 期间服务自己已经把麦放掉了（见 `ListenService` 类注释的取舍 1），
 * 那几档要是也判成"被占用"，用户就会在**明明能录**的时候被告知不能录。
 */
internal fun listenHoldsMic(phase: ListenState.Phase): Boolean =
    phase == ListenState.Phase.STARTING || phase == ListenState.Phase.LISTENING

/**
 * 「口述回顾」按下去能不能开始。
 *
 * 顺序是有讲究的（先"根本不可能"，后"临时被占"），因为一次只能给一句原因：
 *  1. 没权限 —— 用户不动手就永远开不了；
 *  2. 已经在录 —— 不需要再开，指路「说完了」；
 *  3. 会议录音占着、4. 常驻听写占着 —— 都是本 App 自己的另一路录制，能当场关掉；
 *  5. 其余情况放行，真开不起来时由 [micFailReason] 补一句（"别的应用占着 / 设备不给开"）。
 *
 * ⚠️ 这里**没有** `SubmitState` / `sendGate` 这类入参：回顾口述与发指令是两个动作，
 * "上一条还在发"不该挡住它（判据：`DictationEntryTest` 的回归钉子）。
 */
internal fun dictationGate(
    hasPermission: Boolean,
    alreadyRecording: Boolean,
    meetingRecording: Boolean,
    listening: Boolean,
): DictationGate = when {
    !hasPermission -> DictationGate.Blocked(DICTATE_NO_PERMISSION)
    alreadyRecording -> DictationGate.Blocked(DICTATE_ALREADY)
    meetingRecording -> DictationGate.Blocked(DICTATE_MIC_MEETING)
    listening -> DictationGate.Blocked(DICTATE_MIC_LISTEN)
    else -> DictationGate.Go
}

/**
 * `startRecording()` 真的返回 false 时（麦建不起来）该说哪一句。
 *
 * 为什么还要按现场状态再分一次：`Recorder.start()` 只能回一个布尔（它看不见"是谁占着麦"），
 * 而"会议录音占着"与"别的应用占着"对用户来说要采取的动作完全不同。
 */
internal fun micFailReason(meetingRecording: Boolean, listening: Boolean): String = when {
    meetingRecording -> DICTATE_MIC_MEETING
    listening -> DICTATE_MIC_LISTEN
    else -> DICTATE_MIC_UNAVAILABLE
}
