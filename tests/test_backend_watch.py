# -*- coding: utf-8 -*-
"""本地后端看门狗的契约（2026-10-11 用户要求"配置是本地后端时 watchdog 也要监控后端"）。

一条真后端都不碰：全程打桩（归属判据 / 端口探活 / 会议状态 / 拉起动作），
钉住的是那三条铁律 —— **不碰别人的**、**不打断会议**、**退避与上限**。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_watch                                        # noqa: E402


class _Case(unittest.TestCase):
    """每个用例前把状态清干净（模块级计数会跨用例留着）。"""

    def setUp(self):
        backend_watch._STATE.update(checks=0, ok=0, restarts=[], last={}, note="",
                                    paused_until=0.0, paused_note="")

    def _tick(self, *, wanted=(True, "ECHO 起的（有 pid 记录）"), healthy=(True, "两个口都在听"), busy="",
              start_result=(True, "已开始「起本机后端」"), now=1000.0):
        calls = {"start": 0}

        def _start(**kw):
            calls["start"] += 1
            return start_result

        with patch.object(backend_watch, "wanted", lambda: wanted), \
                patch.object(backend_watch, "healthy", lambda: healthy), \
                patch.object(backend_watch, "busy", lambda: busy), \
                patch("app.backend_admin.start", _start):
            out = backend_watch.tick(now=now)
        return out, calls


class RulesTests(_Case):
    def test_it_never_touches_a_backend_that_is_not_ours(self):
        """**铁律 1**：既没有 pid 记录、也没有本机配对 = 不归我们管 —— 判不出就不动手。"""
        out, calls = self._tick(wanted=(False, "既没有 pid 记录，也没有指向本机的配对 —— 不碰"),
                                healthy=(False, "数据口 8900 没在听"))
        self.assertEqual("skip", out["action"], out)
        self.assertEqual(0, calls["start"], "别人的后端一个指头都不许碰")
        self.assertIn("不归我管", out["detail"])

    def test_the_pid_record_alone_is_enough(self):
        """**pid 记录会被"过期清理"抹掉**（2026-10-11 真机复现）→ 还要认"本机配对还在"这一条。"""
        import inspect
        src = inspect.getsource(backend_watch.wanted)
        self.assertIn("pairFile", src, "要认本机配对（不能只认 pid 记录）")
        self.assertIn("loopback", src)

    def test_a_healthy_backend_is_left_alone(self):
        out, calls = self._tick()
        self.assertEqual("ok", out["action"], out)
        self.assertEqual(0, calls["start"])
        self.assertEqual(1, backend_watch.state()["ok"])

    def test_it_restarts_our_own_backend_when_a_port_is_gone(self):
        out, calls = self._tick(healthy=(False, "数据口 8900 没在听"))
        self.assertEqual("restart", out["action"], out)
        self.assertEqual(1, calls["start"])
        self.assertIn("8900", out["detail"])
        self.assertEqual(1, backend_watch.state(now=1000.0)["restarts"])

    def test_it_does_not_interrupt_a_meeting(self):
        """**铁律 2**：会议在录音时不重启（同"动手前先看 meeting.active"）。"""
        out, calls = self._tick(healthy=(False, "数据口 8900 没在听"), busy="会议正在录音")
        self.assertEqual("busy", out["action"], out)
        self.assertEqual(0, calls["start"], "录音期间不许动后端")

    def test_it_backs_off_after_a_failed_attempt(self):
        """**铁律 3**：失败后退避 —— 不许一秒一次把 3 GB 的运行时拉成死循环。"""
        first, calls = self._tick(healthy=(False, "数据口 8900 没在听"), now=1000.0)
        self.assertEqual("restart", first["action"])
        again, calls2 = self._tick(healthy=(False, "数据口 8900 没在听"), now=1010.0)   # 10 秒后
        self.assertEqual("backoff", again["action"], again)
        self.assertEqual(0, calls2["start"], "退避期间不许再拉")
        later, calls3 = self._tick(healthy=(False, "数据口 8900 没在听"), now=1100.0)  # 100 秒后
        self.assertEqual("restart", later["action"], later)
        self.assertEqual(1, calls3["start"])

    def test_it_stops_after_the_hourly_cap_and_says_so(self):
        """**铁律 3**：一小时内 3 次之后不再自动拉，改成"需要你看一眼"。"""
        seen = []
        # 每次只隔约 17 分钟 —— 四次都落在同一个"一小时窗口"里，才会触发上限
        for i in range(4):
            out, calls = self._tick(healthy=(False, "数据口 8900 没在听"), now=1000.0 + i * 1000.0)
            seen.append((out["action"], calls["start"]))
        self.assertEqual(["restart"] * 3 + ["capped"], [a for a, _ in seen])
        self.assertEqual([1, 1, 1, 0], [c for _, c in seen], "第 4 次不许再动手")
        self.assertIn("需要你看一眼", backend_watch.state(now=4000.0)["note"])

    def test_a_failed_direct_call_does_not_raise(self):
        """拉起接口自己抛异常时，看门狗必须活着（它还是个循环）。"""
        out, _ = self._tick(healthy=(False, "数据口 8900 没在听"),
                            start_result=(False, "缺运行时"))
        self.assertEqual("backoff", out["action"], out)
        self.assertIn("缺运行时", out["detail"])

    def test_healthy_checks_both_ports(self):
        """**两个口都要探** —— 用户实测的形态正是"只有管理口在听"。"""
        seen = []

        def _pid(port):
            seen.append(int(port))
            return 0 if int(port) == 8900 else 4242

        with patch("app.platform.listening_pid", _pid), \
                patch("app.backend_setup.configured_ports", lambda: (8900, 8901)):
            ok, why = backend_watch.healthy()
        self.assertFalse(ok, why)
        self.assertEqual([8900, 8901], seen, "两个口都要探")
        self.assertIn("8900", why)

    def test_an_explicit_stop_is_respected(self):
        """**明确停过就别马上拉**：否则用户点了「停止后端」，30 秒后它又自己回来
        （而且"跑门禁前先停后端"这条纪律也就不成立了 —— 8900/8901 抢不到空）。"""
        import time as _t
        backend_watch.pause("你点了「停止后端」", seconds=1800)
        out, calls = self._tick(healthy=(False, "数据口 8900 没在听"), now=_t.time() + 10)
        self.assertEqual("skip", out["action"], out)
        self.assertEqual(0, calls["start"], "明确停过的这段时间里不许自动拉起")
        self.assertIn("明确停过", out["detail"])
        self.assertTrue(backend_watch.state()["paused"])

    def test_a_healthy_backend_clears_the_pause(self):
        import time as _t
        backend_watch.pause("x", seconds=60)
        out, _ = self._tick(now=_t.time() + 120)          # 退避过期 + 它现在是好的
        self.assertEqual("ok", out["action"], out)
        self.assertFalse(backend_watch.state()["paused"], "它好了就该把退避清掉")

    def test_state_is_readable_and_honest(self):
        st = backend_watch.state()
        for k in ("checks", "ok", "restarts", "last", "note", "interval", "maxPerHour"):
            self.assertIn(k, st)


if __name__ == "__main__":
    unittest.main()
