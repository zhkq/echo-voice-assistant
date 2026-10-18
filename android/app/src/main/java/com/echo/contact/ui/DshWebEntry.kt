package com.echo.contact.ui

import com.echo.contact.data.dto.StatusResponse

/**
 * 首页那个「**经中继打开 DSH 标准版网页**」的入口（用户 2026-10-18 夜里原话：
 * *"首页新增入口：通过中继打开 DSH 标准版网页"*）。
 *
 * ## ⚠️ URL **不许写死**（这是这一条唯一真正的约束）
 *
 * 地址是**服务端算出来的**：`app/relay.py::web_entry()` =
 * `http://<后端>/v1/relay/<hostId>/` + `api/relaydsh/<只读会话>/` —— `hostId` 每次配对/重启
 * 都可能变，只读会话令牌**每次中继重连都换新**，端口还可能让位。把它写进 App 的后果是
 * "发出去的包在别人手机上打不开 / 过一会儿就 403"，所以这里**只读服务端给的字段**，
 * **不拼、不改、不加参数**（拼法以服务端契约为准）。
 *
 * ## 字段契约（2026-10-18 现行，`GET /api/status` 顶层，`app/api.py::api_status`）
 *
 * | 字段 | 含义 |
 * | --- | --- |
 * | `dshWebUrl` | **能直接在浏览器打开的完整地址**；不可用时是空串（服务端**绝不编**地址） |
 * | `dshWebReason` | 为什么现在是空的（"中继没打开" / "harness 没在运行" …）—— 人话，直接显示 |
 * | `dshWebReadOnly` | 恒为 `true`：这是一条**只读**会话，能看不能发指令/改设置 |
 *
 * 三个都对不上的情况（老服务端没有这几个键）按"字段缺失"处理：置灰 + 说清期望字段名。
 * 兼容别名 `dsh_web_url` / `relayDshUrl` / `dshWebPage`（`StatusResponse` 那边的 `alternate`）。
 *
 * ## 为什么宁可置灰也不"猜一个地址"
 *
 * 中继不可用、harness 没跑、鉴权没开 —— 这三种情况下服务器知道的地址**点了也打不开**。
 * 服务端已经把"为什么"放在 `dshWebReason` 里了，界面就照它说；自己拼一个"看起来像"的地址，
 * 用户得到的只会是浏览器里的一句 404/502，然后以为 ECHO 坏了（本仓库最忌讳这种"看着有、一用就废"）。
 *
 * ## 2026-10-18 深夜改的一处（判据没变，交出去的东西变了）
 *
 * 原来是把这串地址交给**系统浏览器**（`Intent.ACTION_VIEW`）。但中继对 `api/` 下的每条接口
 * 都要求 `Authorization` 头，而系统浏览器**不会带手机令牌** —— 点开就是一段 401 JSON。
 * 现在 `opener` 交给的是 **App 内 WebView**（`ui/DshWebScreen.kt`），由它给**同源**请求补头
 * （规则在 `data/DshWebRequestPolicy.kt`）。**地址仍然只是服务端给的那串**：
 * 不拼、不改、不追加参数 —— 这条约束一个字都没放松。
 */
internal object DshWebEntrySpec {

    /** 期望的服务端字段名（缺失时的说明里要写出来，否则谁也不知道该加哪个键）。 */
    const val EXPECTED_FIELD = "dshWebUrl"

    /** 入口的标题。 */
    const val TITLE = "DSH 标准版网页"

    /**
     * 入口下面那行说明（**经中继 + 只读 + 在 App 内**三件事必须写出来）。
     *
     * 为什么"在 App 内"也要写：用户上一次点这个入口得到的是一屏 401 JSON，
     * 文案里说清"在 App 内打开"，才不会有人再去找浏览器试一遍。
     */
    const val SUBTITLE = "经中继 · 只读，在 App 内打开"

    /** 按钮文案。 */
    const val OPEN_LABEL = "打开"

    /** 置灰时那句话：**说清"为什么点不了"+"该让服务端加什么"**，绝不写"敬请期待"。 */
    const val MISSING_NOTE = "服务端未提供中继地址（期望字段 $EXPECTED_FIELD）"
}

/**
 * 这个入口此刻**能不能点**（纯数据；`DshWebEntryTest` 逐条钉住）。
 *
 * @param url 服务端给的完整地址；空白 = 没给。
 * @param problem 非空 = 置灰的原因（人话，直接显示在界面上）。
 */
internal data class DshWebEntry(
    val url: String? = null,
    val problem: String = "",
) {
    /** 只有"有地址、而且是个真能交给浏览器的 http(s) 地址"才可点。 */
    val enabled: Boolean get() = problem.isEmpty() && !url.isNullOrBlank()
}

/**
 * 把服务端的两个字段变成入口状态（纯函数）。
 *
 * 三种情况各有一句**能照着改**的话，而且**服务端自己说的原因优先**（它知道缺的是中继还是 harness）：
 *  * 没有地址 + 服务端给了原因 → 原样带上那句原因（那是排查的入口）；
 *  * 没有地址 + 服务端也没给原因（老服务端/字段缺失）→ 说"服务端未提供中继地址"并点名期望字段；
 *  * 有值但不是 http(s) → 说清它到底是什么（截断显示），并置灰。
 */
internal fun dshWebEntry(rawUrl: String?, rawReason: String? = null): DshWebEntry {
    val url = rawUrl?.trim().orEmpty()
    if (url.isEmpty()) {
        val reason = rawReason?.trim().orEmpty()
        return DshWebEntry(
            url = null,
            problem = if (reason.isEmpty()) {
                DshWebEntrySpec.MISSING_NOTE
            } else {
                // 服务端那句话本身就是给人看的（"中继没打开（设置 → 中继 → …）"）
                "服务端未提供中继地址：$reason"
            },
        )
    }
    val lower = url.lowercase()
    if (!lower.startsWith("http://") && !lower.startsWith("https://")) {
        return DshWebEntry(
            url = null,
            problem = "服务端给的不是 http(s) 地址：${url.take(60)}（打不开，已置灰）",
        )
    }
    return DshWebEntry(url = url)
}

/** 从 `/api/status` 的回包取这个入口（**唯一**读取点，别在界面里各写一遍）。 */
internal fun dshWebEntryOf(status: StatusResponse?): DshWebEntry =
    dshWebEntry(status?.dshWebUrl, status?.dshWebReason)

/**
 * 点它：把地址交给 [opener]（**可注入**，所以 JVM 门禁用例能断言"开的就是这个 URL"）。
 *
 * 为什么不在这里直接 `startActivity` / 直接建 WebView：那是 Android 上下文的东西，
 * JVM 单测碰不到；而"到底把哪个 URL 交出去了"正是这一条最该钉住的事实
 * （写死 URL / 拼错前缀都会在这里露馅）。
 *
 * 现在真正接到 [opener] 上的是 **App 内 WebView**（`DshWebOpen.put(url)` →
 * `HomeScreen` 画 `DshWebScreen`）。opener 仍然可注入 —— 判据一个字没改。
 *
 * @return 真的交出去了才返回 true（置灰时返回 false，**不许**假装打开了）。
 */
internal fun openDshWeb(entry: DshWebEntry, opener: (String) -> Boolean): Boolean {
    val url = entry.url
    if (!entry.enabled || url.isNullOrBlank()) return false
    return opener(url)
}
