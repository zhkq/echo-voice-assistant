package com.echo.contact.service

import android.content.Context
import kotlin.math.abs

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
 * 目前几项：
 *  * [inputDeviceId]：麦克风用哪一支（`-1` = 跟随系统）。值来自 `AudioDeviceInfo.id` ——
 *    设备拔掉/换了之后那个 id 会失效，所以**用它的时候必须回落到系统默认**，
 *    并把"选的那支不在了"如实说出来（见 `InputDevices.preferredInput`）。
 *  * [outputDeviceId]：ECHO 念优先从哪支放（`-1` = 跟随系统）。**只有 ECHO 念这条路认它**
 *    （`MediaPlayer.setPreferredDevice`）；手机念走系统 TTS，**没有**指定设备的接口 ——
 *    这条限制写在设置页上，不只是写在这里（见 `OutputDevices`）。
 *  * [segmentSeconds]：「手机当麦克风」每段多长（15 / 30 / 60 / 300 / 600，默认 **600**）。
 *  * [mediaKeys]：耳机 / 车机媒体键要不要接管（默认**开**）。
 *  * [audioRetentionDays]：离线录音补传成功之后，本地那份副本还留几天
 *    （默认 **7 天**，`0` = **永不自动清理**）—— 见 [OfflineMeetingRetention]：
 *    "永不删"（上一版）会把手机塞满，而"传完就删"会把用户还没核对的音频删掉。
 *    这是**手机这一侧**的取舍（清理的是 app 私有目录），所以它该在这里，不在 ECHO 面板上。
 *
 * 用普通 prefs 而不是加密存储：这不是凭据，是偏好（与 `ListenPrefs` 同一个口径）。
 */
object AppPrefs {

    const val PREFS = "echo_app"
    const val KEY_INPUT_DEVICE = "input_device_id"
    const val KEY_OUTPUT_DEVICE = "output_device_id"
    const val KEY_SEGMENT_SECONDS = "meeting_segment_seconds"
    const val KEY_MEDIA_KEYS = "media_keys"
    const val KEY_EVENT_CURSOR = "event_cursor"
    const val KEY_AUDIO_RETENTION_DAYS = "offline_audio_retention_days"

    /** 哨兵值：跟随系统默认（`AudioDeviceInfo.id` 从 0 起，负数不可能是真设备）。 */
    const val FOLLOW_SYSTEM = -1

    /**
     * 「每段多久」的可选档（秒）。
     *
     * **默认 600 秒（10 分钟）**：用户 2026-10-18 点名"跟电脑一样 10 分钟一段就行"
     * —— 电脑侧的会议录音本来就是 10 分钟一段，手机跟它对齐，两边的段边界才有同一个含义
     * （这也是"段长与电脑一致"这条硬约束的落点）。其它档位保留：录短会、或网络很差时，
     * 用户可以改小，改完**下次开录生效**（见 [MeetingMicService]）。
     *
     * 代价要如实知道：段越长，一次失败要重传的那一段就越大（10 分钟 ≈ 19 MB 的 WAV，
     * FLAC 之后约一半）。中继单次上限 12 MB —— 走中继且退回 WAV 时可能超限，
     * 这一点没有藏起来（设置页与报告里都写着）。
     */
    val SEGMENT_SECONDS_CHOICES = intArrayOf(15, 30, 60, 300, 600)
    const val DEFAULT_SEGMENT_SECONDS = 600

    /** 纯判据：只有非负的 id 才算"指定了某支麦/某支输出"，其余一律"跟随系统"。 */
    fun normalizeDeviceId(raw: Int): Int = if (raw < 0) FOLLOW_SYSTEM else raw

    /**
     * 纯判据：把库里那个秒数**夹到最近的一档**（[SEGMENT_SECONDS_CHOICES] 里必须有它）。
     *
     * 为什么是"夹到最近档"而不是"夹到上下界"：这是个**单选**（界面上是几档按钮），
     * 库里要是躺着 45（手改过、或以后删了某一档），回落成 15 或 60 都行，
     * 但**绝不能**回落成 0/负数 —— 那会让一段都传不出去（每 0 秒切一段 = 空段刷屏）。
     * 距离平手时取**小的那一档**（`minByOrNull` 返回第一个最小值，而档位表是升序）。
     * 判据只在这一处。
     */
    fun normalizeSegmentSeconds(raw: Int): Int =
        SEGMENT_SECONDS_CHOICES.minByOrNull { abs(it - raw) } ?: DEFAULT_SEGMENT_SECONDS

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun inputDeviceId(context: Context): Int =
        normalizeDeviceId(prefs(context).getInt(KEY_INPUT_DEVICE, FOLLOW_SYSTEM))

    fun saveInputDeviceId(context: Context, id: Int) {
        prefs(context).edit().putInt(KEY_INPUT_DEVICE, normalizeDeviceId(id)).apply()
    }

    fun outputDeviceId(context: Context): Int =
        normalizeDeviceId(prefs(context).getInt(KEY_OUTPUT_DEVICE, FOLLOW_SYSTEM))

    fun saveOutputDeviceId(context: Context, id: Int) {
        prefs(context).edit().putInt(KEY_OUTPUT_DEVICE, normalizeDeviceId(id)).apply()
    }

    /** 「手机当麦克风」每段多少秒（读出来一定在 [SEGMENT_SECONDS_CHOICES] 里，见 [normalizeSegmentSeconds]）。 */
    fun segmentSeconds(context: Context): Int = normalizeSegmentSeconds(
        prefs(context).getInt(KEY_SEGMENT_SECONDS, DEFAULT_SEGMENT_SECONDS))

    fun saveSegmentSeconds(context: Context, seconds: Int) {
        prefs(context).edit().putInt(KEY_SEGMENT_SECONDS, normalizeSegmentSeconds(seconds)).apply()
    }

    fun mediaKeys(context: Context): Boolean = prefs(context).getBoolean(KEY_MEDIA_KEYS, true)

    /**
     * 离线录音**本地副本**的保留天数（读出来一定是 [OfflineMeetingRetention.RETENTION_DAY_CHOICES]
     * 里的一档；`0` = **永不自动清理**）。
     *
     * 为什么默认是 7 天：见 [OfflineMeetingRetention.DEFAULT_RETENTION_DAYS]。
     * 注意夹取**只在这里的前半段**（[OfflineMeetingRetention.normalizeDays]）——
     * 与 [segmentSeconds] 一样，"夹到哪一档"的判据只有一处，而且是纯函数、有用例。
     */
    fun audioRetentionDays(context: Context): Int = OfflineMeetingRetention.normalizeDays(
        prefs(context).getInt(KEY_AUDIO_RETENTION_DAYS,
            OfflineMeetingRetention.DEFAULT_RETENTION_DAYS))

    fun saveAudioRetentionDays(context: Context, days: Int) {
        prefs(context).edit()
            .putInt(KEY_AUDIO_RETENTION_DAYS, OfflineMeetingRetention.normalizeDays(days)).apply()
    }

    /**
     * 事件游标（`GET /api/events` 的 `since`）。
     *
     * `-1` = **还没订阅过** → 第一次只取游标、不推历史（新装的手机不该被旧事件淹一遍，
     * 也不该半夜收到一条"三天前的会议转写好了"）。
     */
    fun eventCursor(context: Context): Long = prefs(context).getLong(KEY_EVENT_CURSOR, -1L)

    fun saveEventCursor(context: Context, cursor: Long) {
        prefs(context).edit().putLong(KEY_EVENT_CURSOR, cursor).apply()
    }

    fun saveMediaKeys(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_MEDIA_KEYS, enabled).apply()
    }
}
