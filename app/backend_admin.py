# -*- coding: utf-8 -*-
"""「起本机后端」的面板后台（客户端简化第 3 步 · 批 1d）。

这一层的位置感与 `app/capability_admin.py` 一样：**面板背后的活儿**。
它自己不实现任何"起/停/写配置"的细节 —— 那些在
`app/backend_pid.py`（归属）/ `app/backend_proc.py`（进程）/ `app/backend_setup.py`（编排）里，
这里只做三件事：

    ``view()``   把"现在什么状态 + 能点什么 + 为什么不能点"整理成面板直接渲染的形状
    ``start()``  在**后台线程**里跑编排，并把每一步实时记进 job（面板轮询它显示进度）
    ``stop()``   停 **ECHO 自己起的** 那个（手工起的实例一个字节都不动）

## 为什么起后端要放后台线程

编排里有一段时间在**等后端把本机配对文件写出来**（`PAIR_FILE_TIMEOUT`，最长 60 秒），
还有模型/进程启动的开销。放在 HTTP 请求线程里，面板会看到一个挂住六十秒的请求 ——
而"点了没反应"正是这套界面最该避免的那种状态（进度要看得见，失败也要当场看见）。
所以：`start()` 立刻返回，`view()["job"]` 里逐条长出步骤，面板每 1.5 秒轮询一次。

## 三条纪律

1. **只停自己起的**（`backend_proc.stop` 的判据是 pid 文件）—— 用户手工调试的实例绝不动；
2. **失败不改任何"以后走哪条路"的设置**（编排自己保证，这里只如实转发它的话）；
3. **进度的每一个字都来自编排**（这里不自己编"正在启动中…"那种没有信息量的话）。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Tuple

from app import backend_env, backend_pid, backend_proc, backend_setup
from app.capabilities import pairing

#: 一次只允许一个「起本机后端」。面板点两次不该起两个进程 ——
#: 端口只有一个，第二个必然失败，而用户会看到一个莫名其妙的错误。
_JOB_LOCK = threading.Lock()
_JOB: Dict[str, Any] = {
    "running": False,
    "ok": None,                 # None = 还没跑过；True/False = 上一次的结果
    "message": "",              # 最后一步那句话（成功或失败都说得出下一步）
    "steps": [],                # 逐条 {name, ok, detail}
    "stage": "",                # 正在做哪一步（面板显示"正在…"）
    "startedAt": "",
    "doneAt": "",
}


def _now() -> str:
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------- 设置

def _setting(key, default=None):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def ports() -> Tuple[int, int]:
    """要用的两个端口：**读生成出来的 `server.yaml`**（出厂值兜底）。

    刻意**不做成客户端设置**：端口是后端的设置，客户端再存一份就是"两处都能改、
    改完不知道谁生效"（`BackendSettingsStayOnTheBackendTests` 盯着这条）。用户手工把
    `server.yaml` 的 `listen` 改成别的端口也算数 —— 因为这里读的就是那份文件。
    """
    try:
        return backend_setup.configured_ports()
    except Exception:                                             # pragma: no cover - 兜底
        return (backend_proc.DEFAULT_PORT, backend_proc.DEFAULT_ADMIN_PORT)


def stop_with_client() -> bool:
    """ECHO 退出时要不要顺手停掉自己起的后端（默认**关**，见设置项说明）。"""
    return bool(_setting("capabilityBackendStopWithClient", False))


#: 最近一次"三层就绪"自测的结论（批 3）。**只存结论、不存音频**；面板直接渲染它。
#: 为什么留在这里而不是每次现跑：L3 是**一次真推理**（模型可能刚加载），
#: 面板每 1.5 秒轮询一次现跑等于把后端打满。所以：一键起后端时跑一次、
#: 用户点「就绪自测」时再跑一次，其余时候显示上一次的结论 + 时间。
_READY: Dict[str, Any] = {}


def last_ready() -> Dict[str, Any]:
    return dict(_READY)


def ready_probe(*, timeout_l3: float = 0.0, diarize: bool = False) -> Tuple[bool, str]:
    """当场跑一次三层就绪自测 → ``(ok, 一句话)``，结论存进 `last_ready()`。

    目标地址永远是**本机那个后端**（由生成出来的 `server.yaml` 决定），
    而不是"当前配对到的那台"—— 这个按钮问的是"我刚起的这个能不能干活"。
    """
    from app import backend_ready
    port, _admin = ports()
    url = backend_setup.loopback_base_url(port)
    try:
        res = backend_ready.probe(url, timeout_l3=float(timeout_l3 or 0.0) or
                                  backend_ready.L3_TIMEOUT_S, diarize=diarize)
    except Exception as e:                                        # pragma: no cover - 兜底
        res = {"ok": False, "state": "error", "baseUrl": url,
               "headline": "就绪自测没跑起来：%s" % e}
    global _READY
    _READY = dict(res)
    return bool(res.get("ok")), str(res.get("headline") or "")


# ---------------------------------------------------------------- 进度（job）

def job() -> Dict[str, Any]:
    """当前/上次那一次「起本机后端」的进度（面板轮询用）。**返回副本，不给内部引用。**"""
    with _JOB_LOCK:
        out = dict(_JOB)
        out["steps"] = [dict(s) for s in _JOB["steps"]]
        return out


def reset_job() -> None:
    """清掉上一次的进度（用例与"重新点一次"用）。"""
    with _JOB_LOCK:
        _JOB.update({"running": False, "ok": None, "message": "", "steps": [],
                     "stage": "", "startedAt": "", "doneAt": ""})


def _note_step(step: Dict[str, Any]) -> None:
    with _JOB_LOCK:
        _JOB["steps"].append(dict(step))
        _JOB["stage"] = str(step.get("name") or "")


# ---------------------------------------------------------------- 状态

def _package_hint() -> Tuple[bool, str]:
    """薄包那一步现在能不能一键走完 → ``(ok, 一句话)``。

    `backend_fetch.plan()` 的 `willFetch` 就是这件事的判据（已经解开 / 本机找得到 /
    设置或环境变量里给了路径或 URL），**不在这一层重算**——两处判据迟早会漂。
    """
    from app import backend_fetch
    try:
        p = backend_fetch.plan()
    except Exception as e:                                        # pragma: no cover - 兜底
        return False, "薄包的计划算不出来：%s" % e
    if p.get("willFetch"):
        return True, str(p.get("package", {}).get("headline") or "薄包能取到")
    return False, ("后端的**薄包**还没有：%s。把 `%s-*.zip` 放到其中之一，或在设置 "
                   "`%s`（或环境变量 `%s`）里填它的路径 / 下载地址。"
                   % ("、".join(p.get("packageSearch") or []) or "（没有可看的目录）",
                      backend_fetch.PACKAGE_PREFIX, backend_fetch.PACKAGE_SETTING,
                      backend_fetch.PACKAGE_ENV))


def _can_start(runtime: str, alive: bool, ports_ok: bool, ports_detail: str) -> Tuple[bool, str]:
    """现在能不能点「起本机后端」？不能的话，**为什么**（这句话直接进按钮的 title）。"""
    if not runtime:
        # 运行时还没有：只要**薄包能取到**，这一趟就还能一键走完（先取薄包 → 再从国内源装运行时）。
        ok, hint = _package_hint()
        if not ok:
            return False, hint
        return True, ("会先取薄包（%s），再按国内源把运行时装上（约 3 GB），"
                      "然后写配置 → 起进程 → 读本机配对文件自动配对" % hint)
    if alive:
        return False, "ECHO 自己起的那个**已经在跑**了 —— 要停它请点「停掉它」"
    if not ports_ok:
        return False, ports_detail
    return True, "会写好配置、把后端起起来，并读它写下的本机配对文件自动配对"


def notes(*, runtime: str, config_exists: bool, alive: bool, local: Dict[str, Any],
          paired: Dict[str, Any]) -> list:
    """要如实说给用户听的那几句（**没有就空着**，不凑数）。"""
    out = []
    if not runtime:
        ok, hint = _package_hint()
        if ok:
            out.append("还没有后端的运行时 —— 点一次「起本机后端」会先取薄包，"
                       "再用国内源装运行时（torch 走 SJTU 的 CUDA 索引）。%s" % hint)
        else:
            out.append("还没有后端的运行时，薄包也还没到：%s" % hint)
    if not config_exists:
        out.append("还没生成过配置 —— 点一次「起本机后端」会写一份只绑回环的 "
                   "`server.yaml`（jwt_secret 只在第一次生成，之后一直沿用）。")
    if alive and not local.get("found"):
        out.append("后端在跑，但本机配对文件还没出现（可能还在启动）—— 稍等再看，"
                   "或者看日志：%s" % backend_pid.log_paths()[0])
    if paired.get("paired") and not str(paired.get("baseUrl") or "").startswith(
            ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")):
        out.append("这台机器现在配对的是 %s（**不是本机后端**）—— 起本机后端不会覆盖它，"
                   "要改用本机那个请先「解除配对」。" % paired.get("baseUrl"))
    if str(_setting("capabilityPrivacy", "lan") or "lan") == "none" and \
            paired.get("paired") and not _is_loopback(paired.get("baseUrl")):
        out.append("「允许音频去哪」= 不出机，而现在配对的后端在别的机器上 —— "
                   "会议会被许可挡住（面板会说「被『允许音频去哪』挡住了」）。"
                   "要全本机跑就用本机后端，或把许可改成「内网」。")
    return out


def _is_loopback(url) -> bool:
    try:
        from app import netlocal
        return bool(netlocal.is_loopback(url))
    except Exception:
        return False


def usability() -> Dict[str, Any]:
    """**配对的那台后端能不能用我**（`ready` / `code` / 一句话原因）。

    为什么要有这个（2026-10-06）：面板原来只回答"那个进程是不是我起的"。于是
    "8900 上跑着另一棵树的后端"这种情况被判成「待启动」，而真相是
    **配对还在、地址也通、只是那台后端不认这个客户端**（`code=unauthorized`）。
    用户看到"待启动"，就去查"为什么没起来"，而该做的是**把后端换成自己的**。

    判据复用能力路由那一份（`app.capabilities.router`）——**不另起一套探测**：
    它已经知道"配对到哪台、那台认不认我、为什么不认"，而且它正是转写真正走的那条路。
    读不到就返回空 code（面板不会因此报错，只是说不出原因）。
    """
    try:
        from app.capabilities.router import build_default_router, Need
        r = build_default_router()
        plan = r.plan(Need(slots=("asr.text",), purpose="meeting"))
        pick = plan.picks.get("asr.text")
        # 先看它有没有被 skip 掉：`skipped` 里那条的 `reason` 就是机器可读的 code
        # （`unauthorized` / `blocked` / `forbidden` / `offline`），`detail` 是人话。
        for s in plan.skipped:
            if s.slot == "asr.text" and getattr(s, "backend_id", "") == "echo-server":
                return {"backendId": "echo-server", "ready": False,
                        "code": str(getattr(s, "reason", "") or ""),
                        "note": str(getattr(s, "detail", "") or "")}
        if pick is not None:
            return {"backendId": pick.backend_id, "ready": True, "code": "",
                    "note": pick.reason}
        return {"backendId": "", "ready": False, "code": "",
                "note": "没有任何后端能提供 asr.text"}
    except Exception as e:                                        # pragma: no cover - 兜底
        return {"backendId": "", "ready": False, "code": "",
                "note": "读不出可用性：%s: %s" % (type(e).__name__, e)}


#: `usability()["code"]` → 给用户看的处置建议。
#: 这张表是"说错原因就会把人引错方向"的防呆：`unauthorized` **不是**"连不上"，
#: 而是"那台后端不认这个客户端"（换树没换后端时的典型症状）。
_USABILITY_ADVICE = {
    "unauthorized": "那台后端不认本机的客户端凭据（换树时最常见：8900 上跑的是另一棵树的后端）。"
                    "点「接管后端」换成本棵树自己的即可 —— 凭据不用重新配对。",
    "blocked": "被「允许音频去哪」的许可挡住了（不是连不上）：去设置里把许可放宽，或改用本机后端。",
    "forbidden": "那台后端按 scopes 拒了这一档：在它的管理面把这个客户端的 scopes 补全。",
    "offline": "连不上那台后端（它没在跑，或地址不对）。",
}


def usability_note(info: Dict[str, Any]) -> str:
    """把 `usability()` 翻成一句可直接显示的话。"""
    if info.get("ready"):
        return ""
    code = str(info.get("code") or "")
    advice = _USABILITY_ADVICE.get(code)
    if advice:
        return advice
    note = str(info.get("note") or "").strip()
    if note:
        return note
    return ""


def view() -> Dict[str, Any]:
    """面板要的全部状态。**一次算完**（面板不用自己拼，也不许自己猜）。"""
    port, admin_port = ports()
    runtime = backend_proc.python_exe()
    config_path = backend_setup.config_path()
    #: 端口占用**只查一次**（每次 netstat 约 0.15 s/端口，面板会反复问这个接口），
    #: 然后把结果同时交给"能不能起"的判据与面板显示 —— 一处查、一处判。
    #:
    #: `with_root=True`：**顺带问出"这个后端属于哪棵树"**（2026-10-06）。多花 1~2 ms，
    #: 但它把"待启动"这句误导性的话换成了可操作的答案 ——
    #: "8900 上是另一棵树的后端，点接管"。
    owners = []
    for p in (port, admin_port):
        try:
            owners.append(backend_proc.port_owner(p, with_root=True))
        except Exception:                                         # pragma: no cover - 兜底
            owners.append({"port": int(p), "pid": 0, "label": ""})
    #: 占着端口的**另一棵树的后端**（本棵树没有它的 pid 记录，但命令行证明它是 ECHO 后端）。
    #: 面板据此给「接管」入口；`port_check` 也会把它与"不明进程"分开报。
    foreign = next((o for o in owners if o.get("foreign")), None)
    # ⚠️ **顺序要紧**（2026-10-06）：`port_check` 会**认领**"本棵树的后端在跑、只是 pid 记录
    # 不在"那种情形（ECHO 重启过就是这样，见 `port_check` 的说明），而认领 = 把 pid 写进
    # 归属记录。所以归属必须**在它之后**读 —— 反过来的话第一次刷新仍会说「待启动」，
    # 用户得刷两次才自愈，而那句"待启动"正是他报上来的问题。
    try:
        ports_ok, ports_detail = backend_proc.port_check((port, admin_port), owners=owners)
    except Exception as e:                                        # pragma: no cover - 兜底
        ports_ok, ports_detail = True, "端口状态读不出来：%s" % e
    alive, note = backend_pid.is_ours_alive()
    pid = int(backend_pid.read_pid() or 0) if alive else 0
    try:
        local = pairing.local_pair_state()
    except Exception:                                             # pragma: no cover - 兜底
        local = {"found": False, "path": "", "baseUrl": "", "expired": False,
                 "expiresAt": 0.0, "message": ""}
    try:
        paired = pairing.state()
    except Exception:                                             # pragma: no cover - 兜底
        paired = {"paired": False, "baseUrl": "", "clientId": "", "serverName": "",
                  "pairedAt": 0.0, "tokenFresh": False}
    config_exists = os.path.isfile(config_path)
    can, why = _can_start(runtime, alive, ports_ok, ports_detail)
    use = usability()
    use_note = usability_note(use)
    all_notes = notes(runtime=runtime, config_exists=config_exists, alive=alive,
                      local=local, paired=paired)
    if use_note:
        all_notes.append(use_note)
    if foreign:
        all_notes.append(
            "端口上跑的是**另一棵树**的后端（%s）—— 点「接管后端」会停掉它并起本棵树"
            "自己的；凭据是同一份，**不需要重新配对**。" % foreign.get("root"))
    return {
        "root": backend_setup.backend_root(),
        "port": int(port), "adminPort": int(admin_port),
        "baseUrl": backend_setup.loopback_base_url(port),
        #: 面板「后端」小卡上的 ↗ 入口（2026-10-05 用户要求）。管理面**只绑回环**是设计
        #: （见 server/admin.py 开头的三条），所以如实写 127.0.0.1；端口取"一处权威"。
        #: ⚠️ 端口被**另一棵树**占着时，这个地址通向的是**别人的**管理面（凭据同源所以能登录）
        #: —— 面板要据此提示，别让"能登录"变成"后端没问题"的错觉。
        "adminUrl": "http://127.0.0.1:%d/admin/" % int(admin_port),
        "runtime": {"ready": bool(runtime), "path": runtime},
        "config": {"path": config_path, "exists": config_exists},
        "running": bool(alive),
        "pid": pid,
        "note": note,
        "ports": owners,
        "portsOk": bool(ports_ok),
        "portsDetail": ports_detail,
        #: 占着端口的是**另一棵树的后端**（面板据此给「接管」按钮）。
        "foreign": foreign or {},
        "foreignBackend": bool(foreign),
        #: **配对的那台能不能用我** —— 这才是"转写到底行不行"的判据；
        #: `running` 只说明"有个进程在跑"（可能是别人的）。
        "usable": use,
        "usableNote": use_note,
        "canTakeOver": bool(foreign),
        "pairFile": local,
        "paired": paired,
        #: **后端在哪**：`local`（配对到本机回环，或还没配对）还是 `network`（配对到别的机器）。
        #:
        #: 为什么要一个显式字段（而不是让调用方自己看 `paired.baseUrl`）：判据是"地址是不是
        #: 回环"，而回环有一堆写法（`127.0.0.1` / `localhost` / `[::1]` / 整段 `127.0.0.0/8`）。
        #: 让每个调用方各写一遍 `startswith("http://127.0.0.1")` 迟早漂开 —— 这个文件里就
        #: 已经有一处这么写的（见上面 `_is_loopback` 的邻居），而正确判据在 `netlocal.is_loopback`。
        #:
        #: 它决定**启动脚本该怎么处理后端**（用户 2026-10-07 的口径）：
        #:   * `local`   → 拉起本机后端（take-over）
        #:   * `network` → **只探测**那台服务，**别去动本机进程**（本机后端这时候不是它要用的）
        "mode": "network" if (paired.get("paired") and not _is_loopback(paired.get("baseUrl")))
                else "local",
        "stopWithClient": stop_with_client(),
        "canStart": bool(can),
        "whyNot": why,
        "notes": all_notes,
        "ready": last_ready(),
        "job": job(),
    }


# ---------------------------------------------------------------- 动作

def start(*, replace_pairing: bool = False, vram_budget_mb: int = 0,
          device: str = "cuda", timeout: float = backend_setup.PAIR_FILE_TIMEOUT,
          python: str = "", cwd: str = "") -> Tuple[bool, str]:
    """开始「起本机后端」（后台线程）。→ ``(ok, 一句话)``。

    这里只做**入口处的快速拒绝**（缺运行时 / 已有一次在跑）—— 真正的判据在编排里，
    而且那里会逐条说出来。返回 True 只代表"任务已经在跑"，**不代表后端起来了**；
    面板要看进度就轮询 `view()["job"]`。
    """
    if not backend_proc.python_exe() and not python:
        # 薄包那条路（用户 2026-09-30 拍板"默认薄包 + 国内可下载"）：**不在这里拒绝** ——
        # 薄包与运行时正是这趟活要取的（第 −1 步 `package`、第 0 步 `runtime`，
        # 见 `app/backend_fetch.py`）。只有"薄包既不在本机、也没给地址"时才说清楚，
        # 别让人白等一次注定失败的下载。
        ok, hint = _package_hint()
        if not ok:
            return False, hint
    with _JOB_LOCK:
        if _JOB["running"]:
            return False, ("已经有一个「起本机后端」在进行中（第 %d 步：%s）—— "
                           "等它做完再点" % (len(_JOB["steps"]), _JOB["stage"] or "准备中"))
        _JOB.update({"running": True, "ok": None, "message": "", "steps": [],
                     "stage": "准备中", "startedAt": _now(), "doneAt": ""})
    port, admin_port = ports()
    # 装哪一档运行时由**显卡**定（`compute_cap` → cu126/cu118…），与"计划"里那个 variant 同一判据。
    try:
        variant = backend_env.variant_for(backend_env.gpu())
    except Exception:                                             # pragma: no cover - 兜底
        variant = ""

    def _worker():
        try:
            res = backend_setup.start(port=port, admin_port=admin_port,
                                      device=device, vram_budget_mb=vram_budget_mb,
                                      timeout=timeout, replace=replace_pairing,
                                      python=python, cwd=cwd, variant=variant,
                                      on_step=_note_step)
        except Exception as e:                                    # pragma: no cover - 兜底
            res = {"ok": False, "message": "起后端时出了意外：%s" % e, "steps": []}
        with _JOB_LOCK:
            _JOB["running"] = False
            _JOB["ok"] = bool(res.get("ok"))
            _JOB["message"] = str(res.get("message") or "")
            if res.get("steps"):
                _JOB["steps"] = [dict(s) for s in res["steps"]]
            _JOB["doneAt"] = _now()
            _JOB["stage"] = ""

    threading.Thread(target=_worker, daemon=True, name="backend-start").start()
    return True, ("已开始「起本机后端」：先取薄包（若还没解开）、再按国内源装运行时、"
                  "写配置、起进程，然后等本机配对文件并配对")


def stop(reason: str = "") -> Tuple[bool, str]:
    """停掉 **ECHO 自己起的** 后端（手工起的实例不动）→ ``(ok, 一句话)``。"""
    with _JOB_LOCK:
        if _JOB["running"]:
            return False, ("「起本机后端」正在进行中（第 %d 步：%s）—— 等它做完再停，"
                           "否则两边会抢同一对端口" % (len(_JOB["steps"]),
                                                       _JOB["stage"] or "准备中"))
    port, admin_port = ports()
    return backend_proc.stop(reason=reason or "面板上点了「停掉它」",
                             ports=(port, admin_port))


def stop_if_configured() -> Tuple[bool, str]:
    """ECHO 退出时调用：只有开关打开时才停（默认关，见设置项说明）。"""
    if not stop_with_client():
        return True, "「随 ECHO 退出时停掉本机后端」是关的，不动它"
    return stop(reason="ECHO 退出（设置里打开了「随 ECHO 退出时停掉本机后端」）")


def take_over(*, vram_budget_mb: int = 0, device: str = "cuda",
              timeout: float = backend_setup.PAIR_FILE_TIMEOUT) -> Tuple[bool, str]:
    """**接管后端**：停掉占着端口的另一棵树的后端，换成当前这棵树自己的。

    为什么需要它（2026-10-06，用户报的真实 bug）：开发版与稳定版是**轮流跑**的，
    但端口（8900/8901）两棵树共用。原来切实例只换 ECHO、**不换后端**，于是新树起来后
    8900 上跑的还是旧树的后端 —— 现象是面板「待启动」、箭头能进（那是别人的管理面）、
    而转写拿 `unauthorized`（**不是连不上，是那台不认这个客户端**）。
    用户的原话："切换脚本和启停脚本都要同步考虑后端的切换"。

    归属判据（**只停"确实是 ECHO 后端但不是本棵树的"那一个**）：
      * 命令行里必须有 ``-m server.main --config <目录>``（证明它是 ECHO 的后端）；
      * 那个目录**不等于**本棵树的后端目录（否则它是我们自己的，走 ``stop()``）。
    两条都不满足时**绝不动手** —— 用户手工起的、别的软件的，一律留给用户
    （见 ``app/backend_pid.py`` 开头那条铁律："归属只能来自可靠判据，不许猜一个来停"）。

    配对**不需要重做**：客户端凭据（``backend.json``）里的 ``base_url`` 仍是本机回环地址，
    起完自己的后端后 ``pair_if_needed()`` 会走"已经配对到本机后端 → 跳过"那条路
    （实测：clients 表行数 13→13，不新建客户端）。若那台新后端确实不认这份凭据，
    才由 ``start()`` 的编排去配一次。
    """
    with _JOB_LOCK:
        if _JOB["running"]:
            return False, ("「起本机后端」正在进行中（第 %d 步：%s）—— 等它做完再接管"
                           % (len(_JOB["steps"]), _JOB["stage"] or "准备中"))
    port, admin_port = ports()
    mine = backend_setup.backend_root()
    victims = []
    for p in (port, admin_port):
        try:
            o = backend_proc.port_owner(p, with_root=True)
        except Exception:                                         # pragma: no cover - 兜底
            continue
        if int(o.get("pid") or 0) > 0 and o.get("foreign") and o.get("root"):
            victims.append(o)
    if victims:
        seen, uniq = set(), []
        for o in victims:
            if o["pid"] in seen:
                continue
            seen.add(o["pid"])
            uniq.append(o)
        for o in uniq:
            killed = False
            try:
                from app import platform as _platform
                killed = bool(_platform.kill_process_tree(int(o["pid"])))
            except Exception:                                     # pragma: no cover - 兜底
                killed = False
            if not killed:
                return False, ("接管失败：停不掉 pid %d（%s，属于 %s）—— 请手工停它，"
                               "或用桌面工具包的「15-后端-清掉孤儿」"
                               % (o["pid"], o.get("label") or "进程", o.get("root")))
            try:
                from app import db
                db.add_log("info", "backend",
                           "接管后端：已停掉另一棵树的后端 pid=%d（%s），本棵树的后端目录是 %s"
                           % (o["pid"], o.get("root"), mine))
            except Exception:
                pass
        # 端口要空下来才起——被杀进程释放端口有几毫秒的窗口
        for _ in range(20):
            left = [backend_proc.port_owner(p) for p in (port, admin_port)]
            if not any(int(x.get("pid") or 0) > 0 for x in left):
                break
            time.sleep(0.25)
    ok, msg = start(vram_budget_mb=vram_budget_mb, device=device, timeout=timeout)
    if not ok:
        return False, "已停掉旧后端，但起本棵树的后端失败：%s" % msg
    if victims:
        # 报 `uniq` 而不是 `victims`：同一个后端进程同时占着数据口与管理口，
        # 用未去重的列表会拼出"pid 31712、pid 31712"（2026-10-06 真机实测看到过）。
        return True, ("已接管：停掉另一棵树的后端（%s），正在起本棵树自己的（%s）。"
                      "凭据是同一份，配对会自动复用，不需要重新配对。"
                      % ("、".join("pid %d" % v["pid"] for v in uniq), mine or "（未配置）"))
    return True, "端口上本来就没有别的树的后端，已直接起本棵树自己的（%s）" % (mine or "（未配置）")
