package com.echo.contact.data

/**
 * 「App 内 WebView 打开 DSH 网页」的**鉴权规则**（纯 JVM 逻辑，`DshWebRequestPolicyTest` 逐条钉住）。
 *
 * ## 为什么必须有这一层（这条需求的起因，别把它删成"顺手优化"）
 *
 * 服务端中继对 `api/` 下的每条接口（配对那几条除外）都要求 `Authorization` 头。系统浏览器
 * **不会**替我们带手机令牌 —— 所以原来那个"用系统浏览器打开"的入口，点开就是一段 401 JSON。
 * 方案是**不改服务端**、把网页放进 App 内的 WebView，由 `shouldInterceptRequest` 给请求补头。
 *
 * ## 三条边界（都是"宁可少补，不可多补"）
 *
 *  1. **只对页面同源补**：别的域名一律不补 —— 把令牌发给第三方，比看到 401 糟得多
 *     （401 只是打不开；令牌泄漏是"别人能替你调 ECHO"）。
 *  2. **令牌只放请求头**：**不许**拼进 URL —— URL 会进日志、浏览历史、`Referer`，
 *     拼进去等于把令牌抄在明片上发出去。
 *  3. 日志里只有 origin 与结论：令牌**只**以掩码形式出现（[maskToken]），
 *     连页面 URL 的 path 都不打（中继的只读会话令牌就在 path 里）。
 *
 * 这一层刻意**不碰** `android.webkit.*`：WebView 在 JVM 门禁里跑不起来，
 * 而"同源才补、别的域名不补"恰恰是最该被钉住的事实 —— 所以规则做成纯函数，
 * WebView 那边只负责"把 request.url 和令牌交进来、把结果拿去发请求"。
 */
internal object DshWebRequestPolicy {

    /** 补哪个头（名字也只在这一处写，别的地方引用它）。 */
    const val HEADER = "Authorization"

    /** Bearer 方案前缀（[bearerFor] 是唯一拼法）。 */
    private const val SCHEME_BEARER = "Bearer "

    /**
     * 从 URL 取 origin（`scheme://host[:port]`），取不出合法 http(s) origin 时返回 null。
     *
     * 归一化三件事：
     *  * scheme / host 转小写（`HTTP://Example.COM` 与 `http://example.com` 是同一个源）；
     *  * 默认端口去掉（http:80 / https:443）—— `http://a` 与 `http://a:80` 必须算同源，
     *    否则"同源"这条判据会在带不带端口上莫名其妙地失效；
     *  * 丢掉 userinfo / path / query / fragment —— 只留"源"。
     *
     * 为什么不用 `java.net.URI`：它对 WebView 实际发出来的地址（含空格、非 ASCII、
     * `data:` / `blob:` 之类）过于挑剔，解析异常会被一路吞成"不同源"或崩溃；
     * 这里只做"切出 origin"这一件事，规则简单、行为可预测。
     */
    fun originOf(url: String?): String? {
        val raw = url?.trim().orEmpty()
        if (raw.isEmpty()) return null
        val schemeEnd = raw.indexOf("://")
        if (schemeEnd <= 0) return null
        val scheme = raw.substring(0, schemeEnd).lowercase()
        if (scheme != "http" && scheme != "https") return null

        val rest = raw.substring(schemeEnd + 3)
            .substringBefore('/')
            .substringBefore('?')
            .substringBefore('#')
        if (rest.isEmpty()) return null

        // 去掉 userinfo（`user:pass@host`）—— 它不属于"源"
        val hostPort = rest.substringAfterLast('@')
        if (hostPort.isEmpty()) return null

        val host: String
        val rawPort: String?
        if (hostPort.startsWith("[")) {
            // IPv6 字面量：`[::1]:8970`
            val close = hostPort.indexOf(']')
            if (close < 0) return null
            host = hostPort.substring(0, close + 1)
            val after = hostPort.substring(close + 1)
            rawPort = if (after.startsWith(":")) after.substring(1) else null
        } else {
            val colon = hostPort.lastIndexOf(':')
            if (colon >= 0) {
                host = hostPort.substring(0, colon)
                rawPort = hostPort.substring(colon + 1)
            } else {
                host = hostPort
                rawPort = null
            }
        }
        if (host.isEmpty()) return null

        val port: Int? = if (rawPort.isNullOrEmpty()) {
            null
        } else {
            // 端口写成非数字 → 这不是一个能比的 origin，别勉强对齐
            rawPort.toIntOrNull() ?: return null
        }
        val defaultPort = if (scheme == "https") 443 else 80
        val portPart = if (port == null || port == defaultPort) "" else ":$port"
        return "$scheme://${host.lowercase()}$portPart"
    }

    /** 请求与页面是不是**同一个源**（取不出 origin 一律不算同源）。 */
    fun isSameOrigin(requestUrl: String?, pageUrl: String?): Boolean {
        val a = originOf(requestUrl) ?: return false
        val b = originOf(pageUrl) ?: return false
        return a == b
    }

    /**
     * 这次请求到底补不补令牌（纯函数，判据的核心）。
     *
     * @param requestUrl WebView 要发的那个请求的地址（`request.url.toString()`）。
     * @param pageUrl 页面本身那串（服务端给的 `dshWebUrl`）—— 同源比较的基准。
     * @param token 手机上现有的配对令牌（来源是 [PairStore]，与 `ApiClient` 同一份）。
     */
    fun decide(requestUrl: String?, pageUrl: String?, token: String?): DshWebDecision {
        val requestOrigin = originOf(requestUrl)
            ?: return DshWebDecision(
                attach = false,
                bearer = null,
                reason = DshWebAuthReason.NOT_HTTP,
                detail = "不补：这不是 http(s) 请求（没有可比的 origin）",
            )
        val pageOrigin = originOf(pageUrl)
            ?: return DshWebDecision(
                attach = false,
                bearer = null,
                reason = DshWebAuthReason.PAGE_UNKNOWN,
                detail = "不补：页面地址读不出 origin，判断不了同源",
            )
        if (requestOrigin != pageOrigin) {
            return DshWebDecision(
                attach = false,
                bearer = null,
                reason = DshWebAuthReason.CROSS_ORIGIN,
                detail = "不补：跨域请求（$requestOrigin ≠ 页面 $pageOrigin），令牌只发给同源",
            )
        }
        val tok = token?.trim().orEmpty()
        if (tok.isEmpty()) {
            return DshWebDecision(
                attach = false,
                bearer = null,
                reason = DshWebAuthReason.NO_TOKEN,
                detail = "不补：手机上还没有配对令牌（去「设置 → 连接」配对）",
            )
        }
        return DshWebDecision(
            attach = true,
            bearer = bearerFor(tok),
            reason = DshWebAuthReason.SAME_ORIGIN,
            detail = "补：同源请求（$requestOrigin），令牌只放请求头",
        )
    }

    /** 请求头的值。**唯一**拼 `Bearer` 的地方（别在别处手写，免得有人顺手拼进 URL）。 */
    fun bearerFor(token: String?): String = SCHEME_BEARER + token.orEmpty().trim()

    /**
     * 令牌的**掩码**形式（能说"有、多长"，但抄不走）。
     *
     * 为什么连前几位都不给：中继令牌不是给人看的，日志只用来回答"到底有没有令牌"。
     */
    fun maskToken(token: String?): String {
        val t = token?.trim().orEmpty()
        return if (t.isEmpty()) "（无令牌）" else "***（${t.length} 位，已隐藏）"
    }

    /**
     * 一行**可以进日志**的话：只有 origin 与结论。
     *
     * 为什么连 path 都不打：服务端给的那串地址的 path 里带着只读会话令牌，
     * 打进日志就等于把"另一种令牌"抄进了日志文件。
     */
    fun logLine(requestUrl: String?, decision: DshWebDecision): String {
        val origin = originOf(requestUrl) ?: "（非 http(s)）"
        return "DshWeb 请求 $origin -> ${decision.detail}"
    }

    /**
     * 回给 WebView 的响应头**白名单**。
     *
     * 为什么不整包透传：OkHttp 会透明解 gzip，此时原始 `Content-Encoding` / `Content-Length`
     * 与真正交给 WebView 的字节流对不上（长度不对会让页面截断或一直转圈）；
     * `Content-Type` 由 `WebResourceResponse` 单独传；`Set-Cookie` 不是这条会话的机制。
     * 只留缓存相关的几个 —— 它们说的事实不会因为解压而变。
     */
    fun responseHeaders(raw: Map<String, String>): Map<String, String> {
        val keep = setOf("cache-control", "etag", "last-modified", "expires")
        return raw.filterKeys { it.lowercase() in keep }
    }
}

/** 为什么没补令牌（日志与用例都按它分类，别用字符串比对）。 */
internal enum class DshWebAuthReason {
    /** 同源 + 有令牌 → 补。 */
    SAME_ORIGIN,

    /** 域名/端口/scheme 与页面不同 → 不补。 */
    CROSS_ORIGIN,

    /** 同源，但手机上没配对令牌 → 不补。 */
    NO_TOKEN,

    /** 不是 http(s)（`data:` / `blob:` / `about:` …）→ 不补。 */
    NOT_HTTP,

    /** 页面地址读不出 origin → 判断不了，不补。 */
    PAGE_UNKNOWN,
}

/**
 * 一次拦截的结论。
 *
 * @param attach 要不要补鉴权头。
 * @param bearer 补的时候用的头值（`Bearer <token>`）；不补时为 null。
 * @param detail 给日志看的人话（**不含令牌**）。
 */
internal data class DshWebDecision(
    val attach: Boolean,
    val bearer: String?,
    val reason: DshWebAuthReason,
    val detail: String,
)
