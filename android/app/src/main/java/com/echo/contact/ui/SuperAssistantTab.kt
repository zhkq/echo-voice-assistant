package com.echo.contact.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
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
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import com.echo.contact.service.ListenState
import com.echo.contact.service.MeetingMicService
import com.echo.contact.service.MicState
import com.echo.contact.ui.theme.EchoPalette

/**
 * **首页（底部导航正中间那一页）**。
 *
 * 结构（用户 2026-10-18 中午两条要求合起来）：
 *
 * ```
 * ┌───────────────────────────────────────────┐
 * │ ● 应用名（第一行）             已连接 / 未连接 │  ← ② 顶部那一行与服务状态**合并成这一张卡**
 * │ http://192.168.1.5:8970            复制     │
 * │ 8/9 组件正常 · 1 未启用                  ⌄  │  ← 点开：原来那 5 格 + 9 个组件详情
 * └───────────────────────────────────────────┘
 * ┌ 在听 ─────────────────────────────────────┐
 * └───────────────────────────────────────────┘
 * 主要功能
 * ┌ 指令 ─────────────────────────────────────┐
 * │ [ 输入框 ..................... ] [ 发送 ] │  ← ③ 在**本页**发出，回复也回显在本页
 * │ 已完成 · 耗时 1.2 秒                       │
 * │ 「它回的那段……」                    看全部指令 │
 * └───────────────────────────────────────────┘
 * ┌ 会议 ─────────────────────────────────────┐
 * │ [ 开始录音 ]  录音中 · 已录 03:12          │  ← 直接开手机麦；同一位置变「结束录音」
 * │                                 看会议列表 │
 * └───────────────────────────────────────────┘
 * ┌ 回顾 ─────────────────────────────────────┐
 * │ [ 口述回顾 ]  今天已回顾 2 轮              │  ← 直接进既有的"口述 → 转写 → 整理"路
 * │                                 看回顾历史 │
 * └───────────────────────────────────────────┘
 * ```
 *
 * ## 为什么顶部那张卡不再单独一行
 *
 * 合并前：顶栏画「应用名 + 配对地址」，这页第一张卡画「服务状态」。同一个事实两处显示，
 * 必然有一天一处说在线、另一处说连不上。现在只剩 [HomeTopCard] 一处
 * （`HomeScreen` 的顶栏只剩刷新按钮 + 页名，见 `homeTopBarTitle`）。
 *
 * ## 为什么卡片按钮不切页
 *
 * 用户原话：*"不用跳转到对应功能页再开始使用功能"*。所以三张卡的按钮都走**既有**动作：
 * `sendCommand` / `startMeeting`（→ `MeetingMicService.start`）/ `stop` / `startRecording` /
 * `submitReviewDictation`。动作本身在数据层就没有"页签"字段（`HomeEffect.Direct`），
 * 判据在 `HomeCardsTest`。
 *
 * 卡片**标题**仍然可点、进对应页 —— 底部 5 页导航照旧有效，卡片只是**快捷方式**。
 */
@Composable
internal fun SuperAssistantTab(vm: MainViewModel, onOpenTab: (EchoTab) -> Unit) {
    val listenPhase by ListenState.phase.collectAsState()
    val listenDetail by ListenState.detail.collectAsState()
    val listenSegments by ListenState.segments.collectAsState()
    val strip = ListenState.stripOf(listenPhase, listenDetail, listenSegments)

    // 两个**现有**状态，决定卡片上按钮的文案（不新造开关）：
    //  · 会议录音 = `MeetingMicService` 的本地状态（手机麦那条路）；
    //  · 口述回顾 = `MainViewModel.recording`（与回顾页「车里口述」同一个录音器）。
    val micState by MeetingMicService.state.collectAsState()
    val dictating by vm.recording.collectAsState()

    val cards = homeCards(micRecording = micState.active, reviewDictating = dictating)

    val context = LocalContext.current

    // 「开始录音」= 手机本地录音，所以要先有 RECORD_AUDIO：没授权先要，授权成功再开始。
    // 这里**不**另写一套录音逻辑，授权后走的还是 `vm.startMeeting()` / `vm.startRecording()`
    // 那两个既有入口（与会议页、回顾页那两颗按钮调的是同一个方法）。
    val meetingMicLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) vm.startMeeting() }
    val reviewMicLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) vm.startRecording() }

    // 为什么 `vm.startMeeting()` 会顺带打开全屏录音界面：那是这条**既有**入口的一部分
    // （2026-10-15 用户要的"漂亮电平 + 结束 + 静音"）。它**不是**切页签 ——
    // 按返回就回到这页，而卡片上的「结束录音 + 本次时长」正是那时候要看的东西。
    // 为了"卡片上就不跳走"另开一条录音入口是不划算的：那会变成第二条录音路。
    val startMeetingWithMic: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) vm.startMeeting() else meetingMicLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }
    val startDictationWithMic: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) vm.startRecording() else reviewMicLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        // ⓪ **唯一**那张"应用名 + 连接 + 配对地址 + 组件概要"卡（原来这里还有顶栏那一行）
        item { HomeTopCard(vm) }

        // ① 在听（现有信息：常驻听写的相位与最近一句文案）
        item { ListenStatusCard(strip, listenPhase) }

        // ② 三张功能卡（每张都有能直接干活的按钮）
        item {
            Text(
                "主要功能",
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 2.dp),
            )
        }
        items(cards.size) { index ->
            val card = cards[index]
            FunctionCard(card, onOpenTab) {
                when (card.kind) {
                    HomeCardKind.COMMAND -> CommandCardBody(vm, onOpenTab)
                    HomeCardKind.MEETING -> MeetingCardBody(card, micState, startMeetingWithMic)
                    HomeCardKind.REVIEW -> ReviewCardBody(
                        card = card,
                        vm = vm,
                        onStartDictation = startDictationWithMic,
                    )
                }
            }
        }
    }
}

/**
 * 一张功能卡的外壳：图标 + 标题 + 说明（**整行可点 → 进对应页**）+ 卡身 + 右下角小链接。
 *
 * 为什么卡身不整块可点：整块可点就等于"点哪儿都跳页"，那正是用户不想要的
 * （"不用跳转到对应功能页再开始使用功能"）。只有标题行与那个小链接会跳。
 */
@Composable
private fun FunctionCard(
    card: HomeCardSpec,
    onOpenTab: (EchoTab) -> Unit,
    body: @Composable ColumnScope.() -> Unit,
) {
    EchoCard {
        Column {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onOpenTab(card.tab) },
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    entryIcon(card.tab),
                    contentDescription = null,
                    tint = EchoPalette.Accent,
                    modifier = Modifier.size(22.dp),
                )
                Spacer(Modifier.size(12.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(card.title, style = MaterialTheme.typography.titleSmall)
                    MutedLine(card.subtitle, maxLines = 1)
                }
                Spacer(Modifier.size(8.dp))
                Icon(
                    Icons.Default.ChevronRight,
                    contentDescription = "打开${card.title}",
                    tint = EchoPalette.Muted,
                    modifier = Modifier.size(20.dp),
                )
            }

            Spacer(Modifier.height(10.dp))
            body()

            // 小链接：**唯一**会切页的东西（"再看完整页面"那条路）
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Spacer(Modifier.weight(1f))
                TextButton(onClick = { onOpenTab(card.tab) }) { Text(card.linkLabel) }
            }
        }
    }
}

/**
 * 指令卡：**输入框 + 发送**，在本页直接发出（`vm.sendCommand`，与「指令」页那颗发送键同一个方法）。
 *
 * 发出后的反馈也在这张卡里：一句状态 + 最近那条回复（很长就截断 + 给「去看详情」）。
 * 为什么只给"简短预览"：卡片不是详情页；完整正文在「指令」页的历史里。
 */
@Composable
private fun CommandCardBody(vm: MainViewModel, onOpenTab: (EchoTab) -> Unit) {
    val submit by vm.submit.collectAsState()
    var input by remember { mutableStateOf("") }

    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedTextField(
            value = input,
            onValueChange = { input = it },
            label = { Text("打字说一句") },
            maxLines = 2,
            modifier = Modifier.weight(1f),
        )
        Button(
            onClick = {
                val text = input
                input = ""
                vm.sendCommand(text)
            },
            enabled = input.isNotBlank(),
        ) { Text("发送") }
    }

    if (submit.phase != SubmitPhase.Idle) {
        val accent = when (submit.phase) {
            SubmitPhase.Failed -> EchoPalette.Red
            SubmitPhase.Done -> EchoPalette.Green
            SubmitPhase.Idle -> EchoPalette.Muted
            else -> EchoPalette.Purple
        }
        Spacer(Modifier.height(6.dp))
        Text(
            submit.message.ifBlank { phaseLabel(submit.phase) },
            style = MaterialTheme.typography.labelSmall,
            color = accent,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )

        // 最近一条回复：短的原样回显；长的不塞满卡片，给「去看详情」
        val preview = replyPreview(submit.reply)
        if (preview.text.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(
                preview.text,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
            if (preview.truncated) {
                TextButton(onClick = { onOpenTab(EchoTab.COMMANDS) }) { Text("去看详情") }
            }
        }
    }
}

/**
 * 会议卡：**开始录音 / 结束录音**（同一个位置换文案）+ 本次时长 + 「看会议列表」。
 *
 * 数据全部来自 `MeetingMicService.state`（`MicState`）：`active` 决定按钮文案、
 * `elapsedSeconds` 就是这一场的时长（服务自己算的，界面不另算一遍）。
 * 暂停也如实说（静音区间照录、内容全零）。
 */
@Composable
private fun MeetingCardBody(card: HomeCardSpec, mic: MicState, onStart: () -> Unit) {
    val context = LocalContext.current

    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        card.buttons.forEach { button ->
            when (button.action) {
                HomeAction.START_MEETING -> Button(onClick = onStart) { Text(button.label) }
                HomeAction.STOP_MEETING -> Button(
                    onClick = { MeetingMicService.stop(context) },
                    colors = ButtonDefaults.buttonColors(containerColor = EchoPalette.Red),
                ) { Text(button.label) }
                // 会议卡上不该有别的动作（文案与动作是一起给的，见 `meetingCardButtons`）
                else -> Unit
            }
        }
        MutedLine(
            meetingCardLine(mic.active, mic.elapsedSeconds, mic.paused),
            maxLines = 1,
        )
    }

    mic.error.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(4.dp))
        MutedLine(it, color = EchoPalette.Red, maxLines = 2)
    }
    mic.stopNote.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(4.dp))
        MutedLine(it, color = EchoPalette.Yellow, maxLines = 2)
    }
}

/**
 * 回顾卡：**口述回顾**（直接进既有的"录一段 → 转写 → 整理"路）+ 「看回顾历史」。
 *
 * 走的是 `vm.startRecording()` / `vm.submitReviewDictation()` —— 与回顾页那张
 * 「车里口述」卡**同一个**入口（`ReviewTab` 的 `startDictation`），没有第二条路。
 */
@Composable
private fun ReviewCardBody(card: HomeCardSpec, vm: MainViewModel, onStartDictation: () -> Unit) {
    val recordMs by vm.recordMs.collectAsState()
    val submitting by vm.reviewSubmitting.collectAsState()
    val submittingNote by vm.reviewSubmittingNote.collectAsState()
    val summary by vm.reviewSummary.collectAsState()

    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        card.buttons.forEach { button ->
            when (button.action) {
                HomeAction.DICTATE_REVIEW -> Button(onClick = onStartDictation) { Text(button.label) }
                HomeAction.SUBMIT_REVIEW -> Button(onClick = { vm.submitReviewDictation() }) {
                    Text(button.label)
                }
                HomeAction.CANCEL_REVIEW -> TextButton(onClick = { vm.cancelReviewDictation() }) {
                    Text(button.label)
                }
                else -> Unit
            }
        }
        MutedLine(
            when {
                submitting -> "正在整理…（通常 2–3 分钟）"
                card.buttons.any { it.action == HomeAction.SUBMIT_REVIEW } ->
                    "录音中 ${elapsedClock(recordMs / 1000.0)}"
                (summary?.todayTurns ?: 0) > 0 -> "今天已回顾 ${summary?.todayTurns} 轮"
                else -> "今天还没回顾"
            },
            maxLines = 1,
        )
    }

    submittingNote?.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(4.dp))
        MutedLine(it, color = EchoPalette.Purple, maxLines = 2)
    }
}

/**
 * 「在听」状态卡。
 *
 * `strip == null`（[ListenState.Phase.OFF]）= 没在听：**如实**说"没在听"，
 * 并告诉用户入口在哪（「指令」页）。不把"没在听"画成一个正在工作的绿点。
 */
@Composable
private fun ListenStatusCard(strip: ListenState.Strip?, phase: ListenState.Phase) {
    val color = when (phase) {
        ListenState.Phase.STARTING, ListenState.Phase.LISTENING -> EchoPalette.Green
        ListenState.Phase.THINKING, ListenState.Phase.SPEAKING -> EchoPalette.Purple
        ListenState.Phase.PAUSED -> EchoPalette.Yellow
        ListenState.Phase.FAILED -> EchoPalette.Red
        ListenState.Phase.OFF -> EchoPalette.Muted
    }
    EchoCard {
        Column {
            CardHeader(
                title = "在听",
                dotColor = color,
                trailing = strip?.label ?: "没在听",
                trailingColor = color,
            )
            Spacer(Modifier.height(4.dp))
            MutedLine(
                strip?.text
                    ?: "想开车时也一直听：去「指令」页点「一直听着」。",
                maxLines = 2,
            )
        }
    }
}

/** 入口 → 图标（只做外观映射；入口本身见 [ASSISTANT_ENTRIES]）。 */
private fun entryIcon(tab: EchoTab): ImageVector = when (tab) {
    EchoTab.COMMANDS -> Icons.Default.Mic
    EchoTab.MEETINGS -> Icons.Default.EventNote
    EchoTab.REVIEW -> Icons.Default.History
    // 三个主入口之外不该走到这里；回一个中性图标而不是崩
    else -> Icons.Default.ChevronRight
}
