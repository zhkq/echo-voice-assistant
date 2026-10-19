package com.echo.contact.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.echo.contact.service.MeetingDraftStore
import com.echo.contact.service.MicState
import com.echo.contact.service.OfflinePhase
import com.echo.contact.service.PendingSyncSummary
import com.echo.contact.ui.theme.EchoPalette
import java.time.LocalDateTime

/**
 * 会议页那块**常驻**的「待上传 N 段 · 现在同步」（用户 2026-10-19）。
 *
 * ## 为什么必须常驻、而且不能依赖录音页
 *
 * 用户原话：*"上午手机录的录音补传一直不成功，报 http 413"*，回家再看时
 * *"没有上传的地方了"*。414 的根因（中继上限）已经修好，可**入口**本身也有问题：
 *  * 全屏录音界面里那颗「现在同步」在结束录音之后就没了（那个页面已经收起）；
 *  * 会议页原来那块补传进度只在 `MicState.syncPhase != IDLE` 时出现，而 `MicState`
 *    是**内存**状态 —— App 重开/进程被杀之后它是空的，于是"磁盘上还有 3 段"的时候
 *    界面上**一块都不画**。那不是数字不对，是入口消失。
 *
 * 所以这一块的数据源是**磁盘上既有的离线清单**（`PendingSyncSummary`，来自
 * `MeetingDraftStore.pending()`），只要还有段没传就出现；同步动作走**既有**入口
 * `MeetingMicService.syncNow()`（→ `ACTION_SYNC` → `OfflineMeetingSync.run(FINALIZE)`），
 * 这里**一行新的同步实现都没有**。
 *
 * ## 三条"不许静默"的落点
 *
 *  1. **失败必须说人话，而且分开说**：413（段太大）、没网（连不上）、会议进行中
 *     （等电脑那场结束）、服务端拒收 —— 四档的原因与下一步完全不同，
 *     糊成一句"同步失败"会把人引去查错的地方（[syncReasonText]）；
 *  2. **同步中要有进度**：已传 M / 共 T 段、正在传第几段（[meetingSyncEntry]）；
 *  3. **没有待传就不占地方**：只在"刚同步完（DONE）"时留一句"全部已上传"，
 *     其余时候整块不画（见 [MeetingSyncEntry.visible]）。
 */

/** 那块入口的按钮动作（**三档互斥**，界面不再自己判）。 */
internal enum class MeetingSyncAction {
    /** 有待传、而且现在就能试：按钮可点。 */
    SYNC_NOW,

    /** 正在同步：按钮置灰（别让人连点，连点只会拿到"上一次还在跑"）。 */
    SYNCING,

    /** 没有待传：不给按钮（[MeetingSyncAction.NONE] 时那一块通常整块不画）。 */
    NONE,
}

/**
 * 「待上传 / 现在同步」要画成什么（**纯数据**，JVM 用例逐档钉住）。
 *
 * 界面只照着它画，不再在 Compose 里算判据 —— 那一层的错法（"没传上去却说已上传"、
 * "失败只剩一句套话"）在 JVM 里根本看不见，所以判据必须能单独验。
 */
internal data class MeetingSyncEntry(
    val visible: Boolean,
    /** 还有几段没被服务端认下（就是标题里的 N）。 */
    val pendingSegments: Int,
    /** 还有几场在等。 */
    val drafts: Int,
    /** 标题（"待上传 3 段" / "正在同步 · 已传 2/5 段（第 3 段）" / "全部已上传"）。 */
    val title: String,
    /** 同步进度那一句（空 = 现在没有进度可说，别用套话占位）。 */
    val progress: String,
    /** 人话原因（失败/等待/刚完成；空 = 没什么要说的）。 */
    val note: String,
    /** 最早那一场的开始时间（`10-19 14:03`；空 = 没有待传）。 */
    val earliestLabel: String,
    val action: MeetingSyncAction,
    val actionLabel: String,
    /** 失败档（按钮文案说"重试"而不是"现在同步"）。 */
    val failed: Boolean,
    /** ECHO 正录着电脑那场：同步会等（[OfflinePhase.WAITING_MEETING_END]）。 */
    val waitingMeetingEnd: Boolean,
) {
    val busy: Boolean get() = action == MeetingSyncAction.SYNCING
}

/**
 * 从"磁盘上的待传摘要" + "内存里的同步状态"算出那块入口要画什么（纯函数）。
 *
 * 两边的分工是刻意的：
 *  * **有没有待传、一共几段**看磁盘（[PendingSyncSummary]）—— 进程重启后仍然是真的；
 *  * **正在传第几段、上次为什么失败**看内存（[MicState]）—— 那是这一轮的事实。
 *
 * 待传段数取两者**较大值**：磁盘摘要每 3 秒才刷一次，而 `MicState` 是同步当场更新的；
 * 取小值会在"刚封完一段"的瞬间先掉到旧数（看起来像"传完了"）。
 */
internal fun meetingSyncEntry(
    pending: PendingSyncSummary,
    mic: MicState,
): MeetingSyncEntry {
    val pendingSegments = maxOf(pending.pendingSegments, mic.syncPending)
    val drafts = maxOf(pending.drafts, mic.syncDrafts)
    // 总数取三者较大值：本轮进度（同步自己知道要传几段）、磁盘摘要、以及"已传 + 待传"。
    // 只看磁盘摘要不行 —— 它每 3 秒才刷一次，同步开始的头几秒会显示旧分母。
    val total = maxOf(pending.totalSegments, mic.syncTotal, mic.syncConfirmed + pendingSegments)
    val confirmed = mic.syncConfirmed.coerceAtMost(total)
    val current = (confirmed + 1).coerceAtMost(maxOf(total, 1))

    val busy = mic.syncPhase == OfflinePhase.UPLOADING
    val waiting = mic.syncPhase == OfflinePhase.WAITING_MEETING_END
    val failed = mic.syncPhase == OfflinePhase.FAILED ||
        mic.syncPhase == OfflinePhase.UNREACHABLE ||
        mic.syncPhase == OfflinePhase.WAITING_UPLOAD
    val done = mic.syncPhase == OfflinePhase.DONE && mic.syncNote.isNotBlank()

    // 没有待传就**整块不画**；唯一例外是"刚刚同步完"那一句（用户要的确认感，一行字）。
    val visible = pendingSegments > 0 || done

    val title = when {
        busy && total > 0 -> "正在同步 · 已传 $confirmed/$total 段（第 $current 段）"
        busy -> "正在同步…"
        waiting && pendingSegments > 0 -> "待上传 $pendingSegments 段 · 等电脑那场结束"
        pendingSegments > 0 -> "待上传 $pendingSegments 段"
        else -> "全部已上传"
    }

    val progress = when {
        busy && total > 0 -> "已传 $confirmed / 共 $total 段；正在传第 $current 段"
        pendingSegments > 0 && mic.syncConfirmed > 0 -> "已传 ${mic.syncConfirmed} 段，还剩 $pendingSegments 段"
        else -> ""
    }

    val note = when {
        waiting -> "电脑上那场会还在录：等它结束就会接着传（不会抢占它的录音）"
        busy -> "这一段传完会接着传后面的；App 关掉音频也不会丢，下次接着传"
        failed -> syncReasonText(mic.syncPhase, mic.syncNote, pendingSegments)
        done -> mic.syncNote
        // 还在录的那一场：段可以照传，但**收尾要等用户按结束** —— 不说这句，
        // 用户会以为"传完了怎么还不出文字"（见 `OfflineMeetingSync` 的降级判据）。
        pending.recordingDrafts > 0 ->
            "其中 ${pending.recordingDrafts} 场还在录：只会传已经录好的段，" +
                "收尾（开始转写）要等你按结束"
        else -> ""
    }

    val action = when {
        pendingSegments <= 0 -> MeetingSyncAction.NONE
        busy -> MeetingSyncAction.SYNCING
        else -> MeetingSyncAction.SYNC_NOW
    }
    val actionLabel = when {
        busy -> "同步中…"
        failed -> "重试同步"
        else -> "现在同步"
    }

    return MeetingSyncEntry(
        visible = visible,
        pendingSegments = pendingSegments,
        drafts = drafts,
        title = title,
        progress = progress,
        note = note,
        earliestLabel = meetingStartLabel(pending.earliestStartedAt),
        action = action,
        actionLabel = actionLabel,
        failed = failed,
        waitingMeetingEnd = waiting,
    )
}

/**
 * 失败原因 → 人话（**四档必须分开**；纯函数）。
 *
 * 为什么不能直接甩 `syncNote`：那一句里混着服务端的原文（"HTTP 413，服务端拒绝"），
 * 用户读完仍然不知道该做什么。而四档的"下一步"完全不同：
 *  * 413 = 这一段体积超了（音频**没丢**，换/升级中继或分段重传）；
 *  * 没网 = 等有网（音频都在手机里）；
 *  * 会议进行中 = 等电脑那场结束（**不是错误**，步调问题）；
 *  * 服务端拒收 = 契约/权限没对齐（重试也未必好，但要把原话带出来）。
 *
 * ⚠️ 任何时候都**不许返回空串**（只要还有待传）：沉默会让用户以为"在传"或"已经好了"。
 */
internal fun syncReasonText(
    phase: OfflinePhase,
    note: String,
    pendingSegments: Int,
): String {
    val raw = note.trim()
    if (phase == OfflinePhase.WAITING_MEETING_END) {
        return "电脑上那场会还在录：等它结束就会接着传（不抢占它的录音）"
    }
    // 先判 413：那一句里同时带着"服务端拒绝"的字样，按下一档走会说歪原因
    if (raw.contains("413")) {
        return "这一段太大，被中继/服务器拒收（HTTP 413）：音频还在手机里，可以再传一次"
    }
    if (phase == OfflinePhase.UNREACHABLE || raw.contains("连不上") || raw.contains("探活")) {
        return "没网 / 连不上 ECHO：音频都在手机里，有网再点「重试同步」"
    }
    if (raw.contains("HTTP 4") || raw.contains("服务端拒")) {
        return "服务器拒收：$raw"
    }
    if (raw.isNotBlank()) return raw
    return if (pendingSegments > 0) {
        "还没传上去（这次没拿到原因）：点「重试同步」再试一次"
    } else {
        ""
    }
}

/**
 * 清单里的开始时间（`yyyy-MM-dd'T'HH:mm:ss`）→ 界面上那一行 `10-19 14:03`。
 *
 * 读不懂就**原样显示**（不编"今天"、也不显示 1970）：这一行是给人对账用的，
 * 编出来的时间比没有更糟。
 */
internal fun meetingStartLabel(iso: String): String {
    val s = iso.trim()
    if (s.isEmpty()) return ""
    return try {
        val t = LocalDateTime.parse(s, MeetingDraftStore.ISO)
        "%02d-%02d %02d:%02d".format(t.monthValue, t.dayOfMonth, t.hour, t.minute)
    } catch (e: Exception) {
        s
    }
}

/**
 * 点那块入口时**真的**调到既有同步入口（用例可打桩：传一个记调用的 lambda 进来）。
 *
 * 返回 true = 这一次确实发起了同步；false = 按现在的状态本来就不该发
 * （没有待传 / 正在同步）。把"该不该发"和"发到哪"分开，是为了让用例能断言
 * "点了会调"而不必去渲染 Compose。
 */
internal fun dispatchMeetingSync(action: MeetingSyncAction, syncNow: () -> Unit): Boolean {
    if (action != MeetingSyncAction.SYNC_NOW) return false
    syncNow()
    return true
}

/**
 * 会议页里那块常驻入口（**画什么全由 [meetingSyncEntry] 决定**）。
 *
 * [onSync] 由调用方接上既有入口（`MeetingMicService.syncNow(context)`）——
 * 这里不认识 Android、也不认识服务，只负责"按数据画 + 点了走 [dispatchMeetingSync]"。
 */
@Composable
internal fun MeetingSyncBlock(
    pending: PendingSyncSummary,
    mic: MicState,
    onSync: () -> Unit,
) {
    val entry = meetingSyncEntry(pending, mic)
    if (!entry.visible) return
    Spacer(Modifier.height(6.dp))
    EchoCard(container = EchoPalette.Card2) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    entry.title,
                    style = MaterialTheme.typography.titleSmall,
                    color = if (entry.busy) EchoPalette.Purple else EchoPalette.Muted,
                    modifier = Modifier.weight(1f),
                )
                if (entry.pendingSegments > 0 && entry.drafts > 0) {
                    Text(
                        "${entry.drafts} 场",
                        style = MaterialTheme.typography.labelSmall,
                        color = EchoPalette.Muted,
                    )
                }
            }
            if (entry.earliestLabel.isNotBlank() && entry.pendingSegments > 0) {
                Spacer(Modifier.height(2.dp))
                MutedLine("最早一场：${entry.earliestLabel}", maxLines = 1)
            }
            if (entry.progress.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                MutedLine(entry.progress, color = EchoPalette.Purple, maxLines = 2)
            }
            if (entry.note.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                MutedLine(
                    entry.note,
                    color = if (entry.failed) EchoPalette.Red else EchoPalette.Yellow,
                    maxLines = 3,
                )
            }
            if (mic.formatNote.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                MutedLine(mic.formatNote, color = EchoPalette.Muted, maxLines = 2)
            }
            if (entry.action != MeetingSyncAction.NONE) {
                Spacer(Modifier.height(6.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = { dispatchMeetingSync(entry.action, onSync) },
                        enabled = entry.action == MeetingSyncAction.SYNC_NOW,
                        colors = ButtonDefaults.buttonColors(
                            containerColor = if (entry.failed) EchoPalette.Red else EchoPalette.Accent,
                        ),
                    ) { Text(entry.actionLabel) }
                }
            }
        }
    }
}
