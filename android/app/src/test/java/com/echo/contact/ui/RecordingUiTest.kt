package com.echo.contact.ui

import org.junit.Assert.assertEquals
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
}
