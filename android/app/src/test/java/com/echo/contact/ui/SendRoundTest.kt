package com.echo.contact.ui

import com.echo.contact.data.dto.CommandItem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **"发失败必须说、不许拿旧数据顶替"** 的判据（2026-10-18 真机 bug）。
 *
 * 那次的现象被人记住的是**结果**：手机上"执行的不是我的语音内容而是测试"。
 * 服务端取证是：13:00 之后**零 POST**、库里也没有新的 mobile 指令 —— 也就是说
 * 那个"测试"是**前一天**那条旧指令，被界面当成了这次的答复。
 *
 * 所以这一组用例钉的不是"某一行代码"，而是四条用户能摸到的规矩：
 *
 *  ① 任何"不发"的出口都要带一句人话（[sendGate] / [failedRound]）；
 *  ② 失败态**回复必须为空**，界面上也只在 `Done` 才显示回复（[showsReply]）；
 *  ③ 语音没听清是"再说一次"，**不是**发个空串去认领别人的行（[voiceHearNote]）；
 *  ④ 认领下界必须现取（[newestCommandId] + `MainViewModel.claimFloorId`）——
 *     用本地那份旧列表算下界，就会把"发送前就存在的旧行"认成这次的结果。
 *
 * 全部是纯函数 + 源码断言：Compose 在 JVM 门禁里点不了，但这些决定都是**数据**。
 */
class SendRoundTest {

    private fun row(
        id: Int,
        text: String,
        source: String = "mobile",
        status: String = "done",
        reply: String? = null,
        ts: String? = null,
    ) = CommandItem(id = id, text = text, source = source, status = status, reply = reply, ts = ts)

    // ---- ① 发不发得出去 ----------------------------------------------------------------

    @Test
    fun `每一个不发出去的出口都带着一句人话（不许静默 return）`() {
        val empty = sendGate("", SubmitPhase.Idle, paired = true)
        val blank = sendGate("   ", SubmitPhase.Idle, paired = true)
        val unpaired = sendGate("打开客厅的灯", SubmitPhase.Idle, paired = false)
        val busy = sendGate("打开客厅的灯", SubmitPhase.Sending, paired = true)

        for ((name, gate) in listOf("空文本" to empty, "全空白" to blank, "没配对" to unpaired, "上一条还在路上" to busy)) {
            assertTrue("$name 必须是 Blocked（否则就是静默丢弃）", gate is SendGate.Blocked)
            assertTrue("$name 必须给一句能显示的原因", (gate as SendGate.Blocked).reason.isNotBlank())
        }

        // 原因要能指导用户做什么，而不是一句"失败了"
        val reason = (unpaired as SendGate.Blocked).reason
        assertTrue("没配对的原因要指明去哪配：$reason", reason.contains("配对") && reason.contains("设置"))
    }

    @Test
    fun `没配对与空文本之后这一轮要换掉（不许留着上一条回复）`() {
        assertEquals(
            "这两条都是'这次什么都没发'，不能保留上一轮（否则旧回复会被当成这次的）",
            false,
            (sendGate("", SubmitPhase.Done, paired = true) as SendGate.Blocked).keepRound,
        )
        assertEquals(
            false,
            (sendGate("打开灯", SubmitPhase.Done, paired = false) as SendGate.Blocked).keepRound,
        )
        assertEquals(
            "只有'上一条还在等电脑回话'才保留那一轮（它还在路上，擦掉会让用户以为丢了）",
            true,
            (sendGate("打开灯", SubmitPhase.Sending, paired = true) as SendGate.Blocked).keepRound,
        )
    }

    @Test
    fun `一切正常时才放行`() {
        assertEquals(SendGate.Go, sendGate("打开客厅的灯", SubmitPhase.Idle, paired = true))
        assertEquals(SendGate.Go, sendGate("打开客厅的灯", SubmitPhase.Done, paired = true))
        assertEquals(SendGate.Go, sendGate("打开客厅的灯", SubmitPhase.Failed, paired = true))
    }

    // ---- ② 失败 ⇒ 报错，且绝不显示旧回复 ------------------------------------------------

    @Test
    fun `发送失败 ⇒ 有原因 且 回复是空的`() {
        val failed = failedRound(
            roundId = 2,
            text = "打开客厅的灯",
            reason = "无法连接：timeout",
        )

        assertEquals(SubmitPhase.Failed, failed.phase)
        assertTrue("失败必须带原因", failed.message.contains("无法连接"))
        assertEquals("失败态的回复必须是空的（不许带上一轮的字）", "", failed.reply)
        assertEquals("失败态也不许带'该念的那段'（否则会念上一轮的）", "", failed.speak)
        assertEquals("失败原因同时进 error（面板/历史那一列也读它）", failed.message, failed.error)
        assertEquals("这一轮发的哪一句要留着", "打开客厅的灯", roundHeadline(failed))
    }

    @Test
    fun `回复只在 Done 显示 —— 失败态即使残留了旧回复也不给看`() {
        assertTrue("只有 Done 能证明 reply 是这一条的", showsReply(SubmitPhase.Done))
        for (phase in listOf(
            SubmitPhase.Idle,
            SubmitPhase.Sending,
            SubmitPhase.Accepted,
            SubmitPhase.Running,
            SubmitPhase.Failed,
        )) {
            assertFalse("$phase 阶段不许显示回复", showsReply(phase))
        }

        // 最坏情况：有人用 copy 把上一轮的 reply 留在失败态里 —— 界面按 showsReply 把关，仍然不显示
        val leaked = failedRound(3, "打开客厅的灯", "超时").copy(reply = "前一天的答复")
        assertFalse(
            "失败态里残留的旧回复绝不能被当成这次的结果",
            showsReply(leaked.phase) && leaked.reply.isNotBlank(),
        )
    }

    @Test
    fun `成功这一轮显示的就是本条`() {
        val ok = startingRound(
            roundId = 7,
            text = "打开客厅的灯",
            phase = SubmitPhase.Accepted,
            message = "已提交",
        ).copy(phase = SubmitPhase.Done, reply = "灯开了", speak = "灯开了", message = "耗时 1.2 秒")

        assertTrue(showsReply(ok.phase))
        assertEquals("打开客厅的灯", roundHeadline(ok))
        assertEquals("灯开了", ok.reply)
        assertTrue("新开的一轮回复从空开始", startingRound(8, "下一句", SubmitPhase.Sending, "正在提交…").reply.isEmpty())
    }

    @Test
    fun `语音那一轮的原话从空文本转成电脑听成的那句`() {
        val round = startingRound(9, "（语音，正在转写…）", SubmitPhase.Sending, "正在上传给 ECHO 转写…")
            .copy(text = "打开客厅的灯", phase = SubmitPhase.Accepted, message = "已提交")
        assertEquals("打开客厅的灯", roundHeadline(round))
        assertEquals("没有文本时给一个诚实的占位", "（语音）", roundHeadline(SubmitState()))
    }

    // ---- ③ 语音没听清 ≠ 发了"测试" ------------------------------------------------------

    @Test
    fun `STT 回空 ⇒ 提示没听清 且 不继续认领`() {
        for (blank in listOf(null, "", "   ", "\n")) {
            val note = voiceHearNote(blank)
            assertEquals("识别结果为空时必须是这句固定的话", NOTE_NO_HEARING, note)
            assertTrue("要让人知道怎么办：$note", note.contains("再说一次"))
            assertTrue("并且明确说这条没发出去：$note", note.contains("没发出去"))
        }
        // 有文本才放行（空串 = 没有原因）
        assertEquals("", voiceHearNote("打开客厅的灯"))
    }

    @Test
    fun `录音为空或太短 ⇒ 报告实际时长 且 不发`() {
        val veryShort = recordingTooShortNote(0)
        assertTrue("要说清没录到声音：$veryShort", veryShort.contains("没录到声音"))
        assertTrue("要说这条没发出去：$veryShort", veryShort.contains("没发出去"))
        assertTrue("要给出实际时长（用户才知道是不是按快了）：$veryShort", veryShort.contains("0.0 秒"))
        assertTrue(recordingTooShortNote(250).contains("2.5 秒"))
        // 负数（时钟异常）不该画出奇怪的字
        assertTrue(recordingTooShortNote(-100).contains("0.0 秒"))

        assertTrue("开不了麦也要能说清楚", NOTE_MIC_UNAVAILABLE.contains("麦克风"))
        assertTrue(NOTE_MIC_UNAVAILABLE.contains("没发出去"))
    }

    // ---- ④ 旧数据绝不顶替：认领下界 ----------------------------------------------------

    @Test
    fun `认领下界必须是发送前电脑上的最新一条（本地旧列表不算数）`() {
        // 本地那份停在 id=6（App 放了一夜；自动刷新以前不刷指令列表）
        val localStale = listOf(row(6, "别的事"), row(5, "更早的事"))
        // 电脑上此刻最新是 id=7 —— 它是**前一天**那条「测试一下测试一下。」（已 done）
        val oldRow = row(7, "测试一下测试一下。", status = "done", reply = "前一天的答复")
        val items = listOf(oldRow, row(6, "别的事"), row(5, "更早的事"))

        assertEquals(6, newestCommandId(localStale))

        // 用本地旧列表算下界 → 旧行（7 > 6）被认领成"这次的结果"！这就是那个 bug
        assertNotNull(
            "这就是旧数据顶替的入口：本地旧下界把'发送前就存在'的旧行放过去了",
            pickSubmitted(items, "测试一下测试一下。", maxIdBefore = newestCommandId(localStale)),
        )

        // 现取的下界（max(本地 6, 电脑 7) = 7）→ 旧行不再算数，只能等真正的新行
        val floor = maxOf(newestCommandId(localStale), newestCommandId(listOf(oldRow)))
        assertEquals(7, floor)
        assertNull(
            "现取的下界之后，旧指令绝不能被当成本次结果",
            pickSubmitted(items, "测试一下测试一下。", maxIdBefore = floor),
        )

        // 真正的新行（8）到了才认领
        val fresh = listOf(row(8, "测试一下测试一下。", status = "pending")) + items
        assertEquals(8, pickSubmitted(fresh, "测试一下测试一下。", maxIdBefore = floor)?.id)
    }

    @Test
    fun `空列表与没有 id 的行不会把下界算成 0`() {
        assertEquals(0, newestCommandId(emptyList()))
        assertEquals(
            "id 为 null 的行不该把下界拉回去",
            7,
            newestCommandId(listOf(CommandItem(id = null, text = "x"), row(7, "y"))),
        )
        // 下界为 0 时任何"有 id 的 mobile 同文本行"都会被认领 —— 所以语音那条以前传 0 是危险的
        assertNotNull(pickSubmitted(listOf(row(3, "打开灯")), "打开灯", maxIdBefore = 0))
    }

    // ---- ⑤ 迟到的结果不许写别人的轮次 ---------------------------------------------------

    @Test
    fun `上一轮迟到的回包不许写进这一轮`() {
        val current = startingRound(2, "这次说的这句", SubmitPhase.Sending, "正在提交…")

        assertTrue(ownsRound(current, 2))
        assertFalse("1 是上一轮的号", ownsRound(current, 1))
        assertFalse("0 是'没有轮次'", ownsRound(current, 0))

        val untouched = applyRound(current, 1) { it.copy(phase = SubmitPhase.Done, reply = "上一次的答复") }
        assertEquals("迟到的那一轮一个字都不许写进来", "", untouched.reply)
        assertEquals(SubmitPhase.Sending, untouched.phase)

        val landed = applyRound(current, 2) { it.copy(phase = SubmitPhase.Done, reply = "这一次的答复") }
        assertEquals("这一次的答复", landed.reply)
    }

    @Test
    fun `挡下的一次点击不改这一轮的状态 却留下一句可见的提示`() {
        val inFlight = startingRound(3, "上一条", SubmitPhase.Sending, "正在提交…")
        val noted = withBlockedNote(inFlight, NOTE_SEND_BUSY)

        assertEquals("还在路上的那一轮不许被擦掉", SubmitPhase.Sending, noted.phase)
        assertEquals("上一条", noted.text)
        assertTrue("但必须说清这次点击没发出去", noted.blocked.contains("没发出去"))
        assertEquals("新开一轮会把提示清掉", "", startingRound(4, "新的一条", SubmitPhase.Sending, "正在提交…").blocked)
    }

    // ---- ⑥ 源码级：静默失败的旧写法不许回来 --------------------------------------------

    /**
     * 这几条读**源码**：它们钉的是"那个静默出口没有了"。
     *
     * 为什么必须读源码：`MainViewModel` 继承 `AndroidViewModel`（要 `Context`），
     * JVM 门禁里造不出来。而"点了没反应"的根因恰恰就是**那个只 `return` 的分支**，
     * 纯函数测不到它 —— 所以只能钉"那段字不许再出现"。
     */
    @Test
    fun `语音失败不再只写进没人读的 speakNote`() {
        val vm = moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")

        assertFalse(
            "`if (!_recording.value) return` 是静默出口：双击「说完了」时用户什么也看不到",
            vm.contains("if (!_recording.value) return"),
        )
        assertFalse(
            "「录得太短」以前只喂给 _speakNote，而全 App 没有一处读 speakNote（= 静默）",
            vm.contains("_speakNote.value = \"录得太短了"),
        )
        assertTrue("录音为空必须把这一轮标成失败", vm.contains("recordingTooShortNote("))
        assertTrue("开不了麦必须把这一轮标成失败", vm.contains("NOTE_MIC_UNAVAILABLE"))
        assertTrue("识别为空必须当场拦下（不许拿空文本去认领）", vm.contains("voiceHearNote("))
        assertFalse(
            "老写法：把可能为空的识别文本直接交给 awaitResult —— 空文本会匹配到别的行",
            vm.contains("text = b.text.orEmpty(),"),
        )
        assertTrue("一轮一个序号（迟到的回包不写别人的轮次）", vm.contains("nextRoundId()"))
        assertTrue("认领下界必须现取（本地列表可能停在昨天）", vm.contains("claimFloorId()"))
    }

    @Test
    fun `两处卡片都按 showsReply 把关回复并显示没发出去的原因`() {
        val screens = listOf(
            "src/main/java/com/echo/contact/ui/SuperAssistantTab.kt",
            "src/main/java/com/echo/contact/ui/TalkScreen.kt",
        )
        for (path in screens) {
            val src = moduleText(path)
            assertTrue("$path 必须用 showsReply(phase) 把关回复", src.contains("showsReply("))
            assertFalse(
                "$path 还在无条件显示 submit.reply（旧回复会被当成这次的）",
                src.contains("if (submit.reply.isNotBlank())"),
            )
            assertTrue("$path 必须把'这次没发出去'的原因显示出来", src.contains("submit.blocked"))
            assertTrue("$path 要摆出这一轮发的原话", src.contains("roundHeadline("))
        }
    }
}
