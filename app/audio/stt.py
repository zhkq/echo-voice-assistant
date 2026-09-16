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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.path.join(BASE_DIR, "models")
os.environ.setdefault("HF_HOME", MODELS_DIR)
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
    d = os.path.join(MODELS_DIR, "faster-whisper", name)
    return d if os.path.isfile(os.path.join(d, "model.bin")) else ""


def _sensevoice_dir():
    """models/sensevoice 下可能有多层 snapshots/<hash>，自动下探找到 model.pt。

    注意：funasr 在 Windows 上无法加载含非 ASCII 字符（如中文）的本地路径，
    遇到这种情况返回 ""，让调用方走模型名（iic/SenseVoiceSmall，落 modelscope 缓存）。
    """
    root = os.path.join(MODELS_DIR, "sensevoice")
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
    d = os.path.join(MODELS_DIR, "sherpa-onnx-streaming")
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
            raise RuntimeError("sherpa 流式模型未就绪: " + os.path.join(MODELS_DIR, "sherpa-onnx-streaming"))
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
    （2026-09-15 事故：迁移后 ECHO_PYTHON/ECHO_PYTHONW 被指向中文路径下的 venv，
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
            "请用指向 venv 的 ASCII 目录联接（junction）解释器启动 ECHO，并设置 ECHO_PYTHON 与 "
            "ECHO_PYTHONW（DSH 的 echo-host 插件用后者拉起服务），详见 docs/DEPLOY.md。")


def _get_qwen3asr(device="auto", model_name="Qwen/Qwen3-ASR-0.6B", forced_aligner=None):
    """funasr Qwen3-ASR（qwen-asr 包，52 语言，中文准确率高于 SenseVoice）。

    依赖：qwen-asr==0.0.6 + transformers==4.57.6（scripts/setup.ps1 或 README 有说明）。
    模型经 modelscope 缓存（~/.cache/modelscope，无中文路径，funasr 可直接加载）。
    显存：0.6B ~4GB，1.7B ~8GB（GPU 不足自动回退 CPU）。
    forced_aligner: "Qwen/Qwen3-ForcedAligner-0.6B" 时启用字符级时间戳（会议转写原生句子）。
    """
    key = f"qwen3asr:{model_name}:{forced_aligner or ''}"
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
        model_path = _resolve_ms_cache(model_name)
        aligner_path = _resolve_ms_cache(forced_aligner) if forced_aligner else None
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


# ---------------------------------------------------------------- 转写入口

def _qwen3asr_sentences(m, wav, lang_hint):
    """Qwen3-ASR + ForcedAligner：返回 (完整文本, [(start, end, 句子), ...])。

    funasr 返回的 timestamp 是「token 级」[start_sec, end_sec]（长度=token 数，
    通常 < 文本字符数），这里按字符位置比例映射到 token 时间轴，再按句末
    标点聚合成自然句子。失败返回 ("", [])。
    """
    try:
        res = m.generate(input=wav, language=lang_hint, return_time_stamps=True)
        if not res:
            return "", []
        text = (res[0].get("text") or "").strip()
        ts = res[0].get("timestamp") or []
        if not text:
            return "", []
        if not ts:
            return text, [(0.0, 0.0, text)]
        n_tok, n_ch = len(ts), len(text)

        def tok_idx(i):
            return min(n_tok - 1, round(i * n_tok / max(1, n_ch - 1)))

        sentences = []
        cur = []
        seg_start = None
        last_end = 0.0
        for i, ch in enumerate(text):
            t = ts[tok_idx(i)]
            s, e = float(t[0]), float(t[1])
            last_end = e
            if seg_start is None:
                seg_start = s
            cur.append(ch)
            if ch in "。！？…!?；;":
                txt = "".join(cur).strip()
                if txt:
                    sentences.append((seg_start, e, txt))
                cur = []
                seg_start = None
        if cur:
            txt = "".join(cur).strip()
            if txt:
                sentences.append((seg_start, last_end, txt))
        return text, sentences
    except Exception as e:
        print(f"[stt] Qwen3-ASR 时间戳转写失败: {e}", file=sys.stderr)
        return "", []

def transcribe(wav, engine="sensevoice", model="small", lang="zh", device="auto"):
    """转写单个 wav，返回文本（失败返回空串并打印 stderr）。"""
    if not os.path.isfile(wav):
        return ""

    if engine == "sensevoice":
        try:
            sv = _get_sensevoice(device)
            res = sv.generate(input=wav, cache={}, language="auto", use_itn=True, batch_size_s=60)
            if not res:
                return ""
            return _clean_sv_text(res[0].get("text", ""))
        except Exception as e:
            print(f"[stt] SenseVoice 转写失败: {e}", file=sys.stderr)
            return ""

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
            return (r if isinstance(r, str) else r.text).strip()
        except Exception as e:
            print(f"[stt] sherpa 转写失败: {e}", file=sys.stderr)
            return ""

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
            res = m.generate(input=wav, language=lang_hint)
            if not res:
                return ""
            return " ".join((res[0].get("text") or "").split())
        except Exception as e:
            print(f"[stt] Qwen3-ASR 转写失败: {e}", file=sys.stderr)
            return ""

    # faster-whisper
    try:
        wm = _get_whisper(model, device)
        segments, _info = transcribe_whisper(wm, wav, lang)
        text = "".join(seg.text for seg in segments).strip()
        return " ".join(text.split())
    except Exception as e:
        print(f"[stt] whisper 转写失败: {e}", file=sys.stderr)
        return ""


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


def engine_key(engine_name, model):
    """返回该引擎在 _ENGINES 里的 key。"""
    if engine_name == "sensevoice":
        return "sensevoice"
    if engine_name == "sherpa":
        return "sherpa"
    if engine_name == "qwen3asr":
        return f"qwen3asr:{model}:"
    return f"whisper:{model}"


def load_engine(engine_name, model, device="auto"):
    """按需/常驻加载引擎，返回其 _ENGINES key。

    torch 系（sensevoice/qwen3asr）加载前先 import ctranslate2，
    保证 ctranslate2 先于 torch 加载（否则 CUDA 动态库冲突 WinError 127）。
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
        _get_qwen3asr(device, model)
        return f"qwen3asr:{model}:"
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
