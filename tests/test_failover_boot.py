# -*- coding: utf-8 -*-
"""D25：ECHO AUTO 的"注册进 DSH"只在装了 `agent-dsh` 时才自动执行

D25 原文：多上游派发路由属于主包（`dsh-failover/` 是 ECHO 自己的代码），重构为
**LLM provider 的一种实现**（`openai-compat + failover`）；"注册进 DSH 配置"那一半改为
**仅在装了 `agent-dsh` 时可选执行**。

为什么这道闸是对的：把模型组写进 `~/.dsh/settings.yaml` + `.credentials.yaml` 是**给 agent 用的**
—— 没装 agent 时写了没人读，还平白在用户家里改配置文件；而**路由本身照常运行**
（它是 LLM provider `echo-auto`，纪要与命令可以直接经它直连上游，P5 已打通）。

本文件钉住 `boot._start_failover` 的四种分支与 `_agent_dsh_available()` 的判据。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.boot as boot                                       # noqa: E402


def _report_collector():
    calls = []

    def report(**kw):
        calls.append(kw)
        return kw

    return calls, report


class AgentGateTests(unittest.TestCase):
    def test_missing_agent_adapter_is_reported(self):
        with patch("app.agents.names", lambda: []):
            ok, why = boot._agent_dsh_available()
        self.assertFalse(ok)
        self.assertIn("未安装 agent-dsh", why)

    def test_availability_comes_from_the_adapter(self):
        class _A:
            def available(self, probe=False):
                return False, "鉴权失败：请在 DSH Desktop 开启「普通浏览器访问」"

        with patch("app.agents.names", lambda: ["dsh"]), \
                patch("app.agents.get_agent", lambda name=None: _A()):
            ok, why = boot._agent_dsh_available()
        self.assertFalse(ok)
        self.assertIn("鉴权失败", why)

        class _B:
            def available(self, probe=False):
                return True, "API 可访问（http://127.0.0.1:43120）"

        with patch("app.agents.names", lambda: ["dsh"]), \
                patch("app.agents.get_agent", lambda name=None: _B()):
            ok, why = boot._agent_dsh_available()
        self.assertTrue(ok)
        self.assertIn("API 可访问", why)

    def test_detection_failure_is_not_fatal(self):
        with patch("app.agents.names", side_effect=RuntimeError("boom")):
            ok, why = boot._agent_dsh_available()
        self.assertFalse(ok)
        self.assertIn("检测 agent-dsh 失败", why)

    def test_the_reason_quoted_is_the_selected_agents(self):
        """失败原因要以**你选中的那个**为主（2026-09-22 同事反馈 3.5）。

        从前按固定顺序 ("dsh","harness") 收集原因再用「；」拼起来，于是选了标准版 harness
        的人会读到**桌面版**的原因「未找到 DSH 凭据文件 …（DSH Desktop 是否已登录过？）」——
        等于又把他往没选的那个上引（同一类误导本轮已出现三次：安装脚本判据、启动页、折叠条）。
        """

        class _Dead:
            def available(self, probe=False):
                return False, "未找到 DSH 凭据文件 C:\\Users\\x\\.dsh\\.credentials.yaml" \
                              "（DSH Desktop 是否已登录过？）"

        class _AlsoDead:
            def available(self, probe=False):
                return False, "标准版 harness 没在监听 http://127.0.0.1:43199"

        def _agent(name=None):
            return _AlsoDead() if name == "harness" else _Dead()

        with patch("app.agents.names", lambda: ["dsh", "harness"]), \
                patch.object(boot, "selected_agent", lambda: "harness"), \
                patch("app.agents.get_agent", _agent):
            ok, why = boot._agent_dsh_available()
        self.assertFalse(ok)
        self.assertIn("标准版 harness 没在监听", why, "没先说用户选中的那个")
        self.assertNotIn("DSH Desktop 是否已登录过", why,
                         "又拿没选的那个（桌面版）的原因去解释了")


class StartFailoverTests(unittest.TestCase):
    """`_start_failover` 的四种分支：路由失败 / 设置关掉 / 没装 agent / 装了 agent。"""

    def setUp(self):
        self.logs = []
        p = patch.object(boot.db, "add_log",
                         lambda level, source, msg: self.logs.append((level, msg)))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, guard=(True, "路由已就绪"), auto_register=True,
             agent=(True, "API 可访问"), sync=(True, "已写入 DSH"), sync_raises=None):
        calls, report = _report_collector()
        sync_calls = []

        def fake_sync():
            sync_calls.append(True)
            if sync_raises:
                raise sync_raises
            return sync

        with patch("app.failover_proxy.start_guard", lambda: guard), \
                patch.object(boot, "_agent_dsh_available", lambda: agent), \
                patch("app.config.settings.get",
                      lambda k, d=None: auto_register if k == "routerAutoRegister" else d), \
                patch("app.llm_router.sync", fake_sync):
            boot._start_failover(report)
        return calls, sync_calls

    def test_router_failure_is_failed_and_never_registers(self):
        calls, sync_calls = self._run(guard=(False, "端口被占"))
        self.assertEqual(calls[-1]["status"], "failed")
        self.assertEqual(sync_calls, [], "路由都没起来就不该谈注册")

    def test_setting_off_skips_registration(self):
        calls, sync_calls = self._run(auto_register=False)
        self.assertEqual(calls[-1]["status"], "online")
        self.assertIn("已按设置跳过", calls[-1]["detail"])
        self.assertEqual(sync_calls, [])

    def test_without_agent_nothing_is_written_to_dsh(self):
        """D25 的核心：没装 agent-dsh 就不动 DSH 的配置文件，但路由照常可用。"""
        calls, sync_calls = self._run(agent=(False, "未安装 agent-dsh（内置适配器未注册）"))
        self.assertEqual(sync_calls, [], "没装 agent 还去写 ~/.dsh/settings.yaml 就是错的")
        self.assertEqual(calls[-1]["status"], "online", "路由本身没问题，不能报 failed")
        self.assertIn("未注册进 DSH", calls[-1]["detail"])
        self.assertIn("路由本身可用", calls[-1]["detail"])
        self.assertTrue(any("跳过 ECHO AUTO 注册" in m for _lv, m in self.logs),
                        "跳过要有日志，别让人以为注册成功过：%s" % self.logs)

    def test_with_agent_registration_happens(self):
        calls, sync_calls = self._run()
        self.assertEqual(len(sync_calls), 1)
        self.assertIn("已写入 DSH", calls[-1]["detail"])
        self.assertEqual(calls[-1]["status"], "online")

    def test_registration_failure_does_not_fail_the_component(self):
        """注册失败只影响"DSH 里能不能选到 ECHO AUTO"，路由本身仍在线。"""
        calls, _ = self._run(sync=(False, "config.json 里没有 groups"))
        self.assertEqual(calls[-1]["status"], "online")
        self.assertIn("未注册", calls[-1]["detail"])

    def test_registration_exception_is_reported_in_the_detail(self):
        calls, _ = self._run(sync_raises=RuntimeError("ruamel 缺失"))
        self.assertEqual(calls[-1]["status"], "online")
        self.assertIn("注册异常", calls[-1]["detail"])
        self.assertIn("ruamel", calls[-1]["detail"])


class ProviderBoundaryTests(unittest.TestCase):
    """D25 的"路由 = LLM provider 的一种实现"这条边界要可查。"""

    def test_echo_auto_is_registered_as_a_llm_provider(self):
        from app import providers as P
        spec = P.describe("llm", "echo-auto")
        self.assertIsNotNone(spec, "多上游派发必须以 LLM provider 的形式存在")
        self.assertEqual(spec["kind"], "llm")
        self.assertTrue(spec["egress"], "内容会发到上游，必须标出网")
        self.assertIn("上游", spec["egress_note"])
        self.assertIn("echo-auto", [p["id"] for p in P.specs("llm")])

    def test_router_provider_is_usable_without_any_agent(self):
        """provider 的调用面不依赖 app.agents（纪要直连那条路就靠这个）。"""
        from app.providers import router as router_mod
        inst = router_mod.EchoAutoLlmProvider()
        self.assertTrue(callable(getattr(inst, "chat", None)))
        self.assertTrue(hasattr(inst, "base_url"))
        # ready() 只问路由进程，不碰 agent
        with patch("app.failover_proxy.proxy_online", lambda timeout=1.0: True), \
                patch("app.agents.get_agent",
                      side_effect=AssertionError("provider 不该碰 agent 注册表")):
            self.assertTrue(inst.ready())


if __name__ == "__main__":
    unittest.main()
