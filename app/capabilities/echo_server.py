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
    SOURCE_LAN,
    SLOTS,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    EmbedResult,
    error_from_server,
)

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


def _base_url_from_settings() -> str:
    return str(_setting("capabilityEchoServerUrl", "") or "").strip().rstrip("/")


def _token_from_settings() -> str:
    """当前要带的 Bearer 令牌。

    **优先用配对拿到的短期 JWT**（`capabilityEchoServerToken`），
    退回静态令牌（`capabilityEchoServerStaticToken`，服务端 `auth.mode=token` 时用）。

    ⚠️ **这里刻意不做"过期自动换令牌"**：换令牌要 `client_id:secret`，
    而那属于凭据管理（本机落盘、DPAPI/0600），是单独一块工作。
    现在拿到 401 就如实报 `blocked` 并提示"凭据过期/未配对"，
    **不假装续期成功** —— 静默续期失败会表现成"所有请求都失败"，更难查。
    """
    jwt = str(_setting("capabilityEchoServerToken", "") or "").strip()
    if jwt:
        return jwt
    return str(_setting("capabilityEchoServerStaticToken", "") or "").strip()


class EchoServerClient(CapabilityClient):
    """服务端的一个连接。构造时不联网 —— 联网发生在 `refresh()` / 第一次调用。"""

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN

    def __init__(self, base_url: str = "", token: str = "", *,
                 backend_id: str = "", timeout_infer: float = INFER_TIMEOUT_S):
        self.base_url = (base_url or _base_url_from_settings()).rstrip("/")
        self._token = token or _token_from_settings()
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
                    "limits": self.limits, "capsError": self._caps_error})
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

    def _request(self, method: str, path: str, *, data: Optional[bytes] = None,
                 content_type: str = "", timeout: float = 30.0,
                 slot: str = "") -> tuple:
        """发一个请求。**失败一律抛 `CapabilityError`**（带分类），不返回空。"""
        if not self.base_url:
            raise CapabilityError("absent", "没配 ECHO 后端地址",
                                  backend_id=self.backend_id, slot=slot)
        url = self.base_url + path
        req = urllib.request.Request(url, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        # 让服务端日志与客户端日志能用同一个 id 对上（服务端 §7.3）
        req.add_header("X-Request-Id", _new_request_id())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            if isinstance(payload, dict) and payload.get("code"):
                raise error_from_server(payload, e.code, self.backend_id, slot) from None
            raise CapabilityError(
                "offline" if e.code in (502, 503, 504) else "error",
                "HTTP %s（读不到错误体，可能是反代/网关回的）" % e.code,
                code="", backend_id=self.backend_id, slot=slot,
                status=e.code) from None
        except Exception as e:
            raise CapabilityError("offline", "连不上 %s（%s）" % (self.base_url, e),
                                  backend_id=self.backend_id, slot=slot) from None


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
    """按设置造一个客户端；**没配地址就返回 None**（不是造一个必然失败的）。"""
    url = _base_url_from_settings()
    if not url:
        return None
    return EchoServerClient(url)
