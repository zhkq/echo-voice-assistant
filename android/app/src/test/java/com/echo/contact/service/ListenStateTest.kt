package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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

    // ------------------------------------------------- 任务 B4：首页那根常驻细条的文案

    /**
     * "不在听 → 不显示"是需求里的硬要求：首页不该常驻一条什么都没在发生的空条。
     * 这是 `stripOf` 与 `textsOf` **唯一的语义差别**（通知里 OFF 也要有一句话，
     * 因为那条通知本身还在，得说清"已经关了"）。
     */
    @Test
    fun `没在听时细条不显示`() {
        assertNull(
            "OFF 必须返回 null —— 否则首页会常驻一条空条",
            ListenState.stripOf(ListenState.Phase.OFF, "", 0),
        )
        assertNull(
            "OFF 即使带着停下来的原因也不该显示（原因归「说话」页那张卡）",
            ListenState.stripOf(ListenState.Phase.OFF, "服务被停止", 0),
        )
    }

    @Test
    fun `每个在跑的档位都要有细条且互相分得开`() {
        val running = ListenState.Phase.values().filter { it != ListenState.Phase.OFF }
        val strips = running.map { ListenState.stripOf(it, "", 0) }
        assertTrue("在跑的档位都必须有细条：$running", strips.none { it == null })
        val labels = strips.map { it!!.label }
        assertEquals(
            "相位词撞车 = 用户分不清还在听、还是被暂停了",
            running.size,
            labels.distinct().size,
        )
        assertTrue("细条不该有空词", labels.none { it.isBlank() })
    }

    /**
     * 需求原话："「已暂停（耳机键按的）」与「在听」要能分开，别都说在听"。
     * 上面那条测的是"词不同"，这条测"话不同" —— 两档的 label 与 text 都不能一样。
     */
    @Test
    fun `已暂停与在听必须说得不一样`() {
        val paused = ListenState.stripOf(ListenState.Phase.PAUSED, "", 0)!!
        val listening = ListenState.stripOf(ListenState.Phase.LISTENING, "", 0)!!
        assertTrue("已暂停与在听不能同词", paused.label != listening.label)
        assertTrue("已暂停与在听不能同一句话", paused.text != listening.text)
    }

    /**
     * 暂停的**原因**优先于默认话术：服务在按耳机键暂停时写的是
     * "按了耳机键：先不听了，再按一下继续"（见 `ListenService.setUserPaused`）。
     * 用户扫一眼细条要能知道"为什么停了"，而不是一句干巴巴的"已暂停"。
     */
    @Test
    fun `暂停原因要原样说出来`() {
        val reason = "按了耳机键：先不听了，再按一下继续"
        val strip = ListenState.stripOf(ListenState.Phase.PAUSED, reason, 0)!!
        assertEquals(reason, strip.text)
    }

    @Test
    fun `细条也要说清已经听出去几句`() {
        // 没有具体 detail 时，计数就是"它在动"的证据（用户据此判断它没卡死）。
        val strip = ListenState.stripOf(ListenState.Phase.LISTENING, "", 3)!!
        assertTrue("在听且已送出 3 句时正文要带上 3：${strip.text}", strip.text.contains("3"))
        assertTrue(
            "没有句数时也不该显示成空（否则细条看着像坏了）",
            ListenState.stripOf(ListenState.Phase.LISTENING, "", 0)!!.text.isNotBlank(),
        )
    }

    @Test
    fun `出错停下也要显示并说这是停下而不是在听`() {
        // 出错后 running=false 但相位不是 OFF：细条要留着并说明"它已经停下了"，
        // 否则用户会以为还在听，说完一句却什么都没发生。
        ListenState.set(ListenState.Phase.FAILED, "没有麦克风权限：请先在「指令」页给权限")
        val strip = ListenState.stripOf(
            ListenState.phase.value, ListenState.detail.value, ListenState.segments.value,
        )
        assertTrue("出错后细条该显示（要让人知道它停了）", strip != null)
        assertTrue("要如实说停下了：${strip!!.label}", strip.label.contains("停下"))
        assertTrue("原因要带上", strip.text.contains("麦克风权限"))
    }
}
