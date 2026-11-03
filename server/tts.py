# -*- coding: utf-8 -*-
"""能力后端的 TTS 通道（edge-tts）与**独立**限速。

为什么单独一个模块，而不是塞进 `routes.py`：

* TTS 是**网络 IO**，不占 GPU / `EnginePool` / ASR 的 `Admission`；
* edge-tts 的版本只应该在**后端**升级一次，手机和 ECHO 共用；
* 每个 client 1 QPS 是 TTS 自己的配额，和 ASR 的“每客户端并发 1”**不是同一件事** ——
  混进 `Admission` 会让一句 TTS 把一条转写通道占掉，或者被转写的 `409` 顶回来。

## 1 QPS 的口径

* 按**认证后的 `client_id`** 单独计数；
* 同一 client 两次请求间隔 < 1 秒 → `429 rate_limited` + `Retry-After: 1`；
* 不排队；没有跨 client 的全局 QPS 上限（手机和 ECHO 是两条独立计数）；
* 鉴权关闭时所有请求都是 `anonymous`，于是会共用同一个 1 QPS —— 这是**预期**，
  要让“手机/ECHO 分开算”，必须在后端开鉴权并给它们各自发 client。

## edge-tts 的边界

`edge-tts` 调的是微软的在线语音接口，不是 ECHO 自己的模型。它能用多久、有没有限流，
由微软决定；服务端只能保证“不因为 ECHO 自己的闸门把请求卡住”，不能保证上游无限。
上游失败 → `502 tts_failed`，客户端按自己的回退策略处理（手机回退系统 TTS）。
"""
from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from typing import Callable, Dict, Optional

from server import errors

#: edge-tts 的默认中文女声。要换音色由调用方或后端配置传 `voice`。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

#: edge-tts 的语速；"+0%" 是原速。
DEFAULT_RATE = "+0%"

#: 文本长度上限的兜底（路由层会以配置 `tts.max_chars` 为准）。
DEFAULT_MAX_CHARS = 2000

#: 单次合成的默认超时（秒）。
DEFAULT_TIMEOUT_S = 15.0

#: 每个客户端两次 TTS 请求之间的最小间隔（秒）。
DEFAULT_PER_SECOND = 1.0

#: edge voice 名字的形状（如 `zh-CN-XiaoxiaoNeural`）。
_VOICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
#: edge rate 的形状（如 `+0%` / `-10%`）。
_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")


class TtsError(RuntimeError):
    """edge-tts 合成失败（路由映射成 502）。"""


class TtsInputError(ValueError):
    """调用方给的 text/voice/rate 不合法（路由映射成 422）。"""


class TtsDisabled(RuntimeError):
    """后端配置里明确关闭了 TTS（路由映射成 503）。"""


class TtsUnavailable(TtsError):
    """edge-tts 依赖缺失/后端没装（路由映射成 503，而不是 502）。"""


def validate_text(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """校验并返回 strip 后的文本；空/超长抛 `TtsInputError`。"""
    value = str(text or "").strip()
    if not value:
        raise TtsInputError("text 不能为空")
    if len(value) > int(max_chars):
        raise TtsInputError("text 太长（上限 %d 字）" % int(max_chars))
    return value


def validate_voice(voice: str) -> str:
    """校验 edge voice 名；空串 = 用默认音色。"""
    value = str(voice or "").strip() or DEFAULT_VOICE
    if not _VOICE_RE.match(value):
        raise TtsInputError("voice 格式不对（示例：zh-CN-XiaoxiaoNeural）")
    return value


def validate_rate(rate: str) -> str:
    """校验 edge 语速；空串 = 原速。"""
    value = str(rate or "").strip() or DEFAULT_RATE
    if not _RATE_RE.match(value):
        raise TtsInputError("rate 格式不对（示例：+0% / -10%）")
    return value


async def synthesize(text: str, *, voice: str = "", rate: str = "",
                     timeout: float = DEFAULT_TIMEOUT_S) -> bytes:
    """调用 edge-tts 合成一句话，返回 MP3 字节。

    **不落临时文件**：edge 的 stream 直接给音频块，收集完一次性返回。
    这样容器只读根文件系统也不会因为临时文件失败。
    """
    try:
        import edge_tts  # 延迟 import：没装包时不让整个服务起不来
    except Exception as exc:                                  # pragma: no cover - 环境依赖
        raise TtsUnavailable("服务端未安装 edge-tts：%s" % exc) from exc

    voice = validate_voice(voice)
    rate = validate_rate(rate)
    parts = []

    async def _collect() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                parts.append(chunk["data"])

    try:
        await asyncio.wait_for(_collect(), timeout=max(1.0, float(timeout)))
    except asyncio.TimeoutError as exc:
        raise TtsError("edge-tts 合成超时（%.0f 秒）" % float(timeout)) from exc
    except TtsError:
        raise
    except Exception as exc:
        raise TtsError("edge-tts 合成失败：%s: %s"
                       % (type(exc).__name__, exc)) from exc

    data = b"".join(parts)
    if not data:
        raise TtsError("edge-tts 返回空音频")
    return data


class TtsRateLimiter:
    """按 client_id 的严格 1 QPS（**不是** ASR 的并发闸门）。

    * 第一次请求立即放行；
    * 一次成功放行后，同一 client 在 `1 / per_second` 秒内再来 → 429；
    * `per_second <= 0` 表示关闭这道 TTS 限速（运维保险，不用于默认）；
    * 不排队，不做跨 client 的总量限制。

    `clock` 可注入，便于用纯 JVM/单测钉住时间语义；生产默认 `time.monotonic`。
    """

    def __init__(self, per_second: float = DEFAULT_PER_SECOND,
                 clock: Optional[Callable[[], float]] = None,
                 max_clients: int = 4096) -> None:
        self.per_second = float(per_second or 0.0)
        self._clock = clock or time.monotonic
        self._max_clients = max(16, int(max_clients))
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        """客户端很多时清掉很久没来的记录；不清会让字典无界增长。"""
        if len(self._last) <= self._max_clients:
            return
        cutoff = now - 600.0
        for key, ts in list(self._last.items()):
            if ts < cutoff:
                self._last.pop(key, None)
        # 还是太多：按最旧的时间删到上限（防止一次突发把内存吃满）
        if len(self._last) > self._max_clients:
            for key, _ in sorted(self._last.items(), key=lambda kv: kv[1])[
                    :len(self._last) - self._max_clients]:
                self._last.pop(key, None)

    def check(self, client_id: str) -> None:
        """放行则返回；太快则抛 `errors.rate_limited`（429 + Retry-After）。"""
        if self.per_second <= 0:
            return
        key = str(client_id or "anonymous")
        now = float(self._clock())
        with self._lock:
            last = self._last.get(key)
            if last is not None:
                wait = (1.0 / self.per_second) - (now - last)
                if wait > 0:
                    retry = max(1, int(math.ceil(wait)))
                    raise errors.rate_limited(
                        retry,
                        detail="每个客户端 TTS 最多 1 QPS（手机和 ECHO 各自独立计数）",
                    )
            self._last[key] = now
            self._prune_locked(now)
