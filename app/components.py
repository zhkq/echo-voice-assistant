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
      detect                就绪探测规则：{"path"|"any"|"python"|"exe"|"setting"}
                            有 ``model_id`` 时**不用它** —— 直接问 `modelinfo`（见 `_detect`）
                            有 ``setting`` 时把它读成 URL 做一次短超时探测（本机服务类组件）
      model_id              对应的模型清单 id（`app/modelinfo.py`）：有它就能在面板里
                            直接下载/复制命令（`/api/models/download` 认这个 id），
                            也保证"装没装"只有一个判据（2026-09-19 合并「模型/组件」时加的）
      pkg / requirements    需要 pip 装的东西（二选一）：`catalog()` 会用**当前解释器**
                            拼出一条可直接粘贴执行的命令（见 `_install_command`）
      source/how            获取方式（在线优先、离线兜底，D2）
      deps                  依赖的组件 id
    """
    return [
        dict(id="runtime-core", kind="runtime", name="运行时核心", required=True, optional=False,
             purpose="Python 运行时依赖（faster-whisper / funasr / sherpa-onnx / numpy…）",
             size_mb=100, platforms=["win32", "macos", "linux"], min_os={},
             detect={"python": "faster_whisper", "any": ["funasr", "sherpa_onnx"]},
             source="pypi", requirements=True,
             how="装 ECHO 的依赖清单（命令已带上本机解释器路径，粘到终端即可）"),
        # 2026-09-19 更正：这条原来写的是「pip install deepseek-harness-sdk（265 MB）」，源自
        # REFACTOR-PLAN 的 S1 试跑。**现在那个包在 PyPI 上已经不存在了**（同名新包
        # `deepseek-harness` 是另一个项目：DeepSeek V4 的 API 客户端，与 DSH 桌面端无关），
        # 而且 ECHO 2.0 **根本不用 SDK** —— `app/agents/dsh_agent.py` 用标准库 urllib 直连
        # DSH Desktop 的本机 HTTP JSON-RPC（`dshBaseUrl`，默认 127.0.0.1:43120）。
        # 于是这条改成"本机服务"：装桌面客户端并保持运行即可，就绪判据 = 那个地址通不通。
        dict(id="agent-dsh", kind="agent", name="DSH Desktop 桌面版（本机服务）", optional=True, required=False,
             purpose="ECHO 通过本机 HTTP JSON-RPC 使用 DSH（会话 / 纪要 / 模型注册）；不需要 Python SDK",
             size_mb=0, platforms=["win32", "macos", "linux"], min_os={"macos": "14.0"},
             detect={"setting": "dshBaseUrl"}, service=True,
             source="manual",
             how="装 DSH Desktop 客户端并让它保持运行即可；地址见 设置 → 智能体 → DSH 服务地址。"
                 "它与下面那条「标准版 harness」**二选一**：没选中的那条显示「未运行 / 未使用」是正常的，"
                 "不代表坏了"),
        # 2026-09-19 用户要求："运行环境哪里要增加独立 DSH" —— 上面那条是桌面客户端，
        # 这条是**独立发行版**（npm @deepseek-ai/dsh）：同样只需"服务在跑"，
        # 但多一条路 —— ECHO 能把它作为自己的子进程随自己拉起（选中该智能体即可）。
        dict(id="agent-harness", kind="agent", name="标准版 harness（DeepSeek Harness · 本机服务）",
             optional=True, required=False,
             purpose="npm 包 @deepseek-ai/dsh 的 web 服务；不装 DSH Desktop 也能用它干活",
             size_mb=0, platforms=["win32", "macos", "linux"], min_os={},
             detect={"setting": "harnessPort"},          # 端口（或地址）通不通 = 就绪
             service=True,
             source="manual",
             command="npx -y @deepseek-ai/dsh web --port 43199 --no-open",
             command_label="复制启动命令",
             how="在 设置 → 智能体 里选中它，ECHO 会自动拉起；安装技能会把它**永久装到 "
                 "<安装目录>/harness/dsh**（冷启动约 10 秒），命令记在设置的 harnessCommand 里。"
                 "右边这条是没装成时的兜底（走 npx，首次要多等 1-2 分钟）"),
        dict(id="accel-cuda", kind="accel", name="CUDA 加速", optional=True, required=False,
             purpose="让转写/说话人分离跑在 N 卡上（3 倍以上速度）",
             size_mb=2500, platforms=["win32", "linux"], min_os={},   # mac 上不出现
             detect={"python": "torch", "exe": None},
             source="pypi", how="按显卡驱动安装 torch 的 CUDA 版（面板不代装）；无 N 卡不要装"),
        dict(id="stt-sensevoice", kind="stt", name="SenseVoice 中文短命令", optional=True, required=False,
             purpose="语音命令与会议转写的默认引擎（自带标点）",
             size_mb=896, platforms=["win32", "macos", "linux"], min_os={},
             model_id="sensevoice", source="modelscope", ref="iic/SenseVoiceSmall",
             how="面板下载或自行拷贝到 models/sensevoice；需 funasr + torch"),
        dict(id="stt-sherpa", kind="stt", name="sherpa-onnx 流式转写", optional=True, required=False,
             purpose="免 torch 的轻量流式转写（推荐组合之一，D1）",
             size_mb=189, platforms=["win32", "macos", "linux"], min_os={},
             model_id="sherpa", source="modelscope",
             how="面板下载或自行拷贝到 models/sherpa-onnx-streaming"),
        dict(id="stt-whisper-tiny", kind="stt", name="Whisper tiny", optional=True, required=False,
             purpose="最小最快的档位（精度最低，适合纯英文短句）",
             size_mb=75, platforms=["win32", "macos", "linux"], min_os={},
             model_id="whisper-tiny",
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/tiny"),
        dict(id="stt-whisper-base", kind="stt", name="Whisper base", optional=True, required=False,
             purpose="免 torch 的推荐组合之一（D1：sherpa-onnx + whisper-base）",
             size_mb=141, platforms=["win32", "macos", "linux"], min_os={},
             model_id="whisper-base",
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/base"),
        dict(id="stt-whisper-small", kind="stt", name="Whisper small", optional=True, required=False,
             purpose="多语种转写（推荐组合之一，D1）",
             size_mb=464, platforms=["win32", "macos", "linux"], min_os={},
             model_id="whisper-small",
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/small"),
        dict(id="stt-whisper-medium", kind="stt", name="Whisper medium", optional=True, required=False,
             purpose="介于 small 与 large-v3 之间的档位（内存换精度）",
             size_mb=1500, platforms=["win32", "macos", "linux"], min_os={},
             model_id="whisper-medium",
             source="hf-mirror", how="面板下载或从源机拷贝 models/faster-whisper/medium"),
        dict(id="stt-whisper-large-v3", kind="stt", name="Whisper large-v3", optional=True, required=False,
             purpose="精度优先的转写档位（显存/内存占用大）",
             size_mb=2950, platforms=["win32", "macos", "linux"], min_os={},
             model_id="whisper-large-v3",
             source="hf-mirror", how="面板下载（约 3 GB）；建议配合 accel-cuda"),
        dict(id="stt-qwen3asr", kind="stt", name="Qwen3-ASR 0.6B + 强制对齐", optional=True, required=False,
             purpose="方言/口音更强的转写，并给出逐句时间对齐",
             size_mb=3600, platforms=["win32", "macos", "linux"], min_os={},
             model_id="qwen3asr", source="modelscope", ref="Qwen/Qwen3-ASR-0.6B",
             how="scripts/install-qwen3asr.ps1（首次自动从 ModelScope 下载）"),
        dict(id="wake-kws", kind="wake", name="唤醒词 KWS", optional=True, required=False,
             purpose="离线关键词唤醒（默认不装：需常开麦克风，有隐私成本）",
             size_mb=40, platforms=["win32", "macos", "linux"], min_os={},
             model_id="kws", source="modelscope",
             how="自行获取 sherpa-onnx KWS 权重，按面板给的四个文件名放好"),
        dict(id="diarize-pyannote", kind="diarize", name="说话人分离（pyannote）", optional=True, required=False,
             purpose="会议里区分不同说话人（三件套；HF 上是 gated，ModelScope 上有同名开放镜像）",
             size_mb=32, platforms=["win32", "macos", "linux"], min_os={},
             model_id="pyannote",
             source="modelscope", never_ship=True,        # 权重仍不随包分发（只在你机器上下载）
             how="点下载即可（ModelScope 同名仓库，匿名可下）。请自行确认 pyannote 的使用条款 ——"
                 "官方在 HF 上要求先同意条件，走镜像等于跳过那一步。"),
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

def _probe_url(url: str, timeout: float = 0.8) -> bool:
    """本机服务是否在监听：任何 HTTP 响应（含 401/404）都算"活着"，连不上才算没装。"""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True          # 有响应 = 服务在跑（只是这个路径没权限/不存在）
    except Exception:
        return False


def _detect(item: dict) -> Optional[bool]:
    """按清单里的 ``detect`` 判断本机是否已具备。返回 True/False/None（无法判定）。

    **有 ``model_id`` 的组件直接问 `modelinfo`**（2026-09-19 合并「模型/组件」两个页签时定的）：
    同一份权重原来有两套判据（组件清单写死路径、modelinfo 各写一个 ready 函数），
    两边一旦分叉就会出现"组件说已装、模型说没装"。现在模型类组件只有一个判据来源。

    **``{"setting": key}``** 用于"本机服务"类组件（DSH Desktop / 独立 harness）：把配置里的值
    当服务地址探一下 —— 这样它就绪判据跟 ECHO 实际连的地址一致，配置改了判据跟着变。
    值可以是完整 URL，也可以只是**端口号**（独立 harness 存的是 `harnessPort`）。
    """
    from app import paths

    mid = item.get("model_id")
    if mid:
        try:
            from app import modelinfo
            return modelinfo.ready(mid)
        except Exception:
            return None
    d = item.get("detect") or {}
    if d.get("setting"):
        try:
            from app.config import settings as _s
            raw = str(_s.get(d["setting"], "") or "").strip()
        except Exception:
            return None
        # 只给端口（如 43199）时补成 http://127.0.0.1:<port>
        url = ""
        if raw.isdigit():
            url = "http://127.0.0.1:%s" % raw
        elif raw:
            url = raw if "://" in raw else "http://%s" % raw
        return _probe_url(url) if url else None
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


def _agent_active(component_id: str):
    """这个"本机服务"式智能体是不是用户**当前选中**的那个；认不出返回 None（不猜）。

    为什么要有这个字段（同事 2026-09-21 反馈）：两个智能体是**二选一**，选标准版时
    DSH Desktop 那条自然是 offline —— 面板只写"未运行"，用户读成"坏了/没装好"。
    有了 `active`，面板才能说清"这条不是坏了，是你没在用"。
    """
    want = {"agent-dsh": "dsh", "agent-harness": "harness"}.get(component_id)
    if not want:
        return None
    try:
        from app.config import settings
        return str(settings.get("agentBackend", "") or "").strip() == want
    except Exception:                                 # noqa: BLE001 —— 读不到就不表态
        return None


def _install_command(item: dict) -> str:
    """给需要 pip 的组件拼一条**可直接粘贴执行**的命令（用当前解释器）。

    为什么带上解释器全路径：用户实测问过"这条命令我应该在哪个目录执行、cmd 还是 PowerShell"——
    pip 不关心当前目录（在哪儿跑都一样），真正会出错的是**用哪个解释器**：
    裸 `pip install x` 会装到 PATH 上第一个 Python 里，ECHO 自己的 venv 根本看不到，
    于是面板永远显示"未安装"。所以命令一律写成 `<本机解释器> -m pip install …`。
    路径含空格时加引号（此时 PowerShell 还需要在前面加 `&`，写在 how 里提醒）。
    """
    if not (item.get("pkg") or item.get("requirements")):
        return ""
    from app import paths
    py = ""
    try:
        import sys
        py = sys.executable or ""
    except Exception:
        py = ""
    if not py:
        return ""
    # ECHO 服务自己是 pythonw.exe（无控制台）——拿它跑 pip 会看不到任何输出、像是卡住。
    # 同一目录下的 python.exe 才是该用的那个（实测：面板会把 sys.executable 原样吐出来）。
    if os.path.basename(py).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(py), "python.exe")
        if os.path.isfile(cand):
            py = cand
    py_arg = '"%s"' % py if " " in py else py
    if item.get("pkg"):
        tail = str(item["pkg"])
    else:
        req = os.path.join(paths.echo_root(), "requirements.txt")
        tail = '-r "%s"' % req
    return "%s -m pip install %s" % (py_arg, tail)


def catalog(*, platform: Optional[str] = None, os_version: Optional[Tuple[int, ...]] = None,
            root: Optional[str] = None, include_blocked: bool = False) -> dict:
    """完整组件清单 + 平台适用性 + 就绪状态 + 可执行安装命令（面板「能力」页签的数据面）。"""
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
        # 清单里可以自带 command（如独立 harness 的启动命令）；没有 pkg/requirements 时别把它清空
        row["command"] = _install_command(item) or str(item.get("command") or "")
        row["command_label"] = str(item.get("command_label") or "下载命令")
        try:
            row["ready"] = _detect(item)
        except Exception:
            row["ready"] = None
        if item.get("kind") == "agent":
            row["active"] = _agent_active(item["id"])
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
