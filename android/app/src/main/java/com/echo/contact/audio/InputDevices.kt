package com.echo.contact.audio

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import com.echo.contact.service.AppPrefs

/**
 * **输入设备**（麦克风）—— App 自己的选项之一。
 *
 * 用户 2026-10-15 的口径：设置页要管"app 自己的选项，比如输入输出设备"。
 * 车里最典型的需求就是**用蓝牙耳机那支麦**，而不是手机自带的那支。
 *
 * ## 只做"指定一支麦"，不承诺"一定能用它"
 *
 * `AudioRecord.setPreferredDevice()` 的语义是**偏好**：系统可以不听（尤其蓝牙在通话/媒体
 * 档位切换时）。所以这里只做两件能站得住的事：
 *  1. 把偏好设上去；
 *  2. **如实回报**：选的那支不在了（拔了/换设备）→ 回落到系统默认，并把原因说出来
 *     （`describe()` 给 UI 用）。悄悄回落会让人以为"选了没用"。
 *
 * `labelFor` 是**纯函数**（JVM 可测）：`AudioDeviceInfo` 的 type 常量说成人话。
 */
object InputDevices {

    /** 挑麦时排前面的是"用户更可能要的"：内置 → 蓝牙 → 有线 → USB → 其它。 */
    private fun rank(type: Int): Int = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_MIC -> 0
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> 1
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> 2
        AudioDeviceInfo.TYPE_USB_DEVICE -> 3
        AudioDeviceInfo.TYPE_USB_HEADSET -> 4
        else -> 9
    }

    /**
     * `(type, 产品名)` → 一句中文。**纯函数**（`InputDeviceLabelTest` 钉着）。
     *
     * 产品名常常和类型重复（"内置麦克风"、"Built-in Mic"）或者长得像一串型号，
     * 那种就不往括号里塞了 —— 界面上出现 `手机麦克风（内置麦克风）` 很蠢。
     */
    fun labelFor(type: Int, productName: String): String {
        val kind = when (type) {
            AudioDeviceInfo.TYPE_BUILTIN_MIC -> "手机麦克风"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "蓝牙耳机麦克风"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "有线耳麦"
            AudioDeviceInfo.TYPE_USB_DEVICE -> "USB 麦克风"
            AudioDeviceInfo.TYPE_USB_HEADSET -> "USB 耳麦"
            AudioDeviceInfo.TYPE_TELEPHONY -> "通话麦克风"
            else -> "其它麦克风"
        }
        val name = productName.trim()
        // 厂商名常常**本身就是一句"麦克风"**（"内置麦克风"、"Built-in Mic"、"蓝牙耳机麦克风"）
        // 或者长得像一串型号 —— 那两种都不往括号里塞：界面上出现
        // `手机麦克风（内置麦克风）` 很蠢，塞一串 40 字符的型号也一样。
        val lower = name.lowercase()
        val skip = name.isEmpty() || name.length > 24 || name.contains(kind) ||
            lower.contains("麦克风") || lower.contains("mic") || lower.contains("microphone")
        return if (skip) kind else "$kind（$name）"
    }

    /** 系统里现在可用的输入设备（按 [rank] 排好）。 */
    fun list(context: Context): List<AudioDeviceInfo> {
        val am = context.applicationContext
            .getSystemService(Context.AUDIO_SERVICE) as? AudioManager ?: return emptyList()
        return try {
            am.getDevices(AudioManager.GET_DEVICES_INPUTS)
                .filter { it != null }
                .sortedWith(compareBy({ rank(it.type) }, { it.productName?.toString() ?: "" }))
        } catch (e: Exception) {                                  // 个别 ROM 会抛
            emptyList()
        }
    }

    fun labelOf(device: AudioDeviceInfo): String =
        labelFor(device.type, device.productName?.toString() ?: "")

    /**
     * 用户选的那支麦**现在还在不在**。返回 `null` = 跟随系统（没选，或选的那支不在了）。
     *
     * 调用方拿到 null 时按系统默认走，并可以用 [describe] 把"为什么回落了"说出来。
     */
    fun preferredInput(context: Context): AudioDeviceInfo? {
        val id = AppPrefs.inputDeviceId(context)
        if (id == AppPrefs.FOLLOW_SYSTEM) return null
        return list(context).firstOrNull { it.id == id }
    }

    /** 给界面的一句话：现在实际会用哪支麦（含"你选的那支不在了"这种如实说明）。 */
    fun describe(context: Context): String {
        val id = AppPrefs.inputDeviceId(context)
        val devices = list(context)
        if (id == AppPrefs.FOLLOW_SYSTEM) {
            val sys = devices.firstOrNull()
            return if (sys == null) "跟随系统（现在看不到可用麦克风）"
            else "跟随系统（系统会选：${labelOf(sys)}）"
        }
        val chosen = devices.firstOrNull { it.id == id }
        return if (chosen != null) labelOf(chosen)
        else "你选的那支麦克风不在了 —— 已回落到系统默认"
    }
}
