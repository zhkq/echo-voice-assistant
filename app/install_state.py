# -*- coding: utf-8 -*-
"""install_state.py — 「这台机器装成什么样了」的唯一真值。

为什么需要它（2026-09-21 实测反馈）
----------------------------------
安装现在由**技能**在用户自己的 agent 里完成（见 `docs/安装-技能优先.md`）。而面板原来只问
一个问题：`data/installed-components.json` 在不在 —— 在就进仪表盘，不在就**强制进向导**
（`web/app.js` 的 `bootView()`）。那个文件只有**向导末页**才写，技能安装从不写，于是
"技能装完，打开 ECHO 还是进向导"。

这里把安装情况变成一等概念：

* :func:`save_report` / :func:`load_report` —— 技能装完登记的真值（`data/install-report.json`）。
* :func:`declared` —— 登记过没有（首装判据：新报告 **或** 老的 installed-components.json）。
* :func:`missing` —— **从真值推**"还缺什么"（模型 ready / pip 模块 import / node / harness online）。
  技能拿它做自检与最终汇报，面板拿它做横幅与「能力」页 —— 一套判断，两处复用。

刻意不碰的东西：**不写设置值**（报告是明文，可能被拷来拷去）、不启动/停止任何进程。
"""
import json
import os
import time

#: 技能登记的文件（数据根下）
REPORT_FILE = "install-report.json"
#: 报告格式版本；将来改结构时靠它迁移
REPORT_SCHEMA = "echo-install-report/1"

#: 引擎 → （pip 模块名 / 模型 id / 写进 sttModel 的值 / 给人看的名字）
#: 这是**权威表**：`echo-install-components.ps1`、`echo-install-components.sh` 各有一份镜像，
#: 由 `tests/test_install_entry.py` 断言三者一致（含与 app/audio/stt.py 的合法性对齐）。
ENGINE_SPECS = {
    "sherpa":           {"module": "sherpa_onnx",     "model": "sherpa",           "stt": "sherpa",   "label": "sherpa-onnx 流式转写"},
    "whisper-tiny":     {"module": "faster_whisper",  "model": "whisper-tiny",     "stt": "tiny",     "label": "Whisper tiny"},
    "whisper-base":     {"module": "faster_whisper",  "model": "whisper-base",     "stt": "base",     "label": "Whisper base"},
    "whisper-small":    {"module": "faster_whisper",  "model": "whisper-small",    "stt": "small",    "label": "Whisper small"},
    "whisper-medium":   {"module": "faster_whisper",  "model": "whisper-medium",   "stt": "medium",   "label": "Whisper medium"},
    "whisper-large-v3": {"module": "faster_whisper",  "model": "whisper-large-v3", "stt": "large-v3", "label": "Whisper large-v3"},
    "sensevoice":       {"module": "funasr",          "model": "sensevoice",       "stt": "sensevoice", "label": "SenseVoice 中文短句"},
    # module 必须是 qwen_asr（真正的引擎包），不能写 transformers：装了 transformers 5.x
    # 而没装 qwen-asr 时，写 transformers 会让这里判"已就绪"，而会议转写会报
    # "qwen-asr package is required for Qwen3-ASR"（2026-09-23 实测，发布版 2.0.0 就有）。
    "qwen3asr":         {"module": "qwen_asr",        "model": "qwen3asr",         "stt": "qwen3asr", "label": "Qwen3-ASR 方言/口音"},
}

#: sttModel 的值 → 引擎名（把"用户选的设置"翻回"要装什么"）
STT_TO_ENGINE = {spec["stt"]: name for name, spec in ENGINE_SPECS.items()}


def _data_root() -> str:
    try:
        from app import paths
        return paths.data_root()
    except Exception:
        return ""


def report_path(path: str = "") -> str:
    return path or os.path.join(_data_root(), REPORT_FILE)


def load_report(path: str = "") -> dict:
    """读安装报告；没有/坏了都返回空 dict（**不抛** —— 面板不能因为一个文件读不了就白屏）。"""
    try:
        with open(report_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_report(payload: dict, path: str = "") -> dict:
    """写安装报告（原子替换）。返回落盘的完整内容。"""
    target = report_path(path)
    data = dict(payload or {})
    data["schema"] = REPORT_SCHEMA
    data["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return data


def _wizard_file_exists() -> bool:
    """老安装（向导装的）靠 installed-components.json 登记 —— 它也算"登记过"。"""
    try:
        from app import wizard
        return os.path.isfile(wizard.installed_path())
    except Exception:
        return False


def declared(path: str = "") -> bool:
    """安装登记过没有。**认两处**：新的 install-report.json 或老的 installed-components.json。

    新老都认是有意的：老机器不去补文件，装过一次就不该再被当成首装。
    """
    try:
        if os.path.isfile(report_path(path)):
            return True
    except Exception:
        pass
    return _wizard_file_exists()


def _module_ok(name: str) -> bool:
    """pip 依赖装没装：**以 import 为准**（不是"pip 说成功"，2026-09-21 踩过）。"""
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _model_ready(model_id: str):
    try:
        from app import modelinfo
        return modelinfo.ready(model_id)
    except Exception:
        return None


def engine_spec(model_id: str) -> dict:
    """按**模型 id** 反查引擎规格（返回副本，取不到给空 dict）。

    为什么需要它：`app/components.py` 的 stt 组件只写 `model_id`（判"装没装"问
    `modelinfo`），可"模型下好了"**不等于能用** —— 引擎的 pip 包是另一件事。
    两张表要能对起来（`ENGINE_SPECS` 就是那张权威表），所以从这里反查，不在
    components 里再抄一份。
    """
    mid = str(model_id or "")
    for spec in ENGINE_SPECS.values():
        if spec.get("model") == mid:
            return dict(spec)
    return {}


def engine_problem(choice: str) -> str:
    """这个 ``sttModel`` / ``meetingSttModel`` 取值**现在能不能真用**。

    返回 ``""`` = 可用；否则一句给人看的原因（缺 pip 包 / 缺模型）。

    为什么要有它（2026-09-23 实测事故）：`missing()` 只按**安装报告里登记过的引擎**
    说话，而用户在面板上把 `sttModel` 改成没装过的引擎时那份报告不会变 —— 于是
    "选了 sherpa、但稳定版 runtime-core 里没有 sherpa_onnx"一路无人提醒，
    直到说第一句命令时才静默转写出空串（面板不报、DSH 也看不到请求）。
    """
    spec = ENGINE_SPECS.get(STT_TO_ENGINE.get(str(choice or "").strip(), "")) or {}
    if not spec:
        # 认不出的取值交给 stt.resolve_engine 的既有回退（它有告警），这里不表态
        return ""
    mod = str(spec.get("module") or "")
    model = str(spec.get("model") or "")
    try:
        if mod and not _module_ok(mod):
            return ("缺 Python 依赖 %s —— 转写时会直接报错；"
                    "面板「能力」页对应组件那一行的「下载命令」可复制安装" % mod)
        if model and _model_ready(model) is False:
            return "模型文件还没就位（%s）—— 面板「能力」页那一行可直接下载" % model
    except Exception as exc:
        # 诊断函数不许把调用方（保存设置）带崩：说不清就说说不清
        return "无法判断这个引擎能不能用（%s: %s）" % (type(exc).__name__, exc)
    return ""


def _node_ok() -> bool:
    try:
        import shutil
        return bool(shutil.which("npx") or shutil.which("node"))
    except Exception:
        return False


def _harness_online() -> bool:
    try:
        from app import harness_proc
        if not harness_proc.requested():
            return False
        return bool(harness_proc.online(timeout=0.6))
    except Exception:
        return False


def _dsh_online() -> bool:
    """选中的是 DSH Desktop 时，问**它自己**在不在（默认 43120）。"""
    try:
        from app import manager
        return bool(manager.dsh_ready())
    except Exception:
        return False


def _wanted_from_settings() -> dict:
    """没有报告时，从**设置**推"用户想要什么"（老安装/向导安装走这条路）。"""
    out = {"engines": [], "wake": False, "diarize": False, "agent": ""}
    try:
        from app.config import settings
        for key in ("sttModel", "meetingSttModel"):
            v = str(settings.get(key, "") or "").strip()
            eng = STT_TO_ENGINE.get(v)
            if eng and eng not in out["engines"]:
                out["engines"].append(eng)
        out["wake"] = bool(settings.get("wakeEnabled", False))
        out["agent"] = str(settings.get("agentBackend", "") or "").strip()
    except Exception:
        pass
    return out


def wanted(report: dict = None) -> dict:
    """这次要的是什么：优先用技能登记的报告，没有就退回设置。"""
    report = report if report is not None else load_report()
    if report:
        return {
            "engines": [e for e in (report.get("engines") or []) if e in ENGINE_SPECS],
            "wake": bool(report.get("wake")),
            "diarize": bool(report.get("diarize")),
            "agent": str(report.get("agent") or ""),
        }
    return _wanted_from_settings()


def _is_windows() -> bool:
    """当前是不是 Windows —— **走平台接缝**，不在 app/ 里直接写 os.name 分支
    （audit-paths 会拦：平台差异只能住在 app/platform/ 下，2026-09-21 实测踩到）。"""
    try:
        from app import platform as echo_platform
        return echo_platform.current() == "win32"
    except Exception:
        return False


def missing(report: dict = None) -> list:
    """**还缺什么** —— 逐项从真值推，返回给人看的三元组列表。

    每一项：``{"feature", "reason", "fix"}``（与向导 `build_plan()` 的 missing 同形，
    面板/技能可以直接复用同一套渲染）。
    """
    want = wanted(report)
    out = []

    for engine in want["engines"]:
        spec = ENGINE_SPECS.get(engine) or {}
        if not _module_ok(spec.get("module", "")):
            fix = "用 echo-install 技能重跑，或 pip install %s" % spec.get("module", "?")
            if _is_windows():
                # Windows 上"包在、导不进来"最常见的原因是缺 VC++ 运行库（原生扩展都要它）——
                # 面板顺手把这句话给出来，用户不用去猜 "DLL load failed" 是什么意思。
                fix += "；若报 DLL load failed，先装 Microsoft Visual C++ 2015-2022 运行库（x64）" \
                       "：https://aka.ms/vs/17/release/vc_redist.x64.exe"
            out.append({
                "feature": spec.get("label") or engine,
                "reason": "缺 Python 依赖（%s）—— 转写时会直接报错" % spec.get("module", "?"),
                "fix": fix,
            })
            continue
        if _model_ready(spec.get("model", "")) is False:
            out.append({
                "feature": spec.get("label") or engine,
                "reason": "模型文件还没下载（依赖已就绪）",
                "fix": "面板 → 能力 → 下载 %s；或让助手跑 echo-install 技能" % (spec.get("model") or engine),
            })

    if want["wake"] and _model_ready("kws") is False:
        out.append({"feature": "唤醒词（喊一声就开始）",
                    "reason": "KWS 权重还没就位", "fix": "面板 → 能力 → 下载唤醒词"})

    if want["diarize"]:
        try:
            from app import modelinfo
            if modelinfo.ready_pyannote() is False:
                out.append({"feature": "说话人分离（会议里区分谁在说）",
                            "reason": "三件套模型还没下载（官方在 HF 上 gated，ModelScope 有同名镜像）",
                            "fix": "面板 → 组件 → 直接点下载（走 ModelScope）；或让助手跑 echo-install 技能"})
        except Exception:
            pass

    if want["agent"] in ("harness", "dsh"):
        # **按选中的那个适配器**判断在不在线。
        # 原来对 harness 与 dsh 都去问标准版 harness —— 而 `_harness_online()` 在
        # `harness_proc.requested()` 为假（= agentBackend 不是 harness）时**必然返回 False**，
        # 于是选了 DSH Desktop 的机器永远被告知"harness 没在运行、还没装完"
        # （2026-09-23 迁移实测撞到：Desktop 正在跑、面板仍列这一条）。
        # 与同事报过的「拿另一个适配器的状态判断」是同一个病。
        if want["agent"] == "dsh":
            if not _dsh_online():
                out.append({"feature": "智能体（会议纪要 / 归档 / 语音指令）",
                            "reason": "DSH Desktop 没在运行（你选的是桌面版）",
                            "fix": "启动 DSH Desktop（它是宿主应用，ECHO 只连不拉）；"
                                   "或在 设置 → 智能体 里改用「标准版 harness」（只要本机有 Node.js）"})
        elif not _node_ok():
            out.append({"feature": "智能体（会议纪要 / 归档 / 语音指令）",
                        "reason": "没找到 Node.js（harness 靠 npx 起）",
                        "fix": "装 Node.js 后重跑 echo-install 技能（它会把标准版永久装到 <安装目录>/harness/dsh 并写好启动命令）"})
        elif not _harness_online():
            out.append({"feature": "智能体（会议纪要 / 归档 / 语音指令）",
                        "reason": "harness 没在运行（已选但没起来）",
                        "fix": "面板 → 服务 → 启动 harness；安装技能会把它永久装到 <安装目录>/harness/dsh（冷启动约 10 秒），起不来就看 data/logs/harness.log"})
    return out


def state(path: str = "") -> dict:
    """给面板与技能的一份完整状态。"""
    report = load_report(path)
    miss = missing(report)
    engines = wanted(report)["engines"]
    return {
        "declared": declared(path),
        "report": report,
        "wanted": wanted(report),
        "engines": [{"id": e, "label": (ENGINE_SPECS.get(e) or {}).get("label", e),
                     "moduleOk": _module_ok((ENGINE_SPECS.get(e) or {}).get("module", "")),
                     "modelReady": _model_ready((ENGINE_SPECS.get(e) or {}).get("model", ""))}
                    for e in engines],
        "missing": miss,
        "ready": not miss,
        "summary": ("这台机器已经装好" if not miss
                    else "还有 %d 项没就绪" % len(miss)),
    }
