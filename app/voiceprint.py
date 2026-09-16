# -*- coding: utf-8 -*-
"""voiceprint.py — 常用联系人声纹库（采样入库 + 匹配识别）

背景（issue #6 常见说话人智能识别）：会议转写已经能做说话人分离（pyannote）
与人工重命名。本模块把「人工改好的名字」沉淀成声纹样本，让 ECHO 记住常用
联系人：下一场会议再听到同一个人的声音，自动把「说话人3」标成「张总」。

三条链路：
  * 入库：在会议里把说话人改名为联系人（或点「声纹入库」）时，取该说话人本场
    的平均嵌入（speaker_embeddings 表，pyannote wespeaker 256 维）存成样本；
    同一会议同一说话人只留最新一条（改名改错不残留旧名字的脏样本）。
  * 识别：转写每段（默认 10 分钟）时，把本段每个说话人的嵌入与样本逐条算余弦，
    每个联系人取最好成绩、全场最高者为候选；通过「阈值 + 与次优的间隔」两道门
    才自动命名（宁可不认，也别认错）。
  * 管理：列表/删除（面板「说话人管理」+ REST API），开关与阈值在 设置 → 会议。

匹配参数（默认值偏保守，可在设置里调）：
  voiceprintThreshold 默认 0.65 —— 余弦相似度下限；
  voiceprintMargin    默认 0.05 —— 与次优联系人的最小差距，差距过小视为歧义、不命名。
每次判定（含未通过的原因）都写日志（source=voiceprint），可用自己的真实录音校准：
相似度普遍偏低就把阈值调低一档，出现认错人就把阈值/间隔调高。

边界：
  * 声纹样本来自 pyannote 说话人分离，只在开启「区分说话人」的会议里才有；
    旧版本转写的会议没有样本，「识别本场」会提示重新转写。
  * 样本只存本机 data/echo.db，不出网；删除样本不影响会议转写内容本身。
"""
import re

import numpy as np

import app.db as db
from app.config import settings

DEFAULT_DIM = 256
DEFAULT_THRESHOLD = 0.65
DEFAULT_MARGIN = 0.05


# ---------------------------------------------------------------- 配置

def enabled():
    """声纹识别总开关（设置 → 会议）。"""
    return bool(settings.get("voiceprintEnabled", True))


def auto_enroll():
    """改名为联系人时是否自动入库。"""
    return bool(settings.get("voiceprintAutoEnroll", True))


def thresholds():
    """(阈值, 间隔)；配置损坏时回退默认值。"""
    try:
        thr = float(settings.get("voiceprintThreshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        thr = DEFAULT_THRESHOLD
    try:
        margin = float(settings.get("voiceprintMargin", DEFAULT_MARGIN))
    except (TypeError, ValueError):
        margin = DEFAULT_MARGIN
    return thr, margin


# ---------------------------------------------------------------- 向量编解码

def _normalize(v):
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    if not np.all(np.isfinite(v)):
        # NaN/Inf（上游嵌入异常时可能出现）会让余弦相似度变成 NaN，而匹配里
        # `best < threshold` 遇 NaN 恒为 False、间隔门同样 → 等于"NaN 反而必中"。
        # 这里归一成零向量：与任何样本的点积都是 0，必然低于阈值 → 按"不认"处理，
        # 与「宁可不认，别认错」的设计意图一致。
        return np.zeros_like(v)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def pack(vec):
    """嵌入 → (BLOB, 维数)：归一化后按 float32 存，比对时直接点积即余弦。"""
    v = _normalize(vec)
    return v.tobytes(), int(v.shape[0])


def unpack(blob, dim=None):
    """BLOB → 归一化嵌入；维数不符或损坏返回 None。"""
    if blob is None:
        return None
    try:
        v = np.frombuffer(blob, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if v.size == 0 or (dim and v.size != int(dim)):
        return None
    return _normalize(v)


def _as_vec(row):
    """db 行（含 embedding/dim）→ 归一化嵌入；不可用返回 None。"""
    if not row:
        return None
    return unpack(row.get("embedding"), row.get("dim"))


# ---------------------------------------------------------------- 匹配

def _no_match(reason, name="", sim=0.0, runner=0.0):
    return {"ok": False, "name": name, "sim": round(float(sim), 4),
            "runner": round(float(runner), 4), "reason": reason}


class VoiceMatcher:
    """声纹库的内存匹配器：样本按联系人分组，逐条算余弦。

    判定两道门（都可配置）：
      threshold —— 与候选联系人的最好成绩必须达到；
      margin    —— 与次优联系人的差距必须拉开，否则视为歧义（宁可不认，也别认错）。
    """

    def __init__(self, samples, threshold=DEFAULT_THRESHOLD, margin=DEFAULT_MARGIN):
        """samples: [(id, name, vec)]。"""
        self.threshold = float(threshold)
        self.margin = float(margin)
        idx, owner, vecs = {}, [], []
        for _vid, name, vec in samples:
            v = _normalize(vec)
            if v.size == 0:
                continue
            if name not in idx:
                idx[name] = len(idx)
            owner.append(idx[name])
            vecs.append(v)
        self._names = [None] * len(idx)
        for name, i in idx.items():
            self._names[i] = name
        self._owner = np.asarray(owner, dtype=int)
        self._vecs = np.stack(vecs) if vecs else None

    @property
    def names(self):
        return [n for n in self._names if n]

    def match(self, vec):
        """对一条嵌入做匹配。返回 {ok, name, sim, runner, reason}。

        reason: ok | below-threshold | ambiguous | empty | dim-mismatch
        """
        if self._vecs is None or not self._names:
            return _no_match("empty")
        q = unpack(vec) if isinstance(vec, (bytes, bytearray, memoryview)) else _normalize(vec)
        if q is None or q.size != self._vecs.shape[1]:
            return _no_match("dim-mismatch")
        sims = self._vecs @ q
        per = {}
        for i, nm in enumerate(self._names):
            if nm is None:
                continue
            row = sims[self._owner == i]
            if row.size:
                per[nm] = float(row.max())
        if not per:
            return _no_match("empty")
        order = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))
        best_name, best = order[0]
        runner = order[1][1] if len(order) > 1 else 0.0
        if best < self.threshold:
            return _no_match("below-threshold", best_name, best, runner)
        if len(order) > 1 and (best - runner) < self.margin:
            return _no_match("ambiguous", best_name, best, runner)
        return {"ok": True, "name": best_name, "sim": round(best, 4),
                "runner": round(runner, 4), "reason": "ok"}


def load_matcher():
    """读声纹库构建匹配器；库空返回 None。"""
    samples = []
    for r in db.get_voiceprint_samples():
        vec = unpack(r.get("embedding"), r.get("dim"))
        if vec is not None:
            samples.append((r["id"], r["name"], vec))
    if not samples:
        return None
    thr, margin = thresholds()
    return VoiceMatcher(samples, threshold=thr, margin=margin)


def identify(embs, labels, label_map, matcher):
    """对一段分离结果里的每个说话人做声纹匹配。

    embs[i] 对应 labels[i]（与 SpeakerRegistry.map 的使用方式一致）；
    label_map: {pyannote 标签: 显示名（说话人N）}，来自 registry.map。
    返回 {显示名: 判定 dict}；同一显示名取更优的一次判定（命中优先、再比相似度）。
    """
    out = {}
    if matcher is None:
        return out
    idx = {}
    for i, lb in enumerate(labels or []):
        idx.setdefault(lb, i)
    for plabel, disp in (label_map or {}).items():
        i = idx.get(plabel)
        if i is None or i >= len(embs):
            continue
        m = matcher.match(np.asarray(embs[i], dtype=np.float32))
        if m["reason"] in ("empty", "dim-mismatch"):
            continue
        old = out.get(disp)
        if old is None or (m["ok"] and not old["ok"]) or m["sim"] > old["sim"]:
            out[disp] = m
    return out


def _key_num(key):
    m = re.search(r"\d+", str(key))
    return int(m.group(0)) if m else 10 ** 9


def duplicate_merges(matched):
    """同一联系人对应多个说话人键 → 大编号并进最小编号。

    matched: {说话人键: 联系人名}；返回 {被并键: 保留键}（无重复时空 dict）。
    用来收拾「同一个人被分离成两簇」的老问题（否则一场会议出现两个「张总」）。
    """
    keep = {}
    for key, name in matched.items():
        cur = keep.get(name)
        if cur is None or _key_num(key) < _key_num(cur):
            keep[name] = key
    return {k: keep[n] for k, n in matched.items() if keep[n] != k}


def is_default_name(name, label=""):
    """空 / 「说话人N」/ 就是标签本身 → 视为默认名（不当作联系人入库）。"""
    s = str(name or "").strip()
    return not s or s == str(label or "").strip() or re.fullmatch(r"说话人\d+", s) is not None


# ---------------------------------------------------------------- 入库 / 管理

def enroll_from_meeting(meeting_id, label, name):
    """把某场会议某说话人的声纹存入联系人名下（同场同人只留最新一条）。

    返回 (ok, message)；message 可直接展示给用户。
    """
    name = str(name or "").strip()
    if not name:
        return False, "联系人名为空"
    if is_default_name(name, label):
        return False, f"「{name or label}」还是默认说话人名，未当作联系人入库"
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    vec = _as_vec(db.get_speaker_embedding(meeting_id, label))
    if vec is None:
        return False, ("本场没有该说话人的声纹样本（转写时需开启「区分说话人」，"
                       "旧版本数据重新转写后再试）")
    blob, dim = pack(vec)
    db.replace_voiceprint_sample(name, blob, dim=dim,
                                 meeting_name=meeting["name"], source_label=label)
    n = db.count_voiceprints(name)
    db.add_log("info", "voiceprint",
               f"声纹入库：{name} ← {meeting['name']}/{label}（样本 {n} 条）")
    return True, f"已入库：{name}（样本 {n} 条）"


def recognize_meeting(meeting_id):
    """用声纹库给一场会议重新认人（只用转写时留存的样本，不重新分离/转写）。

    规则：
      * 只覆盖仍是默认名（说话人N/空）的说话人 —— 用户手动改过的名字优先；
      * 同一声纹被多个说话人命中时合并（编号小的保留，行归到保留键），
        但只合并「本轮自动命名」的说话人：手动改过名的一律不动；
      * 返回 {ok, message, results[], renamed, merged[]}。
    """
    empty = {"results": [], "renamed": 0, "merged": []}
    if not enabled():
        return {"ok": False, "message": "声纹识别已在设置里关闭（设置 → 会议）", **empty}
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return {"ok": False, "message": "会议不存在", **empty}
    emb_rows = db.get_speaker_embeddings(meeting_id)
    if not emb_rows:
        return {"ok": False,
                "message": "本场没有留存的声纹样本（转写时未开启「区分说话人」或为旧版本数据，"
                           "重新转写后再试）", **empty}
    matcher = load_matcher()
    if matcher is None:
        return {"ok": False, "message": "声纹库为空：先把说话人改名为联系人（或点「声纹入库」）",
                **empty}

    results, matched = [], {}
    auto_renamed = set()
    for sp in db.get_speakers(meeting_id):
        label = sp["label"]
        item = {"label": label, "name": sp["name"] or label, "matched": False,
                "sim": 0.0, "runner": 0.0, "reason": "", "renamed": False}
        vec = _as_vec(emb_rows.get(label))
        if vec is None:
            item["reason"] = "无样本"
        else:
            m = matcher.match(vec)
            item.update(matched=bool(m["ok"]), sim=m["sim"], runner=m["runner"],
                        reason=m["reason"])
            if m["ok"]:
                matched[label] = m["name"]
                if is_default_name(sp["name"], label):
                    db.rename_speaker(meeting_id, label, m["name"])
                    auto_renamed.add(label)
                    item["name"] = m["name"]
                    item["renamed"] = True
        results.append(item)

    merged = []
    for src, tgt in duplicate_merges(matched).items():
        # 只在「双方都是本轮自动命名」时合并：手动改过名的说话人不参与
        # 自动合并（宁可少做一步，也不吞掉用户自己改的名字）。
        if src not in auto_renamed or tgt not in auto_renamed:
            continue
        db.merge_speakers(meeting_id, src, tgt)
        merged.append({"from": src, "to": tgt, "name": matched.get(src, "")})
        for item in results:
            if item["label"] == src:
                item["mergedTo"] = tgt
        db.add_log("info", "voiceprint",
                   f"识别本场：{src} 与 {tgt} 是同一个人（{matched.get(src, '')}），已合并")
    for item in results:
        if item["matched"]:
            db.add_log("info", "voiceprint",
                       f"识别本场：{item['label']} → {matched.get(item['label'], item['name'])}"
                       f"（相似度 {item['sim']:.2f}）")
    renamed = sum(1 for x in results if x["renamed"])
    if not matched:
        msg = "没有认出声纹库里的联系人"
    else:
        parts = [f"{renamed} 人自动命名"]
        if merged:
            parts.append(f"{len(merged)} 组同人合并")
        msg = "识别完成：" + "，".join(parts)
    return {"ok": True, "message": msg, "results": results,
            "renamed": renamed, "merged": merged}


def library_view():
    """面板用：联系人 → 样本列表（按最近入库倒序）。"""
    by_name = {}
    for r in db.list_voiceprints():
        it = by_name.setdefault(r["name"], {"name": r["name"], "samples": [],
                                            "last": ""})
        it["samples"].append({"id": r["id"], "meeting_name": r["meeting_name"],
                              "source_label": r["source_label"],
                              "created_at": r["created_at"]})
        it["last"] = max(it["last"], r["created_at"] or "")
    items = sorted(by_name.values(), key=lambda x: x["last"], reverse=True)
    for it in items:
        it["count"] = len(it["samples"])
    return items


def library_stats():
    return {"contacts": db.count_voiceprint_contacts(), "samples": db.count_voiceprints()}


def delete_sample(vid):
    row = db.get_voiceprint(vid)
    if not row:
        return False, "样本不存在"
    db.delete_voiceprint(vid)
    db.add_log("info", "voiceprint", f"已删除声纹样本 #{vid}（{row['name']}）")
    return True, f"已删除一条样本（{row['name']}）"


def delete_contact(name):
    name = str(name or "").strip()
    if not name:
        return False, "联系人名为空"
    n = db.count_voiceprints(name)
    if not n:
        return False, f"声纹库里没有「{name}」"
    db.delete_voiceprints_by_name(name)
    db.add_log("info", "voiceprint", f"已删除联系人声纹：{name}（{n} 条样本）")
    return True, f"已删除「{name}」的 {n} 条样本"
