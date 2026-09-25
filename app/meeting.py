# -*- coding: utf-8 -*-
"""meeting.py — ECHO 会议业务（录音 → 分段 → 转写 → 说话人分离 → 纪要）

编排（去掉旧实现的 http.server 耦合，数据全部入库）：
  start_meeting()   开录音线程（MeetingRecorder，按分钟分段）
  stop_meeting()    停录 → 后台转写（whisper 或 sensevoice）
                      → 可选 pyannote 说话人分离 → 写入 DB（lines/speakers）
                      → 导出 transcript.md → 可选请求 DSH 生成纪要
  regenerate_summary() / retranscribe_meeting()  手动重跑

文件布局：data/meetings/<2026-08-21_10-00-00>/{01.wav, meta.json, transcript.md, summary.md}
"""
import datetime
import json
import os
import re
import sys
import threading
import time

import app.db as db
from app.config import settings
from app.dsh import get_client
from app import paths, worklog
from app.audio.recorder import MeetingRecorder, resolve_input_device
# 音频段的**归档格式与用时解码**（2026-09-26：历史音频无损压成 FLAC）。
# 为什么单独一个模块：sherpa 只认 RIFF/WAV，所以"读到 flac 就先解成临时 wav
# 再交给既有代码"这件事必须**引擎无关**地只写一份（不要指望各引擎自己认 FLAC）。
# 也刻意不去动 `app/audio/stt.py`（另一个任务在改它）。
from app.audio import audiofile
from app.audio import stt as stt_mod
from app.audio import tts as tts_mod
# 拼装层（设计 §4.4）：把各后端给回的东西统一成"逐句 + 时间"，并且**只在这一处**实现。
# 模块级 import 是刻意的：它只依赖标准库 + dataclasses，没有重依赖，
# 而且漏了它会在**第一次转写时**才炸（NameError），不如导入时就炸。
from app.capabilities import assemble
from app import services

# 安装根由路径层给（含 ECHO_ROOT 覆盖）；会议目录一律走 meetings_dir()（D20/D21）。
BASE_DIR = paths.echo_root()
SAMPLE_RATE = 16000


# ---------------------------------------------------------------- 路径（2.0 / D20、D21）
# 1.x 里这是 import 期常量（`BASE_DIR/data/meetings`），用户改不了；2.0 起由配置项
# `meetingsDir` 决定（D20），且**每次调用都重新解析**（D21）。
#
# 为什么是函数而不是"模块级 __getattr__ + 常量名"：PEP 562 的模块 __getattr__ 只在
# **属性访问**（`meeting.meetings_dir()`）时生效，模块内部函数里的**裸名字**不会走它，
# 会直接 NameError。所以内部的 17 处引用统一改成调用 `meetings_dir()`，
# 跨模块的 `meeting.meetings_dir()` 也一并改成函数调用——不留兼容别名，
# 免得"有的地方跟着配置走、有的地方是 import 快照"这种半成品状态。
def meetings_dir() -> str:
    """当前生效的会议目录（用户可在面板里改，改了立刻生效）。"""
    from app import paths
    return paths.meetings_root()


def ensure_meetings_dir() -> str:
    """确保会议目录存在并返回它。录制/写纪要前调用（路径可能随时被用户改）。"""
    root = meetings_dir()
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        pass
    return root


# 保持 1.x"导入后目录就已存在"的行为；配置坏掉时不能因此炸掉 import。
try:
    ensure_meetings_dir()
except Exception:
    pass

# ---------------------------------------------------------------- 状态

_state = {
    "active": False,
    "folder": None,
    "recorder": None,
    "started_at": None,
    "level": 0.0,
    "error": "",
}
# 录音状态锁：start/stop 必须原子化 —— 否则并发 stop（如面板按钮双击/重复请求）
# 会同时通过 active 检查，造成重复转写 + 重复纪要（日志里出现过两次“停止录音”同秒）。
_state_lock = threading.RLock()
_retranscribing = {"set": set(), "lock": threading.Lock()}

# ---------------------------------------------------------------- 音频压缩
# 历史会议的音频段（`01.wav`…）**无损**压成 FLAC（`01.flac`，省约一半磁盘）。
# 详见 `app/audio/audiofile.py` 的模块注释，以及本文件 `compress_meetings()`。
#
# 进度单独一份（**不塞进 `_transcribe_progress`**）：用户在看"转写中"的进度条时
# 压缩不该把它顶掉，反过来也一样。面板用 `GET /api/meetings/compress/status` 取它。
_compress = {
    "active": False,
    "lock": threading.Lock(),
    "progress": {"running": False, "phase": "", "done": 0, "total": 0, "percent": 0,
                 "current": "", "updated_at": 0.0},
    "last": None,        # 最近一次的结果汇总（面板压缩完要显示它）
}


def _set_compress_progress(**kw):
    with _compress["lock"]:
        _compress["progress"].update(kw)
        _compress["progress"]["updated_at"] = time.time()


def compress_progress():
    """压缩进度（面板轮询）。`last` = 最近一次完成的汇总（可能为空）。"""
    with _compress["lock"]:
        out = dict(_compress["progress"])
        out["last"] = dict(_compress["last"]) if _compress["last"] else None
        return out


def compression_state(meeting_name):
    """一场会议在"要不要压/能不能压"上的状态（`(kind, why)`，kind 空 = 可以压）。

    三条硬约束各自对应一支，判据只写这一份（预览与执行共用，所以"看到的能压"
    与"真的去压"不可能对不上）：

      * `"recording"` —— 正在录音的那一场**一律跳过**（不许动正在写的文件）；
      * `"transcribing"` —— 转写中的那一场也跳过（引擎正在读这些文件）；
      * `"retranscribing"` —— 面板刚点过「重新转写」，同一条纪律（进程内标记）。

    注意这里**不判** `meetingKeepRawAudio`：那个设置管的是"删不删原件"，
    不管"压不压"（压缩本身在任何情况下都是无损的，见 `compress_meetings`）。
    """
    name = str(meeting_name or "")
    if not name:
        return "missing", "没有会议名"
    with _state_lock:
        if _state["active"] and os.path.basename(_state["folder"] or "") == name:
            return "recording", "正在录音"
    with _retranscribing["lock"]:
        if name in _retranscribing["set"]:
            return "retranscribing", "正在重新转写"
    try:
        m = db.get_meeting_by_name(name)
    except Exception:
        m = None
    if m and (m.get("status") or "") in ("transcribing",):
        return "transcribing", "正在转写"
    return "", ""


def _compression_summary(meta):
    """从 `meta.json` 里取这场会的压缩记录（没有 = None）。"""
    comp = meta.get("compression") if isinstance(meta, dict) else None
    return comp if isinstance(comp, dict) else None


def compression_info(meeting_name):
    """给面板/接口用的"这一场压过了吗"（没有记录返回 None）。

    形状（列表与详情共用同一份，面板只读它）：
        {beforeBytes, afterBytes, savedPercent, deletedRaw, at, segments, keptRaw}
    """
    meta = meeting_meta(meeting_name)
    comp = _compression_summary(meta)
    if not comp:
        return None
    try:
        before = int(comp.get("beforeBytes") or 0)
        after = int(comp.get("afterBytes") or 0)
    except (TypeError, ValueError):
        return None
    if not before and not after:
        return None
    return {
        "beforeBytes": before,
        "afterBytes": after,
        "savedPercent": int(comp.get("savedPercent")
                            if comp.get("savedPercent") is not None
                            else audiofile.saving_percent(before, after)),
        "deletedRaw": bool(comp.get("deletedRaw")),
        "keptRaw": bool(comp.get("keptRaw")),
        "at": comp.get("at") or "",
        "segments": int(comp.get("segments") or 0),
        "beforeText": audiofile.human_size(before),
        "afterText": audiofile.human_size(after),
        "savedText": audiofile.human_size(max(before - after, 0)),
    }


def compression_preview(limit=500, items=None):
    """「先算给你看」：可压缩 N 场 / 能省 X（**纯读，不写一个字节**）。

    返回的字段就是面板要显示的那几个，另外带上逐场的 `meetings`（详情可展开）。
    `deletedRaw`/`keptRaw` 由 `meetingKeepRawAudio` 决定 —— 用户勾了"保留原始音频"
    时我们**只压不删**，所以"省下多少"是 0，但"能少占多少"仍然报出来
    （面板上必须分得清这两件事，否则用户会以为压缩骗了他）。

    `items` 可显式传 `[(name, folder, skipKind, skipWhy, title), …]`（用例用；
    也方便日后接"只算选中的那几场"）。默认取库里的会议。
    """
    keep_raw = bool(settings.get("meetingKeepRawAudio", True))
    if items is None:
        items = []
        for m in db.list_meetings(limit=max(1, min(int(limit or 500), 2000))):
            name = m["name"]
            kind, why = compression_state(name)
            items.append((name, os.path.join(meetings_dir(), name), kind, why,
                          m.get("title") or name))
    scan = audiofile.scan_meetings([(n, f) for n, f, _k, _w, _t in items],
                                   keep_raw=keep_raw)
    by_name = {e["name"]: e for e in scan["meetings"]}
    out_items = []
    for name, folder, kind, why, title in items:
        entry = by_name.get(name) or {"name": name}
        entry["title"] = title
        entry["skippedReason"] = why
        entry["skipKind"] = kind
        if kind and entry.get("compressible"):
            # 正在用的那一场不参与统计（否则"可压缩 N 场"会把一场点了没反应的算进去）
            scan["count"] -= 1
            scan["beforeBytes"] -= entry.get("beforeBytes") or 0
            scan["estimateBytes"] -= entry.get("estimateBytes") or 0
            entry["compressible"] = 0
            entry["beforeBytes"] = 0
            entry["estimateBytes"] = 0
        out_items.append(entry)
    out = {
        "meetings": out_items,
        "count": max(scan["count"], 0),
        "beforeBytes": max(scan["beforeBytes"], 0),
        "estimateBytes": max(scan["estimateBytes"], 0),
        "alreadyMeetings": scan["alreadyMeetings"],
        "alreadyBytes": scan["alreadyBytes"],
        "keepRawAudio": keep_raw,
        # 勾了"保留原始音频" = **永不自动删原件** → 省下的字节就是 0（压了也不省）
        "reclaimBytes": 0 if keep_raw else max(scan["beforeBytes"] - scan["estimateBytes"], 0),
        "busy": bool(_compress["active"]),
        "note": ("已开启「保留原始音频」：本次只压缩、不删原件 —— 音频确实变小了，"
                 "但不会真正腾出空间（腾空间请先到 设置 → 会议 取消勾选）"
                 if keep_raw else "压缩后会删除原始 WAV（读回校验通过才删）"),
    }
    out["beforeText"] = audiofile.human_size(out["beforeBytes"])
    out["estimateText"] = audiofile.human_size(out["estimateBytes"])
    out["reclaimText"] = audiofile.human_size(out["reclaimBytes"])
    out["alreadyText"] = audiofile.human_size(out["alreadyBytes"])
    return out


def compress_meetings(limit=500):
    """用户确认之后**真的动手**：后台线程逐场压缩（面板轮询进度）。

    返回 `(ok, message)`；已有任务在跑时不重复起第二个。

    ## 这一场到底压不压（`meetingKeepRawAudio` 的语义）

    用户那句设置的原文是「保留原始音频 —— 删除会议时是否同时删除音频」，即
    **"不自动删原件"**。所以：

      * 勾着（默认 True）→ **照压**（压缩是无损的，压了不吃亏），但**不删原件**，
        日志与 `meta.json` 里如实写 `keptRaw: true`，面板显示"已压缩（保留原件）"；
      * 没勾 → 压完**校验通过才删原件**，真正腾出空间。

    为什么不是"勾着就整场跳过"：那样默认设置下这个功能对所有人都不生效，
    用户问的是"能不能省空间"，而被"保留原始音频"这四个字拦在门外 —— 那不是它的意思。
    """
    keep_raw = bool(settings.get("meetingKeepRawAudio", True))
    with _compress["lock"]:
        if _compress["active"]:
            return False, "压缩任务已在进行中"
        _compress["active"] = True
    _set_compress_progress(running=True, phase="准备", done=0, total=0, percent=0,
                           current="")
    threading.Thread(target=_compress_worker, args=(int(limit or 500), keep_raw),
                     daemon=True).start()
    return True, ("已开始压缩（%s）" % ("保留原件，只压缩" if keep_raw else "校验通过后删除原件"))


def _compress_worker(limit, keep_raw):
    """后台压缩：逐场、逐段压缩；每场一个汇总写回 `meta.json` 与日志。"""
    started = time.time()
    summary = {"ok": True, "keepRaw": keep_raw, "meetings": [], "compressed": 0,
               "skipped": 0, "failed": 0, "beforeBytes": 0, "afterBytes": 0,
               "savedBytes": 0, "deletedSegments": 0, "message": ""}
    try:
        items = []
        for m in db.list_meetings(limit=max(1, min(int(limit or 500), 2000))):
            name = m["name"]
            kind, why = compression_state(name)
            items.append((name, os.path.join(meetings_dir(), name), kind, why))
        todo = [it for it in items if not it[2]]
        summary["skipped"] = len(items) - len(todo)
        _set_compress_progress(phase="压缩音频", total=len(todo), done=0, percent=0)
        for pos, (name, folder, _kind, _why) in enumerate(todo, start=1):
            _set_compress_progress(current=name, done=pos - 1,
                                   percent=round((pos - 1) / max(len(todo), 1) * 100),
                                   detail="第 %d/%d 场 · %s" % (pos, len(todo), name))
            try:
                res = _compress_one_meeting(name, folder, keep_raw)
            except Exception as e:                       # 单场炸了不该毁掉整批
                res = {"name": name, "ok": False, "reason": "%s: %s" % (type(e).__name__, e)}
                db.add_log("error", "meeting", "压缩 %s 失败：%s" % (name, res["reason"]))
            summary["meetings"].append(res)
            # 计数按**段**而不是按场：部分失败时"压好了几段"是真事实，
            # 不能因为另一段坏了就把它从汇总里抹掉（面板与日志都要看得见）。
            if res.get("ok"):
                summary["compressed"] += 1
            if res.get("segments") or res.get("deletedSegments"):
                summary["beforeBytes"] += res.get("beforeBytes") or 0
                summary["afterBytes"] += res.get("afterBytes") or 0
                summary["savedBytes"] += res.get("savedBytes") or 0
                summary["deletedSegments"] += res.get("deletedSegments") or 0
            if not res.get("ok"):
                summary["failed"] += 1
            _set_compress_progress(done=pos, percent=round(pos / max(len(todo), 1) * 100))
        summary["seconds"] = round(time.time() - started, 1)
        summary["beforeText"] = audiofile.human_size(summary["beforeBytes"])
        summary["afterText"] = audiofile.human_size(summary["afterBytes"])
        summary["savedText"] = audiofile.human_size(summary["savedBytes"])
        summary["message"] = _compress_summary_text(summary)
        db.add_log("info", "meeting", "音频压缩完成：" + summary["message"])
    except Exception as e:
        import traceback
        summary["ok"] = False
        summary["message"] = "压缩任务异常：%s: %s" % (type(e).__name__, e)
        db.add_log("error", "meeting",
                   summary["message"] + "\n" + traceback.format_exc()[:1200])
    finally:
        with _compress["lock"]:
            _compress["active"] = False
            _compress["last"] = summary
        _set_compress_progress(running=False, phase="完成", percent=100, current="")


def _compress_summary_text(summary):
    """整批的一句话汇总（面板 toast 与日志共用同一句，不许两处各编一套）。"""
    parts = []
    if summary.get("compressed"):
        parts.append("已压缩 %d 场：%s → %s（省 %s）"
                     % (summary["compressed"], summary.get("beforeText") or "0 B",
                        summary.get("afterText") or "0 B",
                        summary.get("savedText") or "0 B"))
    if summary.get("deletedSegments"):
        parts.append("删除原件 %d 段" % summary["deletedSegments"])
    elif summary.get("compressed") and summary.get("keepRaw"):
        parts.append("按「保留原始音频」设置未删原件")
    if summary.get("failed"):
        parts.append("失败 %d 场（原件已保留，详见日志）" % summary["failed"])
    if summary.get("skipped"):
        parts.append("跳过 %d 场（录音中/转写中）" % summary["skipped"])
    return "；".join(parts) or "没有需要压缩的会议"


def _compress_one_meeting(name, folder, keep_raw):
    """压一场会。**任何失败都保留原件**，并把具体原因返回给调用方。

    返回字段：`name/ok/beforeBytes/afterBytes/savedBytes/deletedSegments/segments/
    failures/reason/keptRaw/seconds`。

    写回 `meta.json` 的 `compression` 块是**面板"已压缩：原 149 MB → 现 76 MB（省 49%）"
    的唯一数据来源** —— 不重新估算，只报真实字节。
    """
    t0 = time.time()
    out = {"name": name, "ok": False, "beforeBytes": 0, "afterBytes": 0,
           "savedBytes": 0, "deletedSegments": 0, "segments": [], "failures": [],
           "keptRaw": bool(keep_raw), "reason": "", "seconds": 0.0}
    if not os.path.isdir(folder):
        out["reason"] = "会议目录不存在"
        return out
    # **正在用的那一场一律跳过**（录音中 / 转写中 / 刚点过重新转写）。
    # 判据在这里**再判一次**，不只依赖 `_compress_worker` 的过滤：这个函数是
    # "动文件"的最后一道闸，任何调用方（含日后新加的入口、或用例直接调它）
    # 都不该能绕过去动一个正在写的文件。
    kind, why = compression_state(name)
    if kind:
        out["ok"] = True
        out["reason"] = "跳过：%s（%s）" % (why, kind)
        return out
    plan = audiofile.plan_for_meeting(folder)
    if plan["error"]:
        out["reason"] = plan["error"]
        return out
    if not plan["compressible"]:
        # 没有任何**可压**的段：可能是"已经压过了"（幂等：不重复压、不报错），
        # 也可能是"全坏了"。两者必须分开报 —— 把"全坏了"也说成"没问题"，
        # 用户永远不会知道盘上有段音频是截断的。
        out["ok"] = not plan["broken"]
        if plan["broken"]:
            out["reason"] = "；".join("%s %s" % (f, r) for f, r in plan["broken"][:3])
        else:
            out["reason"] = "已经是压缩状态，无需处理"
        info = compression_info(name)
        if info:
            out["beforeBytes"] = info["beforeBytes"]
            out["afterBytes"] = info["afterBytes"]
        return out

    for path in plan["compressible"]:
        res = audiofile.compress_segment(path, keep_raw=keep_raw)
        seg_name = os.path.basename(path)
        if not res["ok"]:
            # **任何一步不过 → 保留原件 + 明确报错**（`compress_segment` 已经保证
            # 原件还在：它只在写出 flac 且逐样本校验通过之后才删 wav）。
            out["failures"].append({"file": seg_name, "reason": res["reason"]})
            db.add_log("error", "meeting",
                       "%s %s 压缩失败，**原件已保留**：%s" % (name, seg_name, res["reason"]))
            continue
        out["beforeBytes"] += res["before"] or 0
        out["afterBytes"] += res["after"] or 0
        if res["deleted"]:
            out["deletedSegments"] += 1
        if res.get("verified") == "existing":
            continue
        out["segments"].append({
            "file": seg_name,
            "flac": os.path.basename(res["flac"]),
            "before": res["before"], "after": res["after"],
            "percent": audiofile.saving_percent(res["before"], res["after"]),
            "verified": res.get("verified") or "",
            "deleted": bool(res["deleted"]),
        })
        # 逐段留痕：压了什么、省了多少、验证结果、有没有删原件 —— 四件事缺一不可。
        db.add_log("info", "meeting",
                   "音频压缩 %s/%s → %s：%s → %s（省 %d%%，校验=%s，原件%s）"
                   % (name, seg_name, out["segments"][-1]["flac"],
                      audiofile.human_size(res["before"]),
                      audiofile.human_size(res["after"]),
                      out["segments"][-1]["percent"],
                      "逐样本一致" if res.get("verified") == "lossless" else (res.get("verified") or "未校验"),
                      "已删除" if res["deleted"] else "已保留"))

    out["savedBytes"] = max(out["beforeBytes"] - out["afterBytes"], 0)
    out["seconds"] = round(time.time() - t0, 1)
    # 有硬伤、压根没进"可压"清单的段也要算进失败（否则"压了好的 3 段、
    # 第 4 段是截断的"会报成完全成功）。
    for fname, reason in plan["broken"]:
        out["failures"].append({"file": fname, "reason": reason})
    # `ok` 的语义：**这一场要求的活是不是全都干成了**。
    # 部分失败（一段坏了、另一段压好）必须是 False —— 面板据此把失败原因显示出来；
    # 若报 True，用户只会看到"已压缩"，而有一段其实没压（甚至被截断），
    # 那种"看起来成功了"最难查。
    out["ok"] = not out["failures"]
    if out["failures"]:
        total = len(plan["compressible"]) + len(plan["broken"])
        head = "%d/%d 段压缩失败（**原件已保留**）" % (len(out["failures"]), total)
        out["reason"] = "%s：%s" % (
            head, "；".join("%s %s" % (f["file"], f["reason"]) for f in out["failures"][:3]))

    # 落盘：把真实的压缩前后字节写进 meta.json（**只在真的压了东西时写**，
    # 否则会把"上一场的记录"改写成空壳）。
    if out["segments"]:
        try:
            meta = _load_json(os.path.join(folder, "meta.json"), {})
            if not isinstance(meta, dict):
                meta = {}
            prev = _compression_summary(meta) or {}
            before = int(prev.get("beforeBytes") or 0) + out["beforeBytes"]
            after = int(prev.get("afterBytes") or 0) + out["afterBytes"]
            # 段列表同步成"盘上真实的段"（wav 已删、flac 已落地）—— `_transcribe_impl`
            # 优先读 `meta["segments"]`，不改它的话重转会去找那个已经被删掉的 wav。
            meta["segments"] = ["%02d%s" % (i, audiofile.FLAC_EXT)
                                if not os.path.isfile(os.path.join(folder, "%02d%s" % (i, audiofile.WAV_EXT)))
                                else "%02d%s" % (i, audiofile.WAV_EXT)
                                for i in audiofile.list_segments(folder)]
            meta["compression"] = {
                "at": datetime.datetime.now().isoformat(timespec="seconds"),
                "beforeBytes": before,
                "afterBytes": after,
                "savedPercent": audiofile.saving_percent(before, after),
                "deletedRaw": bool(prev.get("deletedRaw")) or out["deletedSegments"] > 0,
                "keptRaw": bool(keep_raw),
                "segments": len(audiofile.list_segments(folder)),
                "lastRun": {
                    "at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "beforeBytes": out["beforeBytes"],
                    "afterBytes": out["afterBytes"],
                    "deletedSegments": out["deletedSegments"],
                    "failures": out["failures"],
                    "seconds": out["seconds"],
                },
            }
            audiofile.write_meta(folder, meta)
            info = compression_info(name) or {}
            out["beforeBytes"] = info.get("beforeBytes", out["beforeBytes"])
            out["afterBytes"] = info.get("afterBytes", out["afterBytes"])
            out["totalSaved"] = max(out["beforeBytes"] - out["afterBytes"], 0)
            db.add_log("info", "meeting",
                       "%s 音频压缩汇总：原 %s → 现 %s（省 %d%%，删原件 %d 段，%s）"
                       % (name, info.get("beforeText") or "?",
                          info.get("afterText") or "?",
                          info.get("savedPercent") or 0,
                          out["deletedSegments"],
                          "保留原件" if keep_raw else "已删原件"))
        except Exception as e:
            # 记录写不进去不影响"音频已经压好"这个事实，但必须吼一声（否则面板
            # 永远显示不出"已压缩"，而用户以为没压成功）。
            db.add_log("warn", "meeting", "%s 压缩记录写入 meta.json 失败：%s" % (name, e))
    return out


# 转写进度：meeting_id -> {phase, seg_index, seg_total, percent, detail, updated_at}
_transcribe_progress = {}
_progress_lock = threading.Lock()


def _set_progress(meeting_id, **kw):
    with _progress_lock:
        _transcribe_progress[meeting_id] = {**_transcribe_progress.get(meeting_id, {}),
                                            **kw, "updated_at": time.time()}


def _clear_progress(meeting_id):
    with _progress_lock:
        _transcribe_progress.pop(meeting_id, None)


def transcribe_progress(meeting_id=None):
    """返回转写进度（无参会话返回全部）。"""
    with _progress_lock:
        if meeting_id is not None:
            p = _transcribe_progress.get(meeting_id)
            return dict(p) if p else None
        return {k: dict(v) for k, v in _transcribe_progress.items()}


def meeting_status():
    return {
        "active": _state["active"],
        "folder": os.path.basename(_state["folder"]) if _state["folder"] else None,
        "startedAt": _state["started_at"],
        "level": _state["level"],
        "error": _state["error"],
    }


def recover_orphaned_meetings():
    """启动恢复：进程重启后，数据库里残留的 recording/transcribing 会议
    已不可能仍在录/在转写（内存状态已丢失），统一标记为 interrupted。
    音频文件保留，可进会议详情手动「重新转写」。

    防误伤说明：重复实例（守护误判拉起、手动重复启动等）已在 app/main.py
    入口处通过「服务端口占用探测」拦截退出，根本走不到本函数 —— 能执行到
    这里的实例必然已成功绑定服务端口、是当前唯一的 ECHO 实例，因此 DB 里
    残留的 recording 会议一定是崩溃残留，可以安全标记。
    """
    try:
        n = 0
        for m in db.list_meetings(limit=500):
            if m["status"] in ("recording", "transcribing"):
                db.set_meeting_status_by_name(m["name"], "interrupted")
                n += 1
                db.add_log("warn", "meeting",
                           f"检测到中断的会议（进程重启），已标记 interrupted: {m['name']}")
        if n:
            db.add_log("info", "meeting", f"启动恢复：共标记 {n} 个中断会议")
    except Exception as e:
        db.add_log("warn", "meeting", f"会议状态恢复失败: {e}")


# ---------------------------------------------------------------- 录音

def start_meeting():
    with _state_lock:
        if _state["active"]:
            return False, "会议录音已在进行中"
        old = _state["recorder"]
        if old and old.thread and old.thread.is_alive():
            return False, "上次录音尚未退出，请稍后重试；持续异常请重启 ECHO"
        cfg = settings
        now = datetime.datetime.now()
        folder = os.path.join(meetings_dir(), now.strftime("%Y-%m-%d_%H-%M-%S"))
        os.makedirs(folder, exist_ok=True)

        meta = {
            "start": now.isoformat(timespec="seconds"),
            "config": {
                "sttModel": cfg.get("meetingSttModel", "small"),
                "sttDevice": cfg.get("device", "auto"),
                "segmentMinutes": cfg.get("meetingSegmentMinutes", 10),
                "autoSummarize": cfg.get("meetingAutoSummarize", True),
                "diarize": cfg.get("meetingDiarize", False),
            },
            "segments": [],
        }
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        meeting_id = db.create_meeting(
            os.path.basename(folder), started_at=meta["start"],
            stt_model=meta["config"]["sttModel"], stt_device=meta["config"]["sttDevice"],
            diarize=1 if meta["config"]["diarize"] else 0)

        # 2026-09-23（D1 单向抢占）：会议要开麦了，先请**正在收音**的语音指令让出。
        #
        # 为什么只允许这个方向：被中断的是一句 ≤30 秒的指令（重说一遍就行），
        # 而会议可能两小时 —— 反过来让指令打断会议就是白录一场
        # （AGENTS.md 里那次事故正是这类伤害）。见 docs/统一路由-模型能力与设备.md §3.6.1。
        #
        # 先等它自然收尾（多数情况用户已说完、几秒内就结束 → 用户无感、指令也不丢），
        # 超时才中止。麦克风只在收音期间被持有，转写/等 DSH 都不占麦。
        try:
            from app import assistant as _assistant
            if _assistant.yield_capture_for_meeting(timeout=3.0):
                db.add_log("info", "meeting",
                           "开始录音：上一条语音指令因让出麦克风被取消（等了 3 秒仍未收尾）")
        except Exception as e:                      # 抢占失败不该挡住会议
            db.add_log("warn", "meeting", f"让出麦克风的处理失败（不影响录音）：{e}")

        recorder = MeetingRecorder(
            folder,
            segment_minutes=int(cfg.get("meetingSegmentMinutes", 10)),
            device_id=resolve_input_device("meeting"),
            level_cb=lambda lv: _state.update(level=lv),
        )
        recorder.start()

        # 同步校验输入流是否真的打开：打不开就当场失败，避免界面显示"录音中"
        # 却一条音频都没录到（2026-09-16 空会议就是设备打不开后线程静默退出）。
        if not recorder.wait_started(timeout=6):
            err = recorder.error or "打开麦克风超时（设备被占用或权限不足）"
            stopped = recorder.stop()
            # 原因必须落库（`meetings.error`，面板直接显示它）：只写 status=error 时，
            # 面板只能自己编一句"麦克风没打开"，与真实原因（这里是具体的打开失败）
            # 对不上也没人知道。见 db.py 迁移 6。
            _mark_meeting_error(meeting_id, f"开始录音失败：{err}")
            _state.update(active=False, folder=None, recorder=None if stopped else recorder,
                          started_at=None, level=0.0, error=err)
            db.add_log("error", "meeting", f"开始录音失败（{os.path.basename(folder)}）：{err}")
            return False, f"无法开始录音：{err}"

        _state.update(active=True, folder=folder, recorder=recorder,
                      started_at=meta["start"], error="")
        threading.Thread(target=_watch_recorder, args=(recorder,), daemon=True).start()
        db.add_event("meeting_started", {"meeting": os.path.basename(folder), "id": meeting_id})
        db.add_log("info", "meeting", f"开始录音: {os.path.basename(folder)}")
        return True, os.path.basename(folder)


def _watch_recorder(recorder):
    """An unexpected device failure must not leave the UI claiming it is recording."""
    recorder.thread.join()
    with _state_lock:
        if _state["recorder"] is recorder and _state["active"]:
            recorder.error = recorder.error or "录音意外结束"
            stop_meeting()


def stop_meeting():
    with _state_lock:
        if not _state["active"]:
            return False, "没有进行中的会议"
        recorder = _state["recorder"]
        folder = _state["folder"]
        if not recorder.stop():
            err = "麦克风尚未释放，录音正在停止；请勿重复开麦，持续异常请重启 ECHO"
            _state.update(error=err, level=0.0)
            return False, err
        _state.update(active=False, level=0.0, recorder=None)

        meta_path = os.path.join(folder, "meta.json")
        meta = _load_json(meta_path, {})
        # 段列表 = 本次录出来的 **∪ 目录里已有的 *.wav**。
        # 为什么把目录里那些也算上（2026-09-23）：把**另一段录音**（比如上一场被中断的）
        # 拷进本场目录当 `00.wav`，就应当本次一并转写 —— 用户在电话里就是这么预期的
        # （"拷进去是不是结束后就自动转了"）。只认录音器自己的内存清单时，拷进去的
        # 那段会被**静默忽略**（`_transcribe_impl` 优先用 meta["segments"]，非空就不看目录）。
        extra = [f for f in audiofile.segment_files(folder)
                 if f.lower().endswith(audiofile.WAV_EXT)]
        segs = sorted(set(recorder.segments) | set(extra),
                      key=lambda n: int(str(n).split(".")[0]))
        meta["end"] = datetime.datetime.now().isoformat(timespec="seconds")
        meta["segments"] = segs
        meta["durationSeconds"] = sum(
            audiofile.audio_seconds(_resolve_seg_paths(folder, [s]).get(s) or
                                    os.path.join(folder, s))
            for s in segs)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _state["folder"] = None

        meeting = db.get_meeting_by_name(os.path.basename(folder))
        if recorder.error and segs:
            if meeting:
                db.update_meeting(meeting["id"], ended_at=meta["end"],
                                  duration_seconds=meta["durationSeconds"],
                                  segments=len(segs), status="interrupted",
                                  error=f"录音中断：{recorder.error}")
            err = f"录音中断：{recorder.error}；已保留音频，可手动重新转写"
            _state.update(error=err)
            db.add_event("meeting_stopped", {"meeting": os.path.basename(folder), "error": err})
            db.add_log("error", "meeting", err)
            return False, err
        if not segs:
            # 没录到任何音频：直接标 error，不再假装"转写中"（否则永远卡住，
            # 因为转写拿到 0 分段会立刻返回）。见 2026-09-16 的设备打开失败。
            err = (recorder.error if recorder else "") or "录音过程没有产生任何音频分段"
            if meeting:
                db.update_meeting(meeting["id"], ended_at=meta["end"],
                                  duration_seconds=0, segments=0, status="error",
                                  error=f"没有录到音频：{err}")
            _state.update(error=err)
            db.add_event("meeting_stopped", {"meeting": os.path.basename(folder), "error": err})
            db.add_log("error", "meeting",
                       f"录音结束但无音频（{os.path.basename(folder)}）：{err}")
            return False, f"没有录到音频：{err}"

        if meeting:
            # `error=""`：这一场开始（重新）转写了，上一次失败的原因不能再留在字段里
            # 冒充"本次的失败原因"（重新转写成功之后面板仍显示旧原因，是最难查的那种假象）。
            db.update_meeting(meeting["id"], ended_at=meta["end"],
                              duration_seconds=meta["durationSeconds"],
                              segments=len(segs), status="transcribing", error="")

        # 后台转写（不阻塞）
        threading.Thread(target=_transcribe_meeting, args=(folder,), daemon=True).start()
        db.add_event("meeting_stopped", {"meeting": os.path.basename(folder)})
        db.add_log("info", "meeting", f"停止录音，开始转写: {os.path.basename(folder)}")
        return True, os.path.basename(folder)


# ---------------------------------------------------------------- 转写

def _resolve_seg_paths(folder, segs):
    """`[段文件名, …]` → `{段文件名: 盘上实际存在的路径}`（找不到的**不放进去**）。

    判据只有一条、而且只写一份：`audiofile.resolve_segment()`（优先 `.wav`、其次 `.flac`）。
    为什么优先 wav：压缩是"先写 flac → 校验通过 → 删 wav"，两个都在意味着**收尾没完成**，
    这时以原件为准。播放那一路（`api.meeting_audio`）用的是同一个函数，
    "转写读的段"与"播放放的段"因此**不可能**不一致。
    """
    out = {}
    for seg in segs:
        try:
            idx = int(str(seg).split(".")[0])
        except (TypeError, ValueError):
            continue
        path = audiofile.resolve_segment(folder, idx)
        if path:
            out[seg] = path
    return out


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _wav_seconds(path):
    """任意会议音频段的时长（秒）；读不了返回 0。

    2026-09-26 起不改名、不改契约，但实现换成 `audiofile.audio_seconds()`：
    段可能是 `.flac`（历史音频无损压缩的产物），而标准库 `wave` **读不了 FLAC** ——
    继续用 `wave` 会让压缩过的会议在时间轴上退化成"每段 0 秒"（`_seg_duration_map`
    与导出、`build_segments` 全用它）。名字留着是为了不动那十几处调用点。
    """
    return audiofile.audio_seconds(path)


def _assign_speakers(seg_rows, turns):
    """说话人分离结果匹配到转写行：按覆盖时长取最长说话人（已实测验证）。"""
    if not turns:
        return [(seg, s, e, "", txt) for seg, s, e, txt in seg_rows]
    turns = sorted(turns, key=lambda x: x[0])
    out = []
    for seg_idx, s_start, s_end, text in seg_rows:
        coverage = {}
        for start, end, spk in turns:
            ov = min(s_end, end) - max(s_start, start)
            if ov > 0:
                coverage[spk] = coverage.get(spk, 0.0) + ov
        if coverage:
            speaker = max(coverage, key=coverage.get)
        else:
            mid = (s_start + s_end) / 2.0
            speaker = min(turns, key=lambda x: min(abs(mid - x[0]), abs(mid - x[1])))[2]
        out.append((seg_idx, s_start, s_end, speaker, text))
    return out


def _ensure_speaker_column(rows):
    """把转写行统一成 5 元组 ``(seg, start, end, speaker, text)``。

    文本优先引擎（SenseVoice / Qwen3-ASR）与 whisper/provider 路径产出的都是 **4 元组**，
    说话人那一列由 `_assign_speakers` 补。这里保证"补过了"这件事**一定发生**：

    原来只有 `if diarize: ... else: ...` 的 else 分支（= 关闭分离）会补空说话人，
    分离**抛异常**时什么都不做，4 元组就一路进到 `db.add_lines`，报
    ``ValueError: not enough values to unpack (expected 5, got 4)`` ——
    前面几十分钟的转写成果全丢（2026-09-23 实测：运行时缺 speechbrain）。
    形状是 db 层的契约，不能靠"分离恰好成功"来维持。
    """
    out = []
    for row in rows:
        if len(row) == 5:
            out.append(tuple(row))
        else:
            seg, s, e, txt = row
            out.append((seg, s, e, "", txt))
    return out


# `_align_sentences`（SenseVoice 文本对齐 whisper 骨架）已搬到
# `app/capabilities/assemble.align_sentences` —— 设计 §4.4：拼装规则只写一份。
# 那边是**原样搬过去**的（这段逻辑在实机上跑过不少会议，重写只会引入难查的时间轴退化）。


def _boot_meeting_stt(status, detail=""):
    """同步 boot 页 stt-meeting 组件状态（懒导入避免循环依赖）。"""
    try:
        import app.boot as boot
        boot.report("stt-meeting", status=status, detail=detail)
    except Exception:
        pass


def _boot_note_meeting_key():
    try:
        import app.boot as boot
        from app.audio import stt
        eng, model = stt.resolve_engine(settings.get("meetingSttModel", "sensevoice"))
        boot.note_stt_loaded("stt-meeting", stt.engine_key(eng, model))
    except Exception:
        pass


# ---------------------------------------------------------------- 引擎分派

#: 会议链路**整场文件转写**能直接驱动的本机引擎（= `meetingSttModel` 的合法引擎取值）。
#:
#: 判据只有一条：`_transcribe_impl` 里真的有一段代码把整个 wav 转成文字、再按
#: `app.capabilities.assemble` 拼成逐句时间。名字列在这里、而不是散在 if/elif 里，
#: 是为了让"驱动不了"那句报错能**自己说出支持哪些** —— 报错文案与分派逻辑必须是
#: 同一份事实，否则改了分派忘了改文案，用户又被指到错方向（2026-09-25）。
MEETING_LOCAL_ENGINES = ("whisper", "sensevoice", "qwen3asr", "sherpa")

#: 报错文案里逐条列出"支持哪些"时用的说法（给人看，不是给代码看）。
MEETING_ENGINE_LABELS = {
    "whisper": "whisper 的模型名（small / medium / large / large-v3 …）",
    "sensevoice": "sensevoice",
    "qwen3asr": "qwen3asr",
    "sherpa": "sherpa",
}


def unsupported_engine_reason(choice):
    """会议链路驱动不了的引擎 → 一句**要素齐全**的人话（哪个引擎 / 支持哪些 / 下一步）。

    三要素缺一不可，这是 2026-09-25 那次故障的教训：只报"转写失败"，用户不知道换什么；
    只说"不支持"，用户不知道支持什么；不给下一步，用户只能重复试同一件事。

    两条可执行的路都写在这里（`capabilityMeetingAsrBackend` **已经没有 auto**，
    所以措辞是"指定"，不是"等它自动挑"）：
      ① 把 `meetingSttModel` 改成下面列出的任一个；
      ② 到「能力」页签把「会议转写用哪个后端」指定为「ECHO 后端」，整场交给配对的机器。

    同时明说"这一场没有开始转写" —— 否则用户会以为是转了一半失败，去翻音频找问题。
    """
    who = str(choice or "").strip() or "（空）"
    supported = "、".join(MEETING_ENGINE_LABELS[e] for e in MEETING_LOCAL_ENGINES)
    return (
        "会议转写引擎「%s」不能用于整场会议的文件转写：这条链路只支持 %s。"
        "可执行的下一步二选一：① 到 设置 → 会议 把「会议转写引擎」改成上面任一个；"
        "② 到 设置 → 能力 把「会议转写用哪个后端」**指定**为「ECHO 后端」"
        "（capabilityMeetingAsrBackend=echo-server；这一项没有 auto，不会自动兜底），"
        "让整场转写交给配对的机器。这一场**没有开始转写**，原样重试也不会成功。"
        % (who, supported))


def resolve_meeting_engine(choice):
    """`meetingSttModel` → `(engine, model, problem)`。

    `problem` 非空 = **这条链路驱动不了这个取值**，调用方必须当场失败并把它原样报出去。

    为什么需要它（2026-09-25 用户报的真实故障，面板显示 0 行 + "麦克风没打开"）：
    原来这里是 ``use_sv = cfg.get("sttModel") in ("sensevoice", "qwen3asr")`` … ``else:
    _get_whisper(cfg.get("sttModel"))`` —— 于是安装器给"只装 sherpa"的默认档写的
    ``meetingSttModel=sherpa`` 会**拿引擎名当 whisper 模型名去加载**：`WHISPER_MODELS`
    里没有 sherpa，每段静默出 0 行，最后只落一个 ``status=error``、原因一个字都没有。
    现在这条路径不存在了。

    解析归 `stt.resolve_engine()`（与 boot、能力层本机后端 `capabilities/local.py`
    **同一份**规则，不再各写一份"哪些值是引擎名"）；whisper 的模型名再按
    `WHISPER_MODELS` 校验一次 —— `resolve_engine` 对认不出的值**一律回退成 whisper**，
    不校验就等于没判（"paraformer" 会变成"加载 whisper 模型 paraformer"）。
    """
    from app.audio import stt
    eng, model = stt.resolve_engine(choice)
    if eng not in MEETING_LOCAL_ENGINES:
        # 目前不可能（resolve_engine 只会出这四个），留个闸：真出了就响亮拒绝，
        # 绝不落到 `else` 里"拿未知名字当 whisper 模型"。
        return "", "", unsupported_engine_reason(choice)
    if eng == "whisper" and model not in stt.WHISPER_MODELS:
        return "", "", unsupported_engine_reason(choice)
    return eng, model, ""


def _mark_meeting_error(meeting_id, reason, **extra):
    """把一场会标成 `error` **并留下具体原因**（落 `meetings.error`，v6 加的那一列）。

    `reason` 是给人看的一句话，不是"失败"两个字：面板就显示它，接口契约见
    `app/api.py` 的 `GET /api/meetings`（列表）与 `GET /api/meetings/{mid}`（详情）。
    """
    if not meeting_id:
        return
    fields = {"status": "error", "error": str(reason or "")[:2000]}
    fields.update(extra)
    db.update_meeting(meeting_id, **fields)


class MeetingEngineRefused(Exception):
    """会议链路**驱动不了配置的引擎** —— 原因已经在库里/日志里/组件状态里说清楚了。

    为什么用异常、而不是让 `_transcribe_impl` 静悄悄 `return`：`_transcribe_meeting`
    把"正常返回"当成**转写完成**（报 `idle`、加载完成提示音"叮叮"）。守卫要表达的是
    "这一场**没有开始转写**"，如果只是 return，上层会紧接着喊一声"转写完成"、
    还响一个成功音 —— 与刚落库的失败原因、与刚才响过的错误音**自相矛盾**。
    """


def _maybe_auto_compress(folder):
    """「转写完成后自动压缩」开关（`meetingAutoCompressAudio`，**默认关**）。

    刻意做成**单场、前台**（不借 `compress_meetings()` 的整批后台任务）：
    这里刚转写完一场，用户要的是"这一场的空间收回来"，而不是顺手把几百场历史
    会议全压一遍（那会突然吃满 CPU/磁盘，而他没点过任何按钮）。
    整批那条路留给面板上的显式入口。

    `meetingKeepRawAudio` 在这里同样生效（勾着 = 只压不删）—— 语义只写一份，
    与 `compress_meetings()` 共用 `_compress_one_meeting()`。
    """
    try:
        if not bool(settings.get("meetingAutoCompressAudio", False)):
            return
        name = os.path.basename(folder)
        kind, why = compression_state(name)
        if kind:
            # 转写刚结束、状态还是 transcribing 时也会落到这里 —— 那种情况**不能压**
            # （`_transcribe_impl` 的 finally 才刚把文件句柄放开），如实说一句就好。
            db.add_log("debug", "meeting", "自动压缩跳过 %s：%s" % (name, why))
            return
        keep_raw = bool(settings.get("meetingKeepRawAudio", True))
        res = _compress_one_meeting(name, folder, keep_raw)
        if res.get("ok"):
            db.add_log("info", "meeting",
                       "自动压缩 %s：原 %s → 现 %s（%s，耗时 %.1fs）"
                       % (name, audiofile.human_size(res.get("beforeBytes") or 0),
                          audiofile.human_size(res.get("afterBytes") or 0),
                          "保留原件" if keep_raw else "已删原件", res.get("seconds") or 0.0))
        else:
            db.add_log("warn", "meeting",
                       "自动压缩 %s 未完成（原件已保留）：%s" % (name, res.get("reason")))
    except Exception as e:
        db.add_log("warn", "meeting", "自动压缩失败（不影响转写结果）：%s: %s"
                   % (type(e).__name__, e))


def _transcribe_meeting(folder):
    """后台转写主入口；任何异常写入日志，不静默丢失。"""
    name = os.path.basename(folder)
    meeting = db.get_meeting_by_name(name)
    mid = meeting["id"] if meeting else None
    _boot_meeting_stt("starting", f"转写中 {name}")
    try:
        _transcribe_impl(folder)
        if mid:
            _clear_progress(mid)
        services.report_meeting("idle", f"转写完成 {name}")
        _boot_note_meeting_key()
        _boot_meeting_stt("online", "转写完成 · 引擎已加载")
        tts_mod.beep_ok()          # 转写完成提示音（叮叮）
        # 可选：转写完成后自动做无损压缩（设置 `meetingAutoCompressAudio`，**默认关**）。
        # 放在提示音**之后**、且自己吞掉所有异常：压缩失败绝不能影响"这一场转写完成了"
        # 这件事，更不能把上一行的状态改回去（用户听到的是"叮叮"，界面必须是成功）。
        _maybe_auto_compress(folder)
    except MeetingEngineRefused:
        # 引擎驱动不了：原因、日志、组件状态、错误提示音都已经在 `_transcribe_impl`
        # 的守卫里做完了。这里唯一要做的是**别把"转写完成"接上去** ——
        # 否则用户同时看到"转写完成 · 引擎已加载"和一条失败原因，还听见两声提示音。
        if mid:
            _clear_progress(mid)
    except Exception as e:
        import traceback
        msg = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] " \
              f"转写异常: {e!r}\n{traceback.format_exc()}"
        db.add_log("error", "meeting", msg[:1500])
        # 落库的是**具体原因**（类型 + 消息），不是"转写失败"四个字：面板要显示它，
        # 而 `_transcribe_meeting` 是最后一道网 —— 走到这里说明上面哪一步炸了，
        # 原因只在这条异常里（`msg` 带时间戳与 traceback，太长，不适合当面板文案）。
        if meeting:
            _mark_meeting_error(meeting["id"],
                                f"转写异常：{type(e).__name__}: {e}")
        if mid:
            _clear_progress(mid)
        services.report_meeting("error", f"转写失败 {name}")
        _boot_meeting_stt("failed", f"转写失败 {name}")
        tts_mod.play_beep("err")   # 转写失败提示音（咚）
        print(msg, file=sys.stderr)


def _apply_capability_meta(meta: dict, cap_plan, cap_kinds) -> dict:
    """把这一场的路由结论写进 `meta`；**没走能力层时要把上一场的结论删掉**。

    为什么"删"这一步是必须的（2026-09-24 真机联调现场抓到）：`meta.json` 是**同一场会
    反复重转时被覆盖写的**，而这两行只在走能力层时才写。于是"上一次走了 ECHO 后端、
    这一次退回本机老路"（privacy 改成 none、或后端在计划阶段就用不上）之后，
    详情页上那行仍然是上一次的 `转写文本 → ECHO 后端` —— 而这一场其实是本机转的。
    `web/meeting.html` 里那句注释说得对：**"页签说会走 GPU、实际走了本机"这种对不上，
    比不显示更糟。**

    没走能力层时**不写一个"本机"占位**：面板那块在"没有数据"时是隐藏的，
    而这台机器为什么退回本机，日志里有那句"配了能力后端，但本场仍走本机引擎 —— 原因：…"。
    """
    if cap_plan is not None:
        meta["capability"] = cap_plan
    else:
        meta.pop("capability", None)
    if cap_kinds:
        meta["timestampsKinds"] = dict(cap_kinds)
    else:
        meta.pop("timestampsKinds", None)
    return meta


def _session_slots(cfg, need_speaker=False):
    """本场要向能力层要哪些槽。**顺序有讲究：说话人那一族按 `diarize.turns` → `speaker.embed`**。

    为什么要先 `diarize.turns`：`router.plan()` 的向量空间锁（L5）是**沿着槽的顺序**
    推进的 —— 第一个同源槽定下本场的 `vectorSpaceId`，后面的候选都按它重判。
    把 `speaker.embed` 排在前面，锁就由它定（同一个后端时结果一样，但语义上不对：
    这场会先有分离，才谈得上"认出的这个人是谁"）。

    `speaker.embed` 只在**声纹识别真的会用**时才要（`need_speaker`）：
    `_capability_diarize_segment` 用的是 `diarize.turns` 那条缝，本环节不消费
    `speaker.embed` —— 把它列进来只会在面板上多出一行"跳过了谁"，而那不是这场会的真相。
    """
    slots = ["asr.text", "asr.timestamps"]
    if bool(cfg.get("diarize")):
        slots.append("diarize.turns")
        if need_speaker:
            slots.append("speaker.embed")
    return tuple(slots)


def _skips_brief(plan, slot):
    """把某一槽的 `skipped` 压成一句人话：`echo-server(absent);local(unsupported)`。

    日志里要能一眼看出"为什么没用那个后端"，而 `Skipped.detail` 里那些长句子
    （"它只提供 asr.text"…）留在 `meta.json` 里给面板展开看，不进日志行。
    """
    return ";".join("%s(%s)" % (s.backend_id or "-", s.reason)
                    for s in plan.skipped if s.slot == slot) or "没配可用后端"


def _first_reason(plan, slot):
    """某一槽该报的降级原因（**权威十词之一**）。

    用 `router.most_informative()` 挑：计划里会同时留下好几条 `skipped`，
    其中 `absent`（"没配这个后端"）几乎必然出现，而真实原因可能是
    `unsupported` / `blocked` —— 报 `absent` 会把用户往错的方向指
    （他会去配一个**配了也没用**的后端）。挑不到就按 `absent`。
    """
    from app.capabilities.router import most_informative
    best = most_informative([s for s in plan.skipped if s.slot == slot])
    return best.reason if best else "absent"


class _CapabilitySession(object):
    """本场会议的能力路由会话：**一个 router + 一份槽清单 + 锁定的向量空间**。

    为什么要一个对象而不是原来那个 `(router, need)` 二元组：
    铁律 L5 要求"**同一场会议**不许混向量空间"，而 `router.call()` 是**每次调用各自
    规划一次**的 —— 不把上一次锁到的空间带过来，第二次调用就可能规划到另一个后端
    （`Need` 是 frozen 的，改不了，只能每次重建）。所以锁必须由调用方持有并传回去。

    它同时钉住"会议这边**不自己 new 客户端**"：所有调用都经由 `router`，
    `SAME_SOURCE_SLOTS` 的强制才不会被我绕过（任务里点名的那条）。

    `__iter__` 是为了兼容既有用法（`router, need = session`）—— 它曾经就是个二元组。
    """

    __slots__ = ("router", "cfg", "slots", "vector_space_id")

    def __init__(self, router, cfg, slots, vector_space_id=""):
        self.router = router
        self.cfg = cfg
        self.slots = tuple(slots)
        self.vector_space_id = str(vector_space_id or "")

    def need(self):
        """这一次调用要用的 `Need`（带上当前锁定的向量空间）。"""
        from app.capabilities import Need
        return Need(slots=self.slots, purpose="meeting",
                    privacy="", vector_space_id=self.vector_space_id)

    def plan(self):
        """先规划一遍（**不联网、不调用**）：用来判"这场要不要走能力层"。"""
        return self.router.plan(self.need())

    def local_slots(self):
        """每个槽的落点：`{槽: "local" | "empty"}` —— 只有**不归能力层**的槽在里面。

        为什么需要这个：会话是**按整场**建的（"有没有槽落在本机以外"），
        而"这段代码走哪条路"是**按槽**定的。只点名了分离的机器上，
        会话存在、但 `asr.text` 归本机 —— 那时转写必须走原来那段本地代码，
        而不是把 `asr.text` 交给能力层再失败一次（`router.call` 在没有本机客户端时
        只会抛 `absent`，整个转写就没了）。

        两种情形，**不要混为一谈**（`docs/能力路由` §5.1 专门把这两种分开）：

          * `"local"` —— 计划**明确**落在本机（用户点名 local；或本机客户端在那儿、
            默认链转到了它）。这是"他选的主选"，不是降级，日志按 info 记。
          * `"empty"` —— 计划里这一槽**谁都干不了**（没有注册的后端能提供它）。
            会议这边仍然回落到本机那段代码（总比整场空着强），但**必须留一条 warn
            带权威 `reason`** —— 用户会问"我配了后端，这场为什么没有说话人"，
            而答案是 `unsupported` / `blocked`（privacy 挡住）之类的具体原因。

        判据是"计划的候选池里有没有这个槽"，不是"有没有注册本机客户端"：
        本机没装引擎时它也注册着（`provides` 为空），拿它当"归本机"会掩盖真正的失败。
        """
        plan = self.plan()
        out = {}
        for slot in self.slots:
            backend = plan.backend_for(slot)
            if backend == "local":
                out[slot] = "local"
            elif plan.candidates.get(slot):
                continue                    # 有能干的 → 归能力层
            elif any(s.backend_id == "local" for s in plan.skipped if s.slot == slot):
                out[slot] = "local"         # 本机候选被跳过（例如它没装这个能力）
            else:
                out[slot] = "empty"
        return out

    def note_local_and_empty(self):
        """把"哪些槽不归能力层、为什么"写进日志。返回 `local_slots()` 的结果。

        放在这里而不是散在调用处：这句话**一场只该说一次**（8 段会议连说 8 遍
        会把日志淹掉），而空槽的原因正是排障要的第一手信息。
        """
        out = self.local_slots()
        for slot, kind in sorted(out.items()):
            if kind == "local":
                continue
            plan = self.plan()
            db.add_log("warn", "capability",
                       "本场 %s 走不了能力后端（reason=%s）：%s —— 这一槽回落本机"
                       % (slot, _first_reason(plan, slot), _skips_brief(plan, slot)))
        return out

    def call(self, slot, **kw):
        """按槽调用；成功后把锁推进到这次实际生效的向量空间。

        `plan.vector_space_id` 只在"这次真的选出了同源后端"时才有值
        （`router.plan` 里锁是**第一次拿到向量时**写进计划的），所以这里刻意
        不把空值写回来 —— 那会把已经锁好的空间抹掉，等于给跨空间回退开门。
        """
        result, plan = self.router.call(slot, self.need(), **kw)
        locked = str(getattr(plan, "vector_space_id", "") or "")
        if locked:
            self.vector_space_id = locked
        return result, plan

    def with_speaker_slot(self, slots):
        """换一份槽清单（判"要不要走能力层"时先不算声纹槽，判完再补上）。"""
        self.slots = tuple(slots)
        return self

    def __iter__(self):
        # 兼容 `router, need = session` 这种老写法（`need` 是**当场算出来的快照**，
        # 与 `call()` 里那份等价 —— 只要中间没有别的调用推进锁）。
        return iter((self.router, self.need()))


def _capability_asr_session(cfg, need_speaker=False):
    """本场是否走**能力后端**。返回 `_CapabilitySession` 或 `None`。

    ## 判据：计划里**任何一个会议槽**落到本机以外的后端

    早先这条只问 `asr.text`（step 3 只接了转写）。step 4 把分离也接上之后，
    "只问 asr.text"会漏掉一种真实配置：转写点名用本机、分离点名用 ECHO 后端
    （台式机有 GPU 转写、但没装 pyannote）。那种机器上，走哪一段代码**按槽分开**：
    `asr.text` 落本机 → 文本走原来那段本地代码；`diarize.turns` 落后端 → 分离走能力层。

    为什么不是"永远走路由器"：那会要求本机后端与原来那段代码**逐字节等价**，
    而那段代码包含 SenseVoice 文本 + whisper 骨架的对齐、qwen3asr 的原生句子、
    whisper 的 segments、以及各自的空结果留痕 —— 一次性替换它风险太高，
    收益也只是"代码好看一点"。**先把远端这条路打通**，本地那条等它被证明可靠再收。

    **没配后端（也没配对）时行为逐字不变**：所有槽都落到本机 → 返回 None →
    `_transcribe_impl` 走原来那段本地代码（含原来的 `diarize_wav_full`）。
    """
    try:
        from app.capabilities import build_default_router
        router = build_default_router()
        base_slots = _session_slots(cfg, need_speaker=False)
        session = _CapabilitySession(router, cfg, base_slots)
        plan = session.plan()
        # 带上声纹槽再规划一次（`need_speaker`）—— 判据只看"有没有落在本机之外"，
        # 多一个槽只会让计划更完整，不会把本机结果变成远端结果。
        if need_speaker and "diarize.turns" in base_slots:
            session.with_speaker_slot(_session_slots(cfg, need_speaker=True))
            plan = session.plan()

        live = [(s, plan.backend_for(s)) for s in session.slots]
        if not any(bid and bid != "local" for _s, bid in live):
            # 配了后端但这一轮用不上 —— **要说清楚为什么**，否则用户以为它在用后端，
            # 实际在啃本机 CPU，而现象只是"转写很慢"（本机那条路的日志一切正常）。
            # 最常见的两种：后端地址配了但连不上（capabilities 拉不回来 → 不支持任何槽）、
            # 或者 privacy 设成了 none。
            #
            # 判据是 `echo_server.configured()`（设置里填了地址**或配对过**），不是只看设置：
            # "只配对、什么都没配"从 §2.7 起就是能用状态，只看设置的话那台机器掉了后端
            # 会一声不响地退回本机 —— 而"不声不响"恰恰是这条告警要防的那件事。
            from app.capabilities import echo_server as _echo_backend
            if _echo_backend.configured():
                why = "；".join("%s=%s[%s]" % (s, b or "-", _skips_brief(plan, s))
                                for s, b in live)
                db.add_log("warn", "capability",
                           "配了能力后端，但本场仍走本机引擎 —— 原因：%s" % why)
                # 分离这一条单独吼一声：`asr.text` 有本机兜底（走原来那段代码），
                # 而 `diarize.turns` **没有**（§5.1）—— 用户以为配了就会有说话人，
                # 实际这一场一个说话人标签都不会有，而表现只是"分离没生效"。
                if "diarize.turns" in session.slots:
                    db.add_log("warn", "capability",
                               "本场不会标说话人：说话人分离没有本机兜底（设计 §5.1），"
                               "而 %s" % _skips_brief(plan, "diarize.turns"))
            return None
        who = "，".join("%s→%s" % (s, b) for s, b in live if b) or "（没有槽被选中）"
        db.add_log("info", "capability",
                   "本场会议走能力后端：%s%s" % (
                       who, "（向量空间 %s）" % plan.vector_space_id
                       if plan.vector_space_id else ""))
        return session
    except Exception as e:
        db.add_log("warn", "capability", f"能力路由不可用，本场回落本地引擎：{e}")
        return None


def _capability_skeleton(cap, seg_path, cfg):
    """从 `asr.timestamps` 槽要一份时间骨架。**要不到就当没有**（不抛、不假装）。

    为什么值得单独要一次：`asr.text` 与 `asr.timestamps` **可以是两个不同的后端**
    （设计 §4.2 的槽清单本来就这么分）。比如文本走网络服务商（它只给文本），
    而骨架走 ECHO 后端。要到了就 `assemble` 对齐（`aligned`），要不到就按字数均摊
    （`estimated`）—— 两种都在数据里标明。

    ⚠️ 这段会**把同一段音频再传一次**。所以只在 `asr.text` 没给句子时才走：
    像 ECHO 后端那种"一个模型同时给文本与时间戳"的情况，第一次调用就已经带回来了
    （`transcribe(want_timestamps=True)` 会传 `timestamps=1`），不该白跑一趟。
    """
    try:
        res, _plan = cap.call("asr.timestamps", wav=seg_path,
                              lang=cfg.get("sttLanguage", "zh"), want_timestamps=True)
        return res.sentences
    except Exception as e:
        db.add_log("debug", "capability", f"没拿到时间骨架，按字数均摊：{e}")
        return ()


def _capability_segment_rows(cap, seg_path, cfg, seg_idx, seg_min, cap_kinds):
    """一段音频走能力后端 → `(seg_rows, plan_dict, got)`。

    **刻意抽出来**，不塞在 `_transcribe_impl` 的大循环里：验收这段逻辑需要构造
    "一场会议 + meta.json + 一个库 + 一个 wav"，而它自己只依赖
    `(会话, wav 路径, 几个设置值)`。混在那个 350 行的函数里测，
    夹具就得把 `db`、`meta`、导出、后台线程全桩掉 —— 实测那样会**污染后面的测试文件**
    （Windows 上删不掉临时库、`database is locked`），而且报错出现在别人那里。

    `cap` 是 `_CapabilitySession`（原来是 `(router, need)` 二元组；改成对象是为了让
    `asr.text` 与 `diarize.turns` **共用同一个向量空间锁**，见 `_CapabilitySession`）。

    `cap_kinds` 是就地累加的档位计数（`{exact: 3, estimated: 1}`），最终写进 `meta.json`。
    """
    lang = cfg.get("sttLanguage", "zh")
    seg_sec = _wav_seconds(seg_path) or seg_min * 60.0
    res, plan = cap.call("asr.text", wav=seg_path, lang=lang, want_timestamps=True)
    # 后端自己给了句级时间轴就直接用；没给就去 `asr.timestamps` 槽要骨架
    # （**可能与文本是不同的后端** —— 那正是槽清单分开的意义）。
    skeleton = () if res.sentences else _capability_skeleton(cap, seg_path, cfg)
    got = assemble.assemble(text=res.text, sentences=res.sentences,
                            skeleton=skeleton, seg_seconds=seg_sec)
    cap_kinds[got.timestamps] = cap_kinds.get(got.timestamps, 0) + 1
    return [(seg_idx, st, en, txt) for st, en, txt in got.sentences], plan.as_dict(), got


def _normalize_diarize(result):
    """把能力层的 `DiarizeResult` 归一成 `diarize_wav_full()` 的形状。

    返回 `(turns, embs, labels)`：
      * `turns`  —— `[(start, end, 局部标签), …]`（与 pyannote 同形）
      * `embs`   —— `(n, dim)` float32，**numpy 数组**（`SpeakerRegistry` 与
                    `voiceprint.identify` 直接对它做 `np.stack` / 索引）
      * `labels` —— `list[str]`，且**每个标签都能在 embs 里找到下标**
                    （`registry.map(embs, labels)` 会按 `labels[i]` 取名字）

    两处坑，都在这里挡住：

    ① **局部标签不一定是 `SPEAKER_xx`。** 契约只保证"这个字符串在本次响应内标识一个
       说话人"（`base.DiarizeResult` 的注释就是这么写的）。所以这里不解析、不改写标签，
       原样交给 `SpeakerRegistry` —— 它只把标签当字典的键，显示名（`说话人N`）由它自己出。

    ② **嵌入可能是稀疏的。** `speakers` 与 `labels` 是两条信息，只有"对得上"时
       第 i 个嵌入才属于第 i 个标签。这里**按标签取嵌入**（`speakers[k]`），
       缺谁就不给谁 —— 宁可少一个人，也不要给错人的向量（比错的后果是认错人且不报错）。
    """
    import numpy as np

    turns = [(float(a), float(b), str(s)) for a, b, s in (result.turns or ())]
    speakers = {str(k): v for k, v in (result.speakers or {}).items()}
    labels = [str(k) for k in speakers]
    dim = int(result.dim or 0)
    if not dim and labels:
        dim = len(speakers[labels[0]] or ())
    if not labels or dim <= 0:
        # 没有嵌入：给一个**形状合法**的空数组（0 行、dim 列），
        # 让 `registry.map(embs, labels)` 走它自己的 n == 0 分支而不是崩在 `axis=1` 上。
        return turns, np.zeros((0, max(dim, 1)), dtype=np.float32), []
    embs = np.asarray([speakers[k] for k in labels], dtype=np.float32).reshape(
        len(labels), dim)
    return turns, embs, labels


def _capability_diarize_segment(cap, seg_path):
    """一段音频走能力层的 `diarize.turns` → `(turns, embs, labels, plan_dict)`。

    形状与 `diarize_wav_full()` **逐字对齐**（见 `_normalize_diarize`）—— 会议那边
    落库/合并/声纹识别那几段代码**一行都不用改**，这正是 step 4 敢接的前提。

    四种返回要分清（前三种调用方走原来那段本地代码）：

      * 本场压根不做分离（槽不在会话里）→ 全 `None`；
      * 计划把 `diarize.turns` 派给本机（**用户显式选的本机**，不是兜底）→ 全 `None`；
      * 能力层这一槽失败 → 全 `None`（+ 一条带 `reason` 的 warn），**不冒充**成功；
      * 拿到结果 → `(turns, embs, labels, plan.as_dict())`。

    为什么失败之后**还允许**调用方落回本机那段代码：`_capability_asr_session` 的判据
    已经把"没配后端"的机器挡在外面了（那些机器根本进不到这里）；能进到这里而这一槽
    失败的情形只有"配了后端但这一槽用不了"（后端没这个模型 / privacy 挡住 / 熔断）。
    那时**回落到用户自己装了的本机引擎**是 §5.1 允许的"他选的主选"，不是被取消的那种
    "自动兜底"；而且失败原因已经写进日志，不会变成"静默降级"。
    """
    if "diarize.turns" not in cap.slots:
        return None, None, None, None
    if "diarize.turns" in cap.local_slots():
        # 这一槽的活不归能力层（用户点名了本机，或这一槽谁都干不了）。
        # **这里刻意不写 warn**：那句话说一次就够（`note_local_and_empty()` 在开会话时
        # 已经说过了，带权威 reason），8 段会议连说 8 遍只会把日志淹掉。
        # 调用方据此走原来那段 `diarize_wav_full` 代码 —— 与今天逐字一致。
        return None, None, None, None
    try:
        res, plan = cap.call("diarize.turns", wav=seg_path)
    except Exception as e:
        reason = getattr(e, "reason", "") or "error"
        db.add_log("warn", "capability",
                   "说话人分离这一槽走不了能力后端（reason=%s）：%s" % (reason, e))
        return None, None, None, None
    turns, embs, labels = _normalize_diarize(res)
    db.add_log("debug", "capability",
               "本段说话人分离来自 %s（向量空间 %s，%d 个说话人）"
               % (res.provenance.backend_id or "?", res.vector_space_id or "?", len(labels)))
    return turns, embs, labels, plan.as_dict()


def _merge_capability_plans(*plans):
    """把几份执行计划合成一份写进 `meta.json`（后给的槽覆盖先给的）。

    为什么要合：`asr.text` 与 `diarize.turns` 是**两次独立的 `router.call()`**，
    各自返回的计划里只有"这次实际用了谁"。只写其中一份，面板上就会缺一个槽 ——
    而"这场会到底用了谁"正是这个字段存在的唯一理由。

    只做浅合并，**不做业务判断**：`picks` 按槽覆盖，`skipped` 去重后保留全部
    （排障时"谁被跳过、为什么"越多越好），`vectorSpaceId` 取最后一份非空的。

    传进来的可能是 `as_dict()` 出来的字典（也可能有 `None`），一律容错 ——
    它是往盘上写的路径，坏一个字段不该让整场转写挂掉。
    """
    out = {"picks": {}, "skipped": [], "notes": []}
    seen_skips = set()
    for plan in plans:
        if not isinstance(plan, dict):
            continue
        picks = plan.get("picks")
        if isinstance(picks, dict):
            out["picks"].update(picks)
        for item in (plan.get("skipped") or []):
            if not isinstance(item, dict):
                continue
            key = (item.get("slot"), item.get("backendId"), item.get("reason"))
            if key in seen_skips:
                continue
            seen_skips.add(key)
            out["skipped"].append(item)
        if plan.get("vectorSpaceId"):
            out["vectorSpaceId"] = plan["vectorSpaceId"]
        notes = plan.get("notes")
        if isinstance(notes, list):
            out["notes"].extend(str(n) for n in notes)
        cands = plan.get("candidates")
        if isinstance(cands, dict):
            out.setdefault("candidates", {}).update(cands)
    if not out["notes"]:
        out.pop("notes")
    return out


def _fallback_sv_rows(sv, wmodel, seg_path, seg_idx, seg_min, cfg):
    """回退路径：whisper 时间戳骨架 + SenseVoice 文本字符级对齐切句（保留句级时间戳）。

    对齐那一步的实现已搬到 `app.capabilities.assemble`（设计 §4.4：拼装规则只写一份）。
    这里保留原有的"三段兜底"顺序 —— 骨架 + 文本 → 骨架 → 整段一行 ——
    但**交给拼装层统一判**，并把精度档位带出来（见 `_transcribe_impl` 里写进 meta 的那处）。
    """
    from app.capabilities import assemble
    wsegs = []
    try:
        out, _info = stt_mod.transcribe_whisper(wmodel, seg_path, cfg.get("sttLanguage", "zh"))
        wsegs = [(s.start, s.end, s.text.strip()) for s in out]
    except Exception as e:
        print("whisper 时间戳骨架失败:", e, file=sys.stderr)
    sv_text = ""
    try:
        res = sv.generate(input=seg_path, cache={}, language="auto", use_itn=True, batch_size_s=60)
        if res:
            sv_text = re.sub(r"<\|[^|]*\|>", "", res[0].get("text", "") or "").strip()
    except Exception as e:
        print("SenseVoice 转写失败:", e, file=sys.stderr)
    got = assemble.assemble(text=sv_text, skeleton=wsegs, seg_seconds=seg_min * 60.0)
    return [(seg_idx, st, en, txt) for st, en, txt in got.sentences]


def _sherpa_rows(seg_path, seg_idx, seg_min, cfg, cap_kinds):
    """sherpa 整段转写 → 逐句行；返回 `(rows, why)`（`why` 非空 = 这段没转出东西的原因）。

    为什么这么短：sherpa 的整文件转写路径**本来就有**（`stt.transcribe_ex(engine="sherpa")`，
    它区分 ok/empty/error/missing），会议这边缺的只是"调它 + 把整段文本拼成逐句时间"。

    为什么**不去借 whisper 时间骨架**（SenseVoice 那条路借了）：骨架要 `_get_whisper("small")`，
    而 sherpa 恰恰是"这台机器上没装 whisper/funasr"时的引擎（安装器给只装 sherpa 的默认档
    写的就是它）—— 在那台机器上骨架必然拿不到。借了的话，同一份设置在不同机器上会给出
    不同的时间轴档位（`aligned` vs `estimated`），而档位是要如实进 `meta.json`、上详情的。
    所以这里固定走"按字数均摊"，档位 `estimated` —— 与 provider 转写、以及 SenseVoice
    拿不到骨架时**同一个机制**（`assemble.assemble`），不另写一套。

    `cap_kinds` 是就地累加的档位计数（`{exact: 3, estimated: 1}`），最终写进 `meta.json`。
    """
    from app.capabilities import assemble
    out = stt_mod.transcribe_ex(seg_path, engine="sherpa",
                               lang=cfg.get("sttLanguage", "zh"))
    text = str(out.get("text") or "").strip()
    if not text:
        # `detail` 是引擎自己说的话（缺依赖 / 模型没就位…），优先用它 —— 只写"空结果"
        # 会把"这段没人说话"和"sherpa 根本没装上"又混成一样，那正是要修掉的病。
        return [], str(out.get("detail") or out.get("status") or "sherpa 返回空结果")
    got = assemble.assemble(text=text, seg_seconds=_wav_seconds(seg_path) or seg_min * 60.0)
    cap_kinds[got.timestamps] = cap_kinds.get(got.timestamps, 0) + 1
    return [(seg_idx, st, en, txt) for st, en, txt in got.sentences], ""


def _active_asr_provider():
    """会议转写是否走 provider（P5）。**只有用户显式配了 `providerAsr` 才返回实例**。

    判据本身在 `app.providers.asr_if_configured()`（命令口述转写也用它 —— 同一条规则
    只写一份）。这里只做日志与"不可用就回落本地引擎"的包装。
    """
    from app import providers as providers_mod
    inst, why = providers_mod.asr_if_configured()
    if inst is None and "未配置" not in why:
        db.add_log("warn", "meeting", why)
    return inst


def _asr_provider_id():
    from app.config import settings
    return str(settings.get("providerAsr", "") or "").strip()


def _split_provider_text(text, seg_dur):
    """把外部转写返回的整段文本按句切分，并按字数在段时长内均摊时间。

    **实现已搬到 `app.capabilities.assemble.estimate_sentences`**（设计 §4.4：
    拼装规则只写一份）。这里留成薄壳，因为它是被用例钉住的既有接口
    （`tests/test_asr_provider.py` 四条）—— 换实现不改契约。

    为什么要搬：同一个"整段文本怎么变成逐句时间"的问题，本地引擎那条路
    （`_fallback_sv_rows`）也有一份自己的做法。两份各自演化就会出现
    "同一场会议里，A 段时间轴一个精度、B 段另一个精度，而面板上看不出区别"。
    """
    from app.capabilities import assemble
    return assemble.estimate_sentences(text, seg_dur)


def _transcribe_impl(folder):
    meta = _load_json(os.path.join(folder, "meta.json"), {})
    # 重新转写用「当前设置」，meta.json 快照仅作兜底（录音时的配置可能已过期）
    mcfg = meta.get("config", {}) or {}
    cfg = {
        "sttModel": settings.get("meetingSttModel", mcfg.get("sttModel", "small")),
        "sttDevice": settings.get("device", mcfg.get("sttDevice", "auto")),
        "sttLanguage": settings.get("sttLanguage", mcfg.get("sttLanguage", "zh")),
        "segmentMinutes": int(settings.get("meetingSegmentMinutes",
                                           mcfg.get("segmentMinutes", 10))),
        "autoSummarize": bool(settings.get("meetingAutoSummarize",
                                           mcfg.get("autoSummarize", True))),
        "diarize": bool(settings.get("meetingDiarize", mcfg.get("diarize", False))),
    }
    # 段发现：`meta["segments"]` 优先（录音当时的快照，含主人手工拷进来的段），
    # 兜底走 `audiofile.segment_files()` —— 它**同时认 `.wav` 与 `.flac`**，
    # 否则"全压成 flac 之后 meta 丢了"的会议会被当成"这场没有音频"（转写直接不开始）。
    segs = sorted(meta.get("segments", []) or audiofile.segment_files(folder),
                  key=lambda n: int(str(n).split(".")[0]))
    meeting_name = os.path.basename(folder)
    meeting = db.get_meeting_by_name(meeting_name)
    if not segs:
        # 兜底：没有音频无法转写，状态不能停在 transcribing（会永远卡住）
        if meeting:
            reason = "这场会没有音频分段，无法转写（录音目录里没有 0*.wav / 0*.flac）"
            _mark_meeting_error(meeting["id"], reason)
            db.add_log("error", "meeting", f"{reason}：{meeting_name}")
        return
    if not meeting:
        return
    meeting_id = meeting["id"]
    db.clear_meeting_lines(meeting_id)
    db.update_meeting(meeting_id, status="transcribing", error="")

    # 进度初始化（面板据此显示第 N/M 段 + 阶段）
    seg_total = len(segs)
    _set_progress(meeting_id, phase="准备模型", seg_index=0, seg_total=seg_total,
                  percent=0, detail=f"共 {seg_total} 段")

    # 段号 → 盘上**实际存在**的音频文件：历史音频压成 FLAC 之后这里给的是 `.flac`。
    # 为什么一次算好、后面各处都用它：
    #   * 播放那一路（`api.meeting_audio`）按同一个规则找文件（`audiofile.resolve_segment`）；
    #   * 分离（`diarize_wav_full`）、能力后端（`wav=seg_path`）拿到的必须**能读**；
    #   * 时间轴的 `audio_seconds()` 也要认 flac（`_wav_seconds` 已经是它了）。
    seg_paths = _resolve_seg_paths(folder, segs)
    segs = sorted(seg_paths, key=lambda n: int(str(n).split(".")[0]))
    seg_total = len(segs)

    db_rows = []
    speaker_names = {}
    seg_min = int(cfg.get("segmentMinutes", 10))
    diarize = bool(cfg.get("diarize", False))

    registry = None
    if diarize:
        try:
            # 这里仍然要 import，因为**本机分离那条路**（第一步没配后端）继续用它：
            # 能力层只是"有后端时"的另一条缝，不是替换（见 `_capability_diarize_segment`）。
            # `diarize_wav_full` 本身在下面按需导入（step 4 起它不再无条件执行）。
            from app.audio.diarize import SpeakerRegistry
            registry = SpeakerRegistry()
        except Exception as e:
            print("说话人分离模块不可用，跳过:", e, file=sys.stderr)
            diarize = False

    # 声纹识别（常用联系人，issue #6）：库里已有联系人样本时启用。
    # vp_names 整场累计「说话人N → 联系人名」（取相似度最高的一次），
    # vp_merges 记录同一联系人被分成多簇时的合并（大编号并进小编号）。
    vp_matcher = None
    vp_names = {}
    vp_merges = {}
    # 声纹判定统计：整场汇总成一行日志 —— 既避免"静默不认人"（真实故障看不出来），
    # 也是校准阈值/间隔的依据（最高相似度 + 未命中原因分布）。
    vp_stats = {"tried": 0, "hit": 0, "best": 0.0, "best_name": "", "miss": {}}
    if diarize:
        try:
            from app import voiceprint
            if voiceprint.enabled():
                vp_matcher = voiceprint.load_matcher()
                if vp_matcher:
                    db.add_log("debug", "voiceprint",
                               f"{meeting_name}：声纹库已加载"
                               f"（{db.count_voiceprint_contacts()} 位联系人）")
        except Exception as e:
            db.add_log("warn", "voiceprint", f"声纹库不可用，跳过自动识别：{e}")

    # 本机引擎分派。**先解析、先判能不能驱动，再加载模型** —— 顺序本身就是这条修复的
    # 一半：驱动不了的引擎必须在任何模型被加载之前就响亮失败（见 resolve_meeting_engine）。
    #   文本优先引擎（SenseVoice / Qwen3-ASR）：Qwen3-ASR 用 ForcedAligner 原生句子+时间戳；
    #   SenseVoice 用 whisper 时间戳骨架 + 字符级对齐切句（保留句级时间戳）；
    #   sherpa 只给整段文本（无句级时间戳）→ 按字数均摊，档位 `estimated`。
    eng = ""               # 本场真正驱动的本机引擎（provider / 能力后端那条路用不到）
    eng_model = ""
    wmodel = None
    sv = None
    asr_provider = _active_asr_provider()          # P5：显式配了 providerAsr 才走在线/外部转写
    # 3.0 能力后端：**只有计划里至少一个会议槽落在本机以外**时才不是 None（见
    # `_capability_asr_session` 的判据）。与 providerAsr 的分工：providerAsr 是
    # "用户显式配了一个在线转写服务"（P5，既有）；能力路由是"按槽选后端"（3.0）。
    # **providerAsr 优先** —— 那是用户已经配好、且在跑的路径，不能被悄悄换掉。
    #
    # `need_speaker`：这一场**会不会真的用声纹板**（v2 的"识别说话人是谁"）。
    # 声纹开着但没有联系人样本时 `load_matcher()` 返回 None —— 那种情况下
    # 不该把 `speaker.embed` 列进计划（面板上会多一行"跳过了谁"，而那不是这场会的真相）。
    need_speaker = False
    if diarize and vp_matcher is not None:
        need_speaker = True
    cap_session = (None if asr_provider is not None
                   else _capability_asr_session(cfg, need_speaker=need_speaker))
    # 会话是**按整场**建的（"有没有槽落在本机以外"），而"这段代码走哪条路"要**按槽**定：
    #   * `asr.text` 归本机（用户点名 local，或这一槽没有可用后端）→ 文本走原来那段本地代码；
    #     **`diarize.turns` 仍可能走后端** —— 正是"台式机自己转写、分离发给 GPU"那种配置。
    #   * 反过来，转写走后端而分离归本机也一样。
    # 不这么分的话，只配了分离的机器上会拿 `asr.text` 去问一个没有本机客户端的路由，
    # 结果是一条 `absent` 错误、**整场转写一行都没有**。
    #
    # `note_local_and_empty()` 就在这个岔口上说一句话：哪些槽不归能力层、为什么
    # （空槽带权威 reason）。整场只在这里说一次。
    cap_local = (cap_session.note_local_and_empty() if cap_session is not None
                 else {})
    asr_is_local = cap_session is None or "asr.text" in cap_local
    if asr_provider is not None:
        # 走 provider 时**不加载本地引擎**（省显存/省时间；也正是"没有 GPU 也能转写"的意义）
        db.add_log("info", "meeting", "本场转写走 provider（不加载本地模型）：%s"
                   % _asr_provider_id())
    elif not asr_is_local:
        # 走远端能力后端时**同样不加载本地引擎** —— 这正是"办公本没有 GPU 也能转写"的意义。
        # 引擎留给"远端失败时回落本地"那条路按需加载（见循环里的兜底）。
        db.add_log("info", "meeting", "本场转写走能力后端（不加载本地模型）")
    else:
        eng, eng_model, eng_problem = resolve_meeting_engine(cfg.get("sttModel"))
        if eng_problem:
            # ① 驱动不了的引擎当场说清楚，绝不静默 ——
            # **此刻一个模型都还没加载**，库里留的是原因，不是"转写失败"四个字。
            db.add_log("error", "meeting",
                       f"{meeting_name} 无法开始转写：{eng_problem}")
            _mark_meeting_error(meeting_id, eng_problem,
                                duration_seconds=meta.get("durationSeconds", 0),
                                segments=len(segs))
            _clear_progress(meeting_id)
            _boot_meeting_stt("failed", eng_problem[:200])
            services.report_meeting("error", f"转写失败 {meeting_name}")
            tts_mod.play_beep("err")
            # 抛出去，别让上层以为"跑完了"（否则紧跟一句"转写完成" + 成功提示音）。
            raise MeetingEngineRefused(eng_problem)
        if cap_session is not None:
            db.add_log("info", "meeting",
                       "本场转写按计划走本机（分离那一槽才走后端）")
        if eng == "sensevoice":
            # 与改动前逐字一致：文本用 SenseVoice，时间骨架借 whisper small
            wmodel = stt_mod._get_whisper("small", cfg.get("sttDevice", "auto"))
            sv = stt_mod._get_sensevoice(cfg.get("sttDevice", "auto"))
        elif eng == "qwen3asr":
            wmodel = stt_mod._get_whisper("small", cfg.get("sttDevice", "auto"))
            sv = stt_mod._get_qwen3asr(cfg.get("sttDevice", "auto"), eng_model,
                                       forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B")
        elif eng == "sherpa":
            # 先加载（加载失败就在**写任何东西之前**当场报错，与另外三条路同一个纪律）。
            # 识别器本身不用往下传：`stt.transcribe_ex()` 取的是 `_get_sherpa()` 的
            # 进程内单例，这里拿到的是同一个对象，传下去只是多一个参数。
            # sherpa **没有句级时间戳**，也因此**不去借 whisper 骨架**：借了的话
            # "时间轴精度"就取决于这台机器上恰好装没装 whisper，而档位必须如实
            # （见 `_sherpa_rows` 的注释）。
            stt_mod._get_sherpa()
        else:
            wmodel = stt_mod._get_whisper(eng_model, cfg.get("sttDevice", "auto"))

    diarize_fail = ""      # 分离失败只记一次：8 段会议连说 8 遍会淹没日志
    #: 3.0：把"这次每个槽用了谁、跳过了谁、为什么"与时间轴档位**写进 meta.json**。
    #: 设计 §4.4 要求执行计划按会议生成一次并落盘 —— 否则"这次为什么走了本机"
    #: 事后完全查不出来（面板与导出都只能看到一个转写结果）。
    cap_plan = None
    asr_plan = None        # asr.text 那次调用留下的计划（`diarize.*` 没跑时用它）
    cap_kinds = {}
    # **用时解码（本次功能的关键一步）**：段可能是 `.flac`，而引擎（尤其 sherpa）
    # 只认 RIFF/WAV —— 这里把整场的 flac 一次解成临时 WAV，交给下面**所有**既有代码
    # （本机引擎 / provider / 能力后端 / 说话人分离都拿同一个 `seg_path`）；
    # `with` 退出时临时文件即刻删除（见 `audiofile.decoded_segments`）。
    # 放在循环外面而不是每段各来一次：`decoded_segments` 只扫一次临时目录、只收集一次
    # 删除清单；逐段解码会在 N 段上扫 N 次（几十分钟的会议犯不着）。
    with audiofile.decoded_segments(
            [(int(str(s).split(".")[0]), seg_paths[s]) for s in segs]) as decoded_items:
        seg_map = {idx: path for idx, path in decoded_items}
        for i, seg in enumerate(segs, start=1):
            seg_idx = int(seg.split(".")[0])
            # 解出来的临时 WAV（是 wav 的段就是原路径，不复制）
            seg_path = seg_map.get(seg_idx) or seg_paths[seg]
            percent = round(i / seg_total * 100) if seg_total else 0
            _set_progress(meeting_id, phase="转写中", seg_index=i, seg_total=seg_total,
                          percent=percent, detail=f"第 {i}/{seg_total} 段 · {cfg.get('sttModel', '')}")
            seg_rows = []
            if asr_provider is not None:
                # P5：外部/在线转写。没有词级时间戳，所以服务端返回的文本在本段时长内
                # 按句切分、按字数均摊时间（比"整段一行"更接近本地引擎的输出形状）。
                try:
                    out = asr_provider.transcribe(seg_path, lang=cfg.get("sttLanguage", "zh"))
                    text = (out.get("text") or "").strip()
                    if text:
                        seg_rows = [(seg_idx, st, en, txt) for st, en, txt in
                                    _split_provider_text(text, _wav_seconds(seg_path) or seg_min * 60.0)]
                    else:
                        # 空结果**显式留痕**：区分"这段没人说话"与"provider 出错"（§19 发现③）
                        why = out.get("reason") or "empty"
                        db.add_log("warn", "meeting",
                                   f"{meeting_name} 第{i}段转写为空（{why}）——本段不写行")
                except Exception as e:
                    db.add_log("error", "meeting",
                               f"{meeting_name} 第{i}段转写失败（provider）：{e}")
            elif not asr_is_local:
                # 3.0：文本走能力后端，时间轴由**拼装层**统一决定（设计 §4.4）。
                try:
                    seg_rows, plan_dict, got = _capability_segment_rows(
                        cap_session, seg_path, cfg, seg_idx, seg_min, cap_kinds)
                    asr_plan = plan_dict          # 最近一次调用的计划（跳过的项也在这里）
                    if not seg_rows:
                        # 空结果**显式留痕**（与本地那条路同一个纪律）：
                        # 区分"这段没人说话"与"后端出了问题"
                        db.add_log("warn", "meeting",
                                   f"{meeting_name} 第{i}段没有内容"
                                   f"（档位 {got.timestamps}）——本段不写行")
                except Exception as e:
                    db.add_log("error", "meeting",
                               f"{meeting_name} 第{i}段能力后端转写失败：{type(e).__name__}: {e}")
                    # **不在段内回落到本地引擎**：一场会议里"前几段走服务端、后几段走本机"
                    # 会让时间轴精度与文本风格前后不一致，而用户看不出来。
                    # 失败就留痕、本段不写行；整场是否重跑由人决定（见 retranscribe_meeting）。
            elif eng == "sensevoice" or eng == "qwen3asr":
                # Qwen3-ASR：优先用 ForcedAligner 原生时间戳（自然句子），失败回退 whisper 骨架对齐
                if eng == "qwen3asr":
                    lang_hint = stt_mod._LANG_MAP.get(str(cfg.get("sttLanguage", "zh")).lower(), None)
                    _full_text, sentences = stt_mod._qwen3asr_sentences(sv, seg_path, lang_hint)
                    if sentences:
                        seg_rows = [(seg_idx, st, en, txt) for st, en, txt in sentences]
                    else:
                        seg_rows = _fallback_sv_rows(sv, wmodel, seg_path, seg_idx, seg_min, cfg)
                else:
                    seg_rows = _fallback_sv_rows(sv, wmodel, seg_path, seg_idx, seg_min, cfg)
                if not seg_rows:
                    # **静默零行是这套流程最贵的失败**：引擎抛异常只 print 到 stderr，
                    # 库里一行不留，事后完全查不出"这场为什么是空的"
                    # （2026-09-21 71 分钟那场就是这样）。
                    db.add_log("warn", "meeting",
                               f"{meeting_name} 第{i}段没有转出任何文字"
                               f"（引擎 {eng}；引擎异常详情见 data/logs/echo-server.log.err）")
            elif eng == "sherpa":
                seg_rows, sherpa_why = _sherpa_rows(seg_path, seg_idx, seg_min, cfg, cap_kinds)
                if not seg_rows:
                    # 与上面两条本地路同一个纪律：空结果要留痕，并且把**引擎自己说的原因**
                    # （依赖缺失/模型没就位/接口变了）带出来 —— sherpa 是这个仓库里最容易
                    # "没装好却看起来在跑"的引擎（2026-09-23 实测过一次）。
                    db.add_log("warn", "meeting",
                               f"{meeting_name} 第{i}段没有转出任何文字（引擎 sherpa）：{sherpa_why}")
            elif eng == "whisper":
                try:
                    out, _info = stt_mod.transcribe_whisper(wmodel, seg_path, cfg.get("sttLanguage", "zh"))
                    seg_rows = [(seg_idx, s.start, s.end, s.text.strip()) for s in out]
                except Exception as e:
                    db.add_log("error", "meeting",
                               f"{meeting_name} 第{i}段 whisper 转写失败：{type(e).__name__}: {e}")
                    print("转写失败:", e, file=sys.stderr)

            if diarize:
                _set_progress(meeting_id, phase="说话人分离", seg_index=i, seg_total=seg_total,
                              percent=percent, detail=f"第 {i}/{seg_total} 段 · 分离说话人")
                try:
                    # 3.0（step 4）：分离先问能力路由的 `diarize.turns` 槽。
                    # 返回 None 的几种情况都退回**原来那段本机代码**（形状已经归一，见
                    # `_normalize_diarize`）：本场没有这个槽 / 这一槽按计划归本机 /
                    # 这一槽失败 / 本场压根没开会话（`cap_session is None` = 没配后端）。
                    # 失败时那条 warn 已经在 `_capability_diarize_segment` 里写过了 ——
                    # 所以这里**不再重复**报错，只是走本机（最坏情况与今天逐字一致）。
                    turns_raw = embs = labels = None
                    if cap_session is not None:
                        turns_raw, embs, labels, dia_plan = _capability_diarize_segment(
                            cap_session, seg_path)
                        if dia_plan:
                            cap_plan = dia_plan
                    if turns_raw is None:
                        from app.audio.diarize import diarize_wav_full
                        turns_raw, embs, labels = diarize_wav_full(seg_path)
                    label_map = registry.map(embs, labels)
                    key_map = {}
                    for plabel, disp in label_map.items():
                        num = re.sub(r"\D", "", disp)
                        key = "S" + num
                        key_map[plabel] = key
                        speaker_names[key] = disp
                    turns = [(s, e, key_map[spk]) for s, e, spk in turns_raw]
                    seg_rows = _assign_speakers(seg_rows, turns)
                    # 声纹识别：本段每个说话人找常用联系人，整场累计（取相似度最高的一次）
                    # 注意这里不需要"注册"：`diarize.turns` 那一次调用**同时带回了每个说话人
                    # 的嵌入**（`DiarizeResult.speakers`）—— 声纹用的就是它，与分离同源。
                    if vp_matcher is not None:
                        try:
                            from app import voiceprint
                            for disp, m in voiceprint.identify(embs, labels, label_map,
                                                               vp_matcher).items():
                                vp_stats["tried"] += 1
                                if float(m["sim"]) > vp_stats["best"]:
                                    vp_stats["best"] = float(m["sim"])
                                    vp_stats["best_name"] = m.get("name") or ""
                                if not m["ok"]:
                                    r = m.get("reason") or "?"
                                    vp_stats["miss"][r] = vp_stats["miss"].get(r, 0) + 1
                                    continue
                                vp_stats["hit"] += 1
                                old = vp_names.get(disp)
                                if old is None:
                                    db.add_log("info", "voiceprint",
                                               f"{meeting_name} 第{i}段：{disp} → {m['name']}"
                                               f"（相似度 {m['sim']:.2f}，次优 {m['runner']:.2f}）")
                                if old is None or m["sim"] > old[1]:
                                    vp_names[disp] = (m["name"], m["sim"])
                            if vp_names:
                                # 同人合并 + 把联系人名写进本场显示名（用户手改过的名字
                                # 由 replace_speakers 的「已有名优先」逻辑保留，不会被顶掉）
                                vp_merges = voiceprint.duplicate_merges(
                                    {d: v[0] for d, v in vp_names.items()})
                                for disp, (nm, _sim) in vp_names.items():
                                    tgt = vp_merges.get(disp, disp)
                                    speaker_names["S" + re.sub(r"\D", "", tgt)] = nm
                        except Exception as e:
                            db.add_log("warn", "voiceprint", f"声纹识别失败（跳过本段）：{e}")
                except Exception as e:
                    # 分离不可用不能连累整场转写：形状归一在下面统一做。失败原因也落库
                    # （原来只 print 到 stderr，日志里查不到"为什么这场没有说话人"）。
                    if not diarize_fail:
                        diarize_fail = f"{type(e).__name__}: {e}"
                        db.add_log("warn", "meeting",
                                   f"{meeting_name} 说话人分离不可用，本场不标说话人：{diarize_fail}")
                    print("说话人分离失败:", e, file=sys.stderr)

            # 形状归一必须在 extend 之前：分离成功给 5 元组，关闭/失败时这里是 4 元组，
            # 而 db.add_lines 只认 5 元组（见 _ensure_speaker_column 的说明）。
            seg_rows = _ensure_speaker_column(seg_rows)
            db_rows.extend(seg_rows)
            meta.setdefault("transcribed", []).append(seg)
            # 3.0：把这次"用了谁/跳过了谁/为什么"与时间轴档位落盘。
            # 为什么每次都写：转写可能中途崩/被重启，**已完成的段也要留下当时的路由结论**，
            # 否则事后只能看到"转了一半"，而不知道为什么后半段没走。
            # 两份计划要合起来看：`asr_plan` 有转写那一槽、`cap_plan`（分离那次调用）
            # 多了 `diarize.turns` —— 只写其中一份，面板上就会缺一个槽，
            # 而"这场会每个槽各用了谁"正是这个字段存在的唯一理由。
            if cap_plan and asr_plan:
                seg_plan = _merge_capability_plans(asr_plan, cap_plan)
            else:
                seg_plan = cap_plan or asr_plan
            _apply_capability_meta(meta, seg_plan, cap_kinds)
            with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

    # 声纹识别产物：① 同人说话人键合并（行改标到保留键）；② 留存各说话人平均声纹
    remap = ({"S" + re.sub(r"\D", "", d): "S" + re.sub(r"\D", "", t)
              for d, t in vp_merges.items()} if vp_merges else {})
    if vp_merges:
        db_rows = [(seg, s, e, remap.get(spk, spk), txt) for seg, s, e, spk, txt in db_rows]
        db.add_log("info", "voiceprint",
                   f"{meeting_name}：声纹识别合并同人说话人 {len(vp_merges)} 组"
                   f"（{'，'.join(f'{k}→{v}' for k, v in remap.items())}）")
    if diarize and registry is not None:
        try:
            from app import voiceprint
            # 声纹是生物特征：功能关闭时不留存任何样本（默认就是关，见 config.py 注释）。
            # 关闭态的代价：该场会议之后点「识别本场」需要重新转写（届时再开也一样）。
            if voiceprint.enabled():
                emb_map = {}
                for disp, (vec, cnt) in registry.snapshot().items():
                    key = "S" + re.sub(r"\D", "", disp)
                    if remap.get(key, key) != key:
                        # 该键已被并进别的说话人（行里已经没有它）：别再留"幽灵样本"，
                        # 否则声纹库里会出现指向不存在说话人的条目。
                        continue
                    blob, dim = voiceprint.pack(vec)
                    emb_map[key] = (blob, dim, cnt)
                if emb_map:
                    db.replace_speaker_embeddings(meeting_id, emb_map)
        except Exception as e:
            db.add_log("warn", "voiceprint", f"留存说话人声纹样本失败：{e}")

    if vp_stats["tried"]:
        # 整场一行汇总：认了没认、最高相似度多少、为什么没认 —— 校准阈值就看这行
        near = f"，最高相似度 {vp_stats['best']:.2f}"
        if vp_stats["best_name"]:
            near += f"（最接近「{vp_stats['best_name']}」）"
        miss_txt = "，".join(f"{k}×{v}" for k, v in sorted(vp_stats["miss"].items())) or "无"
        db.add_log("info", "voiceprint",
                   f"{meeting_name}：声纹判定 {vp_stats['tried']} 次，命中 {vp_stats['hit']} 次"
                   f"{near}；未命中：{miss_txt}（阈值/间隔可在 设置 → 会议 调整）")

    _set_progress(meeting_id, phase="整理结果", seg_index=seg_total, seg_total=seg_total,
                  percent=100, detail="写入数据库与导出转写文件")
    if speaker_names:
        db.replace_speakers(meeting_id, speaker_names)
    db.add_lines(meeting_id, db_rows)
    db.cleanup_empty_speakers(meeting_id)
    if db_rows:
        # `error=""`：转写成功了，上一次失败的原因必须清掉（否则面板会拿旧原因
        # 解释这一场的结果 —— 而那是最像"代码坏了"的一种假象）。
        db.update_meeting(meeting_id, status="transcribed",
                          duration_seconds=meta.get("durationSeconds", 0),
                          segments=len(segs), error="")
    else:
        # 「一行都没有也叫 transcribed」是假话：面板显示成功、纪要写着"没内容"，
        # 真正原因（引擎异常/依赖缺失）只在 stderr 里 —— 用户看到的是"成功了但空的"。
        # 2026-09-21 那场 71 分钟的会就是这么过去的（2026-09-23 复查发现）。
        reason = "没有转出任何文字（转写引擎失败，或这段录音确实没人说话）"
        db.update_meeting(meeting_id, status="error",
                          duration_seconds=meta.get("durationSeconds", 0),
                          segments=len(segs), error=reason)
        db.add_log("error", "meeting", f"{meeting_name} 转写结束但一行文字都没有：{reason}")
        try:
            _cur = db.get_meeting(meeting_id)
            _old = ((_cur["notes"] if _cur is not None else "") or "")
        except Exception:
            _old = ""
        # notes 是用户的地盘：只在它空着的时候写，绝不覆盖用户写的东西
        if not _old.strip():
            db.update_meeting(meeting_id, notes=reason)
    export_transcript(meeting_id, folder)

    if cfg.get("autoSummarize", True) and db_rows:
        _set_progress(meeting_id, phase="生成纪要", seg_index=seg_total, seg_total=seg_total,
                      percent=100, detail="已请求生成纪要+议题分段")
        request_summary(meeting_id, folder)
        request_topic_segments(meeting_id, folder)


def export_transcript(meeting_id, folder=None):
    """从 DB 导出 transcript.md（按段分组，带段标题；行时间戳为会议绝对时间）。"""
    if folder is None:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            return
        folder = os.path.join(meetings_dir(), meeting["name"])
    meeting = db.get_meeting(meeting_id) or {}
    speakers = {s["label"]: s["name"] for s in db.get_speakers(meeting_id)}
    lines = db.get_lines(meeting_id)
    start_ts = meeting.get("started_at", "")
    seg_dur = _seg_duration_map(folder)
    out = [f"# 会议转写 {start_ts}", ""]
    if not lines:
        out.append("（未检测到有效语音）")
    else:
        # 按段分组；段起始 = 之前所有段的时长累计（绝对时间）
        by_seg = {}
        for ln in lines:
            by_seg.setdefault(ln["seg_index"], []).append(ln)
        abs_offset = 0.0
        for seg_idx in sorted(by_seg):
            seg_lines = by_seg[seg_idx]
            dur = seg_dur.get(seg_idx, seg_lines[-1]["end"] if seg_lines else 0)
            seg_abs_start = abs_offset
            seg_abs_end = abs_offset + dur
            out.append(f"## 第 {seg_idx} 段 [{_fmt_ts(seg_abs_start)} - {_fmt_ts(seg_abs_end)}]")
            out.append("")
            for ln in seg_lines:
                spk = speakers.get(ln["speaker_label"], "") or ""
                t_abs = seg_abs_start + float(ln["start"] or 0)
                prefix = f"[{spk}] " if spk else ""
                out.append(f"{prefix}[{_fmt_ts_full(t_abs)}] {ln['text']}")
            out.append("")
            abs_offset = seg_abs_end
    with open(os.path.join(folder, "transcript.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def _fmt_ts(sec):
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _fmt_ts_full(sec):
    sec = int(sec or 0)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _seg_duration_map(folder):
    """段号 -> 该段音频总时长（秒）。

    2026-09-26 起按 `audiofile.resolve_segment()` 找文件：段可能是 `.flac`
    （历史音频无损压缩的产物），而 `.wav` 那时已经删了 —— 只认 wav 会让
    压缩过的会议在详情页/导出里**每段都显示 0 秒**（时间轴整体塌掉）。
    """
    out = {}
    if not os.path.isdir(folder):
        return out
    for idx in audiofile.list_segments(folder):
        path = audiofile.resolve_segment(folder, idx)
        if path:
            out[idx] = audiofile.audio_seconds(path)
    return out


def build_segments(meeting_id):
    """按段聚合转写行，供前端"自然段"视图渲染。

    返回 [{index, duration, start, end, speakers:[{label,name,count}], lines:[...]}]
    """
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return []
    folder = os.path.join(meetings_dir(), meeting["name"])
    seg_dur = _seg_duration_map(folder)
    lines = db.get_lines(meeting_id)
    by_seg = {}
    for ln in lines:
        by_seg.setdefault(ln["seg_index"], []).append(ln)
    out = []
    for seg_idx in sorted(by_seg):
        seg_lines = by_seg[seg_idx]
        spk_count = {}
        for ln in seg_lines:
            lbl = ln["speaker_label"] or "?"
            spk_count[lbl] = spk_count.get(lbl, 0) + 1
        speakers = [{"label": k, "name": k, "count": v} for k, v in
                    sorted(spk_count.items(), key=lambda x: -x[1])]
        duration = seg_dur.get(seg_idx, 0)
        start = seg_lines[0]["start"] if seg_lines else 0
        end = seg_lines[-1]["end"] if seg_lines else duration
        out.append({
            "index": seg_idx,
            "duration": round(duration, 1),
            "start": round(start, 1),
            "end": round(end, 1),
            "speakers": speakers,
            "lines": seg_lines,
        })
    return out


# ---------------------------------------------------------------- 声纹（常用联系人）

def recognize_meeting_speakers(meeting_id):
    """用声纹库给本场重新认人（面板「说话人管理 → 声纹识别」）。

    只用转写时留存的声纹样本，不重新分离、不重新转写；识别到联系人后
    改写说话人名并重导出 transcript.md。返回 voiceprint.recognize_meeting 的结果。
    """
    from app import voiceprint
    res = voiceprint.recognize_meeting(meeting_id)
    if res.get("renamed") or res.get("merged"):
        export_transcript(meeting_id)
    return res


# ---------------------------------------------------------------- 纪要

# 会议纪要会话缓存：meeting_id -> sessionId。
# 语义（2026-09-15 定稿）：**一场会议一个会话**，本场的纪要、分段、语义分段、
# 工作日志归档全部发进它；下一场会议新建。内存缓存之外还落库（db.meeting_sessions），
# 这样 ECHO 重启后不变，归档环节（worklog）也能拿到同一个会话。
_MEETING_SESSIONS = {}
_MEETING_SESSIONS_LOCK = threading.Lock()
# 同一会议的纪要/分段/语义分段请求需串行执行：并发发往同一会话时，
# wait_for_reply 会都抢到第一个完成的回复（整场纪要），导致分段输出被覆盖。
_MEETING_LOCKS = {}
_MEETING_LOCKS_LOCK = threading.Lock()


def _session_key(meeting_id):
    """把会议标识统一成 meeting_sessions 的主键（会议名）。

    调用方给的是 db 主键 int 的地方（如 delete_meeting）和给会议名的地方（如
    纪要流程）都有，这里统一转换，避免两套键各自为政、映射对不上。
    """
    if isinstance(meeting_id, int):
        m = db.get_meeting(meeting_id)
        return (m or {}).get("name") or str(meeting_id)
    return str(meeting_id)


def _summary_session(client, meeting_id):
    """解析本场会议的纪要会话（一会话贯穿整场）。

    优先级：
      1. 内存缓存（最快路径）
      2. 数据库映射（ECHO 重启后仍指向同一会话）
      3. 新建：**在该会议工作区里建**，让 DSH 把它登记进工作区（侧栏归组）。
         关键：session/create 只认 workspaceId 或 cwd 之一；用 workspaceId 建
         才会被登记，用 cwd 建会落到「未分组」。
      未配置 meetingWorkspace 时退回固定的「纪要会话」（旧行为）。
    """
    meeting_id = _session_key(meeting_id)
    # **3.0：会议数据目录 ≡ 会议工作区**（`paths.meeting_space_root()`）。原来这里直接读
    # `meetingWorkspace` 字符串，于是"用户改了「会议文件目录」"之后，DSH 会话还登记在
    # 老目录 —— 文件在 A、工作区在 B，DSH 看不到会议文件，而且没有任何地方会报错。
    # 老装机上"改过 meetingWorkspace、没改 meetingsDir"的值仍被尊重（见 paths 那个函数）。
    from app import paths as _paths
    ws = _paths.meeting_space_root()
    if not ws:
        return client.ensure_session("summary", name="纪要会话")

    with _MEETING_SESSIONS_LOCK:
        sid = _MEETING_SESSIONS.get(meeting_id)
    if sid:
        return sid

    row = db.get_meeting_session(meeting_id, agent=getattr(client, "name", ""))
    if row and row.get("session_id"):
        sid = row["session_id"]
        with _MEETING_SESSIONS_LOCK:
            _MEETING_SESSIONS[meeting_id] = sid
        db.touch_meeting_session(meeting_id)
        return sid

    # 新建：优先走工作区（保证出现在 DSH「会议工作区」分组里）
    sid, workspace_id, how = "", "", ""
    try:
        if getattr(client, "has_workspaces", lambda: False)():
            from app import workspaces as spaces_mod
            wid, created = client.ensure_workspace(
                ws, title=spaces_mod.title_for_path(ws))
            if wid:
                sid = client.create_session(workspace_id=wid)
                workspace_id = wid
                how = f"工作区 {wid}" + ("（新建）" if created else "（已存在）")
    except Exception as e:
        db.add_log("warn", "meeting", f"按工作区创建会议会话失败，回退 cwd 方式：{e}")
    if not sid:
        # 回退：老方式（会话可用，但 DSH 侧会落在「未分组」）
        sid = client.create_session(cwd=ws)
        how = "cwd（未登记工作区，侧栏可能显示未分组）"

    if sid:
        with _MEETING_SESSIONS_LOCK:
            _MEETING_SESSIONS[meeting_id] = sid
        try:
            db.upsert_meeting_session(meeting_id, sid, workspace_id,
                                      agent=getattr(client, "name", ""))
        except Exception as e:
            db.add_log("warn", "meeting", f"会议会话映射落库失败：{e}")
        db.add_log("info", "meeting",
                   f"本场会议新建 DSH 会话 {sid}（{how}）——纪要/分段/归档共用此会话")
    return sid


def _drop_summary_session(meeting_id, archive=True):
    """忘记本场会议的会话；archive=True 时同时在 DSH 侧归档该会话。

    归档后它不再出现在侧栏，也不会掉进「未分组」。
    """
    meeting_id = _session_key(meeting_id)
    with _MEETING_SESSIONS_LOCK:
        sid = _MEETING_SESSIONS.pop(meeting_id, None)
    row = None
    try:
        row = db.get_meeting_session(meeting_id)
    except Exception:
        row = None
    if not sid and row:
        sid = row.get("session_id") or ""
    if archive and sid:
        try:
            client = get_client()
            if getattr(client, "has_workspaces", lambda: False)():
                client.archive_session(sid, workspace_id=(row or {}).get("workspace_id") or "")
                db.add_log("info", "meeting", f"已归档会议会话 {sid}（{meeting_id}）")
        except Exception as e:
            db.add_log("warn", "meeting", f"归档会议会话失败（可忽略）：{e}")
    try:
        db.delete_meeting_session(meeting_id)
    except Exception:
        pass




def _quote_mm_text(raw):
    """mermaid 形状/标签文本：含半角括号等特殊字符但未用引号包裹时补双引号。
    已包裹（"..."）或无需包裹的原文原样返回。

    **引号标签里又嵌半角引号**是 LLM 的常见写法（`…过滤"他人说话"等…`）：内层引号会把
    标签提前闭合，mermaid 解析直接报错。老逻辑漏了这种——它只在"带括号"时才补引号，
    而这种文本里没有括号，于是一路放行（2026-09-20：最新一条会议的纪要就这么挂的）。
    处理办法：把**内层**引号换成 mermaid 实体 `#quot;`，渲染出来仍是 `"`。
    """
    t = raw.strip()
    if not t:
        return raw
    wrapped = len(t) >= 2 and t[0] == '"' and t[-1] == '"'
    if wrapped and t.count('"') == 2:
        return raw  # 已用引号包裹，且内部没有多余引号
    if wrapped and t.count('"') > 2:
        return '"' + t[1:-1].replace('"', "#quot;") + '"'
    if '"' in t:                       # 没加引号却带引号：同样会断解析，一并规范化
        return '"' + t.replace('"', "#quot;") + '"'
    if any(ch in t for ch in "()[]{}|"):
        return '"' + t + '"'
    return raw


def _scan_mm_shape(line, j, expect_close):
    """从 j 起扫描，直到栈空时遇到 expect_close，返回其索引；失败返回 None。
    括号只按嵌套配对，引号字符串整体跳过（视为文本一部分）。"""
    stack = []
    k = j
    n = len(line)
    while k < n:
        c = line[k]
        if c == '"':
            k2 = line.find('"', k + 1)
            if k2 == -1:
                return None
            k = k2 + 1
            continue
        if c in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[c])
        elif c in ")]}":
            if stack and stack[-1] == c:
                stack.pop()
            elif stack:
                return None  # 括号不匹配，放弃（保守不改该行）
            elif c == expect_close:
                return k
            else:
                return None
        elif c == expect_close and not stack:
            return k  # 非括号闭符（如边标签 |...| 的 |）
        k += 1
    return None


def _fix_flowchart_line(line):
    """修复一行 flowchart/graph 代码：给节点形状 / 边标签文本中含括号、
    竖线等特殊字符却未加引号的片段补双引号（CY[初验(出验)证书] → CY["初验(出验)证书"]）。
    解析不确定时整行保持原样，绝不做破坏性修改。"""
    out = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == '"':
            j = line.find('"', i + 1)
            if j == -1:
                out.append(line[i:])
                break
            out.append(line[i:j + 1])
            i = j + 1
            continue
        if c == "|":  # 边标签 |...|
            j = _scan_mm_shape(line, i + 1, "|")
            if j is None:
                out.append(line[i:])
                break
            out.append("|" + _quote_mm_text(line[i + 1:j]) + "|")
            i = j + 1
            continue
        if c in "[({":
            nxt = line[i + 1] if i + 1 < n else ""
            if c == "[" and nxt == "(":
                close, tstart = ")", i + 2
            elif c == "[" and nxt == "[":
                close, tstart = "]", i + 2
            elif c == "(" and nxt == "[":
                close, tstart = "]", i + 2
            elif c == "(" and nxt == "(":
                close, tstart = ")", i + 2
            elif c == "{" and nxt == "{":
                close, tstart = "}", i + 2
            else:
                close, tstart = {"[": "]", "(": ")", "{": "}"}[c], i + 1
            k = _scan_mm_shape(line, tstart, close)
            if k is None:
                out.append(line[i:])
                break
            raw = line[tstart:k].strip()
            quoted = _quote_mm_text(raw)
            out.append(line[i:tstart] + quoted + line[k])
            i = k + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fix_timeline_line(line):
    """修复一行 timeline 代码。

    2026-09-12 实测（用面板自带 mermaid 逐个渲染对照，见提交说明）：
      1) 标题必须是 `title: 文本`（冒号必填）。LLM 常写成 `title 文本`，缺冒号时
         解析器直接报 Expecting 'title',… got 'INVALID'。
      2) **周期文本里不能含冒号**：timeline 用 `:` 分隔"周期 : 事件"，所以
         `00:00:12 : 发起试音` 会被切错，报 Expecting 'period','event' got 'INVALID'。
         实测 `00.00.12 : …`（点号）与 `00-00-12 : …`（短横）都正常渲染，
         而 `{00:00:12}` / `"00:00:12"` 这类修饰写法不被支持。
    因此这里：补 title 的冒号；把行内"时间戳"（纯数字+冒号的序列，如 00:00:12）
    的冒号换成点号，其余部分（分隔符与事件文本里的冒号）保持不动。
    """
    m = re.match(r'^(\s*title)(\s+)(?!:)(.+)$', line)
    if m:
        line = f"{m.group(1)}: {m.group(3).strip()}"
    # 只替换"数字:数字(:数字…)"这种时间戳形态，避免误伤 "12:30 讨论" 之类的事件文本
    return re.sub(r'(?<![\d:])(\d{1,3}(?::\d{2}){1,3})(?![\d:])',
                  lambda mm: mm.group(1).replace(':', '.'), line)


def _fix_timeline_block(block_lines):
    """对整个 timeline 代码块做修复（逐行调用 _fix_timeline_line）。"""
    return [_fix_timeline_line(ln) for ln in block_lines]


def _sanitize_mermaid(md_text):
    """修复 LLM 生成的 markdown 中 mermaid 图表的渲染错误（防 Obsidian/网页报错）。
    处理 ```mermaid 代码块：
      - graph/flowchart：给含括号等符号的节点/边标签补引号（_fix_flowchart_line）
      - timeline：给缺冒号的 `title` 行补冒号（_fix_timeline_line）
    非代码区与其它图型的代码块原样保留。"""
    out_lines = []
    in_code = False
    kind_wait = False
    diagram_kind = ""
    for raw in md_text.split("\n"):
        line = raw
        s = line.strip()
        if s.startswith("```"):
            if not in_code:
                rest = s[3:].strip()
                if rest.startswith("mermaid"):
                    in_code = True
                    kind_wait = True
                    diagram_kind = ""
            else:
                in_code = False
                diagram_kind = ""
                kind_wait = False
            out_lines.append(line)
            continue
        if in_code:
            if kind_wait:
                kind_wait = False
                diagram_kind = s
            if re.match(r"^(graph|flowchart)\b", diagram_kind):
                out_lines.append(_fix_flowchart_line(line))
            elif re.match(r"^timeline\b", diagram_kind):
                out_lines.append(_fix_timeline_line(line))
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _meeting_parts(folder):
    """读取会议各记录来源，返回 (summary_src, topics_src, transcript_src)。
    summary_src：summary.md（markdown 纪要）；topics_src：topics.md（元数据 JSON）；
    transcript_src：transcript.md。缺失返回空串。"""
    def _read(name):
        p = os.path.join(folder, name)
        try:
            # with 块：不然大会议反复读会留下一堆未关闭句柄（门禁输出里的 ResourceWarning）
            with open(p, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return _read("summary.md"), _read("topics.md"), _read("transcript.md")


def _meeting_full_text(summary_src, topics_src, transcript_src):
    """按「会议摘要（元数据） → 会议纪要（markdown） → 议题分段 → 转写详情」
    拼装完整纪要全文（写入归档 _会议纪要.md）。"""
    parts = []
    _t, _intro, abstract, segs = _parse_topics_meta(topics_src)
    if abstract:
        parts.append("# 会议摘要\n\n" + abstract)
    if summary_src:
        parts.append("# 会议纪要\n\n" + summary_src)
    if segs:
        parts.append("# 议题分段\n\n" + _topics_to_md(segs))
    if transcript_src:
        parts.append("# 转写详情\n\n" + transcript_src)
    return "\n\n---\n\n".join(parts)


def _clean_md(s):
    """去掉行内 markdown 强调/链接语法，保留正文文字。"""
    s = re.sub(r"!?\[\[([^\]|]*?)(?:\|[^\]]*?)?\]\]", r"\1", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s


def _clip_text(s, n):
    """截断到 n 字以内，优先在中文标点处断句，过长加省略号。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    for p in "。；；！？，、":
        idx = cut.rfind(p)
        if idx > (n - 1) * 0.5:
            return cut[:idx + 1]
    return cut + "…"


def _meeting_short_summary(content, max_len=140):
    """从纪要 markdown 提取 1~3 句简短会议摘要（供工作日志条目兜底使用）。
    优先级：会议目标 → 议题小节 → 主标题导语 → 首个实质行。返回单行纯文本。"""
    if not content:
        return ""
    text = _clean_md(re.sub(r"```.*?```", "", content, flags=re.S))  # 去代码块并清行内 markdown
    # 1) 会议目标 / 会议要解决的问题（**会议目标**：… 或 会议目标：…，已去 **）
    m = re.search(r"(?:会议目标|会议要解决的问题)\s*[：:]\s*([^\n]+)", text)
    if m:
        s = _clean_md(m.group(1)).strip()
        if len(s) >= 6:
            return _clip_text(s, max_len)
    # 2) 议题小节（## 一、议题 之类）下的要点前几条
    m2 = re.search(r"#{1,6}\s*[^\n]*议题[^\n]*\n(.*?)(?=\n#{1,6}|\Z)", text, re.S)
    if m2:
        items = []
        for ln in m2.group(1).split("\n"):
            s = _clean_md(ln).strip()
            if not s or re.match(r"^```", s):
                continue
            if s.startswith("#"):
                break
            s = re.sub(r"^[-*\d、\.]+|^[-*]\s*", "", s).strip()
            if len(s) >= 4:
                items.append(s)
            if len(items) >= 3:
                break
        if items:
            return _clip_text("；".join(items), max_len)
    # 3) 主标题（# 行）之后的导语正文：跳过时间/参会方元数据，取实质内容行
    body = []
    started = False
    for ln in text.split("\n"):
        if re.match(r"^#\s", ln):
            started = True
            continue
        if not started:
            continue
        if ln.strip().startswith("#"):
            break
        s = _clean_md(ln).strip()
        if not s:
            continue
        if re.match(r"^(会议时间|会议时长|参会方|参会人|参会|时间|地点|主持)", s):
            continue
        body.append(s)
        if len(body) >= 2:
            break
    if body:
        return _clip_text("；".join(body), max_len)
    # 4) 兜底：首个实质行
    for ln in text.split("\n"):
        s = _clean_md(ln).strip()
        if s and not s.startswith("#"):
            return _clip_text(s, max_len)
    return ""


# ---------------------------------------------------------------- 结构化输出解析
# 2026-09-11：两次 DSH 调用分工。
# 调用1（纪要）→ summary.md：纯 markdown 纪要（可含 Mermaid 图表），前端直接渲染。
# 调用2（元数据）→ topics.md：单个 JSON 对象，供列表/工作日志/主题分段/归档使用：
#   {"标题":"…","简介":"…","摘要":"…","分段":[{"标题","开始","结束","摘要"}, …]}
# DSH 可能在 JSON 前后夹带分析文本，_try_load_json 负责从中提取合法 JSON 块。

_JSON_DECODER = json.JSONDecoder()


def _try_load_json(text):
    """从回复文本中提取 JSON。DSH 可能在 JSON 前后附带分析/注释文本，
    直接 json.loads 整段会失败；这里用 raw_decode 扫描、提取每个合法 JSON 块，
    优先返回最后一个 dict/list（DSH 常先给草稿再给 finalize 版）。失败返回 None。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    if not s:
        return None
    try:
        return json.loads(s)  # 纯 JSON 快速路径
    except Exception:
        pass
    results = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in "{[":
            try:
                val, end = _JSON_DECODER.raw_decode(s, i)
                results.append(val)
                i = end
                continue
            except (json.JSONDecodeError, ValueError):
                pass
        i += 1
    if not results:
        return None
    # 优先挑含目标字段的 dict；否则最后一个 dict/list
    for r in reversed(results):
        if isinstance(r, dict) and any(k in r for k in ("标题", "分段", "会议名称", "摘要")):
            return r
    for r in reversed(results):
        if isinstance(r, (dict, list)):
            return r
    return results[-1]


def _parse_topics_meta(text):
    """解析 topics.md 的元数据 JSON。返回 (标题, 简介, 摘要, 分段列表) 或 (None,None,None,None)。
    分段列表元素为 {标题,开始,结束,摘要}；无有效 JSON 时各字段为 None/[]。"""
    obj = _try_load_json(text)
    if not isinstance(obj, dict):
        return None, None, None, None
    title = (obj.get("标题") or "").strip()
    intro = (obj.get("简介") or "").strip()
    abstract = (obj.get("摘要") or "").strip()
    segs = []
    raw = obj.get("分段")
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            segs.append({
                "标题": (it.get("标题") or "").strip(),
                "开始": str(it.get("开始") or "").strip(),
                "结束": str(it.get("结束") or "").strip(),
                "摘要": (it.get("摘要") or "").strip(),
            })
    return title, intro, abstract, segs or None


def _parse_topics_fields(text):
    """从 topics.md 提取议题分段列表（元数据 JSON 的“分段”字段）。
    无有效分段返回 None。兼容旧格式：若顶层就是数组也直接接受。"""
    title, intro, abstract, segs = _parse_topics_meta(text)
    if segs:
        return segs
    obj = _try_load_json(text)
    if isinstance(obj, list):  # 旧格式：顶层即数组
        out = []
        for it in obj:
            if not isinstance(it, dict):
                continue
            out.append({
                "标题": (it.get("标题") or "").strip(),
                "开始": str(it.get("开始") or "").strip(),
                "结束": str(it.get("结束") or "").strip(),
                "摘要": (it.get("摘要") or "").strip(),
            })
        return out or None
    return None


def _topics_to_md(topics):
    """把议题分段数组转成 markdown（供归档“议题分段”节与离线导出）。"""
    if not topics:
        return ""
    lines = []
    for i, t in enumerate(topics, 1):
        rng = f" [{t['开始']} - {t['结束']}]" if (t['开始'] or t['结束']) else ""
        lines.append(f"## {i}. {t['标题']}{rng}\n{t['摘要']}".rstrip())
        lines.append("")
    return "\n".join(lines).strip()


def _meeting_abstract(content):
    """会议摘要（工作日志/展示用）：从纪要 markdown 启发式提取（<200字）。
    结构化「摘要」字段由 topics 元数据提供，工作日志侧优先用 _meeting_summary_for。"""
    return _meeting_short_summary(content, 200)


def _meeting_summary_for(folder, fallback_content=""):
    """工作日志摘要：优先取 topics.md 元数据里的结构化「摘要」，否则回退启发式提取。"""
    try:
        _t, _intro, abstract, _segs = _parse_topics_meta(
            open(os.path.join(folder, "topics.md"), encoding="utf-8").read())
        if abstract:
            return _clip_text(abstract, 200)
    except OSError:
        pass
    return _meeting_short_summary(fallback_content)


def _local_short_title(content, max_len=24):
    """本地兜底：把整场纪要压缩成一行的开会主题短名（DSH 未给标记时用）。"""
    s = _meeting_short_summary(content, max_len)
    if not s:
        return ""
    s = re.sub(r"[。；，、：\s]+$", "", s.strip())
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip("。；，、： ") + "…"


def _meeting_title(meeting):
    """会议展示/日志标题：优先自动生成的简短名称，否则回退时间戳文件夹名。"""
    if not meeting:
        return ""
    t = (meeting.get("title") or "").strip()
    return t or meeting["name"]


def _meeting_date(meeting):
    """会议发生日期（YYYY-MM-DD）：优先取 started_at，解析失败回退今天。
    工作日志/例会归档都应落在会议当天，而不是用户下达归档命令的日期。"""
    raw = (meeting or {}).get("started_at") or ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw.replace("T", " ").strip())
    if m:
        try:
            datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            return m.group(1)
        except ValueError:
            pass
    return datetime.date.today().strftime("%Y-%m-%d")


def _meeting_hour(meeting):
    """会议开始时刻（0-23），用于判定日志写入「上午/下午」节；取不到用当前时刻。"""
    raw = (meeting or {}).get("started_at") or ""
    m = re.search(r"[T ](\d{1,2}):\d{2}", raw.strip())
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return datetime.datetime.now().hour



def _refresh_archived_note(meeting_id):
    """纪要/元数据重新生成后，刷新本地归档 md（meeting_note.md）。

    解决"先生成的是占位版已归档、稍后真正成稿后本地 md 还是旧的"问题。
    只重写本地这一份材料文件；笔记库里已归档的内容是否更新，由用户的归档技能
    在下次归档时幂等覆盖决定——ECHO 不直接改笔记库（2026-09-12 起）。
    """
    try:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            return
        folder = os.path.join(meetings_dir(), meeting["name"])
        _s, _seg, _tr = _meeting_parts(folder)
        full_text = _meeting_full_text(_s, _seg, _tr)
        # 只有源里确实有实质纪要才重写，避免用更空的内容覆盖更全的
        if _looks_placeholder(_s) or not (_s or _seg or _tr):
            return
        path = worklog.export_note(meeting, folder, full_text)
        if path:
            db.add_log("info", "meeting",
                       f"会议 {_meeting_title(meeting)} 本地归档材料已刷新")
    except Exception as e:
        db.add_log("warn", "meeting", f"刷新本地归档材料失败: {e}")


def push_meeting_to_worklog(meeting_id, archive_hint=""):
    """把会议纪要归档到用户的笔记库——**委派给用户自己的归档技能**。

    ECHO 只做三件事：备齐材料（落一份自包含 md）、定位笔记库、把任务送进 DSH。
    写到哪个目录、日志什么格式、有哪些专项与例会，全部由技能决定。
    返回值 (ok, msg)，msg 是技能回的一句话（原样转给面板）。

    archive_hint 即面板「归档要求」自由文本，空则由技能按自身默认规则判断。
    """
    ok, why = worklog.ready()
    if not ok:
        return False, why
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    folder = os.path.join(meetings_dir(), meeting["name"])
    content = open(os.path.join(folder, "summary.md"), encoding="utf-8").read() \
        if os.path.isfile(os.path.join(folder, "summary.md")) else ""
    _summary, _segments, _transcript = _meeting_parts(folder)
    full_text = _meeting_full_text(_summary, _segments, _transcript)
    if not (full_text or content):
        return False, "暂无纪要，请先生成纪要"
    try:
        note_path = worklog.export_note(meeting, folder, full_text, content)
    except OSError as e:
        return False, f"导出纪要文件失败: {e}"
    if not note_path:
        return False, "暂无纪要内容可归档"
    db.add_log("info", "meeting",
               f"会议 {_meeting_title(meeting)} 归档委派：{note_path}")
    return worklog.delegate_archive(
        meeting, note_path, archive_hint=archive_hint,
        date_str=_meeting_date(meeting), hour=_meeting_hour(meeting))


def save_summary(meeting_id, content):
    """人工改写的纪要正文写回 summary.md（面板纪要页签的「编辑」）。

    与 request_summary 同一条落盘路径：summary.md 是纯 markdown 纪要，
    前端 mdToHtml 直接渲染，归档/离线导出也读它。
    注意：整场「重新生成纪要」会覆盖这里的人工修改（前端保存时已提示）。

    返回 (ok, msg)。空内容被拒绝——归档与「写工作日志」都按「有纪要」判定，
    清空等于把这场会议的纪要弄丢，要删请用 DELETE /api/meetings/{id}。
    """
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    text = (content or "").replace("\r\n", "\n").strip()
    if not text:
        return False, "纪要内容为空，未保存"
    folder = os.path.join(meetings_dir(), meeting["name"])
    if not os.path.isdir(folder):
        return False, "会议目录不存在"
    try:
        with open(os.path.join(folder, "summary.md"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError as e:
        return False, f"写入失败: {e}"
    db.add_log("info", "meeting", f"会议 {_meeting_title(meeting)} 纪要已人工编辑保存（{len(text)} 字）")
    return True, "纪要已保存"


def update_meeting_title(meeting_id, title):
    """人工改会议名：同时写 meetings.title 与 topics.md 的「标题」。

    两处都要写——面板列表/顶栏读数据库，工作日志「会议纪要：<名称>」与归档
    读 topics.md 元数据；只改一处会出现"标题不一致"。

    返回 (ok, title, msg)；标题为空视为清空自定义命名，回落到 m.name。
    """
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "", "会议不存在"
    name = (title or "").strip()[:40]     # 与自动命名同一口径（见 _spawn_summary_waiter 的 title[:40]）
    try:
        db.update_meeting(meeting_id, title=name)
    except Exception as e:
        return False, "", f"保存会议名称失败: {e}"
    # topics.md 同步只影响归档/分段展示，失败不回滚数据库（面板已有新名字）
    folder = os.path.join(meetings_dir(), meeting["name"])
    path = os.path.join(folder, "topics.md")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                obj = _try_load_json(f.read())
            if isinstance(obj, dict):
                obj["标题"] = name
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        except Exception as e:
            db.add_log("warn", "meeting", f"topics.md 标题同步失败（数据库已更新）: {e}")
    db.add_log("info", "meeting", f"会议已改名：{name or meeting['name']}")
    return True, name, "会议名称已保存" if name else "已清空自定义名称"


#: 纪要的**格式要求**（两条路共用：agent 路径与直连 LLM 路径）。
#: 放在这里而不是各写一遍 —— Mermaid 的坑是实测踩出来的（PROGRESS §27 那批），
#: 复制一份就等于将来只修好一条路。
_SUMMARY_REQUIREMENTS = (
    "要求：生成完整纪要：**会议背景/议题 → 各议题关键讨论与结论 → 待办事项及责任人**。\n"
    "**尽量用 Mermaid 图表表达结构与流程**：如 flowchart 表达分工/流程、"
    "timeline 表达时间线/进度、sequenceDiagram 表达协作时序；Mermaid 代码用 "
    "```mermaid 代码块标注。\n"
    "**Mermaid 规范**：flowchart/graph 中节点文本与连线标签若含括号等符号，"
    "必须用双引号包裹文本（如 CY[\"初验(出验)证书\"]、|\"中验(待签)\"|），"
    "否则图表无法渲染；文本内不要出现未闭合引号。"
    "**引号标签里不要再出现半角双引号**（2026-09-20 实测：最新一条会议的纪要因此整张图报错）："
    "要引用别人的话，用全角引号或书名号——写成 “他人说话” 或 「他人说话」，"
    "不要写成 过滤\"他人说话\"等，后者会把标签提前闭合。\n"
    "**timeline 专项规范**（2026-09-12 实测：这两点写错整张图直接报错）："
    "1) 标题必须写成 `title: 文本`（冒号不可省）；"
    "2) **周期文本里不能含冒号**——timeline 用 `:` 分隔「周期 : 事件」，"
    "所以时间戳要写成点号形式 `00.00.12 : 发起试音`（或 `00-00-12`），"
    "不要写 `00:00:12 : 发起试音`。"
)

#: 直连 LLM 时内联的转写文本上限（字符）。超了要**明说被截断**，不能悄悄丢内容。
_PROVIDER_TRANSCRIPT_LIMIT = 120000


def _llm_provider_for_summary():
    """纪要要走直连 LLM 时用的 provider 实例；取不到返回 ``(None, 原因)``（P5）。"""
    from app import providers as providers_mod
    try:
        pid, inst = providers_mod.active("llm")
    except Exception as e:
        return None, "没有可用的 LLM provider（%s）" % e
    return inst, pid


def direct_llm_decision():
    """纪要是否走**直连 LLM provider**？返回 ``(bool, 原因)``。

    判据（2026-09-20 按"会议纪要只用 agent"的定调重写）：
      1. **agent（DSH）可用 → 一律走 agent**。纪要不是"孤立地调一次大模型"：同一场会议里
         分段与归档走的是同一个会话（日志原话"纪要/分段/归档共用此会话"），而归档还依赖
         agent 的 skill 机制 —— 只要 agent 在，就不该把它绕过去；
      2. agent 不可用而 LLM provider 就绪 → 直连兜底（P5 的承诺："不装 agent 也能出纪要"）；
      3. 其它 → 保持 agent 路径（由原路径报错，不静默走一条没配好的路）。

    **为什么删掉了原来"用户显式选了 `providerLlm` 就优先直连"那条判据**：
    `providerLlm` 的默认值是 `""`，而 `""` 与 `"echo-auto"` 的**生效 provider 完全一样**
    （`providers.default_id("llm")` 就是 `echo-auto`）。原判据看的是"原始设置非空"，于是
    "在面板里显式选了 ECHO AUTO"这个动作会**静默把纪要从 agent 切到直连** —— 实测踩到过
    （2026-09-19_19-18-25 那场：19:19 走 agent，次日 02:04 变成直连）。同一个 provider
    不该有两种路由，而且用户选路由时并没想到会顺手关掉纪要的 agent 路径。

    只读判断，不做任何副作用；探测失败一律按"不走直连"处理（宁可退回老路）。
    """
    try:
        from app import manager
        agent_ok = bool(manager.dsh_ready())
    except Exception:
        agent_ok = False
    if agent_ok:
        return False, "agent 可用（DSH 就绪）"
    inst, pid = _llm_provider_for_summary()
    if inst is None:
        return False, "agent 不可用且没有可用的 LLM provider"
    return True, "agent 不可用（DSH 未就绪），改用 LLM provider %s" % pid


def _provider_summary_text(folder):
    """把整场会议的转写**内联**成一段文本（直连 LLM 没有文件读取能力）。

    用 `_meeting_parts()` 汇总；超长时截断并**在文末显式说明**（不能让模型以为这就是全部）。
    """
    try:
        summary_src, topics_src, transcript_src = _meeting_parts(folder)
        text = _meeting_full_text(summary_src, topics_src, transcript_src)
    except Exception as e:
        raise RuntimeError("读取会议材料失败：%s" % e) from None
    text = (text or "").strip()
    if not text:
        raise RuntimeError("会议目录里没有可用的转写文本（transcript.md 为空或缺失）")
    if len(text) > _PROVIDER_TRANSCRIPT_LIMIT:
        original_len = len(text)
        text = text[:_PROVIDER_TRANSCRIPT_LIMIT] + (
            "\n\n【注意】以上内容因长度限制被截断（原文 %d 字），"
            "纪要需基于已给出的部分，并在开头注明「材料被截断」。" % original_len)
    return text


def _spawn_provider_summary(meeting_id, folder, out_name="summary.md", extra=""):
    """后台线程：把转写内联交给 LLM provider，写成纪要文件（P5）。

    与 agent 路径共用同一套**落盘纪律**：回复过短或疑似占位话就**不回写**
    （保留已有文件），失败写 warn 日志 —— 宁可留空让人重试，也不要把占位当纪要存下来。
    """
    def _run():
        with _MEETING_LOCKS_LOCK:
            lock = _MEETING_LOCKS.setdefault(meeting_id, threading.Lock())
        with lock:
            try:
                inst, pid = _llm_provider_for_summary()
                if inst is None:
                    db.add_log("error", "meeting", "直连纪要失败：%s" % pid)
                    return
                text = _provider_summary_text(folder)
                prompt = ("以下是会议转写（按片段组织，行内带[绝对时间戳]）：\n\n" + text +
                          "\n\n" + _SUMMARY_REQUIREMENTS + "\n"
                          "**输出约束：只输出纪要 markdown 全文**，不要寒暄、不要解释过程。")
                if extra:
                    prompt += "\n\n追加要求：%s" % extra
                db.add_log("info", "meeting",
                           f"已请求纪要（provider={pid}，{os.path.basename(folder)}）")
                reply = inst.chat([{"role": "system", "content": "你是会议纪要助手。"},
                                   {"role": "user", "content": prompt}], timeout=300)
                path = os.path.join(folder, out_name)
                had_old = os.path.isfile(path)
                old = ""
                if had_old:
                    try:
                        old = open(path, encoding="utf-8").read().strip()
                    except OSError:
                        old = ""
                if not (reply and len(reply.strip()) > 10):
                    db.add_log("warn", "meeting", "纪要 provider 回复过短，本次不回写")
                    return
                if _looks_placeholder(reply):
                    db.add_log("warn", "meeting",
                               "纪要 provider 回复疑似占位/意图话（非实质纪要），本次不回写"
                               f"{'，保留原文件' if had_old and len(old) > 30 else '（无旧有效内容）'}")
                    return
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(reply)
                db.add_log("info", "meeting",
                           f"纪要已生成（provider={pid}）：{os.path.basename(path)}")
            except Exception as e:
                # 直连路径的失败必须留痕：否则面板上表现为"点了没反应"（1.x 的老毛病）
                db.add_log("error", "meeting", f"直连纪要失败：{e}")
    threading.Thread(target=_run, daemon=True, name="summary-provider").start()


def request_summary(meeting_id, folder=None, extra=""):
    """生成整场会议纪要（纯 markdown，可含 Mermaid 图表），后台线程写 summary.md。

    两条路（P5 起，2026-09-20 起**以 agent 为准**）：
      * **agent 路径**（默认，只要 DSH 就绪就走它）：把 transcript.md 的**路径**交给 DSH，
        由它用 read 工具读并撰写；分段与归档共用这个会话；
      * **直连 LLM 路径**（兜底）：**agent 用不了**（DSH 未就绪）而 LLM provider 就绪时，
        把转写**内联**喂给 LLM provider —— 这样"不装 agent 也能出纪要"（P5 的验收点）。
    标题/简介/摘要/分段由第二次调用（request_topic_segments）以 JSON 提供（那次**只走 agent**）。

    ``extra`` = 追加要求（面板「重新生成」里填的那种）。**两条路都必须带上它** ——
    2026-09-19 修：以前 `regenerate_summary` 把它记进 summary_runs 却没往下传，
    等于"追加要求"从来没生效过。
    """
    if folder is None:
        meeting = db.get_meeting(meeting_id)
        folder = os.path.join(meetings_dir(), meeting["name"])
    meeting = db.get_meeting(meeting_id)
    use_direct, why = direct_llm_decision()
    if use_direct:
        db.add_log("info", "meeting", "纪要走直连 LLM：%s" % why)
        _spawn_provider_summary(meeting_id, folder, extra=extra)
        return True
    transcript = os.path.join(folder, "transcript.md").replace("\\", "/")
    text = (f"任务：基于会议转写文件生成会议纪要（markdown 格式）。\n"
            f"步骤：1) 用 read 工具读取文件 \"{transcript}\"（已按片段组织，"
            f"每片有 \"## 第 N 段 [起-止]\" 标题，行内带[绝对时间戳]）。\n"
            f"2) " + _SUMMARY_REQUIREMENTS + "\n"
            f"**输出约束：你只能在最终回复中输出纪要全文（markdown），"
            f"这是唯一的交付方式。严禁调用 write 或任何写文件工具。**")
    if extra:
        text += "\n\n追加要求：%s" % extra
    _spawn_summary_waiter(meeting_id, folder, "summary.md", text, "纪要")
    return True


def request_topic_segments(meeting_id, folder=None):
    """请 DSH 输出结构化会议元数据 JSON（第二次调用，替代原分段+语义分段两次调用）：
    标题、简介、摘要、议题分段，供会议列表/工作日志/前端主题分段/归档使用。

    DSH 在回复中只输出一个 JSON 对象，后台线程等待回复并写入 topics.md：
      {
        "标题": "…(<20字)",
        "简介": "…(一句话)",
        "摘要": "…(<200字)",
        "分段": [
          {"标题": "…", "开始": "mm:ss", "结束": "mm:ss", "摘要": "…"},
          ...
        ]
      }
    """
    if folder is None:
        meeting = db.get_meeting(meeting_id)
        folder = os.path.join(meetings_dir(), meeting["name"])
    meeting = db.get_meeting(meeting_id)
    transcript = os.path.join(folder, "transcript.md").replace("\\", "/")
    text = (f"任务：通读会议转写全文，输出本次会议的结构化元数据 JSON"
            f"（供会议列表/工作日志/主题分段展示使用）。\n"
            f"1) 用 read 工具读取转写文件 \"{transcript}\"（每行带[绝对时间戳]，"
            f"已按音频片段分节，但不代表议题边界）。\n"
            f"2) 输出一个 JSON 对象，含 4 个字段：\n"
            f"   - 标题：一句话（不超过 20 个汉字）概括本次会议主题，"
            f"不带标点结尾、不加引号（如：多模态能力共享中心方案评审）。\n"
            f"   - 简介：一句话（40 字内）介绍会议性质与目的。\n"
            f"   - 摘要：整个会议的内容摘要，不超过 200 个汉字。\n"
            f"   - 分段：按讨论主题/阶段划分 **3~10 个议题段**，每段为一个对象，含："
            f"标题、起止绝对时间（取该段最早和最晚的时间戳，格式 mm:ss 或 h:mm:ss）、"
            f"以及 2~3 句摘要（该议题讨论的核心内容与结论）。\n"
            f"**输出约束：你只能在最终回复中输出一个**合法的 JSON 对象**，形如：\n"
            f"{{\"标题\":\"会议主题\",\"简介\":\"…\",\"摘要\":\"…\","
            f"\"分段\":[{{\"标题\":\"议题一\",\"开始\":\"00:00\",\"结束\":\"05:12\","
            f"\"摘要\":\"…\"}},{{\"标题\":\"议题二\",\"开始\":\"05:12\",\"结束\":\"09:40\","
            f"\"摘要\":\"…\"}}]}}\n"
            f"禁止在 JSON 外输出任何说明文字、禁止调用 write 或任何写文件工具。**")
    _spawn_summary_waiter(meeting_id, folder, "topics.md", text, "议题分段")
    return True


def _looks_placeholder(text):
    """判断 DSH 回复是否为「占位/意图」而非真实纪要正文。

    若 DSH 只回了一句诸如“I'll read the transcript file first.”、
    “正在读取转写文件…”这类意图/进度话，却没有任何实质章节/要点，
    把它当成纪要写盘会造成「真纪要被占位符顶掉、之后展示不出来」。
    """
    t = (text or "").strip()
    if len(t) >= 80:
        return False  # 长度够，视为有实质内容，不再误判
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    plain = [ln for ln in lines if not ln.lstrip().startswith(("#", "```"))]
    has_body = sum(1 for ln in plain if len(_clean_md(ln)) >= 6)
    if has_body >= 2:
        return False  # 有两行以上实质话，视为真内容
    # 仅剩少量文本 → 命中“意图/进度”句则判为占位
    pats = re.compile(
        r"^(I(?:'|\u2019)?ll|i will|let(\u2019s|\u2018s|\u2019)?\s+me|正在|我需要|请稍等|"
        r"先|让我|马上|先读|读一下|待我|稍等|ok|好的).{0,40}$",
        re.I)
    return bool(pats.search(t))


def _spawn_summary_waiter(meeting_id, folder, out_name, prompt_text, label):
    """发 prompt 到纪要会话（工作区每次会议新会话 / 或固定会话），后台线程等回复并写入文件。

    同一会议的多个纪要请求（整场/分段/语义分段）串行执行，防止并发同会话时
    wait_for_reply 抢到同一个回复导致输出串台。

    写入前做「占位符/超短」校验：若 DSH 只回了意图话而没给实质纪要，则**不回写**
    （保留已有的正确文件；若还没有旧文件或旧文件同样为空壳，则留待人工重试），
    避免把占位符当真纪要持久化并用于后续展示/归档。
    """
    def _run():
        with _MEETING_LOCKS_LOCK:
            lock = _MEETING_LOCKS.setdefault(meeting_id, threading.Lock())
        with lock:
            try:
                client = get_client()
                sid = _summary_session(client, meeting_id)
                if not sid:
                    db.add_log("error", "meeting", "无纪要会话，跳过自动纪要")
                    return
                client.clear_stuck(sid)
                client.prompt(sid, prompt_text, mode="queue")
                db.add_log("info", "meeting", f"已请求{label} ({os.path.basename(folder)})")
                reply, _done = client.wait_for_reply(sid, timeout=240, poll=2)
                path = os.path.join(folder, out_name)
                had_old = os.path.isfile(path)
                old = ""
                if had_old:
                    try:
                        old = open(path, encoding="utf-8").read().strip()
                    except OSError:
                        old = ""
                if not (reply and len(reply.strip()) > 10):
                    db.add_log("warn", "meeting", f"{label} 超时未收到回复")
                    return
                if _looks_placeholder(reply):
                    db.add_log("warn", "meeting",
                               f"{label} 回复疑似占位/意图话（非实质纪要），本次不回写"
                               f"{'，保留原文件' if had_old and len(old) > 30 else '（无旧有效内容）'}")
                    return
                content_raw = reply.strip()
                # 写盘内容：调用1（summary.md）为 markdown 纪要；调用2（topics.md）为元数据 JSON
                if out_name == "topics.md":
                    obj = _try_load_json(content_raw)
                    if obj is not None:
                        content = json.dumps(obj, ensure_ascii=False, indent=2)
                    else:
                        content = content_raw + "\n"  # 未能提取 JSON 时按原文回写（可人工修正）
                else:
                    content = _sanitize_mermaid(content_raw + "\n")  # markdown 纪要
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                db.add_log("info", "meeting", f"{label}已写入 {out_name}（{len(content)} 字，"
                           f"{'JSON' if out_name == 'topics.md' and obj is not None else '文本'}）")
                # 元数据成稿后：用结构化「标题」字段写入 meetings.title，
                # 会议列表与工作日志「会议纪要：<名称>」用它，更清晰（2026-09-10 起）。
                if out_name == "topics.md":
                    _t, _intro, _abs, _segs = _parse_topics_meta(content)
                    title = (_t or _local_short_title(
                        open(os.path.join(folder, "summary.md"), encoding="utf-8").read()
                        if os.path.isfile(os.path.join(folder, "summary.md")) else "")).strip()
                    if title:
                        try:
                            db.update_meeting(meeting_id, title=title[:40])
                            db.add_log("info", "meeting", f"会议已自动命名：{title}")
                        except Exception as e:
                            db.add_log("warn", "meeting", f"保存会议名称失败: {e}")
                    # 纪要/元数据重新生成后，刷新本地归档材料（meeting_note.md），
                    # 让后续归档拿到的是成稿版而不是先前的占位版。
                    _refresh_archived_note(meeting_id)
            except Exception as e:
                db.add_log("error", "meeting", f"{label} 生成失败: {e}")
    threading.Thread(target=_run, daemon=True).start()


def regenerate_summary(meeting_id, extra_prompt=""):
    """重新生成纪要 + 分段摘要（可追加要求）。"""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    folder = os.path.join(meetings_dir(), meeting["name"])
    if not os.path.isfile(os.path.join(folder, "transcript.md")):
        return False, "转写文件不存在，无法生成纪要"
    run_id = db.add_summary_run(meeting_id, extra_prompt)
    if extra_prompt:
        # 追加要求时只重生成整场纪要（带要求），议题分段保持。
        # 2026-09-19 修：以前没把 extra_prompt 传下去 —— 那个"追加要求"框填了也没用。
        ok = request_summary(meeting_id, folder, extra=extra_prompt)
    else:
        ok1 = request_summary(meeting_id, folder)
        ok2 = request_topic_segments(meeting_id, folder)
        ok = ok1 and ok2
    db.finish_summary_run(run_id, "done" if ok else "failed")
    return ok, ("已发送纪要+议题分段生成请求" if ok
                else "发送请求失败（agent 与 LLM provider 都不可用？）")


def retranscribe_meeting(meeting_id):
    """手动重新转写一场会议（后台，并发保护）。"""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    name = meeting["name"]
    folder = os.path.join(meetings_dir(), name)
    # 段发现同时认 flac（历史音频压缩后 wav 已删）—— 只认 wav 会让压缩过的会议
    # 报"该会议没有音频片段，无法转写"，而那正是用户最想重新转一场的时候。
    segs = audiofile.segment_files(folder) if os.path.isdir(folder) else []
    if not segs:
        return False, "该会议没有音频片段，无法转写"
    with _retranscribing["lock"]:
        if name in _retranscribing["set"]:
            return False, "该会议已在重新转写中，请稍候"
        if _state["active"] and os.path.basename(_state["folder"] or "") == name:
            return False, "该会议正在录音中，结束后再重新转写"
        _retranscribing["set"].add(name)

    def _run():
        try:
            _transcribe_meeting(folder)
        finally:
            with _retranscribing["lock"]:
                _retranscribing["set"].discard(name)

    threading.Thread(target=_run, daemon=True).start()
    return True, f"已开始重新转写（{name}）"


# ---------------------------------------------------------------- 导入录音（成为一场会议）

#: 与"正在重转"共用同一把纪律：**同一时刻只允许一场导入**。
#: 为什么必须互斥：导入会往会议目录里写 `01.wav/02.wav…`，两场导入并发就会抢同一批段号，
#: 而"谁的 01.wav 是谁的"事后完全查不出来（表现是转写内容串了，最难查的那种）。
_importing = {"lock": threading.Lock(), "busy": False}


class _ImportFailed(Exception):
    """内部：某个音频文件转换失败 —— 原因已经是给人看的一句话（原样返回给调用方）。"""


def _unique_meeting_folder(root, stamp):
    """给这场导入挑一个还没被占用的会议目录名，并**直接建出来**。

    命名沿用会议链路的既有形状 `2026-08-21_10-00-00`（`_meeting_hour()` /
    `_meeting_date()` 都按这个形状解析，换了形状面板就认不出时间）。
    同一秒重复导入（脚本连打两次）时加 `_2`、`_3` 后缀 —— **绝不覆盖**既有会议：
    覆盖会把别人的录音整段冲掉，而那是最不可逆的一种错。
    """
    base = stamp or datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for i in range(1, 100):
        name = base if i == 1 else "%s_%d" % (base, i)
        folder = os.path.join(root, name)
        try:
            os.makedirs(folder)          # exist_ok=False：谁先建谁得到它
        except FileExistsError:
            continue
        except OSError as e:
            return "", "", "无法创建会议目录（%s）：%s" % (folder, e)
        return name, folder, ""
    return "", "", "同一时间点的会议目录已经存在 100 个（%s）——请稍后再导入" % base


def _import_cfg():
    """导入时写进 `meta.json` 的配置快照（与 `start_meeting()` 同一组键）。

    为什么照抄录音那份：`_transcribe_impl` 把 `meta["config"]` 当**兜底**读
    （设置改了之后重转用新值，但快照能说明"导入当时这台机器是怎么配的"）。
    """
    cfg = settings
    return {
        "sttModel": cfg.get("meetingSttModel", "small"),
        "sttDevice": cfg.get("device", "auto"),
        "sttLanguage": cfg.get("sttLanguage", "zh"),
        "segmentMinutes": cfg.get("meetingSegmentMinutes", 10),
        "autoSummarize": cfg.get("meetingAutoSummarize", True),
        "diarize": cfg.get("meetingDiarize", False),
    }


def import_meeting(files, *, title="", start="", notes="", started_at="", block_frames=None,
                   display_names=None):
    """把 1..N 个音频文件导入成**一场会议**（顺序即分段顺序），成功后自动开始转写。

    参数：
      files        音频文件路径的**有序**列表（顺序 = 段号顺序）
      title        可选标题（写 `meetings.title`）
      start        可选的会议时间（`YYYY-MM-DD HH:MM[:SS]` / ISO；空 = 现在）
      notes        可选备注（写 `meetings.notes`）
      started_at   可选的 ISO 起始时间（给了就用它，不再从 `start` 推；测试用来固定目录名）
      block_frames 可选：转码分块大小（用例据此断言"没整段进内存"）
      display_names 可选：与 `files` 一一对应的**显示名**。HTTP 那条路传的是上传临时文件，
                   而报错文案与 `meta.json` 里的 `importedFrom` 必须是用户认识的原文件名
                   （`echo-imp123-ab12.m4a` 这种临时名会让"哪个文件失败了"没法查）。
                   不给就按 `files[i]` 的文件名显示。

    返回 `(ok, name_or_reason)`；`ok=True` 时第二项是会议目录名。

    ## 状态落点：`imported`（「待转写」），不是 `transcribed` 也不是 `interrupted`

    同事的夹具脚本当初只能用 `interrupted`，因为**当时没有"待转写"这一档**；
    他自己在文档里写明了那是个妥协（"面板没有'待转写'这个状态；`transcribed` 会谎称
    已完成、`error` 又会被读成录音失败"）。既然这次是新功能，就把这一档**真的加上**：

      * `transcribed` 是假话 —— 库里一行转写都没有，面板却显示"已转写"；
      * `error` 更糟 —— 面板把它读成"录音失败（没录到音频）"，而音频明明好端端躺在磁盘上；
      * `interrupted` 的意思已经被"录音中断"占住了（进程崩了/设备掉了），
        用它表达"导进来还没转"会让这两种完全不同的状况在日志与列表里长得一模一样；
      * `imported` 只多一个词，却把语义说准了：**音频在、还没有文字**。
        它同时也是转写**失败**之后的落点之一吗？不是 —— 转写失败仍走 `error`
        （带真原因），`imported` 只表示"排着队等转写"。

    面板侧同步加了这一档的文案（`web/app.js` / `web/meeting.html`：
    「待转写」，徽章用 `idle` 色，不是红的）。**"已导入但转写没起来"仍然要看 `error`** ——
    那一档才有原因，`imported` 只是排队。

    ## 顺序：先全部转码成功，再建库记录 / 写 meta

    "不许留下垃圾目录"这条要求决定了顺序：**任何一个文件失败 = 整场不落地**
    （目录删掉、库里没有记录、返回真原因）。所以先把每个文件转成
    `01.wav/02.wav…`，全部成功之后才 `db.create_meeting()`。
    """
    paths = [str(p) for p in (files or []) if str(p or "").strip()]
    if not paths:
        return False, "没有选择任何音频文件"
    # 正在录音时**不许**导入：两个动作都要抢麦克风前后的那台机器（导入完会立刻起转写，
    # 而转写会吃满 CPU/GPU —— 那正好会拖垮正在进行的录音）。这不是技术上的互斥，
    # 是"别在用户录音时干重活"的礼貌，所以给一句能照做的话，而不是含糊地失败。
    if _state["active"]:
        return False, "正在录音中，不能同时导入录音；请先停止录音，再导入"
    # 同一时刻只允许一场导入（见 `_importing`）
    with _importing["lock"]:
        if _importing["busy"]:
            return False, "已有一次录音导入正在进行，请等它结束"
        _importing["busy"] = True
    try:
        return _import_meeting_locked(paths, title=title, start=start, notes=notes,
                                      started_at=started_at, block_frames=block_frames,
                                      display_names=display_names)
    finally:
        with _importing["lock"]:
            _importing["busy"] = False


def _import_meeting_locked(paths, *, title, start, notes, started_at, block_frames,
                           display_names=None):
    from app.audio import importer as imp

    root = ensure_meetings_dir()
    stamp = _parse_start_stamp(start) or datetime.datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S")
    if not started_at:
        try:
            started_at = datetime.datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").isoformat(
                timespec="seconds")
        except ValueError:
            started_at = datetime.datetime.now().isoformat(timespec="seconds")

    # **先定好名字、再真正落盘**：目录一旦建出来就属于"垃圾目录"要清理的那一类。
    name, folder, why = _unique_meeting_folder(root, stamp)
    if not name:
        return False, why

    logger = lambda level, msg: db.add_log(level, "meeting", msg)   # noqa: E731
    # 显示名：HTTP 那条路传的是上传临时文件，用户认识的是原文件名（见 `display_names`）。
    shown = list(display_names or [])
    results = []
    try:
        total = len(paths)
        for i, src in enumerate(paths, start=1):
            seg = "%02d.wav" % i
            dst = os.path.join(folder, seg)
            label = imp.display_name(shown[i - 1] if i - 1 < len(shown) else src)
            _set_progress(name, phase="导入中", seg_index=i, seg_total=total,
                          percent=round((i - 1) / float(total) * 100),
                          detail="正在转换第 %d/%d 个文件（%s）" % (i, total, label))
            kw = {"filename": label, "logger": logger}
            if block_frames:
                kw["block_frames"] = int(block_frames)
            try:
                res = imp.convert_to_16k_mono(src, dst, **kw)
            except imp.ImportAudioError as e:
                raise _ImportFailed(str(e))
            except Exception as e:                      # 磁盘满 / 权限 / 引擎级意外
                raise _ImportFailed("%s：%s" % (type(e).__name__, e))
            results.append(res)
            db.add_log("info", "meeting",
                       "导入 %s：%s → %s（%.1f 秒，源 %.0f Hz/%d 声道）"
                       % (name, label, seg, res.seconds, res.src_rate, res.src_channels))
    except _ImportFailed as e:
        _clear_progress(name)
        _cleanup_import(folder)
        db.add_log("error", "meeting", "导入录音失败（%s）：%s" % (name, e))
        return False, str(e)
    except Exception as e:
        _clear_progress(name)
        _cleanup_import(folder)
        db.add_log("error", "meeting", "导入录音失败（%s）：%r" % (name, e))
        return False, "导入失败：%s: %s" % (type(e).__name__, e)

    duration = round(sum(r.seconds for r in results), 3)
    audio_bytes = sum(int(r.dst_bytes or 0) for r in results)
    # 用户认识的那个文件名的清单（`r.src` 在 HTTP 那条路是临时文件，见 `display_names`）
    shown_names = [r.filename for r in results]
    meta = {
        "start": started_at,
        "source": "import",
        "importedFrom": list(shown_names),
        "importedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "durationSeconds": duration,
        "sampleRate": imp.TARGET_RATE,
        "channels": imp.TARGET_CHANNELS,
        "sampleFormat": "16-bit PCM",
        # 重采样走的哪条路**逐文件落盘**：用户问"这段转得准不准"、或排障时
        # "这台机器上到底用了 soxr 还是兜底插值"，答案在这里（日志里也有同样一行）。
        "resampled": [{"file": r.filename, "how": r.resampled,
                       "srcRate": r.src_rate, "srcChannels": r.src_channels,
                       "srcFormat": r.src_format} for r in results],
        "config": _import_cfg(),
    }
    if (title or "").strip():
        meta["title"] = str(title).strip()[:200]
    if (notes or "").strip():
        meta["notes"] = str(notes).strip()[:2000]

    # 库记录在**音频全部就位之后**才建：失败路径上就不会留下"有记录、没音频"的空会议。
    try:
        meeting_id = db.create_meeting(
            name, started_at=started_at, stt_model=meta["config"]["sttModel"],
            stt_device=meta["config"]["sttDevice"],
            diarize=1 if meta["config"]["diarize"] else 0)
    except Exception as e:
        _cleanup_import(folder)
        db.add_log("error", "meeting", "导入录音建记录失败（%s）：%s" % (name, e))
        return False, "导入失败（写会议记录）：%s" % e

    if str(title or "").strip():
        db.update_meeting(meeting_id, title=str(title).strip()[:200])
    if str(notes or "").strip():
        db.update_meeting(meeting_id, notes=str(notes).strip()[:2000])
    # 段数/时长/体积先写全（**转写之前**面板就能显示"29:37 · 3 段"），
    # 状态留给下一步：`imported` = 「待转写」。见函数头的状态说明。
    db.update_meeting(meeting_id, ended_at=started_at, duration_seconds=duration,
                      segments=len(results), audio_bytes=audio_bytes,
                      status="imported", error="")

    meta["segments"] = ["%02d.wav" % i for i in range(1, len(results) + 1)]
    try:
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except OSError as e:
        # meta.json 写不进去 = 这一场之后没法重转（`_transcribe_impl` 要读它）。
        # 宁可现在整套撤掉，也不要留一场"看着在、其实坏了"的会议。
        _cleanup_import(folder)
        try:
            db.delete_meeting(meeting_id)
        except Exception:
            pass
        db.add_log("error", "meeting", "导入录音写 meta.json 失败（%s）：%s" % (name, e))
        return False, "导入失败（写 meta.json，可能是磁盘满）：%s" % e

    db.add_event("meeting_imported", {"meeting": name, "id": meeting_id,
                                      "files": len(results), "seconds": duration})
    db.add_log("info", "meeting",
               "已导入 %d 个音频文件为一场会议：%s（%.1f 分钟，%d 段）——开始转写"
               % (len(results), name, duration / 60.0, len(results)))

    # 转写：先切到 `transcribing`（面板的进度条据此显示），再起后台线程。
    # 导入阶段的进度**在这里停掉**：它是按会议名登记的（那时还没有 id），
    # 不清的话 `/api/transcribe/status` 会永远挂着一条"导入中 50%"的死记录，
    # 而那是排障时最容易把人带偏的那种残留（"它是不是卡在导入了？"）。
    _clear_progress(name)
    db.update_meeting(meeting_id, status="transcribing", error="")
    threading.Thread(target=_transcribe_meeting, args=(folder,), daemon=True).start()
    return True, name


class _ImportFailed(Exception):
    """内部：某个音频文件转换失败 —— 原因已经是给人看的一句话（原样返回给调用方）。"""


def _cleanup_import(folder):
    """导入失败 → 把这场会议的目录整个删掉（**不许留下垃圾目录**）。

    为什么要删干净而不是"留着半成品让用户自己看"：会议列表是按目录名找音频的，
    留一个只有 01.wav 的目录，用户会以为"导进去了一半"，而它的 `meta.json`
    根本不存在（`_transcribe_impl` 读不到段清单，重新转写也救不回来）。
    """
    import shutil
    try:
        shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        pass


def _parse_start_stamp(value):
    """用户给的会议时间 → 会议目录名形状 `%Y-%m-%d_%H-%M-%S`（认不出就返回空串）。

    认得出的是用户在界面/接口上真会写的形状：`2026-09-20` / `2026-09-20 10:48` /
    ISO（`2026-09-20T10:48:26`）/ 已经就是这个目录名。**认不出不报错** ——
    时间只是个默认名，落到"现在"比拒绝一次导入合理。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("T", " ")
    for fmt in ("%Y-%m-%d_%H-%M-%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d_%H-%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%d_%H-%M-%S")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------- 查询

def get_meeting_detail(meeting_id):
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return None
    folder = os.path.join(meetings_dir(), meeting["name"])
    return {
        **meeting,
        "folder": folder,
        "speakers": db.get_speakers(meeting_id),
        "lines": db.get_lines(meeting_id),
        "summaries": db.get_summary_runs(meeting_id),
        "hasTranscript": os.path.isfile(os.path.join(folder, "transcript.md")),
        "hasSummary": os.path.isfile(os.path.join(folder, "summary.md")),
    }


def meeting_meta(meeting_name: str) -> dict:
    """读一场会的 `meta.json`（读不到/坏了都返回 `{}`）。

    为什么要一个公开入口：**录音当时的那份快照只有这个文件里才有** ——
    3.0 把"每个槽用了谁、跳过了谁、为什么"（`Plan.as_dict()`）和"时间轴精度档位"
    都写在这儿（见 `_transcribe_impl`）。会议详情要显示它，而 `_load_json`
    是模块私有、路径拼接也不该让调用方自己拼。

    **不重新算一遍计划**：配置改了、后端掉了之后重算得到的是"现在会选谁"，
    与人问的"当时用了谁"是两件事。
    """
    if not meeting_name:
        return {}
    # 目录名只取最后一段：库里的 name 正常不会带分隔符，但这条路径是**读文件**，
    # 值得花一行把 `..` / 分隔符挡在门外（api.py 的 `_meeting_dirname` 同理）。
    safe = os.path.basename(str(meeting_name).replace("\\", "/").rstrip("/"))
    if not safe:
        return {}
    path = os.path.join(meetings_dir(), safe, "meta.json")
    data = _load_json(path, {})
    return data if isinstance(data, dict) else {}


def delete_meeting(meeting_id):
    """删除会议：DB 记录 + （可选）音频文件。"""
    meeting = db.get_meeting(meeting_id)
    if not meeting:
        return False, "会议不存在"
    name = meeting["name"]
    if _state["active"] and os.path.basename(_state["folder"] or "") == name:
        return False, "该会议正在录音中，不能删除"
    keep_audio = settings.get("meetingKeepRawAudio", True)
    if not keep_audio:
        import shutil
        folder = os.path.join(meetings_dir(), name)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    # 注意顺序：_drop_summary_session 需要用会议名去查映射表，若先删了会议记录
    # 就拿不到 name（_session_key 会退化成 id 字符串），映射和会话都会清不掉。
    _drop_summary_session(name)
    db.delete_meeting(meeting_id)
    db.add_log("info", "meeting", f"已删除会议 {name}")
    return True, "已删除"


def clean_short_meetings(max_seconds=120):
    """清理时长 ≤ max_seconds 的会议（DB + 音频/转写文件，彻底删除）。

    返回被删除的会议列表 [{id, name, duration}]；正在录音的会议跳过。
    """
    import shutil
    removed = []
    for m in db.list_meetings(limit=1000):
        dur = m.get("duration_seconds") or 0
        if dur > max_seconds:
            continue
        name = m["name"]
        if _state["active"] and os.path.basename(_state["folder"] or "") == name:
            continue   # 正在录音，跳过
        folder = os.path.join(meetings_dir(), name)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        # 同 delete_meeting：先清会话映射（需要会议名），再删会议记录
        _drop_summary_session(name)
        db.delete_meeting(m["id"])
        removed.append({"id": m["id"], "name": name, "duration": dur})
    if removed:
        db.add_log("info", "meeting", f"已清理 {len(removed)} 个短会议（≤{max_seconds}s）")
    return removed


def import_transcript_fallback():
    """（预留）旧会议迁移：历史 transcript.md 的首次导入。旧数据不随本仓库提供，暂不实现。"""
    pass
