package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.max

/**
 * 全屏录音页**新版电平**的判据（2026-10-18 深夜重做：对称镜像 + 渐变 + 峰值保持 + 平滑包络）。
 *
 * 这一批用例只钉"看得见的那几条性质"，不钉具体像素：
 *
 *  1. **攻击快、释放慢**：两个样本就要过半并冲到 0.8 以上，回落要**十个样本**才到 0.2 以下
 *     （旧版 0.25 的释放六个样本就掉没了 —— 用户说的"跳"一半是它）。
 *  2. **包络按帧间时间归一**：120Hz 屏与 60Hz 屏上的**时间响应**必须一致（否则换台手机手感就变了）。
 *  3. **峰值保持**：0.8s 内一动不动（"看得出峰值"），到期后**缓慢**回落（100ms 只许掉一点点），
 *     重开一段要清零（不许挂着上一场的峰）。
 *  4. **安静时是呼吸、不是跳动**：只变亮度（0.30~0.62 的慢脉动），长度一动不动。
 *  5. **别挤掉功能**：电平高度是常量且在 56~72 之间、页面总高度**不增**、
 *     大计时 / 本段进度 / 一行状态 / 两个 56dp 按钮一个都不许少。
 *  6. **配色来自主题**：电平组件里不许出现写死的色值。
 *
 * 为什么这些"观感"值得用例：它们最容易在下一轮被随手改掉，而改坏了**不会报错**，
 * 只会"看着不对"—— 那正是用户这次提的问题。
 */
class RecordingLevelMeterTest {

    // ---- 1. 包络：攻击快、释放慢 ----

    /**
     * 一路电平从 0 跳到 1：**两个样本就要到 0.8 以上**（跟得上说话）。
     *
     * 这条对应判据里"0→1 用 2 帧到 0.8+"—— 这里的"帧"是**一个电平样本**（~100ms），
     * 也就是 [smoothLevel] 的一步。第一版（旧值 0.65 攻/0.25 放）两个样本是 0.88，
     * 新版换了慢释放但攻击保持 0.65，所以这条仍然是 0.88。
     */
    @Test
    fun `电平样本两个就到 0_88 攻击快`() {
        var v = 0f
        v = smoothLevel(v, 1f)
        v = smoothLevel(v, 1f)
        assertTrue("两个电平样本之后必须已经超过 0.8：$v", v >= 0.8f)
    }

    /**
     * 一路电平从 1 掉到 0：**十个样本**才回到 0.2 以下（释放慢、有余响）。
     *
     * 旧值（0.25）六个样本就掉到 0.17 —— 那正是用户说的"一跳就没"。
     * 现在 0.15：0.85^9 ≈ 0.232（还在 0.2 以上）、0.85^10 ≈ 0.197（终于到 0.2 以下）。
     */
    @Test
    fun `电平样本十个才回到 0_2 释放慢`() {
        var v = 1f
        var frames = 0
        while (v >= 0.2f && frames < 200) {
            v = smoothLevel(v, 0f)
            frames += 1
        }
        assertTrue("回落太快（$frames 个样本就到 0.2 以下）：会看着像'啪一下没了'", frames > 8)
        assertEquals("0.15 的释放应当正好十个样本落回 0.2 以下", 10, frames)
        // 反向再钉一次：慢释放意味着"一个样本掉得不多"
        assertTrue("一个样本掉得太多：${1f - smoothLevel(1f, 0f)}", 1f - smoothLevel(1f, 0f) <= 0.2f)
    }

    /** 平滑函数**两端都不会越界**（负数、超过 1 的输入也一样）—— 越界就会画出屏幕。 */
    @Test
    fun `平滑与包络都不会越界`() {
        for (p in listOf(-1f, 0f, 0.3f, 1f, 2f)) {
            for (t in listOf(-1f, 0f, 0.3f, 1f, 2f)) {
                val v = smoothLevel(p, t)
                assertTrue("smoothLevel 越界：$v（prev=$p target=$t）", v in 0f..1f)
            }
        }
        val e = LevelEnvelope()
        for (t in listOf(-1f, 0f, 0.5f, 1f, 2f)) {
            e.step(t, 0f)
            assertTrue("包络越界（dt=0）：${e.value}", e.value in 0f..1f)
            e.step(t, 500f)
            assertTrue("包络越界（超长帧）：${e.value}", e.value in 0f..1f)
        }
    }

    /**
     * 包络**按帧间时间归一**：两个 50ms 的帧与一个 100ms 的帧，走出来的结果必须一样。
     *
     * 为什么要钉：`withFrameNanos` 的间隔不是常数（60/90/120Hz、掉帧）。
     * 如果每帧都用固定系数，120Hz 手机上电平会"跑得比声音快"，掉帧时又变回"一格一格跳"。
     */
    @Test
    fun `包络按帧间时间归一`() {
        val once = LevelEnvelope()
        once.step(1f, 100f)

        val twice = LevelEnvelope()
        twice.step(1f, 50f)
        twice.step(1f, 50f)

        assertEquals("两帧 50ms 与一帧 100ms 应当等价", once.value, twice.value, 0.005f)

        val still = LevelEnvelope()
        still.step(0.6f, 100f)
        val before = still.value
        still.step(1f, 0f)
        assertEquals("dt=0 的那一帧一步都不许走", before, still.value, 0.0001f)
    }

    /**
     * 暂停（输入恒为 0）时**也必须是平滑的**：一帧最多掉 [LEVEL_RELEASE]，而且最终落到 0 附近。
     *
     * 这条是"静音/暂停时不许乱抖"的直接判据：暂停那一刻电平是从当前值慢慢退下去的，
     * 不是"啪一下变成空条"。
     */
    @Test
    fun `暂停时输入零也平滑回落`() {
        val e = LevelEnvelope()
        // 先冲到满格
        repeat(20) { e.step(1f, 100f) }
        assertTrue("包络追满格失败：${e.value}", e.value > 0.99f)

        var prev = e.value
        var maxDrop = 0f
        repeat(40) {
            val v = e.step(0f, 100f)
            maxDrop = max(maxDrop, prev - v)
            prev = v
        }
        assertTrue("一帧掉得太多（会看着像抽搐）：$maxDrop", maxDrop <= 0.13f)
        assertTrue("暂停之后应当平稳退到 0 附近：$prev", prev < 0.05f)

        // 反向：**一帧不许归零**（静音按下去的那一瞬间也要有余响）
        val quick = LevelEnvelope()
        repeat(20) { quick.step(1f, 100f) }
        quick.step(0f, 100f)
        assertTrue("按静音后一帧就掉光了：${quick.value}", quick.value >= 0.85f)
    }

    // ---- 2. 峰值保持 ----

    /**
     * 峰值：立刻跟上新峰 → **停留 0.8s** → 之后**缓慢**回落（不是"啪一下消失"）。
     *
     * "看得出峰值"是这条的唯一目的：没有它，说话那一下一响就退回当前电平，用户根本看不清。
     */
    @Test
    fun `峰值保持 0_8 秒再缓慢回落`() {
        val peak = PeakHold()
        assertEquals("满电平立刻成为峰值", 1f, peak.update(1f, 0L), 0.001f)
        assertEquals("0.7 秒时还停在峰上", 1f, peak.update(0f, 700L), 0.001f)
        assertEquals("0.8 秒（持有到期那一刻）仍在峰上", 1f, peak.update(0f, 800L), 0.001f)

        val at900 = peak.update(0f, 900L)
        assertTrue("到期之后要开始回落：$at900", at900 < 1f)
        assertTrue("但必须**缓慢**回落（100ms 只许掉一点点）：$at900", at900 > 0.92f)

        val at2000 = peak.update(0f, 2000L)
        assertTrue("2 秒后仍应在半格以上（不是没了）：$at2000", at2000 > 0.5f)

        assertEquals("最终落回 0", 0f, peak.update(0f, 6000L), 0.001f)
    }

    /** 峰值**不会低于当前电平、也不会越界**（否则那条线会跑到条子里面或屏幕外）。 */
    @Test
    fun `峰值不越界也不低于当前电平`() {
        val peak = PeakHold()
        assertEquals("没声音时峰值是 0", 0f, peak.update(0f, 0L), 0.001f)
        assertEquals(0.4f, peak.update(0.4f, 100L), 0.001f)
        assertEquals("超过 1 的输入要夹住", 1f, peak.update(2f, 200L), 0.001f)
        // 峰值已经持有到 1 之后，即使电平掉到 0.4，峰值也不许跟着掉下去
        assertEquals("持有期内峰值不许掉", 1f, peak.update(0.4f, 300L), 0.001f)
    }

    /** 复位（重开一段 / 按静音）时峰值**清零**：不许把上一场的峰带过来。 */
    @Test
    fun `重开一段时峰值清零`() {
        val peak = PeakHold()
        peak.update(1f, 0L)
        peak.update(1f, 100L)
        assertEquals(1f, peak.value, 0.001f)

        peak.reset()
        assertEquals("复位之后立刻就是 0", 0f, peak.value, 0.001f)
        assertEquals("复位之后再来一个低电平，峰值就是它（没有旧峰）",
            0.2f, peak.update(0.2f, 150L), 0.001f)
    }

    // ---- 3. 安静时呼吸、亮度跟着走 ----

    /**
     * 呼吸是**慢脉动**：值永远在 0..1、一帧（16ms）变化极小、周期 ≥2s。
     *
     * 为什么把"一帧变化量"也钉住：呼吸要是做快了，就不再是"呼吸"而是"闪"，
     * 而闪在安静界面里非常刺眼（用户要的是"轻微"）。
     */
    @Test
    fun `呼吸是缓慢脉动而不是跳动`() {
        var prev = breathPulse(0L)
        var maxStep = 0f
        var min = prev
        var maxV = prev
        var t = 0L
        while (t <= BREATH_PERIOD_MS.toLong()) {
            val v = breathPulse(t)
            assertTrue("呼吸值越界：$v", v in 0f..1f)
            maxStep = max(maxStep, abs(v - prev))
            min = minOf(min, v)
            maxV = maxOf(maxV, v)
            prev = v
            t += 16L
        }
        assertTrue("一个 16ms 的帧里变了 $maxStep（会看着像闪）", maxStep < 0.025f)
        assertTrue("周期太短就不叫呼吸了：$BREATH_PERIOD_MS", BREATH_PERIOD_MS >= 2000)
        assertTrue("呼吸幅度太小会看不出在动（min=$min max=$maxV）", maxV - min > 0.9f)
    }

    /**
     * 暂停时的亮度**只允许在呼吸区间里慢慢走**（0.30~0.62），有声音时**越响越亮**。
     *
     * "已暂停"的配色必须保持住：暂停时不许回到那套蓝→紫→红的"正在录"。
     */
    @Test
    fun `暂停时亮度只在呼吸区间里变化`() {
        for (b in listOf(0f, 0.25f, 0.5f, 0.75f, 1f)) {
            val a = levelAlpha(level = 0f, paused = true, breath = b)
            assertTrue("暂停时亮度越界：$a（breath=$b）", a in BREATH_MIN_ALPHA..BREATH_MAX_ALPHA)
        }
        // 暂停时即使"电平"那一格有残留（比如刚按下静音的余响），也只按呼吸走
        val pausedHigh = levelAlpha(level = 0.9f, paused = true, breath = 0f)
        assertTrue("暂停时不该因为电平高就变亮：$pausedHigh", pausedHigh <= BREATH_MAX_ALPHA)

        val dim = levelAlpha(level = 0.2f, paused = false, breath = 0f)
        val bright = levelAlpha(level = 0.9f, paused = false, breath = 0f)
        assertTrue("电平升高必须变亮：dim=$dim bright=$bright", bright > dim)

        // 极低电平（非暂停）也走呼吸 —— 否则"安静时轻微呼吸"就没了
        val quiet = levelAlpha(level = 0f, paused = false, breath = 0.5f)
        assertTrue("极低电平应当按呼吸走：$quiet", quiet in BREATH_MIN_ALPHA..BREATH_MAX_ALPHA)
    }

    /** 电平 → 视觉比例是一条**压缩曲线**：0→0、1→1、单调，小音量抬得起来、又不会提前饱和。 */
    @Test
    fun `视觉比例是压缩曲线而不是线性的`() {
        assertEquals(0f, levelToScale(0f), 0.0001f)
        assertEquals("满电平必须是满比例", 1f, levelToScale(1f), 0.0001f)

        var prev = -1f
        for (i in 0..100) {
            val v = levelToScale(i / 100f)
            assertTrue("视觉比例必须单调不减：$v（level=${i / 100f}）", v >= prev)
            assertTrue("视觉比例越界：$v", v in 0f..1f)
            prev = v
        }
        // 线性映射下"说话时 0.1~0.3 的电平"只有一小截 —— 那就是"看着没反应"
        assertTrue("0.1 的电平只画出来 ${levelToScale(0.1f)}（太小了）", levelToScale(0.1f) >= 0.2f)
        assertTrue("0.3 的电平只画出来 ${levelToScale(0.3f)}", levelToScale(0.3f) >= 0.4f)
        assertTrue("0.5 就快满格了（平时会永远顶到头）：${levelToScale(0.5f)}", levelToScale(0.5f) <= 0.8f)
    }

    /** 电平 → 长度的映射：单调不减、不越界，静音时留一小截（不然没东西可"呼吸"）。 */
    @Test
    fun `电平长度单调且不越界`() {
        val maxHalf = 160f
        val tip = 14.4f
        var prev = -1f
        for (i in 0..100) {
            val v = levelHalfLengthPx(maxHalf, tip, i / 100f)
            assertTrue("长度必须单调不减：$v（level=${i / 100f}）", v >= prev)
            assertTrue("长度越过留白：$v > $maxHalf", v <= maxHalf + 0.01f)
            assertTrue("静音也要留一小截（呼吸才有东西可动）：$v", v >= tip - 0.01f)
            prev = v
        }
        assertEquals("满电平正好画到边", maxHalf, levelHalfLengthPx(maxHalf, tip, 1f), 0.01f)
    }

    // ---- 4. 尺寸与布局预算（"别让美化挤掉功能"）----

    /** 电平高度是**常量**，且在用户给的 56~72dp 之间；整段占位由它推导（尺寸只有一处来源）。 */
    @Test
    fun `电平高度是常量且由它推导整段占位`() {
        assertTrue("电平高度越界（应 56~72）：$RECORDING_LEVEL_HEIGHT_DP",
            RECORDING_LEVEL_HEIGHT_DP in 56..72)
        assertEquals(
            "电平带高度必须由'高度 + 上下留白'推导（不许在布局里另写一个数）",
            RECORDING_LEVEL_HEIGHT_DP + 2 * RECORDING_LEVEL_PADDING_DP,
            RECORDING_LEVEL_BAND_DP,
        )
        assertEquals(
            "文字区预算必须由'固定块 + 电平带'推导",
            RECORDING_CHROME_AREA_DP + RECORDING_LEVEL_BAND_DP,
            RECORDING_BASE_AREA_DP,
        )
    }

    /**
     * **页面总高度不许因为美化而增加**：新预算 358（旧版 426），且小屏（568dp）上按钮仍然完整可见。
     *
     * 反证留在这里：既有那条"十几行说明摊在主界面就装不下"的判据照旧成立
     * （见 `RecordingUiTest`）—— 预算变小不是"可以多摊点字"的理由。
     */
    @Test
    fun `录音页总高度不增且小屏按钮仍可见`() {
        assertTrue("新预算比旧版（426）还高：$RECORDING_BASE_AREA_DP", RECORDING_BASE_AREA_DP <= 426)
        assertTrue(
            "小屏（568dp）上按钮被挤出去了",
            recordingButtonsVisible(568, RECORDING_STATUS_MAX_LINES, RECORDING_ERROR_MAX_LINES),
        )
        assertFalse(
            "预算变小不等于可以把说明摊回主界面",
            recordingButtonsVisible(568, statusLines = 10),
        )
    }

    // ---- 5. 源码级判据（JVM 门禁里渲染不了 Compose，只能读源码钉住"怎么写"的）----

    /**
     * 电平组件**只用主题色**（不许写死 ARGB）、**只在绘制阶段画**、**只用帧循环驱动**。
     *
     * 三条都是这一轮点名的硬约束：
     *  * 写死色值 = 换主题时这条电平不会跟着变；
     *  * 用 Compose 的动画状态而不是绘制状态 = 每帧重组整页（大计时/状态行/按钮跟着重算）；
     *  * 用无限动画（`rememberInfiniteTransition`）同样会每帧重组。
     */
    @Test
    fun `电平组件只用主题色且只在绘制阶段画`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/RecordingLevelMeter.kt")

        assertFalse("电平组件里出现了写死的 ARGB 色值（必须走 EchoPalette）", src.contains("0xFF"))
        assertFalse("电平组件里直接 new 了颜色（必须走 EchoPalette）", src.contains("Color(0x"))
        assertTrue("配色必须来自 EchoPalette", src.contains("EchoPalette."))

        assertTrue("电平必须在绘制阶段画（drawWithCache/drawBehind）",
            src.contains("drawWithCache") && src.contains("onDrawBehind"))
        assertTrue("每帧的驱动只能是 withFrameNanos", src.contains("withFrameNanos"))
        assertFalse("不许用无限动画（会每帧重组整页）", src.contains("rememberInfiniteTransition"))
        assertFalse("不许用动画状态做平滑（会每帧重组）", src.contains("animateFloatAsState"))
    }

    /**
     * 新版电平**没有挤掉任何现有信息**：大计时 / 本段进度 / 一行状态 / 两个 56dp 按钮都还在，
     * 而且旧版那 17 根柱子已经彻底退场。
     */
    @Test
    fun `录音页仍然留全大计时本段状态行与两个按钮`() {
        val screen = moduleText("src/main/java/com/echo/contact/ui/MeetingRecordingScreen.kt")

        assertTrue("大计时没了", screen.contains("elapsedLabel("))
        assertTrue("本段进度没了", screen.contains("segmentProgressText("))
        assertTrue("状态行没了", screen.contains("recordingStatusLine("))
        assertEquals(
            "两个主按钮必须还是 56dp",
            2,
            occurrencesOf(screen, "RECORDING_BUTTON_HEIGHT_DP.dp"),
        )
        assertTrue("录音页没接上新电平组件", screen.contains("RecordingLevelMeter("))
        assertTrue("换一场要清零峰值（sessionKey）", screen.contains("sessionKey = mic.startedAt"))
        assertFalse("旧版的柱子又回来了", screen.contains("LEVEL_BARS"))
    }
}
