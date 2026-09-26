# -*- coding: utf-8 -*-
"""model_cleanup.py — 「近期没再使用的模型」清理（**默认只给建议，删要人点头**）。

用户原话（2026-09-26）："本地能力部署运行情况 … 这里近期没再使用的模型就清掉吧"。
但"清掉"必须是**可解释、可反悔、可核对**的动作，所以这个模块的形态是三段：

  1. `preview()`  —— **先算给你看**：每一项的名称 / 占用 / 上次使用 / 是否在用 / 建议理由；
                     它**一个字节都不动盘**（用例钉着：预览后目录必须原样）；
  2. `execute()`  —— 只删"明确点名 + 过了保护判据"的那些；删完**如实回报**每个模型
                     释放了多少字节、哪个失败了、失败的真原因是什么；
  3. `pin()`      —— 给模型打「保留」钉子（永久不列入建议）。

四条"绝不删"（判据在 `_protection()`，改这里必须同时改用例）
----------------------------------------------------------

* **当前配置选中的**（`sttModel` / `meetingSttModel` / `wakeEngine` / 两个能力通道）；
* **正在被加载或使用的**（`stt.engine_status()` 里活着的那几个）；
* **打了「保留」钉子的**；
* **依赖它们的组合**：`qwen3asr` 的两个模型（ASR + 强制对齐器）**同生共死** ——
  删除单元是整个 item（`modelinfo.model_paths()` 给出它的**全部**落点），
  所以不存在"只删对齐器、留下 ASR"这种把一整项变成不可用的删法。

阈值：**默认 90 天**，可用设置 `modelCleanupDays` 改（面板「清理」卡里就能改）。

判据来源（为什么不是文件时间）
------------------------------

"上次使用"以 `model_usage` 账本为准（`app/model_usage.py`）。账本**没有**记录时，
退而求其次用落点里最新的文件时间 —— 但它只用来**推迟**建议（"刚下到盘上的东西先别动"），
不作为"用过"的证据：`idleBasis` 字段会如实写 `ledger` 还是 `file-time`，
面板照原样显示（用户看到的每一句都得是真的）。
"""
from __future__ import annotations

import datetime
import os
import shutil
from typing import Any, Dict, List, Optional

#: 「近期」的出厂阈值（天）。可被设置 `modelCleanupDays` 覆盖。
DEFAULT_DAYS = 90

#: 「保留」钉子的中文说法（面板与回报文案共用，别在两处各写一句）。
PIN_LABEL = "已标记「保留」"

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _parse_time(text) -> Optional[datetime.datetime]:
    s = str(text or "").strip()
    if not s:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt_time(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _days() -> int:
    """当前阈值（天）。设置读不出来就用出厂值 —— 清理**不能**因为读配置失败而放开。"""
    try:
        from app.config import settings
        value = int(settings.get("modelCleanupDays", DEFAULT_DAYS))
    except Exception:
        return DEFAULT_DAYS
    return max(0, value)


def _newest_mtime(paths) -> Optional[datetime.datetime]:
    """这些落点里**最新的**文件时间（只看真实文件，不看目录本身）。"""
    newest = None
    for root in paths:
        if not os.path.isdir(root):
            continue
        for cur, _dirs, files in os.walk(root):
            for name in files:
                try:
                    ts = os.path.getmtime(os.path.join(cur, name))
                except OSError:
                    continue
                if newest is None or ts > newest:
                    newest = ts
    return datetime.datetime.fromtimestamp(newest) if newest else None


def _paths_of(mid: str) -> List[str]:
    from app import modelinfo
    try:
        return list(modelinfo.model_paths(mid))
    except Exception:
        return []


def _dir_bytes(path: str) -> int:
    """目录精确字节数（"释放了多少"要按字节报，MB 是四舍五入过的、对不上账）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _path_rows(paths) -> List[Dict[str, Any]]:
    """每个落点的**精确字节数**与可读的 MB。

    判"有没有权重"必须用**字节**：`_dir_mb()` 是四舍五入过的，测试里几十 KB 的现场
    会被读成 0 MB —— 而"0 MB"在这条链路上等于"本机没有它"，于是明明有目录也判成空。
    显示用 MB，判断用字节，两者各司其职。
    """
    rows = []
    for p in paths:
        exists = os.path.isdir(p)
        nbytes = _dir_bytes(p) if exists else 0
        rows.append({"path": p, "exists": exists, "bytes": nbytes,
                     "mb": int(round(nbytes / 1048576))})
    return rows


def _protection(mid: str, pinned: bool, use_reasons: List[str]) -> str:
    """返回保护理由（空串 = 可以删）。**顺序有意义**：先说事实最硬的那条。"""
    if use_reasons:
        return "正在使用：" + "；".join(use_reasons)
    if pinned:
        return PIN_LABEL
    return ""


def _option_values(key: str) -> set:
    """某个设置键**当前可选**的值（读不出来 = 空集合）。

    **从 `config.DEFAULTS` 读，不从库里读**：库行要等 `seed_defaults()` 才有，
    而清理预览可能在任何时刻被调（面板刚起、库刚建、向导还没跑完）—— 那时库里没有
    这一行，返回空集合会把**每一个**引擎都判成"退役"，于是一屏"建议删除"全是假的。
    选项是**出厂声明的**（含平台折算），与库里的当前值无关，所以这里问 config 才对。
    """
    try:
        from app import config as cfg
        meta = cfg.DEFAULTS.get(key) or {}
        return {str(v).strip().lower() for v in (cfg._effective_options(key, meta) or [])}
    except Exception:
        return set()


def _is_retired(mid: str) -> bool:
    """这一项**在面板里还能不能被选中**（不能 = 已退役）。

    为什么这条判据值得存在（2026-09-26）：whisper 三档的权重还在本机，但 `sttModel` /
    `meetingSttModel` 的选项里早就没有 whisper 了 —— 也就是说**它永远不可能被"用"**。
    这种"留着但退役"的模型正是用户点名要清的（"这里近期没再使用的模型就清掉吧"），
    可它没有使用记录、文件时间还可能很新（拷过来/刚下过），只按 90 天规则会**永远漏掉**。

    所以：**退役 + 无使用记录** = 建议清理（照样只是建议，删要人点头，钉子和在用照样保护）。
    """
    mid = str(mid or "")
    stt = _option_values("sttModel")
    meeting = _option_values("meetingSttModel")
    wake = _option_values("wakeEngine")
    if mid.startswith("whisper-"):
        tier = mid.split("-", 1)[1].lower()
        tier = "large-v3" if tier == "large" else tier
        return tier not in stt and tier not in meeting
    if mid == "sherpa":
        return "sherpa" not in stt and "sherpa" not in meeting and "sherpa" not in wake
    if mid == "kws":
        return "kws" not in wake
    if mid in ("sensevoice", "qwen3asr"):
        return mid not in stt and mid not in meeting
    # 唤醒词 / pyannote 这类不由"引擎下拉"选择的，不走退役判据（pyannote 会议一定会用到）
    return False


def _item(mid: str, days: int, usage: Dict, use_reasons: List[str]) -> Dict[str, Any]:
    """一个候选模型的完整事实。**不做任何写动作**（预览与执行共用它，判据只有一份）。"""
    from app import modelinfo
    entry = modelinfo._by_id(mid)
    name = str((entry or {}).get("name") or mid)
    group = str((entry or {}).get("group") or "")
    paths = _paths_of(mid)
    rows = _path_rows(paths)
    local_bytes = sum(r["bytes"] for r in rows)
    local_mb = int(round(local_bytes / 1048576))
    try:
        ready = bool(modelinfo.ready(mid))
    except Exception:
        ready = False

    last_used = str(usage.get("lastUsedAt") or "")
    basis = "ledger" if last_used else ""
    ref = _parse_time(last_used)
    if ref is None:
        ref = _newest_mtime([r["path"] for r in rows if r["exists"]])
        if ref is not None:
            basis = "file-time"
    idle_days = int((_now() - ref).total_seconds() // 86400) if ref else None
    retired = bool(not last_used and _is_retired(mid))

    protect = _protection(mid, bool(usage.get("pinned")), use_reasons)
    if protect:
        reason = protect
    elif local_bytes <= 0:
        reason = "本机没有它的权重（无可释放）"
    elif retired:
        basis = "retired"
        reason = "已退役（面板里不再提供它，本机也无使用记录）"
    elif idle_days is None:
        reason = "说不清上次使用时间（不列入建议）"
    elif idle_days >= days:
        reason = ("已 %d 天没用过" % idle_days if basis == "ledger"
                  else "本机无使用记录，且文件停在 %d 天前" % idle_days)
    elif basis == "ledger":
        reason = "最近 %d 天用过（阈值 %d 天）" % (idle_days, days)
    else:
        reason = ("本机无使用记录，但文件很新（%d 天前）—— 先不列入建议" % idle_days)

    suggested = bool(not protect and local_bytes > 0
                     and (retired or (idle_days is not None and idle_days >= days)))
    return {
        "id": mid, "name": name, "group": group, "ready": ready,
        "localMb": local_mb, "localBytes": local_bytes,
        "paths": rows,
        "lastUsedAt": last_used, "lastUsedBasis": basis, "useCount": int(usage.get("useCount") or 0),
        "idleDays": idle_days,
        "retired": _is_retired(mid),
        "pinned": bool(usage.get("pinned")),
        "inUse": bool(use_reasons), "inUseReasons": list(use_reasons),
        "protected": bool(protect), "protectReason": protect,
        "suggested": suggested, "reason": reason,
    }


def preview(days: Optional[int] = None) -> Dict[str, Any]:
    """**只读**预览。返回形状见模块文档；`suggested` 是建议删除的 id 列表。"""
    from app import model_usage
    days = _days() if days is None else max(0, int(days))
    usage = model_usage.usage_map()
    in_use = model_usage.in_use_ids()
    items = []
    for mid in model_usage.known_ids():
        items.append(_item(mid, days, usage.get(mid) or {}, in_use.get(mid) or []))
    # 顺序：建议删的在前（占用大的优先），其余按 id —— 面板照着渲染就够，不必再排序
    items.sort(key=lambda it: (not it["suggested"], -it["localMb"], it["id"]))
    suggested = [it["id"] for it in items if it["suggested"]]
    return {
        "days": days,
        "items": items,
        "suggested": suggested,
        "suggestedMb": int(round(sum(it["localBytes"] for it in items
                                     if it["suggested"]) / 1048576)),
        "suggestedBytes": sum(it["localBytes"] for it in items if it["suggested"]),
        "totalMb": int(round(sum(it["localBytes"] for it in items) / 1048576)),
        "totalBytes": sum(it["localBytes"] for it in items),
    }


def _remove_dir(path: str) -> Optional[str]:
    """删一个目录；成功返回 None，失败返回**真原因**（异常类型 + 消息）。"""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return "%s: %s" % (type(e).__name__, e)
    return None


def execute(ids, days: Optional[int] = None) -> Dict[str, Any]:
    """删掉点名的模型。**每一项都要重新过一遍保护判据**（预览与执行之间可能已经变了）。

    返回 ``{"ok", "removed": [{id,name,freedMb,freedBytes,paths}], "failed": [{id,reason}],
    "freedMb", "freedBytes"}``。`failed` 里既有"删不动"（权限/占用）也有"被保护拒绝"——
    两种情况都要给人看**真原因**，不许吞成一句"失败"。
    """
    from app import modelinfo, model_usage
    if isinstance(ids, str):
        ids = [ids]
    wanted = [str(i or "").strip() for i in (ids or []) if str(i or "").strip()]
    if not wanted:
        return {"ok": False, "message": "没有选中任何模型", "removed": [], "failed": [],
                "freedMb": 0, "freedBytes": 0}
    days = _days() if days is None else max(0, int(days))
    usage = model_usage.usage_map()
    in_use = model_usage.in_use_ids()
    removed, failed = [], []
    for mid in wanted:
        if not modelinfo._by_id(mid):
            failed.append({"id": mid, "reason": "未知模型 id（清单里没有这一项）"})
            continue
        item = _item(mid, days, usage.get(mid) or {}, in_use.get(mid) or [])
        if item["protected"]:
            failed.append({"id": mid, "reason": "拒绝删除（%s）" % item["protectReason"]})
            continue
        paths = [r["path"] for r in item["paths"] if r["exists"]]
        if not paths:
            failed.append({"id": mid, "reason": "本机没有它的权重（没有可删的目录）"})
            continue
        before = sum(_dir_bytes(p) for p in paths)
        errs = []
        for p in paths:
            err = _remove_dir(p)
            if err:
                errs.append("%s（%s）" % (p, err))
        after = sum(_dir_bytes(p) for p in paths)
        freed = max(0, int(before - after))
        if errs and all(os.path.isdir(p) for p in paths):
            # 一个都没删掉：这是**失败**，如实报原因（不要报"释放 0 字节"了事）
            failed.append({"id": mid, "reason": "；".join(errs)})
            continue
        if errs:
            # 删了一部分：算成功，但把没删掉的原因一并带上（不许悄悄吞）
            failed.append({"id": mid, "reason": "部分目录删不掉：" + "；".join(errs)})
        model_usage.forget(mid)
        removed.append({"id": mid, "name": item["name"], "freedMb": round(freed / 1048576),
                        "freedBytes": freed, "paths": paths})
    total_bytes = sum(r["freedBytes"] for r in removed)
    return {
        "ok": bool(removed),
        "removed": removed, "failed": failed,
        "freedBytes": total_bytes, "freedMb": round(total_bytes / 1048576),
        "message": ("已删除 %d 个模型，释放 %.0f MB" % (len(removed), total_bytes / 1048576)
                    if removed else "没有删除任何模型"),
    }


def pin(model_id: str, pinned: bool = True) -> Dict[str, Any]:
    """打/摘「保留」钉子（面板上的那一个小按钮）。返回最新的一行事实。"""
    from app import model_usage
    mid = str(model_id or "").strip()
    if not mid:
        return {"ok": False, "message": "缺少模型 id"}
    if not model_usage.pin(mid, pinned):
        return {"ok": False, "message": "写不进库（看看数据库是不是被占着）"}
    return {"ok": True, "id": mid, "pinned": bool(pinned),
            "message": ("已保留：%s（不再列入清理建议）" % mid) if pinned
                       else ("已取消保留：%s" % mid)}
