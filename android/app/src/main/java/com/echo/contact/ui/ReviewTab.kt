package com.echo.contact.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
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
import androidx.compose.material.icons.filled.Today
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
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.data.dto.DailyReviewState
import com.echo.contact.data.dto.DailyReviewSummary
import com.echo.contact.data.dto.ReviewDay
import com.echo.contact.data.dto.ReviewHistoryDetail
import com.echo.contact.ui.theme.EchoPalette

/** 回顾页顶部功能区的标题（判据见 `ReviewTabSectionsTest`）。 */
internal const val REVIEW_FEATURE_TITLE = "回顾功能"

/** 历史列表的标题。 */
internal const val REVIEW_HISTORY_TITLE = "历史回顾"

/** 历史为空时的空状态文案（用户 2026-10-18：不许退成一整页纯列表，也不能空着不说）。 */
internal const val REVIEW_HISTORY_EMPTY = "还没有回顾历史。口述一次今天，这里就会出现第一天。"

/**
 * 回顾页顶部的功能卡（**纯数据**，JVM 可测）。
 *
 * 只放**现有能力**：今日回顾（状态 + 最近播报）、车里口述（手机自己录）、播报（念）。
 * 没有的能力不编 —— 例如"整理某一天的日志"手机端没有这个接口，就不放。
 */
internal enum class ReviewFeature(val title: String) {
    TODAY("今日回顾"),
    DICTATE("车里口述"),
    SPEAK("播报"),
}

internal data class ReviewFeatureTile(
    val feature: ReviewFeature,
    val subtitle: String,
    val enabled: Boolean,
)

/**
 * 功能区三张卡（纯函数，`ReviewTabSectionsTest` 钉住）。
 *
 * `enabled=false` 只出现在"没有可念的播报"那一档：界面把它**置灰 + 写清原因**
 * （不是藏起来 —— 藏起来用户会以为功能没了）。
 */
internal fun reviewFeatureTiles(
    running: Boolean,
    enabled: Boolean,
    ready: Boolean,
    todayTurns: Int,
    hasBrief: Boolean,
    speaking: Boolean,
): List<ReviewFeatureTile> = listOf(
    ReviewFeatureTile(
        feature = ReviewFeature.TODAY,
        subtitle = when {
            running -> "进行中"
            !enabled -> "未启用"
            !ready -> "未就绪"
            todayTurns > 0 -> "今天已回顾 $todayTurns 轮"
            else -> "今天还没回顾"
        },
        // 点它 = 重读状态（这个动作永远有意义，不置灰）
        enabled = true,
    ),
    ReviewFeatureTile(
        feature = ReviewFeature.DICTATE,
        subtitle = "录一段口述，ECHO 转写并整理",
        enabled = true,
    ),
    ReviewFeatureTile(
        feature = ReviewFeature.SPEAK,
        subtitle = if (hasBrief) "念今天 / 最近一次的播报" else "还没有可念的播报",
        enabled = hasBrief && !speaking,
    ),
)

/**
 * 历史回顾**按日期倒序**（用户 2026-10-18：下面是历史回顾列表）。
 *
 * 为什么界面还要再排一次：接口现在就是倒序返回的，但"展示顺序"是界面的事 ——
 * 后端换实现或分页拼起来时不该让用户看到乱序（这条是纯函数，有判据）。
 */
internal fun reviewDaysDescending(days: List<ReviewDay>): List<ReviewDay> =
    days.sortedByDescending { it.date }

/**
 * 回顾页：**顶部功能区 + 下面历史回顾列表**（用户 2026-10-18 点名的形态）。
 *
 * 为什么手机不能"开始主机回顾"：那要占用**主机那支麦克风**口述 —— 那不是手机该干的事。
 * 手机走的是"自己录口述 → ECHO 转写整理 → 念播报"这条路（`vm.submitReviewDictation`），
 * 念用手机系统 TTS（离线、零等待）。
 *
 * 上一版是"一张口述卡 + 一张状态卡 + 一整页列表"；这一版把三件事提成顶部功能区
 * （今日回顾 / 车里口述 / 播报），下面才是历史 —— 与超级助理页的仪表盘形态对齐。
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

    // 三个主入口都是**现有能力**（见 `reviewFeatureTiles`）
    val tiles = reviewFeatureTiles(
        running = review?.running == true,
        enabled = review?.enabled == true,
        ready = review?.ready == true,
        todayTurns = summary?.todayTurns ?: 0,
        hasBrief = !summary?.lastBrief.isNullOrBlank(),
        speaking = speaking,
    )
    val orderedDays = reviewDaysDescending(days)

    // 口述：没授权就先要（与「指令」页同一套做法），授权成功再开始
    val startDictation: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) vm.startRecording() else permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        // ---- 顶部功能区（与超级助理页同一个仪表盘形态）----
        item {
            Text(REVIEW_FEATURE_TITLE, style = MaterialTheme.typography.titleSmall)
        }
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                tiles.forEach { tile ->
                    ReviewFeatureCard(
                        tile = tile,
                        modifier = Modifier.weight(1f).height(104.dp),
                        onClick = {
                            when (tile.feature) {
                                // 重读状态与历史（现有动作，不新造）
                                ReviewFeature.TODAY -> {
                                    vm.loadReview()
                                    vm.loadReviewDays()
                                }
                                ReviewFeature.DICTATE -> startDictation()
                                ReviewFeature.SPEAK -> vm.speakReview()
                            }
                        },
                    )
                }
            }
        }

        // ---- 今日回顾详情：最近一次播报 + 念 / 刷新 / 停 ----
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

        // ---- 口述进行中 / 整理中：控制在这里（不在录音时入口是上面那张「车里口述」卡）----
        if (recording || submitting || voiceText.isNotBlank() || submittingNote != null) {
            item {
                DictationCard(
                    recording = recording,
                    recordMs = recordMs,
                    submitting = submitting,
                    voiceText = voiceText,
                    submittingNote = submittingNote,
                    onDone = { vm.submitReviewDictation() },
                    onCancel = { vm.cancelReviewDictation() },
                )
            }
        }

        // ---- 历史回顾（按日期倒序；空时有空状态文案）----
        item { Text(REVIEW_HISTORY_TITLE, style = MaterialTheme.typography.titleMedium) }

        if (orderedDays.isEmpty()) {
            item { MutedLine(REVIEW_HISTORY_EMPTY, maxLines = 3) }
        } else {
            items(orderedDays, key = { it.date }) { d ->
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

/** 一张功能卡：图标 + 标题 + 一句状态；没有可做的事时置灰（不藏）。 */
@Composable
private fun ReviewFeatureCard(
    tile: ReviewFeatureTile,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    val accent = if (tile.enabled) EchoPalette.Accent else EchoPalette.Muted
    EchoCard(
        modifier = modifier.then(
            if (tile.enabled) Modifier.clickable(onClick = onClick) else Modifier
        ),
    ) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    featureIcon(tile.feature),
                    contentDescription = null,
                    tint = accent,
                    modifier = Modifier.size(18.dp),
                )
                Spacer(Modifier.size(6.dp))
                Text(
                    tile.feature.title,
                    style = MaterialTheme.typography.labelLarge,
                    color = accent,
                    maxLines = 1,
                )
            }
            Spacer(Modifier.height(6.dp))
            MutedLine(tile.subtitle, maxLines = 3)
        }
    }
}

/** 功能 → 图标（只做外观映射）。 */
private fun featureIcon(feature: ReviewFeature): ImageVector = when (feature) {
    ReviewFeature.TODAY -> Icons.Default.Today
    ReviewFeature.DICTATE -> Icons.Default.Mic
    ReviewFeature.SPEAK -> Icons.Default.VolumeUp
}

/**
 * 口述进行中的控制卡（只有真的在录/在整理时才出现）。
 *
 * 为什么入口要拆到功能区那张卡：主界面不该常驻一整张"怎么用"的说明卡；
 * 而一旦开始录，"说完了 / 取消 / 正在整理"必须立刻可见 —— 这几件事不能藏。
 */
@Composable
private fun DictationCard(
    recording: Boolean,
    recordMs: Long,
    submitting: Boolean,
    voiceText: String,
    submittingNote: String?,
    onDone: () -> Unit,
    onCancel: () -> Unit,
) {
    EchoCard {
        Column {
            CardHeader(
                title = "车里口述",
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
            when {
                submitting -> Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp))
                    Spacer(Modifier.size(8.dp))
                    // 整理要一两分钟：先说清，别让用户以为卡住了
                    Text("正在整理…（通常 2–3 分钟）", style = MaterialTheme.typography.bodySmall)
                }
                recording -> Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Button(onClick = onDone) { Text("说完了") }
                    TextButton(onClick = onCancel) { Text("取消") }
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
            MutedLine("点上面的「车里口述」录一段 —— 手机自己录，不用电脑那支麦克风。")
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
