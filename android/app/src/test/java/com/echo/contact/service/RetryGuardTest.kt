package com.echo.contact.service

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 「同一段别传两遍」那条路上的**不可重入判据**（`RetryGuard`）与"结束前那次收尾被挡了怎么说"
 * （`stopRetryNote`）—— 两者都是纯逻辑，JVM 可测。
 *
 * 为什么这件小事值得一组用例：同一段音频被传两次**在手机这边完全静默**
 * （界面照样说"传完了"），代价是电脑侧多存一段、转写里同一段出现两遍。
 * 而同步有**两个**入口（每封一段顺手试一次、结束前那次收尾），
 * 保护只写在其中一个上就等于没写 —— 所以判据只能有一处，用例就钉那一处。
 */
class RetryGuardTest {

    @Test
    fun `第一轮进得去、leave 之后还能再进`() {
        val g = RetryGuard()
        assertFalse("新建的守卫不该是'在跑'", g.running)
        assertTrue("第一轮必须进得去", g.tryEnter())
        assertTrue(g.running)
        assertFalse("同一轮里再进一次也要被挡（判据按'轮'算，不是按调用者）", g.tryEnter())
        g.leave()
        assertFalse("leave() 之后就不该再算'在跑'", g.running)
        assertTrue("下一轮重传必须还能进（否则一次重传之后就永久锁死了）", g.tryEnter())
        g.leave()
    }

    @Test
    fun `并发第二次被挡、如实回 false`() {
        val g = RetryGuard()
        // 主线程先占住 = "上一轮重传正在跑"
        assertTrue(g.tryEnter())

        val got = AtomicBoolean(true)
        val ranIt = CountDownLatch(1)
        val t = Thread {
            got.set(g.tryEnter())
            ranIt.countDown()
        }
        t.start()
        try {
            assertTrue("第二个线程没能跑起来（这是用例本身的问题）", ranIt.await(5, TimeUnit.SECONDS))
            assertFalse("另一轮在跑时，第二次必须**立刻**拿到 false（不排队重跑）", got.get())
        } finally {
            t.join(5_000)
        }
        assertTrue("被挡的那一方不许动别人的守卫", g.running)
        g.leave()
        assertTrue("放掉之后又能进（被挡不是'坏了'）", g.tryEnter())
        g.leave()
    }

    @Test
    fun `被挡的那一轮真的没跑、也不许放掉别人的守卫`() = runBlocking {
        val g = RetryGuard()
        assertTrue(g.tryEnter())
        var ran = false
        val out = g.runExclusive { ran = true }
        assertEquals("被挡的结果必须是 SkippedBusy（调用方据此如实说话）",
            RetryOutcome.SkippedBusy, out)
        assertFalse("被挡就不能偷偷跑一遍", ran)
        assertTrue("更不许把占着的那一方放掉（那会让两轮同时在跑）", g.running)
        g.leave()
    }

    @Test
    fun `runExclusive 跑完就放掉、并如实回 Ran`() = runBlocking {
        val g = RetryGuard()
        var ran = false
        val out = g.runExclusive { ran = true }
        assertEquals(RetryOutcome.Ran, out)
        assertTrue("block 得真的跑过", ran)
        assertFalse("跑完必须放掉（下一段上传前那一轮才进得来）", g.running)
    }

    @Test
    fun `异常路径也会 leave、不会把同步永久锁死`() = runBlocking {
        val g = RetryGuard()
        try {
            g.runExclusive { throw IllegalStateException("上传抛了") }
            org.junit.Assert.fail("block 的异常必须透出来，不能被吞掉")
        } catch (e: IllegalStateException) {
            assertEquals("上传抛了", e.message)
        }
        assertFalse("抛异常也要 leave()（try/finally 保证）—— 否则守卫永远是'在跑'", g.running)
        assertTrue("所以下一轮还能进（界面上「现在同步」不会变成按了没反应）", g.tryEnter())
        g.leave()
    }

    // ---- 有界等待那一档（"结束录音"时那一次收尾用）----

    @Test
    fun `等待那一档：占着的一方放掉之后能进去并真跑`() = runBlocking {
        val g = RetryGuard()
        assertTrue(g.tryEnter())                 // 模拟"一轮机会性上传正在跑"
        var ran = false
        val t = Thread {
            Thread.sleep(120)
            g.leave()                            // 它跑完了
        }
        t.start()
        val out = g.runExclusiveWaiting(timeoutMs = 5_000, stepMs = 20) { ran = true }
        t.join(5_000)
        assertEquals(RetryOutcome.Ran, out)
        assertTrue("等到了就**必须真跑**（不然'结束'那一下的收尾就白等了）", ran)
        assertFalse("跑完要放掉", g.running)
    }

    @Test
    fun `等待那一档：等不到就如实回 SkippedBusy 而且不许偷偷跑`() = runBlocking {
        val g = RetryGuard()
        assertTrue(g.tryEnter())                 // 另一方一直占着（比如网络卡住）
        var ran = false
        val out = g.runExclusiveWaiting(timeoutMs = 200, stepMs = 20) { ran = true }
        assertEquals("等不到就说'这次没跑'（绝不假装传完了）", RetryOutcome.SkippedBusy, out)
        assertFalse("被挡就不能偷偷跑一遍", ran)
        assertTrue("更不许把占着的那一方放掉", g.running)
        g.leave()
    }

    // ---- 结束前那次补传的结局 → 如实的一句话 ----

    @Test
    fun `结束前补传被挡时如实说出还有几段`() {
        val s = stopRetryNote(RetryOutcome.SkippedBusy, left = 3)
        assertTrue("必须说清'这一次没跑'（不是'传失败了'—— 说错原因会把人引去查网络）：$s",
            s.contains("没跑"))
        assertTrue("必须说清还有几段留着：$s", s.contains("3 段"))
        assertTrue("必须交代下次还会试（否则用户只能干看着那几个数字）：$s", s.contains("下次开录"))
    }

    @Test
    fun `结束前补传真跑了但没传完也要说清`() {
        val s = stopRetryNote(RetryOutcome.Ran, left = 2)
        assertTrue("还有欠的段就必须说：$s", s.contains("2 段"))
        assertTrue("但这是'没传完'，不是'没跑'：$s", s.contains("没传完"))
    }

    @Test
    fun `没有欠的段就什么都不说`() {
        assertEquals("别用套话占住界面上那一行",
            "", stopRetryNote(RetryOutcome.Ran, left = 0))
        assertEquals("", stopRetryNote(RetryOutcome.SkippedBusy, left = 0))
    }
}
