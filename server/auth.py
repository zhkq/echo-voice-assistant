# -*- coding: utf-8 -*-
"""配对码 → `client_id` + `secret` → 短期 JWT（设计 §7.1 / §7.4 / §7.5）。

## 三层，各自解决一件事

| 层 | 解决什么 | 用什么 |
|---|---|---|
| 传输层 | 链路加密、防窃听 | TLS（部署层，见 §9） |
| **配对**（一次，带外） | 凭据**怎么发出去** | 一次性配对码 |
| **令牌**（每小时） | 每个请求"这是谁、能做什么" | `client_id` + 短期 JWT |

**mTLS 不替代 `client_id`**（§7.1）：证书即身份会让撤销退化成 CRL 问题，
而"改一个 `token_version` 字段"才是能立刻生效的那个动作。

## JWT 用 PyJWT，不手搓

`verify()` 里 **`algorithms=["HS256"]` 是写死的**，绝不从 token 的 header 里取 `alg` ——
`alg: none` 与算法混淆（拿公钥当 HMAC 密钥）这两个经典漏洞都源于"信任了 token 自称的算法"。
另外 `alg` 与 `typ` 也要求**恰好**是那两串。这几条各有一条用例钉着（`AuthTokenTests`）。

## 撤销：立即生效，靠"版本号"，不靠等 token 过期

短期 JWT 意味着撤销原则上最多滞后 1 小时 —— 那不可接受（§7.4 约定 4）。
所以 JWT 里带 `ver`，`clients.token_version` 是权威：**撤销 = 版本 +1**，
校验时比对**进程内缓存**（单实例立即生效；多实例 ≤5 秒靠轻量轮询，如实写在文档里）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import jwt

from server import errors
from server.store import Store

#: 配对码用的字母表：**去掉 0/O/1/I/L** —— 这个码要人工从管理面念给同事，
#: 长得像的字符会制造"我明明输对了"的排障。
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 8

#: JWT 头**必须恰好**是这样（不是"包含"）。见模块注释。
JWT_ALG = "HS256"


# ---------------------------------------------------------------- 哈希

def _hash(value: str, salt: str = "") -> str:
    """PBKDF2-HMAC-SHA256。secret 与配对码都只存这个。

    为什么不用裸 sha256：配对码只有 8 位、字母表 31 个字符 → 空间约 2^39，
    裸哈希在 GPU 上是可枚举的。加盐 + 迭代把"猜"的代价抬起来。
    配对码本身还有 15 分钟有效期与"用掉即删"兜着，但哈希不该是薄弱的那一环。
    """
    dk = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"),
                             ("echo-server:" + salt).encode("utf-8"), 120_000)
    return dk.hex()


def hash_pairing_code(code: str) -> str:
    """配对码没有盐 —— 校验时只有码本身可比。代价由有效期与限速承担。"""
    return _hash(code, salt="pairing")


def hash_secret(secret: str, salt: str) -> str:
    """`salt` 用 `client_id`：不同客户端的同一个 secret 不会撞成同一个哈希。"""
    return _hash(secret, salt=salt)


def new_pairing_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def new_client_id() -> str:
    return "cli-" + secrets.token_hex(3)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------- 客户端缓存

class _Cached:
    __slots__ = ("row", "fetched_at")

    def __init__(self, row: Dict[str, Any]):
        self.row = row
        self.fetched_at = time.time()


class ClientCache:
    """`client_id` → 客户端行，**进程内**。

    设计 §7.2 的硬约束：配额/鉴权**不每请求查库**（否则库会变成瓶颈）。
    所以这里缓存，并且把"撤销能多快生效"这件事变成**显式的**：
    `revoke()` / `set_disabled()` 直接改缓存里的行 + 落库，单实例立即生效。

    多实例时别的实例看不到这次改动，靠 §7.5 的轻量轮询（`max(updated_at)`）
    在 ≤5 秒内发现。**v1 是单实例，先把接口留出来**（`refresh_all`）。
    """

    def __init__(self, store: Store, ttl_s: float = 60.0):
        self.store = store
        self.ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._rows: Dict[str, _Cached] = {}

    def get(self, client_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            hit = self._rows.get(client_id)
        if hit is not None and (time.time() - hit.fetched_at) < self.ttl_s:
            return hit.row
        row = self.store.client(client_id)          # 缓存没命中/过期 → 查一次库
        with self._lock:
            if row is None:
                self._rows.pop(client_id, None)
            else:
                self._rows[client_id] = _Cached(row)
        return row

    def put(self, row: Dict[str, Any]) -> None:
        """配对成功 / 轮换后立刻把新行放进缓存 —— 别让接着来的第一个请求又查一次库。"""
        with self._lock:
            self._rows[str(row["client_id"])] = _Cached(dict(row))

    def revoke(self, client_id: str) -> int:
        """撤销：**先落库、再改缓存**，然后让缓存项立即失效。

        顺序重要：先落库意味着即使进程在下一行之前崩了，"已撤销"也是持久事实。
        """
        ver = self.store.revoke(client_id)
        with self._lock:
            self._rows.pop(client_id, None)
        return ver

    def set_disabled(self, client_id: str, disabled: bool) -> None:
        self.store.set_disabled(client_id, disabled)
        with self._lock:
            self._rows.pop(client_id, None)

    def forget(self, client_id: str) -> None:
        with self._lock:
            self._rows.pop(client_id, None)

    def refresh_all(self) -> int:
        """多实例用的"发现别人改过"（§7.5 ④）。v1 单实例还没人调它。"""
        rows = self.store.clients()
        with self._lock:
            self._rows = {str(r["client_id"]): _Cached(r) for r in rows}
        return len(rows)


# ---------------------------------------------------------------- 令牌

def issue_token(client: Dict[str, Any], key: str, ttl_s: int) -> Tuple[str, int]:
    """签发短期 JWT。返回 (token, expires_in)。"""
    now = int(time.time())
    claims = {
        "sub": str(client["client_id"]),
        "scopes": str(client.get("scopes") or ""),
        # **撤销靠它**：校验时与缓存里的 token_version 比对
        "ver": int(client.get("token_version") or 1),
        "iat": now,
        "exp": now + int(ttl_s),
        "jti": secrets.token_hex(8),
    }
    # 不放任何业务信息、也不放配额余额：余额必须实时读内存，
    # 签死在 token 里必然不准（§7.5 ②）。
    return jwt.encode(claims, key, algorithm=JWT_ALG), int(ttl_s)


def decode_token(token: str, key: str, leeway_s: int = 60) -> Dict[str, Any]:
    """验签 + 验 exp。**失败一律 `unauthorized`**，不区分原因（别给探测者送信息）。

    `leeway_s=60`：内网机器时钟未必准（§7.5 ③ 第①条）。
    """
    try:
        header = jwt.get_unverified_header(token)
    except Exception:
        raise errors.unauthorized("令牌格式不对")
    if header.get("alg") != JWT_ALG or header.get("typ") not in (None, "JWT"):
        # **不信任 token 自称的算法** —— `alg: none` 与算法混淆都死在这一行。
        raise errors.unauthorized("不支持的令牌算法")
    try:
        return dict(jwt.decode(token, key, algorithms=[JWT_ALG],
                               leeway=int(leeway_s),
                               options={"require": ["exp", "sub"]}))
    except errors.EchoError:
        raise
    except Exception:
        raise errors.unauthorized("令牌无效或已过期")


def parse_basic(header: str) -> Tuple[str, str]:
    """解析 `Authorization: Basic base64(clientId:secret)`（§7.5 ②）。"""
    import base64
    raw = str(header or "")
    if not raw.lower().startswith("basic "):
        raise errors.unauthorized("换令牌要用 Basic 认证")
    try:
        decoded = base64.b64decode(raw[6:].strip(), validate=True).decode("utf-8")
    except Exception:
        raise errors.unauthorized("Basic 凭据不是合法 base64")
    if ":" not in decoded:
        raise errors.unauthorized("Basic 凭据里没有分隔符")
    cid, _, secret = decoded.partition(":")
    if not cid or not secret:
        raise errors.unauthorized("Basic 凭据不完整")
    return cid, secret


class RevocationWatcher:
    """轮询"别人动过客户端没有"，动了就整体刷缓存（设计 §7.5 ④）。

    ## 为什么必须有它

    撤销靠比对 `token_version`，而**版本号缓存在进程里**。同一进程内改是立即生效的
    （`ClientCache.revoke`），但**命令行 `--revoke` 是另一个进程** ——
    它改的是库，跑着的服务并不会因此少读一行缓存。实测就是这样：
    命令行撤销之后，服务端**继续接受**那个 JWT，直到缓存自己过期（默认 60 秒）。

    设计里其实早写了这条（"多实例 ≤5 秒，靠轻量轮询发现"），
    但代码里没实现 —— 于是"撤销立即生效"这句在**唯一的运维入口**（CLI）上是假话。
    这个类把它补上：**每 5 秒一次聚合查询**，不是每请求查库（§7.2 的硬约束仍然成立）。

    ## 延迟要如实说

    | 路径 | 延迟 |
    |---|---|
    | 同一进程内（管理面/管理 API） | **立即** |
    | 另一个进程（CLI、多实例） | **≤ 轮询间隔**（默认 5 秒） |

    **不要把它写成"立即"。** 这条延迟是有意的取舍：用 5 秒换掉"每请求一次库查询"。
    """

    def __init__(self, store: Store, cache: ClientCache, interval_s: float = 5.0):
        self.store = store
        self.cache = cache
        self.interval_s = max(0.5, float(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen = self.store.clients_max_updated_at()
        self.polls = 0
        self.refreshes = 0

    def poll_once(self) -> bool:
        """轮询一次。返回"这次发现并应用了变更吗"。**测试直接调它**，不靠 sleep。"""
        self.polls += 1
        now = self.store.clients_max_updated_at()
        if now <= self._seen:
            return False
        self._seen = now
        self.cache.refresh_all()
        self.refreshes += 1
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        def loop():
            while not self._stop.wait(self.interval_s):
                try:
                    self.poll_once()
                except Exception:
                    pass                      # 轮询不该把服务搞挂；下一轮再来
        self._thread = threading.Thread(target=loop, name="echo-auth-revoke-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=2.0)


# ---------------------------------------------------------------- 配对限速

class PairThrottle:
    """`/v1/pair` 的滥用防护（§7.4 约定 1）。

    它是**唯一不需要既有凭据的能力端点** —— 所以必须额外防猜：
    同一来源连续失败到阈值就退避。在内存里，不落库（这是运行时状态，不是管理数据）。
    """

    def __init__(self, max_failures: int = 5, window_s: float = 300.0):
        self.max_failures = int(max_failures)
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        self._fails: Dict[str, List[float]] = {}

    def check(self, source: str) -> None:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(source, []) if now - t < self.window_s]
            self._fails[source] = hits
            if len(hits) >= self.max_failures:
                wait = int(self.window_s - (now - hits[0])) + 1
                raise errors.rate_limited(max(1, wait),
                                          detail="同一来源连续失败 %d 次" % len(hits))

    def failed(self, source: str) -> None:
        with self._lock:
            self._fails.setdefault(source, []).append(time.time())

    def succeeded(self, source: str) -> None:
        with self._lock:
            self._fails.pop(source, None)


# ---------------------------------------------------------------- 校验入口

class Auth:
    """一个请求的鉴权入口。**所有判断都在内存里**（除了缓存未命中的那一次查库）。"""

    def __init__(self, cfg, store: Store, cache: Optional[ClientCache] = None):
        self.cfg = cfg
        self.store = store
        self.cache = cache or ClientCache(store, ttl_s=float(cfg.get("auth.cache_ttl_s", 60)))
        self.throttle = PairThrottle(
            max_failures=int(cfg.get("auth.pair_max_failures", 5)),
            window_s=float(cfg.get("auth.pair_window_s", 300)))
        # 跨进程撤销的发现机制（见 `RevocationWatcher` 的说明）。默认不启动 ——
        # 由 `main.lifespan` 起停，测试里手动 `poll_once()`。
        self.watcher = RevocationWatcher(
            store, self.cache, interval_s=float(cfg.get("auth.revoke_poll_s", 5)))

    # ---- 开关 --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("auth.enabled", False))

    @property
    def mode(self) -> str:
        """`token`（静态令牌，v1 起步用）| `jwt`（配对 + 短期令牌）。"""
        return str(self.cfg.get("auth.mode", "token") or "token").lower()

    @property
    def key(self) -> str:
        """JWT 签名密钥。**没配就直接拒绝启动**，不生成临时密钥。

        生成临时密钥看着"更安全"，其实是坑：进程一重启所有客户端全部 401，
        而管理员看到的现象是"昨天还好好的"，很难联想到"密钥每次都是新的"。
        宁可起不来，并明确告诉他去配一个。
        """
        secret = str(self.cfg.get("auth.jwt_secret", "") or "").strip()
        if not secret:
            raise errors.auth_misconfigured(
                "auth.mode=jwt 但没配 auth.jwt_secret；"
                "请生成一个（openssl rand -hex 32）后写进配置")
        return secret

    # ---- 静态令牌模式（v1 起步）--------------------------------------------

    def _static_client(self, token: str) -> Dict[str, Any]:
        for row in (self.cfg.get("auth.tokens") or []):
            if hmac.compare_digest(str(row.get("token") or ""), token):
                return {"client_id": str(row.get("client_id") or "unknown"),
                        "scopes": str(row.get("scopes") or ""),
                        "token_version": 1, "disabled": 0}
        raise errors.unauthorized("令牌无效")

    # ---- 主入口 ------------------------------------------------------------

    def authenticate(self, authorization: str, need_scope: str = "") -> Dict[str, Any]:
        """返回这个请求的客户端（至少含 `client_id`）。失败抛 401/403。"""
        if not self.enabled:
            # 鉴权关着 → 所有请求算同一个客户端。启动时会**大声告警**（main.lifespan）。
            return {"client_id": "anonymous", "scopes": "", "token_version": 1, "disabled": 0}

        raw = str(authorization or "")
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        if not token:
            raise errors.unauthorized("缺少 Bearer 令牌")

        if self.mode != "jwt":
            client = self._static_client(token)
            self._check_scope(client, need_scope)
            return client

        claims = decode_token(token, self.key,
                              leeway_s=int(self.cfg.get("auth.clock_skew_s", 60)))
        cid = str(claims.get("sub") or "")
        if not cid:
            raise errors.unauthorized("令牌里没有客户端标识")

        row = self.cache.get(cid)
        if row is None:
            raise errors.unauthorized("客户端不认识或已被删除")
        # ③ 版本号：不等 = 已撤销。**这一步才是撤销能立即生效的地方。**
        if int(row.get("token_version") or 1) != int(claims.get("ver") or 0):
            raise errors.unauthorized("令牌已失效（客户端凭据被撤销或轮换）")
        # ④ 禁用走 403：它是"你被认出来了，但不许用"，与 401 语义不同
        if int(row.get("disabled") or 0):
            raise errors.forbidden("这个客户端已被禁用")
        self._check_scope(row, need_scope)
        return row

    def _check_scope(self, client: Dict[str, Any], need_scope: str) -> None:
        if not need_scope:
            return
        scopes = str(client.get("scopes") or "").split()
        if not scopes:
            # 空 scopes = **没有额外限制**（静态令牌模式与 v1 起步都靠这条）。
            # 刻意不把空解释成"什么都不许"：那会让"我明明配了客户端却全 403"变成一个谜。
            return
        if need_scope not in scopes:
            raise errors.forbidden("这个客户端没有 %s 权限" % need_scope)

    # ---- 配对（§7.4）-------------------------------------------------------

    def create_pairing_code(self, created_by: str = "") -> str:
        """管理面调用：生成一次性配对码，**明文只在这里返回这一次**。"""
        code = new_pairing_code()
        self.store.put_pairing_code(hash_pairing_code(code),
                                    float(self.cfg.get("auth.pairing_ttl_s", 900)),
                                    created_by=created_by)
        return code

    def redeem(self, code: str, client_name: str = "") -> Dict[str, Any]:
        """客户端调用：拿配对码换 `client_id` + `secret`（**secret 只出现这一次**）。"""
        normalized = "".join(str(code or "").split()).upper()
        if not normalized:
            raise errors.bad_request("没有配对码")
        row = self.store.take_pairing_code(hash_pairing_code(normalized))
        if row is None:
            raise errors.unauthorized("配对码无效、已被使用，或服务端压根没发过它")
        if float(row.get("expires_at") or 0) < time.time():
            # 已删除，所以这条其实很难走到（take 已经把行取走了）；
            # 留着是为了"以后改成软删除"时语义不变。
            raise errors.unauthorized("配对码已过期")
        cid = new_client_id()
        secret = new_secret()
        self.store.upsert_client(cid, client_name or "未命名客户端",
                                 hash_secret(secret, cid),
                                 scopes=str(self.cfg.get("auth.default_scopes", "") or ""))
        fetched = self.store.client(cid)
        if fetched:
            self.cache.put(fetched)
        return {"clientId": cid, "secret": secret,
                "serverName": str(self.cfg.get("server.id", "")),
                "protocol": 1}

    # ---- 换令牌（§7.5 ②）---------------------------------------------------

    def token_for(self, authorization: str) -> Dict[str, Any]:
        cid, secret = parse_basic(authorization)
        row = self.cache.get(cid)
        if row is None:
            # 静态令牌模式下没有 clients 行，但换令牌这件事本来就属于 jwt 模式
            raise errors.unauthorized("客户端不认识")
        if int(row.get("disabled") or 0):
            raise errors.forbidden("这个客户端已被禁用")
        if not hmac.compare_digest(str(row.get("secret_hash") or ""),
                                   hash_secret(secret, cid)):
            raise errors.unauthorized("secret 不对")
        ttl = int(self.cfg.get("auth.token_ttl_s", 3600))
        token, expires_in = issue_token(row, self.key, ttl)
        return {"accessToken": token, "expiresIn": expires_in,
                "scopes": str(row.get("scopes") or "").split(),
                "clientId": cid}


def open_store(cfg) -> Store:
    """按配置开库。

    路径优先级：`auth.db`（显式指定）→ `{server.state_root}/echo-server-auth.db`。
    **刻意不回退到 `tmp.root`**：那是"随便删、可以挂 tmpfs"的地方，
    而这里存的是客户端凭据 —— 落在那儿就等于"照文档把 tmp 换成内存盘，
    所有配过的客户端一起消失"（见 `server/settings.py` 的 `state_root` 注释）。
    `:memory:` 用于测试。
    """
    path = str(cfg.get("auth.db", "") or "").strip()
    if not path:
        # `Config.state_root` 自带"空 → {ECHO}/data/server-state"的回退，
        # 所以这里不用再兜一层；dict 形态的配置就走 get()。
        state_root = getattr(cfg, "state_root", None) or str(cfg.get("server.state_root", "") or "")
        path = os.path.join(str(state_root), "echo-server-auth.db")
    return Store(path)
