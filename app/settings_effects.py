# -*- coding: utf-8 -*-
"""settings_effects.py — 设置变更后的**联动**（wake / router / 智能体-harness / 转写引擎）

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

#: 转写引擎设置 → （boot 组件 id，要不要让组件按新引擎重载）
#: `stt-cmd` 是**常驻**（启动时就预热模型），换引擎必须重载，否则"常驻"的还是旧模型；
#: `stt-meeting` 是**按需**（开会才加载），只校验不预热 —— 不能为改一个设置就把
#: 会议模型塞进显存。
_STT_KEYS = {"sttModel": ("stt-cmd", True), "meetingSttModel": ("stt-meeting", False)}


def _wake_device_keys() -> set:
    """改了这些键会改变**唤醒用的麦**，所以要重开唤醒监听。

    事实源是录音层那张表（`recorder.INPUT_DEVICE_KEYS`）：唤醒跟指令共用"指令"那一侧的
    设备，通用 `inputDeviceId` 是兜底。写死在这里会漂，所以现取；取不到就退回字面量。
    """
    try:
        from app.audio.recorder import INPUT_DEVICE_KEYS
        return {"inputDeviceId", INPUT_DEVICE_KEYS["command"]}
    except Exception:
        return {"inputDeviceId", "commandInputDeviceId"}


def apply(updated, *, harness_timeout=None) -> list:
    """按"哪些键变了"做联动。

    ``updated`` = 变更的键序列（``settings.update()`` 的返回值）。
    ``harness_timeout`` = 等独立 harness 就绪的上限（秒）；``None`` 用 `harness_proc` 的默认。
    """
    keys = [str(k) for k in (updated or [])]
    out = []

    # 唤醒监听：改 wake* 要跟着起停；改**唤醒用的那个麦**也要重开 —— 唤醒是在启动时
    # 读一次设备的（app/audio/wake.py 的 _run_impl），不重启就还是老麦，
    # 表现成"设置改了没用"。（_wake() 自己判断 wakeEnabled，没开就不动。）
    if any(k.startswith("wake") for k in keys) or any(k in _wake_device_keys() for k in keys):
        out.append(_wake())

    if any(k.startswith("router") for k in keys):
        out.append(_router(updated))

    if any(k.startswith("agent") or k.startswith("harness") for k in keys):
        out.append(_agent(harness_timeout))

    # 转写引擎：改了就**当场校验**并让常驻组件重载（否则设置热生效会绕过
    # "这个引擎到底能不能用"，见 _stt 的注释）。
    if any(k in _STT_KEYS for k in keys):
        out.append(_stt(updated))

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
            harness_proc.stop(reason="设置里切走了智能体（agentBackend 不再是 harness）")
            notes.append("未选中独立 harness，已停掉 ECHO 自己拉起的那个")
    except Exception as exc:
        ok = False
        notes.append("harness 联动失败：%s" % exc)
    # 智能体动了就复核一次 ECHO AUTO 注册：标准版的家目录是"选中它"之后才存在的，
    # 不补这一次，只装标准版的机器要等到下次重启才在 DSH 里选得到 ECHO AUTO。
    # 注册失败只写进 detail（不把设置保存判成失败）：路由本身与智能体都不受影响。
    try:
        from app.config import settings as _s
        if _s.get("routerAutoRegister", True):
            from app import llm_router
            sok, sdetail = llm_router.sync()
            notes.append(str(sdetail) if sok else "注册 ECHO AUTO 失败：%s" % sdetail)
    except Exception as exc:
        notes.append("注册 ECHO AUTO 异常：%s" % exc)
    return {"scope": "agent", "ok": ok, "detail": "；".join(n for n in notes if n)}


def _stt(updated) -> dict:
    """换了转写引擎：**当场校验它能不能用**，并让常驻组件按新引擎重载。

    2026-09-23 实测：面板把「命令转写引擎」改成 sherpa（而这台稳定版 `runtime-core`
    里没有 `sherpa_onnx`）之后，`stt-cmd` 组件仍显示「sensevoice · 就绪」——
    直到说第一句命令才静默失败（转写出空串、DSH 什么都没收到）。
    设置热生效不该绕过"这个引擎到底能不能用"，校验结果随 `PUT /api/settings` 一起
    回给面板，用户当场就能看到原因。
    """
    from app.config import settings as _settings
    notes, ok = [], True
    for key, (cid, warm) in _STT_KEYS.items():
        if key not in updated:
            continue
        choice = str(_settings.get(key, "") or "")
        try:
            from app import install_state
            problem = install_state.engine_problem(choice)
        except Exception as exc:                   # 校验自己不能把设置保存搞崩
            problem = "校验失败：%s" % exc
        if problem:
            ok = False
            notes.append("「%s」现在用不了：%s" % (choice or "(空)", problem))
        else:
            notes.append("「%s」可用" % (choice or "(空)"))
        if not warm:
            # 会议引擎是按需的：只校验，不为了改一个设置就把会议模型加载进显存
            continue
        try:
            from app import boot
            started, msg = boot.start_component(cid)
            notes.append("已让「%s」按新引擎重载" % cid if started else "「%s」未重载（%s）" % (cid, msg))
        except Exception as exc:
            notes.append("重载 %s 失败：%s" % (cid, exc))
    return {"scope": "stt", "ok": ok, "detail": "；".join(n for n in notes if n)}
