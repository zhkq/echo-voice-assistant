package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 听写参数的边界（`ListenTuning`）—— 纯 JVM。
 *
 * 这一组挡的是**静默坏掉**：界面上滑到 0.5 的门限、或者存进一个负数分钟数，
 * 表现都不是报错，而是"它永远觉得有人在说"或"开着就立刻自己停"。
 * 所以边界口径只能有一处（[ListenTuning.clamped]），并在这里逐条钉住。
 */
class ListenTuningTest {

    @Test
    fun `默认值就是安静房间那一套`() {
        val d = ListenTuning()
        assertEquals(0.012, d.silenceThreshold, 0.0001)
        assertEquals(1200, d.hangoverMs)
        assertEquals(20, d.idleAutoStopMin)
        assertEquals(
            "全双工默认必须关：它依赖设备 AEC，属于要真机确认过再开的档",
            false,
            d.keepListeningWhileSpeaking,
        )
        assertEquals(d, d.clamped())
    }

    @Test
    fun `全双工开关过钳制后保持原值`() {
        val t = ListenTuning(keepListeningWhileSpeaking = true, hangoverMs = 99_999).clamped()
        assertEquals(true, t.keepListeningWhileSpeaking)
        assertEquals(ListenTuning.MAX_HANGOVER_MS, t.hangoverMs)
    }

    @Test
    fun `门限被夹在可用区间里`() {
        assertEquals(
            ListenTuning.MIN_SILENCE_THRESHOLD,
            ListenTuning(silenceThreshold = -1.0).clamped().silenceThreshold,
            0.0001,
        )
        assertEquals(
            ListenTuning.MAX_SILENCE_THRESHOLD,
            ListenTuning(silenceThreshold = 0.5).clamped().silenceThreshold,
            0.0001,
        )
        // 区间内原样保留
        assertEquals(0.03, ListenTuning(silenceThreshold = 0.03).clamped().silenceThreshold, 0.0001)
    }

    @Test
    fun `收尾静音被夹在可用区间里`() {
        assertEquals(ListenTuning.MIN_HANGOVER_MS, ListenTuning(hangoverMs = 10).clamped().hangoverMs)
        assertEquals(ListenTuning.MAX_HANGOVER_MS,
            ListenTuning(hangoverMs = 60_000).clamped().hangoverMs)
    }

    @Test
    fun `自动停的分钟数与毫秒换算对得上`() {
        assertEquals(5 * 60_000L, ListenTuning(idleAutoStopMin = 5).clamped().idleAutoStopMs)
        assertEquals(
            ListenTuning.MIN_IDLE_AUTO_STOP_MIN,
            ListenTuning(idleAutoStopMin = 0).clamped().idleAutoStopMin,
        )
        assertEquals(
            ListenTuning.MAX_IDLE_AUTO_STOP_MIN,
            ListenTuning(idleAutoStopMin = 9_999).clamped().idleAutoStopMin,
        )
    }

    @Test
    fun `钳制后的值再过一遍钳制不变`() {
        val once = ListenTuning(silenceThreshold = 1.0, hangoverMs = -5, idleAutoStopMin = 0).clamped()
        assertEquals(once, once.clamped())
        assertTrue(once.idleAutoStopMs > 0)
    }
}
