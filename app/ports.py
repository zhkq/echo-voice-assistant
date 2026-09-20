# -*- coding: utf-8 -*-
"""ports.py — 端口分配与 Windows 保留段规避（P1）

为什么要单独一层
----------------
真实事故（2026-09-14）：ECHO 面板端口原用 8890，某次重启后 bind 报 **Errno 13**
——不是"端口已占用"（EADDRINUSE），而是**没有权限**。原因是 Windows 的动态端口段会被
Hyper-V/WSL 划进保留区：

    netsh int ipv4 show excludedportrange protocol=tcp

而且这些保留段**每次重启可能漂移**。落在保留段里的端口，bind 直接失败，进程起不来；
从外面看只是"端口没监听"，非常难查（当时为此把默认端口从 8890 换成 8970）。

本模块把这件事变成可判定、可绕开、可测试的：

  * ``excluded_ranges()`` 读系统保留段（非 Windows / 读不到时返回空表，不抛异常）
  * ``probe(port)``      区分"保留段 / 已被占用 / 可用"三种情况
  * ``pick(preferred)``  依次尝试：首选 → 附近候选 → 让操作系统分配
  * ``read_port_file()`` / ``write_port_file()``  维护 ``data/echo-port.txt``
    （外部脚本靠它找服务，见 scripts/startup.ps1、launch-desktop.ps1；**原子写**，
    避免脚本读到半个文件）

注意：端口一旦变化，必须把**实际**端口写回 ``echo-port.txt``，否则脚本会去找旧端口。
"""
from __future__ import annotations

import os
import re
import socket
import tempfile
import time
from typing import List, Optional, Tuple

from app import platform as echo_platform

PORT_FILE_NAME = "echo-port.txt"

#: netsh 保留段缓存时间（秒）。保留段会随重启/虚拟化软件变化，不必每次调用都问系统。
_CACHE_TTL = 60.0
_cache: dict = {"at": 0.0, "ranges": None}


def parse_excluded(text: str) -> List[Tuple[int, int]]:
    """从 ``netsh ... show excludedportrange`` 的输出里解析保留段。

    只认"两个整数 + 可选星号"的行，表头与空行自然被忽略；解析失败不影响调用方。
    """
    out: List[Tuple[int, int]] = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\d{1,5})\s+(\d{1,5})\s*\*?\s*$", line)
        if not m:
            continue
        start, end = int(m.group(1)), int(m.group(2))
        if 0 < start <= end <= 65535:
            out.append((start, end))
    return out


def excluded_ranges(refresh: bool = False) -> List[Tuple[int, int]]:
    """当前系统的 TCP 保留端口段。非 Windows 或命令失败时返回空表（永不抛异常）。"""
    now = time.time()
    if not refresh and _cache["ranges"] is not None and now - _cache["at"] < _CACHE_TTL:
        return list(_cache["ranges"])
    # 命令与平台差异由接缝负责（Windows 跑 netsh；其它平台返回空串）；
    # 解析留在这里 —— parse_excluded() 的语义有单测钉住。
    ranges = parse_excluded(echo_platform.tcp_excluded_port_range_output())
    _cache.update(at=now, ranges=ranges)
    return list(ranges)


def in_excluded(port: int, ranges: Optional[List[Tuple[int, int]]] = None) -> bool:
    for start, end in (excluded_ranges() if ranges is None else ranges):
        if start <= port <= end:
            return True
    return False


def probe(port: int, host: str = "127.0.0.1", ranges: Optional[List[Tuple[int, int]]] = None) -> Tuple[bool, str]:
    """能不能在这个端口上监听？返回 ``(ok, reason)``，reason 为空表示可用。

    reason 取值：``"保留段"``（系统不允许）/ ``"已占用"`` / ``"无法绑定: <err>``。
    """
    if in_excluded(port, ranges):
        return False, "保留段"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 故意**不设** SO_REUSEADDR：Windows 上它允许抢占已被监听的端口，
        # 探测会误判"可用"（这正是端口探测类 bug 的经典来源）。默认语义下
        # bind 遇到已占用会报 WSAEADDRINUSE(10048)，正是我们要的信号。
        s.bind((host, port))
        return True, ""
    except OSError as exc:
        code = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
        # 10048/98 = 已占用；10013/13 = 权限（保留段的常见表现）
        if code in (48, 98, 10048) or "10048" in str(exc):
            return False, "已占用"
        return False, "无法绑定: %s" % exc
    finally:
        try:
            s.close()
        except Exception:
            pass


def listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """端口上已经有东西在监听吗？（用于"重复实例"兜底判断）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def pick(preferred: int, *, host: str = "127.0.0.1", ranges: Optional[List[Tuple[int, int]]] = None,
         window: int = 200, bands: Tuple[Tuple[int, int], ...] = ((18000, 18999), (20000, 20999)),
         allow_os: bool = True) -> Tuple[int, str]:
    """挑一个能监听的端口，返回 ``(port, note)``。

    顺序：首选 → ``preferred+1 .. preferred+window`` → **安全带**（默认 18000-18999、
    20000-20999）→ 最后才让操作系统分配。

    为什么不直接用 ``bind(0)`` 让系统分配（2026-09-19 实测）：**系统刚分配/释放的临时端口
    会被短暂保留**，立刻再 bind 有约 1/6 的概率失败（12 次里 2 次）——而 `pick()` 返回的
    端口随后要交给 uvicorn 去 bind，这等于把"起不来"的概率引进启动路径。安全带里的端口
    长期空闲，bind→释放→再 bind 稳定。

    ``note`` 说明发生了什么（首选可用时为空串），调用方**应当把它写进日志**——
    端口悄悄变了却没人知道，是这类事故最难查的部分。
    """
    rs = excluded_ranges() if ranges is None else ranges
    ok, why = probe(preferred, host, rs)
    if ok:
        return preferred, ""
    for cand in range(preferred + 1, preferred + window + 1):
        if cand > 65535:
            break
        ok2, _ = probe(cand, host, rs)
        if ok2:
            return cand, "首选 %d %s，改用 %d" % (preferred, why, cand)
    for lo, hi in bands:
        if lo <= preferred <= hi:
            continue
        for cand in range(lo, hi + 1):
            ok3, _ = probe(cand, host, rs)
            if ok3:
                return cand, "首选 %d %s，邻近 %d 个也不可用，改用安全带端口 %d" % (preferred, why, window, cand)
    if allow_os:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, 0))
            port = s.getsockname()[1]
            return port, ("首选 %d %s，安全带也不可用，改由系统分配 %d"
                          "（注意：系统刚分配的端口可能被短暂保留，重试一次通常就好）" % (preferred, why, port))
        except OSError as exc:
            return 0, "无法分配端口: %s" % exc
        finally:
            try:
                s.close()
            except Exception:
                pass
    return 0, "无法分配端口（首选 %d %s，且已禁用系统分配）" % (preferred, why)


# ---------------------------------------------------------------- echo-port.txt

def port_file(root_or_data: str) -> str:
    """端口文件路径。

    参数可以是**数据根**（推荐，2.0 的 ``paths.data_root()``）也可以是安装根
    （1.x 的写法是 ``<root>/data/echo-port.txt``）——后者仍被一堆 PowerShell 脚本使用，
    所以两种都认：已经以 ``data`` 结尾就当数据根，否则补 ``data``。
    """
    base = os.path.abspath(root_or_data)
    if os.path.basename(base).lower() != "data":
        base = os.path.join(base, "data")
    return os.path.join(base, PORT_FILE_NAME)


def read_port_file(root_or_data: str, default: int = 0) -> int:
    """读端口文件；读不到/内容非法时返回 ``default``（不抛异常）。"""
    try:
        with open(port_file(root_or_data), encoding="utf-8") as fh:
            text = fh.read().strip()
        port = int(text)
        return port if 0 < port <= 65535 else default
    except Exception:
        return default


def write_port_file(root_or_data: str, port: int) -> bool:
    """**原子**写端口文件（先写临时文件再 replace）。换行结尾，脚本读起来不用管有无换行。"""
    path = port_file(root_or_data)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".echo-port-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("%d\n" % int(port))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return True
    except Exception:
        return False


def resolve_port(preferred: int, *, host: str = "127.0.0.1", data_root: Optional[str] = None,
                 window: int = 200) -> Tuple[int, str]:
    """``pick()`` + 写回 ``echo-port.txt`` 的组合入口（main.py 用）。

    返回值 ``(port, note)``；``note`` 非空时调用方应当**显式打日志**。
    """
    port, note = pick(preferred, host=host, window=window)
    if port and data_root:
        write_port_file(data_root, port)
    return port, note


def active_port(default: int = 8970, data_root: Optional[str] = None) -> int:
    """ECHO 当前**实际**监听端口：``echo-port.txt`` 优先，读不到才回退 ``default``。

    为什么不能用 ``settings.serverPort``：那只是**首选**端口。被占用或落在
    Windows 保留段时，``main.py`` 会让位并把实际端口写回 ``echo-port.txt``
    （见 ``pick()`` / ``write_port_file()``）。面板/边条/浏览器若仍按配置打开，
    就会连到没人监听的旧端口 —— 页面已渲染但所有请求失败，点按钮报
    "Load failed"（2026-09-20 实际事故：ECHO 让位到 8971，边条还开在 8970）。

    ``data_root`` 省略时用 ``paths.data_root()``（惰性导入，避免循环依赖）。
    """
    if data_root is None:
        try:
            from app import paths
            data_root = paths.data_root()
        except Exception:
            data_root = ""
    if data_root:
        port = read_port_file(data_root, 0)
        if 0 < port <= 65535:
            return port
    try:
        default = int(default)
    except (TypeError, ValueError):
        return 0
    return default if 0 < default <= 65535 else 0
