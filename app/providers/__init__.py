# -*- coding: utf-8 -*-
"""providers —— ASR / LLM / TTS 三类能力的统一抽象与注册表（P5 / D25）

为什么需要这一层
----------------
1.x 里"用哪个引擎"是散落的：转写走 `app/audio/stt.py` 的 `sttModel`，朗读走
`app/audio/tts.py` 的 `ttsEngine`，而"纪要"必须经 agent（DSH）——于是**不装 agent 就出不了纪要**。
P5 要把这三类能力收成**同一种形状**：一个 provider = 一段"能做这件事"的实现，
带名字、来源、就绪状态、以及**是否出网**的标注。

规划要求（REFACTOR-PLAN §10 P5 / D25）
------------------------------------
* kind 只有三种：`asr` / `llm` / `tts`；
* **本地引擎与在线服务并列**：本地的（whisper/sensevoice/sherpa、SAPI/say、edge-tts）与在线的
  （OpenAI 兼容 ASR/LLM）用同一套接口注册，用户按 kind 选一个"当前生效的"；
* **多上游派发路由属于主包**（原 `dsh-failover/`，D25）：它作为 **LLM provider 的一种实现**
  出现在这里（见 `providers/router.py`），不再是"agent 的附属"；
* **出网标注**：每个 provider 必须能回答"数据会不会离开本机、去哪"（`egress` / `egress_note`），
  面板据此提示——这是规划里反复强调的一条（用户要知道哪些内容会发给谁）。

设计要点
--------
* 注册表只存**元数据 + 构造器**，不 import 重依赖：`app/audio/stt.py` 会拉 torch/funasr，
  只能在真正调用时 import（见 `providers/local.py` 的方法内部）。
* `catalog()` 只输出**可安全展示**的信息（不含任何凭据）——面板/接口直接用。
* "当前生效的 provider"由配置项决定（`providerAsr` / `providerLlm` / `providerTts`），
  缺省回落到该 kind 的默认实现；找不到或没就绪时明确返回原因，不静默降级。
"""
from __future__ import annotations

import threading

#: 三类能力（顺序固定：面板/CLI 展示稳定）
KINDS = ("asr", "llm", "tts")

KIND_LABELS = {
    "asr": "语音转写（ASR）",
    "llm": "语言模型（LLM）",
    "tts": "语音合成（TTS）",
}

#: provider 来源分类（面板分组用）
SOURCES = ("local", "online", "agent")

#: 可重入锁：`create()` 在报错分支里要调 `specs()`（列出可选项），那会再取一次锁 ——
#: 用普通 `threading.Lock` 会**自锁死**（2026-09-19 实测：找不到 provider 的用例直接挂住）。
_lock = threading.RLock()
_registry = {}          # (kind, pid) -> {spec, factory}
_order = []             # 注册顺序
_instances = {}         # (kind, pid) -> 实例（只对无状态实现复用）


class ProviderSpec(dict):
    """provider 的元数据（故意用 dict 子类：直接可 JSON 序列化给面板）。

    必备键：``id`` / ``kind`` / ``name`` / ``source`` / ``egress``；
    ``egress=True`` 时必须给 ``egress_note`` 说明"什么内容、发给谁"。
    """

    def __init__(self, id, kind, name, source="local", egress=False,
                 egress_note="", purpose="", requires=(), details=None, default=False):
        if kind not in KINDS:
            raise ValueError("未知 provider kind: %r（只支持 %s）" % (kind, "/".join(KINDS)))
        if source not in SOURCES:
            raise ValueError("未知 source: %r（只支持 %s）" % (source, "/".join(SOURCES)))
        if egress and not egress_note:
            raise ValueError("出网的 provider 必须写明 egress_note（发什么、给谁）: %s" % id)
        super().__init__(id=id, kind=kind, name=name, source=source,
                         egress=bool(egress), egress_note=egress_note,
                         purpose=purpose, requires=list(requires),
                         details=dict(details or {}), default=bool(default))


def register(spec, factory):
    """注册一个 provider。同 ``(kind, id)`` 重复注册 = 覆盖（便于测试与后续替换实现）。"""
    key = (spec["kind"], spec["id"])
    with _lock:
        if key not in _registry:
            _order.append(key)
        _registry[key] = {"spec": spec, "factory": factory}
        _instances.pop(key, None)
    return spec


def unregister(kind, pid):
    with _lock:
        key = (kind, pid)
        _registry.pop(key, None)
        _instances.pop(key, None)
        if key in _order:
            _order.remove(key)


def specs(kind=None):
    """按注册顺序返回元数据列表（可选按 kind 过滤）。"""
    with _lock:
        keys = [k for k in _order if kind is None or k[0] == kind]
        return [dict(_registry[k]["spec"]) for k in keys]


def describe(kind, pid):
    """单个 provider 的元数据（找不到返回 None）。"""
    with _lock:
        entry = _registry.get((kind, pid))
        return dict(entry["spec"]) if entry else None


def create(kind, pid):
    """构造（或取缓存的）provider 实例。找不到时抛 KeyError，消息里列出可选项。"""
    key = (kind, pid)
    with _lock:
        entry = _registry.get(key)
        if not entry:
            known = ", ".join(p["id"] for p in specs(kind)) or "（无）"
            raise KeyError("没有这个 provider: %s/%s（可用的：%s）" % (kind, pid, known))
        if key in _instances:
            return _instances[key]
        factory = entry["factory"]
    inst = factory()
    with _lock:
        _instances[key] = inst
    return inst


def default_id(kind):
    """该 kind 的默认 provider id（优先标了 ``default=True`` 的，其次注册最早的）。"""
    items = specs(kind)
    for p in items:
        if p["default"]:
            return p["id"]
    return items[0]["id"] if items else ""


def active_id(kind):
    """当前生效的 provider id：配置项 > 默认实现。

    配置项名 = ``provider<Kind>``（`providerAsr` / `providerLlm` / `providerTts`）。
    配置为空或指向不存在的 id 时回落默认，**并把原因带上**（见 `active`）。
    """
    configured = ""
    try:
        from app.config import settings
        configured = str(settings.get("provider%s" % kind.capitalize(), "") or "").strip()
    except Exception:
        configured = ""
    if configured and describe(kind, configured):
        return configured
    return default_id(kind)


def active(kind):
    """返回 ``(id, instance)``；没有可用实现时抛 KeyError（调用方要显式处理）。"""
    return active_id(kind), create(kind, active_id(kind))


def readiness(kind, pid=None):
    """就绪状态：``True`` / ``False`` / ``None``（无法判定）。永不抛异常。"""
    pid = pid or active_id(kind)
    try:
        inst = create(kind, pid)
        fn = getattr(inst, "ready", None)
        return None if fn is None else (fn() if fn() in (True, False, None) else bool(fn()))
    except Exception:
        return None


def catalog(ready=True):
    """给面板/接口用的清单：元数据 + 是否当前生效 + 就绪状态。

    ``ready=False`` 时不做探测（只列清单，接口更快/更安全）。
    """
    active_ids = {k: active_id(k) for k in KINDS}
    out = []
    for spec in specs():
        item = dict(spec)
        item["active"] = (spec["id"] == active_ids.get(spec["kind"]))
        item["ready"] = readiness(spec["kind"], spec["id"]) if ready else None
        out.append(item)
    return {"kinds": [{"id": k, "label": KIND_LABELS[k],
                       "active": active_ids[k],
                       "default": default_id(k)} for k in KINDS],
            "providers": out}


# ---------------------------------------------------------------- 内置 provider
# 放在最后 import：`local.py` 里的重依赖（torch/funasr）都在方法内 import，
# 所以这里只是"登记"，不会拖慢启动。每个模块登记失败都不该拖垮注册表
# （面板至少要能列出"清单为空"，而不是整个服务起不来）。
from app.providers import local as _local          # noqa: E402,F401
from app.providers import router as _router        # noqa: E402,F401


def _register_builtin():
    for mod in (_local, _router):
        try:
            mod.register_builtin()
        except Exception as e:                      # pragma: no cover - 防御性
            print("[providers] 内置 provider 登记失败（%s）: %s" % (mod.__name__, e))


_register_builtin()
