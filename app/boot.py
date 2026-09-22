# -*- coding: utf-8 -*-
"""boot.py — ECHO 启动编排器

阶段 0：面板 HTTP 立即可用（main.py 同步完成，server 直接 online）。
阶段 1+：后台线程按依赖分阶段拉起各组件，各自独立状态/进度，可重试/启停。

状态机：pending → starting → online | failed | disabled | skipped | idle
  idle 用于"按需"组件（会议转写引擎），表示已就绪但未加载模型。
  skipped 用于"你选了别的那个"的组件（两个智能体是二选一）：它**不是**故障 ——
  启动页不该把它算进"失败"，更不该教用户去启动一个他没选的组件
  （2026-09-22 同事反馈：选了标准版 harness，启动页却报「DSH 执行引擎 失败」并让他去开
  DSH Desktop；上一条同类误导是"dsh offline 是不是坏了"，换个页面又出现）。
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


def selected_agent():
    """用户**当前选中**的智能体名（原始设置，不走 `agents.active_name()` 的降级探测）。

    为什么不用 active_name()：那个会为了降级去**探测可用性**，而这里要回答的是
    "用户选的是哪个" —— 未选中的组件该标 skipped，而不是去猜它能不能用。
    实现在 `agents.selected_name()`：折叠条与 `/api/status` 也要同一口径，
    别各写一份（2026-09-22 那类"拿另一个适配器的状态报失败"的事故就是这么来的）。
    """
    try:
        from app import agents
        return agents.selected_name()
    except Exception:
        return ""


def _agent_choice_detail(want):
    """「未使用（你选的是 X）」——X 用组件的显示名，别让用户去猜内部 id。"""
    label = (_COMPONENTS.get(want) or {}).get("label") or want or "别的智能体"
    return "未使用（你选的是 %s）" % label


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
    # 阶段 1b：独立 harness（选了它才真拉起来；没选就是一行 disabled，不等它）
    _spawn(["harness"])
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
    # skipped 计入就绪：它表示"你选了另一个智能体"，不是待办事项 ——
    # 否则选了标准版 harness 的机器会永远显示「就绪 10/11 · 失败 1」。
    ready = sum(1 for c in comps if c["status"] in ("online", "idle", "disabled", "skipped"))
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
    # 两个智能体是**二选一**：没选桌面版就别去启动它，更别把"没启动"记成失败。
    want = selected_agent()
    if want and want != "dsh":
        report(status="skipped", detail=_agent_choice_detail(want), progress=0.0)
        return
    report(detail="探测中…", progress=0.1)
    if manager.dsh_ready():
        report(status="online", detail="已运行 · API 可访问", progress=1.0)
        return
    ok, msg = manager.dsh_start()
    if not ok:
        report(status="failed", detail=msg, error=msg)
        return
    report(status="online", detail=msg, progress=1.0)


def _start_harness(report):
    """独立 DeepSeek Harness（npm @deepseek-ai/dsh）：选了它才随 ECHO 启动。

    2026-09-19 加：用户要求"在智能体那里增加一个新的 agent 类型，然后随着 echo 一起启动"。
    实测它与 DSH Desktop 的 /api 接口面完全一致，只是鉴权换成"token → Cookie"，
    家目录独立（默认 {DATA}/harness），端口默认 43199（与 Desktop 的 43120 并存）。
    """
    from app import harness_proc
    if not harness_proc.requested():
        # 自愈：上次是我们起的、这次没被选中 → 顺手收掉（否则切回 DSH 后 node 一直挂着）
        if harness_proc._load_pid() or harness_proc.started_by_echo():
            ok, msg = harness_proc.stop(reason="启动自愈：当前没选中它，收掉上次 ECHO 起的实例")
            report(status="skipped" if ok else "failed",
                   detail="未使用，已收尾：%s" % msg if ok else msg)
            return
        harness_proc.sync_status()
        # skipped（不是 disabled）：这不是"坏了也没启用"，而是"你选了另一个"，不该计入失败
        report(status="skipped", detail=_agent_choice_detail(selected_agent()),
               progress=0.0)
        return
    report(detail="拉起独立 harness…", progress=0.2)
    ok, msg = harness_proc.ensure_running()
    # 无论成功/失败/接手都要把状态行写实，别让它停在"启动中"
    harness_proc.sync_status()
    if not ok:
        report(status="failed", detail=msg, error=msg)
        return
    # 标准版的家目录是**它启动时**才建出来的，而"注册 ECHO AUTO"那一步（_start_failover）
    # 跑在阶段 1、可能早于这里 —— 所以智能体就绪后补一次注册（幂等），
    # 否则只装标准版的机器第一次启动时 DSH 里选不到 ECHO AUTO。
    _register_router_after_agent_start()
    report(status="online", detail=msg, progress=1.0)


#: 智能体就绪后"再复核几次 ECHO AUTO 注册"的后台线程开关。
#: 默认开。**测试里必须关掉**（`tests/test_harness_agent.py` 的 setUpModule 会关）：
#: 那个线程会调**真实**的 `llm_router.sync()` 去写 DSH 配置文件，而 AGENTS.md 明令
#: 测试不许碰真实 DSH 状态；线程还会活到测试结束之后。
_ROUTER_RECHECK_ENABLED = True
_ROUTER_RECHECK_ATTEMPTS = 8          # 8 × 15s ≈ 2 分钟
_ROUTER_RECHECK_INTERVAL = 15.0


def _register_router_after_agent_start():
    """智能体起来后补一次 ECHO AUTO 注册（幂等），并**重算 failover 的文案**。

    前半句是原有行为：标准版的家目录是**它启动时**才建出来的，而注册那步跑在阶段 1、
    可能早于这里，所以智能体就绪后要补一次。
    后半句是 2026-09-22 同事反馈的修复：阶段 1 探测出的"标准版 harness 没在监听 43199"
    会一直挂在面板 detail 上，harness 真起来了也不刷新。

    再往后还有一层（同一条反馈的 3.3）：注册依赖的东西可能要**更久**才就绪 ——
    harness 起来之后才写 settings.yaml。所以这一次没成的话，隔一会儿再复核几次，
    每次都按当时的状态重写文案；哪天成了就停。
    """
    try:
        done = _refresh_failover_detail(note="智能体就绪后复核")
    except Exception as exc:
        db.add_log("warn", "boot", "智能体就绪后复核 ECHO AUTO 注册失败：%s" % exc)
        return
    if done or not _ROUTER_RECHECK_ENABLED:
        return
    threading.Thread(target=_router_recheck_loop, daemon=True,
                     name="router-recheck").start()


def _router_recheck_loop():
    """隔一会儿再复核；成了就停（最多 _ROUTER_RECHECK_ATTEMPTS 次）。"""
    for i in range(1, _ROUTER_RECHECK_ATTEMPTS + 1):
        time.sleep(_ROUTER_RECHECK_INTERVAL)
        if not _ROUTER_RECHECK_ENABLED:
            return
        try:
            if _refresh_failover_detail(note="延迟复核 #%d" % i):
                return
        except Exception as exc:
            db.add_log("warn", "boot", "延迟复核 ECHO AUTO 注册失败：%s" % exc)
    db.add_log("warn", "boot",
               "复核 %d 次仍没能把 ECHO AUTO 注册进 DSH（面板上写的是最新结论）"
               % _ROUTER_RECHECK_ATTEMPTS)


def _stop_harness():
    from app import harness_proc
    harness_proc.stop(reason="面板/接口手动停止")


def _agent_dsh_available():
    """有没有**可注册的** DSH（桌面版或标准版任一在跑）。返回 ``(ok, reason)``。

    为什么要这道闸（D25）：把模型组写进 DSH 家目录的 settings.yaml + .credentials.yaml
    是**给 agent 用的**——没装 agent 时写了没人读，还会平白在用户家里建/改配置文件。
    而**路由本身照常运行**：它是 ECHO 的 LLM provider（`echo-auto`），不是 agent 的附属
    （纪要/命令可以直接经它直连上游，这条已在 P5 打通）。
    手动"注册到 DSH"的按钮不受此限制 —— D25 说的是自动那一半。

    2026-09-22 更正判据：原来只看桌面版适配器，于是"只装标准版"（向导的默认选择）
    永远注册不上 —— 同事不一定两个都装。现在两个适配器问一遍，任一可用即放行；
    具体写到哪个家目录由 `llm_router.dsh_homes()` 按**实际存在**的家目录决定。
    """
    try:
        from app import agents
        names = agents.names()
    except Exception as e:
        return False, "检测 agent-dsh 失败：%s" % e
    if "dsh" not in names and "harness" not in names:
        return False, "未安装 agent-dsh（内置适配器未注册）"
    # **先试用户选中的那个**，再试另一个。
    # 为什么顺序要紧（2026-09-22 同事反馈 3.5）：下面那句"未找到 DSH 凭据文件
    # （DSH Desktop 是否已登录过？）"是**桌面版**的原因 —— 用户明明选的是标准版
    # harness，却被问"桌面版登录过没有"，等于又把他往没选的那个上引（同一类误导
    # 在本轮已经出现过三次：安装脚本判据、启动页、折叠条）。
    selected = selected_agent()
    order = [selected] + [n for n in ("dsh", "harness") if n != selected]
    reasons = []
    for name in order:
        if name not in names:
            continue
        try:
            ok, why = agents.get_agent(name).available()
        except Exception as e:
            ok, why = False, "%s 探测失败：%s: %s" % (name, type(e).__name__, e)
        if ok:
            return True, why
        reasons.append((name, why))
    if not reasons:
        return False, "没有可用的 DSH 智能体"
    # 只说**选中的那个**的原因；另一个的原因压在括号里，不喧宾夺主
    head = reasons[0]
    if len(reasons) == 1:
        return False, head[1]
    return False, "%s（另外 %s 也不可用）" % (head[1], reasons[1][0])


# 模型路由自身的基础描述（"运行中 · http://…"）。failover 组件的 detail = 它 + 「注册进 DSH 的情况」。
# 为什么拆成两半：注册那步跑在**阶段 1**，可能早于 harness 起来 —— 当时探测到的
# "标准版 harness 没在监听 43199" 会被**烤进** detail 字符串，等 harness 真起来之后面板还在念
# （2026-09-22 同事反馈：排障时被这句自相矛盾的文案带偏，尤其"node 不在 PATH"那句指向的正是
# 已经修好的那条）。拆开后，harness 就绪时重算后半句即可。
_ROUTER_DETAIL = ""


def _refresh_failover_detail(note="启动时", emit=None):
    """按**当前**状态重写 failover 组件的 detail（幂等，可反复调用）。

    `emit`：组件上报函数。默认走模块级 `report("failover", …)`；
    `_start_failover` 把它**注入的那个** report 传进来 —— 别在这里改成全局调用，
    那会把可测试的接缝拆掉（`tests/test_failover_boot.py` 正是靠注入点断言的）。

    返回 True 表示"这一轮不用再试了"：注册成功、或按设置根本没打算注册。
    返回 False 表示这次没注册上（智能体还没就绪 / 家目录还没初始化）—— 调用方可以稍后重试。
    """
    if not _ROUTER_DETAIL:
        return True
    if emit is None:
        emit = lambda **kw: report("failover", **kw)      # noqa: E731
    base = _ROUTER_DETAIL
    try:
        from app.config import settings as _s
        if not _s.get("routerAutoRegister", True):
            emit(status="online", detail=f"{base} · 已按设置跳过 ECHO AUTO 注册", progress=1.0)
            return True
        agent_ok, agent_why = _agent_dsh_available()
        if not agent_ok:
            # D25：没装 agent 就不动 DSH 的配置文件；路由照常可用
            emit(status="online",
                 detail=f"{base} · 未注册进 DSH（{agent_why}）· 路由本身可用", progress=1.0)
            db.add_log("info", "boot", f"[{note}] 跳过 ECHO AUTO 注册：{agent_why}")
            return False
        from app import llm_router
        rok, rdetail = llm_router.sync()
        if rok:
            emit(status="online", detail=f"{base} · {rdetail}", progress=1.0)
            db.add_log("info", "boot", f"[{note}] ECHO AUTO 注册：{rdetail}")
            return True
        emit(status="online", detail=f"{base} · ECHO AUTO 未注册（{rdetail}）", progress=1.0)
        print(f"[boot] ECHO AUTO 注册失败: {rdetail}")
        return False
    except Exception as exc:
        emit(status="online",
             detail=f"{base} · ECHO AUTO 注册异常（{type(exc).__name__}: {exc}）", progress=1.0)
        print(f"[boot] ECHO AUTO 注册异常: {exc}")
        return False


def _start_failover(report):
    """确保模型路由在运行并起守护线程；**装了 agent-dsh 时**才顺带注册进 DSH（D25）。"""
    global _ROUTER_DETAIL
    from app import failover_proxy
    report(detail="探测路由端口…", progress=0.2)
    ok, detail = failover_proxy.start_guard()
    if not ok:
        report(status="failed", detail=detail, error=detail)
        return
    _ROUTER_DETAIL = detail
    report(detail="注册 ECHO AUTO…", progress=0.7)
    # 顺带把 ECHO 的模型组（config.json 的 groups）注册成 DSH 的本地模型；
    # 注册失败不影响路由本身，只是 DSH 里选不到 ECHO AUTO。
    _refresh_failover_detail(note="启动时", emit=report)


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
    from app import platform as echo_platform
    eng = settings.get("ttsEngine", "auto")
    online = tts.probe_online()
    offline = echo_platform.offline_tts_display()
    # 离线引擎的配置值按平台不同（Windows=sapi / macOS=say / Linux=espeak）
    offline_ids = {"sapi", str(echo_platform.offline_tts_label() or "")}
    if eng in offline_ids:
        detail = "离线 · %s" % offline
    elif eng == "edge-tts":
        detail = "在线 · edge-tts" if online else "在线(不可达，将回退 %s)" % offline
    else:
        detail = "在线 · edge-tts" if online else "离线 · %s（在线不可达）" % offline
    report(status="online", detail=detail, progress=1.0)


def _start_wake(report):
    from app.config import settings
    from app.audio.wake import engine_label
    from app import runtime
    if not settings.get("wakeEnabled", False):
        report(status="disabled", detail="未启用", progress=0.0)
        return
    ok, msg = runtime.start_wake()
    if ok:
        report(status="online", detail=engine_label(), progress=1.0)
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
    register("harness", "标准版 harness", "🧩", start_fn=_start_harness,
             stop_fn=_stop_harness, can_start=True, can_stop=True)
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
