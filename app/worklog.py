# -*- coding: utf-8 -*-
"""worklog.py — 会议纪要归档（委派给用户自己的归档技能）

设计（2026-09-12 定稿）
----------------------
ECHO 在这个环节**不做任何归档决策**，只负责三件事：

  1. 把纪要拼成一份自包含的 markdown 落到本地（`{meeting}/meeting_note.md`）；
  2. 把「笔记库路径 + 会议结构化信息 + 该 md 的绝对路径 + 归档要求」送进 DSH；
  3. 把 DSH（其实是用户的归档技能）回的一句话结果原样带回面板。

写哪个目录、日志什么格式、有哪些专项与年会体系——**全部是技能的事**。
所以 ECHO 代码里不再出现任何个人笔记库路径、专项清单或单位会议体系，
第三方用户只要写好自己的归档技能，改两个设置就能用。

会话与工作目录（2026-09-15 定稿：一场会议一个会话）
--------------------------------------------------
归档复用**本场会议的纪要会话**（见 db.meeting_sessions），使同一场会议的
纪要/分段/归档消息都在同一个会话里，并在 DSH 侧栏正确归入会议工作区。
提示词里给的是 `{vault}` 与 `{md_path}` 的**绝对路径**，所以不依赖会话的工作
目录，技能照常能完成归档。
（老会议若没有登记会话，兜底会在笔记库目录新建一个会话；那种会话会落到
DSH 的「未分组」，仅作兼容。）
"""
import json
import os

import app.db as db
from app.config import settings
from app.dsh import get_client

# 归档 md 在会议目录下的固定文件名
NOTE_FILENAME = "meeting_note.md"

# 等待 DSH 完成的超时：归档是自主多步操作（读文件 → 判归属 → 多文件写入），
# 比单轮问答慢，给足时间。
# 为什么是 900 而不是 600：权限受限的旧会话第一次写入会被沙箱拦下，DSH 会
# "升级一次权限重试"，整场实测要约 640s 才落盘（2026-09-15 的 #78 就是），
# 600s 会让面板先拿到半截回复并显示成失败。新建会话已默认全盘访问，正常几秒完成。
ARCHIVE_TIMEOUT = 900

# DSH 会话权限（settings 的 permission 命名空间）。归档要写笔记库，而笔记库在
# 归档会话的工作区之外 —— 权限不足时首次写入被沙箱拒绝，只能靠 DSH 的
# "自动提权重试"补救（很慢）。这里把它固化成可自愈的运行时约定。
DSH_PERMISSION_NS = "permission"
DSH_FULL_ACCESS = "danger-full-access"
_SESSION_STORE = os.path.expanduser(r"~\.dsh\storages\session_projcache\sessions")


# ---------------------------------------------------------------- 配置

def enabled():
    """归档总开关（未启用/未配置笔记库时，面板不提供写工作日志）。

    唯一的开/关入口。历史上还有一个 `worklogMode`（skill/off）表达同一个"不归档"，
    属重复项，2026-09-19 已弃用并折叠进来（老配置 worklogMode=off 会在启动时
    把本开关置为 False，见 config.DEPRECATION_MIGRATIONS）。
    """
    return bool(settings.get("worklogEnabled", False))


def vault_root():
    """笔记库根目录；未配置返回空串。"""
    return (settings.get("worklogVaultRoot", "") or "").strip()


def ready():
    """是否具备归档条件，返回 (ok, 原因)。"""
    if not enabled():
        return False, "纪要归档未启用（设置 → 纪要归档 → 启用纪要归档）"
    vault = vault_root()
    if not vault:
        return False, "未配置笔记库根目录（设置 → 纪要归档 → 笔记库根目录）"
    if not os.path.isdir(vault):
        return False, f"笔记库根目录不存在：{vault}"
    return True, ""


# ---------------------------------------------------------------- 会话权限

def dsh_default_preset(client=None):
    """读 DSH `permission.defaultPreset`（新建会话的默认权限档位）；读不到返回空串。"""
    client = client or get_client()
    res = client.rpc("settings/describe", {})
    for ns in ((res.get("value") or {}).get("namespaces") or []):
        if ns.get("ns") == DSH_PERMISSION_NS:
            return ((ns.get("value") or {}).get("defaultPreset") or "")
    return ""


def ensure_dsh_default_access():
    """确保 DSH **新建**会话的默认权限是全盘访问，返回 (ok, 说明)。

    为什么必须做：会议归档要把纪要写进 `{vault}`，而笔记库在归档会话的工作区之外。
    DSH 默认档位 `workspace-write` 下，第一次写入会被沙箱拒绝，随后 DSH 会
    "升级一次权限重试"——实测整场要 10 分钟上下，用户看到的就是"归档失败"。
    2026-09-15 的事故正是如此（归档能用，但慢到被误判为失败）。

    改成默认全盘访问后，新建会议会话第一次就能写，几秒完成。这里每次归档前都
    复核一次（DSH 侧设置是 live 生效的），避免换机器/被改回去后静默退化。
    设置 `worklogEnsureSessionAccess=false` 时只检查不修改。
    """
    if not bool(settings.get("worklogEnsureSessionAccess", True)):
        return True, "已按设置跳过自动校正（worklogEnsureSessionAccess=false）"
    client = get_client()
    try:
        cur = dsh_default_preset(client)
    except Exception as e:
        return False, f"读取 DSH 权限设置失败：{e}"
    if cur == DSH_FULL_ACCESS:
        return True, f"DSH 新会话默认权限已是 {DSH_FULL_ACCESS}"
    try:
        client.rpc("settings/update",
                   {"ns": DSH_PERMISSION_NS, "patch": {"defaultPreset": DSH_FULL_ACCESS}})
        now = dsh_default_preset(client)
    except Exception as e:
        return False, f"修正 DSH 默认权限失败（{cur or '未知'} → {DSH_FULL_ACCESS}）：{e}"
    if now != DSH_FULL_ACCESS:
        return False, f"修正 DSH 默认权限未生效（当前 {now or '未知'}）"
    db.add_log("info", "meeting",
               f"已把 DSH 新会话默认权限从 {cur} 校正为 {DSH_FULL_ACCESS}"
               "（会议归档要写笔记库，权限不足会被沙箱拦下并拖到超时）")
    return True, f"已校正：{cur} → {DSH_FULL_ACCESS}"


def session_access(session_id):
    """本场会话的权限档位：`full` / `restricted` / `unknown`（只读，尽力而为）。

    DSH 没有"设置会话权限"的 RPC（session/create 只收 workspaceId / cwd /
    sessionId / agentPreset），权限存在会话存档里、由 defaultPreset 派生。
    所以这里只能**检查**：受限时提前告诉用户"这场会话首次归档会慢一次"，
    避免又一次"看起来失败、其实在写"。
    """
    if not session_id:
        return "unknown"
    try:
        with open(os.path.join(_SESSION_STORE, session_id + ".json"), encoding="utf-8") as f:
            rows = (json.load(f).get("record") or {}).get("rows") or {}
        val = (rows.get("permissions") or {}).get("val") or {}
        if not isinstance(val, dict) or not val:
            return "unknown"
        return "full" if val.get("sandbox") == DSH_FULL_ACCESS else "restricted"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- 材料准备

def _speakers_text(meeting_id):
    """参会人：取该会议说话人显示名；分不出人时返回「未区分」。"""
    try:
        rows = db.get_speakers(meeting_id) or []
    except Exception:
        rows = []
    names = []
    for r in rows:
        n = (r.get("name") or r.get("label") or "").strip()
        if n and n not in names:
            names.append(n)
    return "、".join(names) if names else "未区分"


def export_note(meeting, folder, full_text, content=""):
    """把完整纪要落成一份自包含 markdown，返回绝对路径。

    内容优先用拼装好的全文（摘要→纪要→议题分段→转写详情），
    只有在全文为空时才退回单份 summary 正文。
    """
    body = (full_text or content or "").strip()
    if not body:
        return ""
    path = os.path.join(folder, NOTE_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body if body.endswith("\n") else body + "\n")
    return path


def render_prompt(meeting, note_path, archive_hint="", date_str="", hour=None):
    """按模板渲染送入 DSH 的归档提示词。

    模板里的占位符全部来自设置（worklogPrompt），用户可自行改写；
    未知占位符不会抛错（保持原样），避免用户写错一个花括号就整个归档失败。
    """
    started = (meeting.get("started_at") or "").replace("T", " ")[:16]
    duration = int(round(meeting.get("duration_seconds") or 0))
    values = {
        "vault": vault_root(),
        "title": (meeting.get("title") or "").strip() or meeting.get("name", ""),
        "started_at": started or "未知",
        "date": date_str or "未知",
        "hour": "" if hour is None else str(hour),
        "duration": str(duration),
        "speakers": _speakers_text(meeting.get("id")),
        "md_path": note_path,
        "archive_hint": (archive_hint or "").strip() or "（无，按你的默认规则判断）",
        "meeting_id": meeting.get("name", ""),
    }

    class _Safe(dict):
        """缺失的键原样保留 {key}，不让用户模板失误变成异常。"""
        def __missing__(self, key):
            return "{" + key + "}"

    tpl = settings.get("worklogPrompt", "") or ""
    if not tpl.strip():
        return ""
    return tpl.format_map(_Safe(values))


# ---------------------------------------------------------------- 委派执行

def delegate_archive(meeting, note_path, archive_hint="", date_str="", hour=None):
    """把归档任务送进 DSH，等技能做完，返回 (ok, 人话结果)。

    会话选择（2026-09-15 定稿：一场会议一个会话）：
      优先复用本场会议的纪要会话（db.meeting_sessions 里登记的），这样归档消息
      也落在同一场会议的会话里，与会话分组一致；
      只有在拿不到该会话时（老会议 / 已归档）才退回"笔记库目录新建会话"。
      —— 注意：那种老方式建在笔记库目录下，会落到 DSH 的「未分组」，仅作兜底。

    不做任何结果解析：技能回什么就带什么给面板。
    """
    prompt = render_prompt(meeting, note_path, archive_hint=archive_hint,
                           date_str=date_str, hour=hour)
    if not prompt:
        return False, "归档提示词为空（设置 → 纪要归档 → 归档提示词模板）"
    ok_access, why_access = ensure_dsh_default_access()
    if not ok_access:
        # 只告警不阻断：受限会话仍能靠 DSH 的"自动提权重试"写完，只是慢
        db.add_log("warn", "meeting", f"归档权限自检未通过：{why_access}")
    client = get_client()
    try:
        # ① 复用本场会议的会话（与纪要同一会话）
        sid = ""
        try:
            row = db.get_meeting_session(meeting.get("name", ""))
            if row and row.get("session_id"):
                sid = row["session_id"]
        except Exception:
            sid = ""
        reused = bool(sid)
        if not reused:
            # ② 兜底：在笔记库目录新建（会落到未分组）
            sid = client.create_session(cwd=vault_root())
            db.add_log("warn", "meeting",
                       "本场会议没有可用会话，归档改为在笔记库目录新建会话"
                       "（该会话会落在 DSH 未分组）")
        if not sid:
            return False, "创建归档会话失败（DSH 未就绪？）"
        if session_access(sid) == "restricted":
            db.add_log("warn", "meeting",
                       f"会议会话 {sid} 权限为 {DSH_FULL_ACCESS} 以下（笔记库在会话工作区之外）："
                       "首次写入会被沙箱拦下并由 DSH 自动提权重试，可能需要几分钟；"
                       "新建会议会话已默认全盘访问，不会再出现这种情况")
        client.clear_stuck(sid)
        client.prompt(sid, prompt, mode="queue")
        reply, _done = client.wait_for_reply(sid, timeout=ARCHIVE_TIMEOUT, poll=2)
    except Exception as e:
        db.add_log("error", "meeting", f"归档委派失败: {e}")
        return False, f"归档委派失败：{e}"
    result = (reply or "").strip()
    if not result:
        return False, "归档技能未返回结果（可能超时，详见服务日志）"
    db.add_log("info", "meeting",
               f"归档委派完成（{'复用会议会话' if reused else '新建兜底会话'} {sid}）：{result[:200]}")
    return True, result
