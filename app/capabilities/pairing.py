# -*- coding: utf-8 -*-
"""配对与换令牌（设计 §7.4 / §7.5 ① ②）。

```
管理员  --new-pairing-code-->  一次性配对码
客户端  POST /v1/pair   {code, clientName}   -->  {clientId, secret, …}   [免凭据，防猜]
客户端  POST /v1/token  Basic(clientId:secret) --> {accessToken, expiresIn}
客户端  GET  /v1/asr    Bearer(accessToken)  -->  正常调用
```

配对码**只在生成的那一刻**出现明文；secret **只在兑换的那一刻**出现明文。
之后客户端手里只有 `client_id` + `secret`（落盘见 `credentials.py`）和短命令牌（不落盘）。

## 为什么这个文件不 import `echo_server.py`

`echo_server.py` 是"已经配对好之后怎么调能力"，这里是"还没配对/令牌过期时怎么办"。
两层都长着一点 HTTP 骨架，看起来像重复，但**它们的错误模型不一样**：
能力调用要的是路由能用的 `reason`（十词表，决定换不换后端），
配对是一次性的交互动作，要的是**一句人能看懂的话**（"配对码无效" vs "试太多次了"）。
硬合成一个抽象，只会得到一个既不是路由语义、也不适合直接展示给用户的四不像。

（`echo_server.EchoServerClient` 反过来 import 本模块 —— 它要在这里换令牌。
方向单一，不会成环。）
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from app.capabilities import credentials as cred
from app.capabilities.credentials import BackendCredentials

#: 配对与换令牌都是**人等着看结果的短动作**，超时给短一点：
#: 让它卡 900 秒（推理那个上限）只会让人以为程序死了。
PAIR_TIMEOUT_S = 15.0

#: ECHO 后端对外就挂在这个路径下（服务端 §6.1 的 `/v1/*`）。
API_PREFIX = "/v1"


class PairingError(Exception):
    """配对/换令牌失败。**不是** `CapabilityError` —— 它不进路由，也不参与降级判断。"""

    def __init__(self, message: str, *, code: str = "", status: int = 0,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = int(status or 0)
        self.retry_after = retry_after

    def __str__(self) -> str:
        if self.code:
            return "%s（%s）" % (self.message, self.code)
        if self.status:
            return "%s（HTTP %s）" % (self.message, self.status)
        return self.message


# ---------------------------------------------------------------- TLS（证书固定）

def is_https(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(normalize_base_url(url)).scheme == "https"
    except Exception:
        return False


def _host_port(url: str) -> Tuple[str, int]:
    parts = urllib.parse.urlsplit(normalize_base_url(url))
    return parts.hostname or "", int(parts.port or 443)


def fetch_cert_pem(url: str, *, timeout: float = PAIR_TIMEOUT_S) -> str:
    """取服务端证书（PEM）。**只在配对时用**。

    为什么配对时要取：设计 §7.5 ① 说配对顺带交换信任 —— 客户端把证书固定下来，
    以后连这台后端就只认它，于是**用户不需要给每台机器装自签根**。
    `ssl.get_server_certificate` 是 stdlib 自带，不引依赖。

    这是 TOFU（首次使用即信任）：`fp=` 里带了期望指纹时才真的能防中间人
    （见 `pair`），否则第一次连接本身就是信任建立的那一刻。
    """
    host, port = _host_port(url)
    if not host:
        raise PairingError("地址里没有主机名", code="bad_request")
    try:
        return ssl.get_server_certificate((host, port), timeout=timeout)
    except Exception as e:
        raise PairingError("连不上 %s:%s 取证书（%s）" % (host, port, e),
                           code="offline") from None


def fingerprint_of(cert_pem: str) -> str:
    """证书指纹：`sha256:<hex>`（与 `echo://pair?...&fp=` 里那个格式一致）。"""
    try:
        der = ssl.PEM_cert_to_DER_cert(str(cert_pem or ""))
    except Exception:
        return ""
    if not der:
        return ""
    return "sha256:" + hashlib.sha256(der).hexdigest()


def normalize_fingerprint(value: str) -> str:
    """把用户抄来的指纹规整成可比的形式（`SHA256:AB:CD…` / 带冒号 / 大小写都收）。"""
    text = str(value or "").strip().replace(":", "").replace(" ", "").lower()
    if text.startswith("sha256"):
        text = text[len("sha256"):]
    return ("sha256:" + text) if text else ""


def pinned_context(cert_pem: str, *, what: str = "后端"):
    """按**固定的证书**建一个 SSL 上下文。**没有证书就抛** —— 绝不静默跳过校验。

    为什么 `check_hostname=False`：内网后端常按 IP 或短名访问，证书上不会签它；
    我们靠"证书本身必须就是配对时那一个"来认证，这比校名更强也更实用。
    """
    if not str(cert_pem or "").strip():
        raise PairingError(
            "%s 配的是 https，但本机没有它的证书（没配对过，或凭据里缺 certPem）—— "
            "**拒绝连接**。不静默跳过证书校验：那等于把中间人放进来。" % what,
            code="blocked")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        ctx.load_verify_locations(cadata=str(cert_pem))
    except Exception as e:
        raise PairingError("固定下来的证书解不开（%s）—— 重新配对一次" % e,
                           code="blocked") from None
    return ctx


# ---------------------------------------------------------------- 地址

def normalize_base_url(url: str) -> str:
    """把用户填的地址规整成 `scheme://host[:port]`（去掉尾斜杠）。

    顺手接受"只填了个 IP:端口"这种最常见的手滑 —— 面板上那个输入框，
    用户十有八九填 `10.100.0.24:8900`。报错比容错更让人烦，而这里的容错**不会猜错**：
    没有 scheme 就补 `http://`，服务端跑 https 的人本来就会把 `https://` 写全。
    """
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PairingError("地址要写成 http(s)://主机:端口，收到的是 %r" % url)
    return "%s://%s" % (parts.scheme, parts.netloc)


# ---------------------------------------------------------------- HTTP

def _post_json(url: str, payload: Optional[dict], *, headers: Optional[dict] = None,
               timeout: float = PAIR_TIMEOUT_S,
               context: Optional[ssl.SSLContext] = None) -> Dict[str, Any]:
    """POST 一个 JSON（或空体），回 JSON 字典。**失败一律抛 `PairingError`。**

    `context` 是固定的证书上下文（https 时必须给，见 `pinned_context`）。
    """
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read()
            return _json_or_empty(raw)
    except urllib.error.HTTPError as e:
        body = _read_error_body(e)
        raise _from_error(e.code, body) from None
    except urllib.error.URLError as e:
        # 证书对不上就是这里（`SSLCertVerificationError` 包在 URLError.reason 里）。
        # 单独说一句：否则现象是"连不上"，而人以为网络坏了。
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise PairingError("证书校验失败（%s）—— 服务端换证书了？重新配对一次"
                               % getattr(e, "reason", ""), code="blocked") from None
        raise PairingError("连不上 %s（%s）" % (_host_of(url), _reason_of(e)),
                           code="offline") from None
    except ssl.SSLError as e:
        raise PairingError("证书校验失败（%s）—— 服务端换证书了？重新配对一次" % e,
                           code="blocked") from None
    except PairingError:
        raise
    except Exception as e:                                  # pragma: no cover - 兜底
        raise PairingError("请求 %s 失败：%s" % (url, e), code="offline") from None


def _json_or_empty(raw: bytes) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        out = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}


def _read_error_body(e: urllib.error.HTTPError) -> Dict[str, Any]:
    try:
        return _json_or_empty(e.read())
    except Exception:
        # 早退（大 body / 连接被关）时读不到错误体，状态码还在。
        # 服务端那边已经加了抽干，但中间还有反代 —— 兜住，别在这里崩。
        return {}


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc or url
    except Exception:                                       # pragma: no cover
        return url


def _reason_of(e: urllib.error.URLError) -> str:
    return str(getattr(e, "reason", "") or e)


def _from_error(status: int, body: Dict[str, Any]) -> PairingError:
    """把服务端的错误翻成**一句话**。文案按"用户下一步该干什么"分，不按状态码分。"""
    code = str(body.get("code") or "")
    detail = str(body.get("detail") or body.get("message") or "").strip()
    ra = body.get("retryAfter")
    try:
        ra = int(ra) if ra is not None else None
    except (TypeError, ValueError):
        ra = None
    if status == 401:
        return PairingError(detail or "配对码无效、已被使用或已过期",
                            code=code or "unauthorized", status=status)
    if status == 403:
        return PairingError(detail or "服务端拒绝了这次请求（配对可能已被关闭）",
                            code=code or "forbidden", status=status)
    if status == 429:
        return PairingError(detail or "试得太频繁了，等一会儿再试",
                            code=code or "rate_limited", status=status, retry_after=ra)
    if status in (502, 503, 504):
        return PairingError(detail or "后端不可用（网关回 %s）" % status,
                            code=code or "offline", status=status, retry_after=ra)
    return PairingError(detail or "请求失败（HTTP %s）" % status,
                        code=code or "error", status=status, retry_after=ra)


def _basic(client_id: str, secret: str) -> str:
    """`Basic base64(clientId:secret)` —— 与服务端 `parse_basic` 逐字对应。

    `validate=True` 那边要求严格 base64，所以这里**不能**漏 padding。
    """
    raw = ("%s:%s" % (client_id, secret)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------- 配对

def pair(base_url: str, code: str, client_name: str = "", *,
         timeout: float = PAIR_TIMEOUT_S, cert_fingerprint: str = "",
         save: bool = True) -> BackendCredentials:
    """拿配对码换凭据，成功就落盘。返回 `BackendCredentials`。

    **成功之前不写盘** —— 半路失败留一个坏文件，下次启动会表现成"配过了但连不上"。

    https 的地址会**顺带把服务端证书固定下来**（设计 §7.5 ①）：之后连这台后端只认它，
    于是**用户不需要给每台机器装自签根**。

    `cert_fingerprint` 是**期望的指纹**（配对串里的 `fp=sha256:…`）。给了就一定要对上 ——
    这才是真正防中间人的那一步；不给则是 TOFU（首次连接即信任），如实写在文档里。
    """
    url = normalize_base_url(base_url)
    if not url:
        raise PairingError("还没填后端地址", code="bad_request")
    text = "".join(str(code or "").split()).upper()
    if not text:
        raise PairingError("还没填配对码", code="bad_request")

    cert_pem = ""
    context = None
    fp = ""
    if is_https(url):
        cert_pem = fetch_cert_pem(url, timeout=timeout)
        fp = fingerprint_of(cert_pem)
        want = normalize_fingerprint(cert_fingerprint)
        if want and want != fp:
            # **对不上就停**：这正是"配对串里带指纹"的全部意义。
            raise PairingError(
                "服务端证书指纹与配对串里的不一致（期望 %s，实际 %s）—— "
                "**可能是中间人**，也可能是服务端换了证书。先找管理员核对，别继续。"
                % (want, fp), code="blocked")
        # 这条连接也必须走同一张证书（TOFU 之后不许换）
        context = pinned_context(cert_pem)

    body = _post_json(url + API_PREFIX + "/pair",
                      {"code": text, "clientName": str(client_name or "")},
                      timeout=timeout, context=context)
    client_id = str(body.get("clientId") or "")
    secret = str(body.get("secret") or "")
    if not client_id or not secret:
        raise PairingError("服务端没给出 clientId/secret —— 协议对不上（是不是把地址填成了别的东西？）",
                           code="bad_response")

    creds = BackendCredentials(
        base_url=url,
        client_id=client_id,
        secret=secret,
        server_name=str(body.get("serverName") or ""),
        cert_fingerprint=fp or normalize_fingerprint(cert_fingerprint),
        cert_pem=cert_pem,
        paired_at=time.time(),
    )
    if save:
        cred.save(creds)
    return creds


def unpair() -> bool:
    """解除配对（忘掉本机凭据）。服务端那一行**不删** —— 那是管理员的账。"""
    return cred.clear()


# ---------------------------------------------------------------- 换令牌

def fetch_token(creds: BackendCredentials, *, timeout: float = PAIR_TIMEOUT_S
                ) -> Tuple[str, int]:
    """用 `client_id:secret` 换短期 JWT。返回 `(token, expires_in 秒)`。

    **不做 refresh token**（跟服务端 §7.5 ② 的理由一致）：secret 本来就在本机，
    再存一个长期刷新令牌只是把同一个东西存两份、多一个泄漏面。
    """
    if not (creds and creds.client_id and creds.secret):
        raise PairingError("本机没有可用的后端凭据（没配对或凭据已损坏）", code="absent")
    url = normalize_base_url(creds.base_url)
    if not url:
        raise PairingError("凭据里没有后端地址", code="bad_request")
    # https 时必须用**配对时固定下来的那张证书**。漏了这一步的后果：自签证书直连会
    # `CERTIFICATE_VERIFY_FAILED`（响倒是响得对，但换令牌这条路整个不通），
    # 而如果谁图省事把校验关掉，就变成了"能用但中间人随便进"。
    # 这条是被 `test_a_capability_call_succeeds_with_the_pinned_certificate` 抓出来的 ——
    # 只给 `pair()` 传了 context，忘了这里。
    context = pinned_context(creds.cert_pem) if is_https(url) else None
    body = _post_json(url + API_PREFIX + "/token", None,
                      headers={"Authorization": _basic(creds.client_id, creds.secret)},
                      timeout=timeout, context=context)
    token = str(body.get("accessToken") or "")
    if not token:
        raise PairingError("服务端没给 accessToken", code="bad_response")
    try:
        expires_in = int(body.get("expiresIn") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if body.get("secretRotated"):
        # 服务端在宽限期内认了我们手上这把旧 secret，并顺带告诉我们"该换了"。
        # **它发不出新 secret**（只存哈希，设计 §7.5 ⑤），所以这里能做的只有"说出来" ——
        # 让人重新配对一次。不说的话，宽限期一过就是一片 401，而那时已经没人知道原因。
        try:
            from app import db
            db.add_log("warn", "capability",
                       "后端凭据已被管理员轮换：请在宽限期结束前重新配对"
                       "（服务端只存哈希，没法把新 secret 发给我们）")
        except Exception:
            pass
    return token, expires_in


def ensure_token(creds: BackendCredentials, *, skew_s: float = 60.0,
                 timeout: float = PAIR_TIMEOUT_S) -> str:
    """手上的令牌还新鲜就直接用，否则换一个。**换到的令牌写回内存对象，不落盘。**"""
    if creds.token_fresh(skew_s=skew_s):
        return creds.access_token
    token, expires_in = fetch_token(creds, timeout=timeout)
    creds.access_token = token
    # expiresIn 为 0（服务端没给/给了 0）时**别把令牌当成永不过期**：
    # 按 0 处理的话 `token_fresh` 立刻为假，于是每次调用都换一次 —— 能跑，只是多一次往返。
    # 反过来若当成"永不过期"，就会一直在 401 上撞。宁可多换。
    creds.token_expires_at = time.time() + max(0, expires_in)
    return token


# ---------------------------------------------------------------- 面板用

def state() -> Dict[str, Any]:
    """面板/CLI 要看的配对状态。**绝不含 secret**（只给遮罩后的身份信息）。"""
    c = cred.load()
    if c is None:
        return {"paired": False, "baseUrl": "", "clientId": "", "serverName": "",
                "pairedAt": 0.0, "tokenFresh": False}
    return {
        "paired": True,
        "baseUrl": c.base_url,
        "clientId": c.client_id,
        "serverName": c.server_name,
        "pairedAt": c.paired_at,
        "tokenFresh": c.token_fresh(),
    }
