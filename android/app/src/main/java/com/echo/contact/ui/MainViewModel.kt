package com.echo.contact.ui

import android.app.Application
import androidx.lifecycle.viewModelScope
import com.echo.contact.audio.EchoTtsPlayer
import com.echo.contact.audio.InputDevices
import com.echo.contact.audio.MeetingAudio
import com.echo.contact.audio.MeetingAudioPlayer
import com.echo.contact.audio.Recorder
import com.echo.contact.audio.SpeechInput
import com.echo.contact.audio.SystemTts
import com.echo.contact.data.ApiClient
import com.echo.contact.data.MeetingAudioFetcher
import com.echo.contact.data.PairStore
import com.echo.contact.data.dto.CommandItem
import com.echo.contact.data.dto.CommandRequest
import com.echo.contact.data.dto.CommandSessionResponse
import com.echo.contact.data.dto.DailyReviewState
import com.echo.contact.data.dto.DailyReviewSubmitRequest
import com.echo.contact.data.dto.DailyReviewSummary
import com.echo.contact.data.dto.MeetingDetail
import com.echo.contact.data.dto.MeetingItem
import com.echo.contact.data.dto.ReviewDay
import com.echo.contact.data.dto.ReviewHistoryDetail
import com.echo.contact.data.dto.StatusResponse
import com.echo.contact.data.dto.TranscribeProgress
import com.echo.contact.data.dto.TtsRequest
import com.echo.contact.service.SpeakPrefs
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.RequestBody.Companion.toRequestBody
import retrofit2.HttpException
import java.io.File
import java.io.IOException

/** 一条指令从"点发送"到"看到结果"的可见阶段。 */
enum class SubmitPhase { Idle, Sending, Accepted, Running, Done, Failed }

/** 朗读用哪一边合成。 */
enum class TtsEngine { System, Echo }

/** 最近一次提交的可见状态——**这是"点完就有反馈"的来源**。 */
data class SubmitState(
    val text: String = "",
    val phase: SubmitPhase = SubmitPhase.Idle,
    /** 后端受理时回的那句话（`{ok, message}`），或失败原因。 */
    val message: String = "",
    val reply: String = "",
    val error: String = "",
    val cmdId: Int? = null,
    val startedAt: Long = 0L,
) {
    /** 还在路上（该显示转圈）。 */
    val active: Boolean
        get() = phase == SubmitPhase.Sending || phase == SubmitPhase.Accepted || phase == SubmitPhase.Running
}

/**
 * 主页（仪表盘）：整机状态 + 发文字指令 + 指令历史。
 *
 * **为什么要轮询**：`POST /api/assistant/command` 立刻返回 `{ok: true, message: "已提交"}`
 * （app/assistant.py:783 `send_text` 把活丢进后台线程就返回），真正执行在后台：
 * 那一行 `commands.status` 会走 `pending → sent → running → done/failed`，
 * `reply` 是**后来**才写进去的（后端最长等 `REPLY_TIMEOUT_S = 300` 秒，app/assistant.py:91）。
 * 所以"发完刷一次"必然刷到空结果 —— 必须盯着那一条直到它有结论。
 */
class MainViewModel(app: Application) : BaseViewModel(app) {

    // ---- 仪表盘：整机状态 ----
    /** `GET /api/status` 解析后的结果；null = 还没拿到。 */
    private val _status = MutableStateFlow<StatusResponse?>(null)
    val status: StateFlow<StatusResponse?> = _status.asStateFlow()

    /** null = 还没试过；true/false = 最近一次探测通没通。 */
    private val _connected = MutableStateFlow<Boolean?>(null)
    val connected: StateFlow<Boolean?> = _connected.asStateFlow()

    /** 连不上时的原因（人话）。 */
    private val _statusError = MutableStateFlow<String?>(null)
    val statusError: StateFlow<String?> = _statusError.asStateFlow()

    // ---- 指令 ----
    private val _commands = MutableStateFlow<List<CommandItem>>(emptyList())
    val commands: StateFlow<List<CommandItem>> = _commands.asStateFlow()

    private val _submit = MutableStateFlow(SubmitState())
    val submit: StateFlow<SubmitState> = _submit.asStateFlow()

    private val _baseUrl = MutableStateFlow<String>("")
    val baseUrl: StateFlow<String> = _baseUrl.asStateFlow()

    /** 正在盯着的那次提交；再来一条就把它顶掉。 */
    private var pollJob: Job? = null

    /** 状态连续失败次数：单次抖动不翻脸（自动刷新 3 秒一次）。 */
    private var statusFailStreak = 0

    /** 历史分页：还有没有更早的；以及上次用的 limit/q（"加载更多"要沿用同一套筛选）。 */
    private val _commandsHasMore = MutableStateFlow(false)
    val commandsHasMore: StateFlow<Boolean> = _commandsHasMore.asStateFlow()
    private var lastLimit = 30
    private var lastQuery = ""

    // ---- 朗读（TTS）：这一版只是"拿来听一听"，所以引擎由调用方指定，不存偏好 ----

    /** 系统 TTS 的现状（引擎名 / 音色 / 要不要联网）。 */
    private val _systemTtsInfo = MutableStateFlow("正在初始化系统朗读…")
    val systemTtsInfo: StateFlow<String> = _systemTtsInfo.asStateFlow()

    private val _speaking = MutableStateFlow(false)
    val speaking: StateFlow<Boolean> = _speaking.asStateFlow()

    /** 朗读失败的**人话原因**（409 朗读被关 / 502 合成失败 / 没引擎）。 */
    private val _speakNote = MutableStateFlow<String?>(null)
    val speakNote: StateFlow<String?> = _speakNote.asStateFlow()

    private val systemTts: SystemTts by lazy { SystemTts(getApplication()) }
    private val echoPlayer: EchoTtsPlayer by lazy { EchoTtsPlayer(getApplication()) }

    // ---- 手机识别（SpeechRecognizer）----

    private val speechInput: SpeechInput by lazy { SpeechInput(getApplication()) }

    private val _recognizing = MutableStateFlow(false)
    val recognizing: StateFlow<Boolean> = _recognizing.asStateFlow()

    /** 边说边出的临时结果（能立刻看到识别在动）。 */
    private val _partialText = MutableStateFlow("")
    val partialText: StateFlow<String> = _partialText.asStateFlow()

    /** 最终识别结果。 */
    private val _finalText = MutableStateFlow("")
    val finalText: StateFlow<String> = _finalText.asStateFlow()

    private val _recognizeNote = MutableStateFlow<String?>(null)
    val recognizeNote: StateFlow<String?> = _recognizeNote.asStateFlow()

    /** 引擎自己的阶段（"在听…"/"识别中…"）—— **必须显示出来**，否则点了"说完了"看着像没反应。 */
    private val _recognizeState = MutableStateFlow("")
    val recognizeState: StateFlow<String> = _recognizeState.asStateFlow()

    /** 这台设备能不能用系统识别 + 有哪些识别服务。 */
    private val _speechInfo = MutableStateFlow("")
    val speechInfo: StateFlow<String> = _speechInfo.asStateFlow()

    private var recognizeJob: Job? = null

    fun refreshSpeechInfo() {
        val ok = speechInput.available()
        val svcs = speechInput.services()
        val uis = speechInput.uiHandlers()
        _speechInfo.value = buildString {
            append("服务").append(if (ok) "可用" else "不可用")
            if (svcs.isNotEmpty()) append("（").append(svcs.joinToString("、")).append("）")
            append(" ｜ 识别界面 ").append(uis.size).append(" 个")
            if (uis.isNotEmpty()) append("（").append(uis.joinToString("、")).append("）")
        }
    }

    /** 开始听（调用方要**先**拿到 RECORD_AUDIO 权限）。 */
    fun startListening() {
        _recognizeNote.value = null
        _partialText.value = ""
        _finalText.value = ""
        refreshSpeechInfo()

        speechInput.onPartial = { _partialText.value = it }
        speechInput.onState = { _recognizeState.value = it }
        speechInput.onFinal = { text ->
            recognizeJob?.cancel()
            _finalText.value = text
            _recognizing.value = false
            _recognizeState.value = ""
        }
        speechInput.onError = { msg ->
            recognizeJob?.cancel()
            _recognizeNote.value = msg
            _recognizing.value = false
            _recognizeState.value = ""
        }

        _recognizing.value = speechInput.start()
        if (_recognizing.value) {
            // 兜底：不让它永远卡在"在听…"（用户真机上就是这个症状）
            recognizeJob?.cancel()
            recognizeJob = viewModelScope.launch {
                delay(SESSION_LIMIT_MS)
                if (_recognizing.value) finishListening("超过 ${SESSION_LIMIT_MS / 1000} 秒没有结果，已停下")
            }
        }
    }

    /**
     * 「说完了」：要最终结果；**等不到就一定有反馈**。
     *
     * 为什么要有宽限期兜底：`stopListening()` 之后引擎**可能不回调任何东西**
     * （它可能早就自己结束了那次会话）。那种情况下原来什么都不显示 —— 用户看到的就是
     * "点了没反应"。现在：给引擎 [FINISH_GRACE_MS] 毫秒；还不给，就拿最后那段
     * **临时结果**定稿，并如实说明那是临时结果。
     */
    fun finishListening(reason: String? = null) {
        if (!_recognizing.value) return
        _recognizeState.value = "识别中…"
        speechInput.stop()

        recognizeJob?.cancel()
        recognizeJob = viewModelScope.launch {
            delay(FINISH_GRACE_MS)
            if (!_recognizing.value) return@launch   // 期间引擎给了结果/错误
            when (val outcome = resolveRecognizeOutcome(_finalText.value, _partialText.value)) {
                null -> _recognizeNote.value = reason ?: "没拿到结果（没听清？可以再说一遍）"
                else -> {
                    _finalText.value = outcome.text
                    if (outcome.usedPartial) {
                        _recognizeNote.value = reason ?: "引擎没给最终结果，用的是上面那段临时结果"
                    }
                }
            }
            // **关键**：放弃等结果就必须把会话放掉。只清界面状态不释放，
            // 下一轮会被引擎判成"忙"（ERROR_RECOGNIZER_BUSY）—— 用户真机上正是这样。
            speechInput.release()
            _recognizing.value = false
            _recognizeState.value = ""
        }
    }

    fun cancelListening() {
        recognizeJob?.cancel()
        speechInput.cancel()
        speechInput.release()
        _recognizing.value = false
        _recognizeState.value = ""
    }

    /**
     * **系统识别界面**回来的结果（`RecognizerIntent` + `startActivityForResult`）。
     *
     * 为什么留这条：不少国产 ROM 的识别服务**不服务嵌入式 API**
     * （`SpeechRecognizer` 一个回调都不给），但它自己那个识别界面是好的。
     * 这条走的是"系统的 UI、系统的麦"，我们只收文本 —— 用来判断
     * "这台设备到底能不能识别"，比我在这边猜准得多。
     */
    fun acceptSystemResult(text: String) {
        recognizeJob?.cancel()
        _finalText.value = text.trim()
        _recognizeNote.value = "（来自系统识别界面）"
        _recognizing.value = false
        _recognizeState.value = ""
    }

    /** 试听用的默认句子：两边念**同一句**，A/B 才有意义。 */
    val sampleSentence: String = "你好，我是 ECHO。现在是晚上八点二十，明天北京多云，最高十八度。"

    fun init() {
        // 系统 TTS 的初始化结果是不确定的（设备可能压根没装引擎），所以状态由它回调推给 UI
        systemTts.onChanged = { _systemTtsInfo.value = systemTts.describe() }
        systemTts.init()
        _systemTtsInfo.value = systemTts.describe()

        viewModelScope.launch {
            val url = withContext(Dispatchers.IO) {
                PairStore.baseUrl(getApplication())
            } ?: ""
            _baseUrl.value = url
            refresh()   // 状态 + 历史一起拉
        }
    }

    /**
     * 念一句话。两条路：
     *  - [TtsEngine.System]：本机合成（可能离线、零往返）；
     *  - [TtsEngine.Echo]：`POST /api/tts` 拿字节再播（音色与电脑一致，必须有网）。
     *
     * **默认走设置里选的那个**（`SpeakPrefs`，默认手机念）：自动念的地方
     * （指令回复、回顾播报、会议播报）一个都不再写死引擎 —— 以前写死成
     * [TtsEngine.System]，用户在设置里选了"ECHO 念"也没用（2026-10-15 正式版修的）。
     */
    fun preferredEngine(): TtsEngine =
        if (SpeakPrefs.load(getApplication()) == SpeakPrefs.ENGINE_ECHO) {
            TtsEngine.Echo
        } else {
            TtsEngine.System
        }

    fun speak(text: String, engine: TtsEngine = preferredEngine()) {
        val content = text.trim()
        if (content.isEmpty()) return
        stopSpeaking()
        _speakNote.value = null

        if (engine == TtsEngine.System) {
            if (!systemTts.available) {
                _speakNote.value = systemTts.describe()
                return
            }
            _speaking.value = true
            systemTts.speak(content) { _speaking.value = false }
            return
        }

        _speaking.value = true
        viewModelScope.launch {
            try {
                val resp = api().tts(TtsRequest(text = content))
                if (!resp.isSuccessful) {
                    // 409=朗读被关、422=空文本、502=合成失败：后端都给了 detail，原样显示
                    _speakNote.value = parseError(resp)
                    _speaking.value = false
                    return@launch
                }
                val bytes = resp.body()?.bytes()
                if (bytes == null || bytes.isEmpty()) {
                    _speakNote.value = "ECHO 返回了空音频"
                    _speaking.value = false
                    return@launch
                }
                val mime = resp.body()?.contentType()?.toString()
                val file = withContext(Dispatchers.IO) { echoPlayer.cache(bytes, mime) }
                // 别叫 `engine` —— 那是本函数的参数（选哪条路念），这里说的是
                // **服务端**实际用了哪个 TTS 引擎（回给面板看的）。
                val echoEngine = resp.headers()["X-Echo-Tts-Engine"].orEmpty()
                if (echoEngine.isNotBlank()) _speakNote.value = "ECHO 引擎：$echoEngine"
                echoPlayer.play(file) { _speaking.value = false }
            } catch (e: IOException) {
                _speakNote.value = "无法连接：${e.message}"
                _speaking.value = false
            } catch (e: Exception) {
                _speakNote.value = "朗读出错：${e.message}"
                _speaking.value = false
            }
        }
    }

    fun stopSpeaking() {
        runCatching { systemTts.stop() }
        runCatching { echoPlayer.stop() }
        _speaking.value = false
    }

    override fun onCleared() {
        stopSpeaking()
        runCatching { recorder.cancel() }
        runCatching { systemTts.shutdown() }
        runCatching { speechInput.destroy() }
        super.onCleared()
    }

    private fun api() = ApiClient.create(_baseUrl.value.ifBlank { ApiClient.DEFAULT_BASE_URL })

    /** 拉一次状态与历史（下拉刷新 / 进页面时调）。 */
    fun refresh() {
        checkStatus()
        loadCommands()
        loadReview()
        loadReviewDays()
        loadMeetings()
    }

    // ---- 会议（录音在 ECHO 主机上做，手机只是遥控器）----

    private val _meetings = MutableStateFlow<List<MeetingItem>>(emptyList())
    val meetings: StateFlow<List<MeetingItem>> = _meetings.asStateFlow()

    private val _meetingsNote = MutableStateFlow<String?>(null)
    val meetingsNote: StateFlow<String?> = _meetingsNote.asStateFlow()

    /** 开始/停止录音这类动作正在进行（按钮防连点）。 */
    private val _meetingActionBusy = MutableStateFlow(false)
    val meetingActionBusy: StateFlow<Boolean> = _meetingActionBusy.asStateFlow()

    /** 转写进度：键可能是会议 id，也可能是会议名（导入录音那条路）。 */
    private val _transcribe = MutableStateFlow<Map<String, TranscribeProgress>>(emptyMap())
    val transcribe: StateFlow<Map<String, TranscribeProgress>> = _transcribe.asStateFlow()

    // ---- 单场会议详情 ----

    private val _meetingDetail = MutableStateFlow<MeetingDetail?>(null)
    val meetingDetail: StateFlow<MeetingDetail?> = _meetingDetail.asStateFlow()

    private val _meetingDetailLoading = MutableStateFlow(false)
    val meetingDetailLoading: StateFlow<Boolean> = _meetingDetailLoading.asStateFlow()

    private val _meetingDetailNote = MutableStateFlow<String?>(null)
    val meetingDetailNote: StateFlow<String?> = _meetingDetailNote.asStateFlow()

    /** 「看纪要 / 看主题 / 看转写」取回来的**文本正文**（面板那三个入口）。 */
    private val _meetingFile = MutableStateFlow<Pair<String, String>?>(null)   // kind to text
    val meetingFile: StateFlow<Pair<String, String>?> = _meetingFile.asStateFlow()

    // ---- 会议音频分段播放（阶段6c）----
    //
    // 主机上录音是**按段存的**（01.flac、02.flac…），后端 `GET /meetings/{id}/audio?seg=N`
    // 一次给一段（还支持 Range）。手机上听一段是合理的事，"把一小时会议从头播到尾"不是
    // —— 所以这里只做"第几段 / 上一段 / 下一段"，并把"在取/在放/放完了/出错"如实显示。
    private val audioPlayer: MeetingAudioPlayer by lazy { MeetingAudioPlayer(getApplication()) }

    /** 正在放/正要放的段（0 = 没在放）；界面据此高亮与判断"上一段/下一段"。 */
    private val _audioSeg = MutableStateFlow(0)
    val audioSeg: StateFlow<Int> = _audioSeg.asStateFlow()

    private val _audioState = MutableStateFlow("")
    val audioState: StateFlow<String> = _audioState.asStateFlow()

    private val _audioPlaying = MutableStateFlow(false)
    val audioPlaying: StateFlow<Boolean> = _audioPlaying.asStateFlow()

    private var audioJob: Job? = null

    /** 放这一场会议的第 [seg] 段（1 起）。取音频 → 播；失败原因直接给界面。 */
    fun playMeetingSegment(mid: Int, seg: Int, total: Int) {
        audioJob?.cancel()
        audioPlayer.onState = { state, message ->
            _audioPlaying.value = state == MeetingAudioPlayer.State.PLAYING
            _audioState.value = when (state) {
                MeetingAudioPlayer.State.LOADING -> MeetingAudio.label(seg, total) + " · 正在准备…"
                MeetingAudioPlayer.State.PLAYING -> MeetingAudio.label(seg, total) + " · 播放中"
                MeetingAudioPlayer.State.PAUSED -> MeetingAudio.label(seg, total) + " · 已暂停"
                MeetingAudioPlayer.State.DONE -> MeetingAudio.label(seg, total) + " · 放完了"
                MeetingAudioPlayer.State.ERROR -> message
                MeetingAudioPlayer.State.IDLE -> _audioState.value
            }
        }
        audioJob = viewModelScope.launch {
            _audioSeg.value = seg
            _audioState.value = MeetingAudio.label(seg, total) + " · 正在取音频…"
            val fetcher = MeetingAudioFetcher(
                api(),
                File(getApplication<Application>().cacheDir, "meeting-audio"),
            )
            val file = when (val out = fetcher.fetch(mid, seg)) {
                is MeetingAudioFetcher.Outcome.Failed -> {
                    _audioState.value = out.message
                    _audioPlaying.value = false
                    return@launch
                }
                is MeetingAudioFetcher.Outcome.Cached -> out.file
                is MeetingAudioFetcher.Outcome.Downloaded -> out.file
            }
            audioPlayer.play(file, seg)
        }
    }

    /** 暂停/继续这一段（按钮在界面上是同一个）。 */
    fun toggleMeetingAudio() {
        if (audioPlayer.isPlaying) audioPlayer.pause() else audioPlayer.resume()
    }

    /** 停掉并清掉这一场的音频缓存（离开详情页时调）。 */
    fun stopMeetingAudio(clearCacheFor: Int? = null) {
        audioJob?.cancel()
        audioPlayer.release()
        _audioPlaying.value = false
        _audioSeg.value = 0
        _audioState.value = ""
        if (clearCacheFor != null) {
            runCatching {
                File(getApplication<Application>().cacheDir, "meeting-audio")
                    .listFiles { f -> f.name.startsWith("meeting-$clearCacheFor-seg") }
                    ?.forEach { it.delete() }
            }
        }
    }

    fun openMeeting(id: Int) {
        _meetingDetailLoading.value = true
        _meetingDetailNote.value = null
        _meetingFile.value = null
        viewModelScope.launch {
            try {
                val resp = api().meetingDetail(id)
                if (resp.isSuccessful) {
                    _meetingDetail.value = resp.body()
                } else {
                    _meetingDetailNote.value = parseError(resp)
                }
            } catch (e: IOException) {
                _meetingDetailNote.value = "无法连接：${e.message}"
            } catch (e: Exception) {
                _meetingDetailNote.value = "读取会议出错：${e.message}"
            }
            _meetingDetailLoading.value = false
        }
    }

    fun closeMeeting() {
        stopMeetingAudio()
        _meetingDetail.value = null
        _meetingDetailNote.value = null
        _meetingFile.value = null
    }

    /** 取会议产出的文本文件（转写 / 主题 / 纪要）。 */
    fun loadMeetingFile(id: Int, kind: String) {
        _meetingDetailNote.value = null
        viewModelScope.launch {
            try {
                val resp = api().meetingFile(id, kind)
                if (resp.isSuccessful) {
                    val text = withContext(Dispatchers.IO) { resp.body()?.string().orEmpty() }
                    if (text.isBlank()) {
                        _meetingDetailNote.value = "「$kind」是空的"
                    } else {
                        _meetingFile.value = kind to text
                    }
                } else {
                    _meetingDetailNote.value = parseError(resp)
                }
            } catch (e: IOException) {
                _meetingDetailNote.value = "无法连接：${e.message}"
            } catch (e: Exception) {
                _meetingDetailNote.value = "读取「$kind」出错：${e.message}"
            }
        }
    }

    fun closeMeetingFile() {
        _meetingFile.value = null
    }

    fun loadMeetings(limit: Int = 20) {
        viewModelScope.launch {
            try {
                val resp = api().meetings(limit = limit)
                if (resp.isSuccessful) _meetings.value = resp.body()?.items.orEmpty()
            } catch (_: Exception) {
                // 列表不是主功能，静默
            }
            try {
                val t = api().transcribeStatus()
                if (t.isSuccessful) _transcribe.value = t.body().orEmpty()
            } catch (_: Exception) {
            }
        }
    }

    /**
     * 开始录音。**录的是电脑那支麦克风** —— 手机只是遥控。
     * 后端恒回 200，成没成看 `{ok, message}`。
     */
    fun startMeeting() {
        if (_meetingActionBusy.value) return
        _meetingActionBusy.value = true
        _meetingsNote.value = null
        viewModelScope.launch {
            try {
                val resp = api().meetingStart()
                val b = resp.body()
                _meetingsNote.value = when {
                    !resp.isSuccessful -> parseError(resp)
                    b?.ok == true -> "已开始录音：${b.message.orEmpty()}"
                    else -> b?.message ?: "没能开始录音"
                }
            } catch (e: IOException) {
                _meetingsNote.value = "无法连接：${e.message}"
            } catch (e: Exception) {
                _meetingsNote.value = "出错：${e.message}"
            }
            _meetingActionBusy.value = false
            checkStatus()
            loadMeetings()
        }
    }

    fun stopMeeting() {
        if (_meetingActionBusy.value) return
        _meetingActionBusy.value = true
        _meetingsNote.value = null
        viewModelScope.launch {
            try {
                val resp = api().meetingStop()
                val b = resp.body()
                _meetingsNote.value = when {
                    !resp.isSuccessful -> parseError(resp)
                    b?.ok == true -> "已停止，正在转写：${b.message.orEmpty()}"
                    else -> b?.message ?: "没能停止录音"
                }
            } catch (e: IOException) {
                _meetingsNote.value = "无法连接：${e.message}"
            } catch (e: Exception) {
                _meetingsNote.value = "出错：${e.message}"
            }
            _meetingActionBusy.value = false
            checkStatus()
            loadMeetings()
        }
    }

    // ---- 每日回顾（手机只播报；口述要占主机的麦，手机做不了）----

    private val _review = MutableStateFlow<DailyReviewState?>(null)
    val review: StateFlow<DailyReviewState?> = _review.asStateFlow()

    private val _reviewSummary = MutableStateFlow<DailyReviewSummary?>(null)
    val reviewSummary: StateFlow<DailyReviewSummary?> = _reviewSummary.asStateFlow()

    private val _reviewNote = MutableStateFlow<String?>(null)
    val reviewNote: StateFlow<String?> = _reviewNote.asStateFlow()

    /** 按天的回顾历史（面板「回顾」那一页）。 */
    private val _reviewDays = MutableStateFlow<List<ReviewDay>>(emptyList())
    val reviewDays: StateFlow<List<ReviewDay>> = _reviewDays.asStateFlow()

    /** 手机提交回顾进行中（转写 + 整理可能要一两分钟）。 */
    private val _reviewSubmitting = MutableStateFlow(false)
    val reviewSubmitting: StateFlow<Boolean> = _reviewSubmitting.asStateFlow()

    private val _reviewSubmittingNote = MutableStateFlow<String?>(null)
    val reviewSubmittingNote: StateFlow<String?> = _reviewSubmittingNote.asStateFlow()

    /** 点开某一天：那一轮轮的原文/播报。 */
    private val _reviewDay = MutableStateFlow<ReviewHistoryDetail?>(null)
    val reviewDay: StateFlow<ReviewHistoryDetail?> = _reviewDay.asStateFlow()

    fun loadReviewDays(limit: Int = 60) {
        viewModelScope.launch {
            try {
                val resp = api().dailyReviewHistory(limit)
                if (resp.isSuccessful) _reviewDays.value = resp.body()?.items.orEmpty()
            } catch (_: Exception) {
                // 历史不是主功能，静默
            }
        }
    }

    fun openReviewDay(date: String) {
        viewModelScope.launch {
            try {
                val resp = api().dailyReviewDay(date)
                if (resp.isSuccessful) _reviewDay.value = resp.body()
            } catch (e: IOException) {
                _reviewNote.value = "读取失败：${e.message}"
            } catch (_: Exception) {
            }
        }
    }

    fun closeReviewDay() {
        _reviewDay.value = null
    }

    /** 念某一天的播报（把当天每一轮的 brief 拼起来）。 */
    fun speakReviewDay(date: String) {
        viewModelScope.launch {
            val detail = _reviewDay.value?.takeIf { it.date == date } ?: run {
                try {
                    api().dailyReviewDay(date).body()
                } catch (_: Exception) {
                    null
                }
            }
            val parts = detail?.turns
                ?.mapNotNull { it.brief.takeIf { b -> b.isNotBlank() } }
                .orEmpty()
            if (parts.isEmpty()) {
                _speakNote.value = "$date 没有可念的播报"
                return@launch
            }
            speak(parts.joinToString("\n"))
        }
    }

    /**
     * **手机做回顾**：录一段长口述 → 转写 → 交给 `/api/daily-review/submit` → 念 `spoken`。
     *
     * 为什么要绕"转写再提交"这一下：`submit` 收的是**文本**
     * （api.py:867 的注释写的是"面板调试与文本兜底用"，但它对手机正好是主力路径）。
     * 而主机麦克风那条（`/daily-review/go`）在车里根本听不到你说话 ——
     * 手机做这件事才是对的。
     *
     * ⚠️ `submit` 是**阻塞**的（等智能体整理完才返回，实测一次 122 秒），所以：
     *  - 用长超时的客户端（[ApiClient.LONG_READ_TIMEOUT_S]）；
     *  - 业务失败是 **200 + ok=false**，要看 `ok` 不能只看状态码。
     */
    fun submitReviewDictation() {
        if (!_recording.value) return
        recordTicker?.cancel()
        _recording.value = false
        val wav = recorder.stop()
        if (wav == null) {
            _speakNote.value = "录得太短了（不到 0.3 秒），再说一次"
            return
        }
        _reviewSubmitting.value = true
        _reviewSubmittingNote.value = "正在转写这段口述…"
        viewModelScope.launch {
            try {
                val long = ApiClient.create(
                    _baseUrl.value.ifBlank { ApiClient.DEFAULT_BASE_URL },
                    ApiClient.LONG_READ_TIMEOUT_S,
                )
                val part = MultipartBody.Part.createFormData(
                    "file",
                    "dictation.wav",
                    wav.toRequestBody("audio/wav".toMediaType()),
                )
                val stt = long.transcribe(part)
                if (!stt.isSuccessful) {
                    // 422 = 没识别出文本 / 音频太短；502 = 转写失败
                    _reviewSubmittingNote.value = parseError(stt)
                    _reviewSubmitting.value = false
                    return@launch
                }
                val text = stt.body()?.text.orEmpty().trim()
                if (text.isEmpty()) {
                    _reviewSubmittingNote.value = "没识别出文字（太短或没听清？）"
                    _reviewSubmitting.value = false
                    return@launch
                }
                _voiceText.value = text
                _reviewSubmittingNote.value = "转写好了，正在交给回顾整理…（可能要 2–3 分钟）"

                val resp = long.dailyReviewSubmit(DailyReviewSubmitRequest(text = text))
                val b = resp.body()
                when {
                    !resp.isSuccessful -> _reviewSubmittingNote.value = parseError(resp)
                    b?.ok != true -> _reviewSubmittingNote.value =
                        b?.error.orEmpty().ifBlank { "回顾没成功（服务端没给原因）" }
                    else -> {
                        _reviewSubmittingNote.value =
                            "整理完成（${"%.1f".format(b.seconds)} 秒，来源 ${b.source}）"
                        // spoken 就是"会被念出来的那句话"，直接念
                        b.spoken.takeIf { it.isNotBlank() }?.let { speak(it) }
                    }
                }
                // 无论成没成，都要把口语那句念出来（服务端在三层兜底里都给了 spoken）
                if (b?.ok != true && !b?.spoken.isNullOrBlank()) {
                    speak(b!!.spoken)
                }
            } catch (e: IOException) {
                _reviewSubmittingNote.value = "无法连接（或等超时了）：${e.message}"
            } catch (e: Exception) {
                _reviewSubmittingNote.value = "提交回顾出错：${e.message}"
            }
            _reviewSubmitting.value = false
            loadReview()
            loadReviewDays()
            loadCommands()
        }
    }

    /** 停止**主机上**正在进行的语音回顾（那是主机麦克风那条路，手机停它是有用的）。 */
    fun stopHostReview() {
        viewModelScope.launch {
            try {
                val resp = api().dailyReviewStop()
                _reviewNote.value = if (resp.isSuccessful) "已请求停止主机上的回顾" else parseError(resp)
            } catch (e: Exception) {
                _reviewNote.value = "停止出错：${e.message}"
            }
            loadReview()
        }
    }

    fun cancelReviewDictation() {
        cancelRecording()
    }

    // ---- 指令的会话内容（面板「历史 → 看会话」）----

    private val _session = MutableStateFlow<CommandSessionResponse?>(null)
    val session: StateFlow<CommandSessionResponse?> = _session.asStateFlow()

    private val _sessionLoading = MutableStateFlow(false)
    val sessionLoading: StateFlow<Boolean> = _sessionLoading.asStateFlow()

    private val _sessionNote = MutableStateFlow<String?>(null)
    val sessionNote: StateFlow<String?> = _sessionNote.asStateFlow()

    fun loadSession(cmdId: Int) {
        _sessionLoading.value = true
        _sessionNote.value = null
        viewModelScope.launch {
            try {
                val resp = api().commandSession(cmdId)
                if (resp.isSuccessful) {
                    _session.value = resp.body()
                } else {
                    _sessionNote.value = parseError(resp)
                }
            } catch (e: IOException) {
                _sessionNote.value = "无法连接：${e.message}"
            } catch (e: Exception) {
                _sessionNote.value = "读取会话出错：${e.message}"
            }
            _sessionLoading.value = false
        }
    }

    fun closeSession() {
        _session.value = null
        _sessionNote.value = null
    }

    fun loadReview() {
        viewModelScope.launch {
            try {
                val api = api()
                val state = api.dailyReview()
                val summary = api.dailyReviewSummary()
                if (state.isSuccessful) _review.value = state.body()
                if (summary.isSuccessful) _reviewSummary.value = summary.body()
                if (!state.isSuccessful) _reviewNote.value = parseError(state)
            } catch (e: IOException) {
                _reviewNote.value = "回顾读取失败：${e.message}"
            } catch (_: Exception) {
                // 回顾不是主功能，静默失败即可（卡片会显示"读不到"）
            }
        }
    }

    /**
     * 念回顾播报：**用手机的系统 TTS**（离线、零等待 —— 这是 2026-10-09 定的口径）。
     *
     * 今天有几轮就念今天的几轮（拼起来），否则念最近一次那一段。
     * 念的是 `brief`（播报），**不是** `text`（口述转写原文，带噪音，不是给人听的）。
     */
    fun speakReview() {
        val summary = _reviewSummary.value
        if (summary == null) {
            _speakNote.value = "还没读到回顾数据"
            return
        }
        viewModelScope.launch {
            val parts = mutableListOf<String>()
            if (summary.todayTurns > 0 && summary.today.isNotBlank()) {
                try {
                    val resp = api().dailyReviewDay(summary.today)
                    if (resp.isSuccessful) {
                        parts += resp.body()?.turns?.mapNotNull { it.brief.takeIf { b -> b.isNotBlank() } }.orEmpty()
                    }
                } catch (_: Exception) {
                    // 取当天详情失败就退回"最近一次"
                }
            }
            if (parts.isEmpty()) {
                summary.lastBrief?.takeIf { it.isNotBlank() }?.let { parts += it }
            }
            if (parts.isEmpty()) {
                _speakNote.value = "还没有可念的回顾播报"
                return@launch
            }
            speak(parts.joinToString("\n"))
        }
    }

    /**
     * 探活一次（`/health` + `/api/status`）。**只在进页面/手动刷新时调**。
     *
     * 为什么探活单独一条：`/health` 是唯一不带令牌的接口，用它才能把
     * "连不上"与"令牌不对"分开（拿 status 探活，令牌坏了会得到 401，
     * 很容易被误判成"服务没起来"）。但自动刷新每 3 秒一次，
     * 每次都发两个请求太浪费 —— 所以自动刷新只走 [checkStatus]。
     */
    fun checkStatus() {
        viewModelScope.launch {
            val reachable = try {
                api().health().isSuccessful
            } catch (e: Exception) {
                false
            }
            if (!reachable) {
                statusFailStreak++
                _connected.value = false
                _statusError.value = "连不上 ECHO：地址不对、不在同一网络，或电脑防火墙没放行 8970"
                return@launch
            }
            fetchStatusOnce()
        }
    }

    /**
     * 只拉一次状态（自动刷新用）。
     *
     * 单次失败**不立刻翻脸**：自动刷新 3 秒一次，一次网络抖动就把整页刷成红的很难看。
     * 连续两次才算断。但 **401 是定论**（令牌问题），立刻显示。
     */
    private suspend fun fetchStatusOnce() {
        try {
            val resp = api().status()
            if (resp.isSuccessful && resp.body() != null) {
                statusFailStreak = 0
                _status.value = resp.body()
                _connected.value = true
                _statusError.value = null
            } else if (resp.code() == 401) {
                statusFailStreak = 2
                _connected.value = false
                _statusError.value = "能连上，但令牌无效或缺失（HTTP 401）：请重新配对一次"
            } else {
                noteStatusFailure("后端返回 HTTP ${resp.code()}")
            }
        } catch (e: Exception) {
            noteStatusFailure("读取状态出错：${e.message}")
        }
    }

    private fun noteStatusFailure(message: String) {
        statusFailStreak++
        if (statusFailStreak >= 2) {
            _connected.value = false
            _statusError.value = message
        }
    }

    /** 仪表盘那句"实时"：面板是 3 秒一次，这里对齐。 */
    fun autoRefreshTick() {
        viewModelScope.launch { fetchStatusOnce() }
    }

    /**
     * 发一条指令：立刻给出"已提交"的反馈，然后**盯住这一行直到有结果**。
     *
     * 文字这条路不回 `commandId`（只有 `/assistant/voice-command` 才回），
     * 所以靠"发送前的最大 id + 文本 + source=mobile"认领新出现的那一行（见 [pickSubmitted]）。
     */
    fun sendCommand(text: String) {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        // 只在"正在提交"这一瞬挡住重复点击；执行中可以再发（后端允许排队）
        if (_submit.value.phase == SubmitPhase.Sending) return

        pollJob?.cancel()
        val maxIdBefore = _commands.value.maxOfOrNull { it.id ?: 0 } ?: 0
        _submit.value = SubmitState(
            text = trimmed,
            phase = SubmitPhase.Sending,
            message = "正在提交…",
            startedAt = System.currentTimeMillis(),
        )

        viewModelScope.launch {
            try {
                val resp = api().sendCommand(CommandRequest(text = trimmed, source = "mobile"))
                val body = resp.body()
                if (resp.isSuccessful && body?.ok == true) {
                    _submit.value = _submit.value.copy(
                        phase = SubmitPhase.Accepted,
                        message = body.message ?: "已提交",
                    )
                    pollJob = launch { awaitResult(trimmed, maxIdBefore) }
                    return@launch
                }
                // ok=false 也是**有原因**的（例如"已有命令流进行中，请稍候"），要原样显示
                val msg = if (resp.isSuccessful) (body?.message ?: "指令发送失败") else parseError(resp)
                fail(msg)
            } catch (e: HttpException) {
                fail(parseHttp(e))
            } catch (e: IOException) {
                fail("无法连接：${e.message}")
            } catch (e: Exception) {
                fail("发送出错：${e.message}")
            }
        }
    }

    // ---- 语音指令（录音 → ECHO 转写 → 下发）----

    private val recorder: Recorder by lazy { Recorder() }

    private val _recording = MutableStateFlow(false)
    val recording: StateFlow<Boolean> = _recording.asStateFlow()

    private val _recordMs = MutableStateFlow(0L)
    val recordMs: StateFlow<Long> = _recordMs.asStateFlow()

    /** ECHO 侧转写出来的文本（手机看不到音频，这是唯一的真相来源）。 */
    private val _voiceText = MutableStateFlow("")
    val voiceText: StateFlow<String> = _voiceText.asStateFlow()

    private var recordTicker: Job? = null

    /** 开始录音（调用方要**先**拿到 RECORD_AUDIO 权限）。返回 false = 开不了麦。 */
    fun startRecording(): Boolean {
        if (_recording.value) return true
        _voiceText.value = ""
        _submit.value = SubmitState()
        // 用户在「设置 → 设备」里指定的那支麦（null = 跟随系统）。取不到就跟随系统 ——
        // 选的那支不在了是他的事，不该让"说一句"开不了麦（`InputDevices.describe` 会如实说）
        val mic = runCatching { InputDevices.preferredInput(getApplication()) }.getOrNull()
        val ok = recorder.start(mic)
        if (!ok) {
            _speakNote.value = "开不了麦克风：可能被别的应用占着，或权限没给"
            return false
        }
        _recording.value = true
        _recordMs.value = 0
        recordTicker = viewModelScope.launch {
            while (_recording.value) {
                _recordMs.value = recorder.elapsedMs()
                delay(100)
            }
        }
        return true
    }

    /** 「说完了」：停录 → 上传 ECHO 转写并下发 → 盯住那条结果。 */
    fun finishRecording() {
        if (!_recording.value) return
        recordTicker?.cancel()
        _recording.value = false
        val wav = recorder.stop()
        if (wav == null) {
            _speakNote.value = "录得太短了（不到 0.3 秒），再说一次"
            return
        }
        _submit.value = SubmitState(
            text = "（语音，正在转写…）",
            phase = SubmitPhase.Sending,
            message = "正在上传给 ECHO 转写…",
            startedAt = System.currentTimeMillis(),
        )
        viewModelScope.launch {
            try {
                val body = wav.toRequestBody("audio/wav".toMediaType())
                val resp = api().voiceCommand(body)
                val b = resp.body()
                if (!resp.isSuccessful || b?.ok != true) {
                    val msg = if (resp.isSuccessful) {
                        b?.message ?: "ECHO 没能处理这段语音"
                    } else {
                        parseError(resp)   // 422=形状不对/没识别出文本，502=转写失败
                    }
                    fail(msg)
                    return@launch
                }
                _voiceText.value = b.text.orEmpty()
                _submit.value = _submit.value.copy(
                    text = b.text.orEmpty().ifBlank { "（语音）" },
                    phase = SubmitPhase.Accepted,
                    message = b.message ?: "已提交",
                )
                awaitResult(
                    text = b.text.orEmpty(),
                    maxIdBefore = 0,
                    targetId = b.commandId,
                )
                // 语音进来的就**念出来**（这是语音交互的意义所在）；打字那条不自动念。
                // 念用手机引擎 —— 2026-10-09 定的口径（ECHO 那条要 1.3 秒起，还依赖联网）。
                val done = _submit.value
                if (done.phase == SubmitPhase.Done && done.reply.isNotBlank()) {
                    speak(done.reply)
                }
            } catch (e: HttpException) {
                fail(parseHttp(e))
            } catch (e: IOException) {
                fail("无法连接：${e.message}")
            } catch (e: Exception) {
                fail("上传语音出错：${e.message}")
            }
        }
    }

    fun cancelRecording() {
        recordTicker?.cancel()
        _recording.value = false
        recorder.cancel()
        _recordMs.value = 0
    }

    private fun fail(msg: String) {
        _submit.value = _submit.value.copy(phase = SubmitPhase.Failed, message = msg, error = msg)
    }

    /**
     * 盯住刚发的那一行：每 [POLL_INTERVAL_MS] 拉一次历史，直到它 `done`/`failed` 或超时。
     * 每一轮都把最新历史灌进 [_commands]，所以列表也是实时长的，不用手动刷新。
     */
    private suspend fun awaitResult(text: String, maxIdBefore: Int, targetId: Int? = null) {
        val deadline = System.currentTimeMillis() + RESULT_TIMEOUT_MS
        var seen: CommandItem? = null

        while (System.currentTimeMillis() < deadline) {
            val items = fetchCommands(POLL_LIMIT)
            if (items != null) {
                // 刷新头部但**保留用户翻出来的更早那些行**（否则轮询会把它打回 30 条）
                _commands.value = mergeFreshHead(items, _commands.value)
                // 语音那条回 commandId，直接按 id 认；文字那条没有 id，才需要按文本认领
                val hit = if (targetId != null) {
                    items.firstOrNull { it.id == targetId }
                } else {
                    pickSubmitted(items, text, maxIdBefore)
                }
                if (hit != null) {
                    seen = hit
                    when (hit.status) {
                        "done", "failed" -> {
                            val failed = hit.status == "failed"
                            _submit.value = _submit.value.copy(
                                phase = if (failed) SubmitPhase.Failed else SubmitPhase.Done,
                                cmdId = hit.id,
                                reply = hit.reply.orEmpty(),
                                error = hit.error.orEmpty(),
                                message = if (failed) {
                                    hit.error?.takeIf { it.isNotBlank() } ?: "执行失败"
                                } else {
                                    "耗时 ${"%.1f".format((hit.durationMs ?: 0) / 1000.0)} 秒"
                                },
                            )
                            return
                        }
                        "running" -> _submit.value = _submit.value.copy(phase = SubmitPhase.Running, cmdId = hit.id, message = "正在执行…")
                        "sent" -> _submit.value = _submit.value.copy(phase = SubmitPhase.Accepted, cmdId = hit.id, message = "已发送，等待执行…")
                        else -> _submit.value = _submit.value.copy(phase = SubmitPhase.Accepted, cmdId = hit.id, message = "排队中…")
                    }
                }
            }
            delay(POLL_INTERVAL_MS)
        }

        _submit.value = _submit.value.copy(
            phase = SubmitPhase.Failed,
            message = if (seen == null) {
                "已提交，但一直没等到执行记录（点右上角刷新看看）"
            } else {
                "等了 ${RESULT_TIMEOUT_MS / 1000} 秒还没结论（后端最长等 300 秒，稍后再刷新）"
            },
        )
    }

    private suspend fun fetchCommands(limit: Int, q: String = "", offset: Int = 0): List<CommandItem>? = try {
        val params = mutableMapOf("limit" to limit.toString())
        if (offset > 0) params["offset"] = offset.toString()
        if (q.isNotBlank()) params["q"] = q
        val resp = api().listCommands(params)
        if (resp.isSuccessful) resp.body()?.items else null
    } catch (_: Exception) {
        null // 轮询期间的单次失败不该中断整条等待
    }

    /**
     * 拉**第一页**指令历史（`q` 是后端**在 SQL 里做**的过滤，不是本地过滤）。
     * 会把"还有没有更早的"记下来给界面用。
     */
    fun loadCommands(limit: Int = 30, q: String = "") {
        lastQuery = q
        lastLimit = limit
        viewModelScope.launch {
            fetchCommands(limit, q, offset = 0)?.let { page ->
                _commands.value = page
                _commandsHasMore.value = page.size >= limit
            }
        }
    }

    /**
     * 加载**更早的一页**（面板的"加载更多"）。
     *
     * 之前这里是错的：写的是"再拉 100 条"，其实每次 `offset=0` —— 只是把最新的
     * 前 100 条重拉一遍，永远看不到第 101 条以前的。现在用 `offset = 已有条数` 并**追加**。
     */
    fun loadMoreCommands() {
        if (!_commandsHasMore.value) return
        val limit = lastLimit
        val q = lastQuery
        viewModelScope.launch {
            val page = fetchCommands(limit, q, offset = _commands.value.size) ?: return@launch
            _commands.value = mergeCommandPage(_commands.value, page)
            _commandsHasMore.value = page.size >= limit
        }
    }

    /** 清掉本地凭据（重新配对入口）。 */
    fun clearSession() {
        pollJob?.cancel()
        PairStore.clear(getApplication())
        _commands.value = emptyList()
        _status.value = null
        _connected.value = null
        _statusError.value = null
        _baseUrl.value = ""
        _submit.value = SubmitState()
    }

    private companion object {
        /** 轮询间隔：够快能看见状态变化，又不至于把手机和后端榨干。 */
        const val POLL_INTERVAL_MS = 1500L

        /** 一次轮询拉多少条历史（要能把刚发的那条捞进来）。 */
        const val POLL_LIMIT = 30

        /** 比后端自己的等待上限（300 秒）再宽一点，免得比后端先放弃。 */
        const val RESULT_TIMEOUT_MS = 330_000L

        /** 「说完了」之后给引擎多久出最终结果；超了就用临时结果兜底。 */
        const val FINISH_GRACE_MS = 2_500L

        /** 一次识别最长听多久（兜底，防止永远卡在"在听…"）。 */
        const val SESSION_LIMIT_MS = 15_000L
    }
}
