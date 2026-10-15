package com.echo.contact.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.service.ListenPrefs
import com.echo.contact.service.ListenService
import com.echo.contact.service.ListenState
import com.echo.contact.service.ListenTuning
import com.echo.contact.ui.theme.EchoPalette

/**
 * 指令页：**打字发**、**按住说话**、看历史、看某条的会话。
 *
 * 语音走 ECHO 的转写（`POST /api/assistant/voice-command`，裸 wav），
 * 不用手机的系统识别 —— 这台设备的识别服务不服务第三方（2026-10-09 实测），
 * 而 ECHO 那边又快又准（139–203 ms、语义全对）。
 */
@Composable
fun CommandsTab(vm: MainViewModel) {
    val session by vm.session.collectAsState()
    val sessionLoading by vm.sessionLoading.collectAsState()
    val sessionNote by vm.sessionNote.collectAsState()

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
            onBack = { vm.closeSession() },
        )
        return
    }

    val commands by vm.commands.collectAsState()
    val hasMore by vm.commandsHasMore.collectAsState()
    val submit by vm.submit.collectAsState()
    val speaking by vm.speaking.collectAsState()
    val speakNote by vm.speakNote.collectAsState()
    val systemTtsInfo by vm.systemTtsInfo.collectAsState()
    val recording by vm.recording.collectAsState()
    val recordMs by vm.recordMs.collectAsState()
    val voiceText by vm.voiceText.collectAsState()

    val context = LocalContext.current
    var input by remember { mutableStateOf("") }
    var query by remember { mutableStateOf("") }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) vm.startRecording() }

    // ---- 后台听写（阶段5）：服务在前台服务里常驻听，锁屏/切走都能用 ----
    val listenPhase by ListenState.phase.collectAsState()
    val listenDetail by ListenState.detail.collectAsState()
    val listenSegments by ListenState.segments.collectAsState()
    val listening = listenPhase != ListenState.Phase.OFF && listenPhase != ListenState.Phase.FAILED
    val (listenTitle, listenBody) = ListenState.textsOf(listenPhase, listenDetail, listenSegments)

    // 通知权限**给不给都开**：不给只是看不到那条常驻通知，听写本身照跑
    val notifLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { ListenService.start(context) }

    val listenPermissionLauncher = rememberLauncherForActivityResult(
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
            listenPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
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

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        // ---- 语音 ----
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "按住说话",
                        dotColor = if (recording) EchoPalette.Red else EchoPalette.Muted,
                        trailing = if (recording) "录音中 ${"%.1f".format(recordMs / 1000.0)} 秒" else "",
                        trailingColor = EchoPalette.Red,
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        if (!recording) {
                            Button(onClick = {
                                val granted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                                    PackageManager.PERMISSION_GRANTED
                                if (granted) vm.startRecording() else permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                            }) {
                                Icon(Icons.Default.Mic, contentDescription = null, modifier = Modifier.size(18.dp))
                                Spacer(Modifier.size(6.dp))
                                Text("说一句")
                            }
                        } else {
                            Button(onClick = { vm.finishRecording() }) { Text("说完了") }
                            TextButton(onClick = { vm.cancelRecording() }) { Text("取消") }
                        }
                    }
                    if (voiceText.isNotBlank()) {
                        Spacer(Modifier.height(6.dp))
                        MutedLine("ECHO 听成：$voiceText")
                    }
                    Spacer(Modifier.height(4.dp))
                    MutedLine("录音交给 ECHO 转写（手机上显示的是它听成什么）")
                }
            }
        }

        // ---- 后台听写（阶段5）----
        // 与上面那个"按住说话"是两条路：这条活在**前台服务**里，界面切走/锁屏照听，
        // 一句说完自己发出去、把回复念出来 —— 开车时人不用碰手机。
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "后台听写",
                        dotColor = if (listening) EchoPalette.Red else EchoPalette.Muted,
                        trailing = if (listening) "在听" else "关",
                        trailingColor = if (listening) EchoPalette.Red else EchoPalette.Muted,
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Switch(
                            checked = listening,
                            onCheckedChange = { on ->
                                if (on) startBackgroundListen() else ListenService.stop(context)
                            },
                        )
                        Spacer(Modifier.size(10.dp))
                        Column(modifier = Modifier.fillMaxWidth()) {
                            Text(listenTitle, style = MaterialTheme.typography.bodyMedium)
                            if (listenBody.isNotBlank()) {
                                MutedLine(listenBody, maxLines = 3)
                            }
                        }
                    }
                    Spacer(Modifier.height(4.dp))
                    MutedLine("开着就能锁屏说话：说完停一下自动交给 ECHO，回复由手机念出来")

                    // ---- 听写参数（可调）----
                    // 这套判据是在安静房间里定的，战场却是车里（发动机/风噪/音乐）：
                    // 门限与收尾静音都可能要重定，所以给三个旋钮，改完下次开听写生效。
                    var showTuning by remember { mutableStateOf(false) }
                    var tuning by remember { mutableStateOf(ListenPrefs.load(context)) }
                    TextButton(onClick = { showTuning = !showTuning }) {
                        Text(if (showTuning) "收起参数" else "调参数（车里噪声大就调这里）")
                    }
                    if (showTuning) {
                        TuningRow(
                            label = "说话门限 ${"%.3f".format(tuning.silenceThreshold)}",
                            value = tuning.silenceThreshold.toFloat(),
                            range = ListenTuning.MIN_SILENCE_THRESHOLD.toFloat()..
                                ListenTuning.MAX_SILENCE_THRESHOLD.toFloat(),
                            onChange = { tuning = tuning.copy(silenceThreshold = it.toDouble()) },
                            onDone = { ListenPrefs.save(context, tuning) },
                            hint = "调大 = 车里的底噪不容易被当成说话",
                        )
                        TuningRow(
                            label = "说完静音 ${"%.1f".format(tuning.hangoverMs / 1000.0)} 秒",
                            value = tuning.hangoverMs.toFloat(),
                            range = ListenTuning.MIN_HANGOVER_MS.toFloat()..
                                ListenTuning.MAX_HANGOVER_MS.toFloat(),
                            onChange = { tuning = tuning.copy(hangoverMs = it.toInt()) },
                            onDone = { ListenPrefs.save(context, tuning) },
                            hint = "调小 = 反应更快；调大 = 给思考停顿留余量",
                        )
                        TuningRow(
                            label = "没人说话 ${tuning.idleAutoStopMin} 分钟自动停",
                            value = tuning.idleAutoStopMin.toFloat(),
                            range = ListenTuning.MIN_IDLE_AUTO_STOP_MIN.toFloat()..
                                ListenTuning.MAX_IDLE_AUTO_STOP_MIN.toFloat(),
                            onChange = { tuning = tuning.copy(idleAutoStopMin = it.toInt()) },
                            onDone = { ListenPrefs.save(context, tuning) },
                            hint = "省电用；改了下次开听写生效",
                        )
                        // 全双工：念回复时也收音（能插话打断）。需要设备有 AEC，不支持就自动退回。
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Switch(
                                checked = tuning.keepListeningWhileSpeaking,
                                onCheckedChange = {
                                    tuning = tuning.copy(keepListeningWhileSpeaking = it)
                                    ListenPrefs.save(context, tuning)
                                },
                            )
                            Spacer(Modifier.size(10.dp))
                            Column(modifier = Modifier.fillMaxWidth()) {
                                Text("念回复时也继续听", style = MaterialTheme.typography.bodyMedium)
                                MutedLine(
                                    "能开口打断它；需要这台手机支持回声消除，" +
                                        "不支持会自动退回「念时不收音」"
                                )
                            }
                        }
                    }
                }
            }
        }

        // ---- 打字 ----
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

        if (submit.phase != SubmitPhase.Idle) {
            item { SubmitCard(submit, onSpeak = { vm.speak(it) }) }
        }

        item {
            TtsTestBox(
                systemInfo = systemTtsInfo,
                speaking = speaking,
                note = speakNote,
                defaultText = vm.sampleSentence,
                onSpeak = { text, engine -> vm.speak(text, engine) },
                onStop = { vm.stopSpeaking() },
            )
        }

        // ---- 历史 ----
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

        if (commands.isEmpty()) {
            item { MutedLine("还没有指令记录。") }
        } else {
            items(commands, key = { it.id ?: it.hashCode() }) { c ->
                CommandRow(
                    item = c,
                    onSpeak = { vm.speak(it) },
                    onOpenSession = { vm.loadSession(it.id ?: 0) },
                )
            }
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

/** 某条指令落到的那一轮会话（面板「历史 → 看会话」）。 */
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
                    submit.text,
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

            if (submit.reply.isNotBlank()) {
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

/**
 * 一个参数旋钮：一行标签 + 滑杆 + 一句"调大/调小会怎样"。
 *
 * `onDone`（松手时）才落盘：拖动过程中每一帧都写 SharedPreferences 是白费，
 * 而松手即存又能让"我调完了"这件事立刻生效于**下一次**开听写。
 */
@Composable
private fun TuningRow(
    label: String,
    value: Float,
    range: ClosedFloatingPointRange<Float>,
    onChange: (Float) -> Unit,
    onDone: () -> Unit,
    hint: String,
) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(label, style = MaterialTheme.typography.labelMedium)
        Slider(
            value = value,
            onValueChange = onChange,
            onValueChangeFinished = onDone,
            valueRange = range,
        )
        MutedLine(hint, maxLines = 1)
    }
}

internal fun phaseLabel(phase: SubmitPhase): String = when (phase) {
    SubmitPhase.Idle -> ""
    SubmitPhase.Sending -> "提交中"
    SubmitPhase.Accepted -> "已受理"
    SubmitPhase.Running -> "执行中"
    SubmitPhase.Done -> "已完成"
    SubmitPhase.Failed -> "未完成"
}

/** 朗读测试：一个小框 + 三个按钮。就是拿来听效果的，不做设置项。 */
@Composable
internal fun TtsTestBox(
    systemInfo: String,
    speaking: Boolean,
    note: String?,
    defaultText: String,
    onSpeak: (String, TtsEngine) -> Unit,
    onStop: () -> Unit,
) {
    var text by remember { mutableStateOf(defaultText) }

    EchoCard {
        Column {
            CardHeader(
                title = "朗读测试",
                dotColor = if (speaking) EchoPalette.Purple else EchoPalette.Muted,
                trailing = if (speaking) "正在念…" else "",
                trailingColor = EchoPalette.Purple,
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = text,
                onValueChange = { text = it },
                label = { Text("要念的话") },
                maxLines = 3,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = { onSpeak(text, TtsEngine.System) }, enabled = !speaking) { Text("手机念") }
                Button(onClick = { onSpeak(text, TtsEngine.Echo) }, enabled = !speaking) { Text("ECHO 念") }
                if (speaking) TextButton(onClick = onStop) { Text("停") }
            }
            Spacer(Modifier.height(4.dp))
            MutedLine("系统引擎：$systemInfo")
            note?.let {
                Spacer(Modifier.height(2.dp))
                MutedLine(it, color = EchoPalette.Yellow)
            }
        }
    }
}
