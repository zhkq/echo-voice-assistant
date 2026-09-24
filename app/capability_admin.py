# -*- coding: utf-8 -*-
"""「能力路由」页签背后的活儿（客户端侧）。

这个模块只做两件事：**把后端与配对的状态整理成面板能直接渲染的形状**，
以及**替人去做那两件必须由人来点的动作**（配对 / 解除配对）。

## 一条纪律：这里不重新算"会选中谁"

面板最想显示的是"开会的时候到底谁去转写"。但这个答案**只能有一个算法**
（`router.plan`，按槽 + privacy + 用途算），而真正用它的是会议主链路。
面板要是自己再算一遍，迟早和主链路给出的不一样 —— 那时人看到的是一张
"说会走 GPU 其实走了本机"的表，比不显示更糟。

所以这里显示的是**事实**：每个后端自己声明了什么、健不健康、配的是哪个后端、
允许音频去哪。至于"上一场会实际用了谁"，答案在被记下来的执行计划里
（会议 `meta`），那是会议侧的事，不在这里猜。
"""
from __future__ import annotations

from typing import Any, Dict, List

#: 面板上的中文名。**放这一层而不是契约层**：`base.SLOTS` 是给路由用的词汇表，
#: 往里面塞展示文案会让"契约"变成"界面"。
SLOT_LABELS: Dict[str, str] = {
    "asr.text": "转写文本",
    "asr.timestamps": "句级时间轴",
    "asr.streaming": "流式转写",
    "diarize.turns": "说话人时间轴",
    "diarize.embeddings": "说话人嵌入",
    "diarize.turn_embeddings": "逐句嵌入",
    "speaker.embed": "声纹嵌入",
    "tts": "语音合成",
    "wake": "唤醒词",
}

BACKEND_LABELS: Dict[str, str] = {
    "local": "本机",
    "echo-server": "ECHO 后端",
    "intranet": "内网公共 ASR",
}

#: 哪个设置键管哪些槽 —— **面板要按人来分组**（"开会转写用哪个后端"而不是
#: "asr.text 用哪个后端"）。与 `router._setting_key_for` 是同一份映射的两个方向；
#: 那边是"槽 → 键"，这边是"键 → 槽"。**改一边必须改另一边**，用例钉着。
SETTING_SLOTS: Dict[str, tuple] = {
    "capabilityMeetingAsrBackend": ("asr.text", "asr.timestamps"),
    "capabilityDiarizeBackend": ("diarize.turns", "diarize.embeddings",
                                 "diarize.turn_embeddings"),
    "capabilityEmbedBackend": ("speaker.embed",),
}

#: 这个页签上要显示/可改的设置项（顺序即显示顺序）。
#: 两个令牌设置**不在里面**：它们是排障用的（`capabilityEchoServerToken` /
#: `…StaticToken`），填了会盖过配对凭据 —— 摆在配对区旁边只会让人以为"配对要填令牌"。
ROUTING_KEYS: tuple = ("capabilityEchoServerUrl", "capabilityPrivacy") + \
    tuple(SETTING_SLOTS)

#: 设置的当前值 → 客户端说的人话。
CHOICE_LABELS: Dict[str, str] = {
    "auto": "自动（按优先级挑第一个可用的）",
    "echo-server": "只用 ECHO 后端",
    "local": "只用本机",
    "intranet": "只用内网公共 ASR",
    "off": "关掉（不做这件事）",
}


def _setting(key, default=None):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:                                       # pragma: no cover
        return default


def _backend_rows(force: bool) -> List[Dict[str, Any]]:
    """每个后端一行。`describe()` 是后端自己说的，这里**一个字段都不改**。"""
    from app.capabilities.router import build_default_router
    router = build_default_router()
    rows: List[Dict[str, Any]] = []
    for c in router.clients():
        try:
            if force:
                c.refresh(force=True)
        except Exception:
            pass                       # 探测失败是常态，`describe` 里会体现出来
        d = dict(c.describe())
        d["label"] = BACKEND_LABELS.get(d.get("backendId", ""), d.get("backendId", ""))
        d["slotsLabeled"] = [{"slot": s, "label": SLOT_LABELS.get(s, s)}
                             for s in (d.get("provides") or [])]
        rows.append(d)
    return rows


def pair_view() -> Dict[str, Any]:
    """配对状态（**绝不含 secret**）。"""
    from app.capabilities import pairing
    return pairing.state()


def view(force: bool = False) -> Dict[str, Any]:
    """「能力路由」页签要的全部数据。**一次算完，面板不用自己拼。**"""
    from app.config import settings

    choices = []
    for key, slots in SETTING_SLOTS.items():
        value = str(_setting(key, "auto") or "auto")
        choices.append({
            "key": key,
            "slots": list(slots),
            "slotsLabeled": [{"slot": s, "label": SLOT_LABELS.get(s, s)} for s in slots],
            "value": value,
            "label": CHOICE_LABELS.get(value, value),
        })
    # 设置行**由 `Settings.all()` 出**（含 hidden），形状与设置页完全相同 ——
    # 面板直接复用 `renderSettingRow`，于是标签/下拉/说明的样子天然一致，
    # 也不会出现"设置页能改、这里少一项"的漂移。
    rows = [r for r in settings.all(include_hidden=True) if r.get("key") in ROUTING_KEYS]
    order = {k: i for i, k in enumerate(ROUTING_KEYS)}
    rows.sort(key=lambda r: order.get(r.get("key"), 1e6))
    return {
        "pair": pair_view(),
        "privacy": str(_setting("capabilityPrivacy", "lan") or "lan"),
        "backends": _backend_rows(force),
        "choices": choices,
        "settings": rows,
        "backendLabels": BACKEND_LABELS,
        "slotLabels": SLOT_LABELS,
    }


# ---------------------------------------------------------------- 动作

def pair(base_url: str, code: str, client_name: str = "") -> tuple:
    """配对并落盘。返回 `(ok, 一句话)` —— 与 `router_admin` 那批同样的形状。

    **失败不抛给调用方**：配对失败是用户的日常（码抄错、机器没开、码用过了），
    每一种都该显示成一句人话，而不是一个 500。

    没给名字时用**这台机器的计算机名**：管理员那边 `--list-clients` 看到的清单才有意义
    （服务端只在配对码上没写名字时才用它，所以这不会盖掉管理员填的资产名）。
    """
    from app.capabilities import pairing
    if not str(client_name or "").strip():
        try:
            import socket
            client_name = socket.gethostname()
        except Exception:                                   # pragma: no cover
            client_name = ""
    try:
        creds = pairing.pair(base_url, code, client_name=client_name)
    except pairing.PairingError as e:
        return False, str(e)
    except Exception as e:                                  # pragma: no cover - 兜底
        return False, "配对时出了意外：%s" % e
    return True, "已配对：%s（%s）" % (creds.server_name or creds.base_url, creds.client_id)


def unpair() -> tuple:
    """解除配对（只忘掉本机凭据；服务端那本账归管理员）。"""
    from app.capabilities import pairing
    if pairing.unpair():
        return True, "已解除配对。要再用这台后端，得让管理员重新发一张配对码"
    return False, "凭据文件删不掉（可能正被占用），请稍后再试"


def probe() -> tuple:
    """立即重问一遍所有后端。返回 `(ok, view)`。"""
    try:
        return True, view(force=True)
    except Exception as e:                                  # pragma: no cover - 兜底
        return False, {"error": str(e)}
