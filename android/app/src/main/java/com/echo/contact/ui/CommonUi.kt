package com.echo.contact.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.data.dto.CommandItem
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay

/** 状态点：颜色照面板 `.dot` 规则（见 [EchoPalette.statusColor]）。 */
@Composable
internal fun StatusDot(color: Color, size: Int = 10) {
    Box(modifier = Modifier.size(size.dp).background(color, CircleShape))
}

/**
 * 「连不上 ECHO」那张卡（从旧仪表盘搬来，现在挂在说话页顶上）。
 *
 * 为什么留一条**具体的排查提示**而不是只说"失败"：这条路的故障几乎全是环境问题
 * （不同 Wi-Fi、防火墙、走了中继但中继没开），用户看一句"检查这几样"比看错误码有用。
 */
@Composable
internal fun ErrorCard(message: String) {
    EchoCard(container = EchoPalette.ErrorContainer) {
        Column {
            CardHeader(title = "连不上 ECHO", dotColor = EchoPalette.Red)
            Spacer(Modifier.height(4.dp))
            Text(message, style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.height(4.dp))
            MutedLine("检查：手机和电脑同一 Wi-Fi？电脑防火墙放行了 8970？或者在电脑上开「允许手机经后端中转」、把中继地址填到配对页。")
        }
    }
}

/** 面板的卡片长相：深底 + 1px 描边 + 10px 圆角。 */
@Composable
internal fun EchoCard(
    modifier: Modifier = Modifier,
    container: Color = EchoPalette.Card,
    content: @Composable () -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(10.dp),
        colors = CardDefaults.cardColors(containerColor = container),
        border = BorderStroke(1.dp, EchoPalette.Border),
    ) {
        Box(modifier = Modifier.padding(14.dp)) { content() }
    }
}

/** 卡片标题行：状态点 + 标题 + （右侧）状态词。 */
@Composable
internal fun CardHeader(
    title: String,
    dotColor: Color,
    trailing: String = "",
    trailingColor: Color = EchoPalette.Muted,
) {
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        StatusDot(dotColor)
        Spacer(Modifier.size(8.dp))
        Text(title, style = MaterialTheme.typography.titleSmall)
        Spacer(Modifier.weight(1f))
        if (trailing.isNotBlank()) {
            Text(trailing, style = MaterialTheme.typography.labelLarge, color = trailingColor)
        }
    }
}

/** 一行小字（次要说明）。 */
@Composable
internal fun MutedLine(text: String, color: Color = EchoPalette.Muted, maxLines: Int = 3) {
    Text(
        text,
        style = MaterialTheme.typography.labelSmall,
        color = color,
        maxLines = maxLines,
        overflow = TextOverflow.Ellipsis,
    )
}

/**
 * 朗读那条路的**当下反馈**（"念不出来 / 没装 TTS / ECHO 合成失败 / 没有可念的播报"…）。
 *
 * 为什么非得有这一行：`MainViewModel._speakNote` 以前**全 App 没有一处界面读**
 * （只剩 ViewModel 内部赋值），于是"按了播报没声音"就是彻底静默 —— 用户只能猜。
 * 现在回顾页（状态卡 / 详情页）与指令页、首页各摆一处（判据 `SpeakNoteVisibleTest`）。
 *
 * 为什么是**单行 + 自动消失**：它是"这一下"的反馈，不是状态。长期挂着会喧宾夺主，
 * 也会让人以为"现在仍然坏着"（其实下一次朗读可能就好了）。[SPEAK_NOTE_VISIBLE_MS]
 * 之后调 [onDismiss] 把它清掉（清除入口在 `MainViewModel.clearSpeakNote`）。
 */
@Composable
internal fun SpeakNoteLine(
    note: String?,
    onDismiss: () -> Unit,
    color: Color = EchoPalette.Yellow,
) {
    val text = note?.takeIf { it.isNotBlank() } ?: return
    LaunchedEffect(text) {
        delay(SPEAK_NOTE_VISIBLE_MS)
        onDismiss()
    }
    MutedLine(text, color = color, maxLines = 1)
}

/** 朗读提示在界面上停多久（毫秒）：够读完一句话，又不至于一直占着版面。 */
internal const val SPEAK_NOTE_VISIBLE_MS = 8_000L

/** 组件的英文名 → 面板上那句中文（未知名字原样显示）。 */
internal fun componentLabel(name: String): String = when (name) {
    "server" -> "服务"
    "dsh" -> "DSH"
    "harness" -> "智能体"
    "stt" -> "语音识别"
    "tts" -> "语音合成"
    "wake" -> "唤醒词"
    "hotkey" -> "热键"
    "meeting" -> "会议"
    "diarize" -> "说话人分离"
    else -> name
}

/** 组件状态 → 面板上那句短词。 */
internal fun shortStatus(status: String): String = when (status.lowercase()) {
    "online" -> "在线"
    "active" -> "进行中"
    "ready" -> "就绪"
    "offline" -> "离线"
    "error" -> "故障"
    "idle" -> "空闲"
    "disabled" -> "未启用"
    "skipped" -> "未使用"
    "unknown", "" -> "未知"
    else -> status
}

/** 指令状态 → 人话（db.py:117 的生命周期：pending→sent→running→done/failed）。 */
internal fun commandStatusLabel(status: String?): String = when (status) {
    "pending" -> "排队中"
    "sent" -> "已发送"
    "running" -> "执行中"
    "done" -> "完成"
    "failed" -> "失败"
    null, "" -> ""
    else -> status
}

/** 指令状态 → 灯色。 */
internal fun commandStatusColor(status: String?): Color = when (status) {
    "done" -> EchoPalette.Green
    "failed" -> EchoPalette.Red
    "running", "sent", "pending" -> EchoPalette.Purple
    else -> EchoPalette.Muted
}

/**
 * 指令历史的一行。**指令页与仪表盘"最近指令"共用**，所以抽出来。
 * `onSpeak` / `onOpenSession` 传 null 就不显示对应的入口。
 */
@Composable
internal fun CommandRow(
    item: CommandItem,
    onSpeak: ((String) -> Unit)? = null,
    onOpenSession: ((CommandItem) -> Unit)? = null,
) {
    val status = item.status
    EchoCard {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    item.text ?: "(空指令)",
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.weight(1f),
                    maxLines = 3,
                    overflow = TextOverflow.Ellipsis,
                )
                if (!status.isNullOrBlank()) {
                    Spacer(Modifier.size(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        StatusDot(commandStatusColor(status), size = 8)
                        Spacer(Modifier.size(4.dp))
                        Text(
                            commandStatusLabel(status),
                            style = MaterialTheme.typography.labelSmall,
                            color = EchoPalette.Muted,
                        )
                    }
                }
            }

            val meta = listOfNotNull(
                item.ts?.takeIf { it.isNotBlank() },
                item.source?.takeIf { it.isNotBlank() },
            ).joinToString(" · ")
            if (meta.isNotEmpty()) {
                Spacer(Modifier.height(3.dp))
                MutedLine(meta, maxLines = 1)
            }

            item.error?.takeIf { it.isNotBlank() }?.let { err ->
                Spacer(Modifier.height(5.dp))
                Text(
                    err,
                    style = MaterialTheme.typography.bodySmall,
                    color = EchoPalette.Red,
                    maxLines = 4,
                    overflow = TextOverflow.Ellipsis,
                )
            }

            item.reply?.takeIf { it.isNotBlank() }?.let { reply ->
                Spacer(Modifier.height(5.dp))
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        reply,
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 8,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f),
                    )
                    onSpeak?.let { speak ->
                        IconButton(onClick = { speak(reply) }) {
                            Icon(
                                Icons.Default.VolumeUp,
                                contentDescription = "朗读",
                                modifier = Modifier.size(18.dp),
                            )
                        }
                    }
                }
            }

            // 会话入口：只有拿到 session_id 的才给（没会话就没得看）
            if (onOpenSession != null && !item.sessionId.isNullOrBlank()) {
                TextButton(onClick = { onOpenSession(item) }) {
                    Text("看会话", style = MaterialTheme.typography.labelMedium)
                }
            }
        }
    }
}
