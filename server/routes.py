# -*- coding: utf-8 -*-
"""`/v1/*` —— 能力端点。**只认识能力槽，不认识业务概念。**

两条并发闸门（设计 §3.6），**都不排队**：

    | 闸门     | 上限 | 超了返            | 客户端该做的       |
    |----------|------|-------------------|--------------------|
    | 每客户端 | 1    | 409 client_busy   | **不重试**（自己那条还没完） |
    | 服务端   | N    | 503 server_busy   | 退避重试（听 Retry-After）   |

**为什么必须分成两个不同的 code**：`client_busy` 重试**永远不会成功**，
`server_busy` 重试才是对的。合并成一个 429/503，客户端就只能盲目重试。

闸门在**收完音频、准备推理之前**才过：接收与解码不占通道（§3.6），
否则一个 20 MB 上传会把通道占几十秒，而真正稀缺的是 GPU。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from server import __version__, audio as audio_mod, errors, tmp
from server.pool import EnginePool

router = APIRouter(prefix="/v1")


# ---------------------------------------------------------------- 并发闸门

class Admission:
    """两级闸门。**不排队** —— 拿不到就立刻按类型拒。"""

    def __init__(self, max_concurrent: int, per_client: int, retry_after: int = 5):
        self.max_concurrent = max(1, int(max_concurrent))
        self.per_client = max(1, int(per_client))
        self.retry_after = int(retry_after)
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self._lock = threading.Lock()
        self._per: dict = {}
        self._active = 0

    @contextmanager
    def hold(self, client_id: str):
        with self._lock:
            self._check_locked(client_id)
            self._per[client_id] = self._per.get(client_id, 0) + 1
        if not self._sem.acquire(blocking=False):
            with self._lock:
                self._per[client_id] = max(0, self._per.get(client_id, 1) - 1)
            raise errors.server_busy(self.retry_after)
        with self._lock:
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
                self._per[client_id] = max(0, self._per.get(client_id, 1) - 1)
            self._sem.release()

    def _check_locked(self, client_id: str) -> None:
        """**只判、不占**。必须在持 `_lock` 时调用。

        用自己维护的 `_active` 而不是去读 `BoundedSemaphore._value` ——
        后者是 CPython 的私有实现细节，不该被我们依赖。
        `_active` 在拿到信号量之后、释放信号量之前于锁内加减，
        所以在任何一个静止点上它就是"此刻占着几个通道"。
        """
        if self._per.get(client_id, 0) >= self.per_client:
            # 先查客户端自己：这条**重试没用**，所以给 409 而不是 503
            raise errors.client_busy()
        if self._active >= self.max_concurrent:
            raise errors.server_busy(self.retry_after)

    def precheck(self, client_id: str) -> None:
        """收音频**之前**的便宜预检：满了就立刻拒，一个字节都不读。

        为什么要有它：真正的槽位（`hold`）只该在**推理**前后持有 —— 通道是给 GPU 的，
        不是给网络的，让一个慢上传占着通道是错的。但这样一来，"忙"的判定就发生在
        **上传完成之后**，于是一个客户端能同时开很多条上传、**每条最多 64 MB 先落盘**，
        然后才被 `409` 顶回来：`tmp` 的容量上限能兜住盘，但那是一次真实的写放大，
        而且与"满了立刻告诉客户端系统忙、重试由客户端负责"的原意不符。

        所以这里做一次**无副作用的预检**：提前把"系统忙"说出去。
        它是**尽力而为**的 —— 预检通过之后 `hold` 仍可能失败（别人刚抢了通道），
        那条路径照旧按 `client_busy` / `server_busy` 拒。**权威判定始终在 `hold`。**
        """
        with self._lock:
            self._check_locked(client_id)

    def snapshot(self) -> dict:
        with self._lock:
            return {"active": self._active, "maxConcurrent": self.max_concurrent,
                    "perClientConcurrent": self.per_client}


# ---------------------------------------------------------------- 应用状态

class State:
    """进程内共享状态。由 `main.create_app` 在 lifespan 里装好。"""

    def __init__(self, cfg, pool: EnginePool, sweeper: Optional[tmp.Sweeper] = None,
                 auth=None):
        self.cfg = cfg
        self.pool = pool
        self.sweeper = sweeper
        self.started = time.time()
        # 鉴权对象由 main 装（它要拿 store/config）。None → `client_of` 按"没鉴权"放行。
        self.auth = auth
        self.admission = Admission(
            max_concurrent=cfg.max_concurrent,
            per_client=cfg.per_client_concurrent,
            retry_after=int(cfg.get("limits.busy_retry_after_s", 5)),
        )


def _st(request: Request) -> State:
    return request.app.state.echo


# ---------------------------------------------------------------- 鉴权

#: 端点 → 它需要的 scope（设计 §7.2：`asr | diarize | embed | tts`）。
#: 放在路由这一层，而不是让每个端点在函数体里各写一遍 ——
#: "哪些端点属于哪个权限"是**一张表**，不是散落在各处的字符串。
ENDPOINT_SCOPES = {
    "/v1/asr": "asr",
    "/v1/diarize": "diarize",
    "/v1/speaker/embed": "embed",
}


def client_of(request: Request, need_scope: str = "") -> dict:
    """这个请求是谁（+ 有没有这项权限）。返回客户端行，失败抛 401/403。

    真正的判断都在 `server/auth.py`（内存缓存 + `token_version` 比对，不每请求查库）；
    这里只负责把它接到 FastAPI 的 `Request` 上。

    鉴权关着时（默认，便于本机起步）所有请求都算 `anonymous` ——
    此时"每客户端 1 条通道"就退化成"全局 1 条"，这正是本机单用户时的正确语义。
    """
    st = _st(request)
    auth = getattr(st, "auth", None)
    if auth is None:
        # 理论上不会发生（main 里一定装）。留一条**诚实**的退路：
        # 宁可按"没鉴权"放行并让它显形，也不要在这里凭空造一个 401 出来 ——
        # 那会让"服务端起不来"表现成"所有凭据都不对"。
        return {"client_id": "anonymous", "scopes": "", "token_version": 1, "disabled": 0}
    return auth.authenticate(request.headers.get("authorization") or "", need_scope=need_scope)


def client_id_of(request: Request, need_scope: str = "") -> str:
    return str(client_of(request, need_scope=need_scope).get("client_id") or "anonymous")


# ---------------------------------------------------------------- 配对与令牌

@router.post("/pair")
def pair(request: Request, payload: Optional[dict] = None):
    """用一次性配对码换 `client_id` + `secret`（设计 §7.4）。**唯一的免凭据端点。**

    免凭据 == 必须**额外防猜**：失败计数 + 退避（`PairThrottle`），
    而且管理员可以整个关掉（`auth.pairing_enabled: false`，关掉后新机器进不来）。
    """
    st = _st(request)
    auth = st.auth
    if not bool(st.cfg.get("auth.pairing_enabled", True)):
        raise errors.forbidden("服务端已关闭配对")
    source = _source_of(request)
    auth.throttle.check(source)
    body = payload or {}
    try:
        out = auth.redeem(str(body.get("code") or ""),
                          client_name=str(body.get("clientName") or ""))
    except errors.EchoError:
        auth.throttle.failed(source)
        raise
    auth.throttle.succeeded(source)
    return out


@router.post("/token")
def token(request: Request):
    """用 `client_id:secret` 换短期 JWT（设计 §7.5 ②）。

    **不做 refresh token**：secret 本来就在客户端本地，多一层只是把同一个东西存两份。
    """
    st = _st(request)
    return st.auth.token_for(request.headers.get("authorization") or "")


def _source_of(request: Request) -> str:
    """限速的"来源"。优先 `X-Forwarded-For` 的第一段（反代后面真正的那台）。"""
    fwd = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "unknown")


# ---------------------------------------------------------------- 查询端点

@router.get("/capabilities")
def capabilities(request: Request):
    """能力 + 模型 + 版本 + **此刻的真实可用性**。

    客户端拿它做三件事：生成"服务端能力"选择器、判断"是等还是换后端"、
    以及最要紧的 —— 按 `vectorSpaceId` 决定两批向量能不能互相比对。
    """
    st = _st(request)
    cfg = st.cfg
    models = st.pool.status()
    # 槽 → 模型 id。**从池里取，不在这里重新汇总** —— 池的 `_by_slot` 同时是
    # `pick_for_slot` 用的那一份，所以"宣告的"与"路由得到的"在构造上就是同一个东西。
    # （曾经这里按 `[slot] + supports` 自己算，而池只按 `slot` 建索引，
    #   结果宣告了 asr.timestamps 却路由不过去。顺序也由池定：第一个就是会选中的那个。）
    slots = st.pool.slots()
    return {
        "protocol": 1,
        "server": {"id": cfg.get("server.id", ""), "version": __version__},
        "limits": {
            "maxAudioSeconds": float(cfg.get("limits.max_audio_seconds", 1800)),
            "maxUploadBytes": int(cfg.get("limits.max_upload_bytes", 0)),
            "maxConcurrent": cfg.max_concurrent,
            "perClientConcurrent": cfg.per_client_concurrent,
            "queueMax": int(cfg.get("limits.queue_max", 0)),
        },
        "busy": st.admission.snapshot(),
        "slots": slots,
        "models": models,
        "vram": st.pool.vram(),
    }


@router.get("/health")
def health(request: Request):
    """**存活**探针：几乎不失败；挂了才失败。

    顺便暴露运行期数字 —— 尤其临时目录那三个（**不归零就是泄漏的信号**）。
    """
    st = _st(request)
    return {
        "ok": True,
        "version": __version__,
        "uptimeSeconds": int(time.time() - st.started),
        "busy": st.admission.snapshot(),
        "vram": st.pool.vram(),
        "tmp": tmp.stats(st.cfg.tmp_root),
        # 上一次定时清理的结果（含"删失败"的计数 —— 那个不为 0 也要有人看见）
        "tmpSweep": (st.sweeper.last.as_dict()
                     if getattr(st.sweeper, "last", None) is not None else None),
        "models": {m["id"]: m["state"] for m in st.pool.status()},
    }


@router.get("/ready")
def ready(request: Request):
    """**就绪**探针：模型池可服务才算就绪。

    与 `/health` 分开是必须的 —— 合在一起的话，"模型正在加载"会被编排系统
    判成"服务挂了"而反复重启，于是**永远起不来**。
    """
    st = _st(request)
    models = st.pool.status()
    bad = [m["id"] for m in models if m["state"] == "failed"]
    if bad:
        return JSONResponse(status_code=503,
                            content={"ok": False, "code": "model_failed", "failed": bad})
    return {"ok": True, "models": len(models), "busy": st.admission.snapshot()}


# ---------------------------------------------------------------- 能力端点

#: **为什么解码与推理必须下线程池（`run_in_threadpool`），而不是直接在这儿调。**
#
# 这三个端点是 `async def`（因为要 `await request.stream()` 收 body），
# 而 FastAPI 把 `async def` 处理函数**跑在事件循环上**。于是里面任何一段阻塞调用
# （`soundfile` 解码、`soxr` 重采样、`funasr`/`pyannote` 推理）都会**把整个事件循环按住**，
# 结果是：**所有请求被一条一条地串行处理**，`limits.max_concurrent: 2` 变成一句空话，
# 两级闸门也永远不会看到"两个请求同时在跑"。
#
# 这不是理论风险，是实测出来的（2026-09-24）：两个"并发"请求各花 `t` 与 `2t`，
# 墙钟 ≈ `2t` —— 完全串行。而当时的用例全是绿的，因为 `TestClient` 本来就把
# 同一客户端的请求串起来发，**它根本测不出这件事**（同一个盲点之前已经让
# "并发闸门测试空转"过一次）。现在由 `EventLoopNotBlockedTests` 用
# `httpx.ASGITransport` + `asyncio.gather` 钉住：两个请求的墙钟必须**明显小于**两条之和。
#
# 顺带一个后果：串行时 `/v1/health` 还能答（它是同步端点，走线程池），
# 所以"服务看着是活的"，但能力端点其实在排队 —— 这类问题靠看日志很难发现。


async def _reject(request: Request, exc) -> None:
    """**先把请求体抽干，再抛。**

    不这么做的话，错误码会凭空消失：服务端没读完 body 就回响应并关连接，
    未读数据让对端收到 RST，而 RST 会丢掉对端接收缓冲里的响应体。
    实测 0.9 MB 的 body + `?model=nope` → 404 但 body 为空（2 KB 时正常）——
    而且是竞态，偶尔才复现。完整说明见 `audio.drain`。
    """
    await audio_mod.drain(request)
    raise exc


def _finish_asr(engine, wav: str, lang: str, timestamps: bool, model_id: str,
                spec, seconds: float, t0: float) -> dict:
    out = engine.transcribe(wav, lang=lang, timestamps=timestamps)
    text = str(out.get("text") or "")
    sentences = out.get("sentences") or []
    return {
        "text": text,
        "sentences": sentences,
        # 只有 exact / none —— **服务端不编 estimated**（"按字数均摊"是客户端的兜底）
        "timestamps": "exact" if sentences else "none",
        "modelId": model_id,
        "modelVersion": spec.model_version,
        "audioSeconds": round(seconds, 2),
        "durationMs": int((time.time() - t0) * 1000),
    }


@router.post("/asr")
async def asr(request: Request, variant: str = "long", timestamps: int = 0,
              lang: str = "auto", model: str = ""):
    """音频 → 文本（可选句级时间轴）。

    `variant`：`short`（几秒音频，快）/ `long`（长音频，准）。两个都是**质量与延迟**的
    说法，不含任何业务含义 —— 服务端不认识"会议""指令"这些概念。
    v1 里两者常常落到**同一个**模型上（长档那个声明了 `supports: [asr.text]`），
    因为出厂清单不再放常驻的小模型（见 `engines.default_specs` 的说明）。
    """
    st = _st(request)
    cfg = st.cfg
    want_ts = bool(int(timestamps or 0))

    # `variant` → 槽。**注意短档的槽叫 `asr.text` 而不是 `asr.short`** ——
    # 曾经这里写成 `asr.short`，而那是个**没有任何模型提供的槽**：
    # `variant=short` 一律 404，而默认的 `variant=long` 把测试全带过去了。
    # 现在由 `test_every_advertised_slot_is_routable` 盯着这类幽灵槽。
    slot = "asr.text" if str(variant).lower() in ("short", "fast") else "asr.long"
    # 这一整段都在**读 body 之前**（鉴权 / 挑模型 / 判忙 / 查大小）。
    # 任何一条拒了都必须**先把 body 抽干再抛**（见 `_reject`），
    # 否则客户端拿到的是"空 body 的错误"，它的降级逻辑就瞎了。
    try:
        cid = client_id_of(request, need_scope="asr")
        model_id = st.pool.pick_for_slot(slot, model)
        spec = st.pool.spec(model_id)
        # **先判忙，再读 body**（同样是"先挑模型、再动字节"的顺序）：
        # 忙的时候一个字节都不收，见 `Admission.precheck`。
        st.admission.precheck(cid)
        audio_mod.check_declared_size(request, cfg)
    except errors.EchoError as e:
        if e.code == "payload_too_large":
            raise            # 声报超大：**故意不抽干**（可能真是 999 MB），只回 413
        await _reject(request, e)
    with tmp.TempWorkspace(cfg.tmp_root) as ws:
        src, ctype = await audio_mod.receive(request, ws, cfg)
        wav = ws.path("seg.wav")
        # 解码 / 重采样也是 CPU 活：同样要下线程池（见 `_infer` 上方的说明）
        seconds = await run_in_threadpool(audio_mod.to_wav16k, src, wav, ctype, cfg)

        def _infer():
            t0 = time.time()
            with st.admission.hold(cid):
                with st.pool.acquire(model_id) as engine:
                    return _finish_asr(engine, wav, lang, want_ts, model_id, spec,
                                       seconds, t0)

        return await run_in_threadpool(_infer)


@router.post("/diarize")
async def diarize(request: Request, mode: str = "segment", maxSpeakers: int = 0,
                  model: str = ""):
    """音频 → 说话人时间轴 + 嵌入。

    返回里的 `speaker` 是**局部标签**（`S0`/`S1`…），只在本次响应内有意义 ——
    "跨段是不是同一个人"是**客户端**的判断（它才有整场会议的上下文）。

    `mode=turns`（逐 turn 嵌入）**v1 未实现**：它要给每个 turn 单独跑一次嵌入模型，
    与"段内聚类"是两条路径；先只做 `segment`，避免给出名不副实的东西。
    """
    st = _st(request)
    cfg = st.cfg
    try:
        cid = client_id_of(request, need_scope="diarize")
        if str(mode) != "segment":
            raise errors.bad_request("mode=%r 尚未实现（v1 只支持 segment）" % mode)
        model_id = st.pool.pick_for_slot("diarize.turns", model)
        spec = st.pool.spec(model_id)
        st.admission.precheck(cid)             # 先判忙，再读 body
        audio_mod.check_declared_size(request, cfg)
    except errors.EchoError as e:
        if e.code == "payload_too_large":
            raise
        await _reject(request, e)
    with tmp.TempWorkspace(cfg.tmp_root) as ws:
        src, ctype = await audio_mod.receive(request, ws, cfg)
        wav = ws.path("seg.wav")
        seconds = await run_in_threadpool(audio_mod.to_wav16k, src, wav, ctype, cfg)

        def _infer():
            t0 = time.time()
            with st.admission.hold(cid):
                with st.pool.acquire(model_id) as engine:
                    return engine.analyze(wav, max_speakers=int(maxSpeakers) or None), t0

        (turns, embs, labels), t0 = await run_in_threadpool(_infer)
    speakers = {}
    for i, lab in enumerate(labels or []):
        if i < len(embs):
            speakers[str(lab)] = [round(float(x), 6) for x in embs[i]]
    return {
        "modelId": model_id,
        "modelVersion": spec.model_version,
        # 客户端比较两批嵌入的**唯一**依据（不是模型名）
        "vectorSpaceId": spec.vector_space_id,
        "dim": int(spec.dim or 0),
        "turns": [{"start": round(float(a), 3), "end": round(float(b), 3), "speaker": str(s)}
                  for a, b, s in (turns or [])],
        "speakers": speakers,
        "audioSeconds": round(seconds, 2),
        "durationMs": int((time.time() - t0) * 1000),
    }


@router.post("/speaker/embed")
async def speaker_embed(request: Request, count: int = 1, model: str = ""):
    """音频 → 说话人嵌入（现场注册联系人用）。

    服务端**只提嵌入，不判断"这是谁"** —— 匹配、阈值、歧义间隔、库管理全在客户端
    （库是敏感个人信息，而且阈值是每个客户自己的调参）。
    """
    st = _st(request)
    cfg = st.cfg
    try:
        cid = client_id_of(request, need_scope="embed")
        model_id = st.pool.pick_for_slot("speaker.embed", model)
        spec = st.pool.spec(model_id)
        st.admission.precheck(cid)             # 先判忙，再读 body
        audio_mod.check_declared_size(request, cfg)
    except errors.EchoError as e:
        if e.code == "payload_too_large":
            raise
        await _reject(request, e)
    with tmp.TempWorkspace(cfg.tmp_root) as ws:
        src, ctype = await audio_mod.receive(request, ws, cfg)
        wav = ws.path("seg.wav")
        seconds = await run_in_threadpool(audio_mod.to_wav16k, src, wav, ctype, cfg)

        def _infer():
            t0 = time.time()
            with st.admission.hold(cid):
                with st.pool.acquire(model_id) as engine:
                    return engine.embed(wav), t0

        (vecs, _labels), t0 = await run_in_threadpool(_infer)
    want = max(1, int(count or 1))
    return {
        "embeddings": [[round(float(x), 6) for x in v] for v in (vecs or [])[:want]],
        "dim": int(spec.dim or 0),
        "vectorSpaceId": spec.vector_space_id,
        "modelVersion": spec.model_version,
        "audioSeconds": round(seconds, 2),
        "durationMs": int((time.time() - t0) * 1000),
    }
