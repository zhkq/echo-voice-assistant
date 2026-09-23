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
            if self._per.get(client_id, 0) >= self.per_client:
                # 先查客户端自己：这条**重试没用**，所以给 409 而不是 503
                raise errors.client_busy()
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

    def snapshot(self) -> dict:
        with self._lock:
            return {"active": self._active, "maxConcurrent": self.max_concurrent,
                    "perClientConcurrent": self.per_client}


# ---------------------------------------------------------------- 应用状态

class State:
    """进程内共享状态。由 `main.create_app` 在 lifespan 里装好。"""

    def __init__(self, cfg, pool: EnginePool, sweeper: Optional[tmp.Sweeper] = None):
        self.cfg = cfg
        self.pool = pool
        self.sweeper = sweeper
        self.started = time.time()
        self.admission = Admission(
            max_concurrent=cfg.max_concurrent,
            per_client=cfg.per_client_concurrent,
            retry_after=int(cfg.get("limits.busy_retry_after_s", 5)),
        )


def _st(request: Request) -> State:
    return request.app.state.echo


# ---------------------------------------------------------------- 鉴权

def client_of(request: Request) -> str:
    """返回 client_id。**v1 骨架**：配置里配了几把 token 就认几把。

    配对码 → `client_id` + secret → 短期 JWT 是下一步（设计 §7.4/§7.5）；
    现在这一层已经把"每个请求是谁"这件事定下来，配额与审计都能挂上去。
    鉴权关闭时（默认，便于本机起步）返回 `"anonymous"`。
    """
    st = _st(request)
    cfg = st.cfg
    if not bool(cfg.get("auth.enabled", False)):
        return "anonymous"
    raw = str(request.headers.get("authorization") or "")
    token = raw[7:].strip() if raw.startswith("Bearer ") else ""
    if not token:
        raise errors.unauthorized("缺少 Bearer 令牌")
    for row in (cfg.get("auth.tokens") or []):
        if str(row.get("token") or "") == token:
            return str(row.get("client_id") or "unknown")
    raise errors.unauthorized("令牌无效")


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
    # 槽 → 模型 id，客户端据此路由（一个模型可能满足多个槽）
    slots: dict = {}
    for m in models:
        for s in [m["slot"]] + list(m.get("supports") or []):
            if s:
                slots.setdefault(s, []).append(m["id"])
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

    `variant`：`short`（快、常驻）/ `long`（准、按需）。两个都是**质量与延迟**的说法，
    不含任何业务含义 —— 服务端不认识"会议""指令"这些概念。
    """
    st = _st(request)
    cfg = st.cfg
    cid = client_of(request)
    want_ts = bool(int(timestamps or 0))

    slot = "asr.short" if str(variant).lower() in ("short", "fast") else "asr.long"
    model_id = st.pool.pick_for_slot(slot, model)
    spec = st.pool.spec(model_id)

    audio_mod.check_declared_size(request, cfg)
    with tmp.TempWorkspace(cfg.tmp_root) as ws:
        src, ctype = await audio_mod.receive(request, ws, cfg)
        wav = ws.path("seg.wav")
        seconds = audio_mod.to_wav16k(src, wav, ctype, cfg)
        t0 = time.time()
        with st.admission.hold(cid):
            with st.pool.acquire(model_id) as engine:
                return _finish_asr(engine, wav, lang, want_ts, model_id, spec, seconds, t0)


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
    cid = client_of(request)
    if str(mode) != "segment":
        raise errors.bad_request("mode=%r 尚未实现（v1 只支持 segment）" % mode)

    model_id = st.pool.pick_for_slot("diarize.turns", model)
    spec = st.pool.spec(model_id)
    audio_mod.check_declared_size(request, cfg)
    with tmp.TempWorkspace(cfg.tmp_root) as ws:
        src, ctype = await audio_mod.receive(request, ws, cfg)
        wav = ws.path("seg.wav")
        seconds = audio_mod.to_wav16k(src, wav, ctype, cfg)
        t0 = time.time()
        with st.admission.hold(cid):
            with st.pool.acquire(model_id) as engine:
                turns, embs, labels = engine.analyze(
                    wav, max_speakers=int(maxSpeakers) or None)
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
    cid = client_of(request)
    model_id = st.pool.pick_for_slot("speaker.embed", model)
    spec = st.pool.spec(model_id)
    audio_mod.check_declared_size(request, cfg)
    with tmp.TempWorkspace(cfg.tmp_root) as ws:
        src, ctype = await audio_mod.receive(request, ws, cfg)
        wav = ws.path("seg.wav")
        seconds = audio_mod.to_wav16k(src, wav, ctype, cfg)
        t0 = time.time()
        with st.admission.hold(cid):
            with st.pool.acquire(model_id) as engine:
                vecs, _labels = engine.embed(wav)
    want = max(1, int(count or 1))
    return {
        "embeddings": [[round(float(x), 6) for x in v] for v in (vecs or [])[:want]],
        "dim": int(spec.dim or 0),
        "vectorSpaceId": spec.vector_space_id,
        "modelVersion": spec.model_version,
        "audioSeconds": round(seconds, 2),
        "durationMs": int((time.time() - t0) * 1000),
    }
