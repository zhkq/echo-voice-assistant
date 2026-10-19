package com.echo.contact.ui

import android.content.Context
import android.content.Intent
import android.net.Uri

/**
 * 「去设置：允许后台运行 / 关闭电池优化」那一跳（用户 2026-10-19 深夜点名要的入口）。
 *
 * ## 为什么补传这块也要给这个入口
 *
 * 补传已经尽量不依赖系统好心（前台服务 + `PARTIAL_WAKE_LOCK` + `WifiLock`，见
 * `SyncForeground`），但**国产系统**（以及 Android 的 Doze / App Standby）
 * 仍然可能把进程冻住 —— 那时连前台服务也保不住它。用户唯一能做的是把这个 App
 * 加进电池优化白名单，**所以他必须有一个能点的地方**，而不是听一句"请关闭电池优化"。
 *
 * ## 为什么"只在用户点的时候跳"
 *
 * `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` 会弹一个**系统确认框**。
 * 自动弹（例如一进会议页就弹）等于拿用户的注意力换一个"可能有用"的设置，
 * 而且很容易被当成恶意软件的行为。所以这里只有 [openBatteryOptimizations]
 * ——**由按钮的 onClick 调用**，别在任何 `LaunchedEffect` / `init` 里调它。
 *
 * ## 为什么拆成"纯数据 + 一跳"两半
 *
 * 动作字符串与 data URI 是**判据**（写错就跳不到那个页面，而且用户看不出是代码错了），
 * 而 `Intent` 是 Android 类、JVM 用例里碰不得（stub `android.jar` 一调就抛）。
 * 于是"要发什么"放进 [batterySettingsIntent]（纯函数，`SyncForegroundTest` 钉住），
 * "怎么发"只剩 [openBatteryOptimizations] 这一处。
 */

/**
 * 系统设置里那个"忽略电池优化"的动作。
 *
 * 值必须与 `android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` **逐字相同**
 * （用例钉住这个字面量）。为什么不用常量引用：那个常量是 Android 类，
 * 一引用这一份就不是纯逻辑了 —— 而这条路（这个 App 的后台能不能活）必须可测。
 * 配对权限在 `AndroidManifest.xml`：`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`（早就有了）。
 */
internal const val BATTERY_OPTIMIZATIONS_ACTION =
    "android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"

/** 「去设置」要发的 Intent 的**纯数据**版本。 */
internal data class BatterySettingsIntent(val action: String, val dataUri: String)

/**
 * 这个包名的"忽略电池优化"请求（纯函数）。
 *
 * data URI 的形状是 `package:<包名>` —— 与 `Uri.fromParts("package", pkg, null)` 逐字等价。
 */
internal fun batterySettingsIntent(packageName: String): BatterySettingsIntent =
    BatterySettingsIntent(BATTERY_OPTIMIZATIONS_ACTION, "package:$packageName")

/** 真的跳过去（**只由用户点按钮调用**；跳不过去就静默 —— 这只是个便利入口）。 */
internal fun openBatteryOptimizations(context: Context) {
    val spec = batterySettingsIntent(context.packageName)
    runCatching {
        context.startActivity(
            Intent(spec.action, Uri.parse(spec.dataUri))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}
