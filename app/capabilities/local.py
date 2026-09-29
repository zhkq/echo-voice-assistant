# -*- coding: utf-8 -*-
"""本机后端：**服务语音指令那条 STT 链路**（铁律 L3），别的一概不声明。

2026-09-29（用户拍板）：客户端进程内**不再承担会议转写与说话人分离** ——
即使本机有 GPU，那两件事也以"在本机起一个能力后端"的形式完成（同一个
`echo-server` 取值交给路由）。所以这份声明的 `provides` 只剩 `asr.text`：

  * **不再声明** `diarize.*` / `speaker.embed`（会议分离只落在能声明向量空间的
    能力后端上；本机一旦参与就会引入第二个 `vectorSpaceId`，而混用的表现是
    "认错人且不报错" —— L5）；
  * **不再声明** `asr.timestamps`（句级时间轴是会议链路要的东西，指令转写不需要它；
    留着它会让路由在会议场景里挑到本机，而那条路已经不走客户端的会议转写代码了）；
  * `asr.streaming` / `wake` 仍**不在这里**：它们由路由的默认表钉在本机
    （`wake` 是"长期监听线程"，形态上塞不进"一次调用一次返回"的接口）。
    所以本后端的 `provides` 里本来也没有它们，L3 靠默认表保证。

`diarize()` / `embed()` / `_with_timestamps()` 这些方法**本步先留着**（第 5 步随
模型与能力一起清）—— 它们只是**不再被声明**，路由不会把活派过来；删掉它们要连带
清 `app/audio/diarize.py` 与模型清单，那是另一件事的边界。
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

#: 本机**只声明这一个槽**（服务语音指令那条链路，铁律 L3）。
#:
#: 为什么写成一个常量而不是散在 `_detect_slots()` 里：`/api/capability`、契约用例、
#: 以及"路由到底该把什么派给本机"都读同一份声明 —— 三处各写一遍，迟早出现
#: "面板说本机能分离、路由却不派给它"这种对不上的现象。
LOCAL_SLOTS = frozenset({"asr.text"})

#: 本机的向量空间 id（**当前没有任何槽会用到它**）。
#:
#: 与出厂清单的 `vectorSpaceId` 写同一个值，因为我们用的是**同一套 pyannote 权重**
#: （`models/pyannote/pyannote-wespeaker-local`，即 wespeaker-voxceleb-resnet34-LM）。
#: 2026-09-29 起本机不再承担会议分离（`diarize.*` / `speaker.embed` 都不声明了），
#: 所以这个常量只剩"`DiarizeResult` 的形状契约仍在"这一层意思 —— 见模块注释。
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
        """本机能供的槽 —— **恒为 `LOCAL_SLOTS`**（只有 `asr.text`，服务语音指令）。

        2026-09-29 之前这里看 `find_spec("pyannote")` / `find_spec("funasr")` 之类来声明
        `diarize.*` / `speaker.embed` / `asr.timestamps`。现在那些都不声明了，
        于是"本机装了什么"与"本机能供什么"解耦：不再需要探测重引擎，
        也不再会因为这台机器恰好装了 pyannote 而被路由派去跑会议分离。

        **刻意保留这个函数**（而不是让 `__init__` 直接用常量）：契约用例与
        `_detect_slots()` 的存在本身在说"这份声明曾经是探测出来的"，
        谁要改声明，改的是这里。
        """
        return LOCAL_SLOTS

    def ready(self) -> Optional[bool]:
        try:
            return bool(self.provides)
        except Exception:
            return None

    # ---------------------------------------------------------------- ASR

    def transcribe(self, wav, *, lang="auto", want_timestamps=False,
                   variant="long", engine_hint="", **kw) -> AsrResult:
        """走 `stt.transcribe_ex()`（那条**已经区分了 ok/empty/error/missing** 的入口）。

        为什么不用老的 `transcribe()`：它把"这段没人说话"和"引擎挂了"都变成一个空串，
        调用方只能猜。`transcribe_ex` 就是为了修这个才存在的
        （见 `app/audio/stt.py` 里那段注释，PROGRESS §19 发现③）。

        `want_timestamps=True` 时会尽量给出**句级时间轴**，并按 `assemble` 的档位如实标注
        （`exact` / `aligned` / `estimated`）。**本机后端如今不再声明 `asr.timestamps`**
        （2026-09-29：句级时间轴是会议链路要的东西，会议转写已经不在这里跑），
        所以正常路由**不会**带着 `want_timestamps=True` 找到本机；这条参数留着是给
        `engine_hint` 那类直接调用与既有契约用例的，不代表本机承诺供这一槽。
        """
        if not os.path.isfile(wav):
            raise CapabilityError("open-failed", "文件不存在: %s" % wav,
                                  backend_id=self.backend_id, slot="asr.text")
        from app.audio import stt

        engine, model = self._pick_engine(engine_hint)
        if want_timestamps:
            got, text = self._with_timestamps(wav, engine, model, lang)
            return AsrResult(
                text=text, sentences=tuple(got.sentences), timestamps=got.timestamps,
                provenance=Provenance(self.backend_id, self._model_version(engine, model)),
                audio_seconds=_wav_seconds(wav))

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
        return AsrResult(
            text=str(out.get("text") or ""), timestamps="none",
            provenance=Provenance(self.backend_id, self._model_version(engine, model)),
            audio_seconds=_wav_seconds(wav))

    def _pick_engine(self, engine_hint=""):
        """挑本机引擎。返回 `(engine, model)`。

        **必须用 `stt.resolve_engine()` 解析**，因为那个设置值是**混的**：
        `sensevoice` / `qwen3asr` / `sherpa` 是**引擎名**，而 `small` / `medium` / `large`
        是 **whisper 的模型名**。第一版我在这里按"engine=设置值、model=另一个设置值"接，
        于是设置里写 `small` 时会拿另一个值当模型名 → 引擎加载失败，
        而错误信息看着像"模型不存在"，很难联想到是这里接错了。

        `engine_hint` 让调用方把它自己的设置传进来；**不传就用 `sttModel`**
        （命令转写引擎 —— 本机现在只服务那条链路）。2026-09-29 之前这里读的是
        `meetingSttModel`，而那一项已经废弃（会议不在客户端进程内跑了）。
        """
        from app.audio import stt
        choice = str(engine_hint or _setting("sttModel", "sensevoice") or "sensevoice")
        return stt.resolve_engine(choice)

    def _with_timestamps(self, wav, engine, model, lang):
        """要句级时间轴时走这里。返回 `(Assembled, 文本)`。

        **本机后端已不再声明 `asr.timestamps`**（2026-09-29）：句级时间轴是会议链路
        要的东西，而会议转写已经不在客户端进程内跑。这个方法留着只是因为
        `transcribe(want_timestamps=True)` 这条既有接口还在（契约用例会走它），
        正常路由不会为了时间轴找到本机。

        三条路：
          1. whisper —— 它自己就出 segments（一句一次前向，**不重复跑**）
          2. qwen3asr —— 原生 `_qwen3asr_sentences`（带 ForcedAligner 时是精确句子）
          3. 其余（SenseVoice 等）—— 文本来自它，时间骨架借 whisper，`assemble` 对齐

        第 3 条与已退役的 `meeting.py::_fallback_sv_rows` 是同一套规则。**两边都走
        `assemble`**，所以不会出现"同一场会议 A 段一个精度、B 段另一个精度"。
        """
        from app.audio import stt
        from app.capabilities import assemble

        if engine == "whisper":
            try:
                with stt._ENGINE_LOCK:                          # noqa: SLF001
                    inst = stt._ENGINES.get(stt.engine_key("whisper", model))  # noqa: SLF001
                if inst is None:
                    inst = stt._get_whisper(model, _device())
                segs, _info = stt.transcribe_whisper(inst, wav, lang)
                native = [(float(s.start), float(s.end), str(s.text).strip())
                          for s in segs if str(getattr(s, "text", "")).strip()]
                if native:
                    text = " ".join(t for _a, _b, t in native)
                    return assemble.assemble(sentences=native), text
            except Exception:
                pass                       # 落到下面的"文本 + 骨架"路
            got = assemble.assemble(text=self._text_only(wav, engine, model, lang))
            return got, " ".join(t for _a, _b, t in got.sentences)

        text = self._text_only(wav, engine, model, lang)

        # qwen3asr：原生句子（有 ForcedAligner 时精确）
        if engine == "qwen3asr":
            native = self._qwen_sentences(wav, model, lang)
            if native:
                return assemble.assemble(sentences=native), text

        # 其余（SenseVoice 等）：文本 + whisper 时间骨架 → 按字对齐
        skeleton = self._whisper_skeleton(wav, lang)
        return assemble.assemble(text=text, skeleton=skeleton), text

    def _text_only(self, wav, engine, model, lang) -> str:
        """只取文本（失败/空都返回空串 —— 由调用方按档位如实处理，不在这里抛）。"""
        from app.audio import stt
        try:
            out = stt.transcribe_ex(wav, engine=engine, model=model, lang=lang,
                                   device=_device())
        except Exception:
            return ""
        status = str(out.get("status") or "")
        if _STT_STATUS_TO_REASON.get(status, "error"):
            return ""
        return str(out.get("text") or "")

    def _qwen_sentences(self, wav, model, lang):
        """qwen3asr 的原生句级时间轴。**取不到实例就返回空**（不抛、不假装）。"""
        from app.audio import stt
        for key in (f"qwen3asr:{model}:",
                    f"qwen3asr:{model}:Qwen/Qwen3-ForcedAligner-0.6B"):
            with stt._ENGINE_LOCK:                                  # noqa: SLF001
                inst = stt._ENGINES.get(key)                        # noqa: SLF001
            if inst is None:
                continue
            try:
                _text, sents = stt._qwen3asr_sentences(        # noqa: SLF001
                    inst, wav, stt._LANG_MAP.get(str(lang).lower()))   # noqa: SLF001
                return [(float(a), float(b), t) for a, b, t in sents]
            except Exception:
                return []
        return []

    def _whisper_skeleton(self, wav, lang):
        """借 whisper 的逐句时间轴当骨架。

        固定用 `small`（与会议链路原来的选择一致）—— 骨架只需要**时间**，
        不需要最准的识别；用 large 会白等好几倍时间。
        """
        from app.audio import stt
        try:
            inst = stt._get_whisper("small", _device())             # noqa: SLF001
            segs, _info = stt.transcribe_whisper(inst, wav, lang)
            return [(float(s.start), float(s.end), str(s.text).strip())
                    for s in segs if str(getattr(s, "text", "")).strip()]
        except Exception:
            return []

    @staticmethod
    def _model_version(engine: str, model: str) -> str:
        return "%s-%s" % (engine, model) if model else engine

    # ---------------------------------------------------------------- 说话人
    #
    # ⚠️ **本类不再声明 `diarize.*` / `speaker.embed`**（2026-09-29，见模块注释）：
    # 下面两个方法与 `LOCAL_VECTOR_SPACE` 只是"形状契约仍在"的遗留实现 ——
    # 路由**不会**把会议分离派过来（`provides` 里没有那几个槽）。
    # 它们连同 `app/audio/diarize.py` 一起留到第 5 步（清模型与能力）时删。

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

        理由：那两个缓存是**主链路（语音指令转写）也在用的**。能力层为了"我不用了"
        把模型卸掉，会把正在跑的指令转写一起搞坏 —— 这是"借用别人引擎"必须付的代价，
        写在明处。真要按需卸载，得先给引擎层一个**引用计数**，那是引擎层的活。
        """
        return None


def _wav_seconds(path: str) -> float:
    """读 wav 时长。失败返回 0（时长只是元数据，不该让整次调用失败）。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            sr = w.getframerate() or 1
            return round(w.getnframes() / float(sr), 3)
    except Exception:
        return 0.0
