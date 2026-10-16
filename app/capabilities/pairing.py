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
import ipaddress
import json
import os
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


def pin_certificate(url: str, *, timeout: float = PAIR_TIMEOUT_S,
                    cert_fingerprint: str = "") -> Tuple[str, str]:
    """取回该地址的证书并固定：返回 `(cert_pem, 指纹)`；期望指纹对不上就抛。

    **"取证书 + 校期望指纹"只有这一份实现**：`pair()`（配对时顺带固定）与
    `repin_for_address()`（只改地址时补固定）都调它。原来这段写死在 `pair()` 里，
    于是"改地址"那条路要同一段时**没有第二种写法是对的** —— 抄一份出去，两份迟早漂开
    （一边收 `fp=`、一边不收，就是"能配对、改完地址却连不上"这种哑谜）。

    给了 `cert_fingerprint`（配对串里的 `fp=sha256:…`）就必须对上 —— 那才是真正
    防中间人的那一步；不给则是 TOFU（首次连接即信任），如实写在文档里。
    """
    cert_pem = fetch_cert_pem(url, timeout=timeout)
    fp = fingerprint_of(cert_pem)
    want = normalize_fingerprint(cert_fingerprint)
    if want and want != fp:
        # **对不上就停**：这正是"配对串里带指纹"的全部意义。
        raise PairingError(
            "服务端证书指纹与配对串里的不一致（期望 %s，实际 %s）—— "
            "**可能是中间人**，也可能是服务端换了证书。先找管理员核对，别继续。"
            % (want, fp), code="blocked")
    return cert_pem, fp


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

#: 「不带 scheme 时按**明文 http** 处理」这条老路只留给这些主机名（见 `looks_local_host`）。
_LOCAL_HOSTNAMES = ("localhost", "ip6-localhost", "ip6-loopback")

#: 同上的域名后缀：mDNS / 内网保留后缀。它们**必然在局域网里**，公网 CA 也不会签它们。
_LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".lan", ".internal", ".home.arpa")


def looks_local_host(host: str) -> bool:
    """这个主机名/IP 像不像"本机或局域网"？**纯判据：不联网、不查 DNS、不猜。**

    2026-10-16（缺口 2）为什么需要它：`echo://pair?host=…` 里那个地址**从来都带 scheme**
    （服务端 `ops.pairing_string` 拼的），但"手填一个域名:端口"那条路以前一律补 `http://`
    —— 公网入口已经是 https 时，粘/填一个不带 scheme 的地址就会连到**明文口**
    （真机上被 Caddy 回 400），而且看不出是这个原因。修法是：**只有确实像局域网/本机的地址**
    才继续按 http 容错，其余的不再猜（见 `normalize_base_url`）。

    判据（任何一条成立即算"局域网/本机"）：
      * 回环名（`localhost` 等）、`.local` / `.lan` 这类内网后缀；
      * **单标签主机名**（`gpu-01`）—— 内网短名，公网 DNS 里不存在；
      * IP 字面量落在回环 / 私有 / 链路本地 / 未指定网段（`10.x`、`172.16-31.x`、
        `192.168.x`、`127.x`、`169.254.x`、`::1`、`fe80::/10`、docker 网桥……）。
    """
    h = str(host or "").strip().strip("[]").lower().rstrip(".")
    if not h:
        # 认不出主机名：交给后面的 netloc 校验去报错，这里不算"远程"（不新增一类失败）。
        return True
    if h in _LOCAL_HOSTNAMES or h.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        return bool(ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified)
    # 单标签（内部没有点）= 内网短名。**故意不查 DNS**：部署期的机器常常还没解析，
    # 而"查不到"绝不能变成"那就当它安全/不安全"。
    return "." not in h


def scheme_of(url: str) -> str:
    """这个地址写的什么 scheme（`http` / `https` / `""`）—— **不补齐、不猜**。

    ⚠️ 只认这两个，别的一律返回空串：`urlsplit("echoserver.zhkq.online:8900").scheme`
    是 **`echoserver.zhkq.online`**（那串冒号让它以为前面是 scheme）—— 照着返回，
    "改地址"那条路就会把一个域名当成 scheme 传下去（于是永远拼不出正确的地址）。

    "改地址"那条路用它把**已配对凭据里那个 scheme**当作提示传下去
    （见 `normalize_base_url(scheme_hint=)`）：换 IP/域名不等于换 scheme，
    而手输的新地址往往不带 scheme（面板回填的也是）—— 那时该沿用**公布时给的那个**。
    """
    try:
        got = urllib.parse.urlsplit(str(url or "")).scheme.strip().lower()
    except Exception:                                          # pragma: no cover - 兜底
        return ""
    return got if got in ("http", "https") else ""


def normalize_base_url(url: str, *, scheme_hint: str = "") -> str:
    """把用户填的地址规整成 `scheme://host[:port]`（去掉尾斜杠）。

    ## 没有 scheme 时怎么办（2026-10-16 改的口径，缺口 2）

    | 主机看起来是 | 结果 | 为什么 |
    |---|---|---|
    | 本机 / 局域网（`10.100.0.24:8900`、`gpu-01:8900`、`127.0.0.1:8900`） | 补 `http://` | **老用法一个字都不改**：局域网明文后端（还有本机后端）靠的就是它 |
    | 远程（公网域名 / 公网 IP）+ 有 `scheme_hint` | 用 `scheme_hint` | 那就是**公布时给的那个 scheme**（已配对凭据里记着），不是现猜的 |
    | 远程 + 没有 hint | **抛 `PairingError`** | 静默补 `http://` 正是缺口 2 那个 bug：公网入口是 https 时连到明文口，现象是"连不上/400" |

    ⚠️ **为什么不"远程就默认 https"**：那是拿猜测换安全 —— 一台真的在公网上跑明文 http 的
    后端会被我们连错（而且错误现象与"它挂了"一样）。**宁可不猜**：要么地址里写全，
    要么用管理员给的整串（那串里的 scheme 是权威的）。文案里直接说清怎么改。

    ⚠️ **与服务端"公布"那侧的口径不同，是有意的**：`server/ops.py::_published_scheme`
    对**远程公布的地址**默认按 https 发（那是它知道自己的部署形态、而且是在"告诉别人
    怎么连"）；客户端这一侧手里只有一串人输进来的字，猜错就是把音频发到明文口或连不上 ——
    所以这里要么有 scheme、要么有凭据里记着的 `scheme_hint`、要么报错。
    """
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = _scheme_for_bare_address(text, raw=url, scheme_hint=scheme_hint) + "://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PairingError("地址要写成 http(s)://主机:端口，收到的是 %r" % url)
    return "%s://%s" % (parts.scheme, parts.netloc)


def _scheme_for_bare_address(text: str, *, raw: str, scheme_hint: str = "") -> str:
    """没有 scheme 的地址该按哪个 scheme 走（`normalize_base_url` 的那张表）。"""
    try:
        host = urllib.parse.urlsplit("//" + text).hostname or ""
    except ValueError:                                         # pragma: no cover - 兜底
        host = ""
    if looks_local_host(host):
        return "http"
    hint = str(scheme_hint or "").strip().lower()
    if hint in ("http", "https"):
        return hint
    raise PairingError(
        "这个地址看起来在**别的机器上**（不是局域网/本机地址），而它没写 scheme，"
        "所以我不替它猜：%r。请写成 `https://主机:端口`——公网入口现在基本都是 https"
        "（按明文 http 连会被拒，而且看不出是这个原因）；确实是明文 http 就显式写 "
        "`http://`。也可以直接粘管理员给的整串 `echo://pair?host=…&code=…`，"
        "那串里的 scheme 是权威的。" % (raw,), code="bad_request")


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
        # 取证书、校期望指纹就在这一句里（与"只改地址时补固定"是同一份实现，
        # 见 `pin_certificate`）——两份实现迟早漂开。
        cert_pem, fp = pin_certificate(url, timeout=timeout,
                                       cert_fingerprint=cert_fingerprint)
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


# ---------------------------------------------------------------- 只改地址时补证书固定
#
# 背景（2026-10-16 真机，缺口 1）：公网入口从 `http://echoserver.zhkq.online:8900` 换成
# `https://echoserver.zhkq.online:8900` 之后，用产品自己的 `POST /api/capability/address`
# 改地址**立刻失败**：
#     后端 配的是 https，但本机没有它的证书（缺 certPem）——拒绝连接
# 而 `usable.ready=False`。那一晚是用 `pair()` 里**逐字同一段**
# （`fetch_cert_pem` → `fingerprint_of` → `credentials.save`）手工补的 pin ——
# 也就是说这件事**只差接一根线**：改地址这条路本来就该顺手把证书补上。
#
# 而 2026-10-16 晚的复盘又指出：当时那根线是接在"**先落地址、后抓证书**"上的 ——
# 一次网络抖动就会留下一台"地址已改、证书还是旧的"的机器（`ready=False`）。
# 现在的分工是：`prepare_repin()`（先抓证书，**不写盘**）+ `capability_admin.set_base_url()`
# （成功后**一次性**把地址与证书写下去）；`repin_for_address()` 只剩排障与既有用例在用。
#
# 为什么不能"顺手重新配对一次"：重新配对要管理员再发一张一次性码，而且会**换掉
# clientId**（服务端多一条客户端记录；中继的 hostId 取自 clientId，手机侧地址跟着一起废）。
# 换地址 ≠ 换身份，clientId/secret/scopes/hostId 必须一个字节都不动。


def needs_repin(base_url: str, cert_pem: str, *, old_base_url: str = "",
                cert_fingerprint: str = "") -> bool:
    """现在这个地址需不需要（重新）抓一张证书固定下来？**纯判据，不联网。**

    三条，按"最省事"的顺序：

      ① 不是 https → 不需要。明文没有"固定证书"这回事，所以 http→http、回环那一档
         一个字节都不动（那条纪律写在 AGENTS.md 里）。
      ② 本地没有证书 → 需要。**这就是昨晚那面墙**：http→https 换过去时本地没有它的
         证书，而 https **绝不允许**"没证书也连"（`pinned_context` 直接拒绝），
         于是改完地址立刻不可用。
      ③ 换到了**另一处**（host:port 变了）→ 需要。证书里没有"这是哪台机器"的可靠记录
         （`pinned_context` 故意不校名：内网常按 IP/短名访问，证书上不会签它），
         所以换了 host:port 就只能重新取一张；同一个 host 下换端口/加反代时重取也无害
         （TOFU 一次，取回来的仍是服务端现在这张）。
         给了期望指纹（`fp=`）却与本地存的那张不一致时同理：得去把现在这张取来看看
         ——对不上自然会在 `pin_certificate` 里**响亮地失败**，而不是拿旧证书硬连。

    `old_base_url` 留空 = 调用方不知道旧地址，那就**不**按③重取（保守：不动现有固定）。
    """
    if not is_https(base_url):
        return False
    have = str(cert_pem or "").strip()
    if not have:
        return True
    want = normalize_fingerprint(cert_fingerprint)
    if want and want != fingerprint_of(have):
        return True
    if not old_base_url:
        return False
    try:
        return _host_port(old_base_url) != _host_port(base_url)
    except Exception:
        # 旧地址认不出来（残缺/手改过）→ 当成"换了一处"，重取一张最安全。
        return True


def prepare_repin(url: str, *, old_base_url: str = "", cert_pem: str = "",
                  cert_fingerprint: str = "",
                  timeout: float = PAIR_TIMEOUT_S) -> Tuple[str, str, str]:
    """**落地址之前**先做"要不要 / 能不能固定证书"这一步：返回 `(证书 PEM, 指纹, 一句话)`。

    调用点：`capability_admin.set_base_url()`（= `POST /api/capability/address`）。
    它必须在**写盘之前**调这里 —— 2026-10-16 复盘那条"一次网络抖动就能把一台本来可用的
    机器改成不可用"（A1 的缺口 1 修法版）：改了地址才去抓证书，抓不到时地址已经变了、
    证书还是旧的 → `ready=False`。现在顺序倒过来：**先抓证书 + 校期望指纹，成功了才落地址**；
    这里失败就抛（调用方原样保留旧地址与旧凭据，那台机器仍然可用）。

    ⚠️ **它一个字节都不写盘**（`credentials.save` 由调用方在成功后一次性做）——
    这是"先抓证书、成功才落地址"能成立的前提。"抓证书 + 校指纹"这件事只有
    `pin_certificate` 一份实现（`pair()` 与这里共用），别在别处再写一遍。

    不需要补时返回 `("", "", "")`：三个都空 = 什么都没做、也没什么要说。
    """
    if not needs_repin(url, cert_pem, old_base_url=old_base_url,
                       cert_fingerprint=cert_fingerprint):
        return "", "", ""
    cert, fp = pin_certificate(url, timeout=timeout, cert_fingerprint=cert_fingerprint)
    return cert, fp, ("已把 %s 的证书固定下来（%s）—— clientId/secret 一个字节都没动"
                      % (url, fp))


def repin_for_address(old_base_url: str = "", *, cert_fingerprint: str = "",
                      timeout: float = PAIR_TIMEOUT_S, save: bool = True) -> str:
    """按**凭据里已经记着的地址**补一次证书固定：**返回一句人话**（没做事 = 空串）；失败就抛。

    这是 2026-10-16 上午那条修法的入口（当时是"先 `set_base_url()` 落盘、再补 pin"）。
    ⚠️ **产品那条路（`POST /api/capability/address`）现在走 `prepare_repin` + 一次性提交**
    ——"先落地址、后抓证书"的顺序本身就是那次复盘的病灶，别再把它接回产品路。
    这个入口留着有两个正当用途：① 排障/脚本里"地址已经变了，把证书补上"；
    ② `tests/test_backend_tls.py::AddressChangeRepinTests` 钉着的那套判据。
    两处共用 `needs_repin`（要不要补）与 `pin_certificate`（怎么补），没有第二份实现。

    为什么失败要**抛**而不是静默：https 而本地没有对应证书 = 这台机器现在**用不了**
    这个后端（`usable.ready=False`）。静默过去，用户得到的是一句含糊的"改好了"，
    然后每次转写都撞同一面墙 —— 那正是 2026-10-16 真机上的现象。

    **只动证书那两栏**：`client_id` / `secret` / `server_name` / `paired_at` 原样保留
    （对象是 `credentials.load()` 读回来的那一份，只改 `cert_pem` / `cert_fingerprint`）。
    """
    c = cred.load()
    if c is None:
        return ""                       # 没配对过：改地址这条路本来就走不到这儿
    try:
        url = normalize_base_url(c.base_url)
    except PairingError:
        return ""                       # 地址本身认不出来 → 没有"补证书"可谈
    cert, fp, note = prepare_repin(url, old_base_url=old_base_url, cert_pem=c.cert_pem,
                                   cert_fingerprint=cert_fingerprint, timeout=timeout)
    if not note:
        return ""
    c.cert_pem = cert
    c.cert_fingerprint = fp
    if save:
        cred.save(c)
    return note


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


# ---------------------------------------------------------------- 本机自动配对（方案 1）
#
# 背景（2026-09-28，见 `docs/3.0-设计总览与组件关系.md` §6.6）：ECHO 拆成
# "客户端 + 能力后端"两段之后，"本机跑得动"的实质就是**本机装了一个后端**。
# 那种情况下不该让人抄配对码（配对串是给**另一台**机器准备的）——同机有文件系统，
# 握手用文件即可：后端启动写 `{state_root}/local-pair.json`，客户端读它。
#
# **走的仍然是下面这条 `pair()`**：文件里的 `code` 就是一个普通的一次性配对码，
# 产物与手工粘贴配对串**完全一样**（同一个 `{DATA}/backend.json`、同一套凭据信封、
# 同一个 TLS 指纹固定）。所以这里没有第二套配对实现，只有"码从哪来"的区别。

#: 本机自配对文件的文件名（与服务端 `server/localpair.py` 的 `FILENAME` 同一个）。
LOCAL_PAIR_FILENAME = "local-pair.json"


def _local_pair_setting() -> str:
    try:
        from app.config import settings
        return str(settings.get("capabilityLocalPairPath", "") or "").strip()
    except Exception:
        return ""


def local_pair_candidates() -> list:
    """本机配对文件的候选位置，**顺序即优先级**。

    为什么是"一串候选 + 一个设置项"而不是写死一个路径：方案 1 里后端与客户端同机，
    但后端可能是

      * 同一棵树里直接跑的（dev / 源码装）→ `{DATA}/server-state/local-pair.json`；
      * 官方后端包（compose）装的 → 状态卷通常挂在 `{ECHO_BASE}/backend/state`；
      * 用户自己把状态卷挂在别处（Docker 的宿主路径是用户定的，**猜不出来**）。

    最后那种只能靠设置 `capabilityLocalPairPath`（或环境变量 `ECHO_LOCAL_PAIR_PATH`）。
    猜不到时的行为是"如实说找不到 + 列出看过哪些位置 + 提示可以用配对串"，
    不是静默失败。
    """
    out = []
    explicit = _local_pair_setting()
    if explicit:
        out.append(explicit)
    env = os.environ.get("ECHO_LOCAL_PAIR_PATH", "").strip()
    if env:
        out.append(env)
    data = base = ""
    try:
        from app import paths
        data = paths.data_root()
        base = paths.echo_base()
    except Exception:
        pass
    for root in (os.path.join(data, "server-state") if data else "",
                 os.path.join(base, "backend", "state") if base else "",
                 os.path.join(base, "backend") if base else "",
                 os.path.join(base, "data", "server-state") if base else ""):
        if root:
            out.append(os.path.join(root, LOCAL_PAIR_FILENAME))
    seen, uniq = set(), []
    for p in out:
        full = os.path.abspath(os.path.expanduser(str(p)))
        if full not in seen:
            seen.add(full)
            uniq.append(full)
    return uniq


def read_local_pair(path: str = "") -> Tuple[str, Dict[str, Any]]:
    """找到并读出本机配对文件 → `(实际路径, 内容)`；找不到/读不出**抛人话**。"""
    cands = [path] if path else local_pair_candidates()
    existing = [p for p in cands if os.path.isfile(p)]
    if not existing:
        raise PairingError(
            "没找到本机后端的配对文件 —— 方案 1（本机自建后端）要先在后端那台机器上"
            "把它跑起来（后端启动时会写一份）；后端在**别的机器**上就用手工配对串。"
            "看过的位置：%s" % ("、".join(cands[:4]) or "（没有可看的位置）"), code="absent")
    target = existing[0]
    try:
        with open(target, "r", encoding="utf-8") as f:
            body = json.load(f)
    except Exception as e:
        raise PairingError("本机配对文件读不出来（%s）：%s" % (target, e), code="bad_response")
    if not isinstance(body, dict):
        raise PairingError("本机配对文件的内容不对（%s）：不是 JSON 对象" % target,
                           code="bad_response")
    return target, body


def local_pair_state() -> Dict[str, Any]:
    """面板用：本机有没有可用的配对文件。**不含明文码**。"""
    try:
        target, body = read_local_pair()
    except PairingError as e:
        return {"found": False, "path": "", "baseUrl": "", "expired": False,
                "expiresAt": 0.0, "message": e.message}
    try:
        exp = float(body.get("expiresAt") or 0)
    except Exception:
        exp = 0.0
    return {"found": True, "path": target,
            "baseUrl": str(body.get("baseUrl") or ""),
            "expired": bool(exp and time.time() > exp),
            "expiresAt": exp, "message": ""}


def pair_local(*, client_name: str = "本机自动配对", save: bool = True,
               path: str = "") -> BackendCredentials:
    """方案 1 的握手：读本机配对文件 → 交给**原来那条** `pair()`。

    过期**如实说**：那个码是一次性的、有 TTL（服务端默认 7 天），而后端**每次启动**
    会重发一张。所以正确指引是"让后端再启动一次"，不是让人去查网络或重装。
    """
    target, body = read_local_pair(path)
    base_url = str(body.get("baseUrl") or "").strip()
    code = str(body.get("code") or "").strip()
    if not base_url or not code:
        raise PairingError("本机配对文件缺 baseUrl/code（%s）—— 版本对不上？" % target,
                           code="bad_response")
    try:
        exp = float(body.get("expiresAt") or 0)
    except Exception:
        exp = 0.0
    if exp and time.time() > exp:
        raise PairingError(
            "本机配对码已过期（%s 到期；后端每次启动会重发一张）—— 在后端那台机器上"
            "重启一次后端，或手工粘一张配对串。"
            % time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)), code="expired")
    return pair(base_url, code, client_name=client_name,
                cert_fingerprint=str(body.get("fingerprint") or ""), save=save)
