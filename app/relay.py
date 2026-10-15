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
import json
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


def status() -> dict:
    """给面板/接口看的一份状态（**纯读**，不触发任何动作）。"""
    ok, why = ready()
    out = dict(STATE)
    out["enabled"] = enabled()
    out["ready"] = bool(ok)
    out["reason"] = "" if ok else why
    out["publicUrl"] = public_url() or out.get("publicUrl", "")
    return out


# ---------------------------------------------------------------- 白名单（与后端同一口径）

def path_allowed(path: str) -> bool:
    """只放行 `/health` 与 `api/*`，并挡掉 `..`（挡住"从 api/ 前缀爬出去"那种路径）。

    后端已经判过一次；这里是**第二道**：万一那边放宽了、或有人直接调本模块的接口，
    这一道仍然拦得住。判据与 `server/relay.py::path_allowed` 刻意写成同形。
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
