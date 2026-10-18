package com.echo.contact.data

import com.echo.contact.ui.moduleText
import com.echo.contact.ui.repoRootOrNull
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * **客户端等待上限必须长于服务端的硬上限**（2026-10-18 复查的第 4 条）。
 *
 * 旧值 `LONG_READ_TIMEOUT_S = 330`（5.5 分钟）< 服务端 `app/daily_review.py` 的
 * `ROUND_HARD_MAX_S = 900`（15 分钟）。后果不是"等短了点"，而是**失败模式搞反了**：
 * 超过 5.5 分钟的整理会被**手机先放弃**，而电脑还在干、结果也已经落库 ——
 * 用户看到失败，面板里却有那一条。
 *
 * 所以这里不写死"960 > 330"这种自己跟自己比的断言，而是**真的去读服务端那个常量**：
 * 以后谁把服务端的硬上限调大，这条用例会立刻红，而不是等到真机上又出现
 * "手机上失败了、电脑还在干"。
 */
class ApiTimeoutBudgetTest {

    @Test
    fun `客户端长超时必须大于服务端的硬上限`() {
        val root = repoRootOrNull()
        if (root == null) {
            // 纯 android 检出（仓库里没有服务端源码）：至少把"必须 > 900"这条闸钉住。
            // 服务端的值来自 app/daily_review.py 的 ROUND_HARD_MAX_S（900 秒）。
            assertTrue(
                "客户端等待上限必须比服务端的 900 秒更长，让服务端先放弃",
                ApiClient.LONG_READ_TIMEOUT_S > 900,
            )
            return
        }

        val serverSource = File(root, "app/daily_review.py").readText(Charsets.UTF_8)
        val match = Regex("(?m)^ROUND_HARD_MAX_S\\s*=\\s*([0-9]+(?:\\.[0-9]+)?)")
            .find(serverSource)
        assertNotNull("服务端必须还有 ROUND_HARD_MAX_S（客户端超时就是按它算的）", match)

        val hardMax = match!!.groupValues[1].toDouble()
        assertTrue(
            "服务端硬上限应当仍是 900 秒（这条是服务端自己的预算，客户端不改它）",
            hardMax == 900.0,
        )
        assertTrue(
            "客户端 ${ApiClient.LONG_READ_TIMEOUT_S}s 必须 > 服务端 ${hardMax.toInt()}s：" +
                "否则超过 ${hardMax.toInt()} 秒的整理会被手机先放弃，而电脑还在干",
            ApiClient.LONG_READ_TIMEOUT_S > hardMax,
        )
    }

    @Test
    fun `注释里写清了为什么必须比服务端长`() {
        val source = moduleText("src/main/java/com/echo/contact/data/ApiClient.kt")
        assertTrue(
            "注释必须点名服务端那个常量 —— 以后改这个数的人要知道它在跟谁比",
            source.contains("ROUND_HARD_MAX_S"),
        )
        assertTrue(
            "注释必须写清方向：让服务端先放弃",
            source.contains("服务端先放弃"),
        )
        assertTrue(
            "并且明说别改短服务端（900 是它自己的预算）",
            source.contains("别改短服务端"),
        )
    }
}
