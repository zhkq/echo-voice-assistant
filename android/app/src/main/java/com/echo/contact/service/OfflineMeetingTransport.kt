package com.echo.contact.service

import com.echo.contact.data.ApiClient
import com.echo.contact.data.EchoApi
import com.echo.contact.data.dto.MeetingPausesRequest
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * [OfflineTransport] 的**真**实现：把补传那几个动作接到 `EchoApi` 上。
 *
 * 这一层刻意**薄**（每个方法就是"发一次请求 + 把回包翻成端口语义"），
 * 所有判定（顺序、续传、退避、对账、不抢占）都在 `OfflineMeetingSync` 那边，
 * 于是它们能在 JVM 里验。这里只负责两件容易错的事：
 *  1. **成没成**：后端这两条接口恒回 HTTP 200，真正的结论在 `{ok, message}` 里
 *     （与既有的 `meetingStartPhone` 同一个坑，见 `EchoApi` 的注释）；
 *  2. **服务端认下几段**：见 [segmentCount] —— 字段名有好几种可能，逐个认。
 *
 * ⚠️ 这一版**不许改服务端**（另一条线在重建后端镜像），所以这里对老服务端的降级是
 * 刻意支持的：老服务端会忽略 `clientId`/`seq`/`startedAt`、也不会回段数 ——
 * 那时对账**不做假设**（[CountOutcome.Failed]），把"没法对账"如实报出来。
 *
 * ## 为什么构造时要**两个** EchoApi（2026-10-19 真机：传完第 1 段就停住）
 *
 * [api] 是普通客户端（读 60 秒），用它跑"查状态/建会/收尾"这些**必须快**的接口；
 * [uploadApi] 是 `ApiClient.createForUpload()` 建的那个（读/写都 960 秒 + 单段总预算），
 * **只**用来传段。一段 10 分钟的 WAV 是 ≈18.3 MB、还要过中继 —— 用 60 秒写超时的客户端传它，
 * 第 2 段就会被"60 秒没有进展"判死，而下一轮又是从这一段的第 0 个字节重发，
 * 于是**大段永远传不完，`seq` 永远推进不到 3，`stop` 永远不发**（用户看到的就是
 * "传了一段，后面好像还是不行"）。
 *
 * [uploadApi] 默认等于 [api]：用例里注入同一个假/HTTP 客户端时行为完全不变。
 */
class OfflineMeetingTransport(
    private val api: EchoApi,
    /**
     * 传段专用的客户端（见类注释）。默认与 [api] 同一个实例 —— 生产代码
     * （`MeetingMicService.runOfflineSync`）**必须**传 `ApiClient.createForUpload(baseUrl)`。
     */
    private val uploadApi: EchoApi = api,
) : OfflineTransport {

    /**
     * 探活：`GET /health`（**唯一不带 `/api` 前缀、也不要令牌**的东西，见 `EchoApi`）。
     *
     * 为什么不用 `/api/status`：令牌坏掉会得到 401，很容易被误判成"服务没起来"，
     * 而这里的语义只有一件事 —— ECHO 到底在不在。
     */
    override suspend fun reachable(): ProbeResult? = try {
        val r = api.health()
        ProbeResult(r.code(), if (r.isSuccessful) "" else shortReason(r.code()))
    } catch (e: Exception) {
        // 抛异常（超时/连接被拒）= 连不上：这正是"该进离线"的那一档
        ProbeResult(null, e.javaClass.simpleName + (e.message?.let { ": $it" } ?: ""))
    }

    /**
     * ECHO 现在在录哪一场。
     *
     * 读不到就回 `null`（调用方**算不可达**、立刻停）：不敢在"不知道电脑在不在录"
     * 的情况下往一台可能正在录别的会的机器上灌段（那会把别人的会打断/混音）。
     */
    override suspend fun activeMeeting(): ActiveMeeting? = try {
        val r = api.meetingStatusJson()
        val raw = if (r.isSuccessful) r.body()?.string().orEmpty() else ""
        if (raw.isBlank()) null
        else {
            val obj = jsonOf(raw)
            ActiveMeeting(
                recording = bool(obj, "active"),
                meetingId = int(obj, "meetingId", "meeting_id"),
                source = str(obj, "source"),
                note = str(obj, "folder"),
                // 服务端从 2026-10-16 起在"手机补传"那条路上回 `clientId`（`app/meeting.py`）。
                // 读不到就是空串，那时退化成按会议 id 判（见 `service/MeetingOwnership.kt`：
                // 认不出来**宁可判不是**，最多多等一会儿，绝不抢占别人的会）。
                clientId = str(obj, "clientId", "client_id"),
            )
        }
    } catch (e: Exception) {
        null
    }

    /**
     * 建那场会：**带上缓存的开始时间与客户端 id**。
     *
     * 后端恒 200，成没成看 `{ok}`；`ok=false` 时按回包那句话分类（403/422 这类
     * 是"服务端拒绝"——**重试也不会好**，要如实说，别让用户等一个永远不来的重试）。
     */
    override suspend fun startMeeting(req: StartRequest): StartOutcome = try {
        val r = api.meetingStartPhoneWithId(
            source = req.source,
            clientId = req.clientId,
            startedAt = req.startedAt,
            endedAt = req.endedAt,
        )
        val body = r.body()
        when {
            !r.isSuccessful -> {
                val f = failureOf(r.code())
                StartOutcome.Failed(
                    note = "服务端没收下这场会的开始（HTTP ${r.code()}，${failureText(f)}）",
                    retryable = failureRetryable(f),
                )
            }
            body == null -> StartOutcome.Failed(
                note = "服务端回了空响应（读不到会议 id）", retryable = true)
            !body.ok -> {
                // 200 但 ok=false：把服务端那句原话带上（它比我们猜的准）
                val msg = body.message.orEmpty().ifBlank { "服务端说没开始" }
                val busy = looksBusy(msg)
                StartOutcome.Failed(
                    note = "服务端没开始这场会：$msg",
                    // "正忙"是**等一下**的意思，仍然可重试（但**不是**抢占）
                    retryable = busy || !looksLikeRejection(msg),
                )
            }
            else -> StartOutcome.Ok(body.effectiveMeetingId)
        }
    } catch (e: Exception) {
        StartOutcome.Failed(note = "联系不上 ECHO：${e.message}", retryable = true)
    }

    /**
     * 传一段。带上 `clientId` / `seq` 之后，服务端**才可能**做到幂等与对账。
     *
     * ⚠️ `Content-Type` 用**这一段清单里记的**格式（[UploadRequest.format]），
     * 不是写死的 `audio/wav`：用户 2026-10-17 把段改成了 FLAC，而服务端要按
     * `Content-Type: audio/flac` + `fLaC` 魔数识别。写死成 wav 的话，FLAC 那些段
     * 会被当成 wav 收下 —— 那正是"谎报格式"（服务端解不出来，用户以为传上去了）。
     */
    override suspend fun uploadSegment(req: UploadRequest): SegmentOutcome = try {
        val payload = req.wav.toRequestBody(req.format.contentType.toMediaType())
        // ⚠️ 这一段走 [uploadApi]（读/写都是长超时的那一个）：见类注释 ——
        // 用普通客户端传 18 MB 的 WAV，第 2 段就会被 60 秒的写超时判死。
        val r = uploadApi.meetingSegmentWithId(
            clientId = req.clientId, seq = req.seq, startedAt = req.startedAt,
            source = req.source, body = payload,
        )
        val body = r.body()
        when {
            !r.isSuccessful -> {
                val f = failureOf(r.code())
                SegmentOutcome.Failed(
                    failure = f,
                    note = "第 ${req.seq} 段没被收下（HTTP ${r.code()}，${failureText(f)}）",
                    retryable = failureRetryable(f),
                )
            }
            body == null -> SegmentOutcome.Failed(
                SyncFailure.SERVER_ERROR, "第 ${req.seq} 段的回包是空的", retryable = true)
            // 这条接口**本来就恒回 {ok, message}**（见 EchoApi 的注释），所以按 ok 判是对的。
            !body.ok -> {
                val msg = body.message.orEmpty().ifBlank { "服务端没说是为什么" }
                val f = if (looksBusy(msg)) SyncFailure.SERVER_BUSY else SyncFailure.REJECTED
                SegmentOutcome.Failed(f, "第 ${req.seq} 段被服务端拒了：$msg",
                    retryable = failureRetryable(f))
            }
            else -> SegmentOutcome.Ok(
                serverMeetingId = body.effectiveMeetingId,
                serverCount = body.effectiveCount,
                duplicate = body.duplicate,
            )
        }
    } catch (e: Exception) {
        // 失败也要**说人话**（尤其超时）：见 [uploadFailureNote] ——
        // "这段太大/网太慢、重试中"与"服务端出错"是两件完全不同的事，
        // 混成一句"没发出去"会让人去查错的地方。
        SegmentOutcome.Failed(SyncFailure.UNREACHABLE, uploadFailureNote(req.seq, req.wav.size, e),
            retryable = true)
    }

    /**
     * 问"这一场服务端手上有几段"。
     *
     * 现实里唯一**现在就能用**的来源是 `GET /api/meeting/status` 的 `segments`
     * （既有字段）—— 但只有一个前提：它报的确实是我们这一场。
     * 判"是不是我们这一场"用 `clientId`（如果服务端以后给了），否则用会议 id（清单里记过）。
     *
     * 拿不到（读不到状态 / 不是我们这场 / 认不出）就 [CountOutcome.Failed] ——
     * **绝不用"本地数了数"冒充对账**。
     */
    override suspend fun segmentCount(clientId: String, meetingId: Int): CountOutcome = try {
        val r = api.meetingStatusJson()
        val raw = if (r.isSuccessful) r.body()?.string().orEmpty() else ""
        if (raw.isBlank()) {
            CountOutcome.Failed(SyncFailure.UNREACHABLE, "读不到 ECHO 的会议状态，没法对账")
        } else {
            val obj = jsonOf(raw)
            if (matches(obj, clientId, meetingId)) {
                // 录完了 / 正在录：状态里那个 segments 就是这一场存下来的段数，
                // 正是"补传完成后对账"要的数字。
                CountOutcome.Ok(int(obj, "segments", "segmentCount", "segment_count"))
            } else {
                CountOutcome.Failed(SyncFailure.SERVER_BUSY,
                    "ECHO 正在录另一场（或认不出这一场），读不到这一场的段数：" +
                        "服务端需要能按 clientId 回报段数")
            }
        }
    } catch (e: Exception) {
        CountOutcome.Failed(SyncFailure.UNREACHABLE, "读 ECHO 的会议状态出错：${e.message}")
    }

    /** 状态里那一场是不是**我们这一份**（`clientId` 优先；没有它才比 id）。 */
    private fun matches(obj: JsonObject, clientId: String, meetingId: Int): Boolean {
        val cid = str(obj, "clientId", "client_id")
        if (cid.isNotBlank()) return cid == clientId
        return meetingId > 0 && int(obj, "meetingId", "meeting_id") == meetingId
    }

    /**
     * **收尾这场会**（补传的最后一步）。
     *
     * 为什么这一条是补传流程里**不可省**的一步：服务端只在 `POST /api/meeting/stop` 里
     * 把这场会落盘、写 `meta`、**发起转写**，并把那条唯一的录音槽放掉。
     * 只做 `start` + `segment` 的话：音频躺在电脑上永远不出文字，而且之后
     * 电脑/手机都开不了新会议（用户看到的是"开始录音失败"，根因却在几天前）。
     *
     * ## 为什么**必须**把这一场的 `clientId` 发出去
     *
     * 服务端那条接口有护栏（`app/api.py::meeting_stop` → `meeting.stop_meeting(client_id=…)`）：
     * 带了 `clientId` 且与"当前正在录的那一场"**不符**时，它**拒绝收尾且一个字节都不动**。
     * 这一条正是为了挡住"停错会"：补传是**后台自动**动作（退避重试、断点续传、几份清单一前一后），
     * 只要顺序或时序稍稍错一点，一个不带 id 的 stop 就会把**别人正在录的那场会**停掉
     * —— 那场音频当场就断了（2026-09-23 真出过这次事故），用户白录一段。
     * 不带 `clientId` 时这条护栏在真机流程里**永远不会生效**，所以这里必须发。
     *
     * 发的是**这一份清单自己的** id（`req.clientId`），不是"当前活动那场"的 ——
     * 补传是"给这一场收尾"，拿别人的 id 去收尾等于把护栏绕过去。
     *
     * ## 三种回包的判法（每一种都对应一个真实分支）
     *
     *  1. 2xx + `ok=true` → 收下了（转写已经开始）；
     *  2. 2xx + `ok=false` + "没有进行中的会议" → **幂等命中，算成功**
     *     （`app/meeting.py::stop_meeting` 的第一行；见 [looksAlreadyStopped]）。
     *     不这么算的话，已经停过的那些清单会**永远卡在最后一步**；
     *  3. 其余非 2xx / 抛异常 → 如实分类（**绝不**说成"已经好了"）。
     *
     * ⚠️ **第 3 档里必须把"clientId 不符"那一句单独认出来**（[looksClientIdMismatch]）：
     * 它同样是 `ok=false`，但语义与第 2 档**正好相反** —— 服务端**没有**收尾我们这一场，
     * 那一场还占着录音槽、还没开始转写。把它当成功 = 把一场根本没传完的录音标成"已完成"✗。
     * 它归到**可重试**那一档（服务端正在录别的会，等它录完就能收尾），原因原话照带。
     *
     * ⚠️ `clientId` 为空时**一个 query 都不发**（`null` = Retrofit 跳过这个参数，
     * 退化成改动前的请求，与老服务端/老行为逐字节相同）—— **不能**发成空串：
     * Retrofit 会把空串编码成 `?clientId=`，那既不等于"带上了 id"，
     * 也让"面板/技能那条老路"的请求形状悄悄变样。只有手上有这一场的 id 时才发
     * —— 补传那条路必然有（幂等键就是它）。
     * `meetingId` 仍然只进日志/文案：服务端那条签名里没有它。
     */
    override suspend fun stopMeeting(req: StopRequest): StopOutcome = try {
        val who = req.clientId.ifBlank { if (req.meetingId > 0) "会议 ${req.meetingId}" else "这一场" }
        // 只在这一场真的有 id 时才发（空 = 老行为，见上面那段说明）
        val cid = req.clientId.trim().takeIf { it.isNotEmpty() }
        val r = api.meetingStop(clientId = cid)
        val body = r.body()
        when {
            !r.isSuccessful -> {
                val f = failureOf(r.code())
                StopOutcome.Failed(
                    failure = f,
                    note = "$who：服务端没收下收尾（HTTP ${r.code()}，${failureText(f)}）",
                    retryable = failureRetryable(f),
                    alreadyUploadedHint = true,
                )
            }
            body == null -> StopOutcome.Failed(
                SyncFailure.SERVER_ERROR, "$who：收尾的回包是空的", retryable = true,
                alreadyUploadedHint = true)
            body.ok -> StopOutcome.Ok(alreadyStopped = false, note = body.message.orEmpty())
            else -> {
                val msg = body.message.orEmpty().trim()
                if (looksAlreadyStopped(msg)) {
                    // 服务端说"没有进行中的会议"：这正是"我们已经停过了"。
                    // 卡在这一档是最糟的 —— 用户看到"差最后一步"却永远不动，而服务端一切都好。
                    StopOutcome.Ok(alreadyStopped = true, note = msg.ifBlank { "之前已经停过这场会" })
                } else if (looksClientIdMismatch(msg)) {
                    // 「这场不是 clientId=… 那一场」= 服务端在录**别人**的会，它**没有**给我们收尾。
                    // 这一档**绝不能**算成功：算了就等于说"这场已经传完并开始转写"，
                    // 而真相是"这一场还在服务端占着录音槽、一个字都还没转"。
                    // 归到可重试：等那场录完再收尾（不是契约问题，别吓用户说"重试也没用"）。
                    StopOutcome.Failed(
                        failure = SyncFailure.SERVER_BUSY,
                        note = "$who：服务端没收尾（$msg）—— **还没有开始转写**，" +
                            "缓存留着，等它录完那场再收尾",
                        retryable = true,
                        alreadyUploadedHint = true,
                    )
                } else {
                    val f = if (looksBusy(msg)) SyncFailure.SERVER_BUSY else SyncFailure.REJECTED
                    StopOutcome.Failed(
                        failure = f,
                        note = "$who：服务端没收尾（${msg.ifBlank { "它没说为什么" }}）",
                        retryable = failureRetryable(f),
                        alreadyUploadedHint = true,
                    )
                }
            }
        }
    } catch (e: Exception) {
        StopOutcome.Failed(SyncFailure.UNREACHABLE,
            "收尾那个请求没发出去：${e.message}", retryable = true, alreadyUploadedHint = true)
    }

    /**
     * **上报暂停区间**（`POST /api/meeting/pauses`，用户 2026-10-17 的第③件事）。
     *
     * 契约（两边一致）：JSON body `{"clientId": "...", "pauses": [{"from": .., "to": ..}]}`。
     * 按 `clientId` 幂等（服务端合并去重）—— 所以每次都把**当前的整份**暂停区间发上去，
     * 不必先算差集。
     *
     * ## 为什么这里最重要的是"别把'端点不存在'说成失败"
     *
     * 老服务端（这一版它由另一条线同时在实现）**根本没有这个端点**：
     * FastAPI 会回 404，反向代理还可能回 405/501。这些在语义上都是同一件事 ——
     * **"这次没报上去"**，而不是"补传出错了"：
     *  * 归到 [PauseOutcome.Unsupported] → 编排层照常走完 `start → segment → 对账 → stop`，
     *    只在 `syncNote` 里如实记一句；
     *  * 若把它归成"失败"，用户会因为一个**可有可无**的上报而永远传不上音频
     *    （端点不存在 ≠ 音频有问题）。
     *
     * 其余失败（连不上 / 5xx / 服务端明确拒绝）也**不算补传失败**（编排层同样不管它的返回值
     * 是不是失败），只是**原因**要说准：这一档是 [PauseOutcome.Failed]，不是 Unsupported。
     *
     * 空 `pauses` **一个请求都不发**：没东西可报时发一次请求既没意义，
     * 又会让"老服务端缺端点"这件事在每一轮同步里都刷一遍日志。
     */
    override suspend fun reportPauses(req: PauseRequest): PauseOutcome {
        if (req.pauses.isEmpty()) return PauseOutcome.Ok(0)
        return try {
            val payload = MeetingPausesRequest(
                clientId = req.clientId,
                pauses = req.pauses.map { MeetingPausesRequest.PauseDto(from = it.from, to = it.to) },
            )
            val r = api.meetingPauses(payload)
            val text = r.body()
            when {
                !r.isSuccessful -> {
                    val f = failureOf(r.code())
                    if (looksEndpointMissing("", r.code())) {
                        PauseOutcome.Unsupported(
                            "这台服务端没有上报暂停区间的接口（HTTP ${r.code()}）：这次没报上去，" +
                                "音频照常传")
                    } else {
                        PauseOutcome.Failed(f,
                            "暂停区间没报上去（HTTP ${r.code()}，${failureText(f)}）：不影响音频补传")
                    }
                }
                text == null -> PauseOutcome.Failed(SyncFailure.SERVER_ERROR,
                    "暂停区间的回包是空的：这次没报上去（不影响音频补传）")
                text.ok -> PauseOutcome.Ok(req.pauses.size)
                looksEndpointMissing(text.message.orEmpty()) -> PauseOutcome.Unsupported(
                    "这台服务端没有上报暂停区间的接口（${text.message.orEmpty().ifBlank { "它没说为什么" }}）：" +
                        "这次没报上去，音频照常传")
                else -> PauseOutcome.Failed(
                    if (looksBusy(text.message.orEmpty())) SyncFailure.SERVER_BUSY
                    else SyncFailure.REJECTED,
                    "暂停区间没报上去：${text.message.orEmpty().ifBlank { "服务端没说为什么" }}" +
                        "（不影响音频补传）")
            }
        } catch (e: Exception) {
            PauseOutcome.Failed(SyncFailure.UNREACHABLE,
                "暂停区间没报上去：${e.message}（不影响音频补传）")
        }
    }

    /** 原始 JSON → `JsonObject`（解析不了给一个空对象 —— 调用方只看得到"字段是空的"）。 */
    private fun jsonOf(raw: String): JsonObject = try {
        JsonParser.parseString(raw).takeIf { it.isJsonObject }?.asJsonObject ?: JsonObject()
    } catch (e: Exception) {
        JsonObject()
    }

    private fun str(obj: JsonObject, vararg keys: String): String =
        keys.firstNotNullOfOrNull { k ->
            obj.get(k)?.takeIf { it.isJsonPrimitive }?.asString?.takeIf { it.isNotBlank() }
        }.orEmpty()

    private fun int(obj: JsonObject, vararg keys: String): Int =
        keys.firstNotNullOfOrNull { k ->
            obj.get(k)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asInt
        } ?: 0

    private fun bool(obj: JsonObject, key: String): Boolean =
        obj.get(key)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isBoolean }?.asBoolean
            ?: false

    private fun shortReason(code: Int): String = when {
        code == 401 || code == 403 -> "令牌不对/没权限"
        code in 500..599 -> "ECHO 自己出错了"
        else -> "HTTP $code"
    }

    private fun failureText(f: SyncFailure): String = when (f) {
        SyncFailure.UNREACHABLE -> "连不上"
        SyncFailure.REJECTED -> "服务端拒绝（重试也不会好，多半是契约/权限没对齐）"
        SyncFailure.SERVER_BUSY -> "服务端正忙"
        SyncFailure.SERVER_ERROR -> "服务端出错"
    }

    /** 服务端那句话看起来是不是"我在忙"（409/503 之外，后端也可能用 ok=false 说这件事）。 */
    private fun looksBusy(msg: String): Boolean =
        msg.contains("正在录") || msg.contains("忙") || msg.contains("busy", ignoreCase = true) ||
            msg.contains("转写")

    /** 看起来是不是"拒绝了"（权限/契约/参数问题 → 别傻等重试）。 */
    private fun looksLikeRejection(msg: String): Boolean =
        msg.contains("不允许") || msg.contains("拒绝") || msg.contains("403") ||
            msg.contains("不支持") || msg.contains("未配对") || msg.contains("鉴权") ||
            msg.contains("参数")
}

// --------------------------------------------------------------------------- 传段失败的"人话"

/**
 * 这一次上传异常是不是**超时**（纯函数）。
 *
 * 判据是 OkHttp 的两个事实，不是一个"猜"：
 *  * `SocketTimeoutException`：读/写**单次操作**无进展超过 [ApiClient.UPLOAD_TIMEOUT_S]；
 *  * 其它 `InterruptedIOException`：`callTimeout` 到了 —— 即**单段总预算**
 *    [ApiClient.UPLOAD_CALL_TIMEOUT_S] 用完（"一直在动但永远传不完"那一档）。
 *
 * 为什么必须把它们**单独**认出来：超时的处理方式与"服务端拒了/连不上"完全不同 ——
 * 超时的真相是"这段太大或网太慢，等一下再传"，而且**重试真的会有用**（同一段、同一条链路，
 * 换个时刻可能就过了）。把它混进"没发出去"里，用户与排障的人都会去查错的地方。
 */
internal fun isUploadTimeout(e: Throwable): Boolean = e is java.io.InterruptedIOException

/**
 * 第 [seq] 段（[bytes] 字节）上传失败时**如实**的那句话（纯函数）。
 *
 * 三条口径（每一句都对应一次真实的静默错法）：
 *  1. **超时要说"这段太大 / 网太慢，重试中"** —— 不是"同步失败"，也不是"服务端出错"；
 *     真机现场是 18.3 MB 的 WAV 走中继，而客户端原来只有 60 秒写超时（见 [ApiClient.UPLOAD_TIMEOUT_S]）；
 *  2. **要把"有多大"说出来**：一句"传不上去"没法让人判断"是不是我这段太长了"；
 *  3. **要说清"已传过的段不会重传、下次从这一段接着传"** —— 否则用户以为又要从第 1 段重来
 *     （那正是他这次最怕的事：18 MB 白传一遍）。
 *
 * 非超时的异常不冒充超时：原样带上异常类名与消息（那是排障唯一的线索）。
 */
internal fun uploadFailureNote(seq: Int, bytes: Int, e: Throwable): String {
    val head = "第 $seq 段（${megabytesText(bytes)}）"
    val resume = "重试中：已传过的段不会重传，下次从第 $seq 段接着传"
    return when {
        e is java.net.SocketTimeoutException ->
            "$head 上传超时 —— 这段太大 / 网太慢（连续 ${ApiClient.UPLOAD_TIMEOUT_S} 秒一个字节都没传出去）。" +
                resume
        isUploadTimeout(e) ->
            "$head 上传超时 —— 这段太大 / 网太慢（单段最多等 ${ApiClient.UPLOAD_CALL_TIMEOUT_S} 秒还没传完）。" +
                resume
        else ->
            "$head 没发出去：${e.javaClass.simpleName}" +
                (e.message?.let { "（$it）" } ?: "")
    }
}

/**
 * 字节数 → 人看的大小（纯函数，`18.3 MB` / `640 KB` / `512 B`）。
 *
 * 为什么要单独抽出来并用 `Locale.US`：中文系统上 `String.format` 会用本地小数分隔符，
 * 而这句话会被用例逐字比对（也跟着日志走）—— 一处用逗号一处用点，判据就会莫名其妙地红。
 */
internal fun megabytesText(bytes: Int): String {
    if (bytes <= 0) return "0 B"
    val kb = bytes / 1024.0
    val mb = kb / 1024.0
    return when {
        mb >= 1.0 -> String.format(java.util.Locale.US, "%.1f MB", mb)
        kb >= 1.0 -> String.format(java.util.Locale.US, "%.0f KB", kb)
        else -> "$bytes B"
    }
}
