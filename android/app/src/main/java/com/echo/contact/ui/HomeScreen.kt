package com.echo.contact.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Dashboard
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.Forum
import androidx.compose.material.icons.filled.Mic
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
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.viewmodel.compose.viewModel
import com.echo.contact.ui.theme.EchoPalette

/**
 * 应用外壳：四个分区，照着面板的页签来 —— **仪表盘 / 指令 / 会议 / 回顾**。
 *
 * 手机是"ECHO 的第二个前端"（设计文档 §0/§1），所以分区与面板对齐，
 * 让人在两处看到的是同一件事、同一套口径。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(onUnpair: () -> Unit) {
    val vm: MainViewModel = viewModel()
    val baseUrl by vm.baseUrl.collectAsState()
    var tab by remember { mutableIntStateOf(0) }

    LaunchedEffect(Unit) { vm.init() }

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
            NavigationBar {
                NavigationBarItem(
                    selected = tab == 0,
                    onClick = { tab = 0 },
                    icon = { Icon(Icons.Default.Dashboard, contentDescription = null) },
                    label = { Text("仪表盘") },
                )
                NavigationBarItem(
                    selected = tab == 1,
                    onClick = { tab = 1 },
                    icon = { Icon(Icons.Default.Forum, contentDescription = null) },
                    label = { Text("指令") },
                )
                NavigationBarItem(
                    selected = tab == 2,
                    onClick = { tab = 2 },
                    icon = { Icon(Icons.Default.Mic, contentDescription = null) },
                    label = { Text("会议") },
                )
                NavigationBarItem(
                    selected = tab == 3,
                    onClick = { tab = 3 },
                    icon = { Icon(Icons.Default.EventNote, contentDescription = null) },
                    label = { Text("回顾") },
                )
            }
        },
    ) { padding ->
        Box(modifier = Modifier.fillMaxSize().padding(padding)) {
            when (tab) {
                0 -> DashboardTab(vm, onUnpair = onUnpair)
                1 -> CommandsTab(vm)
                2 -> MeetingsTab(vm)
                else -> ReviewTab(vm)
            }
        }
    }
}
