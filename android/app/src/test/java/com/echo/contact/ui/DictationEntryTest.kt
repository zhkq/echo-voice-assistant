package com.echo.contact.ui

import com.echo.contact.service.ListenState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「口述回顾按下去为什么没发起」的判据（2026-10-18 深夜真机 bug）。
 *
 * ## 现象与取证
 *
 * 用户原话：*"我刚才用手机回顾，不管回顾页面上的按钮，还是界面上按钮都没能发起回顾"*。
 * 电脑侧：`GET /api/daily-review` 与 `/summary` 都是 **200**（手机连得上），
 * 但 `POST /api/stt/transcribe` **一次都没有**、`POST /api/daily-review/submit` **也没有** ——
 * 说明手机在**录音开始之前**就断了，一个字节的音频都没上传。
 *
 * ## 当时那两处静默
 *
 *  * 回顾页：权限没给时回调里只有 `if (granted) { … }` —— **拒绝就什么都不做**；
 *  * 首页回顾卡：`vm.startRecording()` 的布尔值被**直接扔掉**，而且那次失败还被写进
 *    `_submit`（那是"发指令"那一轮的卡片）。
 *
 * 所以这一组用例钉三件事：
 *  ① 每一档"不能开始"都有**互不相同**的一句人话（[dictationGate]，纯函数）；
 *  ② 两个入口都走**同一个**判定、都把它显示在按钮旁边（源码级）；
 *  ③ **回归钉子**：`blocked`（上一条指令还在发）**不该**挡住回顾口述 —— 两个动作，
 *    回顾口述一个字节都不许碰 `_submit`。
 */
class DictationEntryTest {

    private val vm = moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")
    private val reviewTab = moduleText("src/main/java/com/echo/contact/ui/ReviewTab.kt")
    private val homeTab = moduleText("src/main/java/com/echo/contact/ui/SuperAssistantTab.kt")

    // ---- ① 每一档"不能开始"都有话说，而且分得清 -------------------------------------

    @Test
    fun `没给麦克风权限 ⇒ 不能开始 且说清去哪儿打开`() {
        val gate = dictationGate(
            hasPermission = false,
            alreadyRecording = false,
            meetingRecording = false,
            listening = false,
        )
        assertTrue("没权限必须挡下（不然只会静默失败）", gate is DictationGate.Blocked)
        val reason = (gate as DictationGate.Blocked).reason
        assertTrue("要说清是权限：$reason", reason.contains("麦克风权限"))
        assertTrue("要告诉用户点「允许」或去系统设置：$reason", reason.contains("允许"))
        assertTrue("并且明确说这次没开始录：$reason", reason.contains("没开始"))
    }

    @Test
    fun `会议录音占着麦 ⇒ 说清先结束会议录音（不是笼统说开不了麦）`() {
        val gate = dictationGate(
            hasPermission = true,
            alreadyRecording = false,
            meetingRecording = true,
            listening = false,
        )
        assertTrue(gate is DictationGate.Blocked)
        val reason = (gate as DictationGate.Blocked).reason
        assertTrue("必须点名是会议录音占着：$reason", reason.contains("会议录音"))
        assertTrue("要告诉用户下一步（先结束会议录音）：$reason", reason.contains("先结束"))
        assertFalse("不能糊成'别的应用占着'：$reason", reason.contains("别的应用"))
    }

    @Test
    fun `一直听着占着麦 ⇒ 说清去哪停掉`() {
        val gate = dictationGate(
            hasPermission = true,
            alreadyRecording = false,
            meetingRecording = false,
            listening = true,
        )
        assertTrue(gate is DictationGate.Blocked)
        val reason = (gate as DictationGate.Blocked).reason
        assertTrue("必须点名是常驻听写：$reason", reason.contains("一直听着"))
        assertTrue("要给出停它的地方：$reason", reason.contains("指令"))
    }

    @Test
    fun `已经在录 ⇒ 指路说完了 而不是再开一次录`() {
        val gate = dictationGate(
            hasPermission = true,
            alreadyRecording = true,
            meetingRecording = false,
            listening = false,
        )
        assertTrue(gate is DictationGate.Blocked)
        val reason = (gate as DictationGate.Blocked).reason
        assertTrue("要指向「说完了」：$reason", reason.contains("说完了"))
        assertTrue("要说清再按一次不会重新开始：$reason", reason.contains("不会重新开始"))
    }

    @Test
    fun `一切正常 ⇒ 放行`() {
        assertEquals(
            "四档都没问题时必须放行（这里多一个判断就可能又把回顾挡死）",
            DictationGate.Go,
            dictationGate(
                hasPermission = true,
                alreadyRecording = false,
                meetingRecording = false,
                listening = false,
            ),
        )
    }

    @Test
    fun `五档原因互不相同（合成一句等于什么都没说）`() {
        val micProblems = listOf(
            DICTATE_NO_PERMISSION,
            DICTATE_MIC_MEETING,
            DICTATE_MIC_LISTEN,
            DICTATE_MIC_UNAVAILABLE,
            DICTATE_SILENT,
        )
        val all = micProblems + DICTATE_ALREADY + DICTATE_CANCELED
        assertEquals("这几句话必须两两不同", all.size, all.toSet().size)
        // 用户 2026-10-18 点名要分开说：没给权限 / 被录音占用 / 开不了麦
        assertNotEquals(DICTATE_NO_PERMISSION, DICTATE_MIC_UNAVAILABLE)
        assertNotEquals(DICTATE_MIC_MEETING, DICTATE_MIC_LISTEN)
        assertNotEquals(DICTATE_SILENT, DICTATE_MIC_UNAVAILABLE)
        // 五档"麦的问题"每一句都得点名是麦克风（否则用户不知道发生了什么）
        micProblems.forEach { line ->
            assertTrue("这句没说清是麦克风的事：$line", line.contains("麦克风"))
        }
        assertTrue("'已经在录'要指路说完了：$DICTATE_ALREADY", DICTATE_ALREADY.contains("说完了"))
        assertTrue("'取消'要说清没发出去：$DICTATE_CANCELED", DICTATE_CANCELED.contains("没有发出去"))
    }

    @Test
    fun `开不起来的原因按现场状态分档`() {
        assertTrue(micFailReason(meetingRecording = true, listening = false).contains("会议录音"))
        assertTrue(micFailReason(meetingRecording = false, listening = true).contains("一直听着"))
        val other = micFailReason(meetingRecording = false, listening = false)
        assertTrue("剩下的就是'别的应用占着/设备不给开'：$other", other.contains("别的应用"))
        assertEquals(
            "同一档必须永远是同一句（不许两处各编一句）",
            DICTATE_MIC_UNAVAILABLE,
            other,
        )
    }

    @Test
    fun `常驻听写只有 STARTING 与 LISTENING 真的握着麦`() {
        assertTrue(listenHoldsMic(ListenState.Phase.STARTING))
        assertTrue(listenHoldsMic(ListenState.Phase.LISTENING))
        // 这三档服务自己已经把麦放掉了（见 ListenService 类注释的取舍 1）：
        // 把它们也判成"被占用"，用户就会在明明能录的时候被告知不能录。
        for (phase in listOf(
            ListenState.Phase.OFF,
            ListenState.Phase.THINKING,
            ListenState.Phase.SPEAKING,
            ListenState.Phase.PAUSED,
            ListenState.Phase.FAILED,
        )) {
            assertFalse("$phase 不握着麦", listenHoldsMic(phase))
        }
    }

    // ---- ②「收不到声音」这一档：不能一直显示一个假的"录音中 0.0 秒" ---------------

    @Test
    fun `麦开了却收不到声音 ⇒ 会停掉并说原因`() {
        assertTrue("要如实说没收到声音：$DICTATE_SILENT", DICTATE_SILENT.contains("一个字都没收到"))
        assertTrue("要说清这次没有录音：$DICTATE_SILENT", DICTATE_SILENT.contains("这次没有录音"))
        assertTrue("要说清已经停掉了：$DICTATE_SILENT", DICTATE_SILENT.contains("已停止"))
        // 源码：真的会停，而不是只写一句话
        assertTrue("没有'开麦超过 N 毫秒还没数据'的判据", vm.contains("SILENT_MIC_MS"))
        assertTrue("判定成立时必须真的停掉这次录音", vm.contains("recorder.cancel()"))
    }

    @Test
    fun `取消口述不是发指令失败`() {
        val body = functionBody(vm, "fun cancelReviewDictation(", "// ---- 指令的会话内容")
        assertTrue(
            "取消口述必须按'回顾口述'的身份走（asCommand = false）",
            body.contains("cancelRecording(asCommand = false)"),
        )
        assertTrue("并且要说一句'取消了，没有发出去'", body.contains("DICTATE_CANCELED") ||
            vm.contains("_dictateNote.value = DICTATE_CANCELED"))
        assertEquals("取消口述不许碰 _submit", 0, occurrencesOf(body, "_submit"))
    }

    // ---- ③ 回归钉子：blocked 不该挡住回顾口述 ---------------------------------------

    /**
     * **这一条是本轮的回归钉子**：上一轮的真机 bug 是"发指令静默失败"，那一轮给
     * `sendCommand` 加了一道闸（上一条还在路上 → `blocked`，这次点击不发出去）。
     *
     * 那道闸**只属于发指令**。回顾口述是另一个动作：它不认领任何指令行、不写 `_submit`，
     * 所以"上一条指令还没回来"**不该**让它开不了录 —— 若哪天有人把 `sendGate` 套到
     * 口述这条路上，这条会红。
     */
    @Test
    fun `blocked（上一条还在发）不该挡住回顾口述`() {
        // 判定里根本没有"上一条还在发"这个入参：四档都不是它，所以一定是 Go
        assertEquals(
            "回顾口述的判定不许看发指令那一轮的状态",
            DictationGate.Go,
            dictationGate(
                hasPermission = true,
                alreadyRecording = false,
                meetingRecording = false,
                listening = false,
            ),
        )
        // 源码：判定函数与入口都不许提到发指令的那一套（SubmitState / sendGate / _submit）
        val gateBody = functionBody(
            moduleText("src/main/java/com/echo/contact/ui/DictationEntry.kt"),
            "internal fun dictationGate(",
            "internal fun micFailReason(",
        )
        for (banned in listOf("SubmitState", "sendGate", "_submit", "SubmitPhase")) {
            assertFalse("回顾口述的判定里出现了 `$banned`：两个动作又被混在一起了", gateBody.contains(banned))
        }
        val entry = functionBody(vm, "fun startReviewDictation(", "fun noteRecordFailure(")
        for (banned in listOf("_submit", "sendGate", "SubmitPhase", "_speakNote")) {
            assertFalse("startReviewDictation 里出现了 `$banned`（回顾口述不是发指令）", entry.contains(banned))
        }
        // 录音实现那一段也不许直接写发指令那张卡：它现在只按 asCommand 决定交给谁
        val record = functionBody(vm, "fun startRecording(", "fun startReviewDictation(")
        for (banned in listOf("_submit", "_speakNote")) {
            assertFalse(
                "startRecording 里直接写 `$banned` 了：回顾口述会被当成发指令（失败原因跑到指令卡上）",
                record.contains(banned),
            )
        }
        assertTrue("要么按身份分开写（asCommand）", record.contains("if (asCommand)"))
    }

    // ---- ④ 两个入口的接线（源码级：Compose 在 JVM 门禁里点不了）--------------------

    @Test
    fun `回顾页的口述入口走同一个判定 且权限被拒也有话说`() {
        assertTrue(
            "回顾页「车里口述」没走 startReviewDictation（那就是又会静默）",
            reviewTab.contains("vm.startReviewDictation()"),
        )
        assertFalse(
            "回顾页还在直接调 startRecording()：绕过了权限/占用的判定",
            reviewTab.contains("vm.startRecording()"),
        )
        // 权限回调里**不许**再有 `if (granted)`：拒绝时那句话就是在这里丢掉的
        val launcher = functionBody(reviewTab, "val permissionLauncher", "if (day != null)")
        assertFalse(
            "权限回调里还留着 `if (granted)`：用户拒绝时屏幕上什么都不会显示",
            launcher.contains("if (granted)"),
        )
        assertTrue("权限回调必须无条件再试一次（原因由判定来说）", launcher.contains("vm.startReviewDictation()"))
        // 原因要摆在刚按的那三张卡下面
        assertTrue("回顾页没把 dictateNote 显示出来", reviewTab.contains("dictateNote?.takeIf { it.isNotBlank() }"))
        assertTrue("回顾页没读 dictateNote", reviewTab.contains("vm.dictateNote.collectAsState()"))
    }

    @Test
    fun `首页回顾卡改成 PC 式回顾 且原因画在按钮下面`() {
        // 2026-10-22：首页回顾卡与回顾页同一套动作（开始 / 按住说 / 说完了 / 结束），
        // 不再走"口述一次交上去"那条一次性路。
        assertTrue("首页回顾卡没把这一轮的原因显示出来",
            homeTab.contains("listOf(roundNote, sessionNote)"))
        assertTrue("首页回顾卡没读 reviewRoundNote",
            homeTab.contains("vm.reviewRoundNote.collectAsState()"))
        assertTrue("首页回顾卡没读这一场的说明",
            homeTab.contains("vm.reviewSessionNote.collectAsState()"))

        val entry = functionBody(homeTab, "val startReviewRoundWithMic", "// ---- 布局预算")
        assertTrue("首页「按住说话」没接回顾入口", entry.contains("vm.startReviewRound()"))
        assertFalse(
            "首页回顾卡又走回一次性口述（那会把这一轮当成独立口述）",
            entry.contains("startReviewDictation"),
        )
        // 授权回调也要能说清"用户拒绝了"：startReviewRound 自己查权限并把原因写进 roundNote。
        assertTrue(
            "授权回调没有再去试一次回顾这一轮",
            homeTab.contains("{ vm.startReviewRound() }"),
        )
    }

    @Test
    fun `按下按钮真的会开录（走的是既有那一个 startRecording）`() {
        val entry = functionBody(vm, "fun startReviewDictation(", "fun noteRecordFailure(")
        assertTrue(
            "startReviewDictation 放行之后没有真的开录",
            entry.contains("startRecording(asCommand = false)"),
        )
        // 录音入口仍然只有一个：不许因为这条修又长出一条路来
        assertEquals(
            "MainViewModel 里 `fun startRecording(` 只许有一处",
            1,
            occurrencesOf(vm, "\n    fun startRecording("),
        )
        // 开不了麦那两档都要落到"说原因"，不许静默 return
        assertTrue("startRecording 失败时没有写原因", vm.contains("_dictateNote.value = DICTATE_MIC_UNAVAILABLE"))
        assertTrue("真的建不起麦时没有按现场状态分档", entry.contains("micFailReason("))
    }
}
