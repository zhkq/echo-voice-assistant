# -*- coding: utf-8 -*-
"""failover_proxy.py — ECHO 模型路由（dsh-failover/proxy.py）的 ECHO 侧守护

确保 http://127.0.0.1:8899（模型路由进程）在运行：

- ECHO 每次启动（boot 组件）会调用 start_guard()：立即探测，不在则用同
  venv 的 pythonw 拉起；
- 之后每 30 秒复查一次，路由中途退出会自动拉回。

ECHO 本身由 scripts\startup.ps1 的守护循环保证常驻（登录自启 + 崩溃重启），
因此本守护随 ECHO 一起借力：ECHO 活着 → 路由就绪。避免路由挂掉后 ECHO AUTO
模型调用全部失败。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from app import paths
from app import platform as echo_platform

# 路由端口以 dsh-failover/config.json 的 "port" 为准（代理进程自己也是读它）。
# 2026-09-14 起不再写死：Windows 动态端口段（默认 1024-15000）会被 Hyper-V/WSL
# 划为保留段且每次重启漂移，落在其中的端口 bind 会失败（Errno 13）。
PROXY_DEFAULT_PORT = 8899
GUARD_INTERVAL = 30.0        # 守护复查间隔（秒）
READY_WAIT_MAX = 6.0         # 拉起后等待健康检查的最长时间（秒）
LAUNCH_COOLDOWN = 45.0       # 两次拉起之间的最小间隔（秒），见 ensure_running

_lock = threading.Lock()
_guard = None                # 守护线程
_stop_evt = None
_last_launch = 0.0           # 上次拉起时刻（单调时钟），用于冷却

# 安装根只由 app/paths.py 推导（D29）；dsh-failover/ 是安装目录下的代码资产。
BASE_DIR = paths.echo_root()
PROXY_SCRIPT = os.path.join(BASE_DIR, "dsh-failover", "proxy.py")
PROXY_CONFIG = os.path.join(BASE_DIR, "dsh-failover", "config.json")
LOG_DIR = os.path.join(BASE_DIR, "dsh-failover", "logs")


def proxy_port():
    """路由监听端口：优先取 config.json 的 port，取不到用默认值。"""
    try:
        with open(PROXY_CONFIG, "r", encoding="utf-8-sig") as f:
            return int(json.load(f).get("port") or PROXY_DEFAULT_PORT)
    except Exception:
        return PROXY_DEFAULT_PORT


def proxy_online(timeout=1.0):
    """探测模型路由 /health。在线返回 True。"""
    url = "http://127.0.0.1:%d/health" % proxy_port()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pythonw():
    """从当前解释器推导同环境 pythonw.exe（无控制台窗口）。"""
    exe = sys.executable or "pythonw"
    low = exe.lower()
    if low.endswith("python.exe"):
        return exe[:-len("python.exe")] + "pythonw.exe"
    if low.endswith("pythonw.exe"):
        return exe
    return exe  # 兜底：直接用当前解释器


def ensure_running():
    """探测模型路由，不在则拉起。幂等。返回 (ok, detail)。

    两个细节都是为了不再产生"活着但不监听"的僵尸路由（2026-09-18 实测到过）：

    * 拉起前有**冷却**：路由启动要读配置、连上游探测，几秒内 `/health` 还不通。
      原来每个 30s 复查周期都会再 Popen 一次，等于每半分钟制造一个重复进程；
    * 真正防重复靠 `dsh-failover/proxy.py` 里的**内核级单实例锁**（重复实例在
      uvicorn 之前就退出），本函数只是不再无谓地反复去抢。
    """
    global _last_launch
    if proxy_online():
        return True, "模型路由已在运行"
    now = time.monotonic()
    if now - _last_launch < LAUNCH_COOLDOWN:
        return True, "模型路由刚拉起过，等待就绪（冷却 %.0fs）" % (
            LAUNCH_COOLDOWN - (now - _last_launch))
    script = PROXY_SCRIPT
    if not os.path.isfile(script):
        return False, "模型路由脚本缺失: %s" % script
    if not os.path.isdir(LOG_DIR):
        try:
            os.makedirs(LOG_DIR)
        except Exception:
            pass
    pyw = _pythonw()
    out = os.path.join(LOG_DIR, "proxy-echo.log")
    err = os.path.join(LOG_DIR, "proxy-echo.err.log")
    _last_launch = now          # 先记时刻：即使 Popen 抛异常也要进冷却，避免风暴
    try:
        with open(out, "a", encoding="utf-8") as fo, open(err, "a", encoding="utf-8") as fe:
            subprocess.Popen(
                [pyw, script],
                cwd=os.path.dirname(script),
                stdout=fo,
                stderr=fe,
                creationflags=echo_platform.no_window_creationflags(),
            )
    except Exception as e:
        return False, "拉起模型路由失败: %s" % e
    # 等待就绪（最多 READY_WAIT_MAX 秒）
    waited = 0.0
    while waited < READY_WAIT_MAX:
        if proxy_online(0.8):
            return True, "模型路由已拉起"
        time.sleep(0.5)
        waited += 0.5
    return True, "模型路由进程已启动（健康检查暂未通过，守护线程会继续复查）"


def _guard_loop():
    while not _stop_evt.is_set():
        try:
            ensure_running()
        except Exception:
            pass
        _stop_evt.wait(GUARD_INTERVAL)


def start_guard():
    """启动守护线程（幂等），并立即确保模型路由在运行。返回 (ok, detail)。"""
    global _guard, _stop_evt
    with _lock:
        if _guard is not None and _guard.is_alive():
            return True, "守护已在运行"
        _stop_evt = threading.Event()
        _guard = threading.Thread(target=_guard_loop, daemon=True,
                                  name="failover-proxy-guard")
        _guard.start()
    return ensure_running()


def stop_guard():
    """停止守护线程（不影响模型路由进程本身）。"""
    global _guard, _stop_evt
    with _lock:
        if _guard is not None:
            _stop_evt.set()
            _guard = None
        _stop_evt = None
    return True, "守护已停止"
