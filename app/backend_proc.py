# -*- coding: utf-8 -*-
"""ECHO 自己起的后端的**进程层**：起 / 停 / 端口占用者（客户端简化第 3 步 · 批 1b）。

这一层只做三件事
----------------
    ``spawn(argv, …)``        起一个后端进程，并把**归属**写进 pid 文件
    ``stop(reason, ports)``   停 **ECHO 自己起的** 那个；手工起的实例一个字节都不动
    ``port_check(ports)``     要用的端口被谁占着（是我们自己 → 已经不缺后端；是别人 → 不起第二个）

配置怎么生成、什么时候等配对文件、失败了怎么如实说，都不在本模块 —— 那是 1c/1d 的事。
这样切分的理由：**"停"是唯一不可逆的动作**，它必须建立在"这条进程确实是我们起的"之上；
把"读/判"（``app/backend_pid.py``）、"起停"（本模块）、"编排与文案"（1c/1d）分开，
每一层都能单独测透。

归属判据只有一个（**这条是铁律，不是偏好**）
------------------------------------------
``{DATA}/logs/backend.pid`` —— ECHO 亲自 Popen 之后写下的那条记录。本模块**没有**任何
"按进程名 / 按端口 / 按镜像名去猜目标"的代码，将来也**不许**有：用户名下常常同时跑着
好几个后端（手工调试的、别的分支的、稳定版的），它们与 ECHO 起的那个**长得一模一样**
（同一个解释器、同一套依赖）。一旦退化成"看到进程就停"，就会把用户正在调试/正在用的实例
打死 —— 同类事故在 harness 上真发生过（单测读到**真实**的 ``harness.pid`` 就把开发者
正在跑的标准版服务杀了，见 ``AGENTS.md``）。所以：

* 记录在 + 那条 pid 确实活着 → 才是"我们的"，才可以停；
* 没有记录 / 记录陈旧 → **如实说"不是 ECHO 起的"并收手**，绝不猜一个来停。

端口那一半同理：端口被占只说明"起不来"，**不授权**我们去杀占用者。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import List, Optional, Sequence, Tuple

from app import backend_pid, platform, paths

#: 本机后端的标准端口（能力面 / 管理面）。服务端出厂值见 ``server/settings.py``。
DEFAULT_PORT = 8900
DEFAULT_ADMIN_PORT = 8901

#: **解释器在 `runtime/` 下的可能落点**（唯一判据：`python_exe()` 与出包侧的
#: `abi_check()` / `verify()` 都用这一份 —— 2026-09-30 就是因为只写了 venv 那两种，
#: 薄包"只带独立 CPython"时被误判成"没有解释器"）。
PYTHON_RELS: Tuple[str, ...] = (
    os.path.join("runtime", "python.exe"),             # 独立 CPython（uv 托管 / embeddable）
    os.path.join("runtime", "Scripts", "python.exe"),  # Windows venv
    os.path.join("runtime", "bin", "python3"),         # POSIX venv / 独立 CPython
    os.path.join("runtime", "bin", "python"),
    os.path.join("runtime", "python"),
)
#: 停了之后等它真的退出（杀树是异步的）与再确认一次的上限（秒）。
STOP_TIMEOUT = 8.0
#: 每次确认之间的间隔（秒）。
POLL_INTERVAL = 0.2
#: 本进程 Popen 过的那个子进程的句柄。**它不是归属判据**（归属只看 pid 文件，见模块头），
#: 留一个引用只为两件事：① ``stop()`` 里给它收尸（Popen 没被 ``wait()`` 过的话，GC 时会报
#: 一句 "subprocess N is still running" 的 ResourceWarning，日志里看着像没停干净）；
#: ② 它被 GC 时那个进程本来就是我们故意留着跑的，不该报警。ECHO 重启后这个引用自然没了，
#: 那时仍然靠 pid 文件接手。
_proc = None


# ---------------------------------------------------------------- 位置 / 解释器

def backend_root() -> str:
    """后端自己的家：``{echoBase}/backend``（老式扁平安装 = ``{DATA}/backend``）。

    转发 ``paths.backend_root()``，**测试只打桩这一个**就能把整个模块指到临时目录。
    """
    return paths.backend_root()


def python_exe() -> str:
    """扩展包路里那个**独立 venv** 的解释器全路径；没有 = 空串。

    容器路不需要它（镜像里有自己的 python）。判据只看"这个文件在不在"——
    不猜 PATH 上的 python：拿系统 python 去跑后端，缺依赖时的表现是
    "起得来、每个 ``/v1/asr`` 都 503"，而那要到真跑一场会议才现形。
    """
    root = backend_root()
    # 认三种布局（2026-09-30 补第一种）：
    #   * `runtime/python.exe` / `runtime/python` —— **独立 CPython** 的布局（uv 托管的那份与
    #     官方 embeddable 包都把解释器直接放在根目录）。薄包"只带解释器"那一档正是这种；
    #     补之前认不出来 —— 表现是"薄包明明带了 Python，面板却说没有解释器"。
    #   * `runtime/Scripts/python.exe` —— Windows venv 的布局（`uv venv` / `python -m venv`）。
    #   * `runtime/bin/python3` / `bin/python` —— POSIX venv / 独立 CPython 的布局。
    # 这份清单是**唯一判据**：出包侧的 `abi_check()` / `verify()` 也用它（别在两处各写一遍）。
    for rel in PYTHON_RELS:
        cand = os.path.join(root, rel)
        if os.path.isfile(cand):
            return cand
    return ""


def _resolve_exe(argv: Sequence[str]) -> str:
    """argv[0] 解析成真路径（Windows 上 ``python`` 可能是 ``python.exe``）；找不到 = 空串。"""
    if not argv:
        return ""
    exe = str(argv[0])
    if os.path.isabs(exe):
        return exe if os.path.isfile(exe) else ""
    return shutil.which(exe) or ""


# ---------------------------------------------------------------- 端口占用者

def port_owner(port: int) -> dict:
    """谁在监听 ``port``：``{"port": int, "pid": int, "label": str}``；没人监听则 ``pid=0``。

    只**问**，不动手：占用者可能是同事的后端、用户手工起的实例、甚至是别的软件。
    """
    pid = 0
    try:
        pid = int(platform.listening_pid(int(port)))
    except Exception:
        pid = 0
    label = ""
    if pid > 0:
        try:
            label = str(platform.process_label(pid) or "")
        except Exception:
            label = ""
    return {"port": int(port), "pid": pid, "label": label}


def describe_owner(owner: dict) -> str:
    """把 ``port_owner()`` 的结果说成人话：``8900 被 python.exe（pid 1234）占着``。"""
    port = owner.get("port", 0)
    pid = int(owner.get("pid") or 0)
    if pid <= 0:
        return "%s 空着" % port
    label = str(owner.get("label") or "").strip()
    return "%s 被 %s（pid %d）占着" % (port, label or "一个名字取不到的进程", pid)


def port_check(ports: Optional[Sequence[int]] = None,
               owners: Optional[Sequence[dict]] = None) -> Tuple[bool, str]:
    """要用的端口能不能用 → ``(ok, detail)``。**不起第二个**，也不替用户杀占用者。

    判据分三种（``detail`` 一律说出"是谁"）：

    * 端口空着 → ``(True, "… 空着")``；
    * 占用者**就是 ECHO 自己起的那个**（占用的 pid == pid 记录里那条，且那条确实活着）
      → ``(True, …)``：不冲突，**已经在跑了**（调用方据此直接进入配对，而不是再起一个）；
    * 占用者是别人（同事的后端 / 用户手工起的实例 / 别的软件）→ ``(False, …)``：
      如实说出占用者，让用户决定（停它 / 换端口），**我们不替他动手**。

    ``ports=None`` 用本机后端的标准两个端口（8900 / 8901）。
    ``owners``（2026-09-30）：调用方已经查过"谁在监听"时直接传进来 —— 查一次要跑
    `netstat`（本机实测 ~0.15 s/端口），而面板每次刷新都要这份答案；**判据只有这一处**，
    所以是"把结果传进来"，不是在调用方再算一遍（那正是两处判据会漂开的开端）。
    """
    wanted: List[int] = [int(p) for p in (ports if ports is not None
                                         else (DEFAULT_PORT, DEFAULT_ADMIN_PORT))]
    ours_pid = 0
    ours_alive, _ = backend_pid.is_ours_alive()
    if ours_alive:
        ours_pid = int(backend_pid.read_pid() or 0)
    if owners is None:
        owners = [port_owner(p) for p in wanted]
    ours_ports, strangers = [], []
    for owner in owners:
        if int(owner.get("pid") or 0) <= 0:
            continue
        if ours_pid and int(owner["pid"]) == ours_pid:
            ours_ports.append(owner)
        else:
            strangers.append(owner)
    if strangers:
        return False, ("端口被占：%s。这不是 ECHO 起的后端 —— 同机只跑一个后端，"
                       "请先停掉它或换端口（ECHO 不会替你停别人的进程）"
                       % "；".join(describe_owner(o) for o in strangers))
    if ours_ports:
        return True, ("ECHO 起的后端已经在跑（%s），不再起第二个"
                      % "；".join(describe_owner(o) for o in ours_ports))
    return True, "端口空着：%s" % "、".join(str(p) for p in wanted)


# ---------------------------------------------------------------- 起

def spawn(argv: Sequence[str], cwd: str = "", env: Optional[dict] = None,
          ports: Optional[Sequence[int]] = None) -> Tuple[bool, str]:
    """起一个后端进程，**并把归属写进 pid 文件**。返回 ``(ok, detail)``。

    前置两道闸（都不动手，只如实说）：

    1. pid 记录里那条还活着 → 不起第二个，直接说"已经有一个在跑（pid=…）"；
    2. 端口被**别人**占着 → 不起，并说出占用者是谁（``port_check``）。

    启动细节（都有理由，别改成"看着更简单"的写法）：

    * ``cwd`` 默认后端自己的家（``backend_root()``）—— ``python -m server.main`` 要在
      能看见 ``server/`` 的目录里跑；
    * 无控制台窗口：``platform.detach_console_kwargs()``（``CREATE_NO_WINDOW`` + 新进程组）。
      **不要**用裸 ``Popen``：uv 建的 venv 里 ``python.exe`` 是 trampoline，会再 re-exec 一个
      控制台子系统的解释器，那个孙子进程自己开一个可见窗口（``AGENTS.md`` 记过这个坑）；
    * stdout/stderr 直接**追加到文件**（不是管道）：管道需要活着的读端，而 ECHO 迟早会重启，
      读端一死写满就卡住（与 harness 那边同一条教训）；
    * 落盘 pid **失败也要如实说**：没有这条记录，将来 ``stop()`` 就不敢认它。
    """
    global _proc
    argv = [str(a) for a in (argv or []) if str(a)]
    if not argv:
        return False, "启动命令为空：不知道要起什么"
    alive, note = backend_pid.is_ours_alive()
    if alive:
        return False, "ECHO 起的后端已经在跑（%s），不再起第二个" % note
    ok, detail = port_check(ports)
    if not ok:
        return False, detail
    exe = _resolve_exe(argv)
    if not exe:
        return False, ("找不到可执行文件 %s：扩展包路要先装好 runtime（%s），"
                       "容器路则不该走到这里" % (argv[0], backend_root()))
    workdir = cwd or backend_root()
    if not os.path.isdir(workdir):
        return False, "后端目录不存在：%s（先解包/装好再起）" % workdir
    log_path, err_path = backend_pid.log_paths()
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except Exception:
        pass
    child_env = dict(os.environ)
    if env:
        child_env.update({str(k): str(v) for k, v in env.items()})
    flags = {}
    try:
        flags = platform.detach_console_kwargs()
    except Exception:
        flags = {}
    # 启动横幅：日志是追加的，没有分隔时"上一轮的 traceback + 这一轮的"堆在一起，
    # 看着像同一个进程反复重启（harness 那边排障时踩过）。
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n===== 后端启动 %s cwd=%s =====\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), workdir))
    except Exception:
        pass
    try:
        out = open(log_path, "ab")
        err = open(err_path, "ab")
    except Exception as e:
        return False, "打不开后端日志（%s）：%s" % (os.path.dirname(log_path), e)
    try:
        proc = subprocess.Popen([exe] + argv[1:], cwd=workdir, env=child_env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                close_fds=True, **flags)
    except Exception as e:
        return False, "起后端失败：%s（命令：%s）" % (e, " ".join(argv))
    finally:
        # Popen 已经复制了句柄：父进程这边必须关掉，否则日志文件被我们一直占着
        # （Windows 上还会挡住"删掉 backend 目录重来"）。
        for fh in (out, err):
            try:
                fh.close()
            except Exception:
                pass
    if not backend_pid.write_pid(proc.pid):
        # 起了但记不下来：**不能就这么留着** —— 一个没有归属记录的进程，将来 stop() 不敢认它，
        # 用户只能自己去任务管理器里翻（而它正占着 8900）。所以就地把它收掉，再如实报错。
        try:
            platform.kill_process_tree(proc.pid)
            proc.wait(timeout=STOP_TIMEOUT)
        except Exception:
            pass
        return False, ("后端起来了但**归属记录写不进去**（%s）：已把刚起的 pid=%d 收掉，"
                       "没留下没人认领的进程；请检查该目录可写后再试"
                       % (backend_pid.pid_path(), proc.pid))
    _proc = proc
    return True, "已启动后端 pid=%d（cwd=%s，日志 %s）" % (proc.pid, workdir, log_path)

# ---------------------------------------------------------------- 停

def stop(reason: str = "", ports: Optional[Sequence[int]] = None) -> Tuple[bool, str]:
    """停 **ECHO 自己起的** 后端。返回 ``(ok, detail)``；手工起的实例一个字节都不动。

    判据只有一条：``backend_pid.is_ours_alive()``（pid 文件 + 那条 pid 确实活着）。
    记录陈旧时它会顺手清掉文件，并把"已清掉"写进 note —— 本函数照抄这句人话返回，
    **不猜、不动手**。

    收尾：杀**整棵进程树**（``platform.kill_process_tree``）。只 ``terminate()`` 最外层不够：
    venv 的 ``python.exe`` 是 trampoline，真正的服务是它的子进程（``AGENTS.md`` 记过）。

    杀了之后端口还占着的话**如实说**，但**不补刀**：那时端口上的进程已经不是记录里那条，
    归属不明 —— 猜着杀就是我们要避免的那类事故（harness 那边用"谁在监听就杀谁"兜底，
    是因为那条路径全是 ECHO 自己起的 node；这里不是）。**进程确实停了就算成功**，
    端口那句只是附注（返回 ``ok=True`` + 一句"仍有 … 占着，请自行确认"）。

    ``ports``：要顺带看一眼的端口；``None`` 用本机后端的标准两个（8900 / 8901），
    传 ``()`` 就只看"我们那条进程有没有停"。为什么留这个口子：**用例不许依赖本机的
    端口状态**（开发机上常常真的有个后端在跑），而真实调用方（1c/1d）知道自己配了哪两个。
    """
    global _proc
    alive, note = backend_pid.is_ours_alive()
    if not alive:
        return True, "没有可停的后端：%s" % note
    pid = int(backend_pid.read_pid() or 0)
    if pid <= 0:
        return True, "没有可停的后端：pid 记录读不出有效值（%s）" % backend_pid.pid_path()
    who = ("（%s）" % reason) if reason else ""
    try:
        platform.kill_process_tree(pid)
    except Exception as e:
        return False, "停后端失败：杀进程树报错 %s（pid=%d）" % (e, pid)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not platform.pid_alive(pid):
            break
        time.sleep(POLL_INTERVAL)
    if platform.pid_alive(pid):
        return False, "停后端失败：pid=%d 还在跑（等满 %.0f 秒）" % (pid, STOP_TIMEOUT)
    if _proc is not None and int(getattr(_proc, "pid", 0) or 0) == pid:
        # 收尸：Popen 没被 wait 过的话，GC 时会报一句 "subprocess N is still running"
        # 的 ResourceWarning（那句话说"没被 wait"，不说"还在跑"），日志里看着像没停干净。
        try:
            _proc.wait(timeout=2)
        except Exception:
            pass
    backend_pid.clear_pid()
    _proc = None
    try:
        from app import db
        db.add_log("info", "backend", "已停止 ECHO 起的后端%s pid=%d" % (who, pid))
    except Exception:
        pass
    done = "已停止 ECHO 起的后端%s（pid=%d）" % (who, pid)
    wanted = [int(p) for p in (ports if ports is not None
                               else (DEFAULT_PORT, DEFAULT_ADMIN_PORT))]
    leftovers = [o for o in (port_owner(p) for p in wanted) if int(o.get("pid") or 0) > 0]
    if leftovers:
        return True, (done + "；但 %s —— 那不是 ECHO 记下的进程，请自行确认"
                      % "；".join(describe_owner(o) for o in leftovers))
    return True, done
