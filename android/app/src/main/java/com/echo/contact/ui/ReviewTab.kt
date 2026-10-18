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
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
 * 开不了麦时那句人话（回顾口述这条路**一个字都没开始**）。
 *
 * 为什么写在回顾页自己这里：`startRecording()` 失败时只把原因写进 `_speakNote`
 * （`MainViewModel.kt` 的注释自己就说了"以前只写 `_speakNote`，而没有任何界面读它"），
 * 回顾页又把它的返回值丢了 → 用户点「车里口述」看到的是**完全没反应**。
 * 现在这里检查返回值并就地显示（2026-10-18 复查）。
 */
internal const val REVIEW_MIC_BLOCKED =
    "开不了麦克风（可能被别的应用占着，或权限没给）：这次没开始录"

/**
 * 某一天没有回顾记录时的说明。
 *
 * 空页也要告诉用户下一步点哪儿 —— 只写"没有记录"等于把人晾在那儿。
 */
internal const val REVIEW_DAY_EMPTY =
    "这一天没有回顾记录。若是今天：返回后点「车里口述」录一段，第一条就会出现在这里。"

// ---- 失败必须看得见（2026-10-18 复查的五处之一/之二/之三）----
//
// 历史接口失败时，列表是空的 —— 而空列表原来只会说"还没有回顾历史。口述一次今天…"。
// 那是**假空态**：用户以为今天什么都没做，其实是一个字节都没读到。
// 所以失败要有一句自己的话，并且**摆在列表那一块**（用户点的就是那里）。

/** 历史接口失败时的前缀（判据见 `ReviewFailuresTest`）。 */
internal const val REVIEW_HISTORY_FAILED = "回顾历史读取失败"

/** 打开某一天失败时的前缀。 */
internal const val REVIEW_DAY_OPEN_FAILED = "打开这一天失败"

/**
 * 拼一句"失败 + 原因"，且**永不返回空话**。
 *
 * 为什么不直接 `"$prefix：${e.message}"`：`e.message` 可能是 null（`UnknownHostException`
 * 有时就是这样），那样拼出来的是一句"打开这一天失败：null"—— 看着像原因，其实什么都没说。
 */
internal fun reviewFailureLine(prefix: String, reason: String?): String {
    val detail = reason?.trim().orEmpty().ifEmpty { "未知原因（没拿到错误信息）" }
    return "$prefix：$detail"
}

// ---- 停主机回顾：服务端如实回的 `stopped` 三种话术 ----

/** `stopped=true`：主机上那场回顾真的停了。 */
internal const val REVIEW_STOPPED = "已停止主机上的回顾"

/** `stopped=false`：主机上**没有**在跑的回顾 —— 别把"确认了一下"说成"停掉了"。 */
internal const val REVIEW_STOP_NOT_RUNNING = "主机上这场回顾已经不在跑了（没有可停的）"

/** 拿不到 `stopped` 字段（老服务端/回包形状不对）：退回原来那句话，**不编**。 */
internal const val REVIEW_STOP_REQUESTED = "已请求停止主机上的回顾"

/**
 * 从 `POST /api/daily-review/stop` 的回包里取 `stopped`（`{ok, stopped}`）。
 *
 * 只认**真正的布尔**：`"true"` 字符串 / `0` / `1` 一律当"拿不到"（退回原话术）——
 * 猜一个真假比说"已请求"更容易骗到人。Gson 把 JSON 的 true/false 解成 `Boolean`。
 */
internal fun stoppedFlag(body: Map<String, Any>?): Boolean? = body?.get("stopped") as? Boolean

/** [stoppedFlag] 的结果 → 界面上那句话（三态；`null` 不编）。 */
internal fun reviewStopNote(stopped: Boolean?): String = when (stopped) {
    true -> REVIEW_STOPPED
    false -> REVIEW_STOP_NOT_RUNNING
    null -> REVIEW_STOP_REQUESTED
}

/** 点「今日回顾」会发生什么（纯数据；判据见 `ReviewEntryActionsTest`）。 */
internal sealed interface ReviewTodayTap {
    /** 打开今天的详情（真的会发一次 `GET /api/daily-review/history/{date}` 并进详情页）。 */
    data class OpenDay(val date: String) : ReviewTodayTap

    /** 今天还没有回顾 / 状态还没读到：**不装样子**，就地给一句可操作的话。 */
    data class Note(val message: String) : ReviewTodayTap
}

/**
 * 「今日回顾」卡点下去该做什么。
 *
 * 为什么要有这个函数（2026-10-18 复查）：这张卡原来点下去只是
 * `loadReview() + loadReviewDays()` —— 也就是**把状态重新拉一遍**，屏幕上什么都不变。
 * 对一个"功能卡"来说那等于**空壳按钮**（用户看不到任何反应）。现在：
 *   * 状态读到了、今天有轮次 → 打开今天的详情（看今天到底说了什么、播报了什么）；
 *   * 今天还没有 / 还没读到 → 如实说一句，并顺手刷新一次状态。
 */
internal fun reviewTodayTap(
    today: String,
    summaryLoaded: Boolean,
    todayTurns: Int,
): ReviewTodayTap = when {
    !summaryLoaded || today.isBlank() ->
        ReviewTodayTap.Note("还没读到今天的回顾状态：先点「刷新」，或检查与电脑的连接")
    todayTurns <= 0 ->
        ReviewTodayTap.Note("今天还没有回顾：点「车里口述」录一段，第一条就会出现在这里")
    else -> ReviewTodayTap.OpenDay(today)
}

/**
 * 某天详情页的副标题。
 *
 * **刻意不说"已写入工作日志"**：接口只能给"当天那个文件在不在"（`vaultNoteExists`），
 * 那不证明**这一轮**被写进去了 —— 那天的日志可能只是会议归档/手动记录建的。
 * 把"文件在"说成"这一轮已写入"就是一次假成功（2026-10-18 复查改的口径）。
 */
internal fun reviewDaySubtitle(count: Int, vaultNoteExists: Boolean): String =
    "$count 轮 · " + if (vaultNoteExists) "当天工作日志已存在" else "当天工作日志还没生成"

/**
 * 某一轮的"登记核实"提示（`turns[].registered`，服务端比的当天日志指纹）。
 *
 * `null` = 老记录（那时还没有这条核实）→ **不猜、不说话**；`true` 也不炫耀
 * （"检测到日志变了"不等于"这一轮的内容写对了"）；`false` 才必须说。
 */
internal fun reviewTurnRegistryNote(registered: Boolean?): String =
    if (registered == false) "这一轮没检测到工作日志更新（技能可能没装，或这次没有可登记的内容）"
    else ""


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
 *
 * @param todayFailed    今天**失败**的轮数（`todayTurns` 把失败的也算进去了）。
 *   不分开说就是"今天已回顾 3 轮"这种假繁荣：真实库里 2026-10-14 那天两轮**全失败**，
 *   卡片却会说"已回顾 2 轮"。
 * @param summaryLoaded  `GET /api/daily-review/summary` 到底读到了没有。
 *   读失败时 `todayTurns`/`hasBrief` 都是默认的 0/空 —— 那不是"没有回顾"，
 *   是"没读到"；说成"还没有可念的播报"同样是假成功（2026-10-18 复查）。
 */
internal fun reviewFeatureTiles(
    running: Boolean,
    enabled: Boolean,
    ready: Boolean,
    todayTurns: Int,
    hasBrief: Boolean,
    speaking: Boolean,
    todayFailed: Int = 0,
    summaryLoaded: Boolean = true,
): List<ReviewFeatureTile> = listOf(
    ReviewFeatureTile(
        feature = ReviewFeature.TODAY,
        subtitle = when {
            running -> "进行中"
            !enabled -> "未启用"
            !ready -> "未就绪"
            !summaryLoaded -> "还没读到今天的状态"
            todayTurns > 0 && todayFailed > 0 -> "今天已回顾 $todayTurns 轮（$todayFailed 轮失败）"
            todayTurns > 0 -> "今天已回顾 $todayTurns 轮"
            else -> "今天还没回顾"
        },
        // 点它 = 看今天的详情（有轮次）或如实说一句（没有/没读到），见 reviewTodayTap
        enabled = true,
    ),
    ReviewFeatureTile(
        feature = ReviewFeature.DICTATE,
        subtitle = "录一段口述，ECHO 转写并整理",
        enabled = true,
    ),
    ReviewFeatureTile(
        feature = ReviewFeature.SPEAK,
        subtitle = when {
            hasBrief -> "念今天 / 最近一次的播报"
            !summaryLoaded -> "还没读到播报（点「刷新」）"
            else -> "还没有可念的播报"
        },
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
    // 历史列表自己的失败原因（读取失败 / 打开某一天失败）——**必须可见**，
    // 否则空列表就冒充"还没有回顾历史"（见 `reviewFailureLine` 的说明）。
    val daysNote by vm.reviewDaysNote.collectAsState()
    val day by vm.reviewDay.collectAsState()
    val speaking by vm.speaking.collectAsState()
    val recording by vm.recording.collectAsState()
    val recordMs by vm.recordMs.collectAsState()
    val submitting by vm.reviewSubmitting.collectAsState()
    val submittingNote by vm.reviewSubmittingNote.collectAsState()
    val voiceText by vm.voiceText.collectAsState()
    // 朗读那条路的说明（没装 TTS / 没有可念的播报 / 取不到当天详情）以前写在
    // `_speakNote` 里而**全 App 没有界面读它** → 按「播报」没声音也没提示。
    // 这里把它显示出来（`speak()` 每次开口前会清空，所以不会挂着过期的失败原因）。
    val speakNote by vm.speakNote.collectAsState()

    // 这一页**自己**的两句反馈（不改 ViewModel 的状态机）：
    //  · 点「今日回顾」但今天没有/没读到 → 那句话就在这儿；
    //  · 点「车里口述」但麦开不了 → 原因也在这儿（以前是被丢掉的返回值）。
    var localNote by remember { mutableStateOf<String?>(null) }

    val context = LocalContext.current
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            // 授权拿到也可能开不了麦（被别的应用占着）：返回值必须看
            if (!vm.startRecording()) localNote = REVIEW_MIC_BLOCKED else localNote = null
        }
    }

    if (day != null) {
        ReviewDayView(
            detail = day!!,
            speaking = speaking,
            speakNote = speakNote,
            onSpeak = { vm.speakReviewDay(day!!.date) },
            onSpeakText = { vm.speak(it) },
            onDismissSpeakNote = { vm.clearSpeakNote() },
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
        todayFailed = summary?.todayFailed ?: 0,
        summaryLoaded = summary != null,
    )
    val orderedDays = reviewDaysDescending(days)

    // 口述：没授权就先要（与「指令」页同一套做法），授权成功再开始。
    // **返回值要判**：`startRecording()` 返回 false = 麦打不开，这一次什么都没开始。
    val startDictation: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) {
            if (!vm.startRecording()) localNote = REVIEW_MIC_BLOCKED else localNote = null
        } else {
            permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
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
                                // 今日回顾：有轮次就**打开今天的详情**（真的发一次请求），
                                // 没有/没读到就如实说一句 —— 不再只是"重新拉一次状态"。
                                ReviewFeature.TODAY -> when (
                                    val tap = reviewTodayTap(
                                        today = summary?.today.orEmpty(),
                                        summaryLoaded = summary != null,
                                        todayTurns = summary?.todayTurns ?: 0,
                                    )
                                ) {
                                    is ReviewTodayTap.OpenDay -> {
                                        localNote = null
                                        vm.openReviewDay(tap.date)
                                    }
                                    is ReviewTodayTap.Note -> {
                                        localNote = tap.message
                                        vm.loadReview()
                                        vm.loadReviewDays()
                                    }
                                }
                                ReviewFeature.DICTATE -> startDictation()
                                ReviewFeature.SPEAK -> vm.speakReview()
                            }
                        },
                    )
                }
            }
        }

        // 上面那句"点了但没发生任何事"的说明（今天没轮次 / 麦开不了）
        localNote?.takeIf { it.isNotBlank() }?.let {
            item { MutedLine(it, color = EchoPalette.Yellow, maxLines = 3) }
        }

        // ---- 今日回顾详情：最近一次播报 + 念 / 刷新 / 停 ----
        item {
            ReviewStatusCard(
                review = review,
                summary = summary,
                note = note,
                speakNote = speakNote,
                speaking = speaking,
                onSpeak = { vm.speakReview() },
                onRefresh = {
                    vm.loadReview()
                    vm.loadReviewDays()
                },
                onStop = { vm.stopSpeaking() },
                onStopHost = { vm.stopHostReview() },
                onDismissSpeakNote = { vm.clearSpeakNote() },
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

        // 历史接口失败 / 打开某一天失败：**就地**摆在列表这一块（用户点的就是这里）。
        // 有这句话时下面不再画"还没有回顾历史"——那句会把"读不到"说成"没有"。
        daysNote?.takeIf { it.isNotBlank() }?.let {
            item { MutedLine(it, color = EchoPalette.Yellow, maxLines = 3) }
        }

        if (orderedDays.isEmpty()) {
            // 空列表只有在**真的读到了**（没有被失败提示占着）时才是"还没有历史"
            if (daysNote.isNullOrBlank()) {
                item { MutedLine(REVIEW_HISTORY_EMPTY, maxLines = 3) }
            }
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
    speakNote: String?,
    speaking: Boolean,
    onSpeak: () -> Unit,
    onRefresh: () -> Unit,
    onStop: () -> Unit,
    onStopHost: () -> Unit,
    onDismissSpeakNote: () -> Unit,
) {
    val running = review?.running == true
    val enabled = review?.enabled == true
    val ready = review?.ready == true
    val turns = summary?.todayTurns ?: 0
    val failed = summary?.todayFailed ?: 0
    val (label, color) = when {
        running -> "进行中" to EchoPalette.Purple
        !enabled -> "未启用" to EchoPalette.Muted
        !ready -> "未就绪" to EchoPalette.Yellow
        // 失败轮也算在 `todayTurns` 里：不分开说就是"今天已回顾 N 轮"的假繁荣
        turns > 0 && failed > 0 -> "今天已回顾 $turns 轮（$failed 轮失败）" to EchoPalette.Yellow
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
            } else {
                // 读不到摘要 ≠ 没有回顾：这两件事以前都显示成"还没有回顾记录"
                Spacer(Modifier.height(6.dp))
                MutedLine("还没读到回顾状态（点「刷新」或检查与电脑的连接）", color = EchoPalette.Yellow)
            }

            note?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Yellow)
            }

            // 朗读那条路的说明：没有它，"念"没出声时用户看到的是"点了没反应"
            // （系统 TTS 没装 / 缺中文数据 / ECHO 合成失败 / 没有可念的播报都会走到这里）。
            // 单行 + 几秒后自动消失（`SpeakNoteLine`）：它是"这一下"的反馈，不是常驻状态。
            speakNote?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                SpeakNoteLine(it, onDismiss = onDismissSpeakNote)
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
    speakNote: String?,
    onSpeak: () -> Unit,
    onSpeakText: (String) -> Unit,
    onDismissSpeakNote: () -> Unit,
    onBack: () -> Unit,
) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "返回") }
            Column(modifier = Modifier.weight(1f)) {
                Text(detail.date, style = MaterialTheme.typography.titleMedium)
                // 不说"已写入工作日志"：`vaultNoteExists` 只说明那天的文件在（见 reviewDaySubtitle）
                MutedLine(reviewDaySubtitle(detail.count, detail.vaultNoteExists))
            }
            IconButton(onClick = onSpeak, enabled = detail.ok && !speaking) {
                Icon(Icons.Default.VolumeUp, contentDescription = "念这天的播报")
            }
        }

        // 服务端明确回了 ok=false（坏日期 / 读不到）时**如实说**，别画成"这天没有记录"
        if (!detail.ok) {
            Spacer(Modifier.height(8.dp))
            MutedLine(detail.error.ifBlank { "读不到这一天的回顾（服务端没给原因）" },
                color = EchoPalette.Red, maxLines = 3)
            return
        }

        // 朗读那条路的说明（没有它，"念"不出声时就是"点了没反应"）
        speakNote?.takeIf { it.isNotBlank() }?.let {
            Spacer(Modifier.height(6.dp))
            SpeakNoteLine(it, onDismiss = onDismissSpeakNote)
        }

        Spacer(Modifier.height(8.dp))
        if (detail.turns.isEmpty()) {
            MutedLine(REVIEW_DAY_EMPTY)
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
                        // 登记核实（服务端比的当天日志指纹）：没写进去就**说清楚**，
                        // 别让"整理完成"顶替"已登记"。
                        reviewTurnRegistryNote(t.registered).takeIf { it.isNotBlank() }?.let {
                            Spacer(Modifier.height(4.dp))
                            MutedLine(it, color = EchoPalette.Yellow, maxLines = 3)
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
