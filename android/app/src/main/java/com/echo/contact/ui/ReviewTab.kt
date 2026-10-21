package com.echo.contact.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
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
internal const val REVIEW_HISTORY_EMPTY =
    "还没有回顾历史。点「开始回顾」开始一场，这里就会出现第一天。"

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
    "这一天没有回顾记录。若是今天：返回后点「开始回顾」开始一场，第一条就会出现在这里。"

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
        ReviewTodayTap.Note("今天还没有回顾：点「开始回顾」开始一场，第一条就会出现在这里")
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
 * 回顾页：**顶部功能区 + 回顾对话 + 下面历史回顾列表**（用户 2026-10-18 点名的形态）。
 *
 * 手机回顾 = PC 版那套交互（开始 → 一轮轮说/打 → 结束），但**用手机的麦**：
 * 点「开始回顾」只建/取回今天的回顾会话（`/api/daily-review/start`，不碰电脑的麦）；
 * 每一轮语音走 `/api/daily-review/turn`、打字走 `/api/daily-review/submit` ——
 * 两条都是 `daily_review.submit()` 的入口，**不进指令空间**；念用手机系统 TTS。
 *
 * 上一版是"一张口述卡 + 一张状态卡 + 一整页列表"；现在把三件事提成顶部功能区
 * （今日回顾 / 车里口述 / 播报），下面才是历史 —— 与超级助理页的仪表盘形态对齐。
 * 「车里口述」保留为**退路**（一次性口述），主路径是上面的「开始回顾」。
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
    // 「口述回顾」这条路自己的说明（没权限 / 麦被会议录音占着 / 被"一直听着"占着 / 开不了麦 /
    // 已经开始录但收不到声音 / 取消了）。以前这些要么只进了 `_speakNote`（没人读），
    // 要么在"权限没给"那条分支里**什么都不做** —— 用户看到的就是"按了没反应"。
    val dictateNote by vm.dictateNote.collectAsState()
    val voiceText by vm.voiceText.collectAsState()
    // ---- 手机回顾 = PC 版那套交互（开始 → 多轮 → 结束）----
    //
    // 这一组状态是**新的主路径**：见 `ReviewConversation.kt` 的说明。
    // 原来那条"录一段长口述一次性交上去"的路**保留**，但降级成退路（`REVIEW_DICTATE_FALLBACK`）。
    val session by vm.reviewSession.collectAsState()
    val sessionNote by vm.reviewSessionNote.collectAsState()
    val reviewStarting by vm.reviewStarting.collectAsState()
    val reviewEnded by vm.reviewEnded.collectAsState()
    // 这一轮的进度/失败原因（语音上传 + 回顾整理走 /turn、/submit，不再进指令链）
    val roundBusy by vm.reviewRoundBusy.collectAsState()
    val roundNote by vm.reviewRoundNote.collectAsState()
    // 朗读那条路的说明（没装 TTS / 没有可念的播报 / 取不到当天详情）以前写在
    // `_speakNote` 里而**全 App 没有界面读它** → 按「播报」没声音也没提示。
    // 这里把它显示出来（`speak()` 每次开口前会清空，所以不会挂着过期的失败原因）。
    val speakNote by vm.speakNote.collectAsState()

    // 这一页**自己**的两句反馈（不改 ViewModel 的状态机）：
    //  · 点「今日回顾」但今天没有/没读到 → 那句话就在这儿；
    //  · 点「车里口述」但麦开不了 → 原因也在这儿（以前是被丢掉的返回值）。
    var localNote by remember { mutableStateOf<String?>(null) }

    // 进行中对话的输入框（打字继续这一场）。放在这里而不是输入条内部：
    // 输入条会在每次"消息列表变化"时重组，状态留在这里才不会被清空。
    var reviewInput by remember { mutableStateOf("") }

    val context = LocalContext.current
    // 权限回调**一定**再去试一次：`startReviewDictation()` 自己查权限，
    // 所以"用户拒绝"那条路也会在按钮旁边留下一句"没给麦克风权限"（以前是静默 return）。
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { vm.startReviewDictation() }

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

    // 这场回顾是不是**正在进行中**。
    // 结束后会话**保留**（「刚才那一场」只读卡），所以不能只看 `session != null`。
    val reviewActive = session != null && !reviewEnded

    // 「按住说话」那一轮没给权限时的补授权：授权回来**就开始这一轮**。
    // 为什么不假装"松手即完成"：授权弹窗关掉时手指早已抬起，硬走 finish 只会得到
    // "没在录音"。所以开始之后输入条一直显示"录音中 x 秒 · 说完了"，用户点它收尾。
    val roundMicLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) vm.startReviewRound() }

    // 三个主入口都是**现有能力**（见 `reviewFeatureTiles`）。
    // 进行中时先把「车里口述」那颗**藏起来**：这场回顾自己有一条"录一轮"的路
    // （输入条上的按住说话），再摆一颗会开成**一次性口述**（走 submit）—— 两条录音路
    // 混在同一场里，用户分不清刚才那句算不算这一轮。退路写在开始卡上。
    val tiles = reviewFeatureTiles(
        running = review?.running == true || reviewActive,
        enabled = review?.enabled == true,
        ready = review?.ready == true,
        todayTurns = summary?.todayTurns ?: 0,
        hasBrief = !summary?.lastBrief.isNullOrBlank(),
        speaking = speaking,
        todayFailed = summary?.todayFailed ?: 0,
        summaryLoaded = summary != null,
    ).filterNot { reviewActive && it.feature == ReviewFeature.DICTATE }
    val orderedDays = reviewDaysDescending(days)

    // 口述：没授权就先要（与「指令」页同一套做法），授权回来再走同一条判定。
    // 走的是 `vm.startReviewDictation()` —— 这一条**就是**「口述回顾」的唯一入口：
    // 它内部先判权限/占用（`dictationGate`），通过后再调既有的 `startRecording()`。
    // 所以这里**不许**再自己判 `granted`：权限没给时也必须有话（一句话在按钮旁边）。
    val startDictation: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) {
            vm.startReviewDictation()
        } else {
            // 系统弹窗这一下不可能"静默"：回调里一定会再调一次并把原因说出来
            permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    // 进行中那一轮的开录入口：与「车里口述」同一条判定/同一条录音实现，
    // 只是身份是"这一轮"（`startReviewRound` → 唯一那处 `startRecording`）。
    val startRound: () -> Boolean = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) {
            vm.startReviewRound()
        } else {
            roundMicLauncher.launch(Manifest.permission.RECORD_AUDIO)
            false
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

        // ---- 手机回顾：开始 → 多轮对话 → 结束（与 PC 版同一套交互）----
        //
        // 用户 2026-10-19 澄清："我是说跟 pc 版的交互一样，开始回顾开始然后继续交互"。
        // 所以这一块不是"录一段长口述"，而是一场**对话**。三块互斥：
        //  · 没开始 / 已结束 → **开始卡**（按钮 + 原因）；"失败"与"结束那句"都落在这里；
        //  · 已结束 → 再加一张「刚才那一场」**只读卡**（只写一句去向：这几轮已记入
        //    下面的「历史回顾」，见 REVIEW_LAST_ROUNDS_NOTE —— 2026-10-20 起手机
        //    回顾轮次带 context=review，服务端记 source='review'，回顾历史会收到它们）；
        //  · 进行中 → 顶部「回顾进行中」+ 轮数 + 结束按钮，下面是一来一回的消息，
        //    最下面是输入条（打字 → 回顾 submit；按住说话 → 回顾 turn）。
        if (!reviewActive) {
            item {
                ReviewStartCard(
                    starting = reviewStarting,
                    ended = reviewEnded,
                    note = sessionNote,
                    onStart = { vm.startReviewSession() },
                )
            }
        }
        if (session != null && reviewEnded) {
            item { ReviewLastSessionCard(session!!) }
        }
        if (reviewActive) {
            item {
                ReviewRunningCard(
                    session = session!!,
                    todayTurns = summary?.todayTurns ?: 0,
                    note = sessionNote,
                    // 录一轮的"没开始"原因（没权限/被占/这场结束了…）与
                    // 上一轮整理的进度/失败原因都摆在顶部 —— 用户就在看这里
                    roundNote = roundNote,
                    roundBusy = roundBusy,
                    onEnd = { vm.endReviewSession() },
                )
            }
            items(session!!.messages) { m -> ReviewMessageRow(m) }
            item {
                ReviewRoundInputBar(
                    input = reviewInput,
                    onInputChange = { reviewInput = it },
                    onSend = {
                        val text = reviewInput.trim()
                        if (text.isNotEmpty()) {
                            reviewInput = ""
                            // 打字继续这一场也走**回顾**（`/api/daily-review/submit`），
                            // 不走 `sendCommand` —— 否则这句话会发到指令空间（用户 2026-10-22）。
                            vm.submitReviewText(text)
                        }
                    },
                    busy = roundBusy,
                    note = roundNote,
                    recording = recording,
                    recordMs = recordMs,
                    onStartRound = { startRound() },
                    onFinishRound = { vm.finishReviewRound() },
                    onCancelRound = { vm.cancelReviewRound() },
                )
            }
        }

        // ---- 口述卡：**紧跟在那三张功能卡下面**（2026-10-18 真机 bug 之后挪上来的）----
        //
        // 为什么位置也算判据：按「车里口述」之后用户眼睛还停在上面，而这张卡原来排在
        // 「每日回顾」状态卡**下面** —— 小屏上它在折叠线以下，"录音中 0.0 秒 / 说完了"
        // 一个字都看不见，于是体验就是"按了没反应、再按还是没反应"。
        //
        // 这张卡同时负责「口述回顾」这一路的**所有**说明与原因（`dictateNote`：没给权限 /
        // 麦被会议录音占着 / 被"一直听着"占着 / 开不了麦 / 收不到声音 / 取消了）：
        // 它就贴在刚按的那颗按钮下面，不用去找。
        // 这张卡是**一次性口述**（退路）的进行中/说明卡。回顾进行中时它不画：
        // 那一场的录音与"说完了"由上面的输入条负责（否则两张卡各有一个"说完了"，
        // 一个走 submit、一个走这一轮，用户根本分不清）。
        if (!reviewActive && (recording || submitting || voiceText.isNotBlank() ||
                submittingNote != null || dictateNote != null)
        ) {
            item {
                DictationCard(
                    recording = recording,
                    recordMs = recordMs,
                    submitting = submitting,
                    voiceText = voiceText,
                    submittingNote = submittingNote,
                    dictateNote = dictateNote,
                    onDone = { vm.submitReviewDictation() },
                    onCancel = { vm.cancelReviewDictation() },
                )
            }
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
    /**
     * 「口述回顾」这一路自己的说明（没权限 / 麦被占 / 收不到声音 / 取消了）。
     *
     * 为什么也画在这张卡里：这张卡就是"上一次按的那颗按钮"所在的地方（「说完了 / 取消」），
     * 用户点完「口述回顾」之后**就在看这里** —— 原因摆在这儿才不用去找。
     */
    dictateNote: String?,
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
            // 没开始录 / 收不到声音 / 取消了：这句一定要看得见（最多 4 行，不截成半句）
            dictateNote?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Yellow, maxLines = 4)
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
            MutedLine("点上面的「开始回顾」开始一场：一轮轮说或打字，和 PC 版一样。")
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

// ---- 手机回顾 = PC 版那套交互（开始 → 多轮 → 结束）的界面 ---------------------------------
//
// 这一组都只是**画**：判定在 `ReviewConversation.kt`（纯函数），状态在 `MainViewModel`，
// 采集走既有的 `startRecording`，上传走既有的 `voice-command`/`command`。
// 界面不许自己判断能不能录、也不许自己编原因（见 `dictationGate` 的说明）。

/**
 * 开始卡：一颗「开始回顾」+ 原因。
 *
 * "失败"与"结束那句"都落在这里（用户要求）：没配对 / 电脑回 400 / `ok=false` /
 * 结束之后那一句（[reviewEndedLine]）—— 全是 [note]。
 */
@Composable
private fun ReviewStartCard(
    starting: Boolean,
    ended: Boolean,
    note: String?,
    onStart: () -> Unit,
) {
    EchoCard {
        Column {
            CardHeader(
                title = REVIEW_START_TITLE,
                dotColor = if (ended) EchoPalette.Muted else EchoPalette.Accent,
                trailing = when {
                    starting -> "准备中…"
                    ended -> "已结束"
                    else -> "未开始"
                },
                trailingColor = EchoPalette.Purple,
            )
            Spacer(Modifier.height(6.dp))
            MutedLine(REVIEW_START_HINT, maxLines = 3)
            Spacer(Modifier.height(8.dp))
            Button(onClick = onStart, enabled = !starting) {
                if (starting) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp))
                    Spacer(Modifier.size(8.dp))
                    Text("正在开始…")
                } else {
                    Text(if (ended) "再开始一场回顾" else "开始回顾")
                }
            }
            // 没配对 / 开始失败 / 结束那句：都在这里（不许静默）
            note?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(6.dp))
                MutedLine(it, color = EchoPalette.Yellow, maxLines = 5)
            }
            Spacer(Modifier.height(6.dp))
            MutedLine(REVIEW_DICTATE_FALLBACK, maxLines = 4)
        }
    }
}

/** 进行中顶部：标题 + 轮数（今日历史 + 本场）+ 「结束回顾」。 */
@Composable
private fun ReviewRunningCard(
    session: ReviewSession,
    todayTurns: Int,
    note: String?,
    roundNote: String?,
    roundBusy: Boolean,
    onEnd: () -> Unit,
) {
    EchoCard {
        Column {
            CardHeader(
                title = REVIEW_RUNNING_TITLE,
                dotColor = EchoPalette.Purple,
            )
            Spacer(Modifier.height(4.dp))
            MutedLine(
                reviewRunningLine(todayRounds = todayTurns, sessionRounds = session.rounds),
                color = EchoPalette.Purple,
                maxLines = 2,
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = onEnd, enabled = !roundBusy) { Text("结束回顾") }
                MutedLine(
                    if (roundBusy) "这一轮正在整理…" else "说一轮 / 打一句，ECHO 回一轮 —— 和 PC 版一样。",
                    maxLines = 2,
                )
            }
            // 开始的注意事项（电脑权限没准备好）或结束那句
            note?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(6.dp))
                MutedLine(it, color = EchoPalette.Yellow, maxLines = 5)
            }
            // 这一轮"没开始" / 正在整理 / 失败的原因
            roundNote?.takeIf { it.isNotBlank() }?.let {
                Spacer(Modifier.height(4.dp))
                MutedLine(it, color = EchoPalette.Yellow, maxLines = 4)
            }
        }
    }
}

/** 一条消息：谁说的 + 内容；ECHO 的失败原因用红色（别和正常回复混在一起）。 */
@Composable
private fun ReviewMessageRow(message: ReviewMessage) {
    Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 4.dp)) {
        Text(
            message.speaker,
            style = MaterialTheme.typography.labelSmall,
            color = if (message.failed) EchoPalette.Red else EchoPalette.Accent,
        )
        Text(
            message.text,
            style = MaterialTheme.typography.bodySmall,
            color = if (message.failed) EchoPalette.Red else Color.Unspecified,
        )
    }
}

/**
 * 底部输入条：打字 → `/api/daily-review/submit`；按住说话 → 这一轮（`/api/daily-review/turn`）。
 *
 * 两条都**不是**指令链：回顾轮次只进回顾历史，不会发到指令空间（用户 2026-10-22）。
 *
 * 为什么"按住说话"不用 `Button`：`Button` 自带 `clickable`，会在指针事件里先 consume 掉 down，
 * `detectTapGestures` 就再也等不到按下（同 `SuperAssistantTab` 里那颗圆钮的坑）。
 */
@Composable
private fun ReviewRoundInputBar(
    input: String,
    onInputChange: (String) -> Unit,
    onSend: () -> Unit,
    busy: Boolean,
    note: String?,
    recording: Boolean,
    recordMs: Long,
    onStartRound: () -> Boolean,
    onFinishRound: () -> Unit,
    onCancelRound: () -> Unit,
) {
    EchoCard {
        Column {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = input,
                    onValueChange = onInputChange,
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("打字继续这一场回顾…", style = MaterialTheme.typography.bodySmall) },
                    maxLines = 3,
                    textStyle = MaterialTheme.typography.bodySmall,
                    enabled = !busy,
                )
                Button(onClick = onSend, enabled = input.isNotBlank() && !busy) { Text("发送") }
            }
            Spacer(Modifier.height(8.dp))
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Surface(
                    color = when {
                        busy -> EchoPalette.Muted
                        recording -> EchoPalette.Red
                        else -> EchoPalette.Accent
                    },
                    shape = CircleShape,
                    modifier = Modifier.pointerInput(busy) {
                        if (busy) return@pointerInput
                        detectTapGestures(
                            onPress = {
                                // 只有真的开起了麦才等松手 —— 否则松手会走进"没在录音"那条提示，
                                // 把"开不了麦"的原因盖掉（两个实话互相覆盖，等于都说了一半）
                                if (onStartRound()) {
                                    if (tryAwaitRelease()) onFinishRound() else onCancelRound()
                                }
                            },
                        )
                    },
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            Icons.Default.Mic,
                            contentDescription = "按住说话",
                            tint = Color.White,
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            when {
                                busy -> "整理中…"
                                recording -> "录音中…"
                                else -> "按住说话"
                            },
                            color = Color.White,
                            style = MaterialTheme.typography.labelLarge,
                        )
                    }
                }
                if (recording) {
                    MutedLine("录音中 ${"%.1f".format(recordMs / 1000.0)} 秒", maxLines = 1)
                    // 权限补授权回来时手指已经抬起（没有"松手"可等）：这条是那条路的收尾，
                    // 同时也是"按一下开录"这种降级用法的收尾，所以一直摆着。
                    TextButton(onClick = onFinishRound) { Text("说完了") }
                    TextButton(onClick = onCancelRound) { Text("取消") }
                }
            }
            // 这一轮的进度/失败/被挡：都来自 `reviewRoundNote`（不再借用 `_submit`）
            if (busy) {
                Spacer(Modifier.height(6.dp))
                MutedLine(
                    note.orEmpty().ifBlank { "ECHO 正在整理这一轮…（通常 2–3 分钟）" },
                    color = EchoPalette.Purple,
                    maxLines = 3,
                )
            } else {
                note?.takeIf { it.isNotBlank() }?.let {
                    Spacer(Modifier.height(6.dp))
                    MutedLine(it, color = EchoPalette.Yellow, maxLines = 3)
                }
            }
        }
    }
}

/**
 * 结束之后的**只读卡**：「刚才那一场」。
 *
 * 2026-10-20 起这张卡**只留一句去向**、不再把整段对话重画一遍：
 * 手机回顾的每一轮带 `context=review`、服务端记 `source='review'`，
 * 所以它们就在**下面的「历史回顾」**里（按天看得到原文与播报）。
 * 同一页里再画一遍整段对话，等于让用户再看一次同样的内容（用户原话：
 * *"回顾的历史对话，指令里也有能不重复吗？"* —— 页内重复也是重复）。
 */
@Composable
private fun ReviewLastSessionCard(session: ReviewSession) {
    EchoCard {
        Column {
            CardHeader(
                title = REVIEW_LAST_TITLE,
                dotColor = EchoPalette.Muted,
                trailing = "本场 ${session.rounds} 轮",
            )
            Spacer(Modifier.height(6.dp))
            MutedLine(REVIEW_LAST_ROUNDS_NOTE, maxLines = 4)
        }
    }
}
