package com.echo.contact.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.echo.contact.service.ListenState
import com.echo.contact.service.MeetingMicService
import com.echo.contact.service.MicState
import com.echo.contact.ui.theme.EchoPalette

/**
 * **首页（底部导航正中间那一页）** —— 2026-10-18 傍晚按用户那句话重做：
 *
 * > *"首页的样式要按照 app 来设计，不要出现滚动条，要合理利用空间，包括现在顶部那个空条全面设计"*
 *
 * ```
 * ┌──────────────────────────────────────────────┐
 * │ ● ECHO 超级助理                       已连接 │  ← ① 应用名 + 连接状态（全 App 唯一一处）
 * │ http://192.168.1.5:8970                复制 │
 * │ 8/9 组件正常 · 1 未启用                    ⌄ │
 * └──────────────────────────────────────────────┘
 * ┌ ● 在听           已送出 3 句 …        去指令页 ┐  ← ② 44dp 细条（整行可点）
 * └──────────────────────────────────────────────┘
 * 主要功能
 * ┌ 指令 ────────────────────────────────────────┐  ← ③ 指令卡：打字 + 按住说话（都在本页干活）
 * │ [●] [ 打字说一句…        ] [发送]             │
 * │ 这一轮：…… · 已完成                       ›  │
 * └──────────────────────────────────────────────┘
 * ┌ 会议 ──────────────┐ ┌ 回顾 ──────────────┐  ← ④ 会议/回顾**并列**（各半宽）
 * │ 没在录音            │ │ 今天已回顾 2 轮     │
 * │ [ 开始录音 ]        │ │ [ 口述回顾 ]        │
 * └────────────────────┘ └────────────────────┘
 * ```
 *
 * ## 这一版改了什么（以及为什么）
 *
 *  * **顶部那条空顶栏整块删掉**（不是"清空"）：原来首页顶栏只画一个空标题，
 *    整整 64dp 全空。应用名/连接状态只在下面那张合并卡里画一次（`homeTopBar` 返回
 *    [HomeTopBar.None]，判据在 `HomeLayoutTest`）。刷新入口没删：合并卡点开里有「刷新」，
 *    卡片可见时还每 5 秒自动探一次。
 *  * **一屏放得下**：整页按 [homeLayoutPlan] 算出来的**布局预算**摆（360×640 上合计 448dp，
 *    可用 470dp）。预算与界面读同一份数字（[HomeLayoutSpec]），所以不是"用例说放得下"。
 *    屏更高时间距/卡片高度按比例松开，多出来的高度上下各分一半（视觉上垂直居中）。
 *  * **三张功能卡缩小但可读**：每张 = 标题行 + 一行状态 + 主按钮（48dp，仍然**直接干活**，
 *    动作在数据层就没有"页签"字段 —— `HomeEffect.Direct`，判据 `HomeCardsTest`）。
 *    会议卡与回顾卡**并排**，把横向空间用满（360dp 下各约 163dp 宽）。
 *  * **滚动只是兜底**：内容比屏幕高时（例如系统字体放大到 1.4 倍）才滚；
 *    Compose 的 `verticalScroll` **不画滚动条**，内容也一个字都不会被截断。
 *    这不是"用滚动糊过去"—— 360×640 的默认字体下内容 448 < 470，根本不会滚。
 *
 * ## 功能一个都没少
 *
 * 合并卡的展开区（5 格 + 9 个组件 + 版本 + 运行时长 + 启动提示 + 刷新）、在听细条、
 * 指令（打字/按住说话/这一轮结果/回复预览/去指令页）、会议（开始/结束录音 + 时长 + 暂停）、
 * 回顾（口述/说完了/取消 + 今日轮数）、朗读失败提示 —— 全在，而且都还是既有的那些入口
 * （不新造第二条路）。
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
    // 回顾口述：回调里**无条件**再试一次 —— `startReviewDictation()` 自己查权限，
    // 所以"用户拒绝"也会在这张卡上留下一句原因（以前这里只有 `if (granted)`，
    // 拒绝时什么都不显示：用户点了「口述回顾」，屏幕上没有任何反应）。
    val reviewMicLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { vm.startReviewDictation() }

    // 为什么 `vm.startMeeting()` 会顺带打开全屏录音界面：那是这条**既有**入口的一部分
    // （2026-10-15 用户要的"漂亮电平 + 结束 + 静音"）。它**不是**切页签 ——
    // 按返回就回到这页，而卡片上的「结束录音 + 本次时长」正是那时候要看的东西。
    // 为了"卡片上就不跳走"另开一条录音入口是不划算的：那会变成第二条录音路。
    val startMeetingWithMic: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) vm.startMeeting() else meetingMicLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }
    // 口述回顾：**既有**那条"录一段 → 转写 → 整理"路，只是入口换成了"两个都能说清原因"的那个。
    // 以前这里把 `vm.startRecording()` 的返回值**扔掉了**：没给权限 / 麦被占时，
    // 这张卡上什么都不显示 —— 用户点了「口述回顾」，屏幕上没有任何反应（2026-10-18 真机）。
    // 现在走 `vm.startReviewDictation()`：它先判权限/占用，原因写进 `dictateNote`，
    // 卡上就在按钮下面显示（不在别处）。录音实现还是那一个 `startRecording()`。
    val startDictationWithMic: () -> Unit = {
        val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) {
            vm.startReviewDictation()
        } else {
            // 系统弹窗这一下不可能静默：回调里一定会再调一次并把原因说出来
            reviewMicLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    // ---- 布局预算：用**真正的**可用宽高算（不是猜某个机型）----
    BoxWithConstraints(modifier = Modifier.fillMaxSize()) {
        val plan = remember(maxWidth, maxHeight) { homeLayoutPlan(maxWidth.value, maxHeight.value) }
        // 大屏上把多出来的高度**上下各分一半**（视觉上垂直居中，而不是"上面挤、下面空一大片"）。
        //
        // ⚠️ 只在**真的宽松**时（剩余 ≥ 40dp）才分，而且每边还留 6dp 余量：
        // 预算里的区块高度是"至少这么高"，而文字的实际行高在不同 ROM 上会差几个 dp
        // —— 360×640 那一档本来就只余 22dp，再把余量全塞进留白，就可能溢出成一条
        // 谁也看不见的滚动（用户要的是"一屏放得下"）。宁可小屏不居中，也不让它浮起来。
        val bleed = if (plan.spareHeightDp >= 40f) {
            ((plan.spareHeightDp - 12f) / 2f).coerceAtLeast(0f)
        } else {
            0f
        }

        Column(
            modifier = Modifier
                .fillMaxSize()
                // 兜底：只有内容比屏幕高时才会真的滚（字体放大/横屏）；
                // Compose 的滚动**不画滚动条**，所以"不要出现滚动条"这条不会被它破坏。
                .verticalScroll(rememberScrollState())
                .padding(
                    start = HomeLayoutSpec.PAGE_H_PADDING_DP.dp,
                    end = HomeLayoutSpec.PAGE_H_PADDING_DP.dp,
                    top = plan.pageTopDp.dp,
                    bottom = plan.pageBottomDp.dp,
                ),
            verticalArrangement = Arrangement.spacedBy(plan.gapDp.dp),
        ) {
            if (bleed > 0f) Spacer(Modifier.height(bleed.dp))

            // ① **唯一**那张"应用名 + 连接 + 配对地址 + 组件概要"卡（顶栏那一行已经删掉）
            HomeTopCard(vm)

            // ② 在听（常驻听写的相位与最近一句；整行可点 → 回「指令」页）
            ListenRow(strip, listenPhase) { onOpenTab(EchoTab.COMMANDS) }

            // ③ 功能卡上方的层级标签（状态卡 > 功能卡 > 辅助信息）
            Text(
                "主要功能",
                // labelMedium 的行高正好是预算里的 16dp（`SECTION_LABEL_MIN_DP`）
                style = MaterialTheme.typography.labelMedium,
                fontSize = plan.sectionLabelSp.sp,
                color = EchoPalette.Muted,
                modifier = Modifier.heightIn(min = plan.sectionLabelDp.dp),
            )

            // ④ 指令卡：打字 + 按住说话 + 一行状态（都在**本页**干活，不切页）
            val commandCard = cards.first { it.kind == HomeCardKind.COMMAND }
            FunctionCard(
                card = commandCard,
                plan = plan,
                onOpenTab = onOpenTab,
                minHeightDp = plan.commandCardDp,
            ) {
                CommandCardBody(commandCard, vm, onOpenTab)
            }

            // ⑤ 会议/回顾**并列**：把剩下的横向空间用满（各占一半）
            val meetingCard = cards.first { it.kind == HomeCardKind.MEETING }
            val reviewCard = cards.first { it.kind == HomeCardKind.REVIEW }
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(plan.gapDp.dp),
            ) {
                FunctionCard(
                    card = meetingCard,
                    plan = plan,
                    onOpenTab = onOpenTab,
                    modifier = Modifier.weight(1f),
                    minHeightDp = plan.sideCardDp,
                ) {
                    MeetingCardBody(meetingCard, micState, startMeetingWithMic)
                }
                FunctionCard(
                    card = reviewCard,
                    plan = plan,
                    onOpenTab = onOpenTab,
                    modifier = Modifier.weight(1f),
                    minHeightDp = plan.sideCardDp,
                ) {
                    ReviewCardBody(reviewCard, vm, startDictationWithMic)
                }
            }

            if (bleed > 0f) Spacer(Modifier.height(bleed.dp))
        }
    }
}

/**
 * 一张功能卡的外壳：图标 + 标题 + 一行说明（**整行可点 → 进对应页**）+ 卡身。
 *
 * 为什么卡身不整块可点：整块可点就等于"点哪儿都跳页"，那正是用户不想要的
 * （"不用跳转到对应功能页再开始使用功能"）。只有标题行会跳。
 *
 * 高度全部来自 [HomeLayoutPlan]（预算与界面同源）：标题行 [HomeLayoutSpec.HEADER_ROW_DP]、
 * 卡内边距 `plan.cardPaddingDp`。
 */
@Composable
private fun FunctionCard(
    card: HomeCardSpec,
    plan: HomeLayoutPlan,
    onOpenTab: (EchoTab) -> Unit,
    modifier: Modifier = Modifier,
    /**
     * 卡片的最小高度（来自布局预算）。为什么是"至少"而不是写死：
     * 小屏要的是"刚好一屏放得下"，大屏要的是"把空间用满" —— 同一个数字两头都成立。
     * 文字行高在不同 ROM 上会差几个 dp，`heightIn(min=)` 只托底、不封顶，
     * 所以字体放大时卡片会跟着长高（由外层滚动兜底），**不会截断**。
     */
    minHeightDp: Float = 0f,
    body: @Composable ColumnScope.() -> Unit,
) {
    EchoCard(
        modifier = if (minHeightDp > 0f) modifier.heightIn(min = minHeightDp.dp) else modifier,
        contentPadding = plan.cardPaddingDp.dp,
    ) {
        Column {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = HomeLayoutSpec.HEADER_ROW_DP.dp)
                    .clickable { onOpenTab(card.tab) },
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    entryIcon(card.tab),
                    contentDescription = null,
                    tint = EchoPalette.Accent,
                    modifier = Modifier.size(18.dp),
                )
                Spacer(Modifier.size(6.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        card.title,
                        style = MaterialTheme.typography.titleSmall,
                        fontSize = plan.cardTitleSp.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    MutedLine(card.subtitle, maxLines = 1)
                }
                Icon(
                    Icons.Default.ChevronRight,
                    contentDescription = "打开${card.title}",
                    tint = EchoPalette.Muted,
                    modifier = Modifier.size(18.dp),
                )
            }

            Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
            body()
        }
    }
}

/**
 * 指令卡：**打字 + 发送** 与 **按住说话** 都在本页干活（`vm.sendCommand` / `vm.startRecording`），
 * 与「指令」页那几个入口是**同一个**方法（不新造第二条路）。
 *
 * 发出后的反馈压成**一行状态**（这一轮发了什么 + 阶段 + 没发出去的原因），
 * 这一轮自己的回复再占一行；完整的正文去「指令」页看（点状态行，本函数里**唯一**会切页的东西）。
 *
 * ## 2026-10-18 真机 bug 之后必须保留的三件事
 *
 *  1. **把这次发的原话摆出来**（[roundHeadline]）——用户才能确认发的是自己那句；
 *  2. **没发出去就说**（`submit.blocked` / `Failed` 的 `message`）——这是"点了没反应"的正解；
 *  3. **回复只在 [showsReply] 为真时显示**：只有 `Done` 能证明 `reply` 是这一条的。
 */
@Composable
private fun CommandCardBody(
    card: HomeCardSpec,
    vm: MainViewModel,
    onOpenTab: (EchoTab) -> Unit,
) {
    val submit by vm.submit.collectAsState()
    val recording by vm.recording.collectAsState()
    val recordMs by vm.recordMs.collectAsState()
    // 朗读失败/说明（系统 TTS 没装 / ECHO 合成失败 / 没有可念的播报）：单行、几秒后自动消失。
    // 放在这张卡里是因为**自动朗读就发生在这页**（语音指令的回复）。
    val speakNote by vm.speakNote.collectAsState()
    var input by remember { mutableStateOf("") }

    val context = LocalContext.current
    // 第一次按住时要录音权限：先要权限，用户再按一次。**不**在没权限时假装开始录音。
    val commandMicLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* 给不给都由下一次按决定；不给会看到"开不了麦克风"那句如实的话 */ }

    val dictate = card.buttons.firstOrNull { it.action == HomeAction.DICTATE_COMMAND }

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .height(HomeLayoutSpec.COMPOSER_ROW_DP.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // 「按住说话」：按下就录、松手就发（拖开手指 = 放弃）。
        // 为什么不是"点一下开录、再点一下说完了"：这里只有一个圆钮，
        // 两段式会让"以为说完了其实还在录"变成一个新的静默坑。
        //
        // ⚠️ 这里刻意**不用 `Button`**：`Button` 内部自带 `clickable`，它在指针事件的 Main pass
        // 里比外层**先**拿到 down 并 consume 掉 —— `detectTapGestures` 默认 `requireUnconsumed`
        // 就再也等不到按下，按住说话会变成一个点了没反应的死按钮。
        // 所以用一个「长得像按钮」的 `Surface`：里面没有任何 clickable，gesture 一定拿得到。
        if (dictate != null) {
            Surface(
                color = if (recording) EchoPalette.Red else EchoPalette.Accent,
                shape = CircleShape,
                modifier = Modifier
                    .size(HomeLayoutSpec.COMPOSER_ROW_DP.dp)
                    .pointerInput(Unit) {
                        detectTapGestures(
                            onPress = {
                                val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                                    PackageManager.PERMISSION_GRANTED
                                if (!granted) {
                                    commandMicLauncher.launch(Manifest.permission.RECORD_AUDIO)
                                } else {
                                    // 只有真的开起了麦才等松手 —— 否则松手会走进"没在录音"那条提示，
                                    // 把"开不了麦"的原因盖掉（两个实话互相覆盖，等于都说了一半）
                                    if (vm.startRecording()) {
                                        if (tryAwaitRelease()) vm.finishRecording() else vm.cancelRecording()
                                    }
                                }
                            },
                        )
                    },
            ) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Icon(
                        Icons.Default.Mic,
                        contentDescription = dictate.label,
                        tint = Color.White,
                        modifier = Modifier.size(22.dp),
                    )
                }
            }
            Spacer(Modifier.size(8.dp))
        }

        // 打字框：用 BasicTextField 而不是 OutlinedTextField —— 后者带 label 时最小高度 56dp，
        // 而这一行只有 48dp（输入框 44dp）：一屏放得下的预算见 `HomeLayoutSpec`。
        Surface(
            color = EchoPalette.Card2,
            shape = RoundedCornerShape(10.dp),
            border = BorderStroke(1.dp, EchoPalette.Border),
            modifier = Modifier
                .weight(1f)
                .height(44.dp),
        ) {
            Box(
                modifier = Modifier.fillMaxSize().padding(horizontal = 12.dp),
                contentAlignment = Alignment.CenterStart,
            ) {
                BasicTextField(
                    value = input,
                    onValueChange = { input = it },
                    singleLine = true,
                    textStyle = MaterialTheme.typography.bodyMedium.copy(color = EchoPalette.Text),
                    cursorBrush = SolidColor(EchoPalette.Accent),
                    modifier = Modifier.fillMaxWidth(),
                    decorationBox = { inner ->
                        if (input.isBlank()) {
                            Text(
                                "打字说一句…",
                                style = MaterialTheme.typography.bodyMedium,
                                color = EchoPalette.Muted,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                        inner()
                    },
                )
            }
        }
        Spacer(Modifier.size(8.dp))

        // 按钮由 `card.buttons`（HomeCards.kt 的数据）驱动 —— 动作在数据层就没有"页签"字段，
        // 所以这里不可能把页面换掉；判据在 HomeCardsTest（数据）+ SendRoundTest（界面接线）。
        card.buttons.firstOrNull { it.action == HomeAction.SEND_COMMAND }?.let { button ->
            Button(
                onClick = {
                    val text = input
                    input = ""
                    vm.sendCommand(text)
                },
                enabled = input.isNotBlank(),
                contentPadding = PaddingValues(horizontal = 12.dp),
                modifier = Modifier.height(HomeLayoutSpec.PRIMARY_BUTTON_DP.dp),
            ) { Text(button.label) }
        }
    }

    Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))

    // 一行状态：这一轮发了什么 / 阶段 / 没发出去的原因；**整行可点** → 去「指令」页看完整正文。
    // 这是本函数里**唯一**会切页的东西（`HomeCardsTest` 按这个数字钉住）。
    val accent = when {
        submit.blocked.isNotBlank() -> EchoPalette.Red
        submit.phase == SubmitPhase.Failed -> EchoPalette.Red
        submit.phase == SubmitPhase.Done -> EchoPalette.Green
        submit.phase != SubmitPhase.Idle -> EchoPalette.Purple
        recording -> EchoPalette.Red
        else -> EchoPalette.Muted
    }
    val statusText = when {
        recording -> "松开就发 · ${"%.1f".format(recordMs / 1000.0)} 秒"
        submit.blocked.isNotBlank() -> submit.blocked
        submit.phase != SubmitPhase.Idle -> buildString {
            append("这一轮：").append(roundHeadline(submit))
            append(" · ").append(phaseLabel(submit.phase))
            if (submit.message.isNotBlank()) append(" · ").append(submit.message)
        }
        else -> "按住左边麦克风说一句，或打字发送"
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = HomeLayoutSpec.STATUS_LINE_DP.dp)
            .clickable { onOpenTab(card.tab) },
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            statusText,
            style = MaterialTheme.typography.labelSmall,
            color = accent,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Text("›", style = MaterialTheme.typography.labelMedium, color = EchoPalette.Accent)
    }

    // 最近一条回复：**只有这一轮自己的结论**才显示（`showsReply` 把关，旧回复一个字都不许摆）。
    if (showsReply(submit.phase)) {
        val preview = replyPreview(submit.reply)
        if (preview.text.isNotBlank()) {
            Text(
                preview.text,
                style = MaterialTheme.typography.labelSmall,
                color = EchoPalette.Text,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }

    // 朗读失败/说明（"系统 TTS 没装" / "ECHO 合成失败" / 念不出来）：单行、几秒后自己消失。
    // 没有它会怎样：按了播报没声音，用户只能猜。
    SpeakNoteLine(speakNote, onDismiss = { vm.clearSpeakNote() })
}

/**
 * 会议卡：**开始录音 / 结束录音**（同一个位置换文案）+ 一行状态（含本次时长与静音）。
 *
 * 数据全部来自 `MeetingMicService.state`（`MicState`）：`active` 决定按钮文案、
 * `elapsedSeconds` 就是这一场的时长（服务自己算的，界面不另算一遍）。
 * 暂停也如实说（静音区间照录、内容全零 —— 见 `app/audio/recorder.py` 的暂停语义）。
 */
@Composable
private fun MeetingCardBody(card: HomeCardSpec, mic: MicState, onStart: () -> Unit) {
    val context = LocalContext.current

    MutedLine(meetingCardLine(mic.active, mic.elapsedSeconds, mic.paused), maxLines = 1)
    Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
    card.buttons.forEach { button ->
        when (button.action) {
            HomeAction.START_MEETING -> Button(
                onClick = onStart,
                contentPadding = PaddingValues(horizontal = 10.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(HomeLayoutSpec.PRIMARY_BUTTON_DP.dp),
            ) { Text(button.label) }

            HomeAction.STOP_MEETING -> Button(
                onClick = { MeetingMicService.stop(context) },
                colors = ButtonDefaults.buttonColors(containerColor = EchoPalette.Red),
                contentPadding = PaddingValues(horizontal = 10.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(HomeLayoutSpec.PRIMARY_BUTTON_DP.dp),
            ) { Text(button.label) }

            // 会议卡上不该有别的动作（文案与动作是一起给的，见 `meetingCardButtons`）
            else -> Unit
        }
    }

    // 出错 / 收尾说明**不删**：只在有的时候多一行（卡片长高了由外层滚动兜底，绝不截断）
    mic.error.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
        MutedLine(it, color = EchoPalette.Red, maxLines = 1)
    }
    mic.stopNote.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
        MutedLine(it, color = EchoPalette.Yellow, maxLines = 1)
    }
}

/**
 * 回顾卡：**口述回顾**（直接进既有的"录一段 → 转写 → 整理"路）+ 一行状态。
 *
 * 走的是 `vm.startReviewDictation()` / `vm.submitReviewDictation()` —— 与回顾页那张
 * 「车里口述」卡**同一个**入口（`ReviewTab` 的 `startDictation`），没有第二条路。
 * 录音实现仍然只有 `MainViewModel.startRecording()` 那一处；`startReviewDictation`
 * 只多做一件界面必须做的事：**先把"能不能开始"判出来并给一句人话**。
 */
@Composable
private fun ReviewCardBody(card: HomeCardSpec, vm: MainViewModel, onStartDictation: () -> Unit) {
    val recordMs by vm.recordMs.collectAsState()
    val submitting by vm.reviewSubmitting.collectAsState()
    val submittingNote by vm.reviewSubmittingNote.collectAsState()
    val summary by vm.reviewSummary.collectAsState()
    // 「口述回顾」没开始/没收到的原因（没权限 / 被会议录音占着 / 被"一直听着"占着 /
    // 开不了麦 / 收不到声音 / 取消了）。**必须画在按钮下面** —— 这张卡就是用户按的地方。
    val dictateNote by vm.dictateNote.collectAsState()

    MutedLine(
        when {
            submitting -> "正在整理…"
            card.buttons.any { it.action == HomeAction.SUBMIT_REVIEW } ->
                "录音中 ${elapsedClock(recordMs / 1000.0)}"
            (summary?.todayTurns ?: 0) > 0 -> "今天已回顾 ${summary?.todayTurns} 轮"
            else -> "今天还没回顾"
        },
        maxLines = 1,
    )
    Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        card.buttons.forEach { button ->
            when (button.action) {
                HomeAction.DICTATE_REVIEW -> Button(
                    onClick = onStartDictation,
                    contentPadding = PaddingValues(horizontal = 10.dp),
                    modifier = Modifier.height(HomeLayoutSpec.PRIMARY_BUTTON_DP.dp),
                ) { Text(button.label) }

                HomeAction.SUBMIT_REVIEW -> Button(
                    onClick = { vm.submitReviewDictation() },
                    contentPadding = PaddingValues(horizontal = 10.dp),
                    modifier = Modifier.height(HomeLayoutSpec.PRIMARY_BUTTON_DP.dp),
                ) { Text(button.label) }

                HomeAction.CANCEL_REVIEW -> TextButton(
                    onClick = { vm.cancelReviewDictation() },
                    contentPadding = PaddingValues(horizontal = 8.dp),
                    modifier = Modifier.height(HomeLayoutSpec.PRIMARY_BUTTON_DP.dp),
                ) { Text(button.label) }

                else -> Unit
            }
        }
    }

    // 两句说明**都画在按钮下面**（这张卡就是用户按的地方，不摆到别处去）：
    //  · dictateNote    = 这次口述**没开始**/没收到的原因（没权限、被会议录音占着、
    //                     被"一直听着"占着、开不了麦、收不到声音、取消了）；
    //  · submittingNote = 已经开始转写/整理之后的进度与失败原因。
    dictateNote?.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
        // 4 行：这张卡只有半个屏幕宽，3 行会把"点「允许」/去指令页停掉"那半句截掉，
        // 而那半句恰恰是用户唯一能照做的东西。真的没有原因时它一行都不占。
        MutedLine(it, color = EchoPalette.Yellow, maxLines = 4)
    }
    submittingNote?.takeIf { it.isNotBlank() }?.let {
        Spacer(Modifier.height(HomeLayoutSpec.CARD_GAP_DP.dp))
        MutedLine(it, color = EchoPalette.Purple, maxLines = 2)
    }
}

/**
 * 「在听」细条（44dp，整行可点 → 回「指令」页）。
 *
 * 为什么是一条**细条**而不是原来那张卡：它是一行状态（点灯 + 相位 + 最近一句），
 * 以前占掉一张卡（≈80dp），而首页要在 360×640 上一屏放下合并卡 + 三张功能卡。
 * 信息一个字节都没少：相位词、最近一句、以及"怎么开启"的提示都还在。
 *
 * `strip == null`（[ListenState.Phase.OFF]）= 没在听：**如实**说"没在听"，
 * 并告诉用户入口在哪（「指令」页）。不把"没在听"画成一个正在工作的绿点。
 */
@Composable
private fun ListenRow(strip: ListenState.Strip?, phase: ListenState.Phase, onClick: () -> Unit) {
    val color = listenStripColor(phase)
    Surface(
        color = EchoPalette.Card2,
        shape = RoundedCornerShape(10.dp),
        border = BorderStroke(1.dp, EchoPalette.Border),
        modifier = Modifier
            .fillMaxWidth()
            .height(HomeLayoutSpec.LISTEN_ROW_DP.dp)
            .clickable(onClick = onClick),
    ) {
        Row(
            modifier = Modifier.fillMaxSize().padding(horizontal = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            StatusDot(color, size = 8)
            Spacer(Modifier.size(8.dp))
            Text(strip?.label ?: "没在听", style = MaterialTheme.typography.labelLarge)
            Spacer(Modifier.size(8.dp))
            Text(
                strip?.text ?: "想开车时也一直听：去「指令」页点「一直听着」。",
                style = MaterialTheme.typography.labelSmall,
                color = EchoPalette.Muted,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.size(6.dp))
            Text("去指令页", style = MaterialTheme.typography.labelSmall, color = EchoPalette.Accent)
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
