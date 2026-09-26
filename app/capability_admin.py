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

from typing import Any, Dict, List, Optional

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
    "asr-provider": "网络服务商",
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
    "asr-provider": "只用网络服务商",
    "off": "关掉（不做这件事）",
}

#: 降级原因（**权威十词**，`docs/统一路由` §2）→ 一句人话。
#:
#: 为什么这张表在 Python 侧、不在面板里：面板（`web/meeting.html` 与 `app.js`）
#: 只是把这段文字渲染出来，而**权威词汇在 Python 侧**（`base.SKIP_REASONS`）。
#: 在 JS 里再抄一份，迟早出现"后端说 vector-mismatch、面板显示成别的意思"这种对不上的现象。
REASON_LABELS: Dict[str, str] = {
    "absent": "不在位（没配这台后端 / 本机没装这个能力）",
    "blocked": "被策略挡住（「允许音频去哪」不允许给它）",
    "busy": "正忙（上一次还没结束）",
    "offline": "连不上",
    "circuit-open": "连续失败，暂时跳过",
    "quota": "额度用尽",
    "unsupported": "不支持这件事",
    "vector-mismatch": "向量空间对不上（混用会认错人）",
    "open-failed": "打不开",
    "error": "出错了",
}

#: 时间轴精度档位 → 一句人话（就是 `base` 里那四个）。
TIMESTAMPS_LABELS: Dict[str, str] = {
    "exact": "精确（模型自己给的）",
    "aligned": "对齐（按骨架把文本对上去）",
    "estimated": "估算（按字数均摊）",
    "none": "没有时间轴",
}

#: 同一档位的**短说法**（会议历史列表一行里塞不下上面那句解释）。
#: 只在这里定义一次：列表用短说法、详情用长说法，两处都从这一份表里取词。
TIMESTAMPS_SHORT: Dict[str, str] = {
    "exact": "精确",
    "aligned": "对齐",
    "estimated": "估算",
    "none": "无时间轴",
}


def timestamps_summary(timestamps_kinds: Any) -> Optional[Dict[str, Any]]:
    """`meta.json` 的 `timestampsKinds` → 会议历史列表卡片要的那一格（没记过返回 None）。

    形状：`{kinds: {档位: 段数}, label: "估算（按字数均摊） × 1", short: "估算 × 1"}`。
    数字与档位**全部来自录音当时写下的快照**，这里只做翻译，**不重算** ——
    与 `plan_summary` 里那段是同一份判据（抽出来共用，免得两个接口各说一套）。
    """
    kinds: Dict[str, int] = {}
    for key, value in _as_dict(timestamps_kinds).items():
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue                        # 数不出来就当没记过 —— 别让列表打不开
        if n:
            kinds[str(key)] = n
    if not kinds:
        return None
    pairs = sorted(kinds.items())
    return {
        "kinds": kinds,
        "label": "；".join("%s × %d" % (TIMESTAMPS_LABELS.get(k, k), v) for k, v in pairs),
        "short": "；".join("%s × %d" % (TIMESTAMPS_SHORT.get(k, k), v) for k, v in pairs),
    }


def _slot_label(slot: str) -> str:
    return SLOT_LABELS.get(slot, slot)


def _backend_label(backend_id: str) -> str:
    return BACKEND_LABELS.get(backend_id, backend_id or "（未知）")


def _as_dict(value: Any) -> Dict[str, Any]:
    """当字典用；不是字典就当成空。

    **为什么值得一个小函数**：这些值来自盘上的 `meta.json` —— 可能是老版本写的，
    也可能被人手工编辑过。一个 `"picks": "oops"` 就让 `plan_summary` 抛异常，
    而它的调用方是"打开会议详情"：**一场会的详情页不该因为元数据里一个字段坏了就打不开**。
    （这条是被 `test_it_never_raises_on_a_hand_edited_file` 抓出来的 —— 第一版我直接
    `.items()`，一改坏就 500。）
    """
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    """当列表用；不是列表就当成空（字符串也算"不是列表" —— 它会被逐字符拆开）。"""
    return list(value) if isinstance(value, (list, tuple)) else []


def plan_summary(plan: Any, timestamps_kinds: Any = None,
                 diarize_state: Any = None) -> Optional[Dict[str, Any]]:
    """把会议里记下的执行计划（`Plan.as_dict()` 那份）翻成面板能直接渲染的形状。

    **这是"这场会实际用了谁"的唯一来源。** 读的是录音时写进 `meta.json` 的快照，
    **不重新算一遍计划**：重算得到的是"现在会选谁"，与"当时选了谁"可能不同
    （配置改了、后端掉了），而人问的恰恰是后者。

    `diarize_state` 是 `meta.json` 里的 `diarize`（`meeting._new_diarize_state()` 那份）：
    **会议一定有说话人分离**（2026-09-26 概念纠正），所以"没做成"必须说得出来 ——
    它是这次改动里最要紧的一句人话（原来那版会静默产出一场"没有说话人却像正常"的会议）。

    认不出来的槽 / 后端 / 原因**一律退回原始标识符**，不猜也不丢：宁可让人看到
    `asr.streaming` 这种内部词，也不要显示一个错误的翻译。

    整个函数**不抛异常**（输入来自盘上可能被手改坏的 `meta.json`）。
    """
    dia = diarize_summary(diarize_state)
    if not isinstance(plan, dict):
        # 没有执行计划也不算"什么都没有"：分离没做成这一条照样要说（老会议没有 plan，
        # 但新写的 `diarize` 快照是有的）。时间轴档位与 plan 是**两件独立的记录**
        # （老会议就是"有档位、没有 plan"），所以这里同样要带上它。
        ts0 = timestamps_summary(timestamps_kinds) or {}
        return {"picks": [], "skipped": [], "candidates": {}, "notes": [],
                "vectorSpaceId": "", "timestampsKinds": ts0.get("kinds") or {},
                "timestampsLabel": ts0.get("label") or "",
                "diarize": dia} if dia else None
    picks = []
    for slot, pick in sorted(_as_dict(plan.get("picks")).items()):
        if not isinstance(pick, dict):
            continue
        picks.append({
            "slot": slot, "slotLabel": _slot_label(slot),
            "backendId": pick.get("backendId", ""),
            "backendLabel": _backend_label(pick.get("backendId", "")),
            "vectorSpaceId": pick.get("vectorSpaceId", ""),
        })
    skipped = []
    for item in _as_list(plan.get("skipped")):
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        skipped.append({
            "slot": item.get("slot", ""), "slotLabel": _slot_label(item.get("slot", "")),
            "backendId": item.get("backendId", ""),
            "backendLabel": _backend_label(item.get("backendId", "")),
            "reason": reason,
            "reasonLabel": REASON_LABELS.get(reason, reason or "（没给原因）"),
            "detail": str(item.get("detail") or ""),
        })
    # 时间轴档位与详情**共用同一份翻译**（`timestamps_summary`）：会议历史列表与
    # 会议详情是同一件事的两处展示，各算一遍迟早对不上。
    ts = timestamps_summary(timestamps_kinds) or {}
    kinds = ts.get("kinds") or {}
    if not (picks or skipped or kinds or dia):
        # 什么都没记（老会议 / 走的是本机回退路径）→ 返回 None 当"没有这段信息"，
        # 而不是给面板一个空壳（空壳会渲染成"用了谁：无"，看着像出了问题）。
        return None
    return {
        "picks": picks,
        "skipped": skipped,
        # `candidates` / `notes` 原样带上：排障细节，面板自己决定折起来还是展开，
        # 服务端不预先删减（删了就得在别处再判一次"哪些值得留"）。
        "candidates": dict(_as_dict(plan.get("candidates"))),
        "notes": [str(n) for n in _as_list(plan.get("notes"))],
        "vectorSpaceId": plan.get("vectorSpaceId", ""),
        "timestampsKinds": kinds,
        "timestampsLabel": ts.get("label") or "",
        # 分离这一场的结论（成没成 / 不成是为什么）。**面板不许自己编这句话**。
        "diarize": dia,
    }


#: 「分离没做成」在面板上的固定说法。**前半句是硬契约**：任务要求会议记录与面板
#: 都要能一眼看到「说话人分离未执行：<真原因>」，所以那个前缀单独留成常量，
#: 用例断言它、面板直接渲染它，谁都不许改写一遍。
DIARIZE_NOT_EXECUTED = "说话人分离未执行"


def diarize_summary(state: Any) -> Optional[Dict[str, Any]]:
    """`meta.json` 里的 `diarize` → 面板能直接渲染的一小块（`None` = 没这段信息）。

    形状：
      * `executed`     这一场到底标没标上说话人；
      * `reason` / `reasonLabel`  **权威十词之一** + 它的中文（`REASON_LABELS`）——
        只有 `executed=False` 时才有意义；
      * `code` / `retryable`      服务端给的两档（"客户端该干什么"）；没有就是空/假；
      * `backendLabel`  这一场的分离是谁做的（走了后端时才有）；
      * `headline`      **给人看的那一句**（`说话人分离未执行：<原因>`）。

    认不出的 `reason` **原样返回**（与 `plan_summary` 同一条纪律：宁可露出内部词，
    也不要显示一个错误的翻译）。整个函数不抛异常（输入来自盘上可被手改坏的文件）。
    """
    if not isinstance(state, dict):
        return None
    executed = bool(state.get("executed"))
    reason = str(state.get("reason") or "")
    backend_id = str(state.get("backendId") or "")
    out = {
        "executed": executed,
        "reason": reason,
        "reasonLabel": REASON_LABELS.get(reason, reason),
        "detail": str(state.get("detail") or ""),
        "code": str(state.get("code") or ""),
        "retryable": bool(state.get("retryable")),
        "backendId": backend_id,
        "backendLabel": _backend_label(backend_id) if backend_id else "",
    }
    if executed:
        out["headline"] = ("说话人分离已执行"
                           + ("（%s）" % out["backendLabel"] if out["backendLabel"] else ""))
    else:
        # 原因认不出时也要给一句能读的（`reason` 为空 = 老版本/手改坏的文件）
        out["headline"] = "%s：%s" % (DIARIZE_NOT_EXECUTED,
                                      out["reasonLabel"] or "原因未记录")
    return out


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

def pair(base_url: str, code: str, client_name: str = "",
         cert_fingerprint: str = "") -> tuple:
    """配对并落盘。返回 `(ok, 一句话)` —— 与 `router_admin` 那批同样的形状。

    **失败不抛给调用方**：配对失败是用户的日常（码抄错、机器没开、码用过了），
    每一种都该显示成一句人话，而不是一个 500。

    没给名字时用**这台机器的计算机名**：管理员那边 `--list-clients` 看到的清单才有意义
    （服务端只在配对码上没写名字时才用它，所以这不会盖掉管理员填的资产名）。

    `cert_fingerprint` 来自配对串里的 `fp=`（设计 §7.5 ①）：给了就**必须对上**，
    对不上就拒绝 —— 那才是"防中间人"的那一步。留空 = TOFU（第一次见谁信谁），
    与以前的行为一致。
    """
    from app.capabilities import pairing
    if not str(client_name or "").strip():
        try:
            import socket
            client_name = socket.gethostname()
        except Exception:                                   # pragma: no cover
            client_name = ""
    try:
        creds = pairing.pair(base_url, code, client_name=client_name,
                             cert_fingerprint=cert_fingerprint)
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
