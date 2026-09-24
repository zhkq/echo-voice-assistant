# -*- coding: utf-8 -*-
"""管理面（设计 §8.4）：**独立端口上的只读控制台**。

## 为什么是只读的

设计那五个页签要回答的是"**模型跑得怎么样、客户端有谁在连、谁调了什么**" ——
全是**看**。写动作（发码 / 撤销 / 禁用 / 改 scope / 换 secret / 配配额）**命令行已经齐了**
（`server/README.md` 那张表），而且那条路**刻意不打 HTTP、直接开库**：
给管理动作开一条免鉴权的内部端点，正是最容易变成漏洞的做法。

所以这一版刻意**不提供写端点**。要加写动作，先回答一个问题：
**它比"运维在那台机器上敲一条命令"多解决了什么？** 答不上来就别加。

## 与能力面严格隔离（设计 §8.4）

| | 能力面 | 管理面 |
|---|---|---|
| 路径 | `/v1/*` | `/admin/*`（页面）+ `/admin/api/*` |
| 鉴权 | 客户端凭据（`client_id:secret` → JWT） | **管理员用户名 + 密码** → 会话 |
| 端口 | 对客户端网段开放 | `server.admin_listen`（**独立端口**，只对运维网段开放） |
| 内容 | 只过音频与结果 | **只有元数据，永不显示内容** |

两条都用例钉着：`AdminConsoleTests.test_the_admin_app_has_no_capability_routes` 与
`..._the_capability_app_has_no_admin_routes`。合在一个端口上做路径级 ACL 更容易出错，
所以默认就是两个端口。

## 三件安全上的事

1. **口令用 `hashlib.scrypt`**（stdlib、内存硬）。设计写的是 argon2id —— 那是更好的选择，
   但它要引 `argon2-cffi`，而**服务端镜像的依赖面**不值得为它长一条（能用 stdlib 就不引）。
   这是一处**写明了的偏离**，不是"忘了"。要换 argon2id 时：`hash_password` / `verify_password`
   两个函数各换一行，旧哈希靠前缀（`scrypt$…`）区分，可平滑迁移。
2. **会话在进程内**（`SessionStore`）：重启即全部登出。管理面本来就不该水平扩，
   而"把会话写进库"要再加一张表（白名单要走评审）—— 不值。
3. **写请求要带 CSRF 令牌**（双提交）。现在唯一的 POST 是登录（它天然免 CSRF），
   但机制先立起来：将来加写动作时不会忘。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from server import __version__, errors

#: 口令哈希的参数（scrypt）。**这些都是"让人算不快"的参数**：
#: n=2^14 时一次验证约几十毫秒，暴力破解的代价因此抬高。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

#: 会话有效期（秒）。8 小时 = 一个工作日，过了要重新登。
SESSION_TTL_S = 8 * 3600

COOKIE_NAME = "echo_admin"


# ---------------------------------------------------------------- 口令

def hash_password(password: str, *, salt: str = "") -> str:
    """`scrypt$<salt_b64>$<hash_b64>`。格式自带参数前缀，将来换算法能平滑迁移。"""
    raw_salt = base64.b64decode(salt) if salt else os.urandom(16)
    dk = hashlib.scrypt(str(password).encode("utf-8"), salt=raw_salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return "scrypt$%s$%s" % (base64.b64encode(raw_salt).decode(),
                             base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    """**常数时间比较**，且认不出的格式一律 False（不猜）。"""
    text = str(stored or "")
    if not text.startswith("scrypt$"):
        return False
    parts = text.split("$")
    if len(parts) != 3:
        return False
    try:
        raw_salt = base64.b64decode(parts[1])
        want = base64.b64decode(parts[2])
    except Exception:
        return False
    # **空盐 / 空哈希要当场否掉**：`scrypt(dklen=0)` 会抛 `ValueError`，
    # 而"库里的哈希被改坏了"这条路径只该得到 `False`（进不去），不该是一段 traceback
    # —— 那会把"登录失败"变成"服务端 500"。实测被 `test_a_garbage_stored_value_...` 抓到。
    if not raw_salt or not want:
        return False
    try:
        dk = hashlib.scrypt(str(password).encode("utf-8"), salt=raw_salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(want))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(dk, want)


def new_password() -> str:
    """生成一个能念给人听的口令（建账号时只打印一次）。"""
    return "-".join(secrets.token_hex(3) for _ in range(3))


# ---------------------------------------------------------------- 会话

class SessionStore:
    """进程内的会话。**重启即全部登出** —— 管理面不该水平扩，理由见模块开头。"""

    def __init__(self, ttl_s: float = SESSION_TTL_S):
        self.ttl_s = float(ttl_s)
        self._by_token: Dict[str, Dict[str, Any]] = {}

    def create(self, username: str) -> Dict[str, Any]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._by_token[token] = {"username": username, "csrf": csrf,
                                 "expires": time.time() + self.ttl_s}
        return {"token": token, "csrf": csrf, "expiresIn": int(self.ttl_s)}

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        row = self._by_token.get(str(token or ""))
        if row is None:
            return None
        if row["expires"] < time.time():
            self._by_token.pop(str(token), None)
            return None
        return row

    def drop(self, token: str) -> None:
        self._by_token.pop(str(token or ""), None)

    def drop_user(self, username: str) -> int:
        """某人被禁用/删掉时，把他已有的会话一起清掉（下一个请求就掉线）。"""
        gone = [t for t, r in self._by_token.items() if r["username"] == username]
        for t in gone:
            self._by_token.pop(t, None)
        return len(gone)

    def count(self) -> int:
        return len(self._by_token)


# ---------------------------------------------------------------- 应用

def _state_of(request: Request):
    return request.app.state.echo


def create_admin_app(cfg, state) -> FastAPI:
    """造管理面应用。`state` 就是能力面那个 `routes.State`（读它的池/配额/metrics/库）。

    刻意**共用同一个 State**：那些状态本来就该只有一份（池、配额账本、metrics、
    调用记录），两个 app 各造一份会出现"管理面看到的忙闲与能力面不一样"。
    """
    sessions = SessionStore()
    throttle = _Throttle()
    app = FastAPI(title="ECHO admin console", version=__version__)
    app.state.echo = state
    app.state.sessions = sessions
    app.state.throttle = throttle
    api = APIRouter(prefix="/admin/api")

    # ---- 鉴权 ----

    def current_admin(request: Request) -> Dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME) or ""
        row = sessions.get(token)
        if row is None:
            raise errors.unauthorized("没登录或会话已过期")
        store = state.auth.store if state.auth is not None else None
        if store is not None:
            who = store.admin(row["username"])
            if who is None or int(who.get("disabled") or 0):
                # 账号被删/被禁用 → 手上的会话立刻作废（不然"禁用"要等 8 小时才生效）
                sessions.drop(token)
                raise errors.forbidden("这个管理员账号已被禁用")
        return {"username": row["username"], "csrf": row["csrf"]}

    def require_csrf(request: Request, who: Dict[str, Any]) -> None:
        """双提交校验。现在只有登录是 POST（免 CSRF），机制先立着，加写动作时不会忘。"""
        sent = request.headers.get("x-csrf-token") or ""
        if not sent or not hmac.compare_digest(sent, str(who.get("csrf") or "")):
            raise errors.forbidden("CSRF 令牌不对（刷新页面重试）")

    # ---- 登录 / 登出 ----

    @api.post("/login")
    async def login(request: Request):
        """用户名 + 口令换会话。**带失败退避**（免凭据端点都要防爆破，与 `/v1/pair` 同理）。"""
        store = state.auth.store if state.auth is not None else None
        if store is None:
            raise errors.auth_misconfigured("服务端没有可用的鉴权库")
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        username = str((payload or {}).get("username") or "")
        password = str((payload or {}).get("password") or "")
        source = _source_of(request)
        throttle.check(source)
        who = store.admin(username)
        if who is None or int(who.get("disabled") or 0) or not verify_password(password, who["password_hash"]):
            throttle.failed(source)
            # 不区分"用户名不存在"与"口令不对"：那等于给了一个枚举账号的接口
            raise errors.unauthorized("用户名或口令不对")
        throttle.succeeded(source)
        store.touch_admin_login(username)
        sess = sessions.create(username)
        body = JSONResponse({"ok": True, "username": username, "csrf": sess["csrf"],
                             "expiresIn": sess["expiresIn"]})
        body.set_cookie(COOKIE_NAME, sess["token"], httponly=True, samesite="strict",
                        max_age=int(sess["expiresIn"]), path="/admin")
        store.audit(username, "login", "")
        return body

    @api.post("/logout")
    async def logout(request: Request):
        who = current_admin(request)
        require_csrf(request, who)
        sessions.drop(request.cookies.get(COOKIE_NAME) or "")
        body = JSONResponse({"ok": True})
        body.delete_cookie(COOKIE_NAME, path="/admin")
        return body

    # ---- 五个页签要的数据（**全是只读**）----

    @api.get("/overview")
    def overview(request: Request):
        current_admin(request)
        snap = state.metrics.snapshot()
        return {
            "server": {"id": cfg.get("server.id", ""), "version": __version__,
                       "uptimeSeconds": int(time.time() - state.started),
                       "listen": str(cfg.get("server.listen", "")),
                       "tls": bool(str(cfg.get("server.tls.certfile", "") or ""))},
            "busy": state.admission.snapshot(),
            "vram": state.pool.vram(),
            "models": state.pool.status(),
            "quota": state.quota.snapshot(),
            "metrics": snap,
            "calls": (state.call_log.stats() if state.call_log is not None else {}),
            "sessions": sessions.count(),
        }

    @api.get("/models")
    def models(request: Request):
        current_admin(request)
        out = []
        for m in state.pool.status():
            spec = state.pool.spec(m.get("id")) if hasattr(state.pool, "spec") else None
            row = dict(m)
            row["slot"] = getattr(spec, "slot", "")
            row["resident"] = bool(getattr(spec, "resident", False))
            row["maxConcurrency"] = int(getattr(spec, "max_concurrency", 0) or 0)
            row["estVramMb"] = int(getattr(spec, "est_vram_mb", 0) or 0)
            row["vectorSpaceId"] = getattr(spec, "vector_space_id", "")
            out.append(row)
        return {"models": out, "vram": state.pool.vram()}

    @api.get("/clients")
    def clients(request: Request, hours: float = 24):
        current_admin(request)
        store = state.auth.store if state.auth is not None else None
        rows = store.clients() if store is not None else []
        since = time.time() - max(0.0, float(hours)) * 3600.0
        # 变量名避开 `FORBIDDEN_WORDS` 里那几个词（它们属于客户端的业务概念，
        # 服务端只该认识"能力槽与调用元数据"）。这里要表达的是"聚合"，所以用 `agg`。
        # 注意：那条护栏**连注释一起扫** —— 我第一版就是在注释里写出那个词而被抓住的。
        agg = store.calls_summary(since) if store is not None else {"groups": []}
        per_client: Dict[str, Dict[str, Any]] = {}
        for g in agg.get("groups", []):
            cur = per_client.setdefault(g["client_id"], {"calls": 0, "errors": 0,
                                                         "audioSeconds": 0.0})
            cur["calls"] += int(g["calls"] or 0)
            cur["errors"] += int(g["errors"] or 0)
            cur["audioSeconds"] += float(g["audio_seconds"] or 0)
        used = state.quota.snapshot().get("clients", {})
        out = []
        for r in rows:
            cid = r["client_id"]
            stat = per_client.get(cid, {})
            out.append({
                "clientId": cid, "name": r.get("name", ""), "scopes": r.get("scopes", ""),
                "disabled": bool(r.get("disabled")),
                "lastSeen": r.get("last_seen", 0),
                "dailyAudioMinutes": r.get("daily_audio_minutes", 0),
                "usedMinutesToday": used.get(cid, 0),
                "calls": stat.get("calls", 0), "errors": stat.get("errors", 0),
                "audioMinutes": round(float(stat.get("audioSeconds", 0)) / 60.0, 2),
            })
        return {"since": since, "clients": out,
                "totals": {"calls": agg.get("total", 0), "errors": agg.get("errors", 0)}}

    @api.get("/calls")
    def calls(request: Request, hours: float = 24, limit: int = 50):
        current_admin(request)
        store = state.auth.store if state.auth is not None else None
        if store is None:
            return {"aggregate": {"groups": [], "total": 0, "errors": 0}, "recent": []}
        since = time.time() - max(0.0, float(hours)) * 3600.0
        return {"aggregate": store.calls_summary(since),
                "recent": store.recent_calls(limit=limit)}

    @api.get("/inventory")
    def inventory(request: Request):
        """「存了什么」自证页（设计 §8.4/§8.5）：**把白名单渲染成实际状态**。

        它读的是**活着的库**，不是文档：哪些表真的存在、每张表有哪些列。
        这样"服务端不存内容"这句话可以当场核对，而不是一句承诺。

        **这里只报事实，不报判断。** 第一版我在这写了一个"内容列黑名单"，
        立刻被 `NoBusinessCouplingTests.test_no_business_words_in_source` 抓住 ——
        那个黑名单必然要**逐字列出那几个业务概念**，而服务端源码里不许出现它们
        （这正是那条护栏的全部意义：服务端只该认识能力槽）。
        所以"哪些列名算内容"这件事留在**测试**里
        （`AuthSchemaTests.test_no_column_is_named_like_content`），
        管理面只把真实的表与列摊开给人看。
        """
        current_admin(request)
        store = state.auth.store if state.auth is not None else None
        tables = store.columns() if store is not None else {}
        from server import store as store_mod
        return {
            "whitelist": list(store_mod.TABLE_WHITELIST),
            "tables": {t: list(cols) for t, cols in tables.items()},
            "statement": ("服务端只存**管理元数据**：客户端凭据、配对码、调用元数据"
                          "（时间/端点/模型/音频秒数/耗时/状态码）、管理员账号与动作审计。"
                          "**没有内容**。"),
            "contentCheck": ("列名是否像内容，由护栏用例逐列核对："
                             "tests/test_server_contract.py::AuthSchemaTests."
                             "test_no_column_is_named_like_content"),
            "tmpRoot": str(cfg.get("tmp.root", "")),
            "stateRoot": str(cfg.get("server.state_root", "")),
            "recentAudit": (store.recent_audit(20) if store is not None else []),
        }

    @api.get("/me")
    def me(request: Request):
        who = current_admin(request)
        return {"username": who["username"], "csrf": who["csrf"],
                "sessions": sessions.count()}

    app.include_router(api)
    app.add_exception_handler(errors.EchoError, _error_handler)

    @app.get("/admin/", response_class=HTMLResponse)
    @app.get("/admin", response_class=HTMLResponse)
    def index():
        return HTMLResponse(_page())

    return app


def _error_handler(_request: Request, exc):
    headers = {}
    if getattr(exc, "retry_after", None) is not None:
        headers["Retry-After"] = str(int(exc.retry_after))
    return JSONResponse(status_code=exc.status, content=exc.body(), headers=headers)


def _source_of(request: Request) -> str:
    fwd = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "unknown")


def parse_listen(text: str) -> tuple:
    """`"127.0.0.1:8901"` → `("127.0.0.1", 8901)`。空/坏值返回 `("", 0)`。"""
    raw = str(text or "").strip()
    if not raw:
        return ("", 0)
    host, _, port = raw.rpartition(":")
    try:
        return (host or "0.0.0.0", int(port))
    except ValueError:
        return ("", 0)


def start_admin_server(cfg, state, *, log=None):
    """在**另一个端口**上把管理面跑起来（同一个进程、后台线程）。

    由能力面的 lifespan 调用 —— 它得等 `state` 造好再起（管理面读的就是那份状态：
    池、配额账本、metrics、调用记录）。返回 uvicorn 的 `Server`（关的时候要停它），
    没配 `server.admin_listen` 时返回 `None`。

    **两条启动时的告警**（都是"配了但进不去/敞开"这类要靠日志才能发现的事）：
      * 一个管理员账号都没有 → 登录永远失败；
      * 监听地址不是回环 → 那台机器所在网段谁都能敲这个登录页（靠网络层挡，见设计 §8.4）。
    """
    host, port = parse_listen(cfg.get("server.admin_listen", ""))
    if not port:
        return None
    import threading
    import uvicorn
    app = create_admin_app(cfg, state)
    if log is not None:
        store = state.auth.store if state.auth is not None else None
        try:
            if store is not None and not store.admins():
                log.warning("管理面开着（%s:%d），但**一个管理员账号都没有** —— 登录永远会失败。"
                            "用 `python -m server.main --new-admin <名字>` 建一个。", host, port)
        except Exception:
            pass
        if host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("管理面监听在 %s:%d（不是回环）—— 那个网段里谁能连上谁就能看到"
                        "模型/客户端/调用元数据。请用防火墙只放运维网段（设计 §8.4）。",
                        host, port)
        log.info("管理面在 http://%s:%d/admin/ （**只读**；写动作走命令行）", host, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="echo-admin", daemon=True)
    thread.start()
    return server


class _Throttle:
    """登录失败退避（与 `/v1/pair` 的 `PairThrottle` 同一个形状，阈值更紧）。"""

    def __init__(self, max_failures: int = 5, window_s: float = 300.0):
        self.max_failures = int(max_failures)
        self.window_s = float(window_s)
        self._hits: Dict[str, list] = {}

    def _recent(self, source: str) -> list:
        now = time.time()
        hits = [t for t in self._hits.get(source, []) if now - t < self.window_s]
        self._hits[source] = hits
        return hits

    def check(self, source: str) -> None:
        hits = self._recent(source)
        if len(hits) >= self.max_failures:
            wait = int(self.window_s - (time.time() - hits[0])) + 1
            raise errors.rate_limited(max(1, wait), "登录失败次数太多，稍后再试")

    def failed(self, source: str) -> None:
        self._hits.setdefault(source, []).append(time.time())

    def succeeded(self, source: str) -> None:
        self._hits.pop(source, None)


def _page() -> str:
    """管理页面。**一个文件、无构建步骤**（服务端不进前端工具链）。

    它只做一件事：把 `/admin/api/*` 的结果画成五个页签。**没有任何写动作**。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ("<!doctype html><meta charset='utf-8'>"
                "<h1>管理面页面文件缺失</h1>"
                "<p>应为 <code>server/admin.html</code>。API 仍然可用："
                "<code>/admin/api/overview</code>。</p>")
