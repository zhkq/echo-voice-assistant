# -*- coding: utf-8 -*-
"""Windows 平台环境默认值 + 平台原语（D10–D12 的接缝）。

放在这里的理由见 ``app/platform/__init__.py``：平台差异只允许出现在本目录下。
Windows 是 ECHO 的主平台，因此这些值与 1.x 的行为**逐字保持**，不惊动老用户。

本文件里的每个原语都由 ``app/platform/__init__.py`` 转发给业务代码；业务代码
（``app/`` 的其它模块）不得再自己写 ``os.name`` / ``LOCALAPPDATA`` / ``platform.system()`` 分支。
"""
import ctypes
import os

NAME = "win32"

#: 系统数据根：与 1.x 一致，就在安装目录下（老用户升级后 data 不用搬）
PLATFORM_DEFAULTS = {
    "dataDir": "{ECHO}/data",
    #: 模型的一键安装命令（面板直接贴给用户跑）。macOS/Linux 上没有对应脚本 → 不提供，
    #: 调用方回落成 pip 说明。
    "modelInstallCommands": {
        "qwen3asr": "powershell -ExecutionPolicy Bypass -File scripts\\install-qwen3asr.ps1",
    },
    # ---- 配置项的默认值与候选项（D11）----
    # Windows 是基准平台：这里声明的值与 config.DEFAULTS 一致（显式写出来是为了
    # 三个平台一眼可比，而不是靠"Windows 没声明所以用基准"来推断）。
    "settingOptions": {
        "ttsEngine": ["auto", "edge-tts", "sapi", "off"],
    },
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


def detach_gui_kwargs() -> dict:
    """让 GUI 子进程脱离父进程组（边条：``echo-sidebar.exe``）。

    ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``：ECHO 重启/被杀时边条要活下来。
    注意它只适合 **GUI** 程序——控制台程序脱离控制台会静默退出（1.x 实测），
    控制台 helper 用 ``detach_console_kwargs()``。
    """
    import subprocess
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    return {"creationflags": flags}


def detach_console_kwargs() -> dict:
    """控制台 helper：独立于父进程组，但不脱离控制台（``CREATE_NO_WINDOW`` 保静默）。"""
    import subprocess
    flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
             | no_window_creationflags())
    return {"creationflags": flags}


def console_shell_argv(script: str):
    """起一个控制台脚本的 argv（Windows = PowerShell）。"""
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]


def process_running(image_name: str) -> bool:
    """按镜像名查进程（``tasklist``）。查不到/命令失败一律 False（永不抛）。

    用于"边条是否已在运行"：``echo-sidebar.exe`` 是单实例应用，再起一个等于给它发
    toggle，会把用户展开的面板收起来。
    """
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % image_name, "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=no_window_creationflags()).stdout or ""
        return image_name.lower() in out.lower()
    except Exception:
        return False


def kill_process_tree(pid: int) -> bool:
    """杀掉整棵进程树（``taskkill /T /F``）。

    独立 harness 的启动链是 ``cmd → node(npx-cli) → cmd → node(dsh)`` 四层：
    只 ``terminate()`` 最外层，端口照样被占着（2026-09-19 实测）。
    """
    import subprocess
    if pid <= 0:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, creationflags=no_window_creationflags())
        return True
    except Exception:
        return False


def listening_pid(port: int) -> int:
    """谁在监听本机某端口（``netstat -ano``）；找不到 = 0。"""
    import subprocess
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=no_window_creationflags()).stdout or ""
    except Exception:
        return 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":" + str(port)):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return 0


# ------------------------------------------------------------------ 打开窗口 / 提示音 / 离线 TTS

def shell_open(target: str, params: str = "") -> bool:
    """用系统 shell 打开 URL 或可执行文件（``ShellExecuteW("open", …)``）。

    非阻塞、不弹控制台；``params`` 非空时带上（Chromium ``--app=<url>``）。
    ShellExecute 失败时退回 ``cmd /c start``（1.x 的兜底，行为保持不变）。
    """
    try:
        import ctypes
        ctypes.windll.shell32.ShellExecuteW(None, "open", target, params or None, None, 1)
        return True
    except Exception:
        pass
    try:
        import subprocess
        subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
        return True
    except Exception:
        return False


def play_wav_async(path: str) -> bool:
    """异步播放 wav（提示音）。``winsound`` 是 Windows 自带、不占线程。

    ⚠️ 已知问题（§30.2，挂起中）：用户实测**听不到提示音**，而同一批 wav 用
    sounddevice 播是能听到的 —— 怀疑就是这条 ``winsound`` 老 waveOut 通路。
    探针 `scripts/probe-tts.bat`（在稳定树里）跑完再决定是否改用 sounddevice；
    在那之前**保持行为不变**，只把它收进接缝（这样换实现只动这一处）。
    """
    import os
    if not os.path.isfile(path):
        return False
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True
    except Exception:
        return False


def offline_tts_speak(text: str, timeout: int = 60) -> bool:
    """离线朗读（SAPI / System.Speech）。"""
    try:
        from app.platform.win32 import sapi
        return bool(sapi.speak(text, timeout))
    except Exception:
        return False


def offline_tts_label() -> str:
    """离线 TTS 的引擎短名（= 1.x 配置里的 `sapi`，状态文案用）。"""
    return "sapi"


def offline_tts_display() -> str:
    """离线 TTS 的可读名字（与 1.x 面板文案逐字一致）。"""
    try:
        from app.platform.win32 import sapi
        return str(sapi.label())
    except Exception:
        return "Windows SAPI"


def notify(title: str, text: str) -> bool:
    """桌面通知（PowerShell NotifyIcon 气泡，Windows 专有）。异步、不阻塞。"""
    import subprocess
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; $n.BalloonTipTitle = $title; "
        "$n.BalloonTipText = $text; $n.ShowBalloonTip(5000); "
        "Start-Sleep -Milliseconds 600; $n.Dispose()"
    ).replace("$title", "'" + title.replace("'", "''") + "'") \
     .replace("$text", "'" + text.replace("'", "''") + "'")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=8, creationflags=no_window_creationflags())
        return True
    except Exception:
        return False


def sidebar_candidates(install_root: str):
    """边条可执行文件候选（Release 优先，Debug 兜底）。调用方判存在。"""
    return [
        os.path.join(install_root, "sidebar", "bin", "Release", "net7.0-windows",
                     "win-x64", "echo-sidebar.exe"),
        os.path.join(install_root, "sidebar", "bin", "Debug", "net7.0-windows",
                     "win-x64", "echo-sidebar.exe"),
    ]


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


# ------------------------------------------------------------------ 可粘贴的一行命令

#: 在 PowerShell 里**必须加引号**的字符：空格/制表（会被拆成两个参数）、引号、反引号、
#: `$`（变量展开）、`& | ; < > ( )` 这些语句分隔/重定向/分组符、以及 `,`（参数位置的数组
#: 构造符）`%` —— 裸写会让 PowerShell 把它当语法而不是参数（`-c print(1)` 直接报解析错误）。
_PS_NEEDS_QUOTE = " \t\"'`$&|;<>(),%"


def console_command(argv) -> str:
    """把一条 argv 渲染成**能直接粘进 PowerShell 就跑**的一行命令。

    为什么需要它（2026-09-25 同事实测）：面板上「复制命令」给的是 `python -c "…"`，
    而同事那台干净装机是 python.org 嵌入包 —— `python` 不在 PATH 上，粘进 PowerShell
    直接"不是内部或外部命令"，他自己补了绝对路径才跑起来。命令得由**运行时**算出来，
    而"算哪套 shell 的写法"是平台差异，所以收在接缝里（业务代码只给 argv）。

    形态：``& "C:\\…\\python.exe" -c "…"``。第一个参数（可执行文件）**总是**加引号 ——
    路径里有空格时不加引号 PowerShell 会把它当一个字符串表达式；`&` 是它的调用运算符
    （实测 ``& "C:\\Program Files\\…\\python.exe" -c "print('a b')"`` 能跑）。

    ⚠ **参数里不要带内嵌双引号**：PowerShell 5.1 把带内嵌双引号的参数传给原生 exe 时
    会把引号**吞掉**（实测 ``-c "print(\\"a b\\")"`` 到 python 手里成了 ``print(a b)``，
    直接 SyntaxError）。所以 python 载荷里的字符串一律写**单引号**；这里只把 `` ` `` 与
    `$` 转义掉（PowerShell 双引号串里的转义符是反引号，不是反斜杠）。
    """
    parts = [str(a) for a in argv]
    if not parts:
        return ""
    out = ['"%s"' % parts[0].replace("`", "``").replace("$", "`$")]
    for p in parts[1:]:
        if any(ch in p for ch in _PS_NEEDS_QUOTE):
            out.append('"%s"' % p.replace("`", "``").replace("$", "`$"))
        else:
            out.append(p)
    return "& " + " ".join(out)


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


def gpu_info():
    """显卡信息（首装向导判断"要不要装显卡加速"用）。

    只查 NVIDIA —— ECHO 目前只支持它的加速。优先问 ``nvidia-smi``（装了驱动就有），
    **不走 wmic / PowerShell**：那两个是本仓库明令收进平台接缝的 Windows 专有命令，
    而且新版 Windows 上 wmic 已被弃用。探测不到就返回空字段（不猜型号）。
    """
    import shutil
    import subprocess

    out = {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": ""}
    exe = shutil.which("nvidia-smi")
    if not exe:
        return out
    try:
        raw = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
    except Exception:
        return out
    rows = [r for r in (raw.stdout or "").splitlines() if r.strip()]
    if not rows:
        return out
    parts = [p.strip() for p in rows[0].split(",")]
    if len(parts) >= 3:
        try:
            vram = int(float(parts[1] or 0))
        except ValueError:
            vram = 0
        out.update({"vendor": "nvidia", "name": parts[0], "vramMb": vram,
                    "driver": parts[2], "source": "nvidia-smi"})
    return out


def node_dirs():
    """可能装着 node/npx 的目录（按可信度排序）。

    为什么需要（2026-09-22 同事反馈 B1）：ECHO 由**桌面快捷方式**启动时，进程 PATH 里可能
    没有 node —— 机器上只有 WorkBuddy / nvm 这类"托管"node，它们不写系统 PATH。而独立
    harness 靠 npx 起，于是静默失败（只在 /api/boot/status 里看到 failed）。
    托管式 node 放最前：那种机器上它通常才是唯一可用的。
    """
    home = os.path.expanduser("~")
    out = []
    # ① 托管式 node（WorkBuddy 装在自家 binaries 下；版本目录名倒序 = 新版本在前）
    base = os.path.join(home, ".workbuddy", "binaries", "node", "versions")
    try:
        out += [os.path.join(base, v) for v in sorted(os.listdir(base), reverse=True)]
    except OSError:
        pass
    # ② nvm-windows 的 current 软链
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        out.append(os.path.join(appdata, "nvm", "current"))
    # ③ 官方安装包（环境变量为空时跳过，免得拼出相对路径）
    for env_name, tail in (("ProgramFiles", "nodejs"),
                           ("ProgramFiles(x86)", "nodejs"),
                           ("LOCALAPPDATA", os.path.join("Programs", "nodejs"))):
        root = os.environ.get(env_name, "")
        if root:
            out.append(os.path.join(root, tail))
    return [d for d in out if d and os.path.isdir(d)]


# ---------------------------------------------------------------- 秘密保护（凭据落盘）
#
# 这里放的是**客户端凭据**（配对换来的 `client_id` + `secret`）的落盘保护。
# 为什么是 DPAPI 而不是"跟 provider 密钥一样存 SQLite"：provider 密钥泄露 = 花你的额度；
# 后端凭据泄露 = **用你的显卡 + 以你的身份出现在服务端审计里**。值钱程度不同，
# 所以它单独走一条更硬的路（`app/capabilities/credentials.py` 里有完整说明）。
#
# 用**用户作用域**（不传 `CRYPTPROTECT_LOCAL_MACHINE`）：密文绑到当前用户账户，
# 文件被拷到别的机器/别的账户都解不开 —— 这正是我们要的那条性质。

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def protect_secret_kind() -> str:
    """保护方式的标记，写进信封（`dpapi` / `plain`），读的时候据此选解密路径。"""
    return "dpapi"


def _dpapi(protect: bool, data: bytes) -> bytes:
    """`CryptProtectData` / `CryptUnprotectData`（用户作用域，不弹 UI）。

    `CRYPTPROTECT_UI_FORBIDDEN` 是必须的：ECHO 是后台进程，少了它某些情况下会弹一个
    **没人能点**的系统对话框，把调用挂死。

    `argtypes` 全部显式声明：x64 下不声明的话结构体指针会被按 32 位截断，
    表现为"偶尔解密失败"这种极难查的错。
    """
    from ctypes import wintypes
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    ui_forbidden = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "ECHO backend credential",
                                      None, None, None, ui_forbidden,
                                      ctypes.byref(blob_out))
    else:
        descr = ctypes.c_wchar_p()
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), ctypes.byref(descr),
                                        None, None, None, ui_forbidden,
                                        ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect_secret(data: bytes) -> bytes:
    return _dpapi(True, bytes(data))


def unprotect_secret(blob: bytes) -> bytes:
    """解不开就抛（调用方兜住并当成"没配对"）—— **不退回明文去猜**。"""
    return _dpapi(False, bytes(blob))


def restrict_file(path: str) -> None:
    """Windows 上那道墙是 DPAPI（绑用户账户），文件权限不必再管。

    留这个空实现是为了让三个平台的 env 模块**接口一致**（`test_path_seam.py` 钉着）：
    业务代码只写一句 `platform.restrict_file(path)`，不必知道哪几个平台需要它。
    """
    return None
