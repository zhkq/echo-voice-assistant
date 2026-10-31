# -*- coding: utf-8 -*-
"""runtime.py — ECHO 运行时组件生命周期（热键/媒体键监听器）

启动时按配置拉起；配置变更后 restart_* 可热重载。
监听器回调统一进 assistant.capture(source)。

2026-10-31 用户决定彻底取消语音唤醒：本模块不再有唤醒监听器
（`app/audio/wake.py` 已删除）。「按一下说话」的入口（热键/媒体键/面板/手机）保留。
"""
import os
import subprocess
import tempfile
import threading
import time

import app.assistant as assistant
from app.config import settings
from app.hotkey import HotkeyListener
from app import paths, ports, services
from app import platform as echo_platform

_hotkey = None
_lock = threading.Lock()
_panel_last_open = 0.0
# 安装根由路径层给（含 ECHO_ROOT 覆盖）。
BASE_DIR = paths.echo_root()

# 打开仪表盘用的 Chromium 系浏览器候选（--app 独立窗口，无地址栏）。
# 候选列表是平台差异，由接缝给（Windows 下 = Edge/Chrome 的常见安装位置）。


def sidebar_exe_path():
    """边条可执行文件（ECHO\\sidebar 编译产物）。找不到返回 None。

    候选路径是平台差异，由接缝给（Windows = ``sidebar/bin/<cfg>/net7.0-windows/...``；
    macOS/Linux 目前返回空表 —— 原生边条宿主属 P3 未完成部分，调用方会回落到整窗）。
    """
    for path in echo_platform.sidebar_candidates(BASE_DIR):
        if os.path.isfile(path):
            return path
    return None


def _sidebar_running() -> bool:
    """边条进程是否已在运行。

    为什么需要它：echo-sidebar.exe 是单实例应用，**再起一个实例 = 给已有实例发 toggle**。
    自动显示（ECHO 每次启动都会跑一遍）如果盲目起进程，就会把用户已经展开的面板反复收起。
    所以先查进程：在跑就什么都不做。

    查进程的方式（``tasklist`` / ``pgrep``）由接缝负责，本模块不再关心平台命令。
    """
    exe = sidebar_exe_path()
    name = os.path.basename(exe) if exe else "echo-sidebar.exe"
    try:
        return echo_platform.process_running(name)
    except Exception as e:                       # 接缝承诺不抛；这里只兜底
        print(f"[hotkey] 查询边条进程失败（按未运行处理）: {e}")
        return False


def _sidebar_runtime_ok(exe: str = "") -> bool:
    """目标机有没有能跑边条的 .NET Desktop Runtime（没有就别起它）。

    边条是 framework-dependent 的 .NET WinForms 程序；缺运行时的时候 **apphost
    自己弹一个 Windows 对话框**（「You must install or update .NET」），而 `Popen`
    当场就返回 —— 调用方以为起来了，用户以为 ECHO 要装 .NET。ECHO 本体不需要它，
    所以这里先判：判不到就回落到浏览器面板，绝不让那个对话框弹出来。
    """
    try:
        return bool(echo_platform.sidebar_runtime(exe or (sidebar_exe_path() or "")))
    except Exception as e:                   # 接缝承诺不抛；这里只兜底
        print(f"[hotkey] 查边条运行时不成功（按没有处理）: {e}")
        return False


def _spawn_sidebar(collapsed: bool = False):
    """起一个边条进程（单实例：已在跑就等价于 toggle，所以调用前务必先判进程）。

    起之前先判 .NET 运行时（`_sidebar_runtime_ok`）—— 缺运行时时**不启动**，
    返回 False 让调用方回落到浏览器面板（见 2026-10-21 用户真机）。
    """
    exe = sidebar_exe_path()
    if not exe:
        return False
    if not _sidebar_runtime_ok(exe):
        print("[hotkey] 没装 .NET Desktop Runtime（7+）—— 边条不能起，改用浏览器面板")
        return False
    # 实际端口，不是首选端口：ECHO 让位后 echo-port.txt 才是权威（否则边条开在死端口）。
    port = ports.active_port(int(settings.get("serverPort", 8970)))
    args = [exe, f"--url=http://127.0.0.1:{port}/", "--width=450"]
    if collapsed:
        args.append("--collapsed")
    # 脱离父进程组：ECHO 重启/被杀之后边条要活下来。flags 的平台差异在接缝里。
    subprocess.Popen(args, cwd=os.path.dirname(exe), close_fds=True,
                     **echo_platform.detach_gui_kwargs())
    return True


#: 边条已有的"单实例信号"命名管道（名字必须与 `sidebar/Program.cs` 的
#: `SidebarConfig.PipeName` 一致）。命令：toggle / rail-hide / rail-show / rail-expand / quit。
SIDEBAR_PIPE_NAME = "echo-sidebar-toggle"
#: Windows 命名管道路径。macOS/Linux 没有原生边条宿主（`sidebar_candidates()` 返回空表）。
SIDEBAR_PIPE_PATH = "\\\\.\\pipe\\" + SIDEBAR_PIPE_NAME


def _sidebar_pipe_write(command):
    """把一条命令写进边条的命名管道 → 成功 True / 失败 False（**永不抛**）。

    为什么是这条命名管道：边条是**独立进程**（`_spawn_sidebar` 用 DETACHED_PROCESS 起它），
    它现有的 IPC 就是这条管道 —— `toggle_sidebar()` 每次都起一个新实例，而新实例干的
    只是往这条管道里写 `toggle`（`sidebar/Program.cs:TrySignalExistingSidebar`）。
    所以"请它退出"只是**多一条 `quit` 命令**：不新造协议、不给边条开 HTTP 端口、
    也不用窗口消息（WebView2 是子窗口，窗口消息那套对不上）。

    为什么失败要静默并返回 False：这条路径挂在**停服务**上（`manager.echo_stop_self`），
    边条没在跑 / 管道正忙 / 平台没有边条 —— 都不能让"停 ECHO"失败。
    通知失败只记一句日志，剩下的交给边条侧的心跳兜底（连续 3 次探不到服务它自己退）。
    """
    # 平台判据只许一处（`app/platform/__init__.py::current()`）：这里再写一次 os.name
    # 就是第二个判据 —— 门禁（scripts/audit-paths.py 的 PLATFORM_TOKEN）会拦，2026-09-21 实测踩到。
    if echo_platform.current() != "win32":
        return False                      # 非 Windows 没有边条宿主，没什么可通知的
    try:
        # 以文本方式打开命名管道：客户端写完后句柄关闭 = EOF，
        # 边条侧 `StreamReader.ReadToEnd()` 这才拿到整条命令（它按"读到 EOF"收命令）。
        with open(SIDEBAR_PIPE_PATH, "w", encoding="utf-8", newline="") as pipe:
            pipe.write(command)
        return True
    except Exception as e:                # noqa: BLE001 —— 见上：通知失败不影响停服务
        print(f"[hotkey] 通知边条失败（{SIDEBAR_PIPE_PATH}）: {e}")
        return False


#: 正常停止时，先发 quit；边条如果 3 秒还没退（旧版本不认 quit / 管道没接上 /
#: 屏幕上那个是升级前遗留的进程），再由一个**脱离 ECHO 的**清理进程按“本棵树的
#: sidebar\ 路径”强制结束它。3 秒比边条自己的 2 秒收尾多 1 秒，正常路径不会被误杀。
SIDEBAR_FORCE_CLOSE_GRACE_MS = 3000


def _sidebar_force_close_script(grace_ms: int = SIDEBAR_FORCE_CLOSE_GRACE_MS) -> str:
    """生成“延迟 N 毫秒后只结束本棵树边条”的 PowerShell 脚本（纯函数，可测）。

    为什么按**可执行文件路径**而不是 `taskkill /IM echo-sidebar.exe`：一台机器上可能
    同时存在别的 ECHO 树（dev/稳定版/旧安装），不能因为一棵树关机就杀掉别人的边条。
    路径判据与 install-all 的 Stop-EchoForUpgrade 同一口径：`<BASE_DIR>\sidebar\`。
    """
    root = (os.path.abspath(os.path.join(BASE_DIR, "sidebar")) + os.sep)
    root_ps = root.replace("'", "''")
    return """$root = '%s'
Start-Sleep -Milliseconds %d
$rootLower = $root.ToLowerInvariant()
Get-CimInstance Win32_Process -Filter "Name='echo-sidebar.exe'" -ErrorAction SilentlyContinue |
  Where-Object {
    $exe = [string]$_.ExecutablePath
    $cmd = [string]$_.CommandLine
    ($exe -and $exe.ToLowerInvariant().StartsWith($rootLower)) -or
    ($cmd -and $cmd.ToLowerInvariant().Contains($rootLower))
  } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$self = $MyInvocation.MyCommand.Path
if ($self) { Remove-Item -LiteralPath $self -Force -ErrorAction SilentlyContinue }
""" % (root_ps, int(grace_ms))


def _powershell_exe() -> str:
    """系统 PowerShell 的绝对路径；找不到就回退 PATH 里的 powershell.exe。"""
    root = (os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows")
    p = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.isfile(p) else "powershell.exe"


def schedule_sidebar_force_close(grace_ms: int = SIDEBAR_FORCE_CLOSE_GRACE_MS) -> bool:
    """脱离 ECHO 起一个延迟清理进程；失败只返回 False，绝不影响停服务。

    为什么必须**脱离**：ECHO 马上要 `os._exit()`，清理要等 3 秒；如果留在 ECHO 进程里，
    它跟着 ECHO 一起死，等于没做。脚本落成临时 `.ps1` 再 `-File` 起 PowerShell（不用
    `-EncodedCommand`：本机实测那条路会触发 WinError 786）；输出全部丢弃，用户看不到黑窗。
    脚本跑完会把自己删掉，不在 %TEMP% 留垃圾。
    """
    if echo_platform.current() != "win32":
        return False
    path = ""
    try:
        script = _sidebar_force_close_script(grace_ms)
        fd, path = tempfile.mkstemp(prefix="echo-sidebar-cleanup-", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(script)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS |
                 subprocess.CREATE_NEW_PROCESS_GROUP)
        subprocess.Popen(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=flags)
        print("[hotkey] 已安排边条兜底清理（3 秒后只结束本棵树的边条）")
        return True
    except Exception as e:                                  # noqa: BLE001 —— 停止路径的最后一步
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        print(f"[hotkey] 安排边条兜底清理失败（忽略）: {e}")
        return False

def quit_sidebar():

    """请边条退出（ECHO **正常停止**时调用；返回通知到没通知到）。

    实现在 `manager.echo_stop_self()` 里：那是"真的停 ECHO"的唯一出口
    （面板/`POST /api/control/echo/stop` 那条路最后都落在这里）。

    为什么必须由 ECHO 主动通知：边条是独立进程，ECHO 退出带不走它 ——
    2026-10-21 的现场就是"8970 已停、echo-sidebar.exe 还挂在屏幕上"。
    通知到了，边条会先显示"ECHO 已停止"再自己关；通知不到（边条没开）也无所谓。
    """
    try:
        ok = _sidebar_pipe_write("quit")
    except Exception as e:                # noqa: BLE001 —— 兜底：连写管道那位炸了也不许挡停服务
        print(f"[hotkey] 通知边条退出失败（忽略）: {e}")
        ok = False
    print("[hotkey] " + ("已通知边条退出" if ok else "边条不在/连不上（跳过通知，由心跳兜底）"))
    return ok


def toggle_sidebar():
    """切换右缘边条（收起 ⇄ 展开）。

    echo-sidebar.exe 是单实例应用：已在运行时会通过命名管道收到 toggle 并自我切换，
    新起的进程随即退出；没在运行就正常建窗（**展开**——按热键的人要的是面板本身）。
    用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 让边条不受 ECHO 子进程组影响。
    """
    exe = sidebar_exe_path()
    if not exe:
        print("[hotkey] 未找到边条程序 sidebar\\bin\\...\\echo-sidebar.exe，改用整窗模式")
        return open_panel_window()
    try:
        if not _spawn_sidebar(collapsed=False):
            return open_panel_window()
        print(f"[hotkey] 切换仪表盘边条（{os.path.basename(exe)}）")
        return True
    except Exception as e:
        print(f"[hotkey] 启动边条失败: {e}")
        return False


def autostart_sidebar():
    """ECHO 启动后自动显示折叠条（设置 panelAutoStart / panelStartCollapsed）。

    与热键的区别：这里**只在边条没在跑时**才起，且默认以折叠条形态出现 ——
    启动后应该是一条安静的右缘状态条，而不是糊一整块面板在屏幕上。
    已经在跑就原样不动（用户可能正展开着它）。
    """
    try:
        if str(settings.get("panelOpenMode", "sidebar") or "sidebar").lower() != "sidebar":
            return "skip: panelOpenMode != sidebar"
        if not settings.get("panelAutoStart", True):
            return "skip: panelAutoStart=False"
        if _sidebar_running():
            return "skip: already running"
        exe = sidebar_exe_path()
        if not exe:
            return "skip: sidebar exe not built"
        if not _sidebar_runtime_ok(exe):
            # 没有 .NET Desktop Runtime：**不要**去起边条（那会弹 Windows 的安装提示）。
            # 用户其实只想看到面板 —— 用浏览器面板顶上，并说清原因。
            print("[hotkey] 没装 .NET Desktop Runtime（7+）—— 面板改用浏览器打开")
            try:
                open_panel_window()
            except Exception as e:           # 兜底：浏览器也打不开时如实报原因
                return f"error: {e}"
            return "fallback: browser (no .NET desktop runtime)"
        collapsed = bool(settings.get("panelStartCollapsed", True))
        if not _spawn_sidebar(collapsed=collapsed):
            return "skip: spawn failed"
        print(f"[hotkey] 启动后自动显示{'折叠条' if collapsed else '面板'}")
        return "started: collapsed=%s" % collapsed
    except Exception as e:
        print(f"[hotkey] 自动显示边条失败: {e}")
        return f"error: {e}"


def restart_echo():
    """重启 ECHO 服务本身（面板「设置 → 服务」里的按钮）。

    自我重启的关键：处理这个 HTTP 请求的进程马上要自杀，所以真正干活的必须是
    **脱离进程组的独立进程**——起 ``scripts/restart-echo.ps1``（停 → 等端口释放 → 起），
    本进程立刻返回，面板随后轮询 /api/status 直到服务回来。
    与 toggle_sidebar 用的是同一套脱离方式（边条就是这么在 ECHO 重启后活下来的）。
    真正的停/起逻辑只有一份，在 stop.ps1 / start.ps1 里。
    """
    script = os.path.join(BASE_DIR, "scripts", "restart-echo.ps1")
    if not os.path.isfile(script):
        return False, "找不到 scripts\\restart-echo.ps1"
    try:
        # flags 的平台差异（Windows=CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW、POSIX=setsid）
        # 与"用什么 shell 跑脚本"都在接缝里。
        # 注意 Windows 上**不能**用 DETACHED_PROCESS：powershell.exe 是控制台程序，脱离
        # 控制台启动会静默退出（2026-09-12 实测：returncode=0 但脚本一行都没执行）；
        # 边条能用 DETACHED_PROCESS 是因为 echo-sidebar.exe 是 GUI 程序。
        flags = echo_platform.detach_console_kwargs()
        log_dir = os.path.join(BASE_DIR, "data", "logs")
        os.makedirs(log_dir, exist_ok=True)
        # helper 自己的 stdout/stderr 落文件（不能用 DEVNULL：一旦它启动失败就什么都看不到）
        out_path = os.path.join(log_dir, "restart-helper.out")
        err_path = os.path.join(log_dir, "restart-helper.err")
        argv = echo_platform.console_shell_argv(script)
        # 关键：脱离进程组，否则本进程被杀时 helper 会一起死（它要在我们死后继续干活）
        proc = subprocess.Popen(
            argv, cwd=BASE_DIR, close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=open(out_path, "ab"), stderr=open(err_path, "ab"),
            **flags)
        print(f"[runtime] 已请求重启 ECHO：helper pid={proc.pid} argv={argv} cwd={BASE_DIR}")
        return True, "正在重启 ECHO（约 5~15 秒，面板会自动重连）"
    except Exception as e:
        print(f"[runtime] 重启 ECHO 失败: {e}")
        return False, f"重启失败: {e}"


def open_panel_window():
    """打开 ECHO 仪表盘窗口（panelOpenMode=app/browser 时的整窗模式）。

    优先用 Chromium 系浏览器的 --app 模式开独立窗口（等同 PWA，无地址栏）；
    找不到就退回默认浏览器。走系统 shell（Windows=ShellExecuteW、macOS=open、
    Linux=xdg-open），非阻塞、不弹控制台。1.5 秒内重复触发会被忽略（防手抖连按开一堆窗口）。
    """
    global _panel_last_open
    now = time.time()
    if now - _panel_last_open < 1.5:
        return False
    _panel_last_open = now

    port = ports.active_port(int(settings.get("serverPort", 8970)))
    url = f"http://127.0.0.1:{port}/"
    mode = str(settings.get("panelOpenMode", "app") or "app")

    exe = None
    if mode == "app":
        for cand in echo_platform.chromium_candidates():
            path = os.path.expandvars(cand)
            if os.path.isfile(path):
                exe = path
                break
    try:
        ok = echo_platform.shell_open(exe, f"--app={url}") if exe else echo_platform.shell_open(url)
        if ok:
            print(f"[hotkey] 打开仪表盘: {url}（{'app 窗口' if exe else '默认浏览器'}）")
            return True
        print(f"[hotkey] 打开仪表盘失败（{url}）")
        return False
    except Exception as e:
        print(f"[hotkey] 打开仪表盘失败: {e}")
        return False


def meeting_hotkey(action):
    """会议热键（2026-10-02 用户要求：开始/结束录制各一个键）。

    与 `/api/meeting/start|stop` 走**同一对函数**（不在这里另写状态判断），
    结果也照端点那样报一次 `services.report_meeting(...)` —— 否则面板上"会议"那颗灯
    会与实际状态不同步。任何异常都只记日志：热键回调在监听线程里，抛出去会把它带停。
    """
    from app import meeting
    try:
        if action == "start":
            ok, msg = meeting.start_meeting()
            services.report_meeting("active" if ok else "idle", msg)
        else:
            ok, msg = meeting.stop_meeting()
            services.report_meeting("transcribing" if ok else "idle", msg)
        print(f"[hotkey] 会议{'开始' if action == 'start' else '结束'}录制：{'ok' if ok else '失败'} · {msg}")
        return ok
    except Exception as e:  # noqa: BLE001 —— 热键线程不许被带停
        print(f"[hotkey] 会议{action} 失败: {e}")
        return False


def _hotkey_cb(source, detail):
    """hotkey: wakeHotkey/fallbackHotkey → 录音命令流；panelHotkey → 切换仪表盘；
    mediakey: vol_up… → 录音命令流。

    panelHotkey 的行为由 panelOpenMode 决定：
      - sidebar（默认，推荐）：切换**右缘边条**（独立进程 echo-sidebar.exe）——
        无边框/置顶/铺满高度/贴右缘，与升级前 DSH 插件做的边条一致；已在跑则收起⇄展开。
      - app / browser：打开整窗版本（Chromium --app 或默认浏览器）。
    """
    if source == "hotkey":
        if detail == "panelHotkey":
            mode = str(settings.get("panelOpenMode", "sidebar") or "sidebar").lower()
            if mode == "sidebar":
                toggle_sidebar()
            else:
                open_panel_window()
        elif detail in ("meetingStartHotkey", "meetingStopHotkey"):
            meeting_hotkey("start" if detail == "meetingStartHotkey" else "stop")
        else:
            assistant.capture("hotkey")
    elif source == "mediakey":
        keys = settings.get("triggerKeys", ["vol_up"]) or ["vol_up"]
        if detail in keys:
            assistant.capture("mediakey")


def start_hotkey():
    global _hotkey
    with _lock:
        if _hotkey is not None and _hotkey.is_alive():
            return True, "热键监听已在运行"
        _hotkey = HotkeyListener(settings.get, on_trigger=_hotkey_cb)
        _hotkey.start()
        services.report_hotkey("online", "组合键 + 媒体键")
        return True, "热键监听已启动"


def stop_hotkey():
    global _hotkey
    with _lock:
        if _hotkey is not None:
            _hotkey.shutdown()
            _hotkey = None
        services.report_hotkey("offline", "已停止")
        return True, "热键监听已停止"


def start_all():
    """启动监听器。**只有热键/媒体键** —— 语音唤醒已取消（2026-10-31）。"""
    return [start_hotkey()]


def stop_all():
    stop_hotkey()
