package com.echo.contact.ui

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
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.EventNote
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.echo.contact.service.ListenState
import com.echo.contact.ui.theme.EchoPalette

/**
 * **第 1 页：ECHO 超级助理**（用户 2026-10-18：*"第一页是 ECHO 超级助理，类似当前的仪表盘，
 * 展示一些状态和三个功能的主要入口"*）。
 *
 * 这一页只干两件事：
 *  1. **看一眼现在怎么样** —— 服务状态卡（沿用既有 `EchoStatusCard`：服务/智能体/语音合成/
 *     会议/回顾）+ 在听状态。**没有**后端/中继那一档：手机拿不到那些事实，
 *     不编（"没有的能力别编"）。
 *  2. **挑一页去用** —— 指令 / 会议 / 回顾三个主入口，点了切到对应页签
 *     （映射在 [ASSISTANT_ENTRIES]，`EchoTabsTest` 钉住"三个入口都指向真实存在的页"）。
 *
 * 为什么不在这里再放一份录音/指令功能：功能都在各自的页里，这里只是入口。
 * 复制一份到首页，迟早出现"两处都能开始录音、状态却不同步"。
 */
@Composable
internal fun SuperAssistantTab(vm: MainViewModel, onOpenTab: (EchoTab) -> Unit) {
    val listenPhase by ListenState.phase.collectAsState()
    val listenDetail by ListenState.detail.collectAsState()
    val listenSegments by ListenState.segments.collectAsState()
    val strip = ListenState.stripOf(listenPhase, listenDetail, listenSegments)

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        // ⓪ 服务状态（现有信息：服务 / 智能体 / 语音合成 / 会议 / 回顾）
        item { EchoStatusCard(vm) }

        // ① 在听（现有信息：常驻听写的相位与最近一句文案）
        item { ListenStatusCard(strip, listenPhase) }

        // ② 三个主入口
        item {
            Text(
                "主要功能",
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.padding(top = 4.dp, bottom = 2.dp),
            )
        }
        items(ASSISTANT_ENTRIES.size) { index ->
            val entry = ASSISTANT_ENTRIES[index]
            AssistantEntryCard(entry) { onOpenTab(entry.tab) }
        }
    }
}

/** 一张主入口卡：图标 + 标题 + 一句说明 + 箭头，整张卡可点。 */
@Composable
private fun AssistantEntryCard(entry: AssistantEntry, onClick: () -> Unit) {
    EchoCard(modifier = Modifier.clickable(onClick = onClick)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                entryIcon(entry.tab),
                contentDescription = null,
                tint = EchoPalette.Accent,
                modifier = Modifier.size(22.dp),
            )
            Spacer(Modifier.size(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(entry.title, style = MaterialTheme.typography.titleSmall)
                MutedLine(entry.subtitle, maxLines = 1)
            }
            Spacer(Modifier.size(8.dp))
            Icon(
                Icons.Default.ChevronRight,
                contentDescription = "打开${entry.title}",
                tint = EchoPalette.Muted,
                modifier = Modifier.size(20.dp),
            )
        }
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
