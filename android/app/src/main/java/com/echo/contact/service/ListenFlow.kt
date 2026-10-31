package com.echo.contact.service

import com.echo.contact.data.EchoApi
import com.echo.contact.data.dto.ApiErrorResponse
import com.google.gson.Gson
import kotlinx.coroutines.delay
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import retrofit2.Response

/**
 * 「一段语音发出去之后，结果从哪来」——**纯编排，不碰 Android API**，
 * 于是能用 MockWebServer 在 JVM 里把每条失败路径都钉住（`ListenFlowTest`）。
 *
 * 为什么值得单独抽：常驻听写没有界面，用户在**车里**只听到"它有没有回话"。
 * 这一段的每个分支都对应一句必须说对的话：
 *   * 后端 **409/503（自己上一条还没跑完 / 后端忙）** 是"忙"，不是"没听清"也不是"坏了"——
 *     2026-10-31 起转写全在后端，指令与会议/回顾互斥，这条会经常出现；
 *   * 后端 422（没识别出文字）≠ 502（转写失败）≠ 连不上 —— 三种原因、三种处理；
 *   * 智能体的回答是**异步**落到 `commands` 表的，要轮询；**念哪一段由 `SpeakPlan` 一处判**
 *     （电脑给的 `speakText` 优先，老电脑只念开头摘要，电脑发起的任务一个字都不念）——
 *     这里**不再**自己从 `brief`/`reply` 里挑，见那边"单一出处"的说明；
 *   * 超时要说清"可能还在跑"，不能让人以为它坏了。
 *
 * `sleep` 可注入是为了测试能瞬间跑完超时分支（真机上就是 `delay`）。
 */
class ListenFlow(
    private val api: EchoApi,
    private val pollMs: Long = DEFAULT_POLL_MS,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
) {

    sealed interface Outcome {
        /** 后端听懂了并受理（`text` = 它听成什么，给通知/界面显示）。 */
        data class Heard(val text: String, val commandId: Int?) : Outcome

        /** 拿到了要念出来的那句话。 */
        data class Reply(val text: String) : Outcome

        /**
         * 有结论，但按规则**一个字都不念**（电脑发起的任务 / 没有可念的摘要）。
         *
         * 这不是失败：那条指令照常是 `done`，只是没有声音。所以它必须与 [Failed] 分开 ——
         * 混进 [Failed] 会让用户以为"ECHO 出错了"，而其实只是"这条不该在手机上念"。
         * 原因写在 [why] 里，由调用方原样说出去（常驻听写没有界面）。
         */
        data class Silent(val why: String) : Outcome

        /** 没成：一句可以直接显示/念的人话。 */
        data class Failed(val message: String) : Outcome

        /**
         * **忙，不是坏**：能力后端回 409（自己上一条还没跑完）或 503（后端忙/模型加载）。
         *
         * 为什么要单独一档：用户 2026-10-31 的口径是「本人已有请求在运行就报忙碌」——
         * 报成"没听清"会让车里反复重说，报成"坏了"会让人去查网络/后端，两种都是指错方向。
         * 调用方应当**念出来**（常驻听写没有界面），并继续听下一句。
         */
        data class Busy(val message: String) : Outcome
    }

    /** 从 FastAPI 的 `{"detail": "..."}` 里取人话；取不到返回空串。 */
    private fun errorDetail(resp: Response<*>): String {
        val raw = runCatching { resp.errorBody()?.string().orEmpty() }.getOrDefault("")
        if (raw.isBlank()) return ""
        return runCatching {
            Gson().fromJson(raw, ApiErrorResponse::class.java)?.detail.orEmpty()
        }.getOrDefault("")
    }

    /** 上传一段 wav（裸 body，`Content-Type: audio/wav`）。 */
    suspend fun submit(wav: ByteArray): Outcome {
        val resp = try {
            api.voiceCommand(wav.toRequestBody(WAV_MEDIA_TYPE.toMediaType()))
        } catch (e: Exception) {
            return Outcome.Failed("连不上 ECHO：${e.message ?: e.javaClass.simpleName}")
        }
        val body = resp.body()
        val code = resp.code()
        val detail = body?.message?.takeIf { it.isNotBlank() } ?: errorDetail(resp)
        // 409 = 自己上一条还没跑完（会议/回顾/上一条指令占着后端）；503 = 后端忙/模型加载中。
        // 两者都是"稍后再来"，必须与 422/502/网络 分开说。
        if (code == 409 || code == 503) {
            return Outcome.Busy(
                detail.takeIf { it.isNotBlank() }
                    ?: "我这边正忙着上一件事，等它完了再说一次。"
            )
        }
        if (!resp.isSuccessful || body?.ok != true) {
            val why = when (code) {
                // 422 一律说"没听清"（后端那句 detail 在手机上读起来像"失败了"）；
                // 502 用后端的人话（可能是"后端不可用/转写失败"），没有就退回固定文案。
                422 -> "没听清这一段（后端没识别出文字）"
                502 -> detail.takeIf { it.isNotBlank() } ?: "后端转写失败了"
                401, 403 -> detail.takeIf { it.isNotBlank() }
                    ?: "令牌不对了，去面板重新配对一次"
                else -> detail.takeIf { it.isNotBlank() } ?: "HTTP $code"
            }
            return Outcome.Failed(why)
        }
        return Outcome.Heard(body.text.orEmpty(), body.commandId)
    }

    /** 盯住刚发的那条指令，直到它有结论或超时。 */
    suspend fun awaitReply(commandId: Int?): Outcome {
        if (commandId == null) return Outcome.Failed("后端没给这条指令的编号，等不到结果")
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val item = try {
                api.listCommands(mapOf("limit" to "5")).body()
                    ?.items?.firstOrNull { it.id == commandId }
            } catch (e: Exception) {
                null                       // 单次拉取失败不算失败：下一轮再来（车里网络会抖）
            }
            if (item != null) {
                when (item.status) {
                    "done" -> {
                        // **念什么由 `SpeakPlan` 一处决定**（用户 2026-10-17 真机：手机把详情也念了）：
                        //  * 电脑给了 `speakText` → 原样念它（唯一出处，这里不再自己切正文）；
                        //  * 老电脑没给 → 只念开头那段摘要；
                        //  * `origin=pc` → 一个字都不念（谁发起谁念）。
                        // `reply` 那整篇仍然留在结果里给**眼睛**看，只是不再往耳朵里灌。
                        val speak = SpeakPlan.decide(item)
                            ?: return Outcome.Silent(SpeakPlan.silentReason(item))
                        return Outcome.Reply(speak)
                    }
                    "failed" -> return Outcome.Failed(
                        (item.error?.takeIf { it.isNotBlank() }?.let { "ECHO 那边失败了：$it" })
                            ?: "ECHO 那边失败了（没给原因）"
                    )
                }
            }
            sleep(pollMs)
        }
        return Outcome.Failed("ECHO 没在 ${timeoutMs / 1000} 秒内回话（那句可能还在跑）")
    }

    companion object {
        const val DEFAULT_POLL_MS = 1500L
        const val DEFAULT_TIMEOUT_MS = 90_000L
        private const val WAV_MEDIA_TYPE = "audio/wav"
    }
}
