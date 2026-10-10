# -*- coding: utf-8 -*-
"""tests/test_control_echo.py — 「关闭 ECHO」那把闸（2026-10-02）

`POST /api/control/echo/stop` 这个端点早就存在，但**谁都拦不住**：脚本或面板一调就停。
而"录音到一半被停掉"正是 2026-09-23 那次事故的形状（用户白录一场）。
现在它与 `/system/restart` 用**同一把闸**：
  * `assistant.is_busy()` 为真 → 拒（理由要说"有命令正在处理"）；
  * `meeting.meeting_status()['active']` 为真 → 拒（理由要说"正在录音"）；
  * 都干净 → 才真的走到 `manager.echo_stop_self`（这里用替身接住，**绝不停真服务**）。

⚠️ 第三个用例会起一个 0.5 秒的 `threading.Timer`（端点就是这么实现的），
所以 `echo_stop_self` 必须**全程是替身** —— 真跑一次会把开发机上正在跑的 ECHO 关掉。
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.api as api_mod  # noqa: E402
import app.assistant as assistant_mod  # noqa: E402
import app.meeting as meeting_mod  # noqa: E402


class EchoStopGuardTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(api_mod.router)
        cls.client = TestClient(app)

    def setUp(self):
        self.called = threading.Event()
        self._orig = (assistant_mod.is_busy, meeting_mod.meeting_status,
                      api_mod.manager.echo_stop_self)
        assistant_mod.is_busy = lambda: False
        meeting_mod.meeting_status = lambda: {"active": False}
        api_mod.manager.echo_stop_self = lambda *a, **k: self.called.set()

    def tearDown(self):
        (assistant_mod.is_busy, meeting_mod.meeting_status,
         api_mod.manager.echo_stop_self) = self._orig

    def test_refuses_while_a_command_is_running(self):
        assistant_mod.is_busy = lambda: True
        r = self.client.post("/api/control/echo/stop").json()
        self.assertFalse(r["ok"], r)
        self.assertIn("命令", r["message"])
        self.assertFalse(self.called.wait(1.0), "被拒的时候绝不能真去关服务")

    def test_refuses_while_recording(self):
        meeting_mod.meeting_status = lambda: {"active": True, "folder": "2026-10-02_x"}
        r = self.client.post("/api/control/echo/stop").json()
        self.assertFalse(r["ok"], r)
        self.assertIn("录音", r["message"])
        self.assertFalse(self.called.wait(1.0), "正在录音时绝不能真去关服务")

    def test_stops_when_idle(self):
        r = self.client.post("/api/control/echo/stop").json()
        self.assertTrue(r["ok"], r)
        self.assertIn("关闭", r["message"])
        self.assertTrue(self.called.wait(2.0), "空闲时必须真的走到关服务那一步（0.5s 定时器）")

    def test_the_guard_reads_like_the_restart_one(self):
        """两个"停服"入口的闸必须是同一套判据（免得一个拦一个不拦）。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "api.py"), encoding="utf-8").read()
        restart = src[src.index('@router.post("/system/restart")'):]
        restart = restart[:restart.index("@router", 10)]
        stop = src[src.index('@router.post("/control/echo/stop")'):]
        stop = stop[:stop.index("@router", 10)]
        for body, name in ((restart, "restart"), (stop, "echo/stop")):
            with self.subTest(endpoint=name):
                self.assertIn("assistant.is_busy()", body)
                self.assertIn("meeting_status()", body)
                self.assertIn("正在录音中，请先结束录音", body)


if __name__ == "__main__":
    unittest.main()


# --- 2026-10-11: 门禁在**开发者这台机器**上跑时 apiAuthEnabled=true（配对过手机，
# 且 serverBindMode=lan 不让关），而裸 TestClient 的对端是 "testclient"、netguard 判不出
# 回环 → fail closed → /api/* 一律 401，用例却期望 200。这里在**内存里**遮掉这一项
# （绝不写库 —— 写库会动用户真实的配对设置），说明见 tests/auth_off.py。
from tests.auth_off import install_for_module as _auth_off_install


def setUpModule():
    _auth_off_install(globals())


def tearDownModule():
    _auth_off_install(globals())
