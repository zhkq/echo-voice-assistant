package com.echo.contact.ui

import com.echo.contact.data.DshWebRequestPolicy

/**
 * WebView 打开失败时**给人看的那两句话**（纯函数，`DshWebScreenTest` 钉住）。
 *
 * 为什么要把文案抽出来：用户 2026-10-18 的规矩是"**加载失败如实提示原因，不许白屏没话说**"。
 * 而 WebView 的失败回调在 JVM 门禁里跑不起来 —— 文案是这一条唯一能被钉住的部分，
 * 所以它不许藏在 `WebViewClient` 里（那样就只剩"上真机才知道说了什么"）。
 *
 * ## 两条铁律
 *
 *  * **只带 origin，不带整条 URL**：服务端给的那串地址的 path 里就有只读会话令牌，
 *    把 URL 原样贴到界面上（以及以后有人顺手复制进日志）都会把它泄漏出去。
 *  * **说清"下一步做什么"**：只说"加载失败"等于没说 —— 每一档都给一个能照着做的动作。
 */
internal data class DshWebFailure(
    /** 一句话定性（例如"连不上中继"）。 */
    val title: String,
    /** 具体原因 + 下一步（显示在按钮上方那几行）。 */
    val detail: String,
)

/**
 * `WebViewClient.ERROR_*` 的取值。
 *
 * 为什么自己写一份而不引用 `android.webkit.WebViewClient.ERROR_*`：那些常量来自 android.jar，
 * 在 JVM 单测里不可靠（mock 过的 jar 只保证方法能编过）；文案要能被门禁钉住，
 * 判据就不能依赖"测试时那个常量到底是多少"。
 * 取值与官方文档一致（`ERROR_UNKNOWN=-1` … `ERROR_UNSAFE_RESOURCE=-16`）。
 */
internal object DshWebNet {
    const val ERROR_UNKNOWN = -1
    const val ERROR_HOST_LOOKUP = -2
    const val ERROR_CONNECT = -6
    const val ERROR_IO = -7
    const val ERROR_TIMEOUT = -8
    const val ERROR_REDIRECT_LOOP = -9
    const val ERROR_UNSUPPORTED_SCHEME = -10
    const val ERROR_BAD_URL = -11
    const val ERROR_FILE = -13
    const val ERROR_FILE_NOT_FOUND = -14
    const val ERROR_TOO_MANY_REQUESTS = -15
    const val ERROR_UNSAFE_RESOURCE = -16
}

/** 页面顶部那行小字：只显示 origin（见文件头"不带整条 URL"）。 */
internal fun dshWebOriginLabel(url: String?): String {
    val origin = DshWebRequestPolicy.originOf(url)
    return if (origin == null) "地址异常（读不出 origin）" else origin
}

/**
 * 网络层失败（`onReceivedError`）→ 人能照着做的一句话。
 *
 * @param errorCode `WebResourceError.errorCode`（取值见 [DshWebNet]）。
 * @param description WebView 给的原话（可能为空；原样附在 detail 里，方便对照）。
 * @param failingUrl 失败的地址 —— **只取 origin**。
 */
internal fun dshWebLoadFailure(
    errorCode: Int,
    description: String?,
    failingUrl: String?,
): DshWebFailure {
    val origin = dshWebOriginLabel(failingUrl)
    val raw = description?.trim().orEmpty()
    val suffix = if (raw.isEmpty()) "" else "（WebView：$raw）"
    return when (errorCode) {
        DshWebNet.ERROR_HOST_LOOKUP -> DshWebFailure(
            title = "连不上：域名解析失败",
            detail = "手机解析不了 $origin $suffix\n" +
                "先确认手机能上网；中继地址是电脑给的，如果电脑换了网络，回首页刷新状态再点一次。",
        )
        DshWebNet.ERROR_CONNECT -> DshWebFailure(
            title = "连不上中继",
            detail = "$origin 拒绝连接 $suffix\n" +
                "中继可能已经断了（电脑上的「允许手机经后端中转」是不是关了？），回首页刷新后重试。",
        )
        DshWebNet.ERROR_TIMEOUT -> DshWebFailure(
            title = "连接超时",
            detail = "$origin 在限定时间内没有响应 $suffix\n" +
                "换到和电脑同一个 Wi-Fi 再试；还不行就看电脑上的中继是否还在跑。",
        )
        DshWebNet.ERROR_IO -> DshWebFailure(
            title = "连接中断",
            detail = "和 $origin 的连接中途断了 $suffix\n网络切换（Wi-Fi ↔ 流量）时常见，重试即可。",
        )
        DshWebNet.ERROR_REDIRECT_LOOP -> DshWebFailure(
            title = "重定向打转",
            detail = "$origin 反复要求跳转 $suffix\n" +
                "多半是中继的会话已经失效，回首页刷新状态、让它重新发一个地址。",
        )
        DshWebNet.ERROR_UNSUPPORTED_SCHEME -> DshWebFailure(
            title = "地址的协议不支持",
            detail = "服务端给的地址不是 http(s)（$origin） $suffix\n" +
                "回首页刷新状态；若仍这样，服务端那条中继配置有问题。",
        )
        DshWebNet.ERROR_BAD_URL -> DshWebFailure(
            title = "地址不合法",
            detail = "服务端给过来的地址读不懂（$origin） $suffix\n" +
                "回首页刷新状态再试；还不行就是服务端给错了地址。",
        )
        DshWebNet.ERROR_FILE, DshWebNet.ERROR_FILE_NOT_FOUND -> DshWebFailure(
            title = "打不开（本地文件）",
            detail = "这条地址指向了本机文件而不是网页 $suffix\nApp 只允许打开服务端给的网页地址。",
        )
        DshWebNet.ERROR_TOO_MANY_REQUESTS -> DshWebFailure(
            title = "请求太频繁",
            detail = "$origin 让先缓一缓 $suffix\n等十几秒再点重试。",
        )
        DshWebNet.ERROR_UNSAFE_RESOURCE -> DshWebFailure(
            title = "被安全策略拦下",
            detail = "系统认为 $origin 不安全（http 与 https 混用） $suffix\n" +
                "让服务端用 https，或确认中继地址是不是拼错了。",
        )
        DshWebNet.ERROR_UNKNOWN -> DshWebFailure(
            title = "打不开这个网页",
            detail = "$origin 报了个没分类的错误 $suffix\n先点重试；一直这样就回首页刷新状态再点一次。",
        )
        else -> DshWebFailure(
            title = "打不开这个网页（错误 $errorCode）",
            detail = "$origin $suffix\n先点重试；一直这样就回首页刷新状态再点一次。",
        )
    }
}

/**
 * HTTP 层失败（`onReceivedHttpError`，或拦截器自己拿到的错误状态码）→ 一句话；`< 400` 返回 null。
 *
 * 401/403 是这条需求**最该说清**的两档：401 = 我们补的令牌没被认（或压根没补），
 * 403 = 会话过期（中继每次重连都会换一个新的只读会话）。混成一句"打开失败"会把
 * 用户引去查网络，而真正该做的是刷新/重新配对。
 */
internal fun dshWebHttpFailure(statusCode: Int, url: String?): DshWebFailure? {
    if (statusCode < 400) return null
    val origin = dshWebOriginLabel(url)
    return when (statusCode) {
        401 -> DshWebFailure(
            title = "中继说没授权（401）",
            detail = "$origin 拒绝了这次请求：手机上这条令牌没被认。\n" +
                "去「设置 → 连接」重新配对，再回首页刷新状态后重试。",
        )
        403 -> DshWebFailure(
            title = "这条只读会话已失效（403）",
            detail = "$origin 不接受这条会话了。\n" +
                "它每次中继重连都会换新，回首页刷新状态（等地址更新）再点这个入口。",
        )
        404 -> DshWebFailure(
            title = "地址不存在（404）",
            detail = "$origin 上没有这个页面。\n" +
                "服务端换了地址（端点/会话变了），回首页刷新状态再点一次。",
        )
        429 -> DshWebFailure(
            title = "请求太频繁（429）",
            detail = "$origin 让先缓一缓。等十几秒再点重试。",
        )
        502, 503, 504 -> DshWebFailure(
            title = "电脑那边暂时不可用（HTTP $statusCode）",
            detail = "$origin 回的是 $statusCode。\n" +
                "通常是电脑上的 DSH 标准版没在运行，或中继暂时连不上它；去电脑上看一眼再重试。",
        )
        else -> DshWebFailure(
            title = "服务端回了个错误（HTTP $statusCode）",
            detail = "$origin 回的是 HTTP $statusCode。先点重试；一直这样就回电脑上看一眼。",
        )
    }
}

/**
 * 从 `Content-Type` 里取字符集（给 `WebResourceResponse` 用）。
 *
 * 不传就会让 WebView 自己猜：`text/html` 猜错一次，中文页面就是一屏乱码 ——
 * 而"看着像服务坏了"的乱码最难排查。所以：显式 charset 优先，文本类默认 utf-8，
 * 二进制（图片/字体/wasm）返回 null 交给 WebView 按字节处理。
 */
internal fun dshWebCharsetOf(contentType: String?): String? {
    val raw = contentType?.trim().orEmpty()
    if (raw.isEmpty()) return "utf-8"
    val marker = raw.lowercase().indexOf("charset=")
    if (marker >= 0) {
        val value = raw.substring(marker + "charset=".length)
            .substringBefore(';')
            .trim()
            .trim('"')
            .trim('\'')
        if (value.isNotEmpty()) return value
    }
    val mime = raw.substringBefore(';').trim().lowercase()
    val textual = mime.startsWith("text/") ||
        mime.contains("javascript") ||
        mime.contains("json") ||
        mime.contains("xml") ||
        mime.contains("svg")
    return if (textual) "utf-8" else null
}
