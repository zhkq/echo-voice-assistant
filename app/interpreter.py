# -*- coding: utf-8 -*-
"""interpreter.py —— 「本机解释器」与「能直接粘贴的命令」的唯一出处。

为什么要有这一层（同事 2026-09-25 干净装机实测）
------------------------------------------------
面板「设置 → 模型」上并排放着三个按钮：**下载 / 复制命令 / 复制安装命令**。其中

  * 「复制安装命令」（`app/components.py::_install_command`）早就用 `sys.executable` 拼命令；
  * 「复制命令」（`app/modelinfo.py` 的 `cmd=`）却是**裸 `python`**。

那台机器是 python.org 的嵌入包，`python` 根本不在 PATH 上 —— 用户把命令粘进 PowerShell
直接"不是内部或外部命令"，自己补全绝对路径才跑起来。同一个面板上两条命令一个能跑、
一个不能跑，根因就是"算解释器"这件事被抄了两份、只改了一份。

所以现在收成一份，谁要拼命令都调这里：

  * :func:`python_executable` —— 本机解释器的绝对路径（`pythonw.exe` → 同目录 `python.exe`）；
  * :func:`python_command`    —— 一条"能直接粘进控制台"的 python 命令；
  * :func:`pip_install_command` —— 一条 pip 安装命令（同样带解释器全路径）。

**不许把解释器路径钉死到某一台机器的安装位置**：ECHO 可能在 `runtime-core\\`、`venv\\`
或系统 Python 下跑，甚至被整体搬走；只有**运行时**的 `sys.executable` 算得对。
"""
from __future__ import annotations

import os
import sys

from app import platform as echo_platform


def python_executable() -> str:
    """本机（ECHO 自己那个）解释器的绝对路径；取不到返回空串。

    ECHO 服务自己是 `pythonw.exe`（无控制台）—— 拿它跑 `-c` / `pip` **什么都看不到**
    （用户会以为卡住了），所以换同目录下的 `python.exe`。这是 1.x 面板实测踩过的坑，
    原先写在 `app/components.py` 里，现在只有这一份。
    """
    try:
        py = sys.executable or ""
    except Exception:                       # 解释器信息读不到时不许把调用方带崩
        py = ""
    if not py:
        return ""
    if os.path.basename(py).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(py), "python.exe")
        if os.path.isfile(cand):
            py = cand
    return py


def python_command(*args) -> str:
    """``<本机解释器> <args…>`` 的**可粘贴命令行**（Windows 带 `& "…"`，POSIX 直接写）。

    解释器取不到时返回空串 —— 调用方据此**别给出**一条跑不通的命令（而不是硬编 `python`）。
    """
    exe = python_executable()
    if not exe:
        return ""
    return echo_platform.console_command([exe] + [str(a) for a in args])


def pip_install_command(*targets) -> str:
    """``<本机解释器> -m pip install <targets…>``。

    为什么不用裸 `pip install x`：那会装到 PATH 上第一个 Python 里，ECHO 自己的 venv
    根本看不到，于是面板永远显示"未安装"。cwd 无所谓 —— pip 不看目录。
    """
    return python_command("-m", "pip", "install", *targets)
