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
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
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

    // 只在界面可见时轮询（离开组合就停）：别让一个看不见的界面一直发请求
    LaunchedEffect(Unit) {
        while (true) {
            vm.pollMeetingStatus()
            delay(POLL_MS)
        }
    }

    // 不熄屏（录音时屏幕灭掉最容易让人以为没录）
    val view = LocalView.current
    DisposableEffect(Unit) {
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = false }
    }

    // 电平平滑：攻击快（跟得上说话）、回落慢（看着像声浪在退）
    var shown by remember { mutableStateOf(0f) }
    val target = st.level.coerceIn(0.0, 1.0).toFloat()
    LaunchedEffect(target) {
        shown = smoothLevel(shown, target)
    }

    val paused = st.paused
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
            Text(
                elapsedText(st.startedAt),
                style = MaterialTheme.typography.displaySmall,
                fontWeight = FontWeight.Light,
            )
            Spacer(Modifier.height(4.dp))
            MutedLine(
                buildString {
                    append("已录 ${st.segments} 段")
                    st.folder?.takeIf { it.isNotBlank() }?.let { append("　·　$it") }
                },
                maxLines = 1,
            )

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
                if (paused) "恢复后接着录；暂停这段是空白，会议详情里能看到暂停区间"
                else "声音来自电脑那支麦克风（手机只是触点）",
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
                    onClick = { vm.toggleMeetingPause() },
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
                    onClick = { vm.stopMeeting() },
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
            MutedLine("结束后电脑会自动转写、出纪要（手机在会议页能看到进度）",
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
