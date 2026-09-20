# -*- coding: utf-8 -*-
"""mac_runtime.py — app.runtime 的 macOS 实现

对外接口与 Windows 版保持一致（start_hotkey/stop_hotkey/start_wake/stop_wake/
restart_echo/toggle_sidebar/autostart_sidebar/open_panel_window/start_all/stop_all），因此
app.boot / app.api / app.main 无需任何改动。

差异：
  * 原生 AppKit / WKWebView 右缘边条，未构建时退回浏览器
  * 重启走 mac/restart_mac.sh（不再是 powershell）
  * 唤醒沿用跨平台的 app.audio.wake.WakeListener
"""
import os
import re
import signal
import subprocess
import threading
import time

import app.assistant as assistant
from app.audio.wake import WakeListener
from app.config import settings
from app.hotkey import HotkeyListener
from app import paths, ports, services

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_hotkey = None
_wake = None
_lock = threading.Lock()
_panel_last_open = 0.0


def _actual_port() -> int:
    """ECHO **实际**监听端口：echo-port.txt 优先，回退配置的首选端口。

    不能用 settings.serverPort 直接开浮动框/浏览器：ECHO 让位后（端口被占或
    落在保留段）实际端口写在 echo-port.txt，按配置开就会连到死端口。
    """
    return ports.active_port(int(settings.get("serverPort", 8970)))


def _sidebar_processes():
    """当前运行的 echo-sidebar 进程 → ``[(pid, port)]``（查不到返回空表）。"""
    found = []
    try:
        res = subprocess.run(["pgrep", "-x", "echo-sidebar"],
                             capture_output=True, text=True, timeout=2)
    except Exception:
        return found
    for pid_text in res.stdout.split():
        if not pid_text.isdigit():
            continue
        try:
            cmd = subprocess.run(["ps", "-p", pid_text, "-o", "command="],
                                 capture_output=True, text=True, timeout=2).stdout
        except Exception:
            continue
        m = re.search(r"--port\s+(\d+)", cmd)
        if m:
            found.append((int(pid_text), int(m.group(1))))
    return found


def _retire_stale_sidebars(port: int) -> None:
    """退出开在**别的**端口上的旧浮动框。

    浮动框的单实例锁是按端口命名的（Sidebar.swift 的 sidebar-<port>.lock），
    ECHO 换端口后旧实例不会被新实例接管，结果是屏幕上两个边条、其中一个永远连不上。
    """
    for pid, proc_port in _sidebar_processes():
        if proc_port != port:
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[mac] 退出旧浮动框 pid={pid}（端口 {proc_port} → {port}）")
            except OSError:
                pass


def sidebar_exe_path():
    path = os.path.join(BASE_DIR, "mac", "sidebar", "build", "ECHO Sidebar.app",
                        "Contents", "MacOS", "echo-sidebar")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _spawn_sidebar(command):
    exe = sidebar_exe_path()
    if not exe:
        return False
    port = _actual_port()
    # 换端口后先清掉旧端口的浮动框，否则旧实例（锁按端口命名）会赖在屏幕上连不上。
    _retire_stale_sidebars(port)
    log_dir = os.path.join(BASE_DIR, "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    try:
        with open(os.path.join(log_dir, "sidebar-mac.log"), "ab") as log:
            subprocess.Popen(
                [exe, "--port", str(port), "--command", command,
                 "--data", paths.data_root()], cwd=BASE_DIR,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,
            )
        return True
    except OSError as exc:
        print(f"[mac] 浮动框启动失败: {exc}")
        return False


def toggle_sidebar():
    if settings.get("panelOpenMode", "sidebar") == "sidebar" and _spawn_sidebar("toggle"):
        return True
    return _open_browser()


def autostart_sidebar():
    if settings.get("panelOpenMode", "sidebar") != "sidebar":
        return "skip: panelOpenMode != sidebar"
    if not settings.get("panelAutoStart", True):
        return "skip: panelAutoStart=False"
    # Both startup commands are idempotent for an already running host.
    command = "collapsed" if settings.get("panelStartCollapsed", True) else "expanded"
    if _spawn_sidebar(command):
        return "macOS 浮动框已启动"
    return "skip: 请先运行 bash mac/build_sidebar.sh 构建浮动框"


def open_panel_window():
    if settings.get("panelOpenMode", "sidebar") == "sidebar" and _spawn_sidebar("expand"):
        return True
    return _open_browser()


def _open_browser():
    """用系统默认浏览器打开 ECHO 面板（1.5 秒防抖）。"""
    global _panel_last_open
    now = time.time()
    if now - _panel_last_open < 1.5:
        return False
    _panel_last_open = now
    port = _actual_port()
    url = f"http://127.0.0.1:{port}/"
    try:
        subprocess.Popen(["open", url], close_fds=True)
        print(f"[mac] 打开面板: {url}")
        return True
    except Exception as e:
        print(f"[mac] 打开面板失败: {e}")
        return False


def restart_echo():
    """自我重启：脱离进程组起 mac/restart_mac.sh（停 → 等端口释放 → 起）。"""
    script = os.path.join(BASE_DIR, "mac", "restart_mac.sh")
    if not os.path.isfile(script):
        return False, "找不到 mac/restart_mac.sh"
    log_dir = os.path.join(BASE_DIR, "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_path = os.path.join(log_dir, "restart-mac.out")
    err_path = os.path.join(log_dir, "restart-mac.err")
    try:
        with open(out_path, "ab") as fo, open(err_path, "ab") as fe:
            subprocess.Popen(
                ["/bin/bash", script],
                cwd=BASE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=fo,
                stderr=fe,
                start_new_session=True,   # 脱离进程组，父进程被杀不影响它
            )
        print(f"[mac] 已请求重启 ECHO: {script}")
        return True, "正在重启 ECHO（约 3~8 秒，面板会自动重连）"
    except Exception as e:
        print(f"[mac] 重启失败: {e}")
        return False, f"重启失败: {e}"


def _hotkey_cb(source, detail):
    """与 Windows 版语义一致：panelHotkey → 开面板；其余 → 录音命令流。"""
    if source == "hotkey":
        if detail == "panelHotkey":
            toggle_sidebar()
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
        err = getattr(_hotkey, "error", "") or ""
        if err:
            services.report_hotkey("offline", err[:160])
            return False, err
        services.report_hotkey("online", "macOS 全局热键")
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
        services.report_wake("online", "sherpa KWS")
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
