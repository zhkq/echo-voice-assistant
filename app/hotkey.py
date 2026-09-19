# -*- coding: utf-8 -*-
"""hotkey.py — 全局热键与媒体键（**门面**；实现按平台收在 app/platform/<os>/）

P3 搬迁说明（2026-09-19）
------------------------
原来这个文件自己就是 Windows 实现（模块级 ``ctypes.windll``），于是：
  * 非 Windows 上导入即失败（mac 侧只能靠 ``mac/run_mac.py`` 往 ``sys.modules`` 里
    塞替身，属于 1.x 的注入式入口，D17 要求收掉）；
  * "平台专有实现"与"业务调用面"混在一个文件里，守卫测试无法区分。

现在：
  * ``app/platform/win32/hotkey.py``   —— Windows：RegisterHotKey + 低级键盘钩子（ctypes）
  * ``app/platform/_posix_hotkey.py``  —— macOS/Linux：pynput 全局热键
  * 本文件只做转发：业务代码（``app/runtime.py``）继续 ``from app.hotkey import
    HotkeyListener``，不必知道平台怎么实现。

触发统一回调 ``on_trigger(source, detail)``：
    ('hotkey', 'Ctrl+Alt+C') / ('mediakey', 'vol_up')
"""
from app import platform as echo_platform

#: 当前平台的实现模块（Windows = win32/hotkey、macOS/Linux = _posix_hotkey）
impl = echo_platform.hotkey_impl()

HotkeyListener = impl.HotkeyListener
parse_hotkey_combo = getattr(impl, "parse_hotkey_combo", lambda combo: None)
run_once = getattr(impl, "run_once", None)

# 只转发**跨平台**的数据面：媒体键表与命名键表（调试脚本/文档据此对照）。
# Windows 的虚拟键码与修饰键位（MOD_* / WM_* / KBDLLHOOKSTRUCT）**故意不在这里出现**：
# 它们是平台专有 API 细节，需要的人应当直接 import app.platform.win32.hotkey
# —— 门面暴露它们就等于把平台差异漏回业务面（守卫测试 test_path_seam 会拦）。
MEDIA_KEYS = dict(getattr(impl, "MEDIA_KEYS", {}) or {})
NAMED_KEYS = dict(getattr(impl, "NAMED_KEYS", {}) or {})


if __name__ == "__main__":
    print(run_once() if run_once else "本平台没有 run_once（调试入口只在 Windows 实现里）")
