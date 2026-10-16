package com.echo.contact.data

import com.echo.contact.audio.MeetingAudio
import java.io.File

/**
 * 把会议的**一段音频**取到本地缓存再播（纯 HTTP + 文件，可 JVM 单测）。
 *
 * 为什么走"下载到文件"而不是把 URL 直接丢给播放器：
 *   * 这条接口要 `Authorization: Bearer`（令牌在 `PairStore` 里），
 *     而 `MediaPlayer` 传自定义头要额外那套 API、各家实现还不一致；
 *   * 会议段可能被后端**用时解码**（flac → wav），直接流播会边下边解、更容易卡；
 *   * 落到缓存文件之后，"上一段/下一段"来回按不会重复下载。
 *
 * 缓存文件名由 [MeetingAudio.cacheName] 统一给（同一场同一段复用）。
 */
class MeetingAudioFetcher(private val api: EchoApi, private val cacheDir: File) {

    sealed interface Outcome {
        /** 缓存里已有，直接用。 */
        data class Cached(val file: File) : Outcome

        /** 这次下好了。 */
        data class Downloaded(val file: File, val bytes: Long) : Outcome

        /** 没成：一句人话（界面直接显示）。 */
        data class Failed(val message: String) : Outcome
    }

    suspend fun fetch(mid: Int, seg: Int): Outcome {
        val target = File(cacheDir, MeetingAudio.cacheName(mid, seg))
        if (target.isFile && target.length() > 0) return Outcome.Cached(target)

        val resp = try {
            api.meetingAudio(mid, seg)
        } catch (e: Exception) {
            return Outcome.Failed("取音频失败：${e.message ?: e.javaClass.simpleName}")
        }
        val body = resp.body()
        if (!resp.isSuccessful || body == null) {
            val why = when (resp.code()) {
                404 -> "这场会议没有第 $seg 段音频"
                416 -> "段号超出范围"
                401, 403 -> "令牌不对了，去面板重新配对一次"
                else -> "HTTP ${resp.code()}"
            }
            return Outcome.Failed(why)
        }
        return try {
            cacheDir.mkdirs()
            // 先写临时文件再改名：中途断网不会留下半个 wav（播放器会当成完整文件播）
            val tmp = File(cacheDir, MeetingAudio.cacheName(mid, seg) + ".part")
            tmp.outputStream().use { out -> body.byteStream().use { it.copyTo(out) } }
            if (tmp.length() <= 0) {
                tmp.delete()
                return Outcome.Failed("这段音频是空的")
            }
            if (!tmp.renameTo(target)) {
                // 有些文件系统 rename 会失败（已存在/跨挂载）：退回"复制过去再删"
                tmp.copyTo(target, overwrite = true)
                tmp.delete()
            }
            Outcome.Downloaded(target, target.length())
        } catch (e: Exception) {
            Outcome.Failed("存音频失败：${e.message ?: e.javaClass.simpleName}")
        }
    }

    /** 清掉这场会议的音频缓存（换会议时顺手清）。 */
    fun clear(mid: Int) {
        runCatching {
            cacheDir.listFiles { f -> f.name.startsWith("meeting-$mid-seg") }
                ?.forEach { it.delete() }
        }
    }
}
