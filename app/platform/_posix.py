# -*- coding: utf-8 -*-
"""POSIX（Linux / macOS）共享原语：单实例锁（``flock``）+ 运行时替换。

放在 ``app/platform/`` 下的**共享**实现，由 ``linux/env.py`` 与 ``darwin/env.py`` 转发——
两个平台语义基本相同（差异只有命令名），没必要抄两份。Windows 那套见 ``win32/env.py``。

锁由 fd 持有：进程退出（含崩溃）时内核自动释放，不留需要人工清理的陈旧锁文件。

⚠️ 运行时原语（进程查询 / 打开窗口 / 提示音 / 离线 TTS）**尚未在 macOS 上实测**
（S7/S9/S10 缺机器，按 P3 纪律记为未验证风险）：只保证"契约与 Windows 一致 + 不抛异常"，
不承诺可运行。
"""
import os


def acquire(path):
    """尝试独占 ``path``。返回 ``(fd, detail)``；已被别处持有时 fd 为 None。"""
    import fcntl
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None, "已有同名实例在运行（%s）" % path
    try:                       # 写入 pid 便于排查（锁本身不依赖文件内容）
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    return fd, path


def held(path) -> bool:
    """该锁当前**是否存在**（含本进程自己持有）。只探测，不获取。"""
    import fcntl
    if not os.path.isfile(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True            # 别人持着
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def release(fd):
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ------------------------------------------------------------------ 子进程

def detach_kwargs():
    """POSIX 下"脱离父进程组" = 新会话（``setsid``）。

    对应 Windows 的 ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``：目的是让 helper
    在父进程被杀之后继续活着（ECHO 自我重启靠这个）。
    """
    return {"start_new_session": True}


def process_running(image_name: str) -> bool:
    """按名字查进程（``pgrep -f``）；命令不存在/查不到一律 False（永不抛）。"""
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", image_name],
                             capture_output=True, text=True, timeout=5).stdout or ""
        return bool(out.strip())
    except Exception:
        return False


def kill_process_tree(pid: int) -> bool:
    """杀进程组（独立 harness 的 npx 会套多层 shell，只杀最外层不够）。"""
    import os
    import signal
    if pid <= 0:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False


def listening_pid(port: int) -> int:
    """谁在监听本机某端口（``lsof`` 优先，退化 ``ss``）。找不到 = 0。"""
    import subprocess
    for cmd in (["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                ["ss", "-lptn", "sport = :%d" % port]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout or ""
        except Exception:
            continue
        for tok in out.replace(",", " ").split():
            if tok.isdigit() and int(tok) > 0:
                return int(tok)
    return 0


def console_shell_argv(script: str):
    """起一个控制台脚本的 argv（POSIX = ``/bin/sh``）。"""
    return ["/bin/sh", script]


# ------------------------------------------------------------------ 打开窗口 / 提示音 / 离线 TTS

def shell_open(target: str, params: str = "", opener=("open",)) -> bool:
    """用系统 shell 打开 URL 或可执行文件。

    * ``params`` 为空：交给平台 opener（macOS ``open`` / Linux ``xdg-open``）；
    * ``params`` 非空：直接执行 ``target``（Chromium 系带 ``--app=…`` 启动）。

    与 Windows 的 ``ShellExecuteW("open", …)`` 语义对齐：非阻塞、失败返回 False。
    """
    import shlex
    import subprocess
    try:
        argv = ([target] + shlex.split(params)) if params else (list(opener) + [target])
        subprocess.Popen(argv, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def play_wav_async(path: str, players=(("afplay",),)) -> bool:
    """异步播放一个 wav（对应 Windows 的 ``winsound.PlaySound(..., SND_ASYNC)``）。

    玩家命令**逐个尝试**（Linux 上 paplay/aplay 不一定都装了）；全失败返回 False。
    """
    import subprocess
    if not os.path.isfile(path):
        return False
    for player in players:
        try:
            subprocess.Popen(list(player) + [path], close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def offline_tts_speak(text: str, timeout: int = 60,
                      engines=(("say",), ("espeak-ng",), ("spd-say",))) -> bool:
    """离线朗读（对应 Windows 的 SAPI）：逐个尝试可用的本地合成器。"""
    import subprocess
    if not (text or "").strip():
        return False
    for engine in engines:
        try:
            proc = subprocess.run(list(engine) + [text], timeout=timeout,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if proc.returncode == 0:
                return True
        except Exception:
            continue
    return False
