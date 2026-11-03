# -*- coding: utf-8 -*-
"""短期委托 token 的撤销表（进程内）。

委托 token 默认 1 小时 / 最长 24 小时；但"撤销"必须在**下一个请求**就生效，
不能等 TTL。这张表按 `sub`（`phone:<deviceId>`）记撤销时间；多实例部署时各进程
各一份 —— 与现有 `quota` 的取舍一致，文档写明。
"""
from __future__ import annotations

import threading
import time
from typing import Dict

_lock = threading.Lock()
#: subject -> 撤销记录到期时间（撤销本身保留 25 小时，覆盖最长 token TTL）
_revoked: Dict[str, float] = {}
_KEEP_S = 25 * 3600.0


def revoke(subject: str) -> None:
    key = str(subject or "").strip()
    if not key:
        return
    if not key.startswith("phone:"):
        key = "phone:" + key
    with _lock:
        _now = time.time()
        for k, exp in list(_revoked.items()):
            if exp < _now:
                _revoked.pop(k, None)
        _revoked[key] = _now + _KEEP_S


def is_revoked(subject: str) -> bool:
    key = str(subject or "").strip()
    if not key.startswith("phone:"):
        key = "phone:" + key
    now = time.time()
    with _lock:
        exp = _revoked.get(key)
        if exp is None:
            return False
        if exp < now:
            _revoked.pop(key, None)
            return False
        return True


def reset() -> None:
    with _lock:
        _revoked.clear()
