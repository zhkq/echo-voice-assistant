# -*- coding: utf-8 -*-
"""services.py — ECHO 组件状态注册表

面板 /api/status 的统一数据源：各组件（server/dsh/stt/tts/wake/hotkey/
meeting/diarize）上报 status/detail，内存实时 + DB 持久化（重启后可查）。

status 取值约定：online|offline|active|idle|error|disabled|unknown
"""
import os
import time

import app.db as db
from app import platform as echo_platform

COMPONENT_ORDER = ["server", "dsh", "harness", "stt", "tts", "wake", "hotkey", "meeting", "diarize"]


def _set(name, status, detail="", pid=0):
    db.set_component_state(name, status, detail, pid)


def report_server(pid=None):
    _set("server", "online", f"ECHO {_version()} · {echo_platform.display_name()}",
         pid or os.getpid())


def report_dsh(status, detail=""):
    _set("dsh", status, detail)


def report_harness(status, detail=""):
    """独立 DeepSeek Harness（@deepseek-ai/dsh，随 ECHO 启动的那个）的状态。"""
    _set("harness", status, detail)


def report_stt(status, detail=""):
    _set("stt", status, detail)


def report_tts(status, detail=""):
    _set("tts", status, detail)


def report_wake(status, detail=""):
    _set("wake", status, detail)


def report_hotkey(status, detail=""):
    _set("hotkey", status, detail)


def report_meeting(status, detail=""):
    _set("meeting", status, detail)


def report_diarize(status, detail=""):
    _set("diarize", status, detail)


def snapshot():
    """返回面板可渲染的状态快照（按固定顺序 + 时间）。"""
    states = {s["name"]: s for s in db.get_component_states()}
    out = []
    for name in COMPONENT_ORDER:
        s = states.get(name) or {"name": name, "status": "unknown",
                                 "detail": "", "pid": 0, "updated_at": ""}
        out.append(s)
    return out


def _version():
    try:
        from app import __version__
        return __version__
    except Exception:
        return "?"


# 简易启动时间（进程级）
_started_at = time.time()


def uptime():
    return int(time.time() - _started_at)
