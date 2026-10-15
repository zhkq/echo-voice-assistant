package com.echo.contact.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.ui.theme.EchoPalette

/**
 * 会议页：状态 + 开始/停止录音 + 列表。
 *
 * **录音在 ECHO 那台机器上做**（用它那支麦）—— 手机只是遥控器。
 * 这一点界面必须说清楚，否则用户会以为"手机上录"。
 *
 * **故意没有"删除会议"**：`app/api.py` 的 `_NETWORK_DENIED` 把
 * `DELETE /api/meetings/{id}` 对网上来的调用**一律 403**（"不允许从手机/手表删除会议"）。
 * 手机端提供这个按钮只会得到一个必然失败的操作，所以不提供。
 */
@Composable
fun MeetingsTab(vm: MainViewModel) {
    val meeting = vm.status.collectAsState().value?.meeting
    val meetings by vm.meetings.collectAsState()
    val note by vm.meetingsNote.collectAsState()
    val busy by vm.meetingActionBusy.collectAsState()
    val transcribe by vm.transcribe.collectAsState()
    val detail by vm.meetingDetail.collectAsState()
    val detailLoading by vm.meetingDetailLoading.collectAsState()
    val detailNote by vm.meetingDetailNote.collectAsState()
    val file by vm.meetingFile.collectAsState()
    val audioSeg by vm.audioSeg.collectAsState()
    val audioState by vm.audioState.collectAsState()
    val audioPlaying by vm.audioPlaying.collectAsState()
    val showingRecording by vm.showRecordingScreen.collectAsState()

    // 全屏录音界面（用户 2026-10-15 要的：电平 + 结束 + 静音/恢复）。
    // 放在最前面：录音时它盖住整个页签；「返回」只关界面、**不停录音**。
    if (showingRecording) {
        MeetingRecordingScreen(vm)
        return
    }

    if (detail != null || detailLoading || detailNote != null) {
        MeetingDetailView(
            detail = detail,
            loading = detailLoading,
            note = detailNote,
            file = file,
            audioSeg = audioSeg,
            audioState = audioState,
            audioPlaying = audioPlaying,
            onBack = { vm.closeMeeting() },
            onCloseFile = { vm.closeMeetingFile() },
            onOpenFile = { kind -> detail?.let { vm.loadMeetingFile(it.id, kind) } },
            onSpeak = { vm.speak(it) },
            onPlaySeg = { seg -> detail?.let { vm.playMeetingSegment(it.id, seg, it.segmentCount) } },
            onToggleAudio = { vm.toggleMeetingAudio() },
        )
        return
    }

    val active = meeting?.active == true

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "会议录音",
                        dotColor = when {
                            !meeting?.error.isNullOrBlank() -> EchoPalette.Red
                            active -> EchoPalette.Purple
                            else -> EchoPalette.Muted
                        },
                        trailing = if (active) "录音中" else "未在录音",
                        trailingColor = if (active) EchoPalette.Purple else EchoPalette.Muted,
                    )

                    if (active) {
                        meeting?.folder?.takeIf { it.isNotBlank() }?.let {
                            Spacer(Modifier.height(4.dp))
                            MutedLine("正在录：$it", maxLines = 1)
                        }
                        (meeting?.startedAt)?.takeIf { it.isNotBlank() }?.let {
                            Spacer(Modifier.height(2.dp))
                            MutedLine("开始于 $it", maxLines = 1)
                        }
                    }
                    meeting?.error?.takeIf { it.isNotBlank() }?.let {
                        Spacer(Modifier.height(4.dp))
                        MutedLine("上次错误：$it", color = EchoPalette.Red)
                    }

                    Spacer(Modifier.height(10.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        if (active) {
                            Button(
                                onClick = { vm.stopMeeting() },
                                enabled = !busy,
                                colors = ButtonDefaults.buttonColors(containerColor = EchoPalette.Red),
                            ) { Text("停止录音") }
                        } else {
                            Button(onClick = { vm.startMeeting() }, enabled = !busy) { Text("开始录音") }
                        }
                        if (active) {
                            // 录音中：能随时回到那个全屏界面（电平 + 静音/结束）
                            TextButton(onClick = { vm.openRecordingScreen() }) { Text("全屏界面") }
                        }
                        TextButton(onClick = { vm.loadMeetings() }, enabled = !busy) { Text("刷新") }
                    }
                    Spacer(Modifier.height(4.dp))
                    MutedLine(
                        "现在录的是电脑那支麦克风的声音（手机只负责开始/停止）。" +
                            "让手机当麦克风来录，是下一步要做的。",
                        maxLines = 2,
                    )
                    note?.let {
                        Spacer(Modifier.height(4.dp))
                        MutedLine(it, color = EchoPalette.Yellow)
                    }
                }
            }
        }

        item { Text("最近的会议", style = MaterialTheme.typography.titleMedium) }

        if (meetings.isEmpty()) {
            item { MutedLine("没有会议记录。") }
        } else {
            items(meetings, key = { it.id }) { m ->
                EchoCard {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                m.title.ifBlank { m.name },
                                style = MaterialTheme.typography.bodyMedium,
                                modifier = Modifier.weight(1f),
                                maxLines = 2,
                                overflow = TextOverflow.Ellipsis,
                            )
                            Spacer(Modifier.size(8.dp))
                            if (m.transcribing) {
                                Text("转写中", style = MaterialTheme.typography.labelSmall, color = EchoPalette.Purple)
                            } else {
                                Text(
                                    meetingStatusLabel(m.status),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = meetingStatusColor(m.status),
                                )
                            }
                        }

                        val meta = buildString {
                            m.startedAt.takeIf { it.isNotBlank() }?.let { append(it) }
                            if (m.durationSeconds > 0) {
                                if (isNotEmpty()) append(" · ")
                                append("${(m.durationSeconds / 60).toInt()} 分 ${(m.durationSeconds % 60).toInt()} 秒")
                            }
                            if (m.segments > 0) {
                                if (isNotEmpty()) append(" · ")
                                append("${m.segments} 段")
                            }
                            if (m.hasSummary) {
                                if (isNotEmpty()) append(" · ")
                                append("有纪要")
                            }
                        }
                        if (meta.isNotEmpty()) {
                            Spacer(Modifier.height(3.dp))
                            MutedLine(meta, maxLines = 2)
                        }
                        if (m.speakerNames.isNotEmpty()) {
                            Spacer(Modifier.height(2.dp))
                            MutedLine("说话人：" + m.speakerNames.joinToString("、"), maxLines = 1)
                        }
                        Spacer(Modifier.height(2.dp))
                        TextButton(onClick = { vm.openMeeting(m.id) }) { Text("看正文") }
                        // 转写进度：键**可能是 id 也可能是会议名**（导入录音那条路走 name）
                        if (m.transcribing) {
                            val p = transcribe[m.id.toString()] ?: transcribe[m.name]
                            Spacer(Modifier.height(4.dp))
                            MutedLine(
                                if (p == null) "转写中…"
                                else buildString {
                                    append(p.phase.ifBlank { "转写中" })
                                    if (p.segTotal > 0) append(" ${p.segIndex}/${p.segTotal}")
                                    append(" ${p.percent}%")
                                    if (p.detail.isNotBlank()) append(" · ${p.detail}")
                                },
                                color = EchoPalette.Purple,
                                maxLines = 2,
                            )
                        }
                        // 详情页的字段名与列表**故意不一致**，这里只用列表有的那几个，
                        // 不做"列表当详情用"的事（has_summary vs hasSummary 就是坑）。
                        m.error.takeIf { it.isNotBlank() }?.let {
                            Spacer(Modifier.height(3.dp))
                            MutedLine(it, color = EchoPalette.Red, maxLines = 2)
                        }
                    }
                }
            }
        }
    }
}

/**
 * 单场会议：正文（转写逐行，带说话人与时间）+ 说话人 + 看纪要/主题/转写原文。
 *
 * 手机上**不放音频播放**：`/meetings/{id}/audio?seg=N` 是能取的（还支持 Range），
 * 但一小时的会议在手机上逐段点着听并不是这里该干的事 —— 那是面板/电脑的活。
 * 这里要解决的是"我在外面，想知道这场会讲了什么"。
 */
@Composable
private fun MeetingDetailView(
    detail: com.echo.contact.data.dto.MeetingDetail?,
    loading: Boolean,
    note: String?,
    file: Pair<String, String>?,
    audioSeg: Int,
    audioState: String,
    audioPlaying: Boolean,
    onBack: () -> Unit,
    onCloseFile: () -> Unit,
    onOpenFile: (String) -> Unit,
    onSpeak: (String) -> Unit,
    onPlaySeg: (Int) -> Unit,
    onToggleAudio: () -> Unit,
) {
    // 先把"看某个文件"这一层挡在前面（点进纪要就整页看它）
    if (file != null) {
        val (kind, text) = file
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onCloseFile) { Icon(Icons.Default.ArrowBack, contentDescription = "返回") }
                Text(fileKindLabel(kind), style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.weight(1f))
                IconButton(onClick = { onSpeak(text) }) {
                    Icon(Icons.Default.VolumeUp, contentDescription = "念出来", modifier = Modifier.size(20.dp))
                }
            }
            Spacer(Modifier.height(8.dp))
            LazyColumn { item { Text(text, style = MaterialTheme.typography.bodySmall) } }
        }
        return
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "返回") }
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    detail?.let { it.title.ifBlank { it.name } } ?: "会议",
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                detail?.let {
                    MutedLine(
                        buildString {
                            append(it.startedAt)
                            if (it.durationSeconds > 0) {
                                append(" · ${(it.durationSeconds / 60).toInt()} 分 ${(it.durationSeconds % 60).toInt()} 秒")
                            }
                            if (it.segmentCount > 0) append(" · ${it.segmentCount} 段")
                        },
                        maxLines = 2,
                    )
                }
            }
            detail?.let {
                Text(
                    meetingStatusLabel(it.status),
                    style = MaterialTheme.typography.labelSmall,
                    color = meetingStatusColor(it.status),
                )
            }
        }

        if (loading) {
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(16.dp))
                Spacer(Modifier.size(8.dp))
                Text("正在读取…", style = MaterialTheme.typography.bodySmall)
            }
            return
        }
        note?.let {
            Spacer(Modifier.height(6.dp))
            MutedLine(it, color = EchoPalette.Yellow)
        }
        val d = detail ?: return

        if (d.error.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            MutedLine(d.error, color = EchoPalette.Red)
        }

        // 说话人
        if (d.speakers.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            MutedLine("说话人：" + d.speakers.joinToString("、") { it.name.ifBlank { it.label } })
        }

        // 三个文件入口（有才给按钮）
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (d.hasSummary) TextButton(onClick = { onOpenFile("summary") }) { Text("看纪要") }
            if (d.hasSegments) TextButton(onClick = { onOpenFile("topics") }) { Text("看主题") }
            if (d.hasTranscript) TextButton(onClick = { onOpenFile("transcript") }) { Text("看转写原文") }
        }

        // 录音分段播放（阶段6c）：主机上按段存 flac，后端用时解码成 wav；
        // 手机只做"第几段 / 上一段 / 下一段" —— 听一小段是合理的，把一小时会议从头播到尾不是。
        if (d.segmentCount > 0) {
            val cur = if (audioSeg > 0) audioSeg else 1
            Spacer(Modifier.height(6.dp))
            EchoCard(container = EchoPalette.Card2) {
                Column {
                    CardHeader(
                        title = "录音分段",
                        dotColor = if (audioPlaying) EchoPalette.Red else EchoPalette.Muted,
                        trailing = com.echo.contact.audio.MeetingAudio.label(cur, d.segmentCount),
                    )
                    Spacer(Modifier.height(6.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        TextButton(
                            onClick = { onPlaySeg(com.echo.contact.audio.MeetingAudio.prev(cur, d.segmentCount)) },
                            enabled = cur > 1,
                        ) { Text("上一段") }
                        Button(onClick = {
                            when {
                                audioPlaying -> onToggleAudio()   // 正在放 → 暂停
                                audioSeg > 0 -> onPlaySeg(audioSeg) // 暂停/刚放完 → 继续（缓存命中，秒开）
                                else -> onPlaySeg(1)
                            }
                        }) { Text(if (audioPlaying) "暂停" else "播放") }
                        TextButton(
                            onClick = { onPlaySeg(com.echo.contact.audio.MeetingAudio.next(cur, d.segmentCount)) },
                            enabled = cur < d.segmentCount,
                        ) { Text("下一段") }
                    }
                    if (audioState.isNotBlank()) {
                        MutedLine(audioState, maxLines = 2)
                    } else {
                        MutedLine("一段一段听；主机上的原始录音不动")
                    }
                }
            }
        }

        Spacer(Modifier.height(4.dp))
        if (d.lines.isEmpty()) {
            MutedLine(if (d.transcribing) "正在转写…" else "这场没有可显示的正文。")
            return
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            items(d.lines, key = { it.id }) { line ->
                EchoCard(container = EchoPalette.Card2) {
                    Column {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            MutedLine(
                                "${line.speakerLabel} · ${clock(line.start)}",
                                maxLines = 1,
                            )
                            Spacer(Modifier.weight(1f))
                            IconButton(
                                onClick = { onSpeak(line.text) },
                                modifier = Modifier.size(28.dp),
                            ) {
                                Icon(
                                    Icons.Default.VolumeUp,
                                    contentDescription = "念这一句",
                                    modifier = Modifier.size(16.dp),
                                )
                            }
                        }
                        Text(line.text, style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
    }
}

private fun fileKindLabel(kind: String): String = when (kind) {
    "summary" -> "纪要"
    "topics" -> "主题"
    "transcript" -> "转写原文"
    else -> kind
}

/** 秒 → `mm:ss`（超过一小时给 `h:mm:ss`）。 */
private fun clock(seconds: Double): String {
    val s = seconds.toInt().coerceAtLeast(0)
    val h = s / 3600
    val m = (s % 3600) / 60
    val sec = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

/** 会议状态 → 人话。
 *
 * 实测会出现的值**比 schema 注释里写的多**：注释只写 `recording|transcribing|done|error`，
 * 而列表实测见到 **`transcribed`**，启动恢复还会写 **`interrupted`**（2026-10-09 核实）。
 * 所以这里把五个都认下来，**认不出的原样显示**，不硬翻成某个词。
 */
internal fun meetingStatusLabel(status: String): String = when (status.lowercase()) {
    "done", "completed" -> "已完成"
    "transcribed" -> "已转写"
    "error", "failed" -> "失败"
    "recording" -> "录音中"
    "running" -> "进行中"
    "transcribing" -> "转写中"
    "interrupted" -> "被中断"
    "pending", "idle", "" -> "待处理"
    else -> status
}

internal fun meetingStatusColor(status: String): androidx.compose.ui.graphics.Color = when (status.lowercase()) {
    "done", "completed", "transcribed" -> EchoPalette.Green
    "error", "failed" -> EchoPalette.Red
    "recording", "running", "transcribing" -> EchoPalette.Purple
    "interrupted" -> EchoPalette.Yellow
    else -> EchoPalette.Muted
}
