# -*- coding: utf-8 -*-
"""stt.py — ECHO 语音转文字（离线，自动 GPU）

引擎：
  whisper    faster-whisper（base/small/medium/large-v3），本地模型在 models/faster-whisper/
  sensevoice funasr SenseVoiceSmall（中文短命令最快、自带标点），本地模型在 models/sensevoice/
  sherpa     sherpa-onnx 流式 zipformer 中英（CPU 实时），本地模型在 models/sherpa-onnx-streaming/

进程内单例（懒加载）：
  长期驻留进程对同一 CUDA 模型反复加载会因 ctranslate2/torch 动态库顺序冲突
  （WinError 127）崩溃 —— 每个引擎只加载一次，失败自动回退 CPU（已实测验证）。

CLI：python -m app.audio.stt <wav> [--engine ...] [--model ...] [--lang ...]
"""
import os
import re
import sys
import threading

from app import paths

# 安装根由路径层给（含 ECHO_ROOT 覆盖）；模型目录一律走 models_dir()（D20/D21）。
BASE_DIR = paths.echo_root()


def models_dir() -> str:
    """当前生效的模型目录（用户可在面板里改，见 D20/D21）。

    为什么是函数而不是常量：模型目录要能随配置变化，而"模块级 __getattr__"对模块内部
    的裸名字无效（PEP 562 只管属性访问），所以内部引用一律调用本函数。
    """
    from app import paths
    return paths.models_root()


# HF_HOME 只能在 import 时定一次（HF 库自己读环境变量、没有每次调用的入口），
# 这里取一次快照；真正读模型文件时一律走 models_dir()，跟随用户改配置。
os.environ.setdefault("HF_HOME", models_dir())
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

MODEL_ALIASES = {"large": "large-v3"}
WHISPER_MODELS = {"tiny", "base", "small", "medium", "large", "large-v3"}

# ---------------------------------------------------------------- 语言
# 「同一个设置喂两家引擎」原来是个坑（2026-09-15 issue #2）：
#   * faster-whisper 只认 ISO-639-1 小写码（zh/en/ja…）——**不认**全名、大写或 "auto"，
#     填错会抛 ValueError，而调用方把异常吞成"转写结果为空"，界面上只看到没文字；
#   * funasr / qwen-asr 要全名（Chinese/English…），"auto" 等价于不传（自动识别）。
# 现在统一成一张别名表：whisper 侧走 normalize_lang()，funasr 侧走 _LANG_MAP。
_LANG_ALIASES = {
    "zh": ("zh", "Chinese"), "zh-cn": ("zh", "Chinese"), "zh-hans": ("zh", "Chinese"),
    "cn": ("zh", "Chinese"), "chinese": ("zh", "Chinese"), "中文": ("zh", "Chinese"),
    "en": ("en", "English"), "english": ("en", "English"), "英文": ("en", "English"),
    "ja": ("ja", "Japanese"), "jp": ("ja", "Japanese"), "japanese": ("ja", "Japanese"),
    "ko": ("ko", "Korean"), "korean": ("ko", "Korean"),
    "yue": ("yue", "Cantonese"), "cantonese": ("yue", "Cantonese"), "粤语": ("yue", "Cantonese"),
}

# Qwen3-ASR / funasr 的语言提示（需要全称，如 "Chinese"；取不到 = 自动识别）
_LANG_MAP = {alias: full for alias, (_iso, full) in _LANG_ALIASES.items()}

# Whisper 的中文训练语料以繁体为主，不给提示词时容易输出繁体字（issue #2）。
# 命令 / 会议 / 对外 API 三条路径共用下面这个入口，避免再出现"命令有提示词、会议没有"。
WHISPER_INITIAL_PROMPT = "以下是普通话的日常对话片段。"

# Qwen3-ASR 的强制对齐器：**句级时间戳的唯一来源**。
# 不给它时，`return_time_stamps=True` 只会换来一句
# "return_time_stamps requires forced_aligner. Skipping timestamps." —— 时间戳一个都没有。
# 客户端会议链路（`app/meeting.py`）与本地能力（`app/capabilities/local.py`）一直是这么加载的；
# 能力后端的加载路径 2026-09-26 前漏了这一步，于是服务端宣告的 `asr.timestamps` 名不副实。
QWEN3_FORCED_ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"

# Qwen3-ASR 一次喂多长音频（秒）与最多几条一起前向。**必须自己切片**，原因见
# `_qwen3asr_transcribe` 的注释：qwen-asr 只在 1200 秒才自己切，于是 10 分钟会议音频是
# 一整条长序列进模型（batch=1、GPU 空转、还被 512 token 的生成上限截断）。
QWEN3_CHUNK_S = 60.0
QWEN3_BATCH = 4


def normalize_lang(lang, default="zh"):
    """把设置里的语言值收敛成 faster-whisper 认的 ISO 码；`auto`/空 → None（自动识别）。

    非法值不再直接抛给 whisper（那会让整段转写静默变成空），而是回退 `default` 并留一行
    可查的日志。接受：ISO 码（zh/en/ja…）、全名（Chinese/English…）、`auto`。
    """
    s = str(lang if lang is not None else "").strip().lower()
    if s in ("auto", "", "none", "null"):
        return None
    if s in _LANG_ALIASES:
        return _LANG_ALIASES[s][0]
    if 2 <= len(s) <= 3 and s.isalpha():
        return s          # 疑似 ISO 码：交给 whisper 自己校验（它支持 99 种语言）
    print(f"[stt] 语言设置 {lang!r} 不是有效语言码，已回退 {default}"
          f"（可用：zh/en/ja/ko/yue 或 auto）", file=sys.stderr)
    return default


def transcribe_whisper(model, wav, lang="zh"):
    """faster-whisper 的唯一调用入口，返回 (segments, info)。

    统一 language / vad_filter / beam_size / initial_prompt —— 这几个参数以前在
    `app/meeting.py` 里被各自复制过一份，且漏了 initial_prompt（会议转写出繁体字的成因）。
    """
    return model.transcribe(
        wav, language=normalize_lang(lang), vad_filter=True, beam_size=5,
        initial_prompt=WHISPER_INITIAL_PROMPT)

# ---------------------------------------------------------------- 设备解析

def resolve_device(choice="auto"):
    if choice == "cpu":
        return "cpu", "int8"
    if choice == "cuda":
        return "cuda", "float16"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


# ---------------------------------------------------------------- 本地模型定位

def _whisper_dir(model_name):
    name = MODEL_ALIASES.get(model_name, model_name)
    d = os.path.join(models_dir(), "faster-whisper", name)
    return d if os.path.isfile(os.path.join(d, "model.bin")) else ""


def _sensevoice_dir():
    """models/sensevoice 下可能有多层 snapshots/<hash>，自动下探找到 model.pt。

    注意：funasr 在 Windows 上无法加载含非 ASCII 字符（如中文）的本地路径，
    遇到这种情况返回 ""，让调用方走模型名（iic/SenseVoiceSmall，落 modelscope 缓存）。
    """
    root = os.path.join(models_dir(), "sensevoice")
    if not os.path.isdir(root):
        return ""
    for dirpath, _dirs, files in os.walk(root):
        if "model.pt" in files or "model.pb" in files:
            if any(ord(ch) > 127 for ch in dirpath):
                return ""
            return dirpath
    if any(ord(ch) > 127 for ch in root):
        return ""
    return root


def _sherpa_files():
    d = os.path.join(models_dir(), "sherpa-onnx-streaming")
    if not os.path.isdir(d):
        return None
    enc = next((f for f in os.listdir(d) if f.startswith("encoder") and f.endswith(".onnx")), "")
    dec = next((f for f in os.listdir(d) if f.startswith("decoder") and f.endswith(".onnx")), "")
    joi = next((f for f in os.listdir(d) if f.startswith("joiner") and f.endswith(".onnx")), "")
    tok = os.path.join(d, "tokens.txt")
    if not (enc and dec and joi and os.path.isfile(tok)):
        return None
    return (os.path.join(d, enc), os.path.join(d, dec), os.path.join(d, joi), tok)


# ---------------------------------------------------------------- 引擎单例

_ENGINES = {}
_ENGINE_LOCK = threading.Lock()


def _clean_sv_text(t):
    return re.sub(r"<\|[^|]*\|>", "", t or "").strip()


def _get_whisper(model_name="small", device="auto"):
    key = f"whisper:{model_name}"
    with _ENGINE_LOCK:
        if key in _ENGINES:
            return _ENGINES[key]
        from faster_whisper import WhisperModel
        model_ref = _whisper_dir(model_name) or model_name
        try:
            dev, compute = resolve_device(device)
            model = WhisperModel(model_ref, device=dev, compute_type=compute)
        except Exception as e:
            print(f"[stt] {model_name}@{device} 加载失败，回退 CPU int8: {e}", file=sys.stderr)
            model = WhisperModel(model_ref, device="cpu", compute_type="int8")
        _ENGINES[key] = model
        _cache_gpu_name()   # ctranslate2 已加载，此时导入 torch 顺序安全
        return model


def _get_sensevoice(device="auto"):
    key = "sensevoice"
    with _ENGINE_LOCK:
        if key in _ENGINES:
            return _ENGINES[key]
        os.environ["TQDM_DISABLE"] = "1"
        os.environ.setdefault("MODELSCOPE_DISABLE_PROGRESS_BAR", "1")
        from funasr import AutoModel
        use_cuda = False
        try:
            import torch
            use_cuda = device != "cpu" and torch.cuda.is_available()
        except Exception:
            use_cuda = False
        dev = "cuda:0" if use_cuda else "cpu"
        model_dir = _sensevoice_dir()
        kwargs = dict(model=model_dir or "iic/SenseVoiceSmall",
                      vad_model="fsmn-vad",
                      vad_kwargs={"max_single_segment_time": 30000},
                      device=dev, disable_update=True)
        try:
            model = AutoModel(**kwargs)
        except Exception as e:
            print(f"[stt] SenseVoice 加载失败，回退 CPU: {e}", file=sys.stderr)
            kwargs["device"] = "cpu"
            model = AutoModel(**kwargs)
        _ENGINES[key] = model
        _cache_gpu_name()
        return model


def _get_sherpa():
    key = "sherpa"
    with _ENGINE_LOCK:
        if key in _ENGINES:
            return _ENGINES[key]
        files = _sherpa_files()
        if not files:
            raise RuntimeError("sherpa 流式模型未就绪: " + os.path.join(models_dir(), "sherpa-onnx-streaming"))
        import sherpa_onnx
        enc, dec, joi, tok = files
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tok, encoder=enc, decoder=dec, joiner=joi,
            num_threads=2, sample_rate=16000, feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=0.8,
            rule2_min_trailing_silence=0.5,
            rule3_min_utterance_length=10,
        )
        _ENGINES[key] = recognizer
        return recognizer


def _ascii_path_hint(exc):
    """加载失败时的路径根因提示：解释器/站点包落在非 ASCII 路径下。

    nagisa → dyNet 用窄字符 fopen 读模型文件，路径含中文时甩出来的只有一句
    "Could not read model from ...\\nagisa\\data\\nagisa_v001.model"，很难一眼看出是路径问题
    （2026-09-15 事故：迁移后 ECHO_PYTHON 被指向中文路径下的 venv，
    qwen3asr 每场会议都在这里炸，只剩 wav 没有转写）。
    命中时把修复方向直接附在异常后，见 docs/DEPLOY.md「路径尽量全英文」。
    """
    msg = str(exc)
    if "nagisa" not in msg and "Could not read model" not in msg:
        return ""
    try:
        import site
        cands = list(site.getsitepackages() or []) + [sys.prefix, sys.executable]
        if not any(any(ord(c) > 127 for c in str(p)) for p in cands):
            return ""
    except Exception:
        return ""
    return ("\n[stt] 根因：解释器/站点包位于非 ASCII 路径，nagisa(dyNet) 读不了这类路径下的模型文件。"
            "请用指向 venv 的 ASCII 目录联接（junction）解释器启动 ECHO，并设置 ECHO_PYTHON，"
            "详见 docs/DEPLOY.md。")


def qwen3asr_aligner(forced_aligner=QWEN3_FORCED_ALIGNER):
    """这次真的会一起加载的对齐器名；**本机没有权重就返回 ""**。

    `engine_key()` / `load_engine()` / `_get_qwen3asr()` 必须用**同一份判据**：
    三处各算一次而判据不同的话，`load_engine()` 返回的 key 在 `_ENGINES` 里找不到，
    加载器会报"引擎没有加载起来"（一个自己造出来的假故障）。
    """
    if not forced_aligner:
        return ""
    try:
        # 解析不出本地目录 = 本机没有权重 → funasr 会去 modelscope 拉 1.7 GB（离线就是失败）
        return forced_aligner if _resolve_local_model(forced_aligner) != forced_aligner else ""
    except Exception:
        return ""


def _get_qwen3asr(device="auto", model_name="Qwen/Qwen3-ASR-0.6B",
                  forced_aligner=QWEN3_FORCED_ALIGNER):
    """funasr Qwen3-ASR（qwen-asr 包，52 语言，中文准确率高于 SenseVoice）。

    依赖：qwen-asr==0.0.6 + transformers==4.57.6（scripts/setup.ps1 或 README 有说明）。
    模型经 modelscope/HF 缓存离线加载（见 `_resolve_local_model`）。
    显存：0.6B ~4GB，1.7B ~8GB（GPU 不足自动回退 CPU）。
    forced_aligner: **默认就带上** `QWEN3_FORCED_ALIGNER`（"Qwen/Qwen3-ForcedAligner-0.6B"）——
      句级时间戳（`return_time_stamps=True`）没有它就只有一句警告、一个时间戳都没有。
      传 `None` 可以显式关掉（省 1.2 GB 显存）。本机没有该权重时自动降级（留一行日志）。
    """
    aligner = qwen3asr_aligner(forced_aligner)
    if forced_aligner and not aligner:
        print(f"[stt] 强制对齐器 {forced_aligner} 不在本机（modelscope/HF 缓存都没有），"
              f"本次不加载 —— 句级时间戳将不可用（不会去联网下 1.7 GB）。", file=sys.stderr)
    key = f"qwen3asr:{model_name}:{aligner}"
    with _ENGINE_LOCK:
        if key in _ENGINES:
            return _ENGINES[key]
        from funasr import AutoModel
        use_cuda = False
        try:
            import torch
            use_cuda = device != "cpu" and torch.cuda.is_available()
        except Exception:
            use_cuda = False
        dev = "cuda:0" if use_cuda else "cpu"
        dtype = "bf16" if use_cuda else "fp32"
        # model 保留模型名（funasr 注册表查找类），model_path 用本地快照路径（离线加载，跳过联网检查）
        model_path = _resolve_local_model(model_name)
        aligner_path = _resolve_local_model(aligner) if aligner else None
        kwargs = dict(model=model_name, hub="ms", device=dev, dtype=dtype, disable_update=True)
        if model_path and model_path != model_name:
            kwargs["model_path"] = model_path
        if aligner_path:
            kwargs["forced_aligner"] = aligner_path
        try:
            model = AutoModel(**kwargs)
        except Exception as e:
            print(f"[stt] Qwen3-ASR 加载失败，回退 CPU: {e}", file=sys.stderr)
            kwargs["device"] = "cpu"
            kwargs["dtype"] = "fp32"
            try:
                model = AutoModel(**kwargs)
            except Exception as e2:
                # 两次都失败：多半是环境问题（如非 ASCII 路径），把可执行的修复方向一起抛出去
                raise RuntimeError(f"{e2}{_ascii_path_hint(e2)}") from e2
        _ENGINES[key] = model
        _cache_gpu_name()
        return model


def _resolve_ms_cache(model_id):
    """把 modelscope 模型名解析为本地快照路径（存在则返回，否则返回原名）。"""
    if not model_id:
        return model_id
    import os as _os
    cache = _os.path.expanduser(
        f"~/.cache/modelscope/models/{model_id.replace('/', '--')}/snapshots/master")
    return cache if _os.path.isdir(cache) else model_id


def _resolve_hf_cache(model_id):
    """把模型名解析为 **HF hub 缓存**里的本地快照目录（存在才返回，否则返回原名）。

    本机 `models/hub/models--Qwen--Qwen3-ForcedAligner-0.6B/` 就是这种布局
    （`HF_HOME` 在本模块开头被定成 models 目录，所以 HF 的落点就是 `{models}/hub`）。
    只认 modelscope 会让"对齐器明明在本地"变成"去联网下 1.7 GB"。
    """
    if not model_id:
        return model_id
    snapshots = os.path.join(models_dir(), "hub",
                             "models--" + model_id.replace("/", "--"), "snapshots")
    try:
        names = sorted(n for n in os.listdir(snapshots)
                       if os.path.isdir(os.path.join(snapshots, n)))
    except Exception:
        return model_id
    return os.path.join(snapshots, names[0]) if names else model_id


def _resolve_local_model(model_id):
    """两个本地缓存都认：modelscope 优先，HF hub 兜底；都没有则返回原名（= 交给 funasr）。"""
    path = _resolve_ms_cache(model_id)
    return path if path != model_id else _resolve_hf_cache(model_id)


# ---------------------------------------------------------------- 转写入口

def _wav16k_mono(path):
    """读 wav → 16k 单声道 float32；**读不了就返回 None**（调用方退回整段路径）。

    只认 16-bit PCM / 16 kHz：能力后端的 `server.audio.to_wav16k` 与会议录音分段
    都是这个格式。别的采样率要重采样，那是另一件事（也说明这不是我们要切的对象）——
    返回 None 让行为退回改动前，而不是猜。
    """
    import wave as _wave
    import numpy as _np
    try:
        with _wave.open(path, "rb") as f:
            if f.getsampwidth() != 2 or f.getframerate() != 16000:
                return None
            channels = f.getnchannels()
            raw = f.readframes(f.getnframes())
    except Exception:
        return None
    try:
        a = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
        if channels > 1:
            a = a.reshape(-1, channels).mean(axis=1).astype(_np.float32)
    except Exception:
        return None
    return a


def _qwen3asr_chunks(audio, chunk_sec=None, sr=16000, search_sec=2.0):
    """把音频切成 ≤chunk_sec 的片段，返回 [(片段, 偏移秒), ...]。

    切点落在**目标点附近最安静的位置**（±2 秒窗口内、20 ms 滑窗能量和最小处）——
    直接按 60 秒硬切会落在词中间，那一两个音节当场丢掉。这也是 qwen-asr 自己
    切长音频的思路（低能量边界）；这里自己写十几行，是不想依赖它的内部函数。
    """
    import numpy as _np
    if audio is None:
        return []
    chunk_sec = float(chunk_sec or QWEN3_CHUNK_S)
    total = int(audio.shape[0])
    span = int(chunk_sec * sr)
    if total <= 0:
        return []
    if total <= span:
        return [(audio, 0.0)]
    win = max(4, int(0.02 * sr))
    reach = int(search_sec * sr)
    out = []
    start = 0
    while total - start > span:
        cut = start + span
        lo = max(start + win, cut - reach)
        hi = min(total - win, cut + reach)
        if hi > lo:
            energy = _np.convolve(_np.abs(audio[lo:hi]),
                                  _np.ones(win, dtype=_np.float32), mode="valid")
            cut = lo + int(_np.argmin(energy))
            cut = max(cut, start + 1)
        out.append((audio[start:cut], start / float(sr)))
        start = cut
    out.append((audio[start:], start / float(sr)))
    return out


def _row_text(res, i):
    """funasr 结果列表里第 i 条文本（**只取文本，不做任何拼装**）。"""
    try:
        return str((res[i] or {}).get("text") or "")
    except Exception:
        return ""


def _row_ts(res, i):
    """funasr 结果列表里第 i 条 token 级时间轴（秒）。"""
    try:
        rows = (res[i] or {}).get("timestamp") or []
        return [(float(t[0]), float(t[1])) for t in rows]
    except Exception:
        return []


def _qwen3asr_transcribe(m, wav, lang_hint, timestamps=False):
    """Qwen3-ASR 的唯一推理入口：**切片 + 分批** → [(片文本, 片内时间轴, 片偏移秒), ...]。

    为什么必须自己切片（2026-09-26 实测，同一段 600 秒真会议音频）：

    * qwen-asr 只在 **1200 秒**才自己切（`utils.MAX_ASR_INPUT_SECONDS`），
      所以 10 分钟音频是**一整条**长序列进模型；`funasr.AutoModel.generate` 在没有
      `vad_model` 时又是**一条一条**喂的（`batch_size` 默认 1）。实测：forward 236.6 秒、
      **peak 显存 11.4 GB**（这块卡只有 8 GB —— 溢出到共享内存了，转写期间 GPU
      利用率 95% 但吞吐全耗在换页上）、600 秒音频**只转出 36 个字**。
      切成 60 秒一片后同样的音频变成 11 条短序列，注意力从 O(n²) 掉到 11×O((n/11)²)，
      还能一次并行 4 条：forward 35.1 秒、peak 2.9 GB、3531 字。
    * 另一个**正确性**问题：生成上限默认 512 token；整段 600 秒音频的文本远超这个数，
      一旦模型真按这个上限生成就会被**截断**（切片后每片 60 秒，用不完）。

    切片/批处理也是 `return_time_stamps=True` 能有意义的前提：对齐器按片对齐，
    片内时间轴相对片头，**片偏移随每一片一起返回**（由 `_sentences_of()` 加回去）。
    不能借用 funasr 的 VAD 路径拿时间戳：那条路给列表型时间戳加的是**毫秒**偏移，
    而 qwen-asr 给的是**秒** —— 两个不同轴相加，时间戳会离谱到没法看
    （实测 VAD + `return_time_stamps` 出来的值从 `[0, 0]` 一路到 `[117192, 117192]`）。

    任何一步不成立（不是 16k 单声道 PCM、切不出片）就**退回整段** `m.generate()` ——
    那时行为与改动前逐字一致（慢，但不会更差）。
    """
    chunks = _qwen3asr_chunks(_wav16k_mono(wav))
    if not chunks:
        res = m.generate(input=wav, language=lang_hint,
                         **({"return_time_stamps": True} if timestamps else {}))
        if not res:
            return []
        return [(_row_text(res, 0), _row_ts(res, 0) if timestamps else [], 0.0)]
    extra = {"return_time_stamps": True} if timestamps else {}
    parts = []
    for i in range(0, len(chunks), QWEN3_BATCH):
        group = chunks[i:i + QWEN3_BATCH]
        res = m.generate(input=[a for a, _off in group], batch_size=len(group),
                         language=lang_hint, **extra)
        if not res:
            # 这一批没结果：**如实留空**，不假装有文本（上层按"这段没人说话"处理）
            for _a, off in group:
                parts.append(("", [], float(off)))
            continue
        for j, (_a, off) in enumerate(group):
            parts.append((_row_text(res, j), _row_ts(res, j) if timestamps else [], float(off)))
    return parts


def _sentences_of(text, ts, offset=0.0):
    """把**一段**文本 + 它的 token 级时间轴聚合成句级（按句末标点断句）。

    funasr/qwen-asr 返回的 timestamp 长度是 token 数（通常 < 文本字符数），
    这里按字符位置比例映射到 token 时间轴。`offset` 是这段在整段音频里的起点（秒）。

    **时间轴不许倒退**（`cursor`）：funasr 的 qwen3asr 包装把秒**截断成整数**
    （`int(ts.start_time)`），于是同一句话的末字和下一句的首字可能落在同一个 token 上 ——
    那 1 秒的重叠会让"按行铺时间轴"的下游（行与行不许交叠）出错。这里把下一条的起点夹到
    上一条的终点之后。精度上限仍然是那条 `int()`（1 秒）；对齐器本身给到 10 毫秒。
    """
    text = (text or "").strip()
    if not text:
        return []
    if not ts:
        return [(offset, offset, text)]
    n_tok, n_ch = len(ts), len(text)

    def tok_idx(i):
        return min(n_tok - 1, round(i * n_tok / max(1, n_ch - 1)))

    sentences = []
    cur = []
    seg_start = None
    cursor = offset
    last_end = offset
    for i, ch in enumerate(text):
        s, e = ts[tok_idx(i)]
        s, e = s + offset, e + offset
        last_end = e
        if seg_start is None:
            seg_start = max(s, cursor)
        cur.append(ch)
        if ch in "。！？…!?；;":
            txt = "".join(cur).strip()
            if txt:
                end = max(e, seg_start)
                sentences.append((seg_start, end, txt))
                cursor = end
            cur = []
            seg_start = None
    if cur:
        txt = "".join(cur).strip()
        if txt:
            sentences.append((seg_start, max(last_end, seg_start), txt))
    return sentences


def _qwen3asr_sentences(m, wav, lang_hint):
    """Qwen3-ASR + ForcedAligner：返回 (完整文本, [(start, end, 句子), ...])。

    句子是**按片**断出来的（每片 60 秒、切点在最安静处），再把片偏移加回去 ——
    所以时间轴是**对齐器给的真实时间**，不是按字数均摊的 estimated。
    失败返回 ("", [])（调用方据此回退别的骨架）。
    """
    try:
        parts = _qwen3asr_transcribe(m, wav, lang_hint, timestamps=True)
        text = "".join(t for t, _ts, _off in parts).strip()
        if not text:
            return "", []
        sentences = []
        for chunk_text, ts, off in parts:
            sentences.extend(_sentences_of(chunk_text, ts, off))
        if not sentences:
            return text, [(0.0, 0.0, text)]
        return text, sentences
    except Exception as e:
        print(f"[stt] Qwen3-ASR 时间戳转写失败: {e}", file=sys.stderr)
        return "", []

def transcribe(wav, engine="sensevoice", model="small", lang="zh", device="auto"):
    """转写单个 wav，返回文本（**兼容签名**：失败/空结果都返回空串）。

    要区分"没人说话"和"引擎挂了"请用 `transcribe_ex()` —— 见那里的说明（PROGRESS §19 发现③）。
    """
    return transcribe_ex(wav, engine, model, lang, device)["text"]


#: transcribe_ex() 的 status 取值
TRANSCRIBE_OK = "ok"           # 拿到文本
TRANSCRIBE_EMPTY = "empty"     # 引擎正常但没内容（可能是这段真没人说话）
TRANSCRIBE_ERROR = "error"     # 引擎抛异常（依赖缺失、显存不足、模型损坏…）
TRANSCRIBE_MISSING = "missing"  # 文件不存在


def transcribe_ex(wav, engine="sensevoice", model="small", lang="zh", device="auto"):
    """转写单个 wav，返回 ``{"text", "status", "detail"}``。

    为什么要有它（2026-09-19，PROGRESS §19 发现③）：原来的 `transcribe()` 每个分支都
    `except → print(stderr) → return ""`，于是**"这段没人说话"和"引擎挂了"完全一样** ——
    实测 whisper 对一段 23 秒静音音频会无异常、无 stderr、直接返回空串，调用方据此
    可能静默产出空纪要。本函数把区别显式化，`transcribe()` 保持原行为（返回 text）。
    """
    if not os.path.isfile(wav):
        return {"text": "", "status": TRANSCRIBE_MISSING, "detail": "文件不存在: %s" % wav}

    if engine == "sensevoice":
        try:
            sv = _get_sensevoice(device)
            res = sv.generate(input=wav, cache={}, language="auto", use_itn=True, batch_size_s=60)
            text = _clean_sv_text(res[0].get("text", "")) if res else ""
            return _result(text, "SenseVoice")
        except Exception as e:
            print(f"[stt] SenseVoice 转写失败: {e}", file=sys.stderr)
            return {"text": "", "status": TRANSCRIBE_ERROR, "detail": "SenseVoice: %s" % e}

    if engine == "sherpa":
        try:
            import numpy as np
            import wave as wave_mod
            rec = _get_sherpa()
            stream = rec.create_stream()
            with wave_mod.open(wav, "rb") as f:
                sr = f.getframerate()
                data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            chunk = 5120
            tail = np.zeros(int(0.6 * sr), dtype=np.float32)
            samples = np.concatenate([data, tail])
            for i in range(0, len(samples), chunk):
                stream.accept_waveform(sr, samples[i:i + chunk])
                while rec.is_ready(stream):
                    rec.decode_stream(stream)
            r = rec.get_result(stream)
            text = (r if isinstance(r, str) else r.text).strip()
            return _result(text, "sherpa")
        except Exception as e:
            print(f"[stt] sherpa 转写失败: {e}", file=sys.stderr)
            return {"text": "", "status": TRANSCRIBE_ERROR, "detail": "sherpa: %s" % e}

    if engine == "qwen3asr":
        try:
            # model 参数默认是 whisper 的 "small"，这里要解析成 Qwen3-ASR 模型名
            if model.startswith("Qwen/"):
                qwen_model = model
            elif model in ("0.6B", "1.7B"):
                qwen_model = f"Qwen/Qwen3-ASR-{model}"
            else:
                qwen_model = "Qwen/Qwen3-ASR-0.6B"
            m = _get_qwen3asr(device, qwen_model)
            lang_hint = _LANG_MAP.get(str(lang).lower(), None)
            # 切片 + 分批（`_qwen3asr_transcribe` 的注释解释了为什么不能整段喂）
            parts = _qwen3asr_transcribe(m, wav, lang_hint)
            text = " ".join("".join(t for t, _ts, _off in parts).split())
            return _result(text, "Qwen3-ASR")
        except Exception as e:
            print(f"[stt] Qwen3-ASR 转写失败: {e}", file=sys.stderr)
            return {"text": "", "status": TRANSCRIBE_ERROR, "detail": "Qwen3-ASR: %s" % e}

    # faster-whisper
    try:
        wm = _get_whisper(model, device)
        segments, _info = transcribe_whisper(wm, wav, lang)
        text = " ".join("".join(seg.text for seg in segments).split())
        return _result(text, "whisper")
    except Exception as e:
        print(f"[stt] whisper 转写失败: {e}", file=sys.stderr)
        return {"text": "", "status": TRANSCRIBE_ERROR, "detail": "whisper: %s" % e}


def _result(text, who):
    """统一成型：空文本 = EMPTY（引擎没报错，但没内容），非空 = OK。"""
    text = (text or "").strip()
    if text:
        return {"text": text, "status": TRANSCRIBE_OK, "detail": ""}
    return {"text": "", "status": TRANSCRIBE_EMPTY,
            "detail": "%s 返回空结果（可能是这段没有语音，也可能引擎内部静默失败）" % who}


def reset_engines():
    """卸载全部引擎（改配置/释放显存后调用）。"""
    with _ENGINE_LOCK:
        _ENGINES.clear()


# ---------------------------------------------------------------- 引擎状态

# GPU 型号名缓存：引擎加载完成后（torch 已安全导入）再取，请求路径不碰 torch
_GPU_NAME = ""


def _cache_gpu_name():
    """在 ctranslate2 已加载（顺序安全）后缓存 GPU 型号名，供 engine_status 展示。"""
    global _GPU_NAME
    if _GPU_NAME:
        return
    try:
        import torch
        if torch.cuda.is_available():
            _GPU_NAME = torch.cuda.get_device_name(0).split()[-1]
    except Exception:
        pass


# 设备信息缓存（请求路径热读，避免每次 /api/status 都 import ctranslate2 + 探测 CUDA）
_DEVICE_INFO = None


def _detect_device():
    global _DEVICE_INFO
    if _DEVICE_INFO is not None:
        return _DEVICE_INFO
    cuda = False
    dev = "cpu"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            cuda = True
            dev = "cuda:0" + (f" · {_GPU_NAME}" if _GPU_NAME else "")
    except Exception:
        pass
    _DEVICE_INFO = {"cuda": cuda, "dev": dev}
    return _DEVICE_INFO


def device_label():
    return _detect_device()["dev"]


def engine_status():
    """返回转写引擎加载状态（面板展示 + 外部调用诊断）。

    {"loaded": [{"key","engine","model"}...], "device": "cuda:0"|"cpu", "cuda": bool}

    请求路径热读缓存设备信息，不 import torch / 不重复探测 CUDA。
    """
    info = _detect_device()
    loaded = []
    with _ENGINE_LOCK:
        for key in sorted(_ENGINES):
            engine, _, model = key.partition(":")
            loaded.append({"key": key, "engine": engine, "model": model or ""})
    return {"loaded": loaded, "device": info["dev"], "cuda": info["cuda"]}


# ---------------------------------------------------------------- 引擎拆分（boot 编排用）

def resolve_engine(choice):
    """把配置值解析成 (engine_name, model)。"""
    choice = (choice or "sensevoice").strip()
    if choice == "sensevoice":
        return "sensevoice", "sensevoice"
    if choice == "sherpa":
        return "sherpa", ""
    if choice == "qwen3asr":
        return "qwen3asr", "Qwen/Qwen3-ASR-0.6B"
    if choice.startswith("Qwen/"):
        return "qwen3asr", choice
    if choice in ("0.6B", "1.7B"):
        return "qwen3asr", f"Qwen/Qwen3-ASR-{choice}"
    return "whisper", choice


def engine_key(engine_name, model, forced_aligner=QWEN3_FORCED_ALIGNER):
    """返回该引擎在 _ENGINES 里的 key。

    qwen3asr 的 key 里带**对齐器**：带与不带是两份权重、两个实例
    （所以 `forced_aligner` 不是可有可无的装饰，它是 key 的一部分）。
    """
    if engine_name == "sensevoice":
        return "sensevoice"
    if engine_name == "sherpa":
        return "sherpa"
    if engine_name == "qwen3asr":
        return f"qwen3asr:{model}:{qwen3asr_aligner(forced_aligner)}"
    return f"whisper:{model}"


def load_engine(engine_name, model, device="auto", forced_aligner=QWEN3_FORCED_ALIGNER):
    """按需/常驻加载引擎，返回其 _ENGINES key。

    torch 系（sensevoice/qwen3asr）加载前先 import ctranslate2，
    保证 ctranslate2 先于 torch 加载（否则 CUDA 动态库冲突 WinError 127）。

    `forced_aligner` 默认带上（见 `_get_qwen3asr`）：能力后端那个 spec 宣告了
    `supports: [asr.text, asr.timestamps]`，不带对齐器的话 `asr.timestamps` 是空头支票。
    返回的 key 与 `engine_key()` 用**同一份判据**算 —— 否则 `key_loaded()` 会假红。
    """
    if engine_name in ("sensevoice", "qwen3asr"):
        try:
            import ctranslate2  # noqa: F401  顺序安全
        except Exception:
            pass
    if engine_name == "sensevoice":
        _get_sensevoice(device)
        return "sensevoice"
    if engine_name == "sherpa":
        _get_sherpa()
        return "sherpa"
    if engine_name == "qwen3asr":
        _get_qwen3asr(device, model, forced_aligner)
        return engine_key("qwen3asr", model, forced_aligner)
    _get_whisper(model, device)
    return f"whisper:{model}"


def key_loaded(key):
    with _ENGINE_LOCK:
        return key in _ENGINES


def unload_key(key):
    with _ENGINE_LOCK:
        _ENGINES.pop(key, None)


# ---------------------------------------------------------------- CLI

def main():
    import argparse
    ap = argparse.ArgumentParser(description="ECHO 语音转文字")
    ap.add_argument("wav")
    ap.add_argument("--engine", default="sensevoice",
                    choices=["whisper", "sensevoice", "sherpa", "qwen3asr"])
    ap.add_argument("--model", default="small")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()
    text = transcribe(args.wav, engine=args.engine, model=args.model,
                      lang=args.lang, device=args.device)
    print(text)
    return 0 if text else 2


if __name__ == "__main__":
    sys.exit(main())
