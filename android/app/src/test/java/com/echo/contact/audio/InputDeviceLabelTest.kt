package com.echo.contact.audio

import android.media.AudioDeviceInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 麦克风选项**在界面上叫什么**（`InputDevices.labelFor`，纯函数）。
 *
 * 为什么这件小事值得一组用例：设置页那一列就是用户挑设备的唯一依据，
 * 而它的输入是系统的 `type + productName`（厂商名千奇百怪）。两条口径要钉住：
 *  1. **类型说成人话**（不建议把 `TYPE_BLUETOOTH_SCO` 这种原样丢给用户看）；
 *  2. **不重复**："内置麦克风"、"Built-in Mic" 这类名字与类型同义，别拼成
 *     `手机麦克风（内置麦克风）`。
 */
class InputDeviceLabelTest {

    @Test
    fun `每种常见类型都说人话`() {
        assertEquals("手机麦克风", InputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_MIC, ""))
        assertEquals("蓝牙耳机麦克风", InputDevices.labelFor(AudioDeviceInfo.TYPE_BLUETOOTH_SCO, ""))
        assertEquals("有线耳麦", InputDevices.labelFor(AudioDeviceInfo.TYPE_WIRED_HEADSET, ""))
        assertEquals("USB 麦克风", InputDevices.labelFor(AudioDeviceInfo.TYPE_USB_DEVICE, ""))
        // 认不出的类型也要有话说，而不是留空
        assertEquals("其它麦克风", InputDevices.labelFor(12345, ""))
    }

    @Test
    fun `有意义的厂商名拼在后面`() {
        val s = InputDevices.labelFor(AudioDeviceInfo.TYPE_BLUETOOTH_SCO, "WH-1000XM5")
        assertTrue(s, s.startsWith("蓝牙耳机麦克风"))
        assertTrue(s, s.contains("WH-1000XM5"))
    }

    @Test
    fun `与类型同义的名字不重复拼`() {
        assertFalse(InputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_MIC, "内置麦克风").contains("（"))
        assertFalse(InputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_MIC, "Built-in Mic").contains("（"))
    }

    @Test
    fun `长得像型号串、超过 24 字的名字不拼`() {
        val long = "A".repeat(40)
        assertEquals("手机麦克风", InputDevices.labelFor(AudioDeviceInfo.TYPE_BUILTIN_MIC, long))
    }
}
