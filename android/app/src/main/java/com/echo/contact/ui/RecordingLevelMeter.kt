package com.echo.contact.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.dp
import com.echo.contact.ui.theme.EchoPalette
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sin

/**
 * # 全屏录音页的电平（2026-10-18 深夜**重做**；用户原话："电平效果感觉还是不好看，找找新设计样式"）
 *
 * 旧版是 17 根柱子拼的"声浪"：柱子高、颜色三档硬切、每 ~100ms 跳一格 —— 看着像音量条，
 * 看不出"声音的形状"。这一版换成一条**从中心向两边镜像**的电平：
 *
 * ```
 *   ┃  ╭────────────────────╮  ┃    ← 峰值线（细亮线）停在峰值处，0.8s 后才缓慢回落
 *      │  ████████████████  │       ← 等厚胶囊：以中心为轴、两端圆角
 *   ┃  ╰────────────────────╯  ┃      渐变：中心蓝 → 两端紫 → 端头红（亮度随电平升）
 * ```
 *
 * ## 四条约束（都是点名的，别随手改）
 *
 *  1. **平滑**：麦克风每 ~100ms 才报一个 `level`，直接画就是"一格一格跳"。
 *     所以两级：先按**样本**抑刺（[smoothLevel]：一个单点尖刺不足以把整条电平拉满），
 *     再交给**帧级包络**（[LevelEnvelope]：攻击 0.35、释放 0.12，按**帧间时间**归一）——
 *     看着有弹性、但不乱抖。**低通平滑是为了"看着稳"**，不是为了"数据准"。
 *  2. **峰值保持**（[PeakHold]）：记住峰值，**停留 0.8s** 再按每秒 0.3 缓慢回落。
 *     **峰值保持是为了"看得出峰值"**：只有实时的条子，说话那一下一响就退回去了，
 *     用户根本看不清刚才有多响。
 *  3. **安静时呼吸**：暂停或极低电平时**只改亮度**（0.34~0.66 的慢脉动，周期 2.6s），
 *     长度一动不动 —— 长度一跳就变成"抖动"，那是另一种难看。暂停时整条用"已暂停"的配色
 *     （[EchoPalette.Yellow]，与标题那行"已暂停 · 时长在累计"同一个色），不会跟录音中混。
 *  4. **别让美化挤掉功能**：这条电平**高度固定 64dp**（[RECORDING_LEVEL_HEIGHT_DP]），
 *     整段占位由它推导（[RECORDING_LEVEL_BAND_DP]），页面总高度**只减不增**
 *     （旧版电平占 180dp，这一版 112dp；见 `MeetingRecordingScreen` 里的
 *     `RECORDING_BASE_AREA_DP`）。大计时 / 本段进度 / 一行状态 / 两个 56dp 按钮一个都不动。
 *
 * ## 性能：为什么是"单一 draw 状态 + drawWithCache"
 *
 * 电平每帧都在变。如果它进 composition，整页每帧重组一次（大计时、状态行、按钮全跟着重算）。
 * 所以帧循环（[withFrameNanos]）只把数值写进 [MeterFrame] 的几格 `mutableStateOf`，
 * 而它们**只**在 `onDrawBehind` 里被读 —— Compose 只会把**绘制阶段**标脏，重组一次都不会发生。
 * 三个 brush（主条渐变 / 暂停色 / 底槽淡出）在 `drawWithCache` 里按尺寸建一次；
 * 每帧的绘制只有**两个圆角矩形 + 两根线**，不 new shader、也不 new 路径。
 */

// ---------------------------------------------------------------------------
// 尺寸（"尺寸只有一处来源"：布局预算与用例都引这几个常量，别在 Modifier 里另写数字）
// ---------------------------------------------------------------------------

/**
 * 电平**自己**的高度（dp）。用户给的区间是 56~72，取 **64**：
 * 比两个 56dp 的主按钮略高一点点（视觉上像"一条带子"而不是"另一排按钮"），
 * 又在 72 以内，小屏上不至于把按钮顶下去。
 */
internal const val RECORDING_LEVEL_HEIGHT_DP = 64

/** 电平上下各留的呼吸（dp）。整段占位 = 64 + 2×24 = 112dp（旧版那一段是 180dp）。 */
internal const val RECORDING_LEVEL_PADDING_DP = 24

/** 电平这一段在页面里**占的高度**（dp）。由上面两个常量推导，改高度时这里自动跟着变。 */
internal const val RECORDING_LEVEL_BAND_DP = RECORDING_LEVEL_HEIGHT_DP + 2 * RECORDING_LEVEL_PADDING_DP

// ---------------------------------------------------------------------------
// 平滑 / 峰值 / 呼吸的参数（理由都写在函数上）
// ---------------------------------------------------------------------------

/** 一个电平样本的节拍（ms）。麦克风那侧大约 100ms 报一次电平。 */
internal const val LEVEL_TICK_MS = 100f

/** 同上，纳秒版（`withFrameNanos` 给的是纳秒；1e8ns = 100ms）。 */
private const val LEVEL_TICK_NS = 100_000_000L

/**
 * 帧级包络的**攻击**系数（每个 [LEVEL_TICK_MS] 走掉剩余差距的 35%）。
 *
 * 取 0.35（给定区间 0.25~0.35 的上沿）：这一层是"视觉弹性"，
 * 太低（0.25）会让人感觉"电平跟不上说话"，太高就退回"一格一格跳"。
 */
internal const val LEVEL_ATTACK = 0.35f

/**
 * 帧级包络的**释放**系数（每个 [LEVEL_TICK_MS] 只落 12%）。
 *
 * 取 0.12（区间 0.08~0.15 的中上）：声音退下去时人眼接受得很慢（真实声浪也有余响），
 * 所以释放必须比攻击慢一截 —— 这条是"看着不急"的关键。
 */
internal const val LEVEL_RELEASE = 0.12f

/**
 * **样本级**抑刺的攻击系数。
 *
 * 保留旧的 0.65：这一层只负责"一个坏样本别把整条电平拉满"（碰一下麦、敲一下键盘），
 * 它不需要慢 —— 慢的那一层是 [LevelEnvelope]。
 */
internal const val LEVEL_SAMPLE_ATTACK = 0.65f

/**
 * 样本级的释放系数：**从旧的 0.25 调到 0.15**。
 *
 * 为什么改：旧值（一个样本落 25%）是"啪一下掉下去"的手感，与新版的"慢释放"冲突。
 * 0.15 仍在给定的慢释放区间（0.08~0.15）里，且它有一个可测的性质：
 * 从 1 落回 0.2 需要 **10 个样本**（0.85^10 ≈ 0.197），比"8 帧"更慢 —— 见用例。
 */
internal const val LEVEL_SAMPLE_RELEASE = 0.15f

/** 峰值**停留**多久才开始回落（ms）。用户要的 ~0.8s。 */
internal const val PEAK_HOLD_MS = 800L

/** 峰值回落速度（每秒掉多少，0..1 的空间）。0.3/s → 从满格退到 0 大约 3.3s，够慢。 */
internal const val PEAK_FALL_PER_SECOND = 0.30f

/** 呼吸的周期（ms）。2.6s 一次，慢到"像在呼吸"，快于 2s 就开始像"闪"。 */
internal const val BREATH_PERIOD_MS = 2600

/** 呼吸时的亮度下限 / 上限（不透明度）。 */
internal const val BREATH_MIN_ALPHA = 0.34f
internal const val BREATH_MAX_ALPHA = 0.66f

/** 低于这个电平就当"安静"（开始呼吸）。0.03 以下肉眼已经看不出条子的长度差别了。 */
internal const val LEVEL_QUIET = 0.03f

/**
 * 刻度片数（奇数：中间一片、两边对称）。用它给底槽做"中间亮、两端淡出"的淡出曲线
 * （[barWeight] 的钟形），也是这条电平的"形状语言"。
 */
internal const val LEVEL_BARS = 17

/**
 * 条子的厚度占整段高度的比例（0.62）。
 *
 * 为什么不铺满 64dp：铺满就是一根"粗壮的分段进度条"，上下没有留白、峰值线也没地方站。
 * 0.62 之后条子约 40dp 高、峰值线略高过它 —— 两样都看得清。
 * （真机前的几何 mock 对比过"等厚胶囊"与"中间厚两端薄"两种：后者在小屏上有台阶感，
 * 而且低电平时像一颗土豆，所以定**等厚胶囊**。）
 */
private const val RIBBON_HEIGHT_FRACTION = 0.62f

/** 静音时也留这么一小截（否则"呼吸"没有东西可呼吸 —— 一条看不见的线是不会脉动的）。 */
private const val RIBBON_MIN_FRACTION = 0.05f

/**
 * 电平 → 视觉比例的**压缩指数**。
 *
 * 为什么不是线性：麦克风报上来的 RMS 本来就很低（说话时通常 0.1~0.3），线性映射下
 * 条子永远只有一小截 —— "按了半天没反应"也是"不好看"的一部分。
 * 0.65 是折中：小的也动得起来（0.15 → 0.29），大的还拉得开（0.8 → 0.87），不至于人人满格。
 */
private const val LEVEL_GAMMA = 0.65f

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

/**
 * 对称镜像电平（**唯一的**绘制入口）。参数全是"事实"，不含任何动画控制：
 *
 * @param rawLevel 麦克风报上来的原始电平（0..1）；平滑在这里面做，调用方别自己再平滑一遍。
 * @param paused 暂停中（= 写空白音频）。暂停时整条换成"已暂停"的配色并进入呼吸。
 * @param sessionKey 这一场录音的标识（`MicState.startedAt`）。**换值 = 重开一段 → 清零峰值**。
 */
@Composable
internal fun RecordingLevelMeter(
    rawLevel: Float,
    paused: Boolean,
    sessionKey: String,
    modifier: Modifier = Modifier,
) {
    // 帧循环里要读**最新**的输入，但不许因为它重启（重启一次就会丢掉包络/峰值的历史）
    val levelNow = rememberUpdatedState(rawLevel)
    val pausedNow = rememberUpdatedState(paused)

    val frame = remember { MeterFrame() }
    val envelope = remember { LevelEnvelope() }
    val peakHold = remember { PeakHold() }

    // 重开一段时清零：上一场的峰值带过来，就是一条"这一场根本没响过"的假峰值。
    LaunchedEffect(sessionKey) {
        envelope.reset()
        peakHold.reset()
        frame.level = 0f
        frame.peak = 0f
    }

    // 帧循环：**只写 draw 状态**（不写任何被 composition 读的东西），所以不会触发整页重组。
    LaunchedEffect(Unit) {
        var lastNs = 0L
        var lastGateNs = 0L
        var gated = 0f
        var wasPaused = pausedNow.value
        while (true) {
            val ns = withFrameNanos { it }
            // 首帧没有 dt（=0 → 一步都不走），掉帧时也别让包络一次跳很远（上限 80ms）
            val dtMs = if (lastNs == 0L) 0f else (ns - lastNs) / 1_000_000f
            lastNs = ns

            val p = pausedNow.value
            // 一按静音，峰值就不该还挂在那儿（用户看到的是"这段是空白"）
            if (p && !wasPaused) peakHold.reset()
            wasPaused = p

            val sample = if (p) 0f else levelNow.value.coerceIn(0f, 1f)
            // 样本级抑刺**按 100ms 的节拍**跑（不是按"值变了"判断）：
            // 麦克风报上来的电平常常一连几拍都是同一个值（安静时就是 0），
            // 若用"值变了"当触发条件，那几拍就不会再收敛，条子会卡在半路。
            if (ns - lastGateNs >= LEVEL_TICK_NS) {
                gated = smoothLevel(gated, sample)
                lastGateNs = ns
            }
            val level = envelope.step(gated, dtMs.coerceIn(0f, 80f))
            frame.level = level
            frame.peak = peakHold.update(level, ns / 1_000_000L)
            frame.paused = p
            frame.nowMs = ns / 1_000_000L
        }
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(RECORDING_LEVEL_HEIGHT_DP.dp)
            .drawWithCache {
                // 渐变按**屏幕坐标**铺满整条（不是按当前条子的包围盒）：
                // 这样"中心永远是蓝、端头永远是红"，颜色才表示"走到量程的哪一段"。
                val brush = Brush.horizontalGradient(
                    colors = listOf(
                        EchoPalette.Red,
                        EchoPalette.Purple,
                        EchoPalette.Accent,
                        EchoPalette.Purple,
                        EchoPalette.Red,
                    ),
                    startX = 0f,
                    endX = size.width,
                )
                val pausedBrush = SolidColor(EchoPalette.Yellow)
                // 底槽两端淡出：用 `barWeight` 那条钟形曲线当**透明度**曲线（中间亮、两端淡）。
                // 这条曲线原来是"柱子中间高两边低"，现在给底槽的淡出用 —— 判据还是同一条。
                val trackBrush = Brush.horizontalGradient(
                    colorStops = Array(LEVEL_BARS) { i ->
                        val t = i / (LEVEL_BARS - 1f)
                        t to EchoPalette.Border.copy(
                            alpha = 0.12f + 0.34f * barWeight(i, LEVEL_BARS),
                        )
                    },
                    startX = 0f,
                    endX = size.width,
                )
                onDrawBehind {
                    drawMeterLevel(
                        brush = brush,
                        pausedBrush = pausedBrush,
                        trackBrush = trackBrush,
                        level = frame.level,
                        peak = frame.peak,
                        paused = frame.paused,
                        nowMs = frame.nowMs,
                    )
                }
            },
    )
}

/**
 * 帧循环写、draw 读的四格状态（电平 / 峰值 / 是否暂停 / 当前时刻）。
 *
 * 为什么不是 `Animatable`：这几格是**同一帧算出的一组数**（电平、峰值、时刻），
 * 分成几个动画对象反而会对不齐（峰值可能落在上一帧的电平上）。
 * 为什么用 `mutableStateOf` 而不是普通 `Float`：**变了要触发重绘**——但只有绘制阶段观察到它。
 */
private class MeterFrame {
    var level by mutableStateOf(0f)
    var peak by mutableStateOf(0f)
    var paused by mutableStateOf(false)
    var nowMs by mutableStateOf(0L)
}

// ---------------------------------------------------------------------------
// 绘制（只有这里碰 Canvas / 颜色）
// ---------------------------------------------------------------------------

private fun DrawScope.drawMeterLevel(
    brush: Brush,
    pausedBrush: Brush,
    trackBrush: Brush,
    level: Float,
    peak: Float,
    paused: Boolean,
    nowMs: Long,
) {
    if (size.width <= 0f || size.height <= 0f) return

    val cx = size.width / 2f
    val cy = size.height / 2f
    // 条子只占中间 62%：上下留白既是"呼吸"，也是峰值线站的地方
    val halfThick = size.height / 2f * RIBBON_HEIGHT_FRACTION
    // 胶囊的圆头半径 = 半厚（等厚），所以"从中心到端头"的距离直接就是 halfLen
    val capHalf = halfThick
    // 两端各留 2dp：条子贴到屏幕边上会显得"被裁掉了"
    val maxHalf = (size.width / 2f - 2.dp.toPx()).coerceAtLeast(capHalf)

    val l = level.coerceIn(0f, 1f)
    val p = peak.coerceIn(0f, 1f)
    val breath = breathPulse(nowMs)
    val alpha = levelAlpha(l, paused, breath)

    // 底槽：一条很淡的细线（中间亮、两端淡出）。它的作用是"让人知道量程有多长"，
    // 也是峰值线落地时的参照物；两端淡出是为了不让这条直尺把视线从条子上拉走。
    val trackH = 1.5f.dp.toPx()
    drawRoundRect(
        brush = trackBrush,
        topLeft = Offset(cx - maxHalf, cy - trackH / 2f),
        size = Size(maxHalf * 2f, trackH),
        cornerRadius = CornerRadius(trackH / 2f),
    )

    // 主条：**以中心为轴**的等厚胶囊（左右各 halfLen），长度随电平、亮度随电平
    val halfLen = levelHalfLengthPx(maxHalf, capHalf, l)
    drawRoundRect(
        brush = if (paused) pausedBrush else brush,
        topLeft = Offset(cx - halfLen, cy - halfThick),
        size = Size(halfLen * 2f, halfThick * 2f),
        cornerRadius = CornerRadius(halfThick),
        alpha = alpha,
    )

    // 峰值：一根细亮线（左右各一根，对称）。只有比当前电平**明显更远**时才画 ——
    // 否则它就压在条子的圆头上，看不出是"另一条线"。
    // 线色走 `barColor`：峰值有多"热"（蓝=弱、红=强）与底槽/端头是同一套色。
    if (!paused && p > l + 0.01f) {
        val peakHalf = levelHalfLengthPx(maxHalf, capHalf, p)
        val lineHalf = size.height * 0.26f
        val stroke = 2f.dp.toPx()
        val peakColor = barColor(p, 1f)
        for (dir in floatArrayOf(1f, -1f)) {
            val x = cx + dir * peakHalf
            drawLine(
                color = peakColor,
                start = Offset(x, cy - lineHalf),
                end = Offset(x, cy + lineHalf),
                strokeWidth = stroke,
                cap = StrokeCap.Round,
                alpha = 0.95f,
            )
        }
    }
}

// ---------------------------------------------------------------------------
// 纯函数（用例如 `RecordingLevelMeterTest` 钉着；改参数先看那里）
// ---------------------------------------------------------------------------

/**
 * **样本级**平滑：攻击快、释放慢（一次跳变只走一步的 65% / 15%）。
 *
 * 为什么不能直接用采样值：麦克风每 ~100ms 一个瞬时值，偶发的单点尖刺（碰麦、敲键盘）
 * 会把整条电平一下拉满 —— 这一层就是"一个坏样本不许决定整段画面"。
 * 为什么释放是 0.15 而不是旧的 0.25：旧的落得太快（一个样本掉四分之一），
 * 与新版的"慢释放"手感冲突；0.15 让"退下去"有余响，且仍是可测的慢（见用例）。
 */
internal fun smoothLevel(prev: Float, target: Float): Float {
    val p = prev.coerceIn(0f, 1f)
    val t = target.coerceIn(0f, 1f)
    val k = if (t >= p) LEVEL_SAMPLE_ATTACK else LEVEL_SAMPLE_RELEASE
    return (p + (t - p) * k).coerceIn(0f, 1f)
}

/**
 * **帧级**包络跟随（状态类，可测）：攻击 [LEVEL_ATTACK]、释放 [LEVEL_RELEASE]。
 *
 * 为什么要按 `dtMs` 归一：`withFrameNanos` 给的帧间隔不是常数（掉帧、90/120Hz 屏）。
 * 若每个绘制帧都用固定系数，120Hz 手机上电平会"跑得比声音快"、掉帧时又变成"一格一格跳"。
 * 所以系数按"一个 [LEVEL_TICK_MS] 节拍"标定，帧间用 `1-(1-k)^(dt/节拍)` 折算 ——
 * 这样不同刷新率下的**时间响应**一致。
 */
internal class LevelEnvelope(
    private val attack: Float = LEVEL_ATTACK,
    private val release: Float = LEVEL_RELEASE,
) {
    /** 当前显示值（0..1）。 */
    var value: Float = 0f
        private set

    fun reset() {
        value = 0f
    }

    /** 朝 [target] 走一步（[dtMs] 是距上一帧的毫秒数）。返回走完后的值。 */
    fun step(target: Float, dtMs: Float = LEVEL_TICK_MS): Float {
        val t = target.coerceIn(0f, 1f)
        val k = if (t >= value) attack else release
        val ticks = (dtMs / LEVEL_TICK_MS).coerceIn(0f, 2f)
        val alpha = if (ticks <= 0f) 0f else 1f - (1f - k).pow(ticks)
        value = (value + (t - value) * alpha).coerceIn(0f, 1f)
        return value
    }
}

/**
 * 峰值保持（状态类，可测）：新峰立刻跟上，之后**停留 [holdMs]**，再按
 * [fallPerSecond] 缓慢回落。
 *
 * 为什么"先持有再回落"而不是直接跟着电平走：峰值一闪就没了，看不到"刚才那一下有多响"；
 * 为什么回落是线性的、而不是指数：线性回落有确定的"每 0.8s 掉多少"，屏幕上是一条匀速下沉的线，
 * 不会出现"掉到一半忽然停住"那种黏滞感。
 */
internal class PeakHold(
    private val holdMs: Long = PEAK_HOLD_MS,
    private val fallPerSecond: Float = PEAK_FALL_PER_SECOND,
) {
    /** 当前峰值（0..1）。 */
    var value: Float = 0f
        private set

    private var peak = 0f
    private var holdUntilMs = 0L

    /** 复位（重开一段 / 按了静音）：峰值清零，不留上一场的峰。 */
    fun reset() {
        peak = 0f
        holdUntilMs = 0L
        value = 0f
    }

    /** 用 [level] 与当前时刻 [nowMs] 更新峰值，返回这一帧该画的峰值。 */
    fun update(level: Float, nowMs: Long): Float {
        val l = level.coerceIn(0f, 1f)
        if (l >= peak) {
            peak = l
            holdUntilMs = nowMs + holdMs
        }
        // 回落从"持有到期那一刻"开始算，所以反复调用是幂等的（不会越掉越快）
        val decayed = if (nowMs <= holdUntilMs) {
            peak
        } else {
            peak - fallPerSecond * (nowMs - holdUntilMs) / 1000f
        }
        value = max(l, decayed).coerceIn(0f, 1f)
        return value
    }
}

/**
 * 呼吸脉动：返回 0..1 的慢正弦（周期 [periodMs]，默认 2.6s）。
 *
 * 为什么是正弦、不是方波/跳动：安静时用户盯着的是"这条还在不在工作"，
 * 缓慢的明暗变化像呼吸；哪怕只有 1px 的长度跳动，也会被看成"电平在乱抖"。
 */
internal fun breathPulse(nowMs: Long, periodMs: Int = BREATH_PERIOD_MS): Float {
    val period = if (periodMs <= 0) 1 else periodMs
    val phase = Math.floorMod(nowMs, period.toLong()).toDouble() / period
    return (0.5 + 0.5 * sin(2.0 * PI * phase)).toFloat().coerceIn(0f, 1f)
}

/**
 * 电平 → **视觉比例**（0..1）：一条压缩曲线（指数 [LEVEL_GAMMA]）。
 *
 * 声音的响度与人眼的"觉得多响"都不是线性的，所以电平条从来不是线性映射
 * （这也是为什么线性画出来"看着没反应"）。0→0、1→1，中间抬高但不饱和。
 */
internal fun levelToScale(level: Float): Float = level.coerceIn(0f, 1f).pow(LEVEL_GAMMA)

/**
 * 画出来的不透明度：**安静/暂停时只靠呼吸脉动**，有声音时随电平变亮。
 *
 * 为什么要抽成纯函数：这条直接决定"看起来亮不亮、会不会忽明忽暗"，是观感里最容易
 * 悄悄退回"整条死白"或"闪一下"的地方。用例钉住两端：暂停时只允许在
 * [BREATH_MIN_ALPHA]~[BREATH_MAX_ALPHA] 之间慢慢走；电平升高时单调变亮。
 */
internal fun levelAlpha(level: Float, paused: Boolean, breath: Float): Float {
    val l = level.coerceIn(0f, 1f)
    val b = breath.coerceIn(0f, 1f)
    return if (paused || l < LEVEL_QUIET) {
        (BREATH_MIN_ALPHA + (BREATH_MAX_ALPHA - BREATH_MIN_ALPHA) * b).coerceIn(0.2f, 0.8f)
    } else {
        // 亮度也跟着那条压缩曲线走：否则小音量时条子又短又暗，等于"看不见"
        (0.55f + 0.40f * levelToScale(l)).coerceIn(0.2f, 1f)
    }
}

/**
 * 电平 → 从中心到**端头**的距离（px，含圆头）。
 *
 * 静音时不是 0：留 [RIBBON_MIN_FRACTION] 一小截（否则"呼吸"没有东西可呼吸），
 * 满电平时正好落在 [maxHalf]（不越过留白）。中间按 [levelToScale] 压缩。
 * 纯函数，用例断言"单调、不越界、0 也有一小块、1 正好到边"。
 */
internal fun levelHalfLengthPx(maxHalf: Float, tipHalf: Float, level: Float): Float {
    val reachable = (maxHalf - tipHalf).coerceAtLeast(0f)
    return tipHalf + reachable * (RIBBON_MIN_FRACTION + (1f - RIBBON_MIN_FRACTION) * levelToScale(level))
}

/**
 * 中间高、两边低的钟形（0..1）：1 在最中间、[LEVEL_BARS] 两端是 0.25。
 *
 * 旧版它是"柱子的高度"，这一版它是"条子的**截面厚度**"—— 判据不变：
 * 中间最厚、两端收窄（所以端头看起来是圆的，不是被刀切平的）。
 */
internal fun barWeight(index: Int, bars: Int = LEVEL_BARS): Float {
    if (bars <= 1) return 1f
    val mid = (bars - 1) / 2f
    val d = abs(index - mid) / mid            // 0（中间）→ 1（两端）
    return (1f - d * 0.75f).coerceIn(0.25f, 1f)
}

/**
 * 颜色跟着强度走：弱=蓝（主色）、中=紫、强=红（与面板的状态色同一套）。
 *
 * 这一版它给**端头/峰值**用（条子本身是渐变）。刻意与 `EchoPalette` 一一对应，
 * 不写死任何色值 —— 换主题时这里自动跟着走。
 */
internal fun barColor(level: Float, weight: Float): Color {
    val v = (level * weight).coerceIn(0f, 1f)
    return when {
        v > 0.66f -> EchoPalette.Red
        v > 0.33f -> EchoPalette.Purple
        else -> EchoPalette.Accent
    }
}
