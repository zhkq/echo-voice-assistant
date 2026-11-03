# -*- coding: utf-8 -*-
"""状态格**不许说陈旧的话**（2026-10-11 用户实测：43199 空着，面板还说"运行中"）。

机制：`api_status` 的设计是"状态取自组件表、不在这里探活"（高频轮询 —— codebuddy 那种
"探一次就起一个进程"的适配器不能被拖进来），但**唯一那行 DSH 的探活只是一次 HTTP 探测**
（不起进程），所以读取时要校准一次；否则"它被停了/崩了"之后面板会一直写"运行中"。

2026-11-03 一套 DSH：校准对象从"harness 行"变成唯一那行 dsh（桌面版或 ECHO 起的无头）。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                            # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from app import api as api_mod                                          # noqa: E402
from app import harness_proc, manager, services                         # noqa: E402
from app.api import router                                              # noqa: E402
from tests.auth_off import api_auth_off                                 # noqa: E402


class StatusFreshnessTests(unittest.TestCase):
    def _status(self, desktop_ok=False, headless_ok=False, gui_running=False):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        with api_auth_off(), \
                patch.object(manager, "dsh_ready", lambda: desktop_ok), \
                patch.object(harness_proc, "desktop_gui_running", lambda: gui_running), \
                patch.object(harness_proc, "yield_to_desktop",
                             lambda: {"yielded": False}), \
                patch.object(harness_proc, "online", lambda timeout=1.0: headless_ok):
            return client.get("/api/status")

    def test_the_dsh_row_is_recalibrated_on_read(self):
        r = self._status(headless_ok=True)
        self.assertEqual(200, r.status_code, r.text)
        by_name = {c["name"]: c for c in r.json()["components"]}
        self.assertIn("dsh", by_name)
        self.assertEqual(by_name["dsh"]["status"], "online",
                         "读 /api/status 时要按实际探活校准唯一那行 DSH")
        self.assertTrue(r.json()["dsh"]["online"])

    def test_a_stopped_engine_is_reported_offline(self):
        r = self._status(desktop_ok=False, headless_ok=False)
        self.assertEqual(200, r.status_code, r.text)
        by_name = {c["name"]: c for c in r.json()["components"]}
        self.assertEqual(by_name["dsh"]["status"], "offline")
        self.assertFalse(r.json()["dsh"]["online"])

    def test_this_hot_path_never_probes_other_agents(self):
        """codebuddy 那种"探一次就起一个进程"的适配器**绝不能**在这个接口里被探。"""
        with patch("app.agents.list_agents") as probe:
            self._status()
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
