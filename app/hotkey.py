# -*- coding: utf-8 -*-
"""hotkey.py — ECHO 全局热键与媒体键监听（纯 ctypes，无 C# 编译依赖）

两条路径（都在一个监听线程内）：
  1. RegisterHotKey      注册组合键（Ctrl+Alt+C / Ctrl+Alt+V 等），WM_HOTKEY 派发
  2. WH_KEYBOARD_LL      低级键盘钩子，捕获媒体键（vol_up/vol_down/play_pause/
                          next/prev/mute），可选择拦截不向系统透传

触发统一回调 on_trigger(source, detail)：
    ('hotkey', 'Ctrl+Alt+C') / ('mediakey', 'vol_up')
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

# ---- Windows 常量 ----
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# 媒体键虚拟键码
MEDIA_KEYS = {
    "vol_up": 0xAF, "vol_down": 0xAE, "vol_mute": 0xAD,
    "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
}

# 命名键（多字符）→ 虚拟键码；单字符键直接 ord()，无需在此登记
NAMED_KEYS = {
    "SPACE": 0x20, "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09,
    "ESC": 0x1B, "ESCAPE": 0x1B, "BACKSPACE": 0x08, "DELETE": 0x2E,
    "INSERT": 0x2D, "HOME": 0x24, "END": 0x23,
    "PGUP": 0x21, "PAGEUP": 0x21, "PGDN": 0x22, "PAGEDOWN": 0x22,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    **{f"F{i}": 0x70 + (i - 1) for i in range(1, 25)},
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# 规范签名（修复媒体键钩子静默失效）：
# kernel32.GetModuleHandleW 默认按 c_int 返回，64 位句柄被截断成 32 位，
# 传给 SetWindowsHookExW 的 hMod 是坏指针 -> 要么安装失败(last_error 126)，
# 要么"装上但永远收不到事件"。给全部相关调用补上 restype/argtypes。
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HMODULE, wt.DWORD]
user32.CallNextHookEx.restype = ctypes.c_long
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]


def parse_hotkey_combo(combo):
    """'Ctrl+Alt+V' -> (mods, vk)；无效返回 None。

    支持单个字母/数字（A-Z、0-9）与命名键（F1-F12、Space、Enter、Esc 等）——
    RegisterHotKey 接到的是虚拟键码，Shift 不必换算成不同 vk（vk 用基础键即可）。

    修饰键必须是**认识的一个或多个**（2026-09-19 安全网修正）：原实现把不认识的
    修饰键静默忽略，于是 `"+V"` / `"Meta+V"` 会解析成 mods 只剩 MOD_NOREPEAT 的
    "全局裸键 V" —— RegisterHotKey 装上去会把用户系统里**所有** V 键都吞掉，
    而单写 `"V"` 早就被 len(parts)<2 拒掉了。判据现在一致：没有识别到修饰键 = 无效。
    """
    if not combo:
        return None
    parts = [p.strip() for p in combo.split("+")]
    if len(parts) < 2:
        return None
    last = parts[-1].upper()
    if len(last) == 1 and last.isalnum():
        vk = ord(last)
    else:
        vk = NAMED_KEYS.get(last)
    if not vk:
        return None
    mods = 0
    for m in parts[:-1]:
        ml = m.lower()
        if ml == "ctrl":
            mods |= MOD_CONTROL
        elif ml == "alt":
            mods |= MOD_ALT
        elif ml == "shift":
            mods |= MOD_SHIFT
        elif ml == "win":
            mods |= MOD_WIN
        else:
            return None      # 空修饰键 / 拼错的修饰键：拒绝，不退化成全局裸键
    if not mods:
        return None
    return mods | MOD_NOREPEAT, vk


class HotkeyListener(threading.Thread):
    """常驻热键监听线程。on_trigger(source, detail) 在后台线程回调。"""

    def __init__(self, settings_get, on_trigger=None, daemon=True):
        super().__init__(daemon=daemon)
        self.settings_get = settings_get
        self.on_trigger = on_trigger or (lambda source, detail: None)
        self._stop = threading.Event()
        self._hook = None
        self._hotkey_ids = {}
        self._next_id = 1
        self._thread_id = None
        self.error = ""

    # ------------------------------------------------------------- 回调绑定
    @staticmethod
    def _cb(*args):
        return 0

    def _make_hook_proc(self):
        hook_ref = [None]

        def _proc(nCode, wParam, lParam):
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(kbd.vkCode)
                for name, code in MEDIA_KEYS.items():
                    if vk == code:
                        try:
                            self.on_trigger("mediakey", name)
                        except Exception:
                            pass
                        if bool(self.settings_get("consumeMediaKey", True)):
                            return 1   # 拦截
            return user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

        hook_ref[0] = HOOKPROC(_proc)
        return hook_ref[0]

    # ------------------------------------------------------------- 生命周期
    def _register_hotkeys(self):
        """读取配置里的组合键并注册。返回已注册列表。

        panelHotkey（默认 Ctrl+Shift+E）用于打开 ECHO 仪表盘窗口，注册在
        ECHO 服务进程里而不是 DSH 插件里——DSH Desktop 2.0.9 的插件拿不到
        electron 的 globalShortcut（见 config.py 注释）。
        若 panelHotkey 与 wake/fallback 撞车，注册会失败并记录在返回列表里。
        """
        registered = []
        for key in ("wakeHotkey", "fallbackHotkey", "panelHotkey"):
            combo = self.settings_get(key, "")
            parsed = parse_hotkey_combo(combo)
            if not parsed:
                continue
            mods, vk = parsed
            hid = self._next_id
            self._next_id += 1
            if user32.RegisterHotKey(None, hid, mods, vk):
                self._hotkey_ids[hid] = (key, combo)
                registered.append((key, combo))
            else:
                # 组合键被别的程序占用（或与本进程其它热键重复）→ 明确记下来
                print(f"[hotkey] 注册失败（可能被占用）: {key}={combo}")
        return registered

    def run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        # 低级键盘钩子（媒体键）
        try:
            proc = self._make_hook_proc()
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                self.error = f"SetWindowsHookEx 失败: {ctypes.get_last_error()}"
                print(f"[hotkey] {self.error}（媒体键不可用，仅组合键）")
        except Exception as e:
            self.error = str(e)
            print(f"[hotkey] 低级钩子初始化失败: {e}")
        # 组合键
        try:
            regs = self._register_hotkeys()
            print(f"[hotkey] 已注册组合键: {regs or '无'}")
        except Exception as e:
            print(f"[hotkey] RegisterHotKey 失败: {e}")

        # 消息循环
        msg = wt.MSG()
        while not self._stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message == WM_HOTKEY:
                hid = msg.wParam
                info = self._hotkey_ids.get(hid)
                if info:
                    key_name, combo = info
                    try:
                        self.on_trigger("hotkey", key_name)
                    except Exception:
                        pass
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        # 清理
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
        for hid in list(self._hotkey_ids):
            user32.UnregisterHotKey(None, hid)

    def shutdown(self):
        self._stop.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


def run_once(seconds=15, settings_get=None, callback=None):
    """调试：跑 seconds 秒打印触发事件。"""
    from app.config import settings as _s
    g = settings_get or _s.get
    seen = []
    hk = HotkeyListener(g, on_trigger=callback or (lambda s, d: seen.append((s, d))))
    hk.start()
    time.sleep(seconds)
    hk.shutdown()
    return seen


if __name__ == "__main__":
    print(run_once())
