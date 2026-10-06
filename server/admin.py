# -*- coding: utf-8 -*-
"""管理面（设计 §8.4）：**独立端口上的控制台**（2026-09-25 起带写端点）。

## 为什么从只读变成可写，以及这一版的边界

只读那一版（2026-09-24）的判据是：五个页签要回答"模型跑得怎么样、客户端有谁在连、
谁调了什么" —— 全是**看**，而写动作命令行已经齐了。那条判据今天依然成立，
**变的是需求**：用户要"发授权"这类动作能在面板上完成（人在 GPU 那台机器之外时，
"去敲一条命令"往往是"先 SSH 进去"，那一步经常比动作本身还贵）。

所以写端点开了，但**每一条都带防护**（下面"写面的七道闸"），
而且**判断与落库一行都不在这里新写** —— 全部走 `server/ops.py`，
与命令行共用同一份实现（两道出口漂移过一次就够贵了）。

## 写面的七道闸（缺一条都算没做完）

1. **仍只监听 `server.admin_listen`**（出厂/本机是 `127.0.0.1:8901`）。
   能力面 `0.0.0.0:8900` 一个字节都没动。
2. **必须管理员会话**：没有有效会话一律 401，**绝不降级成"只读放行"**。
3. **CSRF / DNS-rebinding**：写请求要 (a) `Origin`（或 `Referer`）是本站、
   (b) 带一个非简单请求标志头 `X-ECHO-Admin`、(c) 带会话里的 `X-CSRF-Token`。
   为什么三样都要：cookie 是 `SameSite=Strict`，但 **DNS-rebinding 不受它保护**
   —— 攻击者把域名解析到 `127.0.0.1` 时，浏览器认为这是**同站**请求，
   cookie 照发。挡住它的是(a)：那种请求的 `Origin` 是攻击者的域名，对不上。
   (a) 里的"本站"= **`Origin` 的 host:port 等于这次请求自己的 `Host`，且 `Host`
   是回环名**（不是"等于配置里 `admin_listen` 的端口" —— 那条会把 `ssh -L` 隧道
   用户挡在门外，见 `same_site_write`）。
   (b) 的另一个作用见下面 `WRITE_HEADER` 的注释。
4. **审计**：每个写动作（**含失败**）落一条 `admin_audit`，操作者是**登录的管理员名**
   （不是字符串 `cli`）。失败把原因写进 `target`（表只有四列，加列要走 §8.5 评审）。
5. **危险动作二次确认**：撤销 / 轮换 secret / 作废配对码 —— 前端弹确认框，
   后端还要求请求体里带 `confirm`（值 = 目标 id 或 `true`），缺了或不对就是 400。
6. **秘密只回显一次**：轮换出的 secret、刚发的配对串只在**这一次响应**里出现，
   之后任何接口都不再返回明文（配对码库里本来就是哈希）。
7. **复用命令行那条路的实现**（`server/ops.py`），不在管理面里另写一套。

## 账号：面板只开「自己改自己口令」这一条路（2026-09-29 用户要求）

原来的立场是"**刻意不改账号**"：改账号等于"改谁能进这扇门"，而面板本身就在这扇门里
（一次会话劫持就能顺手把攻击者自己加成管理员）。这条**继续保留**，只开一个口子：

* ✅ `POST /admin/api/password`：**改自己的口令**，而且**必须同时给出当前口令**
  （`current` 验过才允许改）。于是"一次会话劫持"不足以改掉口令 —— 攻击者不知道当前口令。
  改的是**会话里的那个名字**，请求体里的 `username` 只用来**拒绝**（指着别人就 400）。
* ❌ 新建 / 删除 / 禁用 / 启用管理员、改**别人**的口令：**仍然只在命令行**
  （`--new-admin` / `--disable-admin` / `--delete-admin`）。页面上如实写着这句话
  （文案在 `/admin/api/admins` 的 `note` / `selfServiceNote` 里，页面渲染它们，
  不在 HTML 里另抄一份 —— 抄的那份迟早与后端漂开）。

改成功后**让其它会话失效**：这里没有"会话版本号"那种机制（会话整个就在
`SessionStore` 的进程内存里），所以用**既有手段**
`SessionStore.drop_user(username, keep=<当前令牌>)`：别的会话下一个请求就 401，
当前这个留着（不然刚改完就把自己踢回登录页，看着像改失败了）。

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
3. **写请求要带 CSRF 令牌**（双提交）**+ 非简单请求标志头 + 同站 Origin**，
   三样都由一道中间件统一把关（见 `_write_guard`）—— 默认拒绝，
   以后新加的写端点自动受保护，不会因为"忘了调某个 helper"而漏。
4. **会话 cookie 没有 `Secure`**：设计 §8.4 那张表写的是 `HttpOnly + Secure +
   SameSite=Strict`，而管理面**是明文 http**（回环端口，`start_admin_server` 不过 TLS）。
   给一个 http 页面设 `Secure` cookie，浏览器**根本不会存** —— 表现是"登录成功了，
   下一个请求又是 401"。所以这里刻意只设 `HttpOnly + SameSite=Strict`（照实际实现来），
   并在设计文档里把这处偏离写明；将来管理面走 https 时再补上 `Secure`。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from server.jsonx import SafeJSONResponse

from server import __version__, errors
from server import advertised as advertised_mod
from server import limits as limits_mod
from server import ops as ops_mod
from server import perfmon as perfmod

#: 写请求必须带的**非简单请求标志头**（值随便，非空即可）。
#:
#: 它的作用不是"再验一次身份"，而是让浏览器**必须先发 CORS 预检**：
#: 跨站页面能发 `<form method=post>`（简单请求，不带自定义头），也能发
#: `<img>`/`<script>` 这类拿不到响应的请求 —— 但它们**都发不出自定义头**。
#: 所以"必须有这个头"这一条，直接把"只靠受害者浏览器里的 cookie 就能提权"
#: 那条路堵死，而且它不依赖任何浏览器的默认行为。
WRITE_HEADER = "X-ECHO-Admin"

#: 审计里给"被闸门挡回来的写请求"用的动作名（真实动作名由端点自己记）。
GUARD_ACTION = "write-guard"

#: 「发授权」表单能填的有效期边界（秒）。**有上下界**是有意的：
#: 太短（<60 秒）等于发出去就是废码，太长（>7 天）等于把一张长期通行证留在库里。
MIN_PAIRING_TTL_S = 60
MAX_PAIRING_TTL_S = 7 * 24 * 3600

#: 只认这几个名字是"本站"。`parse_listen` 出的 `0.0.0.0`/`::`/`*` 是通配地址，
#: 它们不是浏览器地址栏里能出现的东西，所以换成这三个回环名。
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")

#: 判"这个主机名是不是回环"用的别名集：比上面多一个**不带方括号**的 `::1`
#: （`Host` 头里的 IPv6 按 RFC 要带方括号，别处偶尔会漏，认了不损失什么）。
_LOOPBACK_ALIASES = _LOOPBACK_NAMES + ("::1",)

#: 口令哈希的参数（scrypt）。**这些都是"让人算不快"的参数**：
#: n=2^14 时一次验证约几十毫秒，暴力破解的代价因此抬高。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

#: 会话有效期（秒）。8 小时 = 一个工作日，过了要重新登。
SESSION_TTL_S = 8 * 3600

COOKIE_NAME = "echo_admin"

#: 改口令时的长度下限 / 上限（**策略只有这一份**，页面那份是从 `/admins` 读过去显示的）。
#:
#: 下限 8：这是"人自己选的"口令，不是 `new_password()` 那种机器生成的 16+ 位随机串；
#: 门槛定得再高会把人逼到"写在便签上"。**上限 200**：scrypt 对超长输入照样要算，
#: 一个几 MB 的"口令"只会变成一次免费的 CPU 消耗。
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 200


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


def password_policy_error(new: str, current: str = "") -> str:
    """新口令合不合策略。返回**中文原因**（空串 = 通过）。

    **只在服务端判**：页面那份提前提示只是为了少一次往返，不构成保证 ——
    任何客户端都能直接构造请求体。
    """
    text = str(new if new is not None else "")
    if not text:
        return "新口令不能为空"
    if len(text) < MIN_PASSWORD_LEN:
        return "新口令至少 %d 位（口令短了挡不住在线猜测）" % MIN_PASSWORD_LEN
    if len(text) > MAX_PASSWORD_LEN:
        return "新口令最多 %d 位" % MAX_PASSWORD_LEN
    if current and hmac.compare_digest(text, str(current)):
        return "新口令不能与当前口令相同"
    return ""


def change_admin_password(store, username: str, current: str, new: str) -> Dict[str, Any]:
    """改**当前登录的这个管理员**的口令。返回摘要（**任何字段都不含口令**）。

    与命令行那条路的差别**只有一处**：`main._admin_cli --new-admin` 是"重置"，
    它生成一个随机口令并打印一次（能开库 = shell 权限，谁都能重置谁）；
    这里是"本人改本人的"，所以必须先过 `current` 这一关。
    落库走**同一个** `store.upsert_admin()`（同一个哈希函数、同一张表），
    不另开一条写路径 —— 两条路各写一遍，迟早有一处忘了换哈希算法。

    判定顺序是有意的：**先判"请求本身合不合法"（400），再验当前口令（403）**。
    与既有端点一致（缺字段/越界 = 400，身份不够 = 401/403），
    且不合法的请求不会白算一次 scrypt。
    """
    reason = password_policy_error(new, current)
    if reason:
        raise errors.bad_request(reason)
    row = store.admin(username) if store is not None else None
    if row is None or int(row.get("disabled") or 0):
        raise errors.forbidden("这个管理员账号已不可用")
    if not verify_password(current, row.get("password_hash") or ""):
        # 403 而不是 400：请求本身是合法的，**是身份不够** ——
        # 这正是"必须提供当前口令"这条闸门的落点（会话劫持者卡在这里）。
        raise errors.forbidden("当前口令不对（改口令要先证明你是本人）")
    store.upsert_admin(username, hash_password(new))
    return {"ok": True, "username": username}


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

    def drop_user(self, username: str, *, keep: str = "") -> int:
        """某人被禁用/删掉时，把他已有的会话一起清掉（下一个请求就掉线）。

        `keep` = **留一个令牌不清**。改自己口令时用它：口令变了，别的登录会话
        立刻失效（那正是"改口令"想达到的效果），而**手上这个留着** ——
        否则刚改完就把自己踢回登录页，用户会以为改失败了。
        """
        gone = [t for t, r in self._by_token.items()
                if r["username"] == username and t != str(keep or "")]
        for t in gone:
            self._by_token.pop(t, None)
        return len(gone)

    def count(self) -> int:
        return len(self._by_token)


# ---------------------------------------------------------------- 应用

def _state_of(request: Request):
    return request.app.state.echo


def create_admin_app(cfg, state, *, perf=None) -> FastAPI:
    """造管理面应用。`state` 就是能力面那个 `routes.State`（读它的池/配额/metrics/库）。

    刻意**共用同一个 State**：那些状态本来就该只有一份（池、配额账本、metrics、
    调用记录），两个 app 各造一份会出现"管理面看到的忙闲与能力面不一样"。

    `perf` 是「性能」页签那个采集器（`server/perfmon.py` 的 `PerfMonitor`）。
    **可以注入**：用例注入一个"采样函数全打桩"的替身，于是
    **测试永远不会真的去调 `nvidia-smi`**（本仓库对"测试碰真实资源"极敏感）。
    不传就造一个真的（采样只在按下「开始记录」且有人在看时才发生）。
    """
    sessions = SessionStore()
    throttle = _Throttle()
    #: 改口令那条路的失败退避。**与登录那个分开**：一次"当前口令记错了"不该把
    #: 登录也一起锁住（两个端点各自的尝试次数各自算，这也正是通行做法）。
    pwd_throttle = _Throttle()
    app = FastAPI(title="ECHO admin console", version=__version__,
                    default_response_class=SafeJSONResponse)
    app.state.echo = state
    app.state.sessions = sessions
    app.state.throttle = throttle
    app.state.pwd_throttle = pwd_throttle
    perf_monitor = perf if perf is not None else perfmod.PerfMonitor(
        server_sampler=perfmod.server_sampler_of(state))
    app.state.perf = perf_monitor
    api = APIRouter(prefix="/admin/api")

    # ---- 鉴权 ----

    def store_of() -> Optional[Any]:
        """能力面那个鉴权库（管理面读写的都是它）。没有 → None。"""
        return state.auth.store if state.auth is not None else None

    def current_admin(request: Request) -> Dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME) or ""
        row = sessions.get(token)
        if row is None:
            raise errors.unauthorized("没登录或会话已过期")
        store = store_of()
        if store is not None:
            who = store.admin(row["username"])
            if who is None or int(who.get("disabled") or 0):
                # 账号被删/被禁用 → 手上的会话立刻作废（不然"禁用"要等 8 小时才生效）
                sessions.drop(token)
                raise errors.forbidden("这个管理员账号已被禁用")
        return {"username": row["username"], "csrf": row["csrf"]}

    def require_csrf(request: Request, who: Dict[str, Any]) -> None:
        """双提交校验。写请求那道中间件已经查过一遍（默认拒绝），
        这里再查一次是**纵深防御**：某天中间件被改动/被挪走，登录态本身也还在。"""
        sent = request.headers.get("x-csrf-token") or ""
        if not sent or not hmac.compare_digest(sent, str(who.get("csrf") or "")):
            raise errors.forbidden("CSRF 令牌不对（刷新页面重试）")

    async def body_of(request: Request) -> Dict[str, Any]:
        """请求体（字典）。缺 body / 不是 JSON / 是别的类型 → 空字典。

        **不在这里报错**：具体哪个字段缺了由那个动作自己说（那样报错里能带上字段名），
        而且"缺 confirm"这类判断要在审计区间**之内**做（见 `as_write`）。
        """
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def check_confirm(payload: Dict[str, Any], target: str) -> None:
        """危险动作的二次确认。值 = 目标 id 或 `true`（也收字符串 `"true"`）。

        **后端也要查，不能只靠前端弹窗**：前端确认框挡的是"手滑"，
        挡不住"直接构造一个请求"—— 而这个字段的意义正是让"撤销/换 secret/作废码"
        这类动作必须**明确指名**它要动谁（写错 id 与写对 id 的请求长得不一样）。
        """
        got = (payload or {}).get("confirm")
        if got is True:
            return
        text = str(got if got is not None else "").strip()
        if text and text.lower() == "true":
            return
        if text and text == str(target):
            return
        raise errors.bad_request("危险操作要带 confirm（值 = 目标 id 或 true）")

    def as_write(request: Request, action: str, target: str, fn, *, confirm=None):
        """跑一个写动作：**会话 → 二次确认 → 执行 → 审计（成功与失败都留痕）**。

        操作者用**登录的管理员名**（`who["username"]`），不是字符串 `cli` ——
        审计的全部价值就在于回答"是**谁**动的"。命令行那条路记的是 `cli`
        （它没有"谁"这个概念，安全边界是 shell 权限）。

        顺序：闸门/确认失败也要留痕，所以确认检查放在 try 里面。
        """
        who = current_admin(request)
        store = store_of()
        try:
            if confirm is not None:
                check_confirm(confirm, target)
            out = fn(who)
        except errors.EchoError as exc:
            audit_write(store, who["username"], action + ".failed", target, exc)
            raise
        except Exception as exc:                               # pragma: no cover - 兜底
            audit_write(store, who["username"], action + ".failed", target, exc)
            raise
        audit_write(store, who["username"], action, target)
        return out

    # ---- 写请求的统一闸门（**默认拒绝**）----
    #
    # 为什么是中间件而不是"每个端点开头调一个 helper"：写端点是**会长的**
    # （这一批加了七个），而"新加一个端点时别忘了调某某"是靠人记住的事。
    # 中间件在这里，新的写端点**自动**受保护；漏掉的反而是"忘了给只读端点开口子"
    # —— 那种错一测就出来，而且不会静默提权。

    @app.middleware("http")
    async def _write_guard(request: Request, call_next):
        path = request.url.path
        is_write = (request.method not in ("GET", "HEAD", "OPTIONS")
                    and path.startswith("/admin/api"))
        if is_write and path != "/admin/api/login":
            denied = check_write_request(request, cfg, sessions)
            if denied is not None:
                # 失败也留痕（但**不知道是谁**的请求不留 —— 那会是扫描器刷表）。
                row = sessions.get(request.cookies.get(COOKIE_NAME) or "")
                if row is not None:
                    audit_write(store_of(), row.get("username") or "",
                                GUARD_ACTION, "%s %s" % (request.method, path), denied)
                return JSONResponse(status_code=denied.status, content=denied.body())
        return await call_next(request)

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
            # 用同一个 `client_view`（**白名单式**）—— 列表与详情两处各挑一遍字段，
            # 迟早会有一处把 `secret_hash` 带出去。
            row = client_view(r)
            row.update({
                "usedMinutesToday": used.get(cid, 0),
                "calls": stat.get("calls", 0), "errors": stat.get("errors", 0),
                "audioMinutes": round(float(stat.get("audioSeconds", 0)) / 60.0, 2),
            })
            out.append(row)
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

    @api.get("/admins")
    def admins(request: Request):
        """管理员账号清单（**只读**）+ 「我能做什么」的自述。

        管理面**不改别人**的账号：新建 / 删除 / 禁用 / 启用、以及改别人的口令
        仍然只在命令行 —— 改账号是"改谁能进这扇门"，而面板本身就在这扇门里，
        让面板改账号等于给一次会话劫持配一个提权出口。
        唯一开的口子是**改自己的口令**（见 `POST /admin/api/password`），
        而且必须带当前口令；`selfServiceNote` 就是给页面显示的那句话，
        `passwordPolicy` 是**策略的唯一来源**（页面拿它渲染"至少 N 位"，
        不另抄一遍数字）。
        """
        who = current_admin(request)
        store = store_of()
        rows = store.admins() if store is not None else []
        return {"admins": [
            {"username": r.get("username", ""), "disabled": bool(r.get("disabled")),
             "createdAt": float(r.get("created_at") or 0),
             "lastLogin": float(r.get("last_login") or 0)} for r in rows],
            "self": who["username"],
            "note": ("账号的增删改只在命令行（--new-admin / --disable-admin / "
                     "--delete-admin）；面板只能改**自己**的口令，而且要先给出当前口令。"),
            "selfServiceNote": ("新增 / 删除 / 禁用管理员请用命令行 —— 那是「谁能进门」的事，"
                               "不该在门里做；一次会话劫持也不足以改掉口令"
                               "（攻击者不知道当前口令）。"),
            "passwordPolicy": {"minLength": MIN_PASSWORD_LEN,
                               "maxLength": MAX_PASSWORD_LEN,
                               "selfService": True,
                               "otherSessionsDropped": True}}

    # ---- 我的口令（写：只改自己，**必须带当前口令**）----
    #
    # 这条是"刻意不改账号"那个立场上唯一的例外，理由与边界见模块开头。
    # 走 `as_write`：会话 + 同站 Origin + `X-ECHO-Admin` + CSRF + 审计
    # （成功 `password-change`、失败 `password-change.failed` 带中文原因）。

    @api.post("/password")
    async def change_my_password(request: Request):
        """改**当前登录的这个管理员**的口令。body：`{"current": "...", "new": "..."}`。

        错误码（与既有端点一个口径）：

        * 没会话 → 401；错 Origin / 缺 `X-ECHO-Admin` / 缺 CSRF → 403（写得**不合格**的
          `new` 也一样，闸门在进端点之前就拦）；
        * `new` 空 / 短于下限 / 超过上限 / 与 `current` 相同 → **400 + 中文原因**；
        * **`current` 不对 → 403**（不是 400：请求本身合法，是身份不够）——
          这条就是"会话劫持改不了口令"的落点；
        * 请求体里带**别人**的用户名 → 400，一口回绝（见下面 `target` 那段）。

        成功 → 200 `{"ok": true, "username": ..., "sessionsRevoked": N}`，
        并且**让其它会话失效**（当前这个留着，见 `SessionStore.drop_user`）。
        响应里**任何字段都不含口令**（哈希、明文都不）。
        """
        payload = await body_of(request)
        source = _source_of(request)

        def fn(who):
            # 请求体里的 `username` **不用来选题**，只用来拒绝：
            # 页面没有这个入口，所以"指着一个别人的名字"只可能是手搓的请求
            # （或者某个客户端理解错了）—— 那必须被明说，而不是被静默忽略。
            target = str(payload.get("username") or "").strip()
            if target and target != who["username"]:
                raise errors.bad_request(
                    "改口令只能改当前登录的这个管理员（请求里给的是 %s）" % target)
            # 与登录端点同源的失败退避（**各算各的**）：会话被劫持之后，
            # "拿这个端点当 `current` 口令的在线爆破口"是剩下的那点风险。
            pwd_throttle.check(source)
            try:
                out = change_admin_password(store_of(), who["username"],
                                            str(payload.get("current") or ""),
                                            str(payload.get("new") or ""))
            except errors.EchoError as exc:
                if exc.status == 403 and exc.code == "forbidden":
                    pwd_throttle.failed(source)
                raise
            pwd_throttle.succeeded(source)
            # 口令一变，**别的登录会话立刻失效**（当前这个留着）。
            out["sessionsRevoked"] = sessions.drop_user(
                who["username"], keep=request.cookies.get(COOKIE_NAME) or "")
            return out

        # 审计的 `target` 写"改的是谁" = 会话里那个名字（as_write 自己会再认一次身份）。
        return as_write(request, "password-change", current_admin(request)["username"], fn)

    @api.get("/clients/{client_id}")
    def client_detail(request: Request, client_id: str):
        """一个客户端的详情。**没有 `secret_hash`**（见 `client_view`）。"""
        current_admin(request)
        store = store_of()
        row = store.client(client_id) if store is not None else None
        if row is None:
            raise errors.EchoError(404, "client_not_found", "没有这个客户端", detail=client_id)
        used = state.quota.snapshot().get("clients", {})
        view = client_view(row)
        view["usedMinutesToday"] = used.get(client_id, 0)
        # **名字说清楚它是什么**：这不是"今日调用数"（那个在列表里，按小时窗口算），
        # 而是最近 200 条里属于它的那几条 —— 面板按需展开时看的就是这个。
        view["recentCalls"] = len(store.recent_calls(limit=200, client_id=client_id))
        return view

    # ---- 发授权：配对码（写）----

    @api.get("/pairing-codes")
    def pairing_codes(request: Request):
        """待用的配对码。**明文永远不在这里** —— 库里只有哈希（设计 §7.4 约定 2）。

        这里给的是**能用的**码：`ops.pending_pairing_codes()` 已经把"已消费"、"已过期"
        与**本机自动配对那张**都排除掉了（判据只有一份，命令行 `--list-codes` 用的是同一个
        函数）。2026-09-29 用户实测的 bug 就是这张表里出现了「已过期」（甚至已经兑过的）的码
        —— 徽章写着「已过期」却列在「待用」，标题与内容自相矛盾。
        2026-10-06 用户又给了一条判据：*"发出后没有被用的码才叫待用"* ——
        本机那张码是机器对机器的握手凭据（客户端一旦有凭据就跳过配对，永远不会来兑换），
        挂在"待用"里是假的，所以也不再列。
        """
        current_admin(request)
        return {"codes": ops_mod.pending_pairing_codes(store_of()),
                "defaultTtlSeconds": int(cfg.get("auth.pairing_ttl_s", 900)),
                "minTtlSeconds": MIN_PAIRING_TTL_S, "maxTtlSeconds": MAX_PAIRING_TTL_S,
                "note": "这张表里只有**能用的**码（未用过、未过期）：用过的码在 /v1/pair 兑走的"
                        "那一刻就从库里删掉了，过期的也不列在这里。"
                        "本机自动配对用的那张也不列 —— 它不是给人抄的，客户端自己会走本机"
                        "配对文件，永远不会来兑换它。"
                        "想知道某张码什么时候被谁兑走了，看「存了什么」页签里的审计"
                        "（action = pair-redeem）。配对码只存哈希 —— 明文只在发出去的那一刻"
                        "出现过一次，这里看不到。"}

    @api.post("/pairing-codes")
    async def issue_pairing_code(request: Request):
        """**发授权**：生成一张带名字 / scopes / 有效期的配对码。

        响应里那个 `pairingCode.url` 是**一次性**明文（含证书指纹的整串）：
        只出现这一次，之后任何接口都不会再给（库里存的是哈希）。
        `confirm` 不是必需的 —— 发码是"多了一张待用的码"，不是破坏性动作，
        而且撤销它只需要再点一次「作废」。

        `advertisedHost`（可选）= **这一张码**里公布的地址，盖过全局那一项 ——
        同一台后端同时服务本机与远程时，本机那张给 `127.0.0.1`、远程那张给局域网 IP
        （串本身带 host，两张码互不影响）。空 = 用「对外公布地址」那一项。
        """
        payload = await body_of(request)
        name = str(payload.get("name") or "")
        scopes = str(payload.get("scopes") or "")
        note = str(payload.get("createdBy") or payload.get("note") or "")
        per_issue = payload.get("advertisedHost")

        def fn(who):
            return {"ok": True, "pairingCode": ops_mod.issue_pairing_code(
                cfg, state.auth, name=name, scopes=scopes,
                ttl_s=parse_ttl(payload.get("ttlSeconds"),
                                float(cfg.get("auth.pairing_ttl_s", 900))),
                created_by=note or ("admin:" + str(who["username"])),
                advertised=per_issue)}

        return as_write(request, "issue-pairing-code", name or "(未命名客户端)", fn)

    @api.delete("/pairing-codes/{code_id}")
    async def revoke_pairing_code(request: Request, code_id: str):
        """作废一张**未使用**的配对码（**危险动作**：要带 `confirm`）。"""
        payload = await body_of(request)
        return as_write(request, "delete-pairing-code", code_id,
                        lambda who: ops_mod.revoke_pairing_code(store_of(), code_id),
                        confirm=payload)

    # ---- 客户端管理（写）----

    @api.post("/clients/{client_id}/disable")
    async def disable_client(request: Request, client_id: str):
        """禁用：它会收到 403「认识你但不许用」，凭据本身**没有失效**。"""
        return as_write(request, "disable", client_id,
                        lambda who: ops_mod.set_client_disabled(cfg, state.auth,
                                                                client_id, True))

    @api.post("/clients/{client_id}/enable")
    async def enable_client(request: Request, client_id: str):
        """启用：**它手上那个令牌直接就能用**（版本号没动，见 `ops.set_client_disabled`）。"""
        return as_write(request, "enable", client_id,
                        lambda who: ops_mod.set_client_disabled(cfg, state.auth,
                                                                client_id, False))

    @api.post("/clients/{client_id}/revoke")
    async def revoke_client(request: Request, client_id: str):
        """撤销（**危险动作**：要带 `confirm`）：`token_version + 1`，令牌立刻失效。

        管理面与能力面在**同一个进程**、共用同一个 `Auth` 缓存，
        所以这里改完**当场**生效（不是"最多 5 秒"，那是命令行/多实例那条路）。
        """
        payload = await body_of(request)
        return as_write(request, "revoke", client_id,
                        lambda who: ops_mod.revoke_client(cfg, state.auth, client_id),
                        confirm=payload)

    @api.post("/clients/{client_id}/scopes")
    async def set_client_scopes(request: Request, client_id: str):
        """改权限（`scopes` 必须给，空串 = 不限）。**下一个请求就生效**。"""
        payload = await body_of(request)

        def fn(who):
            if "scopes" not in payload:
                raise errors.bad_request("要带 scopes 字段（空串 = 不限）")
            return ops_mod.set_client_scopes(cfg, state.auth, client_id,
                                             str(payload.get("scopes") or ""))

        return as_write(request, "set-scopes", client_id, fn)

    @api.post("/clients/{client_id}/quota")
    async def set_client_quota(request: Request, client_id: str):
        """改每日音频分钟数（`dailyAudioMinutes`，0 = 用全局默认）。

        **不清零今天的已用量** —— 额度按自然日算，改上限不该变成"送你一次重置"。
        """
        payload = await body_of(request)

        def fn(who):
            if "dailyAudioMinutes" not in payload:
                raise errors.bad_request("要带 dailyAudioMinutes 字段（0 = 用全局默认）")
            try:
                minutes = float(payload.get("dailyAudioMinutes"))
            except (TypeError, ValueError):
                raise errors.bad_request("dailyAudioMinutes 要是分钟数")
            if minutes < 0:
                raise errors.bad_request("dailyAudioMinutes 不能是负数")
            return ops_mod.set_client_quota(cfg, state.auth, client_id, minutes)

        return as_write(request, "set-quota", client_id, fn)

    @api.post("/clients/{client_id}/rotate-secret")
    async def rotate_client_secret(request: Request, client_id: str):
        """轮换 secret（**危险动作**：要带 `confirm`）。

        响应里的新 secret **只出现这一次**：库里存的是哈希，之后任何接口都不会再给。
        同时 `token_version + 1` —— 它原来那些令牌立刻全失效（泄漏时最想断的就是它们）。

        `graceHours > 0` 时旧 secret 在宽限期内仍能换令牌（例行轮换不打断客户端），
        并**顺带再发一张配对码**把那台机器接回来 —— 与命令行的 `--grace-hours`
        走的是同一条路。⚠️ 宽限期**不能用于 secret 泄漏**（旧 secret 照样进得来）。
        """
        payload = await body_of(request)

        def fn(who):
            raw = payload.get("graceHours")
            grace = 0.0
            if raw not in (None, ""):
                try:
                    grace = float(raw)
                except (TypeError, ValueError):
                    raise errors.bad_request("graceHours 要是小时数")
                if grace < 0 or grace > 24 * 30:
                    raise errors.bad_request("graceHours 要在 0 ~ 720 之间")
            out = ops_mod.rotate_client_secret(cfg, state.auth, client_id, grace_hours=grace)
            if grace > 0:
                out["pairingCode"] = ops_mod.issue_pairing_code(
                    cfg, state.auth, name=out.get("name") or client_id,
                    scopes=out.get("scopes") or "",
                    created_by="rotate:" + str(who["username"]))
            return out

        return as_write(request, "rotate-secret", client_id, fn, confirm=payload)

    # ---- 性能监控（写：开始 / 停止记录；读：状态与**增量**取点）----
    #
    # 采集与缓冲全在 `server/perfmon.py` 里（**只在内存里，一个字节都不落盘** ——
    # 容器根文件系统是只读的，`read_only: true`）。管理面这里只做两件事：
    #   ① 写端点走 `as_write`：会话 + 同站 Origin + `X-ECHO-Admin` + CSRF + 审计，
    #      与其它写动作**同一套闸门**（没有新开免检端点）；
    #   ② 读端点只需要登录（与 `/overview` 那半边一致，见 ReadOnlyNotLockedTests）。
    #
    # 「有人在看」是靠**这两条读端点被调用**来判定的：页面一关、轮询一停，
    # `viewerTtlSeconds` 一到，采集线程自己退出（不再空跑）。`recording` 那个标志
    # 留着 —— 页面再打开就接着采，不必重新按一次「开始记录」。
    # 于是"页面关闭/切走时停止采样"这件事**不需要浏览器发关闭请求**
    # （那种请求还得带自定义头，`sendBeacon` 发不出来）。

    @api.post("/perf/start")
    async def perf_start(request: Request):
        """**开始记录**：1 秒一个点，只放内存。

        可选 `windowSeconds`（60 ~ 7200）换窗口；**换了窗口等于丢掉旧点**
        （环形容量变了，序列号接着往下数，客户端手上那个 `since` 不会失效）。
        """
        payload = await body_of(request)

        def fn(who):
            raw = payload.get("windowSeconds")
            target = None
            if raw not in (None, ""):
                try:
                    target = float(raw)
                except (TypeError, ValueError):
                    raise errors.bad_request("windowSeconds 要是秒数")
            try:
                return perf_monitor.start(window_s=target)
            except ValueError as exc:
                raise errors.bad_request(str(exc))

        return as_write(request, "perf-start", "perf", fn)

    @api.post("/perf/stop")
    async def perf_stop(request: Request):
        """停止记录。**已有的点不删**（图还在，只是不再长）。"""
        return as_write(request, "perf-stop", "perf", lambda who: perf_monitor.stop())

    # ---- 运行参数（读：登录即可；写：与其它写动作同一套闸门 + 审计）----
    #
    # 2026-09-29 用户要求："把并发能力提到 6，而且这个应该由管理员在管理页面上配"
    # （不用改环境变量、不用重启）。所以这里加一对端点：
    #   * `GET  /admin/api/limits` —— 生效值 + **每个值的来源**（管理面/环境变量/默认）
    #   * `POST /admin/api/limits` —— 保存（走 `as_write`：会话 + 同站 Origin +
    #     `X-ECHO-Admin` + CSRF + 审计 `limits-set`）
    # 校验与落库都在 `server/limits.py` 里一份（页面与将来的命令行共用），
    # 这里只做"怎么问、怎么回 JSON"。
    #
    # ⚠️ **优先级写死在 `server/limits.py`：管理面配置 > 环境变量 > 出厂默认。**
    # 管理面存下的值盖过 `ECHO_MAX_CONCURRENT` —— 不然 compose 里那个默认变量会让
    # 页面上的改动静默失效（见那个模块开头的说明）。`notes` 会把这件事如实说出来。

    @api.get("/limits")
    def get_limits(request: Request):
        """此刻生效的运行参数 + 来源 + 取值范围（+ "env 被管理面盖住"的如实提示）。"""
        current_admin(request)
        return _no_store(limits_mod.view(cfg, store_of()))

    @api.post("/limits")
    async def set_limits(request: Request):
        """保存运行参数。越界 / 非数 → 400 + 中文原因（**不静默夹紧**），值不变。

        保存**当场生效**：`limits.set_limits` 写的就是能力面那份 `cfg`，
        而闸门（`Admission.current`）与 `/v1/capabilities` 每次现读它。
        持久化在同一个动作里（state 卷的 `server_limits` 表），重启后还在。
        """
        payload = await body_of(request)

        def fn(who):
            got = limits_mod.set_limits(cfg, store_of(), payload,
                                        updated_by=str(who.get("username") or ""))
            out = limits_mod.view(cfg, store_of())
            out["ok"] = True
            out["saved"] = {k: got[k] for k in got}
            return out

        return as_write(request, "limits-set", limits_mod.audit_target(payload), fn)

    # ---- 对外公布地址（读：登录即可；写：与其它写动作同一套闸门 + 审计）----
    #
    # 2026-09-30 用户实测的 bug：容器里发出来的配对串带的是 **Docker 网桥地址**
    # （`echo://pair?host=http://172.18.0.2:8900&code=…`），而"同事真正连得到哪台机器"
    # **只有部署的人知道** —— 以前每次都靠人手工改串，换个人第一次用必然踩。
    # 所以这里开一对端点，值存在 state 卷（重启还在），落在
    # `cfg.raw["server"]["advertised_host"]` —— 发码那条路（`ops.resolve_advertised`）
    # 下一次就现读到（**热生效**，与运行参数同一个机制）。
    #
    # ⚠️ 这一项是「**每次发码时想公布的地址**」，不是一次性全局常量：
    #   ① 同事要连 → 局域网 IP；② 本机后端 → 127.0.0.1；③ 两边都服务 → 发码时各写各的
    #   （`POST /admin/api/pairing-codes` 的 `advertisedHost` 字段）。
    # `formNote` 就是这三句话（**由后端下发**，页面不另抄一份）。

    @api.get("/advertised-host")
    def get_advertised_host(request: Request):
        """当前的对外公布地址 + **来源**（admin / env / config / auto）。"""
        current_admin(request)
        return advertised_mod.view(cfg, store_of())

    @api.post("/advertised-host")
    async def set_advertised_host(request: Request):
        """保存（或清空）对外公布地址。空串 = 清空 → 回到自动探测。

        不合法 → **400 + 中文原因**（不静默忽略：静默忽略会让人以为配了、
        其实还在探测 —— 那正是这次要修的那个 bug 的形态）。
        保存**当场生效**（下一次发码就用它），并落在 state 卷里，重启后还在。
        """
        payload = await body_of(request)

        def fn(who):
            value = advertised_mod.set_advertised(cfg, store_of(), payload,
                                                  updated_by=str(who.get("username") or ""))
            out = advertised_mod.view(cfg, store_of())
            out["ok"] = True
            out["saved"] = value
            return out

        return as_write(request, "advertised-host-set",
                        advertised_mod.audit_target(payload), fn)

    @api.get("/perf/state")
    def perf_state(request: Request):
        """是否在记录 + 采样间隔 + 已有点数。**这一次调用本身就算"有人在看"**。"""
        current_admin(request)
        return _no_store(perf_monitor.state())

    @api.get("/perf/points")
    def perf_points(request: Request, since: str = "", limit: int = 0):
        """**增量取点**：`since` 给序号（或 ≥1e9 的 unix 时间戳）。

        * 空 `since` → 回**最近** `limit` 个（页面首屏）；
        * 给了 `since` → 只回它**之后**的点，响应里带 `nextSince`（下一批从这里取）；
        * 一批最多 `limit` 个（默认 300，上限 2000），被截断时 `truncated=true`。
        """
        current_admin(request)
        try:
            return _no_store(perf_monitor.points(since=since, limit=(limit or None)))
        except ValueError as exc:
            raise errors.bad_request(str(exc))

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


def _no_store(payload):
    """带 `Cache-Control: no-store` 的 JSON 响应（**只给性能那两个读端点用**）。

    为什么：它们是"每一秒都不一样"的读数，被任何一层缓存住都会表现成
    "图冻在那儿了"，而那种故障看起来像采集器坏了 —— 排查方向完全错。
    """
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


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


# ---------------------------------------------------------------- 写请求的三道闸

def allowed_origins(cfg, request: Optional[Request] = None) -> set:
    """管理面**回环本站**的 origin 集合（`scheme://host:port`，小写）—— 契约断言 / 排障用。

    端口：给了 `request` 就用**请求自己的 `Host`**（浏览器真正在用的那个地址 ——
    经 `ssh -L` 隧道时是 18901 这类别的端口）；没给就退一步用 `server.admin_listen`。

    主机名**只取回环名**：`0.0.0.0`、配置里写的真实网卡地址、任意域名，一个都不进
    —— 它们不是浏览器地址栏里能出现的"本站"。

    ⚠️ 写闸门**不再**用这个集合判同站：那正是"Origin 必须恰好等于 `admin_listen`
    端口"这个 bug 的来源（见 `same_site_write` 的长注释）。这个函数留着，是让
    契约测试与排障输出有一份"本站应该长什么样"的清单，**不参与放行/拒绝**。
    """
    _host, port = ("", 0)
    if request is not None:
        _host, port = parse_listen(request.headers.get("host") or "")
    if not port:
        _host, port = parse_listen(cfg.get("server.admin_listen", ""))
    if not port:
        return set()
    # 两种 scheme 都收：管理面自己是明文 http，但放在反代后面时浏览器看到的是 https。
    # 认的是**同站**这件事，scheme 不改变它是不是本站。
    return {"%s://%s:%d" % (scheme, name, port)
            for name in _LOOPBACK_NAMES for scheme in ("http", "https")}


def origin_of(request: Request) -> str:
    """这次请求的"来源站"：`Origin` 优先，没有就用 `Referer` 的 scheme+host+port。

    `Referer` 要**只取到 host 为止**（它带完整路径），而且取不出来就返回空串 ——
    空串在调用方那里一律是拒绝，不会因为"解析失败"变成放行。
    """
    raw = str(request.headers.get("origin") or "").strip()
    if raw:
        return raw.lower().rstrip("/")
    ref = str(request.headers.get("referer") or "").strip()
    if not ref:
        return ""
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://[^/?#]+)", ref)
    return m.group(1).lower() if m else ""


def _host_port(request: Request) -> tuple:
    """这次请求自己的 `Host` 头 → `(host, port)`（主机名小写）。

    缺 `Host`、没带端口、坏值一律 `("", 0)` —— 调用方按**拒绝**处理（失败关闭）。
    小写化是必须的：`Host: LOCALHOST:8901` 与 `Origin: http://localhost:8901`
    说的是同一件事，不该因为大小写被判成两个站。
    """
    host, port = parse_listen(request.headers.get("host") or "")
    return (str(host or "").lower(), port)


#: `scheme://host[:port]` —— 只认到端口为止（`Origin` 本来就是这种形状，
#: `Referer` 还带路径，所以 `origin_of()` 已经先把路径切掉了）。
_ORIGIN_RE = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://(\[[0-9A-Fa-f:.]+\]|[^:/?#]+)(?::(\d{1,5}))?$")


def _origin_host_port(origin: str) -> tuple:
    """`"http://127.0.0.1:18901"` → `("127.0.0.1", 18901)`。

    `http` / `https` **不写端口**时按 80 / 443 归一（浏览器地址栏里的
    `http://127.0.0.1` 就是 80 端口，`Origin` 也会省掉它）。
    解析不出来 → `("", 0)`，调用方一律拒绝 —— 不会"解析失败=放行"。
    """
    m = _ORIGIN_RE.match(str(origin or "").strip())
    if not m:
        return ("", 0)
    scheme, host, port = m.group(1).lower(), m.group(2).lower(), m.group(3)
    if port:
        return (host, int(port))
    return (host, 443 if scheme == "https" else 80)


def same_site_write(request: Request) -> bool:
    """这次写请求是不是**浏览器从本站页面**发出的？—— 同站判据（2026-09-29 改）。

    ## 规则（两条，缺一不可）

      1. `Origin`（没有就兜底用 `Referer`，见 `origin_of`）里的 **host:port**
         必须**等于这次请求自己的 `Host` 头**里的 host:port；
      2. `Host` 的主机名必须是**回环名**（`127.0.0.1` / `localhost` / `[::1]`），
         而且**带端口**。

    scheme **不参与**比较：管理面自己收明文 http，放在反代后面时浏览器看到的是
    https，而内层请求的 scheme 我们无法可靠得知（旧实现也是两种都收）。
    比较的 host:port 一致时，`Origin` 的主机名必然也是回环名 —— 见下面两条。

    ## 为什么"Host 相等 + 回环名"就等于同站

    浏览器只在"页面地址与请求地址同源"时才把 `Origin` 填成那个源，而 `Host` 是
    浏览器按**它正在访问的地址**填的。两者逐字对上（同主机、同端口），才说明这次
    请求是那个页面发出的；再叠一层"主机名只能是回环名"，就把"攻击者自己的域名"
    整个排除在外。

    ## 为什么旧判据（必须等于 `admin_listen` 的端口）是错的

    管理面只发布在宿主回环上，文档推荐的访问方式就是
    `ssh -L 18901:127.0.0.1:8901`（本机 8901 常被别的后端占着，换个本地端口不是错）。
    这时浏览器眼里的本站是 `http://127.0.0.1:18901`，`Host` 与 `Origin` 都是它；
    而配置里 `admin_listen` 是 `:8901` —— 旧判据于是把**用户自己**挡在门外：
    "发配对码 / 保存运行参数 / 改自己口令"一律 403，报的还是"不是本站"。
    **同站 ≠ "配置里那个端口"**，同站 = "这次请求自己的 Host"。

    ## 为什么这样仍然挡得住本机恶意网页（要防的主要场景）

    恶意页跑在 `http://127.0.0.1:6666` 上，向管理面发请求 → 浏览器发的 `Origin`
    是 `http://127.0.0.1:6666`，而请求的 `Host` 是 `127.0.0.1:8901`（或隧道口的
    `127.0.0.1:18901`）→ **host 相同、端口不等 → 拒绝** ✓。

    反之若这里只比"两者相等"、不要求回环名，DNS-rebinding 就能过来：攻击者把自己
    的域名 `evil.example` 解析到 `127.0.0.1`，受害者的浏览器访问
    `http://evil.example:8901` 时 `Host` 与 `Origin` **都是** `evil.example:8901`
    —— 一样相等。第 2 条（只认回环名）就是专门拦它的，所以**绝不能**放宽成
    "任意来源"。

    ## 顺带

    `server.admin_listen` 绑 `0.0.0.0` 还是 `127.0.0.1` 与这条判据**无关**：这里
    一个字节都不读配置，所以"配置写法不同导致时松时紧"这一类 bug 结构上不存在。
    """
    host, hport = _host_port(request)
    if not hport or host not in _LOOPBACK_ALIASES:
        return False
    origin = origin_of(request)
    if not origin:
        return False
    ohost, oport = _origin_host_port(origin)
    if not ohost or not oport or ohost not in _LOOPBACK_ALIASES:
        return False
    # 严格相等（不做 `127.0.0.1` ↔ `localhost` 的"别名互换"）：真实浏览器发出来的
    # `Origin` 与 `Host` 本来就是同一个地址的两种写法，逐字对上才是"同一次导航"。
    # 对不上就拒（失败关闭）—— 少一分宽松，多一分可复核。
    return ohost == host and oport == hport


def check_write_request(request: Request, cfg, sessions, *, cookie_name: str = COOKIE_NAME):
    """写请求的闸门。返回 `None` = 放行，否则返回一个 `EchoError`（401/403）。

    **顺序是有意的**：先认身份（401），再判"这次请求像不像浏览器从本站发出的"（403）。
    反过来的话，一个没登录的跨站请求会得到 403，而 403 已经泄漏了
    "这个端点存在且需要凭据"这件小事 —— 401 更诚实也更有用。

    同站那一条见 `same_site_write`。`cfg` 保留在签名里是为了不改调用方（中间件），
    但它**已经不参与**同站判定 —— 正是"拿配置里的端口当判据"害了隧道用户。
    """
    token = request.cookies.get(cookie_name) or ""
    row = sessions.get(token)
    if row is None:
        return errors.unauthorized("没登录或会话已过期")
    sent = str(request.headers.get("x-csrf-token") or "")
    if not sent or not hmac.compare_digest(sent, str(row.get("csrf") or "")):
        return errors.forbidden("CSRF 令牌不对（刷新页面重试）")
    if not str(request.headers.get(WRITE_HEADER) or "").strip():
        # 见 `WRITE_HEADER` 的注释：这一条不是身份校验，是"非简单请求"的标志。
        return errors.forbidden("写请求必须带 %s 头（防跨站表单的那道闸）" % WRITE_HEADER)
    if not same_site_write(request):
        return errors.forbidden("写请求的 Origin/Referer 不是本站（%s）"
                                % (origin_of(request) or "缺失"))
    return None


def audit_write(store, admin: str, action: str, target: str = "", exc=None) -> None:
    """记一条写动作（成功或失败）。

    **失败也要留痕**：`admin_audit` 只有四列（`ts/admin/action/target`，设计 §8.5），
    所以"结果与原因"编码进 `action`（成功 `revoke`、失败 `revoke.failed`）与
    `target`（`cli-x | forbidden 没登录或会话已过期`）—— **不加列**：
    加列要走 §8.5 那道评审门，而这里的信息量四列装得下。

    写不进去**不覆盖原来的错误**：审计失败不该把"一次已经成功的动作"变成 500
    （客户端会重试，于是动作做两遍），也不该把原始异常吃掉。
    """
    if store is None:
        return
    text = str(target or "")
    if exc is not None:
        why = "%s %s" % (getattr(exc, "code", "error"),
                         getattr(exc, "detail", "") or getattr(exc, "message", ""))
        text = (text + " | " + why) if text else why.strip()
    try:
        store.audit(str(admin or ""), str(action or ""), text)
    except Exception:                                          # pragma: no cover - 兜底
        pass


def client_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """一个客户端行 → **可以出网**的那份视图。

    **白名单式**：只挑这几个字段。绝不 `dict(row)` —— 那一行里有 `secret_hash`
    与 `prev_secret_hash`，而管理面的每一条响应都会进浏览器（截图、控制台、
    发给人的报错）。"秘密只回显一次"这条闸门就靠这个函数守住。
    """
    prev_exp = float(row.get("prev_secret_expires_at") or 0)
    return {
        "clientId": str(row.get("client_id") or ""),
        "name": str(row.get("name") or ""),
        "scopes": str(row.get("scopes") or ""),
        "scopesList": str(row.get("scopes") or "").split(),
        "disabled": bool(row.get("disabled")),
        "tokenVersion": int(row.get("token_version") or 0),
        "dailyAudioMinutes": float(row.get("daily_audio_minutes") or 0.0),
        "createdAt": float(row.get("created_at") or 0),
        "updatedAt": float(row.get("updated_at") or 0),
        "lastSeen": float(row.get("last_seen") or 0),
        "secretRotatedAt": float(row.get("secret_rotated_at") or 0),
        # 宽限期里旧 secret 还能换令牌 —— 这件事必须显形，否则"轮换过了"是个错觉
        "oldSecretUsableUntil": prev_exp if prev_exp > time.time() else 0.0,
    }


def parse_ttl(seconds, default: float) -> float:
    """配对码有效期（秒）。空 → 用配置默认；坏值/越界 → 400。

    **越界就是 400，不静默夹紧**：夹紧会让"我明明填了 30 天"变成"其实只发了 7 天"，
    而发码的人以为同事有 30 天。
    """
    if seconds in (None, "", 0):
        return float(default)
    try:
        ttl = float(seconds)
    except (TypeError, ValueError):
        raise errors.bad_request("ttlSeconds 要是秒数")
    if ttl < MIN_PAIRING_TTL_S or ttl > MAX_PAIRING_TTL_S:
        raise errors.bad_request("ttlSeconds 要在 %d ~ %d 秒之间（%.1f 分钟 ~ %.0f 天）"
                                 % (MIN_PAIRING_TTL_S, MAX_PAIRING_TTL_S,
                                    MIN_PAIRING_TTL_S / 60.0, MAX_PAIRING_TTL_S / 86400.0))
    return ttl


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
        log.info("管理面在 http://%s:%d/admin/ （**可写**：发授权 / 禁用 / 撤销 / 改 scopes / "
                 "改配额 / 轮换 secret / 运行参数（并发·队列，改完立刻生效） / "
                 "改**自己**的口令（要带当前口令；增删管理员仍然只在命令行） / "
                 "性能记录（只在内存，不落盘）；"
                 "全部要求管理员会话 + 同站 Origin + %s 头）",
                 host, port, WRITE_HEADER)
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

    它只做两件事：把 `/admin/api/*` 的结果画成页签，以及把写动作发给那几个写端点
    （写请求都带 `X-ECHO-Admin` 与 `X-CSRF-Token`，危险动作先弹二次确认）。
    页面里**没有任何秘密** —— 新 secret / 新配对串只存在这一次响应的内存里，
    刷新之后不再出现（后端也不会再给）。
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
