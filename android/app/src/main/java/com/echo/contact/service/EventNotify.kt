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
 *
 * ## 点通知要落到"那一场会议"（2026-10-16 补）
 *
 * 之前点开只回到首页，用户还得自己去会议列表里翻。现在"跟某场会议有关"的事件会把
 * 会议名塞进 intent 的 [EXTRA_MEETING]；`MainActivity` 读出来交给 `MainViewModel`
 * 去打开那场详情（找不到就如实提示，见 `MainViewModel.openMeetingByName`）。
 *
 * **为什么不是把会议名当参数传进来**：两条发送路 —— `MainViewModel.notifyEvent`
 * 手里有 payload，而 `ListenService.eventLoop`（后台那条）只拿到
 * `summarize` 的**标题+正文**。所以会议名在 [summarize] 里顺手登记到
 * [rememberedMeeting]（有界的小表），[post] 按同一句正文取回来。
 * 这样**不用改 ListenService**，两条路都能带上目标；查不到就退回原来的行为
 * （只开首页），不会比以前更坏。
 */
object EventNotify {

    const val CHANNEL_ID = "echo_events"

    /** 通知 intent 里"要打开哪场会议"的额外键（值是会议名）。 */
    const val EXTRA_MEETING = "echo_open_meeting"

    /**
     * 「哪个事件跟某场会议有关」的映射（**纯函数**，单测钉住）。
     *
     * 返回 null = 这条事件**不指向具体会议**（例如 `command_done` 是"回话好了"，
     * 与会议无关）—— 那就该按老样子只开首页，别硬塞一个空目标。
     *
     * 为什么不带会议 id：事件 payload 里给的是**名字**（`meeting` / `name`），
     * 而"哪一场"最终要按列表里的**名字**去认（id 在手机侧不是权威来源）。
     */
    fun meetingTargetOf(type: String, payload: Map<String, Any?> = emptyMap()): Pair<String, String>? {
        // 只认这几型：正文/纪要/停止/导入都是"那一场会议的事"；
        // command_* / review_ready / 唤醒这类与会议无关，硬跳过去反而找不着北。
        val related = when (type) {
            "meeting_transcribed", "meeting_summary_ready", "meeting_stopped", "meeting_imported" -> true
            else -> false
        }
        if (!related) return null
        val meeting = str(payload["meeting"]) ?: str(payload["name"]) ?: return null
        return EXTRA_MEETING to meeting
    }

    /**
     * 正文 → 会议名的小表（[post] 用它把目标补回 intent）。
     *
     * 有界（最多 [REMEMBER_MAX] 条，按插入顺序丢最旧的）：这是一个**长期活着的进程**
     * （常驻听写），无界表会一直长。查不到时的后果只是"点通知回首页"，
     * 与这条功能之前的行为一样，可以接受。
     *
     * **加锁**：两条订阅路（`MainViewModel` 的前台节拍、`ListenService` 的后台长轮询）
     * 都可能碰它，而 `LinkedHashMap` 不是线程安全的（并发改会读到"填到一半"的结构）。
     * 本仓库在"共享可变结构不加锁"上栽过一次（设置缓存的并发 bug），所以这里从一开始就锁住。
     */
    private val rememberedMeeting = LinkedHashMap<String, String>()
    private val meetingLock = Any()

    private const val REMEMBER_MAX = 40

    private fun remember(body: String, meeting: String) {
        synchronized(meetingLock) {
            rememberedMeeting.remove(body)
            rememberedMeeting[body] = meeting
            while (rememberedMeeting.size > REMEMBER_MAX) {
                val oldest = rememberedMeeting.keys.firstOrNull() ?: break
                rememberedMeeting.remove(oldest)
            }
        }
    }

    /** 测试与排障用：看看某句正文记住了哪场会议。 */
    fun rememberedMeetingFor(body: String): String? = synchronized(meetingLock) { rememberedMeeting[body] }

    /** 事件 → 一句通知。返回 null = 这类事件**不值得打扰**（例如 command_sent）。 */
    fun summarize(type: String, payload: Map<String, Any?> = emptyMap()): Pair<String, String>? {
        val meeting = str(payload["meeting"]) ?: str(payload["name"])
        val text = when (type) {
            "meeting_transcribed" -> "会议转写好了" to
                (meeting?.let { "$it 的正文可以看了；纪要在随后几十秒内出来" } ?: "正文可以看了")
            "meeting_summary_ready" -> "会议纪要好了" to (meeting ?: "可以去会议页看")
            "meeting_stopped" -> "录音已结束" to
                (meeting?.let { "$it 正在转写（完了会再提醒一次）" } ?: "正在转写")
            "meeting_imported" -> "录音已导入" to (meeting ?: "正在转写")
            "command_done" -> "指令有结果了" to "回复已到，打开 App 就能听到"
            "review_ready" -> "今天的回顾好了" to "播报已就绪"
            else -> null                       // 其余（command_received/command_sent…）不打扰
        } ?: return null
        // 顺手把"这句正文是哪场会议"记下来：post 只拿得到标题+正文（ListenService 那条路
        // 就是这么调的），靠这句正文把目标找回来，点通知才能落到那场会议。
        meetingTargetOf(type, payload)?.let { remember(text.second, it.second) }
        return text
    }

    private fun str(v: Any?): String? = (v as? String)?.takeIf { it.isNotBlank() }

    /**
     * 发一条事件通知（点一下回 App，会议类**直达那场会议**）。
     * **权限没给或通道建不起来时静默返回** —— 通知不是主链路。
     *
     * ⚠️ 这里**没有**加「听最新一条」动作按钮（任务里说"除非简单到不引入新状态"）：
     * 那条路要重新读"最新一条指令回复"，等于把 `MainViewModel` 的
     * `loadCommands` + 选择逻辑在通知侧再实现一遍，还多一份"念哪一条"的状态
     * （而回复可能已经被念过、或用户已经把历史翻走了）。收益是省一次点击，
     * 代价是一份会漂的重复逻辑 —— 不做，如实说明。
     *
     * 会议目标**不必**从参数传：`summarize` 已经把"这句正文是哪场会议"登记好了
     * （[rememberedMeeting]），所以 `ListenService` 那条只拿得到标题+正文的路
     * 也能带上目标，这边不用改 ListenService。
     */
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
                // "跟某场会议有关"的通知带上目标：点一下直达那场会议详情，而不是回首页再翻。
                // 目标从正文反查（见 rememberedMeeting 的说明）；查不到就不加这一项 ——
                // 点开仍然能到首页，与这条功能之前的行为一致。
                rememberedMeetingFor(body)?.let { putExtra(EXTRA_MEETING, it) }
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
            // 目标只对**这次投递**有效：Extra 不能留在 intent 里被下一次/另一个实例复用
            // （复用会导致"没点通知却也跳会议"）。PendingIntent 已经拿到自己的副本了。
            intent.removeExtra(EXTRA_MEETING)
        } catch (e: Exception) {
            // 通知发不出去不影响任何主链路
        }
    }
}
