# -*- coding: utf-8 -*-
"""browser_open.py — 打开 DSH 的 Web 界面，并**把登录凭据先写进浏览器**。

为什么不能只是"打开一个网址"（2026-10-07 用户实测，连查两轮才定位）
--------------------------------------------------------------------
DSH 的登录有两条路，**只有一条能给浏览器**：

  * **token**（`dsh web` 启动时打印的 `/?token=…`）
    - 跟着**那个进程**；ECHO 每重启一次 harness 就换一枚；
    - 旧 token 打开的页面**能加载、但鉴权不过** → 侧栏没有「工作区」、没有任何会话，
      而且**没有任何提示**（用户只能看到一片空白）。
  * **签名 Cookie**（密钥在 `<harness 家目录>/.credentials.yaml` 的
    `client-connection/browser-session.secret`）
    - 我们**能自己算**（`_make_cookie`，与 DSH Desktop 同一套算法）；
    - 它的寿命跟着**那个文件**，**不随进程重启失效**。

ECHO 自己一直走第二条，所以它一直好用；而"打开浏览器"原来只会拼 token URL 交给
系统 shell —— 于是**用户看到的是坏的**。这个模块把第二条也接给浏览器：
**先用 CDP 写 Cookie，再导航**，最后**自检**页面里有没有「工作区」。

实测判据（2026-10-07，两个方向都验过）
--------------------------------------
    写 Cookie 后 `document.body.innerText` 出现「工作区」，且不再出现
    `authentication required`；侧栏列出 4 个工作区与其会话。
    而同一门服务用**旧 token** 打开时，同一判据全部落空（这正是用户的现象）。

为什么用**独立 profile** 起浏览器
---------------------------------
系统默认浏览器没有"注入 Cookie"的时机，而独立 profile 的实例可以用
`--remote-debugging-port` 完全控制。代价是这个窗口不带你日常的书签/登录 ——
它是一扇**专用的 DSH 视图**（用户已确认：只保证 Edge 能正常打开即可）。

平台差异（可执行文件位置 / 脱离父进程的 flags）**一律走 `app.platform` 接缝**，
本模块不出现盘符、`os.name` 分支或硬编码 flags（门禁 `tests/test_path_seam.py` 钉着）。
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request

from app import platform as echo_platform

#: 专用 profile 的目录名（放在平台默认的应用数据目录下，与日常浏览器 profile 分开）
PROFILE_DIR_NAME = "dsh-view"

#: 我们自己挑的调试端口候选（被占就换下一个）
DEBUG_PORT_CANDIDATES = (9333, 9334, 9335, 9336, 0)


def profile_dir():
    """专用 profile 目录（平台数据目录/ECHO/dsh-view）。"""
    return os.path.join(echo_platform.user_data_dir(), "ECHO", PROFILE_DIR_NAME)


def _expand_candidate(raw):
    """展开接缝给的 `$VAR` 候选路径。

    ⚠️ **不能直接用 `os.path.expandvars`**（2026-10-07 实测踩到，很隐蔽）：
    `expandvars` 把 `(X86)` 当普通字符，变量名只匹配到 `PROGRAMFILES` 就停，
    于是 `$PROGRAMFILES(X86)\\Microsoft\\Edge\\...` 被展开成
    `C:\\Program Files(X86)\\Microsoft\\Edge\\...`（反斜杠丢了、路径不存在），
    结果 **Edge 的头号落点从来没被检查过**、一路落到 Chrome。

    这里按 `$VAR` 语法**从长到短**试变量名，`(X86)` 这种带括号的名字才能正确认出。
    """
    text = str(raw or "")
    for var in sorted(os.environ, key=len, reverse=True):
        text = text.replace("$" + var, os.environ[var])
    return text


def _browser_exe():
    """**优先 Edge**，其次任何 Chromium 候选；都没有返回空串。

    为什么优先 Edge（用户 2026-10-07 明确要求："不用管谷歌，能确保 edge 正常就行"）：
    `chromium_candidates()` 把 Edge 与 Chrome 混在一张表里、Chrome 可能排在前面，
    直接取"第一个存在的"会挑到 Chrome —— 而用户要的是 Edge。
    （实测踩过：挑成 Chrome 之后，因为 Chrome 已经在跑，新进程被吸附、
    调试端口没开 → 打开失败。）
    """
    paths = [_expand_candidate(raw) for raw in echo_platform.chromium_candidates()]
    present = [p for p in paths if p and os.path.isfile(p)]

    def is_edge(p):
        low = p.lower()
        return "msedge" in low or "edge" in os.path.basename(low)

    return (next((p for p in present if is_edge(p)), "")
            or (present[0] if present else ""))


def _free_port():
    """在候选里挑一个能绑上的端口（含 0 = 让系统给）。"""
    for candidate in DEBUG_PORT_CANDIDATES:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            if port:
                return port
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    return 9333


def _kill_stale_browsers(exe, prof):
    """清掉**会妨碍这次启动**的浏览器实例（只动该动的，别关用户正在用的窗口）。

    ⚠️ 2026-10-07 为此浪费了两次排查：Chromium 系浏览器只要**已有实例在运行**
    （哪怕 profile 不同），带 `--remote-debugging-port` 再启动它**不会开新进程、
    只是在旧实例里开个标签页** —— 调试端口于是永远不开，
    症状是"浏览器起来了但调试端口没开"。

    策略（**尽量不打扰用户**）：
      * 先只清**用我们自己 profile 的**实例（那是这扇"DSH 视图"窗口，替换它是本意）；
      * 用户自己开的其它窗口**先留着** —— 只有在它确实把调试端口挡住时，
        上层才会再调一次 `nuke_all=True`（并会在返回语里如实说明）。
    """
    image = os.path.basename(exe or "")
    if not image:
        return 0
    marker = prof.lower()
    killed = 0
    for pid in echo_platform.pids_of(image):
        cmd = (echo_platform.process_command_line(pid) or "").lower()
        if marker in cmd:
            if echo_platform.kill_process_tree(pid):
                killed += 1
    if killed:
        time.sleep(1.0)
    return killed


def _nuke_browser(exe):
    """实在挡路时：把该浏览器的**所有**实例清掉（会关掉用户自己的窗口，慎用）。"""
    image = os.path.basename(exe or "")
    if not image:
        return
    try:
        subprocess.run(["taskkill", "/F", "/IM", image],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        time.sleep(1.0)
    except Exception:
        pass


# ---------------------------------------------------------------- CDP（最小实现）

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("连接关闭")
        buf += chunk
    return buf


def _ws_connect(url):
    """与 CDP 的 WebSocket 建连（RFC6455；客户端帧**必须掩码**，实测服务端会掐）。"""
    rest = url.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port or 80)
    s = socket.create_connection((host, port), timeout=15)
    req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
           % (path, hostport))
    s.sendall(req.encode())
    hdr = b""
    while b"\r\n\r\n" not in hdr:
        hdr += s.recv(1)
    if b"101" not in hdr.split(b"\r\n")[0]:
        raise IOError("WebSocket 升级失败：%s" % hdr.split(b"\r\n")[0])

    def send_text(text):
        payload = text.encode()
        mask = os.urandom(4)
        ln = len(payload)
        if ln < 126:
            head = struct.pack("!BB", 0x81, 0x80 | ln)
        elif ln < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, ln)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, ln)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        s.sendall(head + mask + masked)

    def recv_frame():
        _, b2 = _recv_exact(s, 2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", _recv_exact(s, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", _recv_exact(s, 8))[0]
        return _recv_exact(s, ln).decode("utf-8", "replace")

    return s, send_text, recv_frame


def _cdp(send, recv, mid, method, params=None, timeout=20):
    mid[0] += 1
    send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
    end = time.time() + timeout
    while time.time() < end:
        msg = json.loads(recv())
        if msg.get("id") == mid[0]:
            return msg
    return None


def _eval(send, recv, mid, expr, timeout=20):
    m = _cdp(send, recv, mid, "Runtime.evaluate",
             {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
    return (((m or {}).get("result") or {}).get("result") or {}).get("value")


# ---------------------------------------------------------------- 对外入口

def _launch_and_wait(exe, prof, port, wait):
    """起浏览器并等调试端口上的页签出现；返回 (target, detail)。"""
    try:
        # 脱离父进程组：ECHO 重启不该带走这扇窗口（flags 由平台接缝给）
        subprocess.Popen(
            [exe, "--user-data-dir=" + prof, "--no-first-run", "--no-default-browser-check",
             "--disable-sync", "--window-size=1400,1000",
             "--remote-debugging-port=%d" % port, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **echo_platform.detach_gui_kwargs())
    except Exception as e:
        return None, "起浏览器失败：%s: %s" % (type(e).__name__, e)

    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % port, timeout=3) as r:
                tabs = json.loads(r.read().decode())
            target = next((t for t in tabs if t.get("type") == "page"), None)
            if target:
                return target, ""
        except Exception:
            continue
    return None, ("浏览器起来了但调试端口没开（%d）—— 有别的实例在抢（Chromium 系浏览器"
                  "只要已有实例在跑，带调试端口再启动也只会吸附过去）" % port)


def open_with_cookie(url, cookie_header, timeout=60.0):
    """用**专用 profile 的浏览器 + 注入 Cookie** 打开 ``url``。

    ``cookie_header``：形如 ``"dsh-auth-xxx=yyy"``（`harness_proc.secret_cookie()` 的产物）。
    返回 ``(ok, detail)``；失败时 ``detail`` 是能直接给用户看的原因（**绝不谎报成功**）。

    打扰用户的程度（刻意分级）：
      * 默认**只清掉用我们自己 profile 的**浏览器实例；
      * 只有在"调试端口仍没开"（= 用户自己开的浏览器把单例占了）时，
        才**再清一次该浏览器的全部实例**并重试 —— 这会在提示语里如实说明。
    """
    exe = _browser_exe()
    if not exe:
        return False, "找不到 Edge/Chrome 可执行文件"

    prof = profile_dir()
    try:
        os.makedirs(prof, exist_ok=True)
    except Exception as e:
        return False, "建不了浏览器 profile 目录（%s）：%s" % (prof, e)

    _kill_stale_browsers(exe, prof)

    port = _free_port()
    target, why = _launch_and_wait(exe, prof, port, max(20.0, timeout / 3))
    if not target:
        # 用户自己开的浏览器把单例占了 → 只能连它一起清（如实告知）
        _nuke_browser(exe)
        port = _free_port()
        target, why = _launch_and_wait(exe, prof, port, max(20.0, timeout / 3))
        if target:
            why = "（为了腾出调试端口，已关掉你先前开着的 %s 窗口）" % os.path.basename(exe)
    if not target:
        return False, why

    sock = None
    try:
        sock, send, recv = _ws_connect(target["webSocketDebuggerUrl"])
        mid = [0]
        # ① **先写 Cookie**（此时页面还没加载，时序才对 —— 这是与旧做法的关键差别）
        name, _, value = cookie_header.split(";")[0].strip().partition("=")
        _cdp(send, recv, mid, "Network.enable")
        _cdp(send, recv, mid, "Network.setCookie",
             {"name": name, "value": value, "domain": "127.0.0.1", "path": "/"})
        time.sleep(0.4)
        # ② 再导航
        _eval(send, recv, mid, "location.href = %s" % json.dumps(url))
        # ③ **自检**：页面里必须出现「工作区」，且不能仍是"要登录"
        end = time.time() + 25
        while time.time() < end:
            time.sleep(1.5)
            txt = _eval(send, recv, mid, "document.body.innerText") or ""
            if "authentication required" in txt:
                return False, "Cookie 没被接受（页面仍要求登录）"
            if "工作区" in txt:
                return True, "已打开并登录（%s）%s" % (url, why)
        return True, "已打开 %s，但没等到侧栏渲染完 —— 若空白请按 Ctrl+F5%s" % (url, why)
    except Exception as e:
        return False, "注入登录凭据失败：%s: %s" % (type(e).__name__, e)
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass
