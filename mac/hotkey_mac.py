# -*- coding: utf-8 -*-
"""hotkey_mac.py — **兼容薄壳**（1.x 的注入式入口用）

真正实现在接缝里：``app/platform/darwin/hotkey.py`` → ``app/platform/_posix_hotkey.py``。
P3 把实现搬进 ``app/platform/`` 之后，这里只做转发，避免两份实现分叉。

历史原因：``mac/run_mac.py`` 会在 import ``app.main`` **之前**把本模块塞进
``sys.modules["app.hotkey"]``（1.x 的注入式入口，D17 要求收掉）。那份入口在
过渡期仍然可用，所以这个模块名要留着。
"""
from app.platform._posix_hotkey import (        # noqa: F401
    HOTKEY_SETTING_KEYS, HotkeyListener, _to_pynput, run_once)

__all__ = ["HotkeyListener", "run_once", "HOTKEY_SETTING_KEYS"]
