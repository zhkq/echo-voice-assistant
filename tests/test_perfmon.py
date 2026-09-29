# -*- coding: utf-8 -*-
"""性能采集器（`server/perfmon.py`）：内存环形窗口、增量取点、注入式采样、**不落盘**。

这个文件钉四件事：

1. **只在"已开始记录且有人在看"时采**：没按开始不采、按了停止不采、
   `viewer_ttl_s` 过了（页面关了）也不采 —— 而且**不常驻空跑**（线程自己退）。
2. **环形窗口**：塞超过窗口的点数后，缓冲长度不超过上限（从最老的开始覆盖）。
3. **不落盘**：① **源码扫描**（模块里没有一处写文件）；② **运行期**把
   `builtins.open` 与 `os.replace/remove/rename/makedirs` 打桩，走一遍
   `start → tick → points → stop`，**一个写调用都不许有**。
4. **不碰真实资源**：`nvidia-smi`（`subprocess.run`）、`psutil`、`/proc`
   **全部注入替身/夹具**。`setUpModule` 把 `subprocess.run` 换成"一调就炸"，
   所以这个文件里哪怕将来有人手滑写了真采集，也是**当场红**而不是
   "在这台开发机上碰巧能跑"（本仓库对"测试碰真实资源"极敏感，见 AGENTS.md）。
"""
import builtins
import io
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import perfmon as perfmon_mod                               # noqa: E402

_PATCHERS = []


def setUpModule():
    """**整个文件里一次真外部命令都不许跑。**"""
    patcher = patch.object(perfmon_mod.subprocess, "run",
                           side_effect=AssertionError("测试里不许真的调外部命令（如 nvidia-smi）"))
    patcher.start()
    _PATCHERS.append(patcher)


def tearDownModule():
    for p in reversed(_PATCHERS):
        p.stop()
    _PATCHERS.clear()


def wait_until(pred, timeout_s=5.0, step=0.01):
    """等一个条件成立（线程那两条用例要用）。超时返回 False，**不抛**。"""
    end = time.time() + float(timeout_s)
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class _FakeHost:
    """假主机采集器：计数 + 固定/递增的假数据。**不碰任何真实资源。**"""

    def __init__(self, values=(10.0, 20.0, 30.0, 40.0)):
        self.calls = 0
        self.last_error = ""
        self.values = list(values)

    def sample(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return {"gpuPercent": value, "gpuMemUsedMb": value * 10.0, "gpuMemTotalMb": 24564.0,
                "gpuTempC": 61.0, "gpuPowerW": 180.0, "cpuPercent": value,
                "memPercent": 50.0, "memUsedMb": 8192.0, "memTotalMb": 16384.0}


def _monitor(host=None, **kw):
    """一个**不起线程**的采集器（用例自己驱动 `tick()`，于是没有 sleep 抖动）。"""
    kw.setdefault("interval_s", 1.0)
    kw.setdefault("window_s", 60.0)
    kw.setdefault("viewer_ttl_s", 10.0)
    kw.setdefault("background", False)
    kw.setdefault("server_sampler",
                  lambda: {"active": 1, "maxConcurrent": 2,
                           "vramUsedMb": 5123.0, "vramBudgetMb": 20480.0})
    return perfmon_mod.PerfMonitor(sampler=host or _FakeHost(), **kw)


def _pump(monitor, count, start=100.0, step=1.0):
    """像**页面在轮询**那样驱动：每次都先 `touch()` 再 `tick()`。

    这不是为了迁就用例，而是真实情形：采样本来就只在"有人在看"时发生，
    所以连采几十个点的用例必须先假装有个页面在取点（否则 `viewer_ttl_s` 一过就停）。
    """
    for i in range(count):
        when = start + i * step
        monitor.touch(when)
        monitor.tick(when)
    return start + (count - 1) * step


# ---------------------------------------------------------------- 纯函数

class ParseSinceTests(unittest.TestCase):
    """`since` 既收**序号**也收**时间戳**（用户要的"增量"，同时给一条时间坐标）。"""

    def test_empty_means_no_lower_bound(self):
        self.assertEqual(perfmon_mod.parse_since(""), (None, None))
        self.assertEqual(perfmon_mod.parse_since(None), (None, None))
        self.assertEqual(perfmon_mod.parse_since(0), (None, None))

    def test_a_small_number_is_a_sequence_number(self):
        self.assertEqual(perfmon_mod.parse_since("12"), ("seq", 12))
        self.assertEqual(perfmon_mod.parse_since("0.9"), ("seq", 0))

    def test_a_big_number_is_a_timestamp(self):
        self.assertEqual(perfmon_mod.parse_since("1700000000"), ("t", 1700000000.0))

    def test_garbage_is_an_error_not_a_silent_full_reload(self):
        """打错的 `since` 要**当场报错** —— 静默当成"从头来"会让每次轮询都变成全量重拉。"""
        for bad in ("abc", "1,2", "1.2.3"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    perfmon_mod.parse_since(bad)


class ParseGpuTests(unittest.TestCase):
    def test_a_normal_row(self):
        out = perfmon_mod.parse_gpu_csv("42, 3456, 24564, 61, 180.34\n")
        self.assertEqual(out["gpuPercent"], 42.0)
        self.assertEqual(out["gpuMemUsedMb"], 3456.0)
        self.assertEqual(out["gpuMemTotalMb"], 24564.0)
        self.assertEqual(out["gpuTempC"], 61.0)
        self.assertAlmostEqual(out["gpuPowerW"], 180.34)

    def test_units_are_tolerated(self):
        out = perfmon_mod.parse_gpu_csv("42, 3456 MiB, 24564 MiB, 61, 180.34")
        self.assertEqual(out["gpuMemUsedMb"], 3456.0)
        self.assertEqual(out["gpuMemTotalMb"], 24564.0)
        self.assertAlmostEqual(out["gpuPowerW"], 180.34)

    def test_not_available_is_none_not_zero(self):
        """`[N/A]`（笔记本/权限不足）**不许变成 0** —— 0% 与"读不到"是两件事。"""
        out = perfmon_mod.parse_gpu_csv("[N/A], [N/A], [N/A], [N/A], [N/A]")
        for key in ("gpuPercent", "gpuMemUsedMb", "gpuMemTotalMb", "gpuTempC", "gpuPowerW"):
            self.assertIsNone(out[key], key)

    def test_empty_output_is_an_empty_dict(self):
        self.assertEqual(perfmon_mod.parse_gpu_csv(""), {})
        self.assertEqual(perfmon_mod.parse_gpu_csv("\n\n"), {})


class ParseProcTests(unittest.TestCase):
    """没有 psutil 那条路：`/proc/stat` 与 `/proc/meminfo` 自己算。"""

    def test_proc_stat_sums_all_fields(self):
        self.assertEqual(perfmon_mod.parse_proc_stat(
            "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\nintr 7\n"),
            (800.0, 1000.0))

    def test_proc_stat_garbage_is_none(self):
        self.assertIsNone(perfmon_mod.parse_proc_stat("nonsense\n"))
        self.assertIsNone(perfmon_mod.parse_proc_stat("cpu a b c d"))

    def test_meminfo_uses_mem_available(self):
        """用 `MemAvailable` 而不是 `MemFree` —— 后者会把 page cache 算成已用。"""
        out = perfmon_mod.parse_proc_meminfo(
            "MemTotal:       16384000 kB\nMemFree:         1000000 kB\n"
            "MemAvailable:    8192000 kB\nBuffers:          100000 kB\n")
        self.assertEqual(out["memTotalMb"], 16000.0)
        self.assertEqual(out["memUsedMb"], 8000.0)
        self.assertEqual(out["memPercent"], 50.0)

    def test_meminfo_falls_back_to_mem_free(self):
        out = perfmon_mod.parse_proc_meminfo("MemTotal: 1024000 kB\nMemFree: 512000 kB\n")
        self.assertEqual(out["memUsedMb"], 500.0)

    def test_meminfo_garbage_is_empty(self):
        self.assertEqual(perfmon_mod.parse_proc_meminfo(""), {})
        self.assertEqual(perfmon_mod.parse_proc_meminfo("no colon here"), {})


# ---------------------------------------------------------------- 注入式采集

class _FakePsutil:
    def __init__(self, cpu=17.5):
        self.cpu = cpu
        self.cpu_calls = 0

    def cpu_percent(self, interval=None):
        self.cpu_calls += 1
        return self.cpu

    def virtual_memory(self):
        return types.SimpleNamespace(percent=63.2, used=10 * 1048576 * 1024,
                                     total=16 * 1048576 * 1024)


class HostSamplerTests(unittest.TestCase):
    """**外部命令与 psutil 都是构造参数** —— 用例里一个真资源都不碰。"""

    def test_the_gpu_command_is_the_documented_one(self):
        seen = []

        def runner(argv, timeout):
            seen.append(list(argv))
            return "1, 2, 3, 4, 5\n"

        sampler = perfmon_mod.HostSampler(runner=runner, psutil_mod=False)
        out = sampler.gpu()                       # 只看 GPU 那一路（不碰真的 /proc）
        self.assertEqual(seen, [[sampler.nvidia_smi,
                                 "--query-gpu=" + ",".join(perfmon_mod.GPU_QUERY),
                                 "--format=csv,noheader,nounits"]])
        self.assertEqual(out["gpuPercent"], 1.0)

    def test_psutil_is_used_when_present(self):
        fake = _FakePsutil()
        sampler = perfmon_mod.HostSampler(runner=lambda *a: "", psutil_mod=fake)
        out = sampler.sample()
        self.assertEqual(fake.cpu_calls, 1)
        self.assertEqual(out["cpuPercent"], 17.5)
        self.assertEqual(out["memPercent"], 63.2)
        self.assertEqual(out["memUsedMb"], 10240.0)

    def test_without_psutil_cpu_and_memory_come_from_proc(self):
        with tempfile.TemporaryDirectory(prefix="echo-proc-") as root:
            with open(os.path.join(root, "stat"), "w", encoding="utf-8") as fh:
                fh.write("cpu  100 0 100 800 0 0 0 0 0 0\n")
            with open(os.path.join(root, "meminfo"), "w", encoding="utf-8") as fh:
                fh.write("MemTotal: 16384000 kB\nMemAvailable: 8192000 kB\n")
            sampler = perfmon_mod.HostSampler(runner=lambda *a: "", psutil_mod=False,
                                              proc_root=root)
            first = sampler.sample()
            self.assertIsNone(first["cpuPercent"], "第一次没有上一次，算不出差值就该是 None")
            self.assertEqual(first["memPercent"], 50.0)
            with open(os.path.join(root, "stat"), "w", encoding="utf-8") as fh:
                fh.write("cpu  200 0 200 1400 0 0 0 0 0 0\n")
            second = sampler.sample()
            self.assertEqual(second["cpuPercent"], 25.0)     # 600/800 空转 → 25% 忙

    def test_one_broken_part_does_not_take_the_others_down(self):
        """GPU 读不到（容器没挂显卡）不该让 CPU/内存也没有。"""
        def boom(*a):
            raise RuntimeError("nvidia-smi 不在")

        sampler = perfmon_mod.HostSampler(runner=boom, psutil_mod=_FakePsutil())
        out = sampler.sample()
        self.assertIsNone(out["gpuPercent"])
        self.assertEqual(out["cpuPercent"], 17.5)
        self.assertIn("nvidia-smi", sampler.last_error)

    def test_the_real_command_runner_is_blocked_in_this_file(self):
        """给上面那条 `setUpModule` 打桩一个正面的断言（证明护栏真的开着）。"""
        with self.assertRaises(AssertionError):
            perfmon_mod.run_command(["nvidia-smi", "--query-gpu=utilization.gpu"])


class _BoomState:
    """`state` 上缺 `admission`/`pool`（或它们炸了）时，后端自报那两个数就是 None。"""


class ServerSamplerTests(unittest.TestCase):
    def test_it_reads_the_same_numbers_as_health(self):
        state = types.SimpleNamespace(
            admission=types.SimpleNamespace(snapshot=lambda: {"active": 2, "maxConcurrent": 2}),
            pool=types.SimpleNamespace(
                vram=lambda: {"usedMb": 5123, "budgetMb": 20480},
                status=lambda: [{"state": "ready"}, {"state": "ready"}, {"state": "absent"}]))
        out = perfmon_mod.server_sampler_of(state)()
        self.assertEqual(out["active"], 2.0)
        self.assertEqual(out["vramUsedMb"], 5123.0)
        self.assertEqual(out["vramBudgetMb"], 20480.0)
        self.assertEqual(out["modelsReady"], 2.0)
        self.assertEqual(out["modelsTotal"], 3.0)

    def test_a_broken_state_gives_none_not_an_exception(self):
        out = perfmon_mod.server_sampler_of(_BoomState())()
        self.assertEqual(out, {})


# ---------------------------------------------------------------- 窗口与状态机

class RingWindowTests(unittest.TestCase):
    def test_capacity_comes_from_the_window(self):
        self.assertEqual(perfmon_mod.ring_capacity(900.0, 1.0), 900)
        self.assertEqual(perfmon_mod.ring_capacity(60.0, 1.0), 60)

    def test_the_buffer_never_grows_past_its_cap(self):
        """**环形覆盖**：塞 20 个点，窗口 5 → 只剩最后 5 个，序号接着数。"""
        ring = perfmon_mod.RingBuffer(perfmon_mod.ring_capacity(5.0, 1.0))
        for i in range(20):
            ring.add({"t": float(i)})
        self.assertEqual(ring.capacity, 5)
        self.assertEqual(len(ring), 5)
        self.assertEqual(ring.first_seq, 16)
        self.assertEqual(ring.last_seq, 20)
        self.assertEqual(ring.dropped, 15)
        self.assertEqual([r["t"] for r in ring.tail(5)], [15.0, 16.0, 17.0, 18.0, 19.0])

    def test_changing_the_window_keeps_the_sequence_monotonic(self):
        """换窗口丢掉旧点，但**序号不回退** —— 客户端手上那个 `since` 才不会失效。"""
        monitor = _monitor(window_s=60.0)
        monitor.start()
        for i in range(3):
            monitor.tick(now=100.0 + i)
        before = monitor.state(now=200.0)["lastSeq"]
        monitor.set_window(120.0)
        self.assertEqual(monitor.state(now=200.0)["samples"], 0)
        self.assertEqual(monitor.state(now=200.0)["lastSeq"], before)
        monitor.tick(now=201.0)
        self.assertEqual(monitor.state(now=201.0)["lastSeq"], before + 1)

    def test_the_window_is_bounded(self):
        monitor = _monitor()
        with self.assertRaises(ValueError):
            monitor.set_window(10.0)
        with self.assertRaises(ValueError):
            monitor.set_window(99999.0)


class SamplingGateTests(unittest.TestCase):
    """**已开始记录 且 有人在看** —— 两个条件缺一不可。"""

    def test_nothing_is_sampled_before_start(self):
        host = _FakeHost()
        monitor = _monitor(host)
        self.assertIsNone(monitor.tick(now=100.0))
        self.assertEqual(host.calls, 0)
        self.assertEqual(monitor.state(now=100.0)["samples"], 0)

    def test_start_then_tick_calls_the_sampler(self):
        host = _FakeHost()
        monitor = _monitor(host)
        state = monitor.start(now=100.0)
        self.assertTrue(state["recording"])
        self.assertTrue(state["sampling"], "按了开始的人就在看")
        monitor.tick(now=100.5)
        monitor.tick(now=101.0)
        self.assertEqual(host.calls, 2)
        point = monitor.points(since="", now=101.5)["series"][-1]
        self.assertEqual(point["gpuPercent"], 20.0)          # 打桩的假数据
        self.assertEqual(point["active"], 1.0)               # 后端自报
        self.assertEqual(point["vramUsedMb"], 5123.0)

    def test_stop_freezes_the_count(self):
        host = _FakeHost()
        monitor = _monitor(host)
        monitor.start(now=100.0)
        for i in range(3):
            monitor.tick(now=100.0 + i)
        monitor.stop(now=200.0)
        for i in range(5):
            self.assertIsNone(monitor.tick(now=200.0 + i))
        self.assertEqual(host.calls, 3)
        self.assertEqual(monitor.points(since="", now=210.0)["samples"], 3)

    def test_nobody_watching_means_no_sampling(self):
        """页面关掉（没人取点）→ `viewer_ttl_s` 一过就**不再采**，但"记录中"留着。"""
        host = _FakeHost()
        monitor = _monitor(host, viewer_ttl_s=10.0)
        monitor.start(now=100.0)
        monitor.tick(now=100.0)
        self.assertEqual(host.calls, 1)
        self.assertIsNone(monitor.tick(now=111.0), "超过 viewer_ttl 就不该再采")
        self.assertEqual(host.calls, 1)
        self.assertTrue(monitor.state(now=112.0)["recording"], "记录状态要留着")
        # 有人回来取点（state/points 都算）→ 立刻接着采
        monitor.tick(now=112.0)
        self.assertEqual(host.calls, 2)

    def test_a_failing_sampler_is_recorded_not_raised(self):
        class Boom:
            last_error = ""

            def sample(self):
                raise RuntimeError("显卡炸了")

        host = Boom()
        monitor = _monitor(host)
        monitor.start(now=100.0)
        point = monitor.tick(now=100.0)
        self.assertIsNotNone(point, "采不到也要留一个点（图上是断点，而不是「没这回事」）")
        self.assertIsNone(point["gpuPercent"])
        self.assertIn("显卡炸了", monitor.state(now=100.0)["samplerError"])


class IncrementalPointsTests(unittest.TestCase):
    def test_since_gives_only_the_new_points(self):
        monitor = _monitor()
        monitor.start(now=100.0)
        for i in range(5):
            monitor.tick(now=100.0 + i)
        head = monitor.points(since="", now=200.0)
        self.assertEqual([p["seq"] for p in head["series"]], [1, 2, 3, 4, 5])
        self.assertEqual(head["nextSince"], 5)
        empty = monitor.points(since=head["nextSince"], now=200.0)
        self.assertEqual(empty["series"], [])
        self.assertEqual(empty["nextSince"], 5, "空增量下 nextSince 不许回退")
        monitor.tick(now=205.0)
        monitor.tick(now=206.0)
        inc = monitor.points(since=5, now=207.0)
        self.assertEqual([p["seq"] for p in inc["series"]], [6, 7])
        self.assertEqual(inc["nextSince"], 7)

    def test_since_also_takes_a_timestamp(self):
        """同一个参数也能给 unix 时间戳（≥1e9 就按时间解释）。"""
        base = 1700000000.0
        monitor = _monitor()
        monitor.start(now=base)
        for i in range(4):
            monitor.touch(base + i)
            monitor.tick(now=base + i)
        got = monitor.points(since=base + 1.5, now=base + 10)
        self.assertEqual([p["t"] for p in got["series"]], [base + 2, base + 3])

    def test_an_empty_since_returns_the_newest_not_the_oldest(self):
        """页面刚打开要的是"现在长什么样"，不是"十五分钟前长什么样"。"""
        monitor = _monitor(window_s=60.0)
        monitor.start(now=0.0)
        _pump(monitor, 30, start=1.0)
        got = monitor.points(since="", limit=5, now=200.0)
        self.assertEqual([p["seq"] for p in got["series"]], [26, 27, 28, 29, 30])
        self.assertTrue(got["truncated"])
        self.assertEqual(got["pageLimit"], 5)

    def test_one_response_is_capped(self):
        """**不要每次全量返回**：一批最多 pageLimit 个（上限 MAX_PAGE）。"""
        monitor = _monitor(window_s=900.0)
        monitor.start(now=0.0)
        _pump(monitor, 50, start=1.0)
        # 增量的那一条：since=5 时还有 45 个待取，但一次只回 10 个
        inc = monitor.points(since=5, limit=10, now=200.0)
        self.assertEqual([p["seq"] for p in inc["series"]], list(range(6, 16)))
        self.assertTrue(inc["truncated"])
        self.assertEqual(inc["nextSince"], 15)
        # 首屏那一条（since 为空 = 取最近）：同样受 pageLimit 约束
        head = monitor.points(since="", limit=10, now=200.0)
        self.assertEqual([p["seq"] for p in head["series"]], list(range(41, 51)))
        self.assertEqual(head["nextSince"], 50)
        huge = monitor.points(since="", limit=10 ** 6, now=200.0)
        self.assertEqual(huge["pageLimit"], perfmon_mod.MAX_PAGE)
        self.assertEqual(huge["count"], 50)


class NoPersistenceTests(unittest.TestCase):
    """**"不用持久化"是需求，也是环境约束**（容器根文件系统只读）。

    两条判据：源码里没有写文件的调用；运行期把写入口全打桩，走一遍完整流程，
    **一个写调用都不许有**。
    """

    FORBIDDEN = ("open(", "os.replace", "os.rename", "os.remove", "os.unlink",
                 "makedirs", "mkstemp", "NamedTemporaryFile", "tempfile", "shutil.",
                 "write_text", "write_bytes", "json.dump", "sqlite3", "pickle")

    def test_the_module_source_has_no_file_writing_calls(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "server", "perfmon.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        # `with open(path, "r" ...)` 那条只读读 `/proc` 的例外要**显式**留着
        body = src.replace('with open(path, "r", encoding="utf-8", errors="replace") as fh:',
                           "")
        hits = [token for token in self.FORBIDDEN if token in body]
        self.assertEqual(hits, [], "采集器里出现了写文件/落库的调用：%s" % hits)

    def test_a_full_cycle_makes_no_write_call(self):
        writes = []
        real_open = builtins.open

        def spy_open(file, mode="r", *args, **kw):
            if any(ch in str(mode) for ch in "wax+"):
                writes.append(("open", str(file), str(mode)))
            return real_open(file, mode, *args, **kw)

        def spy(name):
            def fn(*args, **kw):
                writes.append((name, str(args[:1]), ""))
                raise AssertionError("采集器不该调用 %s" % name)
            return fn

        monitor = _monitor()
        with patch.object(builtins, "open", spy_open), \
                patch.object(os, "replace", spy("os.replace")), \
                patch.object(os, "remove", spy("os.remove")), \
                patch.object(os, "rename", spy("os.rename")), \
                patch.object(os, "makedirs", spy("os.makedirs")):
            monitor.start(now=100.0)
            for i in range(5):
                monitor.tick(now=100.0 + i)
            monitor.points(since="", now=200.0)
            monitor.points(since=3, now=200.0)
            monitor.state(now=200.0)
            monitor.stop(now=300.0)
        self.assertEqual(writes, [], "一次完整流程里出现了写文件调用：%s" % writes)


class BackgroundThreadTests(unittest.TestCase):
    """真实线程那一半：**自己采，没人看/停了就自己退**（不常驻空跑）。"""

    def test_the_thread_samples_then_exits_on_stop(self):
        host = _FakeHost(values=(1.0,))
        monitor = perfmon_mod.PerfMonitor(sampler=host, background=True,
                                          interval_s=0.02, window_s=60.0, viewer_ttl_s=5.0)
        self.addCleanup(monitor.shutdown)
        monitor.start()
        self.assertTrue(wait_until(lambda: monitor.state()["samples"] >= 3),
                        "采集线程起来了却没采到点")
        self.assertTrue(monitor.thread_alive)
        monitor.stop()
        self.assertTrue(wait_until(lambda: not monitor.thread_alive, timeout_s=5.0),
                        "停止记录之后线程还在跑（那就是常驻空跑）")
        frozen = monitor.state()["samples"]
        time.sleep(0.2)
        self.assertEqual(monitor.state()["samples"], frozen, "停了还在长点")

    def test_a_page_that_goes_away_lets_the_thread_exit(self):
        """页面关掉（不再取点）→ 线程**自己退**；再有人取点时又起来。"""
        host = _FakeHost(values=(1.0,))
        monitor = perfmon_mod.PerfMonitor(sampler=host, background=True,
                                          interval_s=0.02, window_s=60.0, viewer_ttl_s=0.1)
        self.addCleanup(monitor.shutdown)
        monitor.start()
        self.assertTrue(wait_until(lambda: not monitor.thread_alive, timeout_s=5.0),
                        "没人看之后线程没退")
        self.assertTrue(monitor.state()["recording"], "记录状态要留着")
        monitor.touch()
        self.assertTrue(wait_until(lambda: monitor.thread_alive, timeout_s=5.0),
                        "有人回来取点了，线程该起来")

    def test_a_foreground_monitor_never_starts_a_thread(self):
        """`background=False`（用例用的那种）**不起线程** —— 采不采完全由调用方决定。"""
        monitor = _monitor()
        monitor.start(now=100.0)
        self.assertFalse(monitor.thread_alive)


if __name__ == "__main__":
    unittest.main()
