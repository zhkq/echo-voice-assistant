# -*- coding: utf-8 -*-
"""能力层的契约 —— 槽、结果、错误、以及 `CapabilityClient` 的形状。

这是"**客户端怎么用后端**"的唯一一份契约。它刻意只做三件事，别往里塞业务：

1. **槽是唯一的接口词汇。** 有哪几种能力、名字怎么写，全在这一个文件里
   （`docs/能力路由-三后端与轻客户端.md` §4.2）。调用方说"我要 `asr.text`"，
   不关心它是本机 sherpa 还是服务端 qwen3asr —— 那是路由的事。

2. **结果必须带出处。** 每个结果都带 `backend_id` / `model_version`；
   产出向量的还带 `vector_space_id`。这三样是**下游做判断的依据**
   （能不能比、能不能换、要不要重新入库），不是装饰。

3. **失败必须分类**（`docs/统一路由` §2 那套词）。"失败"是最没用的信息：
   调用方要知道**下一步该干什么** —— 是换后端、是等一会儿、是改分段、还是别试了。

## 与既有 `app/providers/` 的分工（别搞混）

| | `app/providers/` | `app/capabilities/`（本包） |
|---|---|---|
| 选择方式 | **从 N 选 1**（同一时刻只有一个） | **从 N 选 M 并拼装**（一场会议可能同时用三四个） |
| 粒度 | 一整类能力（llm / tts / 整段 asr） | **槽**（`asr.text` 与 `asr.timestamps` 可以是不同后端） |
| 状态 | 用户选定的那一个 | 每个槽一份**执行计划**，按会议生成一次并锁定 |

把槽塞进 `providers/` 会把那个抽象撑坏（3.0 总览 §9 的裁定 #1）。
`providers/` 保留 llm / tts / 整段 asr，**不新增 kind**。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 槽

#: 能力槽清单（`docs/能力路由` §4.2，**逐字对齐**，加一个都要改文档）。
#:
#: 注意**没有 `asr.long`**：那是**服务端模型自己的主槽名**（它的 `models[].slot`），
#: 不是客户端要说的词汇。客户端只说 `asr.text` / `asr.timestamps` / `asr.streaming`，
#: 服务端通过 `capabilities.slots`（`slot` + `supports` 的并集）来回答"我这个能不能满足你"。
#: 这条边界是本文件最容易被搞错的地方 —— 服务端报 `asr.long` 不代表客户端要认识它。
SLOTS: Tuple[str, ...] = (
    "wake",
    "asr.text",
    "asr.timestamps",
    "asr.streaming",
    "diarize.turns",
    "diarize.embeddings",
    "diarize.turn_embeddings",
    "speaker.embed",
    "tts",
)

#: **产出向量**的槽 —— 铁律 L2 的适用面：只跑在**能声明 vectorSpaceId** 的后端上。
#: 混用不同向量空间做余弦相似度**不报错，只是认错人**，所以这条要机械拦住。
VECTOR_SLOTS: Tuple[str, ...] = (
    "diarize.embeddings",
    "diarize.turn_embeddings",
    "speaker.embed",
)

#: **必须与向量同源**的槽 = 上面那三个 **加上 `diarize.turns`**。
#:
#: `diarize.turns` 自己不产出向量（它出的是 `[(start,end,speaker)]`），但它出的
#: **说话人标签只在同一次分离里才有意义** —— 拿 A 后端的标签去配 B 后端的嵌入，
#: 标签根本对不上。设计 §4.4 的拼装规则把这条写死了：
#: *"同一场会议的所有 `diarize.*` / `speaker.embed` 结果必须来自同一个 `vectorSpaceId`"*。
#:
#: 所以判定这两条时都用本元组，差别只在**问的问题不同**：
#:   * L2：这个槽要产向量吗 → 那后端必须**敢声明**自己的空间（`VECTOR_SLOTS`）；
#:   * L5：这一场已经锁了空间吗 → 不一致就换不了（本元组）。
#: 本元组同时要求"敢声明" —— 因为一个声称能分离说话人却不知道自己属于哪个空间的后端，
#: 恰恰是"会认错人"的那种，宁可拒绝也不要用它。
SAME_SOURCE_SLOTS: Tuple[str, ...] = ("diarize.turns",) + VECTOR_SLOTS

#: 唤醒与指令转写属于**本机**（铁律 L3）：后端挂了不该让"说句话"这件事不可用。
LOCAL_ONLY_SLOTS: Tuple[str, ...] = ("wake",)

# ---------------------------------------------------------------- 后端

#: 三类来源。`source` 决定"能不能出机"，是 privacy 约束的判据。
SOURCE_LOCAL = "local"
SOURCE_LAN = "lan"          # ECHO 能力后端（单位内网）
SOURCE_WAN = "wan"          # 内网公共 ASR（其实是"别人的服务"，可能进一步出网）

BACKEND_LOCAL = "local"
BACKEND_ECHO_SERVER = "echo-server"
BACKEND_INTRANET = "intranet"

#: privacy 约束的宽严（`需求.constraints.privacy`）：
#:   none = 不出机（只许本地）｜lan = 允许内网 ｜wan = 允许更远
PRIVACY_ORDER = {"none": 0, "lan": 1, "wan": 2}

# ---------------------------------------------------------------- 降级原因

#: **权威词汇**（`docs/统一路由` §2 / 3.0 总览的裁定表）。
#: 面板、日志、路由的 `skipped` 全用这一套 —— 四域各写一套就没法统一排障。
SKIP_REASONS: Tuple[str, ...] = (
    "absent",           # 不在位（设备拔了 / 后端没配）
    "blocked",          # 被策略禁止（设备黑名单 / privacy 约束 / 凭据不被接受）
    "busy",             # 太忙，这一轮吃不下
    "offline",          # 探测不通
    "circuit-open",     # 熔断冷却中
    "quota",            # 额度用尽 / 被限流
    "unsupported",      # 不支持该需求（内网公共服务被问 diarize）
    "vector-mismatch",  # vectorSpaceId 与本次会议锁定的不一致
    "open-failed",      # 实开失败
    "error",            # 其它失败（带 detail）
)

#: 服务端错误码 → 降级原因（服务端 §6.3 那张表）。
#:
#: **这里有一个真实的不一致，写在明处**：服务端的错误模型坚持要把
#: `client_busy`（自己占着）与 `server_busy`（系统忙）分开，理由写得很清楚 ——
#: "合并成一个码，客户端就只能盲目重试"。但**上面这套权威词汇里没有两档"忙"**，
#: 只有一个 `busy`。同理 `model_loading`（该等）也没有对应词。
#:
#: 本层的做法是：**两个都要，各管一件事**。
#:   * `reason` 只回答"**这一轮要不要跳过这个后端**" → 两档都归 `busy` / `absent`；
#:   * `code` + `retryable` 回答"**客户端该干什么**" → 两档完全不同（见下表）。
#: 信息一点没丢，而且没有擅自扩权威词汇。这个缺口记在
#: `docs/3.0-PROGRESS.md` 的待办里（要么给词汇表加 `loading`/`server-busy`，
#: 要么明确"词汇表只管候选筛选，行为一律看 code"）。
SERVER_CODE_TO_REASON: Dict[str, str] = {
    "bad_request": "error",
    "unauthorized": "blocked",
    "forbidden": "blocked",
    "client_busy": "busy",
    "server_busy": "busy",
    "payload_too_large": "unsupported",
    "audio_too_long": "unsupported",
    "unsupported_media": "unsupported",
    "quota_exceeded": "quota",
    "rate_limited": "quota",
    "queue_full": "busy",
    "model_loading": "absent",
    "model_not_found": "absent",
    "model_failed": "error",
    "gpu_oom": "error",
    "inference_timeout": "error",
    "auth_misconfigured": "error",
}

#: 服务端错误码 → **能不能重试**。与 `SERVER_CODE_TO_REASON` 是两个问题，别合并。
#:
#: 这张表就是服务端那句话的兑现处："`client_busy` 重试永远不会成功，
#: `server_busy` 重试才是对的"。
SERVER_CODE_RETRY: Dict[str, bool] = {
    "client_busy": False,       # 自己上一个还没完 —— 重试只是又撞一次自己
    "server_busy": True,        # 退避后重试
    "queue_full": True,
    "quota_exceeded": False,    # 今天别再试
    "rate_limited": True,       # 等几秒再来
    "model_loading": True,      # 等加载完
    "model_not_found": False,   # 这个后端没有你要的模型
    "model_failed": False,
    "gpu_oom": False,
    "unauthorized": False,      # 先换凭据再说
    "forbidden": False,
    "bad_request": False,
    "payload_too_large": False,  # 该改分段，不是重试
    "audio_too_long": False,
    "unsupported_media": False,
    "inference_timeout": False,
    "auth_misconfigured": False,  # 服务端自己的问题，客户端报给运营
}


# ---------------------------------------------------------------- 错误

class CapabilityError(Exception):
    """一次能力调用的失败。**带分类**，不是一句字符串。

    `reason` 决定"换不换后端"，`retryable` 决定"要不要再试一次" —— 两个问题。
    `code` 是服务端给的原始错误码（本机实现自己造一个同名风格的），
    留在结果里是为了**排障能对上服务端日志**，不是为了再分支一次。
    """

    def __init__(self, reason: str, detail: str = "", *, code: str = "",
                 backend_id: str = "", slot: str = "", retry_after: Optional[int] = None,
                 retryable: Optional[bool] = None, status: int = 0):
        if reason not in SKIP_REASONS:
            raise ValueError("reason 必须是权威词汇之一：%r" % (reason,))
        super().__init__("%s: %s" % (code or reason, detail) if detail else (code or reason))
        self.reason = reason
        self.detail = detail
        self.code = code
        self.backend_id = backend_id
        self.slot = slot
        self.retry_after = retry_after
        self.status = int(status or 0)
        if retryable is None:
            retryable = SERVER_CODE_RETRY.get(code, reason in ("busy", "offline",
                                                               "circuit-open"))
        self.retryable = bool(retryable)

    def __str__(self) -> str:
        bits = [self.reason]
        if self.backend_id:
            bits.append("via %s" % self.backend_id)
        if self.slot:
            bits.append("slot=%s" % self.slot)
        if self.code:
            bits.append("code=%s" % self.code)
        if self.detail:
            bits.append(self.detail)
        return " | ".join(bits)


def error_from_server(payload: Any, status: int, backend_id: str = "",
                      slot: str = "") -> CapabilityError:
    """把服务端的错误体翻成本层的 `CapabilityError`。

    服务端的契约是 `{"code": ..., "message": ..., "detail"?, "retryAfter"?}`（§6.3）。
    **认不出来的 code 一律归 `error`** —— 宁可信息少一点，也不要按猜出来的原因
    做出"换后端/不重试"这类有副作用的决定。
    """
    body = payload if isinstance(payload, dict) else {}
    code = str(body.get("code") or "").strip()
    reason = SERVER_CODE_TO_REASON.get(code, "error")
    detail = str(body.get("detail") or body.get("message") or "").strip()
    if not code:
        # 没有 code：多半是网关/反代回的 HTML，不是我们的服务端
        reason = "offline" if status in (0, 502, 503, 504) else "error"
    retry_after = body.get("retryAfter")
    try:
        retry_after = int(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    return CapabilityError(reason, detail or ("HTTP %s" % status), code=code,
                           backend_id=backend_id, slot=slot, status=status,
                           retry_after=retry_after)


# ---------------------------------------------------------------- 结果

@dataclass(frozen=True)
class Provenance:
    """一个结果**从哪来**。每个结果都带它 —— 这是下游做判断的依据。"""
    backend_id: str = ""
    model_version: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"backendId": self.backend_id, "modelVersion": self.model_version}


@dataclass(frozen=True)
class AsrResult:
    """音频 → 文本。

    `timestamps` 三态，**服务端只会给前两种**：
      * `exact`     —— 模型真的给了句级时间轴
      * `none`      —— 没有
      * `estimated` —— **客户端**拿不到时间戳时的兜底（按字数均摊），
                       而且必须标出来（现在这个信息只写在注释里，面板和导出看不出来）
    """
    text: str
    sentences: Tuple[Tuple[float, float, str], ...] = ()
    timestamps: str = "none"
    provenance: Provenance = field(default_factory=Provenance)
    audio_seconds: float = 0.0
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"text": self.text, "timestamps": self.timestamps,
                               "sentences": [{"start": a, "end": b, "text": t}
                                             for a, b, t in self.sentences],
                               "audioSeconds": self.audio_seconds,
                               "durationMs": self.duration_ms}
        out.update(self.provenance.as_dict())
        return out

    @classmethod
    def from_server(cls, body: Dict[str, Any], backend_id: str) -> "AsrResult":
        sents = tuple((float(s.get("start") or 0.0), float(s.get("end") or 0.0),
                       str(s.get("text") or ""))
                      for s in (body.get("sentences") or []))
        return cls(text=str(body.get("text") or ""), sentences=sents,
                   # 服务端只会说 exact / none；说了别的就当 none（不替它圆场）
                   timestamps="exact" if body.get("timestamps") == "exact" else "none",
                   provenance=Provenance(backend_id, str(body.get("modelVersion") or "")),
                   audio_seconds=float(body.get("audioSeconds") or 0.0),
                   duration_ms=int(body.get("durationMs") or 0))


@dataclass(frozen=True)
class DiarizeResult:
    """音频 → 说话人时间轴 + 每个说话人的嵌入。

    `speaker` 是**本次响应内的局部标签**（`SPEAKER_00`…）——
    "跨段是不是同一个人"要靠 `SpeakerRegistry` 或服务端的同一套聚类，
    不是这个字段能回答的。
    """
    turns: Tuple[Tuple[float, float, str], ...] = ()
    speakers: Dict[str, Tuple[float, ...]] = field(default_factory=dict)
    dim: int = 0
    vector_space_id: str = ""
    provenance: Provenance = field(default_factory=Provenance)
    audio_seconds: float = 0.0
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "turns": [{"start": a, "end": b, "speaker": s} for a, b, s in self.turns],
            "speakers": {k: list(v) for k, v in self.speakers.items()},
            "dim": self.dim, "vectorSpaceId": self.vector_space_id,
            "audioSeconds": self.audio_seconds, "durationMs": self.duration_ms}
        out.update(self.provenance.as_dict())
        return out

    @classmethod
    def from_server(cls, body: Dict[str, Any], backend_id: str) -> "DiarizeResult":
        return cls(
            turns=tuple((float(t.get("start") or 0.0), float(t.get("end") or 0.0),
                         str(t.get("speaker") or ""))
                        for t in (body.get("turns") or [])),
            speakers={str(k): tuple(float(x) for x in (v or []))
                      for k, v in (body.get("speakers") or {}).items()},
            dim=int(body.get("dim") or 0),
            vector_space_id=str(body.get("vectorSpaceId") or ""),
            provenance=Provenance(backend_id, str(body.get("modelVersion") or "")),
            audio_seconds=float(body.get("audioSeconds") or 0.0),
            duration_ms=int(body.get("durationMs") or 0))


@dataclass(frozen=True)
class EmbedResult:
    """音频 → 一个或多个说话人嵌入（现场注册联系人用）。"""
    vectors: Tuple[Tuple[float, ...], ...] = ()
    dim: int = 0
    vector_space_id: str = ""
    provenance: Provenance = field(default_factory=Provenance)
    audio_seconds: float = 0.0
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"embeddings": [list(v) for v in self.vectors],
                               "dim": self.dim, "vectorSpaceId": self.vector_space_id,
                               "audioSeconds": self.audio_seconds,
                               "durationMs": self.duration_ms}
        out.update(self.provenance.as_dict())
        return out

    @classmethod
    def from_server(cls, body: Dict[str, Any], backend_id: str) -> "EmbedResult":
        return cls(
            vectors=tuple(tuple(float(x) for x in (v or []))
                          for v in (body.get("embeddings") or [])),
            dim=int(body.get("dim") or 0),
            vector_space_id=str(body.get("vectorSpaceId") or ""),
            provenance=Provenance(backend_id, str(body.get("modelVersion") or "")),
            audio_seconds=float(body.get("audioSeconds") or 0.0),
            duration_ms=int(body.get("durationMs") or 0))


# ---------------------------------------------------------------- 客户端形状

class CapabilityClient(abc.ABC):
    """一个后端。**只声明它能满足哪些槽**，路由据此派活。

    实现者要保证的三件事（都有契约用例钉住，两个后端跑同一份）：

      * `ready()` **不抛异常**（沿用 `providers/base.py` 的约定）：
        探测失败返回 False/None，绝不把面板或接口带崩；
      * 失败抛 `CapabilityError`（带分类），**不返回空结果冒充成功**；
      * 产出向量的调用必须带 `vector_space_id`，拿不到就抛 `vector-mismatch`
        （宁可失败，也不要给一个"不知道属于哪个空间"的向量 —— 它会被拿去比，
         而比错的后果是**认错人，且不报错**）。

    单条音频的调用一律是**同步阻塞**的（这一层不引入并发模型）；
    要并发由调用方（会议分段流水线）去组织。
    """

    #: 稳定标识，进日志与面板
    backend_id: str = ""
    #: 归属：local / lan / wan —— privacy 约束按它判
    source: str = SOURCE_LOCAL
    #: 这个后端能满足的槽
    provides: frozenset = frozenset()
    #: 这个后端的向量空间（只有产出向量的后端有；拿不到就留空）
    vector_space_id: str = ""

    def ready(self) -> Optional[bool]:
        """True / False / None（判不了）。**不抛异常。**"""
        return True

    def refresh(self, force: bool = False) -> bool:
        """重新问一遍"你能干什么"，写回 `provides` / `vector_space_id`。

        本机后端**不需要**它（能力是常量，看装没装就知道），远程后端需要（要联网问）。
        放进基类而不是留给远程后端自己长，是因为**面板要能对着一排后端说同一句话**：
        "都再问一遍"。没有它就只能在调用处 `getattr(c, "refresh", None)` 试探，
        那样"本机后端到底需不需要刷新"这件事就没人回答了。

        返回"拿到了吗"，**不抛异常**（跟 `ready()` 同一个道理：探测失败是常态）。
        """
        return True

    def describe(self) -> Dict[str, Any]:
        """给面板看的：它是谁、在哪、能干什么、健不健康。"""
        return {"backendId": self.backend_id, "source": self.source,
                "provides": sorted(self.provides),
                "vectorSpaceId": self.vector_space_id,
                "ready": self._safe_ready()}

    def _safe_ready(self) -> Optional[bool]:
        try:
            return self.ready()
        except Exception:
            return None

    def supports(self, slot: str) -> bool:
        return slot in self.provides

    # ---- 按槽派活 ----------------------------------------------------------
    #
    # 只实现它能提供的槽对应的方法；其余保持基类的 NotImplementedError。

    def transcribe(self, wav: str, *, lang: str = "auto", want_timestamps: bool = False,
                   variant: str = "long", **kw) -> AsrResult:
        """`asr.text`（以及 `asr.timestamps`，当 `want_timestamps=True`）。"""
        raise NotImplementedError("%s 不提供 asr.text" % (self.backend_id or type(self).__name__))

    def diarize(self, wav: str, *, max_speakers: Optional[int] = None, **kw) -> DiarizeResult:
        """`diarize.turns` + `diarize.embeddings`。"""
        raise NotImplementedError("%s 不提供 diarize" % (self.backend_id or type(self).__name__))

    def embed(self, wav: str, *, count: int = 1, **kw) -> EmbedResult:
        """`speaker.embed`。"""
        raise NotImplementedError("%s 不提供 speaker.embed" % (self.backend_id or type(self).__name__))

    def close(self) -> None:
        """释放资源（引擎、连接）。**不抛异常**。"""
        return None


def slots_str(slots: Iterable[str]) -> str:
    return ", ".join(sorted(slots))
