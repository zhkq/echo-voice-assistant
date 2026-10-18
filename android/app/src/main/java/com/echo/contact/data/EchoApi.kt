package com.echo.contact.data

import com.echo.contact.data.dto.CommandListResponse
import com.echo.contact.data.dto.CommandRequest
import com.echo.contact.data.dto.CommandResponse
import com.echo.contact.data.dto.CommandSessionResponse
import com.echo.contact.data.dto.DailyReviewStartResponse
import com.echo.contact.data.dto.DailyReviewState
import com.echo.contact.data.dto.DailyReviewSubmitRequest
import com.echo.contact.data.dto.DailyReviewSubmitResponse
import com.echo.contact.data.dto.DailyReviewSummary
import com.echo.contact.data.dto.EventFeed
import com.echo.contact.data.dto.MeetingDetail
import com.echo.contact.data.dto.MeetingListResponse
import com.echo.contact.data.dto.MeetingPausesRequest
import com.echo.contact.data.dto.MeetingSegmentResponse
import com.echo.contact.data.dto.MeetingStartPhoneResponse
import com.echo.contact.data.dto.MeetingStatus
import com.echo.contact.data.dto.PairClaimRequest
import com.echo.contact.data.dto.PairClaimResponse
import com.echo.contact.data.dto.ReviewHistoryDetail
import com.echo.contact.data.dto.ReviewHistoryList
import com.echo.contact.data.dto.StatusResponse
import com.echo.contact.data.dto.SttTranscribeResponse
import com.echo.contact.data.dto.TtsRequest
import com.echo.contact.data.dto.TranscribeProgress
import com.echo.contact.data.dto.VoiceCommandResponse
import okhttp3.MultipartBody
import okhttp3.RequestBody
import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Multipart
import retrofit2.http.POST
import retrofit2.http.Part
import retrofit2.http.Path
import retrofit2.http.Query
import retrofit2.http.QueryMap
import retrofit2.http.Streaming

/**
 * 与 ECHO 后端交互的接口定义。
 *
 * **路径全都带 `api/` 前缀**：后端是 `router = APIRouter(prefix="/api")`（app/api.py:28），
 * 而配对回包里的 `baseUrl` 来自 `_phone_base_url()`（app/api.py:2462），
 * 它给的是**服务根**（形如 `http://192.168.1.5:8970`，**不带 `/api`**）。
 * 所以这里必须自己带前缀，才能拼出 `/api/...`。
 */
interface EchoApi {

    /** 用一次性配对码换令牌。这是**唯一**不带令牌的接口。 */
    @POST("api/pair/phone/claim")
    suspend fun pairClaim(@Body body: PairClaimRequest): Response<PairClaimResponse>

    /** 发一条文字指令给智能体。 */
    @POST("api/assistant/command")
    suspend fun sendCommand(@Body body: CommandRequest): Response<CommandResponse>

    /**
     * **语音一步到位**：裸 wav 字节当 body（`Content-Type: audio/wav`）→ 服务端转写 + 下发，
     * 回 `{ok, message, text, commandId, source}`。
     *
     * 注意这条是**裸 body**，不是 multipart（发成 multipart 会 422，并且后端会明说别用
     * multipart）。这跟 `/api/stt/transcribe` 正好相反 —— 那条才是 multipart。
     */
    @POST("api/assistant/voice-command")
    suspend fun voiceCommand(@Body body: RequestBody): Response<VoiceCommandResponse>

    /**
     * 拉指令历史（取最近若干条，含状态）。后端 `limit` 上限 500。
     *
     * 用 `@QueryMap` 而不是逐个 `@Query`：这样**不带的参数就不出现在 URL 里**
     * （`offset=0&q=` 这种空参数看着无害，但会让"路径长什么样"这件事变得难断言）。
     * 可选键：`limit` / `offset` / `q` / `since`。
     */
    @GET("api/commands")
    suspend fun listCommands(@QueryMap params: Map<String, String>): Response<CommandListResponse>

    /** 某条指令落到的 DSH 会话内容。 */
    @GET("api/commands/{cmdId}/session")
    suspend fun commandSession(@Path("cmdId") cmdId: Int): Response<CommandSessionResponse>

    /** TTS：合成一句话 → 音频字节（Content-Type 可能是 audio/mpeg 或 audio/wav）。 */
    @POST("api/tts")
    suspend fun tts(@Body body: TtsRequest): Response<ResponseBody>

    /** 仪表盘数据：整机状态（组件/智能体/会议/忙不忙/版本/运行时长）。 */
    @GET("api/status")
    suspend fun status(): Response<StatusResponse>

    /**
     * **探活用**：`GET /health` 是唯一**不带 `/api` 前缀、也不要令牌**的东西
     * （main.py:179）。用它区分"连不上"与"令牌不对" ——
     * 拿 `/api/status` 探活的话，令牌坏掉会得到 401，很容易被误判成"服务没起来"。
     */
    @GET("health")
    suspend fun health(): Response<ResponseBody>

    // ---- 会议（手机发起 = **手机录**；ECHO 只负责落盘/转写/存储）----
    //
    // 用户 2026-10-17 真机 bug 之后的口径：手机上按「开始录音」录的必须是**这台手机**的麦克风。
    // 所以这个接口面里**没有**"让电脑开麦"那条路（旧的 `meetingStart()` 已删）：
    // 采集源由 `source=phone` 明说，音频由 `meetingSegmentWithId` 逐段传上来。
    //
    // 注意：**不提供删除** —— `app/api.py` 的 `_NETWORK_DENIED` 把
    // `DELETE /api/meetings/{id}` 对网上来的调用一律 403。

    @GET("api/meetings")
    suspend fun meetings(@Query("limit") limit: Int = 20): Response<MeetingListResponse>

    /**
     * 单场会议详情（含**转写正文** `lines[]` 与说话人 `speakers[]`）。
     * ⚠️ 字段名与列表那份**故意不一致**（`hasSummary` vs `has_summary`、
     * `speakers` 对象数组 vs `speakerNames` 字符串数组），所以 DTO 分成两份。
     */
    @GET("api/meetings/{mid}")
    suspend fun meetingDetail(@Path("mid") mid: Int): Response<MeetingDetail>

    /**
     * 会议产出的文件：`kind` = `transcript` | `topics` | `summary`。
     * 返回的是文件内容（文本），手机上直接当正文显示。
     *
     * 后端对 `kind` 有**路径安全**兜底（`realpath` 必须仍在会议目录内），
     * 所以我们只传这三个固定值，不从别处拼。
     */
    @GET("api/meetings/{mid}/file")
    suspend fun meetingFile(
        @Path("mid") mid: Int,
        @Query("kind") kind: String,
    ): Response<ResponseBody>

    /**
     * 会议录音的**第 seg 段**音频（1 起，`api.py:1637`）：裸 wav，支持 Range。
     *
     * 主机上按段存的是 flac，后端会**用时解码**成 wav 再回 —— 所以这里不当作"小文件"：
     * 用 [com.echo.contact.data.MeetingAudioFetcher] 下到缓存再播（顺带避免重复下载）。
     */
    @Streaming
    @GET("api/meetings/{mid}/audio")
    suspend fun meetingAudio(
        @Path("mid") mid: Int,
        @Query("seg") seg: Int,
    ): Response<ResponseBody>

    // ---- 建会：**只有一条路**（手机发起 = 手机录），别再把它拆成两条 ----
    //
    // 2026-10-17 真机 bug 的客户端半边就出在旧的 `meetingStart()`：它**一个 query 都不带**
    // （`POST /api/meeting/start` 裸路径），服务端于是按"电脑那支麦"开了一场会 —— 手机上点
    // 「开始录音」，录的却是电脑的麦克风（转写里混进了电脑那边的人声）。那条无参数的重载
    // **已经删掉**（不是留着不用）：类型里没有它，就没有地方能再把"遥控电脑开麦"接回来。
    //
    // 现在建会只走这一条：`source=phone`（采集源 = 这台手机）+ `clientId`（幂等键）
    // + `startedAt`（这一场**真正的**开始时间，来自本地录音清单）。
    // 为什么不能只改服务端：请求里根本没告诉它"这是手机录的"—— 服务端没有信息可分，
    // 只能按默认的电脑麦开；客户端不发 source，服务端再聪明也猜不出来。

    /**
     * 建会（手机麦那条唯一的路）：把**采集源**、**幂等键**与**开始时间**一起交上去。
     *
     * 参数来自**本地录音清单**（`MeetingDraft`：由 `MeetingRecordSession` 在开录那一刻写下来）——
     * 于是"这场会是手机在录"不是一句注释，而是请求里真有 `source=phone`、身份真有出处。
     * 服务端支持这些 query 时按 `clientId` 幂等并采用 `startedAt`（**对**的时间轴）；
     * 老服务端不认识时参数被忽略（功能不坏，只是时间轴变成补传那一刻）—— 补传那条路
     * **必须**容忍这个降级并如实说出来（见 `OfflineMeetingSync` 的对账说明）。
     *
     * ⚠️ 这条**不带请求体**：后端 `POST /api/meeting/start` 的签名里只有 `_auth`
     * （`app/api.py`），给它一个 JSON body 只会白白多一份可能不被解析的东西。
     * 于是"采集源 / 客户端 id / 开始时间"只能走 query。
     */
    @POST("api/meeting/start")
    suspend fun meetingStartPhoneWithId(
        // 默认值写死 `"phone"`（与 `MeetingSource.PHONE` 同一个值）：data 层不反向依赖
        // service 层，而"默认必须是 phone"这条由 `MeetingPhoneStartTest` 的请求断言钉住。
        @Query("source") source: String = "phone",
        @Query("clientId") clientId: String,
        @Query("startedAt") startedAt: String,
        @Query("endedAt") endedAt: String = "",
    ): Response<MeetingStartPhoneResponse>

    /** 上传**一段**会议音频（裸 wav 字节）—— "手机当麦克风"那条路。 */
    @POST("api/meeting/segment")
    suspend fun meetingSegment(@Body body: RequestBody): Response<CommandResponse>

    /**
     * 上传一段，并带上**这一段的身份**：客户端 id + 段序号（+ 开始时间）。
     *
     * 服务端要能做两件事（否则离线补传会静默走样）：
     *  1. 按 `(clientId, seq)` **幂等** —— 重试同一段不会在电脑上存成两段；
     *  2. 回一个"我手上有几段"（[MeetingSegmentResponse.effectiveCount]）——
     *     **对账**靠的就是它（对不过就不许说"已上传"，更不许删本地音频）。
     *
     * 与 [meetingSegment] 一样是**裸 wav body**（别发 multipart：那条接口不收）。
     */
    @POST("api/meeting/segment")
    suspend fun meetingSegmentWithId(
        @Query("clientId") clientId: String,
        @Query("seq") seq: Int,
        @Query("startedAt") startedAt: String = "",
        @Query("source") source: String = "phone",
        @Body body: RequestBody,
    ): Response<MeetingSegmentResponse>

    /**
     * 停止录音（**收尾即转写**：紧接着就会按既有流程发起转写）。
     *
     * `clientId`（可选 query，2026-10-16）是**防误停别人会议**的护栏，服务端那侧已实现：
     *  * 带了它 → 只有"当前正在录的那一场"的 `clientId` 与它相等才收尾；
     *    不相等就**拒绝并且一个字节都不动**（别把电脑正在录的那场会停掉 —— 音频当场就断了）；
     *  * 不传它（[clientId] 为空/缺省）→ 行为与以前**逐字节相同**
     *    （面板按钮、`meeting-record` 技能、暂停恢复那条路都不传）。
     *
     * ⚠️ 于是**离线补传那条路必须带上自己那一场的 id**：不带的话这条护栏在真机流程里
     * 永远不会生效 —— 一个错序/重试的补传就能把别人正在录的会停掉。
     * 具体发送与判据见 `OfflineMeetingTransport.stopMeeting()`。
     *
     * ⚠️ [clientId] **可空**（缺省 `null` = **一个 query 都不发**）：Retrofit 对
     * `@Query` 的**空串会编码成 `?clientId=`**（实测：`assertEquals("/api/meeting/stop", path)`
     * 会红）—— 那既不等于"带上了 id"，也会让"面板/技能那条老路"的请求形状悄悄变样。
     * 所以"没有 id"这一档用 `null` 表达，"有 id"才传字符串。
     *
     * 响应恒 HTTP 200，成没成看 `{ok, message}`；而"clientId 不符"的拒绝话术
     * **不能**被当成"这场早就停过了"（见 `looksClientIdMismatch`）。
     */
    @POST("api/meeting/stop")
    suspend fun meetingStop(
        @Query("clientId") clientId: String? = null,
    ): Response<CommandResponse>

    /** 录音进行中的状态：`active/level/paused/segments/startedAt/error`（全屏录音界面用）。 */
    @GET("api/meeting/status")
    suspend fun meetingStatus(): Response<MeetingStatus>

    /**
     * **同一个路径**的裸 JSON 版本 —— 离线补传读那几个服务端还没承诺的字段用
     * （`clientId` / `meetingId` / `source`，见 `MeetingStatusExtra`）。
     *
     * 为什么不直接给 [MeetingStatus] 加字段：那份 DTO 是面板与全屏录音界面共用的既有契约，
     * 而这里要读的东西服务端**还没有**（另一条线在重建后端镜像）。走 DTO 的话它们会被
     * Retrofit 的转换器静默丢掉（DTO 里没有对应属性），于是"这一场是不是我自己那一份"
     * 永远判不出来 —— 而判错的后果是**补传自己把自己挡住**（一直"等这场录完再传"）。
     * 两条路解析的是**同一份 JSON**，口径没有分叉。
     */
    @GET("api/meeting/status")
    suspend fun meetingStatusJson(): Response<ResponseBody>

    /** 静音（暂停录音：不写盘、不产生音频；暂停区间会记进那场会议的 meta）。 */
    @POST("api/meeting/pause")
    suspend fun meetingPause(): Response<CommandResponse>

    /**
     * **上报暂停区间**（用户 2026-10-17 的第③件事；服务端由另一条线同时实现）。
     *
     * 与 [meetingPause] 的区别很重要，别混：
     *  * [meetingPause] 是"**让电脑那支麦暂停**"（电脑在录的那条路，一个动作）；
     *  * 这一条是"**把手机上这段空白告诉服务端**"—— 手机自己静音时电脑根本不知道
     *    （它不在场），不报的话会议详情里那段时间就是一段空白，而不是"被暂停过"。
     *
     * 契约：JSON body `{"clientId": "...", "pauses": [{"from": .., "to": ..}]}`，
     * 相对这一场开始时间的秒数；按 `clientId` 幂等（服务端合并去重）。
     *
     * ⚠️ **老服务端没有这个端点**（回 404/405）。调用方（`OfflineMeetingTransport.reportPauses`）
     * 必须把它当成"这次没报上去"而**不是**补传失败 —— 一个可有可无的上报绝不许拖垮音频补传。
     */
    @POST("api/meeting/pauses")
    suspend fun meetingPauses(@Body body: MeetingPausesRequest): Response<CommandResponse>

    /** 恢复录音（接着写新的段）。 */
    @POST("api/meeting/resume")
    suspend fun meetingResume(): Response<CommandResponse>

    /** 转写进度：**键**可能是会议 id（数字字符串）也可能是会议 name（导入录音那条路）。 */
    @GET("api/transcribe/status")
    suspend fun transcribeStatus(): Response<Map<String, TranscribeProgress>>

    /**
     * 事件流：`since < 0` 只取游标；`since >= 0` 取新事件；`wait` 秒内没新事件就等着。
     *
     * 手机侧两种用法：App 在跑 → `wait=0` 定期问；常驻听写在听 → `wait≈10` 挂着等。
     */
    @GET("api/events/feed")
    suspend fun events(
        @Query("since") since: Long,
        @Query("wait") wait: Double = 0.0,
        @Query("limit") limit: Int = 50,
    ): Response<EventFeed>

    // ---- 每日回顾（手机只做"听播报"，不做口述 —— 口述要占主机的麦）----

    @GET("api/daily-review")
    suspend fun dailyReview(): Response<DailyReviewState>

    @GET("api/daily-review/summary")
    suspend fun dailyReviewSummary(): Response<DailyReviewSummary>

    @GET("api/daily-review/history")
    suspend fun dailyReviewHistory(@Query("limit") limit: Int = 60): Response<ReviewHistoryList>

    @GET("api/daily-review/history/{date}")
    suspend fun dailyReviewDay(@Path("date") date: String): Response<ReviewHistoryDetail>

    /**
     * **开始一场回顾**（与 PC 版面板那颗按钮同一个接口）：
     * 建/取回**今天**那条回顾会话（幂等），**不碰麦克风**。
     *
     * ⚠️ **这条不带请求体**：后端签名里 `body` 是可选的（`app/api.py` 的
     * `post_daily_review_start`），`force_new` 只有"当天也重建"时才需要 —— 手机这条路
     * 不加 body，也就不会因为多带一个字段被老服务端按 422 拒掉。
     *
     * ⛔ **绝对不要换成 `/api/daily-review/go`**：那条会 `assistant.start_review()`，
     * 真的开**电脑那支麦**。手机回顾要的是"开始之后用手机的麦一轮轮说"，
     * 不是"遥控电脑开麦"（判据：`ReviewConversationTest` 的源码断言）。
     */
    @POST("api/daily-review/start")
    suspend fun dailyReviewStart(): Response<DailyReviewStartResponse>

    /** 停掉正在进行的**主机麦克风**那条语音回顾（`assistant.stop_review()`）。 */
    @POST("api/daily-review/stop")
    suspend fun dailyReviewStop(): Response<Map<String, Any>>

    /**
     * **把口述文本交给回顾**（app/api.py:867）—— 手机做回顾靠的就是它。
     *
     * ⚠️ 这条是**阻塞**的：服务端要把口述交给智能体整理、等回复才返回
     * （`daily_review.py:393`），历史上一次实测 **122 秒**。
     * 所以调用方**必须**用 [com.echo.contact.data.ApiClient.LONG_READ_TIMEOUT_S] 那个客户端。
     *
     * ⚠️ 业务性失败（没配好 / 没等到回复）返回的是 **HTTP 200 + `ok=false` + `error`**，
     * 不是 4xx —— 所以不能只看状态码。
     */
    @POST("api/daily-review/submit")
    suspend fun dailyReviewSubmit(@Body body: DailyReviewSubmitRequest): Response<DailyReviewSubmitResponse>

    /**
     * 上传音频换文本（**multipart**，与 voice-command 的裸 body 正好相反）。
     *
     * 用在哪：手机录一段**长口述**（回顾）→ 先转写拿文本 → 再交给 `submit`。
     * `engine` 等参数不传就用后端默认（`sensevoice`）。
     */
    @Multipart
    @POST("api/stt/transcribe")
    suspend fun transcribe(@Part file: MultipartBody.Part): Response<SttTranscribeResponse>
}
