# -*- coding: utf-8 -*-
"""run_mac.py — ECHO macOS 启动入口（零改动 Windows 代码）

原理：Windows 版在 import 时直接依赖 Windows 专有模块（app.hotkey 里的
ctypes.windll、app.runtime 里的 powershell/边条）。本入口在 import app.main
**之前**，把 Mac 实现塞进 sys.modules，覆盖 app.hotkey 与 app.runtime，
并给 app.audio.tts 打补丁 —— 因此原仓库任何文件都不用改。

运行：python mac/run_mac.py   （或 mac/start_mac.sh）
"""
import os
import sys

# ---- 路径：项目根目录 + mac/ 目录都放进 sys.path ----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAC_DIR = os.path.join(BASE_DIR, "mac")
for p in (BASE_DIR, MAC_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---- 1) 先把 Mac 版配置默认值改好（seed_defaults 之前生效）----
import app.config as _config  # noqa: E402

# macOS 独立原生宿主；保留用户显式选择的浏览器模式。
_config.DEFAULTS["panelOpenMode"]["value"] = "sidebar"
_config.DEFAULTS["panelOpenMode"]["description"] = "sidebar=macOS 右缘浮动框；browser=浏览器打开面板"
_config.DEFAULTS["panelOpenMode"]["options"] = ["sidebar", "browser"]
# Mac 默认用 Whisper（内置依赖、开箱即用）。想用 SenseVoice/Qwen3-ASR 需另装
# funasr+torch，已在 Apple 芯片上实测可用（见 mac/README.md），可在设置里自行切换。
_config.DEFAULTS["sttModel"]["value"] = "base"
_config.DEFAULTS["meetingSttModel"]["value"] = "small"
# Mac 没有 CUDA
_config.DEFAULTS["device"]["value"] = "cpu"
# 去掉会强制改回 sidebar 的迁移
_config.DEFAULT_MIGRATIONS.pop("panelOpenMode", None)

# 旧 app 模式在 Mac 上一直使用浏览器，保留该行为。
_orig_seed_defaults = _config.Settings.seed_defaults


def _runtime_available(engine):
    """mac 上该转写引擎的运行时是否已安装（whisper 走 faster_whisper，精简依赖自带）。"""
    import importlib.util
    pkg = {"sensevoice": "funasr", "qwen3asr": "funasr",
           "sherpa": "sherpa_onnx"}.get(str(engine or "").lower())
    if not pkg:
        return True
    try:
        return importlib.util.find_spec(pkg) is not None
    except Exception:
        return False


def _mac_seed_defaults(self):
    _orig_seed_defaults(self)
    dirty = False
    try:
        cur = _config.db.get_setting("panelOpenMode")
        if cur is not None and str(cur).lower() not in ("sidebar", "browser"):
            _config.db.set_setting("panelOpenMode", "browser")
            dirty = True
            print(f"[mac] panelOpenMode {cur!r} → 'browser'")
    except Exception as e:
        print(f"[mac] panelOpenMode 纠正失败: {e}")
    # 从别处继承来的库里 sttModel/meetingSttModel 可能是 sensevoice/qwen3asr，
    # 但 mac 精简依赖不含 funasr → 会静默转写失败。运行时缺失就回退到 whisper。
    for key, fallback in (("sttModel", "base"), ("meetingSttModel", "small")):
        try:
            cur = _config.db.get_setting(key)
            if cur and not _runtime_available(cur):
                _config.db.set_setting(key, fallback)
                dirty = True
                print(f"[mac] {key} {cur!r} 的运行时未安装 → {fallback!r}"
                      "（装好 funasr 后可在设置里切回）")
        except Exception as e:
            print(f"[mac] {key} 纠正失败: {e}")
    if dirty:
        self._cache = None            # 丢弃缓存，让后续 settings.get() 重新读库


_config.Settings.seed_defaults = _mac_seed_defaults

# ---- 2) 注入 Mac 版 runtime / hotkey（必须在 import app.main 之前）----
# 注意顺序：先注册 app.hotkey，再 import mac_runtime（它顶部会 from app.hotkey import）
import hotkey_mac  # noqa: E402

sys.modules["app.hotkey"] = hotkey_mac

import mac_runtime  # noqa: E402

sys.modules["app.runtime"] = mac_runtime

# ---- 3) TTS 补丁：Windows SAPI → macOS say；winsound → sounddevice ----
import app.audio.tts as _tts  # noqa: E402
import tts_mac  # noqa: E402

tts_mac.patch(_tts)

# ---- 4) 通知补丁：Windows 气球通知 → macOS 通知中心 ----
import app.assistant as _assistant  # noqa: E402
import notify_mac  # noqa: E402

_assistant.notify = notify_mac.notify

# ---- 5) 启动 ----
from app.main import main  # noqa: E402

if __name__ == "__main__":
    print("ECHO (macOS) 启动中…")
    main()
