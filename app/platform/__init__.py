# -*- coding: utf-8 -*-
"""app.platform —— 平台接缝的唯一入口（2.0 / D10–D12）

规则（由 ``tests/test_platform_contract.py`` 钉住）：

  **平台差异只允许出现在 ``app/platform/<os>/`` 里。** ``app/`` 下的其它模块一律通过
  本包取平台默认值，不得自己写平台分支，也不得出现平台特征串（如 ``darwin``）。

这是 P1 的切片：目前只承载"环境默认值"（系统数据目录、危险目录前缀）。
P3 会把 ``config.DEFAULTS`` 的平台默认值与 mac 运行时的注入式覆盖一并收拢到这里
（D11/D17：接缝一次性分层做完，P1 只先立起目录与取值入口）。
"""
from __future__ import annotations

import importlib
import os
import sys

#: 已实现的平台目录名。新增平台 = 新增 app/platform/<name>/env.py，并在这里登记。
NAMES = ("win32", "darwin", "linux")


def current() -> str:
    """当前平台名。用 ``os.name`` 判定 Windows，其余看 ``sys.platform``。"""
    if os.name == "nt":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def module(name: str = ""):
    """取某个平台的 env 模块（默认当前平台）。"""
    return importlib.import_module("app.platform.%s.env" % (name or current()))


def defaults() -> dict:
    """当前平台的默认值。取不到就返回空字典——路径解析要能降级到通用值。"""
    try:
        return dict(getattr(module(), "PLATFORM_DEFAULTS", {}) or {})
    except Exception:
        return {}


def dangerous_prefixes() -> list:
    """当前平台下"不该放用户数据"的前缀，``[(前缀, 原因), ...]``（D21 校验用）。"""
    try:
        fn = getattr(module(), "dangerous_prefixes", None)
        return list(fn()) if callable(fn) else []
    except Exception:
        return []
