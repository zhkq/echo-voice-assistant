package com.echo.contact.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import com.echo.contact.MainActivity
import com.echo.contact.R

/**
 * 「电脑上发生了什么」→ 手机通知。
 *
 * 用户 2026-10-15 的口径是"**手机收**"：会议转写好了、纪要好了、指令有回复了，
 * 手机该自己知道，而不是让人回去翻页面。
 *
 * ## 通道的边界（如实写）
 *
 * 没有 FCM/长连接（单位网络下 FCM 不可靠，而中继上再开一条流不值当），所以事件只在
 * **App 打开时**、或**常驻听写开着时**到达。App 关着且听写没开 → **收不到** ——
 * 这一点必须写在设置页上，不能让人以为"随时随地都会响"。
 *
 * [summarize] 是**纯函数**（JVM 可测）：哪种事件值得打扰人、说什么话，全在这里。
 */
object EventNotify {

    const val CHANNEL_ID = "echo_events"

    /** 事件 → 一句通知。返回 null = 这类事件**不值得打扰**（例如 command_sent）。 */
    fun summarize(type: String, payload: Map<String, Any?> = emptyMap()): Pair<String, String>? {
        val meeting = str(payload["meeting"]) ?: str(payload["name"])
        return when (type) {
            "meeting_transcribed" -> "会议转写好了" to
                (meeting?.let { "$it 的正文可以看了；纪要在随后几十秒内出来" } ?: "正文可以看了")
            "meeting_summary_ready" -> "会议纪要好了" to (meeting ?: "可以去会议页看")
            "meeting_stopped" -> "录音已结束" to
                (meeting?.let { "$it 正在转写（完了会再提醒一次）" } ?: "正在转写")
            "meeting_imported" -> "录音已导入" to (meeting ?: "正在转写")
            "command_done" -> "指令有结果了" to "回复已到，打开 App 就能听到"
            "review_ready" -> "今天的回顾好了" to "播报已就绪"
            else -> null                       // 其余（command_received/command_sent…）不打扰
        }
    }

    private fun str(v: Any?): String? = (v as? String)?.takeIf { it.isNotBlank() }

    /** 发一条事件通知（点一下回 App）。**权限没给或通道建不起来时静默返回** —— 通知不是主链路。 */
    fun post(context: Context, title: String, body: String, id: Int = 0) {
        try {
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
                ?: return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val ch = NotificationChannel(
                    CHANNEL_ID, "ECHO 事件", NotificationManager.IMPORTANCE_DEFAULT,
                )
                ch.description = "会议转写/纪要好了、指令有回复了"
                nm.createNotificationChannel(ch)
            }
            val intent = Intent(context, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
            val pi = PendingIntent.getActivity(context, 0, intent, flags)
            val n = NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setContentIntent(pi)
                .build()
            nm.notify(id, n)
        } catch (e: Exception) {
            // 通知发不出去不影响任何主链路
        }
    }
}
