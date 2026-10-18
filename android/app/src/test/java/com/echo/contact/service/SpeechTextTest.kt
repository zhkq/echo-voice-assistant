package com.echo.contact.service

import com.echo.contact.ui.functionBody
import com.echo.contact.ui.moduleText
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「8-19 度被念成 819 度」这条的判据（用户 2026-10-18 傍晚：*"朗读气温的时候 8-19 度，
 * 总被念成 819 度，这个有修复的可能吗"*）。
 *
 * 修法是在**念之前**做文本归一化（[SpeechText]），所以这里钉三件事：
 *
 *  ① **该换的都换**：温度区间的几种写法（`-` / `~` / `—` / `–`、有无空格）都要变成"到"；
 *  ② **不该换的一个都不许换**：日期 `10-18`、版本号 `v2.1.7`、零下 `-5 度` ——
 *     这几类是"过度归一化"最容易踩的坑（把日期念成"10 到 18"同样是错的）；
 *  ③ **只改念法、不改原文**：归一化在**送引擎/发请求**之前发生，屏幕上与通知里仍是原文。
 */
class SpeechTextTest {

    /** 每一行：输入 → 期望（表格驱动；加一条只加一行）。 */
    private val cases: List<Pair<String, String>> = listOf(
        // ---- ① 该换的 ----
        "8-19度" to "8 到 19度",
        "8 - 19 ℃" to "8 到 19 ℃",
        "8~19%" to "8 到 19%",
        "8—19度" to "8 到 19度",
        "8–19度" to "8 到 19度",
        "8－19度" to "8 到 19度",
        "8～19度" to "8 到 19度",
        "今天 8-19 度，明天 20-25 度" to "今天 8 到 19 度，明天 20 到 25 度",
        "连续两个区间 8-19度，20-25度" to "连续两个区间 8 到 19度，20 到 25度",
        "8-19 度左右" to "8 到 19 度左右",
        "8.5-19.5度" to "8.5 到 19.5度",
        "8-19km" to "8 到 19km",
        "8-19公里" to "8 到 19公里",
        "8-19度" to "8 到 19度",
        // 单位不许被吃掉：度/%/℃ 都要原样留着
        "8-19℃" to "8 到 19℃",
        "8 - 19 °C" to "8 到 19 °C",
        // ---- ② 不该换的 ----
        "-5 度" to "-5 度",
        "最低 -5 度" to "最低 -5 度",
        "最低-5度" to "最低-5度",
        "-5-3度" to "-5-3度",
        "10-18" to "10-18",
        "10 - 18" to "10 - 18",
        "2026-10-18" to "2026-10-18",
        "v2.1.7" to "v2.1.7",
        "2.1.7" to "2.1.7",
        "8-19" to "8-19",
        "第8-19条" to "第8-19条",
        "18-25岁" to "18-25岁",
    )

    @Test
    fun `表格驱动：该换的换 不该换的不动`() {
        for ((input, want) in cases) {
            assertEquals("「$input」念成了别的", want, SpeechText.normalize(input))
        }
    }

    @Test
    fun `幂等：同一句归一化两遍和一遍一样`() {
        for ((input, want) in cases) {
            val once = SpeechText.normalize(input)
            assertEquals("「$input」被归一化了两次就变了（会出现「到 到」）", once, SpeechText.normalize(once))
            assertEquals(want, once)
        }
    }

    @Test
    fun `单位照旧 一个字节都不动`() {
        // 只替换"左数字 分隔符 右数字"这一小段：单位与它后面的字原样保留
        assertEquals("8 到 19 度左右（体感）", SpeechText.normalize("8-19 度左右（体感）"))
        assertEquals("8 到 19% 以上", SpeechText.normalize("8-19% 以上"))
        assertEquals("湿度 8 到 19 %", SpeechText.normalize("湿度 8-19 %"))
    }

    @Test
    fun `日期念成到也是错的 所以必须有单位才改`() {
        // 用户明确点名的那条：`10-18` 是日期，不是区间
        assertFalse("没有单位的两数不许当成区间", SpeechText.hasRange("10-18"))
        assertFalse("版本号里没有区间分隔符", SpeechText.hasRange("v2.1.7"))
        assertFalse("零下温度不是区间", SpeechText.hasRange("-5 度"))
        assertTrue("带了温度单位就是区间", SpeechText.hasRange("8-19度"))
        assertTrue(SpeechText.hasRange("8 ~ 19 %"))
    }

    @Test
    fun `空串与没有数字的句子原样返回`() {
        assertEquals("", SpeechText.normalize(""))
        val plain = "明天北京多云，最高十八度。"
        assertEquals(plain, SpeechText.normalize(plain))
    }

    // ---- ③ 接线：只改念的那串，不改原文 ------------------------------------------------

    @Test
    fun `系统 TTS 实际念的是归一化后的那串`() {
        val src = moduleText("src/main/java/com/echo/contact/audio/SystemTts.kt")
        val body = functionBody(src, "fun speak(text: String", "fun stop()")
        assertTrue(
            "手机念之前没有归一化：8-19 还会被念成 819",
            body.contains("val spoken = SpeechText.normalize(text)"),
        )
        assertTrue("归一化完了却还念原文", body.contains("engine.speak(spoken,"))
        assertFalse("不许把归一化结果写回调用方/原文", body.contains("text = SpeechText.normalize"))
    }

    @Test
    fun `ECHO 合成那条也归一化（它把文本发给服务端）`() {
        val vm = moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")
        val body = functionBody(vm, "fun speak(text: String", "fun stopSpeaking()")
        assertTrue(
            "界面朗读入口没有归一化：选了「ECHO 念」就白修了",
            body.contains("val content = SpeechText.normalize(text.trim())"),
        )
        assertTrue("ECHO 合成请求要发归一化后的那串", body.contains("TtsRequest(text = content)"))
        assertTrue("系统引擎那条也要用归一化后的那串", body.contains("systemTts.speak(content)"))
    }

    @Test
    fun `常驻听写那条路也只改念法 通知里仍是原文`() {
        val src = moduleText("src/main/java/com/echo/contact/service/ListenService.kt")
        val body = functionBody(
            src,
            "private suspend fun speak(engine: SystemTts, api: EchoApi, text: String)",
            "private suspend fun playEchoBytes",
        )
        assertTrue("听写念回复那条没归一化", body.contains("val spoken = SpeechText.normalize(text)"))
        // 状态/通知（用户眼睛看到的）仍然摆原文 —— 这是"只改念法、不改原文"的落点
        assertTrue(
            "通知里被改成归一化后的文本了（屏幕上该还是 8-19 度）",
            body.contains("ListenState.set(ListenState.Phase.SPEAKING, text.take("),
        )
        assertTrue(body.contains("TtsRequest(text = spoken)"))
        assertTrue(body.contains("engine.speak(spoken)"))
    }
}
