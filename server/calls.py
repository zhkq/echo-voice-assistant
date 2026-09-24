# -*- coding: utf-8 -*-
"""调用元数据的**异步**记录（设计 §7.3）与进程内 metrics。

## 两条分得很清的线

| 出口 | 数据从哪来 | 为什么 |
|---|---|---|
| `/v1/health` 的 `metrics` | **进程内计数**（`Metrics`） | 探针每几秒被敲一次，**不能查库** —— 那会让库变成探针的瓶颈 |
| 管理面 / `--stats` | `calls` 表（`Store.calls_summary`） | 那些查询要跨时间、跨客户端，还得能重启后仍在 |

两处都叫"统计"，但一个是"此刻的仪表"，一个是"账本"。合起来只会让探针变慢。

## 为什么写库是异步的

`clients` 那张表的写入**从来不发生在请求路径上**（鉴权用缓存）。`calls` 不一样：
每个请求都要落一条。同步写会做两件坏事 —— 把那一次请求的尾延迟绑在 fsync 上，
以及在库锁上制造热点（而库锁同时保护着鉴权缓存要读的行）。

所以请求路径上只做一次 `put_nowait`，后台线程攒一小批一次 `executemany`。
**队列满了就丢，并且计数**：审计写不进去，绝不该把一次已经成功的转写变成失败或变慢。
丢了多少在 `/v1/health` 的 `metrics.callsDropped` 里看得见 —— 丢了要有人知道。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, Optional

#: `calls` 里那十个列（设计 §8.5）。多一列都要走评审，所以这里也照着钉死。
CALL_FIELDS = ("ts", "client_id", "endpoint", "model_id", "audio_seconds",
               "queue_wait_ms", "duration_ms", "status", "error_code", "request_id")


class CallLog:
    """把调用元数据异步写进 `calls` 表。**请求路径上不阻塞、不抛异常。**"""

    def __init__(self, store, *, flush_interval_s: float = 1.0, max_queue: int = 1000,
                 retention_days: float = 30.0, prune_interval_s: float = 3600.0,
                 log=None):
        self.store = store
        self.flush_interval_s = max(0.05, float(flush_interval_s))
        self.retention_days = max(0.0, float(retention_days or 0.0))
        self.prune_interval_s = max(60.0, float(prune_interval_s))
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max(1, int(max_queue)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._dropped = 0
        self._written = 0
        self._pruned = 0
        self._last_prune = time.time()
        self._log = log

    # ---------------------------------------------------------------- 请求路径

    def record(self, **fields) -> bool:
        """记一条。**永不抛、永不阻塞** —— 装不下就丢并计数。

        只留下认识的那十个列：多传的键会被忽略（免得"看起来记了、其实没这一列"）。
        """
        row = {k: fields.get(k) for k in CALL_FIELDS}
        try:
            self._q.put_nowait(row)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        except Exception:                                  # pragma: no cover - 兜底
            with self._lock:
                self._dropped += 1
            return False

    # ---------------------------------------------------------------- 后台线程

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="echo-calllog", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.flush()                    # 退出前把剩下的写完：别丢最后一批

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._drain(self.flush_interval_s)
            if batch:
                self._write(batch)
            self._maybe_prune()

    def _drain(self, timeout: float) -> list:
        """攒一小批。第一个元素等 `timeout`，之后**有多少拿多少**（不额外等）。"""
        out = []
        try:
            out.append(self._q.get(timeout=timeout))
        except queue.Empty:
            return out
        while len(out) < 500:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def _write(self, batch: list) -> int:
        n = self.store.insert_calls(batch)
        with self._lock:
            self._written += n
        if n == 0 and self._log is not None:
            self._log("warn", "calls", "调用记录写库失败（%d 条丢弃）" % len(batch))
        return n

    def flush(self) -> int:
        """把队列抽干并写库。返回写了几条（测试与退出时用）。"""
        total = 0
        while True:
            batch = self._drain(0.0)
            if not batch:
                break
            total += self._write(batch)
            if len(batch) < 500:
                break
        return total

    def _maybe_prune(self) -> None:
        if self.retention_days <= 0:
            return
        now = time.time()
        if (now - self._last_prune) < self.prune_interval_s:
            return
        self._last_prune = now
        self.prune_now()

    def prune_now(self) -> int:
        """删掉保留期之前的记录。返回删了几条。"""
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400.0
        n = self.store.prune_calls(cutoff)
        with self._lock:
            self._pruned += n
        return n

    # ---------------------------------------------------------------- 面板/探针

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"queued": self._q.qsize(), "dropped": self._dropped,
                    "written": self._written, "pruned": self._pruned,
                    "retentionDays": self.retention_days}


class Metrics:
    """进程内计数（设计 §8.4 的"此刻仪表"）。**只加不减**，给 `/v1/health` 用。"""

    def __init__(self):
        self.started = time.time()
        self._lock = threading.Lock()
        self._by_endpoint: Dict[str, Dict[str, Any]] = {}
        self._errors: Dict[str, int] = {}

    def observe(self, endpoint: str, status: int, duration_ms: int,
                audio_seconds: float = 0.0, error_code: str = "") -> None:
        with self._lock:
            row = self._by_endpoint.setdefault(
                endpoint, {"calls": 0, "errors": 0, "audioSeconds": 0.0,
                           "lastMs": 0, "maxMs": 0})
            row["calls"] += 1
            row["audioSeconds"] = round(row["audioSeconds"] + float(audio_seconds or 0.0), 2)
            row["lastMs"] = int(duration_ms or 0)
            row["maxMs"] = max(row["maxMs"], int(duration_ms or 0))
            if int(status or 0) >= 400 or error_code:
                row["errors"] += 1
                key = error_code or ("http_%s" % status)
                self._errors[key] = self._errors.get(key, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "uptimeSeconds": int(time.time() - self.started),
                "endpoints": {k: dict(v) for k, v in sorted(self._by_endpoint.items())},
                "errorCodes": dict(sorted(self._errors.items())),
                "totalCalls": sum(v["calls"] for v in self._by_endpoint.values()),
                "totalErrors": sum(v["errors"] for v in self._by_endpoint.values()),
            }
