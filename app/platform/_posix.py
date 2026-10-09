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


def pid_alive(pid: int) -> bool:
    """那个 pid 现在还在跑吗（POSIX：``os.kill(pid, 0)`` 只做存在性检查，**不发真信号**）。

    返回 False 的两种情形要分清（调用方靠 pid 记录做归属，两种都不该动手）：
      * ``ProcessLookupError`` —— 进程确实没了；
      * 权限不足（``PermissionError``）—— 进程在，但不归我们看/发信号：这里返回 True
        （"可能活着"），免得把"别人的进程"误判成"我们的进程已退出"而清掉记录。
    """
    import os
    if int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def process_label(pid: int) -> str:
    """pid 的可执行名（``ps -p <pid> -o comm=``）；说不出来 = 空串。

    只是给人看的人话（"8900 被 python3（pid 1234）占着"），不是判据。
    """
    import subprocess
    if int(pid) <= 0:
        return ""
    for args in (["ps", "-p", str(int(pid)), "-o", "comm="],
                 ["ps", "-p", str(int(pid)), "-o", "ucomm="]):
        try:
            out = (subprocess.run(args, capture_output=True, text=True,
                                  timeout=5).stdout or "").strip()
        except Exception:
            continue
        if out:
            return out.splitlines()[0].strip()
    return ""


def process_command_line(pid: int) -> str:
    """pid 的**完整命令行**（取不到 = 空串，永不抛）。

    用途与 Windows 那份一致：判断"占用 8900 的后端属于哪棵树"要看命令行的
    ``--config <后端目录>``（见 ``app/backend_proc.py`` 的归属判据）。

    Linux 优先读 ``/proc/<pid>/cmdline``（NUL 分隔，无外部依赖、最快）；
    macOS 没有 procfs，退化用 ``ps -ww -o command=``（``-ww`` 是关键：不加会被截断，
    而我们要找的正是命令行**后半段**的 ``--config`` 路径）。
    """
    import subprocess
    pid = int(pid)
    if pid <= 0:
        return ""
    proc_cmdline = "/proc/%d/cmdline" % pid
    try:
        with open(proc_cmdline, "rb") as fh:
            raw = fh.read()
        if raw:
            parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
            if parts:
                return " ".join(parts)
    except Exception:
        pass
    for args in (["ps", "-ww", "-p", str(pid), "-o", "command="],
                 ["ps", "-p", str(pid), "-o", "args="]):
        try:
            out = (subprocess.run(args, capture_output=True, text=True,
                                  timeout=5).stdout or "").strip()
        except Exception:
            continue
        if out:
            return out.splitlines()[0].strip()
    return ""


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


def offline_tts_render(text: str, path: str, timeout: int = 60,
                       renderers=(("say", ["-o", None, "--data-format=LEI16@22050"]),
                                  ("espeak-ng", ["-w", None]),
                                  ("espeak", ["-w", None]))) -> bool:
    """离线合成**到文件**（对应 Windows 的 `SetOutputToWaveFile`；不播）。

    为什么需要它（2026-10-09 手机/手表触点）：`offline_tts_speak()` 的语义是"在本机喇叭播"，
    而手机/手表要的是"**把音频字节拿走**" —— 引擎一样，只是最后一跳不同。

    各平台"写到文件"的开关不同（macOS `say -o`、Linux `espeak-ng -w`；`spd-say` 没有），
    所以用 `renderers` 描述成 `(可执行文件, 参数模板)`：模板里的 `None` 位置换成 `path`。
    哪个平台给哪份清单由各自的 `env.py` 决定。
    """
    import os
    import subprocess
    if not (text or "").strip():
        return False
    for exe, args in renderers:
        argv = [exe] + [path if a is None else a for a in args] + [text]
        try:
            proc = subprocess.run(argv, timeout=timeout,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        try:
            # > 44 字节：wav 头本身就要 44 字节，"只有头"等于没合成出东西
            if proc.returncode == 0 and os.path.isfile(path) and os.path.getsize(path) > 44:
                return True
        except OSError:
            continue
    return False


# ------------------------------------------------------------------ 秘密保护（凭据落盘）
#
# 与 Windows 那侧（DPAPI，绑用户账户）对称：POSIX 上**没有等价的"绑用户"系统服务**，
# 所以这里的墙是文件权限 `0600`。这是一处**明确的取舍**，不是"忘了加密" ——
# 谁想给 Linux 也加一层，得先在这里实现，再动那侧的说明。
# 取舍由 tests/test_backend_credentials.py 的一条用例写在明处（它叫
# `test_posix_plain_envelope_is_the_documented_tradeoff`）。

def protect_secret_kind() -> str:
    """保护方式的标记，写进信封（`dpapi` / `plain`）。"""
    return "plain"


def protect_secret(data: bytes) -> bytes:
    """原样返回 —— 真保护靠 `restrict_file()` 的 0600。"""
    return bytes(data)


def unprotect_secret(blob: bytes) -> bytes:
    return bytes(blob)


def restrict_file(path: str) -> None:
    """收紧到 0600。失败不抛：有些文件系统不支持改权限，调用方另有兜底。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
