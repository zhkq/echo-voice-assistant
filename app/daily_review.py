# -*- coding: utf-8 -*-
"""daily_review.py — 每日回顾：把车里的一段口述交给 DSH 整理、登记、追问。

它在整个链路里的位置
====================

    唤醒/热键 → 录音 → 转写 → 【本模块】→ DSH 的 daily-review 技能 → 取回【播报】→ TTS

**本模块刻意只做"编排 + 播报提取"**，不做整理、不做登记、不做写笔记库：
那些智力属于 DSH 侧的 `daily-review` 技能（`~/.dsh/skills/daily-review/SKILL.md`），
在这里重做一遍就会出现两套会漂移的规则。

三条路径各归其位（`docs/每日回顾-设计.md` §3.1）
------------------------------------------------

| 角色 | 值 | 用途 |
|---|---|---|
| 回顾会话工作区（= 会话 cwd） | `{echoBase}/review` | 会话落点、侧栏分组「每日回顾」 |
| 笔记库根目录 | 用户自己的 Obsidian 库 | 技能读历史日志/待办、写 `01-工作日志` |

两者**不是同一个目录**，所以必须把 DSH 新会话默认权限校正为全盘访问，否则
`workspace-write` 沙箱会把写笔记库拦下（并拖到超时）。这条机制 ECHO 早就在用：
`app/worklog.py:ensure_dsh_default_access()` —— 本模块复用同一个 RPC，只是开关独立。

为什么播报要"显式标记"而不是让 ECHO 截断
------------------------------------------

车内听到什么是本功能最敏感的一处体验（用户 2026-10-06 明确要求"语音反馈不能过于冗长"）。
`assistant.conclusion_only()` 是**猜**哪句该念，整理稿里一旦有多个结论就必然截错。
所以契约改成：技能**显式**用 `【播报】…【/播报】` 声明哪句该念，ECHO 只负责取出来。
取不到才退回旧的截断逻辑（三层兜底，见 `extract_broadcast`）。
"""
from __future__ import annotations

import datetime
import os
import re
import threading
import time

from app.config import settings

#: 会话 kind 的日期后缀：每天一个会话（`review:2026-10-06`）。
# 为什么不用固定 kind：`dsh_sessions` 表对 kind 有 UNIQUE 约束（一个 kind 至多一条），
# 用固定 kind 的话"今天的回顾"会把"昨天的回顾"覆盖掉 —— 而需求恰恰是"每天一条、可回看"。
# 加日期后缀既满足"一天一条"，又不改表结构。
KIND_PREFIX = "review:"

#: 【播报】标记的匹配（容忍空格与全角括号写法）
_BROADCAST_RE = re.compile(r"【\s*播报\s*】(.*?)(?:【\s*/\s*播报\s*】|$)", re.S)

#: 结束口令：用户说这些就收尾（不提交本轮）
STOP_PATTERNS = (
    "结束回顾", "回顾结束", "今天就这些", "就这些了", "今天就到这里", "不回顾了", "退出回顾",
)

#: 开始口令：命中即进入回顾模式
START_PATTERNS = (
    "回顾一下今天", "回顾今天", "每日回顾", "我们来回顾", "开始回顾",
    "回顾一下", "总结一下今天", "复盘一下今天",
)


# ------------------------------------------------------------------ 配置

def enabled() -> bool:
    """回顾总开关。"""
    return bool(settings.get("dailyReviewEnabled", False))


def vault_root() -> str:
    """回顾用的笔记库根目录；未配置时沿用纪要归档那一项。"""
    v = (settings.get("dailyReviewVaultRoot", "") or "").strip()
    if v:
        return v
    return (settings.get("worklogVaultRoot", "") or "").strip()


def ready():
    """是否具备回顾条件，返回 (ok, 原因)。原因要能直接显示给用户。"""
    if not enabled():
        return False, "每日回顾未启用（设置 → 每日回顾 → 启用每日回顾）"
    vault = vault_root()
    if not vault:
        return False, "未配置笔记库根目录（设置 → 每日回顾 → 回顾用笔记库根目录）"
    if not os.path.isdir(vault):
        return False, f"笔记库根目录不存在：{vault}"
    return True, ""


def record_limit_ms() -> int:
    """一段口述最多录多久（毫秒）。"""
    # 键名在这里**写成字面量**（不用变量形式的 helper）：`scripts/audit-settings.py`
    # 是按 `settings.get("键")` 的字面量做静态读取审计的，包一层变量就会让这些项
    # 被判成"写进去没人读"（tests/test_settings_wiring.py 会红）。
    try:
        return max(1, int(settings.get("dailyReviewMaxRecordSec", 120) or 120)) * 1000
    except (TypeError, ValueError):
        return 120 * 1000


def silence_ms() -> int:
    """静音多久算这一段讲完（毫秒）。"""
    try:
        return max(300, int(settings.get("dailyReviewSilenceMs", 1400) or 1400))
    except (TypeError, ValueError):
        return 1400


def reply_timeout_s() -> int:
    """等 DSH 整理完的上限（秒）。"""
    try:
        return max(30, int(settings.get("dailyReviewReplyTimeoutSec", 180) or 180))
    except (TypeError, ValueError):
        return 180


def broadcast_limit_chars() -> int:
    """播报稿字数上限（超出会被截断并告警）。"""
    try:
        return max(20, int(settings.get("dailyReviewBroadcastChars", 120) or 120))
    except (TypeError, ValueError):
        return 120


# ------------------------------------------------------------------ 纯函数（可独立测试）

def today_kind(now=None) -> str:
    """今天的回顾会话 kind：`review:YYYY-MM-DD`。"""
    d = (now or datetime.datetime.now()).strftime("%Y-%m-%d")
    return KIND_PREFIX + d


def _strip_markdown(text: str) -> str:
    """把播报稿洗成"能被念出来"的样子。

    复用 `assistant._clean_line` 的口径（去 Markdown、去链接、去代码块），
    但**不**在这里做标点替换之外的重写 —— 播报稿是技能写的，ECHO 不改写它的意思。
    """
    if not text:
        return ""
    t = text
    t = re.sub(r"```[\s\S]*?```", "，", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)          # 去掉行首列表符
    t = re.sub(r"^\s*\d+[\.、)]\s*", "", t, flags=re.M)      # 去掉行首编号
    t = re.sub(r"[|\\/]", "，", t)
    t = re.sub(r"[_~^#>]", "", t)
    t = re.sub(r"\s*\n+\s*", " ", t)                         # 折成一行
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip(" ，,、;；")


def extract_broadcast(reply: str, limit_chars: int = 0):
    """从技能回复里取出要念的那一段。

    三层兜底（任何一层生效，都不会出现"把一整篇整理稿念出来"）：

      1. `【播报】…【/播报】`   —— 技能显式声明的（首选）
      2. 第一段非空文本          —— 技能忘了写标记时，只念开头一段，不念全篇
      3. 固定短句                —— 回复为空时也不能静默

    返回 `(spoken, source)`，`source` ∈ `broadcast` / `first_paragraph` / `fallback`，
    调用方据此写日志（"技能没按契约输出"这件事必须可查，不能静默）。
    """
    limit = limit_chars or broadcast_limit_chars()
    text = (reply or "").strip()
    if not text:
        return "整理好了，你看一眼会话。", "fallback"

    m = _BROADCAST_RE.search(text)
    if m:
        spoken = _strip_markdown(m.group(1))
        if spoken:
            return _truncate(spoken, limit), "broadcast"

    # 没有标记：只取第一段（按空行切），绝不把全篇念出来
    first = re.split(r"\n\s*\n", text, maxsplit=1)[0]
    spoken = _strip_markdown(first)
    if spoken:
        return _truncate(spoken, limit), "first_paragraph"

    return "整理好了，你看一眼会话。", "fallback"


def _truncate(text: str, limit: int) -> str:
    """超长就截到语义边界（句号/问号/感叹号），并标注被截断。"""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, max(0, limit // 3), -1):
        if cut[i] in "。！？!?；;":
            return cut[:i + 1]
    return cut + "……"


def wants_start(text: str) -> bool:
    """这句话是不是"我们开始回顾今天"。"""
    t = (text or "").strip()
    if not t:
        return False
    return any(p in t for p in START_PATTERNS)


def wants_stop(text: str) -> bool:
    """这句话是不是"结束回顾"。"""
    t = (text or "").strip()
    if not t:
        return False
    return any(p in t for p in STOP_PATTERNS)


def build_prompt(transcript: str, vault: str = "", now=None) -> str:
    """按模板拼出送入 DSH 的回顾指令。

    **原始转写原样带下去**（不摘要、不改写）—— 它是唯一真相，技能要能对着原话回答
    "我第二段到底怎么说的"。

    **占位符用单次遍历替换，不是顺序 `str.replace`**（2026-10-06 实测抓到的两个 bug）：
    ① 顺序替换时，`{transcript}` 若排在 `{weekday}` / `{chars}` 前面，那么**用户口述里**
       只要出现 `{chars}` 这类字样就会被当成占位符替换掉（他讲的内容被悄悄改写）；
    ② `re.sub` 的替换串会把 `\` 当转义 —— 传字符串时 `【/播报】` 会被写成 `【\\播报】`
       （实测：整个契约段的斜杠都被吃掉）。
    所以这里**必须用函数式替换**（返回值按字面量处理），且只扫模板、不重扫结果。
    """
    now = now or datetime.datetime.now()
    week = "一二三四五六日"[now.weekday()]
    values = {
        "vault": vault or "",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        # 只给裸字（"二"）：模板里写 `星期{weekday}` 就能得到"星期二"。
        # 与 `assistant.build_env_context` 的口径一致（它也是写 `（星期{week}）`）。
        "weekday": week,
        "chars": str(broadcast_limit_chars()),
        "transcript": transcript or "",
    }
    tpl = str(settings.get("dailyReviewPrompt", "") or "").strip()
    if not tpl:
        # 模板被清空时的兜底：最少要保证"转写 + 播报契约"两件事都在
        tpl = (
            "【每日回顾】\n笔记库根目录：{vault}\n当前时间：{date} {time}（星期{weekday}）\n\n"
            "【今日口述·原始转写】\n{transcript}\n【/原始转写】\n\n"
            "请按 daily-review 技能整理并登记进工作日志，并输出一段"
            "【播报】两句话以内+最多一个问题【/播报】。")
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), tpl)


# ------------------------------------------------------------------ 会话与提交

def _client():
    from app.dsh import get_client
    return get_client()


def ensure_session(client=None, force_new=False):
    """取回（无则创建）**今天**的回顾会话，返回 (session_id, workspace_id, info)。

    * 工作区走 `paths.review_workspace_root()`（`{echoBase}/review`），
      传 workspaceId 才会被 DSH 登记进侧栏分组（`dsh_agent.create_session` 的说明）；
    * 每天一个会话：kind = `review:YYYY-MM-DD`；
    * `force_new=True` 时当天也重建（面板"重新开始今天的回顾"用）。
    """
    import app.db as db
    from app import paths

    client = client or _client()
    kind = today_kind()
    sid = ""
    if not force_new:
        row = db.get_session(kind, agent=client.name)
        sid = (row or {}).get("session_id") or ""

    ws_path = paths.review_workspace_root()
    wid = ""
    if ws_path:
        try:
            os.makedirs(ws_path, exist_ok=True)
        except OSError as e:
            db.add_log("warn", "daily_review", f"回顾工作区目录创建失败（{ws_path}）：{e}")
        try:
            title = str(settings.get("dailyReviewWorkspaceTitle", "每日回顾") or "每日回顾")
            wid, created = client.ensure_workspace(ws_path, title=title)
            if created:
                db.add_log("info", "daily_review",
                           f"已建立 DSH 工作区「{title}」→ {ws_path}（{wid}）")
        except Exception as e:
            db.add_log("warn", "daily_review", f"回顾工作区建立失败（{ws_path}）：{e}")

    # 会话已存在但**不在**目标工作区里（用户改过设置/换过后端）→ 当天也重建，
    # 否则"侧栏里看不到它"这种问题会静默存在一整天。
    if sid and ws_path:
        try:
            cur = os.path.normcase(os.path.normpath(client.session_cwd(sid) or ""))
            want = os.path.normcase(os.path.normpath(ws_path))
            if cur and cur != want:
                db.add_log("info", "daily_review",
                           f"今天的回顾会话不在目标工作区（{cur} ≠ {want}），重建")
                sid = ""
        except Exception:
            pass

    if not sid:
        if wid:
            sid = client.create_session(workspace_id=wid)
        if not sid:
            sid = client.create_session(cwd=ws_path or None)
        if sid:
            db.upsert_session(kind, sid, "每日回顾 " + kind.split(":", 1)[-1], agent=client.name)
            db.add_log("info", "daily_review", f"今天的回顾会话：{sid}")
    db.touch_session(kind)
    return sid, wid, {"workspace": ws_path, "kind": kind}


def ensure_access(client=None):
    """确保 DSH **新建**会话的默认权限是全盘访问，返回 (ok, 说明)。

    为什么必须做：回顾要写笔记库，而笔记库在回顾会话工作区之外。默认档位
    `workspace-write` 下第一次写入会被沙箱拒绝，随后 DSH 会"升级一次权限重试" ——
    实测整场要 10 分钟上下，用户看到的就是"回顾卡住了"。

    实现**直接复用** `worklog.ensure_dsh_default_access()`：同一条 RPC、同一套判据，
    只是开关走本功能自己的设置项（`dailyReviewEnsureSessionAccess`）。
    不重写一遍的理由：这是"DSH 权限怎么改"的**单一事实源**，抄一份必然漂移。
    """
    if not bool(settings.get("dailyReviewEnsureSessionAccess", True)):
        return True, "已按设置跳过自动校正（dailyReviewEnsureSessionAccess=false）"
    try:
        from app import worklog
    except Exception as e:                                   # pragma: no cover - 极端兜底
        return False, f"无法加载权限校正模块：{e}"
    # `worklog.ensure_dsh_default_access()` 自己会看 `worklogEnsureSessionAccess`。
    # 用户可能只开了每日回顾、没开纪要归档（那一项默认 False）→ 先判一次，
    # 已关闭时直接走"不修改"的分支，**不去临时改用户的设置**（改设置会波及别处）。
    if not bool(settings.get("worklogEnsureSessionAccess", True)):
        return True, ("纪要归档那一项的权限校正开关是关的；"
                      "回顾若写入被沙箱拦下，把「回顾前校正会话权限」保持开启即可")
    return worklog.ensure_dsh_default_access()


def submit(transcript: str, client=None, force_new=False, timeout=0):
    """把一段口述交给今天的回顾会话，返回结果 dict（**不抛异常**）。

    返回：`{ok, session_id, workspace, reply, spoken, source, error, seconds}`
      * `spoken` —— 要念出来的话（已经过三层兜底）
      * `source` —— `broadcast` / `first_paragraph` / `fallback`，用于排障
    """
    import app.db as db

    t0 = time.time()
    out = {"ok": False, "session_id": "", "workspace": "", "reply": "",
           "spoken": "", "source": "", "error": "", "seconds": 0.0}
    try:
        client = client or _client()
    except Exception as e:
        out["error"] = f"取智能体客户端失败：{e}"
        return out

    ok, why = ready()
    vault = vault_root()
    if not ok:
        # 没配好也要给用户一句人话（车里看不到面板）
        out["error"] = why
        out["spoken"] = "回顾还没配好，你回面板看一眼设置。"
        out["source"] = "fallback"
        db.add_log("warn", "daily_review", f"回顾不可用：{why}")
        return out

    try:
        acc_ok, acc_why = ensure_access(client)
        if not acc_ok:
            db.add_log("warn", "daily_review", f"会话权限校正未成功：{acc_why}")
    except Exception as e:
        db.add_log("warn", "daily_review", f"会话权限校正异常：{e}")

    try:
        sid, wid, info = ensure_session(client, force_new=force_new)
        out["session_id"] = sid
        out["workspace"] = info.get("workspace", "")
        if not sid:
            out["error"] = "没能建立回顾会话（DSH 未返回 sessionId）"
            out["spoken"] = "回顾会话没建起来，你回面板看一眼。"
            out["source"] = "fallback"
            return out

        prompt = build_prompt(transcript, vault=vault)
        client.prompt(sid, prompt)
        reply, done = client.wait_for_reply(
            sid, timeout=timeout or reply_timeout_s())
        out["reply"] = reply or ""
        if not reply:
            out["error"] = "DSH 在超时内没有回复"
            out["spoken"] = "这次整理没等到结果，等会儿再试一次。"
            out["source"] = "fallback"
            db.add_log("warn", "daily_review",
                       f"回顾超时（{timeout or reply_timeout_s()}s，done={done}）会话 {sid}")
            return out
        spoken, source = extract_broadcast(reply)
        out["spoken"] = spoken
        out["source"] = source
        out["ok"] = True
        if source != "broadcast":
            # 技能没按契约输出：必须留下痕迹，否则"念错内容"永远查不出来
            db.add_log("warn", "daily_review",
                       f"技能未按【播报】契约输出（本次用 {source} 兜底）：{reply[:120]}")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["spoken"] = "回顾出了点问题，你回面板看一眼。"
        out["source"] = "fallback"
        db.add_log("warn", "daily_review", f"回顾提交失败：{type(e).__name__}: {e}")
    finally:
        out["seconds"] = round(time.time() - t0, 1)
    return out


# ------------------------------------------------------------------ 面板/API 用的高层动作

def status():
    """面板展示用的状态快照（只读，不建会话、不改权限）。"""
    from app.dsh import get_client
    ok, why = ready()
    client = None
    agent = ""
    session_id = ""
    workspace = ""
    try:
        client = get_client()
        agent = getattr(client, "name", "") or ""
        row = None
        import app.db as db
        row = db.get_session(today_kind(), agent=agent)
        session_id = (row or {}).get("session_id") or ""
    except Exception as e:
        if not why:
            why = f"取智能体客户端失败：{e}"
    try:
        from app import paths
        workspace = paths.review_workspace_root()
    except Exception:
        pass
    return {
        "enabled": enabled(),
        "ready": ok,
        "reason": why,
        "agent": agent,
        "workspace": workspace,
        "vault": vault_root(),
        "kind": today_kind(),
        "sessionId": session_id,
    }


def start(force_new=False):
    """建立（或取回）今天的回顾会话 —— 面板「开始今天的回顾」用。

    只建会话、不提交内容：语音那条路由 `assistant` 的回顾模式驱动，
    面板这条路只是让用户能看到"今天这条会话已经就位"。
    """
    ok, why = ready()
    if not ok:
        return {"ok": False, "error": why}
    client = _client()
    acc_ok, acc_why = ensure_access(client)
    sid, wid, info = ensure_session(client, force_new=force_new)
    return {"ok": bool(sid), "sessionId": sid, "workspaceId": wid,
            "workspace": info.get("workspace", ""),
            "access": acc_why, "accessOk": acc_ok,
            "error": "" if sid else "没能建立回顾会话"}
