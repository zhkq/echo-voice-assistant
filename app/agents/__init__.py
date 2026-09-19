# -*- coding: utf-8 -*-
"""agents/__init__.py — 智能体注册表

ECHO 可对接的"智能体产品"在此登记。每个适配器模块暴露：
    build() -> AgentAdapter   实例工厂
    以及类属性 name / display_name / vendor / description / config_key

对外接口：
    register(cls, factory)          登记一个适配器
    get_agent(name)                 按名字取适配器实例（带缓存）
    active_name()                   当前应使用的智能体名（读配置 agentBackend）
    active_agent()                  当前应使用的适配器实例
    list_agents(probe)              面板用：全部智能体的启用态 + 可用性
    product_options()               配置单选下拉的候选值

降级策略（默认 dsh）：
    * 选中项被关闭 → 自动尝试下一个「已启用且可用」的智能体；
    * 自动降级时 write a log，便于排查"为什么没走我选的那个"。
"""
import threading

from app.config import settings
from app.agents.base import AgentAdapter, AgentError, ECHO_WORKSPACE  # noqa: F401（对外导出）

_PENDING = []        # [(cls, factory)]
_INSTANCES = {}      # name -> adapter 实例
_LOCK = threading.Lock()
_LOADED = False      # 内置适配器是否已尝试导入（只导一次，失败也不反复重试）

DEFAULT_AGENT = "dsh"


def register(cls, factory):
    """登记适配器（模块导入期调用）。"""
    _PENDING.append((cls, factory))
    with _LOCK:
        _INSTANCES.pop(getattr(cls, "name", ""), None)
    return cls


def _registered():
    """登记表（name -> cls/factory）。首次访问时导入内置适配器。

    注意：不能只判断 `if not _PENDING`——内置适配器模块可能因为自身异常而
    注册失败，那样每次调用都会重试导入却始终为空。这里用 _LOADED 标记只导入一次，
    并在确实为空时报出明确错误，避免上层拿到"未知智能体"这种误导性结论。
    """
    global _LOADED
    if not _LOADED:
        _LOADED = True
        _autoload()
    if not _PENDING:
        raise AgentError(
            "智能体注册表为空：内置适配器未能注册（app/agents/dsh_agent.py、"
            "codebuddy.py 导入失败？）")
    return _PENDING


def _autoload():
    """导入内置适配器模块，各自在导入期调用 register()。"""
    from app.agents import dsh_agent      # noqa: F401
    from app.agents import codebuddy      # noqa: F401


def specs():
    """全部适配器类，按登记顺序。"""
    return [cls for cls, _ in _registered()]


def names():
    return [cls.name for cls in specs()]


def get_agent(name=None):
    """按名字取适配器实例（带缓存）。name 为空时返回当前应使用的智能体。"""
    if not name:
        return active_agent()
    _registered()
    with _LOCK:
        inst = _INSTANCES.get(name)
        if inst is not None:
            return inst
    factory = None
    for cls, fac in _registered():
        if cls.name == name:
            factory = fac
            break
    if factory is None:
        raise AgentError(f"未知的智能体：{name}（可选：{', '.join(names())}）")
    inst = factory()
    with _LOCK:
        _INSTANCES[name] = inst
    return inst


def reset():
    """清空实例缓存（配置变更后调用，让新配置立即生效）。"""
    with _LOCK:
        _INSTANCES.clear()


# ------------------------------------------------------------------ 启用态

def is_enabled(name):
    """该智能体是否被启用（没有 config_key 的视为恒启用，如 dsh）。"""
    for cls, _ in _registered():
        if cls.name == name:
            key = getattr(cls, "config_key", "")
            if not key:
                return True
            return bool(settings.get(key, True))
    return False


def enabled_names():
    return [n for n in names() if is_enabled(n)]


def product_options():
    """单选候选：只列已启用的产品（面板上的「启用开关」决定谁能被选）。"""
    opts = enabled_names()
    return opts or [DEFAULT_AGENT]


def active_name():
    """当前应使用的智能体名（含降级）。"""
    want = (settings.get("agentBackend", DEFAULT_AGENT) or DEFAULT_AGENT).strip()
    if want in names() and is_enabled(want):
        return want
    # 降级：按登记顺序取第一个「已启用且可用」的
    for cls in specs():
        if not is_enabled(cls.name):
            continue
        try:
            ok, _ = get_agent(cls.name).available()
        except Exception:
            ok = False
        if ok:
            _log_fallback(want, cls.name)
            return cls.name
    # 再退一步：第一个已启用的（哪怕探测失败，让调用方拿到明确报错）
    enabled = enabled_names()
    if enabled and enabled[0] != want:
        _log_fallback(want, enabled[0])
    return enabled[0] if enabled else DEFAULT_AGENT


def _log_fallback(want, got):
    if want == got:
        return
    try:
        import app.db as db
        db.add_log("warn", "assistant",
                   f"智能体「{want}」不可用或已停用，自动改用「{got}」")
    except Exception:
        pass


def active_agent():
    """当前应使用的适配器实例。"""
    return get_agent(active_name())


# ------------------------------------------------------------------ 面板数据

def agent_settings(cls):
    """某个智能体自己的配置项（面板在它的展开区里渲染）。

    为什么走这条路而不是 /api/settings：这些键在 `config.DEFAULTS` 里标了 ``hidden``
    （不占设置页分组 —— "DSH 服务地址"摊在「面板与服务」里，用户根本不知道它跟谁有关，
    2026-09-19 用户实测原话："下面的 dsh 没必要吧，或者把端口挪上去"），
    于是 `settings.all()` 不下发它们，面板就需要另一条路读到当前值。
    只回**非密钥**项：密钥永远不出接口（这个名单里也不该有密钥）。
    """
    from app.config import DEFAULTS, _effective_options
    from app.config import settings as _s
    rows = []
    for key in getattr(cls, "settings_keys", ()) or ():
        meta = DEFAULTS.get(key)
        if not meta or meta.get("secret"):
            continue
        rows.append({
            "key": key,
            "label": meta["label"],
            "description": meta["description"],
            "value_type": meta["value_type"],
            "options": list(_effective_options(key, meta)),
            "value": _s.get(key, meta["value"]),
        })
    return rows


def list_agents(probe=False):
    """面板用：每个智能体的元信息 + 启用态 + 可用性 + 它自己的配置项。"""
    active = None
    try:
        active = active_name()
    except Exception:
        pass
    out = []
    for cls in specs():
        item = {
            "name": cls.name,
            "displayName": cls.display_name,
            "vendor": getattr(cls, "vendor", ""),
            "description": getattr(cls, "description", ""),
            "configKey": getattr(cls, "config_key", ""),
            "settings": agent_settings(cls),
            "capabilities": list(getattr(cls, "capabilities", ())),
            "enabled": is_enabled(cls.name),
            "active": cls.name == active,
            "available": False,
            "reason": "",
            "probe": False,          # 该结论是否来自重探测（probe=True）
        }
        try:
            ok, reason = get_agent(cls.name).available(probe=probe)
            item["available"] = bool(ok)
            item["reason"] = reason
            item["probe"] = bool(probe)
        except Exception as e:
            item["reason"] = f"探测异常：{e}"
            item["probe"] = bool(probe)
        out.append(item)
    return out
