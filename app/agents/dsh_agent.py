# -*- coding: utf-8 -*-
"""agents/dsh_agent.py — DSH Desktop 适配器

原 `app/dsh.py` 的客户端实现原样迁入本文件，外加 AgentAdapter 接口。
行为与迁移前**完全一致**（会话轮换、卡住清理、turn/end 判定、超时取消全部保留），
只是多了一层"可被注册表按名字取用"的外壳。

DSH Desktop 2.x 的接入要点（原文保留）：
新版 DeepSeek Harness（DSH Desktop 2.0.4 / @deepseek-ai/dsh 0.1.2-alpha.1）不再
提供独立的 3080 服务，改为桌面客户端内置 Web 服务（GUI 与 API 同端口，默认
http://127.0.0.1:43120）。JSON-RPC 风格端点挂在 /api/<namespace/method>：

    请求：{"type":"client-request","rpcId":"...","method":"session/list",
           "payload":{"args":{...}}}           # payload 必须恰好一个 args 键
    响应：{"type":"server-response","rpcId":"...","result":{"ok":true,"value":...}}

访问控制（两层，均需通过）：
  1. Desktop 网关：只放行带 x-dsh-desktop-renderer 令牌的请求。外部程序必须让
     桌面版处于"普通浏览器访问"开启状态（~/.dsh/settings.yaml 中
     dsh-desktop.mode = compatibility 且 openBrowser = true，仅本机 loopback）。
  2. /api 通道：校验签名 Cookie dsh-auth-<b64(sha256(authority))>=v1.<body>.<sig>，
     签名密钥在 ~/.dsh/.credentials.yaml 的
     records['client-connection/browser-session'].payload.secret，
     本模块启动时读取并自铸 Cookie（HMAC-SHA256，与桌面版同一算法）。
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import uuid
import urllib.error
import urllib.request

import app.db as db
from app.config import settings
from app.agents.base import AgentAdapter, AgentError, echo_workspace

DEFAULT_BASE_URL = "http://127.0.0.1:43120"

CREDENTIALS_PATH = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
AUTH_RECORD_KEY = "client-connection/browser-session"
COOKIE_PREFIX = "dsh-auth-"
COOKIE_MAX_AGE_DAYS = 30   # 与桌面版默认一致（服务端校验 expiresAt <= issuedAt+30d）


class DshError(AgentError):
    """DSH 专属错误（AgentError 的子类，旧捕获点继续可用）。"""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _load_browser_secret(path: str = "") -> str:
    """从 `.credentials.yaml` 提取 browser-session 签名密钥（base64url 文本）。

    `path` 缺省是 DSH Desktop 的家目录；独立 harness 传它自己的家目录 —— 两份文件
    **结构完全同构**（都有 `records['client-connection/browser-session'].payload.secret`），
    所以这个解析器能同时服务两个后端（2026-09-19 实测对比过）。
    """
    try:
        with open(path or CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    in_records = False
    in_record = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key = line.strip()
        if indent == 0:
            in_records = key.startswith("records:")
            in_record = False
            continue
        if not in_records:
            continue
        if in_record and key.startswith("secret:"):
            return key.split(":", 1)[1].strip().strip("'\"")
        if key == AUTH_RECORD_KEY + ":":
            in_record = True
    return ""


def _make_cookie(base_url: str, secret_b64: str = "", credentials_path: str = "") -> str:
    """按桌面版算法自铸签名 Cookie（payload v1 + HMAC-SHA256，authority=Host 头）。

    `secret_b64` 显式给密钥（独立 harness 用它**自己家目录**里的那份）；
    不给就从 `credentials_path`（缺省桌面版凭据文件）里读。
    """
    if not secret_b64:
        secret_b64 = _load_browser_secret(credentials_path)
    if not secret_b64:
        raise DshError(
            f"无法读取浏览器会话密钥（{credentials_path or CREDENTIALS_PATH} 中缺少 "
            f"records.{AUTH_RECORD_KEY}.payload.secret）")
    secret = _b64url_decode(secret_b64)

    from urllib.parse import urlsplit
    host = urlsplit(base_url).netloc
    authority = host if host else "127.0.0.1:43120"

    name = COOKIE_PREFIX + _b64url(hashlib.sha256(authority.encode("utf-8")).digest())
    now_ms = int(time.time() * 1000)
    payload = {
        "version": 1,
        "authority": authority,
        "issuedAt": now_ms,
        "expiresAt": now_ms + COOKIE_MAX_AGE_DAYS * 86400 * 1000,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{name}=v1.{body}.{sig}"


class DshAgent(AgentAdapter):
    name = "dsh"
    display_name = "DSH Desktop"
    short_name = "DSH"
    vendor = "DeepSeek Harness"
    description = "DSH Desktop 2.x 本机 JSON-RPC（GUI 与 API 同端口，默认 43120）"
    config_key = ""                       # DSH 是默认后端，不设启用开关
    #: DSH 自己的配置项：服务地址（面板在智能体表格的展开区里编辑，不再占设置页分组）
    settings_keys = ("dshBaseUrl",)
    capabilities = ("workspace", "session", "cancel", "history")

    def __init__(self, base_url=None):
        self.base_url = (base_url or settings.get("dshBaseUrl", DEFAULT_BASE_URL)).rstrip("/")
        self._cookie = None
        self._cookie_ts = 0.0

    # ------------------------------------------------------------- 可用性

    def available(self, probe=False):
        """带 Cookie 调 session/list 探活。"""
        try:
            self.rpc("session/list", {"_request": {}}, timeout=3 if not probe else 6)
            return True, f"API 可访问（{self.base_url}）"
        except DshError as e:
            msg = str(e)
            if "401" in msg or "403" in msg:
                return False, ("鉴权失败：请在 DSH Desktop 开启「普通浏览器访问」"
                               "（~/.dsh/settings.yaml 的 dsh-desktop.mode=compatibility "
                               "且 openBrowser=true，改后重启 DSH Desktop）")
            if not os.path.isfile(CREDENTIALS_PATH):
                return False, f"未找到 DSH 凭据文件 {CREDENTIALS_PATH}（DSH Desktop 是否已登录过？）"
            return False, f"连不上 {self.base_url}（DSH Desktop 未运行？）"

    def ping(self):
        """兼容旧调用点：等价于 available() 的布尔结果。"""
        return self.available()[0]

    # ------------------------------------------------------------- 底层 RPC

    def _cookie_header(self):
        """Cookie 约 5 分钟重铸一次（服务端仅校验时间窗口，宽松即可）。"""
        if not self._cookie or time.time() - self._cookie_ts > 300:
            self._cookie = _make_cookie(self.base_url)
            self._cookie_ts = time.time()
        return self._cookie

    def rpc(self, method, args=None, timeout=15):
        body = {
            "type": "client-request",
            "rpcId": f"echo-{uuid.uuid4().hex[:12]}",
            "method": method,
            "payload": {"args": args or {}},
        }
        req = urllib.request.Request(
            self.base_url + "/api/" + method,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Cookie": self._cookie_header(),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code in (401, 403):
                hint = ("。请确认 DSH Desktop 已运行，且已开启普通浏览器访问"
                        "（~/.dsh/settings.yaml: dsh-desktop.mode = compatibility 且 "
                        "openBrowser = true，修改后需重启 DSH Desktop）")
            raise DshError(f"DSH RPC {method} 失败: HTTP {e.code} {e.reason}{hint}") from e
        except Exception as e:
            raise DshError(f"DSH RPC {method} 失败: {e}（DSH Desktop 是否已启动？）") from e
        result = data.get("result") or {}
        if not result.get("ok"):
            err = result.get("error") or {}
            raise DshError(f"DSH RPC {method} 返回错误: {err.get('code')} {err.get('message')}")
        return result

    # ------------------------------------------------------------- 会话

    def create_session(self, cwd=None, workspace_id=None):
        """新建会话。

        重要：session/create **只接受 workspaceId 或 cwd 之一**（两个都给会被拒：
        "session.create accepts workspaceId or cwd, not both"）。
          * workspace_id → 会话建在该工作区的 path 下，并被**登记进工作区**
            （侧栏归组的必要条件）；
          * cwd → 只建在该目录，不登记，侧栏里会落到「未分组」。
        会议会话必须走 workspace_id，否则用户在工作区里看不到它们。
        """
        if workspace_id:
            args = {"request": {"workspaceId": workspace_id}}
        else:
            args = {"request": {"cwd": cwd} if cwd else {}}
        res = self.rpc("session/create", args)
        return (res.get("value") or {}).get("sessionId", "")

    # ------------------------------------------------------------- 工作区

    def has_workspaces(self):
        return True

    def _workspace_registry_path(self):
        """本后端自己的 DSH 工作区注册表 `workspace.json` 的路径（只读）。

        DSH Desktop 的注册表在桌面版家目录 `~/.dsh`；独立 harness 有**自己**的家目录
        （`harness_proc.home()`），子类覆盖这里即可指向它，**不许**沿用 `~/.dsh`
        （AGENTS.md 铁律：不许把 DSH 家目录写死成 `~/.dsh`）。
        读错注册表 = 拿到别的后端的旧 workspaceId，建会话时报 not-found → 回退 cwd → 未分组。
        """
        return os.path.join(os.path.expanduser("~"), ".dsh", "storages", "workspace.json")

    def _registry_workspaces(self):
        """本后端自己注册表里的 `{workspaceId: {path,title,...}}`；读不到返回 {}。

        只读，绝不改写 DSH 的文件：ECHO 不是这份数据的主人（DSH 才是），
        我们只借它做"这个目录有没有工作区 / 分组叫什么"的判断。
        """
        try:
            import json as _json
            reg = self._workspace_registry_path()
            if not os.path.isfile(reg):
                return {}
            with open(reg, "r", encoding="utf-8") as f:
                doc = _json.load(f)
            return dict(((doc.get("tables") or {}).get("workspaces") or {}))
        except Exception:
            return {}

    def workspace_title(self, workspace_id):
        """某个工作区当前的分组名；读不到返回 ""。"""
        if not workspace_id:
            return ""
        return str((self._registry_workspaces().get(workspace_id) or {}).get("title") or "")

    def find_workspace(self, path):
        """按目录路径找工作区 id（大小写与结尾斜杠容错）。找不到返回 ""。"""
        if not path:
            return ""
        want = os.path.normcase(os.path.normpath(path))
        # ① 会话列表里带 workspaceId 的会话（最直接：反映 DSH 当前认知）
        try:
            for it in self.list_sessions():
                if it.get("workspaceId") and \
                        os.path.normcase(os.path.normpath(it.get("cwd") or "")) == want:
                    return it["workspaceId"]
        except Exception:
            pass
        # ② 兜底：直接读 **本后端自己** 的工作区注册表（只读，不改写）
        for wid, w in self._registry_workspaces().items():
            if os.path.normcase(os.path.normpath(w.get("path") or "")) == want:
                return wid
        return ""

    def ensure_workspace(self, path, title=""):
        """取回（无则创建）指定目录的工作区，返回 (workspaceId, created)。

        建会话时传 workspaceId，DSH 会把它登记进该工作区——这是让会议会话
        出现在「会议工作区」分组里的关键，不需要我们再手工登记会话 id。

        `title` 是**想要的分组名**。DSH 的 `workspace/create` 只收目录、名字由目录名
        派生，所以中文名只能建好之后再 `workspace/rename`（见 `_apply_title`）。
        """
        path = (path or "").strip()
        if not path:
            return "", False
        wid = self.find_workspace(path)
        if wid:
            created = False
        else:
            try:
                res = self.rpc("workspace/create", {"request": {"path": path}}, timeout=20)
            except DshError as e:
                raise DshError(f"创建 DSH 工作区失败（{path}）：{e}") from e
            val = res.get("value") or {}
            ws = val.get("workspace") or {}
            wid = ws.get("workspaceId") or ""
            created = bool(val.get("created"))
        if wid and title:
            self._apply_title(wid, path, title, just_created=created)
        return wid, created

    def _apply_title(self, workspace_id, path, title, just_created=False):
        """把分组名改成 `title` —— **只在用户没自己改过**时改。

        判据：新建工作区的名字必然是目录名（basename），所以
        "当前名 == basename" 就等于"没人动过"；用户起过名字的一律不碰。
        返回动作字符串（renamed / unchanged / user-named / rename-failed），
        供调用方决定要不要告警 —— 改名失败是静默的，必须能说出来。
        """
        title = (title or "").strip()
        if not workspace_id or not title:
            return "skipped"
        base = os.path.basename((path or "").rstrip("\\/"))
        cur = self.workspace_title(workspace_id)
        if cur == title:
            return "unchanged"
        if not just_created and cur and cur != base:
            return "user-named"
        try:
            self.rpc("workspace/rename",
                     {"request": {"workspaceId": workspace_id, "title": title}}, timeout=15)
            return "renamed"
        except Exception as e:
            db.add_log("warn", "dsh",
                       f"分组改名失败（{cur or base} → {title}）：{type(e).__name__}: {e}")
            return "rename-failed"

    def archive_session(self, session_id, workspace_id=None):
        """归档会话：从侧栏隐藏，且不再计入「未分组」。会议删除时调用。"""
        if not session_id:
            return False
        req = {"sessionId": session_id}
        if workspace_id:
            req["workspaceId"] = workspace_id
        try:
            self.rpc("workspace/archiveSession", {"request": req}, timeout=15)
            return True
        except Exception:
            return False

    def list_sessions(self):
        res = self.rpc("session/list", {"_request": {}}, timeout=8)
        return (res.get("value") or {}).get("items", []) or []

    # ----------------------------------------------------- 命令目标（工作区/对话）

    def list_workspaces(self):
        """聚合全部会话的工作区（cwd）列表。"""
        ws = set()
        for it in self.list_sessions():
            cwd = it.get("cwd")
            if cwd:
                ws.add(cwd)
        return sorted(ws)

    def list_sessions_for(self, workspace=None):
        """返回会话列表（可只按工作区过滤），附带标题/时间等展示信息。"""
        items = []
        for it in self.list_sessions():
            if workspace is not None and (it.get("cwd") or "") != workspace:
                continue
            proj = it.get("projections") or {}
            items.append({
                "sessionId": it.get("sessionId"),
                "title": (proj.get("values") or {}).get("title") or "",
                "cwd": it.get("cwd") or "",
                "updatedAt": it.get("updatedAt"),
                "running": bool(it.get("running")),
                "blank": bool(it.get("blank")),
            })
        items.sort(key=lambda x: -(x["updatedAt"] or 0))
        return items

    def resolve_target_session(self, workspace=None, session_id=None):
        """解析命令发送目标会话：
        - 显式 session_id → 直接用
        - 指定工作区 → 该工作区最近更新的会话，没有则新建
        - 否则返回 None（由调用方走 ensure_session 默认路径）
        """
        if session_id:
            return session_id
        if workspace:
            for it in self.list_sessions_for(workspace):
                if not it.get("blank"):
                    return it["sessionId"]
            return self.create_session(cwd=workspace)
        return None

    # AgentAdapter 的通用命名
    def resolve_target(self, workspace=None, session_id=None):
        return self.resolve_target_session(workspace=workspace, session_id=session_id)

    def _cursor_of(self, session_id):
        """取会话当前写入游标（session.list 的 projections.asOfSeq），用作 page 的
        throughSeq。throughSeq=-1 会读空，超过游标会 bad-request，必须取实时值。"""
        try:
            for item in self.list_sessions():
                if item.get("sessionId") == session_id:
                    proj = item.get("projections") or {}
                    return proj.get("asOfSeq")
        except Exception:
            pass
        return None

    def history(self, session_id, max_messages=80):
        """新版无 session.history：用 session.page 拉最近事件，返回兼容旧解析的
        [{"event": {seq, type, data, ...}}, ...]。"""
        through = self._cursor_of(session_id)
        if through is None:
            return []
        args = {
            "request": {
                "address": {"kind": "session", "sessionId": session_id},
                "throughSeq": through,
                "maxMessages": max_messages,
            }
        }
        try:
            res = self.rpc("session/page", args, timeout=10)
        except DshError:
            through = self._cursor_of(session_id)
            if through is None:
                return []
            args["request"]["throughSeq"] = through
            res = self.rpc("session/page", args, timeout=10)
        records = (res.get("value") or {}).get("records", []) or []
        return [{"event": r.get("event") or {}} for r in records]

    def recent_messages(self, session_id, limit=12, anchor_text=None, max_events=600):
        """取会话里的 user/assistant 文本消息（工具调用/步骤事件忽略）。

        用途：面板「命令历史 → 看会话」——DSH（桌面版与标准版 harness 共用同一套 Web 前端）
        没有按会话直达的 URL。这是源码层面验过的硬限制（dsh-web-frontend 0.1.5-rc.2 的 app 与
        vendor bundle 里都没有 location.search / location.hash / URLSearchParams /
        sessionStorage / location.pathname，前端不解析任何 query/hash 参数），不是 ECHO 的疏漏，
        所以只能由 ECHO 用带签名 Cookie 的 RPC 读出来渲染在面板里。

        anchor_text：传命令原文时，定位到该指令那一轮（user 消息里含这段文本），
        从那里往后取 limit 条——同一会话里多条指令各有各的上下文，而不是都看会话尾部。
        找不到就退回"最近 limit 条"。

        返回 {"messages": [{"role","seq","text"}, ...], "anchored": bool, "scanned": int}
        """
        try:
            events = self.history(session_id, max_messages=max_events)
        except DshError:
            raise
        except Exception:
            return {"messages": [], "anchored": False, "scanned": 0}
        out = []
        for ev in events:
            obj = ev.get("event") or {}
            etype = obj.get("type")
            if etype not in ("user/message", "assistant/message"):
                continue
            data = obj.get("data") or {}
            # 新版 data 直接是 message；旧版包一层 {"message": {...}}（与 wait_for_reply 同规则）
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or ("assistant" if etype.startswith("assistant") else "user")
            texts = [c.get("text", "") for c in (msg.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
            text = "".join(texts).strip()
            if not text:
                continue
            out.append({"role": role, "seq": obj.get("seq"), "text": text})
        n = max(1, int(limit))
        key = (anchor_text or "").strip()
        if key:
            # 同一条指令可能发过多次，取最后一次出现的位置；窗口到"下一个用户消息"为止，
            # 这样看到的正好是这条指令那一轮（用户提问 + 助手的回复），不把下一轮混进来
            for i in range(len(out) - 1, -1, -1):
                if out[i]["role"] == "user" and key in out[i]["text"]:
                    end = len(out)
                    for j in range(i + 1, len(out)):
                        if out[j]["role"] == "user":
                            end = j
                            break
                    return {"messages": out[i:min(end, i + n)], "anchored": True,
                            "scanned": len(out)}
        return {"messages": out[-n:], "anchored": False, "scanned": len(out)}

    def prompt(self, session_id, text, mode="queue"):
        args = {
            "request": {
                "requestId": str(uuid.uuid4()),
                "sessionId": session_id,
                "mode": mode,
                "content": [{"type": "text", "text": text}],
            }
        }
        res = self.rpc("session/prompt", args, timeout=10)
        return res.get("ok", False)

    def cancel(self, session_id):
        """取消会话当前正在执行的一轮（模型卡在交互提问/工具时用于解卡）。"""
        try:
            return self.rpc("session/cancel", {"request": {"sessionId": session_id}}, timeout=8)
        except Exception:
            return None

    def is_running(self, session_id):
        try:
            for item in self.list_sessions():
                if item.get("sessionId") == session_id:
                    return bool(item.get("running"))
        except Exception:
            pass
        return False

    def clear_stuck(self, session_id):
        """会话若被卡住（running 且上一轮未完成），取消之，保证新命令可进。"""
        if self.is_running(session_id):
            self.cancel(session_id)
            time.sleep(1)
            return True
        return False

    # ------------------------------------------------------------- 命令流程

    # 延续意图词：文本命中任一即视为用户要求接着上一话题（不轮换默认会话）。
    # 词表偏宽是刻意的——误保留旧会话最多多耗一次上下文，误轮换则丢衔接。
    _CONTINUATION_RE = re.compile(
        r"继续|接着说|接着上|上一(个|次|段|轮|条)?(话题|问题|对话|内容|指令|命令)?"
        r"|刚才|刚才说|上回|上次|回顾|回到刚才|之前(说|讨论|聊|提到|那个|的)"
        r"|往下说|然后呢|还有呢|聊到哪|说到哪")

    def session_cwd(self, session_id):
        """查某个会话的实际工作目录（用于判断它是否落在目标工作区里）。"""
        try:
            for it in self.list_sessions():
                if it.get("sessionId") == session_id:
                    return it.get("cwd") or ""
        except Exception:
            pass
        return ""

    def _new_default_session(self, workspace):
        """按配置的默认工作区新建会话。

        走 workspaceId（而非 cwd）才会被 DSH 登记进该工作区，侧栏里才归组；
        未配置工作区时退回 cwd=ECHO 根目录的旧行为。
        """
        if workspace:
            try:
                # 分组名走 app/workspaces.py 这一份事实源（默认「指令空间」；
                # 用户自己配的工作区则仍用目录名，行为不变）。
                from app import workspaces as spaces_mod
                wid, created = self.ensure_workspace(
                    workspace, title=spaces_mod.title_for_path(workspace))
                if wid:
                    sid = self.create_session(workspace_id=wid)
                    if sid:
                        db.add_log("info", "assistant",
                                   f"默认会话建在 DSH 工作区「{os.path.basename(workspace)}」"
                                   f"{'（新建工作区）' if created else ''} {sid}")
                        return sid
            except Exception as e:
                db.add_log("warn", "assistant",
                           f"按工作区新建默认会话失败，回退 cwd 方式：{e}")
        return self.create_session(cwd=echo_workspace())

    def ensure_command_session(self, text=""):
        """取默认（command）命令会话。

        轮换规则：
          * 距上次使用超过 commandIdleRotateHours 小时且本次未要求延续上一话题；
          * **或该会话不在配置的默认工作区里**（例如用户刚把默认工作区改成
            「日常交互」）——这时轮换一次，让新会话出现在正确的工作区分组下。
        commandIdleRotateHours=0 或空 时关闭按空闲轮换（工作区不匹配仍会轮换）。
        """
        # 只认本后端建的会话（2026-09-19）：切到独立 harness 后，绝不能拿着 Desktop 的
        # session_id 去发命令 —— 那正是"配置了独立 dsh 但命令还是发到 desktop"的原因。
        row = db.get_session("command", agent=self.name)
        sid = (row or {}).get("session_id") or ""
        # 工作区路径**问路径层**（3.0：`{echoBase}/aide`；老装机仍是 `{ECHO}/data/command`）。
        # 原来直接读设置字符串，于是布局一变、或用户改了「会议/指令目录」，
        # 会话就静默落到老目录（看着像"侧栏里多了个未分组"）。
        from app import paths as _paths
        want_ws = _paths.command_root()
        idle_h = None
        if row and sid:
            used = (row.get("last_used_at") or "").strip()
            try:
                last = datetime.datetime.strptime(used, "%Y-%m-%d %H:%M:%S")
                idle_h = (datetime.datetime.now() - last).total_seconds() / 3600.0
            except (ValueError, TypeError):
                idle_h = None
        rotate_hours = float(settings.get("commandIdleRotateHours", 4) or 0)
        wants_cont = bool(self._CONTINUATION_RE.search(text or ""))

        # 会话位置不对（配置了工作区但现会话不在其中）→ 必须轮换
        misplaced = False
        if sid and want_ws:
            cur = os.path.normcase(os.path.normpath(self.session_cwd(sid) or ""))
            tgt = os.path.normcase(os.path.normpath(want_ws))
            misplaced = bool(cur) and cur != tgt
            if misplaced:
                db.add_log("info", "assistant",
                           f"默认命令会话不在配置的工作区（现 {self.session_cwd(sid)} "
                           f"≠ 目标 {want_ws}），轮换到新会话")

        idle_rotate = (sid and idle_h is not None and rotate_hours > 0
                       and idle_h >= rotate_hours and not wants_cont)
        if sid and (misplaced or idle_rotate):
            new_sid = self._new_default_session(want_ws)
            if new_sid:
                db.upsert_session("command", new_sid, "命令会话", agent=self.name)
                if idle_rotate and not misplaced:
                    db.add_log("info", "assistant",
                               f"默认命令会话空闲 {idle_h:.1f}h（阈值 {rotate_hours:g}h）"
                               f"且未要求延续，已轮换新会话 {new_sid}")
                sid = new_sid
        if not sid:
            sid = self._new_default_session(want_ws)
            if sid:
                db.upsert_session("command", sid, "命令会话", agent=self.name)
        db.touch_session("command")  # 记录本次使用时间，作为下次轮换依据
        return sid

    def ensure_session(self, kind, name=""):
        """取回（无则创建）指定用途的会话：command / summary。

        `agent=self.name` 让"换后端"自动失效旧会话（见 db.get_session 的说明）。
        """
        row = db.get_session(kind, agent=self.name)
        if row and row.get("session_id"):
            return row["session_id"]
        from app import paths as _paths
        ws = _paths.command_root() if kind == "command" else ""
        sid = self._new_default_session(ws)
        if sid:
            db.upsert_session(kind, sid, name or kind, agent=self.name)
        return sid

    def wait_for_reply(self, session_id, timeout=90, poll=0.5):
        """发送后轮询会话事件，等待助手最终回复。

        返回 (reply, done)：
          reply — 最后一条 assistant 文本（中间消息会被最终回复覆盖）
          done  — 会话空闲（running=false）或事件序列稳定

        信号1：session.list 中该会话 running=false
        信号2：事件 seq 停止推进且已取到回复
        """
        sent_seq = None
        try:
            events = self.history(session_id, max_messages=3)
            if events:
                sent_seq = events[-1]["event"].get("seq")
        except Exception:
            pass

        reply = None
        done_now = False
        last_seq = -1
        stable = 0
        saw_turn_end = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            done_now = False
            try:
                for item in self.list_sessions():
                    if item.get("sessionId") == session_id:
                        done_now = not bool(item.get("running"))
                        break
            except Exception:
                pass
            cur_seq = -1
            try:
                events = self.history(session_id, max_messages=120)
                if events:
                    cur_seq = events[-1]["event"].get("seq", -1)
                latest = None
                for ev in events:
                    ev_obj = ev.get("event") or {}
                    if sent_seq is not None and ev_obj.get("seq", 0) <= sent_seq:
                        continue
                    if ev_obj.get("type") == "turn/end":
                        # 本轮用户请求完整处理结束的可靠信号（DSH 会多次 read 大文件，
                        # 工具调用间隙 running 会短暂闪断，不能据此判定完成）
                        saw_turn_end = True
                    if ev_obj.get("type") == "assistant/message":
                        # 新版 data 直接是 message（旧版是 {"message": ...}），两者都兼容
                        msg = ev_obj.get("data") or {}
                        if isinstance(msg, dict) and isinstance(msg.get("message"), dict):
                            msg = msg["message"]
                        if msg.get("role") == "assistant":
                            texts = [c.get("text", "") for c in (msg.get("content") or [])
                                     if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
                            if texts:
                                latest = "".join(texts).strip()
                if latest:
                    reply = latest  # 持续用最新助手文本覆盖，turn 结束时即为最终交付
            except Exception:
                pass
            if cur_seq == last_seq:
                stable += 1
            else:
                stable = 0
                last_seq = cur_seq
            # 完成判定：拿到本轮 turn/end 且已取到助手文本且事件序列稳定（trailing 消息落定）。
            # 兜底：无法识别 turn/end（旧版本）时，会话空闲 + 回复稳定才返回。
            if saw_turn_end and reply and stable >= 2:
                break
            if (not saw_turn_end) and done_now and reply and stable >= 8:
                break
        if reply is None:
            # 超时未收到回复：取消当前轮，避免会话被卡住影响下一条命令
            try:
                self.cancel(session_id)
            except Exception:
                pass
        return reply, done_now


def build():
    """适配器工厂：注册表按需调用。"""
    return DshAgent()


# 导入期登记到注册表（懒加载由 agents._autoload 触发）
from app.agents import register as _register          # noqa: E402

_register(DshAgent, build)
