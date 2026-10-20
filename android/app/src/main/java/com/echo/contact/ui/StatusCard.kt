package com.echo.contact.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
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
import com.echo.contact.data.dto.StatusComponent
import com.echo.contact.service.meetingOwnerCell
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.delay

/** 状态卡自动刷新的节拍：面板底部那条是 5 秒，这里对齐（只在卡片可见时跑）。 */
private const val STATUS_TICK_MS = 5_000L

// ---------------------------------------------------------------------------------------------
// 纯函数（JVM 可测）：这一张卡的三行内容全部由它们决定，界面只负责摆
// ---------------------------------------------------------------------------------------------

/**
 * 连接状态灯的三档。
 *
 * **只有三档**，因为 App 只看得见三件事（`MainViewModel.connected` 就是三值）：
 * 第一次探测还没回来 / 最近一次探测通了 / 最近一次探测没通。
 * `ConnLevel` 刻意不叫"重连中"——见 [homeConnLight] 的说明。
 */
internal enum class ConnLevel { OK, DOWN, PENDING }

/** 灯 + 一句话（第一行右侧那半句）。 */
internal data class ConnLight(val label: String, val level: ConnLevel)

/**
 * 主页第一行那个"状态灯 + 连接状态"（纯函数，`HomeTopCardTest` 钉住）。
 *
 * [connected] 是 `MainViewModel` **现成的**三值，没有为这张卡另造一条探活路：
 *  * `true`  = 最近一次 `/api/status` 成功 → 绿、已连接；
 *  * `false` = 最近一次没通（或 401）→ 红、未连接。**下面那半句"自动重试"是真的**：
 *    卡片可见时每 5 秒会再探一次（`MainViewModel.autoRefreshTick`），探通了这里就变回"已连接"；
 *  * `null`  = 进页面后第一次探测还没回来 → 黄、连接中。
 *
 * 为什么不写一个"重连中"：App **看不到**"服务端正在重连"这个过程，它只能看到
 * "上一次探通没通"。把"未连接"画成"重连中"就是在编一个没有数据支撑的状态
 * （而"未连接 · 自动重试"把 App 真在做的事说清楚了）。
 */
internal fun homeConnLight(connected: Boolean?): ConnLight = when (connected) {
    true -> ConnLight("已连接", ConnLevel.OK)
    false -> ConnLight("未连接 · 自动重试", ConnLevel.DOWN)
    null -> ConnLight("连接中…", ConnLevel.PENDING)
}

/**
 * 第二行那段配对地址。
 *
 * 空 = 还没配对：**如实**说"未配对"并指出在哪配对，而不是留一行空白
 * （空白会被当成"这条信息没有了"）。
 */
internal fun addressLine(baseUrl: String): String =
    baseUrl.trim().ifBlank { "未配对：还没有配对地址（去「设置 → 连接」）" }

/**
 * 第三行"9 个组件的概要"（例：`8/9 组件正常 · 1 未启用`）。
 *
 * 为什么分三档而不是只数"好/坏"：真机上 9 个组件里 `dsh=skipped`、`wake=disabled`
 * 是**常态**（那是"没启用"，不是"坏了"）。只数好坏会把一台健康的机器显示成
 * "7/9 正常"、看着像出了两个故障。所以"未启用"单独一档、**只在有的时候**才出现。
 *
 * 判据（`HomeTopCardTest`）：healthy/disabled/broken 三段加起来必须等于 total ——
 * 任何一个组件都不能被漏掉（漏掉就等于骗人说"都正常"）。
 */
internal data class ComponentSummary(
    val healthy: Int,
    val disabled: Int,
    val broken: Int,
    val total: Int,
) {
    /** 卡片第三行那句话（`total == 0` = 还没读到状态）。 */
    val label: String
        get() = if (total == 0) {
            "组件状态还读不到（连上 ECHO 就有了）"
        } else {
            buildString {
                append("$healthy/$total 组件正常")
                if (disabled > 0) append(" · $disabled 未启用")
                if (broken > 0) append(" · $broken 异常")
            }
        }
}

/** 组件状态 → 三档（与面板 `.dot` 同一套取值，见 `EchoPalette.statusColor`）。 */
private val HEALTHY_STATUSES = setOf("online", "active", "ready", "ok", "done")
private val DISABLED_STATUSES = setOf("disabled", "skipped", "idle", "paused")

internal fun componentSummary(components: List<StatusComponent>): ComponentSummary {
    var healthy = 0
    var disabled = 0
    var broken = 0
    components.forEach { c ->
        when (c.status.lowercase()) {
            in HEALTHY_STATUSES -> healthy++
            in DISABLED_STATUSES -> disabled++
            else -> broken++
        }
    }
    return ComponentSummary(
        healthy = healthy,
        disabled = disabled,
        broken = broken,
        total = components.size,
    )
}

// ---------------------------------------------------------------------------------------------
// 界面：**全 App 唯一**一处"应用名 + 连接信息"（用户 2026-10-18 要求合并）
// ---------------------------------------------------------------------------------------------

/**
 * 首页顶部**那唯一一张卡**：应用名 + 连接状态 + 配对地址 + 组件概要（点开是详情）。
 *
 * ## 为什么要把原来两处合成一处
 *
 * 用户原话：*"主页上端的（应用名 + 配对地址）那个可以跟下面的服务状态合并"*。
 * 合并前，同一个事实在首页出现了两次 —— 顶部那一行（应用名 + 配对地址）和
 * 下面那张「服务状态」卡（服务/智能体/语音合成/会议/回顾 + 9 个组件）。
 * 两处都画"连没连上"，只是**画法不同**，于是必然有一天一处说"在线"、另一处说"连不上"
 * （本仓库在"同一个事实两处显示"上栽过好几次）。
 *
 * ## 信息一个都没少（只是**收进这张卡**）
 *
 *  * 第一行：应用名 + 状态灯 + 连接状态（"连上/未连/连接中"，数据是现成的 `connected`）；
 *  * 第二行：配对地址（小字、可截断；**点一下复制**，复制不到也能一眼看全）；
 *  * 第三行：9 个组件的概要 `8/9 组件正常`；**点一下展开** —— 里面还是原来那 5 格
 *    （服务/智能体/语音合成/会议/回顾）+ 9 个组件的逐条明细 + 版本 + 已运行时长 +
 *    启动提示 + 刷新按钮。组件清单的渲染**复用**原来那张卡的代码，没有第二份。
 *  * 连不上时那句排查提示（"同一 Wi-Fi？防火墙？"）仍然**常驻**（不藏在展开里）：
 *    那是用户最需要立刻看到的一句。
 */
@Composable
fun HomeTopCard(vm: MainViewModel) {
    val status by vm.status.collectAsState()
    val connected by vm.connected.collectAsState()
    val statusError by vm.statusError.collectAsState()
    val baseUrl by vm.baseUrl.collectAsState()
    val review by vm.review.collectAsState()
    val reviewSummary by vm.reviewSummary.collectAsState()
    var expanded by remember { mutableStateOf(false) }
    var copied by remember { mutableStateOf(false) }

    val context = LocalContext.current

    // 「会议」那一格也要认**归属**（2026-10-20 真机：手机说"电脑在录音"，其实是它自己那场在补传）。
    // `meeting.active` 只说明"ECHO 那儿有一场在录"，不说明是谁的：判据只有一处
    // （`service/MeetingOwnership.kt`，与会议页共用），这里只读结论、不自己判。
    val micState by com.echo.contact.service.MeetingMicService.state.collectAsState()
    val localDrafts by com.echo.contact.service.MeetingMicService.localDrafts.collectAsState()

    // 只在卡片可见时按节拍取状态（离开组合这个 LaunchedEffect 就取消）
    LaunchedEffect(Unit) {
        while (true) {
            delay(STATUS_TICK_MS)
            vm.autoRefreshTick()
        }
    }

    val light = homeConnLight(connected)
    val lightColor = when (light.level) {
        ConnLevel.OK -> EchoPalette.Green
        ConnLevel.DOWN -> EchoPalette.Red
        ConnLevel.PENDING -> EchoPalette.Yellow
    }
    val summary = componentSummary(status?.components.orEmpty())
    // 「经中继打开 DSH 标准版网页」这个入口的**唯一**读取点（判据在 `DshWebEntryTest`）：
    // URL 只来自服务端的 `dshWebUrl`，App 不拼、不写死；没给就置灰并如实说原因。
    val dshWeb = dshWebEntryOf(status)

    val server = status?.components?.firstOrNull { it.name == "server" }
    val tts = status?.components?.firstOrNull { it.name == "tts" }
    val agent = status?.agent
    val meeting = status?.meeting
    val reviewState = review
    val reviewLabel = when {
        reviewState?.running == true -> "进行中"
        reviewState?.enabled == false -> "未启用"
        reviewState?.ready == false -> "未就绪"
        (reviewSummary?.todayTurns ?: 0) > 0 -> "今天已回顾 ${reviewSummary?.todayTurns} 轮"
        else -> "今天还没回顾"
    }
    val reviewColor = when {
        reviewState?.running == true -> EchoPalette.Purple
        reviewState?.enabled == false -> EchoPalette.Muted
        reviewState?.ready == false -> EchoPalette.Yellow
        (reviewSummary?.todayTurns ?: 0) > 0 -> EchoPalette.Green
        else -> EchoPalette.Muted
    }

    EchoCard(contentPadding = HomeLayoutSpec.TOP_CARD_PADDING_DP.dp) {
        Column {
            // ---- ① 应用名 + 状态灯 + 连接状态（全 App 唯一一处）----
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                StatusDot(lightColor, size = 9)
                Spacer(Modifier.size(7.dp))
                Text(
                    APP_TITLE,
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier.weight(1f),
                )
                Text(
                    light.label,
                    style = MaterialTheme.typography.labelLarge,
                    color = lightColor,
                )
            }

            // ---- ② 配对地址（小字、可截断；点一下复制）----
            // 2026-10-18 傍晚：三行之间的缝收紧到 3/4dp —— 这张卡要在 360×640 上
            // 和下面三张功能卡一起装进一屏（预算见 `HomeLayoutSpec`），
            // 省下来的每一 dp 都是"放得下"与"要滚"的差别。
            Spacer(Modifier.height(3.dp))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable {
                        // 点一下复制：地址通常很长（中继地址尤其），而用户要把它抄到别处。
                        // 不新开一个"看完整地址"的页面 —— 复制是这条信息唯一的用途。
                        val ok = copyToClipboard(context, addressLine(baseUrl))
                        copied = ok
                    },
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    addressLine(baseUrl),
                    style = MaterialTheme.typography.labelSmall,
                    color = EchoPalette.Muted,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.size(6.dp))
                Text(
                    if (copied) "已复制" else "复制",
                    style = MaterialTheme.typography.labelSmall,
                    color = EchoPalette.Accent,
                )
            }

            // ---- ③ 9 个组件的概要（紧凑；点开看详情）----
            Spacer(Modifier.height(4.dp))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { expanded = !expanded },
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    summary.label,
                    style = MaterialTheme.typography.labelMedium,
                    color = when {
                        summary.total == 0 -> EchoPalette.Muted
                        summary.broken > 0 -> EchoPalette.Red
                        else -> EchoPalette.Text
                    },
                    modifier = Modifier.weight(1f),
                )
                Icon(
                    if (expanded) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown,
                    contentDescription = if (expanded) "收起组件详情" else "展开组件详情",
                    tint = EchoPalette.Muted,
                    modifier = Modifier.size(20.dp),
                )
            }

            // 连不上时必须**马上**能看见一句排查提示（不藏在"展开"里）
            if (connected == false) {
                Spacer(Modifier.height(6.dp))
                MutedLine(
                    (statusError ?: "连不上 ECHO") +
                        "　·　检查：同一 Wi-Fi？防火墙放行 8970？或者在电脑上开「允许手机经后端中转」" +
                        "、把中继地址填到配对页。",
                    color = EchoPalette.Red,
                    maxLines = 3,
                )
            }

            if (expanded) {
                Spacer(Modifier.height(10.dp))
                HorizontalDivider(color = EchoPalette.Border)
                Spacer(Modifier.height(8.dp))

                // ①' 2026-10-18 夜里新增的入口：**经中继**打开 DSH 标准版网页。
                // 2026-10-18 深夜改成 **App 内 WebView**：中继要求 `Authorization` 头，
                // 系统浏览器不会带（点开就是 401 JSON），补头只能由 App 自己来。
                //
                // 为什么放在**展开区**（而不是常驻一行）：360×640 那一档的余量只有 22dp
                // （`HomeLayoutSpec` 的预算：内容 448 ≤ 可用 470），常驻一行至少 18+4dp，
                // 立刻就把"一屏放得下"这条挤掉。展开区是用户主动点的，**不占**一屏预算
                // —— 判据 `DshWebEntryTest` 用源码位置钉住它确实在 `if (expanded)` 里面。
                DshWebEntryRow(dshWeb, context)

                Spacer(Modifier.height(8.dp))
                HorizontalDivider(color = EchoPalette.Border)
                Spacer(Modifier.height(8.dp))

                // 第一排：服务 / 智能体 / 语音合成（与面板底部那条同一口径）
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    StatusCell(
                        modifier = Modifier.weight(1f),
                        name = "服务",
                        value = server?.let { shortStatus(it.status) } ?: "—",
                        color = server?.let { EchoPalette.statusColor(it.status) } ?: EchoPalette.Muted,
                    )
                    StatusCell(
                        modifier = Modifier.weight(1f),
                        name = "智能体",
                        value = agent?.shortName?.takeIf { it.isNotBlank() }
                            ?: agent?.displayName?.takeIf { it.isNotBlank() } ?: "—",
                        color = if (agent?.online == true) EchoPalette.Green else EchoPalette.Muted,
                    )
                    StatusCell(
                        modifier = Modifier.weight(1f),
                        name = "语音合成",
                        // 短值取服务端那句诚实 detail 的第一段（"自动（在线）"/"自动（离线）"）
                        value = tts?.detail?.substringBefore(" · ")?.takeIf { it.isNotBlank() }
                            ?: tts?.let { shortStatus(it.status) } ?: "—",
                        color = tts?.let { EchoPalette.statusColor(it.status) } ?: EchoPalette.Muted,
                    )
                }
                Spacer(Modifier.height(8.dp))
                // 第二排：会议 / 回顾
                Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    StatusCell(
                        modifier = Modifier.weight(1f),
                        name = "会议",
                        // 状态词来自**同一处**归属判据（`meetingOwnerCell`）：
                        // 我那场已经录完、还在传 → 「上传中」；不是我那场才说「录音中」。
                        value = when {
                            !meeting?.error.isNullOrBlank() -> "出错"
                            else -> meetingOwnerCell(
                                com.echo.contact.service.meetingOwnership(
                                    active = meeting?.active == true,
                                    source = meeting?.source.orEmpty(),
                                    clientId = meeting?.clientId.orEmpty(),
                                    local = localDrafts,
                                    localRecording = micState.active,
                                ),
                            )
                        },
                        color = when {
                            !meeting?.error.isNullOrBlank() -> EchoPalette.Red
                            meeting?.active == true -> EchoPalette.Purple
                            else -> EchoPalette.Muted
                        },
                    )
                    StatusCell(
                        modifier = Modifier.weight(1f),
                        name = "回顾",
                        value = reviewLabel,
                        color = reviewColor,
                    )
                    Spacer(Modifier.weight(1f))
                }

                Spacer(Modifier.height(10.dp))
                HorizontalDivider(color = EchoPalette.Border)
                Spacer(Modifier.height(6.dp))
                // 版本这一行原来在卡标题右侧（"ECHO 1.x"）—— 标题让位给应用名了，
                // 信息搬到这里，**不删**：用户要能对上面板里的版本号。
                MutedLine("ECHO 版本 ${status?.version ?: "（读不到）"}", maxLines = 1)
                if (status == null) {
                    Spacer(Modifier.height(4.dp))
                    MutedLine("正在读取状态…", maxLines = 1)
                } else {
                    if (status!!.busy) {
                        Spacer(Modifier.height(4.dp))
                        MutedLine(
                            "正在执行：" + listOfNotNull(
                                status!!.busyOwner?.takeIf { it.isNotBlank() },
                                status!!.busyPhase?.takeIf { it.isNotBlank() },
                            ).joinToString(" · "),
                            color = EchoPalette.Purple,
                            maxLines = 1,
                        )
                    }
                    Spacer(Modifier.height(6.dp))
                    status!!.components.forEach { c ->
                        Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
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
                    status!!.startupNotes.forEach { note ->
                        Spacer(Modifier.height(4.dp))
                        MutedLine(note, color = EchoPalette.Yellow)
                    }
                    if (status!!.uptimeText().isNotBlank()) {
                        Spacer(Modifier.height(6.dp))
                        MutedLine("电脑上已运行 ${status!!.uptimeText()}", maxLines = 1)
                    }
                }
                Row(modifier = Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = { vm.refresh() }) { Text("刷新") }
                    Spacer(Modifier.weight(1f))
                }
            }
        }
    }
}

/** 复制配对地址；返回"到底复制没复制上"（界面据此说"已复制"，绝不假报）。 */
private fun copyToClipboard(context: Context, text: String): Boolean {
    val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return false
    return runCatching { cm.setPrimaryClip(ClipData.newPlainText("ECHO 配对地址", text)) }.isSuccess
}

/**
 * 入口那一行：**经中继**在 **App 内**打开 DSH 标准版网页（2026-10-18 深夜改）。
 *
 * ## 三条硬约束（都来自用户 2026-10-18 的要求）
 *
 *  1. **URL 不写死**：地址只来自服务端（`dshWebUrl`），拼法不由 App 决定 —— 见 [DshWebEntrySpec]；
 *  2. **没有地址就置灰 + 说清楚**：一句话写明"服务端未提供中继地址"并点名期望字段，
 *     绝不写"敬请期待"，也绝不拿 `relayUrl`（那是 API 中继根）凑数；
 *  3. **打开失败也要说**：这一版"没打开"只剩一种情况（入口被置灰）—— 真的点下去之后，
 *     是 App 内那个 WebView 在负责"加载失败如实说明原因"（`DshWebScreen` 的失败卡），
 *     不再有"手机上找不到浏览器"那种黑箱。
 *
 * ## 为什么从系统浏览器改成 App 内 WebView
 *
 * 中继对 `api/` 下的每条接口都要求 `Authorization` 头，而**系统浏览器不会带手机令牌** ——
 * 那个 `Intent.ACTION_VIEW` 点开就是一段 401 JSON（用户看到的是"页面打不开"）。
 * 现在交给 `DshWebOpen.put(url)`，由 `HomeScreen` 画全屏的 `DshWebScreen`，
 * 拦截器只对**同源**请求补头（规则在 `DshWebRequestPolicy`）。
 *
 * 「到底把哪个 URL 交出去了」这件事在 [openDshWeb]（纯函数 + 可注入 opener）里仍有判据，
 * 这里只负责把"交给 App 内嵌页"包成 opener。
 */
@Suppress("UNUSED_PARAMETER")
@Composable
private fun DshWebEntryRow(entry: DshWebEntry, context: Context) {
    // context 现在只有"可空处理"这一层意思（入口本身不再自己造 Intent）——
    // 保留它是因为展开区里这一行与上面那条"复制配对地址"共用同一个 context，
    // 将来要在这里加"复制这条地址"时不必再改签名（判据 `DshWebEntryTest` 钉着这个调用形状）。
    var note by remember { mutableStateOf("") }

    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(
                DshWebEntrySpec.TITLE,
                style = MaterialTheme.typography.bodyMedium,
                color = if (entry.enabled) EchoPalette.Text else EchoPalette.Muted,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            MutedLine(
                // 可点：说明"经中继 + 只读 + 在 App 内"；不可点：说明**为什么**（含期望字段名）
                if (entry.enabled) {
                    DshWebEntrySpec.SUBTITLE + (note.takeIf { it.isNotBlank() }?.let { "　·　$it" } ?: "")
                } else {
                    entry.problem
                },
                color = if (entry.enabled) EchoPalette.Muted else EchoPalette.Yellow,
                maxLines = 2,
            )
        }
        Spacer(Modifier.size(6.dp))
        TextButton(
            enabled = entry.enabled,
            onClick = {
                // opener 是可注入的（判据用假 opener 断言"交出去的就是服务端那串"）；
                // 真实这条路 = 交给 App 内嵌页（不离开 App，令牌由拦截器补在请求头上）。
                val ok = openDshWeb(entry) { url ->
                    DshWebOpen.put(url)
                    true
                }
                note = if (ok) "" else "没打开：这个入口现在没有可用地址"
            },
        ) {
            Text(DshWebEntrySpec.OPEN_LABEL, style = MaterialTheme.typography.labelLarge)
        }
    }
}

/** 一格：状态灯 + 名字 + 短值（照面板底部那条的三格）。 */
@Composable
private fun StatusCell(
    modifier: Modifier,
    name: String,
    value: String,
    color: androidx.compose.ui.graphics.Color,
) {
    Column(modifier = modifier) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusDot(color, size = 7)
            Spacer(Modifier.size(5.dp))
            MutedLine(name, maxLines = 1)
        }
        Text(
            value,
            style = MaterialTheme.typography.labelLarge,
            color = color,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}
