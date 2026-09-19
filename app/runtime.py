# -*- coding: utf-8 -*-
"""runtime.py — ECHO 运行时组件生命周期（热键/唤醒监听器）

启动时按配置拉起；配置变更后 restart_* 可热重载。
监听器回调统一进 assistant.capture(source)。
"""
import os
import subprocess
import threading
import time

import app.assistant as assistant
from app.audio.wake import WakeListener, engine_label as wake_engine_label
from app.config import settings
from app.hotkey import HotkeyListener
from app import paths, services
from app import platform as echo_platform

_hotkey = None
_wake = None
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


def _spawn_sidebar(collapsed: bool = False):
    """起一个边条进程（单实例：已在跑就等价于 toggle，所以调用前务必先判进程）。"""
    exe = sidebar_exe_path()
    if not exe:
        return False
    port = int(settings.get("serverPort", 8970))
    args = [exe, f"--url=http://127.0.0.1:{port}/", "--width=450"]
    if collapsed:
        args.append("--collapsed")
    # 脱离父进程组：ECHO 重启/被杀之后边条要活下来。flags 的平台差异在接缝里。
    subprocess.Popen(args, cwd=os.path.dirname(exe), close_fds=True,
                     **echo_platform.detach_gui_kwargs())
    return True


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
        if not sidebar_exe_path():
            return "skip: sidebar exe not built"
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

    port = int(settings.get("serverPort", 8970))
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


def _wake_cb():
    assistant.capture("wake")


def start_wake():
    global _wake
    with _lock:
        if _wake is not None and _wake.is_alive():
            return True, "唤醒监听已在运行"
        _wake = WakeListener(settings.get, on_wake=_wake_cb)
        _wake.start()
        services.report_wake("online", wake_engine_label())
        return True, "唤醒监听已启动"


def stop_wake():
    global _wake
    with _lock:
        if _wake is not None:
            _wake.shutdown()
            _wake = None
        services.report_wake("offline", "已停止")
        return True, "唤醒监听已停止"


def start_all():
    """按配置启动监听器（唤醒词需启用才启动）。"""
    results = [start_hotkey()]
    if settings.get("wakeEnabled", False):
        results.append(start_wake())
    else:
        services.report_wake("disabled", "未启用")
    return results


def stop_all():
    stop_hotkey()
    stop_wake()
