package com.echo.contact.ui

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * 「首页入口 → App 内嵌页」的那一次交接（进程级，故意是 object）。
 *
 * ## 为什么要有它（而不是把 URL 一路当参数传下去）
 *
 * 入口那一行在 `StatusCard` 的展开区里、嵌在 `HomeTopCard` → `SuperAssistantTab` 的 Column 里；
 * 而网页要**盖住整个外壳**（含底部导航，与全屏录音界面同一个道理）。把回调一层层传上去要改
 * 4 个文件的签名，每加一层就多一次"是谁忘了往下传"的机会。
 *
 * `MeetingOpenStore` 已经用过同一套手法（进程级交接 + 界面只有一条消费路径），这里照它写：
 *  * 入口行 `put(url)`（只放服务端给的那串，**不拼不改**）；
 *  * `HomeScreen` 组合时读一次，非空就画 [DshWebScreen]，关闭时 `clear()`。
 *
 * ## 为什么是 URL 而不是"布尔开关"
 *
 * 布尔开关会让"打开的到底是哪个地址"变成界面里的隐含状态（刷新后地址变了就没人知道）。
 * 把**那串地址本身**放在这里，"打开的是服务端给的那串"就成了可断言的事实
 * （判据见 `DshWebScreenTest`）。
 */
internal object DshWebOpen {

    private val _url = MutableStateFlow<String?>(null)

    /** 当前要在内嵌页里打开的地址；null = 没开着。 */
    val url: StateFlow<String?> = _url.asStateFlow()

    /** 收到一次"打开这个地址"（空白串当没收到 —— 那等于白屏）。 */
    fun put(url: String) {
        val trimmed = url.trim()
        if (trimmed.isEmpty()) return
        _url.value = trimmed
    }

    /** 关掉（界面返回/关闭按钮）。 */
    fun clear() {
        _url.value = null
    }
}
