package com.echo.contact.ui

import com.echo.contact.data.dto.StatusResponse
import com.google.gson.Gson
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 首页那个「**经中继打开 DSH 标准版网页**」的入口（用户 2026-10-18 夜里：
 * *"首页新增入口：通过中继打开 DSH 标准版网页"*）。
 *
 * 这一条真正的危险不是"按钮没画出来"，而是两件更糟的事：
 *  ① **URL 被写死**（中继 hostId / 端口一变就全错，而且发出去的包装在别人手机上打不开）；
 *  ② **没有地址却装作能点**（用户点了没反应，或者打开一个 API 根 —— 然后以为 ECHO 坏了）。
 *
 * 所以判据四条：字段缺失=置灰且有说明、有地址=把**原样那个 URL**交给 opener（2026-10-18 深夜起
 * 这个 opener 接的是 **App 内 WebView**，不再是系统浏览器）、不写死、以及**入口不占一屏预算**
 * （它在展开区里，源码位置钉住）。
 */
class DshWebEntryTest {

    private val gson = Gson()

    // ---- ① 没有地址 → 置灰 + 说清为什么 ------------------------------------------------

    @Test
    fun `服务端没给字段时置灰 并说清'服务端未提供中继地址'`() {
        val entry = dshWebEntry(null)
        assertFalse("没有地址就不许可点", entry.enabled)
        assertTrue(
            "必须如实说'服务端未提供中继地址'，实际：${entry.problem}",
            entry.problem.contains("服务端未提供中继地址"),
        )
        assertTrue(
            "还得点名期望的字段（否则没人知道该加哪个键），实际：${entry.problem}",
            entry.problem.contains(DshWebEntrySpec.EXPECTED_FIELD),
        )
        // 置灰时点了什么都不能发生
        var opened: String? = null
        assertFalse("置灰时 openDshWeb 必须返回 false", openDshWeb(entry) { opened = it; true })
        assertNull("置灰时不许调用 opener", opened)
    }

    @Test
    fun `空白串与只有空格也按'没给'处理`() {
        for (raw in listOf("", "   ", "\n")) {
            val entry = dshWebEntry(raw)
            assertFalse("「${raw.replace("\n", "\\n")}」不该可点", entry.enabled)
            assertTrue(entry.problem.contains("服务端未提供中继地址"))
        }
    }

    /**
     * 服务端**自己说了原因**时，界面必须照它说（它知道缺的是中继、地址还是 harness）。
     *
     * 现行契约：`app/relay.py::web_entry()` 在 url 为空时一定带一句 reason，
     * 例如"中继没打开（设置 → 中继 → 允许手机经后端中转）"。
     */
    @Test
    fun `服务端给了原因就照它说 那句话能直接指路`() {
        val reason = "中继没打开（设置 → 中继 → 允许手机经后端中转）"
        val entry = dshWebEntry("", reason)
        assertFalse(entry.enabled)
        assertTrue(
            "服务端那句原因（指向具体设置项）必须原样显示出来，实际：${entry.problem}",
            entry.problem.contains(reason),
        )
        assertTrue(
            "还得保留'服务端未提供中继地址'这个定性，实际：${entry.problem}",
            entry.problem.contains("服务端未提供中继地址"),
        )
    }

    @Test
    fun `给了但不是 http-s 地址也置灰 并把原值带出来`() {
        val entry = dshWebEntry("192.168.1.5:8970/v1/relay/x/")
        assertFalse("不是 http(s) 就打不开，不许假装可点", entry.enabled)
        assertTrue("得说清它是什么，实际：${entry.problem}", entry.problem.contains("http"))
        assertTrue(
            "得把服务端给的那个值带出来（排查要看它到底是什么），实际：${entry.problem}",
            entry.problem.contains("192.168.1.5"),
        )
    }

    // ---- ② 有地址 → 交给 opener（现在是 App 内嵌页），交出去的就是那个 URL -----------------

    @Test
    fun `有地址时可点 而且交给 opener 的正是服务端给的那串`() {
        // 形状照**现行**服务端契约（`app/relay.py::web_entry()`：
        // http://后端/v1/relay/<hostId>/ + api/relaydsh/<只读会话>/）
        val url = "https://echo.example.com/v1/relay/h-9f2c/api/relaydsh/tok-7d1e/"
        val entry = dshWebEntry(url, "")
        assertTrue("合法 http(s) 地址应该可点", entry.enabled)
        assertEquals(url, entry.url)

        var opened: String? = null
        val ok = openDshWeb(entry) { opened = it; true }
        assertTrue("opener 成功时 openDshWeb 要返回 true", ok)
        assertEquals("交给 opener 的必须是**原样**那串（App 不拼、不改、不加参数）", url, opened)
    }

    @Test
    fun `前后空格会被去掉 但路径一个字符都不改`() {
        val entry = dshWebEntry("  http://h:8970/v1/relay/abc/api/relaydsh/t1/  ")
        assertEquals("http://h:8970/v1/relay/abc/api/relaydsh/t1/", entry.url)
        assertTrue(entry.enabled)
    }

    @Test
    fun `opener 失败时如实返回 false`() {
        val entry = dshWebEntry("http://h/v1/relay/abc/api/relaydsh/t1/")
        assertFalse("opener 失败时不许假报'打开了'", openDshWeb(entry) { false })
    }

    // ---- ③ 字段契约（Gson 静默丢字段是老坑）-------------------------------------------

    @Test
    fun `status 回包里的三个 dshWeb 字段都能读进来 缺了就是 null`() {
        val withUrl = gson.fromJson(
            """{"version":"1.2.3","dshWebUrl":"http://h/v1/relay/abc/api/relaydsh/t1/",
               "dshWebReason":"","dshWebReadOnly":true}""",
            StatusResponse::class.java,
        )
        assertEquals("http://h/v1/relay/abc/api/relaydsh/t1/", withUrl.dshWebUrl)
        assertEquals("", withUrl.dshWebReason)
        assertEquals(true, withUrl.dshWebReadOnly)
        assertTrue(dshWebEntryOf(withUrl).enabled)

        // 服务端"现在打不开"：url 空、reason 有话说 → 置灰并照它说
        val unavailable = gson.fromJson(
            """{"dshWebUrl":"","dshWebReason":"DSH 标准版 harness 没在运行",
               "dshWebReadOnly":true}""",
            StatusResponse::class.java,
        )
        val entry = dshWebEntryOf(unavailable)
        assertFalse(entry.enabled)
        assertTrue("要照服务端那句原因显示：${entry.problem}", entry.problem.contains("harness 没在运行"))

        // 老服务端没有这几个键 → null（界面据此置灰，不是崩、也不是白屏）
        val old = gson.fromJson("""{"version":"1.2.3"}""", StatusResponse::class.java)
        assertNull("老服务端没有 dshWebUrl", old.dshWebUrl)
        assertFalse(dshWebEntryOf(old).enabled)
        assertTrue(dshWebEntryOf(old).problem.contains("服务端未提供中继地址"))

        // 别名也认（PC 那条线最终叫这几个名字之一都不必改手机端）
        for (key in listOf("dsh_web_url", "relayDshUrl", "dshWebPage")) {
            val aliased = gson.fromJson("""{"$key":"http://h/v1/relay/abc/web/"}""", StatusResponse::class.java)
            assertEquals("别名 $key 没读进来", "http://h/v1/relay/abc/web/", aliased.dshWebUrl)
        }
    }

    // ---- ④ 入口的位置与"不写死" -------------------------------------------------------

    @Test
    fun `入口在合并卡的展开区里 所以一屏预算一个 dp 都不用动`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt")
        val expandedAt = src.indexOf("if (expanded) {")
        val entryAt = src.indexOf("DshWebEntryRow(dshWeb, context)")
        assertTrue("StatusCard.kt 里找不到展开区", expandedAt >= 0)
        assertTrue("StatusCard.kt 里没有这个入口（用户要的'首页新增入口'不见了）", entryAt >= 0)
        assertTrue(
            "入口跑到展开区**之前**了：那会多占一行，360×640 的一屏预算（余 22dp）会被挤掉",
            entryAt > expandedAt,
        )
        // 加完之后预算仍然放得下（与 HomeLayoutTest 同一份计算）
        assertTrue(
            "加了入口之后 360×640 放不下了",
            homeLayoutPlanForScreen(360f, 640f).fits,
        )
    }

    /** 只留**代码行**（注释里引用契约形状是允许的 —— 那是在说明，不是在写死）。 */
    private fun codeOnly(src: String): String = src.lineSequence()
        .filterNot { line ->
            val t = line.trim()
            t.startsWith("//") || t.startsWith("*") || t.startsWith("/*") || t.startsWith("*/")
        }
        .joinToString("\n")

    @Test
    fun `URL 不许写死 中继路径与主机名都不许出现在代码里`() {
        for (name in listOf("ui/DshWebEntry.kt", "ui/StatusCard.kt", "data/dto/StatusDtos.kt")) {
            val code = codeOnly(moduleText("src/main/java/com/echo/contact/$name"))
            for (banned in listOf("/v1/relay", "relaydsh", "127.0.0.1", "192.168.", "localhost")) {
                assertFalse(
                    "$name 里出现了 `$banned` —— 中继地址必须由服务端下发，不许写死/自己拼",
                    code.contains(banned),
                )
            }
        }
    }
}
