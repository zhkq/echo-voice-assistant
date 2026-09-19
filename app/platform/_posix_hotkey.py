# -*- coding: utf-8 -*-
"""POSIX（macOS / Linux）全局热键实现：``pynput`` 全局热键表。

从 ``mac/hotkey_mac.py`` 整段搬来（P3 剩余搬迁），成为接缝里 macOS/Linux 的
**唯一**实现（``mac/hotkey_mac.py`` 现在是转发给本模块的薄壳，避免两份实现分叉）。

对外接口与 Windows 版 ``HotkeyListener`` 兼容：
    HotkeyListener(settings_get, on_trigger=cb).start() / .is_alive() / .shutdown()
    .error 为空表示可用，否则为给用户看的原因。

⚠️ 未在 macOS 上实测（缺机器，见 REFACTOR-PLAN §9.1 的 S7/S9/S10）。已知限制如实记：
  * 依赖 pynput（可选依赖），未安装时优雅降级 —— 热键不可用，面板录音等一切照常；
  * macOS 上**需要**在「系统设置 → 隐私与安全性 → 辅助功能 / 输入监控」里授权，
    这正是不满足 D19/S10（免授权的常驻原生宿主 + Carbon 热键）的地方；
  * pynput 在 macOS 上"未授权"是**在线程内异步失败**的：``start()`` 不抛异常，
    所以启动后要等一小会儿确认线程还活着（否则会出现"组件显示在线、按键没反应"）。
"""
import time

# Windows 命名键 → pynput 键名
_NAMED = {
    "SPACE": "<space>", "ENTER": "<enter>", "RETURN": "<enter>", "TAB": "<tab>",
    "ESC": "<esc>", "ESCAPE": "<esc>", "BACKSPACE": "<backspace>",
    "DELETE": "<delete>", "INSERT": "<insert>", "HOME": "<home>", "END": "<end>",
    "PGUP": "<page_up>", "PAGEUP": "<page_up>", "PGDN": "<page_down>",
    "PAGEDOWN": "<page_down>", "LEFT": "<left>", "UP": "<up>",
    "RIGHT": "<right>", "DOWN": "<down>",
    **{f"F{i}": f"<f{i}>" for i in range(1, 25)},
}

#: 与 Windows 版保持同一组配置键（注册顺序也一致，便于两边对照）
HOTKEY_SETTING_KEYS = ("wakeHotkey", "fallbackHotkey", "panelHotkey")


def _to_pynput(combo):
    """'Ctrl+Alt+C' → '<ctrl>+<alt>+c'；无法解析返回 None。"""
    if not combo:
        return None
    parts = [p.strip() for p in str(combo).split("+") if p.strip()]
    if len(parts) < 2:
        return None
    out = []
    for m in parts[:-1]:
        ml = m.lower()
        if ml in ("ctrl", "control"):
            out.append("<ctrl>")
        elif ml in ("alt", "option"):
            out.append("<alt>")
        elif ml == "shift":
            out.append("<shift>")
        elif ml in ("win", "cmd", "command", "super"):
            out.append("<cmd>")
        else:
            return None
    last = parts[-1]
    ul = last.upper()
    if len(last) == 1 and last.isalnum():
        out.append(last.lower())
    elif ul in _NAMED:
        out.append(_NAMED[ul])
    else:
        return None
    return "+".join(out)


class HotkeyListener:
    """pynput 全局热键监听。接口兼容 Windows 版。"""

    def __init__(self, settings_get, on_trigger=None, daemon=True):
        self.settings_get = settings_get
        self.on_trigger = on_trigger or (lambda source, detail: None)
        self.error = ""
        self._listener = None

    def _fire(self, key_name):
        try:
            self.on_trigger("hotkey", key_name)
        except Exception:
            pass

    def start(self):
        try:
            from pynput import keyboard
        except Exception as e:
            self.error = ("未安装 pynput，本机全局热键不可用"
                          "（可运行：pip install pynput）")
            print(f"[hotkey-mac] {self.error}: {e}")
            return

        mapping = {}
        for key in HOTKEY_SETTING_KEYS:
            hk = _to_pynput(self.settings_get(key, ""))
            if not hk:
                continue
            # 闭包固定 key 名
            mapping[hk] = (lambda k=key: self._fire(k))
        if not mapping:
            self.error = "配置里没有可用的热键"
            return
        try:
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.daemon = True
            self._listener.start()
            time.sleep(0.4)
            if not self.is_alive():
                self.error = ("热键监听未能启动：请在 系统设置 → 隐私与安全性 → "
                              "辅助功能 / 输入监控 里给本程序授权后重试")
                self._listener = None
                print(f"[hotkey-mac] {self.error}")
                return
            print(f"[hotkey-mac] 已注册热键: {list(mapping)}")
        except Exception as e:
            self.error = ("热键启动失败，请在 系统设置 → 隐私与安全性 → 辅助功能/输入监控 "
                          f"给本程序授权后重试：{e}")
            print(f"[hotkey-mac] {self.error}")

    def is_alive(self):
        return bool(self._listener is not None and self._listener.is_alive())

    def shutdown(self):
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass


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
