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
}
