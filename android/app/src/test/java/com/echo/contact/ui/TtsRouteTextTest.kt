package com.echo.contact.ui

import com.echo.contact.audio.TtsRoute
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 设置页那行「上次 ECHO 念：……」怎么说（`TtsRouteText.line`，纯函数）。
 *
 * 为什么要这组用例：用户设了"ECHO 念优先用蓝牙"之后，**系统到底收没收下这个偏好**
 * 原本在界面上完全看不到 —— 而"设了没生效"与"设了、系统没听"看起来一模一样。
 * 这一行是它唯一的出口，所以两种边界都必须有话说，而且**不许说过头**：
 * Android 没有公开 API 能让 App 确认实时路由，写"正在用蓝牙"就是骗人。
 */
class TtsRouteTextTest {

    /** 四句话都要过的同一条底线：App 无法确认"此刻声音从哪支出去了"。 */
    private fun assertNoOverclaim(s: String) {
        for (banned in listOf("正在用", "正在从", "已经在用")) {
            assertFalse("App 看不到实时路由，不能说「$banned」：$s", s.contains(banned))
        }
    }

    @Test
    fun `从没念过时说还没有事实可讲`() {
        val s = TtsRouteText.line(null)
        assertTrue("从没播过就要如实说没念过，不能拿偏好冒充结果：$s", s.contains("还没念过"))
        assertTrue("得说清念过之后这里会有结果：$s", s.contains("系统"))
        assertNoOverclaim(s)
    }

    @Test
    fun `没指定设备时说那次按系统默认播的`() {
        val r = TtsRoute(preferred = false, deviceLabel = "", applied = false, note = "这次播的是系统默认输出（没选设备）")
        val s = TtsRouteText.line(r)
        assertTrue("没选设备必须说没指定：$s", s.contains("没有指定"))
        assertTrue("并说清那次是系统默认：$s", s.contains("系统默认"))
        assertFalse("没设过偏好就不能说'系统接受了'：$s", s.contains("接受了"))
        assertNoOverclaim(s)
    }

    @Test
    fun `系统接受了偏好时如实说接受、并交代最终路由由系统定`() {
        val r = TtsRoute(
            preferred = true, deviceLabel = "蓝牙音箱/耳机", applied = true,
            note = "已把你选的「蓝牙音箱/耳机」设为偏好；最终走哪支由系统定（蓝牙/听筒尤其常见）")
        val s = TtsRouteText.line(r)
        assertTrue("得带上用户认得出的设备名：$s", s.contains("蓝牙音箱/耳机"))
        assertTrue("setPreferredDevice 回 true 就是收下了，如实说：$s", s.contains("系统接受了"))
        assertTrue("紧接着必须交代'最终是否真走它由系统定'（不能让人以为一定从它出声）：$s",
            s.contains("由系统定"))
        assertNoOverclaim(s)
    }

    @Test
    fun `系统没接受偏好时如实说没接受`() {
        val r = TtsRoute(
            preferred = true, deviceLabel = "手机听筒", applied = false,
            note = "系统没接受「手机听筒」这个偏好，这次按系统默认播（不算错误，下次播放会再试）")
        val s = TtsRouteText.line(r)
        assertTrue("得带上设备名：$s", s.contains("手机听筒"))
        assertTrue("回 false 就是没设上，不许含糊：$s", s.contains("没接受"))
        assertTrue("得说清那次按系统默认播了：$s", s.contains("系统默认"))
        assertFalse("没接受就不能写成接受：$s", s.contains("系统接受了"))
        assertNoOverclaim(s)
    }
}
