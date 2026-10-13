package com.echo.contact.audio

import android.content.Context
import android.content.Intent
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.os.Bundle

/**
 * 安卓**系统自带**的语音识别（`android.speech.SpeechRecognizer`）——就是输入法那个麦克风背后的东西。
 *
 * 与 ECHO 的 `POST /api/assistant/voice-command`（自己录 wav 上传）的区别：
 *  - 这一条**只给你文本**，音频在系统/厂商引擎里，你拿不到，也送不给 ECHO；
 *  - 好处：短句指令很快，且**可能完全离线**；
 *  - **但"离线"是保证不了的**：`EXTRA_PREFER_OFFLINE` 只是**建议**，厂商引擎可以无视；
 *    而且识别这边没有 TTS 那种 `Voice.isNetworkConnectionRequired` 判据可以查。
 *    所以"手机识别 = 不出机"**不成立**，音频可能被送到厂商云上。
 *
 * 两个必须处理的坑（不处理就会误报"这台设备没有识别服务"）：
 *  1. Android 11+ 的**包可见性**：清单里必须声明
 *     `<queries><intent><action android:name="android.speech.RecognitionService"/></intent></queries>`；
 *  2. `RECORD_AUDIO` 是**运行时权限**，必须先要到。
 */
class SpeechInput(private val context: Context) {

    var onPartial: ((String) -> Unit)? = null
    var onFinal: ((String) -> Unit)? = null
    var onError: ((String) -> Unit)? = null
    var onState: ((String) -> Unit)? = null

    private var recognizer: SpeechRecognizer? = null

    /** 是否有一个还没收尾的会话（没收干净就会让下一轮报 ERROR_RECOGNIZER_BUSY）。 */
    private var sessionOpen = false

    fun available(): Boolean = runCatching {
        SpeechRecognizer.isRecognitionAvailable(context)
    }.getOrDefault(false)

    /** 这台设备上装了哪些识别**服务**（用来判断"到底是谁在识别"）。 */
    fun services(): List<String> {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
        return runCatching {
            context.packageManager.queryIntentServices(intent, 0).mapNotNull { info ->
                val pkg = info.serviceInfo?.packageName.orEmpty()
                if (pkg.isBlank()) null else pkg
            }.distinct()
        }.getOrDefault(emptyList())
    }

    /**
     * 有几个**界面**能响应"识别"这个 Intent（包名）。
     *
     * 为什么单独查它：**服务**（`android.speech.RecognitionService`）与**界面**
     * （`android.speech.action.RECOGNIZE_SPEECH`）是两个不同的 action，
     * 包可见性要分别声明。只声明了服务时，`isRecognitionAvailable()` 照常为 true，
     * 但拉起系统识别界面会抛 `ActivityNotFoundException`（2026-10-09 真机踩到）。
     * 返回空表就是"没有可打开的识别界面"。
     */
    fun uiHandlers(): List<String> {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
        return runCatching {
            context.packageManager.queryIntentActivities(intent, 0).mapNotNull { info ->
                info.activityInfo?.packageName
            }.distinct()
        }.getOrDefault(emptyList())
    }

    /** 必须在**主线程**调用。返回 false 表示没起来（原因已经通过 onError 给出）。 */
    fun start(): Boolean {
        if (!available()) {
            onError?.invoke("这台设备没有可用的系统识别服务")
            return false
        }
        // **每轮都用全新的 recognizer**：上一轮万一没收干净，它会一直占着会话，
        // 下一轮就报 ERROR_RECOGNIZER_BUSY（2026-10-09 用户真机上就是这个现象）。
        release()
        return try {
            recognizer = SpeechRecognizer.createSpeechRecognizer(context).apply {
                setRecognitionListener(listener)
            }
            val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                putExtra(
                    RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
                )
                putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
                putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
                putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
            }
            recognizer?.startListening(intent)
            sessionOpen = true
            true
        } catch (e: Exception) {
            onError?.invoke("启动识别失败：${e.message}")
            false
        }
    }

    /**
     * 「说完了」：停止收音，**让引擎把已经收到的那段拿去识别**。
     *
     * 注意 `stopListening()` **不等于**"一定会回调结果"：
     * 引擎已经自己结束过会话（静音判定）时，这一次调用可能什么都不做；
     * 而它若抛异常，**绝不能吞掉** —— 吞掉的表现正是"点了没反应"
     * （2026-10-09 用户真机上就是这个症状）。
     */
    fun stop(): Boolean = try {
        val r = recognizer
        if (r == null) {
            onError?.invoke("识别还没开始")
            false
        } else {
            r.stopListening()
            true
        }
    } catch (e: Exception) {
        onError?.invoke("停止识别失败：${e.message}")
        false
    }

    /** 取消当前会话（用户点了"取消"，或我们决定放弃等结果）。 */
    fun cancel() {
        try {
            recognizer?.cancel()
        } catch (e: Exception) {
            onError?.invoke("取消识别失败：${e.message}")
        }
        sessionOpen = false
    }

    /**
     * **彻底放掉**当前 recognizer（cancel + destroy + 置空）。
     *
     * 什么时候必须调：放弃等结果之后。只把界面状态清掉、不释放会话，
     * 下一轮就会被引擎判成"忙"。
     */
    fun release() {
        if (recognizer != null) {
            runCatching { recognizer?.cancel() }
            runCatching { recognizer?.destroy() }
        }
        recognizer = null
        sessionOpen = false
    }

    fun destroy() = release()

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) {
            sessionOpen = true
            onState?.invoke("在听…")
        }

        override fun onBeginningOfSpeech() = Unit

        override fun onRmsChanged(rmsdB: Float) = Unit

        override fun onBufferReceived(buffer: ByteArray?) = Unit

        override fun onEndOfSpeech() {
            onState?.invoke("识别中…")
        }

        override fun onError(error: Int) {
            sessionOpen = false
            // "忙"意味着上一轮没收干净：**立刻彻底放掉**，下一轮才是全新的
            if (error == SpeechRecognizer.ERROR_RECOGNIZER_BUSY) release()
            onError?.invoke(describe(error))
        }

        override fun onResults(results: Bundle?) {
            sessionOpen = false
            val list = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION).orEmpty()
            onFinal?.invoke(list.firstOrNull().orEmpty())
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val list = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION).orEmpty()
            list.firstOrNull()?.takeIf { it.isNotBlank() }?.let { onPartial?.invoke(it) }
        }

        override fun onEvent(eventType: Int, params: Bundle?) = Unit
    }

    /** 错误码 → 人话。**要能分清"没听清"和"坏了"**，不然用户会去查错方向。 */
    private fun describe(error: Int): String = when (error) {
        SpeechRecognizer.ERROR_NO_MATCH -> "没听清（没匹配到词），再说一遍试试"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "没听到说话（超时）"
        SpeechRecognizer.ERROR_AUDIO -> "录音出错（麦克风被占用或权限不足）"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "缺少录音权限"
        SpeechRecognizer.ERROR_NETWORK -> "识别服务的网络错误（这个引擎需要联网）"
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "识别服务网络超时"
        SpeechRecognizer.ERROR_SERVER -> "识别服务端出错"
        SpeechRecognizer.ERROR_CLIENT -> "客户端错误（识别被中断）"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "识别服务忙，稍后再试"
        SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED -> "这个引擎不支持中文"
        SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> "这个引擎的中文包没装"
        else -> "识别失败（错误码 $error）"
    }
}
