package com.echo.contact.ui

import com.echo.contact.data.dto.StatusComponent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 首页顶部**那一张合并卡**的判据（用户 2026-10-18 中午）：
 *
 * > *"主页上端的 ECHO 触点那个可以跟下面的服务状态合并"*
 *
 * 合并之后这张卡只有三行：**应用名 + 连接状态灯** / **配对地址（可复制）** / **组件概要（点开看详情）**。
 * 三行的内容都是纯函数算出来的，所以能在 JVM 里钉住：
 *
 *  * 灯的三档**只**来自 `MainViewModel.connected` 那个现成三值（不编"重连中"这种 App 看不到的状态）；
 *  * 地址为空时**如实**说"未配对"（不是留一行空白）；
 *  * 组件概要把 9 个组件**一个不漏**地分进"正常 / 未启用 / 异常"三档 ——
 *    漏一个就等于骗人说"都正常"（`dsh=skipped`、`wake=disabled` 在真机上是常态）。
 *
 * 另外钉住"信息一个都没少"：原来那张卡里的 5 格（服务/智能体/语音合成/会议/回顾）、
 * 9 个组件的逐条明细、版本、已运行时长、启动提示，都还在这个文件的展开区里。
 */
class HomeTopCardTest {

    @Test
    fun `连接状态灯三档都来自现有状态`() {
        assertEquals(ConnLight("已连接", ConnLevel.OK), homeConnLight(true))
        assertEquals(ConnLight("未连接 · 自动重试", ConnLevel.DOWN), homeConnLight(false))
        assertEquals(ConnLight("连接中…", ConnLevel.PENDING), homeConnLight(null))
    }

    @Test
    fun `未连接那档说的是 App 真在做的事`() {
        val down = homeConnLight(false)
        // "自动重试"不是客套话：卡片可见时每 5 秒会再探一次（MainViewModel.autoRefreshTick），
        // 探通了这一档就会变回"已连接"
        assertTrue("未连接要告诉用户会自动重试：${down.label}", down.label.contains("自动重试"))

        // App 看不到"服务端正在重连"这个过程，所以不许画一个"重连中"骗人
        listOf(true, false, null).forEach {
            assertFalse("不该出现 App 无法确认的「重连中」", homeConnLight(it).label.contains("重连"))
        }
    }

    @Test
    fun `配对地址空的时候如实说未配对`() {
        assertEquals("http://192.168.1.5:8970", addressLine("http://192.168.1.5:8970"))
        assertEquals("地址两边的空格要去掉（复制出去不能带空白）",
            "http://192.168.1.5:8970", addressLine("  http://192.168.1.5:8970  "))
        assertEquals("https://relay.example.com/echo", addressLine("https://relay.example.com/echo"))

        assertTrue(addressLine("").contains("未配对"))
        assertTrue("未配对要指出在哪配对", addressLine("").contains("设置"))
        assertTrue(addressLine("   ").contains("未配对"))
    }

    @Test
    fun `9 个组件的概要按三档数且一个不漏`() {
        // 真机那台机的形状：9 个组件里 online/ready 一片 + 一个 skipped + 一个 disabled
        val components = listOf(
            StatusComponent(name = "server", status = "online"),
            StatusComponent(name = "dsh", status = "skipped"),
            StatusComponent(name = "harness", status = "online"),
            StatusComponent(name = "stt", status = "ready"),
            StatusComponent(name = "tts", status = "online"),
            StatusComponent(name = "wake", status = "disabled"),
            StatusComponent(name = "hotkey", status = "online"),
            StatusComponent(name = "meeting", status = "online"),
            StatusComponent(name = "diarize", status = "online"),
        )
        val summary = componentSummary(components)

        assertEquals("总数就是服务端给的组件数（不写死 9）", 9, summary.total)
        assertEquals(7, summary.healthy)
        assertEquals("skipped / disabled 是「未启用」，不是「坏了」", 2, summary.disabled)
        assertEquals(0, summary.broken)
        // 三档加起来必须等于总数：漏掉任何一个都会让这句话变成假话
        assertEquals(summary.total, summary.healthy + summary.disabled + summary.broken)
        assertEquals("7/9 组件正常 · 2 未启用", summary.label)
    }

    @Test
    fun `全部正常时就是一句 N 比 N`() {
        val allOk = (1..9).map { StatusComponent(name = "c$it", status = "online") }
        assertEquals("9/9 组件正常", componentSummary(allOk).label)
    }

    @Test
    fun `有故障时那句话会点名异常数`() {
        val mixed = listOf(
            StatusComponent(name = "server", status = "online"),
            StatusComponent(name = "stt", status = "ready"),
            StatusComponent(name = "tts", status = "error"),
            StatusComponent(name = "wake", status = "offline"),
            StatusComponent(name = "dsh", status = "skipped"),
        )
        val summary = componentSummary(mixed)
        assertEquals(5, summary.total)
        assertEquals(2, summary.healthy)
        assertEquals(1, summary.disabled)
        assertEquals("error/offline 要算异常", 2, summary.broken)
        assertEquals("2/5 组件正常 · 1 未启用 · 2 异常", summary.label)
    }

    @Test
    fun `还没读到组件时说的是还读不到`() {
        val empty = componentSummary(emptyList())
        assertEquals(0, empty.total)
        assertTrue("读不到就该说读不到（不能画成「0/0 正常」）", empty.label.contains("读不到"))
        assertFalse(empty.label.contains("0/0"))
    }

    @Test
    fun `原来的信息一个都没少（5 格 组件明细 版本 运行时长 启动提示）`() {
        // 合并 ≠ 删信息：这几样必须还能在这张卡（展开区）里找到
        val src = moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt")

        for (cell in listOf("服务", "智能体", "语音合成", "会议", "回顾")) {
            assertTrue("原来那 5 格里少了「$cell」", src.contains("\"$cell\""))
        }
        assertTrue("9 个组件的逐条明细没了", src.contains("status!!.components.forEach"))
        assertTrue("组件的英文名→中文没复用原来的映射", src.contains("componentLabel("))
        assertTrue("组件明细里的那行 detail 没了", src.contains("c.detail.ifBlank"))
        assertTrue("版本号没了", src.contains("ECHO 版本"))
        assertTrue("已运行时长没了", src.contains("uptimeText()"))
        assertTrue("启动提示没了", src.contains("startupNotes"))
        assertTrue("刷新按钮没了", src.contains("vm.refresh()"))
    }

    @Test
    fun `合并卡是三行结构（应用名与连接 地址 概要）`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/StatusCard.kt")

        // ① 第一行：应用名（常量）+ 连接状态（纯函数）
        assertTrue("第一行没画应用名", src.contains("APP_TITLE"))
        assertTrue("第一行没画连接状态", src.contains("homeConnLight("))
        assertTrue("状态灯没画", src.contains("StatusDot(lightColor"))
        // ② 第二行：配对地址（只有一个来源，且可复制）
        assertTrue("第二行没画配对地址", src.contains("addressLine(baseUrl)"))
        assertTrue("地址点一下要能复制", src.contains("copyToClipboard"))
        // ③ 第三行：组件概要（可展开看详情）
        assertTrue("第三行没画组件概要", src.contains("componentSummary("))
        assertTrue("概要点不开", src.contains("expanded = !expanded"))

        // 这张卡是首页第一项（用户要的"上端"）
        val tab = moduleText("src/main/java/com/echo/contact/ui/SuperAssistantTab.kt")
        assertTrue("首页第一项必须就是这张合并卡", tab.contains("HomeTopCard(vm)"))
        assertFalse("旧的那张独立「服务状态」卡不该再被挂出来", tab.contains("EchoStatusCard"))
    }
}
