package com.echo.contact.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Refresh
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
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.viewmodel.compose.viewModel
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
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(onUnpair: () -> Unit) {
    val vm: MainViewModel = viewModel()
    val baseUrl by vm.baseUrl.collectAsState()
    var tab by remember { mutableIntStateOf(0) }
    var showSettings by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) { vm.init() }

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
                    onClick = { tab = 0 },
                    icon = { Icon(Icons.Default.Mic, contentDescription = null) },
                    label = { Text("说话") },
                )
                NavigationBarItem(
                    selected = tab == 1,
                    onClick = { tab = 1 },
                    icon = { Icon(Icons.Default.EventNote, contentDescription = null) },
                    label = { Text("会议") },
                )
                NavigationBarItem(
                    selected = tab == 2,
                    onClick = { tab = 2 },
                    icon = { Icon(Icons.Default.EventNote, contentDescription = null) },
                    label = { Text("回顾") },
                )
            }
        },
    ) { padding ->
        Box(modifier = Modifier.fillMaxSize().padding(padding)) {
            when (tab) {
                0 -> TalkScreen(vm)
                1 -> MeetingsTab(vm)
                else -> ReviewTab(vm)
            }
        }
    }
}
