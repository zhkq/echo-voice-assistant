# -*- coding: utf-8 -*-
"""临时文件：**按日期落盘 + 定时清理**。

政策（2026-09-23 定，设计 §9.4）——刻意**不搞**内存盘 / 容量硬门禁 / 出入口封装：

    1. **请求结束立即删**（`finally`）—— 第一道、也是最重要的一道
    2. **定时清理** —— 崩溃 / 断电的残留靠它收
    3. 清理时顺带看总大小，超阈值**先删最老的**（而不是拒绝新请求）

目录按日期分，是因为**整目录删比逐文件快**：

    {root}/2026-09-23/<request_id>/seg.wav
    {root}/2026-09-22/            ← 整目录删

落盘**不影响性能**：一段 10 分钟音频 19 MB，相对转写本身的几秒到几十秒是噪声级；
真正影响性能的是 GPU。Linux 上想白拿内存盘，把 `tmp.root` 指到 `/dev/shm` 即可
（零代码改动）。
"""
from __future__ import annotations

import datetime
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional


def _today() -> str:
    return datetime.date.today().isoformat()


def _parse_date(name: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(name)
    except (TypeError, ValueError):
        return None


class TempWorkspace:
    """一次请求的临时工作区。**所有落盘都经由它。**

    `with` 退出时（含异常、含客户端断开）整个 `<request_id>/` 目录被删掉；
    删失败不抛异常（不能因为清理失败把一次成功的请求做成 500），只在 `failed` 里留痕。
    """

    def __init__(self, root: str, request_id: str = ""):
        self.root = root
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.failed: list = []

    @property
    def dir(self) -> str:
        return os.path.join(self.root, _today(), self.request_id)

    def __enter__(self) -> "TempWorkspace":
        os.makedirs(self.dir, exist_ok=True)
        return self

    def __exit__(self, *exc) -> bool:
        self.cleanup()
        return False

    def path(self, name: str = "seg.wav") -> str:
        """申请一个文件路径（不创建）。"""
        return os.path.join(self.dir, os.path.basename(name))

    def cleanup(self) -> None:
        """删掉本次请求的整个目录。**不抛异常**。"""
        try:
            shutil.rmtree(self.dir, ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception as e:                     # 删不掉要留痕，不能静默
            self.failed.append("%s: %s" % (type(e).__name__, e))
        # 顺手收掉空的日期目录，免得根目录攒一堆空壳
        try:
            day = os.path.dirname(self.dir)
            if os.path.isdir(day) and not os.listdir(day):
                os.rmdir(day)
        except Exception:
            pass


# ---------------------------------------------------------------- 定时清理

@dataclass
class SweepReport:
    removed_dirs: int = 0
    freed_bytes: int = 0
    remaining_bytes: int = 0
    failed: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {"removed": self.removed_dirs, "freedBytes": self.freed_bytes,
                "remainingBytes": self.remaining_bytes, "failed": self.failed}


def _dir_bytes(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def _rm(path: str) -> int:
    """删一个目录，返回释放的字节数；失败返回 -1。"""
    size = _dir_bytes(path)
    try:
        shutil.rmtree(path)
        return size
    except Exception:
        return -1


def sweep(root: str, ttl_hours: float = 4.0, max_bytes: int = 0) -> SweepReport:
    """清理过期与超额。返回报告。

    两步，都是"整目录删"：
      1. **超过 TTL 的**：日期目录整删；日期目录内部超龄的 `request_id/` 也删
         （防某次请求卡住、文件一直躺在今天的目录里）
      2. **超过 max_bytes 的**：按修改时间从最老的开始删，直到降到阈值以下
    """
    rep = SweepReport()
    if not root or not os.path.isdir(root):
        return rep

    now = time.time()
    ttl_s = max(0.0, float(ttl_hours)) * 3600.0

    # 1) 过期
    for name in sorted(os.listdir(root)):
        day_path = os.path.join(root, name)
        if not os.path.isdir(day_path):
            continue
        day = _parse_date(name)
        if day is None:
            continue                       # 不是日期目录就不碰（可能有人放了别的东西）
        # 日期目录整体过期（按"这一天的结束"算，TTL=4h 时今天永远保留）
        day_end = datetime.datetime.combine(day, datetime.time.max).timestamp()
        if now - day_end > ttl_s:
            freed = _rm(day_path)
            if freed < 0:
                rep.failed += 1
            else:
                rep.removed_dirs += 1
                rep.freed_bytes += freed
            continue
        # 日期目录内部：超龄的单次请求目录
        for req in sorted(os.listdir(day_path)):
            req_path = os.path.join(day_path, req)
            if not os.path.isdir(req_path):
                continue
            try:
                age = now - os.path.getmtime(req_path)
            except OSError:
                continue
            if age > ttl_s:
                freed = _rm(req_path)
                if freed < 0:
                    rep.failed += 1
                else:
                    rep.removed_dirs += 1
                    rep.freed_bytes += freed

    # 2) 总大小超阈值 → 从最老的开始删
    if max_bytes and max_bytes > 0:
        entries = []
        for day in sorted(os.listdir(root)):
            day_path = os.path.join(root, day)
            if not os.path.isdir(day_path):
                continue
            for req in sorted(os.listdir(day_path)):
                p = os.path.join(day_path, req)
                if not os.path.isdir(p):
                    continue
                try:
                    entries.append((os.path.getmtime(p), p))
                except OSError:
                    pass
        entries.sort()                     # 最老的在前
        total = _dir_bytes(root)
        for _mtime, p in entries:
            if total <= max_bytes:
                break
            freed = _rm(p)
            if freed < 0:
                rep.failed += 1
                continue
            rep.removed_dirs += 1
            rep.freed_bytes += freed
            total -= freed

    # 3) 收掉空壳的日期目录（上面删完 request 目录后会留下空的日期目录）
    try:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p) and _parse_date(name) is not None and not os.listdir(p):
                os.rmdir(p)
    except OSError:
        pass

    rep.remaining_bytes = _dir_bytes(root)
    return rep


def stats(root: str) -> Dict[str, object]:
    """给 `/v1/health` 看的临时目录统计。

    **数字不归零就是 bug 的信号** —— 运维一眼能看出泄漏。所以这三个字段是必须的。
    """
    out = {"root": root, "files": 0, "bytes": 0, "oldestSeconds": 0}
    if not root or not os.path.isdir(root):
        return out
    oldest = None
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out["files"] += 1
            out["bytes"] += st.st_size
            if oldest is None or st.st_mtime < oldest:
                oldest = st.st_mtime
    if oldest is not None:
        out["oldestSeconds"] = int(max(0, time.time() - oldest))
    return out


class Sweeper:
    """后台定时清理线程（退出时可以被叫停）。"""

    def __init__(self, root: str, ttl_hours: float = 4.0,
                 interval_s: float = 3600.0, max_bytes: int = 0):
        self.root = root
        self.ttl_hours = ttl_hours
        self.interval_s = max(1.0, float(interval_s))
        self.max_bytes = max_bytes
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: Optional[SweepReport] = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last = sweep(self.root, self.ttl_hours, self.max_bytes)
            except Exception:
                pass                      # 清理失败不该把服务拖垮
            self._stop.wait(self.interval_s)

    def start(self) -> "Sweeper":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="tmp-sweeper", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
