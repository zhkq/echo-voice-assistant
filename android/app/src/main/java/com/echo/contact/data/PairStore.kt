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

    fun baseUrl(context: Context): String? = prefs(context)?.getString(KEY_BASE_URL, null)

    fun token(context: Context): String? = prefs(context)?.getString(KEY_TOKEN, null)

    fun name(context: Context): String? = prefs(context)?.getString(KEY_NAME, "mobile")

    fun clear(context: Context) {
        prefs(context)?.edit()?.clear()?.apply()
    }
}
