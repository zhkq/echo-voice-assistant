package com.echo.contact.ui

/**
 * 底部导航的 **5 页**（用户 2026-10-18 点名：第一页 ECHO 超级助理，后面跟指令、会议、回顾，
 * 再加设置）。
 *
 * 为什么把页签抽成**纯数据**：这一轮里最容易改坏的就是"页签数量 / 顺序 / 每个入口指向哪"。
 * 界面只负责照 [ECHO_TABS] 渲染，判据（恰好 5 个、名称与顺序、三个主入口能切到对应页）
 * 全在 `EchoTabsTest` 里 —— 以后加页/改序会先红，而不是等到真机上点不到。
 *
 * 为什么设置也做成页签：用户要 5 页。右上角那个齿轮因此撤掉了 —— 同一个目的地留两个入口，
 * 迟早出现"从齿轮进、从页签进"看到两套状态（本仓库在"同一个键只能一处能改"上栽过）。
 */
internal enum class EchoTab(val title: String) {
    /** 第 1 页：仪表盘风格的超级助理（状态卡 + 三个主入口）。 */
    ASSISTANT("超级助理"),

    /** 第 2 页：指令（说一句 / 打字 + 这一轮结果 + 历史）。 */
    COMMANDS("指令"),

    /** 第 3 页：会议（手机本机录音 + 列表）。 */
    MEETINGS("会议"),

    /** 第 4 页：回顾（顶部功能区 + 历史列表）。 */
    REVIEW("回顾"),

    /** 第 5 页：设置（手机这一侧的选项与权限）。 */
    SETTINGS("设置"),
}

/** 页签顺序（底部导航与 `when` 路由都照它；顺序变了用户会找不到）。 */
internal val ECHO_TABS: List<EchoTab> = EchoTab.values().toList()

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
