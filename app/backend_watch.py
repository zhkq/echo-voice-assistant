# -*- coding: utf-8 -*-
"""本地能力后端的看门狗（2026-10-11 用户要求："如果配置是本地后端，watchdog 也应该监控后端"）。

**为什么需要它**：`scripts/echo-supervisor.ps1` 只盯**实例**（ECHO 自己在不在跑、别的实例是否
也在跑），**完全不看能力后端**。而"本机后端"是 ECHO 自己起的（pid 记在
`<数据根>/logs/backend.pid`），它可能**掉一半**：进程还活着、只占着管理口，数据口 8900 不再监听
—— 2026-10-11 08:5x 用户实测正是这个形态（pid 10164 只占 8901、8900 空着），面板一路如实报
"不可用"，但**没有任何人负责把它拉回来**。（注意这与"面板说谎"是两件事：用户 08:5x 确认过，
面板显示"不可用"是**对的**；看着像在跑的是桌面工具包那张表，那一处已经修了。）

**三条铁律**（前两条是这一晚反复踩出来的纪律）：
1. **只碰 ECHO 自己那份**：判据 `backend_pid.read_pid() > 0`（ECHO 起过它）。判不出归属、
   或压根没有 pid 记录 → **一律不动手**，只留一句"不归我管"（判不出就不动的原则同
   `backend_proc` 的归属判据）。
2. **不打断用户**：会议在录音、或已有一次"起本机后端"的任务在跑 → 这一轮什么都不做，
   下一轮再说（同 AGENTS.md"动手前先看 `meeting.active`"）。
3. **有退避与上限**：失败按 60s → 120s → 240s… 退避，**一小时内最多 3 次**；超了就不再自动拉，
   改成"需要你看一眼"。理由：这是个 3 GB 量级的运行时，把它拉成死循环比不拉更糟。

端口**从后端自己的 `server.yaml` 读**（`backend_setup.configured_ports()`）—— 与
`tests/test_capability_admin.py` 盯的那条一致：端口是后端自己的设置，客户端不许再存一份。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("echo.backend_watch")

#: 多久看一次（秒）。30 秒足够"掉了一会儿就拉回来"，也不至于把 8900 探成负担。
WATCH_INTERVAL = 30
#: 一小时内最多自动拉起几次（超了就只报，不再动手）。
MAX_RESTARTS_PER_HOUR = 3
#: 失败退避的基数（秒）：第 n 次失败后退避 BASE * 2^(n-1)。
BACKOFF_BASE = 60

#: 「明确停过」之后多久不再自动拉起（秒）。
#:
#: 为什么需要它（2026-10-11 实测出来的）：没有这一条，看门狗会**跟用户自己的「停止后端」对着干**
#: —— 用户点了停止（或运维按 AGENTS.md 的纪律"跑门禁前先停后端"），30 秒后它又给拉起来：
#:   * 用户的意图被无声推翻（点了没反应，过一会儿又回来了）；
#:   * 全量门禁要求 8900/8901 **空着**，于是门禁根本没法跑。
#: 语义：**明确停 = 这一次听你的**；30 分钟后或你重新点「启动」就恢复自动看护。
PAUSE_AFTER_STOP = 1800

_STATE = {
    "paused_until": 0.0,  # 「明确停过」的退避截止（0 = 没有）
    "paused_note": "",
    "checks": 0,          # 看过几次
    "ok": 0,              # 看到"两个口都在"
    "restarts": [],       # 每次真的动手的时间戳
    "last": {},           # 最近一次 tick 的结果
    "note": "",           # 给人看的一句话（超限、不归我管…）
}
_LOCK = threading.Lock()
_THREAD = None


def wanted() -> tuple:
    """该不该由 ECHO 看着它：``(要?, 依据)``。

    ⚠️ **不能只认 pid 记录**（这是我第一版的错，2026-10-11 真机复现出来了）：进程一被杀，
    别的代码路径（`backend_admin.view()` 的"过期清理"）会**把 pid 文件删掉**，
    于是看门狗以为"不是我起的"、什么都不做 —— 而那一刻恰恰是最该动手的时候。
    所以判据是**两条任一**：

      ① 有 pid 记录（ECHO 起的，最快路径）；
      ② **本机配对还在**（`backend_admin.view()` 的 `pairFile`/`paired` 说后端地址是回环）——
         这表示"当前用的就是这个本机后端"，它掉了当然该拉回来。

    两条都不成立（没有 pid 记录、也没有指向本机的配对）→ **不碰**：可能是用户自己起的、
    或用的远端后端，那不是我们该管的。
    """
    try:
        from app import backend_pid
        if int(backend_pid.read_pid() or 0) > 0:
            return True, "ECHO 起的（有 pid 记录）"
    except Exception:
        pass
    try:
        from app import backend_admin
        v = backend_admin.view() or {}
        pf = v.get("pairFile") or {}
        pr = v.get("paired") or {}
        url = str(pf.get("baseUrl") or pr.get("baseUrl") or "")
        loopback = ("127.0.0.1" in url) or ("localhost" in url) or ("[::1]" in url)
        if (pf.get("found") or pr.get("paired")) and loopback:
            return True, "本机配对还在（%s）" % url
    except Exception:
        pass
    return False, "既没有 pid 记录，也没有指向本机的配对 —— 不碰"


def healthy() -> tuple:
    """两个口都在听吗？``(健康?, 一句话)``。

    为什么**两个都要**：用户实测的那个形态就是"只有 8901 在听" —— 只探一个口会判成健康，
    于是 8900 掉了永远没人管。
    """
    try:
        from app import backend_setup, platform
        data_port, admin_port = backend_setup.configured_ports()
    except Exception as e:                                   # noqa: BLE001
        return True, "读不到端口配置（%s）—— 不猜，先放过" % str(e)[:80]
    bad = []
    for label, port in (("数据口", data_port), ("管理口", admin_port)):
        try:
            pid = int(platform.listening_pid(int(port)) or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            bad.append("%s %s 没在听" % (label, port))
    if bad:
        return False, "；".join(bad)
    return True, "两个口都在听"


def pause(reason: str = "用户明确停掉了后端", seconds: int = PAUSE_AFTER_STOP) -> float:
    """记一次「明确停过」—— 这段时间内看门狗**不自动拉起**（听用户的）。返回截止时间戳。"""
    until = time.time() + max(0, int(seconds))
    with _LOCK:
        _STATE["paused_until"] = until
        _STATE["paused_note"] = "%s（%d 分钟内不自动拉起）" % (reason, int(seconds) // 60)
    log.info("backend_watch: 收到明确停止 —— %s", _STATE["paused_note"])
    return until


def paused(now: float = None) -> str:
    """现在是否处于"明确停过"的退避期；是则返回原因，否则空串。"""
    now = float(now if now is not None else time.time())
    with _LOCK:
        until = float(_STATE.get("paused_until") or 0.0)
        note = _STATE.get("paused_note") or ""
    if until and now < until:
        return note or "明确停过"
    return ""


def busy() -> str:
    """现在**不该动手**的理由；空串 = 能动。"""
    try:
        from app import backend_admin
        job = (backend_admin.view() or {}).get("job") or {}
        if job.get("running"):
            return "已经有一次「起本机后端」在跑（第 %s 步）" % (job.get("stage") or "?")
    except Exception:
        pass
    try:
        from app import meeting
        if (meeting.meeting_status() or {}).get("active"):
            return "会议正在录音"
    except Exception:
        pass
    return ""


def _restarts_recent(now: float) -> list:
    return [t for t in _STATE["restarts"] if now - t < 3600]


def tick(now: float = None) -> dict:
    """看一次。返回 ``{action, detail}``；``action`` ∈ ok / skip / busy / backoff / capped / restart。"""
    now = float(now if now is not None else time.time())
    with _LOCK:
        _STATE["checks"] += 1

    def done(action, detail=""):
        out = {"action": action, "detail": detail, "at": now}
        with _LOCK:
            _STATE["last"] = out
            if action in ("capped", "skip"):
                _STATE["note"] = detail
            elif action in ("ok", "restart"):
                _STATE["note"] = ""
        if action in ("restart", "capped"):
            log.warning("backend_watch: %s (%s)", action, detail)
        return out

    want, why_want = wanted()
    if not want:
        return done("skip", "这份后端不归我管：%s" % why_want)
    why_paused = paused(now)
    if why_paused:
        return done("skip", "你明确停过：%s —— 不自动拉起" % why_paused)

    ok, why = healthy()
    if ok:
        with _LOCK:
            _STATE["ok"] += 1
            # 它现在是好的 → 之前那次"明确停"的退避没意义了（用户可能又点起来了）
            _STATE["paused_until"] = 0.0
            _STATE["paused_note"] = ""
        return done("ok", why)

    why_busy = busy()
    if why_busy:
        return done("busy", "%s —— 这一轮不动手（%s）" % (why, why_busy))

    recent = _restarts_recent(now)
    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        return done("capped", "%s；一小时内已经自动拉起 %d 次，不再自动拉了 —— 需要你看一眼"
                              % (why, len(recent)))
    if recent:
        wait = BACKOFF_BASE * (2 ** (len(recent) - 1))
        if now - max(recent) < wait:
            return done("backoff", "%s；距上次拉起不到 %d 秒，等一等" % (why, wait))

    try:
        from app import backend_admin
        started, msg = backend_admin.start()
    except Exception as e:                                   # noqa: BLE001
        return done("backoff", "拉起失败：%s" % str(e)[:120])
    if not started:
        return done("backoff", "没能开始拉起：%s" % str(msg)[:120])
    with _LOCK:
        _STATE["restarts"].append(now)
    return done("restart", "%s —— 已经在后台拉起：%s" % (why, str(msg)[:120]))


def state(now: float = None) -> dict:
    """给面板/日志看的一份快照（**只读**）。`now` 可注入 —— 用例用假时钟跑 `tick()`，
    读快照时也必须用同一个时钟，否则"最近一小时"的统计会把假时间戳全滤掉。"""
    now = float(now if now is not None else time.time())
    with _LOCK:
        return {"checks": _STATE["checks"], "ok": _STATE["ok"],
                "restarts": len(_restarts_recent(now)),
                "last": dict(_STATE["last"]), "note": _STATE["note"],
                "paused": bool(_STATE.get("paused_until") and now < _STATE["paused_until"]),
                "pausedNote": _STATE.get("paused_note") or "",
                "interval": WATCH_INTERVAL, "maxPerHour": MAX_RESTARTS_PER_HOUR}


def _loop():
    while True:
        try:
            tick()
        except Exception as e:                               # noqa: BLE001 —— 看门狗自己不许死
            log.warning("backend_watch tick 异常：%s", e)
        time.sleep(WATCH_INTERVAL)


def start() -> bool:
    """起看门狗线程（幂等）。ECHO 启动时调一次。"""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return False
    _THREAD = threading.Thread(target=_loop, daemon=True, name="backend-watch")
    _THREAD.start()
    log.info("backend_watch 已启动（每 %d 秒看一次，只碰 ECHO 自己起的后端）", WATCH_INTERVAL)
    return True
