package com.echo.contact.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.echo.contact.service.OfflinePhase
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** 电平条的格数（奇数，中间高、两边低，看起来像一条"声浪"）。 */
internal const val LEVEL_BARS = 17

/** 取状态的节拍：~350ms。电平要做成好看的动效就得够密，而这条请求很小。 */
private const val POLL_MS = 350L

/**
 * **全屏会议录音界面**（用户 2026-10-15 要的）：
 * 好看的实时电平 + 结束 / 静音（暂停）· 恢复，外加已录时长与段数。
 *
 * ## 数据从哪来
 *
 * 电平是**电脑那支麦**的实时电平（`GET /api/meeting/status` 的 `level`）—— 录音在电脑上做，
 * 手机只是触点。所以这里是"隔着一跳网络"的电平：`POLL_MS` 取 350ms，客户端再做
 * **攻击快、回落慢**的平滑（见 [smoothLevel]），看起来才是连续的，而不是跳动的数字。
 * 等"手机当麦克风"做完，这里会换成手机本地电平（那时可以更密、更顺）。
 *
 * ## 两个刻意的取舍
 *
 *  * **返回键只关这个界面，不停止录音**：录音界面是"看着它录"，不是"开着才录"。
 *    真想停就按那个红色的「结束」。
 *  * **界面开着时不熄屏**（`keepScreenOn`）：录音时屏幕灭掉会让人以为没在录。
 */
@Composable
fun MeetingRecordingScreen(vm: MainViewModel) {
    val st by vm.meetingRecording.collectAsState()
    val busy by vm.meetingActionBusy.collectAsState()
    val note by vm.meetingsNote.collectAsState()
    // 「手机当麦克风」那条路：电平与段数都是**本地**的（见 MeetingMicService）
    val mic by com.echo.contact.service.MeetingMicService.state.collectAsState()
    val phoneMic = mic.active

    // 只在界面可见时轮询（离开组合就停）：别让一个看不见的界面一直发请求。
    // **手机麦那条路不用轮询电平** —— 本地就有，顺得多。
    LaunchedEffect(phoneMic) {
        if (phoneMic) return@LaunchedEffect
        while (true) {
            vm.pollMeetingStatus()
            delay(POLL_MS)
        }
    }

    // 不熄屏（录音时屏幕灭掉最容易让人以为没录）
    val context = LocalContext.current
    val view = LocalView.current
    DisposableEffect(Unit) {
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = false }
    }

    // 电平平滑：攻击快（跟得上说话）、回落慢（看着像声浪在退）
    var shown by remember { mutableStateOf(0f) }
    val target = (if (phoneMic) mic.level else st.level.toFloat()).coerceIn(0f, 1f)
    LaunchedEffect(target) {
        shown = smoothLevel(shown, target)
    }

    val paused = if (phoneMic) mic.paused else st.paused
    val accent = if (paused) EchoPalette.Yellow else EchoPalette.Red

    Surface(modifier = Modifier.fillMaxSize(), color = EchoPalette.Bg) {
        Column(
            modifier = Modifier.fillMaxSize().padding(20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { vm.closeRecordingScreen() }) { Text("← 返回（继续录）") }
                Spacer(Modifier.weight(1f))
                Text(
                    if (paused) "已暂停" else "录音中",
                    style = MaterialTheme.typography.titleMedium,
                    color = accent,
                    fontWeight = FontWeight.SemiBold,
                )
            }

            Spacer(Modifier.height(18.dp))

            // ---- 大计时 ----
            // 手机麦这条路：**开始时间永远是手机自己记的**（用户 2026-10-17 合并成一条路之后，
            // 不再有"电脑给了 startedAt 才有值"那一档），所以这里就是 `mic.startedAt`。
            Text(
                elapsedText(if (phoneMic) mic.startedAt else st.startedAt),
                style = MaterialTheme.typography.displaySmall,
                fontWeight = FontWeight.Light,
            )
            Spacer(Modifier.height(4.dp))
            MutedLine(
                if (phoneMic) {
                    buildString {
                        // 这条路永远是"先落本地"：说清"已缓存几段、其中几段已经传上去了"
                        // —— 只说"已送 N 段"会让人以为本地没有副本（其实本地那份才是本体）。
                        append("手机麦克风 · 已缓存 ${mic.syncPending} 段")
                        if (mic.syncConfirmed > 0) append("（已传 ${mic.syncConfirmed} 段）")
                        if (mic.formatNote.isNotBlank()) {
                            // 格式**如实**：退回 WAV 时要说清是哪一段、为什么（用户点名要求）
                            append(" · ${mic.formatNote}")
                        }
                    }
                } else {
                    buildString {
                        append("电脑麦克风 · 已录 ${st.segments} 段")
                        st.folder?.takeIf { it.isNotBlank() }?.let { append("　·　$it") }
                    }
                },
                maxLines = 2,
            )

            // ---- 音频"传上去没有"：**如实的一行** + 「现在同步」----
            // 这一行是"音频到底有没有传上去"的唯一出口：连不上时先说"ECHO 连不上、
            // 音频先存在这台手机里"，绝不说成"已上传"。
            if (phoneMic && mic.syncNote.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        mic.syncNote,
                        style = MaterialTheme.typography.bodyMedium,
                        color = if (mic.syncPhase == OfflinePhase.DONE) EchoPalette.Green
                        else EchoPalette.Yellow,
                        fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.weight(1f),
                    )
                    if (mic.syncPending > 0 && !mic.syncBusy) {
                        OutlinedButton(
                            onClick = {
                                // 复用服务里那一条补传路（不在这里另写上传逻辑）
                                com.echo.contact.service.MeetingMicService.syncNow(context)
                            },
                        ) { Text("现在同步") }
                    }
                }
            }

            Spacer(Modifier.height(28.dp))

            // ---- 电平：中间高两边低的柱状"声浪" ----
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(180.dp)
                    .clip(RoundedCornerShape(14.dp))
                    .background(EchoPalette.Card2),
                contentAlignment = Alignment.Center,
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    for (i in 0 until LEVEL_BARS) {
                        val weight = barWeight(i)          // 中间高、两边低
                        val h = (8 + shown * weight * 150).dp
                        Box(
                            modifier = Modifier
                                .weight(1f)
                                .height(h)
                                .clip(RoundedCornerShape(6.dp))
                                .background(if (paused) EchoPalette.Muted else barColor(shown, weight)),
                        )
                    }
                }
                if (paused) {
                    Text("已静音 —— 这段时间不会录进会议", color = EchoPalette.Yellow,
                        style = MaterialTheme.typography.bodyMedium)
                }
            }

            Spacer(Modifier.height(10.dp))
            MutedLine(
                when {
                    paused -> "恢复后接着录；暂停这段是空白，会议详情里能看到暂停区间"
                    // 这条路永远是"先落本地、再顺手传"：说清这一点（**不**说"离线"，
                    // 因为在线时也一样先落本地 —— 用户 2026-10-17 的原话是"没有本质区别"）
                    phoneMic -> "声音来自**这台手机的麦克风**：每一段先存在手机里，" +
                        "网通就顺手传给电脑（最多滞后一段），不通就留着等联网自动补传"
                    else -> "声音来自电脑那支麦克风（手机只是触点）"
                },
                maxLines = 2,
            )
            note?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Yellow, maxLines = 2)
            }
            st.error.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine("上次错误：$it", color = EchoPalette.Red, maxLines = 3)
            }

            Spacer(Modifier.weight(1f))

            // ---- 两个主按钮：静音/恢复、结束 ----
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                OutlinedButton(
                    onClick = {
                        if (phoneMic) {
                            // 手机麦：静音 = **照常录、写进去的是全零（空白）音频**，时间轴逐字
                            // 连续、总时长不受影响（用户 2026-10-16 深夜的口径，与电脑侧统一；
                            // 旧的"本地丢帧"已被推翻 —— 丢帧会让手机那场的时间轴在暂停期间变短）。
                            // 这段区间**记进清单**；恢复时会把它上报给服务端
                            // （`POST /api/meeting/pauses`）。
                            // ⚠️ **不**再顺手调电脑的暂停接口：这条路上电脑不在场（它只负责落盘/转写），
                            // 调它只会拿到一个失败，而失败原因会写进界面 —— 明明静音已经生效了
                            // （把自己的缺点说成用户的错，最不该）。
                            com.echo.contact.service.MeetingMicService.setPaused(context, !paused)
                        } else {
                            vm.toggleMeetingPause()
                        }
                    },
                    enabled = !busy,
                    modifier = Modifier.weight(1f).height(56.dp),
                ) {
                    Icon(
                        if (paused) Icons.Default.Mic else Icons.Default.MicOff,
                        contentDescription = null,
                        modifier = Modifier.size(18.dp),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(if (paused) "恢复" else "静音")
                }
                Button(
                    onClick = {
                        if (phoneMic) {
                            // 手机麦：先让服务把欠的段补传、并让电脑结束这场会议
                            com.echo.contact.service.MeetingMicService.stop(context)
                        } else {
                            vm.stopMeeting()
                        }
                    },
                    enabled = !busy,
                    colors = ButtonDefaults.buttonColors(containerColor = EchoPalette.Red),
                    modifier = Modifier.weight(1f).height(56.dp),
                ) {
                    Icon(Icons.Default.Stop, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("结束")
                }
            }
            Spacer(Modifier.height(8.dp))
            MutedLine(
                when {
                    phoneMic -> "结束后手机把剩下的段补齐、让 ECHO 收尾（收尾之后才开始转写）；" +
                        "传不完的会留着，联网后自动接着传（也可以点「现在同步」）"
                    else -> "结束后电脑会自动转写、出纪要（手机在会议页能看到进度）"
                },
                maxLines = 2, color = EchoPalette.Muted)
            Spacer(Modifier.height(6.dp))
        }
    }
}

/**
 * 电平平滑：**攻击快、回落慢**。
 *
 * 为什么不能直接用采样值：网络间隔 350ms，直接用会是一格一格跳；而"声音退下去"这件事
 * 人眼接受得很慢（真实声浪也有余响）。所以：涨的时候跟 65%，落的时候只落 25%。
 */
internal fun smoothLevel(prev: Float, target: Float): Float {
    val p = prev.coerceIn(0f, 1f)
    val t = target.coerceIn(0f, 1f)
    return if (t >= p) p + (t - p) * 0.65f else p + (t - p) * 0.25f
}

/** 中间高、两边低（0..1 的钟形）。纯函数，有判据。 */
internal fun barWeight(index: Int, bars: Int = LEVEL_BARS): Float {
    if (bars <= 1) return 1f
    val mid = (bars - 1) / 2f
    val d = abs(index - mid) / mid            // 0（中间）→ 1（两端）
    return (1f - d * 0.75f).coerceIn(0.25f, 1f)
}

/** 颜色跟着强度走：弱=青、中=紫、强=红（与面板的状态色同一套）。 */
internal fun barColor(level: Float, weight: Float): Color {
    val v = (level * weight).coerceIn(0f, 1f)
    return when {
        v > 0.66f -> EchoPalette.Red
        v > 0.33f -> EchoPalette.Purple
        else -> EchoPalette.Accent
    }
}

/**
 * `startedAt`（ISO 本地时间，如 `2026-10-15T23:40:12`）→ `mm:ss` / `hh:mm:ss`。
 *
 * 解析不了就回 `--:--`：**不编一个 00:00 出来**（那会让人以为"刚开始录"，
 * 而真相是我们读不懂那个时间字段）。
 */
internal fun elapsedText(startedAt: String?, now: Long = System.currentTimeMillis()): String {
    val s = startedAt?.trim().orEmpty()
    if (s.isEmpty()) return "--:--"
    val startMs = try {
        java.time.LocalDateTime.parse(s).atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli()
    } catch (e: Exception) {
        return "--:--"
    }
    val secs = max(0L, (now - startMs) / 1000)
    val h = secs / 3600
    val m = (secs % 3600) / 60
    val ss = secs % 60
    return if (h > 0) String.format("%d:%02d:%02d", h, m, ss) else String.format("%02d:%02d", m, ss)
}
