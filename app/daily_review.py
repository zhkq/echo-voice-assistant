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
import json
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


#: 当天工作日志相对笔记库的目录（技能契约：整理结果写在 `<vault>/01-工作日志/<date>.md`）。
#: **只在这一处定义** —— `history_detail()` 报给用户的路径与 `submit()` 的"登记核实"
#: 必须看同一个文件，各写一份迟早漂成两个地方。
VAULT_NOTE_DIR = "01-工作日志"


def vault_note_path(date: str) -> str:
    """某一天的工作日志路径；没配笔记库（或没给日期）时返回空串。"""
    vault = vault_root()
    if not vault or not date:
        return ""
    return os.path.join(vault, VAULT_NOTE_DIR, date + ".md")


def note_stamp(path: str):
    """工作日志的"有没有被写过"指纹 `(mtime_ns, size)`；文件不在返回 `None`。

    为什么需要它（2026-10-18 复查"回顾功能都正常不"）：真正把整理结果**登记**进笔记库的
    是 DSH 侧的 `daily-review` 技能，ECHO 看不到那个动作，只能看**结果文件**。
    以前这里不看结果：技能没装（或没写）时，`submit()` 照样回 `ok=true`、手机照样显示
    "整理完成" —— 用户以为登记了，日志里其实一个字都没多（**200 假成功**）。
    现在拿这份指纹前后比一次，把"没登记上"如实带回去（见 `submit()`）。
    """
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    # `st_mtime_ns` 是 3.3+ 就有的；拿不到就退回秒级（只要两次比较用同一套就没问题）
    return (int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), int(st.st_size))


def _row_meta(row) -> dict:
    """把 `commands.meta`（JSON 文本）读成 dict；坏了当空表。"""
    raw = (row or {}).get("meta")
    if not raw:
        return {}
    try:
        got = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


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

    ok, why = ready()
    vault = vault_root()

    try:
        # 没配好也要给用户一句人话（车里看不到面板），并且**照样落库**：
        # 那段口述是他真说过的，历史里留一条"没成 + 原因"比悄悄丢掉强
        # （`_persist` 的承诺就是"失败也留痕"）。
        if not ok:
            out["error"] = why
            out["spoken"] = "回顾还没配好，你回面板看一眼设置。"
            out["source"] = "fallback"
            db.add_log("warn", "daily_review", f"回顾不可用：{why}")
            return out

        # 「登记核实」的**前**指纹：这一轮说完之后工作日志有没有被写过（见 note_stamp 的说明）。
        # 取在发指令之前 —— 前置环节/别的进程刚刚写过的那一下不该算到这一轮头上。
        day = time.strftime("%Y-%m-%d", time.localtime())
        note_path = vault_note_path(day)
        note_before = note_stamp(note_path)

        # 取客户端也放进**主 try** 里：它的失败同样要走结尾的 `finally: _persist`。
        # 以前它是 try 之外的一个早退分支，于是"DSH 没装/没起来"这种最常见的失败
        # 在历史里**一条都看不到** —— 用户说"昨天那次没记上"时无从判断。
        try:
            client = client or _client()
        except Exception as e:                                   # noqa: BLE001
            raise RuntimeError(f"取智能体客户端失败：{e}") from e

        # 权限校正失败**不致命**（只是可能写不进笔记库），单独吞掉并留日志
        try:
            acc_ok, acc_why = ensure_access(client)
            if not acc_ok:
                db.add_log("warn", "daily_review", f"会话权限校正未成功：{acc_why}")
        except Exception as e:                                   # noqa: BLE001
            db.add_log("warn", "daily_review", f"会话权限校正异常：{e}")

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

        # ---- 登记核实：真的写进笔记库了吗 ----
        # 整理结果由 DSH 技能写进 `<vault>/01-工作日志/<date>.md`，ECHO 只能看结果文件。
        # 以前这一轮无论写没写都回 ok=true、手机显示"整理完成" —— 技能没装时用户
        # 以为登记了，其实日志一个字都没多（**200 假成功**，比报错更坏）。
        # 现在：检测到文件出现/变大/改时间 → registered=true；
        # 否则 registered=false + 日志 + **在播报里如实补一句**（车里看不到面板）。
        out["vaultNote"] = note_path
        note_after = note_stamp(note_path)
        out["registered"] = bool(note_path and note_after is not None and note_after != note_before)
        if not out["registered"]:
            db.add_log("warn", "daily_review",
                       "这一轮没检测到工作日志更新（%s）：技能可能没装，或这次没有可登记的内容"
                       % (note_path or "（未配置笔记库）"))
            out["spoken"] = (out["spoken"] + "（不过工作日志没更新，可能没装回顾技能）").strip()
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
    meta = {"review": True, "date": date, "kind": today_kind(),
            "workspace": str(out.get("workspace") or ""),
            "spokenSource": str(out.get("source") or "")}
    # 只在这一轮真的比对过时才写 `registered`（失败轮没有"登记"可言）：
    # 历史详情据此如实说"这一轮没写进工作日志"，而不是让用户以为都登记了。
    if "registered" in out:
        meta["registered"] = bool(out.get("registered"))
    row_id = db.add_command(
        transcript,
        source=REVIEW_SOURCE,
        status="done" if ok else "failed",
        session_id=str(out.get("session_id") or ""),
        meta=meta)
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
        # 摘要取**当天最新一条有内容的播报**：行是按 `ts DESC` 来的，所以从最新往回
        # 找到的第一条非空 `brief` 就是它 —— 一旦填上就不再被后面（更早）的覆盖。
        #
        # 从前这里只认 `turns == 1`（当天最新那一轮）：最新一轮的 `brief` 为空时
        # （那一轮失败、或技能没按【播报】契约输出），**那天更早的播报也一起被吃掉**，
        # 列表看起来像"这天什么都没说"。空 `brief` 不该吃掉那天的播报。
        # 一整天都没有播报时**如实留空**（面板据此说"这天没有播报"）—— 不编一句。
        # ⚠️ 这里**不**退回 `text`：`brief` 是"念给用户听的那句"，列表里混入口述原文
        # 会让"摘要"这一列时而是播报、时而是原文，读起来像两种东西。
        if not item["brief"] and str(d.get("brief") or "").strip():
            item["brief"] = str(d["brief"]).strip()
        if not item["sessionId"]:
            item["sessionId"] = str(d.get("session_id") or "")
    items = [days[d] for d in order][: max(1, int(limit or 60))]
    return {"items": items, "total": len(order), "today": time.strftime("%Y-%m-%d")}


def history_detail(date: str) -> dict:
    """某天的回顾详情：每一轮的原文 + 播报 + 状态，外加当天工作日志的路径与是否在。

    `vaultNote` 是**最终成果所在**（DSH 技能把整理好的条目写进 Obsidian 工作日志）——
    面板给一个"打开它"的按钮，用户就能从"我说了什么"跳到"记成了什么"。

    ⚠️ `vaultNoteExists` 只说明**那天那个文件在**，不证明**这一轮**被写进去了
    （那天的日志可能只是别的来源建的）。所以每轮另有 `registered`（见 [note_stamp]），
    界面要按它说话 —— 把"文件在"说成"这一轮已写入"就是一次假成功。
    """
    date = str(date or "").strip()
    # 严格到**日历**：`2026-13-99` 这种原来也能过（只查了长短与两个连字符），
    # 于是它被当成一个"那天没有回顾"的正常日期返回 —— 用户以为那天真没记录。
    try:
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            raise ValueError(date)
        datetime.datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return {"ok": False, "error": "日期格式要是 YYYY-MM-DD", "date": date}
    rows = _rows_for_date(date)
    turns = []
    for r in rows:
        meta = _row_meta(r)
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
            # None = 老记录（那时还没有这条核实），界面**不要**据此说任何话
            "registered": (bool(meta["registered"]) if "registered" in meta else None),
        })
    vault = vault_root()
    note = vault_note_path(date)
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
    # `lastAt` 与 `lastBrief` 是**两个不同的问题**，取值口径也不同：
    #
    #   * `lastAt` 问的是"最近一次回顾**发生在什么时候**" —— 它必须是最新那一轮的
    #     `ts`，**不该**因为那一轮 brief 为空就往前挪。挪了会让仪表盘说"最后一次回顾是
    #     昨天"，而用户今天明明刚做过一轮（那轮失败了，但确实做过）。
    #   * `lastBrief` 问的是"最近一次**播报说了什么**" —— 播报是念给用户听的那句。
    #     旧实现只取最新一行的 brief ✗：最新一轮失败、或技能没按【播报】契约输出时，
    #     brief 是空串，于是仪表盘那张"最近一次播报"卡片**整块变空**，
    #     而更早那轮**明明播过** —— 用户看着像 ECHO 从没播报过。
    #
    # 所以这里从最新往回找**第一条非空 `brief`**（与 `history_list()` 同口径）。
    # 全部为空就**如实留空**（面板据此说"还没有播报过"）—— 不拿 `text`（口述原文）顶替：
    # "最近一次播报"里混进原文，这一格就时而是播报、时而是原文，读起来像两种东西。
    # SQL 里把空白的判定交给 `TRIM`（空格/Tab/CR/LF 与 Python `.strip()` 对齐），
    # 拿到的那行仍在 Python 里再 `strip()` 一次，落进接口的是干净的一句。
    last_brief_row = db._query_one(                              # noqa: SLF001
        "SELECT brief FROM commands WHERE source=? AND TRIM(COALESCE(brief, ''), "
        "' ' || char(9) || char(10) || char(13)) <> '' "
        "ORDER BY ts DESC, id DESC LIMIT 1", (REVIEW_SOURCE,)) or {}
    return {
        "today": today,
        "todayTurns": int(t.get("n") or 0),
        "todayFailed": int(t.get("f") or 0),
        "lastAt": str(last_d.get("ts") or ""),
        "lastBrief": str(last_brief_row.get("brief") or "").strip(),
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
    client_error = ""
    try:
        client = get_client()
        agent = getattr(client, "name", "") or ""
        row = None
        import app.db as db
        row = db.get_session(today_kind(), agent=agent)
        session_id = (row or {}).get("session_id") or ""
    except Exception as e:
        # ⚠️ 这里原来只把原因写进 `why`，却仍然回 `ready=ok`（只按设置判）——
        # 于是面板/手机拿到 `ready=true` + 一句"取智能体客户端失败"，而界面只在
        # `!ready` 时才显示 `reason`，那句话就被吞掉了；用户看到的是"今天还没回顾"，
        # 点下去才失败（2026-10-18 复查发现的"假就绪"）。
        # 智能体客户端拿不到 = 这次回顾根本提交不了，**必须**算未就绪。
        client_error = f"取智能体客户端失败：{e}"
        if not why:
            why = client_error
    try:
        from app import paths
        workspace = paths.review_workspace_root()
    except Exception:
        pass
    return {
        "enabled": enabled(),
        "ready": bool(ok and not client_error),
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
    "① 扫当天文件（企微 / 联通私有化企微 / 微信 / 桌面临时 / WPS 缓存，"
    "`scan_cache.py --days 1 --platforms wecom,wechat,desktop,wps,unicom`），按归属登记；\n"
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

#: 前置环节的共享状态。
#:
#: 2026-10-25 起它不只服务 PC 语音回顾：手机 App 通过
#: `POST /api/daily-review/pre` 触发同一个准备任务（`start_pre()`），再用
#: `GET /api/daily-review/pre` 轮询进度。所以：
#:   * **一天只跑一份**：PC/手机谁先到谁拥有，另一个加入等待（`preflight` 里判断）；
#:   * 所有读写都在 `_PRE_LOCK` 里，对外只给**完整快照**（不许看到半初始化状态）；
#:   * `gen` 是一次运行的代号：停止/重开时旧线程晚到的更新会被丢掉。
_PRE_LOCK = threading.RLock()
_PRE_START_LOCK = threading.Lock()       # 串行化 start_pre() 的"检查 + 起线程"
_PRE = {
    "phase": "idle", "day": "", "startedAt": 0.0, "seconds": 0.0, "finished": False,
    "etaSec": PRE_ETA_S, "error": "", "nudges": 0,
    # 手机端要看的"准备已经开始 + 本次回顾会梳理什么"
    "ack": "", "topics": [], "reply": "", "sessionId": "",
    # 内部字段（不对外）
    "gen": 0, "owner": "", "doneEvent": None, "stopEvent": None, "thread": None,
}

#: `pre_state()` 对外的字段（内部字段不外泄）。
_PRE_PUBLIC = ("phase", "day", "startedAt", "seconds", "finished", "etaSec",
               "error", "nudges", "ack", "topics", "reply", "sessionId")


def _pre_today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _pre_update(gen, **kw) -> None:
    """只允许**当前那一次运行**改状态（旧线程晚到的更新丢掉）。"""
    with _PRE_LOCK:
        if int(_PRE.get("gen") or 0) != int(gen or 0):
            return
        _PRE.update(kw)


def _pre_done(gen) -> None:
    """把 doneEvent 置位（允许重复调用）。"""
    with _PRE_LOCK:
        if int(_PRE.get("gen") or 0) != int(gen or 0):
            return
        ev = _PRE.get("doneEvent")
    if ev is not None:
        ev.set()


def pre_state() -> dict:
    """前置环节的现状（面板 / 手机轮询）。**纯读**，不触发任何动作。"""
    with _PRE_LOCK:
        st = {k: _PRE.get(k) for k in _PRE_PUBLIC}
        topics = st.get("topics")
        st["topics"] = list(topics) if isinstance(topics, (list, tuple)) else []
        if st["phase"] == "pre" and st["startedAt"]:
            st["elapsed"] = round(time.time() - float(st["startedAt"] or 0), 1)
        else:
            st["elapsed"] = st["seconds"]
    return st


def _pre_result() -> dict:
    """给 `preflight()` 的"加入已有准备"分支用的结果（与原来同形）。"""
    st = pre_state()
    return {"ok": bool(st["finished"]), "session_id": st.get("sessionId") or "",
            "ack": st.get("ack") or "", "finished": bool(st["finished"]),
            "timed_out": st["phase"] == "timeout", "seconds": st["seconds"],
            "reply": st.get("reply") or "", "error": st.get("error") or "",
            "nudges": st.get("nudges") or 0,
            "stopped": st["phase"] == "stopped"}


def _pre_begin(ack_text="", topics=None, session_id="", owner="", eta=None):
    """开一次新的准备（调用方负责先确认没有正在跑的）。返回 `(gen, done, stop)`。"""
    with _PRE_LOCK:
        gen = int(_PRE.get("gen") or 0) + 1
        done = threading.Event()
        stop = threading.Event()
        _PRE.update({
            "gen": gen, "phase": "pre", "day": _pre_today(),
            "startedAt": time.time(), "seconds": 0.0, "finished": False,
            "etaSec": int(eta or PRE_ETA_S), "error": "", "nudges": 0,
            "ack": ack_text or pre_ack_text(), "topics": list(topics or []),
            "reply": "", "sessionId": session_id or "", "owner": owner or "",
            "doneEvent": done, "stopEvent": stop, "thread": None,
        })
        return gen, done, stop


def _pre_finish(gen, out) -> None:
    """一次运行收尾：写终态 + 置 doneEvent。**幂等**（重复调用不覆盖）。"""
    with _PRE_LOCK:
        if int(_PRE.get("gen") or 0) != int(gen or 0):
            return
        if _PRE.get("doneEvent") is not None and _PRE["doneEvent"].is_set():
            return
        phase = str(out.get("phase") or "")
        if not phase:
            if out.get("finished"):
                phase = "done"
            elif out.get("stopped"):
                phase = "stopped"
            elif out.get("timed_out"):
                phase = "timeout"
            else:
                phase = "timeout"
        started = float(_PRE.get("startedAt") or 0)
        seconds = out.get("seconds")
        if seconds is None:
            seconds = round(time.time() - started, 1) if started else 0.0
        _PRE.update({
            "phase": phase, "finished": bool(out.get("finished")),
            "seconds": round(float(seconds or 0.0), 1),
            "error": str(out.get("error") or ""),
            "nudges": int(out.get("nudges") or 0),
            "reply": str(out.get("reply") or "")[:300],
            "thread": None,
        })
        ev = _PRE.get("doneEvent")
    if ev is not None:
        ev.set()


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


def pre_phone_ack_text(eta=None) -> str:
    """**手机版**准备开场（App 准备卡上显示的那句）。

    与 PC 的 `pre_ack_text()` 分开：PC 那句是"扫完就开始录你说的内容"（它会开麦），
    手机这句是"扫完我就提示你"（手机不会自动开麦）。**不许改 PC 的文案**。
    """
    return ("好，回顾准备开始了。我先扫今天的素材：企微、微信、桌面和 WPS 的当天文件，"
            "还有今天的会议纪要；%s。扫完我就提示你。" % pre_eta_text(eta))


def pre_topics(now=None) -> list:
    """「本次回顾会梳理」——手机准备卡上的主要内容提示（最多 4 条，每条 ≤60 字）。

    只报**类别 + 今天的会议标题**，不报文件内容；具体计数由前置环节的回复给
    （`pre.reply`）。在 `start_pre()` 时算一次并缓存 —— 不要每次 GET 都扫盘。
    """
    now = now or datetime.datetime.now()
    meetings = today_meetings(now)
    if meetings:
        shown = []
        for m in meetings[:3]:
            when = str(m.get("time") or "").strip()
            title = str(m.get("title") or m.get("name") or "").strip()
            flag = "（纪要已生成）" if m.get("hasSummary") else ""
            shown.append(("%s %s%s" % (when, title, flag)).strip())
        if len(meetings) > 3:
            # 超过 3 场：只列前 2 场 + "等"（保证整条 ≤60 字，别让"等"被截掉）
            shown = shown[:2]
            line = "今天的会议（%d 场）：%s 等" % (len(meetings), " · ".join(shown))
        else:
            line = "今天的会议（%d 场）：%s" % (len(meetings), " · ".join(shown))
    else:
        line = "今天的会议：ECHO 里没有会议记录"
    out = [
        line,
        "今天收到或改过的文件：企微 · 微信 · 桌面 · WPS 缓存",
        "笔记库里今天的工作日志与待办",
        "昨天没说完的、明天要做的",
    ]
    return [t if len(t) <= 60 else t[:59] + "…" for t in out]


def preflight(client=None, force_new=False, on_ack=None, on_progress=None,
              should_stop=None, ack_text=None, _owned=False) -> dict:
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

    # ---- 单例：PC 语音回顾与手机 App 共用同一个准备任务（2026-10-25）----
    # 手机先通过 `/api/daily-review/pre` 起了一个后台准备线程；PC 的 `_review_worker`
    # 仍直接调本函数 —— 这里**加入等待**，不再跑第二份（否则会重复扫盘、互相覆盖状态）。
    # `_owned=True` 是那后台线程自己进来：状态已由 `start_pre()` 先写好，别再 begin。
    join_event = None
    join_ack = ""
    if not _owned:
        with _PRE_LOCK:
            if _PRE.get("phase") == "pre" and _PRE.get("doneEvent") is not None:
                join_event = _PRE["doneEvent"]
                join_ack = str(_PRE.get("ack") or "")
        if join_event is not None:
            if callable(on_ack) and join_ack:
                try:
                    on_ack(join_ack)         # PC 那侧还是要"先出声"
                except Exception:            # noqa: BLE001
                    pass
            deadline = time.time() + PRE_MAX_S + 30
            while not join_event.wait(0.5):
                if callable(should_stop) and should_stop():
                    break
                if time.time() > deadline:
                    break
            return _pre_result()

    if _owned:
        with _PRE_LOCK:
            gen = int(_PRE.get("gen") or 0)
            stop_event = _PRE.get("stopEvent") or threading.Event()
            out_ack = str(_PRE.get("ack") or "") or (ack_text or pre_ack_text())
            alive = str(_PRE.get("phase") or "") == "pre"
        if stop_event.is_set() or not alive:
            # 线程还没开跑就被手机"先开始说"了：别再发 prompt
            return _pre_result()
    else:
        gen, _done_event, stop_event = _pre_begin(
            ack_text=ack_text or pre_ack_text(), topics=pre_topics(),
            owner="pc" if callable(on_ack) else "sync")
        out_ack = ack_text or pre_ack_text()

    started_at = time.time()
    with _PRE_LOCK:
        if int(_PRE.get("gen") or 0) == int(gen or 0) and _PRE.get("startedAt"):
            started_at = float(_PRE["startedAt"])

    out = {"ok": False, "session_id": "", "ack": out_ack, "finished": False,
           "timed_out": False, "stopped": False, "seconds": 0.0, "reply": "", "error": ""}

    def _fail(msg):
        out["error"] = msg
        out["seconds"] = round(time.time() - started_at, 1)
        out["phase"] = "failed"
        _pre_finish(gen, out)
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
        # 两种"停"：PC 喊停（should_stop）/ 手机点「先开始说」（stop_event）。
        if (callable(should_stop) and should_stop()) or stop_event.is_set():
            out["stopped"] = True
            out["error"] = ("用户跳过了准备" if stop_event.is_set()
                            else "用户结束了这次回顾")
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
                on_progress(round(time.time() - started_at, 1))
            except Exception:                # noqa: BLE001
                pass
    else:
        out["timed_out"] = True
        out["error"] = "前置环节在 %d 秒内没等到「%s」标记" % (int(PRE_MAX_S), PRE_DONE_MARK)
    out["nudges"] = nudges
    _pre_update(gen, nudges=nudges)

    out["seconds"] = round(time.time() - started_at, 1)
    _pre_finish(gen, out)
    db.add_log("info" if out["finished"] else "warn", "daily_review",
               "前置环节%s（%.1fs）%s" % ("完成" if out["finished"] else "未完成",
                                        out["seconds"],
                                        "" if out["finished"] else "：" + out["error"]))
    return out


# ---------------------------------------------------------------- 手机端：准备任务（2026-10-25）

def _pre_error(msg) -> dict:
    """`start_pre()` 的失败回包：HTTP 200 + `ok=false` + 人话（与 `/submit` 同口径）。"""
    return {"ok": False, "started": False, "already": False, "done": False,
            "sessionId": "", "etaSec": PRE_ETA_S, "ack": "", "topics": [],
            "pre": pre_state(), "error": str(msg or "")}


def _pre_ok(**kw) -> dict:
    base = {"ok": True, "started": False, "already": False, "done": False,
            "sessionId": "", "etaSec": PRE_ETA_S, "ack": "", "topics": [],
            "pre": pre_state(), "error": ""}
    base.update(kw)
    return base


def _pre_worker(gen, client, force_new, stop_event) -> None:
    """后台跑一次准备（手机 `/pre` 触发的）。"""
    try:
        preflight(client=client, force_new=force_new,
                  should_stop=stop_event.is_set, _owned=True)
    except Exception as e:                       # noqa: BLE001 - 后台线程不许把状态挂在"pre"
        with _PRE_LOCK:
            started = float(_PRE.get("startedAt") or 0)
            out = {"phase": "failed", "finished": False,
                   "error": "准备任务异常：%s: %s" % (type(e).__name__, e),
                   "seconds": round(time.time() - started, 1) if started else 0.0}
            _pre_finish(gen, out)


def start_pre(force_new=False) -> dict:
    """**手机开始回顾后的准备任务**：把技能第 0 步发给配置的智能体。

    与 PC 的 `preflight()` 共用同一份状态（一天只跑一份）：
      * 已经在跑 → `already=true`（不重复发 prompt）；
      * 今天已完成 → `done=true`（不重新扫）；
      * 否则建/取会话、写状态、起后台 daemon 线程，**立刻返回**（响应里带
        `ack` / `topics` / `etaSec` 给 App 做"准备已开始 + 本次回顾会梳理"提示）。

    **绝不**开麦克风、不调 `assistant.start_review()`、不写回顾轮次。
    """
    with _PRE_START_LOCK:
        today = _pre_today()
        with _PRE_LOCK:
            phase = str(_PRE.get("phase") or "")
            same_day = str(_PRE.get("day") or "") == today
            if same_day and phase == "pre":
                return _pre_ok(already=True,
                               sessionId=_PRE.get("sessionId") or "",
                               etaSec=_PRE.get("etaSec") or PRE_ETA_S,
                               ack=_PRE.get("ack") or pre_phone_ack_text(),
                               topics=list(_PRE.get("topics") or []))
            if same_day and phase == "done":
                return _pre_ok(done=True,
                               sessionId=_PRE.get("sessionId") or "",
                               etaSec=_PRE.get("etaSec") or PRE_ETA_S,
                               ack=_PRE.get("ack") or "",
                               topics=list(_PRE.get("topics") or []))

        ok, why = ready()
        if not ok:
            return _pre_error(why)
        # 电脑正忙（在跑别的指令 / 会议录音）：别添乱。PC 回顾自己不算忙
        # （它就是准备的主人；真在跑的话上面已经 `already` 了）。
        try:
            from app import assistant as _assistant
            owner = str((_assistant._busy_owner or {}).get("name") or "")
        except Exception:                        # noqa: BLE001
            owner = ""
        if owner and owner != "review":
            return _pre_error("电脑正忙（%s），准备没开始" % owner)
        try:
            client = _client()
        except Exception as e:                   # noqa: BLE001
            return _pre_error("取智能体客户端失败：%s" % e)
        try:
            acc_ok, acc_why = ensure_access(client)
            if not acc_ok:
                import app.db as db
                db.add_log("warn", "daily_review",
                           "前置环节：会话权限校正未成功：%s" % acc_why)
        except Exception as e:                   # noqa: BLE001
            import app.db as db
            db.add_log("warn", "daily_review", "前置环节：会话权限校正异常：%s" % e)
        try:
            sid, _wid, _info = ensure_session(client, force_new=force_new)
        except Exception as e:                   # noqa: BLE001
            return _pre_error("建立回顾会话失败：%s" % e)
        if not sid:
            return _pre_error("没能建立回顾会话（DSH 未返回 sessionId）")

        topics = pre_topics()
        ack = pre_phone_ack_text()
        gen, _done, stop = _pre_begin(ack_text=ack, topics=topics,
                                      session_id=sid, owner="phone")
        thread = threading.Thread(target=_pre_worker,
                                  args=(gen, client, force_new, stop),
                                  daemon=True, name="daily-review-pre")
        with _PRE_LOCK:
            if int(_PRE.get("gen") or 0) == gen:
                _PRE["thread"] = thread
        thread.start()
        return _pre_ok(started=True, sessionId=sid, ack=ack, topics=topics)


def stop_pre() -> bool:
    """停掉正在跑的准备（手机点「先开始说」），**不结束回顾会话**。

    返回是否真的停了一个。停止最多等 ~4.5 秒（一次探测的窗口）；等不到就
    换代强制收成 `stopped`，让旧线程晚到的更新失效 —— **绝不卡住请求**。
    """
    with _PRE_LOCK:
        if str(_PRE.get("phase") or "") != "pre":
            return False
        gen = int(_PRE.get("gen") or 0)
        stop_event = _PRE.get("stopEvent")
        done_event = _PRE.get("doneEvent")
        started = float(_PRE.get("startedAt") or 0)
    if stop_event is not None:
        stop_event.set()
    if done_event is not None:
        done_event.wait(4.5)
    ev = None
    with _PRE_LOCK:
        if int(_PRE.get("gen") or 0) == gen and str(_PRE.get("phase") or "") == "pre":
            _PRE["gen"] = gen + 1                # 换代：旧线程晚到更新全部作废
            _PRE.update({
                "phase": "stopped", "finished": False,
                "seconds": round(time.time() - started, 1) if started else 0.0,
                "error": "用户跳过了准备", "thread": None,
            })
            ev = _PRE.get("doneEvent")
    if ev is not None:
        ev.set()
    return True
