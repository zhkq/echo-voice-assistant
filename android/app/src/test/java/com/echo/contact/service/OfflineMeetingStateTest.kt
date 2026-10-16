package com.echo.contact.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **手机录音的状态机与它的判据**。
 *
 * 这一组用例盯的全是"会静默出错"的地方：
 *  * 连不上时**不许**变成"录音失败"（那等于什么都没录）；
 *  * 状态文案里**不许**把"没传上去"说成"已上传"；
 *  * ECHO 正录着别的会时**不许**抢占；
 *  * 退避**必须有上限**（不然断网一晚上把电池和流量烧光）；
 *  * 重试要有次数上限，但**网络刚恢复时要立刻试**（用户能感知的只有这一下）。
 *
 * ⚠️ 2026-10-17 用户把"在线/离线两条路"**合并成一条**（*"离线录音和在线录音就没什么
 * 本质区别了"*）之后，这里原来那几条钉"双路"的用例被**改写成钉新契约**：
 *  * `startDecision` / `StopDecision` / `watermarkAfterHandoff` 的判据**退役**了
 *    （它们存在的意义就是"按可达性分岔"，而那条分岔已经删掉）——
 *    换成"录音中那一档的文案"与"暂停上报端点缺失的判定"；
 *  * 通知文案里 `offline` 那一组字段也没了（不再有"离线模式"这个状态），
 *    改成按"**还有几段没传上去**"说 —— 那条铁律（绝不说已上传）一个字都没放松。
 */
class OfflineMeetingStateTest {

    // ---------------------------------------------------------------- ① 不可达 → 也照样录（只有一条路）

    @Test
    fun `开始录音不再按可达性分岔（合并成一条路之后）`() {
        // 老版本这里是 `startDecision(paired, reachable)` 三个分支；合并之后开始录音
        // **只有一个动作**：在本地把这一场立起来。可达性不再参与"能不能录"的判定 ——
        // 它只影响后面"顺手传上去的那一下"。这条判据的落点在 MeetingRecordSession
        // （见 MeetingRecordSessionTest/MeetingSinglePathTest），这里钉的是**状态机的口径**：
        // 录音中那一档与"网通不通"无关。
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.RECORDING_LOCAL, totalSegments = 2, pendingSegments = 2))
        assertTrue("录音中就是录音中，不该出现'离线'这个模式：$line", !line.contains("离线"))
        assertTrue(line, line.contains("本机录音中"))
        assertTrue("网通不通只作为**原因**出现", line.contains("还没传"))
    }

    @Test
    fun `探活返回非 2xx 与抛异常都算不可达`() {
        assertFalse(isReachable(null))          // 抛异常（超时/拒绝连接）
        assertFalse(isReachable(0))
        assertFalse(isReachable(401))           // 连得上但令牌不对：这条路一样开不了会
        assertFalse(isReachable(500))
        assertFalse(isReachable(302))
        assertTrue(isReachable(200))
        assertTrue(isReachable(204))
        assertEquals(Reachability.UNREACHABLE, reachabilityOf(null))
        assertEquals(Reachability.REACHABLE, reachabilityOf(200))
    }

    @Test
    fun `录音中即使连不上也不许说录音失败`() {
        // 需求原话："开始/结束录音时若 ECHO 不可达……不要让录音失败，而是进入离线录音"
        // 合并成一条路之后这句话更强了：**录不录得下来与服务端无关**，
        // 状态里只有"还有几段没传上去"，没有一个"失败"档是给网络准备的。
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.UNREACHABLE, pendingSegments = 3,
            note = "ECHO 连不上（HTTP null，ConnectException）"))
        assertTrue(line, line.contains("ECHO 连不上"))
        assertTrue("原因照带：$line", line.contains("ConnectException"))
        assertTrue("音频还在手机上（已缓存）：$line", line.contains("已缓存 3 段"))
        assertFalse("**不许**说录音失败：$line", line.contains("失败"))
    }

    // ---------------------------------------------------------------- ③ 暂停上报（端点缺失的判定）

    @Test
    fun `老服务端没有暂停端点时认得出来（那一档不算失败）`() {
        // 这一条的后果是"补传会不会被一个可有可无的上报拖垮"
        assertTrue(looksEndpointMissing("", 404))
        assertTrue(looksEndpointMissing("", 405))
        assertTrue(looksEndpointMissing("", 501))
        assertTrue("有些实现用 200 + 一句话表达同一件事",
            looksEndpointMissing("不支持的接口", 200))
        assertTrue(looksEndpointMissing("Not Found", 200))
        assertFalse("5xx 是'它自己坏了'，不是'没这个接口'（原因要说准）",
            looksEndpointMissing("", 500))
        assertFalse("连不上不是端点不存在", looksEndpointMissing("", null))
        assertFalse(looksEndpointMissing("参数不对", 422))
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

    // ---- 段序号接续（合并成一条路之后不再需要"中途转离线"的交接）----

    @Test
    fun `中途转离线那套水位交接退役了（每一段都先写本地）`() {
        // 老版本这里钉的是 `watermarkAfterHandoff`：联网那条路把段**先发给电脑、本地不留**，
        // 于是"录到一半断网"时补水位的算法很关键。用户 2026-10-17 把两条路合并之后，
        // **每一段都先落本地**（见 MeetingRecordSession），那条交接的前提（"前几段只在电脑上"）
        // 不存在了 —— 所以那个纯函数连它一起退役。
        // 这条用例把**退役这件事本身**钉住：清单的段号只由"本地已有的最大序号"与
        // "服务端已认下的水位"决定，取两者的大值（不会重号、也不会回退）。
        val d = MeetingDraft(
            clientId = "c", startedAt = "2026-10-17T09:30:00",
            segments = (4..5).map {
                DraftSegment(seq = it, file = "seg-000$it.flac", startedAt = "t",
                    startOffsetSeconds = 0.0, durationSeconds = 1.0,
                    format = com.echo.contact.audio.SegmentFormat.FLAC)
            },
            acknowledgedSegments = 3,
        )
        assertEquals("水位 3、本地 4/5 → 待传的就是 4/5（前面几段确实在服务端）",
            listOf(4, 5), segmentsToUpload(d.segments.map { it.seq }, d.acknowledgedSegments))
        assertEquals(5, acknowledgedAfterUpload(d.acknowledgedSegments, 5))
        assertTrue(needsSync(d))
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
    fun `录音中那一行如实说清'已缓存几段'且绝不说已上传`() {
        val line = offlineStatusLine(OfflineSyncState(
            phase = OfflinePhase.RECORDING_LOCAL, pendingSegments = 3, totalSegments = 3))
        assertTrue("这是本机录音那一档（不再是'离线模式'）：$line", line.contains("本机录音中"))
        assertTrue("要有已缓存段数：$line", line.contains("已缓存 3 段"))
        assertTrue("要说清还有几段没传：$line", line.contains("还没传"))
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
    fun `没有本机录音活时不该占一行`() {
        assertEquals("", offlineStatusLine(OfflineSyncState()))
        assertEquals("", offlineStatusLine(OfflineSyncState(phase = OfflinePhase.IDLE)))
    }

    // ---------------------------------------------------------------- ⑦ 通知文案

    @Test
    fun `通知里有段没传上去时先说那件事 且绝不说已上传`() {
        // ⚠️ 改写自"通知里离线优先于待重传"：合并成一条路之后不再有"离线模式"这个字段，
        // 判据换成"**还有几段没传上去**"（同一个铁律：这件事比"已传 N 段"要紧得多）
        val s = notificationText(MicState(
            active = true, syncPending = 4, syncPhase = OfflinePhase.RECORDING_LOCAL,
            syncNote = "本机录音中 · 已缓存 4 段 · ECHO 连不上（超时）"))
        assertTrue("没传上去那件事最要紧：$s", s.contains("已缓存 4 段"))
        assertTrue("真实原因照带：$s", s.contains("ECHO 连不上"))
        assertFalse(s, s.contains("已上传"))
    }

    @Test
    fun `通知里错误最优先（真的可能丢音频盖过一切）`() {
        val s = notificationText(MicState(
            active = true, syncPending = 4, error = "有一段缓存不下来，可能真的丢了"))
        assertEquals("有一段缓存不下来，可能真的丢了", s)
    }

    @Test
    fun `通知里没有欠的段时行为与改动前一致`() {
        assertTrue(notificationText(MicState(active = true, sent = 7)).contains("已传 7 段"))
        assertTrue(notificationText(MicState(active = true, syncPending = 2))
            .contains("已缓存 2 段"))
        assertTrue(notificationText(MicState(active = true, paused = true)).contains("已静音"))
    }

    @Test
    fun `通知里录完了但还在传时不许说'在录'`() {
        // 用户按了结束之后最怕的是"关了就完了"—— 那一刻还在补传，通知必须说清楚
        val s = notificationText(MicState(active = false, syncPending = 2,
            syncNote = "录音已存在本机 · 已缓存 2 段 · 等联网上传"))
        assertTrue(s, s.contains("录音已结束"))
        assertTrue(s, s.contains("已缓存 2 段"))
        assertFalse("已经不在录了：$s", s.contains("在录"))
    }

    // ---------------------------------------------------------------- ⑧ 状态迁移（逐档）

    @Test
    fun `状态迁移：录 → 等 → 传 → 对账完成 且每一步的文案都对`() {
        // ① 本机录音（每一段先落本地）
        var st = OfflineSyncState(phase = OfflinePhase.RECORDING_LOCAL,
            pendingSegments = 2, totalSegments = 2)
        assertTrue(st.hasWork)
        assertTrue(offlineStatusLine(st).contains("本机录音中"))
        // ② 录完了，还欠着
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
