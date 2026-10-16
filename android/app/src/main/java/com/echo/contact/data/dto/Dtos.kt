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

/** `POST /api/assistant/command` 的响应体——只有 `{ok, message}`。 */
data class CommandResponse(
    val ok: Boolean = false,
    val message: String? = null,
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
