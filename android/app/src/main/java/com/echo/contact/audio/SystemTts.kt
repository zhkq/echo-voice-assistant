package com.echo.contact.audio

import android.content.Context
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import java.util.Locale

/**
 * 安卓**系统自带**的朗读（`android.speech.tts.TextToSpeech`）。
 *
 * 与 ECHO 的 `POST /api/tts` 是两回事：
 *  - 系统 TTS：在**手机本地**合成，引擎是厂商预装的（华为/小米/讯飞/Google…），
 *    可能**完全离线**，没有网络往返；但音色与电脑上听到的不一样；
 *  - ECHO TTS：在主机合成好再把音频字节发下来，音色与电脑一致，但必须有网。
 *
 * 所以这里把"用的是哪个引擎、哪个音色、**要不要联网**"都暴露出来 ——
 * 是否需要联网是判断"能不能当降级档"的关键判据，不能靠猜。
 */
class SystemTts(context: Context) {

    enum class Status { INIT, READY, UNAVAILABLE, ERROR }

    var status: Status = Status.INIT
        private set

    /** 引擎展示名（如"讯飞语音引擎"/"Google 语音服务"）。 */
    var engineLabel: String = ""
        private set

    /** 引擎包名（判据用，比展示名可靠）。 */
    var enginePackage: String = ""
        private set

    /** 当前音色名。 */
    var voiceName: String = ""
        private set

    /** 当前音色**是否需要联网**。 */
    var networkRequired: Boolean = false
        private set

    /** 不可用时的人话原因（缺中文数据 / 不支持中文 / 引擎初始化失败）。 */
    var problem: String = ""
        private set

    /** 状态变化时通知 UI 刷新。 */
    var onChanged: (() -> Unit)? = null

    private var tts: TextToSpeech? = null
    private val appContext = context.applicationContext

    fun init() {
        if (tts != null) return
        tts = TextToSpeech(appContext) { st ->
            val engine = tts
            if (engine == null) return@TextToSpeech
            if (st != TextToSpeech.SUCCESS) {
                status = Status.ERROR
                problem = "系统朗读引擎初始化失败（这台设备可能没装 TTS 引擎）"
                onChanged?.invoke()
                return@TextToSpeech
            }

            val lang = engine.setLanguage(Locale.SIMPLIFIED_CHINESE)
            problem = when (lang) {
                TextToSpeech.LANG_MISSING_DATA -> "系统引擎缺中文语音数据（去系统设置里下载）"
                TextToSpeech.LANG_NOT_SUPPORTED -> "系统引擎不支持中文"
                else -> ""
            }

            // 找出"这台设备默认用的是哪个引擎"。
            // 注意：TextToSpeech 没有公开"我是哪个引擎"的方法（getDefaultEngine() 不存在），
            // 真正的判据是系统安全设置 tts_default_synth；EngineInfo 的 priority 也不是公开字段，
            // 所以**认不出就如实说认不出**，不猜一个排第一的糊上去。
            val infos = runCatching { engine.engines }.getOrNull().orEmpty()
            val defaultPkg = runCatching {
                Settings.Secure.getString(appContext.contentResolver, "tts_default_synth")
            }.getOrNull().orEmpty()
            val picked = infos.firstOrNull { it.name == defaultPkg }
            enginePackage = (picked?.name ?: defaultPkg).orEmpty()
            engineLabel = picked?.label?.toString().orEmpty()
                .ifBlank { enginePackage.ifBlank { "系统默认引擎（读不到引擎名）" } }

            val voice: Voice? = runCatching { engine.voice }.getOrNull()
            voiceName = voice?.name.orEmpty()
            networkRequired = voice?.isNetworkConnectionRequired ?: false

            status = if (problem.isEmpty()) Status.READY else Status.UNAVAILABLE
            onChanged?.invoke()
        }
    }

    /** 一句话说清现状，给界面直接用。 */
    fun describe(): String {
        return when (status) {
            Status.INIT -> "正在初始化系统朗读…"
            Status.ERROR -> problem.ifBlank { "系统朗读不可用" }
            Status.UNAVAILABLE -> problem
            Status.READY -> buildString {
                append(engineLabel.ifBlank { "系统默认引擎" })
                if (voiceName.isNotBlank()) append(" · ").append(voiceName)
                append(if (networkRequired) " · 需要联网" else " · 可离线")
            }
        }
    }

    val available: Boolean get() = status == Status.READY

    fun speak(text: String, onFinished: () -> Unit) {
        val engine = tts
        if (engine == null || !available) {
            onFinished()
            return
        }
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = Unit
            override fun onDone(utteranceId: String?) = onFinished()
            @Suppress("OVERRIDE_DEPRECATION")
            override fun onError(utteranceId: String?) = onFinished()
            override fun onError(utteranceId: String?, errorCode: Int) = onFinished()
        })
        engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, "echo-tts-" + System.currentTimeMillis())
    }

    fun stop() {
        runCatching { tts?.stop() }
    }

    fun shutdown() {
        runCatching { tts?.stop() }
        runCatching { tts?.shutdown() }
        tts = null
    }
}
