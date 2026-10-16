package com.echo.contact.data.dto

import com.google.gson.annotations.SerializedName

/**
 * 会议列表的一行（`GET /api/meetings` 的 `items[]`）。
 *
 * ⚠️ 这里的字段名来自 `db.list_meetings` + `app/api.py:1492-1531` 的**源码阅读**
 * （那份清单另有 `compression` / `timestamps` / `audio_bytes` / `stt_*` 等字段，
 * 界面暂时不用就不声明 —— Gson 会忽略不认识的字段，少声明比声明错更安全）。
 * 等契约核实回来再收紧/补齐。
 */
data class MeetingItem(
    val id: Int = 0,
    val name: String = "",
    val title: String = "",
    @SerializedName("started_at") val startedAt: String = "",
    @SerializedName("ended_at") val endedAt: String = "",
    /** 秒。后端可能给整数也可能给小数，统一按 Double 收。 */
    @SerializedName("duration_seconds") val durationSeconds: Double = 0.0,
    val status: String = "",
    /** 已切出的分段数。 */
    val segments: Int = 0,
    /** 有没有生成纪要。 */
    @SerializedName("has_summary") val hasSummary: Boolean = false,
    /** 说话人名字（认出来的联系人）。 */
    @SerializedName("speakerNames") val speakerNames: List<String> = emptyList(),
    /** 这场正在转写吗（列表里就带这个标记）。 */
    val transcribing: Boolean = false,
    /** 失败原因（空串 = 没出错）。 */
    val error: String = "",
)

/** `GET /api/meetings` 的响应。⚠️ **没有 `total`**（api.py:1531 只回 items）——
 *  "还有没有下一页"只能靠 `len(items) == limit` 自己判断。 */
data class MeetingListResponse(
    val items: List<MeetingItem> = emptyList(),
)

/** 会议里的一个说话人。 */
data class MeetingSpeaker(
    val id: Int = 0,
    val label: String = "",
    /** 识别出来的名字（认不出就是"说话人1"这种默认名）。 */
    val name: String = "",
    val color: String = "",
)

/** 转写正文的一行。 */
data class MeetingLine(
    val id: Int = 0,
    @SerializedName("seg_index") val segIndex: Int = 0,
    /** 秒。 */
    val start: Double = 0.0,
    val end: Double = 0.0,
    @SerializedName("speaker_label") val speakerLabel: String = "",
    val text: String = "",
    /** `speech` 之类；不是语音的行（例如标记）可以用它区分。 */
    val kind: String = "",
)

/**
 * `GET /api/meetings/{mid}` 的单场会议详情。
 *
 * ⚠️ **与列表那份故意不一致**（api.py:1496-1498 明说如此）：
 * 详情用 `hasSummary` / `hasTranscript`（**camel**）、说话人是 `speakers`（**对象数组**）、
 * 而 `segments` 在这边是**数组**（原段数搬去了 `segment_count`）。
 * 所以这个 DTO **刻意不声明 `segments`** —— 声明成 Int 会和列表混，声明成数组又用不上；
 * 需要段数就用 `segmentCount`。
 */
data class MeetingDetail(
    val id: Int = 0,
    val name: String = "",
    val title: String = "",
    /** 主机上的绝对路径（手机上打不开，只用来显示/排障）。 */
    val folder: String = "",
    @SerializedName("started_at") val startedAt: String = "",
    @SerializedName("ended_at") val endedAt: String = "",
    @SerializedName("duration_seconds") val durationSeconds: Double = 0.0,
    val status: String = "",
    val error: String = "",
    val speakers: List<MeetingSpeaker> = emptyList(),
    val lines: List<MeetingLine> = emptyList(),
    @SerializedName("segment_count") val segmentCount: Int = 0,
    /** camel —— 与列表的 `has_summary` 不一样。 */
    @SerializedName("hasTranscript") val hasTranscript: Boolean = false,
    @SerializedName("hasSummary") val hasSummary: Boolean = false,
    /** 有没有主题分段（判据是目录里有没有 `topics.md`）。 */
    @SerializedName("hasSegments") val hasSegments: Boolean = false,
    val transcribing: Boolean = false,
)

/**
 * `GET /api/transcribe/status` 里一个任务的进度。
 *
 * 实测无任务时返回 `{}`（字段取自 `meeting.py` 的 `_set_progress` 调用点）。
 * ⚠️ 这个 map 的**键**既可能是会议 id（转写），也可能是会议 name（导入录音那条路）
 * （meeting.py:3580），所以客户端只能当字符串键用、不能假定"键一定是数字"。
 */
data class TranscribeProgress(
    /** `准备模型` / `转写中` / `说话人分离` / `整理结果` / `生成纪要` / `导入中` */
    val phase: String = "",
    @SerializedName("seg_index") val segIndex: Int = 0,
    @SerializedName("seg_total") val segTotal: Int = 0,
    val percent: Int = 0,
    val detail: String = "",
    /** **epoch 秒的浮点数**，不是别处那种时间字符串。 */
    @SerializedName("updated_at") val updatedAt: Double = 0.0,
)
