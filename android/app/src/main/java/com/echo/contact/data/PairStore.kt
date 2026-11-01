package com.echo.contact.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.security.GeneralSecurityException

/**
 * 配对凭据的安全存储。
 * 令牌明文只在创建时出现一次，这里用 Android Keystore + AES 加密存（EncryptedSharedPreferences）。
 */
object PairStore {

    private const val PREFS = "echo_pair"
    private const val KEY_BASE_URL = "base_url"
    private const val KEY_TOKEN = "token"
    private const val KEY_NAME = "name"
    // 方案 B：设备 P-256 私钥（PKCS#8）、ECHO 公钥、deviceId、后端 TTS 凭据。
    private const val KEY_DEVICE_PRIVATE = "e2e_device_private"
    private const val KEY_DEVICE_PUBLIC = "e2e_device_public"
    private const val KEY_ECHO_PUBLIC = "e2e_echo_public"
    private const val KEY_DEVICE_ID = "e2e_device_id"
    private const val KEY_TTS_URL = "e2e_tts_url"
    private const val KEY_TTS_TOKEN = "e2e_tts_token"
    private const val KEY_TTS_PIN = "e2e_tts_tls_pin"

    @Volatile private var prefs: SharedPreferences? = null

    private fun prefs(context: Context): SharedPreferences? {
        prefs?.let { return it }
        synchronized(this) {
            prefs?.let { return it }
            return try {
                val masterKey = MasterKey.Builder(context.applicationContext)
                    .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                    .build()
                EncryptedSharedPreferences.create(
                    context.applicationContext,
                    PREFS,
                    masterKey,
                    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
                ).also { prefs = it }
            } catch (e: GeneralSecurityException) {
                // 极少数设备 Keystore 异常：退回普通 prefs，但至少保证可用（凭据本身低敏场景）
                context.applicationContext
                    .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .also { prefs = it }
            } catch (e: RuntimeException) {
                context.applicationContext
                    .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .also { prefs = it }
            }
        }
    }

    fun isPaired(context: Context): Boolean =
        !prefs(context)?.getString(KEY_TOKEN, "").isNullOrBlank()

    fun save(context: Context, baseUrl: String, token: String, name: String) {
        prefs(context)?.edit()
            ?.putString(KEY_BASE_URL, baseUrl.trim().trimEnd('/'))
            ?.putString(KEY_TOKEN, token)
            ?.putString(KEY_NAME, name)
            ?.apply()
    }

    /** 方案 B：保存 ECHO 身份公钥、设备密钥和后端 TTS 委托凭据。 */
    fun saveE2E(
        context: Context,
        baseUrl: String,
        token: String,
        name: String,
        echoPublicKey: String,
        deviceId: String,
        devicePrivateKey: String,
        devicePublicKey: String,
        ttsUrl: String = "",
        ttsToken: String = "",
        ttsTlsPin: String = "",
    ) {
        prefs(context)?.edit()
            ?.putString(KEY_BASE_URL, baseUrl.trim().trimEnd('/'))
            ?.putString(KEY_TOKEN, token)
            ?.putString(KEY_NAME, name)
            ?.putString(KEY_ECHO_PUBLIC, echoPublicKey)
            ?.putString(KEY_DEVICE_ID, deviceId)
            ?.putString(KEY_DEVICE_PRIVATE, devicePrivateKey)
            ?.putString(KEY_DEVICE_PUBLIC, devicePublicKey)
            ?.putString(KEY_TTS_URL, ttsUrl)
            ?.putString(KEY_TTS_TOKEN, ttsToken)
            ?.putString(KEY_TTS_PIN, ttsTlsPin)
            ?.apply()
    }

    fun echoPublicKey(context: Context): String? = prefs(context)?.getString(KEY_ECHO_PUBLIC, null)
    fun deviceId(context: Context): String? = prefs(context)?.getString(KEY_DEVICE_ID, null)
    fun devicePrivate(context: Context): String? = prefs(context)?.getString(KEY_DEVICE_PRIVATE, null)
    fun devicePublic(context: Context): String? = prefs(context)?.getString(KEY_DEVICE_PUBLIC, null)
    fun ttsUrl(context: Context): String? = prefs(context)?.getString(KEY_TTS_URL, null)
    fun ttsToken(context: Context): String? = prefs(context)?.getString(KEY_TTS_TOKEN, null)
    fun ttsTlsPin(context: Context): String? = prefs(context)?.getString(KEY_TTS_PIN, null)

    /** 方案 B 的密钥材料齐了吗（旧配对没有这些字段）。 */
    fun e2eReady(context: Context): Boolean =
        !echoPublicKey(context).isNullOrBlank() && !devicePrivate(context).isNullOrBlank() &&
            !deviceId(context).isNullOrBlank()

    fun baseUrl(context: Context): String? = prefs(context)?.getString(KEY_BASE_URL, null)

    fun token(context: Context): String? = prefs(context)?.getString(KEY_TOKEN, null)

    fun name(context: Context): String? = prefs(context)?.getString(KEY_NAME, "mobile")

    fun clear(context: Context) {
        prefs(context)?.edit()?.clear()?.apply()
    }
}
