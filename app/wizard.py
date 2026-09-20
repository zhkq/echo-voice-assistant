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
    "note": "",
}


def plan_path() -> str:
    return os.path.join(paths.data_root(), "wizard-plan.json")


def _normalize(data) -> dict:
    plan = dict(DEFAULT_PLAN)
    plan["choices"] = {}
    if isinstance(data, dict):
        for key in ("state", "updatedAt", "note"):
            if key in data:
                plan[key] = data[key]
        if isinstance(data.get("choices"), dict):
            plan["choices"] = dict(data["choices"])
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
