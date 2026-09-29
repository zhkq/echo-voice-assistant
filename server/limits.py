# -*- coding: utf-8 -*-
"""运行参数（总并发 / 每客户端并发 / 队列上限）的**权威值**。

## 优先级：**管理面配置 > 环境变量 > 出厂默认**

| 顺序 | 来源 | 落在哪 |
|---|---|---|
| 1 | 管理面配置 | 鉴权库的 `server_limits` 表（state 卷，**重启后还在**） |
| 2 | 环境变量 | `ECHO_MAX_CONCURRENT` / `ECHO_PER_CLIENT_CONCURRENT` / `ECHO_QUEUE_MAX` |
| 3 | 出厂默认 | `settings.DEFAULTS`（总并发 6，2026-09-29 从 2 提上来） |

## 为什么让管理面赢，而不是让环境变量赢

`server/compose.yaml` **默认就给容器设了** `ECHO_MAX_CONCURRENT`。若 env 优先，
那么"管理员在页面上把总并发改成 6"会**静默不生效** —— 页面显示 6、实际还是 2，
而排障的人会去翻代码和用例，不会想到是编排层那个变量。这正是本轮需求要避开的坑
（原话："这个应该是管理页面可以由管理员进行配置"）。

代价**如实呈现、不静默**：env 在场面且与管理面值不同时，
`GET /admin/api/limits` 会带一句 `notes`（"环境变量 ECHO_MAX_CONCURRENT=2 已设，
但当前以管理面配置的 6 为准"），启动日志里也打印同一句话。env 于是只剩一个作用：
**这台后端还没被管理面配过时**（空 state 卷的首启）给它一个起点。

## 热生效（不用重启容器）

`apply_stored()` 把管理面的值写进**同一个** `cfg.raw["limits"]`，而
`Admission`（`server/routes.py` 的两级闸门）与 `/v1/capabilities` **每次判定都现读**
`cfg` —— 所以保存完就是"下一个请求生效"。用例
`test_admin_console.LimitsConsoleTests.test_a_hot_change_is_seen_by_the_gate`
钉的是"真的被 503 顶回来"，不只是"库里读得到"。

## 队列上限（`queue_max`）目前只有宣告、没有执行者

v1 服务端**不排队**（设计 §3.6）：通道满了直接 `503 server_busy`。所以这个值现在
只影响 `/v1/capabilities` 里宣告的 `limits.queueMax`；把它改成 > 0 **不会真的开始排队**
（那要另做队列与 `429 queue_full`）。这一点在页面上写明，不让人以为改了就排队了。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from server import errors

#: 运行参数的**唯一一份**清单：内部键 / 接口字段 / 环境变量 / 取值范围 / 中文名。
#: 加一个参数只改这里 —— 读取、校验、页面、审计的话术都从它派生。
FIELDS: tuple = (
    {"key": "max_concurrent", "api": "maxConcurrent", "env": "ECHO_MAX_CONCURRENT",
     "min": 1, "max": 64, "label": "总并发"},
    {"key": "per_client_concurrent", "api": "perClientConcurrent",
     "env": "ECHO_PER_CLIENT_CONCURRENT", "min": 1, "max": 64, "label": "每客户端并发"},
    {"key": "queue_max", "api": "queueMax", "env": "ECHO_QUEUE_MAX",
     "min": 0, "max": 100, "label": "队列上限"},
)

BY_KEY: Dict[str, dict] = {f["key"]: f for f in FIELDS}
BY_API: Dict[str, dict] = {f["api"]: f for f in FIELDS}


# ---------------------------------------------------------------- 小工具

def _as_int(raw: Any) -> Optional[int]:
    """能当整数用就给出整数，否则 `None`（**不猜、不截断**）。

    `"6"` / `6` / `6.0` 都收；`""` / `"abc"` / `None` / `true` / `6.5` → `None`。
    `True` 也否掉：JSON 里 `true` 大多不是"1"的意思，而 `int(True)` 会静默变成 1
    —— 配置层最贵的错就是这种静默反向。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return int(raw)
    try:
        num = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return int(num) if float(num).is_integer() else None


def _rows(store) -> List[dict]:
    """库里的持久化行。没有库 / 读不出来 → 空（当作"从没配过"）。"""
    if store is None:
        return []
    try:
        return list(store.limit_overrides())
    except Exception:                                          # pragma: no cover - 兜底
        return []


def stored(store) -> Dict[str, int]:
    """管理面配过的值（只有配过的键）。空 = 从没配过 → 走 env / 出厂默认。"""
    out: Dict[str, int] = {}
    for row in _rows(store):
        key = str(row.get("name") or "")
        if key in BY_KEY:
            out[key] = int(row.get("value") or 0)
    return out


def _newest(store) -> dict:
    rows = _rows(store)
    if not rows:
        return {}
    return max(rows, key=lambda r: float(r.get("updated_at") or 0))


def env_values() -> Dict[str, int]:
    """环境变量里能认出来的那些（认不出来 = 没设，与 `settings._env_overrides` 同一判据）。"""
    out: Dict[str, int] = {}
    for f in FIELDS:
        got = _as_int(os.environ.get(f["env"], ""))
        if got is not None:
            out[f["key"]] = got
    return out


def effective(cfg) -> Dict[str, int]:
    """此刻**真正生效**的三个值（cfg 已经是"应用过管理面配置"的那一份）。"""
    out: Dict[str, int] = {}
    for f in FIELDS:
        try:
            out[f["key"]] = int(cfg.get("limits." + f["key"], f["min"]))
        except (TypeError, ValueError):
            out[f["key"]] = int(f["min"])
    return out


# ---------------------------------------------------------------- 应用 / 呈现

def apply_stored(cfg, store) -> Dict[str, int]:
    """把管理面配过的值写进 `cfg.raw["limits"]`（**覆盖** env 与出厂默认）。返回写进去的那些。

    进程启动时调一次（`main.create_app` 的 lifespan，赶在能力面闸门收到请求之前）。
    只覆盖**配过的键**：没配过的键继续吃 env / 默认，所以"只改了每客户端并发"
    不会把总并发一起钉死。
    """
    got = stored(store)
    if not got:
        return {}
    limits = cfg.raw.setdefault("limits", {})
    for key, value in got.items():
        limits[key] = value
    return got


def priority_notes(cfg, store=None) -> List[str]:
    """env 在场面、却被管理面盖住时的**如实提示**（一句话一条，没有就是空表）。

    "谁赢了"必须说出口：不说的话，`compose.yaml` 里那个 `ECHO_MAX_CONCURRENT`
    会让现场的人以为"我改了 env 怎么没变"（其实是管理面赢了），或者反过来。
    """
    saved, envs, now = stored(store), env_values(), effective(cfg)
    notes = []
    for f in FIELDS:
        key = f["key"]
        if key in saved and key in envs and envs[key] != now[key]:
            notes.append("环境变量 %s=%d 已设，但当前以管理面配置的 %d 为准。"
                         % (f["env"], envs[key], now[key]))
    return notes


def view(cfg, store=None) -> Dict[str, Any]:
    """`GET /admin/api/limits` 的响应体：生效值 + 每个值的来源 + 取值范围 + 如实提示。"""
    saved, envs, now = stored(store), env_values(), effective(cfg)
    limits: Dict[str, Any] = {}
    source: Dict[str, str] = {}
    ranges: Dict[str, list] = {}
    for f in FIELDS:
        key, api = f["key"], f["api"]
        limits[api] = now[key]
        ranges[api] = [int(f["min"]), int(f["max"])]
        if key in saved:
            source[api] = "admin"          # 管理面配置（权威）
        elif key in envs:
            source[api] = "env"            # 环境变量
        else:
            source[api] = "default"        # 出厂默认
    newest = _newest(store)
    return {
        "limits": limits,
        "source": source,
        "ranges": ranges,
        "labels": {f["api"]: f["label"] for f in FIELDS},
        "envNames": {f["api"]: f["env"] for f in FIELDS},
        # env 里**真的设了**什么（null = 没设）。与 limits 分开报，才能看出"谁盖了谁"。
        "envValues": {f["api"]: envs.get(f["key"]) for f in FIELDS},
        "priority": "admin",
        "priorityNote": "管理面配置 > 环境变量 > 出厂默认：在管理面里改过的值覆盖环境变量，"
                        "重启后仍在（存在 state 卷的库里）。",
        "notes": priority_notes(cfg, store),
        "updatedAt": float(newest.get("updated_at") or 0.0),
        "updatedBy": str(newest.get("updated_by") or ""),
        # 队列上限现在**没有执行者**（v1 不排队）—— 页面照着这句话显示，别让人误会。
        "queueNote": "v1 服务端不排队：队列上限现在只影响 /v1/capabilities 里宣告的 "
                     "queueMax；改成大于 0 不会真的开始排队。",
    }


# ---------------------------------------------------------------- 写入

def audit_target(payload: Optional[dict]) -> str:
    """审计里的"对象"那一栏（用请求里给的原始值，失败也留痕）。

    与 `server/admin.py` 既有的做法一致：表只有四列，所以"改了什么"编码进 `target`。
    """
    parts = []
    for f in FIELDS:
        if f["api"] in (payload or {}):
            parts.append("%s=%s" % (f["api"], (payload or {})[f["api"]]))
    return " ".join(parts) if parts else "(空请求)"


def parse(payload: Optional[dict], current: Dict[str, int]) -> Dict[str, int]:
    """校验请求体，返回要写入的 `{内部键: 值}`。**任何不合法都是 400，不做静默夹紧**。

    夹紧（把 999 悄悄当 64）会让"我明明填了 999"变成"其实只开了 64"，
    而改的人以为生效了 —— 与 `parse_ttl` 里那条"越界就是 400"同一个理由。
    """
    out: Dict[str, int] = {}
    for api, raw in (payload or {}).items():
        field = BY_API.get(str(api))
        if field is None:
            raise errors.bad_request("不认识的运行参数：%s（认这几个：%s）"
                                     % (api, "、".join(f["api"] for f in FIELDS)))
        value = _as_int(raw)
        if value is None:
            raise errors.bad_request("%s 要是整数（给的是 %r）" % (field["label"], raw))
        if value < field["min"] or value > field["max"]:
            raise errors.bad_request("%s要在 %d ~ %d 之间（给的是 %d）"
                                     % (field["label"], field["min"], field["max"], value))
        out[field["key"]] = value
    merged = dict(current or {})
    merged.update(out)
    if merged.get("per_client_concurrent", 1) > merged.get("max_concurrent", 1):
        raise errors.bad_request("每客户端并发（%d）不能大于总并发（%d）"
                                 % (merged["per_client_concurrent"], merged["max_concurrent"]))
    return out


def set_limits(cfg, store, payload: Optional[dict], *, updated_by: str = "") -> Dict[str, int]:
    """校验 → 落库 → **当场生效**（改 `cfg.raw`，闸门下一个请求就现读到）。

    先落库再改 `cfg`：库写失败就不该让内存里出现一个"看起来生效了、重启就没了"的值。
    """
    got = parse(payload, effective(cfg))
    if not got:
        raise errors.bad_request("要带至少一个运行参数：%s"
                                 % "、".join(f["api"] for f in FIELDS))
    if store is not None:
        store.set_limit_overrides(got, updated_by)
    cfg.raw.setdefault("limits", {}).update(got)
    return got
