package com.echo.contact.data.dto

import com.google.gson.annotations.SerializedName

/**
 * 每日回顾相关 DTO。字段名照**活着的后端实测**（2026-10-09 抓的），不是照面板猜的：
 *  - `GET /api/daily-review`         → `{enabled, ready, reason, agent, workspace, vault, kind, sessionId, running}`
 *  - `GET /api/daily-review/summary` → `{today, todayTurns, todayFailed, lastAt, lastBrief, lastStatus, lastText}`
 *  - `GET /api/daily-review/history/{date}` → `{ok, date, turns:[{id, at, text, reply, brief, status, error, seconds, sessionId, registered}], count, vault, vaultNote, vaultNoteExists[, error]}`
 *    （`error` 只在 `ok=false` 时出现；`registered` 是 2026-10-18 加的登记核实，老记录为缺省）
 *  - `GET /api/daily-review/history?limit=` → `{items:[{date, turns, failed, lastAt, brief, sessionId}], total, today}`
 */

/** 回顾功能本身的状态。 */
data class DailyReviewState(
    val enabled: Boolean = false,
    val ready: Boolean = false,
    val reason: String = "",
    val agent: String = "",
    val workspace: String = "",
    val vault: String = "",
    val kind: String = "",
    @SerializedName("sessionId") val sessionId: String = "",
    val running: Boolean = false,
)

/** 仪表盘那一小口：今天做没做、最近一次是什么。 */
data class DailyReviewSummary(
    val today: String = "",
    @SerializedName("todayTurns") val todayTurns: Int = 0,
    @SerializedName("todayFailed") val todayFailed: Int = 0,
    @SerializedName("lastAt") val lastAt: String? = null,
    /** **播报文本**（给耳朵听的那一版），就是要念的东西。 */
    @SerializedName("lastBrief") val lastBrief: String? = null,
    @SerializedName("lastStatus") val lastStatus: String? = null,
    /** 口述原文（转写，可能有噪音），不用来念。 */
    @SerializedName("lastText") val lastText: String? = null,
)

/** 某一天里的一轮回顾。 */
data class ReviewTurn(
    val id: Int = 0,
    val at: String = "",
    val text: String = "",
    val reply: String = "",
    /** 这一轮的播报（要念的就是它）。 */
    val brief: String = "",
    val status: String = "",
    val error: String = "",
    val seconds: Double = 0.0,
    @SerializedName("sessionId") val sessionId: String = "",
    /**
     * 这一轮有没有**真的**写进笔记库的工作日志（服务端比的当天日志指纹，2026-10-18 加）。
     *
     * `null` = 老记录（那时还没有这条核实）—— 界面**不许**据此说任何话；
     * `false` = 服务端明确没检测到日志更新，界面要说清（否则"整理完成"会被当成"已登记"）。
     */
    val registered: Boolean? = null,
)

/** 某一天的回顾详情。 */
data class ReviewHistoryDetail(
    val ok: Boolean = false,
    val date: String = "",
    val turns: List<ReviewTurn> = emptyList(),
    val count: Int = 0,
    val vault: String = "",
    @SerializedName("vaultNote") val vaultNote: String? = null,
    @SerializedName("vaultNoteExists") val vaultNoteExists: Boolean = false,
    /**
     * 服务端如实给的失败原因（坏日期等）。
     *
     * 为什么必须有这个字段：`ok=false` 时 HTTP 仍是 **200**，而界面以前只判
     * `isSuccessful` → 点开一个坏日期会画成"这一天没有回顾记录"（假空态）。
     */
    val error: String = "",
)

/** 回顾历史里的一天（`GET /api/daily-review/history` 的 `items[]`）。 */
data class ReviewDay(
    val date: String = "",
    val turns: Int = 0,
    val failed: Int = 0,
    @SerializedName("lastAt") val lastAt: String? = null,
    /** 当天最新那条播报。 */
    val brief: String? = null,
    @SerializedName("sessionId") val sessionId: String = "",
)

/** `GET /api/daily-review/history?limit=` 的响应。 */
data class ReviewHistoryList(
    val items: List<ReviewDay> = emptyList(),
    val total: Int = 0,
    val today: String = "",
)

/**
 * `POST /api/daily-review/submit` 的入参（app/api.py:165 `DailyReviewSubmitIn`）。
 *
 * **这就是"手机也能做回顾"的关键**：它收的是**口述文本**（不是音频），
 * 所以手机上录一段 → 转写成文字 → 交到这里，整理与落库都由 ECHO 干，
 * 与"主机麦克风那条路"得到的是同一个结果。
 */
data class DailyReviewSubmitRequest(
    val text: String,
    @SerializedName("force_new") val forceNew: Boolean = false,
)

/**
 * `POST /api/daily-review/submit` 的响应
 * （`daily_review.py:349` 的 `{ok, session_id, workspace, reply, spoken, source, error, seconds}`）。
 */
data class DailyReviewSubmitResponse(
    val ok: Boolean = false,
    @SerializedName("session_id") val sessionId: String = "",
    val workspace: String = "",
    /** 智能体的完整回复。 */
    val reply: String = "",
    /** **要念出来的那句话**（已过三层兜底）—— 手机就念它。 */
    val spoken: String = "",
    /** `broadcast` / `first_paragraph` / `fallback`（排障用：技能有没有按契约输出）。 */
    val source: String = "",
    /** 业务性失败的原因（注意：`ok=false` 时 HTTP 仍是 **200**）。 */
    val error: String = "",
    val seconds: Double = 0.0,
)

/**
 * `POST /api/daily-review/start` 的响应（`app/daily_review.py::start()` 的返回）。
 *
 * **它是"手机开始一场回顾"的那个动作**（与 PC 版面板上那颗按钮同一个接口）：
 * 只建/取回**今天**那条回顾会话（幂等），**不碰麦克风**。
 * 为什么不调 `/api/daily-review/go`：那条会 `assistant.start_review()`，
 * 真的开**电脑那支麦** —— 手机点开始绝不该把电脑的麦打开。
 *
 * @property access   `ensure_access()` 给的人话说明（"全盘访问已开启"之类）。
 * @property accessOk **三态**：`true`=电脑那边"写笔记库"的权限已就绪；
 *   `false`=没就绪（整理可能变慢，界面要如实说）；`null`=老服务端没这个字段（**不猜**）。
 *   做成可空就是为了"拿不到就不说话"，而不是把缺省值说成 `false`（fake failure）。
 */
data class DailyReviewStartResponse(
    val ok: Boolean = false,
    @SerializedName("sessionId") val sessionId: String = "",
    @SerializedName("workspaceId") val workspaceId: String = "",
    val workspace: String = "",
    val access: String = "",
    @SerializedName("accessOk") val accessOk: Boolean? = null,
    /** `ok=false` 时的原因（`ok=true` 时是空串）。 */
    val error: String = "",
)

/** `POST /api/stt/transcribe`（**multipart**）的响应。 */
data class SttTranscribeResponse(
    val text: String = "",
    val engine: String = "",
    val model: String = "",
)
