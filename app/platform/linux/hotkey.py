# -*- coding: utf-8 -*-
"""Linux 全局热键（接缝实现）：转发给 POSIX 共享实现（``pynput``，X11 下可用）。

Linux 不是当前交付目标，但接缝要齐（守卫测试会断言三平台实现同一组原语）。
"""

from app.platform._posix_hotkey import (        # noqa: F401
    HOTKEY_SETTING_KEYS, HotkeyListener, _to_pynput, run_once)
