# -*- coding: utf-8 -*-
"""Windows 平台环境默认值 + 平台原语（D10–D12 的接缝）。

放在这里的理由见 ``app/platform/__init__.py``：平台差异只允许出现在本目录下。
Windows 是 ECHO 的主平台，因此这些值与 1.x 的行为**逐字保持**，不惊动老用户。

本文件里的每个原语都由 ``app/platform/__init__.py`` 转发给业务代码；业务代码
（``app/`` 的其它模块）不得再自己写 ``os.name`` / ``LOCALAPPDATA`` / ``platform.system()`` 分支。
"""
import os

NAME = "win32"

#: 系统数据根：与 1.x 一致，就在安装目录下（老用户升级后 data 不用搬）
PLATFORM_DEFAULTS = {
    "dataDir": "{ECHO}/data",
}


def dangerous_prefixes():
    """用户不该把数据/模型目录指到这些位置。"""
    out = []
    win = os.environ.get("SystemRoot") or r"C:\Windows"
    out.append((os.path.abspath(win), "不能放在系统目录（%s）下" % win))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        base = os.environ.get(var)
        if base:
            out.append((os.path.abspath(base), "不能放在 %s 下" % base))
    return out


# ------------------------------------------------------------------ 展示

def display_name() -> str:
    """状态文案里的系统名（与 ``platform.system()`` 在 Windows 上的取值一致）。"""
    return "Windows"


# ------------------------------------------------------------------ 子进程

def no_window_creationflags() -> int:
    """起控制台子进程时抑制黑窗的 creationflags。"""
    import subprocess
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


# ------------------------------------------------------------------ 浏览器 / CLI 候选

def chromium_candidates():
    """Chromium 系浏览器可执行文件候选（值与 1.x 逐字一致；调用方做 expandvars）。"""
    return [
        r"$PROGRAMFILES(X86)\Microsoft\Edge\Application\msedge.exe",
        r"$PROGRAMFILES\Microsoft\Edge\Application\msedge.exe",
        r"$LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
        r"$PROGRAMFILES\Google\Chrome\Application\chrome.exe",
        r"$PROGRAMFILES(X86)\Google\Chrome\Application\chrome.exe",
        r"$LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    ]


def agent_cli_candidates():
    """CodeBuddy CLI 的 Windows 专有候选：WorkBuddy 内置 + CodeBuddy IDE 自带。"""
    import glob
    local = os.environ.get("LOCALAPPDATA", "")
    out = []
    if local:
        out.append(os.path.join(local, "Programs", "WorkBuddy", "resources",
                                "app.asar.unpacked", "cli", "bin", "codebuddy"))
        # 版本目录可能变化，兜底 glob
        out.extend(glob.glob(os.path.join(
            local, "Programs", "WorkBuddy", "resources", "app.asar.unpacked",
            "cli", "bin", "codebuddy*")))
    for base in filter(None, (local, os.environ.get("PROGRAMFILES", ""))):
        out.extend(glob.glob(os.path.join(
            base, "*CodeBuddy*", "**", "cli", "bin", "codebuddy"), recursive=True))
    return out


# ------------------------------------------------------------------ TCP 保留端口段

def tcp_excluded_port_range_output() -> str:
    """``netsh int ipv4 show excludedportrange protocol=tcp`` 的原始输出。

    只负责**跑命令、取文本**：解析留在 ``app/ports.py``（那里的 ``parse_excluded()`` 有测试）。
    读不到时返回空串，永不抛异常。
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=no_window_creationflags())
        return proc.stdout or ""
    except Exception:
        return ""


# ------------------------------------------------------------------ HuggingFace 下载脚本

def hf_executable(install_root: str) -> str:
    """venv 里的 ``hf`` 命令行入口。"""
    return os.path.join(install_root, "venv", "Scripts", "hf.exe")


def shell_script(hf: str, jobs) -> str:
    """把若干条 argv 渲染成 Windows PowerShell 下载脚本（面板直接贴给用户）。"""
    lines = ["$env:HF_ENDPOINT = 'https://huggingface.co'",
             "$env:HF_HUB_OFFLINE = '0'"]
    for argv in jobs:
        lines.append("& " + " ".join("'" + str(p).replace("'", "''") + "'" for p in argv))
        lines.append("if ($LASTEXITCODE -ne 0) { throw '模型下载失败，请检查授权与网络' }")
    return "\n".join(lines)


# ------------------------------------------------------------------ 单实例锁（命名互斥量）

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def acquire_named_lock(lock_id, lock_path):
    """创建/打开命名互斥量。返回 ``(handle, detail)``；已有实例时 handle 为 None。"""
    import ctypes
    k32 = _kernel32()
    handle = k32.CreateMutexW(None, True, "Local\\" + lock_id)
    err = ctypes.get_last_error()      # 必须紧跟调用读取
    if not handle:
        return None, "CreateMutexW 失败（err=%s）" % err
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return None, "已有同名实例在运行（%s）" % lock_id
    return handle, lock_id


def named_lock_held(lock_id, lock_path) -> bool:
    k32 = _kernel32()
    handle = k32.OpenMutexW(SYNCHRONIZE, False, "Local\\" + lock_id)
    if handle:
        k32.CloseHandle(handle)
        return True
    return False


def release_named_lock(handle):
    _kernel32().CloseHandle(handle)
