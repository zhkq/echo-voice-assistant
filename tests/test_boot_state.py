# -*- coding: utf-8 -*-
"""启动状态机的 characterization 测试（§13.8 安全网第 2 项，P3 前置）

`app/boot.py` 是"面板先可用、其余组件后台分阶段拉起"的编排器，状态机是
`pending → starting → online | failed | disabled | idle`。P3 要动的是被它拉起的
那些组件（热键/TTS/边条/模型加载），一旦状态机语义被改动，面板上看到的
"启动中/失败/已就绪"就会失真 —— 而面板是用户唯一的观察窗口。

本文件钉住：注册与快照的结构、report() 的状态跃迁与上报映射、_run_start 的三条
分支（无启动函数 / 正常 / 抛异常）、手动启停的拒绝条件、以及 setup() 登记的组件清单。

不碰真实 data/（db 重定向到临时目录）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.boot as boot                                      # noqa: E402
import app.db as db                                          # noqa: E402
from app.config import settings                              # noqa: E402


class _BootStateTestCase(unittest.TestCase):
    """每个用例一套干净的注册表（boot 的注册表是模块级全局）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-boot-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        boot._COMPONENTS.clear()
        del boot._ORDER[:]
        boot.set_phase("init")
        self._log = patch.object(boot, "_log")          # 不往库里写日志，只记录调用
        self.log = self._log.start()
        self.addCleanup(self._log.stop)

    def _wait_status(self, cid, want, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if boot._COMPONENTS[cid]["status"] == want:
                return True
            time.sleep(0.01)
        return False


class RegistrationAndSnapshotTests(_BootStateTestCase):
    def test_register_and_snapshot_structure(self):
        boot.register("x", "某组件", "🎤", can_start=True, can_stop=True, kind="model")
        snap = boot.snapshot()
        self.assertEqual(snap["phase"], "init")
        self.assertEqual(len(snap["components"]), 1)
        comp = snap["components"][0]
        self.assertEqual(comp["id"], "x")
        self.assertEqual(comp["label"], "某组件")
        self.assertEqual(comp["status"], "pending")
        self.assertEqual(comp["kind"], "model")
        # 内部函数不许外泄到 API 面
        self.assertNotIn("_start_fn", comp)
        self.assertNotIn("_stop_fn", comp)

    def test_register_is_idempotent(self):
        """重复注册同一个 id 不能把组件列两遍（summary.total 会被算翻倍）。"""
        boot.register("x", "某组件", "🎤")
        boot.register("x", "某组件（改）", "🎤")
        snap = boot.snapshot()
        self.assertEqual(len(snap["components"]), 1)
        self.assertEqual(snap["summary"]["total"], 1)
        self.assertEqual(snap["components"][0]["label"], "某组件（改）")

    def test_summary_counts_each_state(self):
        boot.register("a", "A", "", status="online")
        boot.register("b", "B", "", status="idle")
        boot.register("c", "C", "", status="disabled")
        boot.register("d", "D", "", status="failed")
        boot.register("e", "E", "", status="starting")
        boot.register("f", "F", "", status="pending")
        s = boot.snapshot()["summary"]
        self.assertEqual(s, {"total": 6, "ready": 3, "failed": 1, "running": 1, "pending": 1})

    def test_setup_registers_the_documented_components(self):
        boot.setup()
        snap = boot.snapshot()
        self.assertEqual([c["id"] for c in snap["components"]],
                         ["server", "dsh", "failover", "harness", "stt-cmd", "stt-meeting",
                          "tts", "wake", "hotkey", "meeting", "diarize"])
        by_id = {c["id"]: c for c in snap["components"]}
        self.assertEqual(by_id["server"]["status"], "online", "面板服务阶段 0 就已就绪")
        self.assertFalse(by_id["server"]["can_start"])
        self.assertEqual(by_id["stt-meeting"]["status"], "idle", "会议引擎按需加载")
        self.assertEqual(by_id["stt-meeting"]["kind"], "model")
        for cid in ("hotkey", "wake", "stt-cmd", "stt-meeting", "harness"):
            self.assertTrue(by_id[cid]["can_stop"], "%s 应可手动停止" % cid)
        self.assertTrue(by_id["harness"]["can_start"], "独立 harness 可手动拉起")

    def test_setup_twice_does_not_duplicate(self):
        boot.setup()
        boot.setup()
        self.assertEqual(boot.snapshot()["summary"]["total"], 11)


class ReportTests(_BootStateTestCase):
    def test_starting_records_start_time_once(self):
        boot.register("x", "X", "")
        boot.report("x", status="starting", detail="启动中…", progress=0.0)
        started = boot._COMPONENTS["x"]["started_at"]
        self.assertIsNotNone(started)
        boot.report("x", status="starting", detail="还是启动中")
        self.assertEqual(boot._COMPONENTS["x"]["started_at"], started)

    def test_online_records_duration_and_detail(self):
        boot.register("x", "X", "")
        boot.report("x", status="starting")
        time.sleep(0.02)
        snap = boot.report("x", status="online", detail="已启动", progress=1.0)
        self.assertEqual(snap["status"], "online")
        self.assertEqual(snap["detail"], "已启动")
        self.assertEqual(snap["progress"], 1.0)
        self.assertGreaterEqual(snap["duration"], 0.0)

    def test_empty_detail_keeps_previous_text(self):
        boot.register("x", "X", "")
        boot.report("x", status="starting", detail="加载模型…")
        boot.report("x", status="online")
        self.assertEqual(boot._COMPONENTS["x"]["detail"], "加载模型…")

    def test_error_is_stored_and_logged(self):
        boot.register("x", "X", "")
        boot.report("x", status="failed", detail="炸了", error="详细错误")
        self.assertEqual(boot._COMPONENTS["x"]["error"], "详细错误")
        self.assertTrue(self.log.called)
        levels = [c.args[1] for c in self.log.call_args_list]
        self.assertIn("error", levels)

    def test_online_is_logged_as_info(self):
        boot.register("x", "标签", "")
        boot.report("x", status="online", detail="就绪")
        self.assertEqual(self.log.call_args_list[-1].args[0], "x")
        self.assertEqual(self.log.call_args_list[-1].args[1], "info")

    def test_unknown_component_returns_none(self):
        self.assertIsNone(boot.report("nope", status="online"))

    def test_report_maps_to_the_dashboard_service_registry(self):
        """面板的组件状态来自 services；映射错了面板就永远显示 unknown。"""
        boot.register("hotkey", "热键/媒体键", "")
        fake = MagicMock()
        with patch.dict(boot._SERVICE_REPORTERS, {"hotkey": fake}):
            boot.report("hotkey", status="online", detail="组合键 + 媒体键")
        fake.assert_called_once_with("online", "组合键 + 媒体键")

    def test_failed_report_falls_back_to_the_error_text(self):
        boot.register("hotkey", "热键", "")
        fake = MagicMock()
        with patch.dict(boot._SERVICE_REPORTERS, {"hotkey": fake}):
            boot.report("hotkey", status="failed", detail="", error="钩子装不上")
        fake.assert_called_once_with("failed", "钩子装不上")

    def test_server_reporter_takes_no_arguments(self):
        boot.register("server", "面板服务", "")
        called = []
        with patch.dict(boot._SERVICE_REPORTERS, {"server": lambda: called.append(True)}):
            boot.report("server", status="online", detail="ECHO x.y.z")
        self.assertEqual(called, [True])

    def test_reporter_exception_does_not_break_reporting(self):
        boot.register("hotkey", "热键", "")
        with patch.dict(boot._SERVICE_REPORTERS,
                        {"hotkey": MagicMock(side_effect=RuntimeError("db down"))}):
            snap = boot.report("hotkey", status="online", detail="就绪")
        self.assertEqual(snap["status"], "online")


class RunStartTests(_BootStateTestCase):
    def test_missing_start_fn_counts_as_builtin_online(self):
        boot.register("x", "X", "", start_fn=None)
        boot._run_start("x")
        self.assertEqual(boot._COMPONENTS["x"]["status"], "online")
        self.assertEqual(boot._COMPONENTS["x"]["detail"], "内置")

    def test_exception_becomes_failed_with_error_text(self):
        def boom(report):
            raise RuntimeError("模型加载失败")
        boot.register("x", "X", "", start_fn=boom)
        boot._run_start("x")
        comp = boot._COMPONENTS["x"]
        self.assertEqual(comp["status"], "failed")
        self.assertEqual(comp["error"], "模型加载失败")
        self.assertEqual(comp["detail"], "模型加载失败")

    def test_start_fn_reporting_its_own_status_wins(self):
        def ok(report):
            report(status="online", detail="已运行 · API 可访问", progress=1.0)
        boot.register("x", "X", "", start_fn=ok)
        boot._run_start("x")
        self.assertEqual(boot._COMPONENTS["x"]["detail"], "已运行 · API 可访问")

    def test_start_fn_marking_failed_is_not_overwritten(self):
        def fail(report):
            report(status="failed", detail="没装上", error="没装上")
        boot.register("x", "X", "", start_fn=fail)
        boot._run_start("x")
        self.assertEqual(boot._COMPONENTS["x"]["status"], "failed")

    def test_progress_callback_reaches_the_snapshot(self):
        def staged(report):
            report(detail="探测中…", progress=0.1)
            report(detail="注册 ECHO AUTO…", progress=0.7)
            report(status="online", detail="完成", progress=1.0)
        boot.register("x", "X", "", start_fn=staged)
        boot._run_start("x")
        snap = boot.snapshot()["components"][0]
        self.assertEqual(snap["progress"], 1.0)
        self.assertEqual(snap["detail"], "完成")

    def test_unknown_component_is_a_noop(self):
        boot._run_start("nope")     # 不抛异常
        self.assertEqual(boot.snapshot()["components"], [])


class ManualStartStopTests(_BootStateTestCase):
    def test_start_unknown_component(self):
        self.assertEqual(boot.start_component("nope"), (False, "组件不存在"))

    def test_start_rejected_when_not_startable(self):
        boot.register("x", "X", "", can_start=False)
        self.assertEqual(boot.start_component("x"), (False, "该组件不可手动启动"))

    def test_start_rejected_while_starting(self):
        boot.register("x", "X", "", status="starting")
        self.assertEqual(boot.start_component("x"), (False, "正在启动中"))

    def test_start_runs_the_start_fn_in_background(self):
        done = threading.Event()

        def fn(report):
            done.set()
            report(status="online", detail="好了")
        boot.register("x", "X", "", start_fn=fn)
        self.assertEqual(boot.start_component("x"), (True, "已开始启动"))
        self.assertTrue(done.wait(3.0), "启动函数应在后台线程里跑")
        self.assertTrue(self._wait_status("x", "online"))

    def test_stop_unknown_component(self):
        self.assertEqual(boot.stop_component("nope"), (False, "组件不存在"))

    def test_stop_rejected_without_stop_fn(self):
        boot.register("x", "X", "", start_fn=lambda report: None, stop_fn=None)
        self.assertEqual(boot.stop_component("x"), (False, "该组件不可停止"))

    def test_stop_sets_idle(self):
        boot.register("x", "X", "", start_fn=lambda report: None,
                      stop_fn=lambda: None, status="online")
        self.assertEqual(boot.stop_component("x"), (True, "已停止"))
        self.assertEqual(boot._COMPONENTS["x"]["status"], "idle")
        self.assertEqual(boot._COMPONENTS["x"]["detail"], "已停止")

    def test_stop_exception_is_returned_not_raised(self):
        def bad_stop():
            raise RuntimeError("卸载失败")
        boot.register("x", "X", "", start_fn=lambda report: None,
                      stop_fn=bad_stop, status="online")
        self.assertEqual(boot.stop_component("x"), (False, "卸载失败"))
        self.assertEqual(boot._COMPONENTS["x"]["status"], "online", "失败时别谎报已停")

    def test_spawn_skips_settled_components(self):
        started = []
        for cid, status in (("a", "pending"), ("b", "online"),
                            ("c", "starting"), ("d", "disabled")):
            boot.register(cid, cid, "", status=status,
                          start_fn=(lambda c: (lambda report: started.append(c)))(cid))
        boot._spawn(["a", "b", "c", "d", "missing"])
        end = time.time() + 3.0
        while time.time() < end and not started:
            time.sleep(0.01)
        time.sleep(0.1)
        self.assertEqual(started, ["a"], "只应拉起 pending 的那个")


class BootOrchestrationTests(_BootStateTestCase):
    def test_run_boot_settles_everything_and_marks_done(self):
        """全量编排：所有登记的组件都拉起后 phase=done（用"内置"组件避免加载模型）。"""
        for cid in ("failover", "dsh", "tts", "hotkey", "meeting", "diarize",
                    "stt-cmd", "wake"):
            boot.register(cid, cid, "", start_fn=None)
        boot._run_boot()
        self.assertEqual(boot._PHASE, "done")
        snap = boot.snapshot()
        self.assertEqual(snap["summary"]["running"], 0)
        self.assertEqual(snap["summary"]["ready"], 8)

    def test_start_all_async_returns_immediately(self):
        for cid in ("failover", "dsh", "tts", "hotkey", "meeting", "diarize",
                    "stt-cmd", "wake"):
            boot.register(cid, cid, "", start_fn=None)
        t0 = time.time()
        boot.start_all_async()
        self.assertLess(time.time() - t0, 1.0, "编排必须是后台线程，不能阻塞启动路径")
        end = time.time() + 3.0
        while time.time() < end and boot._PHASE != "done":
            time.sleep(0.01)
        self.assertEqual(boot._PHASE, "done")


class SkippedAgentTests(_BootStateTestCase):
    """「你选了另一个智能体」不该被报成失败（2026-09-22 同事反馈）。

    症状：装了标准版 harness，启动页却写「就绪 10/11 · 失败 1」，把「DSH 执行引擎」
    渲染成红色，还教用户去启动一个**他没选**的组件（"请启动 DSH Desktop…"）。
    根因：`_start_dsh` 不看 `agentBackend`，无条件去起桌面版，起不来就报 failed ——
    而两个智能体是**二选一**，没选它的机器上它本来就该是"未使用"。

    这是同一类误导的第二次出现（上一轮是"dsh offline 是不是坏了"，换了个页面又来）。
    """

    def _register_agents(self):
        """两个智能体都要登记 —— detail 里的名字取自组件显示名，只登记一个会回退成内部 id。"""
        boot.register("dsh", "DSH 执行引擎", "⚙️", start_fn=boot._start_dsh)
        boot.register("harness", "标准版 harness", "🧩", start_fn=boot._start_harness)

    def test_unselected_desktop_is_skipped_not_failed(self):
        self._register_agents()
        with patch.object(boot, "selected_agent", return_value="harness"):
            boot._run_start("dsh")
        comp = boot._COMPONENTS["dsh"]
        self.assertEqual(comp["status"], "skipped", "没选它就不该去启动、更不该报失败")
        self.assertEqual(comp["error"], "", "skipped 不是错误")
        self.assertIn("标准版", comp["detail"], "要说清你选的是哪一个，别让人去猜")

    def test_selected_desktop_still_starts(self):
        """选中桌面版时行为一个字都不变 —— 别把正常启动路径也 skip 掉。"""
        self._register_agents()
        with patch.object(boot, "selected_agent", return_value="dsh"), \
             patch("app.manager.dsh_ready", return_value=True):
            boot._run_start("dsh")
        self.assertEqual(boot._COMPONENTS["dsh"]["status"], "online")

    def test_unselected_harness_is_skipped(self):
        self._register_agents()
        with patch.object(boot, "selected_agent", return_value="dsh"), \
             patch("app.harness_proc.requested", return_value=False), \
             patch("app.harness_proc._load_pid", return_value=None), \
             patch("app.harness_proc.started_by_echo", return_value=False), \
             patch("app.harness_proc.sync_status"):
            boot._run_start("harness")
        comp = boot._COMPONENTS["harness"]
        self.assertEqual(comp["status"], "skipped")
        self.assertIn("DSH", comp["detail"])

    def test_skipped_counts_as_ready_and_never_as_failed(self):
        """统计口径：skipped 是"不用它"，不是"它坏了" —— 否则选标准版的机器永远显示失败 1。"""
        boot.register("a", "A", "", status="online")
        boot.register("dsh", "DSH 执行引擎", "", status="skipped")
        s = boot.snapshot()["summary"]
        self.assertEqual(s["failed"], 0)
        self.assertEqual(s["ready"], 2)
        self.assertEqual(s["pending"], 0)

    def test_only_the_two_agents_are_ever_skipped(self):
        """skipped 只用于"二选一的智能体"，别扩散成"所有没启用的组件"。

        `diarize` 的 disabled 有别的含义（模型缺失、可补救），两者不能混。
        """
        boot.setup()
        with patch.object(boot, "selected_agent", return_value="harness"), \
             patch("app.manager.dsh_ready", return_value=False), \
             patch("app.manager.dsh_start", return_value=(False, "没开")), \
             patch("app.harness_proc.requested", return_value=False), \
             patch("app.harness_proc._load_pid", return_value=None), \
             patch("app.harness_proc.started_by_echo", return_value=False), \
             patch("app.harness_proc.sync_status"):
            boot._run_start("dsh")
            boot._run_start("harness")
        by_id = {c["id"]: c["status"] for c in boot.snapshot()["components"]}
        self.assertEqual(by_id["dsh"], "skipped")
        self.assertEqual(by_id["harness"], "skipped")
        self.assertNotEqual(by_id["diarize"], "skipped", "diarize 的 disabled 含义不同")


class FailoverDetailRefreshTests(_BootStateTestCase):
    """failover 的文案不能是启动那一瞬间的陈旧结论（2026-09-22 同事反馈）。

    注册 ECHO AUTO 那步跑在**阶段 1**，可能早于 harness 起来 —— 当时探测到的
    "标准版 harness 没在监听 43199" 被烤进 detail，等 harness 真起来了面板还在念，
    排障时被这句自相矛盾的话带偏（尤其"node 不在 PATH"那句，指向的正是已修好的那条）。
    """

    def setUp(self):
        super().setUp()
        boot.register("failover", "模型路由（ECHO AUTO）", "🛰️", status="online")
        self._old_router_detail = boot._ROUTER_DETAIL
        boot._ROUTER_DETAIL = "运行中 · http://127.0.0.1:18060"
        self.addCleanup(setattr, boot, "_ROUTER_DETAIL", self._old_router_detail)

    def test_detail_is_recomputed_when_the_agent_comes_up(self):
        with patch.object(boot, "_agent_dsh_available",
                          return_value=(False, "标准版 harness 没在监听 http://127.0.0.1:43199")):
            boot._refresh_failover_detail(note="启动时")
        self.assertIn("没在监听", boot._COMPONENTS["failover"]["detail"])

        # 智能体就绪后再算一次：必须按**现在**的状态重写
        with patch.object(boot, "_agent_dsh_available", return_value=(True, "")), \
             patch("app.llm_router.sync", return_value=(True, "已写入 2 个家目录")):
            boot._refresh_failover_detail(note="智能体就绪后复核")
        detail = boot._COMPONENTS["failover"]["detail"]
        self.assertNotIn("没在监听", detail, "旧的探测结论必须被重写掉")
        self.assertIn("已写入 2 个家目录", detail)

    def test_router_base_detail_is_preserved(self):
        """重算的只是"注册进 DSH 的情况"，路由本身那句话不能丢。"""
        with patch.object(boot, "_agent_dsh_available", return_value=(True, "")), \
             patch("app.llm_router.sync", return_value=(True, "ok")):
            boot._refresh_failover_detail(note="测试")
        self.assertIn("http://127.0.0.1:18060", boot._COMPONENTS["failover"]["detail"])

    def test_noop_before_the_router_is_up(self):
        """路由还没起来时（_ROUTER_DETAIL 为空）不该瞎写一句。"""
        boot._ROUTER_DETAIL = ""
        boot.report("failover", status="starting", detail="探测路由端口…")
        boot._refresh_failover_detail(note="测试")
        self.assertEqual(boot._COMPONENTS["failover"]["detail"], "探测路由端口…")

    def test_return_value_tells_the_caller_whether_to_retry(self):
        """返回值就是"要不要稍后再试"的信号（同事反馈 3.3 的重试靠它）。

        为什么需要重试：标准版的家目录（含 settings.yaml）是 **harness 自己启动后**才写出来的，
        而 `ensure_running()` 返回时只保证端口在听 —— 第一次复核经常撞上"家目录还没初始化"，
        之后又没人再试，文案就永久停在失败态（实测 40 秒后再看一个字没变）。
        """
        with patch.object(boot, "_agent_dsh_available",
                          return_value=(False, "没找到 DSH 家目录")):
            self.assertFalse(boot._refresh_failover_detail(note="测试"),
                             "没注册上就应该告诉调用方「还可以再试」")
        with patch.object(boot, "_agent_dsh_available", return_value=(True, "")), \
             patch("app.llm_router.sync", return_value=(True, "已写入 2 个家目录")):
            self.assertTrue(boot._refresh_failover_detail(note="测试"),
                            "注册成功了就不该再排重试")

    def test_recheck_loop_stops_immediately_when_disabled(self):
        """测试环境必须能一键关掉那个后台线程（它会写真实 DSH 配置）。"""
        old = boot._ROUTER_RECHECK_ENABLED
        boot._ROUTER_RECHECK_ENABLED = False
        try:
            boot._router_recheck_loop()      # 立刻返回，不睡也不调 sync
        finally:
            boot._ROUTER_RECHECK_ENABLED = old


class PanelRendersSkippedTests(unittest.TestCase):
    """前端契约：skipped 要有文案、要渲染成 idle 而不是错误 —— 否则后端改对了面板还是红的。"""

    def test_status_text_and_badge_cover_skipped(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web", "app.js")
        with open(path, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("skipped:", js, "STATUS_TEXT 里没有 skipped —— 徽章会显示原始英文")
        self.assertIn('status === "skipped"', js, "bootBadgeCls 不认识 skipped")
        # 「未使用」不能被染成错误色：bootBadgeCls 里 skipped 必须排在 failed 之后返回 idle
        self.assertIn('if (status === "skipped") return "idle"', js)


if __name__ == "__main__":
    unittest.main()
