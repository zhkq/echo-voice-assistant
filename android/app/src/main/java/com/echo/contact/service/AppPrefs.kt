package com.echo.contact.service

import android.content.Context

/**
 * **App 自己的选项**（不是 ECHO 的设置）。
 *
 * 用户 2026-10-15 定的口径：
 * *"app 是 echo 的前端触点……设置需要单独设计负责 app 自己的选项，权限等设置的地方，
 * 比如输入输出设备，媒体键或者手机按键等快捷方式"*。
 *
 * 所以这一份只管"影响**手机这一侧**"的东西；ECHO 侧的配置（转写引擎、能力后端、会议参数）
 * 一律留在电脑面板上，App 不重复实现（`SettingsScreen` 的注释里写着这条边界）。
 *
 * 目前两项：
 *  * [inputDeviceId]：麦克风用哪一支（`-1` = 跟随系统）。值来自 `AudioDeviceInfo.id` ——
 *    设备拔掉/换了之后那个 id 会失效，所以**用它的时候必须回落到系统默认**，
 *    并把"选的那支不在了"如实说出来（见 `InputDevices.preferredInput`）。
 *  * [mediaKeys]：耳机 / 车机媒体键要不要接管（默认**开**）。
 *
 * 用普通 prefs 而不是加密存储：这不是凭据，是偏好（与 `ListenPrefs` 同一个口径）。
 */
object AppPrefs {

    const val PREFS = "echo_app"
    const val KEY_INPUT_DEVICE = "input_device_id"
    const val KEY_MEDIA_KEYS = "media_keys"

    /** 哨兵值：跟随系统默认（`AudioDeviceInfo.id` 从 0 起，负数不可能是真设备）。 */
    const val FOLLOW_SYSTEM = -1

    /** 纯判据：只有非负的 id 才算"指定了某支麦"，其余一律"跟随系统"。 */
    fun normalizeDeviceId(raw: Int): Int = if (raw < 0) FOLLOW_SYSTEM else raw

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun inputDeviceId(context: Context): Int =
        normalizeDeviceId(prefs(context).getInt(KEY_INPUT_DEVICE, FOLLOW_SYSTEM))

    fun saveInputDeviceId(context: Context, id: Int) {
        prefs(context).edit().putInt(KEY_INPUT_DEVICE, normalizeDeviceId(id)).apply()
    }

    fun mediaKeys(context: Context): Boolean = prefs(context).getBoolean(KEY_MEDIA_KEYS, true)

    fun saveMediaKeys(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_MEDIA_KEYS, enabled).apply()
    }
}
