package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **离线录音的状态机与它的判据**（用户 2026-10-16 的需求）。
 *
 * 这一组用例盯的全是"会静默出错"的地方：
 *  * 连不上时**不许**变成"录音失败"（那等于什么都没录）；
 *  * 状态文案里**不许**把"没传上去"说成"已上传"；
 *  * ECHO 正录着别的会时**不许**抢占；
 *  * 退避**必须有上限**（不然断网一晚上把电池和流量烧光）；
 *  * 重试要有次数上限，但**网络刚恢复时要立刻试**（用户能感知的只有这一下）。
 */
class OfflineMeetingStateTest {

    // ---------------------------------------------------------------- ① 不可达 → 离线

    @Test
    fun `连不上 ECHO 时走离线那条路 而不是报录音失败`() {
        // 需求原话："开始/结束录音时若 ECHO 不可达……不要让录音失败，而是进入离线录音"
        val d = startDecision(paired = true, reachable = false, why = "ECHO 连不上（超时）")
        assertTrue("连不上必须是离线那一档：$d", d is StartDecision.Offline)
        assertEquals("ECHO 连不上（超时）", (d as StartDecision.Offline).why)
    }

    @Test
    fun `没配对也允许离线录音（配对只影响能不能传 不影响能不能录）`() {
        val d = startDecision(paired = false, reachable = false)
        assertTrue(d is StartDecision.Offline)
        assertTrue("原因要如实说是没配对，而不是笼统的'连不上'：${(d as StartDecision.Offline).why}",
            d.why.contains("配对"))
    }

    @Test
    fun `可达时走联网那条路（行为与改动前一致）`() {
        assertEquals(StartDecision.Online, startDecision(paired = true, reachable = true))
    }

    @Test
    fun `探活返回非 2xx 与抛异常都算不可达`() {
        assertFalse(isReachable(null))          // 抛异常（超时/拒绝连接）
        assertFalse(isReachable(0))
        assertFalse(isReachable(401))           // 连得上但令牌不对：录音这条路一样开不了会
        assertFalse(isReachable(500))
        assertFalse(isReachable(302))
        assertTrue(isReachable(200))
        assertTrue(isReachable(204))
        assertEquals(Reachability.UNREACHABLE, reachabilityOf(null))
        assertEquals(Reachability.REACHABLE, reachabilityOf(200))
    }

    @Test
    fun `结束录音在离线时完全本地 不碰服务端`() {
        // 断网时 meetingStop 必然失败，而用户的"结束"必须立刻生效（本地不再采音、清单落盘）
        assertEquals(StopDecision.FinishLocally, stopDecision(recordingOffline = true))
        assertEquals(StopDecision.NotifyServer, stopDecision(recordingOffline = false))
    }

    // ---------------------------------------------------------------- ② 不抢占

    @Test
    fun `ECHO 正在录别的会时不抢占`() {
        assertTrue("电脑正在录：只许等，不许抢", echoBusyElsewhere(echoRecording = true, localRecording = false))
        assertFalse("正在录的是我们自己这一场：谈不上抢占",
            echoBusyElsewhere(echoRecording = true, localRecording = true))
        assertFalse("ECHO 没在录：可以传", echoBusyElsewhere(echoRecording = false, localRecording = false))
    }

    @Test
    fun `失败分类分得开（连不上 服务端拒绝 服务端正忙 服务端出错）`() {
        assertEquals(SyncFailure.UNREACHABLE, failureOf(null))
        assertEquals(SyncFailure.REJECTED, failureOf(403))
        assertEquals(SyncFailure.REJECTED, failureOf(422))
        assertEquals(SyncFailure.SERVER_BUSY, failureOf(409))
        assertEquals(SyncFailure.SERVER_BUSY, failureOf(503))
        assertEquals(SyncFailure.SERVER_ERROR, failureOf(500))
    }

    @Test
    fun `服务端拒绝时不再重试（重试也不会好）`() {
        assertFalse("4xx 是契约/权限问题：一直重试只是空转，还骗用户'在传'",
            failureRetryable(SyncFailure.REJECTED))
        assertTrue(failureRetryable(SyncFailure.UNREACHABLE))
        assertTrue(failureRetryable(SyncFailure.SERVER_BUSY))
        assertTrue(failureRetryable(SyncFailure.SERVER_ERROR))
    }

    // ---------------------------------------------------------------- ③ 退避

    @Test
    fun `退避间隔递增且有上限`() {
        assertEquals(2_000L, backoffDelayMs(1))
        assertEquals(4_000L, backoffDelayMs(2))
        assertEquals(8_000L, backoffDelayMs(3))
        assertEquals(16_000L, backoffDelayMs(4))
        // 递增（到上限之前）
        for (i in 1..6) assertTrue("第 $i 次没有比上一次久", backoffDelayMs(i + 1) > backoffDelayMs(i))
        // 上限：不许无限翻倍（那样断网一晚上 = 几千年的等待）
        // 2s → 4 → 8 → 16 → 32 → 64 → 128 → 256；下一次再翻就超过 5 分钟，直接落到上限
        assertEquals(256_000L, backoffDelayMs(8))
        assertEquals(BackoffPolicy.MAX_MS, backoffDelayMs(9))
        assertEquals(BackoffPolicy.MAX_MS, backoffDelayMs(30))
        assertEquals(BackoffPolicy.MAX_MS, backoffDelayMs(1000))
        assertTrue("上限是硬上限：任何次数都不许超过它",
            (1..200).all { backoffDelayMs(it) <= BackoffPolicy.MAX_MS })
        assertEquals("0 或负数不该给一个正的等待", 0L, backoffDelayMs(0))
    }

    @Test
    fun `自动重试有次数上限 用尽之后只留手动`() {
        assertTrue(BackoffPolicy.shouldAutoRetry(0))
        assertTrue(BackoffPolicy.shouldAutoRetry(BackoffPolicy.MAX_AUTO_ATTEMPTS - 1))
        assertFalse(BackoffPolicy.shouldAutoRetry(BackoffPolicy.MAX_AUTO_ATTEMPTS))
        assertNull("不会再自动试时**别给一个永远不成立的时刻**",
            BackoffPolicy.nextAttemptAt(1_000L, BackoffPolicy.MAX_AUTO_ATTEMPTS))
        assertEquals(1_000L + backoffDelayMs(1),
            BackoffPolicy.nextAttemptAt(1_000L, 0)!!)
    }

    @Test
    fun `网络刚恢复时立刻试（不等退避）`() {
        // 用户走进 WiFi 的那一刻就该开始传 —— 这是他唯一能感知到的行为
        val now = 10_000L
        val nextAt = now + 300_000L
        assertTrue("上一轮不可达、这一轮可达 → 立刻试",
            shouldRetryNow(now, attempts = 3, nextAtMs = nextAt,
                wasUnreachable = true, reachable = true))
        assertFalse("同样还没到时间、而且网络状态没变 → 等",
            shouldRetryNow(now, attempts = 3, nextAtMs = nextAt,
                wasUnreachable = true, reachable = false))
        assertTrue("到点了就该试",
            shouldRetryNow(nextAt, attempts = 3, nextAtMs = nextAt,
                wasUnreachable = false, reachable = true))
        assertFalse("次数用尽之后连'刚恢复'也不自动试（只留手动按钮）",
            shouldRetryNow(now, attempts = BackoffPolicy.MAX_AUTO_ATTEMPTS, nextAtMs = nextAt,
                wasUnreachable = true, reachable = true))
        assertTrue("从没排过下一次（nextAt=null）时不该卡死",
            shouldRetryNow(now, attempts = 0, nextAtMs = null,
                wasUnreachable = false, reachable = true))
    }

    @Test
    fun `网络恢复会把重试计数清零（否则进出隧道几轮就用光了）`() {
        assertEquals(0, attemptsAfterReachableChange(wasUnreachable = true, reachable = true, attempts = 7))
        assertEquals(7, attemptsAfterReachableChange(wasUnreachable = true, reachable = false, attempts = 7))
        assertEquals(7, attemptsAfterReachableChange(wasUnreachable = false, reachable = true, attempts = 7))
    }

    // ---------------------------------------------------------------- ④ 续传与顺序

    @Test
    fun `续传：前 k 段已确认时只传剩下的 且顺序正确`() {
        val seqs = listOf(1, 2, 3, 4, 5)
        assertEquals(listOf(4, 5), segmentsToUpload(seqs, acknowledged = 3))
        assertEquals("一段都没确认：全传（顺序升序）", listOf(1, 2, 3), segmentsToUpload(listOf(3, 1, 2), 0))
        assertEquals("全确认了：一段都不传", emptyList<Int>(), segmentsToUpload(seqs, acknowledged = 5))
        assertEquals("确认水位超过已有的段（服务端多认了）：也不该传出负数/越界",
            emptyList<Int>(), segmentsToUpload(seqs, acknowledged = 9))
    }

    @Test
    fun `确认水位只前进不后退`() {
        assertEquals(3, acknowledgedAfterUpload(2, 3))
        assertEquals("旧的重试回来了也不许把水位拉回去", 5, acknowledgedAfterUpload(5, 2))
        assertEquals(5, acknowledgedAfterUpload(5, 5))
    }

    // ---- 录音中途"联网 → 离线"的交接（水位怎么起）----

    @Test
    fun `中途转离线时水位要接上联网时已经送出去的段`() {
        // 联网已经送了 6 段（它们在电脑上，本地没有），转离线之后录到第 8 段：
        // 水位要抬到 6（不是 0），否则本地最大序号是 8、服务端那边是 8…… 看着一样？
        // 不：水位 0 时本地"最大序号"是 8 而服务端是 8 —— 对得上，但**续传会重传 1..6**
        // （那些本地压根没有文件，于是整份补传卡在第 1 段永远动不了）。
        assertEquals(6, watermarkAfterHandoff(sentOnline = 6, localSeqs = listOf(7, 8)))
    }

    @Test
    fun `中途转离线时水位不许超过清单里已有的段（否则续传会跳段）`() {
        // 联网送了 2 段就断了，然后本地录了第 3 段：
        //   水位抬到 2 → 本地最大 3、服务端 3 ✓
        assertEquals(2, watermarkAfterHandoff(sentOnline = 2, localSeqs = listOf(3)))
        // 联网送了 9 段，但本地清单里只有第 10 段（水位最多到 9 = maxLocal - 1）
        assertEquals(9, watermarkAfterHandoff(sentOnline = 9, localSeqs = listOf(10)))
        // 本地一段都没有时不许把水位抬起来（那时没什么可续的）
        assertEquals(0, watermarkAfterHandoff(sentOnline = 5, localSeqs = emptyList()))
        assertEquals(0, watermarkAfterHandoff(sentOnline = 0, localSeqs = listOf(1)))
        // 负数/越界输入不该造出负水位
        assertEquals(0, watermarkAfterHandoff(sentOnline = -3, localSeqs = listOf(1)))
    }

    @Test
    fun `中途转离线之后 续传只传本地真有的那几段`() {
        // 水位 6、本地只有 7/8 → 要传的就是 7/8（1..6 在电脑上，本地没有文件，
        // 但续传按水位跳过了它们）
        val seqs = listOf(7, 8)
        val acked = watermarkAfterHandoff(sentOnline = 6, localSeqs = seqs)
        assertEquals(listOf(7, 8), segmentsToUpload(seqs, acked))
    }

    @Test
    fun `清单的待传段与水位一致`() {
        val d = MeetingDraft(
            clientId = "c", startedAt = "t",
            segments = (1..4).map {
                DraftSegment(seq = it, file = "seg-$it.wav", startedAt = "t",
                    startOffsetSeconds = 0.0, durationSeconds = 1.0)
            },
            acknowledgedSegments = 2,
        )
        assertEquals(2, d.pendingSegments)
        assertEquals(2, d.confirmedSegments)
        assertEquals(listOf(3, 4), segmentsToUploadOf(d).map { it.seq })
        assertTrue(needsSync(d))
        assertFalse("对账完成的清单不再动它", needsSync(d.copy(uploaded = true)))
        assertFalse("没有段的清单没什么可传（**别**把它算成待补传）",
            needsSync(d.copy(segments = emptyList())))
    }

    // ---------------------------------------------------------------- ⑤ 对账

    @Test
    fun `对账一致才算通过`() {
        val v = reconcile(localSeqs = listOf(1, 2, 3), server = 3)
        assertTrue(v is ReconcileVerdict.Match)
        assertEquals(3, (v as ReconcileVerdict.Match).segments)
    }

    @Test
    fun `续传之后对账比的是本地传到第几段 而不是这一轮传了几段`() {
        // 这一条是真实踩过的坑（写第一版时就在这里红过）：续传时这一轮只发后两段，
        // 服务端报的是"它手上这一场总共几段"（5），而不是"这一轮收了几段"（2）。
        // 拿"水位当个数"（水位是 1 起的**序号**）去比，每一轮续传都会被判"不符"，
        // 于是功能看着没坏、却**永远不敢说已上传**。
        val v = reconcile(localSeqs = listOf(1, 2, 3, 4, 5), server = 5)
        assertTrue("本地到第 5 段、服务端也有 5 段 → 通过（哪怕这一轮只发了 2 段）：$v",
            v is ReconcileVerdict.Match)
    }

    @Test
    fun `本地清单缺段时不算对账通过（哪怕服务端数字看着对）`() {
        val v = reconcile(localSeqs = listOf(1, 3), server = 3)
        assertTrue("清单被截断过：那种'对上'是假的", v is ReconcileVerdict.Mismatch)
        assertTrue((v as ReconcileVerdict.Mismatch).note.contains("缺了第 2 段"))
    }

    @Test
    fun `中途转离线那种清单（前面几段只在电脑上）不算缺段`() {
        // 水位 6、本地只有 7/8：1..6 **不是洞** —— 它们本来就在电脑上。
        // 判成"缺段"会让这种清单永远传不完（用户什么都没做错）。
        val v = reconcile(localSeqs = listOf(7, 8), server = 8)
        assertTrue("本地这一段是连续的 a..b 就该算对：$v", v is ReconcileVerdict.Match)
        // 但本地自己中间有洞（7 与 9 之间缺 8）仍要判不符
        val holed = reconcile(localSeqs = listOf(7, 9), server = 9)
        assertTrue(holed is ReconcileVerdict.Mismatch)
    }

    @Test
    fun `本地一段都没有时不假装对上`() {
        val v = reconcile(localSeqs = emptyList(), server = 0)
        assertTrue(v is ReconcileVerdict.Mismatch)
    }

    @Test
    fun `服务端段数不符时如实报 且本地缓存不许删`() {
        // 需求点名："服务端段数不符 → 不删缓存、如实报"
        val fewer = reconcile(localSeqs = listOf(1, 2, 3, 4), server = 2)
        assertTrue(fewer is ReconcileVerdict.Mismatch)
        fewer as ReconcileVerdict.Mismatch
        assertEquals(4, fewer.sent)
        assertEquals(2, fewer.server)
        assertTrue("要说清朝哪个方向不对：${fewer.note}",
            fewer.note.contains("只认下 2 段") && fewer.note.contains("没删"))

        val more = reconcile(localSeqs = listOf(1, 2), server = 4)
        more as ReconcileVerdict.Mismatch
        assertTrue("服务端比本地多 = 幂等可能没做好（同一段存了两遍）：${more.note}",
            more.note.contains("两遍"))
    }

    @Test
    fun `服务端没回段数时不假装对账通过`() {
        val v = reconcile(localSeqs = listOf(1, 2), server = null)
        assertTrue("拿不到确切数字就说'没法对账'，绝不能偷偷算通过：$v",
            v is ReconcileVerdict.Mismatch)
        assertTrue((v as ReconcileVerdict.Mismatch).note.contains("没回段数"))
    }

    // ---------------------------------------------------------------- ⑥ 界面文案

    @Test
    fun `离线录音中的那一行如实说清'已缓存几段 等联网上传'`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.RECORDING_LOCAL, pendingSegments = 3, totalSegments = 3))
        assertTrue("要出现'离线'：$line", line.contains("离线"))
        assertTrue("要有已缓存段数：$line", line.contains("已缓存 3 段"))
        assertTrue("要说清还在等联网：$line", line.contains("等联网上传"))
        assertFalse("**不许**出现'已上传'（那是最容易撒的谎）：$line", line.contains("已上传"))
    }

    @Test
    fun `等待上传那一档不许说成已上传`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.WAITING_UPLOAD, pendingSegments = 5, confirmedSegments = 2))
        assertTrue(line, line.contains("已缓存 5 段"))
        assertTrue(line, line.contains("已传 2 段"))
        assertFalse("还没对账通过就不能说'已上传'：$line", line.contains("已上传"))
    }

    @Test
    fun `ECHO 正录另一场时说'等这场录完再传'而不是'正在传'`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.WAITING_MEETING_END, pendingSegments = 4))
        assertTrue(line, line.contains("正在录另一场"))
        assertTrue(line, line.contains("等这场录完再传"))
        assertTrue("要说清没有抢占：$line", line.contains("不抢占"))
        assertFalse("这时**没有**在传：$line", line.contains("正在传"))
    }

    @Test
    fun `不可达时先给真实原因 而不是套话`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.UNREACHABLE, pendingSegments = 2,
            note = "ECHO 连不上（HTTP null，ConnectException）"))
        assertTrue("真实原因必须出现（说错原因会把人引去查错的地方）：$line",
            line.contains("ConnectException"))
    }

    @Test
    fun `对账完成并收尾之后才说'补传并开始转写' 且说清本地副本还在`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.DONE, totalSegments = 6, stopped = true,
            retentionNote = OfflineMeetingRetention.retentionNote(7)))
        assertTrue("收尾成功才敢说**开始转写**（没有 stop 就没有转写）：$line",
            line.contains("开始转写") && line.contains("补传"))
        assertTrue("要如实说本地副本还在、按保留策略到期才清：$line",
            line.contains("还留着") && line.contains("备份"))
        assertFalse("**不许**再说'没删'那种含糊话（现在是'保留 N 天'）：$line", line.contains("没删"))
    }

    @Test
    fun `没收尾（没有 stop 的证据）时绝不说开始转写`() {
        // 说错这一句 = 把这次的根因又藏回去：用户会以为"已经有文字了"，而其实服务端从没被收尾
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.DONE, totalSegments = 6, stopped = false))
        assertFalse("没有收尾证据就不许说开始转写：$line", line.contains("开始转写"))
        assertTrue("要如实说还没收到收尾确认：$line", line.contains("收尾确认"))
    }

    @Test
    fun `没有离线活时不该占一行`() {
        assertEquals("", offlineStatusLine(OfflineSyncState()))
        assertEquals("", offlineStatusLine(OfflineSyncState(phase = OfflinePhase.IDLE)))
    }

    // ---------------------------------------------------------------- ⑦ 通知文案

    @Test
    fun `通知里离线优先于待重传 且绝不说已上传`() {
        val s = notificationText(MicState(
            active = true, offline = true, syncPending = 4,
            syncPhase = OfflinePhase.RECORDING_LOCAL, pending = 9))
        assertTrue("离线那件事比在线待重传更要紧：$s", s.contains("离线"))
        assertTrue(s, s.contains("4 段"))
        assertFalse(s, s.contains("已上传"))
    }

    @Test
    fun `通知里错误仍然最优先（离线也不能盖掉真的可能丢音频）`() {
        val s = notificationText(MicState(
            active = true, offline = true, syncPending = 4, error = "有一段缓存不下来，可能真的丢了"))
        assertEquals("有一段缓存不下来，可能真的丢了", s)
    }

    @Test
    fun `通知里没有离线活时行为与改动前一致`() {
        assertTrue(notificationText(MicState(active = true, sent = 7)).contains("已送 7 段"))
        assertTrue(notificationText(MicState(active = true, sent = 4, pending = 2)).contains("2 段待重传"))
        assertTrue(notificationText(MicState(active = true, paused = true)).contains("已静音"))
    }

    // ---------------------------------------------------------------- ⑧ 状态迁移（逐档）

    @Test
    fun `状态迁移：录 → 等 → 传 → 对账完成 且每一步的文案都对`() {
        // ① 离线录音
        var st = OfflineSyncState(phase = OfflinePhase.RECORDING_LOCAL,
            pendingSegments = 2, totalSegments = 2)
        assertTrue(st.hasWork)
        assertTrue(offlineStatusLine(st).contains("等联网上传"))
        // ② 录完了，等联网
        st = st.copy(phase = OfflinePhase.WAITING_UPLOAD)
        assertTrue(offlineStatusLine(st).contains("已缓存 2 段"))
        // ③ 正在传（第 1 段已经传上去了）
        st = st.copy(phase = OfflinePhase.UPLOADING, confirmedSegments = 1, pendingSegments = 1)
        assertTrue(offlineStatusLine(st).contains("已传 1/2 段"))
        // ④ 对账通过
        st = st.copy(phase = OfflinePhase.DONE, confirmedSegments = 2, pendingSegments = 0)
        assertTrue(offlineStatusLine(st).contains("核对一致"))
    }

    @Test
    fun `每一档的文案都互不相同（别都写成同一句）`() {
        val phases = OfflinePhase.entries.filter { it != OfflinePhase.IDLE }
        val lines = phases.map {
            offlineStatusLine(OfflineSyncState(
                phase = it, pendingSegments = 2, confirmedSegments = 1, totalSegments = 3,
                note = "原因"))
        }
        assertEquals("有重复的文案 = 用户分不出自己在哪一档", lines.size, lines.toSet().size)
        assertTrue(lines.none { it.isBlank() })
        assertNotNull(phases.firstOrNull { it == OfflinePhase.DONE })
    }
}
