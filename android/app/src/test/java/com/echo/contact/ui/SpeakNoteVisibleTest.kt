package com.echo.contact.ui

import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `_speakNote` 必须**看得见**（2026-10-18 复查的第 5 条）。
 *
 * 病根不是"没人写"，而是"没人读"：`MainViewModel._speakNote` 以前只有自己赋值，
 * **全 App 没有一处界面 `collectAsState()` 它** —— 于是"按了播报没声音""系统 TTS 没装"
 * "ECHO 合成失败"全都等于静默。用户能做的只有再点一次、再猜一次。
 *
 * 现在的要求是**两处界面各有一处**（回顾页的状态卡/详情页、指令页/首页），
 * 且那一行要**单行 + 几秒后自动消失**（"这一下"的反馈，不是常驻状态，别喧宾夺主）。
 * 判据只能读源码：Compose 在 JVM 门禁里渲染不了，而"读没读这个 StateFlow"就是源码事实。
 */
class SpeakNoteVisibleTest {

    private val reviewTab = moduleText("src/main/java/com/echo/contact/ui/ReviewTab.kt")
    private val talkScreen = moduleText("src/main/java/com/echo/contact/ui/TalkScreen.kt")
    private val home = moduleText("src/main/java/com/echo/contact/ui/SuperAssistantTab.kt")
    private val common = moduleText("src/main/java/com/echo/contact/ui/CommonUi.kt")
    private val vm = moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")

    @Test
    fun `回顾页 指令页 首页都真的把 speakNote 显示出来`() {
        val screens = mapOf(
            "回顾页" to reviewTab,
            "指令页" to talkScreen,
            "首页" to home,
        )
        for ((name, src) in screens) {
            assertTrue(
                "$name 必须读 vm.speakNote（否则 Model 里写的那句永远是黑的）",
                src.contains("vm.speakNote.collectAsState()"),
            )
            assertTrue(
                "$name 必须真的把它画出来（SpeakNoteLine）",
                src.contains("SpeakNoteLine("),
            )
        }

        // 回顾页两处：状态卡 + 某一天详情页（要求里点名了这两处）
        assertTrue(
            "回顾页的状态卡与详情页都要有",
            occurrencesOf(reviewTab, "SpeakNoteLine(") >= 2,
        )
    }

    @Test
    fun `这一行是单行 且到点自己消失`() {
        assertTrue(
            "显示逻辑只能有一处（三页各抄一遍迟早说不一样的话）",
            common.contains("internal fun SpeakNoteLine("),
        )
        val body = functionBody(
            common,
            "internal fun SpeakNoteLine(",
            "internal const val SPEAK_NOTE_VISIBLE_MS",
        )
        assertTrue("必须单行（别喧宾夺主）", body.contains("maxLines = 1"))
        assertTrue(
            "到点要自己消失：定时器 + 调清除入口",
            body.contains("LaunchedEffect(") && body.contains("delay(SPEAK_NOTE_VISIBLE_MS)"),
        )
        assertTrue(
            "空白内容不显示（不能白占一行）",
            body.contains("takeIf { it.isNotBlank() }"),
        )

        // 清除入口真的存在，而且界面调的就是它
        assertTrue(
            "ViewModel 要有清除入口（让界面不必等下一次朗读才消失）",
            vm.contains("fun clearSpeakNote()"),
        )
        for ((name, src) in mapOf("回顾页" to reviewTab, "指令页" to talkScreen, "首页" to home)) {
            assertTrue("$name 到点必须调 clearSpeakNote()", src.contains("clearSpeakNote()"))
        }
    }
}
