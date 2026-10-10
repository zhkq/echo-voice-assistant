# -*- coding: utf-8 -*-
"""harness **归属**判据：端口上那个是不是本棵树的（2026-10-10 用户实测）。

现场（两次 500）：切到稳定版之后，ECHO 的 `ensure_running()` 只问 `online()`
—— "43199 有人应答"就算数 —— 于是**复用了 dev 的 harness**。两棵树家目录不同，
工作区/会话 id 全对不上：点「开始回顾」→ `session/create` → `workspace/not-found` → 500。

判据与"后端归属"同一套（`backend_proc.port_owner` + AGENTS.md 铁律）：
pid 记录 → 命令行里的入口路径。**判不出就不拦**（拦错会把正常复用挡掉）。

全部打桩 —— 一个真进程都不碰（这台机器上跑着用户自己的 harness）。
"""
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import harness_proc                                   # noqa: E402

OURS = r"D:\ECHO\dsh\app\node_modules\@deepseek-ai\dsh\lib\bin.js"
THEIRS = r"C:\echo-dev\harness\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js"
NODE = r"C:\some\node.exe"


def _cmd(entry):
    return '"%s" "%s" web --port 43199 --no-open' % (NODE, entry)


class HarnessOwnerTests(unittest.TestCase):
    def _owner(self, *, pid=4242, cmdline=_cmd(OURS), local=OURS, pidfile=0):
        with patch.object(harness_proc, "port", lambda: 43199), \
                patch.object(harness_proc, "local_entry", lambda: local), \
                patch.object(harness_proc, "_load_pid", lambda: pidfile), \
                patch("app.platform.listening_pid", lambda p: pid), \
                patch("app.platform.process_label", lambda p: "node.exe"), \
                patch("app.platform.process_command_line", lambda p: cmdline):
            self.foreign = harness_proc.foreign_owner()
            return harness_proc.owner()

    def test_nobody_listening_is_not_an_error(self):
        got = self._owner(pid=0)
        self.assertEqual(got["pid"], 0)
        self.assertTrue(got["ours"], "没人监听时不该被判成外人的")
        self.assertEqual(self.foreign, {})

    def test_pid_record_wins_over_the_command_line(self):
        """pid 记录对得上 = ECHO 自己拉的那个 —— 最硬的判据（哪怕命令行看不懂）。"""
        got = self._owner(pidfile=4242, cmdline=_cmd(THEIRS))
        self.assertEqual(got["pid"], 4242)
        self.assertTrue(got["ours"], "pid 对得上就不该判成外人的")
        self.assertEqual(self.foreign, {}, "pid 对得上就不是外人")

    def test_our_own_entry_is_ours(self):
        got = self._owner()
        self.assertEqual(got["entry"], OURS)
        self.assertTrue(got["ours"])
        self.assertEqual(self.foreign, {}, "自己那份不该被当成外人")

    def test_another_trees_entry_is_foreign(self):
        """**本次的 bug**：端口上跑的是 dev 的 harness → 必须判成外人的。"""
        got = self._owner(cmdline=_cmd(THEIRS))
        self.assertEqual(got["entry"], THEIRS)
        self.assertFalse(got["ours"], "别的树的入口必须判成外人的")
        self.assertEqual(self.foreign.get("pid"), 4242, "foreign_owner 要把它交出来")

    def test_undecidable_is_not_foreign(self):
        """判不出（npx 起的实例命令行里没有本机路径）→ **不拦**。"""
        for local, cmdline in (("", _cmd(OURS)), (OURS, '"npx" "-y" "@deepseek-ai/dsh" "web"'),
                               ("", "")):
            with self.subTest(local=local, cmdline=cmdline):
                got = self._owner(local=local, cmdline=cmdline)
                self.assertTrue(got["ours"], "判不出时一律不拦：%s" % got)


class EnsureRunningRefusesForeignTests(unittest.TestCase):
    """`ensure_running()` 遇到别人的 harness：**拒绝复用**并说清是谁占着。"""

    def _run(self, foe):
        with patch.object(harness_proc, "online", lambda timeout=1.0: True), \
                patch.object(harness_proc, "port_conflict", lambda: None), \
                patch.object(harness_proc, "foreign_owner", lambda: foe), \
                patch.object(harness_proc, "ensure_node_on_path", lambda: None):
            return harness_proc.ensure_running()

    def test_it_refuses_and_names_the_owner(self):
        ok, msg = self._run({"pid": 7936, "entry": THEIRS})
        self.assertFalse(ok, "不该把别人的 harness 当成'已在运行'")
        self.assertIn("另一棵树", msg)
        self.assertIn("7936", msg, "要说清是谁占着，用户才知道去停哪个")
        self.assertIn("bin.js", msg)

    def test_it_still_reuses_our_own(self):
        ok, msg = self._run({})
        self.assertTrue(ok)
        self.assertIn("已在运行", msg)


class HarnessRpcOfflineRetryTests(unittest.TestCase):
    """连不上 harness 时**自己拉起来再试**；但 401/业务错误不许触发重启。

    为什么（2026-10-10 用户实测两次 500）：`ensure_running()` 只有 boot 与应用智能体设置
    两处调用，回顾/助手/会议/归档谁都不管 harness 在不在跑 → 切树之后点「开始回顾」直接
    "目标计算机积极拒绝" → 500。
    """

    def _agent(self):
        from app.agents.harness_agent import HarnessAgent
        return HarnessAgent()

    def _dsh(self, text="DSH RPC session/create 失败", cause=None):
        from app.agents.dsh_agent import DshError
        try:
            raise DshError(text) from cause
        except DshError as e:
            return e

    def test_connection_failure_starts_the_harness_and_retries(self):
        from app.agents.dsh_agent import DshAgent
        a = self._agent()
        seen = {"rpc": 0, "ensure": 0}
        mk = self._dsh                      # ⚠️ `fake_rpc` 的 self 是 agent，不是 TestCase

        def fake_rpc(self, method, args=None, timeout=15):
            seen["rpc"] += 1
            if seen["rpc"] == 1:
                raise mk("…<urlopen error [WinError 10061] 目标计算机积极拒绝…",
                         urllib.error.URLError("refused"))
            return {"ok": True, "value": {"sessionId": "s1"}}

        def fake_ensure(*args, **kwargs):
            seen["ensure"] += 1
            return True, "已拉起"

        with patch.object(DshAgent, "rpc", fake_rpc), \
                patch.object(harness_proc, "ensure_running", fake_ensure):
            out = a.rpc("session/create", {})

        self.assertEqual(seen["ensure"], 1, "连不上就该把 harness 拉一次")
        self.assertEqual(seen["rpc"], 2, "拉起来之后要重试一次")
        self.assertEqual(out["value"]["sessionId"], "s1")

    def test_a_failed_restart_says_why(self):
        from app.agents.dsh_agent import DshAgent, DshError
        a = self._agent()
        mk = self._dsh

        def fake_rpc(self, method, args=None, timeout=15):
            raise mk("…拒绝…", urllib.error.URLError("refused"))

        with patch.object(DshAgent, "rpc", fake_rpc), \
                patch.object(harness_proc, "ensure_running",
                             lambda *a_, **k: (False, "43199 上是另一棵树的 harness")):
            with self.assertRaises(DshError) as cm:
                a.rpc("session/create", {})
        self.assertIn("另一棵树", str(cm.exception), "拉不起来时要说得清为什么")

    def test_401_does_not_restart_a_healthy_harness(self):
        """**关键**：401 是 cookie 过期，不是"没起来" —— 绝不因此重启 harness。"""
        from app.agents.dsh_agent import DshAgent
        a = self._agent()
        seen = {"rpc": 0, "ensure": 0, "login": 0}

        err = urllib.error.HTTPError("http://127.0.0.1:43199/rpc", 401, "Unauthorized", {}, None)
        mk = self._dsh

        def fake_rpc(self, method, args=None, timeout=15):
            seen["rpc"] += 1
            if seen["rpc"] == 1:
                raise mk("DSH RPC x 失败: HTTP 401 Unauthorized", err)
            return {"ok": True}

        def fake_ensure(*args, **kwargs):
            seen["ensure"] += 1
            return True, "不该被调用"

        with patch.object(DshAgent, "rpc", fake_rpc), \
                patch.object(harness_proc, "ensure_running", fake_ensure), \
                patch.object(type(a), "_login", lambda self: "cookie=1"):
            out = a.rpc("session/list", {})

        self.assertEqual(seen["ensure"], 0, "401 不该去重启一个好好的 harness")
        self.assertEqual(seen["rpc"], 2, "应当重登后重试一次")
        self.assertTrue(out["ok"])

    def test_business_errors_are_not_offline(self):
        """`workspace/not-found` 这类**业务**错误 → 原样抛，不去重启 harness。"""
        a = self._agent()
        self.assertFalse(a._looks_offline(self._dsh(
            "DSH RPC session/create 返回错误: workspace/not-found workspace \"x\" not found")))
        self.assertTrue(a._looks_offline(self._dsh("x", urllib.error.URLError("refused"))))
        self.assertFalse(a._looks_offline(self._dsh(
            "HTTP 403", urllib.error.HTTPError("u", 403, "Forbidden", {}, None))))


if __name__ == "__main__":
    unittest.main()
