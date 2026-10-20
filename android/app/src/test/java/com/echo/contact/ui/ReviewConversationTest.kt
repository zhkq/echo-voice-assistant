package com.echo.contact.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 手机回顾 = **PC 版那套交互**（开始 → 多轮对话 → 结束）的判据。
 *
 * 用户 2026-10-19 澄清的原话：*"我是说跟 pc 版的交互一样，开始回顾开始然后继续交互"*。
 *
 * 这一轮钉住的事情（每一条都能在真机上对应到一种"用户看到的假象"）：
 *  ① 多轮是**追加**：两轮 = 4 条，不是覆盖成 2 条（否则用户只看到最后一轮）；
 *  ② 同一轮**不重复**、还在路上的**不追加**（否则一轮会变成四行，或者把"正在转写…"当原话）；
 *  ③ 开始**只调 start、不调 go**（`/go` 会 `assistant.start_review()` = 真的开电脑那支麦）；
 *  ④ 结束读服务端如实回的 `stopped`（三态，不编）；
 *  ⑤ **只有一处录音路**（`startRecording`），"录一轮"只是它的薄包装；
 *  ⑥ 一次性口述的**兜底还在**（降级为退路，不是删掉）。
 *
 * 为什么一半是源码断言：`MainViewModel` 是 `AndroidViewModel`（要 Context），
 * Compose 在 JVM 门禁里也渲染不了 —— 而"调的是哪个接口""有没有第二条录音路"
 * 恰恰是**文件里写了什么**这个事实。
 */
class ReviewConversationTest {

    private val vm = moduleText("src/main/java/com/echo/contact/ui/MainViewModel.kt")
    private val api = moduleText("src/main/java/com/echo/contact/data/EchoApi.kt")
    private val reviewTab = moduleText("src/main/java/com/echo/contact/ui/ReviewTab.kt")

    // ---- ① 多轮追加，不覆盖 -------------------------------------------------------------

    @Test
    fun `两轮都追加不是覆盖（2 轮 4 条）`() {
        val empty = ReviewSession(sessionId = "s-1", workspace = "C:\\echo-dev\\review")
        val first = appendReviewRound(empty, roundId = 1L, mine = "第一轮我说的", echo = "第一轮 ECHO 回的")

        // 纯函数：不许改传进来的那一份（追加 = 新会话，老会话还在）
        assertEquals("append 不许原地改（界面正拿着旧的那份在画）", 0, empty.messages.size)

        val second = appendReviewRound(first, roundId = 2L, mine = "第二轮我说的", echo = "第二轮 ECHO 回的")

        assertEquals("两轮必须是 4 条（覆盖的话就是 2 条）", 4, second.messages.size)
        assertEquals(2, second.rounds)
        assertEquals(
            listOf("第一轮我说的", "第一轮 ECHO 回的", "第二轮我说的", "第二轮 ECHO 回的"),
            second.messages.map { it.text },
        )
        assertEquals(
            "一轮两条：我说的 + ECHO 回的",
            listOf(REVIEW_SPEAKER_ME, REVIEW_SPEAKER_ECHO, REVIEW_SPEAKER_ME, REVIEW_SPEAKER_ECHO),
            second.messages.map { it.speaker },
        )
        // 第一轮那两条必须还在（这就是"追加"的字面意思）
        assertTrue(second.messages.take(2).map { it.text }.contains("第一轮我说的"))
        assertEquals("最后一条落地是哪一轮，就记哪一轮", 2L, second.lastRoundId)
    }

    @Test
    fun `失败的一轮也是两条 且原因被标成失败`() {
        val s = appendReviewRound(
            ReviewSession(),
            roundId = 7L,
            mine = "（语音）",
            echo = "无法连接：timeout",
            failed = true,
        )
        assertEquals(2, s.messages.size)
        assertFalse("我说的那句不是失败", s.messages[0].failed)
        assertTrue("ECHO 那条是失败原因", s.messages[1].failed)
        assertTrue("原因要原样带出来", s.messages[1].text.contains("timeout"))
    }

    @Test
    fun `空内容不画成一条空消息`() {
        val s = appendReviewRound(ReviewSession(), roundId = 1L, mine = "   ", echo = "")
        assertEquals(2, s.messages.size)
        assertTrue("我的那句不能空白", s.messages[0].text.isNotBlank())
        assertTrue("回复不能空白", s.messages[1].text.isNotBlank())

        val failed = appendReviewRound(ReviewSession(), roundId = 2L, mine = "x", echo = "", failed = true)
        assertTrue("失败但没有原因也要占位（不能空白）", failed.messages[1].text.isNotBlank())
    }

    // ---- ② 同一轮不重复、在路上的不追加 -----------------------------------------------

    @Test
    fun `同一轮不重复追加 还在路上的不追加`() {
        // 模拟"开始这场之前那条指令"：lastRoundId 已经到 5
        val afterFirstRound = appendReviewRound(
            ReviewSession(lastRoundId = 5L),
            roundId = 6L,
            mine = "这一轮",
            echo = "回了",
        )
        assertEquals(1, afterFirstRound.rounds)

        // StateFlow 会把同一个值再发一次：同一轮 roundId 重复回调 → 不追加
        assertFalse("同一轮被重复回调时不许再追加", reviewShouldAppend(afterFirstRound, 6L, false))
        // 新的一轮 → 追加
        assertTrue("新的一轮要能追加", reviewShouldAppend(afterFirstRound, 7L, false))
        // 还在路上（Sending/Accepted/Running）→ 不追加
        assertFalse("还在路上就追加会把'正在转写…'当原话", reviewShouldAppend(afterFirstRound, 7L, true))
        // 开始之前那条旧轮次（id 更小）→ 不追加
        assertFalse("开始之前那条旧指令不算这一场", reviewShouldAppend(afterFirstRound, 5L, false))
        // 没有会话 / roundId 无效 → 不追加
        assertFalse(reviewShouldAppend(null, 7L, false))
        assertFalse(reviewShouldAppend(afterFirstRound, 0L, false))

        // 走两轮：就算每一轮的回调来了两次，也只该有 4 条
        var s = ReviewSession(lastRoundId = 5L)
        for (round in listOf(7L, 7L, 8L, 8L)) {
            if (reviewShouldAppend(s, round, active = false)) {
                s = appendReviewRound(s, round, mine = "第 $round 轮", echo = "回 $round")
            }
        }
        assertEquals("重复回调不该让一轮变成四行", 4, s.messages.size)
        assertEquals(2, s.rounds)
    }

    // ---- ③ 开始：只调 start，不调 go ----------------------------------------------------

    @Test
    fun `开始回顾只调 start 绝不调 go`() {
        val body = functionBody(vm, "fun startReviewSession(", "fun endReviewSession(")
        assertTrue("开始必须真的打 /daily-review/start", body.contains("api().dailyReviewStart()"))

        // `/go` 会 assistant.start_review() → 真的开**电脑那支麦**。接口面里不许有它。
        assertFalse(
            "EchoApi 里出现了 /api/daily-review/go（那会开电脑的麦）",
            api.contains("@POST(\"api/daily-review/go\")"),
        )
        assertFalse(
            "ViewModel 里出现了 dailyReviewGo —— 手机绝不该遥控电脑开麦",
            vm.contains("dailyReviewGo"),
        )
        // start 这个接口本身**不带请求体**（后端 body 可选，手机不需要 force_new）
        val startApi = functionBody(api, "suspend fun dailyReviewStart(", "suspend fun dailyReviewStop(")
        assertFalse("start 不许带 @Body（多带字段会被老服务端 422）", startApi.contains("@Body"))
        assertTrue("停止接口还在", api.contains("@POST(\"api/daily-review/stop\")"))
    }

    @Test
    fun `没配对只写原因不发请求`() {
        val body = functionBody(vm, "fun startReviewSession(", "fun endReviewSession(")
        val notPaired = body.indexOf("_reviewSessionNote.value = REVIEW_NOT_PAIRED")
        val call = body.indexOf("api().dailyReviewStart()")
        assertTrue("没配对必须留一句原因", notPaired >= 0)
        assertTrue("这句原因必须在发请求之前（没地址就别发）", notPaired < call)
        assertTrue("没配对那句要说清'没开始'", REVIEW_NOT_PAIRED.contains("没开始"))
        assertTrue("没配对那句要指路去设置配对", REVIEW_NOT_PAIRED.contains("配对"))
    }

    @Test
    fun `开始失败一定有可见原因`() {
        val body = functionBody(vm, "fun startReviewSession(", "fun endReviewSession(")
        assertTrue("非 2xx 没读服务端原因", body.contains("parseError(resp)"))
        assertTrue("ok=false 没读 error", body.contains("!b.ok") && body.contains("b.error"))
        assertTrue("sessionId 为空也要说", body.contains("sessionId.isBlank()"))
        assertTrue(
            "三条失败路径都必须落到同一个可见字段（_reviewSessionNote）",
            occurrencesOf(body, "reviewFailureLine(REVIEW_START_FAILED") >= 5,
        )
        assertTrue(
            "每条失败都要写进界面读的那个字段",
            occurrencesOf(body, "_reviewSessionNote.value =") >= 5,
        )
        assertTrue(REVIEW_START_FAILED.isNotBlank())
        assertTrue(
            "失败原因永不空话（null 也要有一句）",
            reviewFailureLine(REVIEW_START_FAILED, null).startsWith(REVIEW_START_FAILED),
        )
    }

    // ---- ④ 结束：本地 + 主机两件事，主机按 stopped 三态 -------------------------------

    @Test
    fun `结束时读 stopped 三态 且本地立刻结束`() {
        val body = functionBody(vm, "fun endReviewSession(", "fun startReviewRound(")
        assertTrue("结束要打 /daily-review/stop", body.contains("api().dailyReviewStop()"))
        assertTrue("必须读服务端如实回的 stopped", body.contains("stoppedFlag(resp.body())"))
        assertTrue("话术只能来自三态纯函数", body.contains("reviewEndedLine("))
        assertTrue("结束先落本地（不等网络往返）", body.contains("_reviewEnded.value = true"))
        assertTrue("结束要撤掉收集器", body.contains("reviewCollector?.cancel()"))
        // 还在录音时不许结束（那段录音会没有"这一轮"的出口）
        assertTrue("录音中结束要先说清", body.contains("REVIEW_STILL_RECORDING"))
    }

    @Test
    fun `结束那句话把本地与主机分开说`() {
        val trueLine = reviewEndedLine(rounds = 2, stopped = true)
        assertTrue("必须说本地这场结束了", trueLine.contains("已结束"))
        assertTrue("必须说本场几轮", trueLine.contains("本场 2 轮"))
        assertTrue("必须写明每一轮已记入回顾历史", trueLine.contains("「回顾历史」"))
        assertTrue("主机那侧要按 stopped=true 说", trueLine.contains(reviewStopNote(true)))

        val falseLine = reviewEndedLine(rounds = 2, stopped = false)
        assertTrue(
            "stopped=false 必须如实说主机没有可停的（不能说成停掉了）",
            falseLine.contains(reviewStopNote(false)) && falseLine.contains("不在跑了"),
        )
        val unknownLine = reviewEndedLine(rounds = 2, stopped = null)
        assertTrue("拿不到 stopped 就退回原话术（不编）", unknownLine.contains(REVIEW_STOP_REQUESTED))
        assertNotEquals(trueLine, falseLine)
        assertNotEquals(falseLine, unknownLine)
    }

    // ---- ⑤ 只有一处录音路 --------------------------------------------------------------

    @Test
    fun `录一轮还是走唯一那处 startRecording`() {
        assertEquals(
            "MainViewModel 里 `fun startRecording(` 只许有一处",
            1,
            occurrencesOf(vm, "\n    fun startRecording("),
        )
        val startRound = functionBody(vm, "fun startReviewRound(", "fun finishReviewRound(")
        assertTrue(
            "录一轮没有走既有那处 startRecording（那就是第二条录音路）",
            // 回顾这一轮还要带上 context=REVIEW_CONTEXT（判据在 ⑦ 那一组）
            startRound.contains("startRecording(asCommand = true"),
        )
        val finishRound = functionBody(vm, "fun finishReviewRound(", "private fun startReviewCollector(")
        assertTrue("收尾这一轮要走既有的 finishRecording", finishRound.contains("finishRecording()"))

        // 回顾页自己不许开麦（只能通过 ViewModel 的入口）—— 既有用例钉着，这里再钉一次
        assertFalse("ReviewTab 直接调了 startRecording", reviewTab.contains("vm.startRecording("))
        assertTrue("输入条的按住说话没接 startReviewRound", reviewTab.contains("vm.startReviewRound()"))
        assertTrue("输入条没接 finishReviewRound", reviewTab.contains("vm.finishReviewRound()"))
        assertTrue("打字继续这一场没走既有指令链", reviewTab.contains("vm.sendCommand(text, context = REVIEW_CONTEXT)"))
    }

    @Test
    fun `收集器只追加有结论的轮次`() {
        val collector = functionBody(vm, "private fun startReviewCollector(", "fun checkStatus(")
        assertTrue("收集器没盯 _submit", collector.contains("_submit.collect"))
        assertTrue("收集器没挡同一轮/在路上", collector.contains("reviewShouldAppend("))
        assertTrue("收集器没真的追加", collector.contains("appendReviewRound("))
        assertTrue("结束之后不许再追加", collector.contains("_reviewEnded.value"))
        // onCleared 要撤收集器（不然 ViewModel 都没了还在往一个没人看的列表里写）
        val cleared = functionBody(vm, "override fun onCleared()", "private fun api()")
        assertTrue("onCleared 没撤收集器", cleared.contains("reviewCollector?.cancel()"))
    }

    // ---- ⑥ 一次性口述兜底还在（退路） --------------------------------------------------

    @Test
    fun `一次性口述的兜底还在`() {
        assertTrue("一次性口述入口被删了", reviewTab.contains("vm.startReviewDictation()"))
        assertTrue("一次性口述的提交被删了", reviewTab.contains("vm.submitReviewDictation()"))
        assertTrue("它的原因显示被删了", reviewTab.contains("dictateNote?.takeIf { it.isNotBlank() }"))
        assertTrue("退路说明没写", reviewTab.contains("REVIEW_DICTATE_FALLBACK"))
        assertTrue("ViewModel 的旧口述入口被删了", vm.contains("fun submitReviewDictation()"))
        assertTrue(
            "退路要说明它是一次性的、不进这场对话",
            REVIEW_DICTATE_FALLBACK.contains("一次性"),
        )
    }

    // ---- 轮数与四档闸门 ----------------------------------------------------------------

    @Test
    fun `进行中那一行同时给今日与本场轮数`() {
        val line = reviewRunningLine(todayRounds = 3, sessionRounds = 2)
        assertTrue("要看见本场轮数：$line", line.contains("本场 2 轮"))
        assertTrue("要看见今日历史轮数：$line", line.contains("今日历史 3 轮"))
        assertTrue(
            "两个数都变了，这句话也要跟着变",
            reviewRunningLine(todayRounds = 0, sessionRounds = 1) != line,
        )
    }

    @Test
    fun `录一轮的闸门复用口述那四档 且多一档没在这场`() {        assertEquals(
            "不在回顾里要先说'先开始回顾'",
            DictationGate.Blocked(REVIEW_NO_SESSION),
            reviewRoundGate(
                inSession = false,
                hasPermission = true,
                alreadyRecording = false,
                meetingRecording = false,
                listening = false,
            ),
        )
        // 四档与 dictationGate **逐字一致**（同一套原因，不许两处各编一句）
        for (args in listOf(
            listOf(false, false, false, false),   // 没权限
            listOf(true, true, false, false),     // 已经在录
            listOf(true, false, true, false),     // 会议录音占着
            listOf(true, false, false, true),     // 一直听着占着
        )) {
            assertEquals(
                "四档原因必须复用 dictationGate 的口径：$args",
                dictationGate(
                    hasPermission = args[0],
                    alreadyRecording = args[1],
                    meetingRecording = args[2],
                    listening = args[3],
                ),
                reviewRoundGate(
                    inSession = true,
                    hasPermission = args[0],
                    alreadyRecording = args[1],
                    meetingRecording = args[2],
                    listening = args[3],
                ),
            )
        }
        assertEquals(
            "四档都没问题时必须放行",
            DictationGate.Go,
            reviewRoundGate(
                inSession = true,
                hasPermission = true,
                alreadyRecording = false,
                meetingRecording = false,
                listening = false,
            ),
        )
    }

    // ---- ⑦ 回顾轮次的标签：进回顾历史、不进指令历史（2026-10-20）---------------------
    //
    // 用户原话："回顾的历史对话，指令里也有能不重复吗？"
    // 修法：这一场每一轮带 context=review（服务端记 source='review'），
    // 于是它们进「回顾历史」、不再在「指令历史」里重复。标签**不改执行**。

    @Test
    fun `回顾这一场的两条发送路都带 context review`() {
        // 打字继续这一场：ReviewTab 调 sendCommand(context = REVIEW_CONTEXT)
        assertTrue(
            "打字继续这一场没带标记",
            reviewTab.contains("vm.sendCommand(text, context = REVIEW_CONTEXT)"),
        )
        // 按住说话这一轮：startReviewRound 把标记交进**唯一**那处 startRecording
        val startRound = functionBody(vm, "fun startReviewRound(", "fun finishReviewRound(")
        assertTrue("录一轮没带标记", startRound.contains("context = REVIEW_CONTEXT"))
        assertTrue("录一轮没走既有开麦入口", startRound.contains("startRecording(asCommand = true"))

        // 两条上传都把标记真的带给服务端（否则服务端仍按 mobile 落库 = 没修好）
        val send = functionBody(vm, "fun sendCommand(", "private suspend fun claimFloorId(")
        assertTrue("文字指令没把 context 发给服务端",
            send.contains("context = if (reviewTurn) REVIEW_CONTEXT else null"))
        val finish = functionBody(vm, "fun finishRecording(", "fun cancelRecording(")
        assertTrue("语音轮次没把 context 发给服务端",
            finish.contains("context = if (reviewTurn) REVIEW_CONTEXT else null"))
        // 回顾轮次按 id 取结果（它们不在指令历史列表里）
        assertTrue("回顾轮次没走按 id 取结果", finish.contains("awaitReviewResult("))
        assertTrue("回顾轮次没带幂等键", finish.contains("reviewRequestIdFor("))
    }

    @Test
    fun `普通指令与一次性口述都不带标记`() {
        // 一次性口述走 `/daily-review/submit`，**不是**这一场的一轮：不许带标记
        val dictate = functionBody(vm, "fun startReviewDictation(", "private fun noteRecordFailure(")
        assertFalse("一次性口述不该带 context", dictate.contains("context = REVIEW_CONTEXT"))
        // startRecording 的默认值必须是 null（普通语音/指令不带 = 老行为不变）
        val startRec = functionBody(vm, "fun startRecording(", "fun startReviewDictation(")
        assertTrue("startRecording 的 context 默认值不是 null", startRec.contains("context: String? = null"))
        // 界面不许自己开麦（只有 ViewModel 的入口能带标记）
        assertFalse("ReviewTab 直接调了 startRecording", reviewTab.contains("vm.startRecording("))
    }

    @Test
    fun `幂等键由这一场与这一轮决定`() {
        assertEquals("sess-1:1700000000000:7", reviewRequestId("sess-1", 1700000000000L, 7))
        assertEquals(
            "同一场同一轮必须得到同一个键（重发才能被认成同一轮）",
            reviewRequestId("sess-1", 1700000000000L, 7),
            reviewRequestId("sess-1", 1700000000000L, 7),
        )
        assertNotEquals(
            "不同轮次是不同的键（内容相同也是两轮）",
            reviewRequestId("sess-1", 1700000000000L, 7),
            reviewRequestId("sess-1", 1700000000000L, 8),
        )
        assertNotEquals(
            "App 重启后 round 从 1 重数：开始时刻不同就必须是不同的键（否则会丢真内容）",
            reviewRequestId("sess-1", 111L, 1),
            reviewRequestId("sess-1", 222L, 1),
        )
        assertNull("没有会话 → 不发键", reviewRequestId("", 1700000000000L, 7))
        assertNull("没有本场开始时刻 → 不发键", reviewRequestId("sess-1", 0L, 7))
        assertNull("轮次无效 → 不发键", reviewRequestId("sess-1", 1700000000000L, 0))
    }

    @Test
    fun `刚才那一场只写去向 不再重画整段对话`() {
        assertTrue(
            "那句话没说清这几轮去哪了：$REVIEW_LAST_ROUNDS_NOTE",
            REVIEW_LAST_ROUNDS_NOTE.contains("已记入回顾历史"),
        )
        assertTrue(
            "还要说明它们不再重复进指令历史：$REVIEW_LAST_ROUNDS_NOTE",
            REVIEW_LAST_ROUNDS_NOTE.contains("指令"),
        )
        // 卡里再画一遍整段对话 = 同一页里又看一次同样的内容
        assertFalse(
            "「刚才那一场」又把整段对话重画了一遍",
            reviewTab.contains("session.messages.forEach"),
        )
    }
}
