package com.echo.contact.service

import android.content.Context

/**
 * 「谁念回复」的落盘（`SharedPreferences`）。
 *
 * 正式版把那个"朗读测试"小框收掉了（用户 2026-10-15：*"那些测试的东西可以清掉或者藏起来"*），
 * 但**选择本身要留着**——所以把它变成一个设置项，并且**所有自动念的地方都读它**
 * （指令回复、回顾播报、会议纪要播报）。
 *
 * 为什么默认是**手机念**：
 *  * 它是"手机收"这条链路的本分（回一声不该依赖网络往返）；
 *  * ECHO 念要好听些，但必须有网（`POST /api/tts` 拿字节再播）。
 * 两条路都在，用户自己在设置里挑。
 *
 * 用普通 prefs 而不是加密存储：这不是凭据，是偏好；加密存储反而多一条 Keystore 失败路径
 * （与 `ListenPrefs` 同一个口径）。
 */
object SpeakPrefs {

    const val PREFS = "echo_speak"
    const val KEY_ENGINE = "engine"

    /** 手机自己的 TTS。 */
    const val ENGINE_SYSTEM = "system"

    /** ECHO 那边的 `POST /api/tts`（音色一致，但要有网）。 */
    const val ENGINE_ECHO = "echo"

    /** 认不出来的值一律回**默认**，不抛也不静默变成空 —— 判定只在这一处。 */
    fun normalize(raw: String?): String =
        if (raw == ENGINE_ECHO) ENGINE_ECHO else ENGINE_SYSTEM

    fun load(context: Context): String = normalize(
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_ENGINE, null)
    )

    fun save(context: Context, engine: String) {
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_ENGINE, normalize(engine))
            .apply()
    }

    /** 设置页上那句中文（和 [normalize] 一样只有一处判据）。 */
    fun label(engine: String?): String =
        if (normalize(engine) == ENGINE_ECHO) "ECHO 念" else "手机念"
}
