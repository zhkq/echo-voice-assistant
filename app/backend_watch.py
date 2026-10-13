# -*- coding: utf-8 -*-
"""本地能力后端的看门狗（2026-10-11 用户要求："如果配置是本地后端，watchdog 也应该监控后端"）。

**为什么需要它**：`scripts/echo-supervisor.ps1` 只盯**实例**（ECHO 自己在不在跑、别的实例是否
也在跑），**完全不看能力后端**。而"本机后端"是 ECHO 自己起的（pid 记在
`<数据根>/logs/backend.pid`），它可能**掉一半**：进程还活着、只占着管理口，数据口 8900 不再监听
—— 2026-10-11 08:5x 用户实测正是这个形态（pid 10164 只占 8901、8900 空着），面板一路如实报
"不可用"，但**没有任何人负责把它拉回来**。（注意这与"面板说谎"是两件事：用户 08:5x 确认过，
面板显示"不可用"是**对的**；看着像在跑的是桌面工具包那张表，那一处已经修了。）

**三条铁律**（前两条是这一晚反复踩出来的纪律）：
1. **只碰该碰的那份，而"该不该碰"由配置回答**（2026-10-13 用户："守护内容需要根据
   配置项确认"）：三个槽都不要能力后端、或这棵树配的是**远端**后端 → **不守**
   （pid 记录只是"曾起过"的痕迹，不是"还需要"的证据）；配对指向回环、或没有配对但
   本机后端配置齐且有 pid 记录 → 守。判不出归属 → **不动手**，只留一句"不归我管"
   （判不出就不动的原则同 `backend_proc` 的归属判据）。
2. **不打断用户**：会议在录音、或已有一次"起本机后端"的任务在跑 → 这一轮什么都不做，
   下一轮再说（同 AGENTS.md"动手前先看 `meeting.active`"）。
3. **有退避与上限**：失败按 60s → 120s → 240s… 退避，**一小时内最多 3 次**；超了就不再自动拉，
   改成"需要你看一眼"。理由：这是个 3 GB 量级的运行时，把它拉成死循环比不拉更糟。

端口**从后端自己的 `server.yaml` 读**（`backend_setup.configured_ports()`）—— 与
`tests/test_capability_admin.py` 盯的那条一致：端口是后端自己的设置，客户端不许再存一份。
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
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
    #: 最近一次"该不该守"的结论（tick 里算一次、面板直接读它）。
    #: 为什么由 tick 存而不是让 state() 现算：`wanted()` 要调 `backend_admin.view()`，
    #: 而 `view()` 反过来会读 `state()` —— 现算就是无限递归（2026-10-13）。
    "wanted": {},
}
_LOCK = threading.Lock()
_THREAD = None


def _slot_wants_capability() -> tuple:
    """三个槽里**有没有人**要能力后端：``(要?, 依据)``。

    配置项是权威（2026-10-13 用户："守护内容需要根据配置项确认"）：
      * `capabilityMeetingAsrBackend == "echo-server"` -> 会议转写的 asr.text 要能力后端；
      * `capabilityDiarizeBackend != "off"` -> 说话人分离要（**分离只落在能力后端上**，
        没有"本机引擎"那一档，见 config.py 里那一项的说明）；
      * `capabilityEmbedBackend != "off"` -> 声纹嵌入同理。

    三个都不要 = 这台机器根本不用能力后端，守它的本机后端毫无意义。
    读不到配置时**不拿它当理由**（返回要=True）：判不出来时"守"比"不守"安全
    —— 不守会让一个正在用的后端掉了没人管。
    """
    want = []
    try:
        from app.config import settings
        asr = str(settings.get("capabilityMeetingAsrBackend", "") or "").strip().lower()
        dia = str(settings.get("capabilityDiarizeBackend", "") or "").strip().lower()
        emb = str(settings.get("capabilityEmbedBackend", "") or "").strip().lower()
        if asr == "echo-server":
            want.append("会议转写走 ECHO 后端")
        if dia and dia != "off":
            want.append("说话人分离走能力后端")
        if emb and emb != "off":
            want.append("声纹嵌入走能力后端")
    except Exception as e:                                   # noqa: BLE001
        return True, "读不到槽配置（%s）—— 不拿它当理由" % str(e)[:60]
    if not want:
        return False, "配置里三个槽都不要能力后端"
    return True, "；".join(want)


def _backend_target() -> tuple:
    """这棵树**现在连的是谁**：``(url, 来源说明)``；url 空 = 没有线索。

    配对文件优先（AGENTS.md：后端地址的真正来源是配对文件 `data/backend.json`，
    隐藏设置 `capabilityEchoServerUrl` 只作兜底）。顺带回报"本机后端配置齐不齐"
    —— 那是"有 pid 记录但没有任何配对线索"时唯一能用的配置判据。
    """
    configured = ""
    try:
        from app import backend_admin
        v = backend_admin.view() or {}
        pr = v.get("paired") or {}
        pf = v.get("pairFile") or {}
        url = str(pr.get("baseUrl") or pf.get("baseUrl") or "")
        if url:
            return url, "配对"
        ok = (bool((v.get("runtime") or {}).get("ready"))
              and bool((v.get("config") or {}).get("exists")))
        configured = "本机后端已配置" if ok else "本机后端没配置齐"
    except Exception:
        pass
    # 配对文件自己读一遍：`backend_admin.view()` 在实例没起来时给不出配对 URL，而这时
    # 隐藏设置 `capabilityEchoServerUrl` 可能还是**旧的**（2026-10-13 实测：dev 树的配对
    # 文件写的是远端、设置里却还留着 127.0.0.1，于是判成"该守"）。AGENTS.md 的口径是
    # **后端地址的真正来源是配对文件**，所以它排在设置前面。
    try:
        import json as _json
        from app import paths as _paths
        p = os.path.join(str(_paths.data_root()), "data", "backend.json")
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as fh:
                url = str((_json.load(fh) or {}).get("base_url") or "")
            if url:
                return url, "配对"
    except Exception:
        pass
    try:
        from app.config import settings
        url = str(settings.get("capabilityEchoServerUrl", "") or "").strip()
        if url:
            return url, "设置"
    except Exception:
        pass
    return "", configured


def _is_loopback(url: str) -> bool:
    return bool(re.match(r"^https?://(127(\.\d+){3}|localhost|\[::1\])(:|/|$)",
                         str(url or ""), re.I))


def wanted() -> tuple:
    """该不该由 ECHO 看着它：``(要?, 依据)``。

    **判据按配置项来**（2026-10-13 用户："守护内容需要根据配置项确认"）。三条，按序：

      ① 三个槽（`capabilityMeetingAsrBackend` / `capabilityDiarizeBackend` /
         `capabilityEmbedBackend`）**都不要能力后端** -> 本机后端对这台机器没用：不守；
      ② 这棵树**连的是谁**（配对文件的 `baseUrl`，兜底 `capabilityEchoServerUrl`）：
         * **回环** -> 用的就是本机后端，该守（pid 记不记得都算）；
         * **远端** -> **不该守**：本机后端与它无关。**这里即使有 pid 记录也不拉**
           —— pid 记录只证明"ECHO 曾经起过它"，不证明"现在还需要它"
           （2026-10-13 现场：改用网络后端的机器上，那个本机后端被 pid 记录永远看护着，
           没人知道它为什么还占着 8900/8901，杀了 30 秒后又被拉回来）；
      ③ 既没有配对、也没有设置：**有 pid 记录 且 本机后端配置齐**（runtime 可用 +
         server.yaml 在）才算"我们起的"；判不出就不动手。

    与旧版的差别只有一处、但很关键：**pid 记录不再是充分条件**。"需不需要"这件事
    只能由配置回答（旧版把它当成充分条件，正是 2026-10-13 那次"多余的本机后端"的成因）。
    """
    want_slots, why_slots = _slot_wants_capability()
    if not want_slots:
        return False, "%s —— 本机后端不需要（配置说了算）" % why_slots

    pid = 0
    try:
        from app import backend_pid
        pid = int(backend_pid.read_pid() or 0)
    except Exception:
        pid = 0

    url, source = _backend_target()
    if url:
        if _is_loopback(url):
            return True, "%s指向本机后端（%s）" % (source, url)
        why = "%s指向远端后端（%s）—— 本机后端不需要" % (source, url)
        if pid > 0:
            why += "；但还留着 pid 记录（pid %d）：它只是「曾起过」的痕迹，建议停掉（9-backend-stop）" % pid
        return False, why

    if pid > 0 and source == "本机后端已配置":
        return True, "有 pid 记录（pid %d），且本机后端配置齐" % pid
    if pid > 0:
        return False, "有 pid 记录（pid %d）但%s —— 不碰" % (pid, source or "没有任何配对线索")
    return False, "既没有配对、也没有 pid 记录 —— 不碰"


def port_state() -> list:
    """每个口现在是**谁**在听：``[{label, port, pid, ours, foreign}, ...]``（只读）。

    归属判据复用 `backend_proc.port_owner(with_root=True)` —— 它从命令行里的
    `--config <树>\server.yaml` 认出"这是哪棵树的后端"（与桌面工具包、`backend_proc` 同一套）。
    """
    rows = []
    try:
        from app import backend_setup, backend_proc
        data_port, admin_port = backend_setup.configured_ports()
    except Exception as e:                                   # noqa: BLE001
        return [{"label": "配置", "port": 0, "pid": 0, "ours": False, "foreign": False,
                 "note": "读不到端口配置：%s" % str(e)[:80]}]
    for label, port in (("数据口", data_port), ("管理口", admin_port)):
        info = {}
        try:
            info = backend_proc.port_owner(int(port), with_root=True) or {}
        except Exception:
            info = {}
        pid = int(info.get("pid") or 0)
        rows.append({"label": label, "port": int(port), "pid": pid,
                     "ours": bool(pid and info.get("is_ours")),
                     "foreign": bool(pid and info.get("foreign"))})
    return rows


def healthy() -> tuple:
    """``(健康?, 一句话, 原因类别)``；类别 ∈ ``"" | "missing" | "foreign" | "unknown"``。

    **两个口都在听还不够**：2026-10-11 实测，门禁泄漏的一个 **dev 树**后端占着 8900/8901，
    只探端口的判据把稳定版的后端判成"健康"（而真后端是停的）；反过来，端口被别人占着时
    去拉起只会失败。所以这里把"不是本棵树的在听"单独判成 ``foreign`` —— 上层据此**不瞎拉**、
    而是如实报"需要你看一眼"。
    """
    rows = port_state()
    if rows and rows[0].get("note"):
        return True, rows[0]["note"], "unknown"       # 读不到配置就不猜，先放过
    missing = [r for r in rows if not r["pid"]]
    if missing:
        return False, "；".join("%s %d 没在听" % (r["label"], r["port"]) for r in missing), "missing"
    unknown = [r for r in rows if not r["ours"] and not r["foreign"]]
    foreign = [r for r in rows if r["foreign"]]
    if unknown or (foreign and len(foreign) == len(rows)):
        # 判不出归属（比如命令行里没有 --config 的进程）→ 当作"不是我们的"，别去拉
        bad = foreign or unknown
        return (False,
                "；".join("%s %d 被**别的**进程占着（pid %d，不是本棵树的）"
                          % (r["label"], r["port"], r["pid"]) for r in bad),
                "foreign")
    if foreign:
        return (False,
                "；".join("%s %d 被**别的**进程占着（pid %d，不是本棵树的）"
                          % (r["label"], r["port"], r["pid"]) for r in foreign),
                "foreign")
    return True, "两个口都在听，且都是本棵树的后端", ""


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
    with _LOCK:
        # 存下来给 state()/面板读：state() 不能现算（会经 backend_admin.view() 递归回来）。
        _STATE["wanted"] = {"want": bool(want), "why": why_want, "at": now}
    if not want:
        return done("skip", "这份后端不归我管：%s" % why_want)
    why_paused = paused(now)
    if why_paused:
        return done("skip", "你明确停过：%s —— 不自动拉起" % why_paused)

    ok, why, why_kind = healthy()
    if (not ok) and why_kind == "foreign":
        # 端口被别人的/残留的进程占着 —— **别去拉**（只会失败），如实报出来让人看一眼。
        return done("foreign", "%s —— 不是本棵树的后端，拉起也会失败，需要你看一眼" % why)
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
                "ports": port_state(),
                "wanted": dict(_STATE.get("wanted") or {}),
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


def disabled() -> str:
    """该不该**不起**看门狗：返回原因，空串 = 可以起。

    两条（2026-10-11 实测出来的，都很硬）：
    1. **测试进程里一律不起**。某个用例把线程起在了测试进程里（`boot._run_boot` 会在启动时调
       本函数），于是看门狗**尽职地真去拉后端** → 泄漏了一个真实 dev 后端的进程（`--config`
       指向真实数据根），并且它与测试抢同一个 sqlite → `database is locked`，
       门禁因此红了（`test_providers::...` 那条）。
       用例必须自己打桩；看门狗**不在测试里干活**。
    2. `ECHO_NO_BACKEND_WATCH=1` 显式关掉 —— 这也顺便永久解决了
       「跑门禁前先停后端」与「看门狗看到停就拉」的冲突（门禁可以带这个变量跑）。
    """
    env = str(os.environ.get("ECHO_NO_BACKEND_WATCH", "") or "").strip().lower()
    if env and env not in ("0", "false", "no"):
        return "ECHO_NO_BACKEND_WATCH 显式关掉了"
    if "unittest" in sys.modules or "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return "跑在测试进程里（用例不该真去拉后端）"
    return ""


def start() -> bool:
    """起看门狗线程（幂等）。ECHO 启动时调一次；测试进程里**不起**（见 :func:`disabled`）。"""
    global _THREAD
    why = disabled()
    if why:
        log.info("backend_watch 未启动：%s", why)
        return False
    if _THREAD and _THREAD.is_alive():
        return False
    _THREAD = threading.Thread(target=_loop, daemon=True, name="backend-watch")
    _THREAD.start()
    log.info("backend_watch 已启动（每 %d 秒看一次，只碰 ECHO 自己起的后端）", WATCH_INTERVAL)
    return True
