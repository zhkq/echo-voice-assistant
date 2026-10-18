package com.echo.contact.data.dto

import com.google.gson.annotations.SerializedName

/**
 * `GET /api/status` 的响应（手机仪表盘的数据源）。
 *
 * 字段名是**照着真后端实测出来的**（2026-10-09 在跑着的 dev 实例上抓的），不是猜的：
 * 顶层 `components / dsh / agent / stt / meeting / busy / busyOwner / busyPhase /
 * startupNotes / uptime / version`。
 *
 * 实测那台机上的 9 个组件：
 * `server(online) dsh(skipped) harness(online) stt(ready) tts(online)
 *  wake(disabled) hotkey(online) meeting(online) diarize(online)` ——
 * 也就是说 `status` 会出现 `online / ready / skipped / disabled` 这几种值，
 * 界面必须都认（别只认 online，其余全画成"故障"）。
 */

/** 一个组件：`{name, status, detail, pid, updated_at}`。 */
data class StatusComponent(
    val name: String = "",
    val status: String = "",
    val detail: String = "",
    val pid: Int = 0,
    @SerializedName("updated_at") val updatedAt: String = "",
)

/** `dsh`：`{online, skipped}`。 */
data class DshStatus(
    val online: Boolean = false,
    val skipped: Boolean = false,
)

/** `agent`：当前选中的智能体后端。 */
data class AgentStatus(
    val name: String = "",
    @SerializedName("displayName") val displayName: String = "",
    @SerializedName("shortName") val shortName: String = "",
    val vendor: String = "",
    val status: String = "",
    val detail: String = "",
    val online: Boolean = false,
)

/** `stt.loaded` 的一条：`{key, engine, model}`（app/audio/stt.py；面板设置页也显示这个）。 */
data class SttLoadedModel(
    val key: String = "",
    val engine: String = "",
    val model: String = "",
)

/** `stt`：`{loaded, device, cuda}`。 */
data class SttStatus(
    val loaded: List<SttLoadedModel> = emptyList(),
    val device: String = "",
    val cuda: Boolean = false,
)

/** `meeting`：`{active, folder, startedAt, level, error}`。 */
data class MeetingStatus(
    val active: Boolean = false,
    val folder: String? = null,
    @SerializedName("startedAt") val startedAt: String? = null,
    val level: Double = 0.0,
    val error: String = "",
    /** 已暂停（会议中途按了「静音」）。 */
    val paused: Boolean = false,
    /** 已经写出来的段数（全屏录音界面显示进度用）。 */
    val segments: Int = 0,
)

/** `GET /api/status` 整体。 */
data class StatusResponse(
    val components: List<StatusComponent> = emptyList(),
    val dsh: DshStatus? = null,
    val agent: AgentStatus? = null,
    val stt: SttStatus? = null,
    val meeting: MeetingStatus? = null,
    val busy: Boolean = false,
    @SerializedName("busyOwner") val busyOwner: String? = null,
    @SerializedName("busyPhase") val busyPhase: String? = null,
    @SerializedName("startupNotes") val startupNotes: List<String> = emptyList(),
    /** 已运行秒数。 */
    val uptime: Int = 0,
    val version: String = "",
    /**
     * 「经中继打开 DSH 标准版网页」的完整地址（**服务端给，App 不拼、不写死**）。
     *
     * 契约见 `app/api.py::api_status` + `app/relay.py::web_entry()`
     * （2026-10-18 与 PC 那条线对齐的字段名：`dshWebUrl` / `dshWebReason` / `dshWebReadOnly`）：
     * 地址只在"真的能打开"时非空（中继已连 + 有 publicUrl + DSH 标准版在跑），
     * 否则**空串**并附一句原因 —— 界面照 `DshWebEntrySpec` 的规矩把它显示成
     * "置灰 + 服务端说的那句原因"，**不会**自己拼地址。
     *
     * 兼容几个别名：服务端那条线如果最终叫 `dsh_web_url` / `relayDshUrl` / `dshWebPage`，
     * Gson 也能读进来（`alternate` 只在**反序列化**时生效，正是这里需要的）。
     */
    @SerializedName(
        value = "dshWebUrl",
        alternate = ["dsh_web_url", "relayDshUrl", "dshWebPage"],
    )
    val dshWebUrl: String? = null,
    /** `dshWebUrl` 为空时的原因（服务端的人话："中继没打开" / "harness 没在运行" …）。 */
    @SerializedName(value = "dshWebReason", alternate = ["dsh_web_reason"])
    val dshWebReason: String? = null,
    /** 这条网页会话是不是**只读**（现行恒为 `true`；缺省按 `true` 理解）。 */
    @SerializedName(value = "dshWebReadOnly", alternate = ["dsh_web_read_only"])
    val dshWebReadOnly: Boolean? = null,
) {
    /** 把秒数说成人话。 */
    fun uptimeText(): String {
        val s = uptime
        return when {
            s <= 0 -> ""
            s < 60 -> "${s} 秒"
            s < 3600 -> "${s / 60} 分 ${s % 60} 秒"
            s < 86400 -> "${s / 3600} 小时 ${(s % 3600) / 60} 分"
            else -> "${s / 86400} 天 ${(s % 86400) / 3600} 小时"
        }
    }
}
