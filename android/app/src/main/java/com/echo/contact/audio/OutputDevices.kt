package com.echo.contact.audio

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import com.echo.contact.service.AppPrefs

/**
 * **输出设备**（往哪儿放音）—— App 自己的选项之一，与 [InputDevices] 同一个口径。
 *
 * ## 只做"实际能做到的"（用户 2026-10-15 拍板："按你说的"）
 *
 * 「谁念回复」有两条路，能力**不一样**，这里必须如实说清：
 *  * **手机念**（`SystemTts` → `android.speech.tts.TextToSpeech`）：**没有**指定输出设备的接口
 *    （系统 TTS 只认系统路由）→ 这条路只能跟随系统。设置页上照实写，不摆一个选了也没用的控件。
 *  * **ECHO 念**（`EchoTtsPlayer` → `MediaPlayer`）：`MediaPlayer.setPreferredDevice()` 可用 → 能设偏好。
 *    但它是**偏好**：蓝牙 / 听筒这类档位切换时，**最终路由由系统定** —— 所以文案一律写"**优先**用它"，
 *    绝不写"一定用它"（写了就是骗人）。
 *
 * ## 值失效就如实回落
 *
 * 存下来的是 `AudioDeviceInfo.id`；设备拔了/换了之后那个 id 就失效了。与输入侧同一条纪律：
 * **用它的时候必须回落到系统默认，并把"你选的那支不在了"说出来**（见 [describe]）。
 * 悄悄回落会让人以为"选了没用"。
 *
 * [labelFor] / [rank] / [matchesKeyword] 是**纯函数**（JVM 可测，`OutputDeviceLabelTest` 钉着）。
 */
object OutputDevices {

    /**
     * 挑输出设备时的排序：**"用户更可能要的"排前面**。
     *
     * 内置扬声器第一：它是"没插任何东西"时的默认，也是 `describe()` 里"跟随系统会用哪支"的答案
     * （车里最常见的是蓝牙，但那得等它真连上才出现在列表里）。
     * 听筒排最后：它音最小、还常常是"贴着耳朵"的场景 —— 放会议纪要最难听清。
     */
    fun rank(type: Int): Int = when (type) {
        AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> 0
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> 1
        AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> 2
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> 3
        AudioDeviceInfo.TYPE_WIRED_HEADSET -> 4
        AudioDeviceInfo.TYPE_USB_DEVICE -> 5
        AudioDeviceInfo.TYPE_USB_HEADSET -> 6
        AudioDeviceInfo.TYPE_HDMI -> 7
        AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> 8
        else -> 9
    }

    /**
     * 「这个名字与类型同义吗」—— 同义就不往括号里塞。
     *
     * 与 `InputDevices.labelFor` 是**故意重复**的一小段：两边的关键词表不一样
     * （麦那边关心 "mic/麦克风"，这边关心 "speaker/扬声器/音箱"），
     * 而把两张表硬合成一张，只会让任一侧的下一个特例去改对方的显示。
     */
    fun matchesKeyword(name: String): Boolean {
        val n = name.lowercase()
        if (n.isEmpty()) return true
        // 长度上限：一串 40 字符的型号塞进括号里，在手机那一列只会被省略号截掉
        if (name.length > 24) return true
        return listOf(
            "扬声器", "喇叭", "音箱", "听筒", "内置",
            "speaker", "headphone", "earpiece", "built-in", "builtin", "internal",
        ).any { n.contains(it) }
    }

    /**
     * `(type, 产品名)` → 一句中文。**纯函数**（`OutputDeviceLabelTest` 钉着）。
     *
     * 这一列是用户挑输出设备的**唯一依据**，所以：认不出的类型也要有话说（"其它输出设备"），
     * 而不是留空或者把 `TYPE_BLUETOOTH_A2DP` 原样丢出去。
     */
    fun labelFor(type: Int, productName: String): String {
        val kind = when (type) {
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "手机扬声器"
            AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "手机听筒"
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "蓝牙音箱/耳机"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "蓝牙通话音频"
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "有线耳机"
            AudioDeviceInfo.TYPE_WIRED_HEADSET -> "有线耳麦"
            AudioDeviceInfo.TYPE_USB_DEVICE -> "USB 音频"
            AudioDeviceInfo.TYPE_USB_HEADSET -> "USB 耳麦"
            AudioDeviceInfo.TYPE_HDMI -> "HDMI 音频"
            else -> "其它输出设备"
        }
        val name = productName.trim()
        // 厂商名常常**本身就是一句"扬声器/耳机"**（"内置扬声器"、"Built-in Speaker"），
        // 或者长得像一串型号 —— 那两种都不往括号里塞：界面上出现
        // `手机扬声器（内置扬声器）` 很蠢。
        return if (matchesKeyword(name)) kind else "$kind（$name）"
    }

    /** 系统里现在可用的**输出**设备（按 [rank] 排好）。 */
    fun list(context: Context): List<AudioDeviceInfo> {
        val am = audioManager(context) ?: return emptyList()
        return try {
            am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                .filter { it != null }
                .sortedWith(compareBy({ rank(it.type) }, { it.productName?.toString() ?: "" }))
        } catch (e: Exception) {                                  // 个别 ROM 会抛
            emptyList()
        }
    }

    fun labelOf(device: AudioDeviceInfo): String =
        labelFor(device.type, device.productName?.toString() ?: "")

    /**
     * 用户选的那支**现在还在不在**。`null` = 跟随系统（没选，或选的那支不在了）。
     *
     * 调用方拿到 null 时按系统默认走，并可以用 [describe] 把"为什么回落了"说出来。
     */
    fun preferredOutput(context: Context): AudioDeviceInfo? {
        val id = AppPrefs.outputDeviceId(context)
        if (id == AppPrefs.FOLLOW_SYSTEM) return null
        return list(context).firstOrNull { it.id == id }
    }

    /**
     * 给界面的一句话：ECHO 念这条路上**偏好**用哪支，以及"为什么回落了"。
     *
     * ⚠️ 这里说的是**偏好**，不是"实际一定走它" —— `MediaPlayer.setPreferredDevice()` 之后
     * 系统仍可能按自己的策略换路由（蓝牙/听筒尤其常见）。所以措辞里不能出现"正在用"这种保证。
     */
    fun describe(context: Context): String {
        val id = AppPrefs.outputDeviceId(context)
        val devices = list(context)
        val sys = devices.firstOrNull()                           // 排序后第一支 ≈ 系统默认
        if (id == AppPrefs.FOLLOW_SYSTEM) {
            return if (sys == null) "跟随系统（现在看不到可用输出设备）"
            else "跟随系统（ECHO 念时系统多半会用：${labelOf(sys)}）"
        }
        val chosen = devices.firstOrNull { it.id == id }
        return if (chosen != null) "优先用 ${labelOf(chosen)}"
        else "你选的那支输出设备不在了 —— 已回落到系统默认" +
            (sys?.let { "（${labelOf(it)}）" } ?: "")
    }

    private fun audioManager(context: Context): AudioManager? =
        context.applicationContext.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
}

/**
 * 一次播放的**实际路由结果**（`EchoTtsPlayer` 每次播完留给界面看的那一句）。
 *
 * 为什么要有它：偏好设过之后，用户在界面上**看不到结果** ——
 * 而"设了没生效"和"设了、系统没听"看起来一模一样。所以 [EchoTtsPlayer] 每次播放都把
 * `setPreferredDevice` 的真实返回记在 `EchoTtsPlayer.lastRoute` 里（**失败不算错误**：
 * 偏好而已，系统可以不听，下次播放会再试）。
 *
 * 当前 App 里那个**偏好的样子**（选了哪支、在不在）由 [OutputDevices.describe] 给设置页，
 * 这条记录是"上一次真播的时候系统怎么答的"—— 两者一起看才是全部事实。
 */
data class TtsRoute(
    /** 本次是否**选中了**某支偏好设备（false = 跟随系统）。 */
    val preferred: Boolean,
    /** 偏好设备的展示名；跟随系统时为空。 */
    val deviceLabel: String,
    /** `MediaPlayer.setPreferredDevice` 的真实返回 / 失败原因。 */
    val applied: Boolean,
    /** 说给用户听的**人话**（失败也照样有一句，不是空字符串）。 */
    val note: String,
)

/**
 * 一次播放的路由口径（**纯函数**，JVM 可测）。
 *
 * 两条边界都要能说出来，而且**不许说成"一定用它"**：
 *  * 没设/失败 → "跟随系统"+"为什么"；
 *  * 设上了 → "系统已把这个偏好收到"，并紧接着说明**最终路由仍由系统定**。
 */
internal fun routeOutcome(preferred: Boolean, label: String, applied: Boolean): TtsRoute = when {
    !preferred -> TtsRoute(
        preferred = false, deviceLabel = "", applied = false,
        note = "这次播的是系统默认输出（没选设备）",
    )
    applied -> TtsRoute(
        preferred = true, deviceLabel = label, applied = true,
        note = "已把你选的「$label」设为偏好；最终走哪支由系统定（蓝牙/听筒尤其常见）",
    )
    else -> TtsRoute(
        preferred = true, deviceLabel = label, applied = false,
        note = "系统没接受「$label」这个偏好，这次按系统默认播（不算错误，下次播放会再试）",
    )
}
