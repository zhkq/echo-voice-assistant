package com.echo.contact.service

import android.content.Context

/**
 * 听写参数的落盘（`SharedPreferences`）。
 *
 * 只做"读进来 → [ListenTuning.clamped] → 用"和"存出去"两件事：
 * **边界一律交给 [ListenTuning.clamped]**，这里不自己判——
 * 两处各判一次必然会漂移（一边放行 0.5、另一边当合法值用，就是静默坏掉）。
 *
 * 用普通 prefs 而不是 `EncryptedSharedPreferences`（`PairStore` 那份）：这几个数是
 * 用户自己调的旋钮，不是凭据；用加密存储反而会在 Keystore 异常的设备上多一条失败路径。
 */
object ListenPrefs {

    private const val PREFS = "echo_listen_tuning"
    private const val KEY_THRESHOLD = "silence_threshold"
    private const val KEY_HANGOVER = "hangover_ms"
    private const val KEY_IDLE_MIN = "idle_auto_stop_min"
    private const val KEY_FULL_DUPLEX = "keep_listening_while_speaking"

    fun load(context: Context): ListenTuning {
        val p = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val d = ListenTuning()
        return ListenTuning(
            silenceThreshold = p.getString(KEY_THRESHOLD, null)?.toDoubleOrNull()
                ?: d.silenceThreshold,
            hangoverMs = p.getString(KEY_HANGOVER, null)?.toIntOrNull() ?: d.hangoverMs,
            idleAutoStopMin = p.getString(KEY_IDLE_MIN, null)?.toIntOrNull() ?: d.idleAutoStopMin,
            keepListeningWhileSpeaking = p.getBoolean(
                KEY_FULL_DUPLEX, d.keepListeningWhileSpeaking,
            ),
        ).clamped()
    }

    fun save(context: Context, tuning: ListenTuning) {
        val t = tuning.clamped()
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_THRESHOLD, t.silenceThreshold.toString())
            .putString(KEY_HANGOVER, t.hangoverMs.toString())
            .putString(KEY_IDLE_MIN, t.idleAutoStopMin.toString())
            .putBoolean(KEY_FULL_DUPLEX, t.keepListeningWhileSpeaking)
            .apply()
    }
}
