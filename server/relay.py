# -*- coding: utf-8 -*-
"""中继（relay）：让**互相连不通**的手机与主机借这台服务器说上话。

## 它解决什么

办公室里手机与办公电脑常常互相连不通（AP 隔离 / 跨网段 / 入站被防火墙拦），
但两边**都能连到这台后端**。于是：主机主动来这台服务器"取活"（出站，不会被拦），
手机的请求经这里转交给主机、再把结果带回去。

    手机 ──HTTP──▶ 后端 /v1/relay/<hostId>/api/… ──┐
                                                   │（主机来 poll，取走并执行）
    主机 ──HTTP──▶ 后端 /v1/relay/host/poll ────────┘
                  后端 /v1/relay/host/result ◀── 主机执行本机 127.0.0.1:8970 的结果

## 为什么用长轮询而不是 WebSocket

**不引新依赖**（这台服务器只有 fastapi/uvicorn；客户端那边更是刻意薄）。
长轮询的延迟与常连接几乎一样（有活时 poll 立刻返回），而且**能用 curl 调**、
能被任何代理转发、出错时看得见。代价是主机侧略多一点空转请求（默认 25 秒一轮）。

## 三条安全口径（这一版刻意这么定，别悄悄放宽）

1. **hostId 就是主机的 client_id**（认证过的那个），**不接受请求里自报**——
   否则任何拿到令牌的客户端都能冒充别人的主机、把别人的手机流量收走。
2. **中继入口不额外要后端令牌**：真正鉴权的是 **ECHO 自己那枚手机令牌**（端到端），
   主机侧转发时会带上 `X-Echo-Relay`，让 ECHO 的鉴权**不再把回环当免检**。
   所以这里只做"必须带 Authorization 头"这一层粗筛 + 下面的白名单与限流。
   * 例外：`api/pair/*`（首次配对时手机还没有令牌，配对码本身就是凭据）。
3. **只放行 `/health` 与 `api/*`**，两处都判（这里判一次、主机侧再判一次）——
   中继绝不该变成"随便访问办公电脑上的任何服务"。

**默认关**：主机不开中继时，这台服务器上根本没有主机在 poll，手机那条路只会得到
503 `relay_host_offline`（而且什么都不会被转发）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import time
from collections import deque
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from server import errors

router = APIRouter(prefix="/v1/relay")

#: 手机那条路允许的路径（两处都判：这里 + 主机侧）。
ALLOWED_PREFIX = "api/"
ALLOWED_EXACT = ("health",)

#: 首次配对时手机还没有令牌 —— 这条**不需要** Authorization（配对码本身就是凭据）。
NO_TOKEN_REQUIRED_PREFIX = "api/pair/"

#: 单次请求体上限（16 kHz 单声道 5 分钟 ≈ 9.6 MB；再大就该分片了）。
MAX_BODY_BYTES = 24 * 1024 * 1024

#: 主机执行一次调用的上限。手机那条 `daily-review/submit` 实测要 120~180 秒，
#: 所以这里必须比客户端那侧（330 s）更宽 —— **让后端先放弃，而不是客户端先放弃**。
CALL_TIMEOUT_S = 600.0

#: 主机来 poll 时最多等多久（长轮询）。太短会空转、太长会让"主机在线"的判定变钝。
POLL_WAIT_S = 25.0

#: 多久没来 poll 就算这台主机掉线了（poll 间隔 25 s，给 3 倍余量）。
HOST_STALE_S = 80.0

#: 每个来源 IP 每分钟最多多少次中继调用（挡扫描器与失控客户端）。
RATE_PER_MIN = 120


class RelayError(errors.EchoError):
    """中继自己的错误（沿用后端统一的错误体：code + 人话）。

    ⚠️ `EchoError` 的构造是**位置参数** `(status, code, message)` —— 写反了不会报错，
    只会得到一个状态码与 code 互换的错误体（客户端按 code 分支，那就全走错）。
    所以这里只留一个工厂函数，全模块都走它。
    """

    def __init__(self, status: int, code: str, message: str, **kw):
        super().__init__(status, code, message, **kw)


def _human(code: str, message: str, status: int) -> RelayError:
    return RelayError(status, code, message)


def path_allowed(path: str) -> bool:
    """手机那条路允许的路径。**只认相对路径**（`/` 开头也容忍，但会被规范掉）。

    判据写在一处：这里说不行就是不行，主机侧还有一道（两边都判 = 一处漏了不会变成敞口）。

    ⚠️ **必须挡掉 `..`**：`api/../../etc/passwd` 前缀看着是 `api/`，但主机侧一旦
    原样拼进 URL，HTTP 客户端会把它规范化成 `/etc/passwd` —— 那就是从"只放行 api"
    变成"能读办公电脑上任何文件"。所以逐段看一眼，任何一段是 `..` 一律拒。
    """
    p = (path or "").replace("\\", "/").lstrip("/")
    if not p:
        return False
    parts = p.split("/")
    if any(seg in ("..", ".") for seg in parts):
        return False
    if p in ALLOWED_EXACT:
        return True
    return p.startswith(ALLOWED_PREFIX)


def _needs_token(path: str) -> bool:
    """这条中继请求要不要带令牌。

    两条**刻意的豁免**（各自都有理由，别顺手收紧）：
      * `health` —— ECHO 自己那条探针本来就不要令牌（`main.py` 的说明），
        中继上必须同一个口径，否则"手机连不上"与"令牌不对"又分不开了；
      * `api/pair/*` —— 首次配对时手机还没有令牌，配对码本身就是凭据。
    """
    p = (path or "").lstrip("/")
    if p in ALLOWED_EXACT:
        return False
    return not p.startswith(NO_TOKEN_REQUIRED_PREFIX)


# ---------------------------------------------------------------- 速率限制

class RateLimiter:
    """按 key（来源 IP）的滑动窗口。**纯内存、单进程**—— 与这台服务器的部署形态一致。

    为什么要有它：中继入口对着公网，一旦有人拿它当扫描器（哪怕每条都被 ECHO 401），
    也会把主机侧那条 poll 通道和日志刷爆。
    """

    def __init__(self, per_minute: int = RATE_PER_MIN, now=None):
        self.per_minute = max(1, int(per_minute))
        self._hits: Dict[str, deque] = {}
        self._now = now or time.monotonic

    def allow(self, key: str) -> bool:
        now = self._now()
        q = self._hits.setdefault(key, deque())
        cut = now - 60.0
        while q and q[0] < cut:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


# ---------------------------------------------------------------- 中枢

class Host:
    """一台已接入的主机（一次 poll 一轮）。"""

    def __init__(self, host_id: str, name: str = "", version: str = "",
                 client_id: str = "", now: float = 0.0):
        self.host_id = host_id
        self.name = name
        self.version = version
        self.client_id = client_id
        self.connected_at = now or time.time()
        self.last_seen = self.connected_at
        self.calls = 0
        #: 待主机取走的调用（主机没在 poll 时先排队；队太深说明主机卡了）。
        self.queue: deque = deque()
        self.wakeup = asyncio.Event()
        #: 等待结果的调用：callId → Future。
        self.pending: Dict[str, asyncio.Future] = {}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "hostId": self.host_id,
            "name": self.name,
            "version": self.version,
            "clientId": self.client_id,
            "connectedAt": self.connected_at,
            "lastSeen": self.last_seen,
            "calls": self.calls,
            "queued": len(self.queue),
            "pending": len(self.pending),
        }


#: 队列上限：主机长时间不来取（比如它自己卡住了），别把内存堆满。
MAX_QUEUED = 32


class RelayHub:
    """主机登记册 + 请求转发。**单进程内存态**（`Dockerfile` 的 CMD 就是一个进程）。

    ⚠️ 如果将来改成多 worker，这里必须换成外部登记（Redis 之类）——
    否则"手机连到 A 进程、主机挂在 B 进程"会出现**随机的 503**。
    """

    def __init__(self, call_timeout_s: float = CALL_TIMEOUT_S,
                 host_stale_s: float = HOST_STALE_S):
        self.call_timeout_s = float(call_timeout_s)
        self.host_stale_s = float(host_stale_s)
        self.hosts: Dict[str, Host] = {}
        self.limiter = RateLimiter()

    # ---- 主机侧 ------------------------------------------------------

    def touch(self, host_id: str, name: str = "", version: str = "",
              client_id: str = "") -> Host:
        host = self.hosts.get(host_id)
        if host is None:
            host = Host(host_id, name=name, version=version, client_id=client_id)
            self.hosts[host_id] = host
        else:
            if name:
                host.name = name
            if version:
                host.version = version
            if client_id:
                host.client_id = client_id
        host.last_seen = time.time()
        return host

    def live_host(self, host_id: str) -> Optional[Host]:
        host = self.hosts.get(host_id)
        if host is None:
            return None
        if time.time() - host.last_seen > self.host_stale_s:
            return None                      # 掉线的不算在线（别把请求堆给它）
        return host

    async def poll(self, host_id: str, wait_s: float = POLL_WAIT_S) -> list:
        """主机来取活：有就立刻给，没有就等到 `wait_s`（长轮询）。"""
        host = self.touch(host_id)
        if not host.queue:
            host.wakeup.clear()
            # 清完**再看一眼**：`clear()` 与提交侧的 `set()` 之间有个极窄的窗口，
            # 不补这一眼的话，那条调用要白等满一轮（表现是"偶尔慢 25 秒"）。
            if not host.queue:
                try:
                    await asyncio.wait_for(host.wakeup.wait(), timeout=max(0.1, wait_s))
                except asyncio.TimeoutError:
                    pass
            host.last_seen = time.time()
        out = []
        while host.queue:
            out.append(host.queue.popleft())
        return out

    def push_result(self, host_id: str, call_id: str, payload: Dict[str, Any]) -> bool:
        """主机交回结果。返回 False = 这条已经过期（手机那边早超时了）。"""
        host = self.hosts.get(host_id)
        if host is None:
            return False
        fut = host.pending.pop(call_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    async def submit(self, host_id: str, call: Dict[str, Any]) -> Dict[str, Any]:
        """手机的那条请求：入队 → 等主机结果（有上限）。"""
        host = self.live_host(host_id)
        if host is None:
            raise _human("relay_host_offline",
                         "这台电脑的中继没连上（或没开）：请在那台电脑上打开「中继」，"
                         "或确认它连得到这台服务器。", 503)
        if len(host.queue) >= MAX_QUEUED:
            raise _human("relay_busy",
                         "中继上排队的请求太多了（那台电脑可能卡住了），稍后再试。", 503)
        call_id = call["id"]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        host.pending[call_id] = fut
        host.queue.append(call)
        host.wakeup.set()
        try:
            return await asyncio.wait_for(fut, timeout=self.call_timeout_s)
        except asyncio.TimeoutError:
            host.pending.pop(call_id, None)
            raise _human("relay_timeout",
                         "等那台电脑回话超时了（它可能正忙或卡住）。", 504)

    def forget(self, host_id: str) -> None:
        self.hosts.pop(host_id, None)

    def snapshot(self) -> list:
        now = time.time()
        return [h.snapshot() for h in self.hosts.values()
                if now - h.last_seen <= self.host_stale_s]


#: 进程级唯一实例（与部署形态一致；多 worker 的注意见 `RelayHub` 的说明）。
HUB = RelayHub()


def _hub(request: Request) -> RelayHub:
    """取中枢：优先用 app.state 上的（单测可以塞一个进来），否则用进程级那个。"""
    hub = getattr(request.app.state, "relay_hub", None)
    return hub if hub is not None else HUB


def _client_ip(request: Request) -> str:
    """来源 IP：只看直连对端。**不信任 X-Forwarded-For** —— 它能被随便伪造，
    拿它做限流等于没限流（真正的来源判断由部署层的反向代理负责）。"""
    return (request.client.host if request.client else "") or "?"


# ---------------------------------------------------------------- 路由

@router.post("/status")
def relay_status(request: Request):
    """谁挂在中继上（面板/管理面/运维看这个）。**不含任何业务内容，但要凭据。**

    为什么是 POST + 要凭据（而不是顺手写成免凭据的 GET）：它含**主机名与 hostId**，
    而中继的 hostId 正是"往哪台电脑转发"的钥匙 —— 匿名公开等于给扫描器一张地图。
    （另一条硬约束：能力面的 GET 端点在这套契约里必须是**公开**的，
    见 `tests/test_backend_pairing.py::test_every_other_endpoint_rejects_an_anonymous_request`
    —— 那条用例会把"匿名可达的 GET"全列出来。要凭据就得走 POST。）
    """
    from server.routes import client_of
    client_of(request, need_scope="")
    return {"ok": True, "hosts": _hub(request).snapshot(),
            "callTimeoutSec": int(_hub(request).call_timeout_s)}


@router.post("/host/poll")
async def host_poll(request: Request):
    """主机来取活（长轮询）。body 可选：`{"wait": 25}`。

    ⚠️ **hostId 取自已认证的 client_id**（由调用方在 `main.py` 里注入 `request.state.client_id`），
    不接受请求体里自报 —— 否则任何人都能冒充别人的主机。
    """
    host_id = getattr(request.state, "relay_host_id", "") or ""
    if not host_id:
        raise _human("relay_unauthorized", "这台主机没有有效的后端凭据，不能挂中继。", 401)
    body = {}
    try:
        body = await request.json()
    except Exception:                                             # noqa: BLE001 - 空 body 合法
        body = {}
    wait_s = float(body.get("wait") or POLL_WAIT_S)
    wait_s = max(0.0, min(wait_s, POLL_WAIT_S * 2))
    hub = _hub(request)
    hub.touch(host_id, name=str(body.get("name") or ""),
              version=str(body.get("version") or ""),
              client_id=host_id)
    calls = await hub.poll(host_id, wait_s)
    host = hub.hosts.get(host_id)
    if host is not None:
        host.calls += len(calls)
    return {"ok": True, "hostId": host_id, "calls": calls,
            "timeoutSec": int(hub.call_timeout_s)}


@router.post("/host/result")
async def host_result(request: Request):
    """主机交回一次调用的结果。"""
    host_id = getattr(request.state, "relay_host_id", "") or ""
    if not host_id:
        raise _human("relay_unauthorized", "这台主机没有有效的后端凭据，不能挂中继。", 401)
    try:
        body = await request.json()
    except Exception:                                             # noqa: BLE001
        raise _human("relay_bad_result", "结果体不是 JSON。", 400)
    call_id = str(body.get("id") or "")
    if not call_id:
        raise _human("relay_bad_result", "结果里没有 id，认不出是哪一条。", 400)
    hub = _hub(request)
    hub.touch(host_id, client_id=host_id)
    accepted = hub.push_result(host_id, call_id, body)
    return {"ok": True, "accepted": bool(accepted)}


@router.api_route("/{host_id}/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
async def relay_call(host_id: str, path: str, request: Request):
    """手机那条路：把请求转交给那台主机，等它的结果。"""
    hub = _hub(request)
    if not path_allowed(path):
        raise _human("relay_forbidden_path",
                     "中继只放行 %s 与 %s*，其余一律不通。" % (ALLOWED_EXACT[0], ALLOWED_PREFIX),
                     403)
    auth = request.headers.get("authorization") or ""
    if _needs_token(path) and not auth.strip():
        raise _human("relay_unauthorized",
                     "这条中继请求没带令牌：请先在 App 里配对（拿到的令牌会自动带上）。", 401)
    if not hub.limiter.allow(_client_ip(request)):
        raise _human("relay_rate_limited", "中继请求太频繁，稍等一会儿。", 429)

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise _human("relay_too_large",
                     "这次上传太大了（上限 %d MB）：长音频请分段发。"
                     % (MAX_BODY_BYTES // (1024 * 1024)), 413)

    import uuid
    call = {
        "id": uuid.uuid4().hex,
        "method": request.method,
        "path": path.lstrip("/"),
        "query": request.url.query or "",
        "headers": {"authorization": auth,
                    "content-type": (request.headers.get("content-type") or "")},
        "body": base64.b64encode(raw).decode("ascii") if raw else "",
    }
    result = await hub.submit(host_id, call)
    if result.get("error"):
        raise _human("relay_host_error",
                     "那台电脑没能完成这次请求：%s" % result["error"], 502)
    try:
        payload = base64.b64decode(result.get("body") or "")
    except (binascii.Error, ValueError):
        raise _human("relay_bad_result", "那台电脑回的内容解不开（base64 坏了）。", 502)
    headers = {}
    ctype = ((result.get("headers") or {}).get("content-type") or "").strip()
    if ctype:
        headers["content-type"] = ctype
    return Response(content=payload, status_code=int(result.get("status") or 200),
                    headers=headers)


def install(app) -> None:
    """把中继挂到 app 上（`main.create_app` 调它）。

    同时给主机侧两条端点注入 `relay_host_id`（**取自已认证的 client_id**）——
    这件事必须在鉴权之后、路由之前做成一个薄薄的一层，见 `host_identity_middleware`。
    """
    app.include_router(router)
