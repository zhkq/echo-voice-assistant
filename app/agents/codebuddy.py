# -*- coding: utf-8 -*-
"""agents/codebuddy.py — CodeBuddy Code CLI 适配器（WorkBuddy 内置同一引擎）

背景（已实测确认）：
  * WorkBuddy 桌面端没有自己的 CLI，其内置执行引擎就是 CodeBuddy Code。
    CLI 入口通常位于（不在 PATH 里，必须按路径找）：
        %LOCALAPPDATA%\\Programs\\WorkBuddy\\resources\\app.asar.unpacked\\cli\\bin\\codebuddy
    独立安装则来自 `npm i -g @tencent-ai/codebuddy-code`，命令名 codebuddy / cbc。
  * 该 CLI 是 Node 脚本，需要 Node ≥ 18.20.8；WorkBuddy 自带一份：
        ~/.workbuddy/binaries/node/versions/<ver>/node.exe
  * 无头调用方式：-p "<prompt>" --output-format json --permission-mode auto
    输出为 JSON 数组，最后一个 type=result 元素含：
        result      → 最终助手文本（我们需要的 reply）
        session_id  → 会话 id（续接用 -r/--resume）
  * 认证：绑定在 Windows 用户账户下（凭据不在 ~/.codebuddy，改 USERPROFILE 即报
        "Authentication required. Please use /login command to sign in to your account"
    ECHO 以同一用户起子进程，自动继承登录态，无需额外配置。
  * 每次调用是一个独立进程、阻塞到出结果，因此 wait_for_reply 无需轮询。
"""
import glob
import json
import os
import shutil
import subprocess
import threading
import time

from app.config import settings
from app.agents.base import AgentAdapter, AgentError, ECHO_WORKSPACE
from app import platform as echo_platform

# 认证失效的识别串（实测原文）
AUTH_HINT = "Authentication required"
AUTH_REASON = ("CLI 未登录：请先在终端运行一次 codebuddy 完成浏览器授权登录"
               "（凭据绑定当前 Windows 用户，换用户/换机器后需重新登录）")

# 单次调用的默认超时（秒）。命令场景 ECHO 侧给 90s，这里留出更大上限，
# 由调用方传入的 timeout 决定；此值仅作兜底。
DEFAULT_TIMEOUT = 240

# 我们的「会话 id」→ CLI 真实 session id 的映射（CLI 首轮才生成真实 id）
_SESSION_MAP = {}
_MAP_LOCK = threading.Lock()


def _candidate_paths():
    """CLI 入口候选路径，按优先级排列。"""
    out = []
    # ① 用户在设置里显式指定
    custom = (settings.get("agentCustomPath", "") or "").strip()
    if custom:
        out.append(os.path.expandvars(os.path.expanduser(custom)))
    # ② PATH（独立安装）
    for exe in ("codebuddy", "cbc", "codebuddy-code"):
        hit = shutil.which(exe)
        if hit:
            out.append(hit)
    # ③④ 平台专有的内置安装位置（Windows：WorkBuddy 内置 / CodeBuddy IDE 自带）
    out.extend(echo_platform.agent_cli_candidates())
    # 去重保序
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _find_node():
    """能跑 CLI 的 Node：优先 WorkBuddy 自带，其次 PATH。"""
    home = os.path.expanduser("~")
    pats = [
        os.path.join(home, ".workbuddy", "binaries", "node", "versions", "*", "node.exe"),
        os.path.join(home, ".workbuddy", "binaries", "node", "versions", "*", "bin", "node"),
    ]
    cands = []
    for pat in pats:
        cands.extend(sorted(glob.glob(pat), reverse=True))
    which = shutil.which("node")
    if which:
        cands.append(which)
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def locate():
    """定位可用的 CLI。（返回 (path, reason)）"""
    for p in _candidate_paths():
        if os.path.isfile(p):
            return p, ""
    return "", ("未找到 CodeBuddy Code CLI。可任选其一：① 安装 WorkBuddy 桌面端"
                "（自带 CLI）；② npm i -g @tencent-ai/codebuddy-code；"
                "③ 在下方「CLI 路径」里手动指定可执行文件")


def _run_headless(cli, node, prompt, timeout, cwd=None, resume_sid=""):
    """起一次无头调用，返回 (rc, stdout, stderr, 耗时)。"""
    cmd = []
    if cli.lower().endswith((".js",)) or not os.access(cli, os.X_OK):
        # Node 脚本（WorkBuddy 内置的就是这种）需要显式用 node 执行
        if node:
            cmd.append(node)
    elif os.path.splitext(cli)[1].lower() in ("", ".cmd", ".bat") and node:
        # 无扩展名的 Node 脚本：Windows 下直接用 node 跑最稳
        cmd.append(node)
    cmd.append(cli)
    cmd += ["-p", prompt, "--output-format", "json", "--permission-mode", "auto"]
    if resume_sid:
        cmd += ["-r", resume_sid]

    env = dict(os.environ)
    env["CODEBUDDY_FORCE_HEADLESS_BUNDLE"] = "1"   # 走 headless bundle，避免 TUI
    env.setdefault("PYTHONIOENCODING", "utf-8")

    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd or ECHO_WORKSPACE, env=env,
                           capture_output=True, timeout=timeout,
                           creationflags=echo_platform.no_window_creationflags())
    except subprocess.TimeoutExpired:
        raise AgentError(f"CodeBuddy CLI 超过 {timeout}s 未返回（可用 --max-turns 或缩短任务）")
    except OSError as e:
        raise AgentError(f"启动 CodeBuddy CLI 失败：{e}") from e
    dt = time.time() - t0
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"),
            dt)


def _parse_output(stdout):
    """解析 CLI 的 JSON 输出，返回 (reply, real_session_id)。

    取最后一个 type=result 元素：result 是最终助手文本，session_id 是会话 id。
    兼容 stream-json（逐行 NDJSON）与顶层不是数组的退化情况。
    """
    text = (stdout or "").strip()
    if not text:
        return None, ""
    data = None
    try:
        data = json.loads(text)
    except Exception:
        # stream-json：逐行解析
        data = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except Exception:
                continue
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None, ""
    reply, sid = None, ""
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "result":
            r = item.get("result")
            if isinstance(r, str) and r.strip():
                reply = r.strip()
            elif r is not None and reply is None:
                reply = str(r)
            sid = item.get("session_id") or item.get("sessionId") or sid
    if reply is None:
        # 兜底：取最后一条 assistant 文本
        for item in data:
            if isinstance(item, dict) and item.get("type") == "message" \
                    and item.get("role") == "assistant":
                parts = [c.get("text", "") for c in (item.get("content") or [])
                         if isinstance(c, dict) and c.get("type") in ("text", "output_text")]
                joined = "".join(parts).strip()
                if joined:
                    reply = joined
    return reply, sid


class CodeBuddyAgent(AgentAdapter):
    name = "codebuddy"
    display_name = "CodeBuddy Code（WorkBuddy 引擎）"
    short_name = "CodeBuddy"
    vendor = "腾讯 CodeBuddy"
    description = ("CodeBuddy Code CLI 无头模式（-p）。WorkBuddy 桌面端内置同一引擎；"
                   "每次调用一个独立进程，直接拿到最终回复")
    config_key = "agentCodebuddyEnabled"
    #: 它自己的配置项：CLI 路径（面板在展开区里编辑；原来靠 settingsValue() 取，
    #  而那个键是 hidden 的、/api/settings 根本不下发 → 一直是空框，属隐藏 bug）
    settings_keys = ("agentCustomPath",)
    capabilities = ("workspace", "session", "cancel")

    def __init__(self):
        self._cli = ""
        self._node = ""

    # ------------------------------------------------------------- 可用性

    def cli_path(self):
        if not self._cli:
            self._cli, _ = locate()
        return self._cli

    def available(self, probe=False):
        cli, reason = locate()
        if not cli:
            return False, reason
        node = _find_node()
        if not node:
            return False, ("未找到可用 Node（需要 ≥18.20.8）。可安装 Node，"
                           "或先安装 WorkBuddy（自带 Node）")
        if not probe:
            return True, f"已找到 CLI：{cli}"
        # 重活：真跑一次 --version，能确认 CLI 可执行
        try:
            p = subprocess.run([node, cli, "--version"], capture_output=True, timeout=60,
                               creationflags=echo_platform.no_window_creationflags())
            ver = p.stdout.decode("utf-8", "replace").strip()
            if p.returncode != 0 or not ver:
                return False, f"CLI 无法执行（exit={p.returncode}）：{ver[:120]}"
            return True, f"可用，版本 {ver.splitlines()[0]}"
        except Exception as e:
            return False, f"执行 CLI 探测失败：{e}"

    def check_auth(self):
        """跑一次极小请求以确认登录态。返回 (ok, reason)。"""
        try:
            reply, _ = self.ask("", "只回复：ok", timeout=120)
        except AgentError as e:
            msg = str(e)
            if AUTH_HINT.lower() in msg.lower():
                return False, AUTH_REASON
            return False, msg
        if reply is None:
            return False, "CLI 未返回任何回复（可能未登录，或模型侧异常）"
        return True, f"调用正常（返回 {reply[:20]!r}）"

    # ------------------------------------------------------------- 会话

    def ensure_session(self, kind, name="", **kw):
        """CodeBuddy 自己管理会话：这里只给一个稳定占位 id。

        真实 session id 由首轮调用的 result.session_id 给出，之后用 -r 续接。
        """
        return f"cb-{kind}"

    def resolve_target(self, workspace=None, session_id=None):
        """工作区通过子进程 cwd 体现；会话 id 直接沿用。"""
        return session_id or "cb-command"

    # ------------------------------------------------------------- 收发

    def prompt(self, session_id, text, mode="queue"):
        """兼容旧接口：CodeBuddy 是「调用即完成」，故此处记录待发文本，
        由 wait_for_reply 实际执行。业务侧若直接调 ask() 则走 send()。"""
        self._pending = (session_id, text)
        return True

    def wait_for_reply(self, session_id, timeout=90, poll=0.5):
        pending = getattr(self, "_pending", None)
        if not pending:
            return None, True
        _, text = pending
        self._pending = None
        try:
            reply, done = self.send(session_id, text, timeout=timeout)
            return reply, done
        except AgentError as e:
            raise

    def send(self, session_id, text, timeout=DEFAULT_TIMEOUT, cwd=None):
        """发一条指令并同步等到最终回复，返回 (reply, done)。"""
        cli, reason = locate()
        if not cli:
            raise AgentError(reason)
        node = _find_node()
        if not node:
            raise AgentError("未找到可用 Node（需要 ≥18.20.8）")

        with _MAP_LOCK:
            real_sid = _SESSION_MAP.get(session_id, "")
        rc, out, err, dt = _run_headless(cli, node, text, timeout,
                                         cwd=cwd or ECHO_WORKSPACE,
                                         resume_sid=real_sid)
        if AUTH_HINT.lower() in (err or "").lower() or AUTH_HINT.lower() in (out or "").lower():
            raise AgentError(f"{AUTH_REASON}（原始报错：{err.strip()[:160]}）")
        if rc != 0 and not out.strip():
            raise AgentError(f"CodeBuddy CLI 退出码 {rc}：{err.strip()[:200] or '无输出'}")
        reply, got_sid = _parse_output(out)
        if got_sid:
            with _MAP_LOCK:
                _SESSION_MAP[session_id] = got_sid
        if reply is None:
            raise AgentError(f"CodeBuddy CLI 未返回可解析的回复（{dt:.1f}s，"
                             f"stderr：{err.strip()[:160] or '空'}）")
        return reply, True

    def cancel(self, session_id):
        """无状态后端：不存在可取消的驻留会话，返回 None 即可。"""
        return None

    def clear_stuck(self, session_id):
        return False


def build():
    return CodeBuddyAgent()


# 导入期登记到注册表（懒加载由 agents._autoload 触发）
from app.agents import register as _register          # noqa: E402

_register(CodeBuddyAgent, build)
