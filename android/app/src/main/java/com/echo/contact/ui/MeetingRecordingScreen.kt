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
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay
import kotlin.math.max

/** 主按钮的高度（dp）。**界面与布局判据共用这一个数**：按钮不许被文字挤掉，
 * 所以"文字区 + 这个高度 + 底部留白"必须装得进屏幕（见 [recordingButtonsVisible]）。
 */
internal const val RECORDING_BUTTON_HEIGHT_DP = 56

/** 主界面上状态行只许有**一行**（用户 2026-10-18："简洁些就行，内容把按钮都盖住了"）。 */
internal const val RECORDING_STATUS_MAX_LINES = 1

/** 错误行是例外（录不起来必须说原因），但最多两行，且要算进按钮的高度预算。 */
internal const val RECORDING_ERROR_MAX_LINES = 2

/**
 * 主界面里**除状态行/错误行/按钮之外**那些固定块占的高度（dp）。
 *
 * 2026-10-20 按"电平加高"后的布局重算（加高是用户点名的："上下空间占得更高些"）：
 * 上下 padding 40 + 顶栏 48 + 标题 24 + 间距 6+18 + 大计时 44 + 本段进度 18
 * + 状态行上方间距 4 + 电平前间距 28 + 电平带 [RECORDING_LEVEL_BAND_DP]（100 + 2×24 = 148）
 * + 按钮上方至少 10 + 按钮下方留白 6 = **394**。
 *
 * 沿革：最旧那一版 426（电平区占 180，等于**没有**波形的位置），上一版 358（电平带 112）。
 * 这一版把电平从 64dp 加高到 100dp（带子 112 → 148），所以预算回到 394 —— 但**仍比 426 矮**，
 * 大计时 / 本段进度 / 状态行 / 两个 56dp 按钮一个都没动。
 *
 * 为什么固化成两个常量（而不是写一个死数）：**电平高度是"尺寸的唯一来源"**
 * （`RECORDING_LEVEL_HEIGHT_DP`），预算必须从它**推导**出来，否则哪天电平改高度、
 * 预算没跟着改，就会同时犯两种错 —— 小屏按钮被挤掉（预算偏小）或凭空多出一段空白（偏大）。
 *
 * 为什么把它抽成一个数：旧版把 5 段解释性文字（最多十几行）全堆在按钮上方，
 * 小屏上"文字 + 电平 + 按钮"超过了屏幕高度，于是按钮被挤出可视区
 * （用户截图里正是"文字盖住按钮"）。现在解释性内容只放在 ⓘ 弹窗里，
 * 主界面只留一行状态 —— 这个预算函数就是"按钮完整可见"的可测判据。
 * 加高之后这条判据**一条没放松**：小屏（可用 568dp）上仍然是
 * 394 + 一行状态 16 + 两行错误 32 + 按钮 56 = **498dp**，还留 70dp 余量。
 */
internal const val RECORDING_CHROME_AREA_DP = 246

/** 文字区预算 = 除电平外的固定块 + 电平带（电平高度变了，这里自动跟着变）。 */
internal const val RECORDING_BASE_AREA_DP = RECORDING_CHROME_AREA_DP + RECORDING_LEVEL_BAND_DP

/** 一行小字的高度（dp，labelSmall/bodyMedium 一行约 16dp）。 */
internal const val RECORDING_LINE_DP = 16

/**
 * **全屏会议录音界面**（用户 2026-10-15 要的）：好看的实时电平 + 结束 / 静音（暂停）· 恢复，
 * 外加已录时长与段数。
 *
 * ## 2026-10-18 收口：主界面只给"正在录的人"看
 *
 * 用户原话："全屏界面里不用加这些啰嗦的说明，简洁些就行，内容把按钮都盖住了"。
 * 所以这一版的主界面**只有一行状态**（[recordingStatusLine]），解释性内容全部收进
 * 右上角 ⓘ 的弹窗（[recordingInfoLines]）：**主界面是给正在录的人看的，不是文档**。
 * 大计时 / 本段进度 / 电平条 / 两个按钮一个都没少。
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

    val paused = mic.paused
    val accent = if (paused) EchoPalette.Yellow else EchoPalette.Red

    // 解释性内容默认收起来（用户 2026-10-18：主界面别被文字占满）
    var showInfo by remember { mutableStateOf(false) }

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
            // ---- 顶栏：返回 / （有欠段时）现在同步 / ⓘ 说明 ----
            // 说明性长文只在 ⓘ 里出现：主界面留给大计时、电平与按钮。
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = { vm.closeRecordingScreen() }) { Text("← 返回") }
                Spacer(Modifier.weight(1f))
                if (mic.syncPending > 0) {
                    TextButton(
                        enabled = !mic.syncBusy,
                        onClick = {
                            // 复用服务里那一条补传路（不在这里另写上传逻辑）
                            com.echo.contact.service.MeetingMicService.syncNow(context)
                        },
                    ) { Text(if (mic.syncBusy) "同步中…" else "现在同步") }
                }
                IconButton(onClick = { showInfo = true }) {
                    Icon(
                        Icons.Default.Info,
                        contentDescription = "录音说明",
                        tint = EchoPalette.Muted,
                    )
                }
            }

            Spacer(Modifier.height(6.dp))
            // 暂停时明确说"时长在累计"：只说"已暂停"会让人以为时间也停了
            // （暂停 = 照常录、写全零，时间轴逐字连续 —— 用户 2026-10-16 深夜的口径）。
            Text(
                recordingTitle(paused),
                style = MaterialTheme.typography.titleMedium,
                color = accent,
                fontWeight = FontWeight.SemiBold,
            )

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
            // ---- 唯一的精简状态行（不再堆"已缓存/已传/格式/补传说明"那一堆）----
            Text(
                recordingStatusLine(
                    cached = mic.syncPending + mic.syncConfirmed,
                    pending = mic.syncPending,
                ),
                style = MaterialTheme.typography.bodyMedium,
                color = EchoPalette.Muted,
                maxLines = RECORDING_STATUS_MAX_LINES,
            )

            // 录音真的起不来时（最常见是没给麦克风权限），原因必须显示在这里 ——
            // 界面开着却什么都没录是最糟的状态，至少要说清为什么。
            // 这是**错误**，不是解释性文字：它照旧显示在主界面（最多两行）。
            mic.error.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Red, maxLines = RECORDING_ERROR_MAX_LINES)
            }

            Spacer(Modifier.height(28.dp))

            // ---- 电平：**会滚动的柱状波形**（2026-10-20 重做；用户两次说"不要呼吸、要波形"）----
            // 旧版是"一根等厚胶囊 + 安静时呼吸"，用户觉得"温、看着没反应"。
            // 现在整个绘制交给 `RecordingLevelMeter`（近两秒的波形 + 更快包络 + 峰值保持），
            // 这里只负责"占位"：带子 148dp = 100dp 的电平 + 上下各 24dp 的呼吸。
            // 加高是用户点名的（"上下空间占得更高些"）；带子加高后预算变 394dp，
            // 小屏 568dp 上仍然装得下两个 56dp 按钮（见 RECORDING_BASE_AREA_DP 与用例）。
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(RECORDING_LEVEL_BAND_DP.dp)
                    .clip(RoundedCornerShape(18.dp))
                    .background(EchoPalette.Card2)
                    .padding(horizontal = 18.dp, vertical = RECORDING_LEVEL_PADDING_DP.dp),
                contentAlignment = Alignment.Center,
            ) {
                RecordingLevelMeter(
                    rawLevel = mic.level,
                    paused = paused,
                    // 一场一个 key：换一场（重开一段）时包络与峰值一起清零 ——
                    // 否则界面上会挂着上一场"根本没响过"的那根峰值线。
                    sessionKey = mic.startedAt,
                )
            }

            // 按钮上方至少留 10dp（`weight(1f)` 在小屏上会被压到 0，那时按钮就贴着电平了）
            Spacer(Modifier.height(10.dp))
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
                    modifier = Modifier.weight(1f).height(RECORDING_BUTTON_HEIGHT_DP.dp),
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
                    modifier = Modifier.weight(1f).height(RECORDING_BUTTON_HEIGHT_DP.dp),
                ) {
                    Icon(Icons.Default.Stop, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("结束")
                }
            }
            Spacer(Modifier.height(6.dp))
        }

        // 解释性内容收在这里：点 ⓘ 才出现，**不占主界面、不盖按钮**。
        if (showInfo) {
            val lines = recordingInfoLines(
                paused = paused,
                syncNote = mic.syncNote,
                formatNote = mic.formatNote,
                note = note,
                segmentSeconds = mic.segmentSeconds,
            )
            AlertDialog(
                onDismissRequest = { showInfo = false },
                confirmButton = {
                    TextButton(onClick = { showInfo = false }) { Text("知道了") }
                },
                title = { Text("录音说明") },
                text = {
                    Column {
                        lines.forEach { line ->
                            Text(
                                line,
                                style = MaterialTheme.typography.bodySmall,
                                color = EchoPalette.Muted,
                            )
                            Spacer(Modifier.height(6.dp))
                        }
                    }
                },
            )
        }
    }
}

/**
 * 主界面上**唯一**那一行状态（纯函数，`RecordingUiTest` 钉住）。
 *
 * 用户 2026-10-18 点名的形态：`本机录音中 · 已缓存 1 段 · 1 段待传`；
 * **没有待传就不提**（不让"0 段待传"这种废话占位置）。
 * 缓冲里一段都没有时只留"本机录音中"——不编一个"已缓存 0 段"出来。
 */
internal fun recordingStatusLine(cached: Int, pending: Int): String {
    val c = cached.coerceAtLeast(0)
    val p = pending.coerceAtLeast(0)
    return buildString {
        append("本机录音中")
        if (c > 0) append(" · 已缓存 $c 段")
        if (p > 0) append(" · $p 段待传")
    }
}

/**
 * 标题（纯函数）。
 *
 * 暂停时直接写"已暂停 · 时长在累计"：暂停 = 照常录音、写全零、总时长不受影响，
 * 只写"已暂停"会让人以为时间也停了（用户 2026-10-17 的真机问题①）。
 */
internal fun recordingTitle(paused: Boolean): String =
    if (paused) "已暂停 · 时长在累计" else "录音中"

/**
 * 点 ⓘ 才显示的说明（纯函数）。
 *
 * 这些内容**不能丢**（用户 2026-10-18：解释性内容不要丢，收到帮助里或 ⓘ 里），
 * 但它们也不该占着主界面 —— 主界面是给正在录的人看的，不是文档。
 */
internal fun recordingInfoLines(
    paused: Boolean,
    syncNote: String,
    formatNote: String,
    note: String?,
    /** 当前段长（秒；0 = 还没读到设置，就按默认 10 分钟说）。 */
    segmentSeconds: Int = 0,
): List<String> = buildList {
    add("返回只关这个界面，不停录音；要停就按「结束」。")
    // 滞后多久**按当前段长说**（默认 600 秒 = 10 分钟）：说"默认 10 分钟"而用户选了 30 秒，
    // 就是一句不准的话 —— 这个界面上的说明宁可短，也不能错。
    val lag = if (segmentSeconds > 0) {
        "最多滞后一个段长（当前 ${elapsedTextOf(segmentSeconds.toDouble())}）"
    } else {
        "最多滞后一个段长（默认 10:00）"
    }
    add("声音来自这台手机的麦克风：每段先存在手机里，网通就顺手传给电脑" +
        "（$lag），不通就留着、联网后自动补传。")
    if (paused) add("已暂停：这段写空白，时长照常累计、段照常在封；恢复后接着录。")
    add("结束时手机把剩下的段补齐（不足一段补成尾段），ECHO 收尾后才开始转写。")
    // 同步与格式的**如实**说明（这几条以前直接摊在主界面上）
    syncNote.takeIf { it.isNotBlank() }?.let { add(it) }
    formatNote.takeIf { it.isNotBlank() }?.let { add(it) }
    note?.takeIf { it.isNotBlank() }?.let { add(it) }
}

/**
 * 主界面文字区（除按钮外）需要的高度（dp）。`statusLines` 是状态行数、
 * `errorLines` 是错误行数 —— 两者都是**会变化的事实**，所以要算进预算。
 */
internal fun recordingTextAreaDp(statusLines: Int, errorLines: Int = 0): Int =
    RECORDING_BASE_AREA_DP +
        (statusLines.coerceAtLeast(0) + errorLines.coerceAtLeast(0)) * RECORDING_LINE_DP

/**
 * "文字区 + 按钮"装不装得进这块屏幕（**布局断言**：按钮完整可见、不被文字挤掉）。
 *
 * 为什么用纯函数而不是仪器化布局测试：这个工程的门禁是 JVM 单测（没有 androidTest），
 * 而"按钮会不会被文字挤出去"其实是**高度的加减法**。这里把布局里的真实块高度
 * （[RECORDING_BASE_AREA_DP] / [RECORDING_LINE_DP] / [RECORDING_BUTTON_HEIGHT_DP]）
 * 变成可测的判据：改回"把一堆说明摊在主界面"时，这条用例会先红。
 */
internal fun recordingButtonsVisible(
    screenHeightDp: Int,
    statusLines: Int,
    errorLines: Int = 0,
): Boolean = recordingTextAreaDp(statusLines, errorLines) + RECORDING_BUTTON_HEIGHT_DP <= screenHeightDp

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
 * "本段 00:12 / 10:00"（段长未知时回空串 —— **不编数字**）。
 *
 * 为什么要在界面上显示它：段是按累计样本数切的（暂停的零样本照常计数），所以
 * **暂停期间这一段也在攒**。用户看不到这件事时，恢复后立刻封一段就会被当成
 * "不到一分钟就封段了"（真机问题②）；把它摆出来，"这一段是暂停期间按挂钟攒满的"就一目了然。
 * 段长默认 600 秒（与电脑一致），所以这里会显示 `/ 10:00`。
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
