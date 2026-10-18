package com.echo.contact.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.echo.contact.service.ListenState
import com.echo.contact.ui.theme.EchoPalette

/**
 * 应用外壳：**5 页**（指令 / 会议 / **超级助理（正中间）** / 回顾 / 设置，用户 2026-10-18 点名）。
 *
 * 与上一版的区别：
 *
 *  * **底部导航改成"中间凸起"的造型**：超级助理挪到**正中间**、做得更大更高（`EchoNavBar`），
 *    左右各两项。为什么：用户原话是"可以考虑 echo 超级助理在中间，左右各两个页签的样式，
 *    可以设计个造型"——原来的五颗等大、超级助理在最左边，它和别的页签长得一模一样，
 *    **主入口要在一眼能看到的位置**，所以才有了那颗凸起的圆底座。
 *  * **顺序只在 `EchoTabs.kt` 一处决定**：这一层只按 `ECHO_TABS` 渲染，
 *    中间那颗由 `echoCenterTab(ECHO_TABS)` 算出来（不在界面里写死名字）。
 *  * **页签从 3 个变 5 个**：指令、会议、回顾各自一页（功能一个都没少），设置也从右上角
 *    齿轮升成第 5 页 —— 齿轮已撤掉，同一个目的地不留两个入口。
 *  * **全屏录音界面盖住整个外壳**（含底部导航）：用户要的是"全屏"，录音时不该还能点别的页签。
 *
 * ## 这一层还是两条"全局可见性"的落点（2026-10-16）
 *
 *  * **常驻听写细条**（任务 B4）：常驻听写的状态原来只在某一页看得见，切到会议/回顾
 *    就不知道它还在听、还是被暂停了。细条挂在**这一层**（五个页签之上），任何页都能看见；
 *    点它回「指令」页。文案来自 `ListenState.stripOf`（纯函数，`ListenStateTest` 钉住）。
 *  * **通知落点**（任务 B8）：`MeetingOpenStore` 里那次"要打开哪场会议"由这里消费，
 *    切到会议页并交给 `MainViewModel.openMeetingByName`（复用已有的 `openMeeting`）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(onUnpair: () -> Unit) {
    val vm: MainViewModel = viewModel()
    // 页签由 ViewModel 持有：通知点进来时"先切到会议页"要能改它（见 openMeetingByName）。
    val tabIndex by vm.tab.collectAsState()
    // 读不懂的 index 回落**首页**（[START_TAB]，即超级助理，不是"第 0 项"）——
    // 顺序改了之后 0 是「指令」，所以这里跟起始页用同一个常量，别让界面白屏、也别落到错的那页。
    val currentTab = ECHO_TABS.getOrElse(tabIndex) { START_TAB }

    // 常驻听写的状态（进程级 object，界面只是读）：任何页都能看见"在听/在等/在念/已暂停"。
    val listenPhase by ListenState.phase.collectAsState()
    val listenDetail by ListenState.detail.collectAsState()
    val listenSegments by ListenState.segments.collectAsState()
    val listenStrip = ListenState.stripOf(listenPhase, listenDetail, listenSegments)

    val showRecording by vm.showRecordingScreen.collectAsState()

    LaunchedEffect(Unit) { vm.init() }

    // ---- 通知带来的"打开那场会议"（冷启动/热启动都写进同一个 store）----
    // 消费一次就没了：所以转屏、重放、重复投递都不会把同一个目标打开两遍。
    LaunchedEffect(Unit) {
        MeetingOpenStore.consume()?.let { vm.openMeetingByName(it.name) }
    }

    // 全屏录音界面：盖住整个外壳（含底部导航）—— 录音时不该还能切到别的页签。
    // 「返回」只关这个界面、**不停录音**（这条语义没变）。
    if (showRecording) {
        MeetingRecordingScreen(vm)
        return
    }

    Scaffold(
        topBar = {
            // 2026-10-18 傍晚：用户点名"**现在顶部那个空条**全面设计"。
            // 首页原来那条顶栏只画一个留空的标题（`homeTopBarTitle` 对首页返回空串），
            // 整整 64dp 全是空白 —— 这一版**整块不画**（`HomeTopBar.None`），
            // 应用名 + 连接状态只留在下面那张合并卡里（同一个事实不画两遍）。
            // 别的页仍然有顶栏：页名 + 刷新（那些页没有合并卡，顶栏是它们唯一的位置提示）。
            // 判据：`homeTopBar` + `HomeLayoutTest`（首页顶栏高度必须是 0）。
            when (val bar = homeTopBar(currentTab)) {
                HomeTopBar.None -> Unit
                is HomeTopBar.Title -> TopAppBar(
                    title = { Text(bar.text, style = MaterialTheme.typography.titleMedium) },
                    actions = {
                        IconButton(onClick = { vm.refresh() }) {
                            Icon(Icons.Default.Refresh, contentDescription = "刷新")
                        }
                    },
                )
            }
        },
        bottomBar = {
            // 造型在 EchoNavBar：中间那颗（正中间 = echoCenterTab(ECHO_TABS)）凸起、更大；
            // 顺序只照 ECHO_TABS 渲染（判据在 EchoTabsTest / EchoNavBarTest）。
            // 它自身的高度已经含"露在条外的那一截"，所以 Scaffold 给内容的底部 padding 也包含了它
            // —— 凸起不会盖住页面内容，更不会挡住页面底部的按钮。
            EchoNavBar(
                current = currentTab,
                onSelect = { vm.selectTab(it.ordinal) },
            )
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            // 常驻听写细条：**首页不画**（首页自己在内容区有一条同样的"在听"行，
            // 两处都画就等于同一个事实显示两遍）。别的页没有那条行，所以照旧挂在最上面。
            if (currentTab != START_TAB) {
                listenStrip?.let { strip ->
                    ListenStrip(strip, listenPhase) { vm.selectTab(EchoTab.COMMANDS.ordinal) }
                }
            }
            Box(modifier = Modifier.fillMaxSize()) {
                when (currentTab) {
                    EchoTab.ASSISTANT -> SuperAssistantTab(vm) { vm.selectTab(it.ordinal) }
                    EchoTab.COMMANDS -> TalkScreen(vm)
                    EchoTab.MEETINGS -> MeetingsTab(vm)
                    EchoTab.REVIEW -> ReviewTab(vm)
                    EchoTab.SETTINGS -> SettingsScreen(
                        vm = vm,
                        // 设置是页签：返回键回第 1 页（超级助理），不退出 App
                        onBack = { vm.selectTab(EchoTab.ASSISTANT.ordinal) },
                        onUnpair = onUnpair,
                    )
                }
            }
        }
    }
}

/**
 * 顶栏标题 = 当前页名；**首页留空**（纯函数，`HomeCardsTest` 钉住）。
 *
 * 为什么首页要空着：首页顶部那张卡（`HomeTopCard`）第一行就是应用名 + 连接状态，
 * 顶栏再写一次就成了"同一个事实两处显示"——这正是用户 2026-10-18 要求合并掉的那件事。
 *
 * 为什么别的页照旧显示页名：那些页没有那张卡，顶栏是它们唯一的位置提示
 * （底部导航虽然也亮着，但页面滚起来之后顶栏那句更直接）。页名**不含**应用名，
 * 所以"应用名只出现一处"这条不会被它破掉 —— 判据同时钉住这一点。
 */
internal fun homeTopBarTitle(tab: EchoTab): String = if (tab == START_TAB) "" else tab.title

/**
 * 顶栏这一块**画什么**（纯数据，`HomeLayoutTest` 钉住）。
 *
 * 为什么要多这一层：光有 [homeTopBarTitle] "返回空串"并不等于"没有空条"——
 * 只要 `Scaffold` 的 `topBar` 里仍然调用 `TopAppBar`，那 64dp 就还在，
 * 只是里面什么都没写。用户看到的正是这个"顶部空条"。
 *
 * 所以把顶栏做成两种**互斥**的形态：
 *  * [None] = **整块不存在**（首页：应用名/连接状态在合并卡里，顶栏不该再占一条）；
 *  * [Title] = 页名（其余四页：它们没有合并卡，顶栏是唯一的位置提示）。
 *
 * 判据（`HomeLayoutTest`）：首页必须 [None]，其余页必须是**非空**的 [Title] ——
 * "不空"这件事是断言出来的，不是靠"标题恰好有字"碰出来的。
 */
internal sealed class HomeTopBar {
    /** 不画顶栏（**整块不存在**，不是画一条空的）。 */
    data object None : HomeTopBar()

    /** 画一条顶栏，标题为 [text]（一定非空）。 */
    data class Title(val text: String) : HomeTopBar()
}

/**
 * 当前页的顶栏形态（纯函数）。首页 → [HomeTopBar.None]，其余页 → [HomeTopBar.Title]。
 *
 * 标题仍然只有一个来源（[homeTopBarTitle]），所以"首页留空"这条既有判据不会被绕过。
 */
internal fun homeTopBar(tab: EchoTab): HomeTopBar {
    val title = homeTopBarTitle(tab)
    return if (title.isBlank()) HomeTopBar.None else HomeTopBar.Title(title)
}

/**
 * 常驻听写细条（任务 B4）：一行小字 + 一个状态灯，点一下回「指令」页。
 *
 * 为什么放在外壳这一层而不是每个页签各放一条：这是**全应用**的状态，
 * 复制五份必然有一天只改了其中一份（而且"在听/已暂停"这种真相会分叉）。
 *
 * 文案来自 `ListenState.stripOf`（纯函数）、灯色在 [listenStripColor]（界面的事）——
 * 判断与配色分开，文案才能在 JVM 里逐档钉住。
 */
@Composable
private fun ListenStrip(strip: ListenState.Strip, phase: ListenState.Phase, onClick: () -> Unit) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp)
            .clickable(onClick = onClick),
        shape = RoundedCornerShape(8.dp),
        colors = CardDefaults.cardColors(containerColor = EchoPalette.Card2),
        border = BorderStroke(1.dp, EchoPalette.Border),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            StatusDot(listenStripColor(phase), size = 8)
            Spacer(Modifier.size(8.dp))
            Text(strip.label, style = MaterialTheme.typography.labelLarge)
            if (strip.text.isNotBlank()) {
                Spacer(Modifier.size(8.dp))
                Text(
                    strip.text,
                    style = MaterialTheme.typography.labelSmall,
                    color = EchoPalette.Muted,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
            } else {
                Spacer(Modifier.weight(1f))
            }
            Text("去指令页", style = MaterialTheme.typography.labelSmall, color = EchoPalette.Accent)
        }
    }
}

/**
 * 相位 → 灯色。
 *
 * 为什么用**颜色也分档**而不是一律绿灯：用户扫一眼细条就要能分清
 * "在听下来（绿）"与"暂停了（黄）" —— 如果两种都亮绿的，"已暂停"就等于没提示，
 * 那这条细条白加了。紫色给"在工作但不是收音"（等回复/念回复），红色给出错。
 */
internal fun listenStripColor(phase: ListenState.Phase) = when (phase) {
    ListenState.Phase.STARTING, ListenState.Phase.LISTENING -> EchoPalette.Green
    ListenState.Phase.THINKING, ListenState.Phase.SPEAKING -> EchoPalette.Purple
    ListenState.Phase.PAUSED -> EchoPalette.Yellow
    ListenState.Phase.FAILED -> EchoPalette.Red
    ListenState.Phase.OFF -> EchoPalette.Muted
}
