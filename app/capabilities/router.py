# -*- coding: utf-8 -*-
"""能力路由：**按槽选后端、失败换下一个、并把"为什么"留下来。**

它做四件事，每件都对应一条会被写错就出事的东西：

| 做什么 | 不这么做会怎样 |
|---|---|
| 按槽把活派给"声明自己能干"的后端 | 把 `diarize` 派给只会转写的内网公共服务 → 每次失败 |
| 失败**换下一个**，而不是整条链路失败 | 一个后端挂了 = 一场会议没有转写 |
| **铁律 L2/L5**：说话人这一家子只落在能声明 `vectorSpaceId` 的后端、且全场同源 | 跨向量空间比余弦相似度 → **认错人且不报错** |
| **铁律 L3**：指令链路的主选必须是本机 | 服务端一挂，"说句话"这件事就不可用了 |
| 每次都留下 `skipped` + `reason` | 面板只能显示"失败了"，用户不知道**为什么没用他要的那个** |

## 为什么 `skipped` 一定要带原因

设计（`docs/统一路由` §2）把这条写成了硬要求：*"用户要知道为什么没用我要的那个设备/后端，
而不是只看到一个失败了"*。所以**挑不中也要说话** —— 这是本模块与"一个 for 循环试到底"
的区别。

## 一个刻意没做的降级

`quality-rejected`（"结果不达标：空文本 / 说话人数异常"）在权威词汇里，
但**这里不自动降级**：转写结果为空**绝大多数时候是"这段没人说话"**，
而不是后端不行。因为它去换后端，会做出"安静片段 → 换后端 → 还是安静 → 再换"这种
既浪费又难查的行为。空结果如实返回，让**上游**（会议流水线）按上下文判断。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.capabilities.base import (
    BACKEND_ECHO_SERVER,
    BACKEND_INTRANET,
    BACKEND_LOCAL,
    LOCAL_ONLY_SLOTS,
    PRIVACY_ORDER,
    SAME_SOURCE_SLOTS,
    SLOTS,
    SOURCE_LAN,
    SOURCE_LOCAL,
    SOURCE_WAN,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    DiarizeResult,
    EmbedResult,
)

#: 每轮最多试几个后端。**不是**"重试次数" —— 换的是**后端**，不是同一个后端再打一次。
#: 上限存在的意义：别让一次调用把一整列后端挨个试到超时，把交互拖死。
MAX_ATTEMPTS = 3

#: 一个槽谁都干不了时，**先报哪个原因**。
#:
#: 为什么要挑：计划里会同时留下好几条 `skipped`，其中 `absent`（"没配这个后端"）
#: 几乎是必然出现的（默认表里那些后端大半没配）。但如果真实原因是
#: `unsupported`（"它不支持这个槽"），报 `absent` 就是把用户往错的方向指 ——
#: 他会去配一个**配了也没用**的后端。
#: 设计 §2 的要求是"用户要知道为什么没用我要的那个"，所以这里按**信息量**排，
#: 而不是按遍历顺序。
REASON_PRIORITY: Tuple[str, ...] = (
    "vector-mismatch",     # 最具体：说得出是哪个空间对不上
    "blocked",             # 策略/凭据：用户能去改
    "unsupported",         # 能力不匹配：用户能去换后端
    "quota",
    "busy",
    "circuit-open",
    "offline",
    "open-failed",
    "error",
    "absent",              # 最后才轮到"没配"
)

#: 默认优先级（`auto` 时用）。顺序 = 质量与可控性的折中：
#:   ECHO 后端（可控、有向量空间） → 本机（不出机但弱） → 内网公共（不可控）
#: 注意 `wake` 与**指令**链路不走这张表（L3 把它钉在本机）。
DEFAULT_ORDER: Dict[str, Tuple[str, ...]] = {
    "asr.text": (BACKEND_ECHO_SERVER, BACKEND_LOCAL, BACKEND_INTRANET),
    "asr.timestamps": (BACKEND_ECHO_SERVER, BACKEND_LOCAL),
    "asr.streaming": (BACKEND_LOCAL,),          # 流式是"边说边出"，只有本机做得到
    "wake": (BACKEND_LOCAL,),                   # 铁律 L3
    "diarize.turns": (BACKEND_ECHO_SERVER, BACKEND_LOCAL),
    "diarize.embeddings": (BACKEND_ECHO_SERVER, BACKEND_LOCAL),
    "diarize.turn_embeddings": (BACKEND_ECHO_SERVER,),
    "speaker.embed": (BACKEND_ECHO_SERVER, BACKEND_LOCAL),
    "tts": (),                                  # 不走能力层（客户端自己的 providers 管）
}


@dataclass(frozen=True)
class Need:
    """一个需求：要哪些槽 + 约束。

    `purpose` 不是业务概念，而是**判据**：`command` 走铁律 L3（主选必须本机），
    `meeting` 才允许把音频发到后端。两个都不能省 —— 这正是"指令与会议是两个不同队列"
    在能力层的落点。
    """
    slots: Tuple[str, ...]
    purpose: str = "meeting"            # command | meeting
    privacy: str = ""                   # 空 = 用设置里的 capabilityPrivacy
    lock_vector_space: bool = True
    #: 已经锁定的向量空间（同一场会议第二次分段时带上）。空 = 本次决定并锁定。
    vector_space_id: str = ""

    def __post_init__(self):
        for s in self.slots:
            if s not in SLOTS:
                raise ValueError("不是能力槽：%r（槽清单见 base.SLOTS）" % (s,))
        if self.privacy and self.privacy not in PRIVACY_ORDER:
            raise ValueError("privacy 只能是 %s" % list(PRIVACY_ORDER))


@dataclass
class Pick:
    """一个槽落到哪个后端 + **为什么是它**。"""
    slot: str
    backend_id: str
    reason: str = ""
    vector_space_id: str = ""


@dataclass
class Skipped:
    """一个没被选中的候选 + 原因（权威词汇）。**面板排障靠它。**"""
    slot: str
    backend_id: str
    reason: str
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"slot": self.slot, "backendId": self.backend_id,
                "reason": self.reason, "detail": self.detail}


@dataclass
class Plan:
    """一份执行计划：每个槽选谁、以及**谁被跳过了、为什么**。

    设计 §4.4 要求"按会议生成一次并写进 `meta.json`，整场锁定" —— 所以这份对象
    要能序列化（`as_dict`），而不只是一次调用的临时产物。
    """
    picks: Dict[str, Pick] = field(default_factory=dict)
    skipped: List[Skipped] = field(default_factory=list)
    vector_space_id: str = ""
    notes: List[str] = field(default_factory=list)
    #: 每个槽**按优先级排好的、确实可用的**后端 id。
    #:
    #: 为什么单独存一份：`call()` 要"失败换下一个"，而"下一个"必须是**筛过的**候选。
    #: 早先它是拿 `_order_for()` 重新算的 —— 那张表里有 echo-server / intranet
    #: 这些**压根没注册**的名字，于是"尝试预算"被它们占掉，真正能用的备选反而被
    #: MAX_ATTEMPTS 截掉了（表现是"明明有备选却说都不行"）。
    candidates: Dict[str, List[str]] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"picks": {k: {"backendId": v.backend_id, "reason": v.reason,
                              "vectorSpaceId": v.vector_space_id}
                          for k, v in self.picks.items()},
                "skipped": [s.as_dict() for s in self.skipped],
                "candidates": {k: list(v) for k, v in self.candidates.items()},
                "vectorSpaceId": self.vector_space_id,
                "notes": list(self.notes)}

    def backend_for(self, slot: str) -> str:
        p = self.picks.get(slot)
        return p.backend_id if p else ""


def most_informative(skips: Sequence[Skipped]) -> Optional[Skipped]:
    """从一堆 `skipped` 里挑出**最该说给用户听**的那条。"""
    if not skips:
        return None
    rank = {r: i for i, r in enumerate(REASON_PRIORITY)}
    return sorted(skips, key=lambda s: (rank.get(s.reason, len(REASON_PRIORITY)),
                                       s.backend_id))[0]


class CapabilityRouter:
    """按槽派活。**无状态之外的一切都在这里**（健康、降级、向量空间锁）。"""

    def __init__(self, clients: Sequence[CapabilityClient] = (),
                 settings_get=None, log=None):
        self._clients: Dict[str, CapabilityClient] = {c.backend_id: c for c in clients}
        self._get = settings_get or _default_setting
        self._log = log or _default_log

    # ---------------------------------------------------------------- 注册

    def register(self, client: CapabilityClient) -> None:
        self._clients[client.backend_id] = client

    def clients(self) -> List[CapabilityClient]:
        return list(self._clients.values())

    def get(self, backend_id: str) -> Optional[CapabilityClient]:
        return self._clients.get(backend_id)

    # ---------------------------------------------------------------- 计划

    def plan(self, need: Need) -> Plan:
        """给每个槽挑一个后端。**不联网**（用各后端已缓存的能力声明与向量空间）。"""
        out = Plan()
        privacy = need.privacy or str(self._get("capabilityPrivacy", "lan") or "lan")
        allowed_sources = _sources_for_privacy(privacy)
        locked = str(need.vector_space_id or "")
        out.vector_space_id = locked

        for slot in need.slots:
            order = self._order_for(slot, need)
            if not order:
                out.skipped.append(Skipped(slot, "", "absent", "没有配置任何后端"))
                continue
            eligible: List[str] = []
            slot_locked = locked            # 本槽开始时的锁（L5 只在槽内推进）
            for backend_id in order:
                client = self._clients.get(backend_id)
                if client is None:
                    out.skipped.append(Skipped(slot, backend_id, "absent", "没配这个后端"))
                    continue
                if client.source not in allowed_sources:
                    out.skipped.append(Skipped(
                        slot, backend_id, "blocked",
                        "privacy=%s 不允许用 %s 上的后端" % (privacy, client.source)))
                    continue
                if not client.supports(slot):
                    out.skipped.append(Skipped(
                        slot, backend_id, "unsupported",
                        "它只提供 %s" % (", ".join(sorted(client.provides)) or "（什么都不提供）")))
                    continue
                # 铁律 L2 / L5 的合流处：说话人这一家子**必须同源**。
                # 详见 `base.SAME_SOURCE_SLOTS` —— 简言之：标签与嵌入分属两家就完全对不上，
                # 而"对不上"的表现是**认错人且不报错**。
                if slot in SAME_SOURCE_SLOTS and not client.vector_space_id:
                    out.skipped.append(Skipped(
                        slot, backend_id, "vector-mismatch",
                        "它没说自己的向量空间，拿它的结果去比会认错人"))
                    continue
                if slot in SAME_SOURCE_SLOTS and slot_locked and \
                        client.vector_space_id != slot_locked:
                    out.skipped.append(Skipped(
                        slot, backend_id, "vector-mismatch",
                        "它的空间是 %s，本场已锁定 %s" % (client.vector_space_id, slot_locked)))
                    continue
                # 通过全部判据 → 这是一个**真的可用**的候选（也是 `call()` 的备选池）
                eligible.append(backend_id)
                if slot not in out.picks:
                    out.picks[slot] = Pick(slot, backend_id,
                                           self._why(slot, backend_id, need, privacy),
                                           client.vector_space_id)
                    # 本场第一次拿到向量 → 锁定它（L5 的起点），**后面的候选按新锁重判**
                    if slot in SAME_SOURCE_SLOTS and not locked and client.vector_space_id:
                        locked = client.vector_space_id
                        out.vector_space_id = locked
                        slot_locked = locked
            out.candidates[slot] = eligible
            if not eligible:
                out.skipped.append(Skipped(slot, "", "absent", "所有后端都不行"))
        return out

    def _order_for(self, slot: str, need: Need) -> Tuple[str, ...]:
        """候选顺序：**指令链路先钉死本机**（L3），其余按设置 → 默认表。"""
        if slot in LOCAL_ONLY_SLOTS or (need.purpose == "command" and slot == "asr.text"):
            return (BACKEND_LOCAL,)
        explicit = str(self._get(_setting_key_for(slot), "auto") or "auto").strip()
        if explicit == "off":
            return ()
        if explicit and explicit != "auto":
            # 用户点名了：**只试它**（"换下一个"会让"我要用这个"变成一句空话）
            return (explicit,)
        order = DEFAULT_ORDER.get(slot, ())
        if not self._get("capabilityEchoServerUrl", ""):
            # 没配后端地址时把 echo-server 剔掉，免得每轮都白试一次
            order = tuple(b for b in order if b != BACKEND_ECHO_SERVER)
        # **注册了但不在默认表里的后端也要被考虑**，追加在后面。
        # 这条是踩出来的：默认表只写了三个已知 id，于是"名字不在这三个里"的后端
        # （测试替身、将来新增的实现）**注册了却永远不会被选中**，
        # 而表现是"它明明声明支持这个槽，路由却报 absent"—— 一个很费解的哑谜。
        extra = tuple(b for b in self._clients if b not in order)
        return order + extra

    def _why(self, slot: str, backend_id: str, need: Need, privacy: str) -> str:
        if need.purpose == "command" and slot == "asr.text":
            return "指令链路（铁律 L3：主选必须本机）"
        if backend_id == BACKEND_LOCAL:
            return "指定用本机" if self._get(_setting_key_for(slot), "auto") == "local" \
                else "前面的后端不可用/没配，落到本机"
        if backend_id == BACKEND_ECHO_SERVER:
            return "指定用 ECHO 后端" if self._get(_setting_key_for(slot), "auto") == "echo-server" \
                else "默认优先 ECHO 后端（可控、有向量空间）"
        return "privacy=%s 允许，且它声明支持这个槽" % privacy

    # ---------------------------------------------------------------- 调用

    def call(self, slot: str, need: Need, **kw):
        """按计划调用一个槽。**失败换下一个后端**，全都不行才抛。

        返回 `(结果, Plan)` —— 计划要跟着结果走：调用方（会议流水线）需要把
        "这次实际用了谁、跳过了谁、为什么"写进 `meta.json` 与日志。
        """
        plan = self.plan(need)
        pick = plan.picks.get(slot)
        if pick is None:
            reasons = [s for s in plan.skipped if s.slot == slot]
            best = most_informative(reasons)
            raise CapabilityError(
                best.reason if best else "absent",
                "没有任何后端能提供 %s：%s" % (
                    slot, "; ".join("%s(%s)" % (s.backend_id or "-", s.reason)
                                    for s in reasons) or "没配后端"),
                slot=slot)

        tried: List[str] = []
        last: Optional[CapabilityError] = None
        for backend_id in (plan.candidates.get(slot) or [pick.backend_id])[:MAX_ATTEMPTS]:
            client = self._clients.get(backend_id)
            if client is None:
                continue
            t0 = time.time()
            try:
                result = self._invoke(client, slot, kw)
            except CapabilityError as e:
                last = e
                tried.append("%s(%s)" % (backend_id, e.reason))
                self._note_degrade(slot, backend_id, e, need, time.time() - t0)
                plan.skipped.append(Skipped(slot, backend_id, e.reason, e.detail))
                plan.picks.pop(slot, None)
                continue
            if backend_id != pick.backend_id:
                plan.picks[slot] = Pick(slot, backend_id, "上一个后端失败后换到它",
                                        client.vector_space_id)
                if slot in SAME_SOURCE_SLOTS and client.vector_space_id:
                    plan.vector_space_id = client.vector_space_id
            return result, plan

        raise CapabilityError(
            last.reason if last else "absent",
            "试过 %s 都不行：%s" % (", ".join(tried) or "(没有可选后端)",
                                    last.detail if last else ""),
            code=last.code if last else "", slot=slot,
            retry_after=last.retry_after if last else None,
            retryable=last.retryable if last else False)

    def _attempt_order(self, slot: str, first: str, need: Need) -> List[str]:
        """（保留给需要"重新算一遍候选"的调用方。）

        `call()` 用的是计划里已经筛好的 `plan.candidates[slot]` —— 重新算一遍的代价
        不只是浪费，还会**算错**：`_order_for()` 那张表里有压根没注册的名字，
        它们会占掉尝试预算。（这个名字保留是为了不悄悄改掉一个被测试引用的接口。）
        """
        pool = self._clients
        rest = [b for b in self._order_for(slot, need) if b != first and b in pool]
        return ([first] + rest)[:MAX_ATTEMPTS]

    def _invoke(self, client: CapabilityClient, slot: str, kw: Dict[str, Any]):
        if slot in ("asr.text", "asr.timestamps"):
            return client.transcribe(kw.get("wav", ""), lang=kw.get("lang", "auto"),
                                     want_timestamps=bool(kw.get("want_timestamps"))
                                     or slot == "asr.timestamps",
                                     variant=kw.get("variant", "long"))
        if slot in ("diarize.turns", "diarize.embeddings"):
            return client.diarize(kw.get("wav", ""), max_speakers=kw.get("max_speakers"))
        if slot == "speaker.embed":
            return client.embed(kw.get("wav", ""), count=int(kw.get("count", 1) or 1))
        raise CapabilityError("unsupported", "能力层不处理这个槽：%s" % slot, slot=slot)

    # ---------------------------------------------------------------- 观测

    def _note_degrade(self, slot: str, backend_id: str, err: CapabilityError,
                      need: Need, seconds: float) -> None:
        """每次降级写一条日志（设计 §5.2 的硬要求）。

        **只记元数据**：槽 / 从哪 / 为什么 / 耗时。没有音频、没有文本 ——
        与"服务端不存内容"同一条纪律，客户端这边的日志也不该攒内容。
        """
        try:
            self._log("warn", "capability",
                      "降级 slot=%s from=%s reason=%s code=%s %.2fs purpose=%s"
                      % (slot, backend_id, err.reason, err.code or "-", seconds,
                         need.purpose))
        except Exception:
            pass

    def describe(self) -> List[Dict[str, Any]]:
        """给面板「能力路由」页签：每个后端是谁、在哪、能干什么、健不健康。"""
        return [c.describe() for c in self._clients.values()]


# ---------------------------------------------------------------- 小工具

def _sources_for_privacy(privacy: str) -> set:
    """privacy 越严，允许的来源越少。**这是"哪些后端根本不被考虑"的判据。**"""
    level = PRIVACY_ORDER.get(privacy, 1)
    allowed = {SOURCE_LOCAL}
    if level >= PRIVACY_ORDER["lan"]:
        allowed.add(SOURCE_LAN)
    if level >= PRIVACY_ORDER["wan"]:
        allowed.add(SOURCE_WAN)
    return allowed


def _setting_key_for(slot: str) -> str:
    """槽 → "用哪个后端"那个设置键。"""
    return {
        "asr.text": "capabilityMeetingAsrBackend",
        "asr.timestamps": "capabilityMeetingAsrBackend",
        "diarize.turns": "capabilityDiarizeBackend",
        "diarize.embeddings": "capabilityDiarizeBackend",
        "speaker.embed": "capabilityEmbedBackend",
    }.get(slot, "")


def _default_setting(key, default=None):
    try:
        from app.config import settings
        return settings.get(key, default)
    except Exception:
        return default


def _default_log(level, source, message):
    try:
        from app import db
        db.add_log(level, source, message)
    except Exception:
        pass


def build_default_router(settings_get=None, log=None) -> CapabilityRouter:
    """按设置造一个路由器：本机 + （配了地址才有的）ECHO 后端。

    内网公共服务（`intranet`）**还没做**（那是施工顺序的 step 2），
    所以这里不造它 —— 造一个空壳只会让"配了却永远失败"变成一个谜。
    """
    from app.capabilities.local import LocalCapabilityClient

    clients: List[CapabilityClient] = [LocalCapabilityClient()]
    get = settings_get or _default_setting
    if str(get("capabilityEchoServerUrl", "") or "").strip():
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient()
        c.refresh()                     # 拉一次 capabilities（失败不抛，只是不支持任何槽）
        clients.append(c)
    return CapabilityRouter(clients, settings_get=settings_get, log=log)
