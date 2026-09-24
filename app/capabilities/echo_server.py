# -*- coding: utf-8 -*-
"""ECHO 能力后端（服务端）的客户端适配器。

它对服务端的了解**只有 `/v1/*` 那份协议**（`docs/ECHO能力后端-服务端设计.md` §6）。
不 import 服务端的任何代码，也不假设它是 Python —— 这是"前后分离"的意义所在。

## 三个容易做错的地方

**① 槽要取交集，别把服务端的槽名原样搬来。**
服务端报的是**它自己模型的主槽**（`asr.long` / `asr.text` / `diarize.turns`…）
加上 `supports`。其中 `asr.long` **不是客户端的词汇**（客户端只说 `asr.text` /
`asr.timestamps` / `asr.streaming`，见 `base.SLOTS`）。所以这里 `provides` 取的是
`capabilities.slots ∩ base.SLOTS` —— 不然路由会拿一个没人认识的槽去派活。

**② 失败原因从服务端的 `code` 翻，不从 HTTP 状态猜。**
"503" 可能是 `server_busy`（该退避重试）也可能是 `model_failed`（该换后端）——
光看状态码分不出来，而这两种的处理**完全相反**。服务端 §6.3 存在的意义就是这个。

**③ 大 body 出错时错误体可能读不到**（服务端已修，但客户端仍要兜住）。
服务端在"读 body 之前"拒掉请求时不会读我们的上传；那边已经加了抽干
（`audio.drain`），但**网络中间还有反代**。所以读不到 body 时按状态码兜底成
`offline`/`error`，而不是崩在 `json.loads` 上。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Set

from app.capabilities.base import (
    BACKEND_ECHO_SERVER,
    SERVER_CODE_TO_REASON,
    SKIP_REASONS,
    SOURCE_LAN,
    SLOTS,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    EmbedResult,
    error_from_server,
)
from app.capabilities import pairing

#: `capabilities` 缓存多久。探测是"面板与路由都要看"的东西，
#: 但**每次调用都问一次**又太吵 —— 30 秒是个折中（面板刷新默认 3 秒，够用）。
CAPS_TTL_S = 30.0

#: 请求超时。上传与推理分开：上传看网速，推理看模型（服务端默认上限 900 秒）。
UPLOAD_TIMEOUT_S = 120.0
INFER_TIMEOUT_S = 900.0


def _setting(key, default=None):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def _creds():
    """本机的配对凭据；没配/坏了返回 None（`credentials.load` 永不抛）。"""
    try:
        from app.capabilities import credentials
        return credentials.load()
    except Exception:
        return None


def _base_url_from_settings() -> str:
    """后端地址：**设置里填的优先**，没填就用配对时记下的那个。

    顺序这么定是因为两种来源的语义不同：设置里的是"运营让我连这台"，
    凭据里的是"我配对时连的是这台"。前者是显式配置，该赢。
    """
    url = str(_setting("capabilityEchoServerUrl", "") or "").strip().rstrip("/")
    if url:
        return url
    c = _creds()
    return str(getattr(c, "base_url", "") or "").strip().rstrip("/")


def _token_from_settings() -> str:
    """设置里手填的 Bearer 令牌（服务端 `auth.mode=token` 的静态令牌，
    或者临时贴一个 JWT 进去调试用）。

    ⚠️ **手动填的令牌不参与自动续期**：它不是我们换来的，也就不知道它的期限，
    静默拿凭据去换一个反而会把人的调试意图搅乱。401 时如实报错。
    """
    jwt = str(_setting("capabilityEchoServerToken", "") or "").strip()
    if jwt:
        return jwt
    return str(_setting("capabilityEchoServerStaticToken", "") or "").strip()


class EchoServerClient(CapabilityClient):
    """服务端的一个连接。构造时不联网 —— 联网发生在 `refresh()` / 第一次调用。

    令牌的来源按这个顺序定（**先到先得，不合并**）：

    1. 构造参数 `token` / 设置里手填的 `capabilityEchoServerToken`、`…StaticToken`
       —— 别人给的，客户端**不知道它的期限，所以不替它续期**；
    2. 本机凭据（`{DATA}/backend.json`，配对得来）—— 过期就自动换，401 还会再换一次。

    顺序把"手填"放在前面，是为了让排障时能拿一个令牌把凭据那一路整个绕开。
    """

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN

    def __init__(self, base_url: str = "", token: str = "", *,
                 backend_id: str = "", timeout_infer: float = INFER_TIMEOUT_S,
                 creds: Optional[Any] = None):
        self.base_url = (base_url or _base_url_from_settings()).rstrip("/")
        self._token = token or _token_from_settings()
        # `creds=None` 表示"自己去读一次"；显式传 `False` 表示"就当本机没配对"
        # （测试要能明确地说"这次不要碰凭据文件"）
        self._creds = _creds() if creds is None else (creds or None)
        if backend_id:
            self.backend_id = backend_id
        self.timeout_infer = float(timeout_infer)
        self.provides: frozenset = frozenset()
        self.vector_space_id = ""
        self.models: tuple = ()
        self.limits: Dict[str, Any] = {}
        self.server_name = ""
        self._caps_at = 0.0
        self._caps_error = ""

    # ---------------------------------------------------------------- 探测

    def refresh(self, force: bool = False) -> bool:
        """拉一次 `GET /v1/capabilities`。返回"拿到了吗"。**不抛异常。**

        拿不到不算致命：`ready()` 会返回 False，路由会把这一路标成 `offline` 并跳过。
        把探测失败做成异常，会让"面板想显示一下后端状态"变成一件要 try/except 的事。
        """
        if not self.base_url:
            self._caps_error = "没配地址"
            return False
        if not force and (time.time() - self._caps_at) < CAPS_TTL_S and self.provides:
            return True
        try:
            status, body = self._request("GET", "/v1/capabilities", timeout=15)
        except CapabilityError as e:
            self._caps_error = str(e)
            self.provides = frozenset()
            return False
        if status != 200 or not isinstance(body, dict):
            self._caps_error = "capabilities 回了 %s" % status
            self.provides = frozenset()
            return False

        self.server_name = str((body.get("server") or {}).get("id") or "")
        self.limits = dict(body.get("limits") or {})
        self.models = tuple(body.get("models") or [])

        # ① 槽取交集：服务端的 `asr.long` 之类**不是**客户端的词汇
        raw_slots: Set[str] = set((body.get("slots") or {}).keys())
        self.provides = frozenset(raw_slots & set(SLOTS))

        # 向量空间：把**所有**产出向量的模型都收进来。
        # 服务端保证它们只有一个值（它有护栏测试钉着）；万一不是，
        # 我们取那个"唯一值"，多于一个就**拒绝提供向量类槽** ——
        # 因为"不确定属于哪个空间"的向量拿去做余弦相似度会**认错人且不报错**。
        spaces = {str(m.get("vectorSpaceId") or "") for m in self.models
                  if m.get("vectorSpaceId")}
        if len(spaces) == 1:
            self.vector_space_id = spaces.pop()
        elif len(spaces) > 1:
            self.vector_space_id = ""
            self.provides = frozenset(s for s in self.provides
                                      if not s.startswith(("diarize.", "speaker.")))

        self._caps_at = time.time()
        self._caps_error = ""
        return True

    def ready(self) -> Optional[bool]:
        if not self.base_url:
            return False
        try:
            status, body = self._request("GET", "/v1/health", timeout=5)
        except CapabilityError:
            return False
        except Exception:
            return None
        return bool(status == 200 and isinstance(body, dict) and body.get("ok"))

    def describe(self) -> Dict[str, Any]:
        out = super().describe()
        out.update({"baseUrl": self.base_url, "serverName": self.server_name,
                    "limits": self.limits, "capsError": self._caps_error,
                    "paired": self._creds is not None,
                    "auth": "manual" if self._token else
                            ("paired" if self._creds is not None else "none")})
        return out

    # ---------------------------------------------------------------- 调用

    def transcribe(self, wav, *, lang="auto", want_timestamps=False,
                   variant="long", **kw) -> AsrResult:
        q = "?variant=%s%s" % ("short" if variant == "short" else "long",
                               "&timestamps=1" if want_timestamps else "")
        if lang and lang != "auto":
            q += "&lang=%s" % urllib.parse.quote(str(lang))
        body = self._post_audio("/v1/asr" + q, wav, slot="asr.text")
        return AsrResult.from_server(body, self.backend_id)

    def diarize(self, wav, *, max_speakers=None, **kw) -> DiarizeResult:
        q = "?mode=segment"
        if max_speakers:
            q += "&maxSpeakers=%d" % int(max_speakers)
        body = self._post_audio("/v1/diarize" + q, wav, slot="diarize.turns")
        return DiarizeResult.from_server(body, self.backend_id)

    def embed(self, wav, *, count=1, **kw) -> EmbedResult:
        body = self._post_audio("/v1/speaker/embed?count=%d" % max(1, int(count or 1)),
                                wav, slot="speaker.embed")
        return EmbedResult.from_server(body, self.backend_id)

    # ---------------------------------------------------------------- 凭据

    def _renewable(self) -> bool:
        """这一路是"本机配对得来的凭据"吗 —— 只有它才谈得上续期。"""
        if self._token:
            return False
        c = self._creds
        return bool(c is not None and c.client_id and c.secret)

    def _auth_header(self, *, slot: str = "") -> str:
        """这次请求带什么 `Authorization`，没有就返回空串（服务端可能没开鉴权）。"""
        if self._token:
            return "Bearer " + _header_safe(self._token, "手填的令牌", self.backend_id, slot)
        if self._renewable():
            try:
                token = pairing.ensure_token(self._creds, timeout=pairing.PAIR_TIMEOUT_S)
            except pairing.PairingError as e:
                # **不静默降级成"不带令牌"**：那会把"凭据不能用了"表现成服务端的 401，
                # 而服务端那句"缺少 Bearer 令牌"完全指不到真正的原因。
                raise _pairing_failure(e, self.backend_id, slot) from None
            return "Bearer " + _header_safe(token, "换来的令牌", self.backend_id, slot)
        return ""

    def _force_renew(self, *, slot: str = "") -> None:
        """把令牌当成已失效，强制换一个（401 之后用）。失败就抛分类好的错。"""
        try:
            pairing.ensure_token(self._creds, skew_s=float("inf"),
                                 timeout=pairing.PAIR_TIMEOUT_S)
        except pairing.PairingError as e:
            raise _pairing_failure(e, self.backend_id, slot) from None

    # ---------------------------------------------------------------- HTTP

    def _post_audio(self, path: str, wav: str, *, slot: str) -> Dict[str, Any]:
        """把音频**原样**（raw body）传上去 —— 与服务端 §5.4 的约定一致。

        不用 multipart：那会让服务端把大文件交给框架 spool 到磁盘，
        而且那个文件**由框架创建、它的清理看不到它**（服务端 §5.4 的理由）。
        我们自己读文件、自己发 octet-stream，服务端那边一路是"边收边算"。
        """
        import os
        if not os.path.isfile(wav):
            raise CapabilityError("open-failed", "文件不存在: %s" % wav,
                                  backend_id=self.backend_id, slot=slot)
        with open(wav, "rb") as fh:
            data = fh.read()
        status, body = self._request("POST", path, data=data,
                                     content_type="audio/wav",
                                     timeout=self.timeout_infer, slot=slot)
        if status != 200 or not isinstance(body, dict):
            raise error_from_server(body, status, self.backend_id, slot)
        return body

    def _ssl_context(self, *, slot: str = ""):
        """https 时的固定证书上下文；http 时返回 `None`。

        **https 而没有固定证书 → 直接抛**（`blocked`），绝不静默跳过校验：
        那等于把中间人放进来，而且从现象上完全看不出来（"能用"）。
        """
        if not pairing.is_https(self.base_url):
            return None
        try:
            return pairing.pinned_context(getattr(self._creds, "cert_pem", "") or "",
                                          what="ECHO 后端")
        except pairing.PairingError as e:
            raise CapabilityError("blocked", str(e), code="blocked",
                                  backend_id=self.backend_id, slot=slot) from None

    def _request(self, method: str, path: str, *, data: Optional[bytes] = None,
                 content_type: str = "", timeout: float = 30.0,
                 slot: str = "", retry_auth: bool = True) -> tuple:
        """发一个请求。**失败一律抛 `CapabilityError`**（带分类），不返回空。"""
        if not self.base_url:
            raise CapabilityError("absent", "没配 ECHO 后端地址",
                                  backend_id=self.backend_id, slot=slot)
        url = self.base_url + path
        req = urllib.request.Request(url, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        auth = self._auth_header(slot=slot)
        if auth:
            req.add_header("Authorization", auth)
        # 让服务端日志与客户端日志能用同一个 id 对上（服务端 §7.3）
        req.add_header("X-Request-Id", _new_request_id())
        context = self._ssl_context(slot=slot)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                raw = resp.read()
                return resp.status, _maybe_json(raw)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                # ③ 大 body 早退时**读不到错误体**（连接已被关）。状态码还在，
                #    按状态码兜底 —— 不能在这里崩掉。
                pass
            payload = _maybe_json(raw)
            if e.code == 401 and retry_auth and self._renewable():
                # 手上那个令牌被拒了，但本机凭据还在 → **换一次再试一次**。
                #
                # 什么时候真会走到这里：令牌还没到我们以为的过期时间，服务端那边
                # 已经不认了 —— 管理员 `--rotate-secret`（`token_version` +1）、
                # 或者内网两台机器的时钟差了几分钟。这两种都是"再换一个就好"。
                #
                # **只试一次**：真是 secret 失效的话，再换也换不来，
                # 无限重试只会把一次失败变成一个转不出来的循环。
                self._force_renew(slot=slot)
                return self._request(method, path, data=data, content_type=content_type,
                                     timeout=timeout, slot=slot, retry_auth=False)
            if isinstance(payload, dict) and payload.get("code"):
                raise error_from_server(payload, e.code, self.backend_id, slot) from None
            raise CapabilityError(
                "offline" if e.code in (502, 503, 504) else "error",
                "HTTP %s（读不到错误体，可能是反代/网关回的）" % e.code,
                code="", backend_id=self.backend_id, slot=slot,
                status=e.code) from None
        except CapabilityError:
            raise
        except Exception as e:
            raise CapabilityError("offline", "连不上 %s（%s）" % (self.base_url, e),
                                  backend_id=self.backend_id, slot=slot) from None


def _header_safe(token: str, what: str, backend_id: str, slot: str) -> str:
    """令牌里混进非 ASCII / 控制字符时，**在这里就说清楚**。

    这不是多余的校验：HTTP 头只能是 latin-1。让一个带中文的令牌走到 `urlopen` 里，
    抛出来的是 `'latin-1' codec can't encode characters…`，而我们的兜底会把它报成
    "连不上 <地址>" —— 一个**指错方向的诊断**（网络好好的，是令牌抄错了）。
    实测踩到过（用例里我随手写了句中文件当令牌）。
    """
    bad = [ch for ch in token if not (0x20 < ord(ch) < 0x7f)]
    if bad:
        raise CapabilityError(
            "blocked",
            "%s 里有不能放进 HTTP 头的字符（%r…）：多半是复制时带进了中文、全角空格或换行。"
            "重新复制一次" % (what, "".join(bad[:3])),
            code="bad_request", backend_id=backend_id, slot=slot)
    return token


def _pairing_failure(e, backend_id: str, slot: str) -> CapabilityError:
    """换令牌失败 → 路由能用的 `CapabilityError`。

    **这里是"reason 与 code 各管一件事"的又一个实例**（见 `base.SERVER_CODE_TO_REASON`）：
    换令牌失败有两种完全不同的成因，而它们对路由的意义相反 ——

      * `offline`：**那台机器不可达** → 这一轮该跳过它（可重试）；
      * `unauthorized`/`forbidden`：**凭据不被接受** → 跳过它，并且**别重试**，
        要去让人重新配对。把它当成 offline 会让客户端一直拿废凭据去撞。

    所以 `PairingError.code` 只要本身就是权威词汇（`absent`/`offline`）就直接用，
    否则过 `SERVER_CODE_TO_REASON` 那张表翻；两张表都不认识的一律 `error`
    —— 宁可信少一点，也不按猜出来的原因做"换后端/不重试"这种有副作用的决定。
    """
    code = str(getattr(e, "code", "") or "")
    reason = code if code in SKIP_REASONS else SERVER_CODE_TO_REASON.get(code, "error")
    hint = "—— 需要重新配对" if reason == "blocked" else "—— 这一轮先用别的后端"
    return CapabilityError(reason,
                           "后端凭据不能用：%s%s" % (e, hint),
                           code=code or "unauthorized",
                           backend_id=backend_id, slot=slot,
                           status=int(getattr(e, "status", 0) or 0),
                           retry_after=getattr(e, "retry_after", None))


def _maybe_json(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def _new_request_id() -> str:
    import uuid
    return uuid.uuid4().hex[:16]


# `urllib.parse.quote` 要用到，但只在这一个地方 —— 放模块底下省一次 import 开销
import urllib.parse  # noqa: E402


def client_from_settings() -> Optional[EchoServerClient]:
    """按设置造一个客户端；**没配地址就返回 None**（不是造一个必然失败的）。

    地址的来源有两个（设置里填的、配对时记下的），`_base_url_from_settings` 已经
    把顺序定好了 —— 所以"只配对、什么都没配"也是一种能用状态。
    """
    url = _base_url_from_settings()
    if not url:
        return None
    return EchoServerClient(url)
