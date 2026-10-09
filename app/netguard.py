# -*- coding: utf-8 -*-
"""netguard.py — 只允许"本机回环"访问的来源守卫（防跨站读取 / 跨站写入 / DNS Rebinding）

背景（2026-09-13 安全审计 CRITICAL-1）
------------------------------------
ECHO 的本地 API 原本既不校验来源、CORS 又是 `allow_origins=["*"]`。后果是：你只要打开**任意一个网页**，
它的 JS 就能：

  * 读光数据：`GET /api/meetings` 列清单，再逐条 `GET /api/meetings/{id}/audio?seg=N` 把会议原始 wav、
    `/file?kind=transcript|summary` 把逐字转写与纪要整段拉走；
  * 反向写入：`POST /api/meeting/start` 让 **ECHO 服务进程**（不是浏览器）开始录音——不需要麦克风权限、
    浏览器也不会亮录音指示灯，随后再把音频下载走：一条可远程触发的窃听链路；
  * `POST /api/assistant/command` 让大模型执行任意指令；`POST /control/echo/stop` 直接停服；
    `POST /models/download` 拉几 GB 占满磁盘。

两层防护，缺一不可
-----------------
1. **CORS 收紧到回环来源**：浏览器才不会把跨域响应交给页面（`allow_origins=["*"]` 相当于"欢迎读取"）。
2. **本守卫按 Host / Origin 判定来源，非回环一律 403**：
   * 只收紧 CORS 还是挡不住"简单请求"（无预检的 POST，例如不带自定义头的
     `fetch('http://127.0.0.1:8970/api/meeting/start', {method:'POST'})`）——请求照样会被服务端执行，
     只是页面读不到响应。必须在服务端拒绝。
   * 同时封死 DNS Rebinding：攻击者用自己控制的域名，先解析到真实 IP 通过 CORS 校验，再把 DNS 改指
     127.0.0.1，浏览器就认为同源了。不看 Host 头就拦不住这条路。

判定规则
--------
* `Host` 的主机部分必须是 `127.0.0.1` / `localhost` / `::1`（端口随意）；
* 带 `Origin` 时必须也是回环来源（`http(s)://` + 回环主机 + 任意端口）；
* **不带 Origin 的请求放行**：curl、PowerShell、DSH 技能、原生 App 都不带 Origin；
* `Origin: null`（`file://` 页面、sandbox iframe、data: URL）**一律拒绝** —— 因此折叠条页面改成经
  `http://127.0.0.1:8970/web/rail.html` 同源加载，不再用 `file://`。

副作用（预期行为）：默认档下"用局域网 IP 从手机/别的机器访问面板"会失效 —— 这是**故意的**。

局域网档（2026-10-09：手机 / 手表触点）
--------------------------------------
`serverBindMode=lan` 是一次**显式选择**（默认仍是 `loopback`，见 `app/config.py`）。开它时：

* `app/main.py` 改绑 `0.0.0.0`（回环照旧可用），于是手机/手表能连上；
* 守卫**不是"放行一切"**，而是加一条**同时满足两个条件**的放行分支：
  ① Host 必须是**本机自己的地址**（`local_addresses()`，含用户显式指定的 `serverLanHost`）
     或本机主机名；② 对端 IP **不是公网地址**。两条都要判 ——
     只看 Host 挡不住"内网里一个恶意页面把域名解析到本机"（浏览器发的 Host 是攻击者的域名）；
     只看对端 IP 则连"内网里随便一个网页都能读"都挡不住。
* CORS 来源白名单跟着放宽到**本机私有地址字面量**（`is_allowed_origin()`，正则见
  `CORS_ORIGIN_REGEX`）。这不会变成"欢迎读取"：局域网上没有令牌的请求会先被
  `apiAuthEnabled` 的 401 挡下，而 `serverBindMode=lan` **强制联动** `apiAuthEnabled=true`
  （联动点只有一个：`app/config.py::Settings.update()`）。
* 公网仍然连不上（对端是公网地址 → 403）；局域网档**不替代**反向代理那条路
  （Caddy/Nginx + Basic Auth + TLS，见 docs/DEPLOY.md）—— 那是"要出网"时的正解。
"""
import ipaddress
import re
import socket
import threading
import time

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 回环主机名（Host 头里不带端口的那部分）
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 回环来源（浏览器 Origin 头）：协议 + 回环主机 + 可选端口
LOOPBACK_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_ORIGIN_RE = re.compile(LOOPBACK_ORIGIN_REGEX, re.IGNORECASE)

#: 私有网段字面量（IPv4 三段私有 + IPv6 链路本地/唯一本地）。**故意只认 IP 字面量**：
#: 主机名（`mypc:8970`）不进 CORS 白名单 —— 面板同源访问不需要 CORS，而"任意名字"等于放行一切。
_PRIVATE_HOST_BODY = (
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|\[(?:fe80:[0-9a-f:]+|f[cd][0-9a-f]{2}:[0-9a-f:]+)\]"
)
_PRIVATE_ORIGIN_RE = re.compile(r"^https?://(" + _PRIVATE_HOST_BODY + r")(?::\d+)?$", re.IGNORECASE)

#: CORS 允许的来源（**静态**：中间件在启动时装一次，所以这里必须把两种档的来源都写上）。
#: 放开到私有网段**不等于**让局域网裸奔：真正的判权在 `local_only_guard` 与 `apiAuthEnabled`，
#: 这一条只决定"浏览器能不能把响应交给页面"。
CORS_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\]|" + _PRIVATE_HOST_BODY + r")(:\d+)?$"

# CORS 允许的方法/头（不再用通配）
ALLOW_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
ALLOW_HEADERS = ["Content-Type", "Authorization"]


def _host_name(host_header: str) -> str:
    """Host 头 → 主机部分（去端口、去 IPv6 方括号、小写）。空/畸形返回空串。"""
    host = (host_header or "").strip()
    if not host:
        return ""
    if host.startswith("["):                      # [::1]:8970
        end = host.find("]")
        name = host[1:end] if end > 0 else host.strip("[]")
    else:
        name = host.rsplit(":", 1)[0] if ":" in host else host
    return name.strip().lower()


def is_loopback_host(host_header: str) -> bool:
    """Host 头（可能带端口，IPv6 可能带方括号）是否指向本机回环。"""
    return _host_name(host_header) in LOOPBACK_HOSTS


def is_loopback_origin(origin: str) -> bool:
    """浏览器 Origin 头是否为本机回环来源。"""
    return bool(_ORIGIN_RE.match((origin or "").strip()))


# ---------------------------------------------------------------- 局域网档（手机 / 手表触点）
def _setting(key: str, default=None):
    """读设置。**函数内 import**：本模块是中间件层，别在 import 期把 config 拖进来。"""
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def lan_mode_enabled() -> bool:
    """当前是不是"允许局域网"档（`serverBindMode=lan`）。"""
    return str(_setting("serverBindMode", "loopback") or "loopback").strip().lower() == "lan"


def is_private_ip(text) -> bool:
    """是不是**私有** IP 字面量（回环不算 —— 回环走另一条分支）。"""
    try:
        ip = ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return False
    return bool(ip.is_private) and not ip.is_loopback


#: 本机地址缓存：`local_addresses()` 里有一次 `getaddrinfo`（可能要问 DNS），
#: 而守卫**每个请求**都要问一次 —— 面板在轮询，不能每次都做系统解析。
#: TTL 30 秒：既不会把 DHCP 换地址后的旧值留太久，也不至于每请求一次。
_OWN_TTL = 30.0
_own_lock = threading.Lock()
_own_cache = {"at": 0.0, "names": frozenset()}


def local_addresses() -> list:
    """本机**非回环的私有 IPv4** 地址，第一个是"默认路由那块网卡"的地址。

    两个来源，按可靠性排序：

    ① UDP `connect()` 一个不可达目标 —— **不发任何包**，只是让内核按路由表选出出口网卡。
       多网卡机器（VPN / Hyper-V / WSL 虚拟网卡）上，这一条通常正是用户心里那个地址。
    ② `socket.getaddrinfo(hostname)` —— 兜底（没有默认路由时 ① 会失败）。

    两个都拿不到就返回**空表**：调用方必须能接受空表（**不许编一个地址出来** ——
    编出来的地址会让手机连到一台不存在的机器上，而现象是"配对成功但连不上"）。
    """
    found = []

    def add(value):
        ip = str(value or "").strip()
        if ip and ip not in found and is_private_ip(ip):
            found.append(ip)

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 9))
            add(probe.getsockname()[0])
        finally:
            probe.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    return found


def own_names() -> frozenset:
    """本机"算自己"的名字集合：自己的 IP + 主机名 + 用户显式配的 `serverLanHost`。

    这是局域网档 Host 判定的**唯一权威**（`is_lan_host()` 只查它）。
    """
    now = time.time()
    with _own_lock:
        if _own_cache["names"] and now - _own_cache["at"] < _OWN_TTL:
            return _own_cache["names"]
    names = set(local_addresses())
    for candidate in (socket.gethostname(), _setting("serverLanHost", "")):
        name = str(candidate or "").strip().lower()
        if name:
            names.add(name)
    frozen = frozenset(names)
    with _own_lock:
        _own_cache["names"] = frozen
        _own_cache["at"] = now
    return frozen


def forget_own_names() -> None:
    """丢掉地址缓存（设置改了、或测试里要重算）。"""
    with _own_lock:
        _own_cache["names"] = frozenset()
        _own_cache["at"] = 0.0


def is_lan_host(host_header: str) -> bool:
    """Host 头是不是**本机自己**（局域网档用）。

    注意与 `is_loopback_host()` 的分工：那个回答"是不是回环"，这个回答"是不是我自己的另一个地址"。
    **故意不改 `is_loopback_host` 的语义** —— 它是安全判据，放宽它等于把回环和局域网混成一个概念。
    """
    name = _host_name(host_header)
    if not name:
        return False
    if name in LOOPBACK_HOSTS:
        return False
    return name in own_names()


def is_public_peer(client) -> bool:
    """对端是不是**公网**地址（局域网档要拒它）。

    判不出对端（`client` 缺失、或不是 IP —— 例如 Starlette 的 TestClient 给的是 `"testclient"`）
    → 返回 **False（不拒）**：uvicorn 真跑起来时对端一定是 IP，而 Host 判定**照样生效**，
    所以这里不做"猜"的拦截（猜错会把正常访问挡在门外，而现象是莫名其妙的 403）。
    """
    try:
        peer = client.host
    except Exception:
        return False
    try:
        ip = ipaddress.ip_address(str(peer or "").strip())
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


def is_loopback_peer(peer) -> bool:
    """对端地址是不是回环。**判不出 → False（fail closed）**。

    方向与 `is_public_peer()` **故意相反**：那一条决定"要不要拒"，判不出就不拒（宁放不误杀）；
    这一条决定"要不要给豁免"（本机调用不必带令牌），判不出就**不给豁免** ——
    少给一次豁免只会多要一次令牌，而错给一次豁免就是把凭据免了。
    """
    try:
        ip = ipaddress.ip_address(str(peer or "").strip())
    except ValueError:
        return False
    return ip.is_loopback


def is_allowed_origin(origin: str) -> bool:
    """跨域来源是否可信：回环永远可信；局域网档下**只认本机自己的私有地址**。"""
    origin = (origin or "").strip()
    if is_loopback_origin(origin):
        return True
    if not lan_mode_enabled():
        return False
    match = _PRIVATE_ORIGIN_RE.match(origin)
    if not match:
        return False
    return is_lan_host(match.group(1))


def preferred_address() -> str:
    """配对时该告诉手机/手表哪个地址：用户显式配的优先，否则默认路由那块网卡。

    都拿不到返回空串 —— 调用方**必须**把"没地址"当成一个要如实告知的状态
    （配对的返回里 `baseUrl` 为空），而不是猜一个。
    """
    configured = str(_setting("serverLanHost", "") or "").strip()
    if configured:
        return configured
    found = local_addresses()
    return found[0] if found else ""


async def local_only_guard(request, call_next):
    """来源守卫：回环放行；局域网档下"Host 是自己的地址 且 对端不来自公网"才放行。

    非回环 Host 在默认档一律 403（同时封 DNS Rebinding）。
    """
    host = request.headers.get("host", "")
    if is_loopback_host(host):
        origin = request.headers.get("origin")
        if origin and not is_loopback_origin(origin):
            return JSONResponse({"detail": "forbidden: cross-site Origin"},
                                status_code=403)
        return await call_next(request)
    if lan_mode_enabled() and is_lan_host(host) and not is_public_peer(request.client):
        origin = request.headers.get("origin")
        if origin and not is_allowed_origin(origin):
            return JSONResponse({"detail": "forbidden: cross-site Origin"},
                                status_code=403)
        return await call_next(request)
    return JSONResponse({"detail": "forbidden: non-loopback Host (DNS rebinding guard)"},
                        status_code=403)


def install(app):
    """给 FastAPI 应用装上「CORS 只允许回环/本机私有来源」+「Host/Origin 守卫」。

    中间件顺序：后加的在外层，所以守卫先跑——非回环请求直接 403，不会进入业务路由，
    也就挡掉了"简单请求"式的跨站写入。
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_methods=ALLOW_METHODS,
        allow_headers=ALLOW_HEADERS,
    )
    app.middleware("http")(local_only_guard)
    return app
