package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 钉住「引擎没给最终结果时用什么」这条规则 —— 它是"点说完了没反应"那个 bug 的核心。
 *
 * 真实症状（2026-10-09 真机）：点「说完了」什么都不发生，界面一直停在"在听…"。
 * 因为 `stopListening()` 之后引擎**可能一个回调都不给**，而原来的代码只在
 * `onFinal` / `onError` 里收尾 —— 两边都不来，就永远没有收尾。
 */
class RecognizeOutcomeTest {

    @Test
    fun `有最终结果就用最终结果`() {
        val r = resolveRecognizeOutcome(final = "明天天气怎么样", partial = "明天天气")

        assertEquals("明天天气怎么样", r?.text)
        assertFalse("不该标成临时结果", r!!.usedPartial)
    }

    @Test
    fun `没有最终结果但有临时结果 就用临时的并标注`() {
        val r = resolveRecognizeOutcome(final = "", partial = "明天天气怎么样")

        assertEquals("明天天气怎么样", r?.text)
        assertTrue("必须标注这是临时结果兜的底", r!!.usedPartial)
    }

    @Test
    fun `两边都没有 返回 null（界面要说没听清，而不是静默）`() {
        assertNull(resolveRecognizeOutcome(final = "", partial = ""))
        assertNull(resolveRecognizeOutcome(final = "   ", partial = "\n"))
    }

    @Test
    fun `最终结果两边空白时不算数`() {
        val r = resolveRecognizeOutcome(final = "  ", partial = "半句话")

        assertEquals("半句话", r?.text)
        assertTrue(r!!.usedPartial)
    }

    @Test
    fun `结果两端空白会被去掉`() {
        val r = resolveRecognizeOutcome(final = "  你好  ", partial = "")

        assertEquals("你好", r?.text)
    }
}
