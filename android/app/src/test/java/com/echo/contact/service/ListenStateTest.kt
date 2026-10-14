package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 常驻听写的状态与文案（`ListenState`）—— 纯 JVM。
 *
 * 为什么文案也值得钉：这里两行字是用户判断"它到底在干什么"的**唯一**依据
 * （锁屏时看不到界面）。"在听"和"正在交给 ECHO"混了，人就会以为说完没反应；
 * 出错档没有原因，人只会反复开关。
 */
class ListenStateTest {

    @Before
    fun setUp() {
        ListenState.reset()
    }

    @Test
    fun `每个档位都有不一样的标题`() {
        val titles = ListenState.Phase.values().map { ListenState.textsOf(it, "", 0).first }
        assertEquals(
            "档位标题撞车 = 用户分不清它在干什么",
            ListenState.Phase.values().size,
            titles.distinct().size,
        )
        assertTrue(titles.none { it.isBlank() })
    }

    @Test
    fun `说完的计数写进正文`() {
        val (_, body) = ListenState.textsOf(ListenState.Phase.LISTENING, "", 3)
        assertTrue(body.contains("3"))
    }

    @Test
    fun `等待那一档要说清在等什么`() {
        val (_, body) = ListenState.textsOf(ListenState.Phase.THINKING, "", 0)
        assertTrue(body.contains("等"))
    }

    @Test
    fun `具体原因优先于默认话术`() {
        val (_, body) = ListenState.textsOf(
            ListenState.Phase.FAILED, "没有麦克风权限：请先在「指令」页给权限", 0,
        )
        assertEquals("没有麦克风权限：请先在「指令」页给权限", body)
    }

    @Test
    fun `running 只在真的在听时为真`() {
        ListenState.set(ListenState.Phase.STARTING)
        assertTrue(ListenState.running)
        ListenState.set(ListenState.Phase.LISTENING)
        assertTrue(ListenState.running)
        ListenState.set(ListenState.Phase.FAILED, "打不开麦克风")
        assertFalse("挂了就不该显示成在听", ListenState.running)
        ListenState.set(ListenState.Phase.OFF)
        assertFalse(ListenState.running)
    }

    @Test
    fun `计数在 reset 时归零`() {
        ListenState.countSegment()
        ListenState.countSegment()
        assertEquals(2, ListenState.segments.value)
        ListenState.reset()
        assertEquals(0, ListenState.segments.value)
        assertEquals(ListenState.Phase.OFF, ListenState.phase.value)
    }
}
