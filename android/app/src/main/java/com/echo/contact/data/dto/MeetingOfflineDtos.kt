package com.echo.contact.data.dto

import com.google.gson.annotations.SerializedName

/**
 * **离线补传那条路的会话语义**（手机侧按"最合理的契约"实现；服务端那一半由 Lead 另派）。
 *
 * ## 为什么这些字段要有（逐条对应一个必须解决的问题）
 *
 *  * `clientId`：客户端生成的**稳定** id。断网时手机先在本地把一场会建起来，
 *    联网后要"把同一场会补到服务端" —— 而补传可能重试很多次（超时重发）。
 *    **服务端必须按它幂等**：同一个 `clientId` 反复调 `POST /api/meeting/start`
 *    只该得到同一场会议，不该建出两场（否则一次超时重发 = 用户看到两场一模一样的会）。
 *  * `startedAt`：**缓存的开始时间**（不是补传的时间）。用户 10:00 开始录、
 *    11:00 才联网，会议在服务端的时间轴必须是 10:00 起 —— 否则转写、纪要、
 *    与电脑侧其他事件的对照全错位。
 *  * `seq`：段序号（1 起）。这是**对账与续传的基准**：服务端认下哪几段、本地跳到第几段，
 *    两边都靠它对齐。重复的 `(clientId, seq)` 必须被当作同一段（不能存两遍）。
 *
 * ## 服务端**没做到**时会怎样（客户端怎么降级）
 *
 * 不认识的多余 query 参数会被后端忽略，所以老服务端会**静默**忽略 `clientId`/`seq`/
 * `startedAt` —— 表现是"补传看上去成功、但会议时间是补传那一刻、段可能重复"。
 * 所以客户端在拿不到段数时**不假装对账通过**（见 `OfflineMeetingSync` 那一档），
 * 并把"服务端没回段数"如实报出来。
 */
data class MeetingStartPhoneRequest(
    val source: String = "phone",
    /** 客户端稳定 id（幂等键）。 */
    @SerializedName("clientId") val clientId: String,
    /** 缓存的开始时间（ISO-8601 本地时间，如 `2026-10-16T20:31:02`）。 */
    @SerializedName("startedAt") val startedAt: String,
    /** 结束时间（补传时已经录完了；空 = 还在录）。 */
    @SerializedName("endedAt") val endedAt: String = "",
    /** 清单里的总段数（服务端可据此核对，也可只用它做日志）。 */
    @SerializedName("segments") val segments: Int = 0,
    @SerializedName("durationSeconds") val durationSeconds: Double = 0.0,
    /** 暂停区间（相对开始时间的秒；与电脑侧 `meta["pauses"]` 同一个语义）。 */
    @SerializedName("pauses") val pauses: List<PauseDto> = emptyList(),
) {
    data class PauseDto(
        val from: Double = 0.0,
        val to: Double = 0.0,
        val by: String = "local",
    )
}

/**
 * `POST /api/meeting/start` 的响应（**宽容解析**：多一个字段不会坏，少一个也不会）。
 *
 * `ok` / `message` 与既有的 [CommandResponse] 一致（后端恒 200，成没成看 `ok`）；
 * 另外**希望**服务端给出：
 *  * `meetingId`：那场会议的 id（客户端记进清单，便于人肉对账与后续核对）；重复 `clientId`
 *    必须回**同一个** id；
 *  * `duplicate`：这一次是不是命中了幂等（`true` = 这场会之前就建好了，没有新建）。
 */
data class MeetingStartPhoneResponse(
    val ok: Boolean = false,
    val message: String? = null,
    @SerializedName("meetingId") val meetingId: Int? = null,
    val duplicate: Boolean = false,
    /** 有些实现会把会议详情摊平在这里（`id` / `name`），一起收着，免得白丢。 */
    val id: Int? = null,
) {
    /** 会议 id：`meetingId` 优先，其次摊平的 `id`。 */
    val effectiveMeetingId: Int get() = meetingId ?: id ?: 0
}

/**
 * `POST /api/meeting/segment` 的响应。
 *
 * 需要服务端在收下这一段之后**如实说它手上有几段**（下面四个字段**任一**即可，
 * 客户端按顺序尽取）：
 *  * `segmentCount`：这一场它手上已有的段数（**对账的首选**）；
 *  * `seqCount`：同上（另一个常见命名）；
 *  * `segments`：段序号数组（客户端取它的长度；这是既有 `GET /api/meeting/status`
 *    那一族的形状，所以最可能被复用）；
 *  * `segmentIndex`：刚收下这一段的序号（**至少**要能回答"你收的是第几段吗"，
 *    否则连"这段到底进没进"都无从判断）。
 *
 * `duplicate = true` 表示这段是幂等命中（之前就收过）：客户端把它当**成功**（
 * 那正是"重试不会把同一段存两遍"的证据）。
 */
data class MeetingSegmentResponse(
    val ok: Boolean = false,
    val message: String? = null,
    @SerializedName("meetingId") val meetingId: Int? = null,
    @SerializedName("segmentCount") val segmentCount: Int? = null,
    @SerializedName("seqCount") val seqCount: Int? = null,
    val segments: List<Any?>? = null,
    @SerializedName("segmentIndex") val segmentIndex: Int? = null,
    val duplicate: Boolean = false,
) {
    /** 服务端**明确**回的段数（null = 它没说 → 客户端不做对账假设）。 */
    val effectiveCount: Int?
        get() = segmentCount ?: seqCount ?: segments?.size

    val effectiveMeetingId: Int get() = meetingId ?: 0
}

/**
 * `POST /api/meeting/pauses` 的请求体（用户 2026-10-17 的第③件事）。
 *
 * 契约（用户拍的板，两边一致）：
 * ```json
 * {"clientId": "phone-...", "pauses": [{"from": 12.5, "to": 31.0}]}
 * ```
 *  * `from` / `to`：**相对这一场开始时间**的秒数（与电脑侧 `meta["pauses"]` 同一个语义 ——
 *    暂停 = 不写盘、不产生音频；用相对秒而不是绝对时间，暂停之后时间轴才不会跳）；
 *  * 按 `clientId` **幂等**（服务端合并去重）→ 客户端每次把当前的整份报一遍就行。
 *
 * ⚠️ 为什么这里**没有** `by`（`local`/`remote`）：契约里只有 `from`/`to`。
 * 多发的字段会被服务端静默忽略（等于没发），而"以为它在起作用"是最难查的错法
 * —— 契约上没有的东西，客户端不发明。
 */
data class MeetingPausesRequest(
    @SerializedName("clientId") val clientId: String,
    val pauses: List<PauseDto>,
) {
    data class PauseDto(
        val from: Double = 0.0,
        val to: Double = 0.0,
    )
}

/**
 * `GET /api/meeting/status` 的扩展字段（**同一族**，所以放在这份 response 里）。
 *
 * `clientId` / `completed` 是**希望**服务端补上的：补传时客户端要判"ECHO 现在录的那一场
 * 是不是我自己这一份清单的会"（判错会**抢占电脑正在录的会**，那是明令禁止的）。
 * 拿不到 `clientId` 时客户端退化成"按会议 id 相等"判（见 `OfflineMeetingSync.mineRecording`），
 * 拿不到就**宁可判不是**（最多多等一会儿，不会打断别人的会）。
 */
data class MeetingStatusExtra(
    @SerializedName("clientId") val clientId: String? = null,
    /** 手机侧离线录音补传完成时用的标记（不认也没关系）。 */
    val completed: Boolean = false,
)
