# -*- coding: utf-8 -*-
"""modelinfo.py — 模型清单（面板 → 设置 → 模型 的唯一数据源）

目的：拿到本项目的人一眼看清"哪些模型要下、多大、下到哪、怎么下"。
每条只描述三件事，互相独立：
  1. name / purpose —— 显示用，随便起；
  2. target / ready  —— 落地路径与就绪判断，**路径是代码约定，不能改名**；
  3. source / how / cmd —— 获取方式（自动下载 / 脚本 / 只能从源机拷贝）。

只读检测，不下载任何东西。

路径约定的出处（改名会让加载器找不到模型）：
  app/audio/stt.py      _sensevoice_dir() / _whisper_dir() / _sherpa_files() / _resolve_ms_cache()
  app/audio/wake.py     KWS_MODEL_DIR + 四个写死的文件名
  app/audio/diarize.py  PYANNOTE_DIR / SEG_DIR / EMB_DIR / PLDA_DIR
ModelScope 缓存固定在 ~/.cache/modelscope/models（funasr 不认识中文路径，所以不放仓库里）。
"""
import importlib.util
import os
import shlex
import threading
import time

# 和 stt.py 用同一套缓存约定：HF 落 models/hub（走国内镜像），ModelScope 落 ~/.cache/modelscope
os.environ.setdefault("HF_HOME", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("MODELSCOPE_DISABLE_PROGRESS_BAR", "1")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(BASE_DIR, "models")
MS_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "modelscope", "models")

WHISPER_SIZES = {"tiny": "~75 MB", "base": "~141 MB", "small": "~464 MB",
                 "medium": "~1.5 GB", "large-v3": "~2.9 GB"}


def _dir_mb(path):
    """目录实际占用（MB）；不存在返回 0。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return round(total / 1048576)


def _first_stem_dir_with_files(root, prefixes, must_have=()):
    """models/sensevoice 允许 snapshots/<hash>/ 这种多层结构：逐层找含指定文件的目录。"""
    if not os.path.isdir(root):
        return None
    for cur, _dirs, files in os.walk(root):
        if all(any(f.startswith(p) for f in files) for p in prefixes) and \
           all(f in files for f in must_have):
            return cur
    return None


def _ms_dir(model_id):
    return os.path.join(MS_CACHE, model_id.replace("/", "--"))


def _pkg_available(name):
    """仅探测包能否 import（find_spec 不真正加载，避免拖慢面板/占用显存）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _sensevoice_model_dir():
    """本地 models/sensevoice（含多层 snapshots）或 ModelScope 缓存，返回落地目录或 None。"""
    if _first_stem_dir_with_files(os.path.join(MODELS_DIR, "sensevoice"), ["model."]):
        return os.path.join(MODELS_DIR, "sensevoice")
    if os.path.isdir(_ms_dir("iic/SenseVoiceSmall")):
        return _ms_dir("iic/SenseVoiceSmall")
    return None


def _ready_sensevoice():
    """模型落地 + funasr/torch 运行时都齐了才算就绪。

    只下载模型、没装 funasr（或 torch）时跑不起来，此时不应显示「已就绪」。
    """
    return (_sensevoice_model_dir() is not None
            and _pkg_available("funasr") and _pkg_available("torch"))


def _whisper_hub_dir(name):
    """HF 缓存目录（面板点下载落这里；stt 按名字加载时也读这个缓存）。"""
    return os.path.join(MODELS_DIR, "hub", f"models--Systran--faster-whisper-{name}")


def _ready_whisper(name):
    """两种落地都算就绪：
       ① stt._whisper_dir() 认的本地目录 models/faster-whisper/<档>/model.bin；
       ② HF 缓存 models/hub/models--Systran--faster-whisper-<档>/snapshots/*/model.bin（面板下载的产物）。"""
    if os.path.isfile(os.path.join(MODELS_DIR, "faster-whisper", name, "model.bin")):
        return True
    hub = _whisper_hub_dir(name)
    for _root, _dirs, files in os.walk(hub) if os.path.isdir(hub) else ():
        if "model.bin" in files:
            return True
    return False


def _ready_sherpa():
    """stt._sherpa_files(): encoder*/decoder*/joiner*.onnx + tokens.txt 同时存在。"""
    d = os.path.join(MODELS_DIR, "sherpa-onnx-streaming")
    if not os.path.isdir(d):
        return False
    files = os.listdir(d)
    def ok(pre, suf):
        return any(f.startswith(pre) and f.endswith(suf) for f in files)
    return ok("encoder", ".onnx") and ok("decoder", ".onnx") and ok("joiner", ".onnx") \
        and os.path.isfile(os.path.join(d, "tokens.txt"))


def _ready_kws():
    """wake.py 里写死的四个文件名。"""
    d = os.path.join(MODELS_DIR, "wakeword", "kws-zh-en-3m")
    need = ["encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
            "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "tokens.txt"]
    return all(os.path.isfile(os.path.join(d, n)) for n in need)


def _ready_pyannote():
    """空目录不能代表模型已下载；校验加载器需要的实际文件。"""
    from scripts.install_pyannote import ASSETS
    return all(os.path.isfile(os.path.join(MODELS_DIR, "pyannote", folder, filename))
               and os.path.getsize(os.path.join(MODELS_DIR, "pyannote", folder, filename)) > 0
               for _, folder, filename in ASSETS)


def _pyannote_command():
    from scripts.install_pyannote import ASSETS
    hf = os.path.join(BASE_DIR, "venv", "Scripts" if os.name == "nt" else "bin",
                      "hf.exe" if os.name == "nt" else "hf")
    lines = []
    if os.name == "nt":
        lines.append("$env:HF_ENDPOINT = 'https://huggingface.co'")
        lines.append("$env:HF_HUB_OFFLINE = '0'")
    for repo, folder, filename in ASSETS:
        args = [hf, "download", repo, filename, "--local-dir",
                os.path.join(BASE_DIR, "models", "pyannote", folder)]
        if os.name == "nt":
            lines.append("& " + " ".join("'" + p.replace("'", "''") + "'" for p in args))
            lines.append("if ($LASTEXITCODE -ne 0) { throw '模型下载失败，请检查授权与网络' }")
        else:
            lines.append("HF_ENDPOINT=https://huggingface.co HF_HUB_OFFLINE=0 " + shlex.join(args))
    return "\n".join(lines) if os.name == "nt" else " &&\n".join(lines)


def _ready_qwen(model_id):
    return os.path.isdir(_ms_dir(model_id))


# ---------------------------------------------------------------- 清单
# source: auto = 首次使用会自动联网下载；script = 跑安装脚本；copy = 只能从源机拷贝
CATALOG = [
    dict(id="sensevoice", group="转写引擎", name="SenseVoice 中文短命令",
         purpose="语音命令 + 会议转写的默认引擎", size="~896 MB",
         target="models/sensevoice 或 ModelScope 缓存（二选一）",
         source="auto", ref="iic/SenseVoiceSmall",
         how="还需 funasr + torch 运行时（Windows 随 requirements.txt 装好；macOS 运行 "
             "`venv/bin/pip install funasr modelscope torch`）。选中即用：首次加载会自动从 "
             "ModelScope 下载模型（含 VAD）。也可先手动拉：",
         cmd='python -c "from modelscope import snapshot_download; print(snapshot_download(\'iic/SenseVoiceSmall\'))"'),

    dict(id="qwen3asr", group="转写引擎", name="Qwen3-ASR 0.6B + 强制对齐",
         purpose="更准的中文转写（会议纪要推荐，显存约 4GB）", size="~3.6 GB（两个模型）",
         target="ModelScope 缓存 Qwen/Qwen3-ASR-0.6B + Qwen/Qwen3-ForcedAligner-0.6B",
         source="script", ref="Qwen/Qwen3-ASR-0.6B",
         how="跑安装脚本（会先装 qwen-asr/transformers 依赖，再从 ModelScope 下模型）：",
         cmd="powershell -ExecutionPolicy Bypass -File scripts\\install-qwen3asr.ps1"),

    dict(id="sherpa", group="转写引擎", name="sherpa-onnx 流式 zipformer（中英）",
         purpose="流式转写；唤醒词功能也用它", size="~189 MB",
         target="models/sherpa-onnx-streaming/（encoder*/decoder*/joiner*.onnx + bpe.model + tokens.txt）",
         source="auto", ref="csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
         allow=["encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
                "joiner-epoch-99-avg-1.int8.onnx", "bpe.model", "tokens.txt"],
         how="上游是 HF 上的 k2-fsa 模型（国内走 hf-mirror 镜像）。点下载只拉 int8 三件 + bpe.model + "
             "+ tokens.txt（约 189MB）直接落到该目录；代码用前缀匹配认文件，文件名不用改。"),
]

for _n in ("tiny", "base", "small", "medium", "large-v3"):
    CATALOG.append(dict(
        id=f"whisper-{_n}", group="转写引擎", name=f"Whisper {_n}",
        purpose=("faster-whisper 档位" + ("（默认 whisper 档）" if _n == "small" else "")
                 + "；中文可能输出繁体（训练语料偏繁体），需要简体请改用 SenseVoice / Qwen3-ASR"),
        size=WHISPER_SIZES[_n],
        target=f"models/faster-whisper/{_n}/（须含 model.bin）或 HF 缓存 models/hub/models--Systran--faster-whisper-{_n}/",
        source="auto", ref=f"Systran/faster-whisper-{_n}",
        how="点下载会从 HF 镜像（hf-mirror.com）拉到 models/hub 缓存（stt 按名字加载时直接命中该缓存）；"
            "也可以从源机拷 models/faster-whisper/<档> 目录过来。",
        cmd=f'python -c "from faster_whisper import WhisperModel; WhisperModel(\'{_n}\')"'))

CATALOG += [
    dict(id="pyannote", group="可选功能", name="说话人分离（pyannote 三件套）",
         purpose="会议纪要区分说话人（设置里默认关闭）", size="模型与 PyTorch 等依赖需额外磁盘空间",
         target="models/pyannote/{pyannote-segmentation-3.0-local, pyannote-wespeaker-local, pyannote-plda-local}",
         source="script", ref="pyannote/segmentation-3.0 + wespeaker-voxceleb-resnet34-LM",
         cmd=_pyannote_command(), cmd_label="复制下载命令", downloadable=False,
         links=[{"label": "分段模型授权", "url": "https://huggingface.co/pyannote/segmentation-3.0"},
                {"label": "PLDA 模型授权", "url": "https://huggingface.co/pyannote/speaker-diarization-community-1"}],
         how="首次需在官方页面同意使用条件，并通过 hf auth login 登录（只读 Token）。"
             "复制下方命令到终端自行执行，即可将所需文件下载到对应目录。"
             "命令仅下载模型；使用说话人分离还需安装 pyannote.audio 4.x 与 speechbrain 依赖。"),

    dict(id="kws", group="可选功能", name="唤醒词 KWS（kws-zh-en-3m）",
         purpose="语音唤醒（设置里默认关闭）", size="~39 MB",
         target="models/wakeword/kws-zh-en-3m/（四个文件名写死）",
         source="copy", ref="sherpa-onnx kws-zh-en-3m",
         how="只能拷贝：wake.py 写死了 encoder/decoder/joiner-epoch-13-avg-2-chunk-8-left-64(.int8).onnx 与 tokens.txt 四个文件名，目录名与其他文件都不能改。"),
]

_PROBES = {
    "sensevoice": _ready_sensevoice,
    "qwen3asr": lambda: _ready_qwen("Qwen/Qwen3-ASR-0.6B") and _ready_qwen("Qwen/Qwen3-ForcedAligner-0.6B"),
    "sherpa": _ready_sherpa,
    "pyannote": _ready_pyannote,
    "kws": _ready_kws,
}


def _target_path(entry):
    """就绪检测对应的实际目录（用于统计本地占用）。"""
    i = entry["id"]
    if i == "sensevoice":
        return _sensevoice_model_dir() or _ms_dir("iic/SenseVoiceSmall")
    if i == "qwen3asr":
        return _ms_dir("Qwen/Qwen3-ASR-0.6B")
    if i == "sherpa":
        return os.path.join(MODELS_DIR, "sherpa-onnx-streaming")
    if i == "pyannote":
        return os.path.join(MODELS_DIR, "pyannote")
    if i == "kws":
        return os.path.join(MODELS_DIR, "wakeword", "kws-zh-en-3m")
    if i.startswith("whisper-"):
        tier = i.split("-", 1)[1]
        local = os.path.join(MODELS_DIR, "faster-whisper", tier)
        return local if os.path.isdir(local) else _whisper_hub_dir(tier)
    return ""


def _measure_paths(entry):
    """本地占用的统计范围。pyannote 只统计真正要用的三个 -local 目录，
    否则会把同目录下的 faster-whisper-* 冗余副本（5GB）也算进去。"""
    i = entry["id"]
    if i == "pyannote":
        p = os.path.join(MODELS_DIR, "pyannote")
        return [os.path.join(p, n) for n in ("pyannote-segmentation-3.0-local",
                                             "pyannote-wespeaker-local",
                                             "pyannote-plda-local")]
    path = _target_path(entry)
    return [path] if path else []


def inventory():
    """返回清单 + 就绪状态 + 本地实际占用（MB）。任何异常都不抛，按未就绪处理。"""
    items = []
    for e in CATALOG:
        probe = _PROBES.get(e["id"])
        if probe is None:
            name = e["id"].split("-", 1)[1] if e["id"].startswith("whisper-") else ""
            def probe(n=name):
                return _ready_whisper(n)
        try:
            ready = bool(probe())
        except Exception:
            ready = False
        local = sum(_dir_mb(p) for p in _measure_paths(e) if os.path.isdir(p))
        items.append(dict(e, ready=ready, local_mb=local))
    return items


# ---------------------------------------------------------------- 下载（面板按钮）
# pyannote 仅提供可复制的官方下载命令，由用户在终端执行。
_EXPECTED_MB = {"sensevoice": 900, "qwen3asr": 3600, "sherpa": 190,
                "whisper-tiny": 75, "whisper-base": 141, "whisper-small": 464,
                "whisper-medium": 1500, "whisper-large-v3": 2950}

_JOBS = {}                      # id -> {status, percent, message, started_at, done_at, mb}
_ACTIVE = {"id": None}
_JOB_LOCK = threading.Lock()


def _watch_paths(mid):
    """下载进度只统计这些目录的体积增长（不依赖库的进度回调）。"""
    if mid == "sensevoice":
        return [os.path.join(MS_CACHE, "iic--SenseVoiceSmall"),
                os.path.join(MS_CACHE, "iic--speech_fsmn_vad_zh-cn-16k-common-pytorch")]
    if mid == "qwen3asr":
        return [os.path.join(MS_CACHE, "Qwen--Qwen3-ASR-0.6B"),
                os.path.join(MS_CACHE, "Qwen--Qwen3-ForcedAligner-0.6B")]
    if mid.startswith("whisper-"):
        tier = mid.split("-", 1)[1]
        return [os.path.join(MODELS_DIR, "hub", f"models--Systran--faster-whisper-{tier}")]
    if mid == "sherpa":
        return [os.path.join(MODELS_DIR, "sherpa-onnx-streaming")]
    return []


def _downloaded_mb(mid):
    return sum(_dir_mb(p) for p in _watch_paths(mid) if os.path.isdir(p))


def _download_worker(entry):
    mid = entry["id"]
    expected = _EXPECTED_MB.get(mid) or 0
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            mb = _downloaded_mb(mid)
            pct = min(99, int(mb * 100 / expected)) if expected else 0
            _JOBS[mid].update(downloaded_mb=mb, percent=pct)
            stop.wait(2)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    # diarize.py:107 会把 HF_HUB_OFFLINE 设成 "1"（强制 pyannote 只用本地目录，不去联网）。
    # 但 huggingface_hub 是在 **import 时**把该变量读进 constants.HF_HUB_OFFLINE 的，
    # 所以下载期间要同时改环境变量和常量，结束后再原样恢复，别破坏 pyannote 的离线策略。
    prev_env = os.environ.get("HF_HUB_OFFLINE")
    prev_const = None
    os.environ["HF_HUB_OFFLINE"] = "0"
    try:
        import huggingface_hub.constants as _hc
        prev_const = getattr(_hc, "HF_HUB_OFFLINE", None)
        _hc.HF_HUB_OFFLINE = False
    except Exception:
        pass

    try:
        if mid == "sensevoice":
            from modelscope import snapshot_download
            snapshot_download("iic/SenseVoiceSmall")
            try:
                snapshot_download("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
            except Exception as e:
                print(f"[modelinfo] VAD 模型下载失败（加载时会再试）: {e}")
        elif mid == "qwen3asr":
            from modelscope import snapshot_download
            snapshot_download("Qwen/Qwen3-ASR-0.6B")
            snapshot_download("Qwen/Qwen3-ForcedAligner-0.6B")
        elif mid.startswith("whisper-"):
            tier = mid.split("-", 1)[1]
            from huggingface_hub import snapshot_download
            snapshot_download(f"Systran/faster-whisper-{tier}")
        elif mid == "sherpa":
            # 只拉代码认的那几个文件名，直接落到目标目录（local_dir），省掉 400MB 的 fp32 与测试音频
            from huggingface_hub import snapshot_download
            snapshot_download(entry["ref"],
                              local_dir=os.path.join(MODELS_DIR, "sherpa-onnx-streaming"),
                              allow_patterns=entry.get("allow") or None)
        _JOBS[mid].update(status="done", percent=100,
                          message="下载完成", done_at=time.strftime("%H:%M:%S"),
                          downloaded_mb=_downloaded_mb(mid))
        print(f"[modelinfo] {mid} 下载完成，{_downloaded_mb(mid)} MB")
    except Exception as e:
        _JOBS[mid].update(status="failed", message=f"{type(e).__name__}: {e}",
                          done_at=time.strftime("%H:%M:%S"))
        print(f"[modelinfo] {mid} 下载失败: {e}")
    finally:
        stop.set()
        _ACTIVE["id"] = None
        # 恢复离线标记（原值可能来自 diarize.py 的强制离线）
        if prev_env is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = prev_env
        if prev_const is not None:
            try:
                import huggingface_hub.constants as _hc
                _hc.HF_HUB_OFFLINE = prev_const
            except Exception:
                pass


def _is_ready(entry):
    """统一就绪判断（download 前的保护用；whisper 档不在 _PROBES 里，要按档名判）。"""
    mid = entry["id"]
    if mid.startswith("whisper-"):
        return _ready_whisper(mid.split("-", 1)[1])
    probe = _PROBES.get(mid)
    return bool(probe()) if probe else False


def start_download(mid, force=False):
    """开始下载（后台线程）。返回 (ok, message)。同一时刻只允许一个下载任务。

    force=False 时，已就绪的模型会拒绝——避免手滑重下 190MB / 3GB（界面上"重新下载"才带 force）。
    """
    entry = _by_id(mid)
    if not entry:
        return False, f"未知模型: {mid}"
    if entry.get("downloadable") is False:
        return False, "请复制下载命令，在终端自行执行。"
    if entry.get("source") == "copy":
        return False, "该模型没有稳定的公开下载源，请从源机拷贝（见说明）"
    if not force:
        try:
            if _is_ready(entry):
                return False, f"{entry['name']} 已就绪；要重装请点「重新下载」"
        except Exception:
            pass
    with _JOB_LOCK:
        if _ACTIVE["id"]:
            return False, f"已有下载在进行：{_ACTIVE['id']}"
        _ACTIVE["id"] = mid
        _JOBS[mid] = {"id": mid, "status": "running", "percent": 0, "downloaded_mb": 0,
                      "message": "下载中…", "started_at": time.strftime("%H:%M:%S"), "done_at": ""}
    threading.Thread(target=_download_worker, args=(entry,), daemon=True).start()
    return True, f"已开始下载 {entry['name']}"


def _by_id(mid):
    for e in CATALOG:
        if e["id"] == mid:
            return e
    return None


def jobs():
    """当前/历史下载任务状态（面板轮询用）。"""
    active = _ACTIVE["id"]
    out = {}
    for mid, job in _JOBS.items():
        j = dict(job)
        j["active"] = (mid == active)
        out[mid] = j
    return {"active": active, "items": out}
