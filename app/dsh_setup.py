# -*- coding: utf-8 -*-
"""dsh_setup.py — 首次启用向导里的 DSH 检测 / 安装 / 启用。

2026-11-03 的架构口径（见 AGENTS.md）：ECHO 只用**一套** DSH，程序来自官方
DeepSeek Harness 桌面端自带的 ``resources\\runtime\\cli\\bin\\dsh.cmd``。没有 npm /
Node 安装路径；安装包不打包 DSH，安装器只负责把 ECHO 本身装好，首次启动时由本模块
调用安装技能里唯一那份 ``harness-install-desktop.ps1``（官方下载、Authenticode 校验、
``/S`` 当前用户静默安装）。失败必须明确报出来，绝不存在"没装成却显示成功"。

这个模块只做三件事：
  * ``status()``  —— 现在有没有可用的官方 CLI / 服务是否在线（纯探测，不下载）；
  * ``detect()``  —— 真跑一次 ``harness-install-desktop.ps1 -NoDownload``（会跑
    ``dsh.cmd --version``，所以"目录在"不算数）；
  * ``start_install()`` / ``state()`` —— 后台下载安装，向导轮询进度。

装好之后写的是 ``agentBackend=dsh`` + ``dshAutoStart=true``，再走与面板 PUT 设置
完全相同的联动（``settings_effects.apply``）把无头 dsh 拉起来；**不写 harnessCommand /
agentHarnessEnabled**（新架构已退役）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from app import paths
from app import platform as echo_platform
from app.config import settings

MANUAL_URL = "https://download.deepseek.com/desktop/dsh-latest-windows-x64.exe"
DETECT_TIMEOUT_S = 120
INSTALL_TIMEOUT_S = 1800

_LOCK = threading.RLock()
_VERSION_CACHE: Dict[str, tuple] = {}

_JOB: Dict[str, object] = {
    "state": "idle",          # idle | running | done | failed
    "phase": "",
    "message": "",
    "detail": "",
    "installed": False,
    "ready": False,
    "cli": "",
    "version": "",
    "error": "",
    "startedAt": "",
    "finishedAt": "",
    "log": "",
}


def _script() -> str:
    return os.path.join(paths.echo_root(), ".dsh", "skills", "echo-install", "scripts",
                        "harness-install-desktop.ps1")


def _log_path() -> str:
    return os.path.join(paths.data_root(), "logs", "dsh-setup.log")


def _command_path() -> str:
    return os.path.join(paths.data_root(), "logs", "dsh-command.txt")


def supported() -> bool:
    return echo_platform.current() == "win32" and os.path.isfile(_script())


def log_path() -> str:
    return _log_path()


# ---------------------------------------------------------------- helpers

def _tail(path: str, lines: int = 25) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()
        return "\n".join(rows[-lines:])
    except Exception:
        return ""


def _parse_output(stdout: str, command_file: str, version_file: str) -> Dict[str, str]:
    out: Dict[str, str] = {"command": "", "version": "", "installDir": ""}
    try:
        if os.path.isfile(command_file):
            out["command"] = Path(command_file).read_text(encoding="utf-8-sig").strip()
    except Exception:
        pass
    try:
        if os.path.isfile(version_file):
            out["version"] = Path(version_file).read_text(encoding="utf-8-sig").strip()
    except Exception:
        pass
    text = stdout or ""
    if not out["command"]:
        rows = [r.strip() for r in text.splitlines() if r.strip().startswith("HARNESS_COMMAND=")]
        if rows:
            out["command"] = rows[-1][len("HARNESS_COMMAND="):].strip()
    if not out["version"]:
        rows = [r.strip() for r in text.splitlines() if r.strip().startswith("DSH_VERSION=")]
        if rows:
            out["version"] = rows[-1][len("DSH_VERSION="):].strip()
    rows = [r.strip() for r in text.splitlines() if r.strip().startswith("DSH_INSTALL_DIR=")]
    if rows:
        out["installDir"] = rows[-1][len("DSH_INSTALL_DIR="):].strip()
    return out


def _run_script(*, no_download: bool, timeout: int) -> subprocess.CompletedProcess:
    script = _script()
    if not os.path.isfile(script):
        raise FileNotFoundError("缺 harness-install-desktop.ps1：%s" % script)
    log = _log_path()
    os.makedirs(os.path.dirname(log), exist_ok=True)
    command_file = _command_path()
    for path in (command_file, command_file + ".version"):
        try:
            os.remove(path)
        except OSError:
            pass

    argv = list(echo_platform.console_shell_argv(script))
    argv += ["-LogFile", log, "-CommandFile", command_file]
    if no_download:
        argv.append("-NoDownload")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=int(echo_platform.no_window_creationflags() or 0),
    )


def _cli_version(cli: str) -> str:
    """真跑 ``dsh.cmd --version``（带硬超时）。目录存在不算"已安装"。"""
    if not cli or not os.path.isfile(cli):
        return ""
    try:
        cache_key = (os.path.normcase(cli), os.path.getmtime(cli))
    except OSError:
        cache_key = (os.path.normcase(cli), 0)
    with _LOCK:
        cached = _VERSION_CACHE.get(cache_key)
        if cached is not None:
            return str(cached)
    if echo_platform.current() != "win32":
        return ""
    tmp = tempfile.mkdtemp(prefix="echo-dsh-version-")
    env = dict(os.environ)
    env["DSH_HOME"] = tmp
    version = ""
    try:
        # Official desktop ships a .cmd shim; CreateProcess cannot execute .cmd directly.
        # Run it through PowerShell (the platform seam's inline_shell_argv) and quote the
        # path as a PowerShell single-quoted literal (paths cannot contain double quotes).
        quoted = "'" + str(cli).replace("'", "''") + "'"
        proc = subprocess.run(
            list(echo_platform.inline_shell_argv("& " + quoted + " --version")),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, env=env,
            creationflags=int(echo_platform.no_window_creationflags() or 0),
        )
        rows = [r.strip() for r in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()
                if re.match(r"^\d+\.\d+", r.strip())]
        if rows:
            version = rows[-1]
    except Exception:
        version = ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with _LOCK:
        _VERSION_CACHE[cache_key] = version
    return version


def _harness():
    try:
        from app import harness_proc
        return harness_proc
    except Exception:
        return None


def _online_and_desktop() -> tuple:
    hp = _harness()
    if hp is None:
        return False, False, ""
    online = False
    desktop = False
    try:
        online = bool(hp.online(timeout=0.8) or hp.desktop_mode(timeout=0.8))
    except Exception:
        online = False
    try:
        desktop = bool(hp.desktop_gui_running())
    except Exception:
        desktop = False
    return online, desktop, ""


def _snapshot_job() -> Dict[str, object]:
    with _LOCK:
        return dict(_JOB)


def _set_job(**values) -> None:
    with _LOCK:
        _JOB.update(values)


# ---------------------------------------------------------------- public API

def status() -> Dict[str, object]:
    """Cheap status for the wizard: installed? running? which CLI/version?"""
    cli = ""
    hp = _harness()
    if hp is not None:
        try:
            cli = str(hp.find_desktop_cli() or "")
        except Exception:
            cli = ""
    version = _cli_version(cli) if cli else ""
    installed = bool(cli and version)
    online, desktop_running, _ = _online_and_desktop()
    message = ""
    if not supported():
        message = "只有 Windows 客户端在首次向导里安装 DSH。"
    elif installed and online:
        message = "DSH 已安装并在线。"
    elif installed:
        message = "DSH 程序已安装，但它的服务还没在线。"
    elif cli:
        message = "找到了 dsh.cmd，但运行 --version 失败（可重新安装修复）。"
    else:
        message = "还没检测到官方 DeepSeek Harness 桌面端。"

    agent = ""
    auto = True
    try:
        agent = str(settings.get("agentBackend", "") or "")
        auto = bool(settings.get("dshAutoStart", True))
    except Exception:
        pass

    return {
        "supported": bool(supported()),
        "installed": installed,
        "ready": bool(installed and online),
        "online": bool(online),
        "desktopRunning": bool(desktop_running),
        "cli": cli,
        "version": version,
        "agentBackend": agent,
        "dshAutoStart": auto,
        "message": message,
        "manualUrl": MANUAL_URL,
        "log": _log_path(),
        "job": _snapshot_job(),
    }


def detect() -> Dict[str, object]:
    """Run the installer helper with -NoDownload: this actually executes --version."""
    base = status()
    if not supported():
        base.update({"ok": False, "error": "当前平台不支持在这里安装 DSH。"})
        return base
    try:
        proc = _run_script(no_download=True, timeout=DETECT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        base.update({"ok": False, "error": "检测 DSH 超时（120 秒）。请稍后重试。"})
        return base
    except Exception as exc:                       # noqa: BLE001 —— 探测失败要如实回报
        base.update({"ok": False, "error": "检测 DSH 失败：%s" % exc})
        return base

    parsed = _parse_output(proc.stdout or "", _command_path(), _command_path() + ".version")
    detail = _tail(_log_path())
    if parsed["command"] and parsed["version"]:
        base.update({
            "ok": True,
            "installed": True,
            "cli": parsed["command"],
            "version": parsed["version"],
            "message": "已找到官方 DSH：%s（%s）" % (parsed["command"], parsed["version"]),
        })
        return base
    reason = (proc.stderr or proc.stdout or "").strip().splitlines()
    reason_text = reason[-1] if reason else "harness-install-desktop.ps1 没有返回可用的 dsh"
    base.update({
        "ok": False,
        "error": "未检测到可用的官方 DSH：%s" % reason_text,
        "detail": detail,
    })
    return base


def start_install() -> Dict[str, object]:
    if not supported():
        return {"ok": False, "error": "当前平台不支持在这里安装 DSH。", "job": _snapshot_job()}
    with _LOCK:
        if _JOB.get("state") == "running":
            return {"ok": True, "alreadyRunning": True, "job": dict(_JOB)}
        _JOB.update({
            "state": "running", "phase": "starting", "message": "准备安装 DSH…",
            "detail": "", "installed": False, "ready": False, "cli": "", "version": "",
            "error": "", "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finishedAt": "", "log": _log_path(),
        })
    threading.Thread(target=_install_worker, daemon=True, name="echo-dsh-install").start()
    return {"ok": True, "job": _snapshot_job()}


def state() -> Dict[str, object]:
    return _snapshot_job()


def _install_worker() -> None:
    try:
        _set_job(phase="install", message="正在下载并静默安装官方 DeepSeek Harness…")
        proc = _run_script(no_download=False, timeout=INSTALL_TIMEOUT_S)
        parsed = _parse_output(proc.stdout or "", _command_path(), _command_path() + ".version")
        if not parsed["command"] or not parsed["version"]:
            reason = (proc.stderr or proc.stdout or "").strip().splitlines()
            reason_text = reason[-1] if reason else "安装脚本没有返回可用的 dsh"
            _set_job(state="failed", phase="install", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                     error="DSH 安装失败：%s" % reason_text, message="DSH 没有装成",
                     detail=_tail(_log_path()))
            return

        _set_job(phase="settings", installed=True, cli=parsed["command"],
                 version=parsed["version"], message="DSH 程序已就位，正在启用…")
        effects = []
        try:
            updated = settings.update({"agentBackend": "dsh", "dshAutoStart": True})
            from app import settings_effects
            effects = settings_effects.apply(updated)
        except Exception as exc:                   # noqa: BLE001
            _set_job(state="failed", phase="settings", installed=True, ready=False,
                     finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                     error="DSH 已安装，但写设置/启动失败：%s" % exc,
                     message="DSH 已安装，但服务没起来", detail=_tail(_log_path()))
            return

        detail = "；".join(str(e.get("detail") or "") for e in effects if e.get("detail"))
        hp = _harness()
        ok = False
        if hp is not None:
            try:
                ok, msg = hp.ensure_running()
                detail = (detail + ("；" if detail else "") + str(msg)) if msg else detail
            except Exception as exc:               # noqa: BLE001
                detail = (detail + "；" if detail else "") + "启动 DSH 异常：%s" % exc
                ok = False
        online, _, _ = _online_and_desktop()
        ready = bool(ok and online)
        _set_job(
            state="done" if ready else "failed",
            phase="done" if ready else "verify",
            installed=True,
            ready=ready,
            cli=parsed["command"],
            version=parsed["version"],
            finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
            message=("DSH 已安装并在线。" if ready
                     else "DSH 已安装，但服务没有在线；请看日志后重试启动。"),
            error="" if ready else "DSH 服务未在线",
            detail=detail,
            log=_log_path(),
        )
    except subprocess.TimeoutExpired:
        _set_job(state="failed", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                 error="安装 DSH 超时（30 分钟）", message="DSH 没有装成",
                 detail=_tail(_log_path()))
    except Exception as exc:                       # noqa: BLE001 —— 后台任务必须如实落状态
        _set_job(state="failed", finishedAt=time.strftime("%Y-%m-%d %H:%M:%S"),
                 error="安装 DSH 异常：%s" % exc, message="DSH 没有装成",
                 detail=_tail(_log_path()))
