# -*- coding: utf-8 -*-
"""macOS 全局热键（接缝实现）：转发给 POSIX 共享实现（``pynput``）。

见 ``app/platform/_posix_hotkey.py``：未实测、需辅助功能授权，是 D19/S10 尚未完成的部分。
"""

from app.platform._posix_hotkey import (        # noqa: F401
    HOTKEY_SETTING_KEYS, HotkeyListener, _to_pynput, run_once)
