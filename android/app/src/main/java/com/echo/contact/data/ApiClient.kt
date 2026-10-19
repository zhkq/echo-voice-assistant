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
     * 默认**写**超时（秒）：普通接口的请求体都很小（几 KB 的 JSON），60 秒足够。
     *
     * ⚠️ 它**不是**传音频那一档该用的值 —— 见 [UPLOAD_TIMEOUT_S]。
     */
    const val DEFAULT_WRITE_TIMEOUT_S = 60

    /**
     * 长任务用的读超时（秒）。
     *
     * 什么时候需要：`POST /api/daily-review/submit` 是**阻塞**的 ——
     * 它把口述交给智能体整理、等回复才返回（`daily_review.py:393` 的
     * `wait_for_reply(timeout=timeout or reply_timeout_s())`）。
     *
     * 实测（2026-10-09 晚，看用户自己那两轮回顾落库的行）：
     * 一轮 **180.5 秒**成功、紧接着一轮 **180.9 秒**报 "DSH 在超时内没有回复"
     * —— 也就是说服务端那边的**提醒**上限约 **180 秒**，而默认读超时只有 60 秒。
     * 不改的话手机会先放弃，而后端其实还在干、结果也已经落库
     * （表现就是"手机上失败了，但面板里能看到那次回顾"）。转写大音频同理。
     *
     * ## 为什么是 960（**必须 > 服务端的硬上限**）
     *
     * 服务端一轮回顾**不是**在 180 秒就放弃：`app/daily_review.py` 的
     * `ROUND_HARD_MAX_S = 900`（15 分钟）才是真正的硬上限 —— 180 秒只是"多久没动静提醒一句"。
     * 所以客户端等待上限**必须比它更长**（960 = 900 + 60 秒余量），让"服务端先放弃、
     * 把一句人话回给手机"；客户端先放弃的话，手机会看到失败，而电脑还在整理、
     * 结果也已经落库 —— 这就是 330（5.5 分钟）那版的真实缺陷：超过 5.5 分钟的整理
     * 手机会先放弃，而电脑还在干。**改这个数时别改短服务端**（900 是它自己的预算）。
     */
    const val LONG_READ_TIMEOUT_S = 960

    /**
     * **传一段会议音频**用的超时（秒）—— 读与写**同一个值**，且与 [LONG_READ_TIMEOUT_S] 对齐。
     *
     * ## 为什么专门给"传段"一档（2026-10-19 真机：传完第 1 段就停住）
     *
     * 一段 10 分钟的 **WAV** 是 **≈18.3 MB**（手机 FLAC 编码失败会退回 WAV，真机就是这样），
     * 而且要走**中继**才到电脑。它原来和"查状态""建会"那些秒回接口**共用同一个客户端**：
     * 读 60 秒、写 **60** 秒（`build()` 里写死的）。写超时不是"整个请求体的总预算"，而是
     * **单次 socket 写操作**的上限 —— 也就是说"整整 60 秒一个字节都没写出去"就判死。
     * 在慢链路/中继上（中继往往先把请求体收下来再转发）这并不是异常现象，而是常态；
     * 一旦第 2 段被这样判死，这一轮就停在"第 1 段已传、第 2 段没传"上，
     * 而**下一轮又从同一个第 2 段重新发**（body 不能续传，见 `OfflineMeetingTransport`）：
     * 于是"大段永远传不完 → 循环永远推进不到 seq=3 → `stop` 永远不发"。
     *
     * 所以：**写要和读一样长**。这不是把超时"调大一点"，而是把同一个预算的两半对齐 ——
     * 读那头等了 16 分钟还不算失败，写这头 60 秒就放弃，那两个数字本来就不该是两套。
     */
    const val UPLOAD_TIMEOUT_S = LONG_READ_TIMEOUT_S

    /**
     * **一段**音频上传的**总预算**（秒）：从发出请求到拿到回包，超过就如实判超时。
     *
     * 为什么除了 [UPLOAD_TIMEOUT_S]（单次读写无进展的上限）还要一个总预算：
     * 单次超时只能抓"卡住"，抓不住"一直在动但永远传不完"（链路极慢时那会一直占着
     * 同步守卫，界面上永远"正在同步…"）。总预算给 20 分钟 = 18.3 MB 算下来
     * **最低约 15 KB/s 也能过**，比这更慢的链路如实说"这段太大/网太慢"，
     * 而不是无限期地假装还在传。
     */
    const val UPLOAD_CALL_TIMEOUT_S = 1_200

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

    /**
     * **传段专用**的客户端（唯一一处这么建）：读/写超时都取 [UPLOAD_TIMEOUT_S]，
     * 另外加一个总预算 [UPLOAD_CALL_TIMEOUT_S]。
     *
     * 为什么它必须是**另一个实例**（而不是把默认那个的超时改大）：默认客户端还服务
     * "查状态""建会""收尾"这些**必须快**的接口 —— 一个卡住的状态查询如果也有 16 分钟
     * 的耐心，整轮补传就会被它拖死（而用户看到的是"同步中"永远不动）。
     * 所以：**只有传段这一条路**用这个客户端（`OfflineMeetingTransport` 的 `uploadApi`）。
     *
     * 缓存键里带 `upload` 是刻意的：`create(baseUrl, UPLOAD_TIMEOUT_S)` 得到的是
     * **另一个**实例（写超时仍是 60、也没有总预算），两者不能混用。
     */
    fun createForUpload(baseUrl: String): EchoApi {
        val normalized = normalizeBaseUrl(baseUrl)
        return cache.getOrPut("$normalized|upload") {
            build(
                normalizedBaseUrl = normalized,
                readTimeoutSeconds = UPLOAD_TIMEOUT_S,
                writeTimeoutSeconds = UPLOAD_TIMEOUT_S,
                callTimeoutSeconds = UPLOAD_CALL_TIMEOUT_S,
            )
        }
    }

    /** 丢掉缓存的客户端（单测换端口、或想强制重建时用）。 */
    fun clearCache() {
        cache.clear()
    }

    private fun build(
        normalizedBaseUrl: String,
        readTimeoutSeconds: Int,
        /** 写超时（秒）；默认 [DEFAULT_WRITE_TIMEOUT_S]。传段那一档传 [UPLOAD_TIMEOUT_S]。 */
        writeTimeoutSeconds: Int = DEFAULT_WRITE_TIMEOUT_S,
        /** 整个调用的总预算（秒）；`0` = 不限（OkHttp 的默认值，其它接口保持原样）。 */
        callTimeoutSeconds: Int = 0,
    ): EchoApi {
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }
        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(readTimeoutSeconds.toLong(), TimeUnit.SECONDS)
            .writeTimeout(writeTimeoutSeconds.toLong(), TimeUnit.SECONDS)
            .apply {
                // 0 = 不限（OkHttp 默认）—— 只有传段那一档才给总值，别的接口行为一个字节都不变
                if (callTimeoutSeconds > 0) {
                    callTimeout(callTimeoutSeconds.toLong(), TimeUnit.SECONDS)
                }
            }
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
