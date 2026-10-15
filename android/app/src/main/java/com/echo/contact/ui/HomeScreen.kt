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
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Refresh
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
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.echo.contact.service.ListenState
import com.echo.contact.ui.theme.EchoPalette

/**
 * 应用外壳：**三个分区 + 设置**（2026-10-15 正式版定型）。
 *
 * 与旧版的区别（旧版是"边做边堆"的四页：仪表盘/指令/会议/回顾）：
 *
 *  * **首页是「说话」**，不是「仪表盘」 —— 手机是触点，一进来就该能说话、能看这一轮的结果；
 *    整机状态（版本/端口/组件）收进「设置 → 关于」，不占主业的位置。
 *  * **`指令` 改名「说话」**：它本来就是"说一句 + 看结果 + 翻历史"，名字跟着功能走。
 *  * **设置是齿轮（右上角）而不是第五个页签**：它是"偶尔改一次"的地方，
 *    不该与每天要用的三件事平起平坐。
 *
 * ## 这一层还是两条"全局可见性"的落点（2026-10-16）
 *
 *  * **常驻听写细条**（任务 B4）：常驻听写的状态原来只在「说话」页看得见，切到会议/回顾
 *    就不知道它还在听、还是被暂停了。细条挂在**这一层**（三个页签之上），任何页都能看见；
 *    点它回「说话」页。文案来自 `ListenState.stripOf`（纯函数，`ListenStateTest` 钉住）。
 *  * **通知落点**（任务 B8）：`MeetingOpenStore` 里那次"要打开哪场会议"由这里消费，
 *    切到会议页并交给 `MainViewModel.openMeetingByName`（复用已有的 `openMeeting`）。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(onUnpair: () -> Unit) {
    val vm: MainViewModel = viewModel()
    val baseUrl by vm.baseUrl.collectAsState()
    // 页签由 ViewModel 持有：通知点进来时"先切到会议页"要能改它（见 openMeetingByName）。
    val tab by vm.tab.collectAsState()
    var showSettings by remember { mutableStateOf(false) }

    // 常驻听写的状态（进程级 object，界面只是读）：任何页都能看见"在听/在等/在念/已暂停"。
    val listenPhase by ListenState.phase.collectAsState()
    val listenDetail by ListenState.detail.collectAsState()
    val listenSegments by ListenState.segments.collectAsState()
    val listenStrip = ListenState.stripOf(listenPhase, listenDetail, listenSegments)

    LaunchedEffect(Unit) { vm.init() }

    // ---- 通知带来的"打开那场会议"（冷启动/热启动都写进同一个 store）----
    // 消费一次就没了：所以转屏、重放、重复投递都不会把同一个目标打开两遍。
    LaunchedEffect(Unit) {
        MeetingOpenStore.consume()?.let { vm.openMeetingByName(it.name) }
    }

    if (showSettings) {
        SettingsScreen(vm = vm, onBack = { showSettings = false }, onUnpair = onUnpair)
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
                    IconButton(onClick = { showSettings = true }) {
                        Icon(Icons.Default.Settings, contentDescription = "设置")
                    }
                },
            )
        },
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = tab == 0,
                    onClick = { vm.selectTab(0) },
                    icon = { Icon(Icons.Default.Mic, contentDescription = null) },
                    label = { Text("说话") },
                )
                NavigationBarItem(
                    selected = tab == 1,
                    onClick = { vm.selectTab(1) },
                    icon = { Icon(Icons.Default.EventNote, contentDescription = null) },
                    label = { Text("会议") },
                )
                NavigationBarItem(
                    selected = tab == 2,
                    onClick = { vm.selectTab(2) },
                    icon = { Icon(Icons.Default.EventNote, contentDescription = null) },
                    label = { Text("回顾") },
                )
            }
        },
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            // 不在听（OFF）就不显示：首页不该常驻一条空条；出错停了也**要显示**（如实说停下了）。
            listenStrip?.let { strip ->
                ListenStrip(strip, listenPhase) { vm.selectTab(0) }
            }
            Box(modifier = Modifier.fillMaxSize()) {
                when (tab) {
                    0 -> TalkScreen(vm)
                    1 -> MeetingsTab(vm)
                    else -> ReviewTab(vm)
                }
            }
        }
    }
}

/**
 * 常驻听写细条（任务 B4）：一行小字 + 一个状态灯，点一下回「说话」页。
 *
 * 为什么放在外壳这一层而不是每个页签各放一条：这是**全应用**的状态，
 * 复制三份必然有一天只改了其中一份（而且"在听/已暂停"这种真相会分叉）。
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
            Text("去说话页", style = MaterialTheme.typography.labelSmall, color = EchoPalette.Accent)
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
