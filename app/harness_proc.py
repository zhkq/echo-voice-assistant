# -*- coding: utf-8 -*-
"""harness_proc.py — 独立 DeepSeek Harness 的进程管理（随 ECHO 启动）

背景（2026-09-19 实测，见 docs/独立harness接入.md）
--------------------------------------------------
官方**独立发行版**是 npm 包 `@deepseek-ai/dsh`（实测 0.1.5-rc.2，与 DSH Desktop 自带
那个 `dsh` **同版本**）。`dsh web --port N --no-open` 起的就是 Desktop 那套 **web profile**：
同一个 `/api` JSON-RPC 接口面（`session/*`、`workspace/*`、`settings/*`）——
隔离实测（独立 DSH_HOME + 空闲端口 43199）`session/list`、`session/create`、
`workspace/create` 全部通过，**ECHO 现有请求体原封不动就能用**。

与 Desktop 唯一的差别是**鉴权**：
  * Desktop：`/api` 校验签名 Cookie（密钥在 `~/.dsh/.credentials.yaml`，ECHO 自铸 HMAC）；
  * 独立 harness：启动时打印一条 `dsh web: http://…/?token=<token>`，
    用它访问一次 `/?token=…` 就换到一枚 `dsh-auth-…` Cookie，之后照常调 `/api/*`。

本模块负责把 harness 当作 **ECHO 的子进程** 拉起/探活/停止，并把 token 交给适配器：
  * 幂等：先探活，已在跑（用户自己起的、或上次留下的）就不重复起；
  * 冷却：避免每个复查周期都 Popen 一次（与 failover_proxy 同一套教训）；
  * token：从子进程 stdout 解析出来，存进程内 + 落一份到 ``data/logs/harness-token.txt``
    （下次 ECHO 重启时若发现同一实例还在跑，可以复用它，免得再起一个）；
  * 只停 ECHO 自己起的那个（记住 pid），不动用户手工启动的实例；
  * 默认**不随 ECHO 启动**：只有把 `agentBackend` 选成 harness（或 `agentHarnessEnabled`
    打开）时才拉起 —— 选了 DSH Desktop 的人不该平白多一个 node 进程。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request

from app import paths, services
from app.config import settings

#: 默认端口：避开 DSH Desktop 的 43120（两者可以同时在跑，互不干扰）
DEFAULT_PORT = 43199
#: 默认启动命令（可配置；用户 Node 不在 PATH 时在这里填全路径）
DEFAULT_COMMAND = "npx -y @deepseek-ai/dsh web"
LAUNCH_COOLDOWN = 20.0        # 拉起后多久内不再重复拉（秒）
READY_TIMEOUT = 60.0          # 等它就绪的上限（首次会 pnpm 装插件，可能久一点）
TOKEN_RE = re.compile(r"[?&]token=([A-Za-z0-9._~+/=-]+)")

_lock = threading.Lock()
_proc = None                  # ECHO 拉起的子进程（仅用于 stop 时判断"是不是我们起的"）
_proc_pid = 0
_token = ""                   # 从 stdout 解析到的 token（进程内）
_last_launch = 0.0
_reader = None


# ---------------------------------------------------------------- 基本量

def port():
    try:
        return int(settings.get("harnessPort", DEFAULT_PORT) or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def base_url():
    return "http://127.0.0.1:%d" % port()


def home():
    """harness 自己的 DSH_HOME（独立于 Desktop 那边，避免两边抢同一份会话/设置）。

    注意：这里刻意**不**去读 Desktop 的家目录常量 —— 本模块只认 `harnessHome` 配置
    （留空 = `{DATA}/harness`）。上面那句"独立"就是这个意思：谁也别改谁。
    """
    raw = str(settings.get("harnessHome", "") or "").strip()
    from app.config import expand_path
    if raw:
        return expand_path(raw)
    return os.path.join(paths.data_root(), "harness")


#: 本地永久安装的落点（安装技能把 `@deepseek-ai/dsh` 装到这里，见 `.dsh/skills/echo-install`）。
LOCAL_ENTRY_REL = os.path.join("harness", "dsh", "node_modules",
                               "@deepseek-ai", "dsh", "lib", "bin.js")


def local_entry():
    """本地永久安装的 `lib/bin.js` 全路径；没有（或空文件）返回空串。

    为什么专门找它（同事 2026-09-22 实测 B5）：`npx -y @deepseek-ai/dsh web` 冷启动
    **2 分 10 秒**（npx 每次重新解析安装），直连这个文件只要 **9 秒**。
    """
    p = os.path.join(paths.echo_root(), LOCAL_ENTRY_REL)
    try:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    except OSError:
        pass
    return ""


def _node_exe():
    """node 可执行文件全路径：先按平台接缝找（托管 / nvm / 官方安装），再退回 PATH。"""
    d = find_node_dir()
    if d:
        for name in ("node.exe", "node"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    return shutil.which("node") or ""


def is_default_command(raw) -> bool:
    """设置里还是出厂那条 npx 命令（= 用户没自己表过态）。"""
    return str(raw or "").strip() in ("", DEFAULT_COMMAND, "npx -y @deepseek-ai/dsh web")


def command():
    raw = str(settings.get("harnessCommand", DEFAULT_COMMAND) or DEFAULT_COMMAND).strip()
    # 装了本地件、但设置还停在出厂 npx 时自动改走本地入口（老设置/升级上来的机器都受益）。
    # 只在"用户没改过"时生效 —— 用户自己写的命令一个字都不动。
    if is_default_command(raw):
        entry = local_entry()
        if entry:
            node = _node_exe()
            if node:
                return '"%s" "%s" web' % (node, entry)
    return raw


def requested():
    """是否**应该**由 ECHO 把 harness 跑起来：既选中了它、又没被关掉。

    判定用"与"而不是"或"（2026-09-19 实测踩过）：面板"选中智能体"会把
    `agentHarnessEnabled` 一起打开，切走时只改 `agentBackend`。若用"或"，切回 DSH 后
    node 进程会一直挂着（实测：切到 dsh 后 43199 仍在监听），启动页看着像没生效。
    进程跟着**选择**走，和模型路由那种"常驻服务"不是一回事。
    """
    try:
        if str(settings.get("agentBackend", "") or "").strip() != "harness":
            return False
        return bool(settings.get("agentHarnessEnabled", False))
    except Exception:
        return False


#: 真实 token 是 43 字符的 base64url（实测）；短于这个长度的值一律当成"没填"。
#  为什么要有这道闸：设置里可能留着垃圾值（我这次就撞上过 —— 值竟是 `"echo"`），
#  拿它登录必然 401，还会把**浏览器打开**那个功能带沟里（URL 里拼个假 token）。
MIN_TOKEN_LEN = 16


def _looks_like_token(value) -> bool:
    return len(str(value or "").strip()) >= MIN_TOKEN_LEN


def token():
    """当前 token：进程内捕获的 > 上次落盘的（服务重启、实例还活着的情形）> 用户手填的。"""
    if _token:
        return _token
    saved = load_saved_token()          # ECHO 自己重启后，harness 往往还在跑
    if saved:
        return saved
    try:
        manual = str(settings.get("harnessToken", "") or "").strip()
    except Exception:
        return ""
    return manual if _looks_like_token(manual) else ""


def set_token(value):
    """外部（适配器/测试）注入 token。"""
    global _token
    _token = str(value or "").strip()
    return _token


def online(timeout=1.0):
    """harness 在监听吗（任何 HTTP 响应都算，含 401）—— 不发 token，纯探活。"""
    try:
        urllib.request.urlopen(base_url() + "/", timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def started_by_echo():
    return bool(_proc_pid and _proc is not None and _proc.poll() is None)


# ---------------------------------------------------------------- 启动 / 停止

def _argv():
    """把配置里的命令拆成 argv，并把第一段解析成真路径（Windows 上 npx 是 .cmd）。

    `shutil.which` 会按 PATHEXT 找，所以默认的 `npx` 在 Windows 上会解析成 npx.CMD ——
    否则 Popen 直接报 FileNotFoundError（这是"用户装了 Node 却起不来"的常见坑）。
    """
    try:
        parts = shlex.split(command(), posix=False)
    except Exception:
        parts = command().split()
    parts = [p.strip('"') for p in parts if p.strip()]
    if not parts:
        return []
    exe = shutil.which(parts[0]) or parts[0]
    return [exe] + parts[1:]


#: npx 的可执行名（Windows 上是 npx.cmd —— isfile 判断不认 PATHEXT）
_NPX_NAMES = ("npx.cmd", "npx.exe", "npx")


def find_node_dir():
    """本机 node/npx 所在目录；找不到返回空串。平台差异一律走 ``app.platform`` 接缝。"""
    from app import platform as echo_platform
    try:
        dirs = echo_platform.node_dirs()
    except Exception:
        dirs = []
    for d in dirs:
        for name in _NPX_NAMES:
            if os.path.isfile(os.path.join(d, name)):
                return d
    return ""


def ensure_node_on_path():
    """把 node 目录补进**本进程**的 PATH（幂等），返回补进去的目录（没找到则空串）。

    为什么必须改本进程（2026-09-22 同事反馈 B1）：``shutil.which("npx")`` 读的是**本进程**的
    ``os.environ``，早于给子进程准备的那份 env —— 所以"只在 Popen 前构造 env"完全没用，
    ``_argv()`` 里那句 which 依旧返回 None，harness 就被静默放弃了。

    顺带的好处：在 ``env = dict(os.environ)`` **之前**调用，子进程也会自动继承这条 PATH。
    """
    d = find_node_dir()
    if not d:
        return ""
    cur = os.environ.get("PATH", "")
    have = [p.lower() for p in cur.split(os.pathsep) if p]
    if d.lower() not in have:
        os.environ["PATH"] = d + os.pathsep + cur
    return d


def _read_output(proc):
    """后台读子进程输出：写日志 + 抓 token。"""
    global _token
    log_dir = os.path.join(paths.data_root(), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    log_path = os.path.join(log_dir, "harness.log")
    try:
        fh = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception:
        fh = None
    if fh:
        # 每次启动写一条分隔：日志是追加模式，没有分隔时堆在一起的 traceback 看着像"同一个进程
        # 反复重启"（同事 2026-09-21 排障反馈），根本分不清哪段属于哪次。
        try:
            import time as _t
            fh.write("\n===== harness 启动 %s pid=%s =====\n"
                     % (_t.strftime("%Y-%m-%d %H:%M:%S"), getattr(proc, "pid", "?")))
            fh.flush()
        except Exception:
            pass
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if fh:
                try:
                    fh.write(line + "\n")
                    fh.flush()
                except Exception:
                    pass
            m = TOKEN_RE.search(line)
            if m and not _token:
                _token = m.group(1)
                _persist_token(_token)
                services.report_harness("online", "独立 harness（%s）" % base_url())
    except Exception:
        pass
    finally:
        if fh:
            try:
                fh.close()
            except Exception:
                pass


def _persist_token(value):
    """token 落一份到 data/logs（服务重启后若实例还在跑，可复用，不必再起一个）。"""
    try:
        p = os.path.join(paths.data_root(), "logs", "harness-token.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(value)
    except Exception:
        pass


# ---------------------------------------------------------------- 归属（pid）记录
#
# 为什么要落盘：ECHO 重启后 harness 往往还在跑，新进程会**接手**这个实例（不再重复拉起）。
# 但"接手"之后如果只认内存里的 `_proc`，切走时就会说"不是我起的"而拒绝停止 ——
# 实测踩过：切到 DSH 后 43199 仍挂着。所以把"是我们拉起的"这件事记到 pid 文件里。

def _pid_path():
    return os.path.join(paths.data_root(), "logs", "harness.pid")


def _persist_pid(pid):
    try:
        with open(_pid_path(), "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except Exception:
        pass


def _load_pid():
    try:
        with open(_pid_path(), "r", encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except Exception:
        return 0


def _clear_pid():
    try:
        os.remove(_pid_path())
    except Exception:
        pass


#: 这两个端口属于 ECHO 自己与 DSH Desktop，harness 不许占（占了就会互相打架）
RESERVED_PORTS = {43120, 18060}


def port_conflict():
    """端口是不是撞了 Desktop / ECHO 自己？返回一句人话（正常返回空串）。"""
    if port() in RESERVED_PORTS:
        return ("harness 端口 %d 被 DSH Desktop（43120）或 ECHO 自己（18060）占用，"
                "请换成别的端口（默认 43199）" % port())
    return ""


def load_saved_token():
    """读回上次落盘的 token（进程内为空时用）。"""
    global _token
    if _token:
        return _token
    try:
        p = os.path.join(paths.data_root(), "logs", "harness-token.txt")
        with open(p, "r", encoding="utf-8") as fh:
            _token = fh.read().strip()
    except Exception:
        _token = ""
    return _token


def forget_token():
    """丢掉一个用不了的 token（文件里的 + 进程内的），下次重拉时会重新捕获。"""
    global _token
    _token = ""
    try:
        os.remove(os.path.join(paths.data_root(), "logs", "harness-token.txt"))
    except Exception:
        pass


def ensure_running(timeout=None):
    """确保 harness 在跑。幂等。返回 ``(ok, detail)``。

    ``timeout``：等它就绪的上限（秒），``None`` 用 ``READY_TIMEOUT``（60s，首次 npx 装插件
    确实要那么久）。向导执行相传一个小值（见 `settings_effects.WIZARD_HARNESS_TIMEOUT`）——
    它是在 HTTP 请求线程里跑的，不能让"开始准备"卡满一分钟；拉起本身照旧发生，只是不在这里等。

    并发安全：boot 的启动步骤与「切换智能体」的联动可能几乎同时调进来，
    所以"探活 → 决定是否 Popen"这段必须**串行**（否则会拉起两个实例 ——
    实测过：43199 与 43206 两个 node 同时在跑）。
    """
    global _proc, _proc_pid, _last_launch
    # 先把 node 目录补进**本进程** PATH —— `shutil.which` 读的是它，而不是下面给子进程的 env。
    # 桌面快捷方式启动时 PATH 里常常没有托管式 node（2026-09-22 同事反馈 B1）。
    ensure_node_on_path()
    wait_for = READY_TIMEOUT if timeout is None else max(0.0, float(timeout))
    conflict = port_conflict()
    if conflict:
        return False, conflict
    with _lock:
        if online():
            return True, "独立 harness 已在运行（%s）" % base_url()
        now = time.monotonic()
        if now - _last_launch < LAUNCH_COOLDOWN:
            return True, "独立 harness 刚拉起过，等待就绪（冷却 %.0fs）" % (
                LAUNCH_COOLDOWN - (now - _last_launch))
        argv = _argv()
        if not argv:
            return False, "harness 启动命令为空（设置 → 智能体 → 启动命令）"
        exe = argv[0]
        if not (os.path.isabs(exe) and os.path.isfile(exe)) and not shutil.which(exe):
            return False, ("找不到 %s：需要本机有 Node.js（npx）。装了但不在 PATH 里时，"
                           "把「启动命令」改成本机 npx 的全路径" % exe)
        dsh_home = home()
        try:
            os.makedirs(dsh_home, exist_ok=True)
        except Exception:
            pass
        env = dict(os.environ)
        env["DSH_HOME"] = dsh_home                      # 独立家目录：不碰 Desktop 的家
        full = argv + ["--port", str(port()), "--no-open"]
        flags = {}
        try:
            from app import platform as echo_platform
            flags = echo_platform.detach_console_kwargs()   # 无控制台窗口（同 failover/边条）
        except Exception:
            flags = {}
        _last_launch = now
        try:
            proc = subprocess.Popen(
                full, cwd=dsh_home, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                close_fds=True, **flags)
        except Exception as e:
            return False, "拉起独立 harness 失败：%s（命令：%s）" % (e, " ".join(full))
        _proc, _proc_pid = proc, proc.pid
        _persist_pid(proc.pid)          # 落盘：ECHO 重启后仍认得"这是我们起的"
    # 锁外等就绪（首次会 pnpm 装插件，几十秒；持有锁会挡住并发调用者）
    try:
        services.report_harness("starting", "启动中：%s" % " ".join(full[:3]))
    except Exception:
        pass
    threading.Thread(target=_read_output, args=(proc,), daemon=True,
                     name="harness-log").start()
    deadline = time.monotonic() + wait_for
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False, ("独立 harness 启动后立即退出（code=%s）——命令或环境有问题，"
                           "详见 data/logs/harness.log" % proc.returncode)
        if online():
            return True, "独立 harness 已启动（%s）" % base_url()
        time.sleep(0.5)
    return True, "独立 harness 启动中（%s，首次要装插件，可能要一会儿）" % base_url()


def _kill_tree(pid):
    """杀掉整棵进程树（平台差异在 app/platform 接缝里：Windows=taskkill /T）。"""
    if pid <= 0:
        return
    try:
        from app import platform as echo_platform
        echo_platform.kill_process_tree(pid)
    except Exception:
        pass


def _listener_pid(want_port):
    """谁在监听 want_port（平台差异同样收在接缝里）。找不到返回 0。"""
    try:
        from app import platform as echo_platform
        return echo_platform.listening_pid(want_port)
    except Exception:
        return 0


def stop(reason=""):
    """停止 **ECHO 自己起的** harness（用户手工起的实例不动）。返回 ``(ok, detail)``。

    归属判据（两道，任一成立即认为是我们起的）：
      * 本进程 Popen 过的（`_proc_pid`）；
      * 上次 ECHO 落盘的 pid 文件（服务重启后接手的情形）—— 否则会出现
        "切走了但端口还挂着"，实测踩过。
    收尾动作：先杀整棵树（npx 会套 cmd→node→cmd→node，只 terminate 最外层等于没杀），
    再用"谁在监听本端口"兜底补一刀 —— 但**只在端口确实归我们管**时才动它。

    `reason`（2026-09-22 加）：**谁、为什么**把它停掉的，写进日志与组件状态。
    起因：有同事发现"标准版服务自己停了"，而当时只有一句"已停止"，光看状态分辨不出是
    "设置里切走了智能体"、"面板点了停止"、还是"换 token 重启没起来"—— 全是 stop() 一个出口。
    """
    global _proc, _proc_pid
    proc, pid = _proc, _proc_pid
    recorded_pid = pid or _load_pid() or 0     # 提前取：下面 _clear_pid() 之后就查不到了
    recorded = bool(recorded_pid)
    if not recorded:
        _proc, _proc_pid = None, 0
        return True, "独立 harness 不是本进程起的，未做处理"
    if proc is not None and proc.poll() is None:
        _kill_tree(pid or proc.pid)
        try:
            proc.wait(timeout=8)
        except Exception:
            pass
    elif pid:
        _kill_tree(pid)
    # 兜底：wrapper 早退了、真正的 node 还在监听
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and online(timeout=0.6):
        time.sleep(0.4)
    if online(timeout=0.6):
        lpid = _listener_pid(port())
        if lpid and lpid != os.getpid():
            _kill_tree(lpid)
            time.sleep(1.0)
    _proc, _proc_pid = None, 0
    _clear_pid()
    forget_token()
    still = online(timeout=0.6)
    who = ("（%s）" % reason) if reason else ""
    try:
        services.report_harness("offline" if not still else "online",
                                ("已停止%s" % who) if not still
                                else "停止失败：端口仍在监听")
    except Exception:
        pass
    if not still:
        try:
            # 留一条可追溯的日志：下次"它怎么自己停了"就有据可查（面板日志页可见）
            from app import db
            db.add_log("info", "harness",
                       "已停止标准版 harness%s pid=%s" % (who, recorded_pid or "?"))
        except Exception:
            pass
    return (not still), ("已停止独立 harness" if not still else
                         "停止失败：%s 仍在监听（可能需要手动结束该进程）" % base_url())


def ensure_token(timeout=45.0):
    """确保手里有一枚 **可用的** token；没有就把 ECHO 自己起的实例重启一次重新抓。

    为什么需要它：token 只在"ECHO 亲自 Popen 并读 stdout"那一刻能拿到。ECHO 重启后接手
    旧实例时手里没有 token（进程不是我们起的、stdout 也不在我们手里）—— 而**用浏览器打开
    它的 Web 界面**必须带 token（浏览器没法用我们那枚密钥 Cookie）。
    所以此时把实例重启一次（**只动 ECHO 自己起的**，pid 文件为凭），换一枚新 token。

    返回 ``(token, detail)``；`token` 为空表示拿不到（用户手工起的实例，我们不碰）。
    """
    tok = token()
    if tok:
        return tok, "已有 token"
    if not (started_by_echo() or _load_pid()):
        return "", ("这个 harness 不是 ECHO 起的（没有 pid 记录），拿不到登录 token —— "
                    "把它停掉让 ECHO 重新拉起，或把启动时打印的 token 填到设置里")
    stop(reason="为了拿登录 token 重启一次（ECHO 接手旧实例时手里没有 token）")
    ok, msg = ensure_running()
    if not ok:
        return "", msg
    deadline = time.monotonic() + max(5.0, float(timeout))
    while time.monotonic() < deadline:
        tok = token()
        if tok:
            return tok, "已重新拉起并拿到新 token"
        time.sleep(0.5)
    return "", "重启后仍没抓到 token：看 data/logs/harness.log"


def status_detail():
    """给面板/日志用的一句话状态。"""
    if online():
        return "online", "独立 harness 运行中（%s，token %s）" % (
            base_url(), "已获取" if token() else "未获取")
    if requested():
        return "idle", "已配置为随 ECHO 启动，但当前没在监听 %s" % base_url()
    return "disabled", "未启用（选中「独立 harness」时才会启动）"


def sync_status():
    """把当前状态写进组件状态表（启动页/仪表盘读它）。

    为什么要显式同步：进程可能是**上一次 ECHO 拉起的**（这次只是接手），那条路径不会走
    `_read_output`（只在真正 Popen 时启动），状态行就会一直停在"启动中"。
    """
    try:
        status, detail = status_detail()
        services.report_harness(status, detail)
        return status, detail
    except Exception:
        return "unknown", ""
