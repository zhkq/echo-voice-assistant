# -*- coding: utf-8 -*-
"""把客户端那套引擎层（`app/audio/*`）包成 `EnginePool` 能持有的加载器。

**服务端只借用引擎层，不碰业务层。** `app/audio/stt.py` 与 `app/audio/diarize.py`
本来就是干净的 —— 前者只 import `os/re/sys/threading` + `app.paths`，
后者只 import `os/threading/wave` + `app.paths` + `numpy`。
业务编排全在客户端业务层（`app/` 下那两个编排模块），**不进服务端**。

两个已知的取舍，写在明处：

  1. 客户端那两个模块用**模块级全局单例**缓存引擎（`stt._ENGINES` / `diarize._pipeline`）。
     服务端直接复用，所以 `close()` 得走它们的卸载入口。因此
     **两个 spec 不能指向同一个 `(impl, model)` 组合** —— 那会共用同一个全局实例，
     LRU 卸载会互相影响。默认清单里每个 spec 的组合都是唯一的。
  2. `diarize` 没有公开的"卸载"入口（只有 `stt` 有 `unload_key`），
     所以这里直接置 `_pipeline = None`。**这是欠债**，将来应给它加一个公开入口。
"""
from __future__ import annotations

from typing import Dict, List

from server.pool import ModelSpec


# ---------------------------------------------------------------- 设备

def cuda_available() -> bool:
    """能不能用 GPU，**只看事实**。"""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return True
    except Exception:
        pass
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


#: impl → 它真正跑在哪个运行时上。**判据必须按 impl 分开**（原因见 `_assert_device`）。
#: `sensevoice` / `qwen3asr` / `pyannote*` 都是 torch 系：`_get_sensevoice` 看的是
#: `torch.cuda.is_available()`，`diarize._load_pipeline` 看的是 `torch.device("cuda")`
#: 还是 `"cpu"`。`load_engine` 里那句"先 import ctranslate2"只是**导入顺序**的
#: 规避（WinError 127），不是设备决定的依据 —— 别把它当成 ctranslate2 系。
_IMPL_RUNTIME = {
    "sensevoice": "torch",         # funasr AutoModel
    "qwen3asr": "torch",           # funasr AutoModel + ForcedAligner
    "whisper": "ctranslate2",      # faster-whisper
    "sherpa": "cpu-ok",            # onnxruntime：本来就是 CPU 引擎，没有"回退"这回事
    "pyannote": "torch",           # pyannote 分离管线
    "pyannote-embed": "torch",     # 同一套管线的嵌入出口
}


def _runtime_has_cuda(runtime: str) -> bool:
    """**只问真正会决定去留的那个运行时。**

    为什么不能笼统地问 "本机有没有 CUDA"：客户端那两个 `_get_*` 各自按**自己**的
    运行时决定设备 —— `_get_sensevoice` 看 `torch.cuda.is_available()`，
    `_get_whisper` 看 ctranslate2 能不能建出模型。如果这台机器 ctranslate2 有 CUDA
    而 torch 没有（或反过来），一句笼统的 "cuda_available()" 会放行，
    然后**客户端那层自己的 `except -> CPU` 就把我们悄悄降级了** ——
    正是这个函数要拦住的事。按 impl 问，才拦得住。
    """
    if runtime == "cpu-ok":
        return True
    if runtime == "torch":
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:
            return False
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _assert_device(impl: str, device: str) -> None:
    """服务端**不许静默回退 CPU**（设计 §3.4，与客户端刻意相反）。

    客户端单用户，偷偷回退 CPU 是对的（宁可慢也要出结果）；服务端一旦回退，
    会拖慢**所有**客户端，而且从指标上看不出原因。所以这里**在加载前**就失败，
    让客户端的那个引擎层根本没机会走它的 `except -> cpu` 分支。

    **判据按 impl 分开问**（见 `_runtime_has_cuda`）—— 笼统地问"CUDA 可用吗"
    会在两个运行时不一致的机器上放行，然后被客户端那层悄悄降级。

    残留缺口（诚实记着）：`_get_whisper` / `_get_sensevoice` **内部**那个
    `except -> CPU` 还包着"运行时说能用、但建模型时炸了"（cuDNN 版本不匹配之类）。
    那种情况这里拦不住，因为失败发生在加载过程里。真正的堵法是给客户端引擎层加一个
    `allow_cpu_fallback=False`，属于客户端侧改动，还没做。
    """
    if device != "cuda":
        return
    runtime = _IMPL_RUNTIME.get(impl, "torch")
    if runtime == "cpu-ok":
        return
    if not _runtime_has_cuda(runtime):
        raise RuntimeError(
            "模型 %s 要求 GPU，但它依赖的运行时（%s）看不到可用的 CUDA —— "
            "服务端不回退 CPU（会把所有客户端一起拖慢）。请检查显卡驱动/容器 --gpus，"
            "或把这个 spec 的 device 改成 cpu 并接受它的性能。" % (impl, runtime))


# ---------------------------------------------------------------- 语音转文本

class _SttEngine:
    """客户端引擎层的适配器：一次加载、可复用、可卸载。"""

    def __init__(self, engine_name: str, model: str, device: str, key: str):
        self.engine_name = engine_name
        self.model = model
        self.device = device
        self.key = key

    def _instance(self):
        """按 key 取回实例。

        `stt` 没有"给我实例"的公开入口（它把 `_ENGINES` 当内部缓存），所以只能按 key 读 ——
        与 `diarize._pipeline` 是同一类欠债，记在这里，将来给引擎层补公开入口。
        """
        from app.audio import stt
        with stt._ENGINE_LOCK:
            return stt._ENGINES.get(self.key)

    def transcribe(self, wav: str, lang: str = "auto", timestamps: bool = False) -> dict:
        from app.audio import stt
        if timestamps:
            inst = self._instance()
            if inst is not None:
                if self.engine_name == "whisper":
                    # 公开入口：faster-whisper 的 segments 自带句级时间戳
                    segs, _info = stt.transcribe_whisper(inst, wav, lang)
                    rows = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                            for s in segs if s.text and s.text.strip()]
                    if rows:
                        return {"text": " ".join(r["text"] for r in rows),
                                "sentences": rows, "status": stt.TRANSCRIBE_OK}
                elif self.engine_name == "qwen3asr":
                    # 只有带强制对齐的那个入口给句级时间戳（客户端自己的实现，直接复用）
                    text, sents = stt._qwen3asr_sentences(
                        inst, wav, stt._LANG_MAP.get(str(lang).lower()))
                    if text:
                        return {"text": text,
                                "sentences": [{"start": float(a), "end": float(b), "text": t}
                                              for a, b, t in sents],
                                "status": stt.TRANSCRIBE_OK}
        out = stt.transcribe_ex(wav, engine=self.engine_name, model=self.model,
                                lang=lang, device=self.device)
        return {"text": out.get("text", ""), "sentences": [],
                "status": out.get("status", ""), "detail": out.get("detail", "")}

    def close(self) -> None:
        try:
            from app.audio import stt
            stt.unload_key(self.key)
        except Exception:
            pass


def _stt_loader(engine_name: str, model: str, device: str):
    def load(spec: ModelSpec):
        _assert_device(engine_name, device)
        from app.audio import stt
        mdl = model
        if not mdl and engine_name == "qwen3asr":
            mdl = "Qwen/Qwen3-ASR-0.6B"
        key = stt.load_engine(engine_name, mdl, device)
        if not stt.key_loaded(key):
            raise RuntimeError("引擎 %s 没有加载起来（key=%s）" % (engine_name, key))
        return _SttEngine(engine_name, mdl, device, key)
    return load


# ---------------------------------------------------------------- 说话人向量

class _DiarizeEngine:
    """说话人分离：出时间轴 + 每个说话人的嵌入。

    **必须过 `_assert_device`**（由加载器做）：`diarize._load_pipeline` 内部是
    `torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")` ——
    一句实打实的**静默 CPU 回退**。客户端单用户时它是对的（宁可慢也要出结果），
    服务端上它就是"把所有人都拖慢且指标上看不出来"。所以这一层要在加载前拦住。
    """

    def __init__(self):
        from app.audio import diarize
        diarize._load_pipeline()          # 把"加载失败"暴露在加载阶段，而不是第一次请求
        self._d = diarize

    def analyze(self, wav: str, max_speakers=None):
        return self._d.diarize_wav_full(wav, max_speakers)

    def close(self) -> None:
        try:
            self._d._pipeline = None      # 欠债：diarize 还没有公开的卸载入口
        except Exception:
            pass


class _EmbedEngine:
    """说话人嵌入（现场注册用）。

    v1 复用说话人分离那一套 —— **这不是偷懒，是一致性要求**：
    客户端要拿"刚注册的联系人"去比对"会议里认出的说话人"，
    两者必须在**同一个 `vectorSpaceId`** 里，否则余弦相似度没有意义。
    所以它也声明同一个 `vectorSpaceId`（见 `default_specs`）。
    """

    def __init__(self):
        from app.audio import diarize
        diarize._load_pipeline()
        self._d = diarize

    def embed(self, wav: str):
        _turns, embs, labels = self._d.diarize_wav_full(wav)
        return [list(map(float, v)) for v in embs], list(labels)

    def close(self) -> None:
        try:
            self._d._pipeline = None
        except Exception:
            pass


def _torch_loader(impl: str, device: str, factory):
    """产出一个"先查设备、再建引擎"的加载器。

    `_diarize_loader` / `_embed_loader` 原来**没有**查设备，于是 pyannote 那句
    静默 CPU 回退在服务端是可达的 —— 这是一处真实的漏洞，不是风格问题。
    收在这一个工厂里，将来再加 torch 系 impl 也不会漏。
    """
    def load(_spec: ModelSpec):
        _assert_device(impl, device)
        return factory()
    return load


def _diarize_loader(device: str):
    return _torch_loader("pyannote", device, _DiarizeEngine)


def _embed_loader(device: str):
    return _torch_loader("pyannote-embed", device, _EmbedEngine)


# ---------------------------------------------------------------- 注册表

def build_loaders(device: str = "cuda") -> Dict[str, object]:
    """impl 名 → 加载器。**只在这里认识"客户端有哪些引擎"**，池本身不认识。"""
    return {
        "sensevoice": _stt_loader("sensevoice", "", device),
        "qwen3asr": _stt_loader("qwen3asr", "", device),
        "whisper": _stt_loader("whisper", "", device),
        "sherpa": _stt_loader("sherpa", "", device),
        "pyannote": _diarize_loader(device),
        "pyannote-embed": _embed_loader(device),
    }


def default_specs() -> List[ModelSpec]:
    """出厂模型清单。

    `est_vram_mb` 与 `max_concurrency` 是**占位值**（按 24 GB 卡的粗略估算），
    上线前要用真机压测校准 —— 尤其 `max_concurrency`，它直接决定
    "服务端总通道 2" 这个数压不压得住。

    `vectorSpaceId` 只有产出向量的两个模型有，而且**必须相同**（见 `_EmbedEngine`）。
    """
    return [
        # 短音频（语音指令增强）：常驻，冷启动付一次，之后每次 < 300 ms
        ModelSpec(id="asr-short", slot="asr.text", impl="sensevoice", resident=True,
                  max_concurrency=2, est_vram_mb=1800,
                  model_version="sensevoice-small"),
        # 长音频（会议分段）：按需 + LRU；它**同时**给文本与句级时间戳
        ModelSpec(id="asr-long", slot="asr.long", impl="qwen3asr", resident=False,
                  max_concurrency=1, est_vram_mb=3900,
                  model_version="qwen3-asr-0.6b", supports=("asr.text", "asr.timestamps")),
        # 说话人分离：**不是线程安全的**，所以并发只能 1
        ModelSpec(id="diarize", slot="diarize.turns", impl="pyannote", resident=False,
                  max_concurrency=1, est_vram_mb=2600,
                  model_version="pyannote-3.1-wespeaker-v1",
                  vector_space_id="ws-resnet34-v1", dim=256,
                  supports=("diarize.embeddings",)),
        # 说话人嵌入：与上面**同一个 vectorSpaceId**（要能互相比对）
        ModelSpec(id="speaker-embed", slot="speaker.embed", impl="pyannote-embed", resident=False,
                  max_concurrency=1, est_vram_mb=0,
                  model_version="wespeaker-voxceleb-resnet34-LM-v1",
                  vector_space_id="ws-resnet34-v1", dim=256),
    ]
