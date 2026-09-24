# -*- coding: utf-8 -*-
"""本机后端：**原样包现有的引擎，一行都不改 `app/audio/*`。**

这一步的全部价值就在"原样"两个字。`app/audio/stt.py` 与 `diarize.py` 是**已经在实机
验证过**的实现（指令转写、会议转写、说话人分离都在用），把它们重写一遍只会引入
"能力层能跑但主链路坏了"这种最难查的问题。所以这里只做三件事：

  1. 把 `stt.transcribe_ex()` 那个"状态是字符串"的返回翻成结构化的 `AsrResult`
     （含 `timestamps: exact|none`，**不编 estimated** —— 那是拼装层的事，见 §4.4）；
  2. 把 `diarize.diarize_wav_full()` 的 numpy 结果翻成 `DiarizeResult`；
  3. 声明哪些槽它真的能供（`provides`）—— 声明错了路由就会派错活，
     所以这份声明由契约用例与本机实况对照。

## 三条要写明的取舍

**① 本机声明 `vector_space_id`，但要承认它是"本机空间"。**
`models/pyannote` 与 ECHO 服务端用的是同一套权重（wespeaker-resnet34-LM），
所以 `LOCAL_VECTOR_SPACE` 暂时写成与出厂清单相同的 id。**但这只在"同权重跨运行时可比"
被实测确认之后才成立** —— 设计文档已经把这条标成待验证
（`docs/能力路由` §5.1 末尾："这条要先实测同权重跨运行时是否可比才能开"）。
在实测之前，`speaker.embed` 与 `diarize.embeddings` 的**默认路由都不给本机**
（见 `router.py` 的默认表），这条声明只是让"如果用户硬要"能表达出来。

**② 唤醒（`wake`）不在本文件里。** 它在 `app/audio/wake.py`，形态是"长期监听线程"，
不是"给一段音频出结果"。塞进这个"一次调用一次返回"的接口只会拧巴。
所以本后端的 `provides` 里**没有 `wake`** —— 铁律 L3 由路由的默认表保证
（`wake` 与指令转写的主选必须是本机），不靠"本机客户端声称自己会唤醒"。

**③ 本机是"能就不抛"的。** `stt.transcribe_ex()` 内部有 `except → 回退 CPU`
（客户端单用户，宁可慢也要出结果，这跟服务端刻意相反）。这里**保持它**，
但把它的失败状态如实翻成 `CapabilityError`，不吞。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from app.capabilities.base import (
    BACKEND_LOCAL,
    SOURCE_LOCAL,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    EmbedResult,
    Provenance,
)

#: 本机的向量空间 id。
#:
#: 与出厂清单的 `vectorSpaceId` 写同一个值，因为我们用的是**同一套 pyannote 权重**
#: （`models/pyannote/pyannote-wespeaker-local`，即 wespeaker-voxceleb-resnet34-LM）。
#: **但这不等于跨运行时可比** —— 见模块注释 ①。改这里之前先看那条。
LOCAL_VECTOR_SPACE = "ws-resnet34-v1"
LOCAL_EMBED_DIM = 256

#: `stt.transcribe_ex()` 的 status → 本层的降级原因
_STT_STATUS_TO_REASON = {
    "ok": None,
    "empty": None,          # 空文本**不是失败**：这段就是没人说话。交给调用方判断
    "missing": "open-failed",
    "error": "open-failed",  # 引擎自己炸了：换个后端可能就好
}


def _setting(key, default=None):
    """读客户端设置。**失败就吃异常返回默认值** —— 能力层不该因为配置层的问题挂掉。"""
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def _device() -> str:
    """`device` 设置的取值：auto / cpu / cuda。"""
    return str(_setting("device", "auto") or "auto")


class LocalCapabilityClient(CapabilityClient):
    """本机引擎。**懒加载**：不构造就不碰模型（面板打开、启动都不该付加载时间）。"""

    backend_id = BACKEND_LOCAL
    source = SOURCE_LOCAL
    vector_space_id = LOCAL_VECTOR_SPACE

    def __init__(self, provides=None):
        # 声明能供哪些槽。默认按"本机装了哪些引擎"来定，而不是无脑全给 ——
        # 声明了却做不到，路由会把活派过来然后每次失败。
        self.provides = frozenset(provides) if provides is not None else self._detect_slots()

    # ---------------------------------------------------------------- 能力探测

    @staticmethod
    def _detect_slots() -> frozenset:
        """看本机**实际**有什么，据此声明槽。

        这份探测刻意**不 import 重引擎**（torch / funasr / pyannote）——
        只"点一下模块在不在"，否则打开面板就会把 torch 拉起来。
        真正确认能不能用，是第一次调用的事（失败会抛带分类的 `CapabilityError`）。
        """
        import importlib.util

        slots = set()
        # asr.text：sherpa（必装、torch-free）或 whisper / sensevoice 任一
        if importlib.util.find_spec("sherpa_onnx") is not None:
            slots |= {"asr.text", "asr.streaming"}
        if importlib.util.find_spec("faster_whisper") is not None:
            slots |= {"asr.text", "asr.timestamps"}
        if importlib.util.find_spec("funasr") is not None:
            # SenseVoice 不给句级时间戳；qwen3asr 给（启用 ForcedAligner 时）
            slots.add("asr.text")
        # diarize / speaker.embed：pyannote
        if importlib.util.find_spec("pyannote") is not None:
            slots |= {"diarize.turns", "diarize.embeddings", "speaker.embed"}
        return frozenset(slots)

    def ready(self) -> Optional[bool]:
        try:
            return bool(self.provides)
        except Exception:
            return None

    # ---------------------------------------------------------------- ASR

    def transcribe(self, wav, *, lang="auto", want_timestamps=False,
                   variant="long", **kw) -> AsrResult:
        """走 `stt.transcribe_ex()`（那条**已经区分了 ok/empty/error/missing** 的入口）。

        为什么不用老的 `transcribe()`：它把"这段没人说话"和"引擎挂了"都变成一个空串，
        调用方只能猜。`transcribe_ex` 就是为了修这个才存在的
        （见 `app/audio/stt.py` 里那段注释，PROGRESS §19 发现③）。
        """
        if not os.path.isfile(wav):
            raise CapabilityError("open-failed", "文件不存在: %s" % wav,
                                  backend_id=self.backend_id, slot="asr.text")
        from app.audio import stt

        engine, model = self._pick_engine(want_timestamps)
        try:
            out = stt.transcribe_ex(wav, engine=engine, model=model, lang=lang,
                                    device=_device())
        except Exception as e:                       # 引擎抛异常：如实翻，不吞
            raise CapabilityError("open-failed", "%s: %s" % (engine, e),
                                  backend_id=self.backend_id, slot="asr.text") from None

        status = str(out.get("status") or "")
        reason = _STT_STATUS_TO_REASON.get(status, "error")
        if reason:
            raise CapabilityError(reason, str(out.get("detail") or status),
                                  code=status, backend_id=self.backend_id, slot="asr.text")

        text = str(out.get("text") or "")
        sentences = ()
        timestamps = "none"
        if want_timestamps:
            sentences = self._sentences(wav, engine, model, lang)
            timestamps = "exact" if sentences else "none"
        return AsrResult(
            text=text, sentences=sentences, timestamps=timestamps,
            provenance=Provenance(self.backend_id, self._model_version(engine, model)),
            audio_seconds=_wav_seconds(wav))

    def _pick_engine(self, want_timestamps: bool):
        """挑本机引擎。`meetingSttModel` / `sttModel` 是既有的两个设置，沿用它。

        要时间戳时优先**能出句级时间戳**的引擎（whisper / qwen3asr）——
        否则"我要时间戳"会静默降级成没有，而调用方无法区分
        "模型给不出"和"我们没挑对引擎"。
        """
        if want_timestamps:
            preferred = "whisper" if importlib_find("faster_whisper") else ""
            if preferred:
                return preferred, str(_setting("sttModel", "small") or "small")
        model = str(_setting("meetingSttModel", "sensevoice") or "sensevoice")
        return model, str(_setting("sttModel", "small") or "small")

    def _sentences(self, wav, engine, model, lang):
        """句级时间戳。只有 whisper 那条路是现成可用的。

        qwen3asr 的 `_qwen3asr_sentences` 需要拿**已加载的实例**，而本机这条路径
        不持有实例（`transcribe_ex` 内部自己管缓存）。所以这里**刻意只做 whisper**，
        其余一律返回空 = 没有时间戳 —— 宁可如实说"没有"，也不去猜一个时间轴出来。
        """
        if engine != "whisper":
            return ()
        try:
            from app.audio import stt
            with stt._ENGINE_LOCK:                      # noqa: SLF001 —— 与 stt 内部同款
                inst = stt._ENGINES.get("whisper:%s" % model)   # noqa: SLF001
            if inst is None:
                return ()
            segs, _info = stt.transcribe_whisper(inst, wav, lang)
            return tuple((float(s.start), float(s.end), str(s.text).strip())
                         for s in segs if str(getattr(s, "text", "")).strip())
        except Exception:
            return ()

    @staticmethod
    def _model_version(engine: str, model: str) -> str:
        return "%s-%s" % (engine, model) if model else engine

    # ---------------------------------------------------------------- 说话人

    def diarize(self, wav, *, max_speakers=None, **kw) -> DiarizeResult:
        if not os.path.isfile(wav):
            raise CapabilityError("open-failed", "文件不存在: %s" % wav,
                                  backend_id=self.backend_id, slot="diarize.turns")
        try:
            from app.audio import diarize as dia
            turns, embs, labels = dia.diarize_wav_full(wav, max_speakers)
        except Exception as e:
            raise CapabilityError("open-failed", "pyannote: %s" % e,
                                  backend_id=self.backend_id,
                                  slot="diarize.turns") from None
        return DiarizeResult(
            turns=tuple((float(a), float(b), str(s)) for a, b, s in turns),
            speakers={str(lab): tuple(float(x) for x in embs[i])
                      for i, lab in enumerate(labels or []) if i < len(embs)},
            dim=LOCAL_EMBED_DIM,
            vector_space_id=LOCAL_VECTOR_SPACE,
            provenance=Provenance(self.backend_id, "pyannote-local"),
            audio_seconds=_wav_seconds(wav))

    def embed(self, wav, *, count=1, **kw) -> EmbedResult:
        """现场注册联系人：复用分离那一套（**同一个向量空间**才谈得上比对）。

        这不是偷懒 —— 客户端要拿"刚注册的联系人"去比"会议里认出的说话人"，
        两者必须在同一个 `vector_space_id` 里，否则余弦相似度没有意义。
        """
        got = self.diarize(wav)
        want = max(1, int(count or 1))
        return EmbedResult(
            vectors=tuple(got.speakers[k] for k in sorted(got.speakers))[:want],
            dim=got.dim, vector_space_id=got.vector_space_id,
            provenance=Provenance(self.backend_id, got.provenance.model_version),
            audio_seconds=got.audio_seconds)

    def close(self) -> None:
        """本后端的引擎由 `stt` / `diarize` 的模块级缓存持有，**故意不在这里卸**。

        理由：那两个缓存是**主链路（指令/会议转写）也在用的**。能力层为了"我不用了"
        把模型卸掉，会把正在跑的会议转写一起搞坏 —— 这是"借用别人引擎"必须付的代价，
        写在明处。真要按需卸载，得先给引擎层一个**引用计数**，那是引擎层的活。
        """
        return None


def importlib_find(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _wav_seconds(path: str) -> float:
    """读 wav 时长。失败返回 0（时长只是元数据，不该让整次调用失败）。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            sr = w.getframerate() or 1
            return round(w.getnframes() / float(sr), 3)
    except Exception:
        return 0.0
