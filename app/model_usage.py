# -*- coding: utf-8 -*-
"""model_usage.py — 本地模型「用没用过」账本（**客户端已无本地模型**）

## 2026-10-31 用户拍板

客户端默认档不再装任何本地 STT 模型/引擎，指令/回顾/会议转写统一走能力后端。
所以这里只剩**兼容层**：

* `known_ids()` / `in_use_ids()` 返回空（清理页据此不再给出任何建议）；
* `note_used()` / `note_engine_used()` / `pin()` / `forget()` 保留签名与落库行为，
  由历史引擎路径或测试调用；没有本地模型时不会有人调到它们；
* `usage_map()` / `usage_of()` 仍可读库（老账本行不影响任何决策）。

清理/统计的真实口径在能力后端那侧（`server/engines.py` 的模型清单），不在客户端。
"""
from __future__ import annotations

from typing import Dict, List, Optional

#: 账本总闸。测试包在导入时关掉（见 `tests/__init__.py`），生产路径不动它。
ENABLED = True


def _db():
    from app import db
    return db


def model_id_for_engine(engine_name: str, model_name: str = "") -> str:
    """客户端已无本地模型 → 认不出任何引擎（返回空串，调用方据此不记）。"""
    return ""


def engine_model_id(value) -> str:
    """设置值 → 模型 id：客户端已无本地模型 → 空串。"""
    return ""


def note_used(model_id: str, when: Optional[str] = None) -> bool:
    """记一次使用。返回是否写了；**任何异常都吞掉**（账本不许把主链路带崩）。"""
    if not ENABLED:
        return False
    mid = str(model_id or "").strip()
    if not mid:
        return False
    try:
        return bool(_db().record_model_use(mid, when=when))
    except Exception:
        return False


def note_engine_used(engine_name: str, model_name: str = "",
                     when: Optional[str] = None) -> bool:
    """按 (engine, model) 记一次使用；客户端已无本地引擎 → 永远不记。"""
    return note_used(model_id_for_engine(engine_name, model_name), when=when)


def usage_map() -> Dict[str, Dict]:
    """账本全量：``{model_id: {lastUsedAt, useCount, pinned, pinnedAt}}``（读失败 = 空）。"""
    try:
        rows = _db().model_usage() or []
    except Exception:
        return {}
    out = {}
    for r in rows:
        out[str(r.get("model_id"))] = {
            "lastUsedAt": str(r.get("last_used_at") or ""),
            "useCount": int(r.get("use_count") or 0),
            "pinned": bool(r.get("pinned")),
            "pinnedAt": str(r.get("pinned_at") or ""),
        }
    return out


def usage_of(model_id: str) -> Dict:
    """单个模型的使用情况（没有记录 = 空白的四个字段，**不是 None**）。"""
    blank = {"lastUsedAt": "", "useCount": 0, "pinned": False, "pinnedAt": ""}
    return usage_map().get(str(model_id or "").strip(), blank)


def pin(model_id: str, pinned: bool = True) -> bool:
    """打/摘「保留」钉子（历史兼容；客户端已无本地模型可保护）。"""
    mid = str(model_id or "").strip()
    if not mid:
        return False
    try:
        return bool(_db().set_model_pin(mid, pinned=bool(pinned)))
    except Exception:
        return False


def forget(model_id: str) -> bool:
    """删掉一个模型后清它的账本行（历史兼容）。"""
    try:
        _db().clear_model_usage(str(model_id or "").strip())
        return True
    except Exception:
        return False


def known_ids() -> List[str]:
    """出厂清单里的全部 item id：客户端已无本地模型 → 空列表。"""
    return []


def in_use_ids() -> Dict[str, List[str]]:
    """"现在谁在用"：客户端已无本地模型 → 空（清理页不会给任何建议）。"""
    return {}
