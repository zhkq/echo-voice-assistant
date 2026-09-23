# -*- coding: utf-8 -*-
"""默认 DSH 工作区（侧栏分组）：建目录 → 建工作区 → 起中文名。

为什么需要这个模块
==================
DSH 侧栏的分组是**显式登记制**：一个会话只有登记进 `workspace.json` 里某个工作区，
才会出现在对应分组下，否则落到「未分组」（见 `app/agents/base.py` 的说明）。
而建会话时"登记"这件事只有一条路：**用 workspaceId 建**（传 cwd 只建目录、不登记）。

`workspace/create` 又**只收一个目录路径**，分组名由目录名派生（`title = basename`）——
所以想让分组叫「会议空间」而不是 `meetings`，只能建好之后再 `workspace/rename`。

于是"新机器装好就有两个分组"这件事是**三件一起做**，缺一件用户看到的就是
"没分组"或者"分组名是个英文目录名"：
  1. 目录真的存在（`workspace/create` 会校验目录）；
  2. `workspace/create` 拿到 workspaceId；
  3. `workspace/rename` 改成中文名。

改名只在**用户没自己改过**时才做：新建的工作区名必然是目录名，所以"当前名 == 目录名"
就是"没人动过"；用户起过名字的一律不碰（那是他的地盘）。

这个模块是"默认空间"的**单一事实源**：目录来自哪两个设置、该叫什么名字，
`app/meeting.py`（会议会话）、`app/agents/dsh_agent.py`（默认命令会话）、
`/api/dsh/workspaces/ensure`（面板与安装技能预建）都从这里取。
"""
import os

#: 两个默认分组。key = 目录设置；title_key = 分组名设置（用户可改）；default_title = 出厂名。
DEFAULT_SPACES = (
    dict(key="meetingWorkspace", title_key="meetingWorkspaceTitle",
         default_title="会议空间", what="每场会议的纪要/议题/归档会话"),
    dict(key="commandWorkspace", title_key="commandWorkspaceTitle",
         default_title="指令空间", what="默认命令与语音指令会话"),
)


def _setting(key, default=""):
    """读一个设置值（字符串、已去空白）。settings 不可用时返回 default。"""
    try:
        from app.config import settings
        return str(settings.get(key, default) or "").strip()
    except Exception:
        return default


def title_for_path(path):
    """这个目录该叫什么分组名。

    命中默认空间 → 用配置里的标题（出厂是「会议空间」/「指令空间」）；
    其它目录 → 目录名（保持原有行为，用户自己配的工作区不动）。
    """
    if not path:
        return ""
    want = os.path.normcase(os.path.normpath(path))
    for spec in DEFAULT_SPACES:
        space = _setting(spec["key"])
        if space and os.path.normcase(os.path.normpath(space)) == want:
            return _setting(spec["title_key"]) or spec["default_title"]
    return os.path.basename(path.rstrip("\\/"))


def space_specs():
    """返回两个默认空间的完整描述：`[{key,label,path,title,what}]`。

    `label` 是给人看的固定名字（日志/报告里用它指代"哪个空间"），
    `title` 是**实际要写进 DSH 的分组名**（可能被用户改过）。
    """
    out = []
    for spec in DEFAULT_SPACES:
        title = _setting(spec["title_key"]) or spec["default_title"]
        out.append(dict(key=spec["key"], label=spec["default_title"],
                        path=_setting(spec["key"]), title=title, what=spec["what"]))
    return out


def _client():
    """当前选中的智能体客户端；取不到返回 (None, 原因)。"""
    try:
        from app.dsh import get_client
        return get_client(), ""
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def ensure_spaces(client=None, make_dirs=True, log=True):
    """建立/补齐两个默认分组，返回逐项报告（**不抛异常**）。

    报告项：`{key,label,path,title,title_ok,workspaceId,action,detail}`
      * action = created / kept / skipped（没配目录）/ no-agent（智能体不可用）/ failed
      * title_ok = True 改名成功；False 目标名与 DSH 里读回的不一致；None 读不回（不判错）
    """
    if client is None:
        client, why = _client()
    else:
        why = ""
    report = []
    for spec in space_specs():
        item = dict(spec)
        item.update(workspaceId="", title_ok=None, action="", detail="")
        if not item["path"]:
            item["action"] = "skipped"
            item["detail"] = "没配目录 —— 这个空间不建（会话会落在「未分组」）"
        elif client is None:
            item["action"] = "no-agent"
            item["detail"] = "智能体不可用，稍后会自动重试：%s" % (why or "未知原因")
        else:
            try:
                if make_dirs:
                    os.makedirs(item["path"], exist_ok=True)
                wid, created = client.ensure_workspace(item["path"], title=item["title"])
                item["workspaceId"] = wid or ""
                if not wid:
                    item["action"] = "failed"
                    item["detail"] = "DSH 没返回 workspaceId"
                else:
                    item["action"] = "created" if created else "kept"
                    item["detail"] = "工作区 %s（%s）" % (wid, "新建" if created else "已存在")
                    # 读回 **DSH 里真实的**分组名：改名失败是静默的（RPC 异常被吞），
                    # 只报"我想要什么名字"等于自说自话。
                    actual = ""
                    try:
                        actual = str(client.workspace_title(wid) or "")
                    except Exception:
                        actual = ""
                    if actual:
                        item["title_ok"] = (actual == item["title"])
                        item["detail"] += "，名为「%s」" % actual
                    else:
                        item["detail"] += "（分组名读不回，不判错）"
            except Exception as e:
                item["action"] = "failed"
                item["detail"] = "%s: %s" % (type(e).__name__, e)
        report.append(item)
    if log:
        _log(report)
    return report


def _log(report):
    """把结果写进 DB 日志 —— 用户报"没分组"时这里是第一现场。"""
    try:
        from app import db
    except Exception:
        return
    for it in report:
        if it["action"] == "failed":
            db.add_log("warn", "dsh", "建立分组「%s」失败：%s" % (it["label"], it["detail"]))
        elif it["action"] == "created":
            db.add_log("info", "dsh", "已建立分组「%s」→ %s（%s）"
                       % (it["label"], it["path"], it["detail"]))
        elif it["action"] == "kept" and it["title_ok"] is False:
            db.add_log("warn", "dsh", "分组「%s」的名字不是预期值：%s"
                       % (it["label"], it["detail"]))
