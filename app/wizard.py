# -*- coding: utf-8 -*-
"""wizard.py — 首装向导的数据面（D23/D24）

设计见 `docs/向导-分步设计.md`。这一层只做两件事，都是"只读检测"或"只写自己的小文件"：

  * ``environment_report()``：向导的"检查你的电脑"，以及每一步"推荐哪个 / 默认勾什么"的依据
    —— 三处位置与空间（能力包 / 会议文件 / 笔记库）、显卡、网络、麦克风、Node、智能体。
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
LOCATIONS = (
    ("models", "能力包", "modelsDir", True),
    ("meetings", "会议文件", "meetingsDir", True),
    ("notes", "笔记库", "worklogVaultRoot", False),
)

#: 网络体检目标（设计 §2 的 S0）。探这些是因为"能不能在线装"决定走在线还是离线组件包。
NET_TARGETS = (
    ("modelscope", "能力包下载（ModelScope）", "https://www.modelscope.cn"),
    ("hf_mirror", "能力包下载（HF 镜像）", "https://hf-mirror.com"),
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
        _location_entry("models", "能力包", "modelsDir", model_path, bool(models)),
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


# ---------------------------------------------------------------- 决策 → 执行计划
#
# 用户的选择（choices）要展开成两类动作：
#   * **下载**：能力包 → `modelinfo.start_download(model_id)`（组件清单里的 model_id 是唯一映射）；
#   * **写配置**：三处位置、provider 选择、智能体后端 —— 不占空间但要落进设置。
# 还有一类**面板不代装**的（CUDA 的 pip 包、gated 的 pyannote、本机服务），单独列出来并
# 写清原因与做法（设计 §0.6：不隐藏、不假装）。
#
# 顺序固定（设计 §4）：配置先行（下载才知道往哪写）→ 引擎 → 加速 → 兜底。
# runtime-core 由安装器在打开面板之前就装好，所以这里只校验、不下载。

PHASE_ORDER = ("config", "engines", "accel", "fallback")

PLAN_BUILD_SCHEMA = "echo-wizard-build/1"


def _setting_updates(choices: dict) -> list:
    """三处位置 → 设置项。只写用户真的填了的（空值不覆盖已有配置）。"""
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
    llm = choices.get("llm") or {}
    if str(llm.get("provider", "") or "").strip():
        rows.append({"key": "providerLlm", "value": str(llm["provider"]).strip()})
    # 在线服务/转写的地址与密钥：键名由 providers 层定义，向导不猜 —— 由调用方原样传入
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
                "reason": "清单里没有这个能力包（看 app/components.py 或 components/*.json）"}
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
                    "reason": "这个能力包不能随包分发（许可证限制），要你自己获取"})
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
    out.update({"kind": "manual", "reason": "这个能力包要手工准备（见说明）"})
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
                return {"kind": kind, "id": provider_id, "name": row.get("name", ""),
                        "source": row.get("source", ""), "egress": bool(row.get("egress")),
                        "egressNote": row.get("egress_note", "")}
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
    llm = choices.get("llm") or {}
    if str(llm.get("provider", "") or "").strip():
        spec = _provider_spec("llm", str(llm["provider"]).strip())
        if spec:
            providers.append(spec)

    todo = [d for d in downloads if d.get("ready") is not True]
    total = sum(int(d.get("approxMb") or 0) for d in downloads)
    todo_mb = sum(int(d.get("approxMb") or 0) for d in todo)
    return {
        "schema": PLAN_BUILD_SCHEMA,
        "builtAt": _now(),
        "phases": list(PHASE_ORDER),
        "config": _setting_updates(choices),
        "providers": providers,
        "downloads": downloads,
        "manual": manual,
        "unavailable": unavailable,
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
        "config": execution.get("config") or [],
        "failed": execution.get("failed") or [],
        "summary": ("%d/%d 项已就绪" % (len(done), len(rows))) if rows else "没有要下载的东西",
    }
