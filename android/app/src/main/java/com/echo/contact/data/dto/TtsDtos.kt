package com.echo.contact.data.dto

import com.google.gson.annotations.SerializedName

/** `POST /api/tts` 的入参（app/api.py:150 `TtsIn`）。`engine` 留空 = 用 ECHO 设置里的 `ttsEngine`。 */
data class TtsRequest(
    val text: String,
    val engine: String = "",
)

/**
 * `POST /api/assistant/voice-command` 的响应（app/api.py:789-790，逐字段读过）。
 *
 * 这条**回 `commandId`**（文字那条 `/assistant/command` 不回）——
 * 所以语音这条路可以直接盯住那一条轮询结果，不用"按文本认领"。
 */
data class VoiceCommandResponse(
    val ok: Boolean = false,
    val message: String? = null,
    /** ECHO 侧转写出来的文本（手机看不到音频，这是唯一的真相来源）。 */
    val text: String? = null,
    @SerializedName("commandId") val commandId: Int? = null,
    val source: String? = null,
)
