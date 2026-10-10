# -*- coding: utf-8 -*-
"""dialog.py — 让用户在本机**指一个目录**的原生选择器（面板专用）。

背景（2026-10-11 用户要求）：首次启用向导的 S2 要让用户指定 Obsidian 笔记库。
在车里/手机上敲一长串 `D:\\OneDrive\\…` 很难受，所以要有一个「浏览…」按钮。

四条设计约束（都来自"不要在服务器上乱弹窗"）：
1. **只有回环调用能给**（接口层用 `api._is_loopback_call`）—— 面板在本机，选择器也只能在本机弹；
2. **取消不是失败**：`{"ok": False, "cancelled": True}` —— 前端该继续让用户手敲路径，
   绝不能因为"用户点了取消"就报一句红字；
3. **超时 / 没有图形会话 / 不支持的系统**一律**优雅失败**（回一个能读的 `reason`），
   前端据此回落到手敲；**绝不抛异常、绝不挂住请求**；
4. **测试里绝不允许真弹窗**：运行器（`:func:`_default_runner`）是可打桩的，
   用例只喂假的 runner。
"""
from __future__ import annotations

import json
import os
import subprocess

#: 等用户选目录的上限（秒）。用户可能一边找一边想，别太短；但也不能无限等 ——
#: 超过就回 "timeout"，前端回落手敲。
PICK_TIMEOUT = 180

#: PowerShell 侧的选择器。**从环境变量收参数**（JSON）—— 不拼字符串，
#: 于是路径里的引号/中文/`$` 都不会变成注入。
_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$cfg = $env:ECHO_PICK_JSON | ConvertFrom-Json
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.FolderBrowserDialog
if ($cfg.title) { $d.Description = [string]$cfg.title }
if ($cfg.initial -and (Test-Path -LiteralPath ([string]$cfg.initial))) {
    $d.SelectedPath = [string]$cfg.initial
}
$r = $d.ShowDialog()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($d.SelectedPath)
}
"""


def supported() -> bool:
    """有没有实现（目前只有 Windows；macOS 上还没写，接口回 `unsupported` 而不是 500）。

    按平台问**接缝**（`app/platform/`），不在业务代码里写 `sys.platform`（D12）。
    """
    try:
        from app import platform as echo_platform
        return bool(echo_platform.has_native_folder_picker())
    except Exception:
        return False


def _no_window() -> int:
    """别让选择器顺带闪一个控制台窗口出来（同 `scripts/echo-launch-lib.ps1` 那条教训）。"""
    try:
        from app import platform as echo_platform
        return int(echo_platform.no_window_creationflags() or 0)
    except Exception:
        return 0


def _default_runner(argv, env, timeout):
    """真的去起 PowerShell。**测试只喂假 runner，不碰这个。**"""
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_no_window())
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()                     # ⚠️ 必须杀 —— 否则那个窗口会一直挂在用户桌面上
        proc.communicate()
        raise
    return proc.returncode, out or b"", err or b""


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw.strip()
    try:
        return (raw or b"").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def pick_folder(title: str = "", initial: str = "", timeout: int = PICK_TIMEOUT,
                runner=None) -> dict:
    """弹一个"选文件夹"的窗口，返回 `{ok, path, cancelled, reason, message}`。

    契约（面板据此决定"用这个路径"还是"让用户手敲"）：

    * 选中 → ``{"ok": True, "path": "<绝对路径>", "cancelled": False}``
    * 取消 → ``{"ok": False, "cancelled": True}``  ← **不是失败**
    * 超时 → ``{"ok": False, "cancelled": False, "reason": "timeout"}``
    * 其余 → ``reason`` 为 ``"no-gui"`` / ``"unsupported"`` / ``"error"``

    **任何情况都不抛异常**：这是接口层，抛出去就是 500，而"没人点"、"没开图形会话"
    都是正常情况，不是错误。
    """
    if not supported():
        return {"ok": False, "cancelled": False, "reason": "unsupported",
                "message": "这个系统上还没有原生选择器，直接粘贴路径即可"}

    payload = json.dumps({"title": title or "选择文件夹", "initial": initial or ""},
                         ensure_ascii=False)
    env = dict(os.environ)
    env["ECHO_PICK_JSON"] = payload
    # 起 shell 一律走接缝（Windows=PowerShell / POSIX=/bin/sh）—— 业务代码里不许出现
    # `"powershell"` 这种字面量（`tests/test_path_seam.py` 钉着；同一条纪律见 D12）。
    # ⚠️ 不需要 `-STA`：**Windows PowerShell 5.1 默认就是 STA**，FolderBrowserDialog 要的正是它。
    try:
        from app import platform as echo_platform
        argv = list(echo_platform.console_shell_argv(_PS))
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "cancelled": False, "reason": "unsupported",
                "message": "起不了本机 shell：%s" % str(e)[:160]}
    run = runner or _default_runner
    try:
        res = run(argv, env, timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": False, "reason": "timeout",
                "message": "等了 %d 秒没有选择 —— 直接粘贴路径也一样" % int(timeout)}
    except Exception as e:                                  # noqa: BLE001 —— 什么都不许漏出去
        return {"ok": False, "cancelled": False, "reason": "error",
                "message": "%s: %s" % (e.__class__.__name__, str(e)[:180])}

    rc, out, err = 0, b"", b""
    if isinstance(res, tuple) and len(res) == 3:
        rc, out, err = res
    else:                                                   # 宽容一点：只回一个对象也行
        rc = int(getattr(res, "returncode", 0) or 0)
        out = getattr(res, "stdout", b"") or b""
        err = getattr(res, "stderr", b"") or b""
    text = _decode(out)
    if text:
        return {"ok": True, "path": text, "cancelled": False}
    if rc != 0:
        return {"ok": False, "cancelled": False, "reason": "no-gui",
                "message": _decode(err)[-200:] or "选择器起不来（可能没有图形会话）"}
    return {"ok": False, "cancelled": True}
