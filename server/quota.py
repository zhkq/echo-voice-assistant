# -*- coding: utf-8 -*-
"""每日音频分钟数配额（设计 §7.2）。

## 为什么是**进程内计数**

设计里定死了"能力面不查库"（§8.5）：每请求查一次库会让鉴权库变成瓶颈，
而配额判断正好在**每个**请求的最前面。所以：

| 数据 | 在哪 | 为什么 |
|---|---|---|
| 今天已经用了多少分钟 | **进程内**（本模块） | 每请求查库会把库变成瓶颈 |
| 配额**上限** | 库（`clients.daily_audio_minutes`）/ 配置默认值 | 改动少，可缓存 |
| 长期统计 | 将来的 `calls_rollup` | 给运营看，不给鉴权看 |

**代价写在明处**：多实例部署时计数不共享 → 同一个客户端的"每日分钟数"按实例各算一份。
和并发配额是同一个取舍。要精确就得把客户端粘到固定实例（设计 §9.4），或上外部计数器 ——
v1 都不做，但**不许把它说成"精确的日额度"**。

## 记账的时机：**先占通道，再记账**

计费点在拿到推理通道之后（`routes.State.billing`）。被 `409 client_busy` /
`503 server_busy` 顶回去的请求**不计费** —— 我们没为它烧 GPU，凭什么收钱。
反过来，通道拿到了、模型开始跑了，即使推理失败也计费：那段 GPU 时间真的花了。
"""
from __future__ import annotations

import datetime
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from server import errors


def _seconds_until_tomorrow(now: Optional[datetime.datetime] = None) -> int:
    """到本地明天 0 点还有几秒（`Retry-After` 用它 —— 配额是按**本地自然日**算的）。

    至少给 1 秒：0 会让某些客户端把 `Retry-After: 0` 理解成"立刻重试"，
    于是它在 0 点前后打转。
    """
    now = now or datetime.datetime.now()
    tomorrow = datetime.datetime.combine(now.date() + datetime.timedelta(days=1),
                                         datetime.time.min)
    return max(1, int((tomorrow - now).total_seconds()))


class QuotaLedger:
    """按客户端记"今天用了多少秒音频"。限额 0（或负数）= **不限**。"""

    def __init__(self, default_minutes: float = 0.0,
                 limit_for: Optional[Callable[[str], float]] = None,
                 clock: Optional[Callable[[], datetime.datetime]] = None):
        #: 全局默认（分钟）。0 = 不限。
        self.default_minutes = max(0.0, float(default_minutes or 0.0))
        #: 取某个客户端的**专属**上限（分钟；0/None = 用全局）。鉴权关着时不会被调用。
        self._limit_for = limit_for
        self._clock = clock or datetime.datetime.now
        self._lock = threading.Lock()
        self._day = self._today()
        #: client_id → 今天已用的**秒数**（存秒，显示时再换算成分钟）
        self._used: Dict[str, float] = {}

    # ---------------------------------------------------------------- 内部

    def _today(self) -> str:
        return self._clock().date().isoformat()

    def _rollover_locked(self) -> None:
        """跨天就把计数清零。在锁内调用。

        为什么用"本地自然日"而不是 24 小时滑动窗口：**人理解的额度就是一整天**
        （"今天还能用 30 分钟"）。滑动窗口在界面上没法用一句人话说清楚。
        """
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = {}

    def _limit_of_locked(self, client_id: str) -> float:
        if self._limit_for is not None:
            try:
                per_client = float(self._limit_for(client_id) or 0.0)
            except Exception:
                per_client = 0.0
            if per_client > 0:
                return per_client
        return self.default_minutes

    # ---------------------------------------------------------------- 查询

    def limit_minutes(self, client_id: str) -> float:
        """这个客户端今天的上限（分钟）。0 = 不限。"""
        with self._lock:
            self._rollover_locked()
            return self._limit_of_locked(client_id)

    def used_minutes(self, client_id: str) -> float:
        with self._lock:
            self._rollover_locked()
            return self._used.get(client_id, 0.0) / 60.0

    def remaining_minutes(self, client_id: str) -> Optional[float]:
        """今天还剩几分钟；**不限时返回 `None`**（不是 -1，也不是一个很大的数 —— 那两种
        客户端都会当成"有额度"去显示，而"不限"该显示成"不限"）。"""
        with self._lock:
            self._rollover_locked()
            limit = self._limit_of_locked(client_id)
            if limit <= 0:
                return None
            return max(0.0, limit - self._used.get(client_id, 0.0) / 60.0)

    # ---------------------------------------------------------------- 动作

    def check(self, client_id: str) -> None:
        """用完了就抛 `quota_exceeded`（429 + `Retry-After` 到明天 0 点）。

        **只看"已经用完"，不预测"这一条会不会超"**：预测量要等解码出音频秒数，
        而那时 body 已经收完了 —— 那正是设计要避免的"先落盘再说不行"。
        所以边界上允许最后一次略微超额（超出的部分记进今天，明天照常扣）。
        要更严就得在**声明长度**上估秒数，那是拿字节数猜时长，误差极大。
        """
        with self._lock:
            self._rollover_locked()
            limit = self._limit_of_locked(client_id)
            if limit <= 0:
                return
            if self._used.get(client_id, 0.0) >= limit * 60.0:
                raise errors.quota_exceeded(_seconds_until_tomorrow(self._clock()))

    def add(self, client_id: str, seconds: float) -> None:
        """记一笔。**不加锁地容错**：负数/NaN 一律当 0（宁可少算，也不要把额度算成负的）。"""
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if not (value > 0):
            return
        with self._lock:
            self._rollover_locked()
            self._used[client_id] = self._used.get(client_id, 0.0) + value

    # ---------------------------------------------------------------- 面板/探针

    def snapshot(self) -> Dict[str, Any]:
        """给 `/v1/health`、`/v1/capabilities` 与排障用。**只有数字，没有内容。**"""
        with self._lock:
            self._rollover_locked()
            return {
                "day": self._day,
                "defaultMinutes": self.default_minutes,
                "clients": {cid: round(sec / 60.0, 2) for cid, sec in sorted(self._used.items())},
            }

    def reset(self) -> None:
        """清空计数（测试与"换天"用；生产里靠 `_rollover_locked` 自动换）。"""
        with self._lock:
            self._used = {}
            self._day = self._today()


def open_ledger(cfg, limit_for=None) -> QuotaLedger:
    """按配置造一个账本。`limits.daily_audio_minutes` 缺省 0 = 不限。"""
    return QuotaLedger(default_minutes=float(cfg.get("limits.daily_audio_minutes", 0) or 0),
                       limit_for=limit_for)
