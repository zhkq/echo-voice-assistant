# -*- coding: utf-8 -*-
"""model_cleanup.py — 本地模型清理（**客户端已无本地模型**）

2026-10-31 用户拍板：客户端默认档不再装任何本地 STT 模型/引擎，指令/回顾/会议转写
统一走能力后端（Qwen3-ASR）。因此客户端没有可清理的模型：

* `preview()` 返回空清单（形状不变，面板的「清理」卡照旧能渲染）；
* `execute()` 拒绝任何 id（"客户端已不再安装本地模型"）；
* `pin()` 保留签名与落库行为（历史账本兼容）。

清理/显存/模型驻留的真实位置在能力后端那台机器（`server/`），不在客户端。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 「近期」的出厂阈值（天）。设置项 `modelCleanupDays` 仍保留（历史兼容）。
DEFAULT_DAYS = 90

#: 「保留」钉子的中文说法（面板与回报文案共用）。
PIN_LABEL = "已标记「保留」"

_NOTE = "客户端不再安装本地模型（转写统一走能力后端），没有可清理项"


def _days() -> int:
    try:
        from app.config import settings
        value = int(settings.get("modelCleanupDays", DEFAULT_DAYS))
    except Exception:
        return DEFAULT_DAYS
    return max(0, value)


def preview(days: Optional[int] = None) -> Dict[str, Any]:
    """**只读**预览：客户端已无本地模型 → 空清单。"""
    d = _days() if days is None else max(0, int(days))
    return {
        "days": d,
        "items": [],
        "suggested": [],
        "suggestedMb": 0,
        "suggestedBytes": 0,
        "totalMb": 0,
        "totalBytes": 0,
        "note": _NOTE,
    }


def execute(ids, days: Optional[int] = None) -> Dict[str, Any]:
    """删掉点名的模型：客户端已无本地模型 → 一律拒绝并说明原因。"""
    if isinstance(ids, str):
        ids = [ids]
    wanted = [str(i or "").strip() for i in (ids or []) if str(i or "").strip()]
    failed: List[Dict[str, str]] = [
        {"id": mid, "reason": _NOTE} for mid in wanted
    ]
    return {
        "ok": False,
        "message": _NOTE,
        "removed": [],
        "failed": failed,
        "freedMb": 0,
        "freedBytes": 0,
    }


def pin(model_id: str, pinned: bool = True) -> Dict[str, Any]:
    """打/摘「保留」钉子（历史账本兼容；客户端已无本地模型可保护）。"""
    from app import model_usage
    mid = str(model_id or "").strip()
    if not mid:
        return {"ok": False, "message": "缺少模型 id"}
    if not model_usage.pin(mid, pinned):
        return {"ok": False, "message": "写不进库（看看数据库是不是被占着）"}
    return {"ok": True, "id": mid, "pinned": bool(pinned),
            "message": ("已保留：%s（不再列入清理建议）" % mid) if pinned
                       else ("已取消保留：%s" % mid)}
