package com.echo.contact.ui

import android.app.Application
import androidx.lifecycle.viewModelScope
import com.echo.contact.data.ApiClient
import com.echo.contact.data.E2E
import com.echo.contact.data.PairStore
import com.echo.contact.data.dto.PairClaimRequest
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import retrofit2.HttpException
import java.io.IOException

sealed interface PairUiState {
    data object Idle : PairUiState
    data object Submitting : PairUiState
    data object Success : PairUiState
    data class Error(val message: String) : PairUiState
}

/** 配对页：输入后端地址 + 一次性配对码，换取并保存令牌。 */
class PairViewModel(app: Application) : BaseViewModel(app) {

    private val _state = MutableStateFlow<PairUiState>(PairUiState.Idle)
    val state: StateFlow<PairUiState> = _state.asStateFlow()

    /** 「测试连接」的结果，纯展示。 */
    private val _connectionMessage = MutableStateFlow<String?>(null)
    val connectionMessage: StateFlow<String?> = _connectionMessage.asStateFlow()

    private val _checking = MutableStateFlow(false)
    val checking: StateFlow<Boolean> = _checking.asStateFlow()

    /** 上次用过的后端地址（重新配对时回填）；读加密存储，所以放 IO。 */
    private val _initialBaseUrl = MutableStateFlow<String?>(null)
    val initialBaseUrl: StateFlow<String?> = _initialBaseUrl.asStateFlow()

    init {
        viewModelScope.launch {
            _initialBaseUrl.value = withContext(Dispatchers.IO) {
                PairStore.baseUrl(getApplication())
            }
        }
    }

    /** 探测后端连通性：GET /status 是否 2xx。 */
    fun verifyConnection(baseUrl: String) {
        if (_checking.value) return
        val url = baseUrl.trim().trimEnd('/')
        if (url.isBlank()) {
            _connectionMessage.value = "请先填写后端地址"
            return
        }
        _checking.value = true
        _connectionMessage.value = "正在连接…"
        viewModelScope.launch {
            try {
                // ⚠️ 打 `health`（**不要令牌**），不打 `api/status`。
                // 2026-10-15 用户实测：过中继、或电脑开着「API 鉴权」时，`api/status` 会回 401，
                // 而这按钮就显示"后端返回 HTTP 401" —— 看着像失败，其实地址是通的（只是还没配对、
                // 手里没有令牌）。连通性检查就该用那条不需要凭据的探针。
                val resp = ApiClient.create(url).health()
                _connectionMessage.value =
                    if (resp.isSuccessful) "连通正常（HTTP ${resp.code()}）"
                    else "通了，但后端返回 HTTP ${resp.code()}"
            } catch (e: IOException) {
                _connectionMessage.value = "无法连接：${e.message}"
            } catch (e: Exception) {
                _connectionMessage.value = "无法连接：${e.message}"
            }
            _checking.value = false
        }
    }

    /** 方案 B：扫 ECHO 二维码，用 ECHO 公钥加密 claim；relay 只看到密文。 */
    fun claimV2(ticketText: String, fallbackBaseUrl: String, name: String) {
        val rawTicket = ticketText.trim()
        if (rawTicket.isBlank()) {
            _state.value = PairUiState.Error("二维码内容为空")
            return
        }
        _state.value = PairUiState.Submitting
        viewModelScope.launch {
            try {
                val ticket = withContext(Dispatchers.Default) { E2E.parseTicket(rawTicket) }
                val base = (ticket.relay.ifBlank { fallbackBaseUrl }).trim().trimEnd('/')
                if (base.isBlank()) throw IllegalArgumentException("二维码里没有中继地址")
                val deviceKey = withContext(Dispatchers.Default) { E2E.newKeyPair() }
                val claim = withContext(Dispatchers.Default) {
                    E2E.buildClaim(ticket, name.trim().ifBlank { "mobile" }, deviceKey)
                }
                val resp = ApiClient.create(base).pairClaimV2(claim)
                val envelope = resp.body()
                if (!resp.isSuccessful || envelope == null) {
                    _state.value = PairUiState.Error(parseError(resp))
                    return@launch
                }
                val body = withContext(Dispatchers.Default) { E2E.decryptResponse(envelope) }
                val token = body.get("token")?.asString.orEmpty()
                if (token.isBlank()) throw IllegalStateException("后端没有返回手机 token")
                val echoPub = body.get("echoPublicKey")?.asString ?: ticket.echoPublicKey
                val devicePub = E2E.publicToStorage(deviceKey.public)
                val deviceId = body.get("deviceId")?.asString ?: E2E.deviceId(devicePub)
                val tts = body.getAsJsonObject("tts")
                // 手机只能经 relay 时，ticket 里的 relay 才是可达入口；服务端返回的
                // baseUrl 可能是局域网地址（claim 从别的路径进来时尤其如此），
                // 不能拿它覆盖 ticket.relay，否则配对成功后状态请求必然连不上。
                val storedBase = ticket.relay.ifBlank {
                    body.get("baseUrl")?.asString ?: base
                }
                PairStore.saveE2E(
                    getApplication(),
                    storedBase.trim().trimEnd('/'),
                    token,
                    body.get("name")?.asString ?: name.trim().ifBlank { "mobile" },
                    echoPublicKey = echoPub,
                    deviceId = deviceId,
                    devicePrivateKey = E2E.privateToStorage(deviceKey.private),
                    devicePublicKey = devicePub,
                    ttsUrl = tts?.get("baseUrl")?.asString.orEmpty(),
                    ttsToken = tts?.get("token")?.asString.orEmpty(),
                    ttsTlsPin = tts?.get("tlsPin")?.asString.orEmpty(),
                )
                _state.value = PairUiState.Success
            } catch (e: HttpException) {
                _state.value = PairUiState.Error(parseHttp(e))
            } catch (e: IOException) {
                _state.value = PairUiState.Error("无法连接：${e.message}")
            } catch (e: Exception) {
                _state.value = PairUiState.Error("扫码配对失败：${e.message}")
            }
        }
    }

    /** 用一次性配对码换令牌并落盘；结果通过 [state] 暴露。 */
    fun claim(baseUrl: String, code: String, name: String) {
        val url = baseUrl.trim().trimEnd('/')
        val trimmedCode = code.trim()
        if (url.isBlank() || trimmedCode.isBlank()) {
            _state.value = PairUiState.Error("后端地址与配对码都不能为空")
            return
        }
        _state.value = PairUiState.Submitting
        viewModelScope.launch {
            try {
                val resp = ApiClient.create(url).pairClaim(
                    PairClaimRequest(
                        code = trimmedCode,
                        name = name.trim().ifBlank { "mobile" },
                    )
                )
                val body = resp.body()
                if (resp.isSuccessful && body != null && body.ok == true && !body.token.isNullOrBlank()) {
                    PairStore.save(
                        getApplication(),
                        (body.baseUrl ?: url).trim().trimEnd('/'),
                        body.token,
                        body.name ?: name.trim().ifBlank { "mobile" },
                    )
                    _state.value = PairUiState.Success
                } else {
                    val msg = body?.note
                        ?: if (resp.isSuccessful) "配对失败：后端未返回令牌"
                        else parseError(resp)
                    _state.value = PairUiState.Error(msg)
                }
            } catch (e: HttpException) {
                _state.value = PairUiState.Error(parseHttp(e))
            } catch (e: IOException) {
                _state.value = PairUiState.Error("无法连接：${e.message}")
            } catch (e: Exception) {
                _state.value = PairUiState.Error("配对出错：${e.message}")
            }
        }
    }
}
