package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回顾那三处"失败/边界"的判据（2026-10-18 复查确认、上一轮没修完的三条）。
 *
 * 1. `loadReviewDays` 吞异常 → 页面显示"还没有回顾历史"（**假空态**：用户以为今天没做，
 *    其实是一个字节都没读到）；
 * 2. `openReviewDay` 遇到非 2xx / 解析异常 → 点"看这一天"**没有任何反应**；
 * 3. `stopHostReview` 不看服务端如实回的 `stopped` → 明明没有可停的也恒说"已请求停止"。
 *
 * 为什么用**源码级**判据：`MainViewModel` 是 `AndroidViewModel`（要 `Context`），
 * JVM 门禁里造不出来；而这三处的病根分别是"那个分支里没写任何东西""那个 catch 是空的"
 * "那个字段没被读"——只有读源码才钉得住（纯函数部分照旧在 JVM 里逐字断言）。
 */
class ReviewFailuresTest {

    private val vmSource = moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")
    private val tabSource = moduleText("src/main/java/com/echo/contact/ui/ReviewTab.kt")

    // ---- ① 历史接口失败：必须有提示，且不许显示假空态 ------------------------------

    @Test
    fun `历史接口失败要说原因 而不是画成没有历史`() {
        // 纯函数：任何原因（含 null/空白）都能拼出一句可显示的话，绝不出现"null"
        assertEquals("回顾历史读取失败：timeout", reviewFailureLine(REVIEW_HISTORY_FAILED, "timeout"))
        for (reason in listOf(null, "", "   ")) {
            val line = reviewFailureLine(REVIEW_HISTORY_FAILED, reason)
            assertTrue("前缀必须固定：$line", line.startsWith(REVIEW_HISTORY_FAILED))
            assertFalse("不许拼出 null（看着像原因，其实什么都没说）：$line", line.contains("null"))
            assertTrue("原因不能是空的：$line", line.substringAfter("：").isNotBlank())
        }

        // 源码：非 2xx 与异常两条路都要写这条提示（以前 catch 是空的、非 2xx 连 else 都没有）
        val body = functionBody(vmSource, "fun loadReviewDays(", "fun openReviewDay(")
        assertTrue(
            "非 2xx 必须写提示（以前只判 isSuccessful，失败什么都不写）",
            body.contains("parseError(resp)"),
        )
        assertEquals(
            "非 2xx 与异常两条路都必须落到同一句提示",
            2,
            occurrencesOf(body, "reviewFailureLine(REVIEW_HISTORY_FAILED"),
        )
        assertFalse(
            "旧注释「历史不是主功能，静默」正是那个假空态的病根",
            body.contains("历史不是主功能，静默"),
        )

        // 界面：提示要摆出来；而且只在**真的读到了**时才画"还没有回顾历史"
        assertTrue(
            "回顾页必须读这个失败字段",
            tabSource.contains("vm.reviewDaysNote.collectAsState()"),
        )
        assertTrue(
            "失败提示要显示在历史那一块",
            tabSource.contains("daysNote?.takeIf { it.isNotBlank() }"),
        )
        assertTrue(
            "有失败提示时不许再显示「还没有回顾历史」（假空态）",
            tabSource.contains("if (daysNote.isNullOrBlank())"),
        )
    }

    // ---- ② 打开某一天失败：必须有提示（点"看这一天"不许掉进黑洞）-------------------

    @Test
    fun `打开某一天失败要说出原因 成功路径照旧`() {
        val body = functionBody(vmSource, "fun openReviewDay(", "fun closeReviewDay(")

        // 成功路径**逐字不变**：仍然只是把回包塞进 _reviewDay
        assertTrue("成功路径照旧", body.contains("_reviewDay.value = resp.body()"))
        // 非 2xx：以前没有 else，什么都没发生
        assertTrue("非 2xx 必须说话", body.contains("parseError(resp)"))
        assertTrue(
            "非 2xx / 网络异常 / 解析异常都要落到同一句提示",
            occurrencesOf(body, "reviewFailureLine(REVIEW_DAY_OPEN_FAILED") >= 2,
        )

        // 解析异常那条 catch 以前是空的 —— 单独钉住它现在也会说话
        val parseCatch = body.indexOf("catch (e: Exception)")
        assertTrue("必须有解析异常那条 catch", parseCatch > 0)
        assertTrue(
            "解析异常（后端回了个不是预期形状的 JSON）也必须说，否则点了没反应",
            body.substring(parseCatch).contains("reviewFailureLine(REVIEW_DAY_OPEN_FAILED"),
        )
        // 旧写法（只写 _reviewNote 且解析异常静默）不许回来
        assertFalse(
            "旧写法：IO 异常只写 _reviewNote，解析异常一个字节都不写",
            body.contains("catch (_: Exception) {\n            }"),
        )
    }

    // ---- ③ 停回顾：服务端的 stopped 三态，不编 --------------------------------------

    @Test
    fun `停主机回顾按 stopped 三态如实说`() {
        // true=真的停了 / false=本来就没在跑 / null=拿不到字段，退回原话术
        assertEquals("已停止主机上的回顾", reviewStopNote(true))
        assertTrue(
            "stopped=false 必须如实说「已经不在跑了」，不能说成停掉了",
            reviewStopNote(false).contains("不在跑了") && reviewStopNote(false).contains("没有可停"),
        )
        assertEquals(
            "拿不到该字段就退回现有话术（不编真假）",
            "已请求停止主机上的回顾",
            reviewStopNote(null),
        )

        // 回包解析：只认真正的布尔
        assertTrue(stoppedFlag(mapOf("ok" to true, "stopped" to true)) == true)
        assertTrue(stoppedFlag(mapOf("ok" to true, "stopped" to false)) == false)
        assertNull("老服务端没有这个字段", stoppedFlag(mapOf("ok" to true)))
        assertNull("只认真正的布尔，不猜字符串", stoppedFlag(mapOf("stopped" to "true")))
        assertNull("数字也不猜（0/1 不是布尔）", stoppedFlag(mapOf("stopped" to 0)))
        assertNull("没有回包", stoppedFlag(null))

        // 源码：ViewModel 必须真的读回包里的 stopped（不是恒定一句话）
        val body = functionBody(vmSource, "fun stopHostReview(", "fun cancelReviewDictation(")
        assertTrue(
            "必须读服务端如实回的 stopped",
            body.contains("stoppedFlag(resp.body())"),
        )
        assertTrue("话术只能来自一处（三态纯函数）", body.contains("reviewStopNote("))
        assertFalse(
            "旧写法：只要 HTTP 200 就恒说「已请求停止」",
            body.contains("\"已请求停止主机上的回顾\""),
        )
    }
}
