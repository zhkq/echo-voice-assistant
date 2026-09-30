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

def _can_start(runtime: str, alive: bool, ports_ok: bool, ports_detail: str) -> Tuple[bool, str]:
    """现在能不能点「起本机后端」？不能的话，**为什么**（这句话直接进按钮的 title）。"""
    if not runtime:
        return False, ("后端的运行时还没装好（%s 下没有 runtime/）—— 扩展包要先解包/装好；"
                       "容器路还没做" % backend_proc.backend_root())
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
        out.append("还没有后端的运行时（%s 下没有 runtime/）：扩展包要先解包/装好。"
                   "容器路（Docker）还没做。" % backend_proc.backend_root())
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


def view() -> Dict[str, Any]:
    """面板要的全部状态。**一次算完**（面板不用自己拼，也不许自己猜）。"""
    port, admin_port = ports()
    runtime = backend_proc.python_exe()
    config_path = backend_setup.config_path()
    alive, note = backend_pid.is_ours_alive()
    pid = int(backend_pid.read_pid() or 0) if alive else 0
    #: 端口占用**只查一次**（每次 netstat 约 0.15 s/端口，面板会反复问这个接口），
    #: 然后把结果同时交给"能不能起"的判据与面板显示 —— 一处查、一处判。
    owners = []
    for p in (port, admin_port):
        try:
            owners.append(backend_proc.port_owner(p))
        except Exception:                                         # pragma: no cover - 兜底
            owners.append({"port": int(p), "pid": 0, "label": ""})
    try:
        ports_ok, ports_detail = backend_proc.port_check((port, admin_port), owners=owners)
    except Exception as e:                                        # pragma: no cover - 兜底
        ports_ok, ports_detail = True, "端口状态读不出来：%s" % e
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
    return {
        "root": backend_setup.backend_root(),
        "port": int(port), "adminPort": int(admin_port),
        "baseUrl": backend_setup.loopback_base_url(port),
        "runtime": {"ready": bool(runtime), "path": runtime},
        "config": {"path": config_path, "exists": config_exists},
        "running": bool(alive),
        "pid": pid,
        "note": note,
        "ports": owners,
        "portsOk": bool(ports_ok),
        "portsDetail": ports_detail,
        "pairFile": local,
        "paired": paired,
        "stopWithClient": stop_with_client(),
        "canStart": bool(can),
        "whyNot": why,
        "notes": notes(runtime=runtime, config_exists=config_exists, alive=alive,
                       local=local, paired=paired),
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
        # 运行时正是这趟活要装的（第 0 步 `runtime`，见 `app/backend_fetch.py`）。
        # 但**薄包没解开**（没有 server/requirements.txt）就先说清楚，别让人等一次必然失败的下载。
        from app import backend_fetch
        if not backend_fetch.plan()["sourceReady"]:
            return False, ("后端的**薄包还没解开**：%s 下找不到 server/requirements.txt —— "
                           "先把薄包解到那里（解好后这一趟会从国内源把运行时装上）。"
                           % backend_proc.backend_root())
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
    return True, "已开始「起本机后端」：先写配置，再起进程，然后等本机配对文件并配对"


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
