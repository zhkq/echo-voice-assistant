# -*- coding: utf-8 -*-
"""model_usage.py — 本地模型的「用没用过」账本 + 「在用」判据（清理功能的地基）。

为什么要单独一个模块（2026-09-26）
==================================

用户要求"近期没再使用的模型就清掉"，但库里 `last_used_at` **只存在于**
`api_keys` / `dsh_sessions` / `meeting_sessions`，模型一个都没有 —— 于是当时的判据
只能退化成"看文件修改时间"，那不是判据，是猜：拷进来一次 mtime 就是新的，
系统迁移/杀毒软件扫一遍也可能把它改掉。清理要动手删 GB 级的东西，**不许靠猜**。

所以这里做两件事：

1. **记**：`note_used()` —— 模型真的被加载/调用时写一行（次数 +1、上次使用时间更新）。
   落点 `data/echo.db` 的 `model_usage` 表（schema v7，见 `app/db.py`），**不出网**。
   记录粒度 = `/api/models` 的 item id（`whisper-small` / `qwen3asr` / `sensevoice` /
   `sherpa` / `pyannote` / `kws`），与面板上那一行一一对应。
2. **说**：`in_use_ids()` —— "现在谁在用"。判据只有两处来源：**当前配置选中的引擎**
   与**本进程真的加载了的引擎**。清理建议必须拿它做保护（`app/model_cleanup.py`）。

纪律
----

* **绝不影响主链路**：所有写入都吞异常（账本写不进去也不该让一次转写失败）；
* **只在真的用时写**：列清单、探就绪、量占用都**不算**使用 —— 否则"刚下载没用的模型"
  会立刻被记成"刚用过"，清理建议就永远空转；
* `ENABLED` 是给测试的闸：`tests/__init__.py` 会把它关掉（用例会往"碰巧配着的那个库"
  里写真实使用记录，那是污染用户数据）。要验证账本的用例自己再打开它。
"""
from __future__ import annotations

from typing import Dict, List, Optional

#: 账本总闸。测试包在导入时关掉（见模块文档最后一条），生产路径不动它。
ENABLED = True

#: 设置里的引擎值 → `/api/models` 的 item id（三个非 whisper 引擎是 1:1）。
ENGINE_MODEL_IDS = {"sensevoice": "sensevoice", "sherpa": "sherpa", "qwen3asr": "qwen3asr"}

#: 与 `app/modelinfo.py` 的 whisper 档位（CATALOG 里逐档生成）同一份口径。
WHISPER_TIERS = ("tiny", "base", "small", "medium", "large", "large-v3")


def model_id_for_engine(engine_name: str, model_name: str = "") -> str:
    """引擎名 + 模型名 → item id（认不出来返回空串，调用方据此不记）。

    两个入口都要它：`stt.transcribe_ex()` 拿到的是 (engine, model) 对，
    而设置里存的是**单个值**（`sttModel`），由 `engine_model_id()` 折算 —— 两个函数
    共用同一张表，所以不会出现"设置说 whisper-small、账本记成 whisper"。
    """
    eng = str(engine_name or "").strip().lower()
    if eng in ENGINE_MODEL_IDS:
        return ENGINE_MODEL_IDS[eng]
    name = str(model_name or "").strip()
    if name == "large":                      # stt.MODEL_ALIASES 的同一条折算
        name = "large-v3"
    if name in WHISPER_TIERS:
        return "whisper-" + name
    return ""


def engine_model_id(value) -> str:
    """设置值（`sttModel` / `meetingSttModel`）→ item id。"""
    v = str(value or "").strip().lower()
    if v in ENGINE_MODEL_IDS:
        return ENGINE_MODEL_IDS[v]
    if v.startswith("qwen/") or v in ("0.6b", "1.7b"):
        return "qwen3asr"
    if v == "large":                         # stt.MODEL_ALIASES 的同一条折算
        v = "large-v3"
    if v in WHISPER_TIERS:
        return "whisper-" + v
    return ""


def _db():
    from app import db
    return db


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
    """按 (engine, model) 记一次使用（`stt` / `capabilities.local` 那条路用它）。"""
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
    """单个模型的使用情况（没有记录 = 空白的四个字段，**不是 None**：
    面板上"从未用过"与"读不出来"要用同一形状渲染，但值本身是事实）。"""
    blank = {"lastUsedAt": "", "useCount": 0, "pinned": False, "pinnedAt": ""}
    return usage_map().get(str(model_id or "").strip(), blank)


def pin(model_id: str, pinned: bool = True) -> bool:
    """打/摘「保留」钉子（永久不列入清理建议）。"""
    mid = str(model_id or "").strip()
    if not mid:
        return False
    try:
        return bool(_db().set_model_pin(mid, pinned=bool(pinned)))
    except Exception:
        return False


def forget(model_id: str) -> bool:
    """删掉一个模型后清它的账本行（免得留一行指向已经不存在的模型）。"""
    try:
        _db().clear_model_usage(str(model_id or "").strip())
        return True
    except Exception:
        return False


def known_ids() -> List[str]:
    """出厂清单里的全部 item id（懒导入 modelinfo，避免模块级循环依赖）。"""
    try:
        from app import modelinfo
        return [str(e.get("id")) for e in modelinfo.CATALOG]
    except Exception:
        return []


#: 「在用」判据里，设置键 → 说明（面板要把原因原样说给用户听，不许只说"在用"）。
_SETTING_ENGINE_KEYS = (
    ("sttModel", "命令转写引擎（设置 sttModel）"),
    ("meetingSttModel", "会议转写引擎（设置 meetingSttModel）"),
)


def in_use_ids() -> Dict[str, List[str]]:
    """"现在谁在用" → ``{model_id: [原因, …]}``。

    **两个来源，缺一不可**：

    1. 当前配置选中的（`sttModel` / `meetingSttModel` / `wakeEngine` / 两个能力通道）——
       选中的引擎即使此刻没加载，也绝不能删（删了下次一定跑不起来）；
    2. 本进程已经加载的（`stt.engine_status()`）—— 正在用的不许拔电源。

    保守取向：`capabilityDiarizeBackend=auto` 时**本机也可能被选中**，所以照样保护 pyannote。
    宁可少给一条清理建议，也不要删掉一个还会被用到的模型。
    """
    out: Dict[str, List[str]] = {}

    def add(mid: str, why: str) -> None:
        mid = str(mid or "").strip()
        if mid:
            out.setdefault(mid, []).append(why)

    try:
        from app.config import settings
        for key, why in _SETTING_ENGINE_KEYS:
            add(engine_model_id(settings.get(key)), why)
        wake = str(settings.get("wakeEngine") or "")
        try:
            wake_on = bool(settings.get("wakeEnabled"))
        except Exception:
            wake_on = False
        if wake_on:
            if wake == "kws":
                add("kws", "唤醒词引擎（wakeEngine=kws）")
            elif wake == "sherpa":
                add("sherpa", "唤醒词引擎（wakeEngine=sherpa）")
        for key, why in (("capabilityDiarizeBackend", "说话人分离"),
                         ("capabilityEmbedBackend", "声纹嵌入")):
            value = str(settings.get(key) or "auto")
            if value in ("auto", "local"):
                add("pyannote", "%s 可能走本机（%s=%s）" % (why, key, value))
    except Exception:
        pass

    try:
        from app.audio import stt
        for row in ((stt.engine_status() or {}).get("loaded") or []):
            mid = model_id_for_engine(row.get("engine"), row.get("model"))
            add(mid, "本进程已加载：%s" % (row.get("key") or row.get("engine") or ""))
    except Exception:
        pass
    return out
