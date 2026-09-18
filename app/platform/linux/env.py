# -*- coding: utf-8 -*-
"""Linux 平台环境默认值（非当前交付目标，先把接缝补齐，避免以后再动 app/）。

系统数据按 XDG：``$XDG_DATA_HOME/ECHO``，未设置则 ``~/.local/share/ECHO``。
"""
import os

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
