package com.echo.contact.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
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
import com.echo.contact.audio.InputDevices
import com.echo.contact.service.AppPrefs
import com.echo.contact.service.ListenPrefs
import com.echo.contact.service.ListenTuning
import com.echo.contact.service.SpeakPrefs
import com.echo.contact.ui.theme.EchoPalette

/**
 * 设置页 —— 只管 **App 自己**的选项与权限。
 *
 * 用户 2026-10-15 的口径（照抄，别改意思）：
 *
 * > app 是 echo 的前端触点，不必所有功能都实现，核心功能入口、资料展示、服务状态是主要功能；
 * > 设置需要单独设计，负责 **app 自己的选项、权限等设置**的地方，
 * > 比如输入输出设备，媒体键或者手机按键等快捷方式。
 *
 * 于是这一页的**边界**是：凡是"影响手机这一侧"的（连接、设备、按键、朗读方式、听写参数、
 * 权限与省电、App 版本）都在这；凡是 **ECHO 那台电脑**上的配置（转写引擎、能力后端、
 * 会议参数、模型路由……）**一律不在这里** —— 那些去电脑面板改。两处都能改同一个键，
 * 迟早漂移（本仓库在"同一个键只有一处能改"上栽过）。
 *
 * 「服务状态」也不在这（它在首页那张状态卡上）：状态是天天看的，设置是偶尔改的。
 */
@Composable
fun SettingsScreen(vm: MainViewModel, onBack: () -> Unit, onUnpair: () -> Unit) {
    val context = LocalContext.current
    val baseUrl by vm.baseUrl.collectAsState()
    val connected by vm.connected.collectAsState()
    val statusError by vm.statusError.collectAsState()
    val status by vm.status.collectAsState()

    var engine by remember { mutableStateOf(SpeakPrefs.load(context)) }
    var tuning by remember { mutableStateOf(ListenPrefs.load(context)) }
    var mediaKeys by remember { mutableStateOf(AppPrefs.mediaKeys(context)) }
    var inputId by remember { mutableStateOf(AppPrefs.inputDeviceId(context)) }
    // 设备列表与版本号"进页面取一次"：它们是系统状态，不该跟着组合重组反复查
    val inputs = remember { InputDevices.list(context) }
    val appVersion = remember { appVersionText(context) }

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
            MutedLine("这里只放手机这一侧的选项；电脑上的配置去 ECHO 面板改", maxLines = 2)
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
                        TextButton(onClick = { vm.clearSession(); onUnpair() }) { Text("重新配对") }
                    }
                    MutedLine(
                        "手机与电脑互相连不通时：在电脑面板上打开「允许手机经后端中转」，" +
                            "把面板显示的那串中继地址填到配对页的「后端地址」里。",
                        maxLines = 4,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 设备
        item {
            EchoCard {
                Column {
                    CardHeader(title = "设备", dotColor = EchoPalette.Muted)
                    Spacer(Modifier.height(6.dp))
                    MutedLine("录音用哪支麦克风。现在：" + InputDevices.describe(context), maxLines = 3)
                    Spacer(Modifier.height(6.dp))
                    DeviceChoiceRow(
                        label = "跟随系统",
                        selected = inputId == AppPrefs.FOLLOW_SYSTEM,
                        onClick = {
                            inputId = AppPrefs.FOLLOW_SYSTEM
                            AppPrefs.saveInputDeviceId(context, inputId)
                        },
                    )
                    inputs.forEach { d ->
                        DeviceChoiceRow(
                            label = InputDevices.labelOf(d),
                            selected = inputId == d.id,
                            onClick = {
                                inputId = d.id
                                AppPrefs.saveInputDeviceId(context, inputId)
                            },
                        )
                    }
                    if (inputs.isEmpty()) {
                        MutedLine("现在只看到系统默认那一支（插上蓝牙耳机后回这一页再看）", maxLines = 2)
                    }
                    Spacer(Modifier.height(4.dp))
                    MutedLine("改完下次开听写生效（正在听时换麦要重开采集）。", maxLines = 2)
                    Spacer(Modifier.height(6.dp))
                    HorizontalDivider(color = EchoPalette.Border)
                    Spacer(Modifier.height(6.dp))
                    MutedLine(
                        "输出设备：跟随系统。手机念那条路走系统 TTS，没有指定输出设备的接口；" +
                            "ECHO 念那条路能设，但蓝牙/听筒的最终路由仍由系统定 —— " +
                            "所以这里只写实际做到的，不摆一个选了也没用的控件。",
                        maxLines = 5,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 按键与快捷方式
        item {
            EchoCard {
                Column {
                    CardHeader(title = "按键与快捷方式", dotColor = EchoPalette.Muted)
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Switch(
                            checked = mediaKeys,
                            onCheckedChange = {
                                mediaKeys = it
                                AppPrefs.saveMediaKeys(context, it)
                            },
                        )
                        Spacer(Modifier.size(10.dp))
                        Column(modifier = Modifier.fillMaxWidth()) {
                            Text("耳机 / 车机媒体键", style = MaterialTheme.typography.bodyMedium)
                            MutedLine(
                                "播放键=继续、暂停键=暂停、停止键=停掉听写。只在听写活着时生效 —— " +
                                    "绝不会按一下就开始录音；改完下次开听写生效。",
                                maxLines = 3,
                            )
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    MutedLine("通知里也有「暂停 / 继续」「停止」两个按钮（锁屏界面可直接点）。", maxLines = 2)
                    Spacer(Modifier.height(6.dp))
                    HorizontalDivider(color = EchoPalette.Border)
                    Spacer(Modifier.height(6.dp))
                    MutedLine(
                        "音量键长按静音：没做 —— 后台拦音量键要装无障碍服务（一项敏感权限），" +
                            "而媒体键 + 通知按钮已经能覆盖同一个需求。",
                        maxLines = 3,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 语音（朗读方式 + 听写参数）
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
                    Spacer(Modifier.height(4.dp))
                    MutedLine("手机念：秒回、离线也行。ECHO 念：音色与电脑一致，但要有网。", maxLines = 2)
                    Spacer(Modifier.height(8.dp))
                    HorizontalDivider(color = EchoPalette.Border)
                    Spacer(Modifier.height(6.dp))
                    MutedLine("听写（车里噪声大才动这里；改完下次开听写生效）", maxLines = 2)
                    Spacer(Modifier.height(6.dp))
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

        // ---------------------------------------------------------------- 通知
        item {
            EchoCard {
                Column {
                    CardHeader(title = "通知", dotColor = EchoPalette.Muted, trailing = "两种时机")
                    Spacer(Modifier.height(6.dp))
                    MutedLine(
                        "会通知你的：会议转写好了、纪要好了、录音结束/导入、指令有结果了；" +
                            "过程类事件（收到指令、已交给智能体）不打扰。",
                        maxLines = 3,
                    )
                    Spacer(Modifier.height(4.dp))
                    MutedLine(
                        "什么时候收得到：① App 打开时；② 常驻听写开着时（车里那条路，长轮询）。" +
                            "App 关着、听写也没开时**收不到** —— 没有推送通道" +
                            "（单位网络下 FCM 不可靠，中继上再开一条流不值当）。这条边界如实写着。",
                        maxLines = 5,
                    )
                }
            }
        }

        // ---------------------------------------------------------------- 权限与省电
        item {
            EchoCard {
                Column {
                    CardHeader(title = "权限与省电", dotColor = EchoPalette.Muted)
                    Spacer(Modifier.height(6.dp))
                    PermissionRow(
                        name = "麦克风",
                        ok = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                            PackageManager.PERMISSION_GRANTED,
                        why = "没有它，说一句和常驻听写都用不了",
                        onOpen = { openAppSettings(context) },
                    )
                    PermissionRow(
                        name = "通知",
                        ok = hasNotificationPermission(context),
                        why = "常驻听写的状态与暂停/继续按钮就挂在通知上（不给也能听，只是看不到）",
                        onOpen = { openAppSettings(context) },
                    )
                    PermissionRow(
                        name = "电池优化白名单",
                        ok = isIgnoringBatteryOptimizations(context),
                        why = "后台听写最怕被系统省电杀掉；加进白名单它才能一直在",
                        onOpen = { requestIgnoreBatteryOptimizations(context) },
                    )
                    Spacer(Modifier.height(4.dp))
                    MutedLine("这三条都是 App 自己的权限，跟电脑那边的设置无关。", maxLines = 2)
                }
            }
        }

        // ---------------------------------------------------------------- 关于
        item {
            EchoCard {
                Column {
                    CardHeader(
                        title = "关于",
                        dotColor = EchoPalette.Muted,
                        trailing = appVersion,
                        trailingColor = EchoPalette.Accent,
                    )
                    Spacer(Modifier.height(6.dp))
                    MutedLine("ECHO 触点 $appVersion", maxLines = 1)
                    MutedLine(
                        "电脑那边是 ECHO ${status?.version ?: "（读不到）"}；" +
                            "组件与端口在首页那张「服务状态」卡里（点一下展开）。",
                        maxLines = 3,
                    )
                    Spacer(Modifier.height(4.dp))
                    MutedLine("本机记录的后端地址：${baseUrl.ifBlank { "还没配对" }}", maxLines = 2)
                }
            }
        }
    }
}

/** 一行设备选项：选中的标「正在用」，其余给「用这支」。 */
@Composable
private fun DeviceChoiceRow(label: String, selected: Boolean, onClick: () -> Unit) {
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(
            label,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.weight(1f),
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        if (selected) {
            Text("正在用", style = MaterialTheme.typography.labelSmall, color = EchoPalette.Green)
        } else {
            TextButton(onClick = onClick) { Text("用这支") }
        }
    }
}

/** 一行权限：状态灯 + 名字 + 一句为什么 + 「去设置」。 */
@Composable
private fun PermissionRow(name: String, ok: Boolean, why: String, onOpen: () -> Unit) {
    Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(if (ok) EchoPalette.Green else EchoPalette.Yellow, size = 8)
                Spacer(Modifier.size(6.dp))
                Text(name, style = MaterialTheme.typography.bodyMedium)
            }
            MutedLine(why, maxLines = 2)
        }
        TextButton(onClick = onOpen) { Text(if (ok) "系统设置" else "去开启") }
    }
}

/** 一个参数旋钮：标签 + 滑杆 + 一句"调大/调小会怎样"；**松手才落盘**。 */
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

// ---------------------------------------------------------------- 系统交互（都收在这一处）

private fun hasNotificationPermission(context: Context): Boolean =
    Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
        context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
        PackageManager.PERMISSION_GRANTED

private fun isIgnoringBatteryOptimizations(context: Context): Boolean = try {
    val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
    pm?.isIgnoringBatteryOptimizations(context.packageName) == true
} catch (e: Exception) {
    false
}

private fun openAppSettings(context: Context) {
    runCatching {
        context.startActivity(
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.fromParts("package", context.packageName, null))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}

private fun requestIgnoreBatteryOptimizations(context: Context) {
    runCatching {
        context.startActivity(
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.fromParts("package", context.packageName, null))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}

/** App 自己的版本（今天问过"哪份 APK 最新"，所以关于页必须能自证是哪一版）。 */
private fun appVersionText(context: Context): String = try {
    val pi = context.packageManager.getPackageInfo(context.packageName, 0)
    val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
        pi.longVersionCode
    } else {
        @Suppress("DEPRECATION") pi.versionCode.toLong()
    }
    "v${pi.versionName} (${code})"
} catch (e: Exception) {
    "版本读不到"
}
