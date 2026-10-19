package com.echo.contact.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.echo.contact.MainActivity
import com.echo.contact.R
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * [SyncForegroundHost] 的**实现**（Android 那一层；判据全在纯逻辑那一侧）。
 *
 * ## 为什么补传要 `dataSync` 这个前台类型
 *
 * Android 14 起，前台服务的**类型必须声明，而且要与它真正在干的事对得上**。
 * 补传是"把手机里的音频往服务端搬" = **数据同步**，不是录音。
 * 而 `MeetingMicService` 同时还有录音那条路（`microphone`）—— 于是清单里
 * `foregroundServiceType="microphone|dataSync"`，**同一个服务按路径切类型**：
 *  * 录音那条：`MICROPHONE`（老行为，一个字都没改）；
 *  * 补传那条：`DATA_SYNC`；
 *  * 录音中顺手补传：`MICROPHONE|DATA_SYNC`（进程这时本来就是前台，别把它降级）。
 *
 * 为什么不做成"两个服务"：这一场录音的全部现场（`session` / `captureTail` / `sealQueue`）
 * 都是 `MeetingMicService` 的实例字段，拆成两个服务就得把这些状态搬过进程内的另一处，
 * 而"手机麦"这条路最怕的正是**两套账**（用户 2026-10-17 合并过一次）。所以一个服务、
 * 两条前台路径，类型按需给。
 *
 * ## 锁：两个都必须有超时
 *
 *  * `PARTIAL_WAKE_LOCK`：`acquire(timeoutMs)` —— 有超时（上限见 [MAX_LOCK_MS]）；
 *  * `WifiLock`：**系统没给超时参数**，所以这里自己加一个看门狗（[lockWatchdog]，
 *    `delay(timeoutMs)` 之后无条件释放）。这是"所有锁都必须有超时"的落点 ——
 *    没有它，一次卡死的上传就能让 WiFi 一直高功耗模式挂着。
 *
 * ## 通知：一条常驻（不可滑）、一条收尾（可滑）
 *
 * 常驻那条是**前台服务的外在形式**：`setOngoing(true)` + 前台服务本身 → 滑不掉；
 * 传完 `stopForeground(STOP_FOREGROUND_REMOVE)` 立刻消失（**不许因此变成常驻**）。
 * 收尾那条是普通通知（`autoCancel`），点了就进 App；成功/失败文案在
 * [syncFinishText] 里（纯函数，用例钉住）。
 */
internal class SyncForegroundAndroid(
    private val service: Service,
    private val scope: CoroutineScope,
    /** 录音那条前台是不是已挂着（是 → 挂它那条通知、结束时不撤它）。 */
    private val recording: () -> Boolean,
    /** 录音那条通知本体（由 `MeetingMicService` 建：内容口径只有那一处）。 */
    private val recordingNotification: () -> Notification,
    /** 录音那条通知的 id（同一个 id 才能"更新"，换 id 会留下两条通知）。 */
    private val recordingNotifId: Int,
) : SyncForegroundHost {

    companion object {
        /** 补传常驻通知的 id（与录音那条 4211 分开：两条通知是两件事）。 */
        internal const val SYNC_NOTIF_ID = 0xEC50

        /**
         * 收尾通知的 id（与常驻那条**分开**）。
         *
         * 为什么不用同一个 id：`stopForeground(REMOVE)` 会把前台那条连同 id 一起撤掉，
         * 之后再 `notify(同一个 id)` 虽然也能出现，但"撤掉的是哪一条"就说不清了；
         * 两个 id 让"常驻的归常驻、收尾的归收尾"一眼可见。
         */
        internal const val SYNC_FINISH_NOTIF_ID = 0xEC51

        /** 通知渠道的重要性：**低**（不给声音/横幅，只是"它还在传"）。 */
        private const val CHANNEL_IMPORTANCE = NotificationManager.IMPORTANCE_LOW
    }

    private val power: PowerManager? by lazy {
        service.getSystemService(Context.POWER_SERVICE) as? PowerManager
    }
    private val wifi: WifiManager? by lazy {
        service.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
    }

    @Volatile private var wakeLock: PowerManager.WakeLock? = null
    @Volatile private var wifiLock: WifiManager.WifiLock? = null

    /** WiFi 锁的看门狗（系统没给 WifiLock 超时，只能自己收）。 */
    @Volatile private var lockWatchdog: Job? = null

    override fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = notificationManager() ?: return
        if (nm.getNotificationChannel(SYNC_CHANNEL_ID) != null) return
        val ch = NotificationChannel(SYNC_CHANNEL_ID, SYNC_CHANNEL_NAME, CHANNEL_IMPORTANCE).apply {
            description = "补传会议录音时的那条常驻通知（传完就消失）"
            setShowBadge(false)
            enableVibration(false)
            setSound(null, null)
        }
        nm.createNotificationChannel(ch)
    }

    override fun recordingForeground(): Boolean = runCatching { recording() }.getOrDefault(false)

    override fun startForeground(text: String) {
        ensureChannel()
        val inRecording = recordingForeground()
        // 录音中挂**录音那条**通知：换成"正在上传会议录音"会让用户以为录音停了
        // （那一刻最要紧的事实是"还在录"，上传进度在那条通知的文案里也有）。
        val n = if (inRecording) runCatching { recordingNotification() }.getOrNull()
            ?: buildSyncNotification(text) else buildSyncNotification(text)
        val id = if (inRecording) recordingNotifId else SYNC_NOTIF_ID
        val type = if (inRecording) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        } else {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        }
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                service.startForeground(id, n, type)
            } else {
                service.startForeground(id, n)
            }
        }.onFailure {
            // 起不来（例如 Android 12+ 从后台起前台服务被拒）**不许崩**：
            // 业务那一轮还会照跑（锁已经在拿），最坏是退回"可能被系统掐掉"的老行为。
            android.util.Log.w(SYNC_FG_TAG, "进前台失败（${it.javaClass.simpleName}）：${it.message}")
        }
    }

    override fun updateForeground(text: String) {
        if (recordingForeground()) {
            // 录音中那条通知的内容口径在 `MeetingMicService.notifyState`（按 MicState 拼），
            // 这里再 notify 一次就是两处抢同一行字 —— 让它刷即可。
            return
        }
        val nm = notificationManager() ?: return
        runCatching { nm.notify(SYNC_NOTIF_ID, buildSyncNotification(text)) }
    }

    override fun stopForegroundRemove() {
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                service.stopForeground(Service.STOP_FOREGROUND_REMOVE)
            } else {
                @Suppress("DEPRECATION") service.stopForeground(true)
            }
        }
    }

    override fun postFinish(text: String, failed: Boolean) {
        val nm = notificationManager() ?: return
        val b = NotificationCompat.Builder(service, SYNC_CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(if (failed) "补传会议录音没成" else "会议录音传完了")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            // 可滑掉、点了就进 App：收尾通知是"这件事结束了"，不是常驻状态
            .setAutoCancel(true)
            .setOngoing(false)
            .setShowWhen(false)
            .setContentIntent(openApp())
        runCatching { nm.notify(SYNC_FINISH_NOTIF_ID, b.build()) }
    }

    override fun acquireLocks(timeoutMs: Long) {
        // 先放干净（幂等）：同一实例上连着两轮补传时，旧锁的超时不是新的那个。
        releaseLocks()
        val timeout = timeoutMs.coerceIn(MIN_LOCK_MS, MAX_LOCK_MS)
        // ① 唤醒锁：**一定带超时** —— 忘了放 = 用户手机一整夜不睡。
        runCatching {
            val pm = power ?: return@runCatching
            val wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, SYNC_WAKE_LOCK_TAG)
            wl.setReferenceCounted(false)
            wl.acquire(timeout)
            wakeLock = wl
        }.onFailure {
            android.util.Log.w(SYNC_FG_TAG, "唤醒锁拿不到（${it.javaClass.simpleName}）：锁屏时可能传不完")
        }
        // ② WiFi 锁：弱网/锁屏下 WiFi 会被省电降级（`WIFI_MODE_FULL_HIGH_PERF` 关掉那个）。
        runCatching {
            val wm = wifi ?: return@runCatching
            @Suppress("DEPRECATION")
            val lock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, SYNC_WIFI_LOCK_TAG)
            lock.setReferenceCounted(false)
            lock.acquire()
            wifiLock = lock
        }.onFailure {
            android.util.Log.w(SYNC_FG_TAG, "WiFi 锁拿不到（${it.javaClass.simpleName}）：锁屏时可能降速")
        }
        // ③ 看门狗：WifiLock 没有超时参数，这里用同一个上限自己收（"所有锁都必须有超时"）
        lockWatchdog = scope.launch {
            delay(timeout)
            releaseLocks()
            android.util.Log.w(SYNC_FG_TAG, "锁到上限（${timeout / 60_000} 分钟）自动释放：另有下一轮会重新拿")
        }
    }

    override fun extendLocks(timeoutMs: Long) {
        // 非引用计数的锁，`acquire()` 不会延长已有超时 —— 只能"放掉再拿"。
        acquireLocks(timeoutMs)
    }

    override fun releaseLocks() {
        val dog = lockWatchdog
        lockWatchdog = null
        runCatching { dog?.cancel() }
        val wl = wakeLock
        wakeLock = null
        runCatching { if (wl?.isHeld == true) wl.release() }
        val wifiLockHeld = wifiLock
        wifiLock = null
        runCatching { if (wifiLockHeld?.isHeld == true) wifiLockHeld.release() }
    }

    private fun notificationManager(): NotificationManager? =
        service.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager

    private fun buildSyncNotification(text: String): Notification {
        ensureChannel()
        val b = NotificationCompat.Builder(service, SYNC_CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(SYNC_NOTIFY_TITLE)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            // 不可滑掉：它是前台服务的外在形式（传完 stopForeground(REMOVE) 自己消失）
            .setOngoing(true)
            // 每段只更新数字：别每段都响一下（那是骚扰，不是进度）
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(openApp())
        return b.build()
    }

    /** 点通知 → 打开 App（这里不通向任何具体页面：补传不是"一场会"，落地页由外壳自己决定）。 */
    private fun openApp(): PendingIntent? {
        val intent = Intent(service, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            data = Uri.parse("echo://sync")
        }
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0)
        return runCatching { PendingIntent.getActivity(service, 0, intent, flags) }.getOrNull()
    }
}

/** 日志标签（真机排障只能靠它：`adb logcat -s SyncFg`）。 */
internal const val SYNC_FG_TAG = "SyncFg"
