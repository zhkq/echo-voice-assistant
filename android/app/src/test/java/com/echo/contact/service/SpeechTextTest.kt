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
        // ---- ①' 2026-10-18 夜里这一轮补的形态（上一版漏网的那些）----
        // 两端都带单位：中间那个分隔符被两个单位夹着，上一版根本匹配不到
        "8度-19度" to "8 度到 19 度",
        "8度—19度" to "8 度到 19 度",
        "8℃～19℃" to "8 ℃到 19 ℃",
        "8℃-19度" to "8 ℃到 19 度",
        "8 摄氏度 - 19 摄氏度" to "8 摄氏度到 19 摄氏度",
        // 全角数字（中文输入法里出来的）
        "８-１９度" to "8 到 19度",
        "８～１９℃" to "8 到 19℃",
        // 新认的单位：摄氏度 / 华氏
        "8-19摄氏度" to "8 到 19摄氏度",
        "8-19 摄氏度" to "8 到 19 摄氏度",
        "8-19华氏" to "8-19华氏",
        // 数字与单位之间有空格（上一版**已经**支持，这里显式钉住，别再退化）
        "8～19 度" to "8 到 19 度",
        "8—19 度" to "8 到 19 度",
        "8 - 19 度" to "8 到 19 度",
        // ---- ② 不该换的 ----
        "-5 度" to "-5 度",
        "最低 -5 度" to "最低 -5 度",
        "最低-5度" to "最低-5度",
        "-5-3度" to "-5-3度",
        "10-18" to "10-18",
        "10 - 18" to "10 - 18",
        "2026-10-18" to "2026-10-18",
        "２０２６-１０-１８" to "２０２６-１０-１８",
        "v2.1.7" to "v2.1.7",
        "2.1.7" to "2.1.7",
        "8-19" to "8-19",
        "第8-19条" to "第8-19条",
        "18-25岁" to "18-25岁",
        "8-19 天" to "8-19 天",
        // 「至」已经是"到"的念法，再改就是把原文的字吃掉
        "8至19度" to "8至19度",
        // 只有一端带单位：不算"两端带单位"，而右端（19）后面又没有单位 → 两个规则都不该动它
        "8度-19" to "8度-19",
        // 真实那一例：服务端已经把分隔符抹掉了（见下面那条用例的说明）
        "819度" to "819度",
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

    /**
     * **真实那一句**（用户 2026-10-18 20:59 在手机上听成"819 度"的那次）。
     *
     * 原样抄自电脑上的真凭据 `D:\ECHO\data\echo.db` → `commands` 表 id=181
     * （`ts=2026-10-18 20:59:35`、`source=mobile`）的 `brief` 列 —— 也就是手机念的那串。
     * 字节级核对过：`819` 后面是 U+5EA6（`度`），**没有空格、没有分隔符、也不是 `℃`**。
     *
     * 为什么这条用例断言"**不许改**"：那个 `~` 是在服务端被 `app/assistant.py::_clean_line()`
     * 第 219 行 `re.sub(r"[_~^]", "", t)` 当 markdown 强调符删掉的（第 221 行再把 `℃`→`度`）。
     * 到了手机手上，`819` 与一个真的"八百一十九"**不可区分** —— 任何在这里"猜"的规则
     * 都会把正常数字念坏。所以这条钉的是**边界**：修法必须落在服务端那一段（不在本文件）。
     */
    @Test
    fun `真实那句：2026-10-18 20-59 念出去的 819 度 在安卓侧不可恢复`() {
        val spoken = "北京明天（10月19日）晴转多云，819度，微风无雨，白天回暖明显、早晚偏凉、昼夜温差11度。"
        assertEquals(
            "服务端送来时那个 ~ 就已经没了：安卓不许猜（猜错就把正常数字念坏）",
            spoken,
            SpeechText.normalize(spoken),
        )
        assertFalse("这串里已经没有可识别的区间了", SpeechText.hasRange(spoken))
    }

    /**
     * 同一轮**回复正文**里的那句（`reply` 列，原样）。
     *
     * 为什么这条才是本类真正能救的：老电脑不回 `speakText` 时，`SpeakPlan.summaryOf()`
     * 在手机上从 `reply` 现切摘要，切出来的就是这句 —— 它**带着 `8~19℃` 原样**。
     * 每日回顾的【播报】同理（`app/daily_review.py` 直接落库，不过 `_clean_line`）。
     */
    @Test
    fun `真实那句：同一轮 reply 里的 8~19℃ 必须念成 8 到 19`() {
        val replyHead = "明天（10月19日，周一）北京天气晴转多云，气温 8~19℃，白天南风、夜间北风"
        assertEquals(
            "带上 reply 原文里的那个空格与 ℃ 之后就不该再匹配不上了",
            "明天（10月19日，周一）北京天气晴转多云，气温 8 到 19℃，白天南风、夜间北风",
            SpeechText.normalize(replyHead),
        )
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
