# -*- coding: utf-8 -*-
"""relay_web.py — 经中继**只读**打开 DSH 标准版网页（2026-10-18 用户拍板）。

## 它解决什么

用户要在 App 首页安排一个入口，**经中继**打开 DSH 标准版网页 —— 手机与这台电脑
常常互相连不通（AP 隔离 / 跨网段 / 入站被拦），中继是唯一那条路。
沿用既有中继（`app/relay.py`），我们不新增隧道、不新增端口。

## 为什么入口落在 `api/relaydsh/<会话>/`

中继服务端（`server/relay.py::path_allowed`）**只放行 `/health` 与 `api/*`**，
而那一层已经部署在 echoserver 上、本轮又不许改（改了不部署 = 没改）。
所以网页入口只能借 `api/` 这条**已经存在**的放行通道。换个前缀，
线上中继会直接 403，网页根本到不了这台电脑。

## 为什么网页可以挂在子路径下

DSH 标准版前端的所有浏览器自有路由都是**文档相对**的（资源 `./assets/…`、
插件 `plugins/…`、RPC `api/<ns>/<method>`）。把页面挂在 `.../api/relaydsh/<会话>/`
之后，它们自然落到同一前缀下 —— **不需要改写 HTML/JS**。

（ECHO 自己的面板不行：它写死了 `/api/...` 绝对路径，而且它本身就是管理面 ——
所以这里**不**暴露面板，见下面的安全边界。）

## 安全边界：只读，不是把管理面暴露出去

* **路径白名单**：只放行页面真正要用的 `""`(index)、`assets/`、`plugins/`、
  `manifest.webmanifest`、`favicon*.svg`，以及 DSH 的 `api/` 通道；其余一律 403。
* **RPC 方法白名单**（`READ_RPC`）：`api/` 通道里只有**读方法**能过
  （`session/list`、`session/page`、`settings/describe` …）；
  `session/prompt`（发指令）、`settings/update`（改设置）、`session/create`、
  `terminal/*`、`credentials/*` 等**一律 403**。默认拒绝，不是"列几个坏的"。
* **HTTP 方法**：静态资源只 GET/HEAD；`api/` 下 POST 必须是白名单里的读 RPC。
  PUT/PATCH/DELETE 一律 403。
* **凭据不下发**：DSH 自己的浏览器登录 Cookie 由 ECHO 在本机用 harness 家目录里的密钥
  自铸（`harness_proc.secret_cookie()`），**只在本机 → 本机这一跳使用**；
  手机/AI 带来的 Cookie 不转发。
* **会话令牌**：一枚随机、12 小时过期的令牌放在**路径段**里（不是查询串）——
  页面加载后的相对请求会自然继承它；中继一停就作废（`relay.stop()` 换新令牌）。

## 谁访问、能看到什么、不能做什么

* 谁：拿到 `dshWebUrl` 的那台已配对设备（地址本身经鉴权接口下发）；
* 能：看 DSH 标准版的会话/工作区/设置**只读视图**；
* 不能：发指令、改设置、建/删会话、开终端、改凭据 —— 全部 403。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

#: 页面真正要用的静态路径（**精确匹配**）。
STATIC_EXACT = frozenset({
    "",                     # index.html（文档根）
    "index.html",
    "manifest.webmanifest",  # PWA manifest（DSH 用 .webmanifest，不是 ECHO 的 .json）
    "favicon.svg",
    "favicon-dark.svg",
})

#: 页面真正要用的静态路径（**前缀匹配**）。
STATIC_PREFIXES = ("assets/", "plugins/")

#: DSH RPC 的**只读方法白名单**（namespace → 允许的 method）。
#:
#: 为什么按"方法名"而不是"HTTP 动词"：DSH 的单次 RPC 全部走 `POST /api/<ns>/<method>`，
#: 用动词根本分不出"读会话列表"与"发一句话"。白名单是默认拒绝 ——
#: 新方法没登记就不通，宁可少给一个面板功能，也不让一条写入口漏出去。
READ_RPC = {
    "agentPresets": frozenset({"list", "read"}),
    "commands": frozenset({"list"}),
    "fileReferences": frozenset({"list"}),
    "goals": frozenset({"get"}),
    "job": frozenset({"list"}),
    "llm": frozenset({"discoverModels", "listConfigurableProviders", "listProviders"}),
    "messageFeedback": frozenset({"list"}),
    "permissionPresets": frozenset({"catalog"}),
    "pluginInventory": frozenset({"list"}),
    "pluginManager": frozenset({"inspect", "listBundles", "listPlugins",
                                "listVersionExemptions", "registries"}),
    "productAnalytics": frozenset({"enabled", "watchPolicy"}),
    "schedule": frozenset({"catalog", "history", "list"}),
    "session": frozenset({"attachment", "canOpenWorkspacePath", "list", "modelCatalog",
                          "page", "projections", "search", "workspacePathApplications"}),
    "settings": frozenset({"describe"}),
    "skills": frozenset({"list"}),
    "workspaceFiles": frozenset({"changes", "list", "read", "readBytes", "stat"}),
}

#: 单次上游请求的上限（秒）。中继那边一次能等 570 秒，这里留足余量。
UPSTREAM_TIMEOUT_S = 120.0

#: 不转发给上游的请求头（凭据不下发/不串味；Cookie 由我们自己赋）。
_DROP_HEADERS = ("cookie", "host", "authorization", "origin", "referer")


def clean_path(path: str):
    """规范化入口里的相对路径；含 `..`/`.` 段就返回 `None`（挡路径穿越）。

    与 `relay.path_allowed` 同一个理由：挡不住 `..` 就能从"只放行 assets"变成
    "能读这台电脑上任何文件"。
    """
    p = str(path or "").replace("\\", "/").lstrip("/")
    if any(seg in ("..", ".") for seg in p.split("/") if seg):
        return None
    return p


def path_allowed(p: str) -> bool:
    """这条**静态**路径在不在白名单里（`api/` 由 `rpc_readonly` 另判）。"""
    if p in STATIC_EXACT:
        return True
    return any(p.startswith(x) for x in STATIC_PREFIXES)


def rpc_readonly(body: bytes) -> bool:
    """`POST /api/…` 的请求体是不是一条**只读** RPC。

    认不出（不是 JSON / 没有 method / 方法没登记）一律 False = 拒。
    """
    try:
        msg = json.loads((body or b"").decode("utf-8") or "{}")
    except Exception:                                             # noqa: BLE001
        return False
    if not isinstance(msg, dict):
        return False
    method = str(msg.get("method") or "")
    namespace, _, name = method.partition("/")
    return name in READ_RPC.get(namespace, ())


def _json(status: int, detail: str) -> tuple:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def _cookie() -> str:
    """DSH 标准版的浏览器会话 Cookie（本机自铸，**不下发给浏览器**）。"""
    try:
        from app import harness_proc
        return str(harness_proc.secret_cookie() or "")
    except Exception:                                             # noqa: BLE001
        return ""


def _upstream_base() -> str:
    """上游 = 这台机器上的 DSH 标准版（独立 harness），不是公网地址。"""
    from app import harness_proc
    return harness_proc.base_url().rstrip("/")


def proxy(session: str, method: str, path: str, query: str, body: bytes,
          content_type: str = "") -> tuple:
    """执行一次只读网页请求，返回 `(status, content_type, body)`。

    不抛异常：所有失败都变成一句能直接显示给用户的话（中继那侧会把 body 原样带回）。
    """
    from app import relay as relay_mod

    # ① 会话：不是那条只读会话就 404（不告诉对方"令牌错在哪"）
    if not relay_mod.web_session_valid(session):
        return _json(404, "这条只读中继会话不存在或已过期：回 App/面板重新取一次地址")

    p = clean_path(path)
    if p is None:
        return _json(403, "路径不合法（不允许 .. 段）")

    verb = str(method or "GET").upper()

    # ② 路径与方法：静态只读、RPC 只放读方法
    if p == "api" or p.startswith("api/"):
        if verb in ("GET", "HEAD"):
            pass
        elif verb == "POST":
            if not rpc_readonly(body):
                return _json(403, "只读中继会话不允许写：发指令/改设置/建会话等一律拒绝")
        else:
            return _json(403, "只读中继会话只允许读（GET/HEAD 与只读 RPC）")
    else:
        if not path_allowed(p):
            return _json(403, "这条路径不在只读网页入口的白名单里")
        if verb not in ("GET", "HEAD"):
            return _json(403, "静态资源只允许 GET/HEAD")

    # ③ 上游凭据：拿不到就别假装能打开（如实说原因）
    cookie = _cookie()
    if not cookie:
        return _json(503, "读不到 DSH 标准版的浏览器会话密钥（harness 没跑过，"
                          "或它的家目录里没有 .credentials.yaml）")

    base = _upstream_base()
    url = base + "/" + p
    if query:
        url += "?" + str(query)

    data = body if (verb not in ("GET", "HEAD") and body) else None
    req = urllib.request.Request(url, data=data, method=verb)
    req.add_header("Cookie", cookie)
    if content_type:
        req.add_header("Content-Type", content_type)
    req.add_header("Accept", "*/*")
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return int(resp.status), ctype, (b"" if verb == "HEAD" else payload)
    except urllib.error.HTTPError as e:
        payload = e.read() or b""
        ctype = (e.headers.get("Content-Type") if e.headers else "") or "application/json"
        return int(e.code), ctype, payload
    except Exception as e:                                        # noqa: BLE001
        return _json(502, "连不上这台机器上的 DSH 标准版（%s）：看 data/logs/harness.log" % e)
