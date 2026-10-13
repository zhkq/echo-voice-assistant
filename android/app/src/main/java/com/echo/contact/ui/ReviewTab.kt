package com.echo.contact.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.data.dto.DailyReviewState
import com.echo.contact.data.dto.DailyReviewSummary
import com.echo.contact.data.dto.ReviewHistoryDetail
import com.echo.contact.ui.theme.EchoPalette

/**
 * 回顾页：**手机只做"听播报"**。
 *
 * 为什么手机不能"开始回顾"：回顾要占用**主机那支麦克风**口述
 * （`POST /api/daily-review/go` 是在 ECHO 那台机器上开录），手机点它
 * 只会让电脑开始听 —— 那不是手机该干的事。
 *
 * 念用**手机的系统 TTS**（离线、零等待），这是 2026-10-09 定的口径。
 */
@Composable
fun ReviewTab(vm: MainViewModel) {
    val review by vm.review.collectAsState()
    val summary by vm.reviewSummary.collectAsState()
    val note by vm.reviewNote.collectAsState()
    val days by vm.reviewDays.collectAsState()
    val day by vm.reviewDay.collectAsState()
    val speaking by vm.speaking.collectAsState()
    val recording by vm.recording.collectAsState()
    val recordMs by vm.recordMs.collectAsState()
    val submitting by vm.reviewSubmitting.collectAsState()
    val submittingNote by vm.reviewSubmittingNote.collectAsState()
    val voiceText by vm.voiceText.collectAsState()

    val context = LocalContext.current
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) vm.startRecording() }

    if (day != null) {
        ReviewDayView(
            detail = day!!,
            speaking = speaking,
            onSpeak = { vm.speakReviewDay(day!!.date) },
            onSpeakText = { vm.speak(it) },
            onBack = { vm.closeReviewDay() },
        )
        return
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "口述今天",
                        dotColor = when {
                            submitting -> EchoPalette.Purple
                            recording -> EchoPalette.Red
                            else -> EchoPalette.Muted
                        },
                        trailing = when {
                            submitting -> "整理中…"
                            recording -> "录音中 ${"%.1f".format(recordMs / 1000.0)} 秒"
                            else -> ""
                        },
                        trailingColor = if (recording) EchoPalette.Red else EchoPalette.Purple,
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        when {
                            submitting -> {
                                CircularProgressIndicator(modifier = Modifier.size(16.dp))
                                Text("正在整理…", style = MaterialTheme.typography.bodySmall)
                            }
                            recording -> {
                                Button(onClick = { vm.submitReviewDictation() }) { Text("说完了") }
                                TextButton(onClick = { vm.cancelReviewDictation() }) { Text("取消") }
                            }
                            else -> {
                                Button(onClick = {
                                    val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                                        PackageManager.PERMISSION_GRANTED
                                    if (granted) vm.startRecording() else permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                                }) {
                                    Icon(Icons.Default.Mic, contentDescription = null, modifier = Modifier.size(18.dp))
                                    Spacer(Modifier.size(6.dp))
                                    Text("说今天干了啥")
                                }
                            }
                        }
                    }
                    if (voiceText.isNotBlank()) {
                        Spacer(Modifier.height(6.dp))
                        MutedLine("ECHO 听成：$voiceText", maxLines = 4)
                    }
                    submittingNote?.let {
                        Spacer(Modifier.height(4.dp))
                        MutedLine(it, color = EchoPalette.Purple)
                    }
                    Spacer(Modifier.height(4.dp))
                    // 这条才是手机该走的回顾路径：**手机录、ECHO 整理**。
                    // （主机麦克风那条在车里根本听不到你说话；手机上录才成立。）
                    MutedLine("手机上录口述 → ECHO 转写并整理 → 把播报念给你听（整理要 2–3 分钟）")
                }
            }
        }

        item {
            ReviewStatusCard(
                review = review,
                summary = summary,
                note = note,
                speaking = speaking,
                onSpeak = { vm.speakReview() },
                onRefresh = {
                    vm.loadReview()
                    vm.loadReviewDays()
                },
                onStop = { vm.stopSpeaking() },
                onStopHost = { vm.stopHostReview() },
            )
        }

        item { Text("回顾历史", style = MaterialTheme.typography.titleMedium) }

        if (days.isEmpty()) {
            item { MutedLine("还没有回顾历史。") }
        } else {
            items(days, key = { it.date }) { d ->
                EchoCard {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(d.date, style = MaterialTheme.typography.titleMedium)
                            Spacer(Modifier.size(8.dp))
                            MutedLine(
                                buildString {
                                    append("${d.turns} 轮")
                                    if (d.failed > 0) append(" · 失败 ${d.failed}")
                                    d.lastAt?.takeIf { it.isNotBlank() }?.let { append(" · $it") }
                                },
                                maxLines = 1,
                            )
                            Spacer(Modifier.weight(1f))
                            IconButton(onClick = { vm.speakReviewDay(d.date) }) {
                                Icon(Icons.Default.VolumeUp, contentDescription = "念这天的播报", modifier = Modifier.size(18.dp))
                            }
                        }
                        d.brief?.takeIf { it.isNotBlank() }?.let {
                            Spacer(Modifier.height(4.dp))
                            Text(
                                it,
                                style = MaterialTheme.typography.bodySmall,
                                color = EchoPalette.Muted,
                                maxLines = 3,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                        TextButton(onClick = { vm.openReviewDay(d.date) }) { Text("看这一天") }
                    }
                }
            }
        }
    }
}

@Composable
private fun ReviewStatusCard(
    review: DailyReviewState?,
    summary: DailyReviewSummary?,
    note: String?,
    speaking: Boolean,
    onSpeak: () -> Unit,
    onRefresh: () -> Unit,
    onStop: () -> Unit,
    onStopHost: () -> Unit,
) {
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

            if (!ready && !review?.reason.isNullOrBlank()) {
                Spacer(Modifier.height(4.dp))
                MutedLine(review!!.reason)
            }

            val brief = summary?.lastBrief
            if (!brief.isNullOrBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    (summary?.lastAt?.plus("　") ?: "") + brief,
                    style = MaterialTheme.typography.bodySmall,
                    color = EchoPalette.Muted,
                    maxLines = 5,
                    overflow = TextOverflow.Ellipsis,
                )
            } else if (summary != null) {
                Spacer(Modifier.height(6.dp))
                MutedLine("还没有回顾记录。")
            }

            note?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Yellow)
            }

            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = onSpeak, enabled = !brief.isNullOrBlank() && !speaking) {
                    Icon(Icons.Default.VolumeUp, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.size(6.dp))
                    Text(if (turns > 0) "念今天的播报" else "念最近一次")
                }
                if (speaking) TextButton(onClick = onStop) { Text("停") }
                TextButton(onClick = onRefresh) { Text("刷新") }
            }
            // 主机麦克风那条路开着的时候，手机上能把它停掉（那是真的有用：
            // 你人在别处，而电脑那边正在录）
            if (running) {
                Spacer(Modifier.height(4.dp))
                TextButton(onClick = onStopHost) { Text("停掉主机上正在进行的回顾") }
            }
            Spacer(Modifier.height(4.dp))
            MutedLine("想口述就回上面「口述今天」—— 手机自己录，不用电脑那支麦克风。")
        }
    }
}

@Composable
private fun ReviewDayView(
    detail: ReviewHistoryDetail,
    speaking: Boolean,
    onSpeak: () -> Unit,
    onSpeakText: (String) -> Unit,
    onBack: () -> Unit,
) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "返回") }
            Column(modifier = Modifier.weight(1f)) {
                Text(detail.date, style = MaterialTheme.typography.titleMedium)
                MutedLine("${detail.count} 轮" + if (detail.vaultNoteExists) " · 已写入工作日志" else "")
            }
            IconButton(onClick = onSpeak, enabled = !speaking) {
                Icon(Icons.Default.VolumeUp, contentDescription = "念这天的播报")
            }
        }

        Spacer(Modifier.height(8.dp))
        if (detail.turns.isEmpty()) {
            MutedLine("这一天没有回顾记录。")
            return
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(detail.turns, key = { it.id }) { t ->
                EchoCard {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            MutedLine(t.at, maxLines = 1)
                            Spacer(Modifier.weight(1f))
                            Text(
                                commandStatusLabel(t.status),
                                style = MaterialTheme.typography.labelSmall,
                                color = commandStatusColor(t.status),
                            )
                        }
                        if (t.brief.isNotBlank()) {
                            Spacer(Modifier.height(4.dp))
                            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    t.brief,
                                    style = MaterialTheme.typography.bodyMedium,
                                    modifier = Modifier.weight(1f),
                                )
                                IconButton(onClick = { onSpeakText(t.brief) }) {
                                    Icon(Icons.Default.VolumeUp, contentDescription = "念这一轮", modifier = Modifier.size(18.dp))
                                }
                            }
                        }
                        if (t.error.isNotBlank()) {
                            Spacer(Modifier.height(4.dp))
                            MutedLine(t.error, color = EchoPalette.Red)
                        }
                        if (t.text.isNotBlank()) {
                            Spacer(Modifier.height(4.dp))
                            MutedLine("口述原文：" + t.text, maxLines = 4)
                        }
                    }
                }
            }
        }
    }
}
