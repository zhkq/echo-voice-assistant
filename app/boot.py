# -*- coding: utf-8 -*-
"""boot.py — ECHO 启动编排器

阶段 0：面板 HTTP 立即可用（main.py 同步完成，server 直接 online）。
阶段 1+：后台线程按依赖分阶段拉起各组件，各自独立状态/进度，可重试/启停。

状态机：pending → starting → online | failed | disabled | idle
  idle 用于"按需"组件（会议转写引擎），表示已就绪但未加载模型。
"""
import threading
import time

import app.db as db
from app import services

_LOCK = threading.RLock()
_PHASE = "init"
_COMPONENTS = {}   # cid -> dict
_ORDER = []

# 组件 id → (仪表盘服务组件名, 上报函数)。stt-meeting 不映射到仪表盘（仪表盘 stt=命令引擎）。
_SERVICE_REPORTERS = {
    "server": services.report_server,
    "dsh": services.report_dsh,
    "stt-cmd": services.report_stt,
    "tts": services.report_tts,
    "wake": services.report_wake,
    "hotkey": services.report_hotkey,
    "meeting": services.report_meeting,
    "diarize": services.report_diarize,
}


def _log(cid, level, msg):
    try:
        db.add_log(level, "boot", f"[{cid}] {msg}")
    except Exception:
        pass


def register(cid, label, icon, start_fn=None, stop_fn=None, can_start=True,
             can_stop=False, deps=(), kind="service", status="pending"):
    """登记组件。

    幂等（2026-09-19 安全网）：重复 register 同一个 id 只覆盖定义、**不重复入列**。
    原来每次都往 `_ORDER` 追加，于是 setup() 被调用两次（测试里很常见，未来热重载
    也可能）时 snapshot() 会把同一个组件列两遍、summary.total 也跟着翻倍。
    """
    with _LOCK:
        _COMPONENTS[cid] = {
            "id": cid, "label": label, "icon": icon,
            "status": status, "detail": "", "progress": 0.0, "substep": "",
            "started_at": None, "duration": 0.0, "error": "",
            "can_start": can_start, "can_stop": can_stop,
            "deps": list(deps), "kind": kind,
            "_start_fn": start_fn, "_stop_fn": stop_fn,
        }
        if cid not in _ORDER:
            _ORDER.append(cid)


def set_phase(p):
    global _PHASE
    with _LOCK:
        _PHASE = p


def report(cid, status=None, detail="", progress=None, substep="", error=""):
    """组件上报（线程安全），同步仪表盘 services 状态 + 关键状态写日志。"""
    with _LOCK:
        c = _COMPONENTS.get(cid)
        if not c:
            return None
        if status is not None:
            c["status"] = status
        if detail:
            c["detail"] = detail
        if error:
            c["error"] = error
        if progress is not None:
            c["progress"] = float(progress)
        if substep:
            c["substep"] = substep
        if status == "starting" and c["started_at"] is None:
            c["started_at"] = time.time()
        if status in ("online", "failed") and c["started_at"]:
            c["duration"] = round(time.time() - c["started_at"], 1)
        snap = dict(c)
    fn = _SERVICE_REPORTERS.get(cid)
    if fn and status:
        try:
            if cid == "server":
                fn()
            else:
                fn(status, detail or snap["detail"] or snap["error"] or "")
        except Exception:
            pass
    if status == "failed":
        _log(cid, "error", f"{snap['label']} 失败: {error or detail}")
    elif status == "online":
        _log(cid, "info", f"{snap['label']} 就绪（{detail}）")
    return snap


def _run_start(cid):
    with _LOCK:
        c = _COMPONENTS.get(cid)
        if not c:
            return
        fn = c["_start_fn"]
    report(cid, status="starting", detail="启动中…", progress=0.0)
    if fn is None:
        report(cid, status="online", detail="内置")
        return
    try:
        fn(lambda **kw: report(cid, **kw))
    except Exception as e:
        report(cid, status="failed", detail=str(e)[:200], error=str(e))
        return
    with _LOCK:
        if _COMPONENTS[cid]["status"] == "starting":
            report(cid, status="online", detail="已启动")


def start_component(cid):
    """手动启动/重试（幂等：starting/online 时拒绝）。"""
    with _LOCK:
        c = _COMPONENTS.get(cid)
        if not c:
            return False, "组件不存在"
        if c["status"] == "starting":
            return False, "正在启动中"
        if not c["can_start"]:
            return False, "该组件不可手动启动"
    threading.Thread(target=_run_start, args=(cid,), daemon=True).start()
    return True, "已开始启动"


def stop_component(cid):
    with _LOCK:
        c = _COMPONENTS.get(cid)
        if not c:
            return False, "组件不存在"
        fn = c["_stop_fn"]
    if not fn:
        return False, "该组件不可停止"
    try:
        fn()
        report(cid, status="idle", detail="已停止", progress=0.0)
        return True, "已停止"
    except Exception as e:
        return False, str(e)


def start_all_async():
    """后台分阶段拉起全部组件（立即返回）。"""
    threading.Thread(target=_run_boot, daemon=True).start()


def _spawn(cids):
    for cid in cids:
        with _LOCK:
            c = _COMPONENTS.get(cid)
            if not c:
                continue
            if c["status"] in ("online", "starting", "disabled"):
                continue
        threading.Thread(target=_run_start, args=(cid,), daemon=True).start()


def _run_boot():
    set_phase("booting")
    # 阶段 1：轻量组件（秒级）并行 —— 模型路由先确保起来，DSH 随时可能被调用
    _spawn(["failover", "dsh", "tts", "hotkey", "meeting", "diarize"])
    # 阶段 2：重组件（模型加载）并行 —— 命令转写常驻；会议转写按需不在此启动
    _spawn(["stt-cmd", "wake"])
    # 等所有非按需组件 settle
    while True:
        with _LOCK:
            running = [c["id"] for c in _COMPONENTS.values()
                       if c["status"] == "starting"]
        if not running:
            break
        time.sleep(0.5)
    set_phase("done")


def snapshot():
    """启动页状态快照。"""
    with _LOCK:
        phase = _PHASE
        comps = [dict(_COMPONENTS[c]) for c in _ORDER]
    for c in comps:
        c.pop("_start_fn", None)
        c.pop("_stop_fn", None)
    total = len(comps)
    ready = sum(1 for c in comps if c["status"] in ("online", "idle", "disabled"))
    failed = sum(1 for c in comps if c["status"] == "failed")
    running = sum(1 for c in comps if c["status"] == "starting")
    return {
        "phase": phase,
        "summary": {"total": total, "ready": ready, "failed": failed,
                    "running": running, "pending": total - ready - failed - running},
        "components": comps,
    }


# ---------------------------------------------------------------- 组件启动函数

def _start_dsh(report):
    from app import manager
    report(detail="探测中…", progress=0.1)
    if manager.dsh_ready():
        report(status="online", detail="已运行 · API 可访问", progress=1.0)
        return
    ok, msg = manager.dsh_start()
    if not ok:
        report(status="failed", detail=msg, error=msg)
        return
    report(status="online", detail=msg, progress=1.0)


def _start_failover(report):
    """确保 DSH 模型路由(8899) 在运行，启动 30s 守护线程，并把模型组注册进 DSH。"""
    from app import failover_proxy
    report(detail="探测 8899…", progress=0.2)
    ok, detail = failover_proxy.start_guard()
    if not ok:
        report(status="failed", detail=detail, error=detail)
        return
    # 顺带把 ECHO 的模型组（config.json 的 groups）注册成 DSH 的本地模型；
    # 注册失败不影响路由本身，只是 DSH 里选不到 ECHO AUTO。
    from app.config import settings as _s
    if not _s.get("routerAutoRegister", True):
        report(status="online", detail=f"{detail} · 已按设置跳过 ECHO AUTO 注册", progress=1.0)
        return
    try:
        from app import llm_router
        report(detail="注册 ECHO AUTO…", progress=0.7)
        rok, rdetail = llm_router.sync()
        detail = f"{detail} · {rdetail}" if rok else f"{detail} · ECHO AUTO 未注册（{rdetail}）"
        if not rok:
            print(f"[boot] ECHO AUTO 注册失败: {rdetail}")
    except Exception as exc:
        detail = f"{detail} · ECHO AUTO 注册异常（{type(exc).__name__}: {exc}）"
        print(f"[boot] ECHO AUTO 注册异常: {exc}")
    report(status="online", detail=detail, progress=1.0)


def _stt_engine_and_model(setting_key):
    from app.config import settings
    from app.audio import stt
    choice = settings.get(setting_key, "sensevoice")
    eng, model = stt.resolve_engine(choice)
    return stt, eng, model


def _start_stt_cmd(report):
    from app.config import settings
    stt, eng, model = _stt_engine_and_model("sttModel")
    key = stt.engine_key(eng, model)
    old = _BOOT_STT_KEYS.get("stt-cmd")
    if old and old != key:
        stt.unload_key(old)
    if stt.key_loaded(key):
        _BOOT_STT_KEYS["stt-cmd"] = key
        report(status="online", detail=f"{eng} · {stt.device_label()}", progress=1.0)
        return
    report(detail=f"加载 {model if eng == 'whisper' else eng} …", substep="模型加载", progress=0.1)
    key = stt.load_engine(eng, model, settings.get("device", "auto"))
    _BOOT_STT_KEYS["stt-cmd"] = key
    report(status="online", detail=f"{eng} · {stt.device_label()}", substep="", progress=1.0)


def _start_stt_meeting(report):
    from app.config import settings
    stt, eng, model = _stt_engine_and_model("meetingSttModel")
    key = stt.engine_key(eng, model)
    old = _BOOT_STT_KEYS.get("stt-meeting")
    if old and old != key:
        stt.unload_key(old)
    if stt.key_loaded(key):
        _BOOT_STT_KEYS["stt-meeting"] = key
        report(status="online", detail=f"{eng} · {stt.device_label()}", progress=1.0)
        return
    report(detail=f"加载 {model if eng == 'whisper' else eng} …", substep="模型加载", progress=0.1)
    key = stt.load_engine(eng, model, settings.get("device", "auto"))
    _BOOT_STT_KEYS["stt-meeting"] = key
    report(status="online", detail=f"{eng} · {stt.device_label()}", substep="", progress=1.0)


def _stop_stt_cmd():
    from app.audio import stt
    key = _BOOT_STT_KEYS.pop("stt-cmd", None)
    if key:
        stt.unload_key(key)


def _stop_stt_meeting():
    from app.audio import stt
    key = _BOOT_STT_KEYS.pop("stt-meeting", None)
    cmd_key = _BOOT_STT_KEYS.get("stt-cmd")
    # 会议引擎与命令引擎同模型时共享实例，停止会议引擎不卸载共享模型
    if key and key != cmd_key:
        stt.unload_key(key)


def note_stt_loaded(cid, key):
    """外部（会议转写）加载了 STT 引擎后同步 boot 记录的 key。"""
    _BOOT_STT_KEYS[cid] = key


def _start_tts(report):
    from app.config import settings
    from app.audio import tts
    eng = settings.get("ttsEngine", "auto")
    online = tts.probe_online()
    if eng == "sapi":
        detail = "离线 · Windows 慧慧"
    elif eng == "edge-tts":
        detail = "在线 · edge-tts" if online else "在线(不可达，将回退 SAPI)"
    else:
        detail = "在线 · edge-tts" if online else "离线 · Windows 慧慧（在线不可达）"
    report(status="online", detail=detail, progress=1.0)


def _start_wake(report):
    from app.config import settings
    from app import runtime
    if not settings.get("wakeEnabled", False):
        report(status="disabled", detail="未启用", progress=0.0)
        return
    ok, msg = runtime.start_wake()
    if ok:
        report(status="online", detail="sherpa KWS", progress=1.0)
    else:
        report(status="failed", detail=msg, error=msg)


def _stop_wake():
    from app import runtime
    runtime.stop_wake()


def _start_hotkey(report):
    from app import runtime
    ok, msg = runtime.start_hotkey()
    if ok:
        report(status="online", detail="组合键 + 媒体键", progress=1.0)
    else:
        report(status="failed", detail=msg, error=msg)


def _stop_hotkey():
    from app import runtime
    runtime.stop_hotkey()


def _start_meeting(report):
    from app import meeting
    meeting.recover_orphaned_meetings()
    st = meeting.meeting_status()
    report(status="online", detail="录音中" if st["active"] else "就绪", progress=1.0)


def _start_diarize(report):
    """说话人分离：复用 modelinfo 的校验（模型文件齐全才算就绪，空目录不算）。"""
    try:
        from app import modelinfo
        ok = modelinfo.ready_pyannote()
    except Exception as e:
        # 校验本身出错（依赖/路径异常）不能静默当成"模型缺失"，否则用户只看到"缺失"没法排查
        _log("diarize", "warn", f"说话人分离就绪校验失败: {e}")
        ok = False
    if ok:
        detail = "pyannote 就绪"
        try:      # 顺带报一下常用联系人声纹库（有样本时才显示）
            from app import voiceprint
            st = voiceprint.library_stats()
            if st["samples"]:
                detail += f" · 声纹库 {st['contacts']} 人/{st['samples']} 条"
        except Exception:
            pass
        report(status="online", detail=detail, progress=1.0)
    else:
        report(status="disabled", detail="模型缺失（设置 → 模型 → 说话人分离）", progress=0.0)


# 已加载 STT 引擎 key（stt-cmd / stt-meeting），供卸载与状态判断
_BOOT_STT_KEYS = {}


def setup():
    """注册全部组件（main.py 阶段 0 调用）。"""
    register("server", "面板服务", "🖥️", start_fn=None, can_start=False,
             status="online")
    register("dsh", "DSH 执行引擎", "⚙️", start_fn=_start_dsh, can_start=True,
             can_stop=False)
    register("failover", "模型路由（ECHO AUTO）", "🛰️", start_fn=_start_failover,
             can_start=True, can_stop=False)
    register("stt-cmd", "命令转写引擎（常驻）", "🎤", start_fn=_start_stt_cmd,
             stop_fn=_stop_stt_cmd, can_start=True, can_stop=True)
    register("stt-meeting", "会议转写引擎（按需）", "📝", start_fn=_start_stt_meeting,
             stop_fn=_stop_stt_meeting, can_start=True, can_stop=True,
             kind="model", status="idle", )
    register("tts", "语音合成", "🔊", start_fn=_start_tts, can_start=True,
             can_stop=False)
    register("wake", "语音唤醒", "🗣️", start_fn=_start_wake, stop_fn=_stop_wake,
             can_start=True, can_stop=True)
    register("hotkey", "热键/媒体键", "⌨️", start_fn=_start_hotkey,
             stop_fn=_stop_hotkey, can_start=True, can_stop=True)
    register("meeting", "会议录音", "📼", start_fn=_start_meeting, can_start=False)
    register("diarize", "说话人分离", "👥", start_fn=_start_diarize, can_start=False)
