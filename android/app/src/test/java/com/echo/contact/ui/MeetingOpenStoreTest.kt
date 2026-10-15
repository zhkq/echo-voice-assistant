package com.echo.contact.ui

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 通知 → 界面 的那次交接（任务 B8 的**冷/热启动**两条路共用它）。
 *
 * 为什么要单独测这个 object：`MainActivity` 自己是要真机的（JVM 单测里跑不了），
 * 于是"冷启动与热启动是不是都到了同一个地方"就只能靠这层来保证 ——
 * `onCreate`（冷）与 `onNewIntent`（热）都只调 `put`，界面只调一次 `consume`。
 * 这里钉住的就是这两点，以及"消费一次就没了"（否则转屏/重复投递会把同一场会议
 * 打开两遍，用户会看到页面自己跳回去又跳回来）。
 */
class MeetingOpenStoreTest {

    @Before
    fun setUp() = MeetingOpenStore.clear()

    @After
    fun tearDown() = MeetingOpenStore.clear()

    @Test
    fun `冷启动与热启动都只是 put 进同一个 store`() {
        // 冷启动：onCreate 里 put
        MeetingOpenStore.put("2026-10-16_09-00-00")
        val cold = MeetingOpenStore.consume()
        assertEquals("冷启动这条路要能取到目标", "2026-10-16_09-00-00", cold?.name)

        // 热启动：onNewIntent 里 put
        MeetingOpenStore.put("2026-10-16_10-00-00")
        val warm = MeetingOpenStore.consume()
        assertEquals("热启动这条路要能取到目标", "2026-10-16_10-00-00", warm?.name)
    }

    @Test
    fun `消费一次就没了`() {
        MeetingOpenStore.put("某场会")
        assertEquals("某场会", MeetingOpenStore.consume()?.name)
        assertNull("消费过就不该再有：否则转屏/重复投递会再打开一遍", MeetingOpenStore.consume())
    }

    @Test
    fun `每次 put 的序号都往前走`() {
        // seq 的用途：StateFlow 同值不重发，而"同一个会议名再点一次"必须被当成新的一次。
        // 序号不涨 = 第二次点同一条通知没反应。
        MeetingOpenStore.put("同一场")
        val first = MeetingOpenStore.consume()!!
        MeetingOpenStore.put("同一场")
        val second = MeetingOpenStore.consume()!!
        assertEquals("同名的第二次必须是新请求", "同一场", second.name)
        assertTrue("序号必须递增：${first.seq} → ${second.seq}", second.seq > first.seq)
    }

    @Test
    fun `空白名字不该留下一个空目标`() {
        // 空目标会走到"找不到那场会议（）"这种糊涂提示上，不如当没收到。
        MeetingOpenStore.put("   ")
        assertNull("空白目标等同没收到", MeetingOpenStore.consume())
    }

    @Test
    fun `名字两侧的空白会被去掉`() {
        MeetingOpenStore.put("  2026-10-16_09-00-00  ")
        assertEquals("2026-10-16_09-00-00", MeetingOpenStore.consume()?.name)
    }
}
