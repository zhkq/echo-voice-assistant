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
  app/audio/wake.py     kws_model_dir() + 四个写死的文件名
  app/audio/diarize.py  pyannote_dir() / segmentation_dir() / embedding_dir() / plda_dir()
ModelScope 缓存固定在 ~/.cache/modelscope/models（funasr 不认识中文路径，所以不放仓库里）。
"""
import importlib.util
import os
import re
import threading
import time

from app import interpreter
from app import paths
from app import platform as echo_platform

# 安装根由路径层给（含 ECHO_ROOT 覆盖）；下面的 HF_HOME 也用它，所以先定义。
BASE_DIR = paths.echo_root()

# 和 stt.py 用同一套缓存约定：HF 落 models/hub（走国内镜像），ModelScope 落 ~/.cache/modelscope
#
# 下面这行的兜底值**必须等于权威值** `paths.models_root()`：
#   * 老布局 → `{ECHO}/models`
#   * 新布局（3.0 安装根） → `{echoBase}/models`
# 2026-09-24 改布局时差点留下一个大坑：原来这里写死 `BASE_DIR/models`，
# 而新布局下 `BASE_DIR`（代码根）= `{echoBase}/echo-core` —— 于是**权重下到
# `echo-core/models`、而 `models_root()` 去 `{echoBase}/models` 找**，两边不一致，
# huggingface 会静默重下几 GB，而且模型落进了"可被整体覆盖"的代码目录（违反 L6）。
# 权威值仍由启动阶段（app/main.py 的 lifespan，seed_defaults() 之后）再定一次：
# 配置库要等 seed 完才准，而这个模块可能更早被 import。
os.environ.setdefault("HF_HOME", paths.models_root())
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("MODELSCOPE_DISABLE_PROGRESS_BAR", "1")


def models_dir() -> str:
    """当前生效的模型目录（用户可在面板里改，见 D20/D21）。"""
    from app import paths
    return paths.models_root()
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


#: 权重文件的形状 —— 加载器真正要读的那一类文件名。**"目录存在"不等于"已下载"**：
#: 下载中断/失败留下的空目录会让面板显示「已就绪」，而引擎一加载就炸
#: （pyannote 那边早就踩过这个坑，那条注释写着"空目录不能代表模型已下载"）。
#: `.pb` 是 SenseVoice 老权重的后缀（`stt._sensevoice_dir()` 认 model.pt / model.pb）。
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".pb", ".onnx", ".npz", ".ckpt")


def _has_weights(root, prefix="model."):
    """`root` 下（含 `snapshots/<rev>/` 多层结构与 HF 缓存里的符号链接）有没有权重文件。

    就绪判据的**唯一**形状：问"加载器要读的那个文件在不在"，而不是"这个目录在不在"。
    """
    if not root or not os.path.isdir(root):
        return False
    for _cur, _dirs, files in os.walk(root):
        for name in files:
            if name.startswith(prefix) and name.endswith(_WEIGHT_SUFFIXES):
                return True
    return False


def _pkg_available(name):
    """仅探测包能否 import（find_spec 不真正加载，避免拖慢面板/占用显存）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _sensevoice_model_dir():
    """本地 models/sensevoice（含多层 snapshots）或 ModelScope 缓存，返回落地目录或 None。

    **两处都要真有权重**：`models/sensevoice` 下光有目录不算（下载中断会留下空目录），
    ModelScope 缓存同理 —— 判据必须与"加载器到时候会不会炸"一致（`_has_weights`）。
    """
    local = os.path.join(models_dir(), "sensevoice")
    if _first_stem_dir_with_files(local, ["model."]):
        return local
    ms = _ms_dir("iic/SenseVoiceSmall")
    if _has_weights(ms):
        return ms
    return None


def _ready_sensevoice():
    """模型落地 + funasr/torch 运行时都齐了才算就绪。

    只下载模型、没装 funasr（或 torch）时跑不起来，此时不应显示「已就绪」。
    """
    return (_sensevoice_model_dir() is not None
            and _pkg_available("funasr") and _pkg_available("torch"))


def _whisper_hub_dir(name):
    """HF 缓存目录（面板点下载落这里；stt 按名字加载时也读这个缓存）。"""
    return os.path.join(models_dir(), "hub", f"models--Systran--faster-whisper-{name}")


def _whisper_landings(name):
    """whisper 每个档位**实际会落地**的三个位置（顺序 = 优先级）：

      ① `models/faster-whisper/<档>/`      —— `stt._whisper_dir()` 认的本地目录（拷贝来的）；
      ② `models/hub/models--Systran--faster-whisper-<档>/` —— HF 缓存（`HF_HOME` 指向 models）；
      ③ `~/.cache/modelscope/models/Systran--faster-whisper-<档>/` —— **ModelScope 缓存**。

    ③ 是漏掉的那一个（2026-09-26 抓到的 bug）：面板的下载按钮对 whisper 走
    `_snapshot(ms_id=ms_ref, hf_id=ref)` —— **ModelScope 优先**，所以它下到的是 ③，
    而就绪判据只查 ① ②。于是真下完了 72 MB，面板仍然写「未安装」。
    这条判据必须跟着"下载真的下到哪"走，否则"就绪"这个词就是假的。
    """
    return [
        os.path.join(models_dir(), "faster-whisper", name),
        _whisper_hub_dir(name),
        _ms_dir("Systran/faster-whisper-%s" % name),
    ]


def _ready_whisper(name):
    """三个落点里**任意一处真有权重**就算就绪（见 `_whisper_landings`）。

    为什么必须是"真有权重"而不是"目录在"：`faster_whisper.WhisperModel` 拿到一个空目录
    也是当场抛异常，与"没下载"在用户眼里没区别 —— 谎报就绪只会把人引到别处去找问题。
    """
    return any(_has_weights(p) for p in _whisper_landings(name))


def _ready_sherpa():
    """stt._sherpa_files(): encoder*/decoder*/joiner*.onnx + tokens.txt 同时存在。"""
    d = os.path.join(models_dir(), "sherpa-onnx-streaming")
    if not os.path.isdir(d):
        return False
    files = os.listdir(d)
    def ok(pre, suf):
        return any(f.startswith(pre) and f.endswith(suf) for f in files)
    return ok("encoder", ".onnx") and ok("decoder", ".onnx") and ok("joiner", ".onnx") \
        and os.path.isfile(os.path.join(d, "tokens.txt"))


def _ready_kws():
    """wake.py 里写死的四个文件名。"""
    d = os.path.join(models_dir(), "wakeword", "kws-zh-en-3m")
    need = ["encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "decoder-epoch-13-avg-2-chunk-8-left-64.onnx",
            "joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx",
            "tokens.txt"]
    return all(os.path.isfile(os.path.join(d, n)) for n in need)


def ready_pyannote():
    """空目录不能代表模型已下载；校验加载器需要的实际文件。

    公开函数：boot 的「说话人分离」就绪判定直接用它（别再调 _ready_* 私有名）。
    """
    from scripts.install_pyannote import ASSETS
    return all(os.path.isfile(os.path.join(models_dir(), "pyannote", folder, filename))
               and os.path.getsize(os.path.join(models_dir(), "pyannote", folder, filename)) > 0
               for _, folder, filename in ASSETS)


def _pyannote_command():
    from scripts.install_pyannote import ASSETS
    from app import platform as echo_platform

    hf = echo_platform.hf_executable(BASE_DIR)
    jobs = []
    for repo, folder, filename in ASSETS:
        jobs.append([hf, "download", repo, filename, "--local-dir",
                     os.path.join(models_dir(), "pyannote", folder)])
    return echo_platform.shell_script(hf, jobs)


def _ready_qwen(model_id):
    """ModelScope 缓存里落地**并且真有权重**才算。

    原来只看 `os.path.isdir` —— 一个建好了但没下完（或下砸了）的空目录会被报成
    「已就绪」，而 funasr 一加载就炸。对齐器也走这条判据（`_PROBES["qwen3asr"]` 两个都问）。
    """
    return _has_weights(_ms_dir(model_id))


def _cmd_chain(*parts):
    """把几条可粘贴命令用 `&&` 串起来（取不到的那些丢掉，免得留下光秃秃的 `&&`）。"""
    return " && ".join(p for p in parts if p)


#: SenseVoice 的手动下载命令。**载荷里只用单引号**：PowerShell 5.1 把带内嵌双引号的参数
#: 传给原生 exe 时会把引号吞掉（`-c "print(\"a b\")"` 到 python 手里成了 `print(a b)`）,
#: 而这条命令就是要给人粘进 PowerShell 的。护栏见 tests/test_install_state.py。
_SENSEVOICE_PY = ("from modelscope import snapshot_download; "
                  "print(snapshot_download('iic/SenseVoiceSmall'))")
_QWEN3ASR_PY = ("from modelscope import snapshot_download; "
                "snapshot_download('Qwen/Qwen3-ASR-0.6B')")


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
         cmd=interpreter.python_command("-c", _SENSEVOICE_PY)),

    dict(id="qwen3asr", group="转写引擎", name="Qwen3-ASR 0.6B + 强制对齐",
         purpose="更准的中文转写（会议纪要推荐，显存约 4GB）", size="~3.6 GB（两个模型）",
         target="ModelScope 缓存 Qwen/Qwen3-ASR-0.6B + Qwen/Qwen3-ForcedAligner-0.6B",
         source="script", ref="Qwen/Qwen3-ASR-0.6B",
         how="跑安装脚本（会先装 qwen-asr/transformers 依赖，再从 ModelScope 下模型）：",
         # 一键安装命令是平台专有的（Windows 是 .ps1，macOS 没有对应脚本 → 回落成 pip 说明），
         # 由接缝给（D12：业务代码不写平台命令）。回落那条也要**带解释器全路径** —— 它同样
         # 会被「复制命令」原样贴进终端（2026-09-25：裸 `python` 在一台嵌入包里直接跑不了）。
         cmd=echo_platform.model_install_command(
             "qwen3asr",
             _cmd_chain(interpreter.pip_install_command("qwen-asr", "transformers"),
                        interpreter.python_command("-c", _QWEN3ASR_PY)))),

    dict(id="sherpa", group="转写引擎", name="sherpa-onnx 流式 zipformer（中英）",
         purpose="流式转写；唤醒词功能也用它", size="~189 MB",
         target="models/sherpa-onnx-streaming/（encoder*/decoder*/joiner*.onnx + bpe.model + tokens.txt）",
         source="auto", ref="csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
         # ModelScope 上同一个模型在作者本人的账号下（pkufool = csukuangfj 的 MS 账号）。
         # 优先走它：公司网会用代理拦 hf-mirror 的证书，同事为此被迫设 HF_HUB_VERIFY=0
         # 才能下这个模型（2026-09-21 实测）。探过：仓库存在且 5 个必需文件齐全。
         ms_ref="pkufool/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20",
         allow=["encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.int8.onnx",
                "joiner-epoch-99-avg-1.int8.onnx", "bpe.model", "tokens.txt"],
         how="优先从 ModelScope 拉（sherpa-onnx 官方也在那边镜像），失败才回落 hf-mirror。"
             "只拉 int8 三件 + bpe.model + tokens.txt（约 189MB）直接落到该目录；"
             "代码用前缀匹配认文件，文件名不用改。"),
]

for _n in ("tiny", "base", "small", "medium", "large-v3"):
    CATALOG.append(dict(
        id=f"whisper-{_n}", group="转写引擎", name=f"Whisper {_n}",
        purpose=("faster-whisper 档位" + ("（默认 whisper 档）" if _n == "small" else "")
                 + "；中文可能输出繁体（训练语料偏繁体），需要简体请改用 SenseVoice / Qwen3-ASR"),
        size=WHISPER_SIZES[_n],
        target=f"models/faster-whisper/{_n}/（须含 model.bin）或 HF 缓存 models/hub/models--Systran--faster-whisper-{_n}/",
        source="auto", ref=f"Systran/faster-whisper-{_n}",
        # Systran 官方在 ModelScope 也有同名仓库（实测 base 档 200，且 model.bin /
        # config.json / tokenizer.json / vocabulary.txt 齐全）—— 同样优先走它，绕开证书问题。
        ms_ref=f"Systran/faster-whisper-{_n}",
        how="优先从 ModelScope 拉（Systran 官方镜像），失败才回落 HF 镜像（hf-mirror.com）；"
            "也可以从源机拷 models/faster-whisper/<档> 目录过来。",
        cmd=interpreter.python_command(
            "-c", "from faster_whisper import WhisperModel; WhisperModel('%s')" % _n)))

CATALOG += [
    dict(id="pyannote", group="可选功能", name="说话人分离（pyannote 三件套）",
         purpose="会议纪要区分说话人（设置里默认关闭）", size="模型与 PyTorch 等依赖需额外磁盘空间",
         target="models/pyannote/{pyannote-segmentation-3.0-local, pyannote-wespeaker-local, pyannote-plda-local}",
         # 2026-09-21：这三个仓库在 HF 上是 gated（要同意条款 + Token），但 ModelScope 上**同名
         # 且匿名可下**，所以改成和别的引擎一样一键下载。之前只能让用户自己去 HF 同意条款再拉，
         # 同事选了"说话人分离"就卡在这一步。
         source="modelscope", ref="pyannote/segmentation-3.0 + wespeaker-voxceleb-resnet34-LM",
         cmd=_pyannote_command(), cmd_label="复制下载命令",
         links=[{"label": "分段模型（HF 条款页）", "url": "https://huggingface.co/pyannote/segmentation-3.0"},
                {"label": "PLDA 模型（HF 条款页）", "url": "https://huggingface.co/pyannote/speaker-diarization-community-1"}],
         how="点下载即可（走 ModelScope，与前面几个引擎一样）。"
             "**请自行确认 pyannote 的使用条款**：官方在 HF 上要求先同意条件，"
             "ModelScope 的同名仓库是公开镜像，走它等于跳过那一步 —— 这是使用者的合规判断。"
             "权重不随 ECHO 交付包分发（只在你自己的机器上下载）。"
             "另外还需安装 pyannote.audio 4.x 与 speechbrain 依赖（复制下方命令可装）。"),

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
    "pyannote": ready_pyannote,
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
        return os.path.join(models_dir(), "sherpa-onnx-streaming")
    if i == "pyannote":
        return os.path.join(models_dir(), "pyannote")
    if i == "kws":
        return os.path.join(models_dir(), "wakeword", "kws-zh-en-3m")
    if i.startswith("whisper-"):
        # 三个落点里**第一个真实存在的**（顺序见 `_whisper_landings`）。不把三处相加：
        # 同一档可能在 HF 与 ModelScope 缓存里各有一份，相加会把"本机占用"翻倍。
        landings = _whisper_landings(i.split("-", 1)[1])
        for p in landings:
            if os.path.isdir(p):
                return p
        return landings[0]
    return ""


def _measure_paths(entry):
    """本地占用的统计范围。pyannote 只统计真正要用的三个 -local 目录，
    否则会把同目录下的 faster-whisper-* 冗余副本（5GB）也算进去。

    qwen3asr 这一档**由两个模型组成**（ASR + 强制对齐器），只量一个会把 3.6 GB 报成
    1.8 GB —— 面板上就成了"下了一半"，而实际是齐的（2026-09-26 实测：强制对齐器
    `Qwen--Qwen3-ForcedAligner-0.6B` 就在盘上，却一个字节都没被算进去）。
    """
    i = entry["id"]
    if i == "pyannote":
        p = os.path.join(models_dir(), "pyannote")
        return [os.path.join(p, n) for n in ("pyannote-segmentation-3.0-local",
                                             "pyannote-wespeaker-local",
                                             "pyannote-plda-local")]
    if i == "qwen3asr":
        return [_ms_dir("Qwen/Qwen3-ASR-0.6B"), _ms_dir("Qwen/Qwen3-ForcedAligner-0.6B")]
    path = _target_path(entry)
    return [path] if path else []


def _probe_for(entry):
    """该模型条目的就绪探测函数（whisper 各档走同一个带参数的探测）。"""
    probe = _PROBES.get(entry["id"])
    if probe is not None:
        return probe
    name = entry["id"].split("-", 1)[1] if entry["id"].startswith("whisper-") else ""
    return lambda: _ready_whisper(name)


def ready(model_id):
    """某个模型是否就绪：True/False/None（查不到这个 id 返回 None）。

    给 `app/components.py` 这类外部清单用 —— **"装没装"只有一个判据**（2026-09-19
    合并「模型/组件」两个页签时发现两套探测规则会分叉：组件说已装、模型说没装）。
    """
    for e in CATALOG:
        if e["id"] == model_id:
            try:
                return bool(_probe_for(e)())
            except Exception:
                return False
    return None


# ---------------------------------------------------------------- 「缺依赖」怎么修
#: 引擎的 pip 包没装时，文案里要出现"再点一次下载"这类**下一步**指引 —— 同事 2026-09-25
#: 的实测顺序是：点下载 → 一闪而过报缺依赖 → 复制安装命令装依赖 → 面板如实变成
#: 「模型文件还没下载」→ 用户以为卡死了（其实只差"再点一次下载"，界面一个字都没说）。
_NEXT_STEP_DOWNLOAD = "再回来点下载 → 依赖装好后，回到「设置 → 模型」再点一次「下载」。"


def dependency_problem(model_id) -> dict:
    """这个模型的**引擎 pip 依赖**在本机缺不缺；缺了就一次说清"怎么修"（否则空 dict）。

    返回 ``{"reason", "module", "installCommand", "nextStep", "message"}``：

      * ``reason``         —— 缺什么（给人看的一句话）；
      * ``installCommand`` —— 那条 pip 安装命令（带本机解释器全路径，可直接粘贴）；
      * ``nextStep``       —— 装完之后该点什么（面板可直接渲染）；
      * ``message``        —— 上面三块拼好的整段（`/api/models/download` 的 detail 用它）。

    判据与别处**同一份**：引擎模块名来自 `install_state.ENGINE_SPECS`（权威表），
    命令来自 `components.install_command_for_model()`（组件清单的 pkg 是唯一出处）。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return {}
    try:
        from app import install_state
        spec = install_state.engine_spec(mid)
    except Exception:
        spec = {}
    mod = str((spec or {}).get("module") or "")
    if not mod:
        return {}                       # 不是转写引擎（KWS / pyannote…）：这里不表态
    try:
        if _pkg_available(mod):
            return {}
    except Exception:
        return {}
    try:
        from app import components
        cmd = components.install_command_for_model(mid)
    except Exception:
        cmd = ""
    if not cmd:
        cmd = interpreter.pip_install_command(mod)
    label = str((spec or {}).get("label") or mid)
    reason = "缺 Python 依赖 %s（%s 的引擎包还没装）" % (mod, label)
    step1 = "第一步 · 先装依赖：把这条命令复制到终端执行 —— %s" % (cmd or ("pip install %s" % mod))
    return {"reason": reason, "module": mod, "installCommand": cmd,
            "nextStep": _NEXT_STEP_DOWNLOAD,
            "message": "%s\n%s\n%s" % (reason, step1, "第二步 · " + _NEXT_STEP_DOWNLOAD)}


#: 底层异常里的 "No module named 'x'" —— 下载客户端（modelscope / huggingface_hub）没装时
#: 报的就是它。用户看到的是**下载失败**，其实要装的是 pip 包。
_NO_MODULE_RE = re.compile(r"No module named '([\w.]+)'")


def _failure_hint(mid: str, raw) -> str:
    """下载失败时补一句"该怎么办"（认不出返回空串，不硬编）。"""
    prob = dependency_problem(mid)
    if prob:
        return prob["message"]
    hit = _NO_MODULE_RE.search(str(raw or ""))
    if not hit:
        return ""
    mod = hit.group(1).split(".")[0]
    try:
        from app import components
        cmd = components.install_command_for_model(mid)
    except Exception:
        cmd = ""
    if not cmd:
        cmd = interpreter.pip_install_command(mod)
    return ("缺 Python 依赖 %s（下载客户端没装，模型下不下来）\n"
            "第一步 · 先装依赖：把这条命令复制到终端执行 —— %s\n"
            "第二步 · %s" % (mod, cmd or ("pip install %s" % mod), _NEXT_STEP_DOWNLOAD))


def _next_step_fields(entry) -> dict:
    """未就绪时给面板的**「下一步点什么」**（③：见 `dependency_problem` 那段说明）。

    依赖缺 → 先装依赖（附那条命令）；依赖齐 → 就是"再点一次下载"。
    不提供自动下载的条目（source=copy / downloadable=False）不给下载建议。
    """
    prob = dependency_problem(entry["id"])
    if prob:
        return {"dependencyReady": False, "installCommand": prob["installCommand"],
                "nextStep": prob["nextStep"], "notReadyReason": prob["reason"]}
    downloadable = not (entry.get("downloadable") is False or entry.get("source") == "copy")
    return {"dependencyReady": True, "installCommand": "",
            "nextStep": ("再点一次「下载」，把模型文件拉下来（依赖已就绪）" if downloadable
                         else ""),
            "notReadyReason": "模型文件还没就位" if downloadable else ""}


def inventory():
    """返回清单 + 就绪状态 + 本地实际占用（MB）+ **使用情况**（2026-09-26 加）。

    未就绪的条目额外带 ``dependencyReady`` / ``installCommand`` / ``nextStep`` /
    ``notReadyReason``：面板要能直接说出"下一步点什么"，而不是只显示一个红徽标
    （同事 2026-09-25：装完依赖后界面停在"模型文件还没下载"，看着像卡死）。

    使用情况四个字段（`lastUsedAt` / `useCount` / `pinned` / `pinnedAt`）来自
    `app/model_usage.py` 的账本；`inUse` / `inUseReasons` 是"现在谁在用"的判据 ——
    面板的「本地能力」区据此决定**默认展开哪些**（配置为要用的默认展开，其余默认折叠），
    「清理」卡据此保护不该删的项。**读账本失败一律当"没用过"**，不让附加信息拖垮清单。
    """
    from app import model_usage
    try:
        usage = model_usage.usage_map()
    except Exception:
        usage = {}
    try:
        in_use = model_usage.in_use_ids()
    except Exception:
        in_use = {}
    items = []
    for e in CATALOG:
        probe = _probe_for(e)
        try:
            ready = bool(probe())
        except Exception:
            ready = False
        local = sum(_dir_mb(p) for p in _measure_paths(e) if os.path.isdir(p))
        u = usage.get(e["id"]) or {}
        reasons = list(in_use.get(e["id"]) or [])
        row = dict(e, ready=ready, local_mb=local,
                   lastUsedAt=str(u.get("lastUsedAt") or ""),
                   useCount=int(u.get("useCount") or 0),
                   pinned=bool(u.get("pinned")),
                   pinnedAt=str(u.get("pinnedAt") or ""),
                   inUse=bool(reasons), inUseReasons=reasons)
        if not ready:
            try:
                row.update(_next_step_fields(e))
            except Exception:
                pass
        items.append(row)
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
    """这一档的下载**实际会落**的目录（进度按它们的体积增长算，不依赖库的进度回调）。

    ⚠ 必须与"`_download_worker` 真的把它下到哪"一致。whisper 以前只盯着
    `models/hub`，而它那条路走的是 `_snapshot(ms_id=ms_ref, …)` —— **ModelScope 优先**、
    落 `~/.cache/modelscope/models`。于是真下了 72 MB，进度一路 0，完成时还报
    `downloaded_mb: 0`（message 却是"下载完成"，自相矛盾，2026-09-26 实测）。
    """
    if mid == "sensevoice":
        return [os.path.join(MS_CACHE, "iic--SenseVoiceSmall"),
                os.path.join(MS_CACHE, "iic--speech_fsmn_vad_zh-cn-16k-common-pytorch")]
    if mid == "qwen3asr":
        return [os.path.join(MS_CACHE, "Qwen--Qwen3-ASR-0.6B"),
                os.path.join(MS_CACHE, "Qwen--Qwen3-ForcedAligner-0.6B")]
    if mid.startswith("whisper-"):
        # 三个落点都要盯：ModelScope 缓存（面板下载的落点）与 HF 缓存都在其中。
        return _whisper_landings(mid.split("-", 1)[1])
    if mid == "sherpa":
        return [os.path.join(models_dir(), "sherpa-onnx-streaming")]
    if mid == "pyannote":
        # 与面板的「本机占用」**同一个范围**（只算真正要用的三个 -local 目录）：
        # 那一层目录里还躺着别的模型的冗余副本（本机实测旁边就有 189 MB 的 sherpa
        # 副本），整个目录算进来会把 31 MB 的模型报成 221 MB —— 那同样是"回报不实"。
        return _measure_paths(_by_id("pyannote"))
    return []


def _downloaded_mb(mid):
    """这一档**已经落到盘上**的 MB；**算不出来就返回 `None`（"未知"），不许拿 0 冒充**。

    为什么要 `None`：`0` 是一个断言（"盘上就是 0 字节"），而"这个落点我压根没找到"
    是完全另一回事。从前两者混在一起，于是"真下了 72 MB"与"一个字节都没下"在
    `/api/models` 上是同一个数字 —— 面板据此画进度条，用户看到的是"卡在 0%"。

    判据：**只要有一个落点目录存在**就算"能从盘上算出来"（哪怕此刻真是 0 字节，
    那也是真数）；一个都不存在 = 落点还没有 → `None`。
    """
    paths = [p for p in _watch_paths(mid) if os.path.isdir(p)]
    if not paths:
        return None
    return sum(_dir_mb(p) for p in paths)


def model_paths(mid):
    """这个模型**可能落地的全部目录**（下载落点 ∪ 占用统计口径 ∪ 就绪检测目录）。

    公开函数：清理（`app/model_cleanup.py`）要用它拿到"这一项到底占了哪几个位置"——
    `models\\` 与本机 ModelScope 缓存**两处都要覆盖**，只删一处会留下半份权重，
    下次加载照样从残留里读、看起来"删了没效果"。

    为什么是**并集**（2026-09-26 实测发现的两处漏网）：

      * `_watch_paths()` 管的是"**下载**会下到哪"（whisper 三个落点、sensevoice 的两个
        ModelScope 目录…），但它**没有** kws 这一档；
      * `_measure_paths()` / `_target_path()` 管的是"**面板量占用**时看哪"（kws 的
        `models/wakeword/kws-zh-en-3m`、以及拷进来的 `models/sensevoice`、`models/pyannote/*`），
        但它对 whisper 只取**第一个存在**的落点。

    任何一边单独用都会漏：漏了 kws 就是"面板说有 39 MB、清理说本机没有"。
    所以并起来、去重、保持顺序（靠前的优先）—— 判据只有一个：**这一项真的占了哪些目录**。
    """
    entry = _by_id(mid)
    paths = list(_watch_paths(mid))
    if entry:
        paths += list(_measure_paths(entry))
    seen, out = set(), []
    for p in paths:
        if not p:
            continue
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


#: pyannote 三件套：HF 上**同名仓库是 gated 的**（要同意条款 + Token），而 ModelScope 上
#: 同名仓库**匿名可下**（2026-09-21 实测：三个 repo 都 200，且 plda/plda.npz、
#: plda/xvec_transform.npz、两个 pytorch_model.bin 都在）。只取 ECHO 真正加载的那几个文件，
#: 放进 `app/audio/diarize.py` 期望的目录结构。
#: ⚠ 权重**仍然不随包分发**（components.py 的 never_ship=True 不动）—— 这是"在用户机器上下载"。
PYANNOTE_ASSETS = (
    ("pyannote/segmentation-3.0", "pyannote-segmentation-3.0-local", ["pytorch_model.bin"]),
    ("pyannote/wespeaker-voxceleb-resnet34-LM", "pyannote-wespeaker-local", ["pytorch_model.bin"]),
    ("pyannote/speaker-diarization-community-1", "pyannote-plda-local",
     ["plda/plda.npz", "plda/xvec_transform.npz"]),
)


def download_pyannote() -> str:
    """拉 pyannote 三件套（走 ModelScope）。返回实际用的源名。

    历史上这一步只能由用户自己去 HF 同意条款、`hf auth login` 再下（gated），
    而 ModelScope 上有同名仓库且匿名可下 —— 于是它和别的引擎一样能自动装了。
    """
    root = os.path.join(models_dir(), "pyannote")
    for repo, folder, files in PYANNOTE_ASSETS:
        _snapshot(ms_id=repo, local_dir=os.path.join(root, folder), allow=files)
    return "modelscope"


#: 失败原因的关键词 → 人话（A4：同事被公司代理拦证书，界面上只看到裸 SSLError，不知该干什么）
_ERROR_HINTS = (
    (("certificate verify failed", "certificate_verify_failed", "sslerror", "self signed",
      "self-signed", "unable to get local issuer", "certverifyfailed"),
     "证书校验失败（公司代理常见的中间人证书问题）—— 不要关校验，改用 ModelScope 源或换网络"),
    (("proxy", "407", "tunnel connection failed", "connect tunnel"),
     "被代理拦下（需要认证或代理不通）—— 检查系统代理设置，或把模型源地址加进白名单"),
    (("403", "forbidden", "gated"),
     "源站拒绝（403 / 需要同意条款）—— 到该模型页面同意条款，或改用 ModelScope 镜像"),
    (("401", "unauthorized"),
     "需要登录（401）—— 填 HF Token（设置 → 模型）或改用 ModelScope 源"),
    (("getaddrinfo", "name or service not known", "nodename nor servname",
      "temporary failure in name resolution", "max retries exceeded", "connection refused",
      "connection reset", "timed out", "timeout"),
     "网络不通或超时 —— 稍后重试；公司网里多半要走代理"),
)


def explain_download_error(raw):
    """把底层下载异常翻成一句**能照着做**的人话；认不出返回空串（不硬编）。"""
    low = str(raw or "").lower()
    if not low:
        return ""
    for keys, tip in _ERROR_HINTS:
        if any(k in low for k in keys):
            return tip
    return ""

def _snapshot(ms_id: str = "", hf_id: str = "", local_dir=None, allow=None) -> str:
    """按「先 ModelScope、失败再 HF」的顺序拉一个模型仓库，返回实际用的源。

    为什么优先 ModelScope（2026-09-21 实测反馈）：公司网用代理拦 hf-mirror 的证书，
    同事被逼到设 `HF_HUB_VERIFY=0` 才下得了 sherpa —— 那是**关掉 TLS 校验**，不该是交付路径。
    而 sherpa 与 whisper 在 ModelScope 上都有官方镜像（`pkufool/…` / `Systran/…`，实测文件齐全），
    走它根本不必碰证书。HF 仍然作为回落：万一 ModelScope 上没有或临时挂了。
    """
    errs = []
    if ms_id:
        try:
            from modelscope import snapshot_download as ms_dl
            ms_dl(ms_id, local_dir=local_dir, allow_patterns=allow)
            return "modelscope"
        except Exception as e:                       # noqa: BLE001 - 要把原因带上去
            errs.append("ModelScope(%s): %s" % (ms_id, e))
    if hf_id:
        try:
            from huggingface_hub import snapshot_download as hf_dl
            hf_dl(hf_id, local_dir=local_dir, allow_patterns=allow)
            return "huggingface"
        except Exception as e:                       # noqa: BLE001
            errs.append("HF(%s): %s" % (hf_id, e))
    detail = "；".join(errs) or "没有可用的下载源"
    tip = explain_download_error(detail)
    # 两个源都失败时附一句"该怎么办"（A4）—— 否则用户只看到两段英文异常
    raise RuntimeError(("%s\n怎么办：%s" % (detail, tip)) if tip else detail)


def _download_worker(entry):
    mid = entry["id"]
    expected = _EXPECTED_MB.get(mid) or 0
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            mb = _downloaded_mb(mid)
            # 算不出字节（落点还没出现）时百分比**也**是"未知"：拿 0 顶上等于说
            # "下载进度是 0%"，而那时候我们其实什么都不知道。
            pct = min(99, int(mb * 100 / expected)) if (expected and mb is not None) else None
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
        source_used = ""
        if mid == "sensevoice":
            source_used = _snapshot(ms_id="iic/SenseVoiceSmall")
            try:
                _snapshot(ms_id="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch")
            except Exception as e:
                print(f"[modelinfo] VAD 模型下载失败（加载时会再试）: {e}")
        elif mid == "qwen3asr":
            source_used = _snapshot(ms_id="Qwen/Qwen3-ASR-0.6B")
            _snapshot(ms_id="Qwen/Qwen3-ForcedAligner-0.6B")
        elif mid.startswith("whisper-"):
            source_used = _snapshot(ms_id=entry.get("ms_ref") or "", hf_id=entry["ref"])
        elif mid == "sherpa":
            # 只拉代码认的那几个文件名，直接落到目标目录（local_dir），省掉 400MB 的 fp32 与测试音频
            source_used = _snapshot(ms_id=entry.get("ms_ref") or "", hf_id=entry["ref"],
                                    local_dir=os.path.join(models_dir(), "sherpa-onnx-streaming"),
                                    allow=entry.get("allow") or None)
        elif mid == "pyannote":
            source_used = download_pyannote()
        # 完成时**再量一次真实字节**（不是拿 watch 线程最后一拍）：watch 每 2 秒一拍，
        # 最后那点尾巴可能落在两次采样之间。量不出来就报 None（"大小未知"）。
        final_mb = _downloaded_mb(mid)
        _JOBS[mid].update(status="done", percent=100,
                          message=("下载完成（来自 %s）" % source_used) if source_used else "下载完成",
                          source=source_used,
                          done_at=time.strftime("%H:%M:%S"),
                          downloaded_mb=final_mb)
        print(f"[modelinfo] {mid} 下载完成，"
              f"{'大小未知（落点目录没找到）' if final_mb is None else '%d MB' % final_mb}"
              f"（源：{source_used or '本地脚本'}）")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        hint = _failure_hint(mid, msg)
        if hint:
            # 只报两句英文异常，用户不知道下一步干什么（A4）—— 缺依赖时把"装什么 + 装完
            # 再点一次下载"直接附在后面（同事 2026-09-25 实测那条路）。
            msg = "%s\n怎么办：%s" % (msg, hint)
        # 失败也要如实报**已经落了多少**：`0` 会让人以为一个字节都没下来（其实可能
        # 下了 700 MB 才断），而 None 才是"不知道"。
        _JOBS[mid].update(status="failed", message=msg,
                          done_at=time.strftime("%H:%M:%S"),
                          downloaded_mb=_downloaded_mb(mid))
        print(f"[modelinfo] {mid} 下载失败: {msg}")
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

    **缺依赖时先在入口拦下**（同事 2026-09-25 实测）：不拦的话后台线程会一闪而过地失败
    （`No module named 'modelscope'`），用户只看到"缺依赖"三个字，既不知道该装什么，
    也不知道装完要**回来再点一次下载**。拦下时返回的那句话里三件都有（原因 + 安装命令 +
    下一步），面板的 `/api/models/download` 还会把它们拆成字段（见 `app/api.py`）。
    """
    entry = _by_id(mid)
    if not entry:
        return False, f"未知模型: {mid}"
    if entry.get("downloadable") is False:
        return False, "请复制下载命令，在终端自行执行。"
    if entry.get("source") == "copy":
        return False, "该模型没有稳定的公开下载源，请从源机拷贝（见说明）"
    try:
        prob = dependency_problem(mid)
    except Exception:
        prob = {}
    if prob:
        return False, prob["message"]
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
        # `downloaded_mb` 起手是 **None（"未知"）而不是 0**：这一刻我们还没量过盘，
        # 0 是一个"盘上就是 0 字节"的断言（第一拍量的结果可能是 72 MB）。
        _JOBS[mid] = {"id": mid, "status": "running", "percent": None, "downloaded_mb": None,
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


#: 下载任务的状态词（归一后）。生产端只写 running/done/failed，
#: 但调用方历史上有人判断 "error" —— 见 job_state() 的说明。
JOB_STATES = ("queued", "running", "done", "failed")


def job_state(job) -> str:
    """把一条下载任务归一成 ``queued | running | done | failed``。

    为什么要有它（2026-09-21 同事实测）：``_download_worker`` 失败时写的是 **"failed"**，
    而向导的 ``execution_state`` 判断的是 **"error"** —— 两边词表不一致，于是**下载失败
    在面板上显示成"排队中"，永远不动**（用户看到 qwen3asr 卡在"正在准备中/排队中"）。
    技能里的两份等待逻辑（.ps1 / .sh）也各自踩了同一个坑。

    所以：**所有调用方都走这个函数**，别再自己比对字面量。
    """
    s = str((job or {}).get("status") or "").strip().lower()
    if s in ("failed", "error", "fail"):
        return "failed"
    if s in ("running", "downloading"):
        return "running"
    if s in ("done", "ok", "ready"):
        return "done"
    return "queued"

