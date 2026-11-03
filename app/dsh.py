# -*- coding: utf-8 -*-
"""dsh.py — 智能体客户端兼容层（实现已迁至 app/agents/*）

历史：本模块原先是 ECHO 唯一的执行底层接入点。为了支持"可切换智能体产品"，
DSH 的实现被迁到 `app/agents/dsh_agent.py` 并实现 `AgentAdapter` 接口；
本模块保留原有导出名，避免既有调用点失效：

    DshClient / DshError / get_client() / echo_workspace

**get_client() 的语义（2026-09-19 修正）**：返回**当前选中的**智能体适配器
（`agentBackend` → dsh / harness / codebuddy，含不可用时的降级）。
原来它固定返回 DSH Desktop 适配器，于是一个真实 bug：面板里选了「独立 DeepSeek Harness」，
命令/纪要/归档仍然全部发到 Desktop（用户实测反馈："我配置了独立 dsh 但是命令还是发到了
desktop"）。凡是"让某个智能体干活"的调用点都该用当前选中的那个；
**只有"管 DSH Desktop 这个进程本身"的场景**（启动/探测桌面版）才用
`app.agents.get_agent("dsh")` 指名道姓。
"""
from app.agents.base import echo_workspace                     # noqa: F401
from app.agents.dsh_agent import (                              # noqa: F401
    DshAgent,
    DshError,
    build,
    _load_browser_secret,
    _make_cookie,
    DEFAULT_BASE_URL,
    CREDENTIALS_PATH,
)

# 兼容旧名：DshClient 现在就是 DshAgent
DshClient = DshAgent


def get_client():
    """取**唯一的 DSH** 适配器（注册表缓存；桌面版/无头由它自己按谁在跑决定）。"""
    from app.agents import active_agent
    return active_agent()


_edition = None


def get_desktop_client():
    """指名要 **DSH 桌面版接入**（进程探测/跳转用；不随配置变）。

    2026-11-03 一套 DSH：注册表里只有 `HarnessAgent`（名字 `dsh`，含桌面版接入），
    本函数返回父类 `DshAgent` 的单例实例，专门回答"桌面版 GUI 在不在"这类问题。
    """
    global _edition
    if _edition is None:
        _edition = DshAgent()
    return _edition
