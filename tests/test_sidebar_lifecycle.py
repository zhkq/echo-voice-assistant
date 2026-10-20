# -*- coding: utf-8 -*-
"""tests/test_sidebar_lifecycle.py — 边条（echo-sidebar.exe）要跟着 ECHO 一起退（2026-10-21）

真机 bug（用户报的）：点了面板「关机」→ 服务确实停了（8970 不再监听）→ **边条还挂在屏幕上**。
根因不是"忘了退出"，而是**边条是独立进程**：`app/runtime.py:_spawn_sidebar()` 用
DETACHED_PROCESS 起它（这样 ECHO 重启/被杀时它能活下来），所以 ECHO 退出**带不走它**。

两条修法，各自都有用例：

  1. **正常停止 → 主动通知**（`manager.echo_stop_self()`：面板「关机」/
     `POST /api/control/echo/stop` 那条路最后都落在这一处）：先经边条**已有的**命名管道
     `echo-sidebar-toggle` 发一条 `quit`，再 `os._exit(0)`。通知失败（边条没开/连不上）
     **只记日志**，绝不让"停服务"失败。
  2. **强杀 / 崩溃 → 心跳兜底**：边条自己每 5 秒探 `GET /api/status`，连续 3 次失败就自己退。
     前 30 秒是**启动宽限期**（边条可能比服务先起来，宽限期内的失败不计数）；
     服务恢复一次就清零。判据是 `sidebar/Heartbeat.cs` 的**纯函数**
     `HeartbeatPolicy.NextFailureCount / ShouldExit`。

测试策略（**不起 .NET**：边条是 WinForms 进程，测试里真拉起它只会糊一屏窗口）：
  * 心跳判据按"**C# 源码 + 从源码里读出来的常量**"核对，时序用同一套判据的 Python 对照实现跑；
    常量是从 `Heartbeat.cs` 里**解析**出来的，所以真改了 3 次 / 5 秒 / 30 秒，这里的时序断言会跟着变；
  * 正常停止那条路用**可注入替身**：把 `_sidebar_pipe_write` 与 `os._exit` 换掉 ——
    绝不碰真管道（那会把开发机上正在用的边条关掉），也绝不真退测试进程。
"""
import os
import re
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app.manager as manager_mod      # noqa: E402
import app.runtime as runtime_mod      # noqa: E402

HEARTBEAT_CS = os.path.join(ROOT, "sidebar", "Heartbeat.cs")
PROGRAM_CS = os.path.join(ROOT, "sidebar", "Program.cs")
API_PY = os.path.join(ROOT, "app", "api.py")
MANAGER_PY = os.path.join(ROOT, "app", "manager.py")
STOP_PS1 = os.path.join(ROOT, "scripts", "stop.ps1")
RESTART_PS1 = os.path.join(ROOT, "scripts", "restart-echo.ps1")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _csharp_body(src, signature):
    """取 C# 方法/构造的**花括号内的正文**（够用的括号配平，不处理字符串里的括号）。"""
    i = src.index(signature)
    open_at = src.index("{", i)
    depth = 0
    for k in range(open_at, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at + 1:k]
    raise AssertionError("括号没配平：%r" % signature)


def _policy_constants():
    """从 `sidebar/Heartbeat.cs` 里读心跳判据的三个常量（**唯一权威**在 C# 那边）。"""
    src = _read(HEARTBEAT_CS)
    out = {}
    for name, key in (("IntervalMs", "interval_ms"),
                      ("FailureThreshold", "threshold"),
                      ("StartupGraceMs", "grace_ms")):
        m = re.search(r"const\s+int\s+%s\s*=\s*(\d+)\s*;" % name, src)
        assert m is not None, "Heartbeat.cs 里找不到常量 %s" % name
        out[key] = int(m.group(1))
    return out


# --------------------------------------------------------------------------
# 心跳判据（纯函数）的 Python 对照实现
#
# 与 `sidebar/Heartbeat.cs::HeartbeatPolicy` 逐行对应：下面两个函数的每一句都能在 C# 那边
# 找到同名的一句；`HeartbeatPolicyTests.test_the_source_says_the_same_three_things` 会核对
# 那三句真的在源码里 —— 所以这里**不是**在自己测自己。
# --------------------------------------------------------------------------
def _next_failures(failures, reachable, elapsed_ms, cfg):
    if reachable:
        return 0                                    # 服务恢复一次 → 清零
    if elapsed_ms < cfg["grace_ms"]:
        return 0                                    # 启动宽限期内的失败不计
    return failures + 1


def _should_exit(elapsed_ms, failures, cfg):
    return elapsed_ms >= cfg["grace_ms"] and failures >= cfg["threshold"]


def _simulate(cfg, alive_from_ms=0, alive_until_ms=None, total_ms=None):
    """按边条的节拍跑一遍：返回 (退出时刻 ms 或 None, 每一拍的 (t, failures))。

    边条的真实节拍（`SidebarHeartbeat.Run`）：**t=0 先探一次**，之后每 IntervalMs 开一拍
    （探测本身花掉的时间从睡眠里扣掉，所以拍与拍的间隔仍是 IntervalMs）。
    `alive_from_ms` 之前探不到、`alive_until_ms` 之后探不到（给 None = 一直探得到）——
    "服务被强杀"= alive_until，"边条比服务先起来"= alive_from。
    """
    interval = cfg["interval_ms"]
    total = total_ms if total_ms is not None else cfg["grace_ms"] * 4
    failures = 0
    trace = []
    t = 0
    while t <= total:
        reachable = t >= alive_from_ms and (alive_until_ms is None or t <= alive_until_ms)
        failures = _next_failures(failures, reachable, t, cfg)
        trace.append((t, failures))
        if _should_exit(t, failures, cfg):
            return t, trace
        t += interval
    return None, trace


class HeartbeatPolicyTests(unittest.TestCase):
    """心跳判据：连续 K 次失败才退；宽限期内不计；恢复一次就清零。"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = _policy_constants()

    def test_the_policy_constants_are_the_agreed_ones(self):
        """"建议 5 秒 / 3 次 / 前 30 秒宽限" —— 这三个数字是产品口径，改它要一起改这里。"""
        self.assertEqual(self.cfg["interval_ms"], 5000)
        self.assertEqual(self.cfg["threshold"], 3)
        self.assertEqual(self.cfg["grace_ms"], 30000)

    def test_the_source_says_the_same_three_things(self):
        """C# 的纯函数必须与上面的对照实现同形（否则"对照"就只是自说自话）。"""
        src = _read(HEARTBEAT_CS)
        counter = _csharp_body(src, "public static int NextFailureCount")
        self.assertIn("if (reachable) return 0;", counter)
        self.assertIn("if (elapsedMs < StartupGraceMs) return 0;", counter)
        self.assertIn("return failures + 1;", counter)
        verdict = _csharp_body(src, "public static bool ShouldExit")
        self.assertIn("elapsedMs >= StartupGraceMs", verdict)
        self.assertIn("failures >= FailureThreshold", verdict)
        self.assertIn("&&", verdict)

    def test_the_policy_is_pure_no_io_no_ui(self):
        """判据必须是**纯函数**：不碰网络、不读文件、不弹窗、不睡线程（否则没法这样逐条讲）。"""
        policy = _csharp_body(_read(HEARTBEAT_CS),
                              "internal static class HeartbeatPolicy")
        for forbidden in ("Http", "MessageBox", "File.", "Thread.", "Console.", "Process"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, policy,
                                 "HeartbeatPolicy 里出现了 %s —— 判据被 I/O 混进去了" % forbidden)

    def test_one_or_two_failures_do_not_exit_but_three_do(self):
        """连续 1~2 次失败不许退；第 3 次才退（阈值不是 1 也不是 2）。"""
        cfg, n = self.cfg, self.cfg["threshold"]
        later = cfg["grace_ms"]            # 正好在宽限期结束时：失败开始计数
        failures = 0
        for step in range(1, n):
            failures = _next_failures(failures, False, later, cfg)
            self.assertEqual(failures, step)
            self.assertFalse(_should_exit(later, failures, cfg),
                             "连续 %d 次失败就退 = 阈值写小了" % step)
        failures = _next_failures(failures, False, later, cfg)
        self.assertEqual(failures, n)
        self.assertTrue(_should_exit(later, failures, cfg), "连续 %d 次失败该退了" % n)

    def test_failures_inside_the_startup_grace_are_not_counted(self):
        """前 30 秒的失败不计：边条可能比服务先起来，那几秒探不到是正常的。"""
        cfg = self.cfg
        failures = 0
        t = 0
        while t + cfg["interval_ms"] < cfg["grace_ms"]:
            t += cfg["interval_ms"]
            failures = _next_failures(failures, False, t, cfg)
            self.assertEqual(failures, 0, "宽限期内（t=%dms）的失败被计数了" % t)
            self.assertFalse(_should_exit(t, failures, cfg), "宽限期内（t=%dms）就判了退出" % t)
        # 宽限期一过，失败才真的开始累计
        failures = _next_failures(failures, False, cfg["grace_ms"], cfg)
        self.assertEqual(failures, 1, "宽限期结束后的那次失败应当计数")

    def test_a_single_recovery_clears_the_counter(self):
        """服务恢复一次 → 计数清零（重启中抖两下不该被当成死了）。"""
        cfg = self.cfg
        at = cfg["grace_ms"]
        failures = _next_failures(_next_failures(0, False, at, cfg), False, at, cfg)
        self.assertEqual(failures, 2)
        failures = _next_failures(failures, True, at, cfg)
        self.assertEqual(failures, 0)
        # 清零之后还得重新攒够阈值才退
        failures = _next_failures(failures, False, at, cfg)
        self.assertEqual(failures, 1)
        self.assertFalse(_should_exit(at, failures, cfg), "清零后一次失败就退 = 没清零")

    def test_a_front_loaded_slow_start_is_not_mistaken_for_a_crash(self):
        """边条比服务先起来（前 30 秒一直连不上）→ 服务起来后不许被误杀。"""
        cfg = self.cfg
        came_up_at = cfg["grace_ms"] - cfg["interval_ms"]      # 宽限期最后一拍前起来
        exit_at, trace = _simulate(cfg, alive_from_ms=came_up_at,
                                   total_ms=cfg["grace_ms"] * 4)
        self.assertIsNone(exit_at, "服务只是在启动，边条却退了：%r" % (trace,))
        self.assertTrue(all(f == 0 for _, f in trace), "服务起来之后计数没有清零：%r" % (trace,))

    def test_a_hard_killed_service_is_exited_by_the_heartbeat_alone(self):
        """**没有**任何人调 stop（= 服务被强杀）→ 边条靠心跳自己退。

        时序：服务在 t=10s 被强杀 → 前 30 秒宽限期内的失败不计 → 宽限期后连续 3 拍失败
        → 退出。这也正是"进程被强杀后不会再有人发 quit"的那条路。
        """
        cfg = self.cfg
        exit_at, trace = _simulate(cfg, alive_until_ms=10000,
                                   total_ms=cfg["grace_ms"] * 3)
        self.assertIsNotNone(exit_at, "服务被强杀，边条的心跳却没让它退：%r" % (trace,))
        self.assertGreaterEqual(exit_at, cfg["grace_ms"],
                                "宽限期没到就退了（会误杀'边条先起来'的那种现场）")
        self.assertLessEqual(exit_at, cfg["grace_ms"] + cfg["threshold"] * cfg["interval_ms"],
                             "宽限期过了还要等超过 %d 次探测才退" % cfg["threshold"])
        # 退之前那 3 拍必须真的是"连续失败"
        at_exit = [f for (t, f) in trace if t == exit_at][0]
        self.assertEqual(at_exit, cfg["threshold"])

    def test_the_loop_actually_uses_the_policy(self):
        """心跳线程必须**用**这两个纯函数判据，而不是自己另写一套 if。"""
        src = _read(HEARTBEAT_CS)
        run = _csharp_body(src, "private void Run()")
        self.assertIn("HeartbeatPolicy.NextFailureCount(", run)
        self.assertIn("HeartbeatPolicy.ShouldExit(", run)
        self.assertIn("HeartbeatPolicy.IntervalMs", run)
        self.assertIn("Thread.Sleep", run)
        # 探测耗时要从睡眠里扣掉（否则 5 秒拍会变成"5 秒 + 探测耗时"）
        self.assertIn("spent", run)
        self.assertIn("GetAsync", _csharp_body(src, "private bool Probe()"))


class SidebarQuitOverExistingIpcTests(unittest.TestCase):
    """正常停止：经**边条已有的**命名管道发 `quit`，不是新造一套协议。"""

    def test_the_pipe_name_is_the_one_the_sidebar_listens_on(self):
        """两边的管道名必须一致 —— 名字漂了 = 通知永远发不到（而且不报错）。"""
        program = _read(PROGRAM_CS)
        m = re.search(r'PipeName\s*=\s*"([^"]+)"', program)
        self.assertIsNotNone(m, "sidebar/Program.cs 里找不到 PipeName")
        self.assertEqual(m.group(1), runtime_mod.SIDEBAR_PIPE_NAME)
        self.assertEqual(runtime_mod.SIDEBAR_PIPE_PATH,
                         "\\\\.\\pipe\\" + runtime_mod.SIDEBAR_PIPE_NAME)

    def test_the_sidebar_listener_handles_quit(self):
        """边条侧：管道监听要认 `quit`，并且走**同一条**"服务没了就关窗"的收尾路径。"""
        program = _read(PROGRAM_CS)
        listener = _csharp_body(program, "private static void StartToggleListener")
        self.assertIn('command.Contains("quit"', listener)
        self.assertIn("CloseBecauseServiceStopped", listener)
        closer = _csharp_body(program, "internal void CloseBecauseServiceStopped")
        self.assertIn("Close()", closer)
        self.assertIn("ECHO 已停止", closer,
                      "退出前应当先把标题改成一句人话（可选，但要求里写了）")
        self.assertNotIn("MessageBox", closer, "退出前**不许**弹错误框")

    def test_signal_quit_never_opens_a_new_window(self):
        """`--signal=quit` 的语义是"请**已有**那个边条退出"。

        没有边条在跑时必须原地返回：否则 stop.ps1 那句"请退出"会为了退出而**开一个新窗口**。
        """
        program = _read(PROGRAM_CS)
        main = _csharp_body(program, "private static void Main(string[] args)")
        signal_at = main.index("TrySignalExistingSidebar(signal)")
        guard_at = main.index("no existing sidebar to signal")
        form_at = main.index("new SidebarForm(")
        self.assertLess(signal_at, guard_at, "quit 的空转判断必须在发信号之后")
        self.assertLess(guard_at, form_at, "quit 的空转判断必须在建窗之前")

    def test_the_heartbeat_starts_for_a_real_window_only(self):
        """心跳在 `Application.Run` 之前起；`--probe`（只写诊断日志）那条路不受影响。"""
        program = _read(PROGRAM_CS)
        main = _csharp_body(program, "private static void Main(string[] args)")
        self.assertIn("new SidebarHeartbeat(", main)
        self.assertLess(main.index("--probe"), main.index("new SidebarHeartbeat("),
                        "诊断模式必须跑不到心跳（它写一行日志就该退出）")
        self.assertLess(main.index("new SidebarHeartbeat("), main.index("Application.Run(form)"))

    def test_the_why_comments_are_there(self):
        """为什么这么改要写在代码里（本仓库的约定）：独立进程 / 强杀 / 宽限期。"""
        heartbeat = _read(HEARTBEAT_CS)
        program = _read(PROGRAM_CS)
        self.assertIn("独立进程", heartbeat)
        self.assertIn("强杀", heartbeat)
        self.assertIn("宽限", heartbeat)
        self.assertIn("独立进程", program)


class StopPathNotifiesSidebarTests(unittest.TestCase):
    """`POST /api/control/echo/stop` / 面板「关机」那条路：退出前先请边条退出。"""

    def setUp(self):
        self.writes = []
        self.exited = []

    def _run_stop(self, writer=None):
        """调**真的** `manager.echo_stop_self()`，只把两个副作用换成替身。

        换掉的是 `runtime._sidebar_pipe_write`（绝不碰真管道）和 `os._exit`
        （绝不真把测试进程退掉）—— 别的都是生产代码。
        """
        def _record(command):
            self.writes.append(command)
            return True

        real_exit = os._exit

        def _fake_exit(code):
            self.exited.append(code)

        with patch.object(runtime_mod, "_sidebar_pipe_write", side_effect=writer or _record), \
                patch.object(os, "_exit", side_effect=_fake_exit):
            manager_mod.echo_stop_self()
        self.assertIs(os._exit, real_exit, "os._exit 必须被还原（不然下一次真调用会写坏）")

    def test_a_normal_stop_tells_the_sidebar_to_quit_and_then_exits(self):
        self._run_stop()
        self.assertEqual(self.writes, ["quit"], "正常停止必须发且只发一条 quit")
        self.assertEqual(self.exited, [0], "发了通知之后仍要真的退出 ECHO")

    def test_the_quit_goes_out_before_the_process_exits(self):
        """顺序：先通知、后退出（反了就永远发不出去）。"""
        order = []
        real_exit = os._exit

        def _writer(command):
            order.append("quit")
            return True

        def _fake_exit(code):
            order.append("exit")

        with patch.object(runtime_mod, "_sidebar_pipe_write", side_effect=_writer), \
                patch.object(os, "_exit", side_effect=_fake_exit):
            manager_mod.echo_stop_self()
        self.assertEqual(order, ["quit", "exit"])
        self.assertIs(os._exit, real_exit, "os._exit 必须还原（否则后面的真调用会被替身吃掉）")

    def test_a_sidebar_that_is_not_there_never_fails_the_stop(self):
        """边条没开/管道连不上 → 停止**不许**失败，也不许抛（最坏只是少关一个窗口）。"""
        def _boom(command):
            raise OSError("pipe busy")

        self._run_stop(writer=_boom)
        self.assertEqual(self.exited, [0], "通知失败也必须照常退出 ECHO")

    def test_a_broken_notification_still_exits(self):
        """连"写管道"这个函数自己炸了（比如 import 出问题）也不许挡住退出。"""
        def _explode(*_a, **_k):
            raise RuntimeError("boom")

        with patch.object(runtime_mod, "quit_sidebar", side_effect=_explode), \
                patch.object(os, "_exit", side_effect=lambda code: self.exited.append(code)):
            manager_mod.echo_stop_self()
        self.assertEqual(self.exited, [0])

    def test_the_stop_endpoint_lands_on_that_single_exit(self):
        """源码级：面板「关机」/ HTTP 端点最后都落到 `manager.echo_stop_self`（只有一处出口）。"""
        api = _read(API_PY)
        endpoint = api[api.index('@router.post("/control/echo/stop")'):]
        endpoint = endpoint[:endpoint.index("@router", 10)]
        self.assertIn("manager.echo_stop_self", endpoint)
        self.assertIn("threading.Timer", endpoint)

        manager = _read(MANAGER_PY)
        body = manager[manager.index("def echo_stop_self():"):]
        body = body[:body.index("\ndef ", 1)]
        self.assertIn("quit_sidebar", body, "停服务的唯一出口没有通知边条")
        # 用 rindex：docstring 里也会**提到**这两个名字，比的是真正的调用位置。
        self.assertLess(body.rindex("quit_sidebar"), body.rindex("os._exit"),
                        "通知边条必须在 os._exit 之前，否则根本发不出去")
        self.assertIn("try:", body, "通知边条那一步必须被 try 包住（失败不许挡停服务）")

    def test_quit_sidebar_swallows_a_broken_pipe(self):
        """`quit_sidebar()` 自己也不抛：它挂在停服务的收尾上。"""
        with patch.object(runtime_mod, "_sidebar_pipe_write",
                          side_effect=OSError("no pipe")):
            self.assertFalse(runtime_mod.quit_sidebar())

    def test_the_pipe_write_is_best_effort(self):
        """`_sidebar_pipe_write` 对"打不开管道"只返回 False（最坏是边条没收到）。"""
        with patch("builtins.open", side_effect=FileNotFoundError("no such pipe")):
            self.assertFalse(runtime_mod._sidebar_pipe_write("quit"))


class StopScriptTests(unittest.TestCase):
    """`scripts\\stop.ps1` 是"硬停"那条路：Python 已经死了，只能由脚本自己发 quit。"""

    @classmethod
    def setUpClass(cls):
        cls.text = _read(STOP_PS1)

    def test_the_stop_script_sends_the_same_quit_command(self):
        self.assertIn(runtime_mod.SIDEBAR_PIPE_NAME, self.text)
        self.assertIn("'quit'", self.text)
        self.assertIn("$KeepSidebar", self.text)
        self.assertIn("if (-not $KeepSidebar) { Send-SidebarQuit $root }", self.text,
                      "真正发 quit 的那一句不见了")

    def test_the_stop_script_only_touches_this_trees_sidebar(self):
        """另一棵树（稳定版）的边条不许碰 —— 与 ECHO 进程同一条归属判据。"""
        self.assertIn(r"*$root\sidebar\*", self.text)

    def test_the_stop_script_stays_ascii_only(self):
        """无 BOM 的 .ps1 会被 WinPS 5.1 按 GBK 读 → 中文注释直接解析失败（本仓库的坑）。"""
        with open(STOP_PS1, "rb") as fh:
            raw = fh.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "stop.ps1 是纯 ASCII，不该有 BOM")
        self.assertEqual([b for b in raw if b > 0x7F], [], "stop.ps1 里混进了非 ASCII 字节")

    def test_a_restart_keeps_the_rail_alive(self):
        """重启**不该**关边条：它本来就要在 ECHO 重启期间活着（页面会自己重试）。"""
        restart = _read(RESTART_PS1)
        self.assertIn("-KeepSidebar", restart)


if __name__ == "__main__":
    unittest.main()
