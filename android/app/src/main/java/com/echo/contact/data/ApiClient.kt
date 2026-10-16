package com.echo.contact.data

import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * 构建带 Bearer 令牌拦截器的 Retrofit 客户端。
 *
 * **按 baseUrl 缓存**：轮询指令结果时每 1~2 秒就要发一次请求，
 * 每次都新建 `OkHttpClient` 会连带新建连接池与线程池 —— 那是泄漏，不是小事。
 * 令牌是**每次请求现取**（拦截器里调 `tokenProvider`），所以缓存实例不会把令牌钉死，
 * 重新配对后换了令牌照样立刻生效。
 */
object ApiClient {

    /**
     * 默认后端地址：ECHO **面板/API 自己的端口**（`serverPort` 默认 8970，app/config.py:346）。
     *
     * 注意别和 8900/8901 混了 —— 那是「能力后端」（`server/`，转写/分离）的端口，不是这里要连的。
     * 回环默认值只对「USB 调试 + adb reverse tcp:8970 tcp:8970」成立；
     * 真机走 Wi-Fi 要填电脑的局域网 IP（配对页有提示）。
     */
    const val DEFAULT_BASE_URL = "http://127.0.0.1:8970"

    @Volatile private var tokenProvider: (() -> String?) = { null }

    private val cache = ConcurrentHashMap<String, EchoApi>()

    /** 默认读超时（秒）。绝大多数接口秒回，用它就够。 */
    const val DEFAULT_READ_TIMEOUT_S = 60

    /**
     * 长任务用的读超时（秒）。
     *
     * 什么时候需要：`POST /api/daily-review/submit` 是**阻塞**的 ——
     * 它把口述交给智能体整理、等回复才返回（`daily_review.py:393` 的
     * `wait_for_reply(timeout=timeout or reply_timeout_s())`）。
     *
     * 实测（2026-10-09 晚，看用户自己那两轮回顾落库的行）：
     * 一轮 **180.5 秒**成功、紧接着一轮 **180.9 秒**报 "DSH 在超时内没有回复"
     * —— 也就是说服务端那边的等待上限约 **180 秒**，而默认读超时只有 60 秒。
     * 不改的话手机会先放弃，而后端其实还在干、结果也已经落库
     * （表现就是"手机上失败了，但面板里能看到那次回顾"）。转写大音频同理。
     *
     * 取 330 = 比服务端的 180 秒再宽一截，保证"后端先放弃"而不是"客户端先放弃"。
     */
    const val LONG_READ_TIMEOUT_S = 330

    /** 设置令牌来源（PairStore 提供）。在调用任何接口前先设一次。 */
    fun attachTokenProvider(provider: () -> String?) {
        tokenProvider = provider
    }

    fun create(baseUrl: String, readTimeoutSeconds: Int = DEFAULT_READ_TIMEOUT_S): EchoApi {
        val normalized = normalizeBaseUrl(baseUrl)
        return cache.getOrPut("$normalized|$readTimeoutSeconds") {
            build(normalized, readTimeoutSeconds)
        }
    }

    /** 丢掉缓存的客户端（单测换端口、或想强制重建时用）。 */
    fun clearCache() {
        cache.clear()
    }

    private fun build(normalizedBaseUrl: String, readTimeoutSeconds: Int): EchoApi {
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }
        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(readTimeoutSeconds.toLong(), TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val tok = tokenProvider()
                val req = if (!tok.isNullOrBlank()) {
                    chain.request().newBuilder()
                        .header("Authorization", "Bearer $tok")
                        .build()
                } else {
                    chain.request()
                }
                chain.proceed(req)
            }
            .addInterceptor(logging)
            .build()

        return Retrofit.Builder()
            .baseUrl(normalizedBaseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(EchoApi::class.java)
    }

    /**
     * 把用户/后端给的地址统一成 Retrofit 要的「服务根 + 结尾斜杠」。
     *
     * 为什么容忍结尾的 `/api`：后端 `_phone_base_url()` 给的是服务根（不带 `/api`），
     * 但用户手动填的时候很容易顺手带上 `/api`。两种都收，避免拼成 `/api/api/...` 变 404。
     */
    fun normalizeBaseUrl(raw: String): String {
        var url = raw.trim().trimEnd('/')
        if (url.endsWith("/api")) url = url.removeSuffix("/api")
        return if (url.endsWith("/")) url else "$url/"
    }
}
