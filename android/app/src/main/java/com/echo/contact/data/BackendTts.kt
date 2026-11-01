package com.echo.contact.data

import android.content.Context
import com.google.gson.JsonObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.CertificatePinner
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * 手机直连能力后端 `/v1/tts`（方案 B）。
 *
 * - token 是 ECHO 通过 `/v1/delegate` 换来的 `sub=phone:<id>` 委托 token；
 * - 每次请求带 DPoP proof，relay/后端看到 token 也不能脱离设备私钥重放；
 * - 后端按 phone 的 `sub` 单独做 1 QPS，和 ECHO 的 client 分开；
 * - 后端 TLS 指纹来自 ECHO 配对响应，能校验就固定校验。
 *
 * 失败返回 null，让调用方回退 ECHO `/api/tts` 或手机系统 TTS。
 */
object BackendTts {
    private val jsonType = "application/json; charset=utf-8".toMediaType()

    data class Audio(val mime: String, val bytes: ByteArray)

    fun available(context: Context): Boolean =
        !PairStore.ttsUrl(context).isNullOrBlank() &&
            !PairStore.ttsToken(context).isNullOrBlank() &&
            PairStore.e2eReady(context)

    suspend fun synthesize(context: Context, text: String): Audio? = withContext(Dispatchers.IO) {
        if (!available(context)) return@withContext null
        try {
            val token = PairStore.ttsToken(context) ?: return@withContext null
            val base = PairStore.ttsUrl(context)!!.trimEnd('/')
            val device = PairStore.devicePrivate(context) ?: return@withContext null
            val privateKey = E2E.privateFromStorage(device)
            val devicePublic = PairStore.devicePublic(context) ?: return@withContext null
            val proof = E2E.dpopProof(
                java.security.KeyPair(E2E.publicFromRaw(E2E.unb64u(devicePublic)), privateKey),
                token,
            )
            val body = JsonObject().apply {
                addProperty("text", text)
                addProperty("engine", "")
            }.toString().toRequestBody(jsonType)
            val builder = OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(30, TimeUnit.SECONDS)
            val pin = PairStore.ttsTlsPin(context).orEmpty()
            if (pin.startsWith("sha256/")) {
                try {
                    val host = java.net.URI(base).host ?: ""
                    if (host.isNotBlank()) {
                        builder.certificatePinner(
                            CertificatePinner.Builder().add(host, pin).build())
                    }
                } catch (_: Exception) { /* pin 只是加强，不是播放前提 */ }
            }
            val req = Request.Builder()
                .url(base + "/v1/tts")
                .header("Authorization", "Bearer $token")
                .header("DPoP", proof)
                .post(body)
                .build()
            builder.build().newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return@withContext null
                val bytes = resp.body?.bytes() ?: return@withContext null
                if (bytes.isEmpty()) return@withContext null
                Audio(resp.header("Content-Type") ?: "audio/mpeg", bytes)
            }
        } catch (_: Exception) {
            null
        }
    }
}
