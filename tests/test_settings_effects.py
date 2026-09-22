# -*- coding: utf-8 -*-
"""设置联动的共享层（wake / router / 智能体-harness）。

为什么值得单独测：这段逻辑原先只长在 `PUT /api/settings` 的处理器里，**不走那个 HTTP 接口**
的写入享受不到它 —— 向导执行相就是直接调 `settings.update()`，于是"在向导里选了标准版"
从来不会把 harness 拉起来（2026-09-20 实测）。抽到共享层之后两边共用，所以这里钉住的不只是
分支，还有**不抛异常**（由调用方决定怎么呈现）和**把 harness 超时原样传下去**。
"""
import unittest
from unittest.mock import patch

from app import settings_effects


class DispatchTests(unittest.TestCase):
    """按"哪些键变了"决定跑哪段 —— 没动的键不许连带触发。"""

    def test_only_touched_scopes_run(self):
        with patch.object(settings_effects, "_wake") as w, \
                patch.object(settings_effects, "_router") as r, \
                patch.object(settings_effects, "_agent") as a:
            w.return_value = {"scope": "wake", "ok": True, "detail": ""}
            r.return_value = {"scope": "router", "ok": True, "detail": ""}
            a.return_value = {"scope": "agent", "ok": True, "detail": ""}
            self.assertEqual([e["scope"] for e in settings_effects.apply(["panelHotkey"])], [])
            self.assertEqual([e["scope"] for e in settings_effects.apply(["wakeEnabled"])], ["wake"])
            self.assertEqual([e["scope"] for e in settings_effects.apply(["wakeKeywords"])], ["wake"])
            self.assertEqual([e["scope"] for e in settings_effects.apply(["routerProbeInterval"])],
                             ["router"])
            self.assertEqual([e["scope"] for e in settings_effects.apply(["agentBackend"])], ["agent"])
            self.assertEqual([e["scope"] for e in settings_effects.apply(["harnessPort"])], ["agent"])
            self.assertEqual([e["scope"] for e in settings_effects.apply([])], [])

    def test_router_failure_is_reported_not_raised(self):
        """路由失败**只回报**：抛不抛由调用方定（API 转 HTTP 400，向导只登记）。"""
        with patch("app.router_admin.apply_settings", lambda updated: (False, "端口被占")):
            out = settings_effects.apply(["routerProbeInterval"])
        self.assertEqual(out[0]["scope"], "router")
        self.assertFalse(out[0]["ok"])
        self.assertIn("端口被占", out[0]["detail"])

    def test_a_raising_dependency_never_escapes(self):
        """任何一段炸了都不许把异常抛给调用方 —— 否则向导会整场中断。"""
        with patch("app.router_admin.apply_settings", side_effect=RuntimeError("boom")):
            out = settings_effects.apply(["routerConnectTimeout"])
        self.assertFalse(out[0]["ok"])
        self.assertIn("boom", out[0]["detail"])


class WakeTests(unittest.TestCase):

    def test_restarts_listener_only_when_enabled(self):
        with patch("app.runtime.stop_wake") as stop, patch("app.runtime.start_wake") as start, \
                patch("app.config.settings.get",
                      lambda k, d=None: False if k == "wakeEnabled" else d):
            settings_effects.apply(["wakeEnabled"])
        stop.assert_called_once()
        start.assert_not_called()

    def test_starts_listener_when_enabled(self):
        with patch("app.runtime.stop_wake"), patch("app.runtime.start_wake") as start, \
                patch("app.config.settings.get",
                      lambda k, d=None: True if k == "wakeEnabled" else d):
            settings_effects.apply(["wakeEnabled"])
        start.assert_called_once()


class AgentTests(unittest.TestCase):
    """独立 harness 随选随起 / 随走随停。"""

    def test_launches_harness_with_the_given_timeout(self):
        """向导传的短超时必须原样传下去（它跑在 HTTP 请求线程里，不能卡 60s）。"""
        with patch("app.agents.reset"), \
                patch("app.harness_proc.requested", lambda: True), \
                patch("app.llm_router.sync", lambda: (True, "已注册到 标准版 harness")), \
                patch("app.harness_proc.ensure_running") as run:
            run.return_value = (True, "独立 harness 启动中")
            out = settings_effects.apply(["agentBackend"], harness_timeout=4.0)
        run.assert_called_once_with(timeout=4.0)
        self.assertTrue(out[0]["ok"])
        self.assertIn("harness", out[0]["detail"])

    def test_stops_harness_when_not_requested(self):
        with patch("app.agents.reset"), \
                patch("app.harness_proc.requested", lambda: False), \
                patch("app.llm_router.sync", lambda: (True, "已注册到 DSH 桌面版")), \
                patch("app.harness_proc.stop") as stop:
            settings_effects.apply(["agentBackend"])
        stop.assert_called_once()

    def test_launch_failure_is_reported(self):
        with patch("app.agents.reset"), \
                patch("app.harness_proc.requested", lambda: True), \
                patch("app.llm_router.sync", lambda: (True, "已注册到 标准版 harness")), \
                patch("app.harness_proc.ensure_running",
                      lambda **kw: (False, "找不到 npx：需要本机有 Node.js")):
            out = settings_effects.apply(["agentBackend"])
        self.assertFalse(out[0]["ok"])
        self.assertIn("npx", out[0]["detail"])

    def test_registers_echo_auto_after_the_agent_moves(self):
        """动完智能体要补一次 ECHO AUTO 注册（标准版的家目录是选中它之后才存在的）。

        不补这一次，只装标准版的机器要等到下次重启才能在 DSH 里选到 ECHO AUTO
        —— 同事不一定两个都装（2026-09-22）。
        """
        with patch("app.agents.reset"), \
                patch("app.harness_proc.requested", lambda: True), \
                patch("app.harness_proc.ensure_running", lambda **kw: (True, "已在运行")), \
                patch("app.llm_router.sync") as sync:
            sync.return_value = (True, "已注册到 标准版 harness")
            out = settings_effects.apply(["agentBackend"])
        sync.assert_called_once()
        self.assertIn("已注册到", out[0]["detail"])

    def test_registration_follows_the_auto_register_switch(self):
        """关掉「启动时注册到 DSH」就别写 —— 与 boot 那道闸用同一个开关。"""
        with patch("app.agents.reset"), \
                patch("app.harness_proc.requested", lambda: False), \
                patch("app.harness_proc.stop"), \
                patch("app.config.settings.get",
                      lambda k, d=None: False if k == "routerAutoRegister" else d), \
                patch("app.llm_router.sync") as sync:
            settings_effects.apply(["agentBackend"])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
