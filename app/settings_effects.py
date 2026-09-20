# -*- coding: utf-8 -*-
"""settings_effects.py — 设置变更后的**联动**（wake / router / 智能体-harness）

为什么单独一层
--------------
这段联动原先只长在 `PUT /api/settings` 的处理器里，于是**任何不走那个 HTTP 接口**的写入
都享受不到它。2026-09-20 实测踩到：向导执行相直接调对象方法 `settings.update()`，结果在向导里
选「DSH 标准版」**只写下一行设置**，独立 harness 根本没被拉起 —— 用户看到的是"选了但什么都
没发生"。抽出来之后，面板 API 与向导执行相走**同一份**逻辑，不会各写一遍再分叉。

约定
----
* ``apply()`` **不抛异常**：返回 ``[{"scope", "ok", "detail"}, ...]``，只含**真跑了**的分支。
  由调用方决定呈现：``PUT /api/settings`` 把 router 失败转成 HTTP 400，向导只登记进执行结果
  （不能因为"智能体没起来"就让整场安装中断）。
* 依赖一律**在函数内** import：① 避免 config/runtime 的循环导入；② 测试里 `patch("app.runtime.stop_wake")`
  这类打桩才能生效（模块属性在调用时解析）。
"""
from __future__ import annotations

#: 向导执行相用它：只给"启动后立即失败"留一点探测时间，避免把 `POST /api/wizard/execute`
#: 卡满 harness 的 READY_TIMEOUT（60s —— 首次 npx 装插件确实要那么久，但那是后台的事）。
WIZARD_HARNESS_TIMEOUT = 4.0


def apply(updated, *, harness_timeout=None) -> list:
    """按"哪些键变了"做联动。

    ``updated`` = 变更的键序列（``settings.update()`` 的返回值）。
    ``harness_timeout`` = 等独立 harness 就绪的上限（秒）；``None`` 用 `harness_proc` 的默认。
    """
    keys = [str(k) for k in (updated or [])]
    out = []

    if any(k.startswith("wake") for k in keys):
        out.append(_wake())

    if any(k.startswith("router") for k in keys):
        out.append(_router(updated))

    if any(k.startswith("agent") or k.startswith("harness") for k in keys):
        out.append(_agent(harness_timeout))

    return out


def _wake() -> dict:
    """唤醒监听跟着 `wakeEnabled` 起停。"""
    try:
        from app import runtime
        from app.config import settings
        runtime.stop_wake()
        if settings.get("wakeEnabled", False):
            runtime.start_wake()
        return {"scope": "wake", "ok": True, "detail": "已按新设置重启唤醒监听"}
    except Exception as exc:
        return {"scope": "wake", "ok": False,
                "detail": "%s: %s" % (type(exc).__name__, exc)}


def _router(updated) -> dict:
    """路由相关项落到 ``dsh-failover/config.json`` 并热重载。

    原来这里直接把失败抛成 HTTP 400；现在只回报，由 API 处理器决定抛。
    """
    try:
        from app import router_admin
        ok, detail = router_admin.apply_settings(updated)
        return {"scope": "router", "ok": bool(ok), "detail": str(detail)}
    except Exception as exc:
        return {"scope": "router", "ok": False,
                "detail": "%s: %s" % (type(exc).__name__, exc)}


def _agent(harness_timeout=None) -> dict:
    """智能体相关项：清实例缓存；独立 harness **随选随起 / 随走随停**。

    只停 ECHO 自己拉起的那一个（用户手起的实例不动）—— 判据在 `harness_proc.stop()` 里。
    """
    notes, ok = [], True
    try:
        from app import agents
        agents.reset()                    # 清实例缓存：新选择/新路径立即生效
    except Exception as exc:
        ok = False
        notes.append("清智能体缓存失败：%s" % exc)
    try:
        from app import harness_proc
        if harness_proc.requested():
            # 不给超时时**不传这个 kwarg**：现成的测试替身里有 `lambda: (True, "...")`
            # 这种零参形式，多传一个关键字会把它们打崩。
            if harness_timeout is None:
                hok, msg = harness_proc.ensure_running()
            else:
                hok, msg = harness_proc.ensure_running(timeout=harness_timeout)
            notes.append(str(msg))
            ok = ok and bool(hok)
        else:
            harness_proc.stop()
            notes.append("未选中独立 harness，已停掉 ECHO 自己拉起的那个")
    except Exception as exc:
        ok = False
        notes.append("harness 联动失败：%s" % exc)
    return {"scope": "agent", "ok": ok, "detail": "；".join(n for n in notes if n)}
