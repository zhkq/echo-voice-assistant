# -*- coding: utf-8 -*-
"""ECHO 自己起的后端的 pid 记录与**归属判据**（本步只读不杀）。

这是什么
--------
ECHO 起的后端进程，归属记录落在 ``{DATA}/logs/backend.pid``。本模块只提供四件事：

    ``pid_path()``       -> pid 文件路径（走 ``paths.data_root()``，**可整体打桩**，不硬编码）
    ``log_paths()``      -> ``(backend.log, backend.err)`` 两个路径（只给路径，**不创建文件**）
    ``read_pid()``       -> 读 pid 返回 int；文件不存在 / 内容不是数字 -> ``None``（**永不抛**）
    ``is_ours_alive()``  -> ``(alive, note)``：只有"pid 文件在 + 那个 pid 确实活着 +
                            证书就是 ECHO 自己记下的那份"才 ``True``
    ``write_pid()`` / ``clear_pid()`` -> 落盘/删除**归属记录**（写只有 ECHO 起的那个才写）

本步**只做这些**：``stop()``（杀进程）、端口查询、起进程、装东西、写配置、配对，
全部留给后面的小步（``app/backend_proc.py`` 等）。理由：**"停"是唯一不可逆的动作**，
必须建立在可靠的归属判据之上 —— 所以先把"读/判"这一层做完、测透，再让它去决定杀谁。

那件东西记在哪
--------------
pid 文件是唯一的归属凭据。本步**没有**任何"按进程名 / 按端口 / 按镜像名"去找目标的代码，
将来也**不许**有：那种判据分不清"ECHO 起的后端"与"用户手工起的后端"。

铁律（这条是**理由**，不是口号）
--------------------------------
**只停 ECHO 自己起的后端；用户手工起的实例（例如
``data/backend-dev/run-backend.cmd``）绝不允许被 ECHO 停掉。**

用户名下常常同时跑着好几个后端（手工调试的、别的分支的、稳定版的），它们和 ECHO 起的
那个**长得一模一样**（同一个解释器、同一套依赖）：一旦退化成"看到进程就停"，
就会把用户正在调试/正在用的实例打死。同类事故在 harness 上真发生过 ——
单测读到**真实**的 ``harness.pid``，认定"这是 ECHO 起的"，于是把开发者正在跑的标准版
harness 杀了（见 ``AGENTS.md``「跑单测会把开发机上正在跑的标准版 harness 杀掉」）。
所以：归属**只能**来自 ECHO 自己落盘的 pid 记录；记录失效（进程没了）时必须如实回答
"不是我们的 / 已陈旧"，并顺手把它清掉 —— 而不是猜一个来停。
"""
import os

from app import paths

#: pid 文件与两份日志的文件名（日志路径只返回，不落盘 —— 落盘是后面小步的事）
PID_FILENAME = "backend.pid"
LOG_FILENAME = "backend.log"
ERR_FILENAME = "backend.err"


def _logs_dir():
    """``{DATA}/logs`` —— pid 与日志的父目录（测试里打桩**这一个**就够全隔离）。"""
    return os.path.join(paths.data_root(), "logs")


def pid_path():
    """pid 文件路径：``{DATA}/logs/backend.pid``。

    刻意每次现算（照 ``harness_proc._pid_path()`` 的写法），不缓存、不硬编码 ——
    这样测试可以把 ``_logs_dir`` 指到临时目录，绝不会碰到真实的 ``data/logs/backend.pid``。
    """
    return os.path.join(_logs_dir(), PID_FILENAME)


def log_paths():
    """后端的 stdout / stderr 日志路径（**只返回路径，不创建、不写任何文件**）。"""
    logs = _logs_dir()
    return os.path.join(logs, LOG_FILENAME), os.path.join(logs, ERR_FILENAME)


def read_pid():
    """读 pid 文件的整数；文件不存在 / 读不动 / 内容不是数字 -> ``None``（永不抛）。

    非正数（0 / 负数）同样返回 ``None``：它们不是合法 pid，而 ``kill(0)`` 这种写法会
    打到整个进程组 —— 与其把危险值传下去，不如在这里就判成"没有记录"。
    """
    try:
        with open(pid_path(), "r", encoding="utf-8-sig") as fh:
            raw = (fh.read() or "").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def write_pid(pid) -> bool:
    """把归属记进 pid 文件（``{DATA}/logs/backend.pid``）：**只有 ECHO 起的后端才写这里**。

    返回是否写成功。写不进去必须被调用方如实报出来 —— 没有这条记录，将来 ``stop()``
    就不敢认这个进程，表现是"起得来、停不掉"（还要用户自己去任务管理器里翻）。
    """
    try:
        path = pid_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(int(pid)))
        return True
    except Exception:
        return False


def clear_pid() -> bool:
    """删掉 pid 记录（**只在进程已确认停下之后 / 记录已陈旧时**调用）。

    文件本来就不在也算成功（"没有记录"与"记录已清"对调用方是同一个结果）。
    """
    try:
        os.remove(pid_path())
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def _pid_alive(pid):
    """那个 pid 现在是否还活着（**只查询，绝不发送任何信号**；永不抛）。

    平台差异走接缝 ``app.platform.pid_alive()``（D12：Windows API 只能出现在
    ``app/platform/<os>/`` 里）。**为什么不用 ``os.kill(pid, 0)`` 自己写**：
    Windows 上 ``signal.CTRL_C_EVENT == 0``，那句会变成"给那个进程组发 Ctrl+C" ——
    探活把对端打断，而不是探活（本机实测踩过，见接缝里的说明）。
    """
    from app import platform as echo_platform
    try:
        return bool(echo_platform.pid_alive(pid))
    except Exception:
        return False


def is_ours_alive():
    """"这个后端是 ECHO 起的、现在还在跑吗？" -> ``(alive, note)``。

    判据（三条同时成立才 ``True``）：
      1. pid 文件存在（只有 ECHO 会往 ``{DATA}/logs/backend.pid`` 里写）；
      2. 文件里的 pid 是**正整数**；
      3. 那个 pid **确实还活着**（只看不动地查）。

    ``note`` 是一句人话，说明判据、以及**顺手清掉陈旧 pid 文件**这件事：
    pid 已经消失时，这个文件就是**陈旧记录**，本函数会删掉它，并在 ``note`` 里写明
    "已清掉"（调用方据此就能知道"清掉了"）—— 留着它会误导后面的 ``stop()`` 去杀一个
    已经不存在、甚至被系统复用给别人的 pid。
    """
    path = pid_path()
    if not os.path.exists(path):
        return False, "没有 pid 记录（%s 不存在）：不是 ECHO 起的后端" % path
    pid = read_pid()
    if pid is None:
        return False, ("pid 记录（%s）内容不是有效 pid：不认它，未做处理" % path)
    if _pid_alive(pid):
        return True, "pid=%d 记在 %s 且进程仍在：按记录算 ECHO 自己起的" % (pid, path)
    note = "pid=%d（记在 %s）已不存在：陈旧记录，已清掉 pid 文件" % (pid, path)
    if not clear_pid():
        note = "pid=%d（记在 %s）已不存在：陈旧记录（pid 文件未能清掉）" % (pid, path)
    return False, note
