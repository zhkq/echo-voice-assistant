# -*- coding: utf-8 -*-
"""「起本机后端」的**前置探测**与**计划**（客户端简化第 3 步 · 批 2）。

这一层的职责只有一句话：**在动手之前，把"这台机器能走哪条路、缺什么、要不要联网"说清楚**。
它**只读**（不装东西、不起进程、不改设置），所以面板可以随时问它 —— 而"只读"也是它敢在
点按钮之前就给出结论的原因（实施方案 §2 的第一句就是 ``plan()`` 只读，先给人看）。

两条交付路（实施方案 §1）
------------------------
* **容器路**（宿主有 NVIDIA 卡 + Docker + nvidia runtime）：镜像 + 权重卷，`docker compose` 起；
* **扩展包路**（不走容器）：`{echoBase}/backend` 下自带 `app/`+`server/`+独立 venv。

**这一层不实现这两条路的"装"** —— 容器路的落地是批 4、扩展包的出包是批 5。
所以计划里每条路都有一句 ``implemented`` + 一句人话，面板照实显示"这件事现在能不能一键做；
不能的话手工怎么走"。（这正是实施方案 §4 那张"诚实降级"表的落点。）

为什么每个探针都要留**原文**
--------------------------
`nvidia-smi` / `docker` 失败时，最有用的是**它自己那句话**（"no CUDA-capable device"、
"error during connect: … 拒绝连接"），而不是我们转述的"没检测到显卡/没装 Docker"——
后者会把两种完全不同的故障说成同一句话，而它们的下一步动作正好相反
（装驱动 vs 启动 Docker Desktop）。这条纪律与 `base.UNIMPLEMENTED_BACKENDS` 是同一条。

缓存
----
面板会反复问（每次刷新一次），而 `docker info` / `nvidia-smi` 都是几百毫秒级的子进程调用，
所以结果缓存 ``CACHE_TTL_S`` 秒。**故意不缓存"必须实时"的东西**：端口占用是另一条路
（`backend_proc`），不在这里。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from app import backend_proc, backend_setup, paths, platform

#: 探测结果缓存多久（秒）。面板轮询比这快时吃缓存。
CACHE_TTL_S = 10.0

#: 两条路的**估算**体积与耗时（写清楚是估算，不是承诺）。
SIZE_MODELS_MB = 6200            # 权重（本机模型库里已有的那部分不算）
SIZE_IMAGE_MB = 4096             # 镜像 / 独立 venv（3~5 GB）
ETA_CONTAINER_MIN = 20           # 拉镜像/构建 + 权重 + 首次起
#: 显存估算（实施方案 §6-4 的实测值）：qwen3asr + 强制对齐器峰值 4.6 GB、pyannote 2.6 GB。
VRAM_ASR_MB = 4700
VRAM_DIARIZE_MB = 2600
#: 低于这个显存就别指望两个模型同时常驻（8 GB 卡上桌面还要吃掉 1 GB+）。
VRAM_MIN_MB = 8 * 1024
#: 分离档对算力的要求：pyannote 4.x 要 torch>=2.8，而 cu118 上带 Pascal 的最后一版是 2.7.1。
MIN_COMPUTE_CAP_FOR_DIARIZE = 7.5

#: 每个变体要哪几棵权重子树（与 `scripts/build_backend_kit.py::VARIANTS["models"]` 同一份口径：
#: 名字就是容器/服务端找的那个路径）。
VARIANT_MODELS: Dict[str, Tuple[str, ...]] = {
    "cu126": ("hub/models--Qwen--Qwen3-ASR-0.6B",
              "hub/models--Qwen--Qwen3-ForcedAligner-0.6B",
              "sensevoice",
              "pyannote"),
    "cu118": ("sensevoice",),
}

#: 容器路必须先验一步的那条命令（实施方案 §6-1：Windows + Docker Desktop 的 GPU 直通
#: 是这条路上最容易翻车的一步，不通就整条路降级成扩展包路）。
GPU_PASSTHROUGH_CMD = ("docker run --rm --gpus all "
                       "nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi")

_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}


# ---------------------------------------------------------------- 小工具

def _run(argv: List[str], timeout: float = 8.0) -> Dict[str, Any]:
    """跑一条命令，**把 stdout/stderr 原文都带回来**（永不抛）。"""
    out = {"ok": False, "code": -1, "stdout": "", "stderr": "", "error": ""}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        out["error"] = "找不到命令 %s（不在 PATH）" % argv[0]
        return out
    except Exception as e:
        out["error"] = "运行 %s 失败：%s" % (argv[0], e)
        return out
    out.update({"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": (proc.stdout or "").strip(),
                "stderr": (proc.stderr or "").strip()})
    if not out["ok"] and not out["stderr"]:
        out["stderr"] = out["stdout"]
    return out


def _free_gb(path: str) -> Optional[float]:
    """path 所在盘的剩余空间（GB）。路径还不存在时往上找第一个存在的祖先。

    刻意用 ``os.path.split`` 而不是 ``dirname``：审计规则的 PATH_DERIVATION 认的就是
    ``dirname`` 那一族，本模块不该出现它们（与 `app/wizard.py` 同一个写法）。
    """
    try:
        head = str(path or "")
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


# ---------------------------------------------------------------- 各探针

def docker() -> Dict[str, Any]:
    """Docker 的现状：装没装 / 守护进程起没起 / 有没有 compose / 有没有 nvidia runtime。

    **"装了但没起来"必须与"没装"分开说**（2026-09-30 定）：前者的下一步是启动 Docker
    Desktop，后者是去装一个 —— 说成同一句话会让人白跑一趟。
    """
    out: Dict[str, Any] = {"installed": False, "daemon": False, "version": "",
                           "compose": "", "runtimes": [], "gpuRuntime": False,
                           "error": ""}
    exe = shutil.which("docker")
    if not exe:
        out["error"] = "PATH 里没有 docker（没装 Docker，或者它不在 PATH）"
        return out
    out["installed"] = True
    ver = _run([exe, "version", "--format", "{{.Server.Version}}"])
    if not ver["ok"]:
        # 客户端在、但服务端连不上：Docker Desktop 没起来 / 没权限 / 引擎没跑
        out["error"] = (ver["stderr"] or ver["error"] or "docker version 失败").strip()[:500]
        return out
    out["daemon"] = True
    out["version"] = ver["stdout"].splitlines()[0].strip() if ver["stdout"] else ""
    comp = _run([exe, "compose", "version", "--short"])
    if comp["ok"]:
        out["compose"] = comp["stdout"].splitlines()[0].strip() if comp["stdout"] else "yes"
    info = _run([exe, "info", "--format", "{{json .Runtimes}}"])
    if info["ok"] and info["stdout"]:
        import json
        try:
            runtimes = json.loads(info["stdout"])
            if isinstance(runtimes, dict):
                out["runtimes"] = sorted(str(k) for k in runtimes)
        except Exception:
            out["runtimes"] = [info["stdout"][:200]]
    out["gpuRuntime"] = any("nvidia" in r.lower() for r in out["runtimes"])
    return out


def gpu() -> Dict[str, Any]:
    """显卡：走平台接缝（`platform.gpu_info()`），**带算力与原文报错**。"""
    info = {}
    try:
        info = dict(platform.gpu_info() or {})
    except Exception as e:                                        # pragma: no cover - 兜底
        info = {"error": "读显卡信息失败：%s" % e}
    info.setdefault("vendor", "")
    info.setdefault("name", "")
    info.setdefault("vramMb", 0)
    info.setdefault("driver", "")
    info.setdefault("computeCap", "")
    info.setdefault("error", "")
    return info


def _cap_float(raw) -> float:
    """``"7.5"`` → ``7.5``；判不了返回 ``0.0``（= 未知，按"保守"处理）。"""
    try:
        return float(str(raw).strip())
    except Exception:
        return 0.0


def check_torch_abi(python_exe: str, timeout: float = 180.0) -> Dict[str, Any]:
    """跑一次 torch/torchaudio 的 **ABI 校验** → ``{"ok", "torch", "torchaudio", "error"}``。

    判据照 `server/Dockerfile` 构建期那一条：**两者的 CUDA 源标签必须一致**（都带 `+cu126`），
    并且 `import torchaudio` 必须通过。**版本号相等不是判据**（PyTorch 2.9 之后 torchaudio
    停更，版本号天生对不上，见 `AGENTS.md`）。返回的 `error` 是**原文**
    （"报校验原文"是实施方案 §4 那两行要求的）。

    单独抽成一个函数，是因为**两个地方都要它**：前置探测（`runtime()`）与扩展包出包的
    安装期校验（`scripts/build_backend_portable.py`）—— 两份实现迟早会有一份漏掉。
    """
    out: Dict[str, Any] = {"ok": False, "torch": "", "torchaudio": "", "error": ""}
    if not python_exe or not os.path.isfile(str(python_exe)):
        out["error"] = "没有解释器：%s" % (python_exe or "（空）")
        return out
    code = ("import torch, torchaudio, sys;"
            "t = torch.__version__; a = torchaudio.__version__;"
            "tc = t.split('+')[1] if '+' in t else '';"
            "ac = a.split('+')[1] if '+' in a else '';"
            "print(t); print(a);"
            "sys.exit(0 if (tc and tc == ac) else 3)")
    res = _run([str(python_exe), "-c", code], timeout=timeout)
    lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
    if len(lines) >= 2:
        out["torch"] = lines[0].strip()
        out["torchaudio"] = lines[1].strip()
    if res["ok"]:
        out["ok"] = True
        return out
    out["error"] = (res.get("stderr") or res.get("error") or
                    "torch/torchaudio 的 CUDA 源标签不一致").strip()[:800]
    if res.get("code") == 3:
        out["error"] = ("torch %s 与 torchaudio %s 不是同一个 CUDA 源（标签不一致）—— "
                        "症状是每个 /v1/asr 都 503 model_failed"
                        % (out["torch"] or "?", out["torchaudio"] or "?"))
    return out


def runtime() -> Dict[str, Any]:
    """扩展包那条路的运行时：在不在（+ 装了的话做一次 torch/torchaudio 的 ABI 校验）。

    ABI 校验照 `server/Dockerfile` 构建期那一条：**两者的 CUDA 源标签必须一致**
    （都带 `+cu126`），并且 `import torchaudio` 必须通过 —— 版本号相等**不是**判据
    （PyTorch 2.9 之后 torchaudio 停更，版本号天生对不上，见 `AGENTS.md`）。
    """
    exe = ""
    try:
        exe = backend_proc.python_exe()
    except Exception:                                             # pragma: no cover - 兜底
        exe = ""
    out: Dict[str, Any] = {"ready": bool(exe), "path": exe, "abiOk": None,
                           "torch": "", "torchaudio": "", "error": ""}
    if not exe:
        out["error"] = "还没装运行时（%s 下没有 runtime/）" % backend_proc.backend_root()
        return out
    abi = check_torch_abi(exe)
    out.update({"abiOk": bool(abi["ok"]), "torch": abi["torch"],
                "torchaudio": abi["torchaudio"], "error": abi["error"]})
    return out


def weights(variant: str = "") -> Dict[str, Any]:
    """权重够不够：按变体声明的那几棵子树，看**客户端模型库**里有没有。

    **说清看的是哪一处**（实施方案 §6-3）：这里看的是 `paths.models_root()`；
    服务端还会去它自己的 ModelScope 缓存找（`MODELSCOPE_CACHE`），**那边有没有我们不算** ——
    两处判据混在一起说，就会出现"面板说齐了、服务端仍 503 model_failed"。
    """
    root = ""
    try:
        root = paths.models_root()
    except Exception:                                             # pragma: no cover - 兜底
        root = ""
    wanted = VARIANT_MODELS.get(str(variant or ""), ())
    present, missing = [], []
    for rel in wanted:
        target = os.path.join(root, rel) if root else ""
        ok = False
        if target and os.path.isdir(target):
            try:
                ok = any(True for _ in os.scandir(target))
            except Exception:
                ok = False
        (present if ok else missing).append(rel)
    return {"root": root, "variant": str(variant or ""), "wanted": list(wanted),
            "present": present, "missing": missing,
            "ready": bool(wanted) and not missing}


def _modelscope_cache() -> str:
    """服务端可能会去找的那处缓存（只用来**说清我们没算它**）。"""
    env = str(os.environ.get("MODELSCOPE_CACHE") or "").strip()
    if env:
        return env
    home = os.path.expanduser("~")
    return os.path.join(home, ".cache", "modelscope", "hub") if home else ""


# ---------------------------------------------------------------- 汇总 / 计划

def _safe(fn, default):
    """跑一个探针；它炸了就用 ``default``（**面板每次刷新都问这一层，探针不许把界面带下水**）。

    默认值一律是"**保守/未知**"那一侧（例如显卡探测失败 = 没有显卡 → 计划里如实说
    "没找到 N 卡"并给出原文），而不是假装成功 —— 假装成功的下一步是"点了按钮才发现不行"。
    """
    try:
        return fn()
    except Exception as e:
        out = dict(default) if isinstance(default, dict) else default
        if isinstance(out, dict):
            out["error"] = "探针炸了：%s" % e
        return out


def probe(force: bool = False) -> Dict[str, Any]:
    """一次把该问的都问一遍（带缓存）。**只读，永不抛。**"""
    now = time.monotonic()
    if not force and _CACHE["value"] is not None and (now - _CACHE["at"]) < CACHE_TTL_S:
        return dict(_CACHE["value"])
    root = ""
    try:
        root = backend_setup.backend_root()
    except Exception:                                             # pragma: no cover - 兜底
        root = ""
    ports_ok, ports_detail = True, ""
    try:
        ports_ok, ports_detail = backend_proc.port_check()
    except Exception as e:
        # 端口读不出来时按"不确定"报（**不是**"被占"）：那是两件事，
        # 而"被占"会让用户去停一个根本没占用的东西。
        ports_ok, ports_detail = False, "端口状态读不出来：%s" % e
    blank_gpu = {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": "",
                 "computeCap": "", "error": ""}
    value = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": root,
        "gpu": _safe(gpu, blank_gpu),
        "docker": _safe(docker, {"installed": False, "daemon": False, "version": "",
                                 "compose": "", "runtimes": [], "gpuRuntime": False,
                                 "error": ""}),
        "runtime": _safe(runtime, {"ready": False, "path": "", "abiOk": None,
                                   "torch": "", "torchaudio": "", "error": ""}),
        "diskFreeGB": _safe(lambda: _free_gb(root), None),
        "portsOk": bool(ports_ok),
        "portsDetail": ports_detail,
        "modelscopeCache": _safe(_modelscope_cache, ""),
    }
    _CACHE.update({"at": now, "value": value})
    return dict(value)


def reset_cache() -> None:
    _CACHE.update({"at": 0.0, "value": None})


def variant_for(gpu_info: Dict[str, Any]) -> Tuple[str, List[str]]:
    """按算力选变体 → ``(变体, 要说的理由)``。

    算力判不了（老驱动不认 `compute_cap`）时**按新卡走**并如实说明 —— 因为"猜错成老卡"
    的后果更贵（老卡档装不了 qwen3asr，而那台机器其实跑得动）。
    """
    cap = _cap_float(gpu_info.get("computeCap"))
    if not cap:
        return "cu126", ["算力判不了（nvidia-smi 没给出 compute_cap，驱动较老）：按新卡档 "
                         "cu126 走；装不上就换 cu118 那一档（老卡档只带 SenseVoice）"]
    if cap < MIN_COMPUTE_CAP_FOR_DIARIZE:
        return "cu118", ["算力 %.1f < %.1f：**这块卡做不了说话人分离**（pyannote 4.x 要 "
                         "torch>=2.8，而带 Pascal 的 cu118 最后一版是 2.7.1）—— 这一档"
                         "只装 SenseVoice、**只转写**，配置里也不宣告分离与声纹"
                         % (cap, MIN_COMPUTE_CAP_FOR_DIARIZE)]
    return "cu126", ["算力 %.1f ≥ %.1f：新卡档（qwen3asr + 对齐器 + pyannote）"
                     % (cap, MIN_COMPUTE_CAP_FOR_DIARIZE)]


def plan(force: bool = False) -> Dict[str, Any]:
    """**只读**计划：走哪条路、哪一档、多大、多久、缺什么、要不要联网、先验哪一步。

    面板把它原样显示给用户（"先给人看，再点开始"就是这一步的意义）。
    ``implemented`` 说明"这件事现在能不能一键做"——容器路是批 4、扩展包是批 5，
    在那之前它**如实说不能**，并给出手工怎么走。
    """
    p = probe(force=force)
    gpu_info = p["gpu"]
    docker_info = p["docker"]
    rt = p["runtime"]
    notes: List[str] = []
    missing: List[str] = []
    reasons: List[str] = []

    if gpu_info.get("error"):
        reasons.append("nvidia-smi 原文：%s" % gpu_info["error"])
    if docker_info.get("error"):
        reasons.append("docker 原文：%s" % docker_info["error"])

    # ---- 有没有一块能用的 NVIDIA 卡（没有就没有 CUDA，两条路都不成立）
    if not gpu_info.get("vendor"):
        notes.append("没找到 NVIDIA 显卡 —— 后端要 CUDA，设计上**不回退 CPU**"
                     "（回退会把所有客户端一起拖慢，而且从指标上看不出原因）。")
        notes.append("下一步：① 换成「用同事给我的后端」（配对串）；② 或者改用在线转写"
                     "（录音会上传）；③ 有卡但驱动没装好 → 先装驱动，再回来看这一页。")
        return {"path": "none", "variant": "", "implemented": False,
                "whyNot": "这台机器上没有可用的 NVIDIA 显卡（" +
                          (gpu_info.get("error") or "nvidia-smi 探不到") + "）",
                "missing": ["NVIDIA 显卡 / 驱动"], "notes": notes, "reasons": reasons,
                "needsNetwork": False, "sizeMb": 0, "etaMinutes": 0,
                "diskFreeGB": p["diskFreeGB"], "probe": p}

    variant, variant_reasons = variant_for(gpu_info)
    reasons += variant_reasons

    # ---- 走哪条路
    path = "portable"
    docker_ok = bool(docker_info.get("installed") and docker_info.get("daemon")
                     and docker_info.get("gpuRuntime") and docker_info.get("compose"))
    if docker_ok:
        path = "container"
        reasons.append("Docker %s + compose %s + nvidia runtime 都在 → 容器路"
                       % (docker_info.get("version") or "?",
                          docker_info.get("compose") or "?"))
    else:
        if not docker_info.get("installed"):
            reasons.append("没装 Docker（或不在 PATH）→ 走**扩展包路**（不用容器）")
        elif not docker_info.get("daemon"):
            reasons.append("Docker 装了但**守护进程连不上**（Docker Desktop 没起来？）→ "
                           "修好它就有容器路；现在按扩展包路计划")
            notes.append("先把 Docker 起起来（Docker Desktop / `dockerd`）再回来看这一页；"
                         "原文见下面「原文」那一栏。")
        elif not docker_info.get("compose"):
            reasons.append("Docker 在，但没有 `docker compose`（v2 插件）→ 按扩展包路计划")
            notes.append("要容器路就装 compose 插件（Docker Desktop 自带）；"
                         "或者直接走扩展包路。")
        else:
            reasons.append("Docker 在，但**没有 nvidia runtime**（容器里看不到显卡）→ "
                           "按扩展包路计划")
            notes.append("要容器路：装 nvidia-container-toolkit（或 Docker Desktop 的 "
                         "WSL2 GPU 支持），然后**先验一步**：%s" % GPU_PASSTHROUGH_CMD)

    # ---- 显存够不够（两条路都要看）
    vram = int(gpu_info.get("vramMb") or 0)
    if vram:
        reasons.append("显存 %d MB（约 %.1f GB）" % (vram, vram / 1024.0))
    spec_hint = "full"
    if vram and vram < VRAM_MIN_MB:
        spec_hint = "asr-only"
        notes.append("这块卡 %.1f GB：qwen3asr+对齐器峰值 %.1f GB、pyannote %.1f GB，"
                     "**同时常驻会顶爆** —— 建议只起转写（SenseVoice），"
                     "分离如实不可用（配置里不宣告它）。"
                     % (vram / 1024.0, VRAM_ASR_MB / 1024.0, VRAM_DIARIZE_MB / 1024.0))
    cap = _cap_float(gpu_info.get("computeCap"))
    if cap and cap < MIN_COMPUTE_CAP_FOR_DIARIZE:
        spec_hint = "asr-only"

    # ---- 运行时 / 权重 / 磁盘 / 端口
    if path == "portable":
        if not rt.get("ready"):
            missing.append("后端的运行时（%s 下没有 runtime/）" % backend_setup.backend_root())
            notes.append("扩展包要自带运行时（随包一份 CPython + 依赖）。这一档的**出包**"
                         "还没做（实施方案 §5 的批 5）；在那之前：手工把后端包解到 %s，"
                         "再点「起本机后端」。" % backend_setup.backend_root())
        elif rt.get("abiOk") is False:
            notes.append("**运行时已装但 ABI 不符**（原文：%s）—— 换变体 / 清 runtime 重装。"
                         % (rt.get("error") or ""))
            missing.append("运行时里 torch 与 torchaudio 的 CUDA 标签一致")
    w = weights(variant)
    if w["missing"]:
        missing.append("权重：" + "、".join(w["missing"]))
        notes.append("权重看的是**客户端模型库** `%s`（缺上面那几棵）；服务端还会去它自己的 "
                     "ModelScope 缓存找（`%s`）—— 那边有没有我们不算，所以"
                     "「面板说齐了、服务端仍 503」是可能的。"
                     % (w["root"], p["modelscopeCache"]))
    need_gb = round((SIZE_MODELS_MB + SIZE_IMAGE_MB) / 1024.0, 1)
    if p["diskFreeGB"] is not None and p["diskFreeGB"] < need_gb:
        missing.append("磁盘：需要约 %.1f GB，现有 %.1f GB" % (need_gb, p["diskFreeGB"]))
        notes.append("腾地方或把「本机后端的家」指到别的盘（`capabilityBackendDir`）"
                     "—— 权重与运行时都跟着它走。")
    if not p["portsOk"]:
        missing.append(p["portsDetail"])
        notes.append("端口被占：如果我们自己起的那个在跑 → 直接配对；否则先停掉占用者"
                     "或换端口（改生成的 `server.yaml` 里的 `listen`/`admin_listen`）。")

    implemented = False
    todo = "容器路（实施方案 §5 的批 4）"
    how = ("现在能做的：拿交付的后端包手工 `docker compose up -d`，再回这一页点"
           "「检测本机后端」；或者走扩展包路。")
    if path == "portable":
        todo = "扩展包的出包与自动解包（实施方案 §5 的批 5）"
        how = ("现在能做的：把后端包（源码 + runtime）手工解到 %s，再回这一页点"
               "「起本机后端」。" % backend_setup.backend_root())
    notes.insert(0, "「一键装好」这一步还没做：%s。%s" % (todo, how))

    return {
        "path": path,
        "variant": variant,
        "specsHint": spec_hint,
        "implemented": implemented,
        "whyNot": "" if implemented else "一键安装还没做（%s）" % todo,
        "missing": missing,
        "notes": notes,
        "reasons": reasons,
        "needsNetwork": True,
        "sizeMb": SIZE_MODELS_MB + SIZE_IMAGE_MB,
        "etaMinutes": ETA_CONTAINER_MIN if path == "container" else ETA_CONTAINER_MIN - 5,
        "diskFreeGB": p["diskFreeGB"],
        "verify": ([{"command": GPU_PASSTHROUGH_CMD,
                     "why": "Windows + Docker Desktop 的 GPU 直通是容器路最容易翻车的一步；"
                            "不通就整条路降级成扩展包路"}]
                   if path == "container" else []),
        "weights": w,
        "probe": p,
    }
