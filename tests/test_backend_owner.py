# -*- coding: utf-8 -*-
"""后端**归属与接管**的判据测试（2026-10-06，用户报的 bug）。

背景（真实事故）：开发版与稳定版轮流跑、**共用 8900/8901**，但切实例时后端不会跟着换。
于是新树起来后端口上跑的还是旧树的后端 —— 现象是面板「待启动」、点 ↗ 能进管理页
（那是**别人的**管理面，凭据同源所以能登录）、而会议转写拿 `unauthorized`
（**不是连不上，是那台后端不认这个客户端凭据**）。

本文件盯三件事：
  1. **归属判据**：从命令行 ``-m server.main --config <目录>`` 认出这个后端属于哪棵树
     —— 这是唯一可靠的外部判据（用户 AGENTS.md 那条 + 桌面工具包 ``Get-BackendPid``）；
  2. **只停该停的**：接管只停"命令行证明是 ECHO 后端、且目录属于**别的**树"的那个，
     不明进程一律不动（``backend_pid`` 开头那条铁律）；
  3. **配对不重做**：接管后走的是"已配对到本机回环地址 → 跳过"，不新建客户端。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import backend_admin, backend_proc        # noqa: E402


class CmdlineTests(unittest.TestCase):
    """命令行 → 后端目录。"""

    def test_parses_the_config_switch(self):
        # 注意：`_backend_root_from_cmdline` 用 `os.path.dirname(abspath(...))`，
        # 所以**路径风格必须与当前平台一致**才谈得上期望值（在 Windows 上给一个
        # `/opt/...` 会被补成 `C:\opt\...`）。所以按平台分别验。
        if os.name == "nt":
            cases = [
                (r"C:\x\runtime\python.exe -m server.main --config C:\x\backend\server.yaml --log-level info",
                 r"C:\x\backend"),
                (r'python -m server.main --config "C:\Program Files\e\backend\server.yaml"',
                 r"C:\Program Files\e\backend"),
            ]
        else:
            cases = [
                ("/opt/e/runtime/bin/python -m server.main --config /opt/e/backend/server.yaml",
                 "/opt/e/backend"),
                ("python -m server.main --config '/opt/my e/backend/server.yaml'",
                 "/opt/my e/backend"),
            ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(backend_proc._backend_root_from_cmdline(text),
                                 os.path.normpath(want))

    def test_quoted_paths_work_on_every_platform(self):
        """带引号的路径（含空格）要能被剥掉引号 —— 两种引号都认。"""
        p = os.path.join(os.path.abspath(os.sep), "tmp", "my dir", "backend", "server.yaml")
        for quote in ('"', "'"):
            cmd = "python -m server.main --config %s%s%s" % (quote, p, quote)
            with self.subTest(quote=quote):
                self.assertEqual(backend_proc._backend_root_from_cmdline(cmd),
                                 os.path.dirname(p))

    def test_refuses_anything_that_is_not_an_echo_backend(self):
        """不是后端 / 说不出来 → 空串。**不许猜**（猜一个来停就是打错进程）。"""
        for text in ("", "notepad.exe", "python -m http.server 8900",
                     "node dsh.js web", "python -m server.main", None):
            with self.subTest(text=text):
                self.assertEqual(backend_proc._backend_root_from_cmdline(text or ""), "")


class OwnerTests(unittest.TestCase):
    """`backend_owner()`：占用端口的后端属于哪棵树。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-owner-")
        self.mine = os.path.join(self.tmp, "backend")
        os.makedirs(self.mine, exist_ok=True)
        for p in (mock.patch.object(backend_proc, "backend_root", lambda: self.mine),
                  mock.patch.object(backend_proc, "port_of_interest", lambda: 8900)):
            p.start()
            self.addCleanup(p.stop)

    def _owner(self, pid, cmdline):
        with mock.patch("app.platform.process_command_line", lambda _pid: cmdline), \
                mock.patch.object(backend_proc, "port_owner",
                                  lambda port, with_root=False: {"port": port, "pid": pid,
                                                                 "label": "python.exe"}):
            return backend_proc.backend_owner({"pid": pid, "label": "python.exe"})

    def test_our_own_backend_is_recognised_as_ours(self):
        cmd = "python -m server.main --config %s" % os.path.join(self.mine, "server.yaml")
        o = self._owner(4242, cmd)
        self.assertTrue(o["is_ours"])
        self.assertFalse(o["foreign"])
        self.assertIn("本棵树", o["note"])

    def test_the_other_trees_backend_is_reported_as_foreign(self):
        other = os.path.join(self.tmp, "other-tree", "backend")
        cmd = "python -m server.main --config %s" % os.path.join(other, "server.yaml")
        o = self._owner(5353, cmd)
        self.assertFalse(o["is_ours"])
        self.assertTrue(o["foreign"], "另一棵树的后端必须被认出来（这正是本次 bug 的现场）")
        self.assertIn("另一棵树", o["note"])

    def test_unreadable_cmdline_is_not_guessed(self):
        o = self._owner(6363, "")
        self.assertFalse(o["is_ours"])
        self.assertFalse(o["foreign"], "说不出来时**不许**当成别人的（那会导致误杀）")
        self.assertEqual(o["root"], "")

    def test_nobody_listening(self):
        o = backend_proc.backend_owner({"pid": 0})
        self.assertFalse(o["is_ours"])
        self.assertFalse(o["foreign"])


class PortCheckTests(unittest.TestCase):
    """`port_check()` 要把"另一棵树的后端"与"不明进程"分开报。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pc-")
        self.mine = os.path.join(self.tmp, "backend")
        os.makedirs(self.mine, exist_ok=True)
        p = mock.patch.object(backend_proc, "backend_root", lambda: self.mine)
        p.start()
        self.addCleanup(p.stop)
        # 本棵树没有 pid 记录 → ours_alive False
        p2 = mock.patch.object(backend_proc.backend_pid, "is_ours_alive",
                               lambda: (False, "没有 pid 记录"))
        p2.start()
        self.addCleanup(p2.stop)

    def test_foreign_backend_gets_an_actionable_message(self):
        other = os.path.join(self.tmp, "stable", "backend")
        owners = [{"port": 8900, "pid": 111, "label": "python.exe",
                   "root": other, "foreign": True, "is_ours": False}]
        ok, detail = backend_proc.port_check((8900, 8901), owners=owners)
        self.assertFalse(ok)
        self.assertIn("另一棵树", detail)
        self.assertIn("接管", detail, "必须给出可操作的下一步，不能只说'被占'")

    def test_unknown_process_keeps_the_old_wording(self):
        owners = [{"port": 8900, "pid": 222, "label": "someapp.exe"}]
        ok, detail = backend_proc.port_check((8900, 8901), owners=owners)
        self.assertFalse(ok)
        self.assertIn("不是 ECHO 起的后端", detail)
        self.assertIn("不会替你停别人的进程", detail)

    def test_free_ports_are_ok(self):
        ok, detail = backend_proc.port_check((8900, 8901),
                                            owners=[{"port": p, "pid": 0, "label": ""}
                                                    for p in (8900, 8901)])
        self.assertTrue(ok)
        self.assertIn("空着", detail)

    def test_our_own_backend_without_a_pid_record_is_claimed_not_blocked(self):
        """**本棵树的后端在跑、只是 pid 记录不在** → 认领它，别当成陌生人挡住。

        2026-10-06 用户实测的现场（原话）：**"后端管理页面能进，语音能转写，
        但是仪表盘的状态显示待启动"**。

        真相是**归属记录活不过 ECHO 那一次进程**：pid 文件写的是"**这个 ECHO 进程**起的
        后端"，ECHO 重启之后端口上那个后端还活着（父进程是旧 ECHO，已成孤儿），
        而新 ECHO 手里没有 pid 记录。于是 `view()["running"]` 是 False → 仪表盘说「待启动」，
        可**能力路由根本不看归属记录**，转写照旧成功 —— 用户看到的就是这个矛盾。
        更糟的是这一类原来落进 `strangers`，于是**点"启动"还会被自己挡住**
        （"这不是 ECHO 起的后端…请先停掉它"），而那明明就是本棵树自己的后端。

        判据：① 放行（否则一定会去起第二个、或把用户引去手工杀进程）；
        ② 消息里说得出"已经在跑"（`backend_setup.start()` 靠这句话幂等放行）；
        ③ **就地认领** —— pid 记录被补上，从这一刻起启停/接管都自洽。
        """
        claimed = []
        p = mock.patch.object(backend_proc.backend_pid, "write_pid",
                              lambda pid: claimed.append(int(pid)) or True)
        p.start()
        self.addCleanup(p.stop)
        owners = [{"port": 8900, "pid": 333, "label": "python.exe",
                   "root": self.mine, "is_ours": True, "foreign": False}]
        ok, detail = backend_proc.port_check((8900, 8901), owners=owners)
        self.assertTrue(ok, "把自己的后端当成占用者挡住了：%s" % detail)
        self.assertIn("已经在跑", detail)
        self.assertEqual(claimed, [333], "没有把归属记录补上 —— 下次还得再认一次")


class TakeOverTests(unittest.TestCase):
    """`take_over()`：只停该停的，然后起自己的。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-takeover-")
        self.mine = os.path.join(self.tmp, "backend")
        os.makedirs(self.mine, exist_ok=True)
        for target, value in ((backend_admin.backend_setup, "backend_root"),
                              (backend_admin.backend_proc, "backend_root")):
            p = mock.patch.object(target, value, lambda: self.mine)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(backend_admin, "ports", lambda: (8900, 8901))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(backend_admin, "_JOB",
                              {"running": False, "ok": None, "message": "", "steps": [],
                               "stage": "", "startedAt": "", "doneAt": ""})
        p.start()
        self.addCleanup(p.stop)

    def _take(self, owners, killed, start_result=(True, "已开始")):
        with mock.patch.object(backend_admin.backend_proc, "port_owner",
                               lambda port, with_root=False: owners.get(
                                   port, {"port": port, "pid": 0, "label": ""})), \
                mock.patch("app.platform.kill_process_tree",
                           side_effect=lambda pid: killed.append(pid) or True), \
                mock.patch.object(backend_admin, "start",
                                  side_effect=lambda **kw: start_result):
            return backend_admin.take_over()

    def test_stops_the_other_trees_backend_then_starts_ours(self):
        other = os.path.join(self.tmp, "stable", "backend")
        owners = {8900: {"port": 8900, "pid": 777, "label": "python.exe",
                         "root": other, "foreign": True, "is_ours": False},
                  8901: {"port": 8901, "pid": 0, "label": ""}}
        killed = []
        ok, msg = self._take(owners, killed)
        self.assertTrue(ok, msg)
        self.assertEqual(killed, [777], "必须停掉另一棵树那个后端")
        self.assertIn("不需", msg)          # 明确告诉用户不用重新配对

    def test_one_backend_on_both_ports_is_named_once(self):
        """同一个后端同时占数据口与管理口时，消息里**只报一次 pid**。

        2026-10-06 真机实测看到过 "pid 31712、pid 31712"：拼接用的是去重前的列表。
        这类重复不致命，但会让用户以为"有两个后端要停"，进而怀疑自己按错了按钮。
        """
        other = os.path.join(self.tmp, "stable", "backend")
        both = {"port": 8900, "pid": 777, "label": "python.exe",
                "root": other, "foreign": True, "is_ours": False}
        owners = {8900: dict(both, port=8900), 8901: dict(both, port=8901)}
        killed = []
        ok, msg = self._take(owners, killed)
        self.assertTrue(ok, msg)
        self.assertEqual(killed, [777], "同一个进程只许被杀一次")
        self.assertEqual(msg.count("pid 777"), 1, "消息里不许出现两次同一个 pid：%s" % msg)

    def test_never_kills_an_unknown_process(self):
        """不认识占用者时**绝不动手** —— 这是 backend_pid 开头那条铁律。"""
        owners = {8900: {"port": 8900, "pid": 888, "label": "someapp.exe"},
                  8901: {"port": 8901, "pid": 0, "label": ""}}
        killed = []
        ok, _msg = self._take(owners, killed)
        self.assertTrue(ok)
        self.assertEqual(killed, [], "不明进程不许被 ECHO 杀掉")

    def test_nothing_to_take_over_is_fine(self):
        owners = {8900: {"port": 8900, "pid": 0, "label": ""},
                  8901: {"port": 8901, "pid": 0, "label": ""}}
        killed = []
        ok, msg = self._take(owners, killed)
        self.assertTrue(ok)
        self.assertEqual(killed, [])
        self.assertIn("直接起", msg)

    def test_reports_when_the_new_backend_cannot_start(self):
        owners = {8900: {"port": 8900, "pid": 999, "label": "python.exe",
                         "root": os.path.join(self.tmp, "other"), "foreign": True},
                  8901: {"port": 8901, "pid": 0, "label": ""}}
        killed = []
        ok, msg = self._take(owners, killed, start_result=(False, "没有运行时"))
        self.assertFalse(ok)
        self.assertIn("没有运行时", msg)
        self.assertEqual(killed, [999])


class UsabilityTests(unittest.TestCase):
    """`usability()` 的 code → 处置建议：**说错原因就会把人引错方向**。"""

    def test_unauthorized_is_not_described_as_unreachable(self):
        note = backend_admin.usability_note({"ready": False, "code": "unauthorized",
                                             "note": "后端凭据不能用"})
        self.assertIn("不认", note)
        self.assertIn("接管", note)
        self.assertNotIn("连不上", note)

    def test_offline_is_about_reachability(self):
        note = backend_admin.usability_note({"ready": False, "code": "offline", "note": ""})
        self.assertIn("连不上", note)

    def test_ready_has_no_note(self):
        self.assertEqual(backend_admin.usability_note({"ready": True, "code": ""}), "")

    def test_unknown_code_falls_back_to_the_detail(self):
        note = backend_admin.usability_note({"ready": False, "code": "whatever",
                                             "note": "原始说明"})
        self.assertEqual(note, "原始说明")


class BackendModeTests(unittest.TestCase):
    """`view()["mode"]`：**配对到的是本机还是网络**（2026-10-07 用户的口径）。

    它决定启动脚本该怎么处理后端：

    * `local`   → 拉起本机后端（take-over）
    * `network` → **只探测**那台服务，**别去动本机进程**

    为什么要一个**显式字段**而不是让脚本自己看 `paired.baseUrl`：判据是"地址是不是回环"，
    而回环有一堆写法（`127.0.0.1` / `localhost` / `[::1]` / 整段 `127.0.0.0/8`）。
    让每个调用方各写一遍 `startswith("http://127.0.0.1")` 迟早漂开 ——
    正确判据只有一处（`netlocal.is_loopback`）。
    """

    def setUp(self):
        # `view()` 会去问端口/进程/配对文件——这里只关心 `mode` 那一行，
        # 把它的输入（`pairing.state`）换成替身，其余照真跑（它们对 mode 无影响）。
        self.tmp = tempfile.mkdtemp(prefix="echo-mode-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _mode(self, paired_state):
        with mock.patch.object(backend_admin.pairing, "state", lambda: paired_state):
            return backend_admin.view().get("mode")

    def test_loopback_pairing_is_local(self):
        for url in ("http://127.0.0.1:8900", "http://localhost:8900",
                    "http://127.0.0.5:8900", "https://[::1]:8900"):
            with self.subTest(url=url):
                self.assertEqual(self._mode({"paired": True, "baseUrl": url}), "local",
                                 "回环地址该判成本机：%s" % url)

    def test_a_backend_on_another_machine_is_network(self):
        for url in ("http://192.168.1.170:8900", "http://10.100.0.24:8900",
                    "https://gpu.example.com:8900"):
            with self.subTest(url=url):
                self.assertEqual(self._mode({"paired": True, "baseUrl": url}), "network",
                                 "别的机器的地址该判成网络：%s" % url)

    def test_not_paired_yet_counts_as_local(self):
        """还没配对时按本机处理 —— 新装的机器就是这样，而此时"该起哪台"的答案
        是"起本机那台"（用户从没表达过要用别人的）。"""
        self.assertEqual(self._mode({"paired": False, "baseUrl": ""}), "local")
        self.assertEqual(self._mode({}), "local")


if __name__ == "__main__":
    unittest.main()
