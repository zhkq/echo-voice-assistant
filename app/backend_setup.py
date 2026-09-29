# -*- coding: utf-8 -*-
"""「起本机后端」的**配置与编排**（客户端简化第 3 步 · 批 1c）。

职责边界（与 1a / 1b 的分工）
----------------------------
    1a ``app/backend_pid.py``   归属记录：pid 文件在哪、那条 pid 还活着吗
    1b ``app/backend_proc.py``  进程层：起 / 停 / 端口占用者（**只管进程，不写配置**）
    1c **本模块**               编排：写 ``server.yaml`` → 起 → 等本机配对文件 → 配对
    1d（未做）                  设置项 + HTTP 端点 + 面板按钮/进度

所以这里只做四件事，而且每一步都能单独调用、单独验：

    ``configure()``           写 ``{echoBase}/backend/server.yaml``（只绑回环 + 本机自配对）
    ``launch()``              用 1b 的 ``spawn()`` 把它起起来（cwd = 后端自己的家）
    ``wait_for_pair_file()``  轮询 ``{state_root}/local-pair.json``（后端启动时写）
    ``pair_if_needed()``      已经是同一个回环后端就**跳过**，否则走既有那条 ``pairing.pair()``

``start()`` 把这四步串起来给面板用（逐步 ok/人话），失败**立刻停**、不继续往下做。

## 三条纪律（都有代价，别改成"看着更省事"的写法）

1. **只绑回环**：``listen: 127.0.0.1:8900`` + ``admin_listen: 127.0.0.1:8901``。
   这是"本机后端"的定义（实施方案 §1B）：后端只服务这台机器，音频不出机器。
   改一处不改另一处 = 管理面仍对网段开着（实施方案 §0 记过这个坑）。
   ⚠️ 走到容器路线时**还要**改端口发布（``127.0.0.1:8900:8900``）—— 那是 1d/4 的事。
2. **``jwt_secret`` 生成一次就持久化，永不重生成**：它是"所有客户端凭据的根"，
   重生成等于让配过的机器全部 401，而现象很难联想到"密钥每次都是新的"
   （服务端自己也因此**拒绝**随机生成，见 ``server/settings.py`` 的 auth 段）。
   本模块的取用顺序：**已有 ``server.yaml`` 里那份** > ``jwt-secret.txt`` > 新生成。
3. **失败绝不悄悄改设置**：``start()`` 任何一步失败就返回，**不**去动
   ``capabilityMeetingAsrBackend`` 这类"以后按哪条路转写"的设置 ——
   "先把开关拨过去、结果后端没起来"会让每一场会议都进 ``waiting-backend``。

## 与设置项的关系

``capabilityLocalPairPath``（隐藏设置）由 ``configure()`` **自动写**：路径是客户端自己
算出来的（``{echoBase}/backend/state/local-pair.json``），不用问人。但如果用户**手工填过**
一个**确实存在**的路径，就沿用他的，并在人话里说明（手工部署的状态卷可能挂在别处）。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import backend_pid, backend_proc, paths, platform
from app.capabilities import credentials as cred
from app.capabilities import pairing

#: 配置文件与两个状态目录的名字（都在后端自己的家里，见 ``paths.backend_root()``）。
CONFIG_FILENAME = "server.yaml"
SECRET_FILENAME = "jwt-secret.txt"

#: 只绑回环。见模块头第 1 条纪律。
DEFAULT_LOOPBACK = "127.0.0.1"
#: 等"后端把本机配对文件写出来"的上限（秒）与轮询间隔。
PAIR_FILE_TIMEOUT = 60.0
PAIR_FILE_POLL = 1.0
#: 配对时容忍"服务还没开始接连接"的时长（秒）。**这不是可有可无的重试**：uvicorn 是
#: *先跑 lifespan、后绑端口*，而本机配对文件正是在 lifespan 里写的 —— 所以"文件在了"
#: 与"能连上"之间有几十毫秒到几秒的窗口（2026-09-29 真机冒烟实测：文件 23:37:12.346
#: 写好，此刻 8900 还没 bind，立刻配对拿到的是 `WinError 10061 拒绝连接`）。
PAIR_READY_TIMEOUT = 20.0
#: 备份文件名里那个时间戳的格式（``server.yaml.bak-20260929-2330``）。
BACKUP_STAMP = "%Y%m%d-%H%M"


# ---------------------------------------------------------------- 位置

def backend_root() -> str:
    """后端自己的家（转发 ``paths.backend_root()``：测试只打桩这一个）。"""
    return paths.backend_root()


def config_path() -> str:
    return os.path.join(backend_root(), CONFIG_FILENAME)


def secret_path() -> str:
    return os.path.join(backend_root(), SECRET_FILENAME)


def state_root() -> str:
    """耐久状态（鉴权库 + 本机配对文件）。**故意与 tmp 分开**，见服务端配置里的说明。"""
    return os.path.join(backend_root(), "state")


def tmp_root() -> str:
    """临时目录（随便删）。"""
    return os.path.join(backend_root(), "tmp")


def cache_root() -> str:
    """下载/缓存目录：同时是子进程的 ``MODELSCOPE_CACHE`` 与 ``HOME``。

    为什么必须给：ModelScope/transformers 默认写 ``~/.cache``，而服务端容器/服务进程
    常常没有像样的家目录 —— 缺一个可写缓存时模型下载失败，表现是"每个 ``/v1/asr``
    都 503 model_failed"（实施方案 §1A 记过这条）。
    """
    return os.path.join(backend_root(), "cache")


def pair_file_path() -> str:
    """后端启动时会写的本机配对文件：``{state_root}/local-pair.json``。

    与服务端 ``server/localpair.py::path()`` 同一个规则（``auth.db`` 没显式指定时
    就是 ``server.state_root``），文件名共用 ``pairing.LOCAL_PAIR_FILENAME`` ——
    名字只有一处定义，不会两边漂。
    """
    return os.path.join(state_root(), pairing.LOCAL_PAIR_FILENAME)


def loopback_base_url(port: int = backend_proc.DEFAULT_PORT) -> str:
    return "http://%s:%d" % (DEFAULT_LOOPBACK, int(port))


# ---------------------------------------------------------------- jwt_secret（永不重生成）

def _read_config_file() -> Dict[str, Any]:
    """读现有 ``server.yaml``（不存在/坏了/没有 yaml 库都返回 ``{}``，永不抛）。"""
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return {}
    try:
        import yaml as pyyaml
        data = pyyaml.safe_load(text) or {}
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        pass
    # 没有 yaml 库（或文件被写坏）时，至少把**不能丢的那一项**抠出来：
    # 丢了它等于重生成密钥，所有配过的客户端一起 401。
    return _regex_config(text)


def _regex_config(text: str) -> Dict[str, Any]:
    """兜底解析：只认本模块自己写出去的那几个键的缩进结构。"""
    import re
    out: Dict[str, Any] = {"server": {}, "auth": {}, "models": {}, "tmp": {}}
    section = ""
    for line in str(text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line.strip())
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if indent == 0:
            section = key
            if raw:
                out[key] = _scalar_value(raw)
            elif section not in out or not isinstance(out.get(section), dict):
                out[section] = {}
            continue
        if isinstance(out.get(section), dict):
            out[section][key] = _scalar_value(raw)
    return out


def _scalar_value(raw: str) -> Any:
    """把 YAML 标量文本还原成 Python 值（够用即可：本模块写出去的就是这几种）。"""
    text = str(raw or "").strip()
    for cut in (" #", "\t#"):
        if cut in text:
            text = text.split(cut, 1)[0].strip()
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text in ("", "null", "~", "''", '""'):
        return ""
    if (text.startswith('"') and text.endswith('"')) or \
            (text.startswith("'") and text.endswith("'")):
        try:
            return json.loads(text) if text.startswith('"') else text[1:-1].replace("''", "'")
        except Exception:
            return text[1:-1]
    try:
        return int(text)
    except ValueError:
        return text


def _secret_from_text(text: str) -> str:
    """从 yaml 原文里抠 ``auth.jwt_secret``（yaml 库缺失/文件半坏时的兜底）。"""
    import re
    m = re.search(r"^\s*jwt_secret:\s*(.+?)\s*$", str(text or ""), re.M)
    if not m:
        return ""
    value = _scalar_value(m.group(1))
    return str(value or "").strip()


def existing_secret() -> str:
    """现在这份配置里的 jwt_secret（没有/读不出 = 空串）。**只读，不生成**。"""
    path = config_path()
    text = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        text = ""
    if text:
        try:
            auth = _read_config_file().get("auth")
            found = str((auth or {}).get("jwt_secret") or "") if isinstance(auth, dict) else ""
        except Exception:
            found = ""
        return found.strip() or _secret_from_text(text)
    try:
        with open(secret_path(), "r", encoding="utf-8-sig") as fh:
            return (fh.read() or "").strip()
    except Exception:
        return ""


def ensure_secret() -> Tuple[str, str]:
    """拿到该用的 jwt_secret → ``(secret, note)``。**已有的一律沿用**（见模块头第 2 条）。"""
    current = existing_secret()
    if current:
        return current, "沿用已有的 jwt_secret（未重新生成）"
    value = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(secret_path()), exist_ok=True)
        with open(secret_path(), "w", encoding="utf-8") as fh:
            fh.write(value)
        try:
            platform.restrict_file(secret_path())      # 它是凭据的根，按凭据对待
        except Exception:
            pass
    except Exception as e:
        # 写不进密钥文件不算致命：密钥仍在 yaml 里（下一项），只是"下次要重生成"。
        return value, "新生成 jwt_secret（写入 %s 失败：%s）" % (secret_path(), e)
    return value, "新生成 jwt_secret 并存到 %s" % secret_path()


# ---------------------------------------------------------------- 配置渲染

def _yaml_scalar(value: Any) -> str:
    """把值渲染成 YAML 标量。字符串走 ``json.dumps`` —— **反斜杠安全**。

    为什么不用单引号裸写：Windows 路径里有反斜杠，而 YAML 的**双引号**标量会解释转义
    （``C:\\echo`` 里的 ``\\e`` 不是合法转义，直接解析失败）。JSON 的转义规则是 YAML
    双引号标量的子集，所以 ``json.dumps`` 出来的东西既是合法 JSON、也是合法 YAML。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in str(text).splitlines())


def render_config(*, port: int = backend_proc.DEFAULT_PORT,
                  admin_port: int = backend_proc.DEFAULT_ADMIN_PORT,
                  jwt_secret: str = "", device: str = "cuda",
                  vram_budget_mb: int = 0,
                  models_root_path: str = "",
                  specs: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    """渲染 ``server.yaml`` 全文（纯函数：不读不写盘，方便用例比对内容）。"""
    lines: List[str] = [
        "# ECHO 能力后端 —— 由客户端「帮我起本机后端」自动生成（app/backend_setup.py）。",
        "#",
        "# 本机形态：**只绑回环**（这台机器自己用）+ 本机自配对（后端启动写 local-pair.json，",
        "# 客户端读它自动配对，不用抄配对码）。状态/临时/缓存都在 %s 下，" % backend_root(),
        "# 升级客户端代码树时**不动**它们（auth.db 与 jwt_secret 是配过对的根）。",
        "#",
        "# 这个文件可以手工改；再点一次「起本机后端」会按这里重新生成 ——",
        "# **jwt_secret 永远不会被重新生成**（重生成 = 所有客户端一起 401）。",
        "server:",
        "  id: %s" % _yaml_scalar("echo-local"),
        "  # 只服务本机：能力面与运维面都只听回环（改一处不改另一处 = 管理面仍对网段开着）",
        "  listen: %s" % _yaml_scalar("%s:%d" % (DEFAULT_LOOPBACK, int(port))),
        "  admin_listen: %s" % _yaml_scalar("%s:%d" % (DEFAULT_LOOPBACK, int(admin_port))),
        "  instance_id: %s" % _yaml_scalar("local"),
        "  # 0 = 不限制（按卡校准的预算由「起后端」的探测步骤给，小卡上必须填）",
        "  vram_budget_mb: %s" % _yaml_scalar(int(vram_budget_mb)),
        "  # 耐久状态（鉴权库 + 本机配对文件）：与 tmp 分开，见服务端配置里的理由",
        "  state_root: %s" % _yaml_scalar(state_root()),
        "  # 本机自配对：**这条是「同机不该让人抄配对码」的全部依据**",
        "  local_pair: true",
        "  # 本机后端对外公布的地址就是回环（它只监听回环，别的机器也连不上）",
        "  advertised_host: %s" % _yaml_scalar(DEFAULT_LOOPBACK),
        "  tls:",
        "    certfile: %s" % _yaml_scalar(""),
        "    keyfile: %s" % _yaml_scalar(""),
        "tmp:",
        "  root: %s" % _yaml_scalar(tmp_root()),
        "models:",
        "  # 指客户端那份模型库（**不复制**）：权重已经在机器上了就别再下一遍",
        "  root: %s" % _yaml_scalar(models_root_path or paths.models_root()),
        "  device: %s" % _yaml_scalar(device),
    ]
    if specs:
        try:
            import yaml as pyyaml
            body = pyyaml.safe_dump(list(specs), allow_unicode=True, sort_keys=False,
                                    default_flow_style=False).rstrip()
            lines.append("  specs:")
            lines.append(_indent(body, 4))
        except Exception:
            # 没有 yaml 库：JSON 是 YAML 的子集，一样能被服务端读进去。
            lines.append("  specs: %s" % json.dumps(list(specs), ensure_ascii=False))
    else:
        lines.append("  # 不写 specs = 用服务端出厂清单（server/engines.py:default_specs）")
    lines += [
        "auth:",
        "  # 本机后端也要鉴权：配对换来 client_id/secret，短期 JWT 调用（凭据落客户端）",
        "  enabled: true",
        "  mode: %s" % _yaml_scalar("jwt"),
        "  jwt_secret: %s" % _yaml_scalar(jwt_secret),
        "  # 本机配对文件里那张码的有效期（7 天）；后端每次启动重发一张",
        "  local_pair_ttl_s: 604800",
        "",
    ]
    return "\n".join(lines)


def configure(*, port: int = backend_proc.DEFAULT_PORT,
              admin_port: int = backend_proc.DEFAULT_ADMIN_PORT,
              device: str = "cuda", vram_budget_mb: int = 0,
              specs: Optional[Sequence[Dict[str, Any]]] = None,
              models_root_path: str = "",
              write_setting: bool = True) -> Tuple[bool, str, Dict[str, Any]]:
    """写 ``server.yaml``（幂等）→ ``(ok, 人话, info)``。

    * 目录（state / tmp / cache）一并建好 —— 后端要求它们可写；
    * 已有配置与将要写的内容**不一样**时，先备份成 ``server.yaml.bak-<stamp>``
      （手工改过端口/预算的人不会因为我们重写一次就丢掉改动）；
    * 原子写（同目录临时文件 + ``os.replace``）：半个配置文件比没有更糟。
    * ``write_setting``：是否顺带把 ``capabilityLocalPairPath`` 写上（面板手工部署可关）。
    """
    info: Dict[str, Any] = {"path": config_path(), "port": int(port),
                            "adminPort": int(admin_port),
                            "baseUrl": loopback_base_url(port),
                            "pairFile": pair_file_path(),
                            "stateRoot": state_root(), "tmpRoot": tmp_root(),
                            "cacheRoot": cache_root(),
                            "modelsRoot": models_root_path or paths.models_root()}
    for d in (backend_root(), state_root(), tmp_root(), cache_root()):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            return False, "建不了后端目录 %s：%s" % (d, e), info
    secret, secret_note = ensure_secret()
    info["secretNote"] = secret_note
    text = render_config(port=port, admin_port=admin_port, jwt_secret=secret,
                         device=device, vram_budget_mb=vram_budget_mb, specs=specs,
                         models_root_path=info["modelsRoot"])
    path = config_path()
    old = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                old = fh.read()
        except Exception:
            old = ""
        if old != text:
            backup = "%s.bak-%s" % (path, time.strftime(BACKUP_STAMP))
            try:
                with open(backup, "w", encoding="utf-8") as fh:
                    fh.write(old)
            except Exception:
                backup = ""
            info["backup"] = backup
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        try:
            # 配置文件里**有 jwt_secret**（服务端要它就在 yaml 里），所以按凭据对待：
            # POSIX 收成 0600；Windows 上是空实现（那道墙是 DPAPI，见平台接缝）。
            platform.restrict_file(path)
        except Exception:
            pass
    except Exception as e:
        return False, "写不了 %s：%s" % (path, e), info
    detail = "配置已写好：%s（listen=%s:%d，local_pair=true，jwt_secret %s）" % (
        path, DEFAULT_LOOPBACK, int(port), secret_note)
    if info.get("backup"):
        detail += "；原文件已备份为 %s" % info["backup"]
    if not os.path.isdir(info["modelsRoot"]):
        detail += "；注意模型库 %s 还不存在（权重一批要另做准备）" % info["modelsRoot"]
    if write_setting:
        # 设置写不进去**不算失败**：它只影响面板那个「检测本机后端」按钮能不能自己找到文件，
        # 而本机配对这一步我们用的是**算出来的路径**，照样能成。但必须**大声说出来**
        # （真机冒烟里就撞到过：全新数据目录里 settings 表还没建 → "no such table: settings"）。
        ok_set, note = remember_pair_path(pair_file_path())
        info["settingOk"] = bool(ok_set)
        info["settingNote"] = note
        detail += "；" + note
    return True, detail, info


def remember_pair_path(target: str) -> Tuple[bool, str]:
    """把 ``capabilityLocalPairPath`` 指到 ``target``（**只在需要时动它**）。

    三种情形（都是"如实说一句"）：
      * 现在是空的 → 写上我们算出来的路径；
      * 已经是同一个 → 什么都不做；
      * 手工填过别的、且**那个文件确实在** → **沿用用户的**（状态卷可能真挂在别处）；
        手工填的那个**不存在**了 → 改成我们的，并在人话里说明原来那个是什么。
    """
    from app.config import settings
    current = ""
    try:
        current = str(settings.get("capabilityLocalPairPath", "") or "").strip()
    except Exception:
        current = ""
    if current:
        if os.path.abspath(os.path.expanduser(current)) == os.path.abspath(target):
            return True, "本机配对文件路径已是 %s" % target
        if os.path.isfile(os.path.expanduser(current)):
            return True, "沿用你手填的本机配对文件路径 %s" % current
        note = "原来填的 %s 已不存在，改成 %s" % (current, target)
    else:
        note = "已把本机配对文件路径设为 %s" % target
    try:
        settings.update({"capabilityLocalPairPath": target})
    except Exception as e:
        return False, "写不进设置 capabilityLocalPairPath：%s" % e
    return True, note


# ---------------------------------------------------------------- 起 / 等 / 配对

def process_env() -> Dict[str, str]:
    """后端进程需要的环境变量：**缓存与家目录指到可写目录**（见 ``cache_root()``）。"""
    cache = cache_root()
    try:
        os.makedirs(cache, exist_ok=True)
    except Exception:
        pass
    return {
        "MODELSCOPE_CACHE": cache,
        "HOME": cache,
        # 不缓冲：日志要能实时看到（否则起不来时只能看到一个空文件）
        "PYTHONUNBUFFERED": "1",
    }


def launch(*, port: int = backend_proc.DEFAULT_PORT,
           admin_port: int = backend_proc.DEFAULT_ADMIN_PORT,
           python: str = "", cwd: str = "") -> Tuple[bool, str]:
    """把后端起起来（用 1b 的 ``spawn()``：端口占用/归属判据都在那边）。

    ``python`` / ``cwd`` 留空 = 扩展包路的标准形态（解释器在 ``runtime/``、cwd 是后端自己的家）。
    排障与自测时可以把它们指到源码树（``python -m server.main`` 要在能看见 ``server/`` 的目录里跑）。
    """
    exe = python or backend_proc.python_exe()
    if not exe:
        return False, ("后端还没有自己的运行时（%s 下没有 runtime/）—— "
                       "扩展包要先解包/装好，容器路则不该走到这里"
                       % backend_proc.backend_root())
    cfg = config_path()
    if not os.path.isfile(cfg):
        return False, "还没有配置文件（%s）：先做 configure" % cfg
    workdir = cwd or backend_root()
    argv = [exe, "-m", "server.main", "--config", cfg, "--log-level", "info"]
    return backend_proc.spawn(argv, cwd=workdir, env=process_env(),
                              ports=(int(port), int(admin_port)))


def wait_for_pair_file(timeout: float = PAIR_FILE_TIMEOUT,
                       require_alive: bool = True) -> Tuple[bool, str]:
    """等后端把 ``local-pair.json`` 写出来 → ``(ok, 人话)``。

    为什么必须等它**出现**而不是"起完就算"：配对码是后端启动过程中发的，文件没出来时
    点配对必然报"没找到本机后端的配对文件"。

    ``require_alive``：轮询时顺带看一眼"我们起的那个还在吗" —— 后端起来就崩（缺依赖、
    CUDA 不对）时立刻如实说，而不是让人干等满 60 秒。判据只在**有 pid 记录**时生效
    （没有记录 = 不是我们起的，不拿它下结论）。
    """
    path = pair_file_path()
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if os.path.isfile(path):
            return True, "后端已写出本机配对文件：%s" % path
        if require_alive and os.path.exists(backend_pid.pid_path()):
            alive, note = backend_pid.is_ours_alive()
            if not alive:
                log, err = backend_pid.log_paths()
                return False, ("后端起来后立刻退出了（%s）—— 看 %s / %s；"
                               "本机配对文件因此没出现" % (note, log, err))
        if time.monotonic() >= deadline:
            log, err = backend_pid.log_paths()
            return False, ("等了 %.0f 秒也没等到 %s —— 后端可能在加载模型或起不来，"
                           "看 %s / %s" % (float(timeout), path, log, err))
        time.sleep(PAIR_FILE_POLL)


def pair_if_needed(*, port: int = backend_proc.DEFAULT_PORT,
                   replace: bool = False,
                   ready_timeout: float = PAIR_READY_TIMEOUT) -> Tuple[bool, str]:
    """配对本机后端 → ``(ok, 人话)``。**已经配对到同一个回环地址就跳过**。

    为什么必须跳过：后端**每次启动**都会发一张新配对码，而每配一次就会在服务端
    **新建一个 client**（管理面清单会被撑爆：一天点十次就是十个"本机客户端"）。

    已经配对到**别处**（另一台 GPU 后端）时**不覆盖**（除非 ``replace=True``）：
    那个配对可能是人家正在用的，悄悄换掉的表现是"我连的 GPU 忽然变成本机/本机忽然变卡"。

    ``ready_timeout``：见 ``PAIR_READY_TIMEOUT`` 那段说明 —— 配对文件出现时服务**可能**
    还没开始接连接，所以"连不上"这一类失败要在窗口内重试；**别的失败不重试**
    （配对码过期/文件坏了，重试只是把同一句话重复二十遍）。
    """
    target = loopback_base_url(port)
    try:
        current = cred.load()
    except Exception:
        current = None
    if current is not None:
        now = ""
        try:
            now = pairing.normalize_base_url(current.base_url)
        except Exception:
            now = str(current.base_url or "").rstrip("/")
        if now and now == target:
            return True, ("已经配对到本机后端（%s，%s）—— 跳过，免得在服务端又新建一个客户端"
                          % (current.base_url, current.client_id))
        if not replace:
            return False, ("这台机器现在配对的是 %s（不是本机后端）—— 要用本机后端请先"
                           "「解除配对」，或明确选择覆盖它" % (current.base_url or "（没有地址）"))
    path = pair_file_path()
    if not os.path.isfile(path):
        return False, "本机配对文件还不存在（%s）—— 后端起来了吗？" % path
    deadline = time.monotonic() + max(0.0, float(ready_timeout))
    while True:
        try:
            creds = pairing.pair_local(client_name="本机自动配对", save=True, path=path)
        except pairing.PairingError as e:
            if e.code == "offline" and time.monotonic() < deadline:
                time.sleep(PAIR_FILE_POLL)      # 服务还在 bind：等一会儿再试
                continue
            return False, str(e)
        except Exception as e:                                    # pragma: no cover - 兜底
            return False, "本机自动配对时出了意外：%s" % e
        return True, "已连上本机后端：%s（%s）" % (creds.server_name or creds.base_url,
                                                  creds.client_id)


# ---------------------------------------------------------------- 串联

def start(*, port: int = backend_proc.DEFAULT_PORT,
          admin_port: int = backend_proc.DEFAULT_ADMIN_PORT,
          device: str = "cuda", vram_budget_mb: int = 0,
          specs: Optional[Sequence[Dict[str, Any]]] = None,
          timeout: float = PAIR_FILE_TIMEOUT, replace: bool = False,
          write_setting: bool = True, models_root_path: str = "",
          python: str = "", cwd: str = "") -> Dict[str, Any]:
    """把 configure → launch → 等配对文件 → 配对 串起来 → 结果字典。

    返回 ``{"ok": bool, "steps": [{"name", "ok", "detail"}...], "message", …}`` ——
    面板（1d）把它当进度显示：**每一步都可读**，失败的那一步就是"下一步该看哪里"。
    任何一步失败**立刻停**（不继续往下做，也不改任何"以后走哪条路"的设置）。

    "ECHO 起的那个已经在跑"**算成功**（幂等：点两次不该报错）—— 那正是
    "后端上次起的、还在跑"的正常情形，接下来直接等配对文件 + 配对。
    """
    steps: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> bool:
        steps.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    ok, detail, info = configure(port=port, admin_port=admin_port, device=device,
                                 vram_budget_mb=vram_budget_mb, specs=specs,
                                 models_root_path=models_root_path,
                                 write_setting=write_setting)
    if not record("configure", ok, detail):
        return {"ok": False, "steps": steps, "message": detail, **info}
    ok, detail = launch(port=port, admin_port=admin_port, python=python, cwd=cwd)
    if not ok and "已经在跑" in detail:
        ok = True                       # 幂等：上次起的还在跑，接着往下走
    if not record("launch", ok, detail):
        return {"ok": False, "steps": steps, "message": detail, **info}
    ok, detail = wait_for_pair_file(timeout=timeout)
    if not record("pair-file", ok, detail):
        return {"ok": False, "steps": steps, "message": detail, **info}
    ok, detail = pair_if_needed(port=port, replace=replace)
    if not record("pair", ok, detail):
        return {"ok": False, "steps": steps, "message": detail, **info}
    return {"ok": True, "steps": steps, "message": detail, **info}
