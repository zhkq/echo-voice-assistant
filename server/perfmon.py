# -*- coding: utf-8 -*-
"""性能监控（管理面「性能」页签的采集侧）：**1 秒一个点，只在内存里，永不落盘**。

## 三条判据（对应需求原话）

1. **"登录后有开始记录"** —— 采集**不是常驻**的：只有管理员在面板上按了
   「开始记录」（`POST /admin/api/perf/start`）之后才采。
2. **"不用持久化"** —— 所有点只活在一个 `collections.deque` 里。这个模块
   **一处都没有写文件的调用**（写模式的打开、改名替换、临时文件，一个都没有）。
   容器里根文件系统是只读的（`read_only: true`），所以"不落盘"既是需求也是环境约束。
   `tests/test_perfmon.py` 用两道判据钉着它：**源码扫描** + **运行期给
   `builtins.open` 与 `os` 的改名/删除入口打桩**（断言一个写调用都没有）。
3. **"用图表表示"** —— 见 `server/admin.html` 的「性能」页签（4 张内联 SVG，2×2）。

## 什么时候真的去采：**已开始记录 且 有人在看**

`should_sample()` 要求两件事同时成立：

* `recording` —— 按过「开始记录」，且没按「停止记录」；
* **有人在看** —— 最近 `viewer_ttl_s` 秒内有过一次 `state` / `points` 请求。

第二条是"页面关掉/切走就停"的落地：页面一关，轮询就停了，`viewer_ttl_s` 一到，
采集线程下一次醒来**自己退出**（不是空转，也不是"永远留着"）。
`recording` 这个标志**留着** —— 页面再打开时不必重新按一次「开始记录」，
它自己就接着采。这正是把"在记录"与"有人看"分成两个条件的意思：
**记录是管理员的意图，采样是意图 + 有人看的结果。**

## 为什么采集函数是可注入的

`nvidia-smi` / `/proc` / `psutil` 都是**真机资源**，而本仓库对"测试碰到真实资源"
极其敏感（见 AGENTS.md 里 harness pid 文件那次事故）。所以 `HostSampler` 的
**外部命令与 psutil 都是构造参数**，测试一律注入替身 ——
`tests/` 里**没有任何一处**会真的去调 `nvidia-smi`。

## psutil 是可选的

`server/requirements.txt` 里**没有** psutil（不为它长一条服务端依赖），
所以 CPU 与内存都**优先用 psutil（装了就用），没有就自己读 `/proc`**
（`/proc/stat` 算两次之间的差值、`/proc/meminfo` 算 `MemTotal - MemAvailable`）。
两条路都在用例里跑到，不靠"我这台机器上装了 psutil"。
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

#: 采样间隔（秒）。用户拍板的就是 1 秒。
DEFAULT_INTERVAL_S = 1.0

#: 内存窗口（秒）。15 分钟 × 1 秒 = **900 点**。
DEFAULT_WINDOW_S = 900.0

#: 「有人在看」的判据：最后一次 `state`/`points` 之后，这么久内算有人看。
#: 页面 1.5 秒轮询一次，10 秒留够了"一次卡顿 + 一次 GC"的余量。
VIEWER_TTL_S = 10.0

#: `points` 一次最多回多少个点（**增量取点，不要每次全量**）。
DEFAULT_PAGE = 300

#: 一次最多回多少个点（防"客户端说 since=0"把 15 分钟一次性拖走）。
MAX_PAGE = 2000

#: 窗口的可配置范围（秒）。下界 = 1 分钟，上界 = 2 小时。
MIN_WINDOW_S = 60.0
MAX_WINDOW_S = 7200.0

#: `since` 是**序号**还是**时间戳**的分界。序号从 1 开始数，长不到 10 亿；
#: unix 秒在 2001 年就过了 10 亿。所以 ≥ 1e9 一律按时间戳解释。
_TIMESTAMP_FLOOR = 1e9

#: 一个点里**主机侧**的字段（采样器给什么就填什么，缺的是 None）。
HOST_KEYS = ("gpuPercent", "gpuMemUsedMb", "gpuMemTotalMb", "gpuTempC", "gpuPowerW",
             "cpuPercent", "memPercent", "memUsedMb", "memTotalMb")

#: 一个点里**后端自报**的字段（`/v1/health` 里那几个）。
SERVER_KEYS = ("active", "maxConcurrent", "vramUsedMb", "vramBudgetMb",
               "modelsReady", "modelsTotal")


# ---------------------------------------------------------------- 环形缓冲

def ring_capacity(window_s: float, interval_s: float) -> int:
    """窗口 → 点数。`900 秒 / 1 秒 = 900 点`。"""
    step = max(0.05, float(interval_s))
    return max(1, int(round(float(window_s) / step)))


class RingBuffer:
    """固定容量的环形缓冲：满了从**最老的**那头丢（"最近 N 分钟"就是这个意思）。

    **只在内存里**：`deque(maxlen=...)` 自己就是那个环 —— 没有落盘、
    没有 mmap、没有临时文件，一行 I/O 都没有。

    序号（`seq`）**只增不减**，即使换窗口（`clear`）也不回退：客户端手上那个
    `since` 因此永远不会"看到已经看过的旧点"或者"错过新点"。
    """

    def __init__(self, capacity: int, last_seq: int = 0):
        self.capacity = max(1, int(capacity))
        self._rows: Deque[Dict[str, Any]] = deque(maxlen=self.capacity)
        self._last_seq = int(last_seq)

    def add(self, point: Dict[str, Any]) -> Dict[str, Any]:
        self._last_seq += 1
        row = dict(point)
        row["seq"] = self._last_seq
        self._rows.append(row)
        return row

    def tail(self, limit: int) -> List[Dict[str, Any]]:
        """**最近** limit 个点（页面刚打开时要的就是它，而不是最老的 300 个）。"""
        rows = list(self._rows)
        return rows[-int(limit):] if limit and limit < len(rows) else rows

    def after_seq(self, seq: int, limit: int) -> List[Dict[str, Any]]:
        return [r for r in self._rows if int(r["seq"]) > int(seq)][:int(limit)]

    def after_t(self, when: float, limit: int) -> List[Dict[str, Any]]:
        return [r for r in self._rows if float(r["t"]) > float(when)][:int(limit)]

    def clear(self) -> None:
        """丢掉全部点。**不回退 `_last_seq`**（理由见类文档）。"""
        self._rows.clear()

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def first_seq(self) -> int:
        """当前还在缓冲里的**最老**那个点的序号；空缓冲 → `last_seq + 1`。"""
        if not self._rows:
            return self._last_seq + 1
        return int(self._rows[0]["seq"])

    @property
    def dropped(self) -> int:
        """被环形覆盖掉（已经不在窗口里）的点数。"""
        return max(0, self._last_seq - len(self._rows))


def parse_since(since: Any) -> Tuple[Optional[str], Optional[float]]:
    """`since` → `(mode, value)`。

    * 空 / `<= 0` → `(None, None)`：**不限**，取最近一批（页面首屏）；
    * `>= 1e9` → `("t", 时间戳)`：只要 `t` 更大的点（时间坐标）；
    * 其它 → `("seq", 序号)`：只要 `seq` 更大的点（**增量**的默认坐标）。

    认不出来就抛 `ValueError` —— 由调用方翻成 400，**不静默当成"从头来"**
    （那会让一个打错的 `since` 看起来"工作正常"，实际每次都在全量重拉）。
    """
    text = str("" if since is None else since).strip()
    if not text:
        return (None, None)
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError("since 要是序号或 unix 时间戳（也可以是空）")
    if value <= 0:
        return (None, None)
    if value >= _TIMESTAMP_FLOOR:
        return ("t", value)
    return ("seq", int(value))


# ---------------------------------------------------------------- 真机采集

def _try_psutil():
    """装了就用，没装返回 None（**不引依赖**：`server/requirements.txt` 里没有 psutil）。"""
    try:
        import psutil                                   # noqa: PLC0415 - 可选依赖，故意懒加载
    except Exception:
        return None
    return psutil


def run_command(argv: List[str], timeout_s: float = 3.0) -> str:
    """跑一条只读命令，返回 stdout 文本。非 0 退出 → 抛（调用方按"这块读不到"处理）。"""
    proc = subprocess.run(list(argv), capture_output=True, timeout=float(timeout_s), check=False)
    if proc.returncode != 0:
        raise RuntimeError("%s 退出码 %d" % (argv[0], proc.returncode))
    return proc.stdout.decode("utf-8", "replace")


def _num(text: Any) -> Optional[float]:
    """`"12"` → 12.0；`"[N/A]"` / `""` / 坏值 → None（**不编 0**：0% 与"读不到"是两件事）。"""
    raw = str("" if text is None else text).strip()
    if not raw or raw.lower() in ("[n/a]", "n/a", "na", "[not supported]", "-"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _num_mb(text: Any) -> Optional[float]:
    """显存那一列有时带单位（`"3456 MiB"`）—— 去掉单位再解析。"""
    raw = str("" if text is None else text).strip()
    for unit in ("MiB", "MB", "mib", "mb"):
        if raw.endswith(unit):
            raw = raw[: -len(unit)].strip()
            break
    return _num(raw)


#: `nvidia-smi --query-gpu=...` 的列序（`parse_gpu_csv` 按这个顺序解析）。
GPU_QUERY = ("utilization.gpu", "memory.used", "memory.total",
             "temperature.gpu", "power.draw")


def parse_gpu_csv(text: str) -> Dict[str, Any]:
    """`"12, 3456, 24564, 61, 180.34"` → 一个点的 GPU 那几个字段。

    **只看第一块卡**（多卡的服务端不在这一版里；挑错卡比"只有第一块"更容易骗人）。
    字段读不到就是 `None`，绝不补 0。
    """
    line = ""
    for raw in str(text or "").splitlines():
        if raw.strip():
            line = raw.strip()
            break
    if not line:
        return {}
    cells = [c.strip() for c in line.split(",")]
    cells += [""] * (len(GPU_QUERY) - len(cells))
    return {
        "gpuPercent": _num(cells[0]),
        "gpuMemUsedMb": _num_mb(cells[1]),
        "gpuMemTotalMb": _num_mb(cells[2]),
        "gpuTempC": _num(cells[3]),
        "gpuPowerW": _num(cells[4]),
    }


def parse_proc_stat(text: str) -> Optional[Tuple[float, float]]:
    """`/proc/stat` 的第一行 `cpu ...` → `(idle_jiffies, total_jiffies)`。

    `idle` 含 `iowait`（与 psutil 的口径一致）；认不出来返回 None。
    """
    for raw in str(text or "").splitlines():
        parts = raw.split()
        if parts and parts[0] == "cpu" and len(parts) >= 5:
            try:
                ticks = [float(x) for x in parts[1:]]
            except ValueError:
                return None
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0.0)
            return (idle, sum(ticks))
    return None


def parse_proc_meminfo(text: str) -> Dict[str, Any]:
    """`/proc/meminfo` → `{"memUsedMb", "memTotalMb", "memPercent"}`。

    用 `MemAvailable`（内核估的"还能给新进程用多少"）而不是 `MemFree` ——
    后者会把 page cache 算成"已用"，一台跑了很久的机器会永远显示 95%。
    `MemAvailable` 没有（很老的内核）就退到 `MemFree`。
    """
    vals: Dict[str, float] = {}
    for raw in str(text or "").splitlines():
        key, _, rest = raw.partition(":")
        parts = rest.split()
        if not parts:
            continue
        value = _num(parts[0])
        if value is None:
            continue
        # 单位一律是 kB（`MemTotal:  24564184 kB`）
        scale = 1024.0 if len(parts) > 1 and parts[1].lower() == "kb" else 1.0
        vals[key.strip()] = value * scale
    total = vals.get("MemTotal")
    if not total:
        return {}
    avail = vals.get("MemAvailable")
    if avail is None:
        avail = vals.get("MemFree")
    if avail is None:
        return {"memTotalMb": round(total / 1048576.0, 1)}
    used = max(0.0, total - avail)
    return {"memUsedMb": round(used / 1048576.0, 1),
            "memTotalMb": round(total / 1048576.0, 1),
            "memPercent": round(100.0 * used / total, 2)}


class HostSampler:
    """主机侧采集：GPU（`nvidia-smi`）/ CPU / 内存。

    **每一个外部入口都是构造参数**：`runner`（跑命令）、`psutil_mod`（装了就用）、
    `proc_root`（`/proc` 的位置，同时也是"没有 psutil"时那条路的开关）。
    测试里全部注入替身，所以**测试永远不会真的去调 `nvidia-smi`**，也不会读真 `/proc`。

    `psutil_mod=False` = **强制走 `/proc`**（"没装 psutil"那条路的用例就这么写）。
    """

    def __init__(self, *, runner: Optional[Callable[..., str]] = None, psutil_mod: Any = None,
                 proc_root: str = "/proc", nvidia_smi: str = "nvidia-smi",
                 timeout_s: float = 3.0):
        self._runner = runner if runner is not None else run_command
        self._psutil = _try_psutil() if psutil_mod is None else psutil_mod
        self.proc_root = str(proc_root)
        self.nvidia_smi = str(nvidia_smi)
        self.timeout_s = float(timeout_s)
        self.last_error = ""
        self._last_cpu: Optional[Tuple[float, float]] = None

    # ---- GPU ----

    def gpu_query_argv(self) -> List[str]:
        return [self.nvidia_smi, "--query-gpu=" + ",".join(GPU_QUERY),
                "--format=csv,noheader,nounits"]

    def gpu(self) -> Dict[str, Any]:
        return parse_gpu_csv(self._runner(self.gpu_query_argv(), self.timeout_s))

    # ---- CPU ----

    def cpu_percent(self) -> Optional[float]:
        if self._psutil:
            try:
                # interval=None = "上次调用以来的差值"，**不阻塞**。
                # 第一次调用没有上一次，psutil 给 0.0 —— 如实记 0，不假装没有。
                return float(self._psutil.cpu_percent(interval=None))
            except Exception as exc:
                self._note(exc)
        return self._proc_cpu_percent()

    def _proc_cpu_percent(self) -> Optional[float]:
        try:
            sample = parse_proc_stat(self._read_proc("stat"))
        except Exception as exc:
            self._note(exc)
            return None
        if sample is None:
            return None
        prev, self._last_cpu = self._last_cpu, sample
        if prev is None:
            # 第一次没有"上一次"，算不出差值。**返回 None 而不是 0**
            #（0% 是一个有意义的值，不该拿它冒充"还不知道"）。
            return None
        d_idle, d_total = sample[0] - prev[0], sample[1] - prev[1]
        if d_total <= 0:
            return None
        return round(100.0 * max(0.0, d_total - d_idle) / d_total, 2)

    # ---- 内存 ----

    def memory(self) -> Dict[str, Any]:
        if self._psutil:
            try:
                vm = self._psutil.virtual_memory()
                return {"memPercent": round(float(vm.percent), 2),
                        "memUsedMb": round(float(vm.used) / 1048576.0, 1),
                        "memTotalMb": round(float(vm.total) / 1048576.0, 1)}
            except Exception as exc:
                self._note(exc)
        try:
            return parse_proc_meminfo(self._read_proc("meminfo"))
        except Exception as exc:
            self._note(exc)
            return {}

    # ---- 一把抓 ----

    def sample(self) -> Dict[str, Any]:
        """一个点的主机侧字段。**每一块各自 try** —— 显卡读不到不该让 CPU 也没有。

        返回的字典**始终带齐 `HOST_KEYS`**（读不到的填 `None`）：
        下游（前端/测试）不必区分"这个键不存在"与"这个值读不到"。
        """
        out: Dict[str, Any] = {key: None for key in HOST_KEYS}
        for part in (self.gpu, self.memory):
            try:
                out.update(part() or {})
            except Exception as exc:
                self._note(exc)
        try:
            out["cpuPercent"] = self.cpu_percent()
        except Exception as exc:
            self._note(exc)
        return out

    # ---- 内部 ----

    def _read_proc(self, name: str) -> str:
        path = "%s/%s" % (self.proc_root.rstrip("/"), name)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:   # noqa: PTH123 - 只读
            return fh.read()

    def _note(self, exc: BaseException) -> None:
        text = "%s: %s" % (type(exc).__name__, exc)
        self.last_error = text[:200]


def server_sampler_of(state: Any) -> Callable[[], Dict[str, Any]]:
    """后端**自报**的那几个数（`/v1/health` 里同一份口径）：

    * `active` / `maxConcurrent` —— 在途请求数（`state.admission.snapshot()`）；
    * `vramUsedMb` / `vramBudgetMb` —— 进程内显存账（`state.pool.vram()`）；
    * `modelsReady` / `modelsTotal` —— 模型状态里"就绪几个"（`state.pool.status()`）。

    读不到就是 `None`（**不编数**）；`state` 上缺这些属性也不炸 —— 采集侧
    只该"少一个点"，不该把线程带走。
    """
    def sample() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            busy = state.admission.snapshot() or {}
            out["active"] = _num(busy.get("active"))
            out["maxConcurrent"] = _num(busy.get("maxConcurrent"))
        except Exception:
            pass
        try:
            vram = state.pool.vram() or {}
            out["vramUsedMb"] = _num_mb(vram.get("usedMb"))
            out["vramBudgetMb"] = _num_mb(vram.get("budgetMb"))
        except Exception:
            pass
        try:
            rows = state.pool.status() or []
            out["modelsTotal"] = float(len(rows))
            out["modelsReady"] = float(sum(1 for r in rows if str(r.get("state")) == "ready"))
        except Exception:
            pass
        return out

    return sample


# ---------------------------------------------------------------- 监控器

class PerfMonitor:
    """采样器 + 环形窗口 + 「开始/停止记录」的状态机。**全部在内存里**。

    * `background=True`（默认，生产就是这个）：`start()` 会起一个守护线程，
      每 `interval_s` 调一次 `tick()`；`recording` 关掉**且没人看**时它自己退出。
    * `background=False`：**不起线程**，`tick()` 由调用方驱动 ——
      用例靠它把"什么时候采"变成确定的（没有 sleep、没有超时抖动）。
    """

    def __init__(self, *, sampler: Optional[Any] = None,
                 server_sampler: Optional[Callable[[], Dict[str, Any]]] = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 window_s: float = DEFAULT_WINDOW_S,
                 viewer_ttl_s: float = VIEWER_TTL_S, background: bool = True,
                 clock: Callable[[], float] = time.time):
        self.sampler = sampler if sampler is not None else HostSampler()
        self._server_sampler = server_sampler
        self.interval_s = max(0.05, float(interval_s))
        self.window_s = max(self.interval_s, float(window_s))
        self.viewer_ttl_s = max(0.0, float(viewer_ttl_s))
        self.background = bool(background)
        self._clock = clock
        self._ring = RingBuffer(ring_capacity(self.window_s, self.interval_s))
        self.recording = False
        self.total_sampled = 0
        self.last_sample_at = 0.0
        self.last_error = ""
        self._last_view = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # ---- 窗口 ----

    def set_window(self, window_s: float) -> Dict[str, Any]:
        """换窗口（秒）→ 换环形容量。**旧点直接丢**（宁可空图，不要半截历史）。

        序号接着往下数（`RingBuffer(last_seq=...)`）：客户端手上那个 `since`
        不会因为一次换窗口而"卡住"或"重看旧点"。
        """
        want = float(window_s)
        if want < MIN_WINDOW_S or want > MAX_WINDOW_S:
            raise ValueError("windowSeconds 要在 %d ~ %d 秒之间"
                             % (int(MIN_WINDOW_S), int(MAX_WINDOW_S)))
        with self._lock:
            self.window_s = max(self.interval_s, want)
            self._ring = RingBuffer(ring_capacity(self.window_s, self.interval_s),
                                    last_seq=self._ring.last_seq)
        return self.state()

    # ---- 开始 / 停止 ----

    def start(self, window_s: Optional[float] = None, now: Optional[float] = None) -> Dict[str, Any]:
        """开始记录。**按按钮的人就在看**，所以顺手刷新一次"有人看"的时间戳。"""
        when = self._now(now)
        if window_s is not None:
            self.set_window(window_s)
        with self._lock:
            self.recording = True
            self._last_view = when
        self._ensure_thread()
        return self.state(when)

    def stop(self, now: Optional[float] = None) -> Dict[str, Any]:
        """停止记录。**不 join 线程**：它下一次醒来（最多一个间隔）自己退出
        （这样这个调用不会在请求线程里等 1 秒）。要确定性收尾就用 `shutdown()`。
        """
        with self._lock:
            self.recording = False
        return self.state(now)

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """停掉采集线程并等它退出（进程收尾 / 用例收尾用）。"""
        with self._lock:
            self.recording = False
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout_s)))

    # ---- 采集 ----

    def should_sample(self, now: Optional[float] = None) -> bool:
        """**已开始记录 且 有人在看** —— 两个条件缺一不可。"""
        when = self._now(now)
        with self._lock:
            return bool(self.recording) and (when - self._last_view) <= self.viewer_ttl_s

    def tick(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """采一个点（如果该采）。返回那个点，否则 None。**测试直接调它**。"""
        when = self._now(now)
        if not self.should_sample(when):
            return None
        host: Dict[str, Any] = {}
        try:
            host = dict(self.sampler.sample() or {})
        except Exception as exc:                       # 采集失败不许把线程带走
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
        server: Dict[str, Any] = {}
        if self._server_sampler is not None:
            try:
                server = dict(self._server_sampler() or {})
            except Exception as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
        note = str(getattr(self.sampler, "last_error", "") or "")
        if note:
            self.last_error = note
        point: Dict[str, Any] = {"t": round(when, 3)}
        for key in HOST_KEYS:
            point[key] = host.get(key)
        for key in SERVER_KEYS:
            point[key] = server.get(key)
        with self._lock:
            row = self._ring.add(point)
            self.total_sampled += 1
            self.last_sample_at = when
        return row

    def touch(self, now: Optional[float] = None) -> None:
        """「有人在看」：`state` / `points` 都算。顺带把采集线程拉起来（如果正在记录）。"""
        when = self._now(now)
        with self._lock:
            self._last_view = when
            wanted = bool(self.recording)
        if wanted:
            self._ensure_thread()

    # ---- 读 ----

    def state(self, now: Optional[float] = None) -> Dict[str, Any]:
        """当前状态。**先算 `sampling` 再 touch** —— 否则这个字段永远是 true。"""
        when = self._now(now)
        with self._lock:
            sampling = bool(self.recording) and (when - self._last_view) <= self.viewer_ttl_s
            out = {
                "ok": True,
                "recording": bool(self.recording),
                "sampling": sampling,
                "intervalSeconds": round(self.interval_s, 3),
                "windowSeconds": round(self.window_s, 1),
                "capacity": self._ring.capacity,
                "samples": len(self._ring),
                "totalSampled": int(self.total_sampled),
                "dropped": self._ring.dropped,
                "firstSeq": self._ring.first_seq,
                "lastSeq": self._ring.last_seq,
                "lastSampleAt": self.last_sample_at or None,
                "viewerTtlSeconds": round(self.viewer_ttl_s, 1),
                "samplerError": self.last_error or "",
            }
        self.touch(when)
        return out

    def points(self, since: Any = "", limit: Optional[int] = None,
               now: Optional[float] = None) -> Dict[str, Any]:
        """**增量取点**。`since` 见 `parse_since`；返回里带"下一批从哪取"（`nextSince`）。

        `since` 为空 → 取**最近** `limit` 个（页面首屏要的是"现在长什么样"，
        不是"十五分钟前长什么样"）。
        """
        when = self._now(now)
        cap = int(limit) if limit else DEFAULT_PAGE
        cap = max(1, min(cap, MAX_PAGE))
        with self._lock:
            mode, value = parse_since(since)
            if mode is None:
                rows = self._ring.tail(cap)
                truncated = len(self._ring) > len(rows)
            elif mode == "seq":
                rows = self._ring.after_seq(int(value), cap + 1)
                truncated = len(rows) > cap
                rows = rows[:cap]
            else:
                rows = self._ring.after_t(float(value), cap + 1)
                truncated = len(rows) > cap
                rows = rows[:cap]
            out = self.state(when)
            out.update({
                "since": "" if since is None else str(since),
                "series": rows,
                "count": len(rows),
                "pageLimit": cap,
                "truncated": bool(truncated),
                # 下一批从哪取：回空了就停在最后那个序号上（下次不会重看）
                "nextSince": int(rows[-1]["seq"]) if rows else self._ring.last_seq,
            })
            return out

    # ---- 线程 ----

    @property
    def thread_alive(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _ensure_thread(self) -> None:
        if not self.background:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._loop, name="echo-perfmon", daemon=True)
            self._thread = thread
        thread.start()

    def _loop(self) -> None:
        """每 `interval_s` 采一次；**一旦"记录中 + 有人看"不再成立就退出**。

        退出而不是"留着空转"是有意的：页面关掉之后这个线程最多再活一个 `interval_s`
        （`viewer_ttl_s` 是采样闸门，不是线程寿命）。页面再打开时 `touch()`
        （`state`/`points` 都会调）会把它重新拉起来。
        """
        try:
            while not self._stop.wait(self.interval_s):
                try:
                    self.tick()
                except Exception:                       # pragma: no cover - 兜底
                    pass
                if not self.should_sample():
                    break
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _now(self, now: Optional[float] = None) -> float:
        return float(self._clock() if now is None else now)
