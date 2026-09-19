# -*- coding: utf-8 -*-
"""Linux 平台环境默认值 + 平台原语（非当前交付目标，先把接缝补齐）。

系统数据按 XDG：``$XDG_DATA_HOME/ECHO``，未设置则 ``~/.local/share/ECHO``。
"""
import os

from app.platform import _posix

NAME = "linux"

PLATFORM_DEFAULTS = {
    "dataDir": os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"),
        "ECHO"),
}


def dangerous_prefixes():
    return [
        ("/usr", "不能放在系统目录下"),
        ("/etc", "不能放在系统目录下"),
        ("/boot", "不能放在系统目录下"),
        ("/sys", "不能放在系统目录下"),
        ("/proc", "不能放在系统目录下"),
    ]


def display_name() -> str:
    """状态文案里的系统名（与 ``platform.system()`` 在 Linux 上的取值一致）。"""
    return "Linux"


def chromium_candidates():
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ]

# ------------------------------------------------------------------ 展示 / 子进程

def no_window_creationflags() -> int:
    return 0


def agent_cli_candidates():
    """CodeBuddy CLI 的内置候选：本平台没有，返回空表。"""
    return []


def tcp_excluded_port_range_output() -> str:
    """本平台没有 Windows 那套 TCP 保留端口段，返回空串。"""
    return ""


# ------------------------------------------------------------------ HuggingFace 下载脚本

def hf_executable(install_root: str) -> str:
    return os.path.join(install_root, "venv", "bin", "hf")


def shell_script(hf: str, jobs) -> str:
    """把若干条 argv 渲染成 POSIX sh 下载脚本。"""
    import shlex
    lines = []
    for argv in jobs:
        lines.append("HF_ENDPOINT=https://huggingface.co HF_HUB_OFFLINE=0 "
                     + shlex.join([str(x) for x in argv]))
    return " &&\n".join(lines)


# ------------------------------------------------------------------ 单实例锁（flock）

def acquire_named_lock(lock_id, lock_path):
    return _posix.acquire(lock_path)


def named_lock_held(lock_id, lock_path) -> bool:
    return _posix.held(lock_path)


def release_named_lock(handle):
    return _posix.release(handle)
