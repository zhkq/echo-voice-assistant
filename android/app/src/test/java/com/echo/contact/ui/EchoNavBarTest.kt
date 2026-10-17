package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 底部导航**造型**的判据（用户 2026-10-18："echo 超级助理在中间，左右各两个页签的样式，
 * 可以设计个造型"）。
 *
 * 为什么要用例钉住"造型"：**"看起来差不多大"是最容易悄悄退化的东西**。原来五颗等大的页签，
 * 谁把 `weight(1f)` 改回去、谁把中间那颗的尺寸调成和别的一样，界面都不会报错 ——
 * 只是主入口又看不见了。所以"哪颗是中间那颗 / 它多大 / 抬多高 / 窄屏放不放得下"
 * 全抽成了纯常量与纯函数（`EchoNavSpec`），在这里逐条断言。
 *
 * ⚠️ 这些用例**看不到真机**：它们只能保证"数字上中间那颗确实更大更凸、且塞得进 360dp"，
 * 保证不了"好不好看"。观感仍需真机看（这一点写在报告里，不在用例里假装）。
 */
class EchoNavBarTest {

    @Test
    fun `中间那颗是正中间那格且就是超级助理`() {
        assertEquals("5 项的正中间是第 3 格（index 2）", 2, echoCenterIndex(ECHO_TABS))
        assertEquals(EchoTab.ASSISTANT, echoCenterTab(ECHO_TABS))
        // 不是写死 ASSISTANT：换任何一档页签列表，中间那颗都是"正中间"那一个
        assertEquals(1, echoCenterIndex(listOf(EchoTab.COMMANDS, EchoTab.MEETINGS, EchoTab.REVIEW)))
        assertEquals(
            EchoTab.MEETINGS,
            echoCenterTab(listOf(EchoTab.COMMANDS, EchoTab.MEETINGS, EchoTab.REVIEW)),
        )
    }

    @Test
    fun `中间那颗的图标与触摸区都比其余四项大一档`() {
        assertTrue(
            "中间那颗的图标要大：${EchoNavSpec.CENTER_ICON_DP} vs ${EchoNavSpec.SIDE_ICON_DP}",
            EchoNavSpec.CENTER_ICON_DP > EchoNavSpec.SIDE_ICON_DP,
        )
        assertTrue(
            "中间那颗的触摸高度要大：${EchoNavSpec.CENTER_TOUCH_DP} vs ${EchoNavSpec.SIDE_TAP_HEIGHT_DP}",
            EchoNavSpec.CENTER_TOUCH_DP > EchoNavSpec.SIDE_TAP_HEIGHT_DP,
        )
        // 其余四项也不能小到点不中（无障碍最小 48dp）
        assertTrue(
            "两侧四项的触摸高度不能低于 48dp",
            EchoNavSpec.SIDE_TAP_HEIGHT_DP >= 48f,
        )
        // 图标不能贴住圆边：圆半径减图标半径至少留 8dp 的环形留白
        assertTrue(
            "中间那颗的图标贴住圆边了",
            EchoNavSpec.CENTER_PILL_DP / 2f - EchoNavSpec.CENTER_ICON_DP / 2f >= 8f,
        )
    }

    @Test
    fun `中间那颗向上凸起且约六成露在条外`() {
        assertTrue("抬升量必须 > 0", EchoNavSpec.CENTER_LIFT_DP > 0f)
        val fraction = centerProtrudeFraction()
        assertTrue(
            "凸起应占底座直径的 60~70%，实际 $fraction",
            fraction >= 0.60f && fraction <= 0.70f,
        )
        // 露在条外的那一截必须真的比条本体的"页签行"更高一点才看得出来（不是 1dp 的抖动）
        assertTrue(
            "抬升量太小，看不出「浮起来」：${EchoNavSpec.CENTER_LIFT_DP}dp",
            EchoNavSpec.CENTER_LIFT_DP >= 24f,
        )
        // 但它也不能高到把整个条撑成半屏
        assertTrue(
            "抬升量过大，导航条会占掉太多屏幕",
            EchoNavSpec.CENTER_LIFT_DP <= EchoNavSpec.BAR_HEIGHT_DP,
        )
    }

    @Test
    fun `凸起的高度算进导航条总高度（所以才不会盖住内容）`() {
        // Scaffold 拿"底部栏自身的高度"当内容底部 padding —— 总高度必须包含露在条外的那一截，
        // 否则凸起会浮在内容上（页面底部的按钮会被它压住，这正是这一轮要避免的事）。
        assertEquals(
            "总高度 = 条本体 + 抬出条外的那一截（含触摸圈的 4dp 余量）",
            EchoNavSpec.BAR_HEIGHT_DP + EchoNavSpec.CENTER_TOUCH_OFFSET_DP,
            EchoNavSpec.TOTAL_HEIGHT_DP,
            0.001f,
        )
        assertTrue(
            "总高度里必须包含抬升量",
            EchoNavSpec.TOTAL_HEIGHT_DP - EchoNavSpec.BAR_HEIGHT_DP >= EchoNavSpec.CENTER_LIFT_DP,
        )
        // 触摸圈比底座大一圈、居中套着：多出来的一半算在上方，底座露在条外的高度才等于抬升量
        assertEquals(
            EchoNavSpec.CENTER_LIFT_DP + (EchoNavSpec.CENTER_TOUCH_DP - EchoNavSpec.CENTER_PILL_DP) / 2f,
            EchoNavSpec.CENTER_TOUCH_OFFSET_DP,
            0.001f,
        )
        // 整条不能太厚（40 寸的条比页面还高就不是"导航"了）
        assertTrue(
            "底部导航总高度 ${EchoNavSpec.TOTAL_HEIGHT_DP}dp 太厚",
            EchoNavSpec.TOTAL_HEIGHT_DP <= 110f,
        )
    }

    @Test
    fun `360dp 窄屏下 5 项不重叠`() {
        val width = EchoNavSpec.NARROW_SCREEN_WIDTH_DP
        val slots = ECHO_TABS.size
        assertEquals("360dp 下每格 72dp", 72f, navSlotWidthDp(width, slots), 0.001f)
        assertTrue("360dp 下放不下", navFitsWidth(width))

        // 相邻两格首尾相接、绝不重叠（左闭右开：上一格的右边界 == 下一格的左边界）
        for (i in 0 until slots - 1) {
            assertEquals(
                "第 ${i + 1} 格与第 ${i + 2} 格必须是同一条边界",
                navSlotEndDp(i, width, slots),
                navSlotStartDp(i + 1, width, slots),
                0.001f,
            )
            assertTrue(
                "第 ${i + 1} 格压到第 ${i + 2} 格了",
                navSlotEndDp(i, width, slots) <= navSlotStartDp(i + 1, width, slots),
            )
        }
        assertEquals("5 格正好铺满", width, navSlotEndDp(slots - 1, width, slots), 0.001f)

        // 最容易漏的一条：**凸起那颗的触摸圈必须塞得进自己那一格**
        // （否则它会压到左右两项的点击区——真机上就是"点会议却回到了首页"）
        assertTrue(
            "中间那颗的触摸区 ${EchoNavSpec.CENTER_TOUCH_DP}dp 宽过自己那一格 " +
                "${navSlotWidthDp(width, slots)}dp",
            EchoNavSpec.CENTER_TOUCH_DP <= navSlotWidthDp(width, slots),
        )
        // 更窄的 320dp（小屏手机）也还塞得下，只是更挤
        assertTrue("320dp 下中间那颗会压到邻居", navFitsWidth(320f))
    }

    @Test
    fun `中间那颗横向居中且不压到邻居的格子`() {
        val width = EchoNavSpec.NARROW_SCREEN_WIDTH_DP
        val slots = ECHO_TABS.size
        // 底座的正中心 == 屏幕正中心（否则"中间那颗"就不在正中间了）
        assertEquals(
            "中间那颗必须落在屏幕正中线上",
            width / 2f,
            (centerPillStartDp(width, slots) + centerPillEndDp(width, slots)) / 2f,
            0.001f,
        )
        // 底座整个落在自己那一格里：不会压到左右两项的点击区
        val centerSlotStart = navSlotStartDp(echoCenterIndex(ECHO_TABS), width, slots)
        val centerSlotEnd = navSlotEndDp(echoCenterIndex(ECHO_TABS), width, slots)
        assertTrue(
            "底座压到左邻（底座左 ${centerPillStartDp(width, slots)}dp < 本格左 ${centerSlotStart}dp）",
            centerPillStartDp(width, slots) >= centerSlotStart,
        )
        assertTrue(
            "底座压到右邻",
            centerPillEndDp(width, slots) <= centerSlotEnd,
        )
    }

    @Test
    fun `落在超级助理页时中间那颗是激活态，切到别的页后仍居中`() {
        // 选中态只由 `tab == current` 决定：落在超级助理页 → 中间那颗 == current → 激活
        val onAssistant = EchoTab.ASSISTANT
        assertEquals("中间那颗就是当前页（所以显示激活态）", onAssistant, echoCenterTab(navTabs(onAssistant)))

        // 切到别的页：中间那颗仍在正中间（顺序不因选中而变），只是不再是激活态
        for (other in ECHO_TABS.filter { it != EchoTab.ASSISTANT }) {
            val tabs = navTabs(other)
            assertEquals("顺序不能因为选中页变了而重排", ECHO_TABS, tabs)
            assertEquals("中间那颗必须还在 index 2", 2, tabs.indexOf(EchoTab.ASSISTANT))
            assertNotEquals("别的页不该把超级助理当成当前页", other, echoCenterTab(tabs))
        }
    }
}
