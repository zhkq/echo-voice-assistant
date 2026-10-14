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


#: 「续说轮次」的指令模板（2026-10-14 用户口径：*"不用带首次回顾那么复杂的系统提示词，
#: 用户的转写要求不突出"*）。
#:
#: 第一轮那份 `dailyReviewPrompt` 里有四条长篇要求（读历史、双向链接、遗漏检查……）——
#: 那是**开场**才需要的口径；第 2 轮起用户只是接着补一句，长要求会把"我这段说了什么"
#: 淹没掉，技能反而容易跑偏。所以续说轮次只给三件事：**转写（唯一真相）+ 登记 + 播报契约**。
#: 其余规则本来就在 `daily-review` 技能里写着，不必每轮重复。
#:
#: 想改文案可以设隐藏设置 `dailyReviewPromptNext`（与 `dailyReviewPrePrompt` 同一套做法：
#: 读不到就用这一份，不定义也能跑）。
DEFAULT_NEXT_PROMPT = (
    "【每日回顾·续说】\n"
    "笔记库根目录：{vault}\n"
    "当前时间：{date} {time}（星期{weekday}）\n"
    "\n"
    "【我刚才说的（原始转写，唯一真相，不要改写）】\n"
    "{transcript}\n"
    "【/原始转写】\n"
    "\n"
    "请按 daily-review 技能把**这一段**登记进工作日志（已有内容只追加、别覆盖），"
    "需要的话顺带补一句待办。\n"
    "本回合只回一段【播报】…【/播报】：口语化、无 Markdown、两句话以内 + 最多一个问题"
    "（超过 {chars} 字会被截断）。")


def build_prompt(transcript: str, vault: str = "", now=None, turn: int = 1) -> str:
    """按模板拼出送入 DSH 的回顾指令。

    **原始转写原样带下去**（不摘要、不改写）—— 它是唯一真相，技能要能对着原话回答
    "我第二段到底怎么说的"。

    `turn` 决定用哪一份模板（见 `DEFAULT_NEXT_PROMPT`）：
      * `turn <= 1`（当天第一次口述）→ `dailyReviewPrompt`：完整口径；
      * `turn >= 2`（续说）→ `dailyReviewPromptNext`（缺省 = `DEFAULT_NEXT_PROMPT`）：
        短、且把用户转写顶到最前面。

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
    try:
        first = int(turn or 1) <= 1
    except (TypeError, ValueError):
        first = True
    if first:
        tpl = str(settings.get("dailyReviewPrompt", "") or "").strip()
    else:
        # ⚠️ 键名写成字面量：`scripts/audit-settings.py` 按 `settings.get("键")` 做静态审计，
        # 包一层变量会让它被判成"写进去没人读"（tests/test_settings_wiring.py 会红）。
        tpl = str(settings.get("dailyReviewPromptNext", "") or "").strip() or DEFAULT_NEXT_PROMPT
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
            try:
                sid = client.create_session(workspace_id=wid)
            except Exception as e:
                # ⚠️ **工作区失配不该变成 500**（2026-10-10 用户实测修）。
                # `dsh_agent._workspace_registry_path()` 的注释早就写明这条路：
                # "拿到别的后端的旧 workspaceId，建会话时报 not-found → **回退 cwd**"。
                # 但这里原来把异常直接放走，于是下面那行 `cwd=` 兜底**永远走不到** ——
                # 现场：切到稳定版后 ECHO 复用了 dev 的 harness，本树的工作区在那边不存在，
                # 点「开始回顾」直接 500（日志里是 workspace/not-found）。
                db.add_log("warn", "daily_review",
                           f"按 workspace({wid}) 建回顾会话失败：{e} —— 丢掉它，按目录重建")
                wid = ""
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


# ------------------------------------------------- 轮次等待：按"会话进展"判完成
#
# 2026-10-14 用户口径（原话）："目前你配置了每轮等待的最大时长，但是回顾动作耗时都比较长，
# 你能不能通过探测会话进展来确定会话是否执行完成，而不是直接开启下一轮"。
#
# 原来的做法是 `wait_for_reply(sid, timeout=dailyReviewReplyTimeoutSec)` 一把等 180 秒：
#   * 一轮真的跑了 4 分钟（读日志 + 写日志 + 追问）→ 180 秒到点，ECHO 判"没回复"，
#     但**智能体还在干活** —— 紧接着就开麦录下一轮，下一段口述插进正在跑的轮次里；
#   * 反过来，秒回的那种也要空等到窗口结束（`wait_for_reply` 的判定框是"turn/end + 文本稳定"，
#     基本能早退，但一旦漏判就是干等）。
#
# 现在改成**探测会话进展**：
#   ① 会话空闲连续 `ROUND_IDLE_POLLS` 次 + 已拿到助手文本 → 完成；
#   ② 空闲了却一条文本都没有 → 这一轮没结果，如实返回（不空等）；
#   ③ **还在跑就继续等**（读日志/写日志/调工具都算进展）；沉默超过
#      `dailyReviewReplyTimeoutSec`（语义改为"多久没动静就提醒用户一句"）只在日志与
#      `on_wait` 回调里报一次，**不掐断**；
#   ④ 硬上限 `ROUND_HARD_MAX_S`（15 分钟）到了才放弃 —— 真卡死也要给用户一句话。
#
# 全程 `cancel_on_timeout=False`：等待本身绝不许取消会话（2026-10-14 的前置环节事故，
# 见 `preflight` 的说明）。
ROUND_POLL_S = 3.0          # 每 3 秒探一次"这一轮做完没有"
ROUND_IDLE_POLLS = 2        # 连续两次空闲才算做完（工具调用间隙 `running` 会闪断）
ROUND_HARD_MAX_S = 900.0    # 单轮硬上限：15 分钟
ROUND_START_GRACE_S = 12.0  # 指令刚发下去时的宽限：还没开始跑就先别判"没结果"


def _probe_reply(client, session_id, timeout, poll=ROUND_POLL_S):
    """调 `wait_for_reply`，**逐级退让**到替身/老适配器也能用。

    `cancel_on_timeout=False` 是 2026-10-14 才加的（见 `preflight`）：不认这个参数的
    适配器/测试替身要能照常工作，所以从"参数最全"试到"只有 timeout"。
    """
    for kwargs in ({"timeout": timeout, "poll": poll, "cancel_on_timeout": False},
                   {"timeout": timeout, "poll": poll},
                   {"timeout": timeout}):
        try:
            return client.wait_for_reply(session_id, **kwargs)
        except TypeError:
            continue
    return client.wait_for_reply(session_id, timeout)


def _session_cursor(client, session_id):
    """会话当前的事件游标（seq）—— "有没有新进展"的信号；取不到返回 None。

    走公开的 `history()`（`DshAgent` 用 `session/page` 读出来的 seq）；没有这个方法的
    后端（CodeBuddy 那种一调用就完事的）返回 None，等待逻辑会自动退化成"只看空闲/文本"。
    """
    fn = getattr(client, "history", None)
    if not callable(fn):
        return None
    try:
        events = fn(session_id, max_messages=3)
        if events:
            return (events[-1].get("event") or {}).get("seq")
    except Exception:                                            # noqa: BLE001 - 游标只是加速信号
        return None
    return None


def wait_for_turn(client, session_id, stall_s=0, hard_max_s=0, on_wait=None) -> dict:
    """等**这一轮**的会话真的做完（按进展判），返回 dict —— **不抛异常、不取消会话**。

    返回：`{reply, done, seconds, idle, progress, stalled, error}`
      * `reply` —— 本轮最后一条助手文本（跨窗口累积，不会因为"窗口没新消息"丢掉）；
      * `done`  —— 会话空闲且拿到了文本（= 这一轮真的做完了）；
      * `stalled` —— 期间出现过"超过 `stall_s` 没动静"（只用于**提醒**，不掐断）；
      * `error` —— 没拿到结果时的人话原因。
    """
    import app.db as db

    stall = float(stall_s or reply_timeout_s())
    hard = float(hard_max_s or ROUND_HARD_MAX_S)
    t0 = time.time()
    last_progress = t0
    last_cursor = _session_cursor(client, session_id)
    best = ""
    idle = 0
    moves = 0
    warned = False
    out = {"reply": "", "done": False, "seconds": 0.0, "idle": 0,
           "progress": 0, "stalled": False, "error": ""}
    while True:
        now = time.time()
        if now - t0 >= hard:
            out["error"] = "等整理超时（超过 %.0f 秒硬上限，会话可能卡住了）" % hard
            break
        if now - last_progress >= stall and not warned:
            # 只提醒一次：**不掐断**（长活可能就是一个工具在跑，没有中间事件）
            warned = True
            out["stalled"] = True
            db.add_log("warn", "daily_review",
                       "等整理时已有 %d 秒没有任何新进展（会话 %s）——继续等，不掐断"
                       % (int(now - last_progress), session_id))
        left = min(ROUND_POLL_S, max(0.5, hard - (now - t0)))
        try:
            reply, done = _probe_reply(client, session_id, left)
        except Exception as e:                                   # noqa: BLE001 - 探测出错不许卡住
            out["error"] = "等整理时探测失败：%s" % e
            break
        if reply:
            best = reply
            last_progress = time.time()
        cur = _session_cursor(client, session_id)
        if cur is not None and cur != last_cursor:
            last_cursor = cur
            moves += 1
            last_progress = time.time()
        idle = idle + 1 if done else 0
        if callable(on_wait):
            try:
                on_wait(round(time.time() - t0, 1), out["stalled"])
            except Exception:                                    # noqa: BLE001 - 提醒失败不致命
                pass
        if idle >= ROUND_IDLE_POLLS:
            if best:
                out["done"] = True
            elif moves == 0 and (time.time() - t0) < ROUND_START_GRACE_S:
                pass            # 指令刚发下去、会话还没开始跑：再等一轮，别急着判"没结果"
            else:
                out["error"] = "本轮会话结束了，但没拿到整理结果（智能体没输出文本）"
            if out["done"] or out["error"]:
                break
    out["reply"] = best
    out["idle"] = idle
    out["progress"] = moves
    out["seconds"] = round(time.time() - t0, 1)
    db.add_log("info" if out["done"] else "warn", "daily_review",
               "等整理%s（%.1fs，事件 %d 次%s）"
               % ("完成" if out["done"] else "未完成", out["seconds"], moves,
                  "" if out["done"] else "：" + out["error"]))
    return out


def submit(transcript: str, client=None, force_new=False, timeout=0,
           turn=0, on_wait=None):
    """把一段口述交给今天的回顾会话，返回结果 dict（**不抛异常**）。

    返回：`{ok, session_id, workspace, reply, spoken, source, error, seconds}`
      * `spoken` —— 要念出来的话（已经过三层兜底）
      * `source` —— `broadcast` / `first_paragraph` / `fallback`，用于排障

    `turn`：当天的第几段口述（1 = 第一次）。**不传（0）就按今天已经落库的轮数自动算**，
    于是"续说"自动用短模板（见 `build_prompt` / `DEFAULT_NEXT_PROMPT`）。
    `on_wait(seconds, stalled)`：等待期间的回调（面板/语音用它报一句"还在整理"）。
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

        # 提示词内容由设置 `dailyReviewPrompt`（首轮）/ `dailyReviewPromptNext`（续说）决定
        # （**代码不再按轮次改写它**：2026-10-13 用户提醒"稳定版已经在回顾过程中迭代过"
        # —— 尊重那份模板）。轮次只用来**选哪一份模板**：第 2 段起用短的那份。
        try:
            turn_no = int(turn or 0)
        except (TypeError, ValueError):
            turn_no = 0
        if turn_no <= 0:
            turn_no = len(_rows_for_date(time.strftime("%Y-%m-%d", time.localtime()))) + 1
        prompt = build_prompt(transcript, vault=vault, turn=turn_no)
        client.prompt(sid, prompt)
        wait = wait_for_turn(client, sid, stall_s=timeout or reply_timeout_s(),
                             on_wait=on_wait)
        out["reply"] = wait.get("reply") or ""
        if not out["reply"]:
            out["error"] = wait.get("error") or "DSH 在超时内没有回复"
            out["spoken"] = "这次整理没等到结果，等会儿再试一次。"
            out["source"] = "fallback"
            db.add_log("warn", "daily_review",
                       "回顾没有结果：%s（会话 %s，等 %.1fs）"
                       % (out["error"], sid, wait.get("seconds") or 0.0))
            return out
        spoken, source = extract_broadcast(out["reply"])
        out["spoken"] = spoken
        out["source"] = source
        out["ok"] = True
        if source != "broadcast":
            # 技能没按契约输出：必须留下痕迹，否则"念错内容"永远查不出来
            db.add_log("warn", "daily_review",
                       f"技能未按【播报】契约输出（本次用 {source} 兜底）：{out['reply'][:120]}")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["spoken"] = "回顾出了点问题，你回面板看一眼。"
        out["source"] = "fallback"
        db.add_log("warn", "daily_review", f"回顾提交失败：{type(e).__name__}: {e}")
    finally:
        out["seconds"] = round(time.time() - t0, 1)
        try:
            _persist(transcript, out)
        except Exception as e:                                     # pragma: no cover - 兜底
            # **落库失败不许影响这次回顾**：用户已经在车里听到播报了，
            # 记不进去只该在日志里留一句（历史页少一条，比"回顾失败"轻得多）。
            db.add_log("warn", "daily_review", f"回顾落库失败：{type(e).__name__}: {e}")
    return out


#: `commands.source` 里给回顾用的标记。**复用它而不是另建一张表**（2026-10-06 的决定）：
#: `commands` 的字段正好对得上 —— `text` 是我口述的原文、`reply` 是整理结果、
#: `brief` 是播报（`spoken`）、还有 `session_id/duration_ms/error/meta`，
#: 而且它已经有 `idx_commands_ts` 索引与分页查询。另建表等于把这些再抄一遍。
REVIEW_SOURCE = "review"


def _persist(transcript: str, out: dict) -> int:
    """把**一轮**回顾写进 `commands`（源标记 `source='review'`）。返回那行的 id。

    为什么一轮一条（而不是一天一条）：回顾是**多轮**的 —— 每轮用户说一段、DSH 整理一次。
    一轮一条最自然，也最有信息量（哪一轮说了什么、播报了什么都在）。
    历史页按 `date` 分组展示（见 `history_list()`），所以"按天看"的体验不受影响。

    `meta` 里放 `date`（`YYYY-MM-DD`）与 `kind`（`review:YYYY-MM-DD`，即那条 DSH 会话的
    登记键）—— 前者让"按天取"是一次简单的等值查询，后者让排障时能直接对上会话。
    失败也记（`status='failed'` + `error`）：**"哪几次回顾失败了"必须查得到**，
    否则用户说"昨天那次没记上"时无从判断。
    """
    import app.db as db

    ok = bool(out.get("ok"))
    now = time.localtime()
    date = time.strftime("%Y-%m-%d", now)
    row_id = db.add_command(
        transcript,
        source=REVIEW_SOURCE,
        status="done" if ok else "failed",
        session_id=str(out.get("session_id") or ""),
        meta={"review": True, "date": date, "kind": today_kind(),
              "workspace": str(out.get("workspace") or ""),
              "spokenSource": str(out.get("source") or "")})
    db.update_command(
        row_id,
        reply=str(out.get("reply") or ""),
        brief=str(out.get("spoken") or ""),
        duration_ms=int(round(float(out.get("seconds") or 0) * 1000)),
        error="" if ok else str(out.get("error") or ""))
    return int(row_id)


def _rows_for_date(date: str) -> list:
    """某一天的全部回顾轮次（按时间正序 —— 详情页要按"先说的在前"读）。"""
    import app.db as db
    return db._query(                                            # noqa: SLF001 - 同包内部工具
        "SELECT * FROM commands WHERE source=? AND ts LIKE ? ORDER BY ts ASC, id ASC",
        (REVIEW_SOURCE, str(date) + "%"))


def history_list(limit: int = 60) -> dict:
    """回顾历史：**按天倒序**，每天给出轮数、最后一次的时间与播报摘要。

    一天一行（而不是一轮一行）—— 用户问"回顾历史"时想看的是**哪天做了回顾、
    那天说了什么**；一天几十轮的原始记录堆在列表里没法看。轮次留给详情页。
    """
    import app.db as db
    rows = db._query(                                            # noqa: SLF001
        "SELECT id, ts, brief, error, status, session_id FROM commands "
        "WHERE source=? ORDER BY ts DESC, id DESC", (REVIEW_SOURCE,))
    days, order = {}, []
    for d in rows:
        date = str(d.get("ts") or "")[:10]
        if not date:
            continue
        if date not in days:
            days[date] = {"date": date, "turns": 0, "failed": 0,
                          "lastAt": str(d.get("ts") or ""),
                          "brief": "", "sessionId": ""}
            order.append(date)
        item = days[date]
        item["turns"] += 1
        if str(d.get("status") or "") == "failed":
            item["failed"] += 1
        # 摘要取**当天最新一条有内容的播报**：行是按 `ts DESC` 来的，所以"这一天第一次出现"
        # 就是最新那条 —— 只在第一次填，别被后面（更早）的覆盖掉。
        # ⚠️ 这里**不**退回 `text`：`brief` 是"念给用户听的那句"，列表里混入口述原文
        # 会让"摘要"这一列时而是播报、时而是原文，读起来像两种东西。
        if item["turns"] == 1 and str(d.get("brief") or "").strip():
            item["brief"] = str(d["brief"]).strip()
        if not item["sessionId"]:
            item["sessionId"] = str(d.get("session_id") or "")
    items = [days[d] for d in order][: max(1, int(limit or 60))]
    return {"items": items, "total": len(order), "today": time.strftime("%Y-%m-%d")}


def history_detail(date: str) -> dict:
    """某天的回顾详情：每一轮的原文 + 播报 + 状态，外加当天工作日志的路径与是否在。

    `vaultNote` 是**最终成果所在**（DSH 技能把整理好的条目写进 Obsidian 工作日志）——
    面板给一个"打开它"的按钮，用户就能从"我说了什么"跳到"记成了什么"。
    """
    import os

    date = str(date or "").strip()
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        return {"ok": False, "error": "日期格式要是 YYYY-MM-DD", "date": date}
    rows = _rows_for_date(date)
    turns = []
    for r in rows:
        turns.append({
            "id": int(r.get("id") or 0),
            "at": str(r.get("ts") or ""),
            "text": str(r.get("text") or ""),
            "reply": str(r.get("reply") or ""),
            "brief": str(r.get("brief") or ""),
            "status": str(r.get("status") or ""),
            "error": str(r.get("error") or ""),
            "seconds": round(float(r.get("duration_ms") or 0) / 1000.0, 1),
            "sessionId": str(r.get("session_id") or ""),
        })
    vault = vault_root()
    note = ""
    if vault:
        note = os.path.join(vault, "01-工作日志", date + ".md")
    return {"ok": True, "date": date, "turns": turns, "count": len(turns),
            "vault": vault, "vaultNote": note,
            "vaultNoteExists": bool(note and os.path.isfile(note))}


def history_summary() -> dict:
    """仪表盘那张卡片要的**一小口**数据：今天做没做、最近一次是什么。

    刻意不返回列表（那张卡只显示一行摘要）—— 卡片拉全量历史会让仪表盘
    每次刷新都读一遍几百行。
    """
    import app.db as db
    today = time.strftime("%Y-%m-%d")
    t = db._query_one(                                           # noqa: SLF001
        "SELECT COUNT(*) AS n, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f "
        "FROM commands WHERE source=? AND ts LIKE ?",
        (REVIEW_SOURCE, today + "%")) or {}
    last_d = db._query_one(                                      # noqa: SLF001
        "SELECT ts, brief, status, text FROM commands WHERE source=? "
        "ORDER BY ts DESC, id DESC LIMIT 1", (REVIEW_SOURCE,)) or {}
    return {
        "today": today,
        "todayTurns": int(t.get("n") or 0),
        "todayFailed": int(t.get("f") or 0),
        "lastAt": str(last_d.get("ts") or ""),
        "lastBrief": str(last_d.get("brief") or "").strip(),
        "lastStatus": str(last_d.get("status") or ""),
        "lastText": str(last_d.get("text") or "").strip()[:80],
    }


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
        #: 前置环节（技能第 0 步）现状 —— 面板据此显示"正在收集今天的素材"（2026-10-13）
        "pre": pre_state(),
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


# ---------------------------------------------------------------- 前置环节（技能第 0 步）

#: 前置环节的默认指令（对应技能 `daily-review` 的「第 0 步：自动收集当天素材」）。
#: **刻意不含 `{transcript}`** —— 这一步跑在用户口述**之前**，那时还没有口述。
#: 想改文案可以设隐藏设置 `dailyReviewPrePrompt`；读不到就用这一份（不定义也能跑）。
DEFAULT_PRE_PROMPT = (
    "【每日回顾·前置环节】\n"
    "笔记库根目录：{vault}\n当前时间：{date} {time}（星期{weekday}）\n\n"
    "**现在还没到我口述这一步**（我人在车里，稍后单独说）。请按 daily-review 技能的"
    "「第 0 步：自动收集当天素材」先把今天（{date}）的素材收集好：\n"
    "① 扫当天文件（企微 / 微信 / 桌面临时 / WPS 缓存，`scan_cache.py --days 1`），按归属登记；\n"
    "② 检查 `02-会议记录/ECHO自动纪要/{date}_*.md`，按 meeting-archive 的判定归档；\n"
    "【今天 ECHO 侧的会议（**纪要在这里，不在笔记库**）】\n{meetings}\n\n"
    "⚠️ 笔记库 `02-会议记录/ECHO自动纪要/` 里今天可能**还没有文件** —— 那是「归档的结果」、"
    "不是「今天没开会」：请以上面这份清单为准（纪要文件路径已给出，直接读它），"
    "按 meeting-archive 的规则归档/**再**登记。\n"
    "③ 汇总计数（文件登记数 / 其中专项数、会议归档数 / 其中例会数）留着给我后面播报用。\n"
    "这一步**静默执行**：不用问我问题、不用等我口述；遇到异常静默跳过、别中断流程。\n"
    "⚠️ 技能的 0-0 那句开场确认**不用回**（ECHO 已经念过了）—— 请直接在**这一轮里**把"
    "0-A / 0-B 做完，最后只回一句「前置环节完成 + 计数」；"
    "**不要在开场确认之后就结束这一轮**。")

def today_meetings(now=None) -> list:
    """今天的会议（**ECHO 侧**）：时间 / 标题 / 目录 / 纪要文件。

    为什么前置环节需要它（2026-10-13 用户实测："第一轮没搜到今天的会议和纪要"）：
    技能第 0 步的 0-B 让智能体去**笔记库**找 `02-会议记录/ECHO自动纪要/{date}_*.md`，
    而当天会议刚结束时那里还没有文件（实测当天 0 个）；纪要在 ECHO 这边 ——
    `{meetingWorkspace}/<名字>/summary.md`。**只报路径不够，得把真实位置告诉它。**
    """
    import json as _json
    import os as _os
    now = now or datetime.datetime.now()
    day = now.strftime("%Y-%m-%d")
    try:
        from app import meeting as meeting_mod
        root = meeting_mod.meetings_dir()
        names = sorted(_os.listdir(root))
    except Exception:                                        # noqa: BLE001 - 读不到就当今天没有
        return []
    out = []
    for name in names:
        if not name.startswith(day):
            continue
        d = _os.path.join(root, name)
        if not _os.path.isdir(d):
            continue
        title = ""
        try:
            with open(_os.path.join(d, "meta.json"), encoding="utf-8") as fh:
                title = str((_json.load(fh) or {}).get("title") or "")
        except Exception:                                    # noqa: BLE001
            title = ""
        summ = _os.path.join(d, "summary.md")
        has = _os.path.isfile(summ)
        out.append({"name": name,
                    "time": name.split("_")[-1].replace("-", ":")[:5],
                    "title": title, "dir": d,
                    "summary": summ if has else "", "hasSummary": has})
    return out


def meetings_block(now=None) -> str:
    """把今天的会议列成给智能体看的清单（一条一行，含**纪要文件的绝对路径**）。"""
    ms = today_meetings(now)
    if not ms:
        return "（今天 ECHO 里没有会议目录）"
    lines = []
    for m in ms:
        s = ("纪要：%s" % m["summary"]) if m["hasSummary"] else "纪要：还没生成（只有录音与转写）"
        lines.append("- %s %s ｜目录：%s ｜%s"
                     % (m["time"], m["title"] or m["name"], m["dir"], s))
    return "\n".join(lines)


#: 前置环节的探测节奏。**故意不做成设置**：用户口径是"不用太限定上线秒数，但不能卡死"。
PRE_MAX_S = 900.0     # 最长探测 15 分钟（一次当天素材扫描的正常上限远小于它）
PRE_POLL_S = 3.0      # 每 3 秒主动探一次（不是一次长阻塞等到底）
PRE_ETA_S = 60        # 开场播报里的预估时长（技能 0-0 的原话就是"大概需要一分钟"）
#: 前置环节的**完成标记**。⚠️ 判完成只认它：`wait_for_reply` 的 `done` 只是"**本轮**结束了"，
#: 而技能 0-0 要求智能体先回一句开场确认（很可能就在那一轮结束）—— 2026-10-13 实测就是这么
#: 判早了 35.5 秒（素材还在扫，用户已经开始口述）。
PRE_DONE_MARK = "前置环节完成"
PRE_NUDGE_MAX = 3     # 追问上限（本轮结束却没带完成标记时）
#: 判"**本轮真的结束了**"要连续探到几次"会话空闲"。
#: 为什么不是一次：工具调用之间 `running` 会短暂闪断（`dsh_agent.wait_for_reply` 的
#: 注释早就写明这点），一次就追问会把追问插进还在干活的轮次里。
PRE_IDLE_POLLS = 2
PRE_NUDGE_TEXT = (
    "【前置环节·继续】刚才那一轮结束了，但第 0 步还没做完（扫当天文件 + 会议纪要归档）。"
    "请继续把它做完；**全部做完后只回一句**「前置环节完成 + 计数」，不要再问问题。")

_PRE = {"phase": "", "startedAt": 0.0, "seconds": 0.0, "finished": False,
        "etaSec": PRE_ETA_S, "error": "", "nudges": 0}


def pre_state() -> dict:
    """前置环节的现状（面板读）。**纯读**，不触发任何动作。"""
    st = dict(_PRE)
    if st["phase"] == "pre" and st["startedAt"]:
        st["elapsed"] = round(time.time() - st["startedAt"], 1)
    else:
        st["elapsed"] = st["seconds"]
    return st


def build_pre_prompt(vault="", now=None) -> str:
    """拼前置环节那条指令。占位符机制与 `build_prompt` **同一套**（函数式替换，只扫模板）。"""
    now = now or datetime.datetime.now()
    values = {
        "vault": vault or "",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": "一二三四五六日"[now.weekday()],
        "chars": str(broadcast_limit_chars()),
        # 今天的会议清单（时间/标题/目录/纪要绝对路径）—— 见 today_meetings 的说明
        "meetings": meetings_block(now),
    }
    tpl = str(settings.get("dailyReviewPrePrompt", "") or "").strip() or DEFAULT_PRE_PROMPT
    text = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), tpl)
    if "{meetings}" not in tpl:
        # 自定义模板没带 `{meetings}`：**照样把今天的会议清单附在后面**。否则技能第 0-B 步
        # 又会去笔记库 `02-会议记录/ECHO自动纪要/` 找当天文件，而那里当天是空的（实测 0 个），
        # 会议纪要其实在 ECHO 侧 —— 这正是"第一轮没搜到今天的会议和纪要"的原因之一。
        text += ("\n\n【今天 ECHO 侧的会议（纪要在这里，不在笔记库）】\n"
                 + values["meetings"]
                 + "\n⚠️ 笔记库 `02-会议记录/ECHO自动纪要/` 里今天可能还没有文件 —— "
                   "那是「归档的结果」，不是「今天没开会」：以上面清单为准，"
                   "按 meeting-archive 的规则归档/再登记。")
    return text


def pre_eta_text(seconds=None) -> str:
    """预估时长说成人话。"""
    s = int(seconds or PRE_ETA_S)
    if s < 90:
        return "大概一分钟"
    if s < 150:
        return "大概一两分钟"
    return "大概 %d 分钟" % max(2, int(round(s / 60.0)))


def pre_ack_text(eta=None) -> str:
    """开场那句：**我开始了什么 + 大概多久**（技能 0-0 要求"让 ECHO 先出声"）。

    这句话由 ECHO 自己念（`preflight(on_ack=…)`），**不等智能体先回话** —— 否则用户要等到
    扫描做完才听见声音，正是"点了没反应"的那种体验。
    """
    return ("好，我先扫一下今天的素材：企微、微信、桌面和 WPS 的当天文件，"
            "还有 ECHO 的会议纪要；%s。扫完就开始录你说的内容。" % pre_eta_text(eta))


def preflight(client=None, force_new=False, on_ack=None, on_progress=None,
              should_stop=None) -> dict:
    """**前置环节（技能第 0 步）**：确保会话 → 发指令 → **主动探测**到本轮做完。

    串行（2026-10-13 用户拍板）：素材收集做完才开麦录口述 —— 技能里写的就是
    "在接收用户口述之前"先扫。开场**立刻回调 `on_ack(text)`**，免得用户不知道有没有触发。

    探测纪律：每 `PRE_POLL_S` 秒探一次、最长 `PRE_MAX_S`；`wait_for_reply` 的 `done`
    就是"本轮最终回复到了"（`app/agents/base.py`）。
      * 探到完成 → `finished=True`；
      * 到点 / 探测出错 → `finished=False` + `timed_out`/`error` **如实返回**，
        由调用方带一句说明进入口述 —— **绝不卡死、也绝不假装扫完了**。

    ⚠️ **探测里绝不许取消会话**（`cancel_on_timeout=False`）—— 2026-10-14 真机事故：
    前置环节是一条**几分钟**的长活（扫企微/微信/桌面/WPS 缓存、读当天纪要归档），
    而 `wait_for_reply` 默认在"这个小窗口里没等到**新的**助手文本"时会 `session/cancel`
    收尾。于是现场每 3 秒轮询就打断一次正在干活的智能体：
      * DSH 会话里每一轮都显示「已停止」，`turn/end` 的 reason 是 `aborted/user`；
      * 工具结果报 `glob was aborted before completion (tool caller cancellation)`；
      * 智能体永远走不到「前置环节完成」，ECHO 追问 3 次后**空等到 15 分钟**，
        用户看到的就是"每日回顾不工作了"。
    同一现场还暴露出另外三处，一并按"不能卡死、也不许打断"的口径修掉：
      * 判"本轮结束"必须**连续 `PRE_IDLE_POLLS` 次空闲**（工具间隙会闪断）；
      * 助手文本**跨窗口保留**（`wait_for_reply` 只看本窗口的新消息，追问那一轮
        完成标记可能落在一个"没有新文本"的窗口里）；
      * `should_stop()` 为真（用户按停/喊停）**当轮收尾**，不再空等到 15 分钟。
    """
    import app.db as db

    _PRE.update(phase="pre", startedAt=time.time(), seconds=0.0, finished=False,
                etaSec=PRE_ETA_S, error="", nudges=0)
    out = {"ok": False, "session_id": "", "ack": pre_ack_text(), "finished": False,
           "timed_out": False, "seconds": 0.0, "reply": "", "error": ""}

    def _fail(msg):
        out["error"] = msg
        out["seconds"] = round(time.time() - _PRE["startedAt"], 1)
        _PRE.update(phase="failed", seconds=out["seconds"], finished=False, error=msg)
        db.add_log("warn", "daily_review", "前置环节没跑起来：%s" % msg)
        return out

    if callable(on_ack):
        try:
            on_ack(out["ack"])              # **先出声**
        except Exception:                    # noqa: BLE001 - 出声失败不拖垮流程
            pass

    ok, why = ready()
    if not ok:
        return _fail(why)
    try:
        client = client or _client()
    except Exception as e:                   # noqa: BLE001
        return _fail("取智能体客户端失败：%s" % e)
    try:
        acc_ok, acc_why = ensure_access(client)
        if not acc_ok:
            db.add_log("warn", "daily_review", "前置环节：会话权限校正未成功：%s" % acc_why)
    except Exception as e:                   # noqa: BLE001
        db.add_log("warn", "daily_review", "前置环节：会话权限校正异常：%s" % e)
    try:
        sid, wid, info = ensure_session(client, force_new=force_new)
    except Exception as e:                   # noqa: BLE001
        return _fail("建立回顾会话失败：%s" % e)
    out["session_id"] = sid
    if not sid:
        return _fail("没能建立回顾会话（DSH 未返回 sessionId）")
    try:
        client.prompt(sid, build_pre_prompt(vault=vault_root()))
    except Exception as e:                   # noqa: BLE001
        return _fail("发前置指令失败：%s" % e)

    deadline = time.time() + PRE_MAX_S
    idle = 0                                 # 连续"会话空闲"的次数（见 PRE_IDLE_POLLS）
    nudges = 0
    while time.time() < deadline:
        if callable(should_stop) and should_stop():
            out["error"] = "用户结束了这次回顾"
            break
        left = min(PRE_POLL_S, max(0.5, deadline - time.time()))
        try:
            reply, done = _probe_reply(client, sid, left, poll=PRE_POLL_S)
        except Exception as e:               # noqa: BLE001 - 探测出错也不许卡住
            out["error"] = "探测前置环节失败：%s" % e
            db.add_log("warn", "daily_review", out["error"])
            break
        if reply:
            out["reply"] = reply
            if PRE_DONE_MARK in reply:       # **只认显式标记**（见 PRE_DONE_MARK 的说明）
                out["finished"] = True
                out["ok"] = True
                break
        idle = idle + 1 if done else 0
        if idle >= PRE_IDLE_POLLS:
            if nudges < PRE_NUDGE_MAX:
                # 这一轮真的结束了（连续空闲），却没带完成标记 → 追问一次让它接着做
                nudges += 1
                idle = 0
                db.add_log("info", "daily_review",
                           "前置环节：本轮结束但没带「%s」标记，追问第 %d 次"
                           % (PRE_DONE_MARK, nudges))
                try:
                    client.prompt(sid, PRE_NUDGE_TEXT)
                except Exception as e:       # noqa: BLE001 - 追问失败不致命
                    db.add_log("warn", "daily_review", "前置环节追问失败：%s" % e)
                    break
                continue
            # 追问用尽且会话已经闲着：**如实收尾**，别让用户对着"没反应"等满 15 分钟
            out["error"] = ("前置环节追问 %d 次仍未拿到「%s」标记"
                            % (PRE_NUDGE_MAX, PRE_DONE_MARK))
            break
        if callable(on_progress):
            try:
                on_progress(round(time.time() - _PRE["startedAt"], 1))
            except Exception:                # noqa: BLE001
                pass
    else:
        out["timed_out"] = True
        out["error"] = "前置环节在 %d 秒内没等到「%s」标记" % (int(PRE_MAX_S), PRE_DONE_MARK)
    out["nudges"] = nudges
    _PRE["nudges"] = nudges

    out["seconds"] = round(time.time() - _PRE["startedAt"], 1)
    _PRE.update(phase="done" if out["finished"] else "timeout", seconds=out["seconds"],
                finished=out["finished"], error=out["error"])
    db.add_log("info" if out["finished"] else "warn", "daily_review",
               "前置环节%s（%.1fs）%s" % ("完成" if out["finished"] else "未完成",
                                        out["seconds"],
                                        "" if out["finished"] else "：" + out["error"]))
    return out
