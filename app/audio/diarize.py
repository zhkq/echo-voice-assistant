# -*- coding: utf-8 -*-
"""diarize.py — 本地说话人分离（pyannote.audio 4.x 全离线）

模型全部在 models/pyannote/ 下，从本地目录加载，不访问 HuggingFace：
  pyannote-segmentation-3.0-local   说话人活动分段
  pyannote-wespeaker-local          说话人嵌入（wespeaker-voxceleb-resnet34-LM）
  pyannote-plda-local               聚类辅助

用法：
    from app.audio.diarize import diarize_wav_full, SpeakerRegistry
    turns, embs, labels = diarize_wav_full('meetings/xxx/01.wav')
"""
import os
import threading
import wave

import numpy as np

# GPU 加固：确保 ctranslate2 先于 torch 加载，避免 CUDA 动态库冲突（WinError 127）
try:
    import ctranslate2  # noqa: F401
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYANNOTE_DIR = os.path.join(BASE_DIR, "models", "pyannote")
SEG_DIR = os.path.join(PYANNOTE_DIR, "pyannote-segmentation-3.0-local")
EMB_DIR = os.path.join(PYANNOTE_DIR, "pyannote-wespeaker-local")
PLDA_DIR = os.path.join(PYANNOTE_DIR, "pyannote-plda-local", "plda")

# pyannote 4.x speaker-diarization-3.1 默认超参（centroid 聚类）
HYPERPARAMS = {
    "segmentation": {"min_duration_off": 0.581},
    "clustering": {"method": "centroid", "min_cluster_size": 12, "threshold": 0.444},
}

# 跨片段说话人匹配阈值：同一人嵌入余弦相似度通常 >0.85，不同人 <0.5
SPEAKER_MATCH_THRESHOLD = 0.75

_pipeline = None
_lock = threading.Lock()

# speechbrain 1.1.0 的 lazy/deprecation 机制在 pyannote 模型加载（torch/inspect 探测）时
# 会访问 integrations 下所有可选依赖子模块（k2_fsa/nlp/huggingface.wordemb 等），
# 缺依赖（k2/flair…）直接抛 ImportError，导致 pyannote 说话人分离静默失败。
# 这里在 import pyannote 之前把 lazy 模块全部解析/替换，避免触发缺依赖加载。


def _patch_speechbrain():
    import os
    import sys
    import types
    import speechbrain

    # 1) 递归空壳化 integrations 下所有子模块（含嵌套），并给包加 __path__
    base_dir = os.path.join(os.path.dirname(speechbrain.__file__), "integrations")
    if os.path.isdir(base_dir):
        for dirpath, dirnames, filenames in os.walk(base_dir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            rel = os.path.relpath(dirpath, base_dir).replace(os.sep, ".")
            prefix = "speechbrain.integrations" + ("" if rel == "." else "." + rel)
            for fn in filenames:
                if fn.endswith(".py") and not fn.startswith("__"):
                    name = prefix + "." + fn[:-3]
                    if name not in sys.modules:
                        m = types.ModuleType(name)
                        m.__path__ = []
                        m.__package__ = name
                        sys.modules[name] = m
            if rel != ".":
                if prefix not in sys.modules:
                    m = types.ModuleType(prefix)
                    m.__path__ = []
                    m.__package__ = prefix
                    sys.modules[prefix] = m

    # 2) 真实加载 speechbrain.inference（pyannote 依赖），替换旧路径 redirect
    import speechbrain.inference  # noqa: F401
    for old in ("speechbrain.pretrained",):
        m = sys.modules.get(old)
        if m is not None and not hasattr(m, "__file__"):
            sys.modules[old] = sys.modules["speechbrain.inference"]

    # 3) 解析 speechbrain 包级 lazy 属性与 sys.modules 里的 redirect（失败则移除/空壳）
    from speechbrain.utils.importutils import DeprecatedModuleRedirect, LazyModule
    for name, obj in list(vars(speechbrain).items()):
        if isinstance(obj, (LazyModule, DeprecatedModuleRedirect)):
            try:
                setattr(speechbrain, name, obj.ensure_module(2))
            except Exception:
                setattr(speechbrain, name, types.ModuleType(name))
    for name, obj in list(sys.modules.items()):
        if isinstance(obj, DeprecatedModuleRedirect):
            try:
                sys.modules[name] = obj.ensure_module(2)
            except Exception:
                sys.modules.pop(name, None)


def _load_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    with _lock:
        if _pipeline is not None:
            return _pipeline
        os.environ["HF_HUB_OFFLINE"] = "1"
        _patch_speechbrain()
        import torch
        from pyannote.audio import Model
        from pyannote.audio.pipelines.speaker_diarization import SpeakerDiarization
        map_location = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        seg = Model.from_pretrained(SEG_DIR, strict=False, map_location=map_location)
        emb = Model.from_pretrained(EMB_DIR, strict=False, map_location=map_location)
        pipe = SpeakerDiarization(
            segmentation=seg,
            embedding=emb,
            clustering="AgglomerativeClustering",
            plda={"checkpoint": PLDA_DIR, "subfolder": None},
        )
        pipe.instantiate(HYPERPARAMS)
        if map_location.type == "cuda":
            try:
                pipe._embedding = pipe._embedding.to(map_location)
            except Exception as e:
                print("嵌入模型迁移 GPU 失败，保持 CPU:", e, file=os.sys.stderr)
        _pipeline = pipe
        return _pipeline


def _read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    dtype = "<i2" if sw == 2 else "i1"
    arr = np.frombuffer(raw, dtype=dtype).astype(np.float32) / 32768.0
    if nch > 1:
        arr = arr.reshape(-1, nch).mean(axis=1)
    import torch
    return torch.from_numpy(arr).unsqueeze(0), sr


def diarize_wav_full(path, max_speakers=None):
    """返回 (turns, embeddings, labels)：
      turns      [(start, end, 'SPEAKER_xx'), ...]
      embeddings (num_speakers, 256) float32
      labels     ['SPEAKER_00', ...]
    """
    pipe = _load_pipeline()
    waveform, sr = _read_wav(path)
    file_dict = {"waveform": waveform, "sample_rate": sr, "uri": os.path.basename(path)}
    kwargs = {"num_speakers": max_speakers} if max_speakers else {}
    out = pipe(file_dict, **kwargs)
    ann = out if hasattr(out, "itertracks") else out.speaker_diarization
    turns = [(t.start, t.end, s) for t, _, s in ann.itertracks(yield_label=True)]
    embeddings = getattr(out, "speaker_embeddings", None)
    if embeddings is None:
        labels = sorted(set(s for _, _, s in turns))
        embeddings = np.zeros((len(labels), 256))
    else:
        labels = list(ann.labels())
    return turns, np.asarray(embeddings, dtype=np.float32), labels


class SpeakerRegistry:
    """跨片段说话人身份保持：嵌入余弦相似度匹配，>阈值归入已有编号。"""

    def __init__(self, threshold=SPEAKER_MATCH_THRESHOLD):
        self.threshold = threshold
        self._centroids = []
        self._counts = []

    @staticmethod
    def _norm(v):
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def map(self, embeddings, labels):
        embs = np.asarray(embeddings, dtype=np.float32)
        n = embs.shape[0]
        mapping = {}
        if n == 0:
            return mapping
        if not self._centroids:
            for i in range(n):
                self._centroids.append(self._norm(embs[i].copy()))
                self._counts.append(1)
                mapping[labels[i]] = "说话人%d" % len(self._centroids)
            return mapping
        cents = np.stack([self._norm(c) for c in self._centroids])
        sims = embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9)
        sims = sims @ cents.T
        assigned_global = set()
        order = sorted(((sims[i, j], i, j) for i in range(n) for j in range(len(self._centroids))),
                       reverse=True)
        for sim, i, j in order:
            if i in mapping or j in assigned_global:
                continue
            if sim < self.threshold:
                break
            c = self._centroids[j]
            cnt = self._counts[j]
            self._centroids[j] = self._norm((c * cnt + embs[i]) / (cnt + 1))
            self._counts[j] = cnt + 1
            mapping[labels[i]] = "说话人%d" % (j + 1)
            assigned_global.add(j)
        for i in range(n):
            if labels[i] not in mapping:
                self._centroids.append(self._norm(embs[i].copy()))
                self._counts.append(1)
                mapping[labels[i]] = "说话人%d" % len(self._centroids)
        return mapping

    def snapshot(self):
        """各说话人的质心快照：{显示名("说话人N"): (质心, 参与片段数)}。

        转写结束时调用：把整场的平均声纹留存进 speaker_embeddings 表，
        供「改名为联系人 → 入库声纹」「识别本场」使用（见 app/voiceprint.py）。
        """
        return {f"说话人{i + 1}": (np.asarray(c, dtype=np.float32), int(self._counts[i]))
                for i, c in enumerate(self._centroids)}
