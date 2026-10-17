package com.echo.contact.ui

/**
 * 底部导航的 **5 页**（用户 2026-10-18 点名："可以考虑 echo 超级助理在中间，左右各两个页签的样式"）。
 *
 * ## 顺序：**超级助理在正中间**（第 3 个），左右各两项
 *
 * ```
 * [ 指令 ]  [ 会议 ]      ( 超级助理 )      [ 回顾 ]  [ 设置 ]
 *                        ↑ 凸起的主入口
 * ```
 *
 * **为什么顺序只写在枚举里（这一处决定一切）**：界面按 [ECHO_TABS] 顺序渲染，底部导航把
 * **正中间那一格**（`EchoNavBar.echoCenterIndex`）做成凸起的造型，`MainViewModel` 的
 * `selectTab(ordinal)`、通知跳会议（`EchoTab.MEETINGS.ordinal`）也都只看 ordinal。
 * 于是"中间是谁"不是写死成 ASSISTANT，而是**由这里的顺序自然得出** —— 想换顺序只改这一处，
 * 造型会跟着换到新的中间那颗；反过来，**别在别处再写死一次页签序号**（那就会分叉）。
 *
 * 主入口为什么放中间、做凸起：**一眼能看到**。原来五颗等大、超级助理在最左边，
 * 它和"指令/会议/回顾"长得一模一样，用户第一眼分不出哪颗是主入口。
 *
 * 为什么把页签抽成**纯数据**：这一轮里最容易改坏的就是"页签数量 / 顺序 / 每个入口指向哪"。
 * 判据（恰好 5 个、名称与顺序、中间那颗是超级助理、三个主入口能切到对应页）
 * 全在 `EchoTabsTest` / `EchoNavBarTest` 里 —— 以后加页/改序会先红，而不是等到真机上点不到。
 *
 * 为什么设置也做成页签：用户要 5 页。右上角那个齿轮因此撤掉了 —— 同一个目的地留两个入口，
 * 迟早出现"从齿轮进、从页签进"看到两套状态（本仓库在"同一个键只能一处能改"上栽过）。
 */
internal enum class EchoTab(val title: String) {
    /** 第 1 页（最左）：指令（说一句 / 打字 + 这一轮结果 + 历史）。 */
    COMMANDS("指令"),

    /** 第 2 页：会议（手机本机录音 + 列表）。 */
    MEETINGS("会议"),

    /**
     * 第 3 页：**正中间**的仪表盘风格超级助理（状态卡 + 三个主入口）。
     *
     * 它在枚举里排第 3 不是"顺手"，而是**用户点名的位置**：底部导航中间那颗要凸起，
     * 而那颗就是它。`EchoNavBarTest` 用 `echoCenterTab(ECHO_TABS) == ASSISTANT` 钉住这件事。
     */
    ASSISTANT("超级助理"),

    /** 第 4 页：回顾（顶部功能区 + 历史列表）。 */
    REVIEW("回顾"),

    /** 第 5 页（最右）：设置（手机这一侧的选项与权限）。 */
    SETTINGS("设置"),
}

/**
 * 页签顺序（底部导航与 `when` 路由都照它；顺序变了用户会找不到）。
 *
 * ⚠️ **改这一行 = 改整条导航的顺序**（含中间那颗是谁）：别在界面里再排一次序。
 */
internal val ECHO_TABS: List<EchoTab> = EchoTab.values().toList()

/**
 * **起始页**（也就是"回首页"落点的那一页）= 超级助理。
 *
 * 为什么要有这个东西：`MainViewModel` 的页签值是 `EchoTab.ordinal`，而这一轮顺序改完之后
 * **`0` 不再是超级助理而是「指令」**。起始页要是还写死 `0`，App 一启动就停在指令页 ——
 * 首页（仪表盘）永远没人看见，而且"从别处回首页"也会落到错的那一页。
 * 所以起始页的语义写成"首页"（[EchoTab.ASSISTANT]），不写"第 0 项"；
 * `EchoTabsTest` 另外钉住"起始页正好是底部导航正中间那一格"。
 */
internal val START_TAB: EchoTab = EchoTab.ASSISTANT

/**
 * 超级助理页上的**三个主入口**（点了切到对应页签）。
 *
 * `tab` 必须是 [ECHO_TABS] 里真实存在的页签 —— 用例会检查这一点，
 * 免得某一个入口指到一个已经不存在的页（点了什么都不发生）。
 */
internal data class AssistantEntry(
    val tab: EchoTab,
    val title: String,
    val subtitle: String,
)

internal val ASSISTANT_ENTRIES: List<AssistantEntry> = listOf(
    AssistantEntry(
        tab = EchoTab.COMMANDS,
        title = "指令",
        subtitle = "说一句或打字，看这一轮的结果",
    ),
    AssistantEntry(
        tab = EchoTab.MEETINGS,
        title = "会议",
        subtitle = "用这台手机录音，结束后转写",
    ),
    AssistantEntry(
        tab = EchoTab.REVIEW,
        title = "回顾",
        subtitle = "口述今天，念当天的播报",
    ),
)
