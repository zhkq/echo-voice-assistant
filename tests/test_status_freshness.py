# -*- coding: utf-8 -*-
"""状态格**不许说陈旧的话**（2026-10-11 用户实测：43199 空着，面板还说"运行中"）。

机制：`api_status` 的设计是"状态取自组件表、不在这里探活"（这是有道理的 —— 它被高频轮询，
而 codebuddy 那种"探一次就起一个进程"的适配器不能被拖进来）。但 **harness 的探活只是一次
HTTP 探测**（不起进程），所以它必须**在读取时校准一次**；否则"别人把它停了"
（切换器切树 / 手工杀 / 它自己崩了）之后没有任何人重写那一行，状态就永远是 online。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                            # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from app import api as api_mod                                          # noqa: E402
from app import harness_proc, services                                  # noqa: E402
from app.api import router                                              # noqa: E402
from tests.auth_off import api_auth_off                                 # noqa: E402


class StatusFreshnessTests(unittest.TestCase):
    def _status(self, selected):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        seen = {"synced": 0}

        def _sync():
            seen["synced"] += 1

        with api_auth_off(), \
                patch("app.agents.selected_name", lambda: selected), \
                patch.object(harness_proc, "sync_status", _sync):
            r = client.get("/api/status")
        return r, seen

    def test_harness_status_is_recalibrated_on_read(self):
        r, seen = self._status("harness")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(1, seen["synced"], "读 /api/status 时要校准一次 harness 的状态行")

    def test_a_process_spawning_agent_is_never_probed_on_this_hot_path(self):
        """codebuddy 那种"探一次就起一个进程"的适配器**绝不能**在这个接口里被探 —— 它被高频轮询。"""
        r, seen = self._status("codebuddy")
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(0, seen["synced"], "别的智能体不该被顺带校准（那会起进程）")


if __name__ == "__main__":
    unittest.main()
