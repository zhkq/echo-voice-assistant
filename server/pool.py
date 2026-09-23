# -*- coding: utf-8 -*-
"""`EnginePool` —— 模型实例的生命周期与并发（服务端最核心的一块）。

为什么需要它：客户端的引擎层是按"单用户单进程"写的（全局字典 + 一把全局锁，
`app/audio/stt.py` 的 `_ENGINES`、`app/audio/diarize.py` 的 `_pipeline`）。
搬到服务端，第二个并发请求就会被那把全局锁串行化，而载入大模型时更会**独占锁几十秒**。
这里把"加载 / 复用 / 卸载 / 并发"收在一处。

六条职责（设计 §4.1）：

  * **单飞加载** —— 并发 N 个请求要同一个模型，只加载一次，其余等同一个 future
  * **引用计数** —— 推理中不许卸载
  * **LRU 卸载** —— 显存压力下先卸"最久未用且没人用"的
  * **显存预算** —— 超预算就**拒绝**（`gpu_oom`），不 OOM、**也不静默回退 CPU**
  * **并发槽** —— 每个模型一个信号量（如说话人分离的模型不是线程安全的 → 1）
  * **版本冻结** —— `modelVersion` / `vectorSpaceId` 在**进程生命周期内不变**

最后一条特别重要：客户端按 `vectorSpaceId` 比对向量，并在**一场会议内锁定**它。
服务端若在同一个 id 下悄悄换了权重，客户端的跨段比对会**静默失效** ——
余弦相似度不会崩，只会开始认错人。所以**换模型 = 换 id = 重启服务**。

**服务端不许静默回退 CPU**（与客户端相反，设计 §3.4）：客户端单用户、宁可慢也要出结果；
服务端一旦偷偷用 CPU，会拖慢**所有**客户端的请求，而且让人查不出原因。
显存不足就如实报 `gpu_oom`，让这一个客户端去降级。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Callable, Dict, List, NamedTuple, Optional

from server import errors


class ModelSpec(NamedTuple):
    """一个模型的声明。

    `slot` 是它的**主**能力槽；`supports` 列出它还能满足的其它槽 ——
    同一个模型常常一人多角（长音频那个既给文本又给时间戳），
    客户端靠 `capabilities` 里的这两项决定"这个后端能不能做我要的事"。
    """

    id: str
    slot: str
    impl: str
    resident: bool = False         # True = 启动预热、不参与 LRU
    max_concurrency: int = 1
    est_vram_mb: int = 0
    model_version: str = ""
    vector_space_id: str = ""      # 只有产出向量的模型有；客户端据此比对
    dim: int = 0
    supports: tuple = ()           # 除 slot 外还满足的槽

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        sup = d.get("supports", ())
        if isinstance(sup, str):
            sup = (sup,)
        return cls(
            id=str(d["id"]), slot=str(d.get("slot", "")), impl=str(d.get("impl", d["id"])),
            resident=bool(d.get("resident", False)),
            max_concurrency=max(1, int(d.get("max_concurrency", 1) or 1)),
            est_vram_mb=int(d.get("est_vram_mb", 0) or 0),
            model_version=str(d.get("modelVersion", d.get("model_version", "")) or ""),
            vector_space_id=str(d.get("vectorSpaceId", d.get("vector_space_id", "")) or ""),
            dim=int(d.get("dim", 0) or 0),
            supports=tuple(str(x) for x in (sup or ())),
        )


class _Entry:
    __slots__ = ("spec", "state", "instance", "refs", "slots", "future",
                 "error", "last_used", "load_ms", "unloads")

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.state = "absent"                  # absent|loading|ready|failed|evicting
        self.instance = None
        self.refs = 0
        self.slots = threading.Semaphore(spec.max_concurrency)
        self.future: Optional[Future] = None
        self.error = ""
        self.last_used = 0.0
        self.load_ms = 0
        self.unloads = 0


class Lease:
    """一次推理的占用凭据。`with pool.acquire(mid) as inst:` 用它。"""

    def __init__(self, pool: "EnginePool", entry: _Entry):
        self._pool = pool
        self._entry = entry
        self.instance = entry.instance

    def __enter__(self):
        return self.instance

    def __exit__(self, *exc):
        self._pool.release(self._entry)
        return False


class EnginePool:
    def __init__(self, specs: List[ModelSpec], loaders: Dict[str, Callable[[ModelSpec], object]],
                 vram_budget_mb: int = 0, load_timeout_s: float = 300.0,
                 busy_retry_after: int = 5):
        self._cv = threading.Condition()
        self._entries: Dict[str, _Entry] = {}
        self._by_slot: Dict[str, List[str]] = {}
        for s in specs:
            e = _Entry(s)
            if s.resident:
                # 常驻的启动即预热：让第一个客户端别等加载
                e.state = "absent"
            self._entries[s.id] = e
            self._by_slot.setdefault(s.slot, []).append(s.id)
        self.loaders = dict(loaders)
        self.vram_budget_mb = int(vram_budget_mb or 0)
        self.load_timeout_s = float(load_timeout_s)
        self.busy_retry_after = int(busy_retry_after)

    # ---------------------------------------------------------------- 查询

    def spec(self, model_id: str) -> ModelSpec:
        e = self._entries.get(model_id)
        if e is None:
            raise errors.model_not_found(model_id)
        return e.spec

    def models_for_slot(self, slot: str) -> List[str]:
        return list(self._by_slot.get(slot, []))

    def pick_for_slot(self, slot: str, want: str = "") -> str:
        """按槽选模型；`want` 是指名道姓的覆盖。槽上没有可用模型时报 404。"""
        if want:
            self.spec(want)                      # 不存在则抛
            return want
        ids = self._by_slot.get(slot) or []
        if not ids:
            raise errors.model_not_found(slot)
        return ids[0]

    def state_of(self, model_id: str) -> str:
        with self._cv:
            e = self._entries.get(model_id)
            return e.state if e else "absent"

    def _used_vram_mb(self) -> int:
        return sum(e.spec.est_vram_mb for e in self._entries.values() if e.state == "ready")

    # ---------------------------------------------------------------- 加载

    def _start_load_locked(self, e: _Entry) -> None:
        """必须在持 `_cv` 时调用。单飞：已经有人在加载就复用同一个 future。"""
        if e.future is not None and not e.future.done():
            return
        e.state = "loading"
        e.error = ""
        e.future = Future()
        threading.Thread(target=self._do_load, args=(e,),
                         name="pool-load-%s" % e.spec.id, daemon=True).start()

    def _do_load(self, e: _Entry) -> None:
        t0 = time.time()
        try:
            loader = self.loaders.get(e.spec.impl)
            if loader is None:
                raise RuntimeError("没有为 impl=%r 注册加载器" % e.spec.impl)
            with self._cv:
                # 显存预算：先按 LRU 腾地方；腾不出就**拒绝**（不 OOM、不回退 CPU）
                self._make_room_locked(e.spec.est_vram_mb, exclude=e.spec.id)
            inst = loader(e.spec)                       # 真正可能几十秒的一步
            with self._cv:
                e.instance = inst
                e.state = "ready"
                e.load_ms = int((time.time() - t0) * 1000)
                e.error = ""
                e.last_used = time.time()
                e.slots = threading.Semaphore(e.spec.max_concurrency)
                e.future.set_result(True)
                self._cv.notify_all()
        except Exception as ex:                          # noqa: BLE001 —— 加载什么错都可能
            with self._cv:
                # **失败就是失败**：不回退 CPU、不假装 ready（设计 §3.4）
                e.instance = None
                e.state = "failed"
                e.error = "%s: %s" % (type(ex).__name__, ex)
                if e.future is not None and not e.future.done():
                    e.future.set_result(False)
                self._cv.notify_all()

    def load(self, model_id: str, timeout: Optional[float] = None) -> bool:
        """显式加载（预热 / 后台管理面用）。返回是否 ready。"""
        e = self._entries.get(model_id)
        if e is None:
            raise errors.model_not_found(model_id)
        deadline = time.time() + (self.load_timeout_s if timeout is None else float(timeout))
        with self._cv:
            if e.state == "ready":
                return True
            self._start_load_locked(e)
            fut = e.future
        try:
            fut.result(timeout=max(0.1, deadline - time.time()))
        except Exception:
            return False
        with self._cv:
            return e.state == "ready"

    def warm(self, timeout: float = 0.0) -> Dict[str, bool]:
        """预热常驻模型。`timeout=0` 表示只**开始**加载，不等（启动时别把服务卡住）。"""
        out = {}
        for mid, e in list(self._entries.items()):
            if not e.spec.resident:
                continue
            if timeout <= 0:
                with self._cv:
                    self._start_load_locked(e)
                out[mid] = False
            else:
                out[mid] = self.load(mid, timeout)
        return out

    # ---------------------------------------------------------------- 取用

    def acquire(self, model_id: str, timeout: Optional[float] = None) -> Lease:
        """拿到一个可用的实例。等加载（有上限），拿并发槽（**不排队**）。"""
        e = self._entries.get(model_id)
        if e is None:
            raise errors.model_not_found(model_id)
        deadline = time.time() + (self.load_timeout_s if timeout is None else float(timeout))
        with self._cv:
            if e.state != "ready":
                self._start_load_locked(e)
            while e.state not in ("ready", "failed"):
                left = deadline - time.time()
                if left <= 0:
                    # 还在加载 → 明确告诉客户端"稍后再来"（它会退避重试）
                    raise errors.model_loading(self._retry_after_locked())
                self._cv.wait(min(left, 1.0))
            if e.state == "failed":
                raise errors.model_failed(e.error)
            e.last_used = time.time()
            e.refs += 1

        # 模型自己的并发槽：**不排队** —— 拿不到就是"这会儿忙"
        # （服务端总通道是另一道闸，见 routes；两道都不能排队，见设计 §3.6）
        if not e.slots.acquire(blocking=False):
            with self._cv:
                e.refs -= 1
            raise errors.server_busy(self.busy_retry_after)
        return Lease(self, e)

    def release(self, entry: _Entry) -> None:
        try:
            entry.slots.release()
        except ValueError:
            pass
        with self._cv:
            entry.refs = max(0, entry.refs - 1)
            entry.last_used = time.time()
            self._cv.notify_all()

    # ---------------------------------------------------------------- 卸载

    def _lru_victim_locked(self, exclude: str = "") -> Optional[_Entry]:
        """最久未用、**没人用**、且不是常驻的。"""
        best = None
        for mid, e in self._entries.items():
            if mid == exclude or e.spec.resident:
                continue
            if e.state != "ready" or e.refs > 0:
                continue
            if best is None or e.last_used < best.last_used:
                best = e
        return best

    def _unload_locked(self, e: _Entry) -> None:
        e.state = "evicting"
        e.instance = None
        e.state = "absent"
        e.future = None
        e.unloads += 1

    def _make_room_locked(self, need_mb: int, exclude: str = "") -> None:
        """腾不出就抛 `gpu_oom` —— 这是**有意**的：不回退 CPU、不 OOM。"""
        if not self.vram_budget_mb or need_mb <= 0:
            return
        while self._used_vram_mb() + need_mb > self.vram_budget_mb:
            victim = self._lru_victim_locked(exclude=exclude)
            if victim is None:
                raise errors.gpu_oom(
                    "需要 %d MB，预算 %d MB，已用 %d MB，且没有可卸载的模型"
                    % (need_mb, self.vram_budget_mb, self._used_vram_mb()))
            self._unload_locked(victim)

    def unload(self, model_id: str) -> bool:
        """显式卸载。**有人正在用就不卸**（引用计数）。"""
        with self._cv:
            e = self._entries.get(model_id)
            if e is None or e.state != "ready":
                return False
            if e.refs > 0:
                return False
            self._unload_locked(e)
            self._cv.notify_all()
            return True

    def _retry_after_locked(self) -> int:
        return max(1, int(self.busy_retry_after))

    # ---------------------------------------------------------------- 上报

    def status(self) -> List[dict]:
        """给 `/v1/capabilities` 与后台管理面用。**如实反映此刻能不能用。**"""
        with self._cv:
            used = self._used_vram_mb()
            out = []
            for mid, e in sorted(self._entries.items()):
                s = e.spec
                out.append({
                    "id": s.id,
                    "slot": s.slot,
                    "supports": list(s.supports),
                    "impl": s.impl,
                    "state": e.state,
                    "resident": bool(s.resident),
                    "modelVersion": s.model_version,
                    "vectorSpaceId": s.vector_space_id,
                    "dim": s.dim,
                    "estVramMb": s.est_vram_mb,
                    "maxConcurrency": s.max_concurrency,
                    "inUse": e.refs,
                    "loadMs": e.load_ms,
                    "unloads": e.unloads,
                    "error": e.error,
                })
            return out

    def vram(self) -> dict:
        with self._cv:
            return {"budgetMb": self.vram_budget_mb, "usedMb": self._used_vram_mb()}

    def shutdown(self) -> None:
        """退出时释放全部实例。"""
        with self._cv:
            for e in self._entries.values():
                e.instance = None
                e.state = "absent"
                e.future = None
            self._cv.notify_all()
