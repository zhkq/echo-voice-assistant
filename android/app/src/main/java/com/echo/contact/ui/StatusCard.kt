package com.echo.contact.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay

/** 状态卡自动刷新的节拍：面板底部那条是 5 秒，这里对齐（只在卡片可见时跑）。 */
private const val STATUS_TICK_MS = 5_000L

/**
 * **服务状态**—— 用户 2026-10-15 定的三类主要功能之一（另两类是核心功能入口、资料展示）。
 *
 * 形态照面板底部那条"运行情况"：**一排状态灯 + 短值**，平时只占一张小卡；
 * 点一下展开成组件清单（9 个组件 + 版本 + 已运行时长）。
 *
 * 为什么不做成"设置 → 关于"里的一堆文字（上一版就是那样）：状态是**天天要看**的东西，
 * 而设置是偶尔改一次的；把它埋在设置里，等于每次都得点两层才看得见。
 *
 * 为什么"连不上"也在这张卡里：它就是这条链路的第 0 个状态（离线时其它灯都没意义），
 * 单独再冒一张红卡只会让首页更碎。
 */
@Composable
fun EchoStatusCard(vm: MainViewModel) {
    val status by vm.status.collectAsState()
    val connected by vm.connected.collectAsState()
    val statusError by vm.statusError.collectAsState()
    val review by vm.review.collectAsState()
    val reviewSummary by vm.reviewSummary.collectAsState()
    var expanded by remember { mutableStateOf(false) }

    // 只在卡片可见时按节拍取状态（离开组合这个 LaunchedEffect 就取消）
    LaunchedEffect(Unit) {
        while (true) {
            delay(STATUS_TICK_MS)
            vm.autoRefreshTick()
        }
    }

    val server = status?.components?.firstOrNull { it.name == "server" }
    val tts = status?.components?.firstOrNull { it.name == "tts" }
    val agent = status?.agent
    val meeting = status?.meeting
    val reviewState = review
    val reviewLabel = when {
        reviewState?.running == true -> "进行中"
        reviewState?.enabled == false -> "未启用"
        reviewState?.ready == false -> "未就绪"
        (reviewSummary?.todayTurns ?: 0) > 0 -> "今天已回顾 ${reviewSummary?.todayTurns} 轮"
        else -> "今天还没回顾"
    }
    val reviewColor = when {
        reviewState?.running == true -> EchoPalette.Purple
        reviewState?.enabled == false -> EchoPalette.Muted
        reviewState?.ready == false -> EchoPalette.Yellow
        (reviewSummary?.todayTurns ?: 0) > 0 -> EchoPalette.Green
        else -> EchoPalette.Muted
    }

    EchoCard {
        Column {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { expanded = !expanded },
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "服务状态",
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier.weight(1f),
                )
                if (status != null) {
                    MutedLine("ECHO ${status!!.version}", maxLines = 1)
                    Spacer(Modifier.size(6.dp))
                }
                Icon(
                    if (expanded) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown,
                    contentDescription = if (expanded) "收起" else "展开",
                    tint = EchoPalette.Muted,
                    modifier = Modifier.size(20.dp),
                )
            }

            Spacer(Modifier.height(8.dp))

            // 第一排：服务 / 智能体 / 语音合成（与面板底部那条同一口径）
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                StatusCell(
                    modifier = Modifier.weight(1f),
                    name = "服务",
                    value = server?.let { shortStatus(it.status) } ?: "—",
                    color = server?.let { EchoPalette.statusColor(it.status) } ?: EchoPalette.Muted,
                )
                StatusCell(
                    modifier = Modifier.weight(1f),
                    name = "智能体",
                    value = agent?.shortName?.takeIf { it.isNotBlank() }
                        ?: agent?.displayName?.takeIf { it.isNotBlank() } ?: "—",
                    color = if (agent?.online == true) EchoPalette.Green else EchoPalette.Muted,
                )
                StatusCell(
                    modifier = Modifier.weight(1f),
                    name = "语音合成",
                    // 短值取服务端那句诚实 detail 的第一段（"自动（在线）"/"自动（离线）"）
                    value = tts?.detail?.substringBefore(" · ")?.takeIf { it.isNotBlank() }
                        ?: tts?.let { shortStatus(it.status) } ?: "—",
                    color = tts?.let { EchoPalette.statusColor(it.status) } ?: EchoPalette.Muted,
                )
            }
            Spacer(Modifier.height(8.dp))
            // 第二排：会议 / 回顾
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                StatusCell(
                    modifier = Modifier.weight(1f),
                    name = "会议",
                    value = when {
                        !meeting?.error.isNullOrBlank() -> "出错"
                        meeting?.active == true -> "录音中"
                        else -> "未在录音"
                    },
                    color = when {
                        !meeting?.error.isNullOrBlank() -> EchoPalette.Red
                        meeting?.active == true -> EchoPalette.Purple
                        else -> EchoPalette.Muted
                    },
                )
                StatusCell(
                    modifier = Modifier.weight(1f),
                    name = "回顾",
                    value = reviewLabel,
                    color = reviewColor,
                )
                Spacer(Modifier.weight(1f))
            }

            if (connected == false) {
                Spacer(Modifier.height(8.dp))
                MutedLine(
                    (statusError ?: "连不上 ECHO") +
                        "　·　检查：同一 Wi-Fi？防火墙放行 8970？或者在电脑上开「允许手机经后端中转」" +
                        "、把中继地址填到配对页。",
                    color = EchoPalette.Red,
                    maxLines = 3,
                )
            }

            if (expanded) {
                Spacer(Modifier.height(10.dp))
                HorizontalDivider(color = EchoPalette.Border)
                Spacer(Modifier.height(6.dp))
                if (status == null) {
                    MutedLine("正在读取状态…", maxLines = 1)
                } else {
                    if (status!!.busy) {
                        MutedLine(
                            "正在执行：" + listOfNotNull(
                                status!!.busyOwner?.takeIf { it.isNotBlank() },
                                status!!.busyPhase?.takeIf { it.isNotBlank() },
                            ).joinToString(" · "),
                            color = EchoPalette.Purple,
                            maxLines = 1,
                        )
                        Spacer(Modifier.height(4.dp))
                    }
                    status!!.components.forEach { c ->
                        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
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
                                modifier = Modifier.weight(1.4f),
                            )
                        }
                    }
                    status!!.startupNotes.forEach { note ->
                        Spacer(Modifier.height(4.dp))
                        MutedLine(note, color = EchoPalette.Yellow)
                    }
                    if (status!!.uptimeText().isNotBlank()) {
                        Spacer(Modifier.height(6.dp))
                        MutedLine("电脑上已运行 ${status!!.uptimeText()}", maxLines = 1)
                    }
                }
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = { vm.refresh() }) { Text("刷新") }
                    Spacer(Modifier.weight(1f))
                }
            }
        }
    }
}

/** 一格：状态灯 + 名字 + 短值（照面板底部那条的三格）。 */
@Composable
private fun StatusCell(
    modifier: Modifier,
    name: String,
    value: String,
    color: androidx.compose.ui.graphics.Color,
) {
    Column(modifier = modifier) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusDot(color, size = 7)
            Spacer(Modifier.size(5.dp))
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
