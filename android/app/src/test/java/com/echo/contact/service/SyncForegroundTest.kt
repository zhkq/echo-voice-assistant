package com.echo.contact.service

import com.echo.contact.ui.BATTERY_OPTIMIZATIONS_ACTION
import com.echo.contact.ui.MEETING_SYNC_BATTERY_NOTE
import com.echo.contact.ui.batterySettingsIntent
import com.echo.contact.ui.functionBody
import com.echo.contact.ui.moduleText
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * **补传在锁屏后不被掐断**的判据（用户 2026-10-19 深夜：
 * *"锁屏后，再解锁就要按一下重试，echo 需要后台可运行"*）。
 *
 * ## 一句话说清病根
 *
 * 补传（`ACTION_SYNC` → `runOfflineSync`）原来跑在**一个后台服务**里（`syncNow` 用
 * `startService`），自动那条路更糟 —— 它是 App 进程里一个裸协程
 * （`tickFromForeground` 的 `CoroutineScope(...).launch`）。屏幕一关，进程被冻结/掐掉，
 * 正在传的那一段（10 分钟 ≈18 MB、要跑几分钟）就断在那里，而且**没有任何东西告诉用户它停了**。
 *
 * ## 修法（这一份文件钉的就是这四件事）
 *
 *  1. **上传即前台**：一开同步就 `startForeground`（类型 `dataSync`）+ 一条常驻通知，
 *     内容每段更新（"已传 2/5 段（第 3 段 12.4 MB）"）；
 *  2. **持锁**：`PARTIAL_WAKE_LOCK`（带超时，按字节估、上限 30 分钟）+ `WifiLock`；
 *  3. **传完即撤**：完成/失败/取消都 `stopForeground(REMOVE)` + 放掉全部锁
 *     （`try/finally` 的反证：块里抛异常也一样）；
 *  4. **不传时零通知**：没有待传就不进前台、不起服务（"绝不因此变成常驻"）。
 *
 * ## 为什么这一整套是纯逻辑 + 一个 fake
 *
 * `startForeground` / `WakeLock` 都是 Android 类，JVM 用例里一碰就抛（stub `android.jar`）。
 * 所以"该发生什么、按什么顺序发生"收在 `SyncForeground`（一个 Android 类都不 import），
 * 这里注入一个记账的 [FakeHost]；Android 那一层（真的通知/锁/类型）只做**源码级**断言。
 */
class SyncForegroundTest {

    // ---------------------------------------------------------------- fake 前台 + 锁

    /**
     * 记账用的假前台：**只记调用序列与内容**，什么都不做。
     *
     * 为什么记序列而不只记次数：这一轮的两个真错法都是**顺序**错 ——
     * 先发请求再进前台（那几秒正好是系统掐它的窗口）、先发收尾通知再 `stopForeground`
     * （收尾那条会被顺手删掉，用户根本看不到"传完了"）。
     */
    private class FakeHost(
        /** 录音那条前台是不是挂着（true = 录音中顺手补传）。 */
        var recording: Boolean = false,
    ) : SyncForegroundHost {

        val calls = mutableListOf<String>()
        val foregroundTexts = mutableListOf<String>()
        val updateTexts = mutableListOf<String>()
        val finishTexts = mutableListOf<Pair<String, Boolean>>()

        var channelCalls = 0
        var startForegroundCalls = 0
        var stopForegroundCalls = 0
        var acquireCalls = 0
        var extendCalls = 0
        var releaseCalls = 0
        var lastTimeoutMs = 0L

        /** 现在手上有没有锁（`releaseLocks` 之后必须是 false）。 */
        var locks = false
            private set

        override fun ensureChannel() {
            channelCalls += 1
            calls += "channel"
        }

        override fun recordingForeground(): Boolean = recording

        override fun startForeground(text: String) {
            startForegroundCalls += 1
            foregroundTexts += text
            calls += "start"
        }

        override fun updateForeground(text: String) {
            updateTexts += text
            calls += "update"
        }

        override fun stopForegroundRemove() {
            stopForegroundCalls += 1
            calls += "stop"
        }

        override fun postFinish(text: String, failed: Boolean) {
            finishTexts += text to failed
            calls += "finish"
        }

        override fun acquireLocks(timeoutMs: Long) {
            acquireCalls += 1
            lastTimeoutMs = timeoutMs
            locks = true
            calls += "acquire"
        }

        override fun extendLocks(timeoutMs: Long) {
            extendCalls += 1
            lastTimeoutMs = timeoutMs
            calls += "extend"
        }

        override fun releaseLocks() {
            releaseCalls += 1
            locks = false
            calls += "release"
        }
    }

    private fun progress(
        confirmed: Int,
        total: Int = 5,
        seq: Int = 0,
        bytes: Long = 0,
    ) = SyncProgress(
        drafts = 1,
        totalSegments = total,
        confirmedSegments = confirmed,
        pendingSegments = (total - confirmed).coerceAtLeast(0),
        currentSeq = seq,
        currentBytes = bytes,
    )

    // ---------------------------------------------------------------- ① 开始 → 进前台

    /** 开始同步 → **调用了 startForeground**，而且那条通知的内容里有 "0/5"。 */
    @Test
    fun `开始同步就进前台而且通知里有已传几分之几`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        assertTrue(fg.begin(totalSegments = 5, totalBytes = 5 * 18L * 1024 * 1024))

        assertEquals("必须真的进了前台", 1, host.startForegroundCalls)
        val text = host.foregroundTexts.single()
        assertTrue("通知内容要能对上账（0/5），实际：$text", text.contains("0/5"))
        assertEquals("渠道 echo_sync 要建出来", 1, host.channelCalls)
        assertEquals("渠道 id 就是 echo_sync", "echo_sync", SYNC_CHANNEL_ID)
        assertTrue("锁要拿到", fg.locksHeld)
        assertTrue(host.locks)
        assertTrue("锁必须带超时（> 0）", host.lastTimeoutMs > 0L)
        assertTrue("锁的超时不许超过硬上限", host.lastTimeoutMs <= MAX_LOCK_MS)
        // 拿锁必须在**进前台之后**（`startForegroundService` 给的窗口只有几秒）
        assertTrue(host.calls.indexOf("start") < host.calls.indexOf("acquire"))
    }

    // ---------------------------------------------------------------- ② 每段 → 更新同一条通知

    /** 每段完成 → **通知内容更新**（不是只更一次），而且只有一条常驻通知。 */
    @Test
    fun `每段都更新同一条通知而不是只更一次`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        fg.begin(totalSegments = 5, totalBytes = 0)

        // 第 3 段开始传（12.4 MB）：用户点名的那句
        fg.progress(progress(confirmed = 2, seq = 3, bytes = 13_002_342L))
        // 第 3 段被服务端收下、第 4 段开始
        fg.progress(progress(confirmed = 3, seq = 4, bytes = 18_000_000L, total = 5))

        assertEquals("只进一次前台（后面都是更新同一条）", 1, host.startForegroundCalls)
        assertEquals("两段各更新一次", 2, host.updateTexts.size)
        assertEquals("已传 2/5 段（第 3 段 12.4 MB）", host.updateTexts[0])
        assertTrue("第二句要跟上（3/5），实际：${host.updateTexts[1]}",
            host.updateTexts[1].contains("3/5"))
        assertNotEquals("两次内容必须不一样（否则看着像没动）",
            host.updateTexts[0], host.updateTexts[1])
        assertTrue("每段都要续锁（估的时长可能不够）", host.extendCalls >= 2)
    }

    // ---------------------------------------------------------------- ③ 完成 → 撤前台 + 放锁

    /** 完成 → `stopForeground(REMOVE)` + **锁全部释放** + 一条"完成"通知。 */
    @Test
    fun `完成就撤前台放锁并发一条完成通知`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        fg.begin(totalSegments = 5, totalBytes = 0)
        fg.progress(progress(confirmed = 2, seq = 3, bytes = 1024))

        fg.finish(SyncEnd.Done(segments = 5, drafts = 1, transcribing = true))

        assertEquals("前台必须撤掉（不许变成常驻）", 1, host.stopForegroundCalls)
        assertFalse("锁必须全放", fg.locksHeld)
        assertFalse(host.locks)
        assertFalse("这一轮已经结束", fg.active)
        val (text, failed) = host.finishTexts.single()
        assertTrue("收尾通知要说清结果：$text", text.contains("5 段"))
        assertTrue("收尾了才敢说转写：$text", text.contains("转写"))
        assertFalse("这不是失败", failed)
        // 先撤前台再发收尾通知（否则收尾那条会被 stopForeground 一起删掉）
        assertTrue(host.calls.indexOf("stop") < host.calls.indexOf("finish"))
    }

    /**
     * 收了尾但服务端没收尾（`transcribing = false`）→ **不许说"开始转写"**。
     *
     * 这是这条链上最贵的一句谎话：没有 `stop` 就没有转写，说了就是"看着成了、其实永远不出文字"。
     */
    @Test
    fun `只传上去没收尾时不许说开始转写`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        fg.begin(3, 0)
        fg.finish(SyncEnd.Done(segments = 3, drafts = 1, transcribing = false))
        val text = host.finishTexts.single().first
        assertFalse("不许说开始转写：$text", text.contains("已经开始转写"))
        assertTrue("要如实说没收尾：$text", text.contains("收尾"))
    }

    // ---------------------------------------------------------------- ④ 失败/取消 → 锁也放

    /** 失败 → 锁也释放，而且收尾通知**带着真实原因**。 */
    @Test
    fun `失败也放锁而且原因要带出来`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        fg.begin(5, 0)
        fg.finish(SyncEnd.Failed("服务端拒收这一段（HTTP 413）"))

        assertFalse("失败也必须放锁", fg.locksHeld)
        assertFalse(host.locks)
        assertEquals("前台照样撤", 1, host.stopForegroundCalls)
        val (text, failed) = host.finishTexts.single()
        assertTrue("原因原文要带出来：$text", text.contains("413"))
        assertTrue("这一条是失败通知", failed)
    }

    /**
     * **块里抛异常** → 锁照样释放（`try/finally` 的反证），而且把异常类型说出来。
     *
     * 为什么这条最要紧：断了网/进程被杀时，真机上看到的就是"它不传了，也不说什么"。
     * 如果只有正常路径放锁，一次异常就能让 `PARTIAL_WAKE_LOCK` 挂一整夜
     * （用户第二天没电 —— 那比"这一段没传完"糟得多）。
     */
    @Test
    fun `块里抛异常也放锁而且说出被什么打断`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        var thrown: Throwable? = null
        runBlocking {
            try {
                fg.guard(
                    totalSegments = 5,
                    totalBytes = 1024,
                    block = { throw IOException("boom") },
                    endOf = { SyncEnd.Done(0, 0, false) },
                )
            } catch (t: Throwable) {
                thrown = t
            }
        }
        assertNotNull("异常要照原样抛回去（这一层不吞）", thrown)
        assertFalse("异常路径上锁也必须放掉", fg.locksHeld)
        assertFalse(host.locks)
        assertEquals("前台照样撤", 1, host.stopForegroundCalls)
        val (text, failed) = host.finishTexts.single()
        assertTrue("要说清被什么打断：$text", text.contains("IOException"))
        assertTrue(failed)
    }

    /** 正常路径（`guard` 跑完）→ 完成通知 + 放锁，一个都不许漏。 */
    @Test
    fun `guard 正常跑完会发完成通知并放锁`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        val result = runBlocking {
            fg.guard(
                totalSegments = 2,
                totalBytes = 4_000_000,
                block = {
                    fg.progress(progress(confirmed = 1, total = 2, seq = 2, bytes = 2_000_000))
                    "ok"
                },
                endOf = { SyncEnd.Done(segments = 2, drafts = 1, transcribing = true) },
            )
        }
        assertEquals("ok", result)
        assertEquals(1, host.startForegroundCalls)
        assertEquals(1, host.stopForegroundCalls)
        assertFalse(fg.locksHeld)
        assertTrue("进度发过一次", host.updateTexts.size == 1)
    }

    // ---------------------------------------------------------------- ⑤ 不传时零通知

    /** **没在传时不起前台**（防"悄悄常驻"）：判据函数 + 空转的 coordinator 两层都钉住。 */
    @Test
    fun `没在传时一条通知都不发也不拿锁`() {
        assertFalse("没有待传 → 不进前台", shouldEnterSyncForeground(0))
        assertTrue("有待传 → 进前台", shouldEnterSyncForeground(1))

        val host = FakeHost()
        val fg = SyncForeground(host)
        // 没有 begin（没有真在传）：progress / finish / releaseAll 必须全是空操作
        fg.progress(progress(confirmed = 1, seq = 2, bytes = 1024))
        fg.finish(SyncEnd.Failed("不该出现的通知"))
        fg.releaseAll()

        assertEquals(0, host.startForegroundCalls)
        assertEquals(0, host.updateTexts.size)
        assertEquals(0, host.stopForegroundCalls)
        assertEquals("没在传就不许发收尾通知", 0, host.finishTexts.size)
        assertEquals(0, host.acquireCalls)
        assertEquals(0, host.channelCalls)
    }

    // ---------------------------------------------------------------- ⑥ 渠道 / 超时 / 不抢录音前台

    /** 渠道 `echo_sync` **只建一次**（begin 被重复调到也一样）。 */
    @Test
    fun `通知渠道只建一次`() {
        val host = FakeHost()
        val fg = SyncForeground(host)
        fg.begin(3, 0)
        fg.begin(3, 0)   // 重复的 begin 是空操作（不许把前台再进一遍）
        assertEquals(1, host.channelCalls)
        assertEquals(1, host.startForegroundCalls)
    }

    /**
     * **自动那一轮失败不发通知**（否则每几分钟弹一条"补传没成" = 骚扰），但成功照发。
     *
     * 这是 `SyncEnd.Quiet` 的唯一用途：后台节拍起的轮次，失败原因已经写在会议页那块入口里
     * （`syncReasonText` 分档说人话），不需要再占一次通知栏。
     */
    @Test
    fun `自动那一轮失败不发通知但成功照发`() {
        val quietHost = FakeHost()
        val quiet = SyncForeground(quietHost)
        quiet.begin(3, 0)
        quiet.finish(SyncEnd.Quiet("连不上 ECHO"))
        assertEquals("自动失败不许弹通知", 0, quietHost.finishTexts.size)
        assertEquals("但前台照样撤", 1, quietHost.stopForegroundCalls)
        assertFalse("锁照样放", quiet.locksHeld)

        val doneHost = FakeHost()
        val done = SyncForeground(doneHost)
        done.begin(3, 0)
        done.finish(SyncEnd.Done(segments = 3, drafts = 1, transcribing = true))
        assertEquals("成功要告诉用户一声", 1, doneHost.finishTexts.size)

        // 服务里：自动轮次把失败翻成 Quiet（别在 UI 层再补一条）
        val svc = moduleText("src/main/java/com/echo/contact/service/MeetingMicService.kt")
        assertTrue(svc.contains("syncEndOf(outcome, quietFailure = auto)"))
        assertTrue(svc.contains("SyncEnd.Quiet("))
        assertTrue("自动轮次要能被认出来（extra）", svc.contains("EXTRA_AUTO_SYNC"))
    }

    /** **没配对**（一条请求都发不出去）时也不进前台：白白占一条通知与一块电池。 */
    @Test
    fun `没配对时不进前台`() {
        assertFalse("没配对 → 不进前台", shouldEnterSyncForeground(3, paired = false))
        assertTrue("配对了且有待传 → 进前台", shouldEnterSyncForeground(3, paired = true))
        assertFalse("哪怕有待传，配对是前提", shouldEnterSyncForeground(0, paired = true))
    }

    /** 锁的超时**按字节估、有下限也有硬上限**（"所有锁必须有超时"）。 */
    @Test
    fun `锁超时按字节估而且有上下限`() {
        assertEquals("估不出来就取下限（绝不给 0）", MIN_LOCK_MS, uploadLockTimeoutMs(0))
        assertEquals("负数同样取下限", MIN_LOCK_MS, uploadLockTimeoutMs(-1))

        val oneSegment = uploadLockTimeoutMs(18L * 1024 * 1024)
        assertTrue("18 MB 一段要更久：$oneSegment", oneSegment > MIN_LOCK_MS)
        assertTrue(oneSegment <= MAX_LOCK_MS)

        val huge = uploadLockTimeoutMs(10L * 1024 * 1024 * 1024)
        assertEquals("再大也不许超过 30 分钟（那是忘记释放的保险丝）", MAX_LOCK_MS, huge)
        assertEquals("上限就是 30 分钟", 30L * 60_000L, MAX_LOCK_MS)
    }

    /** 录音中顺手补传：**不许抢走/撤掉录音那条前台**，但锁照样要拿要放。 */
    @Test
    fun `录音中的前台不被补传抢走但锁照样放`() {
        val host = FakeHost(recording = true)
        val fg = SyncForeground(host)
        fg.begin(3, 0)
        // 仍然要调 startForeground（`startForegroundService` 的 5 秒义务），但挂的是录音那条
        assertEquals(1, host.startForegroundCalls)
        assertFalse("这条前台不是我们的", fg.foregroundOwned)
        assertTrue("锁照样要拿", fg.locksHeld)

        fg.finish(SyncEnd.Done(segments = 3, drafts = 1, transcribing = true))
        assertEquals("不许撤录音那条前台", 0, host.stopForegroundCalls)
        assertFalse("锁照样要放", fg.locksHeld)
    }

    /** "12.4 MB" 那个格式化（0 / 负数 → 空串：**没数就不编数**）。 */
    @Test
    fun `字节数格式化不编数`() {
        assertEquals("", humanMb(0))
        assertEquals("", humanMb(-5))
        assertEquals("12.4 MB", humanMb(13_002_342L))
        assertEquals("18.0 MB", humanMb(18L * 1024 * 1024))
    }

    /** 不知道段大小 → 通知里只报段号（**绝不编一个大小**）。 */
    @Test
    fun `不知道段大小时只报段号`() {
        val text = syncNotifyText(progress(confirmed = 2, seq = 3, bytes = 0))
        assertEquals("已传 2/5 段（第 3 段）", text)
        assertFalse("不许出现 MB", text.contains("MB"))
    }

    // ---------------------------------------------------------------- ⑦ 源码级（Android 那一层）

    /** 清单：`dataSync` 类型 + 它要的两个权限 + 两种锁的权限；录音那条类型**不许被删掉**。 */
    @Test
    fun `清单里声明了 dataSync 与它要的权限`() {
        val manifest = moduleText("src/main/AndroidManifest.xml")
        assertTrue("Android 14 要这个权限才能用 dataSync",
            manifest.contains("android:name=\"android.permission.FOREGROUND_SERVICE_DATA_SYNC\""))
        assertTrue("PARTIAL_WAKE_LOCK / WifiLock 都要它",
            manifest.contains("android:name=\"android.permission.WAKE_LOCK\""))
        assertTrue("取 WifiManager 建 WiFi 锁",
            manifest.contains("android:name=\"android.permission.ACCESS_WIFI_STATE\""))
        assertTrue("同一个服务两个前台类型（录音 microphone + 补传 dataSync）",
            manifest.contains("android:foregroundServiceType=\"microphone|dataSync\""))
        assertTrue("录音那条权限不许被删掉",
            manifest.contains("android:name=\"android.permission.FOREGROUND_SERVICE_MICROPHONE\""))
        assertTrue("「去设置」要的权限（只在用户点时用）",
            manifest.contains("android:name=\"android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS\""))
    }

    /** Android 那一层：`dataSync` 类型、两种锁、WiFi 锁**自己加**超时、常驻通知不可滑掉。 */
    @Test
    fun `Android 那一层用 dataSync 类型和两种带超时的锁`() {
        val src = moduleText("src/main/java/com/echo/contact/service/SyncForegroundAndroid.kt")
        assertTrue(src.contains("ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC"))
        assertTrue(src.contains("PowerManager.PARTIAL_WAKE_LOCK"))
        assertTrue("弱网/锁屏下 WiFi 会被省电降级", src.contains("WifiManager.WIFI_MODE_FULL_HIGH_PERF"))
        assertTrue("WifiLock 没有系统超时，必须自己收（看门狗）", src.contains("delay(timeout)"))
        assertTrue("常驻通知不可滑掉", src.contains(".setOngoing(true)"))
        assertTrue("每段只更新数字、不重复响", src.contains(".setOnlyAlertOnce(true)"))
        assertTrue("渠道 id 走同一个常量", src.contains("SYNC_CHANNEL_ID"))
        assertTrue("撤销前台时必须一并删掉那条通知",
            src.contains("STOP_FOREGROUND_REMOVE"))
    }

    /** 纯逻辑那一层**不许** import Android（否则用例就跑不了，判据也就没了）。 */
    @Test
    fun `补传前台这一层是纯逻辑可以 JVM 测`() {
        val src = moduleText("src/main/java/com/echo/contact/service/SyncForeground.kt")
        assertFalse("SyncForeground.kt 里不许出现 import android.", src.contains("import android."))
        assertTrue("但 Android 那一层要真的实现它",
            moduleText("src/main/java/com/echo/contact/service/SyncForegroundAndroid.kt")
                .contains(": SyncForegroundHost"))
    }

    /** 服务里：**先进前台再发请求**；结束录音那次收尾也持锁；`onDestroy` 再放一次锁。 */
    @Test
    fun `服务里补传一定先进前台再发请求`() {
        val svc = moduleText("src/main/java/com/echo/contact/service/MeetingMicService.kt")

        // ① 「现在同步」/ 后台节拍那条路
        val branch = functionBody(svc, "ACTION_SYNC -> scope.launch {", "else -> {")
        assertTrue("要先判有没有待传（没待传就零通知）", branch.contains("shouldEnterSyncForeground"))
        assertTrue("进前台 + 拿锁 + 撤干净都在 guard 里", branch.contains("syncForeground.guard("))
        assertTrue("判据必须排在进前台之前",
            branch.indexOf("shouldEnterSyncForeground") < branch.indexOf("syncForeground.guard("))
        assertTrue("真发请求的那段在 guard 的块里（先挂前台）",
            branch.indexOf("syncForeground.guard(") < branch.indexOf("runOfflineSync("))

        // ② 结束录音那次收尾（用户按完结束就把手机锁屏塞兜里，最常见）
        val stop = functionBody(svc, "private fun stopEverything(", "private suspend fun awaitSealQueueIdle(")
        assertTrue("收尾那一轮也要持锁/进前台", stop.contains("syncForeground.guard("))

        // ③ 服务被收掉时再放一次锁（finally 覆盖不到的那些路）
        val destroy = functionBody(svc, "override fun onDestroy()", "private fun startForegroundCompat()")
        assertTrue("onDestroy 必须再 releaseAll 一次", destroy.contains("syncForeground.releaseAll()"))

        // ④ 「现在同步」必须是**前台**方式起服务（后台服务锁屏就被掐）
        val syncNow = functionBody(svc, "fun syncNow(context: Context)", "// ------")
        assertTrue(syncNow.contains("startForegroundServiceCompat("))
        assertFalse("不许再用后台 startService 起补传", syncNow.contains("app.startService("))
        assertTrue("没有待传就**不起服务**（零通知）", syncNow.contains("shouldEnterSyncForeground"))

        // ⑤ 逐段进度要能刷到那条前台通知
        assertTrue("每段开始/每段成功都要刷通知",
            svc.contains("publishSyncProgress(p)"))
    }

    // ---------------------------------------------------------------- ⑧ 会议页那句说明 + 去设置

    /** 电池优化那句**在会议页「待上传」那块**，而且"去设置"只在用户点时跳。 */
    @Test
    fun `会议页待上传那块有电池优化的如实说明和去设置`() {
        val src = moduleText("src/main/java/com/echo/contact/ui/MeetingSyncEntry.kt")
        val block = src.substringAfter("internal fun MeetingSyncBlock(")
        assertTrue("那块里必须画出这句说明", block.contains("MEETING_SYNC_BATTERY_NOTE"))
        assertTrue("必须有「去设置」入口", block.contains("openBatteryOptimizations("))
        assertFalse("不许自动弹系统确认框（只在用户点时跳）", block.contains("LaunchedEffect"))
        // 说明本身要如实：两件事都要说到
        assertTrue("要说清锁屏也能传：$MEETING_SYNC_BATTERY_NOTE", MEETING_SYNC_BATTERY_NOTE.contains("锁屏"))
        assertTrue("也要说清仍被拦时怎么办", MEETING_SYNC_BATTERY_NOTE.contains("电池优化"))
    }

    /** 「去设置」用的是系统的"忽略电池优化"动作（action + data URI 逐字钉住）。 */
    @Test
    fun `去设置用的是系统的忽略电池优化动作`() {
        val spec = batterySettingsIntent("com.echo.contact")
        assertEquals("android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS", spec.action)
        assertEquals(BATTERY_OPTIMIZATIONS_ACTION, spec.action)
        assertEquals("必须是这个包，不是别的包", "package:com.echo.contact", spec.dataUri)
    }
}
