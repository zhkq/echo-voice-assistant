package com.echo.contact.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.data.dto.MeetingStatus
import com.echo.contact.data.dto.StatusComponent
import com.echo.contact.data.dto.StatusResponse
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay

/** 自动刷新节拍：与面板的 `panelAutoRefresh` 默认值一致（3 秒）。 */
private const val AUTO_REFRESH_MS = 3_000L

/**
 * 仪表盘：整机状态。内容与配色照 `web/` 面板（`web/app.css` 的 `:root` 调色板 +
 * `.dot/.badge` 状态色），数据来自 `GET /api/status`（9 个组件 + agent/meeting/stt/busy/uptime）。
 */
@Composable
fun DashboardTab(vm: MainViewModel, onUnpair: () -> Unit) {
    val status by vm.status.collectAsState()
    val connected by vm.connected.collectAsState()
    val statusError by vm.statusError.collectAsState()
    val commands by vm.commands.collectAsState()
    val review by vm.review.collectAsState()
    val reviewSummary by vm.reviewSummary.collectAsState()

    // 面板的仪表盘是**自动刷新**的（`panelAutoRefresh` 默认 3 秒）。这里对齐：
    // 只在这个页签可见时跑（LaunchedEffect 离开组合就取消），而且只拉状态一条。
    LaunchedEffect(Unit) {
        while (true) {
            delay(AUTO_REFRESH_MS)
            vm.autoRefreshTick()
        }
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        if (connected == false) {
            item { ErrorCard(statusError ?: "连不上后端") }
        }

        // 面板底部那条「运行状态条」的三格：服务 / 智能体 / 语音合成
        item { StatusStrip(status) }

        item { DashboardCard(status) }

        status?.agent?.let { agent ->
            item {
                AgentCard(
                    agentName = agent.shortName.ifBlank { agent.name },
                    detail = agent.detail,
                    online = agent.online,
                )
            }
        }

        item {
            MeetingBriefCard(
                meeting = status?.meeting,
                sttDevice = status?.stt?.device,
                sttCount = status?.stt?.loaded?.size ?: 0,
            )
        }

        item { ReviewBriefCard(review, reviewSummary) }

        item { Text("最近指令", style = MaterialTheme.typography.titleMedium) }

        if (commands.isEmpty()) {
            item { MutedLine("还没有指令记录。") }
        } else {
            items(commands.take(5), key = { it.id ?: it.hashCode() }) { c ->
                CommandRow(c, onSpeak = { vm.speak(it) })
            }
        }

        item {
            TextButton(onClick = {
                vm.clearSession()
                onUnpair()
            }) {
                Text("重新配对")
            }
        }
    }
}

/** 三格状态条（照面板底部那条）：灯 + 名字 / 短值。 */
@Composable
private fun StatusStrip(status: StatusResponse?) {
    val server = status?.components?.firstOrNull { it.name == "server" }
    val tts = status?.components?.firstOrNull { it.name == "tts" }
    val agent = status?.agent

    EchoCard {
        Row(modifier = Modifier.fillMaxWidth()) {
            StripCell(
                modifier = Modifier.weight(1f),
                name = "服务",
                value = server?.let { shortStatus(it.status) } ?: "—",
                color = server?.let { EchoPalette.statusColor(it.status) } ?: EchoPalette.Muted,
            )
            StripCell(
                modifier = Modifier.weight(1f),
                name = "智能体",
                value = agent?.shortName?.takeIf { it.isNotBlank() }
                    ?: agent?.displayName?.takeIf { it.isNotBlank() }
                    ?: "—",
                color = if (agent?.online == true) EchoPalette.Green else EchoPalette.Muted,
            )
            StripCell(
                modifier = Modifier.weight(1f),
                name = "语音合成",
                value = tts?.detail?.substringBefore(" · ")?.takeIf { it.isNotBlank() }
                    ?: tts?.let { shortStatus(it.status) }
                    ?: "—",
                color = tts?.let { EchoPalette.statusColor(it.status) } ?: EchoPalette.Muted,
            )
        }
    }
}

@Composable
private fun StripCell(modifier: Modifier, name: String, value: String, color: androidx.compose.ui.graphics.Color) {
    Column(modifier = modifier) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusDot(color, size = 8)
            Spacer(Modifier.size(6.dp))
            MutedLine(name, maxLines = 1)
        }
        Text(
            value,
            style = MaterialTheme.typography.labelLarge,
            color = color,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun DashboardCard(status: StatusResponse?) {
    EchoCard {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(if (status != null) EchoPalette.Green else EchoPalette.Muted)
                Spacer(Modifier.size(8.dp))
                Text(
                    if (status != null) "ECHO ${status.version}" else "ECHO",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
                Spacer(Modifier.weight(1f))
                if (status != null && status.uptimeText().isNotBlank()) {
                    MutedLine("已运行 ${status.uptimeText()}", maxLines = 1)
                }
            }

            if (status == null) {
                Spacer(Modifier.height(10.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp))
                    Spacer(Modifier.size(8.dp))
                    Text("正在读取状态…", style = MaterialTheme.typography.bodySmall)
                }
                return@Column
            }

            if (status.busy) {
                Spacer(Modifier.height(6.dp))
                Text(
                    "正在执行：" + listOfNotNull(
                        status.busyOwner?.takeIf { it.isNotBlank() },
                        status.busyPhase?.takeIf { it.isNotBlank() },
                    ).joinToString(" · "),
                    style = MaterialTheme.typography.bodySmall,
                    color = EchoPalette.Purple,
                )
            }

            status.startupNotes.forEach { note ->
                Spacer(Modifier.height(6.dp))
                MutedLine(note, color = EchoPalette.Yellow)
            }

            if (status.components.isNotEmpty()) {
                Spacer(Modifier.height(10.dp))
                HorizontalDivider(color = EchoPalette.Border)
                Spacer(Modifier.height(6.dp))
                status.components.forEach { c -> ComponentRow(c) }
            }
        }
    }
}

@Composable
private fun ComponentRow(c: StatusComponent) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        StatusDot(EchoPalette.statusColor(c.status), size = 8)
        Spacer(Modifier.size(8.dp))
        Text(
            componentLabel(c.name),
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.weight(1f),
        )
        Text(
            c.detail.ifBlank { shortStatus(c.status) },
            style = MaterialTheme.typography.labelSmall,
            color = EchoPalette.Muted,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1.5f),
        )
    }
}

@Composable
private fun AgentCard(agentName: String, detail: String, online: Boolean) {
    EchoCard {
        Column {
            CardHeader(
                title = "智能体",
                dotColor = if (online) EchoPalette.Green else EchoPalette.Muted,
                trailing = agentName,
                trailingColor = EchoPalette.Accent,
            )
            if (detail.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                MutedLine(detail)
            }
        }
    }
}

/** 仪表盘上的会议**摘要**（完整操作在「会议」页）。 */
@Composable
private fun MeetingBriefCard(meeting: MeetingStatus?, sttDevice: String?, sttCount: Int) {
    val active = meeting?.active == true
    val hasError = !meeting?.error.isNullOrBlank()
    // 面板口径：**红只留给故障**，录音中是"进行中"所以用紫
    val dot = when {
        hasError -> EchoPalette.Red
        active -> EchoPalette.Purple
        else -> EchoPalette.Muted
    }
    EchoCard {
        Column {
            CardHeader(
                title = "会议",
                dotColor = dot,
                trailing = if (active) "录音中" else "未在录音",
                trailingColor = dot,
            )
            if (active) {
                meeting?.folder?.takeIf { it.isNotBlank() }?.let {
                    Spacer(Modifier.height(4.dp))
                    MutedLine(it, maxLines = 1)
                }
            }
            if (hasError) {
                Spacer(Modifier.height(4.dp))
                MutedLine(meeting!!.error, color = EchoPalette.Red)
            }
            if (!sttDevice.isNullOrBlank()) {
                Spacer(Modifier.height(4.dp))
                MutedLine(
                    "识别设备 $sttDevice" +
                        if (sttCount > 0) " · 已加载 $sttCount 个模型" else " · 未加载模型"
                )
            }
        }
    }
}

/** 仪表盘上的回顾**摘要**（完整操作在「回顾」页）。 */
@Composable
private fun ReviewBriefCard(review: com.echo.contact.data.dto.DailyReviewState?, summary: com.echo.contact.data.dto.DailyReviewSummary?) {
    val running = review?.running == true
    val enabled = review?.enabled == true
    val ready = review?.ready == true
    val turns = summary?.todayTurns ?: 0
    val (label, color) = when {
        running -> "进行中" to EchoPalette.Purple
        !enabled -> "未启用" to EchoPalette.Muted
        !ready -> "未就绪" to EchoPalette.Yellow
        turns > 0 -> "今天已回顾 $turns 轮" to EchoPalette.Green
        else -> "今天还没回顾" to EchoPalette.Muted
    }
    EchoCard {
        Column {
            CardHeader(title = "每日回顾", dotColor = color, trailing = label, trailingColor = color)
            summary?.lastBrief?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(6.dp))
                MutedLine((summary.lastAt?.plus("　") ?: "") + it, maxLines = 3)
            }
        }
    }
}

@Composable
internal fun ErrorCard(message: String) {
    EchoCard(container = EchoPalette.ErrorContainer) {
        Column {
            CardHeader(title = "连不上 ECHO", dotColor = EchoPalette.Red)
            Spacer(Modifier.height(4.dp))
            Text(message, style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.height(4.dp))
            MutedLine("检查：手机和电脑同一 Wi-Fi？电脑防火墙放行了 8970？")
        }
    }
}
