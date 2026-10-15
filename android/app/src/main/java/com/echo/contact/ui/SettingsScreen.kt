package com.echo.contact.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.echo.contact.service.ListenPrefs
import com.echo.contact.service.ListenTuning
import com.echo.contact.service.SpeakPrefs
import com.echo.contact.ui.theme.EchoPalette

/**
 * 设置页：把"平时不用、但要能改"的东西收在一处。
 *
 * 分四组的依据是**用户会在什么时候找它**（不是按代码模块）：
 *
 *  * **连接**：连不上/换地址/重新配对时来这儿；
 *  * **语音**：选谁念 —— 这是日常真会改的一项，所以放前面，不进「高级」；
 *  * **通知**：事件推送（还没做，如实写着）；
 *  * **高级**：听写的门限/收尾/自动停与全双工 —— 车里噪声大才需要动；
 *  * **关于**：版本、运行时长、各组件状态（原来一大张"仪表盘"占着首页，现在收在这里）。
 */
@Composable
fun SettingsScreen(vm: MainViewModel, onBack: () -> Unit, onUnpair: () -> Unit) {
    val context = LocalContext.current
    val baseUrl by vm.baseUrl.collectAsState()
    val connected by vm.connected.collectAsState()
    val statusError by vm.statusError.collectAsState()
    val status by vm.status.collectAsState()
    val systemTtsInfo by vm.systemTtsInfo.collectAsState()

    var engine by remember { mutableStateOf(SpeakPrefs.load(context)) }
    var tuning by remember { mutableStateOf(ListenPrefs.load(context)) }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onBack) { Icon(Icons.Default.ArrowBack, contentDescription = "返回") }
                Text("设置", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
            }
        }

        // ---------------------------------------------------------------- 连接
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "连接",
                        dotColor = when (connected) {
                            true -> EchoPalette.Green
                            false -> EchoPalette.Red
                            null -> EchoPalette.Muted
                        },
                        trailing = when (connected) {
                            true -> "已连接"
                            false -> "连不上"
                            null -> "未知"
                        },
                        trailingColor = when (connected) {
                            true -> EchoPalette.Green
                            false -> EchoPalette.Red
                            null -> EchoPalette.Muted
                        },
                    )
                    Spacer(Modifier.height(6.dp))
                    MutedLine(baseUrl.ifBlank { "还没配对" }, maxLines = 2)
                    if (connected == false && !statusError.isNullOrBlank()) {
                        Spacer(Modifier.height(4.dp))
                        MutedLine(statusError!!, color = EchoPalette.Red, maxLines = 3)
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = { vm.refresh() }) { Text("检测连接") }
                        TextButton(onClick = { vm.clearSession(); onUnpair() }) { Text("解除配对 / 重新配对") }
                    }
                    Spacer(Modifier.height(4.dp))
                    MutedLine(
                        "手机与电脑互相连不通时：在电脑面板上打开「允许手机经后端中转」，" +
                            "把面板显示的那串中继地址填到配对页的「后端地址」里。",
                        maxLines = 4,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 语音
        item {
            EchoCard {
                Column {
                    CardHeader(title = "语音", dotColor = EchoPalette.Muted, trailing = SpeakPrefs.label(engine))
                    Spacer(Modifier.height(8.dp))
                    MutedLine("回复由谁念", maxLines = 1)
                    Spacer(Modifier.height(6.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        if (engine == SpeakPrefs.ENGINE_SYSTEM) {
                            Button(onClick = {}) { Text("手机念") }
                        } else {
                            OutlinedButton(onClick = {
                                engine = SpeakPrefs.ENGINE_SYSTEM
                                SpeakPrefs.save(context, engine)
                            }) { Text("手机念") }
                        }
                        if (engine == SpeakPrefs.ENGINE_ECHO) {
                            Button(onClick = {}) { Text("ECHO 念") }
                        } else {
                            OutlinedButton(onClick = {
                                engine = SpeakPrefs.ENGINE_ECHO
                                SpeakPrefs.save(context, engine)
                            }) { Text("ECHO 念") }
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    MutedLine(
                        "手机念：秒回、离线也行。ECHO 念：音色与电脑一致，但要有网。" +
                            "系统引擎：$systemTtsInfo",
                        maxLines = 3,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 通知
        item {
            EchoCard {
                Column {
                    CardHeader(title = "通知", dotColor = EchoPalette.Muted, trailing = "还没做")
                    Spacer(Modifier.height(6.dp))
                    MutedLine(
                        "目标：会议转写/纪要好了、回复到了，手机自己收到通知（不用你打开 App 去看）。" +
                            "这条要电脑侧先有「事件下行」，下一步做。",
                        maxLines = 4,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 高级
        item {
            EchoCard {
                Column {
                    CardHeader(title = "高级 · 听写", dotColor = EchoPalette.Muted, trailing = "")
                    Spacer(Modifier.height(6.dp))
                    MutedLine("车里噪声大、或者它老是抢话/反应慢，才需要动这里。改完下次开听写生效。", maxLines = 3)
                    Spacer(Modifier.height(8.dp))
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
                    Spacer(Modifier.height(4.dp))
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
                                "能开口打断它；需要这台手机支持回声消除，不支持会自动退回「念时不收音」",
                                maxLines = 2,
                            )
                        }
                    }
                }
            }
        }

        // ---------------------------------------------------------------- 关于
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "关于",
                        dotColor = if (status != null) EchoPalette.Green else EchoPalette.Muted,
                        trailing = status?.version?.let { "ECHO $it" } ?: "ECHO",
                        trailingColor = EchoPalette.Accent,
                    )
                    if (status == null) {
                        Spacer(Modifier.height(6.dp))
                        MutedLine("正在读取状态…", maxLines = 1)
                    } else {
                        if (status!!.uptimeText().isNotBlank()) {
                            Spacer(Modifier.height(6.dp))
                            MutedLine("已运行 ${status!!.uptimeText()}", maxLines = 1)
                        }
                        if (status!!.components.isNotEmpty()) {
                            Spacer(Modifier.height(8.dp))
                            HorizontalDivider(color = EchoPalette.Border)
                            Spacer(Modifier.height(6.dp))
                            status!!.components.forEach { c ->
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    StatusDot(EchoPalette.statusColor(c.status), size = 8)
                                    Spacer(Modifier.size(8.dp))
                                    Text(
                                        componentLabel(c.name),
                                        style = MaterialTheme.typography.bodyMedium,
                                        modifier = Modifier.weight(1f),
                                    )
                                    Text(
                                        c.detail.ifBlank { shortStatus(c.status) },
                                        style = MaterialTheme.typography.labelSmall,
                                        color = EchoPalette.Muted,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                        modifier = Modifier.weight(1.4f),
                                    )
                                }
                            }
                        }
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
