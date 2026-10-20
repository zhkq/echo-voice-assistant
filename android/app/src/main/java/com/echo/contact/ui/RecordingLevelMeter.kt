package com.echo.contact.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
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
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.dp
import com.echo.contact.ui.theme.EchoPalette
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.sin

/**
 * # 全屏录音页的电平：**会滚动的柱状波形**（2026-10-20 重做）
 *
 * 用户连着两天说的是同一件事（原话）：
 *  * 2026-10-19："录音电平现在是**呼吸状**，感觉用**更加敏感的波形效果**更明显"；
 *  * 2026-10-20："现在是呼吸状，我希望改成模型上下空间占的更高些，波动敏感些"。
 *
 * 所以这一版把"一根会呼吸的等厚胶囊"换成**滚动的柱状波形**：
 *
 * ```
 *       ╷  ╷   ╷   ╷╷   ╷              ← 一帧一根柱子（近 48 帧 ≈ 2 秒），
 *   ────┼──┼───┼───┼┼───┼──────          以中心线为轴**上下镜像**、圆角；
 *       ╵  ╵   ╵   ╵╵   ╵    ┃           最新一根贴最右，整体向左滚；
 *                            ┃           最右那根细亮线 = 峰值保持（0.8s 后缓慢回落）
 * ```
 *
 * ## 四条**用户点名**的口径（最容易被人"顺手改回去"，改之前先读这里）
 *
 *  1. **要高**：用户明确允许加高（"上下空间占得更高些 / 反正空间够大"）。
 *     [RECORDING_LEVEL_HEIGHT_DP] 从 64 → **100dp**；柱子从中心线向上下各占一半，
 *     电平高时**顶到带子的上下沿**（[WAVE_HEIGHT_FRACTION] = 0.96），不再只在中线附近抖。
 *     整段占位仍由它推导（[RECORDING_LEVEL_BAND_DP]），布局预算里没有第二个高度数字。
 *  2. **要敏感**：麦克风报上来的 RMS 本来就小（说话通常 0.1~0.3）。
 *     旧映射 `level^0.65` 把 0.15 画成 29% 的长度 —— 用户说的"看着没反应"就是它。
 *     现在 [levelToScale] = 先扣噪声底再 `^0.45`：0.15 → 39%、0.3 → 56%、0.5 → 72%，
 *     小声音明显起得来，又不是人人顶到头（0.5 还不到 8 成）。
 *  3. **要快**：帧级包络攻击 0.35 → [LEVEL_ATTACK]、释放 0.12 → [LEVEL_RELEASE]，
 *     大幅起音再快一档（[LEVEL_ATTACK_FAST]），仍按 `withFrameNanos` 的 dt 归一 ——
 *     60Hz / 120Hz / 掉帧的手机上**时间响应一致**（见 [LevelEnvelope]）。
 *  4. **不要呼吸**：安静时**不再脉动**，只留一条静止的、极淡的底线。
 *     用户两次都说嫌"呼吸状"，所以 [levelAlpha] 里那个 `breath` 形参已经**退役**
 *     （形参还在，只是不再参与计算 —— 别把它接回来）。
 *
 * ## 波形怎么滚起来
 *
 * [LevelHistory] 是纯 Kotlin 的 FloatArray 环形缓冲：每 [LEVEL_FRAME_MS]（40ms）推进一帧，
 * 存 [LEVEL_HISTORY_FRAMES]（48）帧 ≈ 1.9 秒。绘制时"第 i 根 = 缓冲里从最老数第 i 个"，
 * 最新一根贴右边 —— 于是每推进一帧，整条波形就往左移一格。
 *
 * **暂停 = 不再推进**（[shouldPushHistory] 直接返回 false）→ 波形**冻结**、整条换成
 * "已暂停"的 [EchoPalette.Yellow]（与标题那行"已暂停 · 时长在累计"同一个色）。
 *
 * ## 性能：这一版比上一版**更**省，别退回"整页重组"
 *
 * 帧循环只写 [MeterFrame] 的几格**绘制状态**（`mutableFloatStateOf` / `mutableStateOf`），
 * 它们**只**在 `onDrawBehind` 里被读 → Compose 只标脏绘制阶段，重组一次都不会发生。
 * 柱子用 `drawRoundRect(color = ...)`：颜色是 [waveColor] 按强度逐根算出来的主题色渐变，
 * **不 new shader、不 new Path**；每帧固定 48 个圆角矩形（外加一根底线、一根峰值线）。
 * 这是这条波形最贵的地方，再加东西之前先想清楚 120Hz 屏上的每帧预算。
 */

// ---------------------------------------------------------------------------
// 尺寸（"尺寸只有一处来源"：布局预算与用例都引这几个常量，别在 Modifier 里另写数字）
// ---------------------------------------------------------------------------

/**
 * 电平**自己**的高度（dp）。
 *
 * 2026-10-20 从 64 加高到 **100**：用户说"上下空间占得更高些"。
 * 为什么停在一百：整段占位 = 100 + 2×24 = 148dp，页面文字区预算 = 246 + 148 = 394；
 * 在 360×640 的小屏上（可用高度约 568dp）再放"一行状态 + 两行错误 + 两个 56dp 按钮"
 * 仍然只要 498dp —— 还留 70dp 余量，按钮不会被挤出去（见 `recordingButtonsVisible`）。
 * 再往上加（>100）就吃掉这条余量了，所以上限就定在这里。
 */
internal const val RECORDING_LEVEL_HEIGHT_DP = 100

/** 电平上下各留的呼吸（dp）。整段占位 = 100 + 2×24 = 148dp。 */
internal const val RECORDING_LEVEL_PADDING_DP = 24

/** 电平这一段在页面里**占的高度**（dp）。由上面两个常量推导，改高度时这里自动跟着变。 */
internal const val RECORDING_LEVEL_BAND_DP = RECORDING_LEVEL_HEIGHT_DP + 2 * RECORDING_LEVEL_PADDING_DP

// ---------------------------------------------------------------------------
// 平滑 / 峰值 / 静音的参数（理由都写在函数上）
// ---------------------------------------------------------------------------

/** 一个电平样本的节拍（ms）。麦克风那侧大约 100ms 报一次电平。 */
internal const val LEVEL_TICK_MS = 100f

/** 同上，纳秒版（`withFrameNanos` 给的是纳秒；1e8ns = 100ms）。 */
private const val LEVEL_TICK_NS = 100_000_000L

/**
 * 帧级包络的**攻击**系数（每个 [LEVEL_TICK_MS] 走掉剩余差距的 55%）。
 *
 * 旧版是 0.35：说一句话电平要两三百毫秒才爬上去，看着"温"。
 * 用户要"跟得上说话"，所以提到 0.55；再高（>0.7）就退回"一格一格跳"了。
 */
internal const val LEVEL_ATTACK = 0.55f

/**
 * **大幅起音**时用的更快攻击系数（差距超过 [LEVEL_ATTACK_FAST_GAP] 才切换）。
 *
 * 为什么还要这一档：0.55 两帧只到 1-0.45² = 0.80，而用户要"开口那一下立刻起来"。
 * 0.62 两帧到 1-0.38² = 0.86 —— 它仍然是**比例系数**，所以"两个 50ms 帧"与
 * "一个 100ms 帧"完全等价（`包络按帧间时间归一` 那条用例钉着），不会换台手机就变手感。
 */
internal const val LEVEL_ATTACK_FAST = 0.62f

/** 差距大于这个值才算"大幅起音"（小波动仍走 [LEVEL_ATTACK]，不许被瞬间放大成闪烁）。 */
internal const val LEVEL_ATTACK_FAST_GAP = 0.30f

/**
 * 帧级包络的**释放**系数（每个 [LEVEL_TICK_MS] 只落 21%）。
 *
 * 旧版 0.12：声音退下去要一秒多，配合"呼吸"看着更温。用户要更敏感，所以提到 0.21；
 * 仍比攻击慢一截（0.21 < 0.55），所以"有余响、不是啪一下没了"这条还成立。
 */
internal const val LEVEL_RELEASE = 0.21f

/**
 * **样本级**抑刺的攻击系数。
 *
 * 保留旧的 0.65：这一层只负责"一个坏样本别把整条电平拉满"（碰一下麦、敲一下键盘），
 * 它不需要慢 —— 慢的那一层是 [LevelEnvelope]。
 */
internal const val LEVEL_SAMPLE_ATTACK = 0.65f

/**
 * 样本级的释放系数（0.15）。
 *
 * 这一层是"逐样本"的抑刺，比包络快一档才会让包络有东西可跟；
 * 具体数值由既有用例钉着（从 1 落回 0.2 正好十个样本），别顺手改。
 */
internal const val LEVEL_SAMPLE_RELEASE = 0.15f

/** 峰值**停留**多久才开始回落（ms）。用户要的 ~0.8s。 */
internal const val PEAK_HOLD_MS = 800L

/** 峰值回落速度（每秒掉多少，0..1 的空间）。0.3/s → 从满格退到 0 大约 3.3s，够慢。 */
internal const val PEAK_FALL_PER_SECOND = 0.30f

/**
 * 安静脉动的周期（ms，**已退役**，只剩 [breathPulse] 自己用）。
 *
 * 2.6s 一次正弦；保留这个常数只为那条"呼吸是缓慢脉动"的用例仍成立。
 * **电平里不许再用它** —— 用户明确不要呼吸（2026-10-20）。
 */
internal const val BREATH_PERIOD_MS = 2600

/** 旧呼吸区间的亮度下限 / 上限。现在只作"暂停/安静亮度"的取值界（见 [levelAlpha]）。 */
internal const val BREATH_MIN_ALPHA = 0.34f
internal const val BREATH_MAX_ALPHA = 0.66f

/** 安静时的柱子透明度：取旧呼吸区间的下沿（静止、不脉动）。 */
private const val QUIET_ALPHA = BREATH_MIN_ALPHA

/** 暂停时的柱子透明度：取旧呼吸区间的上沿附近（比录音中暗一档，但"整条是黄的"要看得出来）。 */
private const val PAUSED_ALPHA = BREATH_MAX_ALPHA - 0.04f

/**
 * **噪声底**：低于它一律当"没有声音"（不点亮），高于它立刻按压缩曲线起来。
 *
 * 为什么要有：手机麦的底噪常在 0.01~0.03 之间，若不扣掉，安静时每根柱子都会
 * 有一两像素的抖动 —— 那看着像坏点，不像安静。0.03 是"肉眼已经看不出长度差别"的那条线。
 */
internal const val LEVEL_NOISE_FLOOR = 0.03f

/**
 * 刻度片数（奇数：中间一片、两边对称）。用它给**底线**做"中间亮、两端淡出"的淡出曲线
 * （[barWeight] 的钟形）—— 那条直线因此不会把视线从波形上拉走。
 */
internal const val LEVEL_BARS = 17

/**
 * 电平 → 视觉比例的**压缩指数**（0.65 → 0.45）。
 *
 * 为什么不是线性：麦克风 RMS 本来就很低（说话时 0.1~0.3），线性映射下柱子永远只有一小截。
 * 为什么也不是旧的 0.65：0.15 只能画到 29%，用户说的"不敏感"就是它。
 * 0.45：0.15 → 39%、0.5 → 72%、1 → 100% —— 低段抬得起来，高段又不提前饱和。
 */
internal const val LEVEL_GAMMA = 0.45f

// ---------------------------------------------------------------------------
// 波形（环形缓冲 + 每帧一根柱子）
// ---------------------------------------------------------------------------

/** 波形保留多少帧。48 帧 × 40ms ≈ **1.9 秒**（用户要的"最近两秒"）。 */
internal const val LEVEL_HISTORY_FRAMES = 48

/** 波形每推进一步的时间（ms）。40ms 一根 → 48 根正好铺满约两秒。 */
internal const val LEVEL_FRAME_MS = 40

/** 同上，纳秒版。 */
private const val LEVEL_FRAME_NS = LEVEL_FRAME_MS * 1_000_000L

/**
 * 一帧最多算多长的 dt（ms）。掉帧时（比如 200ms 才来一帧）不许让包络一步跳很远 ——
 * 那会看着"闪一下"，所以夹在 80ms（= 两拍）。
 */
private const val LEVEL_FRAME_MAX_MS = 80f

/**
 * 柱子最高占电平高度的比例（0.96）。
 *
 * 为什么不是 1.0：圆角矩形贴到最边上会有一点点被裁掉的错觉；0.96 让"满格"看起来
 * **顶到上下沿**、又不是硬切在边界上。用户要的"上下空间占得更高"就是靠它落地的。
 */
private const val WAVE_HEIGHT_FRACTION = 0.96f

/** 一根柱子占它那一格宽度的比例（0.62）：剩下的是缝，柱子之间要看得出来是"一根根"的。 */
private const val WAVE_BAR_WIDTH_FRACTION = 0.62f

/** 视觉比例低于这个值的柱子不画（安静时只剩那条底线，而不是一排 1px 的坏点）。 */
private const val WAVE_MIN_SCALE = 0.02f

/** 底线粗细（dp）。 */
private const val BASELINE_HEIGHT_DP = 1.5f

/** 暂停时底线的透明度（平时底线是 Border 色 + 两端淡出，不需要额外乘）。 */
private const val BASELINE_ALPHA_PAUSED = 0.55f

/** 峰值细亮线的粗细（dp）。 */
private const val PEAK_LINE_WIDTH_DP = 2f

/** 空波形（复位时用它整体替换一次 → 上一次的柱子立刻从画布上消失）。 */
private val EMPTY_WAVE = FloatArray(0)

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

/**
 * 滚动波形电平（**唯一的**绘制入口）。参数全是"事实"，不含任何动画控制：
 *
 * @param rawLevel 麦克风报上来的原始电平（0..1）；平滑在这里面做，调用方别自己再平滑一遍。
 * @param paused 暂停中（= 写空白音频）。暂停时整条换成"已暂停"的黄色、波形冻结。
 * @param sessionKey 这一场录音的标识（`MicState.startedAt`）。**换值 = 重开一段 → 清空波形与峰值**。
 */
@Composable
internal fun RecordingLevelMeter(
    rawLevel: Float,
    paused: Boolean,
    sessionKey: String,
    modifier: Modifier = Modifier,
) {
    // 帧循环里要读**最新**的输入，但不许因为它重启（重启一次就会丢掉包络/峰值/波形历史）
    val levelNow = rememberUpdatedState(rawLevel)
    val pausedNow = rememberUpdatedState(paused)

    val frame = remember { MeterFrame() }
    val envelope = remember { LevelEnvelope() }
    val peakHold = remember { PeakHold() }
    val history = remember { LevelHistory() }

    // 重开一段时清零：上一场的峰值/波形带过来，就是一条"这一场根本没响过"的假画面。
    LaunchedEffect(sessionKey) {
        envelope.reset()
        peakHold.reset()
        history.clear()
        frame.level = 0f
        frame.peak = 0f
        // 整体替换成空快照：既是"清屏"，也逼绘制阶段重来一次（否则上一场的柱子会留在画布上）
        frame.wave = EMPTY_WAVE
    }

    // 帧循环：**只写 draw 状态**（不写任何被 composition 读的东西），所以不会触发整页重组。
    LaunchedEffect(Unit) {
        var lastNs = 0L
        var lastGateNs = 0L
        var lastPushNs = 0L
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
            // 若用"值变了"当触发条件，那几拍就不会再收敛，柱子会卡在半路。
            if (ns - lastGateNs >= LEVEL_TICK_NS) {
                gated = smoothLevel(gated, sample)
                lastGateNs = ns
            }
            val level = envelope.step(gated, dtMs.coerceIn(0f, LEVEL_FRAME_MAX_MS))
            frame.level = level
            frame.peak = peakHold.update(level, ns / 1_000_000L)
            frame.paused = p

            // 波形推进：**暂停时一步都不推** → 画面冻结（用户："暂停时整条别动"）。
            if (shouldPushHistory(p, ns, lastPushNs)) {
                history.push(level)
                lastPushNs = ns
                // 每 40ms 拷一份小快照（48 个 float）交给绘制阶段：
                // 绘制读的是**稳定快照**，而不是一块正在被改的数组；顺带保证"全是静音、
                // 电平值不变"时也会重绘（否则历史推进了、画面却不动）。
                frame.wave = history.snapshot()
            }
        }
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(RECORDING_LEVEL_HEIGHT_DP.dp)
            .drawWithCache {
                // 底线两条渐变：中间亮、两端淡出（用 `barWeight` 那条钟形当透明度曲线）。
                // 它在 `drawWithCache` 里按**尺寸**建一次，之后每帧只是被复用。
                val baselineBrush = Brush.horizontalGradient(
                    colorStops = Array(LEVEL_BARS) { i ->
                        val t = i / (LEVEL_BARS - 1f)
                        t to EchoPalette.Border.copy(
                            alpha = 0.06f + 0.16f * barWeight(i, LEVEL_BARS),
                        )
                    },
                    startX = 0f,
                    endX = size.width,
                )
                val pausedBaselineBrush = SolidColor(EchoPalette.Yellow)
                onDrawBehind {
                    drawMeterWave(
                        baselineBrush = baselineBrush,
                        pausedBaselineBrush = pausedBaselineBrush,
                        level = frame.level,
                        peak = frame.peak,
                        paused = frame.paused,
                        wave = frame.wave,
                    )
                }
            },
    )
}

/**
 * 帧循环写、draw 读的四格状态（电平 / 峰值 / 是否暂停 / 波形快照）。
 *
 * 为什么不是 `Animatable`：这几格是**同一帧算出的一组数**（电平、峰值、波形），
 * 分成几个动画对象反而会对不齐（峰值可能落在上一帧的电平上）。
 * 为什么用状态而不是普通字段：**变了要触发重绘** —— 但这些状态只在绘制阶段被读到，
 * 所以只标脏绘制、不会引起任何重组。
 */
private class MeterFrame {
    var level by mutableFloatStateOf(0f)
    var peak by mutableFloatStateOf(0f)
    var paused by mutableStateOf(false)

    /** 最近约两秒的波形（从旧到新）。整体替换 → 绘制阶段自动重绘。 */
    var wave by mutableStateOf(EMPTY_WAVE)
}

// ---------------------------------------------------------------------------
// 绘制（只有这里碰 Canvas / 颜色）
// ---------------------------------------------------------------------------

private fun DrawScope.drawMeterWave(
    baselineBrush: Brush,
    pausedBaselineBrush: Brush,
    level: Float,
    peak: Float,
    paused: Boolean,
    wave: FloatArray,
) {
    if (size.width <= 0f || size.height <= 0f) return

    val cy = size.height / 2f
    // 柱子的最大半高：占满到带子上下沿（0.96），这是"上下空间占得更高"的落点
    val halfMax = size.height / 2f * WAVE_HEIGHT_FRACTION

    // ---- 1) 底线：**静止**的一条极淡细线 ----
    // 安静时就只剩它：用户要的是"波形"，不是"呼吸"，所以这里没有任何随时间变化的东西。
    val baselineH = BASELINE_HEIGHT_DP.dp.toPx()
    drawRoundRect(
        brush = if (paused) pausedBaselineBrush else baselineBrush,
        topLeft = Offset(0f, cy - baselineH / 2f),
        size = Size(size.width, baselineH),
        cornerRadius = CornerRadius(baselineH / 2f),
        alpha = if (paused) BASELINE_ALPHA_PAUSED else 1f,
    )

    if (wave.isEmpty()) return

    // ---- 2) 柱阵：一帧一根、最新一根贴右边、整体向左滚 ----
    // 每根柱子占**固定**一格（宽度 / 48）：这样满 48 帧时最老那根正好在左端，
    // 之后每推进一帧所有值往左挪一格 —— 看上去就是波形在向左滚动。
    val slot = size.width / LEVEL_HISTORY_FRAMES
    val barW = (slot * WAVE_BAR_WIDTH_FRACTION).coerceAtLeast(1f)
    val radius = CornerRadius(barW / 2f)
    val n = wave.size
    for (i in 0 until n) {
        val lv = wave[i]
        val scale = levelToScale(lv)
        // 噪声底以下不画：安静时只剩底线，而不是一排一像素的"坏点"
        if (scale < WAVE_MIN_SCALE) continue
        val half = waveBarHalfHeightPx(halfMax, lv)
        val left = size.width - (n - i) * slot + (slot - barW) / 2f
        drawRoundRect(
            color = waveBarColor(lv, paused),
            topLeft = Offset(left, cy - half),
            size = Size(barW, half * 2f),
            cornerRadius = radius,
            alpha = levelAlpha(lv, paused, 0f),
        )
    }

    // ---- 3) 峰值：保留的那根细亮线，落在**最右（当前）**那一格 ----
    // 为什么只画"明显高于当前电平"的时候：否则它就压在最新那根柱子上，看不出是另一条线。
    if (!paused && peak > level + 0.01f) {
        val x = size.width - slot / 2f
        val peakHalf = waveBarHalfHeightPx(halfMax, peak).coerceAtLeast(1.5f.dp.toPx())
        drawLine(
            color = barColor(peak, 1f),
            start = Offset(x, cy - peakHalf),
            end = Offset(x, cy + peakHalf),
            strokeWidth = PEAK_LINE_WIDTH_DP.dp.toPx(),
            cap = StrokeCap.Round,
            alpha = 0.95f,
        )
    }
}

// ---------------------------------------------------------------------------
// 纯函数 / 纯状态（用例如 `RecordingLevelMeterTest` 钉着；改参数先看那里）
// ---------------------------------------------------------------------------

/**
 * **波形历史**：FloatArray 环形缓冲（纯 Kotlin，JVM 可测；不碰 Compose、不碰 Android）。
 *
 * 为什么要环形缓冲、而不是 `List<Float>`：每 40ms 推进一帧，一秒 25 次；用列表就要么
 * 无限长（越跑越占内存）、要么每帧 `removeAt(0)`（O(n) 拷贝）。环形缓冲两样都不用。
 *
 * 语义（用例钉着）：[at] 的 0 是**最老**、[LevelHistory.size] - 1 是**最新**；
 * 满了以后新值覆盖最老那格（[push] 里 `oldest` 前移一格）。
 */
internal class LevelHistory(val capacity: Int = LEVEL_HISTORY_FRAMES) {

    init {
        require(capacity > 0) { "波形容量必须大于 0：$capacity" }
    }

    private val values = FloatArray(capacity)

    /** 最老那一格的物理下标。 */
    private var oldest = 0

    /** 现在存了多少帧（0..[capacity]）。 */
    var size: Int = 0
        private set

    /** 清空（重开一段 / 换一场）。 */
    fun clear() {
        oldest = 0
        size = 0
        values.fill(0f)
    }

    /**
     * 压入一帧电平（夹到 0..1；NaN 当 0 —— 一个坏值不该把整根柱子画成 NaN 高度）。
     *
     * 满了以后挤掉**最老的**那一帧。
     */
    fun push(level: Float) {
        val v = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        if (size < capacity) {
            values[(oldest + size) % capacity] = v
            size += 1
        } else {
            values[oldest] = v
            oldest = (oldest + 1) % capacity
        }
    }

    /** 取第 [index] 帧（0 = 最老，[size] - 1 = 最新）。 */
    fun at(index: Int): Float {
        require(index in 0 until size) { "波形下标越界：$index（size=$size）" }
        return values[(oldest + index) % capacity]
    }

    /** 从旧到新拷一份（绘制阶段用的是它；用例也用它比对顺序）。 */
    fun snapshot(): FloatArray = FloatArray(size) { at(it) }
}

/**
 * 这一帧要不要把电平推进波形历史（**暂停 = 冻结**）。
 *
 * 抽成纯函数是为了让"暂停不再滚"这件事**可测**（JVM 里渲染不了 Compose）。
 * 两条规则：
 *  * 暂停时一律不推 → 波形定在暂停那一刻；
 *  * 没到 [LEVEL_FRAME_MS] 节拍也不推 → 120Hz 屏上波形不会"跑得比声音快"
 *    （否则 2 秒的历史在 120Hz 上只剩 0.4 秒）。
 */
internal fun shouldPushHistory(paused: Boolean, nowNs: Long, lastPushNs: Long): Boolean {
    if (paused) return false
    if (lastPushNs <= 0L) return true
    return nowNs - lastPushNs >= LEVEL_FRAME_NS
}

/**
 * **样本级**平滑：攻击快、释放慢（一次跳变只走一步的 65% / 15%）。
 *
 * 为什么不能直接用采样值：麦克风每 ~100ms 一个瞬时值，偶发的单点尖刺（碰麦、敲键盘）
 * 会把整条电平一下拉满 —— 这一层就是"一个坏样本不许决定整段画面"。
 */
internal fun smoothLevel(prev: Float, target: Float): Float {
    val p = prev.coerceIn(0f, 1f)
    val t = target.coerceIn(0f, 1f)
    val k = if (t >= p) LEVEL_SAMPLE_ATTACK else LEVEL_SAMPLE_RELEASE
    return (p + (t - p) * k).coerceIn(0f, 1f)
}

/**
 * **帧级**包络跟随（状态类，可测）：攻击 [LEVEL_ATTACK]（大幅起音 [LEVEL_ATTACK_FAST]）、
 * 释放 [LEVEL_RELEASE]。
 *
 * 为什么要按 `dtMs` 归一：`withFrameNanos` 给的帧间隔不是常数（掉帧、90/120Hz 屏）。
 * 若每个绘制帧都用固定系数，120Hz 手机上电平会"跑得比声音快"、掉帧时又变成"一格一格跳"。
 * 所以系数按"一个 [LEVEL_TICK_MS] 节拍"标定，帧间用 `1-(1-k)^(dt/节拍)` 折算 ——
 * 这样不同刷新率下的**时间响应**一致；而且两档攻击都是**比例系数**，
 * "两个 50ms 帧"与"一个 100ms 帧"严格等价（用例钉着）。
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
        val ticks = (dtMs / LEVEL_TICK_MS).coerceIn(0f, 2f)
        if (ticks <= 0f) return value
        val rising = t >= value
        val k = when {
            !rising -> release
            // 大幅起音走更快那一档：用户要的是"开口那一下立刻起来"
            t - value > LEVEL_ATTACK_FAST_GAP -> LEVEL_ATTACK_FAST
            else -> attack
        }
        val alpha = 1f - (1f - k).pow(ticks)
        val next = value + (t - value) * alpha
        // 起音不许越过目标（否则会把"还差一点"画成"已经满了"）
        value = (if (rising) next.coerceAtMost(t) else next.coerceAtLeast(t)).coerceIn(0f, 1f)
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
 * 呼吸脉动（**已退役**，2026-10-20）。
 *
 * 旧版安静时用它做 2.6s 的明暗脉动。用户两天里两次说"嫌呼吸状、要波形"，
 * 所以**电平已经不再调用它**（安静时只留一条静止的淡底线，见 [levelAlpha]）。
 *
 * 这个纯函数还留着两个理由：① `RecordingLevelMeterTest` 仍钉着它（用例只增不删）；
 * ② 万一以后别的地方（等待态之类）真需要"呼吸"，别再在电平里造第二个。
 * **不要在电平里把它接回来。**
 */
internal fun breathPulse(nowMs: Long, periodMs: Int = BREATH_PERIOD_MS): Float {
    val period = if (periodMs <= 0) 1 else periodMs
    val phase = Math.floorMod(nowMs, period.toLong()).toDouble() / period
    return (0.5 + 0.5 * sin(2.0 * PI * phase)).toFloat().coerceIn(0f, 1f)
}

/**
 * 电平 → **视觉比例**（0..1）：先扣噪声底，再走压缩指数 [LEVEL_GAMMA]。
 *
 * 为什么不是线性：声音的响度与人眼的"觉得多响"都不是线性的（这也是为什么线性画出来
 * "看着没反应"）。为什么先扣底：手机麦的底噪会让安静时每根柱子都抖一两像素；
 * 扣掉之后"安静 = 一条静止的底线"，一开口就按曲线立刻起来。
 *
 * 两个端点仍然是 0→0、1→1（满电平静止在带子上下沿）。
 */
internal fun levelToScale(level: Float): Float {
    val l = level.coerceIn(0f, 1f)
    if (l <= LEVEL_NOISE_FLOOR) return 0f
    val t = (l - LEVEL_NOISE_FLOOR) / (1f - LEVEL_NOISE_FLOOR)
    return t.pow(LEVEL_GAMMA).coerceIn(0f, 1f)
}

/**
 * 一根柱子的**半高**（px）：电平 1 → 正好 [halfHeightPx]（顶到带子上下沿）。
 *
 * 抽成纯函数是为了让"柱子在竖直方向铺满"可测：用户明确说"上下空间占得更高、
 * 电平高时顶到上下沿"，而这条最容易在下一轮被改回"只在中线附近抖"。
 */
internal fun waveBarHalfHeightPx(halfHeightPx: Float, level: Float): Float {
    if (halfHeightPx <= 0f) return 0f
    return (halfHeightPx * levelToScale(level)).coerceIn(0f, halfHeightPx)
}

/**
 * 强度 → 主题色的**连续渐变**（蓝 → 紫 → 红）。
 *
 * 为什么不用 `Brush` 渐变：柱子是 48 个各自独立的小矩形，`drawRoundRect(brush=...)` 的
 * 渐变坐标是按**每个矩形自己的包围盒**算的 —— 每根柱子都会被压成一条迷你彩虹。
 * 所以这里逐根算颜色（`lerp` 两个主题色，没有 shader、没有每帧分配），
 * 高度表示强度、颜色也表示强度，同一套语义。
 */
internal fun waveColor(level: Float): Color {
    val v = level.coerceIn(0f, 1f)
    return if (v <= 0.5f) {
        lerp(EchoPalette.Accent, EchoPalette.Purple, v * 2f)
    } else {
        lerp(EchoPalette.Purple, EchoPalette.Red, (v - 0.5f) * 2f)
    }
}

/**
 * 一根柱子最终的颜色：**暂停 = 已暂停的黄色**（与标题同色），录音中 = 强度渐变色。
 *
 * 抽成纯函数是为了让"暂停时整条换黄"可测（JVM 门禁里渲染不了 Compose，只能断言颜色值）。
 */
internal fun waveBarColor(level: Float, paused: Boolean): Color =
    if (paused) EchoPalette.Yellow else waveColor(level)

/**
 * 画出来的不透明度：**暂停/安静时是一个常量**（不再呼吸），有声音时随电平变亮。
 *
 * `breath` 形参**已退役**：用户两次说嫌"呼吸状"，安静时只留一条静止的底线。
 * 形参保留是因为既有用例与调用点还按这个签名走；它不再参与任何计算。
 * 暂停与安静的取值仍落在旧的呼吸区间（[BREATH_MIN_ALPHA]~[BREATH_MAX_ALPHA]）里，
 * 这样"已暂停"那一屏的明暗与上一版连续，不会突然跳一档。
 */
@Suppress("UNUSED_PARAMETER")
internal fun levelAlpha(level: Float, paused: Boolean, breath: Float): Float {
    val l = level.coerceIn(0f, 1f)
    return when {
        paused -> PAUSED_ALPHA
        l < LEVEL_NOISE_FLOOR -> QUIET_ALPHA
        // 亮度也跟着那条压缩曲线走：否则小音量时柱子又矮又暗，等于"看不见"
        else -> (0.55f + 0.40f * levelToScale(l)).coerceIn(0.2f, 1f)
    }
}

/**
 * 电平 → 从中心到**端头**的距离（px，含圆头）。
 *
 * **已退役**：波形取代了"单条胶囊"，绘制已经不再调用它。
 * 保留是因为 `RecordingLevelMeterTest` 仍钉着"单调、不越界、静音留一小截"这条性质
 * （用例只增不删），也留给以后真要画"单条长度条"的地方。**别拿它去画波形**。
 */
internal fun levelHalfLengthPx(maxHalf: Float, tipHalf: Float, level: Float): Float {
    val reachable = (maxHalf - tipHalf).coerceAtLeast(0f)
    return tipHalf + reachable * (RIBBON_MIN_FRACTION + (1f - RIBBON_MIN_FRACTION) * levelToScale(level))
}

/** 单条长度条静音时留的那一小截比例（0.05）—— 与 [levelHalfLengthPx] 一起退役。 */
private const val RIBBON_MIN_FRACTION = 0.05f

/**
 * 中间高、两边低的钟形（0..1）：1 在最中间、[LEVEL_BARS] 两端是 0.25。
 *
 * 现在它是**底线**的"两端淡出"曲线（中间实、两端虚）—— 判据不变：
 * 中间最亮、两端收窄，那条直线才不会把视线从波形上拉走。
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
 * 现在它给**峰值细亮线**用（柱子是 [waveColor] 的连续渐变）。
 * 刻意与 `EchoPalette` 一一对应，不写死任何色值 —— 换主题时这里自动跟着走。
 */
internal fun barColor(level: Float, weight: Float): Color {
    val v = (level * weight).coerceIn(0f, 1f)
    return when {
        v > 0.66f -> EchoPalette.Red
        v > 0.33f -> EchoPalette.Purple
        else -> EchoPalette.Accent
    }
}
