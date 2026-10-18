# -*- coding: utf-8 -*-
"""relay.py — 主机侧中继：**主动去后端取活**，把手机的请求在本机执行掉。

## 它解决什么

办公室里手机与这台电脑常常互相连不通（AP 隔离 / 跨网段 / 入站被防火墙拦），
但两边**都能连到那台能力后端**。于是反过来做：

    手机 ──HTTP──▶ 后端 /v1/relay/<hostId>/api/… ──排队──┐
                                                          │（本模块主动去取，出站不会被拦）
    本机 ──POST──▶ 后端 /v1/relay/host/poll ───────────────┘
          ◀── 取回一条调用 ──→ 在本机 127.0.0.1:8970 上执行 ──▶ 交回结果

**为什么必须是"我们主动取"**：这台电脑的**入站**被挡着，出站是通的。
所以连接方向不能反过来；长轮询只是在"没有 WebSocket 依赖"的前提下把延迟压到≈即时。

## 安全：这一模块和 `app/api.py` 里那一条是**成对**的

转发时我们带 `X-Echo-Relay`；`api._is_loopback_call()` 见到它就**不再把回环当免检**。
少了任何一半，中继都会变成一个**绕过令牌的后门**（请求从 127.0.0.1 进来，
本机豁免把鉴权整个跳过）。所以：

  * 中继**必须**在 `apiAuthEnabled=true` 时才允许开（见 `ready()`）——
    鉴权关着的时候本机 API 对回环是全开放的，转发进来等于把电脑敞开；
  * 只放行 `/health` 与 `api/*`（这里判一次，后端那次是第二道）；
  * 只把 `Authorization` 与 `Content-Type` 转发过去，**不转发 Host 等其它头**
    （让落地的请求看起来就是一次干净的本机调用，也不给头注入留口子）。

## 默认关

`relayEnabled` 默认 false：不开时这个模块什么都不做（不起线程、不发一个请求）。
"""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request

from app.config import settings

#: 转发请求带的标记 —— `app/api.py::_is_loopback_call` 认它（**两边必须一致**）。
RELAY_MARKER_HEADER = "X-Echo-Relay"
RELAY_MARKER_VALUE = "1"

#: 长轮询一次等多久（后端那边上限 25 秒；我们留一点余量给网络）。
POLL_WAIT_S = 25.0

#: 单次本机执行的上限：后端等 600 秒，这里要**比它短** ——
#: 让"本机先放弃、把话说清楚"而不是"后端先超时、本机还在跑"。
LOCAL_CALL_TIMEOUT_S = 570.0

#: 空转时的最小间隔（正常情况后端会等满 POLL_WAIT_S 才回，这里只是兜底）。
IDLE_SLEEP_S = 0.5

#: 断线后的退避上下限。
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0

#: 白名单（与 `server/relay.py::path_allowed` **同一口径**，两处都判）。
ALLOWED_PREFIX = "api/"
ALLOWED_EXACT = ("health",)

#: 「经中继打开 DSH 标准版网页」的入口前缀（2026-10-18 用户拍板）。
#:
#: **为什么必须在 `api/` 下**：中继服务端（`server/relay.py::path_allowed`）只放行
#: `/health` 与 `api/*`，而那一层**已经部署在 echoserver 上**、本轮又不许改
#: （改了不部署 = 没改）。所以网页入口只能借 `api/` 这条**已经存在**的放行通道。
#: 这不是"随手选的路径"：换个前缀，线上中继会直接 403，网页根本到不了这台电脑。
WEB_ENTRY_PREFIX = "api/relaydsh/"

#: 只读网页会话的可选请求头。带上它 = "我明确用那条只读会话"；
#: 写请求（POST/PUT/PATCH/DELETE）只要带的是**有效**的这一枚，一律 403
#: （见 `web_session_denied()` 与 `app/api.py::optional_auth` 的第一道闸）。
#: 真正的只读边界在 `app/relay_web.py`（那个入口只放行静态页与读 RPC）——
#: 这个头是"第二道"，防的是"拿着会话令牌去调别的写接口"。
WEB_SESSION_HEADER = "X-Echo-Relay-Web"

#: 会话寿命。过期就换一枚（旧的立刻失效）—— 地址会被手机存下来，
#: 但"一条只读会话"不该是一张永久通票。
WEB_SESSION_TTL_S = 12 * 3600.0

#: 进程级状态（面板/接口读它；纯读不改）。
STATE = {
    "running": False,        # 线程在跑吗
    "connected": False,      # 最近一次 poll 成功吗
    "hostId": "",            # 后端认下来的 hostId（= 我们的 client_id）
    "publicUrl": "",         # 手机该填的中继地址（带 hostId）
    "calls": 0,              # 累计执行过几次
    "lastPollAt": 0.0,
    "lastCallAt": 0.0,
    "lastError": "",
}

#: 「中继专用只读会话」的进程级状态（**只读，不是把管理面暴露出去**）。
#:
#: 一枚随机令牌 + 创建时间。它出现在 `dshWebUrl` 的**路径段**里（不是查询串）：
#: 页面加载后的资源请求都是**文档相对**的（`./assets/…`、`api/<ns>/<method>`），
#: 令牌作为路径段会被它们自然继承 —— 查询串则会在下一次子请求里丢掉。
#: 这样"一条会话"才真的贯穿整页，而不是只有首屏带得上。
_WEB_LOCK = threading.Lock()
_WEB_SESSION = {"token": "", "at": 0.0}


def enabled() -> bool:
    """总开关（默认关）。"""
    return bool(settings.get("relayEnabled", False))


def ready() -> tuple:
    """能不能开中继，返回 `(ok, 原因)`。原因要能直接显示给用户。"""
    if not enabled():
        return False, "中继没打开（设置 → 中继 → 允许手机经后端中转）"
    try:
        from app.capabilities.echo_server import backend_endpoint
        base_url, _auth = backend_endpoint(slot="relay")
    except Exception as e:                                        # noqa: BLE001
        return False, "读不到后端地址/凭据：%s" % e
    if not base_url:
        return False, ("还没配对能力后端：先在面板「能力」页配对一台后端，"
                       "中继要靠它做中转")
    if not bool(settings.get("apiAuthEnabled", False)):
        # 这一条是**硬要求**，不是提醒：鉴权关着时本机 API 对回环全开放，
        # 转发进来等于把办公电脑对整个中继敞开（见模块头部的安全说明）。
        return False, ("要先打开「API 鉴权」（设置 → 面板与服务 → API 鉴权）："
                       "中继进来的请求来自回环，不开鉴权就等于把这台电脑对外开放")
    return True, ""


def public_url() -> str:
    """手机该填的中继地址（形如 `http://后端/v1/relay/<hostId>/`）。"""
    host_id = str(STATE.get("hostId") or "")
    if not host_id:
        return ""
    try:
        from app.capabilities.echo_server import backend_endpoint
        base_url, _auth = backend_endpoint(slot="relay")
    except Exception:                                             # noqa: BLE001
        return ""
    if not base_url:
        return ""
    return "%s/v1/relay/%s/" % (base_url.rstrip("/"), host_id)


_HARNESS_PROBE = {"at": 0.0, "ok": False}
#: 探活缓存几秒：`/api/status` 是每几秒轮询一次的，没必要每次都真连一次 harness。
#: 代价只是"标准版刚起来时，地址最多晚 3 秒出现"—— 比每次轮询都多一跳划算。
_HARNESS_PROBE_CACHE_S = 3.0


def _harness_online(timeout: float = 0.8) -> bool:
    """DSH 标准版 harness 在监听吗（**一次本机探活**，不起进程、不带 token）。

    为什么网页地址要卡这一条：地址的承诺是"点开就能看到 DSH 标准版网页"。
    harness 没跑时把地址给出去，用户点开只会得到一句 502 —— 那是"编了一个打不开的
    地址"，比不给更坏。所以这里如实探一次；探不到就把 `dshWebUrl` 空着 + 说明原因。
    """
    now = time.time()
    if (now - float(_HARNESS_PROBE.get("at") or 0.0)) < _HARNESS_PROBE_CACHE_S:
        return bool(_HARNESS_PROBE.get("ok"))
    try:
        from app import harness_proc
        ok = bool(harness_proc.online(timeout=timeout))
    except Exception:                                             # noqa: BLE001 - 探不了就当没跑
        ok = False
    _HARNESS_PROBE.update(at=now, ok=ok)
    return ok


def web_session(rotate: bool = False) -> str:
    """取（必要时新建）那条「中继专用只读会话」的令牌。**可重复调用。**

    `rotate=True` 立刻换一枚（旧地址立即失效，用于关掉中继/收尾）。
    """
    now = time.time()
    with _WEB_LOCK:
        token = str(_WEB_SESSION.get("token") or "")
        born = float(_WEB_SESSION.get("at") or 0.0)
        if rotate or not token or (now - born) > WEB_SESSION_TTL_S:
            token = secrets.token_urlsafe(24)
            _WEB_SESSION.update(token=token, at=now)
        return token


def web_session_valid(token: str) -> bool:
    """这枚令牌**现在**是不是那条只读会话（过期即失效，比较用常量时间）。"""
    got = str(token or "")
    with _WEB_LOCK:
        want = str(_WEB_SESSION.get("token") or "")
        born = float(_WEB_SESSION.get("at") or 0.0)
    if not want or not got:
        return False
    if (time.time() - born) > WEB_SESSION_TTL_S:
        return False
    return hmac.compare_digest(want, got)


def web_session_denied(request) -> bool:
    """写请求是不是**明确**打着那条只读会话来的；是 → 该拒。

    只认请求头（`X-Echo-Relay-Web`）：只读会话的地址在路径里，浏览器不会把它带进
    `/api/settings` 这种别的写接口 —— 所以"拿着会话令牌去写"必须是**显式**行为，
    我们才拦得住；而普通手机令牌（别的设备）照旧按它自己的权限走。

    ⚠️ 不含 `GET/HEAD/OPTIONS`：只读会话本身就要靠 GET 取静态页。
    ⚠️ 不再拦 `/api/relaydsh/` 自己的 POST：那条路要放行**读** RPC（`session/list` 这种
    就是 POST），它的白名单在 `app/relay_web.py` 里按**方法名**判，比这里按 HTTP 动词判更准。
    """
    try:
        if str(getattr(request, "method", "") or "").upper() in ("GET", "HEAD", "OPTIONS"):
            return False
        path = str(getattr(getattr(request, "url", None), "path", "") or "")
        if path.startswith("/api/relaydsh/"):
            return False
        token = str(request.headers.get(WEB_SESSION_HEADER) or "")
        return web_session_valid(token)
    except Exception:                                             # noqa: BLE001 - 判不出就不拦
        return False


def web_entry() -> dict:
    """`{"url", "reason", "readOnly"}` —— 给手机/面板看的**只读网页入口**。

    * `url` 只在"真的能打开"时非空：中继已连上 + 有 publicUrl + DSH 标准版在跑；
    * 否则**如实为空**，`reason` 说清缺哪一条（**绝不编一个打不开的地址**）。
    """
    if not enabled():
        return {"url": "", "readOnly": True,
                "reason": "中继没打开（设置 → 中继 → 允许手机经后端中转）"}
    if not bool(STATE.get("connected")):
        return {"url": "", "readOnly": True,
                "reason": str(STATE.get("lastError") or "") or "中继还没连上后端"}
    # 地址取**缓存的那份**（每次成功 poll 都会刷新），不要在这里再走一遍
    # `backend_endpoint()` —— 那个可能联网换令牌，而 `/api/status` 是每几秒轮询一次的。
    base = str(STATE.get("publicUrl") or "")
    if not base:
        base = public_url()          # 极少见（连上了但缓存还空着）：补一次
    if not base:
        return {"url": "", "readOnly": True,
                "reason": "还没有中继地址（后端没配对，或还没拿到 hostId）"}
    if not _harness_online():
        return {"url": "", "readOnly": True,
                "reason": "DSH 标准版 harness 没在运行（设置 → 智能体 → 选「标准版 harness」）"}
    return {"url": base + WEB_ENTRY_PREFIX + web_session() + "/", "readOnly": True,
            "reason": ""}


def web_url() -> str:
    """只读网页入口的地址；不可用时回空串（见 `web_entry()`）。"""
    return str(web_entry().get("url") or "")


def status(refresh_ready: bool = False) -> dict:
    """给面板/接口看的一份状态（纯读，不触发任何动作）。

    ⚠️ **默认不现算 `ready()`**：`ready()` 会走 `backend_endpoint()`（读配对文件、
    必要时**联网换令牌**），而面板是每几秒刷一次的 —— 让面板的一次刷新卡在网络上
    是不能接受的。所以默认只回**上一次 poll 的缓存状态**；要"现在到底行不行"，
    由调用方显式传 `refresh_ready=True`（`GET /api/relay` 就是这么用的）。
    """
    out = dict(STATE)
    out["enabled"] = enabled()
    out["publicUrl"] = str(out.get("publicUrl") or "")
    if refresh_ready:
        ok, why = ready()
        out["ready"] = bool(ok)
        out["reason"] = "" if ok else why
    else:
        connected = bool(out.get("connected"))
        out["ready"] = connected
        out["reason"] = "" if connected else (
            str(out.get("lastError") or "") or "中继还没连上（或开关没开）")
    # 经中继打开 DSH 标准版网页的那条**只读**入口（2026-10-18 用户拍板）。
    # 不可用时 url 为空、reason 说清原因 —— 消费者不该自己拼一个。
    entry = web_entry()
    out["webUrl"] = str(entry.get("url") or "")
    out["webReason"] = str(entry.get("reason") or "")
    out["webReadOnly"] = True
    return out


# ---------------------------------------------------------------- 白名单（与后端同一口径）

def path_allowed(path: str) -> bool:
    """只放行 `/health` 与 `api/*`，并挡掉 `..`（挡住"从 api/ 前缀爬出去"那种路径）。

    后端已经判过一次；这里是**第二道**：万一那边放宽了、或有人直接调本模块的接口，
    这一道仍然拦得住。判据与 `server/relay.py::path_allowed` 刻意写成同形。

    ⚠️ 这里的 `api/*` **已经**涵盖网页入口（`api/relaydsh/<会话>/…`）—— 那是刻意的：
    中继服务端的放行面就只到 `api/`，网页入口必须活在那条通道里（见 `WEB_ENTRY_PREFIX`）。
    而"入口里**只**允许哪些路径、哪些方法"是另一层（`app/relay_web.py`），不是这里。
    """
    p = str(path or "").replace("\\", "/").lstrip("/")
    if not p:
        return False
    if any(seg in ("..", ".") for seg in p.split("/")):
        return False
    return p in ALLOWED_EXACT or p.startswith(ALLOWED_PREFIX)


# ---------------------------------------------------------------- HTTP 小工具

def _post_json(url: str, payload: dict, auth: str, timeout: float) -> dict:
    """往后端发一个小 JSON（错了抛异常，由调用方退避重试）。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8") or "{}")


def _local_base() -> str:
    """本机面板/API 的地址（中继的落地目标）。"""
    try:
        port = int(settings.get("serverPort", 8970) or 8970)
    except (TypeError, ValueError):
        port = 8970
    return "http://127.0.0.1:%d" % port


def _execute(call: dict) -> dict:
    """在本机执行一条中继调用，返回给后端的 result 体。**不抛异常。**"""
    call_id = str(call.get("id") or "")
    out = {"id": call_id, "status": 502, "headers": {}, "body": ""}
    path = str(call.get("path") or "")
    if not path_allowed(path):
        out["error"] = "路径不在中继白名单里（只放行 /health 与 /api/*）"
        return out
    query = str(call.get("query") or "")
    url = _local_base() + "/" + path.lstrip("/")
    if query:
        url += "?" + query
    headers = call.get("headers") or {}
    method = str(call.get("method") or "GET").upper()
    body = b""
    if call.get("body"):
        try:
            body = base64.b64decode(call["body"])
        except Exception:                                         # noqa: BLE001
            out["error"] = "请求体解不开（base64 坏了）"
            return out
    req = urllib.request.Request(url, data=body if body else None, method=method)
    # **只转发这两样**：令牌（端到端鉴权）与内容类型（裸音频那条路要用）。
    auth = str(headers.get("authorization") or "")
    ctype = str(headers.get("content-type") or "")
    if auth:
        req.add_header("Authorization", auth)
    if ctype:
        req.add_header("Content-Type", ctype)
    req.add_header(RELAY_MARKER_HEADER, RELAY_MARKER_VALUE)
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_CALL_TIMEOUT_S) as resp:
            payload = resp.read()
            out["status"] = int(resp.status)
            out["headers"] = {"content-type": resp.headers.get("Content-Type") or ""}
    except urllib.error.HTTPError as e:
        # 4xx/5xx 是**正常的业务回答**（401/403/409…），照原样带回给手机
        payload = e.read()
        out["status"] = int(e.code)
        out["headers"] = {"content-type": e.headers.get("Content-Type") or "" if e.headers else ""}
    except Exception as e:                                        # noqa: BLE001
        out["error"] = "本机执行失败：%s" % e
        return out
    out["body"] = base64.b64encode(payload or b"").decode("ascii")
    out.pop("error", None)
    return out


# ---------------------------------------------------------------- 工作线程

class RelayWorker(threading.Thread):
    """常驻线程：poll 取活 → 本机执行 → 交回结果，出错就退避重连。"""

    def __init__(self):
        super().__init__(name="echo-relay", daemon=True)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:                                        # noqa: C901 - 循环最直白
        from app.capabilities.echo_server import backend_endpoint
        ok, why = ready()
        if not ok:
            STATE["running"] = False
            STATE["connected"] = False
            STATE["lastError"] = why
            return
        STATE["running"] = True
        STATE["lastError"] = ""
        backoff = BACKOFF_MIN_S
        while not self._stop.is_set():
            try:
                base_url, auth = backend_endpoint(slot="relay")
                if not base_url:
                    raise RuntimeError("后端地址空着（配对信息读不到）")
                poll_url = base_url.rstrip("/") + "/v1/relay/host/poll"
                data = _post_json(poll_url, {
                    "wait": POLL_WAIT_S,
                    "name": socket.gethostname(),
                    "version": _version(),
                }, auth, timeout=POLL_WAIT_S + 20.0)
                STATE["connected"] = True
                STATE["lastError"] = ""
                STATE["lastPollAt"] = time.time()
                host_id = str(data.get("hostId") or "")
                if host_id:
                    STATE["hostId"] = host_id
                    STATE["publicUrl"] = public_url()
                calls = data.get("calls") or []
                for call in calls:
                    if self._stop.is_set():
                        break
                    result = _execute(call)
                    try:
                        _post_json(base_url.rstrip("/") + "/v1/relay/host/result",
                                   result, auth, timeout=30.0)
                    except Exception as e:                        # noqa: BLE001
                        STATE["lastError"] = "交回结果失败：%s" % e
                    STATE["calls"] = int(STATE.get("calls") or 0) + 1
                    STATE["lastCallAt"] = time.time()
                backoff = BACKOFF_MIN_S
                if not calls:
                    self._stop.wait(IDLE_SLEEP_S)
            except Exception as e:                                # noqa: BLE001 - 断线不许退出
                STATE["connected"] = False
                STATE["lastError"] = str(e)
                self._stop.wait(backoff)
                backoff = min(BACKOFF_MAX_S, backoff * 2)
        STATE["running"] = False
        STATE["connected"] = False


def _version() -> str:
    try:
        from app import api as _api
        return str(getattr(_api, "ECHO_VERSION", "") or "")
    except Exception:                                             # noqa: BLE001
        return ""


_WORKER: RelayWorker | None = None
_LOCK = threading.Lock()


def ensure_running() -> bool:
    """按开关起（或不起）中继线程。返回"现在在跑吗"。**可重复调用。**"""
    global _WORKER
    if not enabled():
        stop()
        return False
    ok, why = ready()
    if not ok:
        STATE["lastError"] = why
        return False
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return True
        _WORKER = RelayWorker()
        _WORKER.start()
        return True


def stop() -> None:
    """停掉中继线程（关开关/退出时调）。"""
    global _WORKER
    with _LOCK:
        worker = _WORKER
        _WORKER = None
    if worker is not None:
        worker.stop()
    STATE["running"] = False
    STATE["connected"] = False
    # 中继停了，那条只读会话的地址立刻作废：换一枚令牌 = 旧地址再也进不来。
    # 不这么做的话，关掉中继之后旧链接在下次打开中继时又会"活过来"。
    with _WEB_LOCK:
        _WEB_SESSION.update(token="", at=0.0)
