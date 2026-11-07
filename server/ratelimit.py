# -*- coding: utf-8 -*-
"""按**账号**的限速与并发闸门（设计 §4.4；2026-11-03 收费）。

两个闸门解决两件不同的事，**都按 `user_id` 而不是 `client_id`**：

| 闸门 | 问的问题 | 超了回什么 |
|---|---|---|
| 1 QPS | 起请求的**速率**太快？ | `429 rate_limited` + `Retry-After: 1` |
| 并发 | 同一时刻有几台设备在**占 GPU**？ | `409 client_busy`（detail 说明另一台设备） |

它们与既有的 `routes.Admission`（设备级 / 全局）**并存**，谁先到谁限：

* `Admission` 是"一台设备同时只跑一个"和"整台后端最多 N 个"；
* `UserConcurrency` 是"一个人同时只跑一个" —— 多设备场景下这是**新增**的一层，
  否则在家里录会的那台不会阻止办公室那台同时抢 GPU。

TTS 不占这个并发槽（不占 GPU），只受 1 QPS。

**进程内**：与 `quota.py` 同一个取舍（多实例各算一份）。要精确就得上外部计数器，
v1 不做，但**不许把它说成"精确的全局 1 QPS"**。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Dict, Optional, Tuple

from server import errors


class TokenBucket:
    """按 key 的令牌桶。默认 `rate=1 burst=1` = **同一账号每秒最多一个请求**。"""

    def __init__(self, rate: float = 1.0, burst: float = 1.0,
                 clock: Optional[Callable[[], float]] = None):
        self.rate = max(0.001, float(rate))
        self.burst = max(1.0, float(burst))
        self._clock = clock or time.time
        self._lock = threading.Lock()
        #: key → (tokens, last_refill)
        self._state: Dict[str, Tuple[float, float]] = {}

    def _take(self, key: str, rate: float) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._state.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + max(0.0, now - last) * rate)
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            return True

    def check(self, key: str, rate: Optional[float] = None,
              detail: str = "同一账号每秒只允许 1 个请求") -> None:
        """拿一个令牌；拿不到就 `429 rate_limited`（`Retry-After: 1`）。

        `detail` 可覆盖默认文案：绑定接口（issue #23）的桶是"每分钟几次"，
        照抄"每秒 1 个请求"会让人误以为自己的能力调用被限速了。
        """
        if not self._take(str(key or ""), max(0.001, float(rate or self.rate))):
            raise errors.rate_limited(1, detail)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


class UserConcurrency:
    """按 user 的并发计数（默认上限 1）。**不排队**：满了立刻 409。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}

    def current(self, key: str) -> int:
        with self._lock:
            return int(self._counts.get(str(key or ""), 0))

    def check(self, key: str, limit: int = 1) -> None:
        """只判不占（便宜预检）。权威判定在 `hold()` 里。"""
        with self._lock:
            if self._counts.get(str(key or ""), 0) >= max(1, int(limit)):
                raise errors.client_busy("同一账号的另一台设备正在处理，请等它结束")

    @contextmanager
    def hold(self, key: str, limit: int = 1):
        with self._lock:
            k = str(key or "")
            if self._counts.get(k, 0) >= max(1, int(limit)):
                raise errors.client_busy("同一账号的另一台设备正在处理，请等它结束")
            self._counts[k] = self._counts.get(k, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._counts[k] = max(0, self._counts.get(k, 1) - 1)
                if not self._counts[k]:
                    self._counts.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
