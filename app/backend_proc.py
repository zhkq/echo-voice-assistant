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
import re
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

def port_owner(port: int, *, with_root: bool = False) -> dict:
    """谁在监听 ``port``：``{"port": int, "pid": int, "label": str}``；没人监听则 ``pid=0``。

    只**问**，不动手：占用者可能是同事的后端、用户手工起的实例、甚至是别的软件。

    ``with_root=True`` 时多问一次"那个进程的命令行里 ``--config`` 指向哪个后端目录"
    （``+1~2 ms``，本机实测 1.0 ms；这是归属判据，见 ``backend_owner()``）。
    **默认关**：面板每次刷新都要调本函数，而归属只在真需要判断时才问
    （面板走的是 ``backend_owner()``，它自己会带上）。
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
    out = {"port": int(port), "pid": pid, "label": label}
    if with_root and pid > 0:
        root = ""
        try:
            root = _backend_root_from_cmdline(platform.process_command_line(pid) or "")
        except Exception:
            root = ""
        if root:
            out["root"] = root
            out["is_ours"] = _same_path(root, backend_root())
            out["foreign"] = not out["is_ours"]
    return out


def _backend_root_from_cmdline(cmdline: str) -> str:
    """从命令行里取出这个后端**自己的目录**：``-m server.main --config <root>/server.yaml``。

    为什么这是归属的**唯一外部判据**（2026-10-06）：后端进程长得一模一样（同一个解释器、
    同一套依赖），唯一带标识的就是 ``--config`` 指向哪棵树的目录。用户 AGENTS.md 那条
    （"唯一可靠判据 = pid 文件 + 命令行的 ``--config <该树的后端目录>``"）与桌面工具包
    ``Get-BackendPid`` 用的都是它。

    取不到返回空串 —— 调用方必须把"说不出来"当成一种合法答案。
    """
    text = cmdline or ""
    if "server.main" not in text:
        return ""
    m = re.search(r"--config\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", text)
    if not m:
        return ""
    cfg = next((g for g in m.groups() if g), "")
    if not cfg:
        return ""
    try:
        return os.path.dirname(os.path.abspath(cfg))
    except Exception:
        return ""


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a or "")) == \
               os.path.normcase(os.path.normpath(b or ""))
    except Exception:
        return False


def backend_owner(owner: Optional[dict] = None) -> dict:
    """占用端口的那个后端**属于哪棵树**。

    返回 ``{pid, root, is_ours, foreign, note}``：
      * ``is_ours=True``  —— 命令行里的 ``--config`` 目录就是**当前这棵树**的
        （与 ``backend_root()`` 同一个值）。这才是"我能用这个后端"的判据；
      * ``foreign=True``  —— 它属于**别的安装**（开发版/稳定版另一棵，或用户手工起的）；
      * 两者都 False     —— 说明"不是后端"或"说不出来"，只有 pid 可供参考。

    为什么必须区分（2026-10-06 的真实事故）：dev 面板显示「待启动」、点箭头能进后端
    管理页、而会议转写报"等待能力后端"。真相是 8900 上跑着**稳定版的后端**，
    dev 客户端拿自己的凭据过去只会拿到 ``unauthorized`` —— **不是连不上，是凭据不属于它**。
    原来的判据只问"这个 pid 是不是记在我那份 pid 文件里"，于是这种情况一律被说成
    "待启动"，把用户引去查启动，而真正该做的是**换掉这台后端**。
    """
    info = owner if owner is not None else port_owner(port_of_interest())
    pid = int(info.get("pid") or 0)
    out = {"pid": pid, "root": "", "is_ours": False, "foreign": False, "note": ""}
    if pid <= 0:
        out["note"] = "没有进程在监听"
        return out
    cmdline = ""
    try:
        from app import platform as _platform
        cmdline = str(_platform.process_command_line(pid) or "")
    except Exception:
        cmdline = ""
    root = _backend_root_from_cmdline(cmdline)
    if not root:
        out["note"] = ("pid %d 在监听，但命令行里没有 server.main --config（不是 ECHO 后端，"
                       "或者读不到它的命令行）" % pid)
        return out
    out["root"] = root
    mine = backend_root()
    if _same_path(root, mine):
        out["is_ours"] = True
        out["note"] = "pid=%d 的后端目录正是本棵树的 %s" % (pid, root)
    else:
        out["foreign"] = True
        out["note"] = ("pid=%d 的后端属于**另一棵树**（%s），不是本棵树的 %s"
                       % (pid, root, mine or "（未配置）"))
    return out


def port_of_interest() -> int:
    """本机后端的数据口（可被打桩；只为 ``backend_owner()`` 的默认参数服务）。"""
    return int(DEFAULT_PORT)


def describe_owner(owner: dict) -> str:
    """把 ``port_owner()``／``backend_owner()`` 的结果说成人话。

    带归属信息时会多点名一句"属于哪棵树、是不是本棵树的" —— 这正是用户排障需要的
    那句话（"8900 被 python.exe（pid 1234）占着" 还不够，得说清**它是谁的**）。
    """
    port = owner.get("port", 0)
    pid = int(owner.get("pid") or 0)
    if pid <= 0:
        return "%s 空着" % port
    label = str(owner.get("label") or "").strip()
    base = "%s 被 %s（pid %d）占着" % (port, label or "一个名字取不到的进程", pid)
    root = str(owner.get("root") or "").strip()
    if not root:
        return base
    if owner.get("is_ours"):
        return base + "（就是本棵树的：%s）" % root
    return base + "（属于另一棵树：%s）" % root


def port_check(ports: Optional[Sequence[int]] = None,
               owners: Optional[Sequence[dict]] = None) -> Tuple[bool, str]:
    """要用的端口能不能用 → ``(ok, detail)``。**不起第二个**，也不替用户杀占用者。

    判据分三种（``detail`` 一律说出"是谁"）：

    * 端口空着 → ``(True, "… 空着")``；
    * 占用者**就是 ECHO 自己起的那个**（占用的 pid == pid 记录里那条，且那条确实活着）
      → ``(True, …)``：不冲突，**已经在跑了**（调用方据此直接进入配对，而不是再起一个）；
    * 占用者是**本棵树的后端，但 pid 记录丢了**（2026-10-06 补）→ ``(True, "…已经在跑")``
      **并就地认领它**。见下面那段；
    * 占用者是别人（同事的后端 / 用户手工起的实例 / 别的软件）→ ``(False, …)``：
      如实说出占用者，让用户决定（停它 / 换端口），**我们不替他动手**。

    ``ports=None`` 用本机后端的标准两个端口（8900 / 8901）。
    ``owners``（2026-09-30）：调用方已经查过"谁在监听"时直接传进来 —— 查一次要跑
    `netstat`（本机实测 ~0.15 s/端口），而面板每次刷新都要这份答案；**判据只有这一处**，
    所以是"把结果传进来"，不是在调用方再算一遍（那正是两处判据会漂开的开端）。

    ## 为什么要多那一类（2026-10-06 用户实测的现场）

    用户原话：**"后端管理页面能进，语音能转写，但是仪表盘的状态显示待启动"**。

    真相是**归属记录活不过 ECHO 那一次进程**：pid 文件写的是"**这个 ECHO 进程**起的后端"，
    而 ECHO 重启之后，端口上那个后端还活着（父进程变成旧的 ECHO，已成孤儿），
    新 ECHO 手里没有 pid 记录。于是：

    * `is_ours_alive()` → False → `view()["running"]` = False → 仪表盘那格说「待启动」；
    * 而**能力路由根本不看归属记录**，转写照旧成功、管理页照旧能进 —— 用户看到的就是这个矛盾；
    * 更糟的是**点"启动"还会被自己挡住**：这一类原来落进 `strangers`，
      面板说"这不是 ECHO 起的后端…请先停掉它或换端口"，而那明明就是本棵树自己的后端。

    判据已经有了：`port_owner(with_root=True)` 会按**命令行**（`--config <目录>`）算出
    `is_ours`（与桌面工具包 `Get-BackendPid` 同一判据），这里只是**用它**。
    认领 = 把这个 pid 写进归属记录 —— 从这一刻起它是"我们起的"，后面的启停/接管都自洽了；
    写不进去（目录不可写）也不影响判定，只是下一次还得再认一次。
    """
    wanted: List[int] = [int(p) for p in (ports if ports is not None
                                         else (DEFAULT_PORT, DEFAULT_ADMIN_PORT))]
    ours_pid = 0
    ours_alive, _ = backend_pid.is_ours_alive()
    if ours_alive:
        ours_pid = int(backend_pid.read_pid() or 0)
    if owners is None:
        owners = [port_owner(p, with_root=True) for p in wanted]
    ours_ports, other_tree, strangers, unmanaged = [], [], [], []
    for owner in owners:
        if int(owner.get("pid") or 0) <= 0:
            continue
        if ours_pid and int(owner["pid"]) == ours_pid:
            ours_ports.append(owner)
        elif owner.get("foreign"):
            # **另一棵树的后端**：既不是"我起的"，也不是"不相干的陌生人"。
            # 分开报的理由：这两种的处置完全不同 —— 前者有明确的换法（接管，见 take_over），
            # 后者只能问用户（我们不替人杀不明进程）。混成一句"被占"就把可操作的答案淹掉了。
            other_tree.append(owner)
        elif owner.get("is_ours"):
            # **本棵树的后端在跑，只是 pid 记录不在**（ECHO 重启过 → 记录随旧进程没了）。
            # 命令行已经证明它是我们的，所以既不是陌生人、也不是"另一棵树"。
            unmanaged.append(owner)
        else:
            strangers.append(owner)
    if other_tree:
        return False, ("端口被**另一棵树的后端**占着：%s。开发版与稳定版轮流跑，"
                       "切换时后端要跟着换 —— 点「接管后端」（停掉它、起本棵树自己的、"
                       "配对复用同一份凭据，不需要重新配对）"
                       % "；".join(describe_owner(o) for o in other_tree))
    if strangers:
        return False, ("端口被占：%s。这不是 ECHO 起的后端 —— 同机只跑一个后端，"
                       "请先停掉它或换端口（ECHO 不会替你停别人的进程）"
                       % "；".join(describe_owner(o) for o in strangers))
    if unmanaged:
        # 就地认领：把这个 pid 写进归属记录。写不进去不是错误（只是下次还得再认一次）——
        # 这一类的重点在**判定**：后端已经在跑、而且就是我们这棵树的，别再起第二个，
        # 更别把它当成陌生人挡下来（用户看到的就是"点启动被自己挡住"）。
        for o in unmanaged:
            try:
                backend_pid.write_pid(int(o["pid"]))
            except Exception:
                pass
        return True, ("本棵树的后端已经在跑（%s），不再起第二个"
                      "（归属记录原本不在 —— ECHO 重启过就这样，已认领它）"
                      % "；".join(describe_owner(o) for o in unmanaged))
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
