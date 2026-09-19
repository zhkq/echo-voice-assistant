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


# ---------------------------------------------------------------- 运行时原语（P3 收口）
# 下面这些原语把"业务代码里的平台分支"收进接缝：app/ 的其它模块只调用它们，
# 不得再自己写 os.name / LOCALAPPDATA / platform.system()（由 tests/test_path_seam.py 钉住）。
# 每个原语在 app/platform/<os>/env.py 里各实现一份；缺实现时给安全的默认值。

def _platform_fn(name):
    """取当前平台 env.py 里的函数；没有则 None（不抛，业务侧不必层层 try）。"""
    try:
        return getattr(module(), name, None)
    except Exception:
        return None


def display_name() -> str:
    """系统名（状态文案用）。"""
    fn = _platform_fn("display_name")
    return fn() if callable(fn) else current()


def no_window_creationflags() -> int:
    """起控制台子进程时抑制黑窗的 creationflags（非 Windows = 0）。"""
    fn = _platform_fn("no_window_creationflags")
    return int(fn()) if callable(fn) else 0


def chromium_candidates():
    """Chromium 系浏览器可执行文件候选（调用方自行 expandvars / 判存在）。"""
    fn = _platform_fn("chromium_candidates")
    return list(fn()) if callable(fn) else []


def agent_cli_candidates():
    """CodeBuddy CLI 由平台专有安装位置带来的候选路径。"""
    fn = _platform_fn("agent_cli_candidates")
    return list(fn()) if callable(fn) else []


def tcp_excluded_port_range_output() -> str:
    """系统 TCP 保留端口段的原始命令输出（解析归 app/ports.py）。"""
    fn = _platform_fn("tcp_excluded_port_range_output")
    return str(fn()) if callable(fn) else ""


def hf_executable(install_root: str) -> str:
    """venv 里的 hf 命令行入口。"""
    fn = _platform_fn("hf_executable")
    if callable(fn):
        return str(fn(install_root))
    return os.path.join(install_root, "venv", "bin", "hf")


def shell_script(hf: str, jobs) -> str:
    """把若干条 argv 渲染成该平台的 shell 下载脚本。"""
    fn = _platform_fn("shell_script")
    return str(fn(hf, jobs)) if callable(fn) else ""


def acquire_named_lock(lock_id: str, lock_path: str):
    """获取内核级单实例锁。返回 (handle, detail)。**失败必须显式上报，不能吞。**"""
    return module().acquire_named_lock(lock_id, lock_path)


def named_lock_held(lock_id: str, lock_path: str) -> bool:
    return bool(module().named_lock_held(lock_id, lock_path))


def release_named_lock(handle) -> None:
    return module().release_named_lock(handle)


# ---------------------------------------------------------------- 清单用的平台标记
# 组件清单（components/*.json 与 app/components.py 的内置清单）要声明"支持哪些平台"，
# 但**不能**在 app/ 的其它地方写出平台特征串（D12 契约）。所以清单统一用中立标记
# （win32 / macos / linux），由本模块负责翻译与版本探测——平台分支只留在接缝里。
MANIFEST_NAMES = {"win32": "win32", "darwin": "macos", "linux": "linux"}
MANIFEST_TO_INTERNAL = {v: k for k, v in MANIFEST_NAMES.items()}


def manifest_name(name: str = "") -> str:
    """内部平台名（= ``current()`` 的取值）→ 清单标记。已是标记则原样返回。"""
    if not name:
        return MANIFEST_NAMES.get(current(), current())
    if name in MANIFEST_TO_INTERNAL:
        return name
    return MANIFEST_NAMES.get(name, name)


def internal_name(token: str) -> str:
    """清单标记 → 内部平台名（``macos`` → ``darwin``）。"""
    return MANIFEST_TO_INTERNAL.get(token, token)


def os_version(name: str = "") -> tuple:
    """系统版本（用于清单的 ``min_os``）。取不到返回空元组 = 不做版本限制。

    ``name`` 收内部平台名；不给则当前平台。
    """
    n = name or current()
    try:
        import platform as _p
        if n == "darwin":
            return tuple(int(x) for x in _p.mac_ver()[0].split(".")[:3] if x.isdigit())
        if n == "win32":
            return tuple(int(x) for x in _p.version().split(".")[:3] if x.isdigit())
    except Exception:
        pass
    return ()
