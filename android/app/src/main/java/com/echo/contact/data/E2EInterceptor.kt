package com.echo.contact.data

import android.content.Context
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import java.nio.charset.StandardCharsets
import java.util.Base64

/**
 * 方案 B：手机↔ECHO 的**常规 API** 全部封成 E2E 信封。
 *
 * 外层请求固定为 `POST <relay>/api/e2e`，body 是加密信封；后端 relay 只能看到
 * 这一条路径与密文。ECHO relay 解密后带手机 token 调本机 API，再把响应加密回来。
 * 配对接口（`api/pair/...`）不套信封：它们本身已有 ECHO 公钥加密的 claim。
 */
class E2EInterceptor(private val context: () -> Context?) : Interceptor {

    // 外层 envelope 的 iv/ciphertext 由 E2E.sealEnvelope 生成，是 URL-safe；
    // 内层 `body` 要交给 ECHO relay 的 Python base64.b64decode，必须用
    // **标准 Base64（带 padding）**，否则语音命令这种非空 body 会 Incorrect padding。
    private val b64Inner = Base64.getEncoder()
    private val b64InnerDec = Base64.getDecoder()

    override fun intercept(chain: Interceptor.Chain): Response {
        val ctx = context() ?: return chain.proceed(chain.request())
        if (!PairStore.e2eReady(ctx)) return chain.proceed(chain.request())
        val req = chain.request()
        val base = PairStore.baseUrl(ctx)?.trimEnd('/') ?: return chain.proceed(req)
        val baseUrl = req.url.resolve(base + "/") ?: return chain.proceed(req)
        val basePath = baseUrl.encodedPath.trimEnd('/') + "/"
        val fullPath = req.url.encodedPath
        if (!fullPath.startsWith(basePath)) return chain.proceed(req)
        val innerPath = fullPath.removePrefix(basePath)
        // 配对请求不套 E2E：它们已经在 claim 内用 ECHO 公钥加密。
        if (innerPath.isBlank() || innerPath.startsWith("api/pair/") ||
            innerPath == "api/e2e" || innerPath.startsWith("api/e2e/")
        ) return chain.proceed(req)

        val devicePrivate = PairStore.devicePrivate(ctx) ?: return chain.proceed(req)
        val echoPub = PairStore.echoPublicKey(ctx) ?: return chain.proceed(req)
        val deviceId = PairStore.deviceId(ctx) ?: return chain.proceed(req)
        val key = try {
            E2E.sessionKey(E2E.privateFromStorage(devicePrivate), echoPub, deviceId)
        } catch (e: Exception) {
            return chain.proceed(req)
        }

        val bodyBytes = req.body?.let { body ->
            Buffer().also { body.writeTo(it) }.readByteArray()
        } ?: ByteArray(0)
        val headers = JsonObject()
        req.header("Authorization")?.let { headers.addProperty("authorization", it) }
        req.body?.contentType()?.let { headers.addProperty("content-type", it.toString()) }
        val inner = JsonObject().apply {
            addProperty("id", java.util.UUID.randomUUID().toString())
            addProperty("ts", System.currentTimeMillis() / 1000)
            addProperty("method", req.method)
            addProperty("path", innerPath)
            addProperty("query", req.url.encodedQuery.orEmpty())
            add("headers", headers)
            addProperty("body", if (bodyBytes.isEmpty()) "" else b64Inner.encodeToString(bodyBytes))
        }
        val envelope = E2E.sealEnvelope(key, inner, deviceId)
        val outerUrl = baseUrl.newBuilder().addPathSegments("api/e2e").build()
        val outer = req.newBuilder()
            .url(outerUrl)
            .method("POST", envelope.toString()
                .toRequestBody("application/json".toMediaTypeOrNull()))
            .removeHeader("Authorization")
            .build()
        val resp = chain.proceed(outer)
        if (!resp.isSuccessful) {
            // 外层非 2xx（relay 400/502/404…）：把原始 body 转成可见错误，
            // 不能直接把已被读过的响应交回 Retrofit（会变成 IOException: closed）。
            val raw = resp.body?.bytes()
            val text = raw?.toString(Charsets.UTF_8).orEmpty().take(800)
            android.util.Log.e("EchoE2E", "outer HTTP ${resp.code}: $text")
            return resp.newBuilder()
                .code(resp.code)
                .body(errorBody("E2E 外层 HTTP ${resp.code}：$text"))
                .build()
        }
        return try {
            val raw = resp.body?.bytes() ?: ByteArray(0)
            val env = JsonParser.parseString(String(raw, StandardCharsets.UTF_8)).asJsonObject
            val result = E2E.openEnvelope(key, env)
            val status = result.get("status")?.asInt ?: 502
            val ctype = result.get("contentType")?.asString.orEmpty()
            val payload = b64InnerDec.decode(result.get("body")?.asString.orEmpty())
            resp.newBuilder()
                .code(status)
                .body(payload.toResponseBody(ctype.toMediaTypeOrNull()))
                .build()
        } catch (e: Exception) {
            // 这里最常见的是 AEADBadTagException（会话键不一致）或外层不是 E2E 信封。
            // 返回人话 + 502，让 UI 能显示真正原因，而不是 "closed"。
            val msg = "E2E 解密失败：${e.javaClass.simpleName}: ${e.message}"
            android.util.Log.e("EchoE2E", msg, e)
            resp.newBuilder()
                .code(502)
                .body(errorBody(msg))
                .build()
        }
    }

    private fun errorBody(text: String): okhttp3.ResponseBody =
        JsonObject().apply { addProperty("detail", text) }
            .toString().toResponseBody("application/json".toMediaTypeOrNull())
}
