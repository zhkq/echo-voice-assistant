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
import androidx.compose.material3.AlertDialog
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
import com.echo.contact.audio.OutputDevices
import com.echo.contact.service.AppPrefs
import com.echo.contact.service.ListenPrefs
import com.echo.contact.service.ListenTuning
import com.echo.contact.service.MeetingMicService
import com.echo.contact.service.OfflineMeetingRetention
import com.echo.contact.service.RetentionPlan
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
    // 「上次 ECHO 念，系统接没接受你选的输出设备」—— 播放发生在 VM 里，这里只是把它显示出来
    val ttsRoute by vm.ttsRoute.collectAsState()

    var engine by remember { mutableStateOf(SpeakPrefs.load(context)) }
    var tuning by remember { mutableStateOf(ListenPrefs.load(context)) }
    var mediaKeys by remember { mutableStateOf(AppPrefs.mediaKeys(context)) }
    var inputId by remember { mutableStateOf(AppPrefs.inputDeviceId(context)) }
    var outputId by remember { mutableStateOf(AppPrefs.outputDeviceId(context)) }
    var segmentSeconds by remember { mutableStateOf(AppPrefs.segmentSeconds(context)) }
    // ---- 离线录音的本地音频（修②：有界保留 + 手动清理入口）----
    var retentionDays by remember { mutableStateOf(AppPrefs.audioRetentionDays(context)) }
    var usage by remember {
        mutableStateOf(MeetingMicService.localAudioUsage(context))
    }
    // 手动清理的结果**如实**写在这里（别静默：删了/没删/删失败都要说）
    var purgeNote by remember { mutableStateOf("") }
    // 删除**不可逆**：按一下先看出"要删的是哪几场"，再确认
    var confirmPurge by remember { mutableStateOf(false) }
    // 设备列表与版本号"进页面取一次"：它们是系统状态，不该跟着组合重组反复查
    val inputs = remember { InputDevices.list(context) }
    val outputs = remember { OutputDevices.list(context) }
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

        // ---------------------------------------------------------------- 设备（输入麦 / 输出 / 段长）
        item {
            EchoCard {
                Column {
                    CardHeader(title = "设备", dotColor = EchoPalette.Muted)
                    Spacer(Modifier.height(6.dp))
                    MutedLine("**输入**：录音用哪支麦克风。现在：" + InputDevices.describe(context), maxLines = 3)
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

                    // ---- 输出设备：只做实际能做到的（手机念没有接口，所以那条路跟随系统）----
                    MutedLine("**输出**（回复从哪儿放）现在：" + OutputDevices.describe(context), maxLines = 3)
                    // 上一行是**偏好**（选了哪支、在不在），这一行是"上一次真播的时候系统怎么答的"
                    // —— 两者一起看才是全部事实。**只**能说"系统接受了/没接受这个偏好"：
                    // App 没有公开 API 能确认实时路由，所以永远不写"正在用蓝牙"（见 TtsRouteText）。
                    MutedLine(TtsRouteText.line(ttsRoute), maxLines = 3)
                    Spacer(Modifier.height(6.dp))
                    DeviceChoiceRow(
                        label = "跟随系统",
                        selected = outputId == AppPrefs.FOLLOW_SYSTEM,
                        buttonLabel = "用它",
                        onClick = {
                            outputId = AppPrefs.FOLLOW_SYSTEM
                            AppPrefs.saveOutputDeviceId(context, outputId)
                        },
                    )
                    outputs.forEach { d ->
                        DeviceChoiceRow(
                            label = OutputDevices.labelOf(d),
                            selected = outputId == d.id,
                            buttonLabel = "优先用它",
                            onClick = {
                                outputId = d.id
                                AppPrefs.saveOutputDeviceId(context, outputId)
                            },
                        )
                    }
                    if (outputs.isEmpty()) {
                        MutedLine("现在看不到可用输出设备（系统会给一个默认的）", maxLines = 2)
                    }
                    // 如实说清限制：**这是界面上必须写的话，不是只写在注释里**
                    MutedLine(
                        "限制（如实）：① 「手机念」跟随系统 —— 系统 TTS 没有指定输出设备的接口，" +
                            "所以上面这些选择对它不起作用；② 「ECHO 念」能优先用你选的这支，" +
                            "但蓝牙/听筒的最终路由由系统定（写着「优先」，不是「一定用它」）；" +
                            "③ 选的那支不在了会回落系统默认，就在上面那行如实写着。",
                        maxLines = 6,
                    )
                    Spacer(Modifier.height(4.dp))
                    MutedLine("改完下次念生效（正在播的那句不会中途换设备）。", maxLines = 2)

                    Spacer(Modifier.height(6.dp))
                    HorizontalDivider(color = EchoPalette.Border)
                    Spacer(Modifier.height(6.dp))

                    // ---- 「手机当麦克风」每段多长 ----
                    MutedLine("**手机当麦克风**：每段 ${segmentSeconds} 秒（改完下次开录生效）", maxLines = 2)
                    Spacer(Modifier.height(6.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        for (sec in AppPrefs.SEGMENT_SECONDS_CHOICES) {
                            if (sec == segmentSeconds) {
                                Button(onClick = {}) { Text("$sec 秒") }
                            } else {
                                OutlinedButton(onClick = {
                                    // 单选的三档都走 AppPrefs 的夹取（判据只有一处），这里不自己判
                                    segmentSeconds = AppPrefs.normalizeSegmentSeconds(sec)
                                    AppPrefs.saveSegmentSeconds(context, segmentSeconds)
                                }) { Text("$sec 秒") }
                            }
                        }
                    }
                    MutedLine(
                        "一段 ≈ 秒数×32KB：长一点段数少，但一次失败要重传的就大；" +
                            "短一点重传轻，但请求多。默认 30 秒 ≈1 MB，走中继很稳。",
                        maxLines = 3,
                    )
                    Spacer(Modifier.height(4.dp))
                    MutedLine(
                        "发不出去的段会留在手机缓存里，下次上传前自动重试 —— " +
                            "录音界面上能看到「待重传 N 段」并按「立即重传」。",
                        maxLines = 3,
                    )

                    Spacer(Modifier.height(6.dp))
                    HorizontalDivider(color = EchoPalette.Border)
                    Spacer(Modifier.height(6.dp))

                    // ---- 离线录音的本地音频：有界保留 + **手动**清理入口（修②）----
                    // 上一版是"永不删"（安全，但一段 ≈1MB、一小时 ≈120MB，手机会被塞满）；
                    // 而"传完就删"又太狠（用户可能还想回头核对）。所以：**有界保留**，
                    // 且**绝不早删** —— 只有"已上传（对账通过 + 服务端收尾）且到期"才清。
                    Text("离线录音的本地音频", style = MaterialTheme.typography.bodyMedium)
                    Spacer(Modifier.height(4.dp))
                    MutedLine(
                        "现在占 " + usage.bytesText + "，共 " + usage.drafts + " 场" +
                            "（其中已上传 " + usage.uploadedDrafts + " 场" +
                            "，已到期可清 " + usage.expiredDrafts + " 场）",
                        maxLines = 2,
                    )
                    Spacer(Modifier.height(6.dp))
                    MutedLine("补传成功后，本地这份副本还留多久", maxLines = 1)
                    Spacer(Modifier.height(6.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        for (d in OfflineMeetingRetention.RETENTION_DAY_CHOICES) {
                            if (d == retentionDays) {
                                Button(onClick = {}) { Text(OfflineMeetingRetention.label(d)) }
                            } else {
                                OutlinedButton(onClick = {
                                    // 单选都走同一个夹取（判据只有一处，且是纯函数有用例）
                                    retentionDays = OfflineMeetingRetention.normalizeDays(d)
                                    AppPrefs.saveAudioRetentionDays(context, retentionDays)
                                    // 改完立刻按新档位重算"已到期可清几场"
                                    usage = MeetingMicService.localAudioUsage(context)
                                }) { Text(OfflineMeetingRetention.label(d)) }
                            }
                        }
                    }
                    Spacer(Modifier.height(4.dp))
                    MutedLine(
                        "「绝不早删」：**没传上去的一律不动**（哪怕已经放了很久）；" +
                            "只有对账通过、服务端也收尾过（已经开始转写）的，超过保留天数才会被清。" +
                            "选了「永不自动清理」就一份都不会自动清。",
                        maxLines = 4,
                    )
                    Spacer(Modifier.height(6.dp))
                    MutedLine(
                        "本地还留着这份副本，" + OfflineMeetingRetention.label(retentionDays) +
                            "；电脑上那份音频一直都在 —— 清本地副本不等于丢会议。",
                        maxLines = 3,
                    )
                    Spacer(Modifier.height(6.dp))
                    OutlinedButton(
                        enabled = usage.expiredDrafts > 0,
                        onClick = { confirmPurge = true },
                    ) { Text("删除已上传的本地音频（${usage.expiredDrafts} 场）") }
                    if (usage.expiredDrafts == 0) {
                        Spacer(Modifier.height(2.dp))
                        MutedLine(
                            if (usage.drafts == 0) "现在本地没有离线录音的音频。"
                            else "现在没有到期的（都还在保留期内，或还没传上去）。",
                            color = EchoPalette.Muted, maxLines = 2,
                        )
                    }
                    purgeNote.takeIf { it.isNotBlank() }?.let {
                        Spacer(Modifier.height(4.dp))
                        MutedLine(it, color = EchoPalette.Green, maxLines = 3)
                    }
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

    // ---- 删除前的确认（**删除是不可逆的**：必须让人先看出"要删的是哪几场"）----
    // 只用 plan 算出的那些（已上传 + 已到期）；**不提供**"全部立刻删"那种按钮 ——
    // 那等于把"绝不早删"这条铁律交给一次误触。
    if (confirmPurge) {
        val store = MeetingMicService.storeOf(context)
        val plan = remember(confirmPurge, usage) {
            OfflineMeetingRetention.plan(
                drafts = runCatching { store.withAudio() }.getOrDefault(emptyList()),
                nowMs = System.currentTimeMillis(),
                days = retentionDays,
                sizeOf = { OfflineMeetingRetention.audioBytes(store, it) },
            )
        }
        AlertDialog(
            onDismissRequest = { confirmPurge = false },
            title = { Text("删除 ${plan.count} 场已上传的本地音频？") },
            text = {
                Column {
                    MutedLine(
                        "共 ${RetentionPlan.humanBytes(plan.bytes)}。删的是这几场（电脑上那份不受影响，" +
                            "转写也已经开始了）：",
                        maxLines = 3,
                    )
                    Spacer(Modifier.height(4.dp))
                    plan.items.take(10).forEach { item ->
                        MutedLine("· " + item.label, maxLines = 2)
                    }
                    if (plan.count > 10) MutedLine("…还有 ${plan.count - 10} 场", maxLines = 1)
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val r = MeetingMicService.purgeUploadedNow(context)
                    purgeNote = "删除结果：" + r.note
                    usage = MeetingMicService.localAudioUsage(context)
                    confirmPurge = false
                }) { Text("删除") }
            },
            dismissButton = {
                TextButton(onClick = { confirmPurge = false }) { Text("取消") }
            },
        )
    }
}

/**
 * 一行设备选项：选中的标「正在用」，其余给一个按钮。
 *
 * [buttonLabel] 默认是输入麦那边的「用这支」；输出那边写「用它 / 优先用它」——
 * 因为输出只是**偏好**（`MediaPlayer.setPreferredDevice`），写"正在用"会造成"一定在用它"的错觉。
 */
@Composable
private fun DeviceChoiceRow(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
    buttonLabel: String = "用这支",
) {
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
            TextButton(onClick = onClick) { Text(buttonLabel) }
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
