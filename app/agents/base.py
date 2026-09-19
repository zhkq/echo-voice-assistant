# -*- coding: utf-8 -*-
"""agents/base.py — 智能体适配器接口

ECHO 的业务侧（assistant / meeting / worklog）只依赖本接口的 4 个动作：
    ensure_session / prompt / wait_for_reply / cancel
其余差异（会话游标、事件格式、进程模型、鉴权方式）全部由各适配器自行消化。

设计约定：
  * available() 返回 (是否可用, 原因)。原因要能直接显示给用户，
    并尽量给出"怎么修"（例如未安装、未登录、路径不对）。
  * 不支持的能力不要抛异常，忽略即可（例如某后端不支持指定工作区）。
  * 会话 id 由后端决定；不区分会话的后端可以返回常量。
"""
from app import paths


class AgentError(Exception):
    """适配器层统一异常（原 DshError 的语义超集）。"""


class AgentAdapter:
    # ---- 元信息（面板展示用）------------------------------------------------
    name = ""              # 内部标识（配置值），如 "dsh" / "codebuddy"
    display_name = ""      # 面板显示名，如 "DSH Desktop"
    vendor = ""            # 厂商/来源，简短
    description = ""       # 一句话说明
    config_key = ""        # 对应的"启用开关"配置键；空串表示无开关（恒启用）
    #: 该智能体自己拥有的配置键（面板在它的展开区里直接编辑，不再占设置页的分组）。
    #  为什么放这儿：这些项（DSH 服务地址、CLI 路径）只有在本智能体被选中时才有意义，
    #  摊到「面板与服务」里用户根本不知道它跟谁有关（2026-09-19 用户实测反馈：
    #  "下面的 dsh 没必要吧，或者把端口挪上去"）。名单里的键在 config.DEFAULTS 里标 hidden，
    #  值经 GET /api/agents 的 settings 字段下发（与启用开关同一路）。
    settings_keys = ()
    capabilities = ()      # 能力集合，如 ("workspace", "preset", "session")
    #: 它自带 Web 界面吗（有的话面板在仪表盘「超级助理」名字后给一个打开它的图标）
    web_ui = False

    # ---- 生命周期 -----------------------------------------------------------
    def available(self, probe=False):
        """可用性探测。probe=True 时做更重的活性检查（可能联网/起进程）。

        返回 (ok: bool, reason: str)。reason 在 ok=True 时是简短说明，
        在 ok=False 时是"为什么不可用 + 怎么修"。
        """
        raise NotImplementedError

    # ---- 会话 ---------------------------------------------------------------
    def ensure_session(self, kind, name="", **kw):
        """取回（无则创建）指定用途的会话 id。kind: command / summary / ..."""
        raise NotImplementedError

    # ---- 工作区（可选能力）--------------------------------------------------
    # 背景：DSH 的侧栏分组是「显式登记制」——只有登记进 workspace.json 的
    # workspaces[<id>].sessionIds 的会话才归入该工作区，未登记的落到「未分组」。
    # 而 session/create **只认 workspaceId 或 cwd 二选一**：
    #   * 传 cwd         → 会话建在该目录，但**不登记** → 显示为「未分组」；
    #   * 传 workspaceId → 会话建在该工作区的 path，且**自动登记** → 正确归组。
    # 所以要让 ECHO 建的会话出现在「会议工作区」里，必须走 workspaceId。
    def has_workspaces(self):
        """后端是否支持工作区概念（不支持时调用方退回 cwd 方式）。"""
        return False

    def find_workspace(self, path):
        """按目录路径找工作区 id；找不到返回 ""。"""
        return ""

    def ensure_workspace(self, path, title=""):
        """取回（无则创建）指定目录的工作区，返回 (workspaceId, created)。"""
        return "", False

    def create_session(self, cwd=None, workspace_id=None):
        """新建会话。给 workspace_id 时应建在该工作区内并登记（保证侧栏归组）。"""
        raise NotImplementedError

    def resolve_target(self, workspace=None, session_id=None):
        """解析命令发送目标（工作区/指定会话）。不支持的后端应忽略参数。"""
        return session_id

    # ---- 收发 ---------------------------------------------------------------
    def prompt(self, session_id, text, mode="queue"):
        """发送一条指令。"""
        raise NotImplementedError

    def wait_for_reply(self, session_id, timeout=90, poll=0.5):
        """等待本轮最终回复，返回 (reply, done)。"""
        raise NotImplementedError

    def cancel(self, session_id):
        """取消会话当前一轮（用于解卡）。不支持则 no-op。"""
        return None

    def clear_stuck(self, session_id):
        """会话若卡住则取消之。默认实现：不处理。"""
        return False

    # ---- 便捷封装 -----------------------------------------------------------
    def ask(self, session_id, text, timeout=90, poll=0.5, mode="queue"):
        """发送并等待回复，返回 (reply, done)。"""
        self.prompt(session_id, text, mode=mode)
        return self.wait_for_reply(session_id, timeout=timeout, poll=poll)


# ECHO 工作区（工作区会话在此目录下创建，GUI 会话列表中归属清晰）
ECHO_WORKSPACE = paths.echo_root()
