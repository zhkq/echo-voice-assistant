package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 全屏录音界面里那几个**纯函数**的判据（电平平滑、柱高、颜色、计时）。
 *
 * 为什么这些也值得用例：它们决定了"看起来对不对"——
 *  * 平滑系数写反了（回落快、攻击慢）→ 电平会像抽搐；
 *  * 计时解析失败时**回 00:00** 而不是 `--:--` → 用户会以为"刚开始录"，而真相是读不懂字段；
 *  * 柱高不夹住 → 电平一高就画出屏幕。
 */
class RecordingUiTest {

    @Test
    fun `电平攻击快回落慢`() {
        // 从 0 往 1 涨：一口气走掉一大半（跟得上说话）
        val up = smoothLevel(0f, 1f)
        assertTrue("涨得太慢：$up", up >= 0.6f)
        // 从 1 往 0 落：只落一小截（看着像余响，不像"啪一下没了"）
        val down = smoothLevel(1f, 0f)
        assertTrue("落得太快：$down", down >= 0.7f)
    }

    @Test
    fun `平滑不会越界`() {
        for (p in listOf(-1f, 0f, 0.5f, 1f, 2f)) {
            for (t in listOf(-1f, 0f, 0.5f, 1f, 2f)) {
                val v = smoothLevel(p, t)
                assertTrue("越界：$v（prev=$p target=$t）", v in 0f..1f)
            }
        }
    }

    @Test
    fun `柱子中间高两边低`() {
        val mid = barWeight(LEVEL_BARS / 2)
        val edge = barWeight(0)
        assertEquals("最中间那根该是最高的", 1f, mid, 0.001f)
        assertTrue("两端必须比中间矮：edge=$edge mid=$mid", edge < mid)
        assertTrue("两端也别矮成 0（否则电平低时看起来像坏了）", edge > 0f)
    }

    @Test
    fun `颜色跟着强度走`() {
        assertEquals("弱电平用青色", com.echo.contact.ui.theme.EchoPalette.Accent, barColor(0.1f, 1f))
        assertEquals("中等电平用紫", com.echo.contact.ui.theme.EchoPalette.Purple, barColor(0.5f, 1f))
        assertEquals("强电平用红", com.echo.contact.ui.theme.EchoPalette.Red, barColor(0.9f, 1f))
    }

    @Test
    fun `计时按开始时间算`() {
        val start = "2026-10-15T23:00:00"
        val base = java.time.LocalDateTime.parse(start)
            .atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli()
        assertEquals("00:07", elapsedText(start, base + 7_000))
        assertEquals("01:00", elapsedText(start, base + 60_000))
        assertEquals("1:02:03", elapsedText(start, base + ((1 * 3600 + 2 * 60 + 3) * 1000L)))
    }

    @Test
    fun `读不懂的开始时间回 -- -- 而不是 00 00`() {
        // 编一个 00:00 会让人以为"刚开始录"，而真相是那个字段读不懂 —— 宁可空缺
        for (bad in listOf(null, "", "  ", "not-a-time", "2026-13-45T99:99:99")) {
            assertEquals("bad=$bad", "--:--", elapsedText(bad))
        }
    }

    // ---- 累计时长（用户 2026-10-17 真机问题①：暂停时计时器也不刷新时间了）----

    /**
     * 计时**不许**在暂停期间冻住。
     *
     * 现行口径里暂停 = 照常录音、写全零、**总时长不受影响**，所以界面上那行数字必须一直走
     * （旧版界面只在电平变化时重组，而暂停时刻意不刷电平 → 计时看上去停了，用户于是
     * 误以为"这段时间被抹掉了"，进而把恢复后封的那一段看成"提前封"）。
     *
     * 这里钉的是那条**纯函数**：界面上显示的秒数取"录制循环报的挂钟秒数"与
     * "按开始时间算到此刻"的较大者 —— 两个都是挂钟，所以暂停期间只会越来越大。
     */
    @Test
    fun `暂停期间计时照挂钟走`() {
        val start = "2026-10-15T23:00:00"
        val base = java.time.LocalDateTime.parse(start)
            .atZone(java.time.ZoneId.systemDefault()).toInstant().toEpochMilli()

        // 录制循环每帧都会推进 stateSeconds（暂停也推进）：这里模拟"暂停到第 95 秒"
        assertEquals("暂停到 95 秒：界面显示 01:35", "01:35", elapsedLabel(95.0, start, base + 95_000))
        // 状态那一格还没刷到（某一帧没来）时，按开始时间算也不许停
        assertEquals("状态只到 40 秒、挂钟已经 95 秒：仍显示 01:35",
            "01:35", elapsedLabel(40.0, start, base + 95_000))
        // 开始时间读不懂也不编 00:00：只有循环真的报过秒数才用它
        assertEquals("读不懂的开始时间 + 有状态秒数", "00:07", elapsedLabel(7.0, "bad", base))
        assertEquals("读不懂的开始时间 + 没有秒数", "--:--", elapsedLabel(0.0, "bad", base))
    }

    /**
     * "本段 00:12 / 00:30"（段长未知时**不编数字**）。
     *
     * 为什么这条要出现在界面上：段边界只由累计样本数决定，暂停的零样本照常计数 ——
     * 所以暂停期间这一段也在攒。用户看不见这件事时，恢复后立刻封一段就会被当成
     * "不到一分钟就封段了"（真机问题②）；把进度摆出来，那一段是"暂停期间攒满的"就一目了然。
     */
    @Test
    fun `本段进度按段长显示`() {
        assertEquals("本段 00:12 / 00:30", "本段 00:12 / 00:30", segmentProgressText(12.0, 30))
        assertEquals("攒满就显示满格（不越过段长）", "本段 00:30 / 00:30",
            segmentProgressText(45.0, 30))
        assertEquals("还没读过设置时不编数字", "", segmentProgressText(3.0, 0))
        assertEquals("极短的一帧也要显示 00:00 而不是空白", "本段 00:00 / 01:00",
            segmentProgressText(0.0, 60))
    }

    // ---- 2026-10-18 收口：全屏录音页去啰嗦（用户：文字把按钮盖住了）----

    /** 暂停时标题必须说清"时长还在累计"（暂停 = 照常录、写零、总时长不变）。 */
    @Test
    fun `暂停时标题说时长在累计`() {
        assertEquals("已暂停 · 时长在累计", recordingTitle(true))
        assertEquals("录音中", recordingTitle(false))
    }

    /**
     * 主界面**只有一行**状态，且没有待传就不提待传。
     *
     * 这一条钉的是"去啰嗦"：旧版那几句长解释（"这一场还在录：已经录到的段都在服务端了…"）
     * 不许再回到状态行里 —— 它们只能在 ⓘ 弹窗里（见下一条用例）。
     */
    @Test
    fun `状态行只有一行且没有待传就不提`() {
        assertEquals("本机录音中 · 已缓存 1 段 · 1 段待传",
            recordingStatusLine(cached = 1, pending = 1))
        assertEquals("没有待传就不许提待传", "本机录音中 · 已缓存 3 段",
            recordingStatusLine(cached = 3, pending = 0))
        assertEquals("一段都没攒到时只留一句，不编 0 段", "本机录音中",
            recordingStatusLine(cached = 0, pending = 0))
        assertEquals("只有待传、一段都还没缓存时也要说得清",
            "本机录音中 · 1 段待传", recordingStatusLine(cached = 0, pending = 1))

        for ((c, p) in listOf(1 to 1, 3 to 0, 0 to 0, 12 to 3)) {
            val line = recordingStatusLine(c, p)
            assertTrue("状态行太长，会挤掉计时/按钮：$line", line.length <= 30)
            assertFalse("状态行里不许夹带解释句（句号/分号是说明文）：$line",
                line.contains("。") || line.contains("；"))
        }
    }

    /** 解释性内容**不许丢**：它们全部收进 ⓘ 弹窗（用户：收到帮助里或 ⓘ 里）。 */
    @Test
    fun `说明内容收进 ⓘ 不丢`() {
        val lines = recordingInfoLines(
            paused = true,
            syncNote = "ECHO 连不上，音频先存在这台手机里",
            formatNote = "第 2 段用 WAV（FLAC 编码器不可用）",
            note = "已开始录音",
            segmentSeconds = 600,
        )
        val text = lines.joinToString("\n")
        assertTrue("返回只关界面、不停录音要说到", text.contains("返回只关这个界面"))
        assertTrue("先存手机 / 联网补传要说到",
            text.contains("先存在手机里") && text.contains("自动补传"))
        assertTrue("滞后多久要按当前段长说（默认 600 秒 = 10:00）", text.contains("10:00"))
        assertTrue("暂停语义要说到", text.contains("写空白") && text.contains("时长照常累计"))
        assertTrue("结束补尾段要说到", text.contains("尾段"))
        assertTrue("补传的如实说明不许丢", text.contains("ECHO 连不上"))
        assertTrue("格式的如实说明不许丢", text.contains("WAV"))
    }

    /**
     * **布局断言**：文字区 + 按钮必须装进屏幕（按钮完整可见、不被文字挤掉）。
     *
     * 小屏（568dp 高，老机型）上"一行状态 + 两行错误 + 56dp 按钮"也要装得下；
     * 反证：旧版那种把十几行说明摊在主界面的方案在这块屏幕上装不下 ——
     * 那正是用户截图里"文字盖住按钮"的成因。
     */
    @Test
    fun `录音页文字预算给按钮留出完整高度`() {
        assertTrue("小屏装不下：按钮会被挤出屏幕",
            recordingButtonsVisible(568, RECORDING_STATUS_MAX_LINES, RECORDING_ERROR_MAX_LINES))
        assertTrue(recordingButtonsVisible(640, RECORDING_STATUS_MAX_LINES))
        assertFalse("十几行说明摊在主界面就装不下了（必须收进 ⓘ）",
            recordingButtonsVisible(568, statusLines = 10))
    }

    /** 段长默认 600 秒（与电脑一致）时，本段进度要显示 `/ 10:00`。 */
    @Test
    fun `默认段长 600 秒时本段显示 10 00`() {
        assertEquals("本段 00:12 / 10:00", segmentProgressText(12.0, 600))
        assertEquals("本段 10:00 / 10:00", segmentProgressText(600.0, 600))
        assertEquals("超过段长也不许显示 11:00", "本段 10:00 / 10:00",
            segmentProgressText(700.0, 600))
    }
}
