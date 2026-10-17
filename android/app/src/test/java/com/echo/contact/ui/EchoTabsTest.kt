package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 底部导航 **5 页**的判据（用户 2026-10-18 点名）。
 *
 * 为什么这些也要用例：页签数量、名称、顺序、以及"三个主入口到底切到哪一页"
 * 全是**用户点名的界面契约**。少一个页 / 顺序变了 / 某个入口指到不存在的页，
 * 在真机上表现就是"点了没反应"，而且很难从代码里一眼看出来。
 */
class EchoTabsTest {

    @Test
    fun `恰好 5 个页签且名称顺序是用户点名的`() {
        assertEquals("用户要的是 5 页，不多不少", 5, ECHO_TABS.size)
        assertEquals(
            listOf("超级助理", "指令", "会议", "回顾", "设置"),
            ECHO_TABS.map { it.title },
        )
    }

    @Test
    fun `页签序号连续且从 0 开始`() {
        // 底部导航按 ordinal 渲染、ViewModel 按 ordinal 切页：中间断号会出现"点不中的页"
        ECHO_TABS.forEachIndexed { index, tab ->
            assertEquals("第 ${index + 1} 页的 ordinal 应该是 $index", index, tab.ordinal)
        }
        assertEquals(0, EchoTab.ASSISTANT.ordinal)
        assertEquals(4, EchoTab.SETTINGS.ordinal)
    }

    @Test
    fun `三个主入口指向真实存在的页`() {
        assertEquals("超级助理页上就是三个入口", 3, ASSISTANT_ENTRIES.size)
        assertEquals(
            listOf("指令", "会议", "回顾"),
            ASSISTANT_ENTRIES.map { it.title },
        )
        assertEquals(
            listOf(EchoTab.COMMANDS, EchoTab.MEETINGS, EchoTab.REVIEW),
            ASSISTANT_ENTRIES.map { it.tab },
        )
        for (entry in ASSISTANT_ENTRIES) {
            assertTrue("入口指向一个不存在的页：${entry.tab}", entry.tab in ECHO_TABS)
            assertNotEquals("主入口不该指向超级助理自己", EchoTab.ASSISTANT, entry.tab)
            assertNotEquals("主入口不该指向设置页", EchoTab.SETTINGS, entry.tab)
            assertTrue("入口说明不能是空的：${entry.title}", entry.subtitle.isNotBlank())
        }
    }

    @Test
    fun `每个页签都有可读的文案（不会被画成空白项）`() {
        for (tab in ECHO_TABS) {
            assertTrue("${tab.name} 没有标题", tab.title.isNotBlank())
            // 中文名，长度不会很长；太长会被底部导航截断
            assertTrue("页签名太长，底部导航会截断：${tab.title}", tab.title.length <= 4)
        }
    }
}
