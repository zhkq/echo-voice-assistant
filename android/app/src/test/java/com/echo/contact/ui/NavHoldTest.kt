package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * 底部中间那颗圆 logo 的「**按住说话**」（用户 2026-10-18 夜里原话：
 * *"app 下方中间的 logo 增加一个按住说话发指令的功能"*）。
 *
 * ## 为什么这套判据值得存在
 *
 * 手势在 JVM 门禁里跑不起来（没有渲染、没有指针事件），而这一轮最容易改坏的四件事
 * 恰恰全是手势语义：
 *  ① 短按被长按吃掉（用户点一下回首页变成"开始录音"）；
 *  ② 松手不发（录了但什么都没出去）；
 *  ③ 拖开没取消（说错了照样发出去）；
 *  ④ **多出第二条录音路**（下一轮谁都可能"顺手"在这里再开一个 `Recorder`）。
 *
 * 所以判据分两半：**状态表**（`navHold`，①②③）+ **源码断言**（④，以及"接线接的是既有入口"）。
 */
class NavHoldTest {

    /** 从 [NavHoldState] 起，按顺序喂一串手势事件。 */
    private fun run(vararg keys: NavHoldKey): NavHoldState =
        keys.fold(NavHoldState()) { st, key -> navHold(st, key) }

    // ---- ① 短按 = 切页（不许被长按吃掉）----------------------------------------------

    @Test
    fun `短按松手就是切页 而且绝不开始录音`() {
        val pressed = run(NavHoldKey.DOWN)
        assertEquals("按下之后应该停在 PRESSED（还什么都没做）", NavHoldPhase.PRESSED, pressed.phase)
        assertTrue("按下时不该有任何动作", pressed.actions.isEmpty())

        val released = run(NavHoldKey.DOWN, NavHoldKey.UP)
        assertEquals(NavHoldPhase.IDLE, released.phase)
        assertEquals(
            "短按松手只该切页（原来 selectable 的行为），实际 ${released.actions}",
            listOf(NavHoldAction.SWITCH_TO_CENTER_TAB),
            released.actions,
        )
        assertFalse(
            "短按不许开录：用户点一下回首页，不能变成按住说话",
            NavHoldAction.START_RECORDING in released.actions,
        )
    }

    @Test
    fun `在圆里抖一下就出去 既不发也不切页`() {
        val cancelled = run(NavHoldKey.DOWN, NavHoldKey.CANCEL)
        assertEquals(NavHoldPhase.IDLE, cancelled.phase)
        assertTrue(
            "手势被取消（还没长按）时什么都不该做，实际 ${cancelled.actions}",
            cancelled.actions.isEmpty(),
        )
    }

    // ---- ② 长按 = 开始录音 ------------------------------------------------------------

    @Test
    fun `按满长按阈值就开始录音 走的是既有那个入口`() {
        val st = run(NavHoldKey.DOWN, NavHoldKey.ELAPSED_LONG_PRESS)
        assertEquals(NavHoldPhase.RECORDING, st.phase)
        assertEquals(listOf(NavHoldAction.START_RECORDING), st.actions)
        assertTrue("按住时要有反馈（浮层提示）", navHoldHint(st) != null)
        assertTrue("按住时圆要放大（只改绘制，不改布局）", navHoldScale(st) > 1f)
    }

    @Test
    fun `长按之后再按满一次也不会开两次录`() {
        val st = run(NavHoldKey.DOWN, NavHoldKey.ELAPSED_LONG_PRESS, NavHoldKey.ELAPSED_LONG_PRESS)
        assertEquals(NavHoldPhase.RECORDING, st.phase)
        assertTrue("第二个长按事件不许再吐一个 START_RECORDING", st.actions.isEmpty())
    }

    // ---- ③ 松手 = 结束并发送 / 拖开 = 取消 --------------------------------------------

    @Test
    fun `按住之后松手就是结束并发送`() {
        val st = run(NavHoldKey.DOWN, NavHoldKey.ELAPSED_LONG_PRESS, NavHoldKey.UP)
        assertEquals(NavHoldPhase.IDLE, st.phase)
        assertEquals(
            "松手要做的是'结束+发送'（既有那条提交路），不是取消、也不是再开一次录",
            listOf(NavHoldAction.FINISH_AND_SEND),
            st.actions,
        )
        assertEquals("松手之后不该再显示按住提示", null, navHoldHint(st))
        assertEquals("松手之后圆要恢复原大小", 1f, navHoldScale(st), 0.001f)
    }

    @Test
    fun `按住之后拖开或上滑出界就是取消`() {
        val st = run(NavHoldKey.DOWN, NavHoldKey.ELAPSED_LONG_PRESS, NavHoldKey.CANCEL)
        assertEquals(NavHoldPhase.IDLE, st.phase)
        assertEquals(
            "拖开必须是取消（与「指令」页那颗「按住说话」同语义），绝不许发出去",
            listOf(NavHoldAction.CANCEL_RECORDING),
            st.actions,
        )
    }

    @Test
    fun `提示文案说清松手发送与上滑取消`() {
        val st = run(NavHoldKey.DOWN, NavHoldKey.ELAPSED_LONG_PRESS)
        val hint = navHoldHint(st).orEmpty()
        assertTrue("按住时必须说'松手发送'：$hint", hint.contains("松手发送"))
        assertTrue("按住时必须说清怎么放弃（上滑取消）：$hint", hint.contains("上滑取消"))
    }

    // ---- ④ 不许新增第二条录音路（源码级）----------------------------------------------

    /** `src/main/java` 下所有 `.kt`（判据扫全模块，别只扫两个文件）。 */
    private fun allMainKotlin(): List<File> {
        val root = File(moduleRoot(), "src/main/java")
        check(root.isDirectory) { "找不到 $root" }
        return root.walkTopDown().filter { it.isFile && it.name.endsWith(".kt") }.toList()
    }

    /** 只留**代码行**（注释里引用"只有一个 `fun startRecording(`"是说明，不是第二处声明）。 */
    private fun codeOnly(src: String): String = src.lineSequence()
        .filterNot { line ->
            val t = line.trim()
            t.startsWith("//") || t.startsWith("*") || t.startsWith("/*") || t.startsWith("*/")
        }
        .joinToString("\n")

    /**
     * **指令录音入口只有一个**：`MainViewModel.startRecording()`。
     *
     * 现在全模块有两处 `fun startRecording(`，而且**必须**是这两处：
     *  * `MainViewModel.startRecording()` —— 语音指令（「指令」页「按住说话」、首页指令卡、底部那颗）；
     *  * `MeetingMicService.startRecording()` —— **会议**手机麦（另一条完全不同的录制路，早就有）。
     *
     * 所以这条断言是"名单固定"而不是"数量随意"：谁再加第三个，就是给同一件事开了第二条路，
     * 这条会当场红。
     */
    @Test
    fun `全仓库只有两个录音入口 且都不在这套按住说话里`() {
        val found = mutableListOf<String>()
        for (f in allMainKotlin()) {
            val n = occurrencesOf(codeOnly(f.readText(Charsets.UTF_8)), "fun startRecording(")
            repeat(n) { found += f.name }
        }
        assertEquals(
            "录音入口名单变了（多出来的那个几乎一定是第二条录音路）：$found",
            listOf("MainViewModel.kt", "MeetingMicService.kt"),
            found.sorted(),
        )
        // 按住说话这层自己**不许**声明任何录音入口
        val navHold = codeOnly(moduleText("src/main/java/com/echo/contact/ui/NavHold.kt"))
        assertFalse("NavHold.kt 里出现了录音入口声明", navHold.contains("fun startRecording("))
    }

    @Test
    fun `按住说话这层没有任何录音实现 只调既有入口`() {
        for (name in listOf("ui/NavHold.kt", "ui/EchoNavBar.kt")) {
            val src = moduleText("src/main/java/com/echo/contact/$name")
            // 针眼都带"函数调用"的形状：注释里点到这些名字是允许的（那是说明，不是实现）
            for (banned in listOf("MediaRecorder(", "AudioRecord(", "recorder.start(", "WavWriter", "VoiceSegmenter")) {
                assertFalse(
                    "$name 里出现了 `$banned` —— 这是第二条录音路，按住说话只许调 MainViewModel 的既有入口",
                    src.contains(banned),
                )
            }
        }
    }

    @Test
    fun `HomeScreen 把三个动作接到既有的三个方法上`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/HomeScreen.kt")
        assertTrue("底部导航没有接按住说话的三个回调", src.contains("onHoldStart = holdToTalkStart"))
        assertTrue("松手没有接到 finishRecording（既有那条结束+发送路）", src.contains("onHoldRelease = { vm.finishRecording() }"))
        assertTrue("拖开没有接到 cancelRecording（既有那条取消路）", src.contains("onHoldCancel = { vm.cancelRecording() }"))
        assertTrue("长按没有接到 startRecording（既有那条开录路）", src.contains("vm.startRecording()"))
        assertEquals(
            "HomeScreen 里只许有一处 startRecording()（多点一处就是第二条路）",
            1,
            occurrencesOf(src, "vm.startRecording()"),
        )
        // 已经有一路在录（例如回顾页口述）时**不抢麦**：抢了就等于把那段口述当指令发出去
        assertTrue("没有'已经在录就不抢麦'这条护栏", src.contains("vm.recording.value -> false"))
    }

    /**
     * 手势时序**必须**自己排（这条钉的是"为什么不用 `detectTapGestures(onLongPress=…)`"）。
     *
     * 那个组合的内部时序是"长按到点就把 press 作用域 release 掉"，`tryAwaitRelease()` 于是在
     * **长按那一刻**返回 true —— 表现成"按住 400ms 就直接发出去，用户还没松手"。
     * JVM 门禁跑不了手势，所以这条只能钉源码：**用手写指针时序**（`awaitEachGesture` +
     * `waitForUpOrCancellation`），不许回到那个组合。
     */
    @Test
    fun `按住说话的时序是自己排的（长按到点不等于松手）`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/EchoNavBar.kt")
        assertTrue("没有自己排手势时序（`awaitEachGesture`）", src.contains("awaitEachGesture"))
        assertTrue("没有等手指真的抬起（`waitForUpOrCancellation`）", src.contains("waitForUpOrCancellation"))
        assertTrue("长按阈值没有从 NavHoldSpec 取（两处各写一个数字必漂）", src.contains("NavHoldSpec.LONG_PRESS_MS"))
        // 注释里点出那个坑是**允许**的（那是说明），所以这里只看代码行
        assertFalse(
            "又用回 `onLongPress` 了：那个时序会在长按那一刻就 release press 作用域，" +
                "于是'按住 400ms'会被当成'松手'直接发出去",
            codeOnly(src).contains("onLongPress"),
        )
        // 状态机必须真的被这两个事件推进（而不是只写了但没用）
        assertTrue(src.contains("NavHoldKey.ELAPSED_LONG_PRESS"))
        assertTrue(src.contains("NavHoldKey.CANCEL"))
    }
}
