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

#: 必装组件（首装就装这些）。
#:
#: 2026-10-31 用户拍板：客户端默认档**不做任何本地转写** —— 指令/回顾/会议统一走
#: 能力后端（echo-server + Qwen3-ASR），语音唤醒取消。所以必装只剩运行时核心；
#: 任何 stt-* / wake-* / diarize-* 组件都已从清单里删除（也不许再加回来）。
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
             purpose="Python 运行时依赖（服务 + 面板 + 音频 IO）；**不含任何本地 STT 引擎**",
             size_mb=120, platforms=["win32", "macos", "linux"], min_os={},
             # 就绪判据必须与 requirements-core.txt **一致**。2026-10-31 起那份清单
             # 只装服务骨架与音频 IO（sherpa-onnx/modelscope/pypinyin 已删）。
             detect={"python": "fastapi"},
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
                 "`{echoBase}/dsh/app`**（新布局；老装机仍在 `<安装目录>/harness/dsh`，"
                 "冷启动约 10 秒），命令记在设置的 harnessCommand 里。"
                 "右边这条是没装成时的兜底（走 npx，首次要多等 1-2 分钟）"),
        dict(id="accel-cuda", kind="accel", name="CUDA 加速（客户端不再需要）", optional=True, required=False,
             purpose="已退役：客户端不跑本地转写/分离模型；CUDA 只在那台能力后端上需要",
             size_mb=2500, platforms=["win32", "linux"], min_os={},   # mac 上不出现
             detect={"python": "torch", "exe": None},
             source="pypi", how="客户端默认档不需要它；不要为 ECHO 客户端装 torch"),
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


def _module_ok(name: str) -> bool:
    """pip 依赖在不在 —— **以 import 为准**（与 `install_state._module_ok()` 同一判据）。"""
    if not name:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _engine_spec(model_id: str) -> dict:
    """模型 id → 引擎规格（stt 组件的 pip 依赖从这里取，不在本文件里再抄一份）。"""
    try:
        from app import install_state
        return install_state.engine_spec(model_id)
    except Exception:
        return {}


def _detect(item: dict) -> Optional[bool]:
    """按清单里的 ``detect`` 判断本机是否已具备。返回 True/False/None（无法判定）。

    **有 ``model_id`` 的组件直接问 `modelinfo`**（2026-09-19 合并「模型/组件」两个页签时定的）：
    同一份权重原来有两套判据（组件清单写死路径、modelinfo 各写一个 ready 函数），
    两边一旦分叉就会出现"组件说已装、模型说没装"。现在模型类组件只有一个判据来源。

    **但"模型在"不等于"能用"**：stt 引擎还要 pip 包（sherpa-onnx / funasr /
    faster-whisper…）。2026-09-23 实测事故：稳定版 `models/sherpa-onnx-streaming` 齐全、
    面板显示「已就绪」，而 runtime-core 里没有 `sherpa_onnx` —— 每次语音指令都在
    转写处抛 ModuleNotFoundError，面板一声不响。所以模型类组件**两个条件都要过**。

    **``{"setting": key}``** 用于"本机服务"类组件（DSH Desktop / 独立 harness）：把配置里的值
    当服务地址探一下 —— 这样它就绪判据跟 ECHO 实际连的地址一致，配置改了判据跟着变。
    值可以是完整 URL，也可以只是**端口号**（独立 harness 存的是 `harnessPort`）。
    """
    from app import paths

    mid = item.get("model_id")
    if mid:
        try:
            from app import modelinfo
            ready = modelinfo.ready(mid)
        except Exception:
            return None
        spec = _engine_spec(mid)
        if ready and not _module_ok(str(spec.get("module") or "")):
            return False
        return ready
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
        checks.append(_module_ok(str(d["python"])))
    if d.get("exe"):
        checks.append(os.path.isfile(d["exe"]))
    if not checks:
        return None
    # any 语义：模型类组件常有多条落地路径，命中一条即算就绪；python 类同理
    return any(checks)


def not_ready_reason(item: dict) -> Tuple[str, str]:
    """「没装」的那一行**到底缺什么、下一步干什么**：返回 ``(kind, 给人看的一句话)``。

    kind ∈ ``""``（说不出）/ ``"python"``（缺 pip 包）/ ``"model"``（缺模型文件）——
    面板据此把徽标从「⬇ 未安装」换成准确的「⚠ 缺依赖」（模型其实已经下好了）。

    文案要**能照着做**（同事 2026-09-25 实测）：那一刻用户看到的是"缺依赖"三个字，
    不知道该装什么、更不知道装完还要**回来再点一次下载**。所以原因后面直接跟
    **安装命令**与一句下一步；面板要单独一个"下一步"字段时用 :func:`not_ready_next_step`。
    """
    mid = item.get("model_id")
    if not mid:
        return "", ""
    spec = _engine_spec(mid)
    mod = str(spec.get("module") or "")
    if not _module_ok(mod):
        cmd = _install_command(item)
        reason = "缺 Python 依赖 %s —— 模型下好了也转写不了；第一步先装依赖：%s" % (mod, cmd or ("pip install %s" % mod))
        reason += "；第二步回到这里再点一次「下载」"
        return "python", reason
    try:
        from app import modelinfo
        if modelinfo.ready(mid) is False:
            return "model", "模型文件还没下载（依赖已就绪）—— 再点一次「下载」把模型文件拉下来"
    except Exception:
        pass
    return "", ""


def not_ready_next_step(item: dict, kind: str = "") -> str:
    """面板要的**「下一步点什么」**（一句话；说不出返回空串）。

    与 :func:`not_ready_reason` 分开：原因会拼进一段红字里，而这一条是**动作**，
    面板可以拿它渲染按钮/提示（同事 2026-09-25：装完依赖后面板停在"模型文件还没下载"，
    没有任何指引，用户以为卡死了）。
    """
    kind = kind or not_ready_reason(item)[0]
    if kind == "python":
        return "先复制执行安装命令把依赖装上，再回来点一次「下载」"
    if kind == "model":
        return "再点一次「下载」，把模型文件拉下来"
    return ""


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


def install_command_for_model(model_id: str) -> str:
    """按**模型 id** 找到对应组件那条 pip 安装命令；找不到返回空串。

    为什么要有它（同事 2026-09-25 实测）：点「下载」被"缺依赖"挡住时，用户需要的是
    **那条能装的命令**，而不是一句"缺 funasr"。命令只能由组件清单的 `pkg` 拼出来
    （`:func:`_install_command`` 是唯一出处），所以 modelinfo / install_state 都从这里取，
    不许再各抄一份"包名 → 命令"的映射 —— 那个 bug 就是抄出来的（一份用了 `sys.executable`、
    另一份还是裸 `python`）。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return ""
    for item in load_manifests():
        if item.get("model_id") == mid:
            return _install_command(item)
    return ""


def _install_command(item: dict) -> str:
    """给需要 pip 的组件拼一条**可直接粘贴执行**的命令（用当前解释器）。

    为什么带上解释器全路径：用户实测问过"这条命令我应该在哪个目录执行、cmd 还是 PowerShell"——
    pip 不关心当前目录（在哪儿跑都一样），真正会出错的是**用哪个解释器**：
    裸 `pip install x` 会装到 PATH 上第一个 Python 里，ECHO 自己的 venv 根本看不到，
    于是面板永远显示"未安装"。所以命令一律写成 `<本机解释器> -m pip install …`。

    "本机解释器怎么算"（`pythonw.exe` → `python.exe`、含空格时怎么加引号、PowerShell 里
    要不要 `&`）收在 `app/interpreter.py`，本文件只负责"装哪个包"。
    """
    if not (item.get("pkg") or item.get("requirements")):
        return ""
    from app import interpreter
    if item.get("pkg"):
        targets = [str(item["pkg"])]
    else:
        from app import paths
        targets = ["-r", os.path.join(paths.echo_root(), "requirements.txt")]
    return interpreter.pip_install_command(*targets)


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
        # 「没就绪」要能说出缺什么：只说"未安装"会把用户引到下载模型上，
        # 而真正缺的可能是 pip 包（2026-09-23 事故）。面板据 readyKind 换徽标措辞。
        row["readyKind"] = ""
        row["readyReason"] = ""
        row["readyNextStep"] = ""
        if row["ready"] is False:
            try:
                row["readyKind"], row["readyReason"] = not_ready_reason(item)
                row["readyNextStep"] = not_ready_next_step(item, row["readyKind"])
            except Exception:
                row["readyKind"], row["readyReason"], row["readyNextStep"] = "", "", ""
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
