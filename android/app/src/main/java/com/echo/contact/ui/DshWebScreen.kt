package com.echo.contact.ui

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.echo.contact.data.DshWebRequestPolicy
import com.echo.contact.data.PairStore
import com.echo.contact.ui.theme.EchoPalette
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/** 日志标签（只打 origin 与结论，见 [DshWebRequestPolicy.logLine]）。 */
private const val TAG = "DshWeb"

/**
 * 重建请求时要原样带过去的头。
 *
 * 为什么是这几个：`Accept` 决定服务端回 HTML 还是 JSON（DSH 前端是 SPA，很多路径
 * 同一个地址按 Accept 分叉）；`User-Agent` 决定它给不给移动端那套布局；
 * `Accept-Language` 决定界面语言；`Referer` 是它自己的同站校验要看的；
 * `Range` 是音视频/大文件拖动要用的（丢了就"进度条能拖但内容不动"）。
 */
private val FORWARDED_REQUEST_HEADERS =
    listOf("Accept", "Accept-Language", "User-Agent", "Referer", "Range")

/**
 * **App 内**打开 DSH 标准版网页的整屏页面（用户 2026-10-18 夜里定下的方案 a）。
 *
 * ## 为什么必须是"App 内 WebView"，而不是把系统浏览器叫起来
 *
 * 中继对 `api/` 下的每条接口都要求 `Authorization` 头，而**系统浏览器不会带手机令牌** ——
 * 原来那个 `Intent.ACTION_VIEW` 的入口点开就是一段 401 JSON（页面本身拿不到，用户
 * 只看到一串错误）。服务端不改（鉴权照旧），所以由 App 在 `shouldInterceptRequest`
 * 里给**同源**请求补头 —— 规则在 [DshWebRequestPolicy]（纯函数、可被 JVM 门禁钉住）。
 *
 * ## 这一层只做"把规则接到 WebView 上"
 *
 *  * 地址**只用服务端给的那串**（参数 [url]，来自 `/api/status` 的 `dshWebUrl`）：
 *    这里不拼、不改、不追加参数 —— 中继的 hostId 与只读会话每次都可能变；
 *  * 令牌来源是**现有的** `PairStore`（与 `ApiClient.attachTokenProvider` 同一份），
 *    不新造一套凭据；读它放到 IO 线程（加密 prefs 的读取不该卡住首帧）；
 *  * **只对同源补**、令牌**只放请求头**、日志只有掩码/ origin —— 三条边界都在策略层。
 *
 * ## 已知边界（如实写在代码里，别当它不存在）
 *
 *  ① `shouldInterceptRequest` **不交出请求体** —— 非同 GET/HEAD 的同源请求转发不了，
 *     只能放行（不带头），界面上会亮一条提示。中继这里本来给的就是**只读会话**，
 *     所以"页面里发不出去写操作"与设计一致；
 *  ② WebSocket 不经过 `shouldInterceptRequest` —— 如果 DSH 前端靠 WS 推更新，
 *     那条路补不了头（本轮没有真机证据，见报告"没做到/存疑"）；
 *  ③ 外部域的链接**不在这个 WebView 里加载**（否则它会与我们的拦截器共享同一个环境），
 *     交给系统浏览器；外部域请求也**不补**令牌。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun DshWebScreen(url: String, onClose: () -> Unit) {
    val context = LocalContext.current
    // WebViewClient 的回调在**非 UI 线程**上跑：所有 Compose 状态的写入都 post 回主线程。
    val main = remember { Handler(Looper.getMainLooper()) }

    var token by remember(url) { mutableStateOf<String?>(null) }
    var tokenReady by remember(url) { mutableStateOf(false) }
    var failure by remember(url) { mutableStateOf<DshWebFailure?>(null) }
    var notice by remember(url) { mutableStateOf("") }
    var loading by remember(url) { mutableStateOf(true) }
    // 重试 = 换一个 key 重建 WebView（比 reload 更干净：旧页的错误状态一并丢掉）
    var reloadKey by remember(url) { mutableIntStateOf(0) }

    // 拦截器里每次请求现取（令牌可能在这页开着的时候被重新配对换掉）
    val latestToken = rememberUpdatedState(token)

    LaunchedEffect(url) {
        token = withContext(Dispatchers.IO) { PairStore.token(context) }
        tokenReady = true
    }

    // 手机返回键 = 关闭这一页（与顶部那个关闭按钮同一条语义）
    BackHandler(onBack = onClose)

    Scaffold(
        containerColor = EchoPalette.Bg,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            DshWebEntrySpec.TITLE,
                            style = MaterialTheme.typography.titleSmall,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        // 只显示 origin：地址的 path 里带着中继那条只读会话令牌
                        Text(
                            dshWebOriginLabel(url),
                            style = MaterialTheme.typography.labelSmall,
                            color = EchoPalette.Muted,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onClose) {
                        Icon(Icons.Default.Close, contentDescription = "关闭网页")
                    }
                },
                actions = {
                    IconButton(
                        onClick = {
                            failure = null
                            notice = ""
                            loading = true
                            reloadKey += 1
                        },
                    ) {
                        Icon(Icons.Default.Refresh, contentDescription = "重新加载")
                    }
                },
            )
        },
    ) { padding ->
        Box(modifier = Modifier.fillMaxSize().padding(padding)) {
            if (!tokenReady) {
                // 读令牌是一瞬间的事（本地加密 prefs），先给个转圈，别先画一个空白 WebView
                CircularProgressIndicator(modifier = Modifier.align(Alignment.Center))
            } else {
                key(reloadKey) {
                    AndroidView(
                        modifier = Modifier.fillMaxSize(),
                        factory = { ctx ->
                            WebView(ctx).apply {
                                configureForDshWeb()
                                webViewClient = DshWebViewClient(
                                    pageUrl = url,
                                    tokenProvider = { latestToken.value },
                                    onLoading = { v -> main.post { loading = v } },
                                    onFailure = { f ->
                                        main.post {
                                            failure = f
                                            loading = false
                                        }
                                    },
                                    onNotice = { n ->
                                        // 只换那条提示：**别**顺手把 loading 关掉 ——
                                        // 子资源被拒可能发生在页面还没加载完的时候
                                        main.post { notice = n }
                                    },
                                    onExternal = { ext ->
                                        main.post { openExternally(ctx, ext) }
                                    },
                                )
                                // 就这一句加载：地址原样用服务端那串（不拼路径、不加参数）
                                loadUrl(url)
                            }
                        },
                        onRelease = { it.destroy() },
                    )
                }
            }

            // 加载中：顶部一条进度（页面画出来就消失）
            if (loading && failure == null) {
                LinearProgressIndicator(
                    modifier = Modifier.fillMaxWidth().align(Alignment.TopCenter),
                )
            }

            // 没令牌就别装作能打开：这条提示本身就是"失败原因"的一部分（点开多半是 401）
            if (tokenReady && token.isNullOrBlank() && failure == null) {
                DshWebNotice(
                    text = "手机上还没有配对令牌：这一页会以未鉴权身份请求，中继很可能回 401。" +
                        "先去「设置 → 连接」配对。",
                    modifier = Modifier.align(Alignment.BottomCenter),
                )
            } else if (notice.isNotBlank() && failure == null) {
                DshWebNotice(text = notice, modifier = Modifier.align(Alignment.BottomCenter))
            }

            // 失败：整屏一张卡，标题 + 原因 + 下一步 + 重试/关闭（绝不白屏没话说）
            failure?.let { f ->
                DshWebFailureCard(
                    failure = f,
                    onRetry = {
                        failure = null
                        notice = ""
                        loading = true
                        reloadKey += 1
                    },
                    onClose = onClose,
                )
            }
        }
    }
}

/**
 * 这一页 WebView 的设置。
 *
 * 为什么逐条这么定：
 *  * `javaScriptEnabled` / `domStorageEnabled`：DSH 标准版前端是纯 JS 应用，
 *    DOM storage 放它的偏好与前端状态 —— 这两个不开，页面就是一张空白或"加载中"；
 *  * `allowFileAccess` / `allowContentAccess` 关掉：这一页只需要网络资源，
 *    多开一个面就多一条"网页读到本机文件"的路；
 *  * 不许 JS 自己开窗口：弹窗会绕过我们的同源判断与关闭入口。
 */
@SuppressLint("SetJavaScriptEnabled")
private fun WebView.configureForDshWeb() {
    settings.javaScriptEnabled = true
    settings.domStorageEnabled = true
    settings.databaseEnabled = true
    settings.allowFileAccess = false
    settings.allowContentAccess = false
    settings.javaScriptCanOpenWindowsAutomatically = false
    settings.setSupportMultipleWindows(false)
    isHorizontalScrollBarEnabled = false
}

/**
 * 网页的 `WebViewClient`：**补头**（同源才补）、拦外链、把失败翻译成人话。
 *
 * 这里刻意不做任何"业务判断"：补不补令牌全交给 [DshWebRequestPolicy.decide]，
 * 失败文案全交给 [dshWebLoadFailure] / [dshWebHttpFailure] —— 那一层是纯函数、有门禁。
 */
internal class DshWebViewClient(
    /** 页面自身那串（服务端给的 `dshWebUrl`）—— 同源比较的唯一基准。 */
    private val pageUrl: String,
    /** 每次请求现取令牌（重新配对后立刻生效，不会把旧令牌钉死在这页里）。 */
    private val tokenProvider: () -> String?,
    /** 加载中/完成。 */
    private val onLoading: (Boolean) -> Unit,
    /** 失败（已翻译成人话）。 */
    private val onFailure: (DshWebFailure) -> Unit,
    /** 需要如实告诉用户、但不至于整屏失败的事（例如"这条非 GET 没补上头"）。 */
    private val onNotice: (String) -> Unit,
    /** 外部域链接：交给系统浏览器（那边**没有**我们的拦截器，也就不会带令牌）。 */
    private val onExternal: (String) -> Unit,
) : WebViewClient() {

    /**
     * 补头用的 HTTP 客户端（独立的超时预算）。
     *
     * 为什么不复用 `ApiClient` 那个 OkHttp：它是 Retrofit 私有的、超时按 ECHO 接口调过
     * （60~960 秒），拿来取网页静态资源不合适。**令牌来源**仍然是同一个 `PairStore`，
     * 这里没有第二套凭据。
     */
    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    override fun shouldInterceptRequest(
        view: WebView,
        request: WebResourceRequest,
    ): WebResourceResponse? {
        val requestUrl = request.url?.toString().orEmpty()
        val decision = DshWebRequestPolicy.decide(requestUrl, pageUrl, tokenProvider())
        // 只打 origin + 结论：令牌与地址的 path（只读会话令牌在 path 里）都不进日志
        Log.d(TAG, DshWebRequestPolicy.logLine(requestUrl, decision))
        if (!decision.attach) return null
        val bearer = decision.bearer ?: return null

        val method = request.method.orEmpty()
        if (!method.equals("GET", ignoreCase = true) && !method.equals("HEAD", ignoreCase = true)) {
            // WebView 不把请求体交出来 → 转发不了；放行（这条**没有**补鉴权头），并如实提示
            onNotice(
                "页面里有一条非 GET 请求没能补鉴权头（WebView 不交出请求体）。" +
                    "这条会话是只读的，写操作本来也不该从这里走。",
            )
            return null
        }

        return runCatching { fetchWithAuth(requestUrl, bearer, request) }
            .onFailure { Log.w(TAG, "补头请求失败（${it.javaClass.simpleName}）") }
            .getOrNull()
    }

    /**
     * 用带 `Authorization` 的请求取一次，再把结果作为当前请求的响应交给 WebView。
     *
     * 这是"给 WebView 的请求加头"的标准做法：`shouldInterceptRequest` 只能返回整份响应，
     * 没有"只加一个头、继续走 WebView 自己的网络栈"的接口。
     *
     * ## 为什么要顺手把 UA/Accept/Referer/Range 与 Cookie 带过去
     *
     * 我们把整条请求**重建**了一遍，WebView 原本会带的东西默认全丢了。DSH 标准版前端是
     * 一个 SPA：`Accept` 决定它拿到 HTML 还是 JSON、`Range` 决定音视频能不能拖动、
     * Cookie 决定它自己的会话能不能续上。丢了这些，症状会是"页面能开、功能半残" ——
     * 那种坏法最难和"服务端坏了"区分开。所以能带的全带上；只有 `Accept-Encoding`
     * 不转发（OkHttp 自己管 gzip，转发会让两边对不上）。
     */
    private fun fetchWithAuth(
        url: String,
        bearer: String,
        request: WebResourceRequest,
    ): WebResourceResponse {
        val mainFrame = request.isForMainFrame
        val builder = Request.Builder()
            .url(url)
            .header(DshWebRequestPolicy.HEADER, bearer)
            .get()
        // 原样转发的几个头（大小写不敏感地取：WebView 给的 key 大小写不完全固定）
        for (name in FORWARDED_REQUEST_HEADERS) {
            val value = request.requestHeaders.entries
                .firstOrNull { it.key.equals(name, ignoreCase = true) }
                ?.value
            if (!value.isNullOrBlank()) builder.header(name, value)
        }
        // Cookie 不在 requestHeaders 里稳定出现，统一从 CookieManager 取
        runCatching { CookieManager.getInstance().getCookie(url) }
            .getOrNull()
            ?.takeIf { it.isNotBlank() }
            ?.let { builder.header("Cookie", it) }

        val response = http.newCall(builder.build()).execute()

        // 服务端给的 Set-Cookie 交回 CookieManager（否则 SPA 每一条请求都像第一次来）
        for (cookie in response.headers.values("Set-Cookie")) {
            runCatching { CookieManager.getInstance().setCookie(url, cookie) }
        }

        val contentType = response.header("Content-Type").orEmpty()
        val mime = contentType.substringBefore(';').trim().ifBlank { "text/html" }
        // 主框架自己拿到 4xx/5xx 时说清楚（不完全依赖 onReceivedHttpError：
        // 从拦截器返回的响应不一定触发那个回调，白屏就糟了）
        if (mainFrame && response.code >= 400) {
            dshWebHttpFailure(response.code, url)?.let(onFailure)
        } else if (!mainFrame && (response.code == 401 || response.code == 403)) {
            // 子资源（页面的 API 调用/静态资源）被拒：整屏不报错，但要**如实说一句** ——
            // 否则用户看到的只是"页面开了、功能全空"，会以为是服务端坏了。
            onNotice(
                "页面里有请求被 ${response.code} 拒绝：手机上的令牌或这条只读会话可能已失效，" +
                    "回首页刷新状态（必要时重新配对）再重试。",
            )
        }
        return WebResourceResponse(
            mime,
            dshWebCharsetOf(contentType),
            response.code,
            response.message.ifBlank { "OK" },
            DshWebRequestPolicy.responseHeaders(response.headers.toMap()),
            response.body?.byteStream() ?: ByteArray(0).inputStream(),
        )
    }

    /**
     * 导航：**只有同源**才允许在这个 WebView 里走。
     *
     * 为什么外部域一律不在这里加载：这个 WebView 挂着"会给同源请求补令牌"的拦截器，
     * 让一个第三方页面住进来，它就能用同源地址发请求（借我们的令牌）——
     * 交系统浏览器打开，那边没有拦截器，也就没有这个面。
     */
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        val target = request.url?.toString().orEmpty()
        if (target.isEmpty()) return false
        if (DshWebRequestPolicy.isSameOrigin(target, pageUrl)) return false
        onExternal(target)
        return true
    }

    override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
        onLoading(true)
    }

    override fun onPageFinished(view: WebView?, url: String?) {
        onLoading(false)
    }

    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        // 子资源失败不算"打不开"（一张图挂了不该整屏报错）
        if (!request.isForMainFrame) return
        onFailure(
            dshWebLoadFailure(
                errorCode = error.errorCode,
                description = error.description?.toString(),
                failingUrl = request.url?.toString(),
            ),
        )
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        response: WebResourceResponse,
    ) {
        if (!request.isForMainFrame) return
        dshWebHttpFailure(response.statusCode, request.url?.toString())?.let(onFailure)
    }
}

/** 外链 → 系统浏览器（只放 http(s)；这个动作**不带**任何令牌）。 */
private fun openExternally(context: Context, url: String) {
    val lower = url.lowercase()
    if (!lower.startsWith("http://") && !lower.startsWith("https://")) return
    runCatching {
        context.startActivity(
            Intent(Intent.ACTION_VIEW, Uri.parse(url))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }
}

/** 底部那条如实提示（黄字，不影响页面本身的操作）。 */
@Composable
private fun DshWebNotice(text: String, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier.fillMaxWidth().padding(8.dp),
        color = EchoPalette.Card2,
        shape = RoundedCornerShape(8.dp),
        border = BorderStroke(1.dp, EchoPalette.Border),
    ) {
        Text(
            text,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            style = MaterialTheme.typography.labelSmall,
            color = EchoPalette.Yellow,
        )
    }
}

/** 失败卡：标题 + 原因 + 下一步 + 两个按钮（整屏，盖住那块空白 WebView）。 */
@Composable
private fun DshWebFailureCard(
    failure: DshWebFailure,
    onRetry: () -> Unit,
    onClose: () -> Unit,
) {
    Surface(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        color = EchoPalette.Card,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, EchoPalette.Border),
    ) {
        Column(
            modifier = Modifier.fillMaxSize().padding(18.dp),
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                failure.title,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Medium,
                color = EchoPalette.Red,
            )
            Spacer(Modifier.height(10.dp))
            Text(
                failure.detail,
                style = MaterialTheme.typography.bodyMedium,
                color = EchoPalette.Muted,
            )
            Spacer(Modifier.height(18.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(onClick = onRetry) { Text("重试") }
                OutlinedButton(onClick = onClose) { Text("关闭") }
            }
        }
    }
}
