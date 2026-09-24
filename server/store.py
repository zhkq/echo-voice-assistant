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

import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

#: 设计 §8.5 的表白名单（**唯一**一份）。`CREATE TABLE` 只允许出现在这里。
TABLE_WHITELIST = ("clients", "pairing_codes")

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
    ),
    "pairing_codes": (
        ("name", "TEXT NOT NULL DEFAULT ''"),
        ("scopes", "TEXT NOT NULL DEFAULT ''"),
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

    def clients(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM clients ORDER BY created_at").fetchall()
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
        """撤销 = `token_version += 1`（设计 §7.5 ④）。返回新版本号。

        **这是撤销能"立即生效"的全部机制** —— 不删行，只把版本推上去。
        删行会让"这个客户端存在过"这件事消失（审计要用），而且并发下
        "读不到"和"已撤销"在客户端看来是同一个 401，但原因不同。
        """
        with self._lock:
            self._db.execute(
                "UPDATE clients SET token_version = token_version + 1, updated_at=? "
                "WHERE client_id=?", (time.time(), client_id))
            self._db.commit()
            row = self._db.execute("SELECT token_version FROM clients WHERE client_id=?",
                                   (client_id,)).fetchone()
        return int(row["token_version"]) if row else 0

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

    def rotate_secret(self, client_id: str, secret_hash: str) -> Optional[Dict[str, Any]]:
        """轮换 secret（设计 §7.5 ⑤）。返回更新后的行；客户端不存在返回 None。

        v1 **不带宽限期**：旧 secret 立刻失效。而且**同时把 `token_version` +1** ——
        理由：v1 里轮换之后客户端本来就必须重新配对（新 secret 只能由管理员带外交给用户），
        既然它已经必须重新配对，就没必要再留一个"旧 JWT 还能用最多 1 小时"的窗口。
        留着那个窗口反而更容易让人误以为"轮换失败了"。
        宽限期（`prev_secret_hash` + 24h 双 secret）是 v2 的事。
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE clients SET secret_hash=?, token_version=token_version+1, "
                "updated_at=? WHERE client_id=?",
                (secret_hash, time.time(), client_id))
            self._db.commit()
            if not cur.rowcount:
                return None
            row = self._db.execute("SELECT * FROM clients WHERE client_id=?",
                                   (client_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- 配对码

    def put_pairing_code(self, code_hash: str, ttl_s: float, created_by: str = "",
                         name: str = "", scopes: str = "") -> None:
        """存一张待用的配对码。

        `name` / `scopes` 是**这张码将要创建的那个客户端**的身份 ——
        设计 §7.4 的"管理员新建客户端时填名字与 scope"就落在这里。
        不带着走的话，兑换出来的客户端只能拿到全局默认 scopes，
        "给这台机器只开 asr"就没法表达。
        """
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO pairing_codes "
                "(code_hash, created_at, expires_at, created_by, name, scopes) "
                "VALUES (?,?,?,?,?,?)",
                (code_hash, now, now + float(ttl_s), created_by, str(name or ""),
                 str(scopes or "")))
            self._db.commit()

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
