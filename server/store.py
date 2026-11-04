# -*- coding: utf-8 -*-
"""服务端的**唯一**一处落库：鉴权元数据（设计 §8.5）。

## 这个文件为什么可以存在

"服务端不存储业务数据"是铁律（L4），但它**不等于**"服务端一个字节都不许落盘"：
- 临时文件可以有（`server/tmp.py`，自有 + 可证明清理 + 有兜底）；
- **关于客户端的管理数据**可以有 —— 没有它就没有撤销、配额与审计。

判据是一句话（设计 §8.5）：存**关于请求的元数据**和**关于客户端的管理数据**；
不存**请求的内容**，也不存任何业务概念。

所以这里落两张表，**一张都不能多**（多一张要走评审）：

| 表 | 存什么 |
|---|---|
| `clients` | 客户端注册与凭据（secret 只存哈希） |
| `pairing_codes` | 待用的配对码（**用掉即删**） |

> 上面那两句是 v1 的原文。2026-09-24 起按设计 §8.5 又落了 `calls`（请求元数据）
> 与 `admin_users` / `admin_audit`（管理面账号与动作审计），2026-09-29 落了
> `server_limits`（**这台后端自己的运行参数**：总并发 / 每客户端并发 / 队列上限，
> 见 `server/limits.py`）。**唯一一份活清单是 `TABLE_WHITELIST`**，
> 加表加列都要先想一想"这算不算内容"。

设计 §8.5 的白名单里还有 `admin_users` / `calls` / `calls_rollup` / `model_events` /
`admin_audit`，那是管理面与统计落地时才建的（v2/v3）。这里是**子集**，不是另一份清单 ——
所以 `tests/test_server_contract.py::AuthSchemaTests` 同时钉两件事：
**表集合 ⊆ 白名单**，且 **列名不得命中列黑名单**（§8.5 那一串"内容/业务概念"的列名）。
黑名单**写在那份测试里、不写在这里**：它是**审计规则**，不是运行时数据 ——
而且它本身就是由那些"不许出现的词"拼成的，放在 `server/` 下会跟
"服务端源码不含业务词"的护栏打架。护栏不该为了自己让路。
加表加列都要先想一想"这算不算内容"。

## 为什么是 SQLite 而不是 JSON 文件

`clients` 要被**并发读**（每个请求校验都碰它，虽然走内存缓存）并且要能**原子地**做
`token_version += 1`（撤销）。JSON 文件在这两件事上都要自己实现一遍锁与原子写，
而 SQLite 本来就是干这个的、且是标准库。**但绝不复用它存业务数据** —— `app/db.py`
是客户端的东西，服务端一行都不 import（有护栏）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

#: 设计 §8.5 的表白名单（**唯一**一份）。`CREATE TABLE` 只允许出现在这里。
#: `calls` 是 2026-09-24 按设计加进来的（审计元数据，设计 §7.3 逐字定义了那十列）。
#: `admin_users` / `admin_audit` 是同一天做管理面时加的（设计 §8.4 那五个页签要一个
#: 管理员账号体系；审计动作表本来就在白名单里）。
#: `server_limits` 是 2026-09-29 加的（用户要求"并发上限能在管理面里由管理员配、
#: 不用改环境变量也不用重启"）—— 它是**第三类**：前两类是"关于客户端的管理数据"与
#: "关于请求的元数据"，而这一张是**这台后端自己怎么跑**（总并发 / 每客户端并发 /
#: 队列上限）。加表本来要走 §8.5 那道评审门，这一轮就是那次评审。
#: `server_advertised` 是 2026-09-30 加的（用户实测的 bug：容器里发出的配对串带的是
#: Docker 网桥地址 `172.18.0.2`，同事连不上）—— 与 `server_limits` 同一个第三类：
#: "这台后端**对外**是谁"，一行一个字符串，没有任何业务概念。
#:
#: **2026-11-03 的收费功能评审（设计 `docs/后端收费-交付给服务器dsh/01-后端收费与自动配对-设计.md` §4.1）**：
#: 收费必然要在服务端存"关于**人**与**钱**的管理数据"，这与"服务端不存业务数据"
#: 不冲突 —— 它仍然不存**请求内容**（音频、转写文本、嵌入）。新增的十张表全部
#: 属于同一类："谁、买了什么、还有多少、什么时候到期"。逐张的理由：
#:
#: | 表 | 为什么可以落在这里 |
#: |---|---|
#: | `plans` | 套餐模板（价格 / 天数 / 额度），**不含任何人的数据** |
#: | `users` | 账号：工号（唯一）、姓名、支付身份；管理数据 |
#: | `user_devices` | 账号 ↔ 设备绑定；撤销/换机/设备上限的依据 |
#: | `invites` | 邀请码（谁能注册）；防外传的管理数据 |
#: | `orders` / `order_events` | 订单与状态流水（金额、状态、时间）；对账必需 |
#: | `alipay_notify` | 支付回调原文与去重（`trade_no` 唯一）；对账/排障必需，**不含请求内容** |
#: | `entitlements` | 权益（到期时间 / 额度 / 状态）；就是"还有多少" |
#: | `authorized_open_ids` / `scan_sessions` | 设计 §3.5.9 的 Phase 4 白名单/扫码会话，**先建表、逻辑不做** |
#:
#: 这十张表都**没有**"内容"列（音频/文本/嵌入/说话人），由
#: `tests/test_server_contract.py::AuthSchemaTests` 的列名黑名单逐列核对。
TABLE_WHITELIST = ("clients", "pairing_codes", "calls", "admin_users", "admin_audit",
                   "server_limits", "server_advertised",
                   # 收费（2026-11-03，设计 §4.1）—— 见上面那段逐张的理由。
                   "plans", "users", "user_devices", "invites",
                   "authorized_open_ids", "scan_sessions",
                   "orders", "order_events", "alipay_notify", "entitlements")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    secret_hash   TEXT NOT NULL,
    scopes        TEXT NOT NULL DEFAULT '',
    token_version INTEGER NOT NULL DEFAULT 1,
    disabled      INTEGER NOT NULL DEFAULT 0,
    -- 每客户端专属的每日音频分钟数配额；0 = 用全局默认（limits.daily_audio_minutes）。
    -- **是上限不是内容**，所以不撞 §8.5 的"内容/业务概念"列名黑名单。
    daily_audio_minutes REAL NOT NULL DEFAULT 0,
    -- 轮换宽限期（设计 §7.5 ⑤，2026-09-24）：轮换后**旧 secret 还能用一阵**，
    -- 让客户端在下一次换令牌时平滑过渡、不必当场重新配对。
    -- `prev_*` 只在宽限期内非空；`secret_rotated_at` 是给人看的时间戳。
    secret_rotated_at      REAL NOT NULL DEFAULT 0,
    prev_secret_hash       TEXT NOT NULL DEFAULT '',
    prev_secret_expires_at REAL NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    last_seen     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash  TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    client_id  TEXT NOT NULL DEFAULT '',
    -- 这对列是 2026-09-24 补的：设计 §7.4 说"管理员新建客户端时填名字与 scope"，
    -- 而"新建"这一步产出的是一张**配对码** —— 名字与 scope 必须**跟着码走**，
    -- 否则兑换出来的客户端只能拿到 `auth.default_scopes`，
    -- "给这台机器只开 asr 权限"就无从表达。
    name       TEXT NOT NULL DEFAULT '',
    scopes     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pairing_expires ON pairing_codes(expires_at);
-- 调用元数据（设计 §7.3）。**只有元数据，没有内容**：没有音频、文本、嵌入、说话人数。
-- 列就是设计 §8.5 表里那十个，一个不多 —— 加列要走那条"多一张表都要评审"的同一道门。
CREATE TABLE IF NOT EXISTS calls (
    ts            REAL NOT NULL,
    client_id     TEXT NOT NULL DEFAULT '',
    endpoint      TEXT NOT NULL DEFAULT '',
    model_id      TEXT NOT NULL DEFAULT '',
    audio_seconds REAL NOT NULL DEFAULT 0,
    queue_wait_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    status        INTEGER NOT NULL DEFAULT 0,
    error_code    TEXT NOT NULL DEFAULT '',
    request_id    TEXT NOT NULL DEFAULT ''
);
-- 两个索引对应两种真实查询：按时间倒着看最近（面板），按客户端+时间做汇总（统计）。
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_client_ts ON calls(client_id, ts);
-- 管理面的账号（设计 §8.4）。**只有哈希**，明文只出现一次（建账号时打印出来）。
-- 它与管理动作的审计是两张表：一张是"谁能进来"，一张是"他做了什么"。
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login    REAL NOT NULL DEFAULT 0
);
-- 管理动作的审计。**动作都是写动作**（只读的看不算动作），所以这张表很小。
CREATE TABLE IF NOT EXISTS admin_audit (
    ts     REAL NOT NULL,
    admin  TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit(ts);
-- 服务端**自己的运行参数**（2026-09-29）：总并发 / 每客户端并发 / 队列上限。
-- 为什么落库：这三个值的权威来源必须能**越过环境变量**（compose 默认就设了
-- `ECHO_MAX_CONCURRENT`，否则"管理面里改了却不生效"），而要让权威值在
-- **重启之后还在**，就只能落在 state 卷的这个库里（`tmp` 是会被清掉的）。
-- 为什么不是"又一张客户端表"：它与客户端、与单次请求都无关 —— 它是"这台后端怎么跑"，
-- 所以列名只有 `name`/`value`/`updated_at`/`updated_by`，没有任何业务概念。
-- **只放运行参数**（见 `server/limits.py` 的那份清单），别把一般配置项搬进来。
CREATE TABLE IF NOT EXISTS server_limits (
    name       TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT ''
);
-- 这台后端**对外公布**的地址（2026-09-30）：发配对码时 `echo://pair?host=…` 里那个 host。
-- 为什么落库：容器里自动探测到的是 Docker 网桥地址（实测 172.18.0.2），而"同事能连到
-- 哪台机器"只有部署的人知道 —— 所以管理面得能改，而且改了要**重启后还在**。
-- 为什么与 `server_limits` 分开：那张表的值是整数，这个是地址串；塞进整数列只能靠编码。
-- 列名只有 value/updated_at/updated_by（与 limits 同口径），没有任何业务概念。
CREATE TABLE IF NOT EXISTS server_advertised (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT ''
);

-- ================================================================ 收费（2026-11-03）
-- 设计：`docs/后端收费-交付给服务器dsh/01-后端收费与自动配对-设计.md` §4.1。十张表都只存**管理数据**
-- （谁、买了什么、到什么时候、还有多少、调用计数），不存请求内容。
-- 列名刻意避开 §8.5 的"内容/业务概念"黑名单（那份黑名单在护栏用例里）。

-- 套餐模板。**价格与额度只在这里**（代码里不写死）；管理面可改价/下架。
CREATE TABLE IF NOT EXISTS plans (
    code                TEXT PRIMARY KEY,
    name                TEXT NOT NULL DEFAULT '',
    price_cents         INTEGER NOT NULL DEFAULT 0,
    days                INTEGER NOT NULL DEFAULT 30,
    daily_audio_minutes REAL NOT NULL DEFAULT 0,
    tts_chars           INTEGER NOT NULL DEFAULT 0,
    qps                 INTEGER NOT NULL DEFAULT 1,
    max_devices         INTEGER NOT NULL DEFAULT 3,
    max_concurrent      INTEGER NOT NULL DEFAULT 1,
    active              INTEGER NOT NULL DEFAULT 1,
    sort                INTEGER NOT NULL DEFAULT 0
);
-- 账号（人）。`employee_no` 唯一（空串不参与唯一：支付即注册那条路可以没有工号）。
-- `token_version` 是 logout/重置设备的撤销机制：userToken 里带 `ver`，比对不上即失效。
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    employee_no   TEXT NOT NULL DEFAULT '',
    -- 注册时用的邀请码。登录只校验"这张码存在且属于该 user"，**不因过期拒绝**
    -- （邀请码的 expires_at 只是注册名额的有效期，不是账号寿命）。
    invite_code   TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    alipay_open_id TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active',
    token_version INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_employee_no
    ON users(employee_no) WHERE employee_no <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_alipay_open_id
    ON users(alipay_open_id) WHERE alipay_open_id <> '';
-- 人 ↔ 设备。`client_id` 唯一：一台设备只属于一个人（换人要走解绑/迁移）。
CREATE TABLE IF NOT EXISTS user_devices (
    user_id     TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT '',
    bound_at    REAL NOT NULL DEFAULT 0,
    last_seen   REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_devices_client ON user_devices(client_id);
CREATE INDEX IF NOT EXISTS idx_user_devices_user ON user_devices(user_id, status);
-- 邀请码（谁能注册）。`used` 在同一个事务里 +1，`max_uses` 到了就拒。
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    max_uses   INTEGER NOT NULL DEFAULT 1,
    used       INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT ''
);
-- 白名单（设计 §3.5.9，Phase 4：**先建表、逻辑不做**）。
CREATE TABLE IF NOT EXISTS authorized_open_ids (
    open_id    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'admin',
    status     TEXT NOT NULL DEFAULT 'active',
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
-- 扫码授权会话（设计 §3.5.9，Phase 4：**先建表、逻辑不做**）。
CREATE TABLE IF NOT EXISTS scan_sessions (
    id         TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',
    open_id    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_sessions_state ON scan_sessions(state);
-- 订单。`status` 只许前进：created → paying → paid → (refunded|closed)；
-- `expires_at` 是未支付订单的关闭时刻；`meta` 只放 JSON 元数据（payUrl 之类）。
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    plan_code       TEXT NOT NULL DEFAULT '',
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'created',
    user_id         TEXT NOT NULL DEFAULT '',
    client_id       TEXT NOT NULL DEFAULT '',
    pairing_code_id TEXT NOT NULL DEFAULT '',
    alipay_trade_no TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0,
    paid_at         REAL NOT NULL DEFAULT 0,
    expires_at      REAL NOT NULL DEFAULT 0,
    meta            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at);
-- 订单流水（审计/对账）。`payload` 只放 JSON 元数据。
CREATE TABLE IF NOT EXISTS order_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL DEFAULT '',
    type     TEXT NOT NULL DEFAULT '',
    payload  TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id, at);
-- 支付回调原文 + 幂等。`trade_no` 是主键 —— 同一笔回调重复到达只会有第一行生效。
CREATE TABLE IF NOT EXISTS alipay_notify (
    trade_no TEXT PRIMARY KEY,
    order_id TEXT NOT NULL DEFAULT '',
    raw      TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    at       REAL NOT NULL DEFAULT 0,
    -- paid / closed / pending / conflict …；冲突也要留痕（迟到支付对账用）。
    status   TEXT NOT NULL DEFAULT ''
);
-- 权益归**人**（`user_id` 主键）。到期/退款时把 `status` 改掉即失效。
CREATE TABLE IF NOT EXISTS entitlements (
    user_id             TEXT PRIMARY KEY,
    plan_code           TEXT NOT NULL DEFAULT '',
    paid_until          REAL NOT NULL DEFAULT 0,
    daily_audio_minutes REAL NOT NULL DEFAULT 0,
    tts_chars           INTEGER NOT NULL DEFAULT 0,
    qps                 INTEGER NOT NULL DEFAULT 1,
    max_devices         INTEGER NOT NULL DEFAULT 3,
    max_concurrent      INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'active',
    order_id            TEXT NOT NULL DEFAULT '',
    updated_at          REAL NOT NULL DEFAULT 0
);
"""

#: `CREATE TABLE IF NOT EXISTS` 对**已经存在**的表不会补列 ——
#: 已经跑过的服务端（开发机上的 `data/backend-dev/auth.db`、容器里那个 state 卷）
#: 不会因为改了 SCHEMA 就多出列来。所以这里做一次**最小的**补列迁移：只加列，不改类型、
#: 不删列、不搬数据。再多就需要真正的迁移工具了（版本表 + 顺序执行）——
#: 那时把这段换掉，别在上面长出第二套逻辑。
_ADDED_COLUMNS = {
    "clients": (
        # 每客户端专属的每日音频分钟数配额（0 = 用全局默认，见 server/quota.py）。
        ("daily_audio_minutes", "REAL NOT NULL DEFAULT 0"),
        # 轮换宽限期（设计 §7.5 ⑤）：旧 secret 还能用一阵。
        ("secret_rotated_at", "REAL NOT NULL DEFAULT 0"),
        ("prev_secret_hash", "TEXT NOT NULL DEFAULT ''"),
        ("prev_secret_expires_at", "REAL NOT NULL DEFAULT 0"),
        # **已撤销标记**（2026-10-06）。撤销原本只是 `token_version += 1`，行留在表里 ——
        # 那是对的（`revoke()` 的注释：那一行是管理员的账）。但没有标记时，管理面的
        # 「客户端」列表会把**历次配对动作**留下的行全平铺出来：本机每重新配对一次就多一行
        # （用户实测 dev 12 行全叫"本机客户端"），而那一页的语义是"成功配对的客户端"。
        # 有了这一列，列表默认只显示**未撤销**的，历史仍可查（`include_revoked`）。
        ("revoked_at", "REAL NOT NULL DEFAULT 0"),
    ),
    "users": (
        # 注册邀请码（登录时只验"属于该 user"；过期不等于账号失效）。
        ("invite_code", "TEXT NOT NULL DEFAULT ''"),
    ),
    "alipay_notify": (
        # 回调状态（paid/closed/conflict…），迟到支付/冲突对账用。
        ("status", "TEXT NOT NULL DEFAULT ''"),
    ),
    "pairing_codes": (
        ("name", "TEXT NOT NULL DEFAULT ''"),
        ("scopes", "TEXT NOT NULL DEFAULT ''"),
        # 收费（2026-11-03）：这张码属于哪个 user、是哪一类码。
        # `purchase` = 支付成功发的配对码；`device-bind` = 换机/重装的一次性绑定码。
        # 空 = 老语义（管理员发码 / 本机自动配对），兑换后不绑 user。
        ("user_id", "TEXT NOT NULL DEFAULT ''"),
        ("kind", "TEXT NOT NULL DEFAULT ''"),
    ),
}


class Store:
    """鉴权元数据的读写。**线程安全**（`check_same_thread=False` + 一把锁）。

    服务端是单进程多线程（ASGI 线程池 + 加载线程），所以并发是真的。
    一把粗锁足够：这些操作都是微秒级的几条语句，而且**每个请求最多碰一次**
    （`AuthCache` 会把它挡住）。
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate_locked()
            self._db.commit()

    def _migrate_locked(self) -> None:
        """补上 `_ADDED_COLUMNS` 里那些列。必须在持 `_lock` 时调用。"""
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in
                    self._db.execute('PRAGMA table_info("%s")' % table).fetchall()}
            for name, decl in cols:
                if name not in have:
                    self._db.execute('ALTER TABLE "%s" ADD COLUMN %s %s'
                                     % (table, name, decl))

    # ---------------------------------------------------------------- 自检

    def tables(self) -> List[str]:
        with self._lock:
            return self._tables_locked()

    def _tables_locked(self) -> List[str]:
        """必须在持 `_lock` 时调用。**不要**在这里调 `self.tables()` ——
        `_lock` 是不可重入的 `Lock`，套着拿会**死锁**（写这个文件时就踩了一次：
        `columns()` 里调了 `tables()`，测试直接挂住不返回）。
        """
        rows = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def columns(self) -> Dict[str, List[str]]:
        """表 → 列名。给护栏测试与将来的"存了什么"自证页用（设计 §8.4）。"""
        out: Dict[str, List[str]] = {}
        with self._lock:
            for t in self._tables_locked():
                rows = self._db.execute('PRAGMA table_info("%s")' % t).fetchall()
                out[t] = [r["name"] for r in rows]
        return out

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- 客户端

    def upsert_client(self, client_id: str, name: str, secret_hash: str,
                      scopes: str, created_by: str = "") -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO clients (client_id, name, secret_hash, scopes, "
                "  token_version, disabled, created_at, updated_at, last_seen) "
                "VALUES (?,?,?,?,1,0,?,?,0) "
                "ON CONFLICT(client_id) DO UPDATE SET "
                "  name=excluded.name, secret_hash=excluded.secret_hash, "
                "  scopes=excluded.scopes, updated_at=excluded.updated_at",
                (client_id, name, secret_hash, scopes, now, now))
            self._db.commit()

    def client(self, client_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM clients WHERE client_id=?",
                                   (client_id,)).fetchone()
        return dict(row) if row else None

    def clients(self, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """客户端行。**默认不含"已撤销"的**（2026-10-06）。

        为什么默认不收起来（用户给的口径："客户端下列出的应该是**成功配对的客户端**列表，
        不是配对动作"）：本机每重新配对一次就会新建一个客户端（`Auth.redeem()` 里
        `new_client_id()`），于是列表里堆着一串"曾经配过、已经不再用"的行 —— dev 实测
        12 行全叫「本机客户端」。它们每一行都真的配成功过，所以列表并非说谎，但它把
        "历史配对动作"和"现在在用"平铺在一起，读起来像有 12 个客户端。

        行**不删**（`revoke()` 的注释：那一行是管理员的账），`include_revoked=True`
        时照旧全都返回 —— 排查"这台机器配过几次"仍然查得到。
        """
        with self._lock:
            sql = "SELECT * FROM clients"
            if not include_revoked:
                sql += " WHERE COALESCE(revoked_at, 0) = 0"
            sql += " ORDER BY created_at"
            rows = self._db.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def touch(self, client_id: str) -> None:
        """记一次"这个客户端还活着"。**刻意不每请求写** —— 由调用方节流。"""
        with self._lock:
            self._db.execute("UPDATE clients SET last_seen=? WHERE client_id=?",
                             (time.time(), client_id))
            self._db.commit()

    def clients_max_updated_at(self) -> float:
        """`MAX(updated_at)` —— 多实例"发现别人改过客户端"的探针（设计 §7.5 ④）。

        为什么是这一条而不是 `MAX(token_version)`：**撤销、禁用、轮换都要能被发现**，
        而它们改的列不一样。`updated_at` 是它们的共同痕迹，一句聚合就够了。
        代价是"改了但没动 updated_at"的写入发现不了 —— 所以本文件里
        **每一处改 `clients` 的地方都显式写了 `updated_at`**。
        """
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(updated_at), 0) AS m FROM clients").fetchone()
        return float(row["m"] or 0.0)

    def revoke(self, client_id: str) -> int:
        """撤销 = `token_version += 1` + 盖 `revoked_at`（设计 §7.5 ④）。返回新版本号。

        **不删行、也不销毁 secret**：删行会让"这个客户端存在过"这件事消失（审计要用），
        而 secret 留着是为了 `revive()` 那条"误标一次能回退"的路 —— 要连凭据一起废掉
        就换 `rotate_secret()`。

        ⚠️ 只推版本号**不足以**挡住一个真实客户端：它手上还有 secret，会拿它换新令牌
        （`/v1/token`），所以撤销的"另一半"在 `Auth.token_for` 里（看 `revoked_at`）。
        2026-11-03 真机踩过：只推版本号时，撤销后的客户端仍能持续转写。

        2026-10-06 补：**同时盖上 `revoked_at`**。撤销过的客户端不该继续占着
        管理面「客户端」列表 —— 那一页要的是"成功配对的客户端"（用户给的口径），
        而本机每重新配对一次就多一行（dev 实测 12 行全叫"本机客户端"）。
        行**不删**，只是默认不再列出来（`Store.clients(include_revoked=True)` 仍可查）。
        """
        with self._lock:
            now = time.time()
            self._db.execute(
                "UPDATE clients SET token_version = token_version + 1, "
                "revoked_at = CASE WHEN revoked_at > 0 THEN revoked_at ELSE ? END, "
                "updated_at=? WHERE client_id=?", (now, now, client_id))
            self._db.commit()
            row = self._db.execute("SELECT token_version FROM clients WHERE client_id=?",
                                   (client_id,)).fetchone()
        return int(row["token_version"]) if row else 0

    def revive(self, client_id: str) -> bool:
        """把"已撤销"标记清掉（**不动** `token_version`）。

        为什么需要：标记只是"从列表里收起来"，不是永久判决。误标一次要能回退，
        而且回退不该顺带把凭据又弄得不能用（版本号不归它管）。
        返回"确实清掉了一个标记吗"。

        ⚠️ 2026-11-03 起 `/v1/token` 与 `authenticate` 都看这个标记（撤销后不许
        再拿 secret 换新令牌），所以 `revive()` 也是"重新授权"的一环：
        清掉标记后，原来那份 secret 又能换令牌了（secret 没被销毁，见 `revoke()`）。
        正常路径上不需要它 —— 重新配对的 `Auth.redeem()` 会自己调一次。
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE clients SET revoked_at=0, updated_at=? WHERE client_id=? AND revoked_at>0",
                (time.time(), str(client_id or "")))
            self._db.commit()
        return bool(cur.rowcount)

    def set_disabled(self, client_id: str, disabled: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE clients SET disabled=?, updated_at=? WHERE client_id=?",
                             (1 if disabled else 0, time.time(), client_id))
            self._db.commit()

    def set_scopes(self, client_id: str, scopes: str) -> bool:
        """改一个客户端的 scopes。返回"确实改到了某个客户端吗"。

        **改完立即生效**（下一个请求就按新 scopes 判）：鉴权读的是**缓存里的行**，
        不是 JWT 里那个 `scopes` 声明 —— 后者只是签发给客户端看的。
        所以这里把 `updated_at` 也推一下，跨进程的轮询能发现（§7.5 ④）。
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE clients SET scopes=?, updated_at=? WHERE client_id=?",
                (str(scopes or ""), time.time(), client_id))
            self._db.commit()
            return bool(cur.rowcount)

    def set_quota(self, client_id: str, daily_audio_minutes: float) -> bool:
        """改一个客户端的**每日音频分钟数**上限（0 = 用全局默认）。返回改到了吗。

        **不推 `updated_at`**：它只影响这个客户端能被用多久，不影响"它是谁、能不能进"，
        所以不需要惊动 `RevocationWatcher`（那位的职责是让"撤销/禁用/换 secret"在别的
        进程里 ≤5 秒被发现）。改了配额还要所有进程立刻知道，得等有真正的需求再说。
        多实例下更要注意：**计数本来就不共享**（见 `quota.py` 开头），
        配额改了之后各实例各按自己的计数判。
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE clients SET daily_audio_minutes=? WHERE client_id=?",
                (max(0.0, float(daily_audio_minutes or 0.0)), client_id))
            self._db.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- 调用元数据（§7.3）

    def insert_calls(self, rows: List[Dict[str, Any]]) -> int:
        """批量写调用记录。返回写了几条。

        **批量**是有意的：`calls` 是异步写（`server/calls.py` 的后台线程），
        一条一条 commit 会让每条都付一次 fsync；攒一小批一次写完更省，
        而且**不阻塞请求**本来就是这条设计的目的（§7.3）。
        写失败不抛给调用方 —— 审计写不进去不该让一次已经成功的转写变成失败。
        """
        if not rows:
            return 0
        cols = ("ts", "client_id", "endpoint", "model_id", "audio_seconds",
                "queue_wait_ms", "duration_ms", "status", "error_code", "request_id")
        #: 缺字段时按列类型给默认值。**不能给 `None`**：这几列都是 `NOT NULL DEFAULT ''`，
        #: 显式写 `None` 会撞约束 → `IntegrityError` → 整批记录被丢掉（而调用方是后台线程，
        #: 只会在日志里留一句"写库失败"）。第一版就是这么写的，被
        #: `CallsTableTests.test_prune_removes_only_the_old_ones`（只传了两个字段）抓出来。
        blanks = {"ts": 0.0, "client_id": "", "endpoint": "", "model_id": "",
                  "audio_seconds": 0.0, "queue_wait_ms": 0, "duration_ms": 0,
                  "status": 0, "error_code": "", "request_id": ""}
        values = [tuple(r.get(c) if r.get(c) is not None else blanks[c] for c in cols)
                  for r in rows]
        with self._lock:
            try:
                self._db.executemany(
                    "INSERT INTO calls (%s) VALUES (%s)" % (", ".join(cols),
                                                            ", ".join("?" * len(cols))),
                    values)
                self._db.commit()
                return len(values)
            except Exception:
                return 0

    def recent_calls(self, limit: int = 50, client_id: str = "") -> List[Dict[str, Any]]:
        """最近若干条（新的在前）。面板/排障用。"""
        limit = max(1, min(1000, int(limit or 50)))
        with self._lock:
            if client_id:
                cur = self._db.execute(
                    "SELECT * FROM calls WHERE client_id=? ORDER BY ts DESC LIMIT ?",
                    (client_id, limit))
            else:
                cur = self._db.execute("SELECT * FROM calls ORDER BY ts DESC LIMIT ?",
                                       (limit,))
            return [dict(r) for r in cur.fetchall()]

    def calls_summary(self, since_ts: float) -> Dict[str, Any]:
        """按 `client_id × endpoint` 汇总一段时间内的调用（面板的"谁在用/出错多少"）。

        **p95 用 SQL 算不出来**（SQLite 没有百分位函数），所以这里取"这一组里第 95 百分位
        的那条的耗时"—— 数据量小（一天几千条）、而且是给人看的，不值得为它引入一个
        统计库。**如实说明它是这么算的**，别当成严格分位数。
        """
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT client_id, endpoint, COUNT(*) AS calls,"
                " SUM(CASE WHEN status >= 400 OR error_code <> '' THEN 1 ELSE 0 END) AS errors,"
                " SUM(audio_seconds) AS audio_seconds,"
                " AVG(duration_ms) AS avg_ms,"
                " MAX(duration_ms) AS max_ms"
                " FROM calls WHERE ts >= ?"
                " GROUP BY client_id, endpoint ORDER BY calls DESC", (float(since_ts),)
            ).fetchall()]
            for r in rows:
                cur = self._db.execute(
                    "SELECT duration_ms FROM calls WHERE ts >= ? AND client_id=? AND endpoint=?"
                    " ORDER BY duration_ms LIMIT 1 OFFSET ?",
                    (float(since_ts), r["client_id"], r["endpoint"],
                     max(0, int((r["calls"] or 0) * 0.95) - 1)))
                one = cur.fetchone()
                r["p95_ms"] = int(one["duration_ms"]) if one else int(r["avg_ms"] or 0)
        return {
            "since": float(since_ts),
            "total": sum(int(r["calls"] or 0) for r in rows),
            "errors": sum(int(r["errors"] or 0) for r in rows),
            "audioSeconds": round(sum(float(r["audio_seconds"] or 0) for r in rows), 2),
            "groups": rows,
        }

    def prune_calls(self, before_ts: float) -> int:
        """删掉 `before_ts` 之前的记录。返回删了几条。

        **这是"两个写卷保留策略相反"的那一半**（§9.3）：`clients` 那些凭据要长期留着，
        而调用记录是**可以老死**的 —— 留着的价值随时间迅速下降，而它每天都在长。
        """
        with self._lock:
            cur = self._db.execute("DELETE FROM calls WHERE ts < ?", (float(before_ts),))
            self._db.commit()
            return int(cur.rowcount or 0)

    def rotate_secret(self, client_id: str, secret_hash: str,
                      grace_hours: float = 0.0) -> Optional[Dict[str, Any]]:
        """轮换 secret（设计 §7.5 ⑤）。返回更新后的行；客户端不存在返回 None。

        **总是**把 `token_version` +1：任何已发出的 JWT 立刻失效。
        理由见 `Auth.rotate_secret` —— 宽限期是"给旧 secret 一条活路"，不是"给旧令牌"。

        `grace_hours > 0` 时**旧 secret 还能用来换令牌**直到过期（`prev_*` 两列）；
        `0`（默认）时旧的**立刻**失效，并且**把可能存在的宽限期一并清掉** ——
        泄漏之后补一次默认轮换，就该把之前开的那扇门也关上。
        """
        now = time.time()
        with self._lock:
            cur = self._db.execute("SELECT secret_hash FROM clients WHERE client_id=?",
                                   (client_id,)).fetchone()
            if cur is None:
                return None
            old_hash = str(cur["secret_hash"] or "")
            if grace_hours and grace_hours > 0:
                self._db.execute(
                    "UPDATE clients SET secret_hash=?, token_version=token_version+1,"
                    " secret_rotated_at=?, prev_secret_hash=?, prev_secret_expires_at=?,"
                    " updated_at=? WHERE client_id=?",
                    (secret_hash, now, old_hash, now + float(grace_hours) * 3600.0,
                     now, client_id))
            else:
                self._db.execute(
                    "UPDATE clients SET secret_hash=?, token_version=token_version+1,"
                    " secret_rotated_at=?, prev_secret_hash='', prev_secret_expires_at=0,"
                    " updated_at=? WHERE client_id=?",
                    (secret_hash, now, now, client_id))
            self._db.commit()
            row = self._db.execute("SELECT * FROM clients WHERE client_id=?",
                                   (client_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- 配对码

    # ---------------------------------------------------------------- 管理面账号（§8.4）

    def upsert_admin(self, username: str, password_hash: str) -> None:
        """建账号 / 重置密码。**只存哈希**（明文只在生成时打印一次）。"""
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO admin_users (username, password_hash, disabled, created_at,"
                " last_login) VALUES (?,?,0,?,0)"
                " ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
                (str(username or ""), str(password_hash or ""), now))
            self._db.commit()

    def admin(self, username: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM admin_users WHERE username=?",
                                   (str(username or ""),)).fetchone()
        return dict(row) if row else None

    def admins(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT username, disabled, created_at, last_login FROM admin_users"
                " ORDER BY username").fetchall()
        return [dict(r) for r in rows]

    def set_admin_disabled(self, username: str, disabled: bool) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE admin_users SET disabled=? WHERE username=?",
                                   (1 if disabled else 0, str(username or "")))
            self._db.commit()
            return bool(cur.rowcount)

    def delete_admin(self, username: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM admin_users WHERE username=?",
                                   (str(username or ""),))
            self._db.commit()
            return bool(cur.rowcount)

    def touch_admin_login(self, username: str) -> None:
        with self._lock:
            self._db.execute("UPDATE admin_users SET last_login=? WHERE username=?",
                             (time.time(), str(username or "")))
            self._db.commit()

    def audit(self, admin: str, action: str, target: str = "") -> None:
        """记一条管理动作。**只有元数据**：谁、做了什么、对谁 —— 没有内容。"""
        with self._lock:
            self._db.execute("INSERT INTO admin_audit (ts, admin, action, target)"
                             " VALUES (?,?,?,?)",
                             (time.time(), str(admin or ""), str(action or ""),
                              str(target or "")))
            self._db.commit()

    def recent_audit(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(500, int(limit or 50)))
        with self._lock:
            rows = self._db.execute("SELECT * FROM admin_audit ORDER BY ts DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 运行参数（§8.5）

    def limit_overrides(self) -> List[Dict[str, Any]]:
        """管理面配过的运行参数（`{name, value, updated_at, updated_by}`）。

        **空表 = 从没配过** —— 这时总并发等值走环境变量 / 出厂默认
        （谁是权威值见 `server/limits.py` 开头的优先级表）。
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT name, value, updated_at, updated_by FROM server_limits"
                " ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def set_limit_overrides(self, values: Dict[str, int], updated_by: str = "") -> None:
        """整批写入运行参数（值由调用方 `limits.parse()` 校验过，这里**不重复校验**）。"""
        now = time.time()
        with self._lock:
            for name, value in (values or {}).items():
                self._db.execute(
                    "INSERT INTO server_limits (name, value, updated_at, updated_by)"
                    " VALUES (?,?,?,?)"
                    " ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                    " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (str(name), int(value), now, str(updated_by or "")))
            self._db.commit()

    # ---------------------------------------------------------------- 对外公布地址（§8.5）

    def advertised_override(self) -> str:
        """管理面配过的**对外公布地址**。**空 = 从没配过** → 走 env / YAML / 自动探测。

        （谁是权威值见 `server/advertised.py` 开头的优先级表。）
        """
        return str((self.advertised_override_row() or {}).get("value") or "")

    def advertised_override_row(self) -> Dict[str, Any]:
        """那一行（含 `updated_at` / `updated_by`，页面要显示"谁什么时候改的"）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT name, value, updated_at, updated_by FROM server_advertised"
                " WHERE name='advertised_host'").fetchone()
        return dict(row) if row else {}

    def set_advertised_override(self, value: str, updated_by: str = "") -> None:
        """写入对外公布地址（值由调用方 `advertised.parse()` 校验过，这里**不重复校验**）。

        空串也照样写一行（= "管理员显式清空、回到自动探测"）——
        与"从没配过"在读出来的结果上一致，但页面上能看出"确实有人动过它"。
        """
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO server_advertised (name, value, updated_at, updated_by)"
                " VALUES ('advertised_host',?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (str(value or ""), now, str(updated_by or "")))
            self._db.commit()

    # ---------------------------------------------------------------- 配对码

    def put_pairing_code(self, code_hash: str, ttl_s: float, created_by: str = "",
                         name: str = "", scopes: str = "", client_id: str = "",
                         user_id: str = "", kind: str = "") -> None:
        """存一张待用的配对码。

        `name` / `scopes` 是**这张码将要创建的那个客户端**的身份 ——
        设计 §7.4 的"管理员新建客户端时填名字与 scope"就落在这里。
        不带着走的话，兑换出来的客户端只能拿到全局默认 scopes，
        "给这台机器只开 asr"就没法表达。

        `client_id`（2026-10-06 启用）—— **这张码属于哪个已存在的客户端**。
        空 = 照旧"兑换时新建一个客户端"（给陌生机器发码的语义）。
        非空 = 兑换时**复用那个身份、只轮换 secret**（给已知机器换凭据的语义）。
        这一列在建表时就有，但一直恒为空，于是"同一台机器每重新配对一次就多一个
        client_id"（dev 实测 12 行全叫「本机客户端」）——「一台机器一个身份」的根治点在这。

        `user_id` / `kind`（2026-11-03，收费）：这张码绑定的账号与用途。
        `kind=purchase` 是支付成功后发给新用户的码；`kind=device-bind` 是换机码
        （两者都复用这张表，**不新造第二套码**）。空 = 老语义（不绑账号）。
        """
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO pairing_codes "
                "(code_hash, created_at, expires_at, created_by, name, scopes, client_id,"
                " user_id, kind) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (code_hash, now, now + float(ttl_s), created_by, str(name or ""),
                 str(scopes or ""), str(client_id or ""), str(user_id or ""),
                 str(kind or "")))
            self._db.commit()

    def peek_pairing_code(self, code_hash: str) -> Optional[Dict[str, Any]]:
        """**只读**取一张码（不删）。给"兑换前的准入预检"用（设备上限/权益）。

        兑换本身仍然只能走 `take_pairing_code()`（原子取+删）；这里的读结果
        只是**预检**，权威判定在兑换那一刻。读不到 = 码不存在/已用/已被清。
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM pairing_codes WHERE code_hash=?",
                                   (str(code_hash or ""),)).fetchone()
        return dict(row) if row else None

    def take_pairing_code(self, code_hash: str) -> Optional[Dict[str, Any]]:
        """**取出并删除**（设计 §8.5："用掉即删"）。

        原子的"取"必须是 `DELETE ... RETURNING` 或事务 —— 否则两个客户端
        同时拿同一个码来换，会各自读到一行、各自配对成功一次。
        这里用 `BEGIN IMMEDIATE` + 查 + 删 + 提交，整段在锁里。
        """
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM pairing_codes WHERE code_hash=?", (code_hash,)).fetchone()
                if row is not None:
                    self._db.execute("DELETE FROM pairing_codes WHERE code_hash=?",
                                     (code_hash,))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return dict(row) if row else None

    def pairing_codes(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pairing_codes ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def delete_pairing_code(self, code_hash: str) -> bool:
        """作废一张**未使用**的配对码。返回"确实删掉了一张吗"。

        **表里活着的都是未使用的**：`take_pairing_code` 在兑换时就把行删掉了
        （设计 §8.5"用掉即删"），所以"作废"与"删掉"是同一件事 ——
        不需要额外的状态列（加列要走 §8.5 那道评审门）。
        返回 `False`（= 没有这张码）时**不是**错误状态的伪装：调用方要回 404，
        否则"我用了一个错误的 id"看起来像成功了。
        """
        with self._lock:
            cur = self._db.execute("DELETE FROM pairing_codes WHERE code_hash=?",
                                   (str(code_hash or ""),))
            self._db.commit()
            return bool(cur.rowcount)

    def sweep_pairing_codes(self, now: Optional[float] = None) -> int:
        """删掉过期的码。返回删了几条。

        过期的码本来就换不到（`expires_at` 会被校验），但**留着会让"待用配对码 0 行"
        这个自证页说谎** —— 设计 §8.4 明说那一页行数与实际库不一致就是 bug。
        """
        now = time.time() if now is None else now
        with self._lock:
            cur = self._db.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
            self._db.commit()
            return int(cur.rowcount or 0)

    # ================================================================ 收费（2026-11-03）
    #
    # 这一段的每个方法都只读写**管理数据**（套餐 / 账号 / 绑定 / 订单 / 权益），
    # 不碰任何请求内容。读写都走同一把 `_lock`，与其它表同一个口径。

    # ---------------------------------------------------------------- 套餐

    def plans(self, *, active_only: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            sql = "SELECT * FROM plans"
            if active_only:
                sql += " WHERE active=1"
            sql += " ORDER BY sort, code"
            rows = self._db.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def plan(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM plans WHERE code=?",
                                   (str(code or ""),)).fetchone()
        return dict(row) if row else None

    def seed_plans(self, rows: List[Dict[str, Any]]) -> int:
        """**只补缺**（`INSERT OR IGNORE`）：已经存在的套餐不覆盖。

        为什么不是 upsert：管理面改过价/下架过的套餐，不能每次重启又被种子覆盖回去
        —— 那会让"我在管理面改的价"看起来像没生效。返回补了几行。
        """
        cols = ("code", "name", "price_cents", "days", "daily_audio_minutes",
                "tts_chars", "qps", "max_devices", "max_concurrent", "active", "sort")
        added = 0
        with self._lock:
            for row in rows or []:
                if not row.get("code"):
                    continue
                values = tuple(row.get(c, 0) for c in cols)
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO plans (%s) VALUES (%s)"
                    % (", ".join(cols), ", ".join("?" * len(cols))), values)
                added += int(cur.rowcount or 0)
            self._db.commit()
        return added

    def upsert_plan(self, row: Dict[str, Any]) -> None:
        """建/改一个套餐（值由 `billing.parse_plan()` 校验过，这里不重复校验）。"""
        cols = ("code", "name", "price_cents", "days", "daily_audio_minutes",
                "tts_chars", "qps", "max_devices", "max_concurrent", "active", "sort")
        values = tuple(row.get(c, 0) for c in cols)
        with self._lock:
            self._db.execute(
                "INSERT INTO plans (%s) VALUES (%s) ON CONFLICT(code) DO UPDATE SET %s"
                % (", ".join(cols), ", ".join("?" * len(cols)),
                   ", ".join("%s=excluded.%s" % (c, c) for c in cols if c != "code")),
                values)
            self._db.commit()

    def delete_plan(self, code: str) -> bool:
        """**硬删**一个套餐（内部/测试用；管理面走 `set_plan_active(False)` 下架）。

        已有订单不受影响：订单里存的是当时的 `amount_cents` 与 plan_code，
        权益里也复制了额度 —— 删模板不会改变已卖出去的东西。但**未支付的订单**
        在支付回调时还要读 plan 才能算天数，所以管理面只下架、不硬删。
        """
        with self._lock:
            cur = self._db.execute("DELETE FROM plans WHERE code=?", (str(code or ""),))
            self._db.commit()
            return bool(cur.rowcount)

    def set_plan_active(self, code: str, active: bool) -> bool:
        """上架 / 下架（管理面的「删」）。下架后不能再下单，但已支付的订单
        仍然能读到这个 plan 算天数/额度。"""
        with self._lock:
            cur = self._db.execute("UPDATE plans SET active=? WHERE code=?",
                                   (1 if active else 0, str(code or "")))
            self._db.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- 账号

    def create_user(self, user_id: str, *, name: str = "", employee_no: str = "",
                    phone: str = "", alipay_open_id: str = "",
                    invite_code: str = "", status: str = "active") -> Dict[str, Any]:
        """建一个账号。`employee_no`/`alipay_open_id` 撞唯一索引时抛 `ValueError`。

        调用方（`account.register()`）会先查重，但唯一索引才是并发下的权威 ——
        两个请求同时用同一个工号注册时，先提交的那个赢，另一个必须拿到
        `employee_no_taken` 而不是两行。
        """
        now = time.time()
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO users (user_id, name, employee_no, invite_code, phone,"
                    " alipay_open_id, status, token_version, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,1,?,?)",
                    (str(user_id), str(name or ""), str(employee_no or ""),
                     str(invite_code or ""), str(phone or ""), str(alipay_open_id or ""),
                     str(status or "active"), now, now))
                self._db.commit()
        except sqlite3.IntegrityError as exc:
            # **必须回滚**：`INSERT` 撞唯一索引后事务仍是打开的；不回滚的话，
            # 下一个 `take_pairing_code` / `consume_invite` 的 `BEGIN IMMEDIATE`
            # 会报 "cannot start a transaction within a transaction"（HTTP 500）。
            self._db.rollback()
            raise ValueError(str(exc))
        return self.user(user_id) or {}

    def user(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE user_id=?",
                                   (str(user_id or ""),)).fetchone()
        return dict(row) if row else None

    def user_by_employee_no(self, employee_no: str) -> Optional[Dict[str, Any]]:
        if not str(employee_no or "").strip():
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE employee_no=?",
                                   (str(employee_no).strip(),)).fetchone()
        return dict(row) if row else None

    def user_by_alipay_open_id(self, open_id: str) -> Optional[Dict[str, Any]]:
        if not str(open_id or "").strip():
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM users WHERE alipay_open_id=?",
                                   (str(open_id).strip(),)).fetchone()
        return dict(row) if row else None

    def users(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: str, **fields) -> bool:
        allowed = {"name", "phone", "alipay_open_id", "invite_code", "status"}
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if not sets:
            return False
        with self._lock:
            cur = self._db.execute(
                "UPDATE users SET %s, updated_at=? WHERE user_id=?"
                % ", ".join("%s=?" % k for k, _ in sets),
                tuple(v for _, v in sets) + (time.time(), str(user_id)))
            self._db.commit()
            return bool(cur.rowcount)

    def bump_user_token_version(self, user_id: str) -> int:
        """撤销这个账号已发出的 userToken（logout / 重置设备）。返回新版本号。"""
        with self._lock:
            self._db.execute(
                "UPDATE users SET token_version=token_version+1, updated_at=?"
                " WHERE user_id=?", (time.time(), str(user_id)))
            self._db.commit()
            row = self._db.execute("SELECT token_version FROM users WHERE user_id=?",
                                   (str(user_id),)).fetchone()
        return int(row["token_version"]) if row else 0

    def delete_user(self, user_id: str) -> bool:
        """删一个刚建出来、但注册后半步失败的账号（邀请码被抢光的回滚路径）。

        **只给注册的回滚用**：正常删账号会让订单/权益变成孤儿，管理面没有这个动作。
        """
        with self._lock:
            cur = self._db.execute("DELETE FROM users WHERE user_id=?", (str(user_id),))
            self._db.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- 设备绑定

    def upsert_device(self, user_id: str, client_id: str, device_name: str = "") -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO user_devices (user_id, client_id, device_name, bound_at,"
                " last_seen, status) VALUES (?,?,?,?,?,'active')"
                " ON CONFLICT(client_id) DO UPDATE SET user_id=excluded.user_id,"
                " device_name=excluded.device_name, status='active', last_seen=excluded.last_seen",
                (str(user_id), str(client_id), str(device_name or ""), now, now))
            self._db.commit()

    def devices(self, user_id: str, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            sql = "SELECT * FROM user_devices WHERE user_id=?"
            if not include_revoked:
                sql += " AND status='active'"
            sql += " ORDER BY bound_at"
            rows = self._db.execute(sql, (str(user_id),)).fetchall()
        return [dict(r) for r in rows]

    def device_for_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM user_devices WHERE client_id=?",
                                   (str(client_id or ""),)).fetchone()
        return dict(row) if row else None

    def count_devices(self, user_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM user_devices WHERE user_id=? AND status='active'",
                (str(user_id),)).fetchone()
        return int(row["n"] if row else 0)

    def revoke_device(self, user_id: str, client_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE user_devices SET status='revoked' WHERE user_id=? AND client_id=?"
                " AND status='active'", (str(user_id), str(client_id)))
            self._db.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- 邀请码

    def put_invite(self, code: str, *, max_uses: int = 1, expires_at: float = 0,
                   created_by: str = "", note: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO invites (code, max_uses, used, expires_at, created_by, note)"
                " VALUES (?,?,0,?,?,?) ON CONFLICT(code) DO UPDATE SET"
                " max_uses=excluded.max_uses, expires_at=excluded.expires_at,"
                " created_by=excluded.created_by, note=excluded.note",
                (str(code or "").upper(), max(1, int(max_uses or 1)),
                 float(expires_at or 0), str(created_by or ""), str(note or "")))
            self._db.commit()

    def invite(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM invites WHERE code=?",
                                   (str(code or "").upper(),)).fetchone()
        return dict(row) if row else None

    def invites(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM invites ORDER BY code").fetchall()
        return [dict(r) for r in rows]

    def consume_invite(self, code: str, now: Optional[float] = None) -> bool:
        """原子地消费一次邀请码。返回"消费成功吗"（不存在/过期/用满 → False）。

        **判定与 +1 在同一个 `BEGIN IMMEDIATE` 里**：否则两个人同时用最后
        一次名额都能成功。
        """
        now = time.time() if now is None else float(now)
        normalized = str(code or "").upper()
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM invites WHERE code=?",
                                       (normalized,)).fetchone()
                if row is None:
                    self._db.rollback()
                    return False
                if float(row["expires_at"] or 0) and float(row["expires_at"]) < now:
                    self._db.rollback()
                    return False
                if int(row["used"] or 0) >= int(row["max_uses"] or 0):
                    self._db.rollback()
                    return False
                self._db.execute("UPDATE invites SET used=used+1 WHERE code=?",
                                 (normalized,))
                self._db.commit()
                return True
            except Exception:
                self._db.rollback()
                raise

    def delete_invite(self, code: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM invites WHERE code=?",
                                   (str(code or "").upper(),))
            self._db.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- 订单

    def create_order(self, row: Dict[str, Any]) -> None:
        cols = ("order_id", "plan_code", "amount_cents", "status", "user_id",
                "client_id", "pairing_code_id", "alipay_trade_no", "created_at",
                "paid_at", "expires_at", "meta")
        values = tuple(row.get(c, 0) for c in cols)
        with self._lock:
            self._db.execute(
                "INSERT INTO orders (%s) VALUES (%s)"
                % (", ".join(cols), ", ".join("?" * len(cols))), values)
            self._db.commit()

    def order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM orders WHERE order_id=?",
                                   (str(order_id or ""),)).fetchone()
        return dict(row) if row else None

    def update_order(self, order_id: str, **fields) -> bool:
        allowed = {"status", "pairing_code_id", "alipay_trade_no", "paid_at",
                   "expires_at", "client_id", "meta"}
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if not sets:
            return False
        with self._lock:
            cur = self._db.execute(
                "UPDATE orders SET %s WHERE order_id=?"
                % ", ".join("%s=?" % k for k, _ in sets),
                tuple(v for _, v in sets) + (str(order_id),))
            self._db.commit()
            return bool(cur.rowcount)

    def orders(self, *, status: str = "", user_id: str = "", limit: int = 200
               ) -> List[Dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 200)))
        sql = "SELECT * FROM orders"
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(str(status))
        if user_id:
            where.append("user_id=?")
            args.append(str(user_id))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def orders_created_since(self, since_ts: float, user_id: str = "") -> int:
        with self._lock:
            if user_id:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM orders WHERE created_at>=? AND user_id=?",
                    (float(since_ts), str(user_id))).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) AS n FROM orders WHERE created_at>=?",
                    (float(since_ts),)).fetchone()
        return int(row["n"] if row else 0)

    def insert_order_event(self, order_id: str, event_type: str,
                           payload: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO order_events (order_id, type, payload, at) VALUES (?,?,?,?)",
                (str(order_id), str(event_type), str(payload or ""), time.time()))
            self._db.commit()

    def order_events(self, order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM order_events WHERE order_id=? ORDER BY id",
                (str(order_id),)).fetchall()
        return [dict(r) for r in rows]

    def record_notify(self, trade_no: str, order_id: str, raw: str = "",
                      verified: bool = False, status: str = "",
                      overwrite: bool = False) -> bool:
        """记一条支付回调。**返回 False = 这笔回调之前已经记过**（幂等）。

        `trade_no` 是主键：重复/并发的同一笔通知只有第一条能写进去。
        `status` 用于区分 paid / closed / conflict；`overwrite=True` 时把已有行
        更新成最新状态（迟到支付冲突、非成功→成功升级时用）。
        """
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO alipay_notify (trade_no, order_id, raw, verified, at,"
                    " status) VALUES (?,?,?,?,?,?)",
                    (str(trade_no), str(order_id), str(raw or ""),
                     1 if verified else 0, time.time(), str(status or "")))
                self._db.commit()
            return True
        except sqlite3.IntegrityError:
            # 重复回调是支付宝重投的**常态**；同样必须回滚，否则下一次
            # `/v1/pair`（BEGIN IMMEDIATE）会 500。
            self._db.rollback()
            if not overwrite:
                return False
            with self._lock:
                self._db.execute(
                    "UPDATE alipay_notify SET order_id=?, raw=?, verified=?, at=?,"
                    " status=? WHERE trade_no=?",
                    (str(order_id), str(raw or ""), 1 if verified else 0,
                     time.time(), str(status or ""), str(trade_no)))
                self._db.commit()
            return True

    def notify_record(self, trade_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM alipay_notify WHERE trade_no=?",
                                   (str(trade_no),)).fetchone()
        return dict(row) if row else None

    def notify_records(self, limit: int = 100) -> List[Dict[str, Any]]:
        """最近的支付回调（管理面对账用；含 conflict 行）。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM alipay_notify ORDER BY at DESC LIMIT ?",
                (max(1, min(1000, int(limit or 100))),)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 权益

    def entitlement(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM entitlements WHERE user_id=?",
                                   (str(user_id or ""),)).fetchone()
        return dict(row) if row else None

    def entitlements(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM entitlements ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def upsert_entitlement(self, row: Dict[str, Any]) -> None:
        cols = ("user_id", "plan_code", "paid_until", "daily_audio_minutes",
                "tts_chars", "qps", "max_devices", "max_concurrent", "status",
                "order_id", "updated_at")
        values = tuple(row.get(c, 0) for c in cols)
        with self._lock:
            self._db.execute(
                "INSERT INTO entitlements (%s) VALUES (%s)"
                " ON CONFLICT(user_id) DO UPDATE SET %s"
                % (", ".join(cols), ", ".join("?" * len(cols)),
                   ", ".join("%s=excluded.%s" % (c, c) for c in cols if c != "user_id")),
                values)
            self._db.commit()

    def set_entitlement_status(self, user_id: str, status: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE entitlements SET status=?, updated_at=? WHERE user_id=?",
                (str(status or ""), time.time(), str(user_id)))
            self._db.commit()
            return bool(cur.rowcount)

    # ---------------------------------------------------------------- 收费事务
    #
    # 支付成功要同时改 4 处：`alipay_notify`（幂等）、`orders`（状态）、
    # `entitlements`（权益）、`order_events`（审计）。**必须同事务**：任何一步失败
    # 都不能留下"订单已 paid、权益没到账"的半成品（那会让重复回调永远返回
    # duplicate，权益再也补不上）。下面两个方法在**持 `_lock`** 时用
    # `BEGIN IMMEDIATE` 完成；`entitlement_fn` 是纯函数（Holding the lock 时不能再
    # 调本类的其它方法，会死锁）。

    _ENT_COLS = ("user_id", "plan_code", "paid_until", "daily_audio_minutes",
                 "tts_chars", "qps", "max_devices", "max_concurrent", "status",
                 "order_id", "updated_at")

    def _upsert_entitlement_locked(self, row: Dict[str, Any]) -> None:
        cols = self._ENT_COLS
        values = tuple(row.get(c, 0) for c in cols)
        self._db.execute(
            "INSERT INTO entitlements (%s) VALUES (%s)"
            " ON CONFLICT(user_id) DO UPDATE SET %s"
            % (", ".join(cols), ", ".join("?" * len(cols)),
               ", ".join("%s=excluded.%s" % (c, c) for c in cols if c != "user_id")),
            values)

    def _event_locked(self, order_id: str, event_type: str, payload: str = "") -> None:
        self._db.execute(
            "INSERT INTO order_events (order_id, type, payload, at) VALUES (?,?,?,?)",
            (str(order_id), str(event_type), str(payload or ""), time.time()))

    @staticmethod
    def _meta_load(text: Any) -> Dict[str, Any]:
        try:
            got = json.loads(str(text or "") or "{}")
        except (TypeError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    def settle_paid_order(self, order_id: str, trade_no: str, notify_raw: str,
                          verified: bool, now: float, plan: Dict[str, Any],
                          entitlement_fn) -> Dict[str, Any]:
        """**原子**落一笔已验签的成功支付。

        `entitlement_fn(existing_row, plan)` 是纯函数（不能在锁内再调 Store）；
        它返回新的权益行。失败时整个事务回滚 —— 订单仍是 `paying`，回调可重试，
        **不会**出现"paid 但没权益"。

        返回 `{"ok": True, "order":…, "entitlement":…}` / `{"duplicate": True, …}`
        / `{"conflict": status, …}` / `{"retry": True, …}`（另一个线程刚抢到状态）。
        """
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM orders WHERE order_id=?",
                                       (str(order_id),)).fetchone()
                if row is None:
                    self._db.rollback()
                    return {"ok": False, "not_found": True}
                order = dict(row)
                current = str(order.get("status") or "")
                if current == "paid":
                    self._db.rollback()
                    return {"ok": False, "duplicate": True, "order": order}
                if current not in ("created", "paying"):
                    self._db.rollback()
                    return {"ok": False, "conflict": current, "order": order}
                try:
                    self._db.execute(
                        "INSERT INTO alipay_notify (trade_no, order_id, raw, verified,"
                        " at, status) VALUES (?,?,?,?,?,?)",
                        (str(trade_no), str(order_id), str(notify_raw or ""),
                         1 if verified else 0, float(now), "paid"))
                except sqlite3.IntegrityError:
                    # 同一 trade_no 之前可能只记过**非成功**通知（closed/pending）：
                    # 订单还没 paid 且这条记录属于同一订单 → **升级为成功并继续结算**；
                    # 只有订单已 paid（或记录属于别的订单）才算真 duplicate。
                    prev = self._db.execute(
                        "SELECT order_id FROM alipay_notify WHERE trade_no=?",
                        (str(trade_no),)).fetchone()
                    if (str(order.get("status") or "") == "paid"
                            or prev is None
                            or str(prev["order_id"] or "") != str(order_id)):
                        self._db.rollback()
                        return {"ok": False, "duplicate": True, "order": order}
                    self._db.execute(
                        "UPDATE alipay_notify SET raw=?, verified=?, at=?, status='paid'"
                        " WHERE trade_no=?",
                        (str(notify_raw or ""), 1 if verified else 0, float(now),
                         str(trade_no)))
                ent_row = self._db.execute(
                    "SELECT * FROM entitlements WHERE user_id=?",
                    (str(order.get("user_id") or ""),)).fetchone()
                existing = dict(ent_row) if ent_row else None
                entitlement = entitlement_fn(existing, plan)
                meta = self._meta_load(order.get("meta"))
                meta["tradeNo"] = str(trade_no)
                meta["fulfilled"] = True
                cur = self._db.execute(
                    "UPDATE orders SET status='paid', paid_at=?, alipay_trade_no=?,"
                    " meta=? WHERE order_id=? AND status IN ('created','paying')",
                    (float(now), str(trade_no), json.dumps(meta, ensure_ascii=False,
                                                            sort_keys=True),
                     str(order_id)))
                if int(cur.rowcount or 0) != 1:
                    self._db.rollback()
                    # **不能在持锁时调 self.order()**（_lock 不可重入）。返回旧行，
                    # 调用方重读即可。
                    return {"ok": False, "retry": True, "order": order}
                if entitlement:
                    self._upsert_entitlement_locked(entitlement)
                self._event_locked(order_id, "paid",
                                   json.dumps({"tradeNo": str(trade_no)},
                                              ensure_ascii=False))
                if entitlement:
                    self._event_locked(order_id, "entitlement",
                                       json.dumps({"plan": entitlement.get("plan_code"),
                                                   "paidUntil": entitlement.get("paid_until")},
                                                  ensure_ascii=False))
                self._db.commit()
                # 不回头调 self.order()（持锁会死锁）：用已读行 + 本次写入的结果。
                updated = dict(order)
                updated["status"] = "paid"
                updated["paid_at"] = float(now)
                updated["alipay_trade_no"] = str(trade_no)
                updated["meta"] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
                return {"ok": True, "order": updated, "entitlement": entitlement}
            except Exception:
                self._db.rollback()
                raise

    def repair_paid_order(self, order_id: str, now: float, plan: Dict[str, Any],
                          entitlement_fn) -> Dict[str, Any]:
        """重复回调的自愈：订单已 `paid` 但 `meta.fulfilled` 不是真 → 补权益。

        老版本/中途失败才会出现这种行；正常路径由 `settle_paid_order` 一次写完。
        """
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM orders WHERE order_id=?",
                                       (str(order_id),)).fetchone()
                if row is None:
                    self._db.rollback()
                    return {"ok": False, "not_found": True}
                order = dict(row)
                if str(order.get("status") or "") != "paid":
                    self._db.rollback()
                    return {"ok": False, "not_paid": True, "order": order}
                meta = self._meta_load(order.get("meta"))
                ent_row = self._db.execute(
                    "SELECT * FROM entitlements WHERE user_id=?",
                    (str(order.get("user_id") or ""),)).fetchone()
                # 已 fulfilled **且**权益行还在 → 真的不用补；权益行丢了则继续补。
                if bool(meta.get("fulfilled")) and ent_row is not None:
                    self._db.rollback()
                    return {"ok": True, "already": True, "order": order}
                entitlement = entitlement_fn(dict(ent_row) if ent_row else None, plan)
                meta["fulfilled"] = True
                self._db.execute(
                    "UPDATE orders SET meta=? WHERE order_id=?",
                    (json.dumps(meta, ensure_ascii=False, sort_keys=True),
                     str(order_id)))
                if entitlement:
                    self._upsert_entitlement_locked(entitlement)
                    self._event_locked(order_id, "entitlement-repair",
                                       json.dumps({"plan": entitlement.get("plan_code"),
                                                   "paidUntil": entitlement.get("paid_until")},
                                                  ensure_ascii=False))
                self._db.commit()
                # 不回头调 self.order()（持锁会死锁）：用已读行 + 本次写入的结果。
                updated = dict(order)
                updated["meta"] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
                return {"ok": True, "order": updated, "entitlement": entitlement}
            except Exception:
                self._db.rollback()
                raise

    def install_pair_code(self, order_id: str, expected_code_id: str,
                          code_row: Dict[str, Any], meta_extra: Dict[str, Any],
                          now: Optional[float] = None) -> Dict[str, Any]:
        """给订单安装一张新配对码；**CAS** 保证并发轮询只装一张。

        `expected_code_id` 是调用方读到的当前值：如果事务里已经变了，说明另一个
        线程刚装好，返回 `raced=True` 让调用方重读。旧码在这里删除（旧码本来就
        已过期/失效），新码与订单的 `pairing_code_id` 同事务写入。
        """
        now = time.time() if now is None else float(now)
        expected = str(expected_code_id or "")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM orders WHERE order_id=?",
                                       (str(order_id),)).fetchone()
                if row is None:
                    self._db.rollback()
                    return {"ok": False, "not_found": True}
                order = dict(row)
                current = str(order.get("pairing_code_id") or "")
                if current != expected:
                    self._db.rollback()
                    return {"ok": False, "raced": True, "order": order}
                if expected:
                    old = self._db.execute(
                        "SELECT expires_at FROM pairing_codes WHERE code_hash=?",
                        (expected,)).fetchone()
                    if old is not None and float(old["expires_at"] or 0) >= now:
                        self._db.rollback()
                        return {"ok": False, "valid": True, "order": order}
                if expected:
                    self._db.execute("DELETE FROM pairing_codes WHERE code_hash=?",
                                     (expected,))
                self._db.execute(
                    "INSERT INTO pairing_codes (code_hash, created_at, expires_at,"
                    " created_by, name, scopes, client_id, user_id, kind)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(code_row.get("code_hash") or ""), now,
                     now + float(code_row.get("ttl_s") or 0),
                     str(code_row.get("created_by") or ""),
                     str(code_row.get("name") or ""),
                     str(code_row.get("scopes") or ""),
                     str(code_row.get("client_id") or ""),
                     str(code_row.get("user_id") or ""),
                     str(code_row.get("kind") or "")))
                meta = self._meta_load(order.get("meta"))
                meta.update(meta_extra or {})
                cur = self._db.execute(
                    "UPDATE orders SET pairing_code_id=?, meta=?"
                    " WHERE order_id=? AND pairing_code_id=?",
                    (str(code_row.get("code_hash") or ""),
                     json.dumps(meta, ensure_ascii=False, sort_keys=True),
                     str(order_id), expected))
                if int(cur.rowcount or 0) != 1:
                    self._db.rollback()
                    return {"ok": False, "raced": True, "order": order}
                self._event_locked(order_id, "pair-code",
                                   json.dumps({"codeId": str(code_row.get("code_hash") or "")},
                                              ensure_ascii=False))
                self._db.commit()
                updated = dict(order)
                updated["pairing_code_id"] = str(code_row.get("code_hash") or "")
                updated["meta"] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
                return {"ok": True, "order": updated}
            except Exception:
                self._db.rollback()
                raise

    def close_expired_orders(self, now: Optional[float] = None,
                             grace_s: float = 0.0) -> List[str]:
        """把过了 `expires_at` 的未支付订单关掉（`billing.order_ttl_s` 的执行者）。"""
        now = time.time() if now is None else float(now)
        cutoff = now - max(0.0, float(grace_s or 0.0))
        closed: List[str] = []
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                rows = self._db.execute(
                    "SELECT order_id FROM orders WHERE status IN ('created','paying')"
                    " AND expires_at > 0 AND expires_at < ?", (cutoff,)).fetchall()
                for r in rows:
                    oid = str(r["order_id"])
                    self._db.execute(
                        "UPDATE orders SET status='closed' WHERE order_id=?"
                        " AND status IN ('created','paying')", (oid,))
                    self._event_locked(oid, "expired", "")
                    closed.append(oid)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return closed
