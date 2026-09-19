# -*- coding: utf-8 -*-
"""本地 provider：把 1.x 的本地引擎包装成 P5 的 provider 形状（不改变它们的行为）

原则：**这一层只做适配，不做替换**。whisper/sensevoice/sherpa、SAPI/say、edge-tts 仍由
`app/audio/stt.py` / `app/audio/tts.py` 实现（它们有独立测试与实测记录），这里只负责：
  * 用统一的 `transcribe()/speak()` 形状暴露出去；
  * 把"就绪状态"翻译成 `ready()`；
  * 明确标注**出网**：本地引擎 `egress=False`，而 edge-tts 虽然是"本地播放"，
    但合成文本会发到微软 → 单独作为一个 provider 标注 `egress=True`。

重依赖（torch/funasr/sherpa）只在方法内部 import —— 注册表在启动时就要能列出清单，
不能因为"这台机器没装 funasr"而 import 失败。
"""
from __future__ import annotations

from app.providers.base import AsrProvider, TtsProvider


def _offline_label():
    """本平台的离线 TTS 引擎名（Windows=sapi / macOS=say / Linux=espeak）。"""
    try:
        from app import platform as echo_platform
        return str(echo_platform.offline_tts_label() or "sapi")
    except Exception:
        return "sapi"


def _runtime_available(engine):
    """该转写引擎依赖的运行时是否装了（判不了返回 None）。

    与 `mac/run_mac.py` 里那段探测同一套映射：funasr 管 sensevoice/qwen3asr、
    sherpa_onnx 管流式、whisper 走 faster-whisper。**只在 ready() 里用**，
    不参与转写决策（决策仍由 stt.py 自己抛错）。
    """
    import importlib.util
    pkg = {"sensevoice": "funasr", "qwen3asr": "funasr",
           "sherpa": "sherpa_onnx", "whisper": "faster_whisper"}.get(str(engine or "").lower())
    if not pkg:
        return None
    try:
        return importlib.util.find_spec(pkg) is not None
    except Exception:
        return None


class LocalAsrProvider(AsrProvider):
    """本地转写：引擎取 `sttModel` 配置（whisper 各档 / sensevoice / qwen3asr / sherpa）。"""

    id = "local-asr"

    def ready(self):
        try:
            from app.audio import stt
            eng, _model = stt.resolve_engine(_setting("sttModel", "sensevoice"))
            return _runtime_available(eng)
        except Exception:
            return None

    def transcribe(self, wav_path, lang="zh", engine=None, model=None, device=None, **kw):
        from app.audio import stt
        choice = engine or _setting("sttModel", "sensevoice")
        eng, mdl = stt.resolve_engine(choice)
        mdl = model or mdl
        dev = device or _setting("device", "auto")
        text = stt.transcribe(wav_path, eng, mdl, lang, dev)
        out = {"text": (text or ""), "engine": eng, "model": mdl, "sentences": []}
        if not out["text"]:
            # §19 发现③：空结果必须能区分"没人说话"与"引擎挂了"。本地引擎目前无法区分，
            # 这里如实标注为 unknown，调用方据此决定是重试还是放过。
            out["reason"] = "empty-or-unknown"
        return out


class LocalTtsProvider(TtsProvider):
    """本平台**离线**朗读（Windows=SAPI、macOS=say、Linux=espeak/spd-say）。不出网。"""

    id = "local-tts"

    def ready(self):
        try:
            from app import platform as echo_platform
            return bool(echo_platform.offline_tts_display())
        except Exception:
            return None

    def speak(self, text, timeout=60, **kw):
        try:
            from app.audio import tts
            return bool(tts.speak(text, _offline_label(), timeout))
        except Exception:
            return False


class EdgeTtsProvider(TtsProvider):
    """在线朗读（微软 edge-tts）：音质好，但**被朗读的文本会出网**。"""

    id = "edge-tts"

    def ready(self):
        try:
            from app.audio import tts
            return tts.probe_online()
        except Exception:
            return None

    def speak(self, text, timeout=60, **kw):
        try:
            from app.audio import tts
            return bool(tts.speak(text, "edge-tts", timeout))
        except Exception:
            return False


def _setting(key, default=None):
    """读配置；配置层不可用时返回默认值（provider 不该因为读配置失败而崩）。"""
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def register_builtin():
    """把本地三个 provider 登记进注册表（由 `app.providers` 在 import 时调用）。"""
    from app import providers as P

    P.register(P.ProviderSpec(
        id=LocalAsrProvider.id, kind="asr", name="本地转写引擎",
        source="local", egress=False, default=True,
        purpose="用本机模型转写（whisper / SenseVoice / Qwen3-ASR / sherpa），数据不出本机",
        details={"setting": "sttModel"}),
        LocalAsrProvider)
    P.register(P.ProviderSpec(
        id=LocalTtsProvider.id, kind="tts", name="离线朗读",
        source="local", egress=False, default=True,
        purpose="用系统自带语音合成朗读（%s），数据不出本机" % _offline_label(),
        details={"setting": "ttsEngine", "engine": _offline_label()}),
        LocalTtsProvider)
    P.register(P.ProviderSpec(
        id=EdgeTtsProvider.id, kind="tts", name="edge-tts（微软在线）",
        source="online", egress=True,
        egress_note="被朗读的文本会发送到微软 speech.platform.bing.com 合成语音",
        purpose="音质更自然的中文朗读；断网时自动回退离线朗读",
        details={"setting": "ttsEngine", "engine": "edge-tts"}),
        EdgeTtsProvider)
