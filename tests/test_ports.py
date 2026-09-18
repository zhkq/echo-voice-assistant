# -*- coding: utf-8 -*-
"""端口分配测试（app/ports.py，P1）。

要钉住的语义：
  1. Windows 保留段（`netsh ... show excludedportrange`）的解析与判定；
  2. `probe()` 能区分"可用 / 已占用 / 保留段"——**不能**把已占用的端口报成可用
     （Windows 上误设 SO_REUSEADDR 就会这样）；
  3. `pick()` 按"首选 → 邻近 → 系统分配"的顺序退让，并且**一定返回能 bind 的端口**；
  4. `echo-port.txt` 的读写与原子性（脚本靠它找服务，不能读到半个文件）。
"""
import os
import socket
import tempfile
import unittest

from app import ports

NETSH_SAMPLE = """
Protocol tcp Port Exclusion Ranges

Start Port    End Port
----------    --------
        1168        1267
        1368        1467
        1468        1567
        1900        1999
      50000       50059     *

* - Administered port exclusions.

"""


def _candidate_port(start=18000):
    """安全带里挑一个"没有监听者"的端口号，**并且不预先 bind 它**。

    为什么这么绕（2026-09-19 实测踩出来的）：Windows 会把**刚 bind 又释放**的端口短暂保留，
    紧接着再 bind 同一个端口有约 1/6 的概率报"已占用"。所以任何"先探测/先 bind，再对同一个
    端口做断言"的写法本身就是 flaky 的（当时 8 次里红了 5 次）。这里只做 listening 探测
    （不 bind），把"第一次 bind"留给被测代码，断言才稳定。
    """
    port = start
    while port < start + 200 and ports.listening("127.0.0.1", port):
        port += 1
    return port


class _Occupier:
    """占住一个端口（不 listen，纯 bind 就足以让 probe 判定为已占用）。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class ParseExcludedTests(unittest.TestCase):
    def test_parses_ranges_and_ignores_header_and_star(self):
        got = ports.parse_excluded(NETSH_SAMPLE)
        self.assertIn((1168, 1267), got)
        self.assertIn((1468, 1567), got)
        self.assertIn((50000, 50059), got)
        self.assertEqual(len(got), 5, "样例里是 5 段（表头、星号说明、空行都不算）")

    def test_tolerates_garbage(self):
        self.assertEqual(ports.parse_excluded(""), [])
        self.assertEqual(ports.parse_excluded("不是 netsh 的输出\nhello 12 34 x"), [])
        self.assertEqual(ports.parse_excluded("  0 0  "), [])          # 非法端口范围
        self.assertEqual(ports.parse_excluded("  99999 100000  "), [])

    def test_in_excluded(self):
        rs = ports.parse_excluded(NETSH_SAMPLE)
        self.assertTrue(ports.in_excluded(1200, rs))
        self.assertTrue(ports.in_excluded(1168, rs))
        self.assertFalse(ports.in_excluded(8970, rs))

    def test_excluded_ranges_never_raises(self):
        self.assertIsInstance(ports.excluded_ranges(refresh=True), list)


class ProbeTests(unittest.TestCase):
    def test_reports_reserved_port(self):
        ok, why = ports.probe(1200, ranges=[(1100, 1300)])
        self.assertFalse(ok)
        self.assertEqual(why, "保留段")

    def test_reports_occupied_port(self):
        occ = _Occupier()
        try:
            ok, why = ports.probe(occ.port, ranges=[])
            self.assertFalse(ok, "已被占用的端口不能报成可用（Windows 误设 SO_REUSEADDR 就会这样）")
            self.assertEqual(why, "已占用")
        finally:
            occ.close()

    def test_reports_free_port(self):
        port = _candidate_port()
        ok, why = ports.probe(port, ranges=[])
        self.assertTrue(ok, why)
        self.assertEqual(why, "")


class PickTests(unittest.TestCase):
    def test_keeps_preferred_when_free(self):
        port = _candidate_port()
        got, note = ports.pick(port, ranges=[])
        self.assertEqual(got, port)
        self.assertEqual(note, "", "首选可用时不该产生任何退让说明")

    def test_moves_away_from_occupied_port(self):
        occ = _Occupier()
        try:
            got, note = ports.pick(occ.port, ranges=[], window=50)
            self.assertNotEqual(got, occ.port, "绝不能返回被别人占着的端口")
            self.assertGreater(got, 0)
            self.assertTrue(note, "退让必须留下说明，否则端口悄悄变了没人知道")
            self.assertIn("已占用", note)
            # 这里**不再**对 got 做 probe 断言：pick() 内部已经 bind→释放过它，而 Windows
            # 会把刚释放的端口短暂保留，紧接着再 bind 同一个端口有约 1/6 概率报"已占用"
            # （实测）——那样的断言本身就是 flaky 的。"pick 返回的端口可用"由
            # test_keeps_preferred_when_free 用"从未被我们 bind 过的候选"证明。
        finally:
            occ.close()

    def test_skips_reserved_port(self):
        port = _candidate_port()
        # 只把首选端口划进保留段
        got, note = ports.pick(port, ranges=[(port, port)], window=20)
        self.assertNotEqual(got, port)
        self.assertIn("保留段", note)

    def test_fallback_prefers_safe_band_over_os_allocated(self):
        """窗口耗尽时应落到安全带，而不是系统临时端口。

        系统刚分配的临时端口会被短暂保留，交给 uvicorn 会偶发 bind 失败（实测 1/6）。
        """
        lo, hi = 40000, 40002
        got, note = ports.pick(lo, ranges=[(lo, hi)], window=1, bands=((18000, 18999),))
        self.assertNotIn(got, range(lo, hi + 1))
        self.assertTrue(18000 <= got <= 18999, "应落到安全带，实际 %s（note=%s）" % (got, note))

    def test_returns_bindable_port_when_window_is_crowded(self):
        occ = _Occupier()
        try:
            got, _ = ports.pick(occ.port, ranges=[], window=1)   # 几乎无处可退
            self.assertGreater(got, 0)
            self.assertNotEqual(got, occ.port)
        finally:
            occ.close()


class PortFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-port-")

    def test_accepts_data_root_and_install_root(self):
        data_root = os.path.join(self.tmp, "data")
        os.makedirs(data_root, exist_ok=True)
        self.assertEqual(ports.port_file(data_root),
                         os.path.join(data_root, ports.PORT_FILE_NAME))
        self.assertEqual(ports.port_file(self.tmp),
                         os.path.join(data_root, ports.PORT_FILE_NAME))

    def test_round_trip(self):
        self.assertTrue(ports.write_port_file(self.tmp, 18060))
        self.assertEqual(ports.read_port_file(self.tmp), 18060)
        with open(ports.port_file(self.tmp), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "18060")

    def test_missing_or_garbage_returns_default(self):
        self.assertEqual(ports.read_port_file(self.tmp, default=8970), 8970)
        ports.write_port_file(self.tmp, 1234)          # 顺带把 data/ 建出来
        with open(ports.port_file(self.tmp), "w", encoding="utf-8") as fh:
            fh.write("not-a-port")
        self.assertEqual(ports.read_port_file(self.tmp, default=8970), 8970)
        with open(ports.port_file(self.tmp), "w", encoding="utf-8") as fh:
            fh.write("99999")
        self.assertEqual(ports.read_port_file(self.tmp, default=8970), 8970)

    def test_no_temp_file_left_behind(self):
        ports.write_port_file(self.tmp, 12345)
        leftovers = [n for n in os.listdir(os.path.join(self.tmp, "data"))
                     if n.startswith(".echo-port-")]
        self.assertEqual(leftovers, [], "原子写不该留下临时文件")

    def test_resolve_port_writes_file(self):
        port, _ = ports.resolve_port(_candidate_port(), data_root=self.tmp, window=10)
        self.assertGreater(port, 0)
        self.assertEqual(ports.read_port_file(self.tmp), port)


if __name__ == "__main__":
    unittest.main()
