package com.echo.contact.service

import java.util.Locale

/**
 * 补传的**前台服务 + 锁**（用户 2026-10-19 深夜：
 * *"锁屏后，再解锁就要按一下重试，echo 需要后台可运行"*）。
 *
 * ## 为什么会这样（现场，判据都在代码里）
 *
 * 补传是 `MeetingMicService` 的 `ACTION_SYNC` → `runOfflineSync` 跑的，而那条路
 * **从来没有 `startForeground`**：服务只是被 `startService` 顺手拉起来的一个**后台**服务
 * （`syncNow` 旧实现），进程一进后台，屏幕一关就被冻结/掐掉；而一段 10 分钟的 WAV
 * ≈18 MB、还要过中继，要跑几分钟 —— 锁屏期间几乎**必然**被掐。
 * 更糟的是自动那条路（`tickFromForeground`）连服务都没有，它是 `HomeScreen` 里一个
 * 裸 `CoroutineScope(...).launch`：进程被冻结时它连"我被打断了"都不知道。
 * 于是解锁之后只能手点「重试」——这就是用户那句话的全部内容。
 *
 * ## 为什么这一份是**纯逻辑**（一个 Android 类都不 import）
 *
 * "有没有进前台 / 锁有没有释放"最容易的错法是**漏一条路径**（异常、取消、跳过某个 return），
 * 而那种错在真机上只表现为"偶尔传不完"，看不出来。所以把**顺序与释放**收在这一层，
 * 用一个可注入的 [SyncForegroundHost] 在 JVM 里逐条钉住（`SyncForegroundTest`）：
 *
 *  * 开始 → 建渠道（`echo_sync`）+ **先** `startForeground` + 拿锁；
 *  * 每段 → **同一条**通知更新内容（不是只更一次）+ 续锁；
 *  * 结束（完成 / 失败 / 抛异常 / 取消）→ **一律**释放全部锁；只有"前台是我们自己挂的"
 *    才撤掉它（录音时那条前台是录音的，撤了用户会以为录音停了）；
 *  * 没有要传的东西 → **一条通知都不发**（[shouldEnterSyncForeground]）——绝不变成常驻。
 *
 * ## 为什么锁**必须带超时、必须在 finally 里释放**
 *
 * `PARTIAL_WAKE_LOCK` 是"不许息屏"的许可：忘了放 = 用户手机一整夜不睡 = 第二天没电
 * （那比"这一段没传完"糟得多）。所以：
 *  * 每一个锁都有**超时**（[uploadLockTimeoutMs]，上限 [MAX_LOCK_MS] = 30 分钟）；
 *  * [guard] 的 `finally` 里**无条件**再释放一次（[releaseAll] 幂等）。
 *
 * 超时是"按每段字节数估算 + 兜底"算的：按**下限吞吐** [LOCK_FLOOR_BYTES_PER_SEC]
 * （32 KB/s ≈ 256 kbps，弱网/中继下的保守值）估传输时间，再加 [RESERVE_LOCK_MS]
 * 给建会/对账/收尾那些小请求；夹在 [MIN_LOCK_MS] 与 [MAX_LOCK_MS] 之间。
 */

// --------------------------------------------------------------------------- 常量

/**
 * 补传那条通知的**渠道 id**（`echo_sync`）。
 *
 * 为什么单独一个渠道（不复用录音的 `echo_meeting_mic`）：两者是**两件事**——
 * 录音是"我正在录"（用户不能滑掉），补传是"我在往外发"（传完就该消失）。
 * 分成两个渠道，用户才能在系统设置里只关掉补传的提示而保留录音的。
 */
internal const val SYNC_CHANNEL_ID = "echo_sync"

/** 渠道名（系统设置里给用户看的那一行）。 */
internal const val SYNC_CHANNEL_NAME = "补传会议录音"

/** 常驻通知的标题（用户点名的那句："正在上传会议录音"）。 */
internal const val SYNC_NOTIFY_TITLE = "正在上传会议录音"

/** 唤醒锁的名字：真机排障时 `dumpsys power | findstr echo` 能一眼看出是谁持着。 */
internal const val SYNC_WAKE_LOCK_TAG = "echo:sync-upload"

/** WiFi 锁的名字（同上）。 */
internal const val SYNC_WIFI_LOCK_TAG = "echo:sync-wifi"

/**
 * 估锁超时时用的**下限吞吐**：32 KB/s（≈256 kbps）。
 *
 * 为什么取这么低：这一段可能是 18 MB 的 WAV、还要过中继；真机上 4G 弱网 + 中继
 * 实测就在这个量级。**估小一点**只会让锁的窗口短一点（下一段会续），
 * 估大了则是"锁着不放"——两者里前者安全。
 */
internal const val LOCK_FLOOR_BYTES_PER_SEC = 32L * 1024L

/** 锁超时的下限（短于它就没意义：建会 + 一段小音频都不止）。 */
internal const val MIN_LOCK_MS = 5L * 60_000L

/**
 * 锁超时的**硬上限**：30 分钟。
 *
 * 这条不是"再传久一点就多给点"——它是**忘记释放时的保险丝**。一场会最多 5 段、
 * 每段几分钟，30 分钟够一轮；真不够的话下一轮会重新拿（不是把上限抬高）。
 */
internal const val MAX_LOCK_MS = 30L * 60_000L

/** 给建会 / 对账 / `stop` 那些"必须快"的请求留的余量（它们不该吃掉传输的窗口）。 */
internal const val RESERVE_LOCK_MS = 5L * 60_000L

// --------------------------------------------------------------------------- 纯函数

/**
 * 一次补传该把锁持多久（毫秒）——**按这次要传的字节数估 + 兜底**（纯函数）。
 *
 * [bytes] <= 0（还没算出来 / 只有收尾要做）→ [MIN_LOCK_MS]（别给 0：
 * 0 就等于"没锁"，那正是要修的毛病）。
 */
internal fun uploadLockTimeoutMs(bytes: Long): Long {
    if (bytes <= 0L) return MIN_LOCK_MS
    val transferMs = bytes / LOCK_FLOOR_BYTES_PER_SEC * 1000L
    return (transferMs + RESERVE_LOCK_MS).coerceIn(MIN_LOCK_MS, MAX_LOCK_MS)
}

/** 字节数 → 通知里那个"12.4 MB"（0 / 负数 → 空串：**没有数就不编一个数出来**）。 */
internal fun humanMb(bytes: Long): String =
    if (bytes <= 0L) "" else String.format(Locale.US, "%.1f MB", bytes / 1048576.0)

/**
 * 补传常驻通知的**内容行**（纯函数；用户点名的形状："已传 2/5 段（第 3 段 12.4 MB）"）。
 *
 * 三档都对得上真实进度：
 *  * 有当前段、也知道它多大 → "已传 2/5 段（第 3 段 12.4 MB）"；
 *  * 知道是哪一段、但不知道大小（老清单 / 读不到文件）→ 只报段号，**不编大小**；
 *  * 什么都不知道（刚开始）→ 只报"已传 M/N 段"。
 */
internal fun syncNotifyText(p: SyncProgress): String {
    val total = p.totalSegments
    if (total <= 0) return "正在把手机里的录音传给 ECHO…"
    val head = "已传 ${p.confirmedSegments}/$total 段"
    if (p.currentSeq <= 0) return head
    val size = humanMb(p.currentBytes)
    val where = if (size.isBlank()) "第 ${p.currentSeq} 段" else "第 ${p.currentSeq} 段 $size"
    return "$head（$where）"
}

/**
 * 刚开始同步时那行字（纯函数）。
 *
 * 为什么它必须带 "0/N"：用户解锁时看到的第一眼就是这条通知，
 * "正在上传"四个字不说明"还有多少"；`0/5` 才是他能对上账的那个数。
 */
internal fun syncBeginText(totalSegments: Int): String =
    if (totalSegments <= 0) "正在把手机里的录音传给 ECHO…" else "已传 0/$totalSegments 段（准备中）"

/**
 * 「这一次要不要因为补传而进前台」的**唯一判据**（纯函数）。
 *
 * 判据是"**磁盘上还有没有待传的场**"（[MeetingDraftStore.pending]），不是"这一轮传几段"：
 *  * 还有场欠着 → 要进（哪怕这一轮只做收尾，那也是一串网络请求，一样会被锁屏掐掉）；
 *  * 一场都不欠 → **不进**（一次都不进）——这就是"不许因此变成常驻"。
 *
 * ⚠️ 这条判据必须在**起服务 / 拿锁之前**问（见 `MeetingMicService.onStartCommand` 的
 * `ACTION_SYNC` 与 `tickFromForeground`）：先起了服务再发现没事可干，
 * 就至少留下一条通知（用户没在传的时候**零通知**是硬要求）。
 *
 * [paired] = 这台手机已经与 ECHO 配对。没配对时**根本发不出去**（一条请求都不会发），
 * 进前台拿锁只是白白占着一条通知与一块电池 —— 所以这一档也不进
 * （那一轮照样会跑，只是它只会在状态里如实写"还没配对"，见 `runOfflineSync` 的开头）。
 */
internal fun shouldEnterSyncForeground(pendingDrafts: Int, paired: Boolean = true): Boolean =
    pendingDrafts > 0 && paired

/** 一轮补传的结局（决定收尾通知那一句；[Nothing] / [Quiet] 一条通知都不发）。 */
internal sealed interface SyncEnd {
    /**
     * 传完了。
     *
     * [transcribing] 是 `OfflineSyncResult.stopped`：**只有 `stop` 成功才敢说"开始转写"**
     * —— 没有收尾就没有转写，说错这句等于把老根因又藏回去。
     */
    data class Done(val segments: Int, val drafts: Int, val transcribing: Boolean) : SyncEnd

    /** 没成（**必须带真实原因**：连不上 / 413 / 服务端拒收 / 对账不符…）。 */
    data class Failed(val reason: String) : SyncEnd

    /** 被打断（服务被销毁 / 进程被回收 / 注入的异常）。 */
    data class Cancelled(val reason: String) : SyncEnd

    /** 本来就没东西要传（不该起前台；万一起了也**不发**收尾通知）。 */
    data class Nothing(val reason: String) : SyncEnd

    /**
     * 跑了，但**不发通知**（边录边传那种机会性上传：每封一段都弹一条"补传完成"是骚扰）。
     * 锁仍然照常释放。
     */
    data class Quiet(val reason: String) : SyncEnd
}

/**
 * 收尾通知那一句（纯函数）。
 *
 * "完成"与"失败"必须长得不一样，而且**失败要带原因**（用户 2026-10-19 的原话是
 * "就要按一下重试"——他需要知道"为什么"才敢再按）。
 */
internal fun syncFinishText(end: SyncEnd): String = when (end) {
    is SyncEnd.Done ->
        if (end.transcribing) {
            "已传完 ${end.segments} 段（${end.drafts} 场）：ECHO 那边已经开始转写"
        } else {
            // 没有 stop 就没有转写 —— 这一档**不许**说"开始转写"
            "已传完 ${end.segments} 段（${end.drafts} 场），但还没收到收尾确认（没有收尾就不会出文字）"
        }
    is SyncEnd.Failed -> "补传没成：${end.reason.ifBlank { "原因不明" }}"
    is SyncEnd.Cancelled -> end.reason.ifBlank { "补传被打断了，剩下的段下次接着传" }
    is SyncEnd.Nothing -> end.reason
    is SyncEnd.Quiet -> end.reason
}

/** 抛出来那个异常 → 一句如实的话（纯函数；用例把 `IOException("boom")` 喂进来钉住）。 */
internal fun syncInterruptNote(t: Throwable): String {
    val msg = t.message?.takeIf { it.isNotBlank() }?.let { "：$it" } ?: ""
    return "补传被打断（${t.javaClass.simpleName}$msg），剩下的段下次接着传"
}

// --------------------------------------------------------------------------- 端口（给测试注入）

/**
 * 前台与锁的**唯一**出口（实现见 `SyncForegroundAndroid`，用例里注入一个 fake）。
 *
 * 为什么要有这一层：`startForeground` / `WakeLock` 都是 Android 类，JVM 用例碰不得
 * （stub `android.jar` 一调就抛）；而"哪一步该发生、发生在什么时候"恰恰是这一轮要钉住的东西。
 */
internal interface SyncForegroundHost {
    /** 建 `echo_sync` 渠道（幂等：系统那边重复建只是覆盖，不会多出第二条）。 */
    fun ensureChannel()

    /**
     * 录音那条前台是不是已经挂着。
     *
     * 是的话：`startForeground` 要挂**录音那条**通知（换掉它用户会以为录音停了），
     * 而且结束时**不许**撤（撤了录音就没前台了，Android 14 直接抛）。
     */
    fun recordingForeground(): Boolean

    /** 进前台（`dataSync` 类型；录音中则补上 `microphone|dataSync`）。 */
    fun startForeground(text: String)

    /** 只更新**同一条**通知（不重新 `startForeground`）。 */
    fun updateForeground(text: String)

    /** `stopForeground(STOP_FOREGROUND_REMOVE)`（把常驻通知一起撤掉）。 */
    fun stopForegroundRemove()

    /** 收尾通知（普通通知：可滑掉、`autoCancel`；[failed] 只影响文案图标）。 */
    fun postFinish(text: String, failed: Boolean)

    /** 拿锁（`PARTIAL_WAKE_LOCK` 带超时 + `WifiLock`；实现里**两个都**带超时）。 */
    fun acquireLocks(timeoutMs: Long)

    /** 续锁（释放再拿一次；`acquire` 不会延长非引用计数锁的超时）。 */
    fun extendLocks(timeoutMs: Long)

    /** 释放全部锁（幂等）。 */
    fun releaseLocks()
}

// --------------------------------------------------------------------------- 编排

/**
 * 一次补传的"前台 + 锁"生命周期（**唯一的实现**）。
 *
 * 用法（`MeetingMicService`）：
 * ```
 * syncForeground.guard(total, bytes, block = { runOfflineSync(...) }, endOf = { syncEndOf(it) })
 * ```
 * 这个函数本身不吞异常：它只负责"**无论怎么结束**都把锁放掉、把前台的状态收干净"，
 * 然后把异常照原样抛回去（调用方那一层决定要不要吞）。
 */
internal class SyncForeground(private val host: SyncForegroundHost) {

    /** 现在是不是"一次补传正在进行"（[progress] / [finish] 只在这个窗口里做事）。 */
    var active: Boolean = false
        private set

    /** 前台是**我们**挂的那条（录音中不撤的那条不算）。 */
    var foregroundOwned: Boolean = false
        private set

    /** 现在手上是不是拿着锁（用例断言"全释放"就是读它）。 */
    var locksHeld: Boolean = false
        private set

    /** 这一次用的是多大的锁超时（用例断言"有超时、而且不超过上限"）。 */
    var lockTimeoutMs: Long = MIN_LOCK_MS
        private set

    /**
     * 进前台 + 拿锁。**只有真有待传时才该调**（见 [shouldEnterSyncForeground]）。
     *
     * 顺序是刻意的：**先** `startForeground`（`startForegroundService` 给的窗口只有几秒，
     * 先拿锁再挂前台的话，一个卡住的 `acquire` 就能把窗口用完 → ANR），然后拿锁。
     */
    fun begin(totalSegments: Int, totalBytes: Long): Boolean {
        if (active) return false
        active = true
        lockTimeoutMs = uploadLockTimeoutMs(totalBytes)
        host.ensureChannel()
        // 录音中也要调：`startForegroundService` 的 5 秒义务与"服务是不是已经在前台"
        // 是两件事，调一次最安全；实现那边会挂**录音那条**通知（不换 notification id）。
        host.startForeground(syncBeginText(totalSegments))
        foregroundOwned = !host.recordingForeground()
        host.acquireLocks(lockTimeoutMs)
        locksHeld = true
        return true
    }

    /**
     * 每段一次：更新那条通知（用户要的"每段都动一下"）+ 续锁。
     *
     * ⚠️ 不在补传中时**什么都不做** —— 否则边录边传那条机会性路径会凭空多出一条通知
     * （用户没在补传的时候，通知栏必须是干净的）。
     */
    fun progress(p: SyncProgress) {
        if (!active) return
        lockTimeoutMs = maxOf(lockTimeoutMs, uploadLockTimeoutMs(p.currentBytes))
        host.updateForeground(syncNotifyText(p))
        host.extendLocks(lockTimeoutMs)
    }

    /**
     * 结束：**无论哪一档**都先撤前台/放锁，再发收尾通知。
     *
     * 顺序不能反：`stopForeground(REMOVE)` 会把前台那条通知一起删掉 ——
     * 先发收尾通知再 stop，那条通知会被顺手删掉（用户根本看不到"传完了"）。
     */
    fun finish(end: SyncEnd) {
        if (!active) {
            // 没进过前台（例如被系统拒了）：**不发任何通知**，只把可能的残留锁放掉
            releaseAll()
            return
        }
        active = false
        if (foregroundOwned) {
            host.stopForegroundRemove()
            foregroundOwned = false
        }
        releaseAll()
        if (end !is SyncEnd.Nothing && end !is SyncEnd.Quiet) {
            host.postFinish(syncFinishText(end), failed = end !is SyncEnd.Done)
        }
    }

    /** 放掉全部锁（幂等；`finally` 里那一道双保险就是调它）。 */
    fun releaseAll() {
        if (!locksHeld) return
        locksHeld = false
        host.releaseLocks()
    }
}

/**
 * 把一整轮补传包在"前台 + 锁"里（`try/finally` 的**唯一**写法）。
 *
 * 判据的形状（`SyncForegroundTest` 逐条钉住）：
 * ```
 * begin(...)                 ← 进前台 + 拿锁（有超时）
 * try   { block() }          ← 真正的补传（可能几分钟）
 * catch { finish(Cancelled) } ← 抛了也要放锁，而且要把原因说出来
 * finally { releaseAll() }   ← 双保险：finish 里已经放过一次
 * ```
 *
 * [endOf] 把业务结果翻成"收尾通知那一句"（完成 / 失败带原因 / 静默）。
 */
internal suspend fun <T> SyncForeground.guard(
    totalSegments: Int,
    totalBytes: Long,
    block: suspend () -> T,
    endOf: (T) -> SyncEnd,
): T {
    begin(totalSegments, totalBytes)
    try {
        val value = block()
        finish(endOf(value))
        return value
    } catch (t: Throwable) {
        finish(SyncEnd.Cancelled(syncInterruptNote(t)))
        throw t
    } finally {
        releaseAll()
    }
}
