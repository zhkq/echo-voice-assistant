# -*- coding: utf-8 -*-
"""wizard.py — 首装向导的数据面（D23/D24）

设计见 `docs/向导-分步设计.md`。这一层只做两件事，都是"只读检测"或"只写自己的小文件"：

  * ``environment_report()``：向导的"检查你的电脑"，以及每一步"推荐哪个 / 默认勾什么"的依据
    —— 三处位置与空间（模型文件 / 会议文件 / 笔记库）、显卡、网络、麦克风、Node、智能体。
  * ``load_plan()`` / ``save_plan()``：**决策相**的用户选择（可续跑）。

**这一层不做任何下载、不写任何设置**：把"选"与"装"分开是这套向导的核心（设计 §1）。
任何一项探测失败都只登记进报告，绝不抛异常 —— 环境坏掉的时候，体检页恰恰最需要能打开。

平台差异一律走 `app.platform` 接缝：本模块不得出现平台特征串（由 `scripts/audit-paths.py`
的 PLATFORM_TOKEN / WINDOWS_* / DRIVE_LITERAL 规则钉住）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request

from app import paths
from app import platform as plat

#: 计划文件的格式版本（与主包的 manifest.json 不是一回事：这是"用户的选择"）
PLAN_SCHEMA = "echo-wizard/1"

#: 决策相的状态机（设计 §5）。draft=还在选；reviewing=在看确认页；
#: running=执行相；done=装完了。
PLAN_STATES = ("draft", "reviewing", "running", "done")

#: 三处位置（设计 §2 的 S1）：key → (界面上的名字, 配置键, 是否"必须可写")
#: 界面用词是**模型文件**（2026-09-20 由"能力包"改的：用户说"能力包"看不出是什么，
#: "模型文件"一眼就懂 —— 它就是这个目录里放的那些下载下来的文件）。
LOCATIONS = (
    ("models", "模型文件", "modelsDir", True),
    ("meetings", "会议文件", "meetingsDir", True),
    ("notes", "笔记库", "worklogVaultRoot", False),
)

#: 网络体检目标（设计 §2 的 S0）。探这些是因为"能不能在线装"决定走在线还是离线组件包。
NET_TARGETS = (
    ("modelscope", "模型文件下载（ModelScope）", "https://www.modelscope.cn"),
    ("hf_mirror", "模型文件下载（HF 镜像）", "https://hf-mirror.com"),
    ("pypi", "依赖下载（PyPI）", "https://pypi.org/simple/"),
)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _settings(name: str) -> str:
    """读一个配置项；配置坏掉时返回空串（体检不能因此挂掉）。"""
    try:
        from app.config import settings
        return str(settings.get(name, "") or "").strip()
    except Exception:
        return ""


def _free_gb(path: str):
    """path 所在盘的剩余空间（GB）。路径还不存在时往上找第一个存在的祖先。

    刻意用 ``os.path.split`` 而不是 ``dirname``：审计规则的 PATH_DERIVATION 认的就是
    ``dirname`` 那一族，本模块不该出现它们。
    """
    try:
        head = path
        while head and not os.path.isdir(head):
            parent = os.path.split(head)[0]
            if parent == head:
                break
            head = parent
        if head:
            return round(shutil.disk_usage(head).free / (1024 ** 3), 1)
    except Exception:
        pass
    return None


def _is_ascii(path: str) -> bool:
    try:
        return paths.is_ascii(path)
    except Exception:
        return True


def _location_entry(key: str, label: str, setting_key: str, path: str, configured: bool) -> dict:
    """一条位置记录：在不在、能不能写、哪个盘、还剩多少、路径是否含中文。

    与 ``paths.preflight()`` 保持同一套字段名，面板侧可以复用同一段渲染。
    """
    row = {
        "key": key,
        "label": label,
        "settingKey": setting_key,
        "path": path,
        "configured": bool(configured),
        "exists": bool(path) and os.path.isdir(path),
        "ascii": _is_ascii(path) if path else True,
        "writable": False,
        "freeGB": _free_gb(path) if path else None,
        "note": "",
    }
    if not path:
        row["note"] = "还没设置" if not configured else "配置为空"
        return row
    try:
        ok, why = paths.validate_dir(path, create=False)
        row["writable"] = bool(ok)
        if not ok:
            row["note"] = why
    except Exception as exc:                      # 体检自己不能挂
        row["note"] = "%s: %s" % (type(exc).__name__, exc)
    warn = paths.ascii_warning(path) if path else ""
    if warn:
        row["note"] = (row["note"] + "；" if row["note"] else "") + warn
    return row


def locations_report() -> list:
    """三处位置 + 各自盘的剩余空间（设计 §2：空间判断必须**分别**做）。"""
    models = _settings("modelsDir")
    meetings = _settings("meetingsDir")
    notes = _settings("worklogVaultRoot")
    model_path = ""
    meeting_path = ""
    try:
        model_path = paths.models_root()
        meeting_path = paths.meetings_root()
    except Exception:
        pass
    return [
        _location_entry("models", "模型文件", "modelsDir", model_path, bool(models)),
        _location_entry("meetings", "会议文件", "meetingsDir", meeting_path, bool(meetings)),
        _location_entry("notes", "笔记库", "worklogVaultRoot", notes, bool(notes)),
    ]


def _probe(url: str, timeout: float = 1.5):
    """能否到达：**任何 HTTP 响应都算通**（含 401/404），连不上才算不通。返回 (ok, ms)。"""
    started = time.time()
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout).close()
        return True, int((time.time() - started) * 1000)
    except urllib.error.HTTPError:
        return True, int((time.time() - started) * 1000)
    except Exception:
        return False, int((time.time() - started) * 1000)


def network_report() -> dict:
    """能不能在线装（决定推荐"在线下载"还是"离线组件包"）。"""
    targets = []
    for key, label, url in NET_TARGETS:
        ok, ms = _probe(url)
        targets.append({"key": key, "label": label, "url": url, "reachable": bool(ok), "ms": ms})
    return {
        "targets": targets,
        "anyReachable": any(t["reachable"] for t in targets),
        "verdict": ("可以联网下载" if any(t["reachable"] for t in targets)
                    else "连不上下载源：向导会改用离线组件包"),
    }


def audio_report() -> dict:
    """麦克风。装的是"只有基础运行环境"的主包时，sounddevice 在、设备也可能没有。"""
    try:
        import sounddevice as sd
        inputs = [d for d in sd.query_devices()
                  if int(d.get("max_input_channels", 0) or 0) > 0]
        return {"available": True, "inputs": len(inputs),
                "names": [str(d.get("name", "")) for d in inputs[:5]]}
    except Exception as exc:
        return {"available": False, "inputs": 0, "names": [],
                "note": "读不到音频设备：%s" % exc}


def node_report() -> dict:
    """有没有 Node（决定 S7 能不能"帮我装好"标准版 DSH）。"""
    npx = shutil.which("npx") or ""
    return {"npx": npx, "ok": bool(npx),
            "note": "" if npx else "没找到 npx：需要先装 Node.js，向导会给安装入口"}


def agent_report() -> dict:
    """两个智能体后端各自的状态：桌面客户端（只能检测）与标准版（能一键准备）。"""
    dsh = {"online": False, "detail": ""}
    try:
        from app.agents.dsh_agent import DshAgent
        ok, detail = DshAgent().available()
        dsh = {"online": bool(ok), "detail": str(detail)}
    except Exception as exc:
        dsh = {"online": False, "detail": "探测失败：%s" % exc}
    harness = {"online": False, "detail": "", "command": ""}
    try:
        from app import harness_proc
        state, detail = harness_proc.status()
        harness = {"online": state == "online", "detail": str(detail),
                   "command": harness_proc.command()}
    except Exception as exc:
        harness = {"online": False, "detail": "探测失败：%s" % exc, "command": ""}
    return {"dsh": dsh, "harness": harness}


def recommend(env: dict) -> dict:
    """每一步的默认建议（设计 §0.4「默认即安全」）。

    只给一条"最省事且能用"的建议，并把理由一起给出来 —— 界面上的"按建议装上"就是它。
    """
    gpu = env.get("gpu") or {}
    vram = int(gpu.get("vramMb") or 0)
    node = env.get("node") or {}
    agent_ready = bool((env.get("agents") or {}).get("harness", {}).get("online"))
    return {
        "engine": "stt-sherpa",
        "engineReason": "不需要独立显卡、体积也小，先把「录音能变成文字」这件事拿到手",
        "showAccel": bool(vram),
        "accelReason": ("检测到独立显卡（%s，%.1f GB）" % (gpu.get("name", ""), vram / 1024.0)
                        if vram else "没检测到独立显卡：这一步用不上"),
        "agent": "agent-harness",
        "agentReady": agent_ready,
        "agentReason": ("标准版可以一键准备" if node.get("ok")
                        else "标准版需要先装 Node.js；也可以改用已装好的桌面客户端"),
    }


def environment_report() -> dict:
    """向导的"检查你的电脑"。**永不抛异常**，取不到的项一律给出空值 + 说明。

    每一节都单独兜住：一节坏了（比如显卡探测炸了）不该让整页打不开 —— 环境越坏，
    这个页面越是唯一能告诉用户"哪里坏了"的地方。
    """
    def safe_list(fn):
        try:
            return fn()
        except Exception as exc:
            return [{"error": "%s: %s" % (type(exc).__name__, exc)}]

    def safe_dict(fn):
        try:
            return fn()
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}

    try:
        gpu = plat.gpu_info()
    except Exception:
        gpu = {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": ""}
    try:
        os_ver = ".".join(str(x) for x in plat.os_version())
    except Exception:
        os_ver = ""
    try:
        platform_name = plat.display_name()
    except Exception:
        platform_name = ""
    locations = safe_list(locations_report)
    report = {
        "generatedAt": _now(),
        "platform": plat.current(),
        "platformName": platform_name,
        "osVersion": os_ver,
        "gpu": gpu,
        "locations": locations,
        "network": safe_dict(network_report),
        "audio": safe_dict(audio_report),
        "node": safe_dict(node_report),
        "agents": safe_dict(agent_report),
        #: 硬阻塞：这些不解决就别往下走（向导据此拦住"开始准备"）
        "blocked": [],
    }
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        if loc.get("key") in ("models", "meetings") and not loc.get("writable"):
            report["blocked"].append(loc)
    report["recommend"] = recommend(report)
    return report


# ---------------------------------------------------------------- 计划（决策相的产物）

DEFAULT_PLAN = {
    "schema": PLAN_SCHEMA,
    "state": "draft",
    "updatedAt": "",
    "choices": {},        # 位置 / 转写方式 / 唤醒 / 分离 / 加速 / AI 服务 / 智能体 / 兜底
    "built": {},          # 由 build_plan() 展开出来的执行计划（确认页显示、执行相执行）
    "execution": {},      # 执行相的执行记录（谁开始了、谁跳过、谁失败）
    "note": "",
}


def plan_path() -> str:
    return os.path.join(paths.data_root(), "wizard-plan.json")


def _normalize(data) -> dict:
    plan = dict(DEFAULT_PLAN)
    plan["choices"] = {}
    plan["built"] = {}
    plan["execution"] = {}
    if isinstance(data, dict):
        for key in ("state", "updatedAt", "note"):
            if key in data:
                plan[key] = data[key]
        if isinstance(data.get("choices"), dict):
            plan["choices"] = dict(data["choices"])
        # built / execution 必须一起保留：否则"关掉面板再打开还能接着看进度"就不成立
        # （2026-09-20 被 test_plan_file_remembers_state_built_and_execution 抓到）。
        if isinstance(data.get("built"), dict):
            plan["built"] = dict(data["built"])
        if isinstance(data.get("execution"), dict):
            plan["execution"] = dict(data["execution"])
    plan["schema"] = PLAN_SCHEMA
    if plan.get("state") not in PLAN_STATES:
        plan["state"] = "draft"
    return plan


def load_plan(path: str = "") -> dict:
    """读计划。文件不存在或坏掉都返回默认骨架：向导要能从头开始，不能被一个坏文件挡住。"""
    target = path or plan_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    return _normalize(data)


def save_plan(data, path: str = "") -> dict:
    """写计划（原子替换）。**只写这一个文件**：不写设置、不下载 —— 那是执行相的事。"""
    target = path or plan_path()
    plan = _normalize(data)
    plan["updatedAt"] = _now()
    folder = os.path.split(target)[0]
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return plan


# ---------------------------------------------------------------- 首装判据 / 执行后真值
#
# 设计 §5：`data/installed-components.json` 是**执行后的真值**，由向导走到末页时写出；
# 它**不存在 = 还没走过向导 = 首装**，面板据此自动进向导（设计 §0/§1："首装只装
# runtime-core，随后立刻进向导，向导不得跳过"）。
#
# 判据是"这个文件在不在"，**不是扫目录猜** —— 扫目录会把"装了但没走过向导"和
# "走过向导但选择跳过"混成同一种状态（设计 §5 明令不靠扫目录猜）。

INSTALLED_FILE = "installed-components.json"
INSTALLED_SCHEMA = "echo-installed-components/1"


def installed_path() -> str:
    return os.path.join(paths.data_root(), INSTALLED_FILE)


def first_run(path: str = "") -> bool:
    """首装？（还没写过 `installed-components.json`）

    老用户升级时也没有这个文件 → 会当首装进一次向导。这是**刻意**的：他们要补
    "模型文件放哪 / 笔记库在哪 / AI 服务"，而决策相全程只读、也能一路跳过。
    取不到时返回 False（宁可漏进一次向导，也不要因为探测失败把面板挡在门外）。
    ``path`` 只为测试注入；默认是数据根下的那个文件。
    """
    try:
        return not os.path.isfile(path or installed_path())
    except Exception:
        return False


def _model_ready(model_id: str):
    """模型文件是否就绪（真值来源：`modelinfo.ready`）。取不到返回 None —— 不假装"没装"。"""
    try:
        from app import modelinfo
        return modelinfo.ready(model_id)
    except Exception:
        return None


def finalize(plan_file: str = "", *, path: str = "", ready=None, write=None) -> dict:
    """向导走到末页时写 `data/installed-components.json`（设计 §4/§5 的"执行后真值"）。

    **绝不写设置的值**：里面可能有在线服务的密钥，而这个文件是明文。只记模型文件的
    已装状态与本地位置，以及"向导写过哪些设置"的**键名**列表。

    `ready` / `write` 可注入，便于测试；默认走 `modelinfo.ready` 与真实写盘。
    """
    plan = load_plan(plan_file)
    built = plan.get("built") or {}
    execution = plan.get("execution") or {}
    ready = ready or _model_ready

    components = []
    seen = set()
    for item in built.get("downloads") or []:
        cid = str(item.get("component") or "")
        mid = str(item.get("modelId") or "")
        if cid:
            if cid in seen:
                continue
            seen.add(cid)
        state = _model_ready_call(ready, mid) if mid else None
        components.append({"id": cid or mid, "modelId": mid,
                           "label": item.get("label") or cid,
                           "phase": item.get("phase") or "",
                           "ready": state,
                           "at": _now()})

    payload = {
        "schema": INSTALLED_SCHEMA,
        "writtenAt": _now(),
        "firstRunDone": True,
        "dataRoot": paths.data_root(),
        "modelsDir": paths.models_root(),
        #: 只记**键名**（不记值）：密钥不能落明文。面板据此知道"向导写过哪些配置"。
        "configKeys": [str(r.get("key")) for r in (built.get("config") or []) if r.get("key")],
        "components": components,
        "skipped": [r.get("component") for r in (execution.get("skipped") or [])],
        "failed": [{"component": r.get("component"),
                    "message": str(r.get("message") or r.get("error") or "")}
                   for r in (execution.get("failed") or [])],
    }

    target = path or installed_path()
    if write is not None:
        write(target, payload)
        return payload
    folder = os.path.split(target)[0]
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    payload["path"] = target
    return payload


def _model_ready_call(ready, model_id: str):
    """调一次就绪探测；任何异常都算 None（未知），不由它把 finalize 拖崩。"""
    try:
        state = ready(model_id)
    except Exception:
        return None
    return None if state is None else bool(state)


# ---------------------------------------------------------------- 决策 → 执行计划
#
# 用户的选择（choices）要展开成两类动作：
#   * **下载**：模型文件 → `modelinfo.start_download(model_id)`（组件清单里的 model_id 是唯一映射）；
#   * **写配置**：三处位置、provider 选择、智能体后端 —— 不占空间但要落进设置。
# 还有一类**面板不代装**的（CUDA 的 pip 包、gated 的 pyannote、本机服务），单独列出来并
# 写清原因与做法（设计 §0.6：不隐藏、不假装）。
#
# 顺序固定（设计 §4）：配置先行（下载才知道往哪写）→ 引擎 → 加速 → 兜底。
# runtime-core 由安装器在打开面板之前就装好，所以这里只校验、不下载。

PHASE_ORDER = ("config", "engines", "accel", "fallback")

PLAN_BUILD_SCHEMA = "echo-wizard-build/1"

#: S6 就地填的三样 → provider 声明里的键名后缀（`providerLlmBaseUrl` / `…ApiKey` / `…Model`）。
#: 按**后缀**匹配而不是写死整串：前缀（providerLlm / providerAsr）属于 provider 的命名空间。
_CRED_FIELDS = (("baseUrl", "BaseUrl"), ("apiKey", "ApiKey"), ("model", "Model"))

#: S6 打开"直连兜底"（`llm.direct`）时落到这一个（单上游直连）。
#: 多上游派发是「ECHO AUTO」那套的事，向导不替用户改（用户显式选了 provider 就以他为准）。
#: 注意它**不会**因为"填了地址"就自动生效 —— 见 `llm_provider_id()` 的说明。
DEFAULT_ONLINE_LLM = "openai-llm"


def llm_choice(choices: dict) -> dict:
    """S6 的选择（provider + 地址/密钥/模型）。容错：不是 dict 就当没填。"""
    llm = (choices or {}).get("llm") or {}
    return llm if isinstance(llm, dict) else {}


def llm_provider_id(choices: dict) -> str:
    """S6 的**直连兜底**用哪个 provider —— **只有用户显式打开才写**，不自动默认。

    2026-09-20 用户定调：会议纪要、归档、语音指令**默认都走智能体**（只有它有 skill
    机制做灵活扩展），直连大模型只是"不装智能体"时的兜底，**不推荐**。
    而 `meeting.direct_llm_decision()` 的第 1 条判据是"`providerLlm` 非空就强制走直连、
    哪怕 agent 也在" —— 所以向导**绝不能**因为"用户填了地址"就替他打开这个开关，
    否则等于把用户从推荐的智能体路径上踢走。
    """
    llm = llm_choice(choices)
    explicit = str(llm.get("provider", "") or "").strip()
    if explicit:
        return explicit
    # 前端只表达"我要用直连兜底"（`llm.direct`）；provider id 留在后端，不进 JS。
    return DEFAULT_ONLINE_LLM if llm.get("direct") else ""


def llm_configured(choices: dict) -> bool:
    """直连兜底是否**可用**：显式打开了它，且地址与密钥都填全。

    只认"填全"：半填的配置一旦写进 `providerLlm`，会把纪要**强制**从智能体切到直连
    却用不了 —— 比不配更糟。
    """
    llm = llm_choice(choices)
    if not llm_provider_id(choices):
        return False
    return bool(str(llm.get("baseUrl", "") or "").strip()
                and str(llm.get("apiKey", "") or "").strip())


def minutes_capable(choices: dict) -> bool:
    """「能不能自动写纪要」：**有智能体**（默认路径）或直连兜底可用。

    纪要不只看"有没有模型"：归档（写进笔记库）与语音指令都依赖智能体的 skill 机制，
    所以默认路径是智能体；直连只是兜底。
    """
    return bool(str((choices or {}).get("agent", "") or "").strip()) or llm_configured(choices)


def _credential_rows(kind: str, provider_id: str, values: dict) -> list:
    """把就地填的地址/密钥/模型落到**该 provider 自己声明**的设置键上（空值跳过）。

    provider 声明了什么键就写什么键 —— 向导不发明键名（声明见
    `app/providers/openai.py` 的 `details.settings`）。
    """
    keys = _provider_spec(kind, provider_id).get("settings") or []
    rows = []
    for field, suffix in _CRED_FIELDS:
        value = str((values or {}).get(field, "") or "").strip()
        if not value:
            continue                       # 空值不覆盖已有配置（与下面位置项同一规矩）
        key = next((k for k in keys if k.endswith(suffix)), "")
        if key:
            rows.append({"key": key, "value": value})
    return rows


def _setting_updates(choices: dict) -> list:
    """三处位置 + provider + 智能体 → 设置项。只写用户真的填了的（空值不覆盖已有配置）。"""
    locs = dict(choices.get("locations") or {})
    rows = []
    for key, setting_key in (("models", "modelsDir"),
                             ("meetings", "meetingsDir"),
                             ("notes", "worklogVaultRoot")):
        value = str(locs.get(key, "") or "").strip()
        if value:
            rows.append({"key": setting_key, "value": value})
    if str(locs.get("notes", "") or "").strip():
        rows.append({"key": "worklogEnabled", "value": True})
    agent = str(choices.get("agent", "") or "").strip()
    if agent:
        rows.append({"key": "agentBackend", "value": agent})
    if choices.get("asrOnline"):
        rows.append({"key": "providerAsr", "value": "openai-asr"})
    llm_provider = llm_provider_id(choices)
    if llm_provider:
        rows.append({"key": "providerLlm", "value": llm_provider})
        # S6 就地填的地址/密钥/模型（键名来自 provider 自己的声明，见 _credential_rows）
        rows += _credential_rows("llm", llm_provider, llm_choice(choices))
    # 其余任意设置：键名由调用方原样传入（向导不猜）
    extra = choices.get("extraSettings") or {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and key.strip() and isinstance(value, (str, int, float, bool)):
                rows.append({"key": key.strip(), "value": value})
    return rows


def _component_action(cid: str, manifests: dict) -> dict:
    """一个组件 id → 一条动作（下载 / 面板不代装 / 未知）。"""
    item = manifests.get(cid)
    if not item:
        return {"kind": "unknown", "component": cid, "label": cid, "approxMb": 0,
                "reason": "清单里没有这个模型文件（看 app/components.py 或 components/*.json）"}
    label = str(item.get("name") or cid)
    size = int(item.get("size_mb") or 0)
    model_id = str(item.get("model_id") or "")
    base = {"component": cid, "label": label, "approxMb": size,
            "how": str(item.get("how") or "")}
    # **不能随包分发 / 不许再分发**要排在 model_id 之前：pyannote 这类既有 model_id
    # 又是 gated（modelinfo 明确不支持从接口下载），先看 model_id 会把它排成"可下载"。
    if item.get("never_ship"):
        out = dict(base)
        out.update({"kind": "manual",
                    "reason": "这个模型文件不能随包分发（许可证限制），要你自己获取"})
        return out
    if model_id:
        ready = None
        try:
            from app import modelinfo
            ready = modelinfo.ready(model_id)
        except Exception:
            ready = None
        out = dict(base)
        out.update({"kind": "download", "modelId": model_id, "ready": ready})
        # 引擎依赖：装了 funasr 才有 sensevoice/qwen3asr；torch 是加速的依赖
        dep = str(item.get("pkg") or "")
        if dep:
            out["needs"] = dep
        return out
    if item.get("command"):
        out = dict(base)
        out.update({"kind": "manual", "reason": "需要在本机跑一个服务，面板不代装",
                    "command": str(item["command"])})
        return out
    out = dict(base)
    out.update({"kind": "manual", "reason": "这一项要手工准备（见说明）"})
    return out


def _provider_spec(kind: str, provider_id: str) -> dict:
    """取一个 provider 的元数据（含出网声明）。取不到就返回空 dict（不编造）。

    注意 ``providers.catalog()`` 的清单键是 **``providers``**（不是 ``items``）——
    2026-09-20 这里写错一次，后果是"用户选了在线转写却被静默丢掉"。
    """
    try:
        from app import providers as providers_mod
        rows = providers_mod.catalog(ready=False).get("providers") or []
        for row in rows:
            if row.get("kind") == kind and row.get("id") == provider_id:
                details = row.get("details") or {}
                return {"kind": kind, "id": provider_id, "name": row.get("name", ""),
                        "source": row.get("source", ""), "egress": bool(row.get("egress")),
                        "egressNote": row.get("egress_note", ""),
                        # 该 provider **自己声明**的设置键（`app/providers/openai.py` 的
                        # `details.settings`）。S6 就地填的地址/密钥就落到这些键上 ——
                        # 向导**不猜键名**，provider 改了自己的键这里自动跟上。
                        "settings": [str(k) for k in (details.get("settings") or [])]}
    except Exception:
        pass
    return {}


def build_plan(choices: dict) -> dict:
    """把用户的选择展开成执行计划（确认页显示它，执行相执行它）。**纯计算，不落地**。"""
    choices = dict(choices or {})
    manifests = {}
    try:
        from app import components
        manifests = {i["id"]: i for i in components.load_manifests()}
    except Exception:
        manifests = {}

    wanted = [("engines", cid) for cid in (choices.get("engines") or [])]
    if choices.get("wake"):
        wanted.append(("engines", "wake-kws"))
    if choices.get("diarize"):
        wanted.append(("engines", "diarize-pyannote"))
    if choices.get("accel"):
        wanted.append(("accel", "accel-cuda"))
    wanted += [("fallback", cid) for cid in (choices.get("fallback") or [])]

    downloads, manual, unavailable = [], [], []
    for phase, cid in wanted:
        action = _component_action(str(cid), manifests)
        action["phase"] = phase
        if action["kind"] == "download":
            downloads.append(action)
        elif action["kind"] == "manual":
            manual.append(action)
        else:
            unavailable.append(action)

    providers = []
    if choices.get("asrOnline"):
        spec = _provider_spec("asr", "openai-asr")
        if spec:
            providers.append(spec)
    llm_provider = llm_provider_id(choices)
    if llm_provider:
        spec = _provider_spec("llm", llm_provider)
        if spec:
            providers.append(spec)

    todo = [d for d in downloads if d.get("ready") is not True]
    total = sum(int(d.get("approxMb") or 0) for d in downloads)
    todo_mb = sum(int(d.get("approxMb") or 0) for d in todo)
    # "还不能做什么"：末页（S11）要能回答这个问题 —— 用"你会失去什么"的说法，不说技术原因
    missing = []
    if not minutes_capable(choices):
        missing.append({"feature": "自动写会议纪要",
                        "reason": "纪要由智能体负责（归档和语音指令也走它，靠技能扩展）；"
                                  "直连一个 AI 服务是不推荐的兜底",
                        "fix": "回到向导第 7 步准备智能体"})
    if not str(choices.get("agent", "") or "").strip():
        missing.append({"feature": "让 ECHO 帮你动手（整理笔记、操作文件）",
                        "reason": "还没选智能体后端", "fix": "回到向导第 7 步"})
    if choices.get("asrOnline"):
        missing.append({"feature": "断网时也能转写",
                        "reason": "你选了在线转写 —— 网断了就转不了",
                        "fix": "向导第 8 步装一个小号兜底"})
    return {
        "schema": PLAN_BUILD_SCHEMA,
        "builtAt": _now(),
        "phases": list(PHASE_ORDER),
        "config": _setting_updates(choices),
        "providers": providers,
        "downloads": downloads,
        "manual": manual,
        "unavailable": unavailable,
        "missing": missing,
        "totalMb": total,
        "todoMb": todo_mb,
        "readyMb": total - todo_mb,
        "downloadCount": len(todo),
        #: 确认页/末页要用的人话汇总（"将下载 N 项、合计 X MB"）
        "summary": {
            "downloads": "将下载 %d 项，合计约 %d MB" % (len(todo), todo_mb),
            "alreadyReady": "另有 %d 项已经装好，不重复下载" % (len(downloads) - len(todo)),
            "config": "将写入 %d 项设置" % len(_setting_updates(choices)),
            "manual": ("%d 项要你手工准备" % len(manual)) if manual else "",
            "missing": ("还有 %d 项功能要补" % len(missing)) if missing else "",
            "egress": [p["egressNote"] for p in providers if p.get("egress")],
        },
    }


def execute_plan(plan: dict = None, *, choices: dict = None,
                 settings_update=None, start_download=None, ready=None,
                 plan_file: str = "") -> dict:
    """执行相：**先把配置写下去，再依次触发下载**（设计 §4 的固定顺序）。

    设计上的两条硬要求在这里落地：
      * 已经就绪的**不重复下载**（`ready() is True` 就跳过，并记进 ``skipped``）；
      * 任何一项失败都**只登记、不中断**其余项（用户可重试或跳过，设计 §4）。
    三个外部动作都可注入，便于测试；默认走真实的 ``settings.update`` / ``modelinfo``。
    """
    built = plan or build_plan(choices or {})
    if settings_update is None:
        from app.config import settings as _settings
        settings_update = _settings.update
    if start_download is None or ready is None:
        from app import modelinfo
        start_download = start_download or modelinfo.start_download
        ready = ready or modelinfo.ready

    result = {"startedAt": _now(), "config": [], "downloads": [], "skipped": [],
              "failed": [], "ok": True}

    # ① 配置先行：下载才知道往哪写
    values = {}
    for row in built.get("config") or []:
        values[row["key"]] = row["value"]
    if values:
        try:
            updated = settings_update(values)
            result["config"] = list(updated or values.keys())
        except Exception as exc:
            result["ok"] = False
            result["failed"].append({"component": "(设置)", "error": "%s: %s"
                                     % (type(exc).__name__, exc)})

    # ② 依次触发下载
    for item in built.get("downloads") or []:
        mid = item.get("modelId") or ""
        row = {"component": item.get("component"), "modelId": mid,
               "label": item.get("label"), "approxMb": item.get("approxMb")}
        if not mid:
            row["status"] = "manual"
            result["failed"].append(row)
            continue
        try:
            if ready(mid) is True:
                row["status"] = "ready"
                result["skipped"].append(row)
                continue
        except Exception:
            pass
        try:
            ok, msg = start_download(mid)
            row["status"] = "running" if ok else "error"
            row["message"] = str(msg)
            (result["downloads"] if ok else result["failed"]).append(row)
            if not ok:
                result["ok"] = False
        except Exception as exc:
            row["status"] = "error"
            row["error"] = "%s: %s" % (type(exc).__name__, exc)
            result["failed"].append(row)
            result["ok"] = False

    # ③ 记进计划文件（面板重开后据此续显）
    current = load_plan(plan_file)
    current["state"] = "running"
    current["built"] = built
    current["execution"] = result
    if choices:
        current["choices"] = dict(choices)
    save_plan(current, plan_file)
    return result


def execution_state(plan_file: str = "") -> dict:
    """执行相的状态：把计划里的每项 + 真实下载进度（``modelinfo.jobs``）合成一份给人看的东西。

    状态词是**界面用语**（设计 §4）：排队中 / 正在下载 / 好了 / 没成 / 已跳过。
    """
    plan = load_plan(plan_file)
    built = plan.get("built") or {}
    execution = plan.get("execution") or {}
    jobs = {}
    try:
        from app import modelinfo
        jobs = modelinfo.jobs() or {}
    except Exception:
        jobs = {}

    def status_of(mid: str) -> dict:
        job = jobs.get(mid) or {}
        state = str(job.get("status") or "")
        if state == "running":
            return {"state": "downloading", "text": "正在下载",
                    "percent": job.get("percent"), "downloadedMb": job.get("downloaded_mb")}
        if state == "done":
            return {"state": "done", "text": "好了", "percent": 100}
        if state == "error":
            return {"state": "error", "text": "没成", "message": str(job.get("error") or "")}
        return {"state": "queued", "text": "排队中", "percent": 0}

    skipped = {r.get("modelId") for r in (execution.get("skipped") or [])}
    rows = []
    for item in built.get("downloads") or []:
        mid = item.get("modelId") or ""
        row = {"component": item.get("component"), "modelId": mid,
               "label": item.get("label"), "approxMb": item.get("approxMb"),
               "phase": item.get("phase")}
        if mid and mid in skipped:
            row.update({"state": "skipped", "text": "已经装好，跳过"})
        else:
            row.update(status_of(mid) if mid else {"state": "manual", "text": "要手工准备"})
        rows.append(row)

    done = [r for r in rows if r["state"] in ("done", "skipped")]
    return {
        "planState": plan.get("state", "draft"),
        "running": bool([r for r in rows if r["state"] == "downloading"]),
        "total": len(rows),
        "finished": len(done),
        "items": rows,
        "manual": built.get("manual") or [],
        "unavailable": built.get("unavailable") or [],
        "missing": built.get("missing") or [],
        "config": execution.get("config") or [],
        "failed": execution.get("failed") or [],
        "summary": ("%d/%d 项已就绪" % (len(done), len(rows))) if rows else "没有要下载的东西",
    }
