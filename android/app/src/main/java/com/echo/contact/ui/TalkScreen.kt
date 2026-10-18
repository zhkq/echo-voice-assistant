package com.echo.contact.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.service.ListenService
import com.echo.contact.service.ListenState
import com.echo.contact.ui.theme.EchoPalette

/**
 * 说话页（正式版首页）：**手机是触点 —— 在这里发起，也在这里收。**
 *
 * 三条设计口径（2026-10-15 与用户定的）：
 *
 *  1. **主操作只有一件**：说一句。常驻听写不再是并排的第二张卡，而是这张卡里的
 *     一个次入口（"开车时让它一直听着"）—— 而且它的状态**就是**服务的状态，
 *     不另存一个开关（两处状态必然漂移）。
 *  2. **"手机收"要看得见**：这一轮的结果（它听成什么 + 回复）就在主操作下面，
 *     带一个重播按钮；历史再往下（默认最近几条，可展开全部、可分页）。
 *  3. **仪表盘不在这里**：整机状态在「超级助理」页（第 1 页）—— 那里才是"看一眼现在怎么样"的地方；
 *     这一页只做"说一句 + 看这一轮结果 + 翻历史"，别把状态卡再复制一份过来。
 */
@Composable
fun TalkScreen(vm: MainViewModel) {
    val session by vm.session.collectAsState()
    val sessionLoading by vm.sessionLoading.collectAsState()
    val sessionNote by vm.sessionNote.collectAsState()
    // 朗读失败/说明：以前只有 ViewModel 内部赋值，**全 App 没有一处界面读它**
    // → 按了「念」没声音、也没提示。指令页这里摆一行（单行、几秒后自动消失）。
    val speakNote by vm.speakNote.collectAsState()

    if (session != null || sessionLoading || sessionNote != null) {
        SessionView(
            loading = sessionLoading,
            note = sessionNote,
            title = session?.title.orEmpty(),
            cwd = session?.cwd.orEmpty(),
            exists = session?.exists == true,
            running = session?.running == true,
            messages = session?.messages.orEmpty().map { it.role.orEmpty() to it.text.orEmpty() },
            onSpeak = { vm.speak(it) },
            speakNote = speakNote,
            onDismissSpeakNote = { vm.clearSpeakNote() },
            onBack = { vm.closeSession() },
        )
        return
    }

    val connected by vm.connected.collectAsState()
    val statusError by vm.statusError.collectAsState()
    val commands by vm.commands.collectAsState()
    val hasMore by vm.commandsHasMore.collectAsState()
    val submit by vm.submit.collectAsState()
    val recording by vm.recording.collectAsState()
    val recordMs by vm.recordMs.collectAsState()
    val voiceText by vm.voiceText.collectAsState()

    val context = LocalContext.current
    var input by remember { mutableStateOf("") }
    var query by remember { mutableStateOf("") }
    var showAll by remember { mutableStateOf(false) }

    // ---- 常驻听写（前台服务）的状态：界面只是**读**它 ----
    val listenPhase by ListenState.phase.collectAsState()
    val listenDetail by ListenState.detail.collectAsState()
    val listenSegments by ListenState.segments.collectAsState()
    val listening = listenPhase != ListenState.Phase.OFF && listenPhase != ListenState.Phase.FAILED
    val (listenTitle, listenBody) = ListenState.textsOf(listenPhase, listenDetail, listenSegments)

    val micLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) vm.startRecording() }

    // 通知权限**给不给都开**：不给只是看不到那条常驻通知，听写本身照跑
    val notifLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { ListenService.start(context) }

    val listenLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (!granted) return@rememberLauncherForActivityResult
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            ListenService.start(context)
        }
    }

    fun startBackgroundListen() {
        val micOk = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (!micOk) {
            listenLauncher.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            ListenService.start(context)
        }
    }

    val shown = if (showAll) commands else commands.take(5)

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        if (connected == false) {
            item { ErrorCard(statusError ?: "连不上 ECHO") }
        }

        // ---- ① 主操作（唯一的一件大事）----
        item {
            EchoCard {
                Column {                    CardHeader(
                        title = if (listening) "一直听着" else "说一句",
                        dotColor = when {
                            recording || listening -> EchoPalette.Red
                            else -> EchoPalette.Muted
                        },
                        trailing = when {
                            recording -> "录音中 ${"%.1f".format(recordMs / 1000.0)} 秒"
                            listening -> "在听"
                            else -> ""
                        },
                        trailingColor = EchoPalette.Red,
                    )
                    Spacer(Modifier.height(10.dp))

                    if (listening) {
                        // 常驻听写进行中：状态 + 停止（参数在「设置 → 高级」）
                        Text(listenTitle, style = MaterialTheme.typography.bodyMedium)
                        if (listenBody.isNotBlank()) {
                            Spacer(Modifier.height(2.dp))
                            MutedLine(listenBody, maxLines = 3)
                        }
                        Spacer(Modifier.height(10.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            MutedLine("已送 ${listenSegments} 句", maxLines = 1)
                            Spacer(Modifier.weight(1f))
                            Button(onClick = { ListenService.stop(context) }) { Text("停止") }
                        }
                    } else if (recording) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Button(onClick = { vm.finishRecording() }) { Text("说完了") }
                            TextButton(onClick = { vm.cancelRecording() }) { Text("取消") }
                        }
                    } else {
                        Button(
                            onClick = {
                                val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                                    PackageManager.PERMISSION_GRANTED
                                if (granted) vm.startRecording() else micLauncher.launch(Manifest.permission.RECORD_AUDIO)
                            },
                            modifier = Modifier.fillMaxWidth().height(56.dp),
                        ) {
                            Icon(Icons.Default.Mic, contentDescription = null, modifier = Modifier.size(20.dp))
                            Spacer(Modifier.size(8.dp))
                            Text("说一句", style = MaterialTheme.typography.titleMedium)
                        }
                    }

                    // 说给我们看：ECHO 把它听成了什么
                    if (voiceText.isNotBlank()) {
                        Spacer(Modifier.height(8.dp))
                        MutedLine("ECHO 听成：$voiceText")
                    }

                    // 开车那条路：不并排、不抢主操作，放在下面当"另一种用法"
                    if (!listening) {
                        Spacer(Modifier.height(10.dp))
                        HorizontalDivider(color = EchoPalette.Border)
                        Spacer(Modifier.height(6.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            MutedLine("开车时不方便按：一直听着，说完自动发", maxLines = 2)
                            Spacer(Modifier.weight(1f))
                            TextButton(onClick = { startBackgroundListen() }) { Text("一直听着") }
                        }
                    }
                }
            }
        }

        // ---- ①½ 朗读反馈：`speak()` 写的那句在这里显形 ----
        // 只有真的发生了（失败/没有可念的/ECHO 引擎名）才占一行；单行、几秒后自动消失。
        speakNote?.takeIf { it.isNotBlank() }?.let {
            item { SpeakNoteLine(it, onDismiss = { vm.clearSpeakNote() }) }
        }

        // ---- ② 刚才这一轮（"手机收"的落点）----
        // 以前这里只在 `phase != Idle` 时画：一次**静默失败**（录音为空 / 麦打不开 / 上一条还没回来）
        // 会把「这一轮」整块留空，用户眼前只剩下面的历史列表 —— 那正是"把旧指令当成这次答复"的形状。
        if (submit.phase != SubmitPhase.Idle || submit.blocked.isNotBlank()) {
            item { SubmitCard(submit, onSpeak = { vm.speak(it) }) }
        }

        // ---- ③ 打字：语音的备用入口 ----
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                OutlinedTextField(
                    value = input,
                    onValueChange = { input = it },
                    label = { Text("或者打字") },
                    maxLines = 3,
                    modifier = Modifier.weight(1f),
                )
                Button(
                    onClick = {
                        val text = input
                        input = ""
                        vm.sendCommand(text)
                    },
                    enabled = input.isNotBlank(),
                ) {
                    Icon(Icons.Default.Send, contentDescription = "发送")
                }
            }
        }

        // ---- ④ 历史 ----
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("最近的指令", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
                TextButton(onClick = { showAll = !showAll }) {
                    Text(if (showAll) "只看最近 5 条" else "看全部")
                }
            }
        }

        if (showAll) {
            item {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    OutlinedTextField(
                        value = query,
                        onValueChange = { query = it },
                        label = { Text("筛选历史") },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                    )
                    OutlinedButton(onClick = { vm.loadCommands(limit = 50, q = query) }) { Text("查") }
                }
            }
        }

        if (shown.isEmpty()) {
            item { MutedLine("还没有指令记录。") }
        } else {
            items(shown, key = { it.id ?: it.hashCode() }) { c ->
                CommandRow(
                    item = c,
                    onSpeak = { vm.speak(it) },
                    onOpenSession = { vm.loadSession(it.id ?: 0) },
                )
            }
            if (showAll) {
                item {
                    if (hasMore) {
                        TextButton(onClick = { vm.loadMoreCommands() }) { Text("加载更早的") }
                    } else {
                        MutedLine("已经是全部了（共 ${commands.size} 条）")
                    }
                }
            }
        }
    }
}

/**
 * 这一轮的结果：提交状态 + 回复（可重播）。
 *
 * 从「指令」页原样搬来的（那时它挂在历史上面），因为它才是"手机收"的主角：
 * 用户说完一句，眼睛要能马上看到"它听成了什么、回了什么"，而不是去列表里翻。
 *
 * ## 2026-10-18 真机 bug 之后的两条硬规矩（判据在 `SendRoundTest`）
 *
 *  * **这一轮到底发了哪一句**要摆在最上面（[roundHeadline]）——"ECHO 听成：…" 只在
 *    电脑回过识别文本时才有，失败时一个字段都没有，用户分不清这条是不是自己那句；
 *  * **回复只在 [showsReply] 为真时显示**。`Done` 是唯一能证明 `reply` 属于这一条的阶段
 *    （它是 `awaitResult` 认领到"这一轮自己那一行"之后才写的）；`Failed` 时哪怕
 *    `reply` 里还留着上一轮的字，也**一个字都不许**摆出来 —— 那就是"旧数据顶替"。
 */
@Composable
private fun SubmitCard(submit: SubmitState, onSpeak: (String) -> Unit) {
    val accent = when (submit.phase) {
        SubmitPhase.Failed -> EchoPalette.Red
        SubmitPhase.Done -> EchoPalette.Green
        SubmitPhase.Idle -> EchoPalette.Muted
        else -> EchoPalette.Purple
    }

    EchoCard(container = EchoPalette.Card2) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (submit.active) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp))
                    Spacer(Modifier.size(8.dp))
                }
                Text(
                    "这一轮：${roundHeadline(submit)}",
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier.weight(1f),
                    maxLines = 3,
                    overflow = TextOverflow.Ellipsis,
                )
                Spacer(Modifier.size(8.dp))
                Text(phaseLabel(submit.phase), style = MaterialTheme.typography.labelSmall, color = accent)
            }

            if (submit.message.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(submit.message, style = MaterialTheme.typography.labelSmall, color = accent)
            }

            // 「刚才那次点击没发出去」：与这一轮分开说（它不改变这一轮的状态）
            if (submit.blocked.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    submit.blocked,
                    style = MaterialTheme.typography.labelSmall,
                    color = EchoPalette.Yellow,
                )
            }

            if (showsReply(submit.phase) && submit.reply.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Surface(
                        color = EchoPalette.Bg,
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.weight(1f),
                    ) {
                        Text(
                            submit.reply,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(10.dp),
                        )
                    }
                    IconButton(onClick = { onSpeak(submit.reply) }) {
                        Icon(Icons.Default.VolumeUp, contentDescription = "朗读")
                    }
                }
            }
        }
    }
}

/** 某条指令落到的那一轮会话（「看会话」）。 */
@Composable
private fun SessionView(
    loading: Boolean,
    note: String?,
    title: String,
    cwd: String,
    exists: Boolean,
    running: Boolean,
    messages: List<Pair<String, String>>,
    onSpeak: (String) -> Unit,
    speakNote: String?,
    onDismissSpeakNote: () -> Unit,
    onBack: () -> Unit,
) {
    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "返回") }
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    title.ifBlank { "会话" },
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                if (cwd.isNotBlank()) MutedLine(cwd, maxLines = 1)
            }
            if (running) Text("进行中", style = MaterialTheme.typography.labelSmall, color = EchoPalette.Purple)
        }

        // 这一页上的「朗读」也会失败：同一行反馈（单行、几秒后自动消失）
        speakNote?.takeIf { it.isNotBlank() }?.let {
            Spacer(Modifier.height(6.dp))
            SpeakNoteLine(it, onDismiss = onDismissSpeakNote)
        }

        Spacer(Modifier.height(8.dp))
        when {
            loading -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(16.dp))
                Spacer(Modifier.size(8.dp))
                Text("正在读取会话…", style = MaterialTheme.typography.bodySmall)
            }
            note != null -> MutedLine(note, color = EchoPalette.Yellow)
            !exists -> MutedLine("这条指令没有关联的会话（发送失败，或当时没拿到会话）。")
            messages.isEmpty() -> MutedLine("这个会话里没有可显示的内容。")
            else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(messages) { (role, text) ->
                    EchoCard(container = if (role == "user") EchoPalette.Card2 else EchoPalette.Card) {
                        Column {
                            MutedLine(if (role == "user") "我" else "ECHO", maxLines = 1)
                            Spacer(Modifier.height(3.dp))
                            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                Text(text, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
                                if (role != "user") {
                                    IconButton(onClick = { onSpeak(text) }) {
                                        Icon(Icons.Default.VolumeUp, contentDescription = "朗读", modifier = Modifier.size(18.dp))
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** 提交阶段 → 中文（唯一判据，别在别处再写一遍）。 */
internal fun phaseLabel(phase: SubmitPhase): String = when (phase) {
    SubmitPhase.Idle -> ""
    SubmitPhase.Sending -> "提交中"
    SubmitPhase.Accepted -> "已受理"
    SubmitPhase.Running -> "执行中"
    SubmitPhase.Done -> "已完成"
    SubmitPhase.Failed -> "未完成"
}
