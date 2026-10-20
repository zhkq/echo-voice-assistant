package com.echo.contact.data.dto

import com.google.gson.annotations.SerializedName

/**
 * 与 ECHO 后端 `/api` 交互的 DTO。
 *
 * 字段名按 `app/api.py` 的真实契约写（**不是**猜的）：
 *  - 配对：`POST /api/pair/phone/claim`（api.py:2526）→ `{ok, token, keyId, name, baseUrl, note}`；
 *    失败时抛 HTTPException(403, detail=…)，所以错误体是 `{"detail": "..."}`。
 *  - 指令：`POST /api/assistant/command`（api.py:710）→ `{ok, message}`；**没有 commandId**。
 *  - 历史：`GET /api/commands`（api.py:1087）→ `{total, items}`，item 是 `commands` 表的行
 *    （db.py:112：`id, ts, source, text, status, session_id, reply, brief, duration_ms, error, meta`）
 *    外加 `_command_row` 摊平出来的 `backend` / `intent`（api.py:1071）。
 *  - 会话：`GET /api/commands/{id}/session`（api.py:1108）→ 见下。
 */

/** 配对（用一次性码换令牌）：`POST /api/pair/phone/claim` 的请求体。 */
data class PairClaimRequest(
    val code: String,
    val name: String = "mobile",
)

/** `POST /api/pair/phone/claim` 的响应体（令牌只出现这一次，需立刻存进安全存储）。 */
data class PairClaimResponse(
    val ok: Boolean = false,
    val token: String? = null,
    @SerializedName("keyId") val keyId: String? = null,
    val name: String? = null,
    @SerializedName("baseUrl") val baseUrl: String? = null,
    val note: String? = null,
)

/** FastAPI 的错误体：`{"detail": "..."}`。 */
data class ApiErrorResponse(
    val detail: String? = null,
)

/** 文字指令：`POST /api/assistant/command` 的请求体（api.py:139 CommandIn）。 */
data class CommandRequest(
    val text: String,
    val source: String = "mobile",
    val workspace: String = "",
    @SerializedName("session_id") val sessionId: String = "",
)

/**
 * `POST /api/assistant/command` 的响应体。
 *
 * `ok` / `message` 是既有键；下面三个是**新增键**（老服务端没有 → null/0，手机自己退回
 * 原来的"按文本认领"那条路，行为不变）：
 *  * `commandId` —— 这一轮那一行的 id（手机回顾轮次靠它按 id 取结果，因为那些轮次
 *    不进 `/api/commands` 列表）；
 *  * `source`    —— 服务端**实际落库**的标签（`review` = 回顾轮次）；
 *  * `duplicate` —— true = 这一轮已经记过了（同一轮重发，服务端幂等忽略）。
 */
data class CommandResponse(
    val ok: Boolean = false,
    val message: String? = null,
    @SerializedName("commandId") val commandId: Int? = null,
    val source: String? = null,
    val duplicate: Boolean? = null,
)

/**
 * `GET /api/commands` 的单条记录：`commands` 表一行 + 摊平的 `backend`/`intent`。
 * 生命周期 `status`：pending → sent → running → done / failed。
 */
data class CommandItem(
    val id: Int? = null,
    val ts: String? = null,
    val source: String? = null,
    val text: String? = null,
    val status: String? = null,
    @SerializedName("session_id") val sessionId: String? = null,
    val reply: String? = null,
    val brief: String? = null,
    @SerializedName("duration_ms") val durationMs: Int? = null,
    val error: String? = null,
    val backend: String? = null,
    val intent: String? = null,

    /**
     * 电脑侧产出的「**要念的那段话**」（只含摘要，与电脑本机念的是同一段文字）。
     * 老电脑没有这个字段 → null（手机侧退回"取摘要"，见 `SpeakPlan`）。
     *
     * 为什么类型是 `Any?` 而不是 `String?`：这是**新字段**，电脑侧要是哪天写成
     * 数字 / 对象 / null，Gson 对 `String` 字段会直接抛（`Expected a string but was
     * BEGIN_OBJECT`）—— 等于**一个脏字段把整页指令历史弄崩**。用 `Any?` 接住，
     * 判据那一侧（`SpeakPlan.decide`）只认字符串，其余一律当"没给"。
     * `alternate` 是宽容：电脑侧要是写成 snake_case（`speak_text`），这里也认。
     */
    @SerializedName(value = "speakText", alternate = ["speak_text"]) val speakText: Any? = null,

    /**
     * 这条指令**是谁发起的**（`mobile` / `pc`）。老电脑没带 → null。
     *
     * 为什么手机要它：**谁发起谁念** —— 电脑上敲的指令不该在手机喇叭上念出来。
     * 判据在 `SpeakPlan`（拿不到就不拦）。
     */
    val origin: String? = null,
)

/** `GET /api/commands?limit=&offset=&q=&since=` 的响应；`total` 是**过滤后**的总数。 */
data class CommandListResponse(
    val total: Int = 0,
    val items: List<CommandItem> = emptyList(),
)

/** 某条命令落到的 DSH 会话：`GET /api/commands/{id}/session` 的响应（api.py:1108）。 */
data class CommandSessionResponse(
    val ok: Boolean = false,
    @SerializedName("session_id") val sessionId: String? = null,
    val messages: List<SessionMessage> = emptyList(),
    val title: String? = null,
    val cwd: String? = null,
    val exists: Boolean = false,
    val running: Boolean = false,
    val error: String? = null,
    val anchored: Boolean = false,
    /** 没有关联会话时后端给的一句人话说明。 */
    val message: String? = null,
)

/** 会话里的一轮消息（api.py:1168 只吐 `role` / `seq` / `text`）。 */
data class SessionMessage(
    val role: String? = null,
    val seq: Int? = null,
    val text: String? = null,
)
