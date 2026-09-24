# -*- coding: utf-8 -*-
"""收音频：流式落进工作区 → 规格化成 16 kHz 单声道 wav。

三个刻意的选择（设计 §5.4 / §6.5）：

  1. **raw body，不用 multipart** —— multipart 会把大文件交给框架 spool 到磁盘
     （实测 `spool_max_size` 默认 1 MB，一个 10 分钟分段 19 MB 必然落盘，
     而且那个文件**由框架创建、我们的 `finally` 看不到它**）。
  2. **先看 `Content-Length` 再读** —— 超限直接拒，一个字节都不收。
  3. **接收不占并发通道**（并发闸门在路由里、推理前才过）—— 否则一个 20 MB 的上传
     会把通道占几十秒，而真正稀缺的是 GPU。代价是"可能传完才发现被拒"，
     所以客户端侧有乐观预检（读 `/v1/capabilities` 的 `activeRequests`）。
"""
from __future__ import annotations

from typing import Tuple

from server import errors

#: 按容器格式读的（交给 libsndfile）；其余按裸 PCM 处理
_CONTAINER_TYPES = ("audio/wav", "audio/x-wav", "audio/wave", "audio/flac",
                    "audio/ogg", "audio/opus", "audio/mpeg")

#: 裸 PCM 的约定：**16 kHz 单声道 16-bit 小端**（与客户端录音一致）
_RAW_TYPES = ("application/octet-stream", "audio/pcm", "audio/l16", "")

SAMPLE_RATE = 16000


def _content_type(request) -> str:
    return str(request.headers.get("content-type") or "").split(";")[0].strip().lower()


def _is_container(ctype: str) -> bool:
    return ctype in _CONTAINER_TYPES


def check_declared_size(request, cfg) -> None:
    """在读**任何**字节之前先按声明的长度拒掉超限的。"""
    limit = int(cfg.get("limits.max_upload_bytes", 64 * 1024 * 1024))
    raw = str(request.headers.get("content-length") or "").strip()
    if raw.isdigit() and int(raw) > limit:
        raise errors.payload_too_large(limit)


async def receive(request, ws, cfg) -> Tuple[str, str]:
    """把请求体流式写进工作区。返回 `(文件路径, 内容类型)`。

    边写边数字节：没有 `Content-Length`（chunked）时靠这一步兜住。
    """
    limit = int(cfg.get("limits.max_upload_bytes", 64 * 1024 * 1024))
    ctype = _content_type(request)
    if not _is_container(ctype) and ctype not in _RAW_TYPES:
        raise errors.unsupported_media("不认识的 Content-Type: %r" % ctype)
    dst = ws.path("upload.bin")
    total = 0
    with open(dst, "wb") as fh:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                # 到这里已经收了一部分：不要再写，直接拒（工作区由调用方的 with 收尾）
                raise errors.payload_too_large(limit)
            fh.write(chunk)
    if total == 0:
        raise errors.bad_request("请求体是空的")
    return dst, ctype


async def drain(request, cap: int = 0) -> int:
    """把**还没读**的请求体抽干（有上限），返回丢掉多少字节。

    ## 为什么需要它 —— 错误码会凭空消失

    HTTP/1.1 下，服务端**没读完请求体**就回响应并关连接时，socket 里剩下的未读数据
    会让对端收到 **RST**，而 RST 会**丢弃对端接收缓冲里已经到达的响应体**。
    客户端于是只拿到状态码、body 是空的。

    实测（2026-09-24，0.9 MB 的 body，`POST /v1/asr?model=nope`）：

        body 2 KB    → 404 且 body 正常带 code
        body 0.9 MB  → 404 但 body **为空**

    而且**它是竞态**：同一次跑里，`?model=nope` 丢了 body、`?mode=turns` 没丢 ——
    所以偶尔能过，非常容易被漏掉。`/v1/asr` 的大上传恰好是最需要错误码的场合
    （`audio_too_long` / `payload_too_large` / `client_busy` / `model_not_found`），
    而客户端整套降级逻辑就是按 `code` 分支的（设计 §6.3）。**丢了 code，
    客户端就只能盲目重试** —— 那正是错误模型存在的意义。

    ## 顺序很重要：**先抽干，再抛**

    不能"先回响应再抽"：那时连接已经要关了，RST 照样发生。
    所以调用方要在 `raise` 之前 `await drain(...)`（见 `routes._drain_then_raise`）。

    ## 上限

    默认取 `limits.max_upload_bytes`。**声报超大（想撑死我们）的请求不会被抽干** ——
    那种情况下我们只回 `413` 就关，客户端拿不到 body 也没关系：
    "413 = 太大了"这个结论光看状态码就够，不需要 code 去区分。
    真正必须保住 code 的，是那些**体量正常但业务上被拒**的请求。
    """
    if not cap:
        try:
            cap = int(request.app.state.echo.cfg.get("limits.max_upload_bytes",
                                                     64 * 1024 * 1024))
        except Exception:
            cap = 64 * 1024 * 1024
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk or b"")
            if total > cap:
                break                      # 超出预期就不奉陪了，直接交给调用方去关
    except Exception:
        pass                                # 抽不动就算了 —— 不能让"发个错误"变成新的错误
    return total


def to_wav16k(src: str, dst: str, ctype: str, cfg) -> float:
    """规格化成 16 kHz 单声道 wav，返回时长（秒）。超长抛 `audio_too_long`。"""
    import numpy as np
    import soundfile as sf
    import soxr

    try:
        if _is_container(ctype):
            data, sr = sf.read(src, dtype="float32", always_2d=True)
            if data.size == 0:
                raise errors.bad_request("音频是空的")
            if data.shape[1] > 1:                     # 多声道 → 下混
                data = data.mean(axis=1, keepdims=True)
            mono = data[:, 0]
        else:
            # 裸 PCM：按 16 kHz 单声道 16-bit 小端解释（客户端的约定）
            raw = np.fromfile(src, dtype="<i2")
            if raw.size == 0:
                raise errors.bad_request("音频是空的")
            mono = raw.astype("float32") / 32768.0
            sr = SAMPLE_RATE
    except errors.EchoError:
        raise
    except Exception as e:                            # noqa: BLE001 —— libsndfile 什么错都可能
        raise errors.unsupported_media("%s: %s" % (type(e).__name__, e))

    if sr != SAMPLE_RATE and sr > 0:
        mono = soxr.resample(mono, int(sr), SAMPLE_RATE)

    seconds = float(len(mono)) / float(SAMPLE_RATE)
    limit = float(cfg.get("limits.max_audio_seconds", 1800))
    if seconds > limit:
        raise errors.audio_too_long(limit)

    sf.write(dst, mono, SAMPLE_RATE, subtype="PCM_16")
    return seconds
