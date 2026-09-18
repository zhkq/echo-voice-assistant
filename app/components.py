# -*- coding: utf-8 -*-
"""components.py — 2.0 的组件内核（P2；对应 D22、D23）

背景：1.x 的"什么模型/引擎装了、缺什么、多大、从哪来"散在 `modelinfo.CATALOG`、
`setup.ps1`、各安装脚本和文档里，而且**平台差异靠人记**。2.0 要把"主包只有核心代码、
其余全是组件"落地（D22），所以先有一个统一的清单与判定层：

  * **清单（manifest）**：每条描述一个组件——id / 类别 / 名称 / 体积 / 支持哪些平台 /
    最低系统版本 / 是否必装 / 怎么获取 / 依赖谁 / 怎么探测就绪。
  * **平台过滤**：`platforms` + `min_os` 决定"这台机器上有没有这个组件"。
    例：`accel-cuda` 只出现在 win32/linux（mac 上不出现）；`agent-dsh` 要求 macOS ≥ 14.0。
  * **就绪探测**：用清单里声明的 `detect` 规则判断本机是否已具备（不下载任何东西）。

清单来源有两层，**目录层覆盖内置层**（同 id 覆盖字段，新 id 追加）：
  1. 内置（本文件）：覆盖 ECHO 当前已知的全部模型与引擎，保证"已有模型都能被识别为组件"；
  2. `<仓库根>/components/*.json`：发布方/用户追加或改写（离线包、内网镜像都放这里）。

**不改** `modelinfo` 与 `/api/models`：1.x 的面板与技能仍在用它们，2.0 期间两者并存
（P2 的验收里明确要求保留 `/api/models` 兼容）。
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
from typing import Dict, List, Optional, Tuple

#: 组件类别
KINDS = ("runtime", "accel", "stt", "diarize", "wake", "tts", "agent")

#: 必装组件（首装只装它，其余由向导逐项问，见 D23）
REQUIRED_IDS = ("runtime-core",)


# ---------------------------------------------------------------- 内置清单

def _builtin() -> List[dict]:
    """内置清单：与 ECHO 现状一一对应（模型面板里能看到的，这里都要有）。

    字段含义：
      id/kind/name/purpose  标识与展示
      size_mb               安装后大致占用（用于向导里的"要不要装"判断）
      platforms             支持的系统（app.platform.current() 的取值）
      min_os                各平台最低版本，如 {"macos": "14.0"}
      optional/required     required 的首装必装（D23）
      detect                就绪探测规则：{"path"|"any"|"python"|"exe"}
      source/how            获取方式（在线优先、离线兜底，D2）
      deps                  依赖的组件 id
    """
    return [
        dict(id="runtime-core", kind="runtime", name="运行时核心", required=True, optional=False,
             purpose="Python 运行时依赖（faster-whisper / funasr / sherpa-onnx / numpy…）",
             size_mb=100, platforms=["win32", "macos", "linux"], min_os={},
             detect={"python": "faster_whisper", "any": ["funasr", "sherpa_onnx"]},
             source="pypi", how="安装器准备：pip install -r requirements.txt（离线包可预置）"),
        dict(id="agent-dsh", kind="agent", name="DSH 智能体后端", optional=True, required=False,
             purpose="用官方 Python SDK 驱动技能（纪要生成、归档、自定义 skill）",
             size_mb=265, platforms=["win32", "macos", "linux"], min_os={"macos": "14.0"},
             detect={"python": "deepseek_harness"},
             source="pypi", how="pip install deepseek-harness-sdk（自带运行时，不需要系统 Node）"),
        dict(id="accel-cuda", kind="accel", name="CUDA 加速", optional=True, required=False,
             purpose="让转写/说话人分离跑在 N 卡上（3 倍以上速度）",
             size_mb=2500, platforms=["win32", "linux"], min_os={},   # mac 上不出现
             detect={"python": "torch", "exe": None},
             source="pypi", how="按显卡驱动安装 torch 的 CUDA 版；无 N 卡不要装"),
        dict(id="stt-sensevoice", kind="stt", name="SenseVoice 中文短命令", optional=True, required=False,
             purpose="语音命令与会议转写的默认引擎（自带标点）",
             size_mb=896, platforms=["win32", "macos", "linux"], min_os={},
             detect={"path": "sensevoice"}, source="modelscope", ref="iic/SenseVoiceSmall",
             how="面板下载或自行拷贝到 models/sensevoice；需 funasr + torch"),
        dict(id="stt-sherpa", kind="stt", name="sherpa-onnx 流式转写", optional=True, required=False,
             purpose="免 torch 的轻量流式转写（推荐组合之一，D1）",
             size_mb=189, platforms=["win32", "macos", "linux"], min_os={},
             detect={"path": "sherpa-onnx-streaming"}, source="modelscope",
             how="面板下载或自行拷贝到 models/sherpa-onnx-streaming"),
        dict(id="stt-whisper-tiny", kind="stt", name="Whisper tiny", optional=True, required=False,
             purpose="最小最快的档位（精度最低，适合纯英文短句）",
             size_mb=75, platforms=["win32", "macos", "linux"], min_os={},
             detect={"any": ["faster-whisper/tiny/model.bin", "hub/models--Systran--faster-whisper-tiny"]},
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/tiny"),
        dict(id="stt-whisper-base", kind="stt", name="Whisper base", optional=True, required=False,
             purpose="免 torch 的推荐组合之一（D1：sherpa-onnx + whisper-base）",
             size_mb=141, platforms=["win32", "macos", "linux"], min_os={},
             detect={"any": ["faster-whisper/base/model.bin", "hub/models--Systran--faster-whisper-base"]},
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/base"),
        dict(id="stt-whisper-small", kind="stt", name="Whisper small", optional=True, required=False,
             purpose="多语种转写（推荐组合之一，D1）",
             size_mb=464, platforms=["win32", "macos", "linux"], min_os={},
             detect={"any": ["faster-whisper/small/model.bin", "hub/models--Systran--faster-whisper-small"]},
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/small"),
        dict(id="stt-whisper-medium", kind="stt", name="Whisper medium", optional=True, required=False,
             purpose="介于 small 与 large-v3 之间的档位（内存换精度）",
             size_mb=1500, platforms=["win32", "macos", "linux"], min_os={},
             detect={"any": ["faster-whisper/medium/model.bin", "hub/models--Systran--faster-whisper-medium"]},
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/medium"),
        dict(id="stt-whisper-large-v3", kind="stt", name="Whisper large-v3", optional=True, required=False,
             purpose="精度优先的转写档位（显存/内存占用大）",
             size_mb=2950, platforms=["win32", "macos", "linux"], min_os={},
             detect={"any": ["faster-whisper/large-v3/model.bin",
                             "hub/models--Systran--faster-whisper-large-v3"]},
             source="hf-mirror", how="面板下载（约 3 GB）；建议配合 accel-cuda"),
        dict(id="stt-qwen3asr", kind="stt", name="Qwen3-ASR 0.6B + 强制对齐", optional=True, required=False,
             purpose="方言/口音更强的转写，并给出逐句时间对齐",
             size_mb=3600, platforms=["win32", "macos", "linux"], min_os={},
             detect={"python": "qwen_asr"}, source="modelscope", ref="Qwen/Qwen3-ASR-0.6B",
             how="scripts/install-qwen3asr.ps1（首次自动从 ModelScope 下载）"),
        dict(id="wake-kws", kind="wake", name="唤醒词 KWS", optional=True, required=False,
             purpose="离线关键词唤醒（默认不装：需常开麦克风，有隐私成本）",
             size_mb=40, platforms=["win32", "macos", "linux"], min_os={},
             detect={"path": "wakeword/kws-zh-en-3m"}, source="modelscope",
             how="自行获取 sherpa-onnx KWS 权重，按面板给的四个文件名放好"),
        dict(id="diarize-pyannote", kind="diarize", name="说话人分离（pyannote）", optional=True, required=False,
             purpose="会议里区分不同说话人（HF 上是 gated 模型）",
             size_mb=32, platforms=["win32", "macos", "linux"], min_os={},
             detect={"any": ["pyannote/pyannote-segmentation-3.0-local",
                             "pyannote/pyannote-wespeaker-local"]},
             source="hf-gated", never_ship=True,          # 许可证不允许再分发（D22 的例外）
             how="面板给授权链接与下载命令；同意条款后由用户自己拉取"),
    ]


# ---------------------------------------------------------------- 平台判定

def _parse_ver(text: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:3])


def _ver_tuple_ge(have: Tuple[int, ...], need: Tuple[int, ...]) -> bool:
    if not need:
        return True
    if not have:
        return True                    # 版本取不到时不拦（宁可多给一个组件，也不误判不可用）
    have = tuple(list(have) + [0] * (len(need) - len(have)))
    return have[:len(need)] >= need


def applicable(item: dict, *, platform: Optional[str] = None,
               os_version: Optional[Tuple[int, ...]] = None) -> Tuple[bool, str]:
    """这个组件在当前平台/版本上是否适用。返回 ``(ok, reason)``，reason 空 = 适用。"""
    from app import platform as plat

    p = platform or plat.current()
    token = plat.manifest_name(p)                     # 清单里用中立标记（macos / win32 / linux）
    plats = item.get("platforms") or []
    if plats and token not in plats:
        return False, "该组件不支持 %s" % token
    need = _parse_ver((item.get("min_os") or {}).get(token, ""))
    if need:
        have = os_version if os_version is not None else plat.os_version(p)
        if not _ver_tuple_has(have, need):
            return False, "需要 %s %s 以上" % (token, ".".join(str(x) for x in need))
    return True, ""


def _ver_tuple_has(have, need) -> bool:
    return _ver_tuple_ge(tuple(have or ()), tuple(need or ()))


# ---------------------------------------------------------------- 就绪探测

def _detect(item: dict) -> Optional[bool]:
    """按清单里的 ``detect`` 判断本机是否已具备。返回 True/False/None（无法判定）。"""
    from app import paths

    d = item.get("detect") or {}
    checks: List[bool] = []
    if d.get("path") or d.get("any"):
        models = paths.models_root()
        if d.get("path"):
            checks.append(os.path.isdir(os.path.join(models, d["path"])))
        for rel in (d.get("any") or []):
            if rel is None:
                continue
            p = os.path.join(models, rel.replace("/", os.sep))
            checks.append(os.path.isfile(p) or os.path.isdir(p))
    if d.get("python"):
        try:
            checks.append(importlib.util.find_spec(d["python"]) is not None)
        except Exception:
            checks.append(False)
    if d.get("exe"):
        checks.append(os.path.isfile(d["exe"]))
    if not checks:
        return None
    # any 语义：模型类组件常有多条落地路径，命中一条即算就绪；python 类同理
    return any(checks)


# ---------------------------------------------------------------- 清单装载

def components_dir(root: Optional[str] = None) -> str:
    from app import paths
    return os.path.join(root or paths.echo_root(), "components")


def load_manifests(root: Optional[str] = None) -> List[dict]:
    """内置清单 + ``<root>/components/*.json``（同 id 覆盖字段，新 id 追加）。"""
    items: Dict[str, dict] = {i["id"]: dict(i) for i in _builtin()}
    for path in sorted(glob.glob(os.path.join(components_dir(root), "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        for raw in (data if isinstance(data, list) else [data]):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            base = items.get(raw["id"], {})
            merged = dict(base)
            merged.update(raw)
            items[raw["id"]] = merged
    return [items[k] for k in sorted(items)]


def catalog(*, platform: Optional[str] = None, os_version: Optional[Tuple[int, ...]] = None,
            root: Optional[str] = None, include_blocked: bool = False) -> dict:
    """完整组件清单 + 平台适用性 + 就绪状态（面板「组件」页签的数据面）。"""
    from app import platform as plat

    p = platform or plat.current()
    ver = os_version if os_version is not None else plat.os_version(p)
    out = []
    for item in load_manifests(root):
        ok, why = applicable(item, platform=p, os_version=ver)
        if not ok and not include_blocked:
            continue
        row = dict(item)
        row["applicable"] = ok
        row["blockedReason"] = why
        row["required"] = bool(item.get("required")) or item["id"] in REQUIRED_IDS
        try:
            row["ready"] = _detect(item)
        except Exception:
            row["ready"] = None
        out.append(row)
    return {"platform": p, "osVersion": ".".join(str(x) for x in ver), "items": out}


def summary(**kw) -> dict:
    """给向导/体检用的一行汇总。"""
    data = catalog(**kw)
    items = data["items"]
    return {
        "platform": data["platform"],
        "total": len(items),
        "required": [i["id"] for i in items if i["required"]],
        "missingRequired": [i["id"] for i in items if i["required"] and i.get("ready") is False],
        "ready": [i["id"] for i in items if i.get("ready") is True],
    }
