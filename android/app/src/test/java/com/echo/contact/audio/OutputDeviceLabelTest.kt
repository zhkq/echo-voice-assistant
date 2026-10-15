package com.echo.contact.audio

import android.media.AudioDeviceInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 输出设备选项**在界面上叫什么**，以及"这次到底有没有用上偏好"怎么说
 * （`OutputDevices.labelFor` / `rank` / `matchesKeyword` / `routeOutcome`，纯函数）。
 *
 * 为什么这件小事值得一组用例 —— 用户 2026-10-16 拍板的是"只做实际能做到的，
 * 不做选了下拉却没用那种控件"。而"实际能做到"这件事只有两处说得清：
 *  1. 设置页那一列（挑设备的唯一依据，输入是系统的 `type + productName`）；
 *  2. 播放完那句**如实**的结果（偏好成没成 —— 说成"一定用它"就是骗人）。
 */
class OutputDeviceLabelTest {

    @Test
    fun `每种常见类型都说人话`() {
        assertEquals("手机扬声器", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, ""))
        assertEquals("手机听筒", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_EARPIECE, ""))
        assertEquals("蓝牙音箱/耳机", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, ""))
        assertEquals("蓝牙通话音频", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BLUETOOTH_SCO, ""))
        assertEquals("有线耳机", OutputDevices.labelFor(AudioDeviceInfo.TYPE_WIRED_HEADPHONES, ""))
        assertEquals("USB 音频", OutputDevices.labelFor(AudioDeviceInfo.TYPE_USB_DEVICE, ""))
        // 认不出的类型也要有话说，而不是留空或者把 TYPE_xxx 原样丢给用户
        assertEquals("其它输出设备", OutputDevices.labelFor(12345, ""))
    }

    @Test
    fun `有意义的厂商名拼在后面`() {
        val s = OutputDevices.labelFor(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, "WH-1000XM5")
        assertTrue("类型必须先说清楚：$s", s.startsWith("蓝牙音箱/耳机"))
        assertTrue("挑设备得看得到型号：$s", s.contains("WH-1000XM5"))
    }

    @Test
    fun `与类型同义的名字不重复拼`() {
        // 系统给的内置设备名常常就是"内置扬声器" —— 拼成 `手机扬声器（内置扬声器）` 很蠢
        assertFalse(OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, "内置扬声器").contains("（"))
        assertFalse(OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, "Built-in Speaker").contains("（"))
        assertFalse(OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_EARPIECE, "听筒").contains("（"))
    }

    @Test
    fun `长得像型号串、超过 24 字的名字不拼`() {
        val long = "A".repeat(40)
        assertEquals("手机扬声器", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, long))
    }

    @Test
    fun `空名字与只有空白的名字都只回类型`() {
        assertEquals("手机扬声器", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER, ""))
        assertEquals("蓝牙音箱/耳机", OutputDevices.labelFor(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP, "   "))
    }

    @Test
    fun `同义关键词判据自己也要成立`() {
        // 这张表是"名字与类型同义吗"的**唯一**判据，所以它自己也钉一下：
        // 漏一个词，界面上就会出现 `手机扬声器（Speaker）` 这种话
        assertTrue(OutputDevices.matchesKeyword("内置扬声器"))
        assertTrue(OutputDevices.matchesKeyword("Speaker"))
        assertTrue(OutputDevices.matchesKeyword(""))            // 空名字不算"有意义的名字"
        assertFalse(OutputDevices.matchesKeyword("WH-1000XM5"))  // 型号是有意义的
    }

    @Test
    fun `排序把常用输出放前面、听筒放后面`() {
        // 第一支会被 describe() 当成"跟随系统时会用哪支"，所以扬声器必须是第一
        assertTrue(OutputDevices.rank(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) <
            OutputDevices.rank(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP))
        assertTrue(OutputDevices.rank(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP) <
            OutputDevices.rank(AudioDeviceInfo.TYPE_BUILTIN_EARPIECE))
        // 认不出的类型排在最后，但不与"听筒"混淆
        assertEquals(9, OutputDevices.rank(12345))
    }

    @Test
    fun `没设偏好时说跟随系统`() {
        val r = routeOutcome(preferred = false, label = "", applied = false)
        assertFalse("没选设备时必须说没选，不能假装设过", r.preferred)
        assertFalse(r.applied)
        assertTrue("必须有一句人话，不能是空串：'${r.note}'", r.note.isNotBlank())
        assertTrue("得说清'这次按系统默认播'：${r.note}", r.note.contains("系统默认"))
    }

    @Test
    fun `偏好设上了也要说最终路由由系统定`() {
        val r = routeOutcome(preferred = true, label = "蓝牙音箱/耳机（XM5）", applied = true)
        assertTrue(r.applied)
        assertTrue("得带上设备名：${r.note}", r.note.contains("蓝牙音箱/耳机（XM5）"))
        // 关键：不能让人以为"一定从它出声" —— 蓝牙/听筒的最终路由是系统说了算
        assertTrue("必须写明最终路由由系统定：${r.note}", r.note.contains("由系统定"))
    }

    @Test
    fun `系统没接受偏好时如实说、且明说这不算错误`() {
        val r = routeOutcome(preferred = true, label = "手机听筒", applied = false)
        assertTrue(r.preferred)
        assertFalse("setPreferredDevice 返回 false 就是没设上，不许含糊", r.applied)
        assertTrue("得带上设备名：${r.note}", r.note.contains("手机听筒"))
        assertTrue("得说清这次按系统默认播：${r.note}", r.note.contains("系统默认"))
        assertTrue("得明说这不算错误（偏好而已，系统可以不听）：${r.note}", r.note.contains("不算错误"))
    }
}
