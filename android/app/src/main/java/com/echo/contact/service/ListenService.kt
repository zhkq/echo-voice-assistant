package com.echo.contact.service

import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * 语音录制前台服务——**占位实现**。
 *
 * 阶段3 只做「文字指令 + 历史」，尚未实现录音链路（阶段4）。
 * 这里保留 AndroidManifest 中已声明的组件，避免 `android:name` 指向不存在的类；
 * 真正开始录音前必须补上：麦克风权限申请、前台通知、音频流与上传。
 */
class ListenService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 阶段4 之前不接受常驻运行：立刻停掉自己，不做任何录音。
        stopSelf(startId)
        return START_NOT_STICKY
    }
}
