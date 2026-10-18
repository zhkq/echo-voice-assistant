package com.echo.contact.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「App 内 WebView 补鉴权头」的**规则**（2026-10-18 深夜，用户定下的方案 a）。
 *
 * 这一条真正的危险不是"页面打不开"，而是**令牌给错人**或者**把令牌写进 URL**：
 * 中继要求 `Authorization` 头（系统浏览器不带 → 401），所以由 App 给同源请求补；
 * 一旦补宽了（跨域也补）或者补错了地方（拼进 URL），代价是令牌泄漏，
 * 那是"别人能替你调 ECHO"，比打不开严重得多。
 *
 * 判据五组：origin 归一化、同源判定、补/不补的每一档、令牌掩码、日志不带 path/令牌。
 */
class DshWebRequestPolicyTest {

    // ---- ① origin：归一化与"什么算取不出来" -------------------------------------------

    @Test
    fun `origin 归一化 scheme 与 host 大小写 并去掉默认端口`() {
        assertEquals(
            "https://echo.example.com",
            DshWebRequestPolicy.originOf("https://echo.example.com/v1/relay/h/api/relaydsh/t/"),
        )
        assertEquals(
            "scheme 与 host 都要转小写（同一个源不能因为大小写被算成两个）",
            "http://h.example",
            DshWebRequestPolicy.originOf("HTTP://H.Example/a"),
        )
        assertEquals(
            "http:80 与不写端口必须算同一个源",
            "http://h.example",
            DshWebRequestPolicy.originOf("http://h.example:80/a"),
        )
        assertEquals(
            "https:443 同理",
            "https://h.example",
            DshWebRequestPolicy.originOf("https://h.example:443/a"),
        )
        assertEquals(
            "非默认端口要保留（那是不同的源）",
            "http://h.example:8970",
            DshWebRequestPolicy.originOf("http://h.example:8970/a?x=1#f"),
        )
        assertEquals(
            "IPv6 字面量不能把端口切错",
            "http://[::1]:8970",
            DshWebRequestPolicy.originOf("http://[::1]:8970/x"),
        )
        assertEquals(
            "userinfo 不属于源",
            "http://h.example:8080",
            DshWebRequestPolicy.originOf("http://user:pass@h.example:8080/x"),
        )
        assertEquals(
            "前后空格容忍（服务端那串偶尔带空格）",
            "http://h.example",
            DshWebRequestPolicy.originOf("  http://h.example/x  "),
        )
    }

    @Test
    fun `取不出 http-s origin 的一律返回 null（data-blob-about-畸形端口）`() {
        for (bad in listOf(
            null,
            "",
            "   ",
            "about:blank",
            "data:text/html,hello",
            "blob:https://h.example/abc",
            "javascript:void(0)",
            "h.example:8970/x",
            "http:///x",
            "http://h.example:abc/x",
        )) {
            assertNull("不该算出一个 origin：$bad", DshWebRequestPolicy.originOf(bad))
        }
    }

    @Test
    fun `同源判定 主机-端口-scheme 任一不同就不算`() {
        val page = "https://relay.example.com/v1/x/"
        assertTrue(DshWebRequestPolicy.isSameOrigin("https://relay.example.com/api/a", page))
        assertTrue(DshWebRequestPolicy.isSameOrigin("https://relay.example.com:443/api/a", page))
        assertFalse(
            "不同主机不许算同源",
            DshWebRequestPolicy.isSameOrigin("https://evil.example.com/api/a", page),
        )
        assertFalse(
            "端口不同不许算同源",
            DshWebRequestPolicy.isSameOrigin("https://relay.example.com:8443/api/a", page),
        )
        assertFalse(
            "scheme 不同不许算同源（http 的页面拿不到 https 那侧的令牌）",
            DshWebRequestPolicy.isSameOrigin("http://relay.example.com/api/a", page),
        )
        assertFalse(
            "取不出 origin 一律不算同源",
            DshWebRequestPolicy.isSameOrigin("data:text/html,x", page),
        )
    }

    // ---- ② 同源才补、别的域名不补 ------------------------------------------------------

    private val page = "https://relay.example.com/v1/relay/h-1/api/relaydsh/sess-1/"
    private val token = "tok-abcdef123456"

    @Test
    fun `同源请求 补 Authorization 且值就是 Bearer 加令牌`() {
        val d = DshWebRequestPolicy.decide("$page" + "api/status", page, token)
        assertTrue("同源必须补", d.attach)
        assertEquals(DshWebAuthReason.SAME_ORIGIN, d.reason)
        assertEquals("Bearer $token", d.bearer)
        assertTrue(
            "说明里要让人看出这是同源补的头：${d.detail}",
            d.detail.contains("同源"),
        )
    }

    @Test
    fun `不同域名 不补令牌（宁可不补 也不能把令牌发给第三方）`() {
        for (other in listOf(
            "https://evil.example.com/api/status",
            "https://cdn.example.org/app.js",
            "http://relay.example.com/api/status", // scheme 不同
            "https://relay.example.com:8443/api/status", // 端口不同
            "https://sub.relay.example.com/api/status", // 子域不是同源
        )) {
            val d = DshWebRequestPolicy.decide(other, page, token)
            assertFalse("跨域不许补：$other", d.attach)
            assertNull("跨域时连头值都不许备好：$other", d.bearer)
            assertEquals(DshWebAuthReason.CROSS_ORIGIN, d.reason)
            assertFalse(
                "说明里不许出现令牌：${d.detail}",
                d.detail.contains(token),
            )
        }
    }

    @Test
    fun `同源但没令牌 不补 并说清是没配对`() {
        for (none in listOf(null, "", "   ")) {
            val d = DshWebRequestPolicy.decide("$page" + "api/status", page, none)
            assertFalse("没令牌时不许补（补一个空的 Bearer 只会得到 401）", d.attach)
            assertNull(d.bearer)
            assertEquals(DshWebAuthReason.NO_TOKEN, d.reason)
            assertTrue("要说清下一步是去配对：${d.detail}", d.detail.contains("配对"))
        }
    }

    @Test
    fun `非 http 请求或页面地址读不出 origin 时 不补`() {
        val nonHttp = DshWebRequestPolicy.decide("data:text/html,x", page, token)
        assertFalse(nonHttp.attach)
        assertEquals(DshWebAuthReason.NOT_HTTP, nonHttp.reason)

        val noPage = DshWebRequestPolicy.decide("https://relay.example.com/a", "", token)
        assertFalse("页面地址都没有，判断不了同源，就不补", noPage.attach)
        assertEquals(DshWebAuthReason.PAGE_UNKNOWN, noPage.reason)
    }

    // ---- ③ 令牌只放请求头：不进 URL、不进日志 -------------------------------------------

    @Test
    fun `决策里的说明与头值之外 任何字段都不许夹带令牌`() {
        val d = DshWebRequestPolicy.decide("$page" + "api/status", page, token)
        assertTrue("头值就该是 Bearer + 令牌", d.bearer == "Bearer $token")
        // detail 是给日志看的人话 —— 里面不许出现令牌
        assertFalse("日志/说明里出现令牌等于把它抄出去：${d.detail}", d.detail.contains(token))
        // 令牌只在头值里；它也没有被拼进任何 URL（decide 不产出 URL，这里钉住"说明里没有"）
        assertTrue(d.detail.contains("请求头"))
    }

    @Test
    fun `掩码只说有没有 多长 抄不走`() {
        val masked = DshWebRequestPolicy.maskToken(token)
        assertFalse("掩码里不许出现令牌本身：$masked", masked.contains(token))
        assertFalse("连前缀都不给（令牌不是给人念的）", masked.contains(token.take(6)))
        assertTrue(
            "要看得出长度（排查时能分辨'没令牌'与'令牌不像'）：$masked",
            masked.contains(token.length.toString()),
        )
        assertEquals("（无令牌）", DshWebRequestPolicy.maskToken(null))
        assertEquals("（无令牌）", DshWebRequestPolicy.maskToken("  "))
    }

    @Test
    fun `日志行只带 origin 不带 path 也不带令牌`() {
        val requestUrl = "$page" + "api/status?x=1"
        val d = DshWebRequestPolicy.decide(requestUrl, page, token)
        val line = DshWebRequestPolicy.logLine(requestUrl, d)
        assertTrue("要能看出是哪个源：$line", line.contains("https://relay.example.com"))
        assertFalse("path 里可能有只读会话令牌，不许进日志：$line", line.contains("sess-1"))
        assertFalse("令牌绝对不许进日志：$line", line.contains(token))
        assertFalse("连 api 路径都不必打：$line", line.contains("/api/"))
    }

    // ---- ④ 回给 WebView 的响应头：只留不会因解压而说谎的那几个 ------------------------

    @Test
    fun `响应头白名单 丢掉长度与编码 保留缓存相关`() {
        val raw = mapOf(
            "Content-Type" to "text/html; charset=utf-8",
            "Content-Length" to "12345",
            "Content-Encoding" to "gzip",
            "Transfer-Encoding" to "chunked",
            "Set-Cookie" to "a=b",
            "Cache-Control" to "no-cache",
            "ETag" to "\"abc\"",
            "Last-Modified" to "Wed, 21 Oct 2015 07:28:00 GMT",
        )
        val kept = DshWebRequestPolicy.responseHeaders(raw)
        assertEquals(
            "只该留下缓存相关的三项（大小写不敏感）",
            setOf("Cache-Control", "ETag", "Last-Modified"),
            kept.keys,
        )
        assertFalse("长度/编码在透明解压后会说谎，不能透传", kept.containsKey("Content-Length"))
        assertEquals("no-cache", kept["Cache-Control"])
    }

    @Test
    fun `bearerFor 是唯一拼法 空白令牌得到空 Bearer 而不是崩`() {
        assertEquals("Bearer abc", DshWebRequestPolicy.bearerFor(" abc "))
        assertEquals("Bearer ", DshWebRequestPolicy.bearerFor(null))
    }
}
