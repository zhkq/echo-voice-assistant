package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「首页入口 → **App 内 WebView** 打开 DSH 网页」这一条的门禁（2026-10-18 深夜，方案 a）。
 *
 * ## 为什么必须内嵌（这一条需求存在的全部理由）
 *
 * 中继对 `api/` 下的每条接口（配对那几条除外）都要 `Authorization` 头；**系统浏览器不会带**
 * 手机令牌 —— 原来那个 `Intent.ACTION_VIEW` 入口点开就是一段 401 JSON。服务端不改，
 * 所以由 App 在 `shouldInterceptRequest` 里给**同源**请求补头。
 *
 * ## JVM 门禁能钉住什么（以及为什么这样就够）
 *
 * WebView 本身在 JVM 里跑不起来，所以这一层把"最该钉住的事实"拆成两类：
 *  ① **纯函数**：补不补头（`DshWebRequestPolicy`，另一份用例逐档钉住）、失败文案
 *     （本文件钉住"每一档都有原因 + 下一步"且不泄漏 path）；
 *  ② **源码事实**：JS/DOM storage 开了、拦截器真的走策略、令牌只进请求头、
 *     入口不再用系统浏览器、服务端那串 URL 原样进 store —— 这些都能读源码断言。
 *
 * 真机上的渲染/网络行为**没有**被这组用例覆盖（见交付报告的"没做到/存疑"）。
 */
class DshWebScreenTest {

    /** 形状照现行服务端契约（path 里那一段就是中继的只读会话令牌）。 */
    private val page = "https://relay.example.com/v1/relay/h-1/api/relaydsh/sess-9/"

    // ---- ① 失败路径：一定有话说，而且只带 origin --------------------------------------

    @Test
    fun `每一档网络失败都有标题 原因 下一步`() {
        val codes = mapOf(
            DshWebNet.ERROR_HOST_LOOKUP to "域名",
            DshWebNet.ERROR_CONNECT to "连不上",
            DshWebNet.ERROR_TIMEOUT to "超时",
            DshWebNet.ERROR_IO to "中断",
            DshWebNet.ERROR_REDIRECT_LOOP to "重定向",
            DshWebNet.ERROR_UNSUPPORTED_SCHEME to "协议",
            DshWebNet.ERROR_BAD_URL to "地址不合法",
            DshWebNet.ERROR_TOO_MANY_REQUESTS to "频繁",
            DshWebNet.ERROR_UNSAFE_RESOURCE to "安全",
            DshWebNet.ERROR_UNKNOWN to "打不开",
            -999 to "999",
        )
        for ((code, keyword) in codes) {
            val f = dshWebLoadFailure(code, "some description", page + "x")
            assertTrue("错误 $code 的标题里要有关键信息：${f.title}", f.title.contains(keyword))
            assertTrue("错误 $code 没有具体原因：${f.detail}", f.detail.contains("relay.example.com"))
            assertTrue(
                "错误 $code 只说了'失败'，没有下一步该做什么：${f.detail}",
                f.detail.length > 20,
            )
        }
    }

    @Test
    fun `WebView 的原话会附在 detail 里（排查要看原话）`() {
        val f = dshWebLoadFailure(DshWebNet.ERROR_CONNECT, "net::ERR_CONNECTION_REFUSED", page)
        assertTrue("WebView 那句原话要带上：${f.detail}", f.detail.contains("ERR_CONNECTION_REFUSED"))
        val noDesc = dshWebLoadFailure(DshWebNet.ERROR_CONNECT, null, page)
        assertFalse("没有原话时不许留个空括号：${noDesc.detail}", noDesc.detail.contains("WebView：）"))
    }

    @Test
    fun `HTTP 层 401 与 403 分开说 因为下一步完全不同`() {
        val unauthorized = dshWebHttpFailure(401, page)
        assertNotNull("401 必须有一句话", unauthorized)
        assertTrue(unauthorized!!.title.contains("401"))
        assertTrue(
            "401 = 我们补的令牌没被认 → 下一步是**重新配对**：${unauthorized.detail}",
            unauthorized.detail.contains("重新配对"),
        )

        val expired = dshWebHttpFailure(403, page)
        assertNotNull("403 必须有一句话", expired)
        assertTrue(expired!!.title.contains("403"))
        assertTrue(
            "403 = 只读会话过期 → 下一步是**回首页刷新**（重新配对解决不了它）：${expired.detail}",
            expired.detail.contains("刷新"),
        )
    }

    @Test
    fun `其余 HTTP 档都有话说 且 400 以下不算失败`() {
        for (code in listOf(404, 429, 500, 502, 503, 504, 418)) {
            val f = dshWebHttpFailure(code, page)
            assertNotNull("HTTP $code 也得有话", f)
            assertTrue("HTTP $code 的标题里要带码：${f!!.title}", f.title.contains(code.toString()))
            assertTrue("HTTP $code 没有下一步：${f.detail}", f.detail.length > 15)
        }
        for (code in listOf(200, 204, 301, 399)) {
            assertNull("HTTP $code 不是失败（重定向由 OkHttp 自己跟完）", dshWebHttpFailure(code, page))
        }
    }

    @Test
    fun `失败文案里不出现地址的 path（只读会话令牌就在那里）`() {
        val load = dshWebLoadFailure(DshWebNet.ERROR_TIMEOUT, null, page + "deep/path")
        val http = dshWebHttpFailure(401, page)!!
        for (text in listOf(load.title, load.detail, http.title, http.detail)) {
            assertFalse("失败文案泄漏了 path（中继会话令牌在里面）：$text", text.contains("sess-9"))
            assertFalse("失败文案里不该出现路径片段：$text", text.contains("/api/"))
        }
    }

    @Test
    fun `charset 解析 显式优先 文本类默认 utf-8 二进制给 null`() {
        assertEquals("GBK", dshWebCharsetOf("text/html; charset=GBK"))
        assertEquals("utf-8", dshWebCharsetOf("application/json; charset=utf-8"))
        assertEquals(
            "没写 charset 的文本按 utf-8（猜错就是整屏乱码，而乱码最像'服务坏了'）",
            "utf-8",
            dshWebCharsetOf("text/html"),
        )
        assertEquals("utf-8", dshWebCharsetOf("application/javascript"))
        assertEquals("utf-8", dshWebCharsetOf("image/svg+xml"))
        assertNull("二进制别硬塞编码", dshWebCharsetOf("image/png"))
        assertNull(dshWebCharsetOf("application/wasm"))
        assertEquals("utf-8", dshWebCharsetOf(null))
    }

    @Test
    fun `顶部那行只显示 origin 地址异常时也有话说`() {
        assertEquals("https://relay.example.com", dshWebOriginLabel(page))
        assertTrue(dshWebOriginLabel(null).contains("地址异常"))
        assertTrue(dshWebOriginLabel("data:text/html,x").contains("地址异常"))
    }

    // ---- ② 入口：从系统浏览器改成 App 内嵌页 ------------------------------------------

    @Test
    fun `入口不再用系统浏览器 而是交给 App 内嵌页`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt")
        val body = functionBody(src, "private fun DshWebEntryRow(", "/** 一格：状态灯")
        assertFalse(
            "入口还在用 Intent.ACTION_VIEW —— 系统浏览器不带鉴权头，点开就是 401",
            body.contains("ACTION_VIEW"),
        )
        assertFalse("入口不许自己发 Intent（那正是被换掉的那条路）", body.contains("startActivity"))
        assertTrue(
            "入口必须把服务端那串交给 App 内嵌页（DshWebOpen.put）",
            body.contains("DshWebOpen.put("),
        )
    }

    @Test
    fun `打开 WebView 的就是服务端给的那串 原样进 store 再原样 loadUrl`() {
        DshWebOpen.clear()
        // openDshWeb 的可注入 opener 是唯一入口（判据在 DshWebEntryTest 里钉过"是原样那串"）
        val entry = dshWebEntry(page)
        var opened: String? = null
        assertTrue(openDshWeb(entry) { opened = it; DshWebOpen.put(it); true })
        assertEquals("交给内嵌页的必须是服务端那串（不拼、不改、不加参数）", page, opened)
        assertEquals("store 里也必须是原样那串", page, DshWebOpen.url.value)

        // WebView 那侧只有一句 loadUrl(url)，而且用的是传进来的参数（不是自己拼的常量）
        val screen = moduleText("src/main/java/com/echo/contact/ui/DshWebScreen.kt")
        assertTrue("WebView 必须加载传进来的那串：loadUrl(url)", screen.contains("loadUrl(url)"))
        assertEquals(
            "地址不许在 WebView 里被拼过（只该原样 loadUrl 一次）",
            1,
            occurrencesOf(screen, "loadUrl("),
        )

        DshWebOpen.clear()
        assertNull("关闭后不该还留着地址（下次打开会重放旧地址）", DshWebOpen.url.value)
    }

    @Test
    fun `store 不接收空白地址（那等于打开一个白屏）`() {
        DshWebOpen.clear()
        DshWebOpen.put("   ")
        assertNull(DshWebOpen.url.value)
        DshWebOpen.put("  " + page + "  ")
        assertEquals("前后空格去掉，path 一个字不改", page, DshWebOpen.url.value)
        DshWebOpen.clear()
    }

    @Test
    fun `首页外壳负责整屏画内嵌页 并且关掉时清空`() {
        val home = moduleText("src/main/java/com/echo/contact/ui/HomeScreen.kt")
        assertTrue("首页外壳要读 store", home.contains("DshWebOpen.url"))
        assertTrue("首页外壳要画内嵌页", home.contains("DshWebScreen("))
        assertTrue("关闭要清空 store（否则返回后又被弹出来）", home.contains("DshWebOpen.clear()"))
        // 盖住整个外壳（含底部导航）：必须在 Scaffold 之前 return（与全屏录音同一个做法）
        val overlayAt = home.indexOf("DshWebScreen(")
        val scaffoldAt = home.indexOf("Scaffold(")
        assertTrue("内嵌页与 Scaffold 的位置读不出来", overlayAt > 0 && scaffoldAt > 0)
        assertTrue("内嵌页必须盖住外壳（要画在 Scaffold 之前）", overlayAt < scaffoldAt)
    }

    // ---- ③ WebView 配置与"令牌只放请求头" ---------------------------------------------

    @Test
    fun `WebView 开了 JS 与 DOM storage（DSH 前端需要）`() {
        val screen = moduleText("src/main/java/com/echo/contact/ui/DshWebScreen.kt")
        assertTrue("没开 JS，DSH 前端就是一张空白", screen.contains("javaScriptEnabled = true"))
        assertTrue("没开 DOM storage，前端状态存不下来", screen.contains("domStorageEnabled = true"))
        assertTrue("拦截器入口不见了", screen.contains("override fun shouldInterceptRequest("))
        assertTrue("拦截器必须走策略（不许在界面层另写一套同源判断）", screen.contains("DshWebRequestPolicy.decide("))
        assertTrue("必须真的把令牌放进请求头", screen.contains("DshWebRequestPolicy.HEADER"))
    }

    @Test
    fun `令牌不进 URL 不进日志 也不注入页面 JS`() {
        val screen = moduleText("src/main/java/com/echo/contact/ui/DshWebScreen.kt")
        for (banned in listOf("?token=", "&token=", "token=", "evaluateJavascript", "loadData")) {
            assertFalse(
                "令牌/页面内容不许这样带进去（$banned）——只许放请求头",
                codeOnly(screen).contains(banned),
            )
        }
        // 任何一行日志都不许出现令牌或 Bearer
        screen.lineSequence().filter { it.contains("Log.") }.forEach { line ->
            val lower = line.lowercase()
            assertFalse("日志里出现了 token：$line", lower.contains("token"))
            assertFalse("日志里出现了 bearer（头值就是令牌）：$line", lower.contains("bearer"))
        }
        assertTrue(
            "日志走的是策略给的'只有 origin + 结论'那行",
            screen.contains("DshWebRequestPolicy.logLine("),
        )
    }

    @Test
    fun `令牌来源是现有的配对存储 不新造一套`() {
        val screen = moduleText("src/main/java/com/echo/contact/ui/DshWebScreen.kt")
        assertTrue("要从现有 PairStore 读令牌", screen.contains("PairStore.token("))
        assertFalse(
            "不许在这条路上另造一个凭据存储",
            screen.contains("EncryptedSharedPreferences"),
        )
        // 读加密 prefs 不能在主线程（首帧卡顿就是它）
        assertTrue("PairStore 的读取要放到 IO 线程", screen.contains("withContext(Dispatchers.IO)"))
    }

    @Test
    fun `加载失败有可见原因（失败卡真的把 detail 画出来）`() {
        val screen = moduleText("src/main/java/com/echo/contact/ui/DshWebScreen.kt")
        assertTrue("失败状态没有画出来", screen.contains("failure?.let"))
        assertTrue("失败卡要显示具体原因", screen.contains("failure.detail"))
        assertTrue("主框架网络失败要翻译成人话", screen.contains("dshWebLoadFailure("))
        assertTrue("主框架 HTTP 失败也要翻译", screen.contains("dshWebHttpFailure("))
        assertTrue(
            "从拦截器返回的响应不一定触发 onReceivedHttpError —— 主框架 4xx 要自己判一次",
            screen.contains("mainFrame && response.code >= 400"),
        )
        assertTrue(
            "子资源被 401/403 拒了也要如实说一句（否则看着像'页面开了、功能全空'）",
            screen.contains("response.code == 401"),
        )
        assertTrue("至少要给'重试'与'关闭'两个出口", screen.contains("onRetry") && screen.contains("onClose"))
    }

    @Test
    fun `策略层是纯函数（不许碰 android 与日志）`() {
        val policy = moduleText("src/main/java/com/echo/contact/data/DshWebRequestPolicy.kt")
        assertFalse("策略层不许 import android（那样 JVM 门禁就测不了它）", policy.contains("import android"))
        assertFalse("策略层不许打日志（令牌就漏在那条路上）", policy.contains("Log."))
        assertEquals(
            "Bearer 只在 bearerFor 里拼一次",
            1,
            occurrencesOf(codeOnly(policy), "Bearer "),
        )
        assertTrue("补头逻辑在策略里", policy.contains("fun decide("))
        assertTrue("同源判定在策略里", policy.contains("fun isSameOrigin("))
    }

    // ---- ④ 源码里不许出现写死的中继地址 / 主机名 ---------------------------------------

    /** 只留**代码行**（注释里引用契约形状是允许的 —— 那是在说明，不是在写死）。 */
    private fun codeOnly(src: String): String = src.lineSequence()
        .filterNot { line ->
            val t = line.trim()
            t.startsWith("//") || t.startsWith("*") || t.startsWith("/*") || t.startsWith("*/")
        }
        .joinToString("\n")

    @Test
    fun `新文件里不出现中继路径与主机名 也不出现写死的令牌`() {
        for (name in listOf(
            "ui/DshWebScreen.kt",
            "ui/DshWebFailure.kt",
            "ui/DshWebOpen.kt",
            "ui/DshWebEntry.kt",
            "ui/StatusCard.kt",
            "ui/HomeScreen.kt",
            "data/DshWebRequestPolicy.kt",
        )) {
            val code = codeOnly(moduleText("src/main/java/com/echo/contact/$name"))
            for (banned in listOf(
                "/v1/relay",
                "relaydsh",
                "127.0.0.1",
                "192.168.",
                "localhost",
                "example.com",
            )) {
                assertFalse(
                    "$name 里出现了 `$banned` —— 中继地址只能由服务端下发",
                    code.contains(banned),
                )
            }
        }
    }
}
