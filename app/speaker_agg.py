# -*- coding: utf-8 -*-
"""speaker_agg.py — 跨会议**说话人聚合建议**（把"同一个人被分成好几簇"收拢成一个人）。

为什么需要它（2026-10-07 用户提出）
----------------------------------
pyannote 分离是**按场独立**聚类的：同一个人在不同会议里会拿到互不相干的标签
（这场叫 S1、下场叫 S3），而且**一场里也可能被切成两簇**。用户面对的是一堆
「说话人1…说话人12」，要认人只能一场一场点开听 —— 10 场就是几十次。

而库里其实**已经存了判据**：`speaker_embeddings` 每场每个说话人一条平均嵌入
（归一化 float32）。所以"这些人是不是同一个"可以**先算出来**，再用**试听**让人确认。

设计口径（与既有代码同源，不另立一套）
--------------------------------------
* 相似度 = 余弦（嵌入已归一化，所以就是点积）——与 `voiceprint.VoiceMatcher` 同一套判据；
* 阈值取 `voiceprint.thresholds()` 的识别阈值（默认 0.75）：**用同一个数**，
  否则会出现"聚合建议说是一个人、认人时又说不像"这种自相矛盾；
* 只建议、不自动做：**要不要并、并成什么名字都由用户定**（这是"认人"这种事的基本要求）。
* 已经入库的联系人（`voiceprints` 里有样本的那些人名）**不再进建议** ——
  它们已经认过人了，再提一遍是噪音。
"""
from __future__ import annotations

import json
import sys

import numpy as np

from app import db

#: 余弦相似度阈值。默认取识别阈值（见模块 docstring 的"设计口径"）。
DEFAULT_THRESHOLD = 0.75

#: 一条建议里最多列几个候选（用户要求"合并建议 1–3"）
MAX_CANDIDATES = 3

#: 一次最多算多少条说话人样本。嵌入比对是 O(n²)，但 n 是"说话人条数"而不是"音频秒数"，
#: 几十场也就几百条 —— 这里给个上限只是防"库里几万条时把面板拖死"。
MAX_ITEMS = 4000


def _threshold():
    """相似度阈值：优先用声纹识别那套（同一个数），拿不到就退回 DEFAULT_THRESHOLD。"""
    try:
        from app import voiceprint
        t = voiceprint.thresholds()
        v = float(t.get("threshold") or 0.0)
        if 0.0 < v <= 1.0:
            return v
    except Exception:
        pass
    return DEFAULT_THRESHOLD


def _pack(vec):
    from app import voiceprint
    blob, dim = voiceprint.pack(vec)
    return blob, dim


def _unpack(row):
    from app import voiceprint
    return voiceprint._as_vec(row)                              # noqa: SLF001 - 同包内部口径


def _cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if not na or not nb:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _label_num(label):
    """`S12` → 12；给"保留哪个标签"用（小的当代表，与 `voiceprint._key_num` 同规则）。"""
    import re
    m = re.search(r"\d+", str(label or ""))
    return int(m.group(0)) if m else 10 ** 9


def _speaker_rows():
    """所有**可参与聚合**的说话人样本：[(meeting_id, meeting_name, label, name, vec, segments)]。

    排除两类：
      * 没嵌入的（旧数据 / 分离没成功）；
      * 名字已经是**联系人**的（非默认名）—— 那种已经认过人，不再建议。
    """
    items = []
    try:
        meetings = {int(m["id"]): m for m in db.list_meetings(limit=500)}
    except Exception:
        meetings = {}
    for mid, meeting in meetings.items():
        try:
            by_label = db.get_speaker_embeddings(mid) or {}
        except Exception:
            continue
        # `get_speaker_embeddings()` 返回的是 **dict**（`{label: row}`），不是 list
        for label, r in by_label.items():
            vec = _unpack(r)
            if vec is None:
                continue
            label = str(label or r.get("label") or "")
            name = _name_of_speaker(mid, label)
            items.append({
                "meetingId": int(mid),
                "meetingName": str(meeting.get("name") or ""),
                "label": label,
                "name": name,
                "vec": vec,
                "segments": int(r.get("segments") or 0),
            })
            if len(items) >= MAX_ITEMS:
                return items
    return items


def _name_of_speaker(meeting_id, label):
    """这个说话人当前的显示名（`speakers` 表里那一行的 name）。取不到就空串。"""
    try:
        row = db._query_one(                                        # noqa: SLF001
            "SELECT name FROM speakers WHERE meeting_id=? AND label=?", (meeting_id, label))
        return str((row or {}).get("name") or "")
    except Exception:
        return ""


def _contact_names():
    """声纹库里已有的联系人名（这些已经认过人，不再建议）。"""
    names = set()
    try:
        for r in db.list_voiceprints():
            n = str(r.get("name") or "").strip()
            if n:
                names.add(n)
    except Exception:
        pass
    return names


def _default_name(row):
    """这条样本的名字是不是"还没认人"（默认名/空）。"""
    try:
        from app import voiceprint
        return voiceprint.is_default_name(row.get("name"), row.get("label"))
    except Exception:
        n = str(row.get("name") or "").strip()
        return not n or n == str(row.get("label") or "").strip()


def suggestions(threshold=None, limit=8):
    """跨会议说话人聚合建议。

    返回 ``{"threshold": t, "scanned": n, "items": [...]}``；每条：

        {
          "id": "<代表条目的 meetingId:label>",
          "members": [{meetingId, meetingName, label, name, segments, similarity}],
          "best": 0.93,                 # 这条建议里最低的那对相似度（保守地报"最弱一环"）
          "segments": 1234,             # 成员样本片段数之和（用来排"谁最值得先并"）
          "meetings": 3,                # 涉及几场
          "suggestedName": "说话人1"    # 代表名的显示名（**不是**联系人名，等用户填）
        }

    只有**至少两个成员**的分组才会返回 —— 一个人自己没什么可并的。
    """
    thr = float(threshold) if threshold else _threshold()
    rows = [r for r in _speaker_rows() if _default_name(r)]
    if len(rows) < 2:
        return {"threshold": thr, "scanned": len(rows), "items": []}

    # 并查集：相似度 ≥ 阈值就并到一组（单链聚类）。
    n = len(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    sim_cache = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cos(rows[i]["vec"], rows[j]["vec"])
            if sim >= thr:
                sim_cache[(i, j)] = sim
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    items = []
    for members in groups.values():
        if len(members) < 2:
            continue
        # 只保留相似度最高的前 MAX_CANDIDATES 个成员（按片段数排，让"话多的那个"当代表）
        members = sorted(members, key=lambda i: (-rows[i]["segments"], _label_num(rows[i]["label"])))
        keep = members[:MAX_CANDIDATES]
        best = 1.0
        for a in range(len(keep)):
            for b in range(a + 1, len(keep)):
                key = (min(keep[a], keep[b]), max(keep[a], keep[b]))
                best = min(best, sim_cache.get(key, thr))
        rep = rows[keep[0]]
        items.append({
            "id": "%d:%s" % (rep["meetingId"], rep["label"]),
            "best": round(best, 3),
            "segments": sum(rows[i]["segments"] for i in keep),
            "meetings": len({rows[i]["meetingId"] for i in keep}),
            "suggestedName": str(rep.get("name") or rep["label"]),
            "members": [{
                "meetingId": rows[i]["meetingId"],
                "meetingName": rows[i]["meetingName"],
                "label": rows[i]["label"],
                "name": str(rows[i].get("name") or rows[i]["label"]),
                "segments": rows[i]["segments"],
            } for i in keep],
        })
    # 排序：先"片段多"（话多的人最值得先认），再"相似度高"
    items.sort(key=lambda it: (-it["segments"], -it["best"]))
    return {"threshold": thr, "scanned": len(rows), "items": items[:max(1, int(limit))]}


def apply(members, name, enroll=True):
    """把建议里的若干成员**并成一个联系人**。

    ``members``：``[{meetingId, label}, …]``（前端把建议里勾中的那些发回来）。
    ``name``：联系人名（**必须显式给** —— 不许拿默认名糊过去）。

    做三件事，且都有据可查：
      1. 每场那个说话人**改名**（`db.rename_speaker`）——详情页/转写导出立刻跟着变；
      2. 声纹**入库**（`db.replace_voiceprint_sample`）：用**样本片段最多**的那一场当样本源
         （片段多的那场嵌入更稳），其余场只改名、不重复写样本（同一个人写多条只会
         让识别变慢、还可能互相稀释）；
      3. 返回一段可直接展示的话。

    返回 ``(ok, message, detail)``。
    """
    from app import voiceprint

    name = str(name or "").strip()
    if not name:
        return False, "请先填联系人名", {}
    if voiceprint.is_default_name(name):
        return False, "「%s」还是默认名，不算联系人" % name, {}

    clean = []
    for m in members or []:
        try:
            mid = int(m.get("meetingId"))
        except (TypeError, ValueError):
            continue
        label = str(m.get("label") or "").strip()
        if mid and label:
            clean.append({"meetingId": mid, "label": label})
    if not clean:
        return False, "没有要合并的说话人", {}

    # 选样本源：片段最多的那个（拿不到片段数就按顺序第一个）
    best, best_seg = None, -1
    for m in clean:
        segs = 0
        try:
            row = (db.get_speaker_embeddings(m["meetingId"]) or {}).get(m["label"]) or {}
            segs = int(row.get("segments") or 0)
        except Exception:
            segs = 0
        if segs > best_seg:
            best, best_seg = m, segs
    if best is None:
        return False, "找不到可用的声纹样本", {}

    renamed = []
    for m in clean:
        try:
            db.rename_speaker(m["meetingId"], m["label"], name)
            renamed.append(m)
        except Exception as e:
            db.add_log("warn", "voiceprint",
                       f"聚合改名失败：{m['meetingId']}/{m['label']} → {name}：{e}")

    detail = {"renamed": renamed, "enrolled": False, "name": name}
    msg = "已改名 %d 处" % len(renamed)

    if enroll:
        meeting = db.get_meeting(best["meetingId"])
        vec = _unpack(db.get_speaker_embedding(best["meetingId"], best["label"]))
        if meeting and vec is not None:
            blob, dim = _pack(vec)
            db.replace_voiceprint_sample(name, blob, dim=dim,
                                         meeting_name=str(meeting.get("name") or ""),
                                         source_label=best["label"])
            detail["enrolled"] = True
            detail["sampleFrom"] = {"meetingId": best["meetingId"],
                                    "meetingName": str(meeting.get("name") or ""),
                                    "label": best["label"], "segments": best_seg}
            n = db.count_voiceprints(name)
            msg += "；已入库 %s（样本 %d 条，取自 %s/%s）" % (
                name, n, meeting.get("name"), best["label"])
            db.add_log("info", "voiceprint",
                       f"聚合入库：{name} ← {meeting.get('name')}/{best['label']}"
                       f"（合并 {len(renamed)} 处，样本 {n} 条）")
        else:
            msg += "；**没入库**（找不到可用嵌入）"
    return True, msg, detail
