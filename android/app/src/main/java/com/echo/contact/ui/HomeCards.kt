package com.echo.contact.ui

/**
 * 首页下半部分那**三张功能卡**（用户 2026-10-18 中午的原话）：
 *
 * > *"下方的主要功能，应该用卡片形式，里面要有功能按钮，不用跳转到对应功能页再开始使用功能"*
 *
 * ## 这一版与上一版的区别（这是本轮改动的**语义**，不是排版）
 *
 * 上一版首页是三个"入口"：每张卡整块可点，点了**切到对应页签**，功能要在那一页才用得上。
 * 这一版三张卡各自带**能直接干活的按钮**：
 *
 * | 卡片 | 卡上的按钮 | 按下去做的事 |
 * | --- | --- | --- |
 * | 指令 | 发送 | `MainViewModel.sendCommand(...)`（在**本页**发出，回复也回显在本页） |
 * | 会议 | 开始录音 / 结束录音 | `MainViewModel.startMeeting()` → `MeetingMicService.start(...)`；结束走 `MeetingMicService.stop(...)` |
 * | 回顾 | 口述回顾 / 说完了 / 取消 | `MainViewModel.startRecording()` → `submitReviewDictation()`（既有那条口述整理路） |
 *
 * **唯一会切页的东西**是每张卡右下角那个小链接（「看全部指令 / 看会议列表 / 看回顾历史」），
 * 以及指令卡在"回复太长"时给的「去看详情」。
 *
 * ## 为什么把"卡片内容"做成纯数据
 *
 * 这一轮最容易被改坏的是"按钮到底直接干活还是切页"。Compose 在 JVM 门禁里断言不了点击，
 * 但这个问题的答案其实就是**数据**：每个按钮的动作是什么、那个动作会不会切页。
 * 所以把三张卡描述成 [HomeCardSpec]，判据（`HomeCardsTest`）就能逐条钉住：
 * 三个卡片齐全、每个都有直接干活的按钮、按钮动作**不改变页签**、录音态文案与时长。
 *
 * ## 为什么按钮复用既有入口（不新造）
 *
 * 录音这一轮只有**一条**路：`MeetingMicService.start(...)`（手机麦 + 本地清单 + 补传）。
 * 卡上的「开始录音」走的是 `MainViewModel.startMeeting()` —— **就是**会议页那颗按钮调的那个方法。
 * 在这层另写一遍"开始录音"等于给同一件事开第二条路，两条路迟早对不上
 * （本仓库 2026-10-17 刚因为"手机发起、录的却是电脑麦"付出过代价）。
 */

/** 三张功能卡的身份（顺序 = 卡片从上到下的顺序）。 */
internal enum class HomeCardKind(val title: String) {
    COMMAND("指令"),
    MEETING("会议"),
    REVIEW("回顾"),
}

/**
 * 卡上按钮要做的事。
 *
 * [switchesTab] 是这条设计的**核心判据**：除了"去看"（[OPEN_TAB]）之外，
 * 所有动作都是**直接执行**，都不改变当前页签。
 */
internal enum class HomeAction(val switchesTab: Boolean) {
    /** 在本页发出这条指令（`MainViewModel.sendCommand`）。 */
    SEND_COMMAND(false),

    /**
     * **在本页**录一段语音指令：按住 = `MainViewModel.startRecording`，
     * 松手 = `MainViewModel.finishRecording`（就是「指令」页那颗「说一句」的两个动作）。
     *
     * 为什么 2026-10-18 这一版才补上：上一版这张卡只有"打字 + 发送"，
     * **没有任何语音入口** —— 可用户说的"从手机发指令"指的就是说话。
     * 卡片的整个卖点是"不用跳到对应功能页再开始用"，语音却是这个 App 的主动作，
     * 于是这里必须有它，而且必须是"按住说话"（按下就录、松手就发）。
     * 它走的还是既有那两个方法，**不新造录音路**。
     */
    DICTATE_COMMAND(false),

    /** 开始手机麦录音（`MainViewModel.startMeeting` → `MeetingMicService.start`）。 */
    START_MEETING(false),

    /** 结束手机麦录音（`MeetingMicService.stop`，既有唯一收尾路）。 */
    STOP_MEETING(false),

    /** 开始口述回顾（`MainViewModel.startRecording`，回顾页那张「车里口述」卡同一个入口）。 */
    DICTATE_REVIEW(false),

    /** 口述说完了 → 转写 + 交给回顾整理（`MainViewModel.submitReviewDictation`）。 */
    SUBMIT_REVIEW(false),

    /** 放弃这次口述（`MainViewModel.cancelReviewDictation`）。 */
    CANCEL_REVIEW(false),

    /** **唯一**会切页的动作：去看完整页面（卡片小链接 / 回复太长时的「去看详情」）。 */
    OPEN_TAB(true),
}

/** 一个按钮：文案 + 动作（文案由状态决定，见 [meetingCardButtons]）。 */
internal data class HomeCardButton(val label: String, val action: HomeAction)

/**
 * 一张功能卡（纯数据）。
 *
 * [tab] 是这张卡对应的页签：点**卡片标题**仍然能进那一页（用户明确要求"底部 5 页导航照旧有效"，
 * 卡片只是**快捷方式**、不是替代）。[buttons] 里的动作**不**切页。
 */
internal data class HomeCardSpec(
    val kind: HomeCardKind,
    val title: String,
    /** 卡片上那句说明（沿用 `ASSISTANT_ENTRIES` 的原文，不在两处各写一份）。 */
    val subtitle: String,
    val tab: EchoTab,
    /** 卡上"直接干活"的按钮（至少一个）。 */
    val buttons: List<HomeCardButton>,
    /** 右下角那个小链接的文案（点它才切页）。 */
    val linkLabel: String,
)

/**
 * 一次点击的**结果**：只可能是"直接干活"或"切到某一页"，没有第三种。
 *
 * 这样"点按钮会不会跳页"就不用靠人眼盯界面：动作在数据层就没有"页签"这个字段，
 * 它**不可能**把页面换掉（判据见 `HomeCardsTest` 的 `tabAfterClick` 那几条）。
 */
internal sealed class HomeEffect {
    /** 直接执行（页签不变）。 */
    data class Direct(val action: HomeAction) : HomeEffect()

    /** 切到某一页（**只**给 [HomeAction.OPEN_TAB]）。 */
    data class SwitchTab(val tab: EchoTab) : HomeEffect()
}

/**
 * 点 [button] 会发生什么。
 *
 * [card] 只用来回答"切到哪一页"——小链接永远指向**这张卡自己的页**。
 * 注意这里**不**接受一个"随便哪个 tab"：动作要切页时，目标只能是卡片对应的页，
 * 免得某天有人拿它去做别的跳转，把"卡片按钮只在本页干活"这条悄悄破掉。
 */
internal fun effectOf(card: HomeCardSpec, button: HomeCardButton): HomeEffect =
    if (button.action.switchesTab) HomeEffect.SwitchTab(card.tab) else HomeEffect.Direct(button.action)

/**
 * 把一次点击作用到"当前页签"上 —— **状态级**判据的唯一落点。
 *
 * `Direct` 原样返回当前页签（= 点按钮不改变页签）；`SwitchTab` 才换页。
 * 做成纯函数是为了能在 JVM 里断言"点了按钮页签没变"，而不是在真机上点着看。
 */
internal fun tabAfterClick(currentTabIndex: Int, effect: HomeEffect): Int = when (effect) {
    is HomeEffect.Direct -> currentTabIndex
    is HomeEffect.SwitchTab -> effect.tab.ordinal
}

/** 会议卡那颗按钮：录音中 = 「结束录音」，否则 = 「开始录音」（同一个位置换文案）。 */
internal fun meetingCardButtons(recording: Boolean): List<HomeCardButton> =
    if (recording) {
        listOf(HomeCardButton("结束录音", HomeAction.STOP_MEETING))
    } else {
        listOf(HomeCardButton("开始录音", HomeAction.START_MEETING))
    }

/** 回顾卡那几颗按钮：口述中 = 「说完了 / 取消」，否则 = 「口述回顾」。 */
internal fun reviewCardButtons(dictating: Boolean): List<HomeCardButton> =
    if (dictating) {
        listOf(
            HomeCardButton("说完了", HomeAction.SUBMIT_REVIEW),
            HomeCardButton("取消", HomeAction.CANCEL_REVIEW),
        )
    } else {
        listOf(HomeCardButton("口述回顾", HomeAction.DICTATE_REVIEW))
    }

/**
 * 三张卡（顺序 = 指令 / 会议 / 回顾）。
 *
 * [micRecording] = `MeetingMicService.state.active`（手机正在录会议）；
 * [reviewDictating] = `MainViewModel.recording`（正在录口述回顾）。
 *
 * 两个都是**现有状态**（既不新造开关，也不猜）。两者不会同时为真：录音器是同一个，
 * 会议录音开着时回顾口述起不来（这也是为什么卡片上不需要"另一个在录"的提示）。
 */
internal fun homeCards(micRecording: Boolean, reviewDictating: Boolean): List<HomeCardSpec> =
    ASSISTANT_ENTRIES.map { entry ->
        when (entry.tab) {
            EchoTab.COMMANDS -> HomeCardSpec(
                kind = HomeCardKind.COMMAND,
                title = entry.title,
                subtitle = entry.subtitle,
                tab = entry.tab,
                // ⚠️ 顺序有讲究：「发送」在**最前**（它是这张卡的主按钮，判据按 `buttons.first()` 取主按钮）；
                // 「按住说话」紧随其后 —— 用户说的"从手机发指令"多半就是说话。
                buttons = listOf(
                    HomeCardButton("发送", HomeAction.SEND_COMMAND),
                    HomeCardButton("按住说话", HomeAction.DICTATE_COMMAND),
                ),
                linkLabel = "看全部指令",
            )
            EchoTab.MEETINGS -> HomeCardSpec(
                kind = HomeCardKind.MEETING,
                title = entry.title,
                subtitle = entry.subtitle,
                tab = entry.tab,
                buttons = meetingCardButtons(micRecording),
                linkLabel = "看会议列表",
            )
            EchoTab.REVIEW -> HomeCardSpec(
                kind = HomeCardKind.REVIEW,
                title = entry.title,
                subtitle = entry.subtitle,
                tab = entry.tab,
                buttons = reviewCardButtons(reviewDictating),
                linkLabel = "看回顾历史",
            )
            // ASSISTANT_ENTRIES 只有这三项（`EchoTabsTest` 钉住）；真走到这里说明那张表被改坏了，
            // 与其静默少一张卡，不如在构建期就让 `when` 报穷尽性错误。
            else -> error("ASSISTANT_ENTRIES 里出现了不该有的页签：${entry.tab}")
        }
    }

// ---------------------------------------------------------------------------------------------
// 卡片里那两句"回显"的纯函数（判据在 HomeCardsTest）
// ---------------------------------------------------------------------------------------------

/** 指令卡上"最近一条回复"最多显示多少字（长回复截断，剩下的去看详情）。 */
internal const val HOME_REPLY_PREVIEW_CHARS = 120

/** 一段回复在卡片上的样子：先截断，再说"是不是被截断了"。 */
internal data class ReplyPreview(val text: String, val truncated: Boolean)

/**
 * 回复 → 卡片上那两三行。
 *
 * 为什么要截断：这是一张**卡片**，不是详情页。一整篇纪要塞进去会把下面的卡片挤没，
 * 而且用户要的是"它答了没"，细节他有「看全部指令」那条路。
 * [truncated] = true 时界面会给一个「去看详情」（那是**唯一**允许切页的那种按钮）。
 */
internal fun replyPreview(reply: String, maxChars: Int = HOME_REPLY_PREVIEW_CHARS): ReplyPreview {
    val text = reply.trim()
    if (text.isEmpty()) return ReplyPreview("", false)
    if (maxChars <= 0 || text.length <= maxChars) return ReplyPreview(text, false)
    return ReplyPreview(text.take(maxChars).trimEnd() + "…", true)
}

/**
 * 秒 → `mm:ss`（超过一小时给 `h:mm:ss`）。
 *
 * 会议卡录音中要显示"本次多长时间"：数据是 `MicState.elapsedSeconds`（服务自己算的），
 * 这里只负责排版 —— 不在界面里现算时长，否则界面与全屏录音页会显示两个数。
 */
internal fun elapsedClock(seconds: Double): String {
    val total = seconds.coerceAtLeast(0.0).toInt()
    val h = total / 3600
    val m = (total % 3600) / 60
    val s = total % 60
    return if (h > 0) "$h:${"%02d".format(m)}:${"%02d".format(s)}" else "${"%02d".format(m)}:${"%02d".format(s)}"
}

/**
 * 会议卡上那句状态（与按钮同一个位置）。
 *
 * 录音中**必须**带时长（用户点名要"显示本次时长"）；暂停也如实说（静音区间照录、
 * 只是内容全零 —— 见 `app/audio/recorder.py` 的暂停语义）。
 */
internal fun meetingCardLine(recording: Boolean, seconds: Double, paused: Boolean): String = when {
    recording && paused -> "录音中（已静音）· 已录 ${elapsedClock(seconds)}"
    recording -> "录音中 · 已录 ${elapsedClock(seconds)}"
    else -> "没在录音"
}
