package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 常驻听写的省电自动停（`IdleAutoStop`）—— 纯 JVM。 */
class IdleAutoStopTest {

    private val limit = 20 * 60 * 1000L

    @Test
    fun `没开始就不该判停`() {
        val idle = IdleAutoStop(limit)
        assertFalse(idle.shouldStop(limit * 10))
        assertEquals(0L, idle.idleMs(limit * 10))
    }

    @Test
    fun `到点才停`() {
        val idle = IdleAutoStop(limit)
        idle.start(1_000)
        assertFalse("还差一毫秒", idle.shouldStop(1_000 + limit - 1))
        assertTrue(idle.shouldStop(1_000 + limit))
    }

    @Test
    fun `说过一句就把计时器推后`() {
        val idle = IdleAutoStop(limit)
        idle.start(0)
        idle.markActivity(limit - 1_000)              // 快到点了，这时候说了一句
        assertFalse(idle.shouldStop(limit))
        assertTrue(idle.shouldStop(limit - 1_000 + limit))
    }

    @Test
    fun `停下来的原因带分钟数且是人话`() {
        val idle = IdleAutoStop(limit)
        idle.start(0)
        val why = idle.stopReason(limit + 61_000)     // 21 分钟多一点
        assertTrue("要说清听了多久：$why", why.contains("21"))
        assertTrue("要告诉用户怎么办：$why", why.contains("省电"))
        assertFalse(why.isBlank())
    }

    @Test
    fun `时钟倒着走也不会算出负数`() {
        val idle = IdleAutoStop(limit)
        idle.start(10_000)
        assertEquals(0L, idle.idleMs(5_000))
        assertFalse(idle.shouldStop(5_000))
    }
}
