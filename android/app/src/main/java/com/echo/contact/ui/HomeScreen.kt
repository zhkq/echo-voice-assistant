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
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
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
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.echo.contact.service.ListenState
import com.echo.contact.ui.theme.EchoPalette

/**
 * 应用外壳：**5 页**（超级助理 / 指令 / 会议 / 回顾 / 设置，用户 2026-10-18 点名）。
 *
 * 与上一版的区别：
 *
 *  * **第 1 页是超级助理**（仪表盘风格）：状态卡 + 三个主入口。它是"看一眼现在怎么样、
 *    要去哪一页"的地方，不是当年的"说话"页。
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
    val baseUrl by vm.baseUrl.collectAsState()
    // 页签由 ViewModel 持有：通知点进来时"先切到会议页"要能改它（见 openMeetingByName）。
    val tabIndex by vm.tab.collectAsState()
    // 读不懂的 index 回落第 1 页（**不**让界面白屏）
    val currentTab = ECHO_TABS.getOrElse(tabIndex) { EchoTab.ASSISTANT }

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
            TopAppBar(
                title = {
                    Column {
                        Text("ECHO 触点", style = MaterialTheme.typography.titleMedium)
                        Text(
                            baseUrl.ifBlank { "未配对" },
                            style = MaterialTheme.typography.labelSmall,
                            color = EchoPalette.Muted,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                },
                actions = {
                    IconButton(onClick = { vm.refresh() }) {
                        Icon(Icons.Default.Refresh, contentDescription = "刷新")
                    }
                },
            )
        },
        bottomBar = {
            // 恰好 5 项、顺序照 ECHO_TABS（判据在 EchoTabsTest）
            NavigationBar {
                ECHO_TABS.forEach { tab ->
                    NavigationBarItem(
                        selected = tab == currentTab,
                        onClick = { vm.selectTab(tab.ordinal) },
                        icon = { Icon(tabIcon(tab), contentDescription = null) },
                        label = { Text(tab.title) },
                    )
                }
            }
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            // 不在听（OFF）就不显示：首页不该常驻一条空条；出错停了也**要显示**（如实说停下了）。
            listenStrip?.let { strip ->
                ListenStrip(strip, listenPhase) { vm.selectTab(EchoTab.COMMANDS.ordinal) }
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

/** 页签 → 图标（只做外观映射；页签本身见 [EchoTab]）。 */
private fun tabIcon(tab: EchoTab): ImageVector = when (tab) {
    EchoTab.ASSISTANT -> Icons.Default.Dashboard
    EchoTab.COMMANDS -> Icons.Default.Mic
    EchoTab.MEETINGS -> Icons.Default.EventNote
    EchoTab.REVIEW -> Icons.Default.History
    EchoTab.SETTINGS -> Icons.Default.Settings
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
private fun listenStripColor(phase: ListenState.Phase) = when (phase) {
    ListenState.Phase.STARTING, ListenState.Phase.LISTENING -> EchoPalette.Green
    ListenState.Phase.THINKING, ListenState.Phase.SPEAKING -> EchoPalette.Purple
    ListenState.Phase.PAUSED -> EchoPalette.Yellow
    ListenState.Phase.FAILED -> EchoPalette.Red
    ListenState.Phase.OFF -> EchoPalette.Muted
}
