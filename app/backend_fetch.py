# -*- coding: utf-8 -*-
"""薄包那条路的**取运行时**：按国内源把 torch/torchaudio/依赖装进 `{backend}/runtime`。

用户 2026-09-30 拍板：**默认 = 薄包 + 国内可下载**（"通用包 / 卡包"两段式以后再说）。
所以薄包本身只有源码 + 配置模板 + 安装脚本 + CPython（可选），**几 GB 的 torch 不随包走** ——
由这一步从**国内镜像**取。国内源在**这台机器所在网络**实测可达（2026-09-30）：

| 用途 | 源 | 实测 |
|---|---|---|
| 普通依赖（fastapi/uvicorn/funasr/transformers/pyannote…） | 清华 PyPI `https://pypi.tuna.tsinghua.edu.cn/simple`、阿里 `https://mirrors.aliyun.com/pypi/simple/` | 200 ✅ |
| **CUDA 版 torch/torchaudio** | SJTU `https://mirror.sjtu.edu.cn/pytorch-wheels/<cuXXX>/`（标准 PEP503 索引） | 200 ✅（`cu128` 实测） |

## 两条纪律，都是从真事故里来的

1. **torch 与 torchaudio 必须同一条 pip、同一个索引**（`server/Dockerfile` 的构建期校验就是这条）。
   分开装、或让 torchaudio 从普通 PyPI 被依赖顺带拉进来，会拿到**无标签/cu13x** 那份 →
   `import torchaudio` 崩 → funasr(SenseVoice) 模型加载也失败 → **每个 `/v1/asr` 都 503 model_failed**。
   所以这里是**两阶段**：① 只装 torch+torchaudio（CUDA 索引）；② 再装其余（PyPI 镜像）。
2. **装完必须自检**（`backend_env.check_torch_abi` + `import funasr`）——**在安装期炸，不要留到运行时**。
   返回的 `detail` 里带 pip 的**原文**，不加工。

**永不抛**：返回 `(ok, detail)`；每一步的输出都落到 `{backend}/logs/fetch-runtime.log`，
面板/端点只显示"到哪一步了 + 失败原因原文"。
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app import backend_proc, backend_env, backend_setup

#: 普通依赖的 PyPI 镜像（按顺序试）。**国内优先**，因为这条路就是为国内网络定的。
PIP_INDEXES: Tuple[str, ...] = ("https://pypi.tuna.tsinghua.edu.cn/simple",
                                "https://mirrors.aliyun.com/pypi/simple/")

#: CUDA 版 torch/torchaudio 的索引模板（按变体填 `cu126` / `cu118` / `cu128`）。
#: SJTU 那个是**标准 PEP503 索引**（实测 200），所以能直接当 `--index-url` 用。
TORCH_INDEX_TMPL = "https://mirror.sjtu.edu.cn/pytorch-wheels/%s/"

#: 每个变体额外要装的（口径照 `server/Dockerfile` 的 `ECHO_EXTRA` 层：
#: 老卡装不了 pyannote，所以 cu118 刻意只装 funasr 那条链）。
VARIANT_EXTRAS: Dict[str, Tuple[str, ...]] = {
    "cu126": ("funasr", "qwen-asr", "transformers", "pyannote.audio", "speechbrain"),
    "cu118": ("funasr", "transformers"),
    "cu128": ("funasr", "qwen-asr", "transformers", "pyannote.audio", "speechbrain"),
}

#: torch 版本钉法：老卡（Pascal/Volta）在 cu118 上最后一版是 2.7.1（Dockerfile 同款）；
#: 新卡不钉（让索引给最新）。
VARIANT_TORCH: Dict[str, str] = {"cu118": "2.7.1", "cu126": "", "cu128": ""}

#: 老卡那档的 pyannote 装不上（4.x 要新 torch），如实说明而不是让它装一半炸掉。
VARIANT_NOTES: Dict[str, str] = {
    "cu118": "老卡档只装转写（funasr/SenseVoice）：pyannote 4.x 要 torch>=2.8，"
             "而 cu118 上 Pascal 的最后一版是 2.7.1 —— 这一档没有说话人分离。",
}

LOG_NAME = "fetch-runtime.log"
#: pip 的超时给足（几 GB，国内源实测速度可接受；这里只兜底"卡死"）。
PIP_TIMEOUT_S = 3600.0


def runtime_dir() -> str:
    return os.path.join(backend_setup.backend_root(), "runtime")


def log_path() -> str:
    return os.path.join(backend_setup.backend_root(), "logs", LOG_NAME)


def torch_index(variant: str) -> str:
    return TORCH_INDEX_TMPL % str(variant or "cu126").strip()


def requirements(variant: str) -> Dict[str, Any]:
    """这一步要装什么（**分两阶段**，因为索引不同）→ 面板/日志共用一份。"""
    v = str(variant or "cu126").strip()
    base = os.path.join(backend_setup.backend_root(), "server", "requirements.txt")
    pin = VARIANT_TORCH.get(v, "")
    torch_pkgs = ["torch" + ("==%s" % pin if pin else ""),
                  "torchaudio" + ("==%s" % pin if pin else "")]
    return {"variant": v, "torchIndex": torch_index(v), "torch": torch_pkgs,
            "extras": list(VARIANT_EXTRAS.get(v, ())), "baseFile": base,
            "baseFileExists": os.path.isfile(base),
            "note": VARIANT_NOTES.get(v, ""),
            "pipIndexes": list(PIP_INDEXES)}


def _write_log(lines: List[str], chunk: str) -> None:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(chunk if chunk.endswith("\n") else chunk + "\n")
    except Exception:
        pass


def _run_pip(exe: str, args: List[str], label: str,
             on_step: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """跑一条 pip，**输出同时落日志**（面板只显示最后几行 + 原文尾部）。"""
    cmd = [exe, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
           "--no-warn-script-location"] + args
    if on_step:
        on_step(label)
    _write_log([], "\n===== %s =====\n$ %s" % (time.strftime("%H:%M:%S"), " ".join(cmd)))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT_S)
    except Exception as e:
        detail = "%s 失败：%s" % (label, e)
        _write_log([], detail)
        return False, detail
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    _write_log([], out[-4000:])
    if proc.returncode != 0:
        tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-6:])
        return False, "%s 失败（pip 退出码 %d）：\n%s\n（完整输出：%s）" % (
            label, proc.returncode, tail, log_path())
    return True, "%s 完成" % label


def ensure_runtime(variant: str = "", *, on_step: Optional[Callable[[str], None]] = None
                   ) -> Tuple[bool, str]:
    """把运行时装进 `{backend}/runtime` → ``(ok, detail)``。**永不抛。**

    前提：薄包已经解开（`{backend}/server/requirements.txt` 在）且**有解释器**——
    解释器可以来自随包的 `runtime/`，也可以由调用方（`backend_setup.start(python=…)`）指定。
    """
    v = str(variant or "cu126").strip()
    need = requirements(v)
    exe = backend_proc.python_exe()
    if not exe:
        return False, ("薄包里没有解释器，也没有可用的 Python —— 薄包应当随包带一份 CPython"
                       "（`runtime/Scripts/python.exe`）；没带的话先在目标机装 Python 3.11。")
    if not need["baseFileExists"]:
        return False, ("薄包还没解开：找不到 %s —— 先把薄包解到 %s，再点一次「起本机后端」。"
                       % (need["baseFile"], backend_setup.backend_root()))
    steps: List[str] = []
    if need["note"]:
        steps.append(need["note"])

    # ① torch + torchaudio：**同一条 pip、同一个 CUDA 索引**（纪律 1）
    torch_args = need["torch"] + ["--index-url", need["torchIndex"]]
    ok, detail = _run_pip(exe, torch_args, "装 torch/torchaudio（%s）" % need["torchIndex"],
                          on_step)
    if not ok:
        return False, detail

    # ② 其余依赖：走 PyPI 镜像（按顺序试，第一个不通换下一个）
    rest = need["extras"] + (["-r", need["baseFile"]] if need["baseFileExists"] else [])
    last = ""
    for index in need["pipIndexes"]:
        ok, detail = _run_pip(exe, rest + ["--index-url", index],
                              "装其余依赖（%s）" % index, on_step)
        if ok:
            last = detail
            break
        last = detail
    else:
        return False, last

    # ③ 装完**立刻自检**（纪律 2）：ABI + funasr 能不能 import
    if on_step:
        on_step("自检：torch/torchaudio 的 ABI + funasr 导入")
    abi = backend_env.check_torch_abi(exe)
    if not abi.get("ok"):
        return False, "装完了但 ABI 自检没过：%s" % (abi.get("error") or abi)
    try:
        probe = subprocess.run([exe, "-c", "import funasr; print(funasr.__version__)"],
                               capture_output=True, text=True, timeout=180)
        if probe.returncode != 0:
            tail = ((probe.stdout or "") + (probe.stderr or "")).strip().splitlines()[-4:]
            return False, ("torch/torchaudio 装好了，但 `import funasr` 失败：\n%s"
                           % "\n".join(tail))
    except Exception as e:                                        # pragma: no cover - 兜底
        return False, "自检 funasr 时炸了：%s" % e
    note = ("；%s" % abi.get("note")) if abi.get("note") else ""
    return True, ("运行时装好了：torch %s / torchaudio %s%s（日志：%s）"
                  % (abi.get("torch"), abi.get("torchaudio"), note, log_path()))


def plan(variant: str = "") -> Dict[str, Any]:
    """这一步**会做什么**（只读，给面板"计划"那格用）。"""
    need = requirements(variant)
    return {"variant": need["variant"], "torchIndex": need["torchIndex"],
            "torch": need["torch"], "extras": need["extras"],
            "pipIndexes": need["pipIndexes"], "note": need["note"],
            "runtimeDir": runtime_dir(), "log": log_path(),
            "python": backend_proc.python_exe(),
            "sourceReady": need["baseFileExists"],
            "approxDownloadGB": 3.0,   # 估：torch+torchaudio+nvidia 库（实测本机 6 GB 解压后）
            "headline": "从国内源装运行时（torch/torchaudio 走 %s，其余走清华/阿里 PyPI）"
                        % need["torchIndex"]}
