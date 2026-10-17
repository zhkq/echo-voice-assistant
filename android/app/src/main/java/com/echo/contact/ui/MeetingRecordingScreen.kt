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
import androidx.compose.ui.unit.dp
import com.echo.contact.service.OfflinePhase
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay
import kotlin.math.abs
import kotlin.math.max

/** 电平条的格数（奇数，中间高、两边低，看起来像一条"声浪"）。 */
internal const val LEVEL_BARS = 17

/**
 * **全屏会议录音界面**（用户 2026-10-15 要的）：
 * 好看的实时电平 + 结束 / 静音（暂停）· 恢复，外加已录时长与段数。
 *
 * ## 数据从哪来（2026-10-17 真机 bug 之后改的）
 *
 * 全部来自**这台手机**：`MeetingMicService.state`（本机麦克风的电平、暂停、段数、
 * 开始时间、补传进度）。旧版轮询 `GET /api/meeting/status` 拿的是**电脑那支麦**的电平 ——
 * 那正是"手机发起、电脑在录"那条路的一部分，已经退役。
 * 而且手机麦这条路**离线也在录**，服务端那时只会说 `active=false`：拿它决定"关不关界面"
 * 会把正在进行的手机录音界面关掉。所以这里**不再**轮询服务端。
 *
 * ## 两个刻意的取舍
 *
 *  * **返回键只关这个界面，不停止录音**：录音界面是"看着它录"，不是"开着才录"。
 *    真想停就按那个红色的「结束」。
 *  * **界面开着时不熄屏**（`keepScreenOn`）：录音时屏幕灭掉会让人以为没在录。
 */
@Composable
fun MeetingRecordingScreen(vm: MainViewModel) {
    val note by vm.meetingsNote.collectAsState()
    // 手机这一侧的全部事实（本地电平 / 暂停 / 段数 / 补传进度）
    val mic by com.echo.contact.service.MeetingMicService.state.collectAsState()
    val context = LocalContext.current
    val view = LocalView.current

    // 从通知栏的「结束」停掉、或服务自己停下时，这个界面也要跟着退出
    // （否则会一直显示"录音中"，而麦克风早就关了）。
    // 刚开始那一刻 `mic.active` 也可能是 false（前台服务是异步起来的），
    // 所以只在**见过真的在录**之后再判"停了"。
    var sawRecording by remember { mutableStateOf(mic.active) }
    LaunchedEffect(mic.active) {
        if (mic.active) sawRecording = true
        else if (sawRecording) vm.closeRecordingScreen()
    }

    // 不熄屏（录音时屏幕灭掉最容易让人以为没录）
    DisposableEffect(Unit) {
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = false }
    }

    // 电平平滑：攻击快（跟得上说话）、回落慢（看着像声浪在退）
    var shown by remember { mutableStateOf(0f) }
    val target = mic.level.coerceIn(0f, 1f)
    LaunchedEffect(target) {
        shown = smoothLevel(shown, target)
    }

    val paused = mic.paused
    val accent = if (paused) EchoPalette.Yellow else EchoPalette.Red

    // 计时**每一秒重算一次**（用户 2026-10-17 真机问题①："暂停时计时器也不刷新时间了"）。
    //
    // 为什么不能只靠 `mic` 的状态变化：Compose 只在**状态变了**才重组，而暂停时电平是
    // **刻意不刷新**的（零帧不该让电平条抖）。旧版界面唯一的每 100ms 变化就是电平 ——
    // 于是暂停期间整块界面一次都不重组，计时看上去就冻住了（事实是时间在走、音频在累计）。
    // 两道保障：录制循环现在每帧推进 `mic.elapsedSeconds`（挂钟，暂停也推进），
    // 这里再加一道**每秒节拍**兜底 —— 万一某一帧没来（麦克风被系统掐一下），计时也不许停。
    var nowTick by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(mic.active) {
        if (!mic.active) return@LaunchedEffect
        while (true) {
            nowTick = System.currentTimeMillis()
            delay(1_000)
        }
    }

    Surface(modifier = Modifier.fillMaxSize(), color = EchoPalette.Bg) {
        Column(
            modifier = Modifier.fillMaxSize().padding(20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { vm.closeRecordingScreen() }) { Text("← 返回（继续录）") }
                Spacer(Modifier.weight(1f))
                Text(
                    // 暂停时也要把"时长还在累计"说在标题上 —— 只写"已暂停"会让人以为时间也停了
                    if (paused) "已暂停 · 时长仍在累计" else "录音中",
                    style = MaterialTheme.typography.titleMedium,
                    color = accent,
                    fontWeight = FontWeight.SemiBold,
                )
            }

            Spacer(Modifier.height(18.dp))

            // ---- 大计时：**挂钟**（从开始到现在，含暂停那一段）----
            // 开始时间永远是**手机自己记的**（这条路上没有"电脑给了开始时间才有值"那一档）。
            Text(
                elapsedLabel(mic.elapsedSeconds, mic.startedAt, nowTick),
                style = MaterialTheme.typography.displaySmall,
                fontWeight = FontWeight.Light,
            )
            // 本段进度：段边界只由**累计样本数**决定（暂停的零样本也照常计数），
            // 把"这一段攒到哪儿了"摆出来，就不会再有"恢复后不到一分钟就封段了"的错觉
            // —— 那一段其实在暂停期间就已经按挂钟攒满了（用户 2026-10-17 真机问题②）。
            segmentProgressText(mic.segmentElapsedSeconds, mic.segmentSeconds)
                .takeIf { it.isNotBlank() }?.let {
                    Spacer(Modifier.height(2.dp))
                    MutedLine(it, maxLines = 1)
                }
            Spacer(Modifier.height(4.dp))
            MutedLine(
                buildString {
                    // 这条路永远是"先落本地"：说清"录到几段、其中几段已经传上去了"
                    // —— 只说"已传 N 段"会让人以为本地没有副本（其实本地那份才是本体）。
                    val total = mic.syncPending + mic.syncConfirmed
                    append("手机麦克风 · 已缓存 ${if (total > 0) total else mic.syncPending} 段")
                    if (mic.syncConfirmed > 0) append("（已传 ${mic.syncConfirmed} 段）")
                    if (mic.formatNote.isNotBlank()) {
                        // 格式**如实**：退回 WAV 时要说清是哪一段、为什么（用户点名要求）
                        append(" · ${mic.formatNote}")
                    }
                },
                maxLines = 2,
            )

            // ---- 音频"传上去没有"：**如实的一行** + 「现在同步」----
            // 这一行是"音频到底有没有传上去"的唯一出口：连不上时先说"ECHO 连不上、
            // 音频先存在这台手机里"，绝不说成"已上传"。
            if (mic.syncNote.isNotBlank()) {
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
                    Text("已静音 · 这段写空白，时长照常累计", color = EchoPalette.Yellow,
                        style = MaterialTheme.typography.bodyMedium)
                }
            }

            Spacer(Modifier.height(10.dp))
            MutedLine(
                when {
                    // 暂停时把**三件事**说清（2026-10-17 的两个真机问题都在这一句里）：
                    // 内容是空白 ✓、时长照常累计 ✓、段也照常在封 ✓（所以恢复后可能马上就封一段，
                    // 那一小段是暂停期间按挂钟攒满的，不是"提前封"）。
                    paused -> "已暂停：这段写空白，时长照常累计、段照常在封；恢复后接着录"
                    // 这条路永远是"先落本地、再顺手传"：说清这一点（**不**说"离线"，
                    // 因为在线时也一样先落本地 —— 用户 2026-10-17 的原话是"没有本质区别"）
                    else -> "声音来自**这台手机的麦克风**：每一段先存在手机里，" +
                        "网通就顺手传给电脑（最多滞后一段），不通就留着等联网自动补传"
                },
                maxLines = 2,
            )
            note?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Yellow, maxLines = 2)
            }
            // 录音真的起不来时（最常见是没给麦克风权限），原因必须显示在这里 ——
            // 界面开着却什么都没录是最糟的状态，至少要说清为什么。
            mic.error.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Red, maxLines = 3)
            }

            Spacer(Modifier.weight(1f))

            // ---- 两个主按钮：静音/恢复、结束（两条都是**这台手机**的动作）----
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                OutlinedButton(
                    onClick = {
                        // 静音 = **照常录、写进去的是全零（空白）音频**，时间轴逐字连续、
                        // 总时长不受影响（用户 2026-10-16 深夜的口径，与电脑侧统一）。
                        // 这段区间**记进清单**；恢复时会把它上报给服务端
                        // （`POST /api/meeting/pauses`）。
                        // ⚠️ **不**调电脑的暂停接口：这条路上电脑不在场（它只负责落盘/转写），
                        // 调它只会拿到一个失败，而失败原因会写进界面 —— 明明静音已经生效了
                        // （把自己的缺点说成用户的错，最不该）。
                        com.echo.contact.service.MeetingMicService.setPaused(context, !paused)
                    },
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
                        // 结束 = 统一路：本地收尾 → 补齐剩余段 → 对账 → `stop(clientId)`。
                        // **不**调"让电脑停"的接口：手机发起的这场会由手机收尾。
                        com.echo.contact.service.MeetingMicService.stop(context)
                        // 界面立刻退出：剩下的补传进度在会议页上继续显示（那里是善后的事）。
                        vm.closeRecordingScreen()
                    },
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
                "结束后手机把剩下的段补齐、让 ECHO 收尾（收尾之后才开始转写）；" +
                    "传不完的会留着，联网后自动接着传（也可以点「现在同步」）",
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
    val startMs = epochOfLocal(s) ?: return "--:--"
    return elapsedTextOf(max(0L, (now - startMs) / 1000).toDouble())
}

/**
 * 秒数 → `mm:ss` / `h:mm:ss`（**唯一**的格式化处：`elapsedText`、大计时与本段进度都走它）。
 *
 * 为什么抽出来：计时原来只有一个入口（按开始时间 + 当前时刻算一次），而这一版还有两个
 * 来源 —— 录制循环报上来的**挂钟秒数**（`MicState.elapsedSeconds`）与本段进度。
 * 三处各写一遍格式，迟早会出现"大计时 01:05、本段 1:05"这种同屏不一致。
 */
internal fun elapsedTextOf(seconds: Double): String {
    val secs = max(0L, seconds.toLong())
    val h = secs / 3600
    val m = (secs % 3600) / 60
    val ss = secs % 60
    return if (h > 0) String.format("%d:%02d:%02d", h, m, ss) else String.format("%02d:%02d", m, ss)
}

/**
 * 界面上该显示的**累计时长**（用户 2026-10-17 真机问题①）。
 *
 * 取两个**挂钟**来源里较大的那个：
 *  1. `stateSeconds`：录制循环每帧推进的秒数（暂停帧、没读到数据的帧也推进）；
 *  2. 从 `startedAt` 到此刻的秒数：界面自己每秒节拍的兜底。
 *
 * 两者都是"从开录那一刻到现在的挂钟"，所以不会互相矛盾；取大只是为了让
 * "某一帧没来"时显示不会往回跳。读不懂 `startedAt` 时**不编 00:00**：
 * 只有录制循环真的报过秒数才用它，否则仍然是 `--:--`。
 */
internal fun elapsedLabel(stateSeconds: Double, startedAt: String?, now: Long): String {
    val s = startedAt?.trim().orEmpty()
    val startMs = if (s.isEmpty()) null else epochOfLocal(s)
    if (startMs == null) return if (stateSeconds > 0.0) elapsedTextOf(stateSeconds) else "--:--"
    val byClock = max(0.0, (now - startMs) / 1000.0)
    return elapsedTextOf(max(max(0.0, stateSeconds), byClock))
}

/**
 * "本段 00:12 / 00:30"（段长未知时回空串 —— **不编数字**）。
 *
 * 为什么要在界面上显示它：段是按累计样本数切的（暂停的零样本照常计数），所以
 * **暂停期间这一段也在攒**。用户看不到这件事时，恢复后立刻封一段就会被当成
 * "不到一分钟就封段了"（真机问题②）；把它摆出来，"这一段是暂停期间按挂钟攒满的"就一目了然。
 */
internal fun segmentProgressText(segmentElapsedSeconds: Double, segmentSeconds: Int): String {
    if (segmentSeconds <= 0) return ""
    val of = segmentElapsedSeconds.coerceIn(0.0, segmentSeconds.toDouble())
    return "本段 ${elapsedTextOf(of)} / ${elapsedTextOf(segmentSeconds.toDouble())}"
}

/** ISO 本地时间 → epoch 毫秒（读不懂回 null，**绝不猜**）。 */
private fun epochOfLocal(iso: String): Long? = try {
    java.time.LocalDateTime.parse(iso).atZone(java.time.ZoneId.systemDefault())
        .toInstant().toEpochMilli()
} catch (e: Exception) {
    null
}
