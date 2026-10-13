# -*- coding: utf-8 -*-
"""本地后端看门狗的契约（2026-10-11 用户要求"配置是本地后端时 watchdog 也要监控后端"）。

一条真后端都不碰：全程打桩（归属判据 / 端口探活 / 会议状态 / 拉起动作），
钉住的是那三条铁律 —— **不碰别人的**、**不打断会议**、**退避与上限**。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_watch                                        # noqa: E402


class _Case(unittest.TestCase):
    """每个用例前把状态清干净（模块级计数会跨用例留着）。"""

    def setUp(self):
        backend_watch._STATE.update(checks=0, ok=0, restarts=[], last={}, note="",
                                    paused_until=0.0, paused_note="")

    def _tick(self, *, wanted=(True, "ECHO 起的（有 pid 记录）"), healthy=(True, "两个口都在听", ""), busy="",
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
                                healthy=(False, "数据口 8900 没在听", "missing"))
        self.assertEqual("skip", out["action"], out)
        self.assertEqual(0, calls["start"], "别人的后端一个指头都不许碰")
        self.assertIn("不归我管", out["detail"])

    def test_the_pairing_is_read_not_only_the_pid_record(self):
        """**pid 记录会被"过期清理"抹掉**（2026-10-11 真机复现）→ 还要认"本机配对还在"。

        2026-10-13 起判据改成配置驱动，`wanted()` 拆成 `_slot_wants_capability()` /
        `_backend_target()` / `_is_loopback()` 三处，所以这一条改成看**模块**里有没有那条
        配对判据（函数体的字面量搬家了，判据本身没搬）；行为面由
        `WantedIsConfigDrivenTests` 钉住 —— 回环配对、**没有** pid 记录时照样守。
        """
        import inspect
        src = inspect.getsource(backend_watch)
        self.assertIn("pairFile", src, "要认本机配对（不能只认 pid 记录）")
        self.assertIn("def _is_loopback", src, "回环判据只许有一处（三处各写一遍迟早对不上）")
        self.assertIn("pid 记录不再是充分条件", inspect.getsource(backend_watch.wanted))

    def test_a_healthy_backend_is_left_alone(self):
        out, calls = self._tick()
        self.assertEqual("ok", out["action"], out)
        self.assertEqual(0, calls["start"])
        self.assertEqual(1, backend_watch.state()["ok"])

    def test_it_restarts_our_own_backend_when_a_port_is_gone(self):
        out, calls = self._tick(healthy=(False, "数据口 8900 没在听", "missing"))
        self.assertEqual("restart", out["action"], out)
        self.assertEqual(1, calls["start"])
        self.assertIn("8900", out["detail"])
        self.assertEqual(1, backend_watch.state(now=1000.0)["restarts"])

    def test_it_does_not_interrupt_a_meeting(self):
        """**铁律 2**：会议在录音时不重启（同"动手前先看 meeting.active"）。"""
        out, calls = self._tick(healthy=(False, "数据口 8900 没在听", "missing"), busy="会议正在录音")
        self.assertEqual("busy", out["action"], out)
        self.assertEqual(0, calls["start"], "录音期间不许动后端")

    def test_it_backs_off_after_a_failed_attempt(self):
        """**铁律 3**：失败后退避 —— 不许一秒一次把 3 GB 的运行时拉成死循环。"""
        first, calls = self._tick(healthy=(False, "数据口 8900 没在听", "missing"), now=1000.0)
        self.assertEqual("restart", first["action"])
        again, calls2 = self._tick(healthy=(False, "数据口 8900 没在听", "missing"), now=1010.0)   # 10 秒后
        self.assertEqual("backoff", again["action"], again)
        self.assertEqual(0, calls2["start"], "退避期间不许再拉")
        later, calls3 = self._tick(healthy=(False, "数据口 8900 没在听", "missing"), now=1100.0)  # 100 秒后
        self.assertEqual("restart", later["action"], later)
        self.assertEqual(1, calls3["start"])

    def test_it_stops_after_the_hourly_cap_and_says_so(self):
        """**铁律 3**：一小时内 3 次之后不再自动拉，改成"需要你看一眼"。"""
        seen = []
        # 每次只隔约 17 分钟 —— 四次都落在同一个"一小时窗口"里，才会触发上限
        for i in range(4):
            out, calls = self._tick(healthy=(False, "数据口 8900 没在听", "missing"), now=1000.0 + i * 1000.0)
            seen.append((out["action"], calls["start"]))
        self.assertEqual(["restart"] * 3 + ["capped"], [a for a, _ in seen])
        self.assertEqual([1, 1, 1, 0], [c for _, c in seen], "第 4 次不许再动手")
        self.assertIn("需要你看一眼", backend_watch.state(now=4000.0)["note"])

    def test_a_failed_direct_call_does_not_raise(self):
        """拉起接口自己抛异常时，看门狗必须活着（它还是个循环）。"""
        out, _ = self._tick(healthy=(False, "数据口 8900 没在听", "missing"),
                            start_result=(False, "缺运行时"))
        self.assertEqual("backoff", out["action"], out)
        self.assertIn("缺运行时", out["detail"])

    def test_healthy_requires_both_ports_and_our_own_backend(self):
        """**两个口都要探，而且得是本棵树的后端** —— 用户实测的形态正是"只有管理口在听"；
        另外，"端口被别人的进程占着"必须判成 foreign（不能当健康、也不能去瞎拉）。"""
        seen = []

        def _owner(port, with_root=False):
            seen.append(int(port))
            if int(port) == 8900:
                return {"pid": 0}
            return {"pid": 4242, "is_ours": True, "foreign": False}

        with patch("app.backend_proc.port_owner", _owner), \
                patch("app.backend_setup.configured_ports", lambda: (8900, 8901)):
            ok, why, kind = backend_watch.healthy()
        self.assertFalse(ok, why)
        self.assertEqual("missing", kind)
        self.assertEqual([8900, 8901], seen, "两个口都要探")
        self.assertIn("8900", why)

    def test_both_ports_held_by_a_foreign_process_is_reported(self):
        def _owner(port, with_root=False):
            return {"pid": 999, "is_ours": False, "foreign": True}

        with patch("app.backend_proc.port_owner", _owner), \
                patch("app.backend_setup.configured_ports", lambda: (8900, 8901)):
            ok, why, kind = backend_watch.healthy()
        self.assertFalse(ok, why)
        self.assertEqual("foreign", kind, why)
        self.assertIn("别的", why)

    def test_an_explicit_stop_is_respected(self):
        """**明确停过就别马上拉**：否则用户点了「停止后端」，30 秒后它又自己回来
        （而且"跑门禁前先停后端"这条纪律也就不成立了 —— 8900/8901 抢不到空）。"""
        import time as _t
        backend_watch.pause("你点了「停止后端」", seconds=1800)
        out, calls = self._tick(healthy=(False, "数据口 8900 没在听", "missing"), now=_t.time() + 10)
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

    def test_a_foreign_process_on_the_port_is_reported_not_fought(self):
        """端口被**别人的/残留的**进程占着 → 别去拉（只会失败），如实报出来。

        现实来源：门禁泄漏过一个 dev 树的 `server.main` 占着 8900/8901，
        只探端口的判据把稳定版的后端判成"健康"（而真后端是停的）。
        """
        out, calls = self._tick(healthy=(False, "数据口 8900 被**别的**进程占着（pid 999，不是本棵树的）", "foreign"))
        self.assertEqual("foreign", out["action"], out)
        self.assertEqual(0, calls["start"], "别人的进程占着端口时拉起必然失败 —— 不许瞎拉")
        self.assertIn("需要你看一眼", out["detail"])

    def test_it_refuses_to_start_inside_a_test_process(self):
        """**测试进程里一律不起** —— 实测：它会在测试进程里尽职地真去拉后端，泄漏一个进程、
        还把测试的 sqlite 锁住（`database is locked`，门禁因此红）。"""
        why = backend_watch.disabled()
        self.assertTrue(why, "测试进程里必须被认出来")
        self.assertIn("测试进程", why)
        self.assertFalse(backend_watch.start(), "不许在测试进程里起线程")

    def test_the_env_switch_disables_it(self):
        """`ECHO_NO_BACKEND_WATCH=1` 显式关掉 —— 门禁可以用它避开"停了又被拉起来"。"""
        with patch.dict("os.environ", {"ECHO_NO_BACKEND_WATCH": "1"}):
            self.assertIn("显式关掉", backend_watch.disabled())

    def test_state_is_readable_and_honest(self):
        st = backend_watch.state()
        for k in ("checks", "ok", "restarts", "last", "note", "interval", "maxPerHour"):
            self.assertIn(k, st)
        self.assertIn("wanted", st, "面板要能看到「最后一次该不该守」的结论")


class WantedIsConfigDrivenTests(_Case):
    """看门狗"守不守"由**配置项**决定（2026-10-13 用户："守护内容需要根据配置项确认"）。

    旧版只有一条判据 `backend_pid.read_pid() > 0`（ECHO 起过它）—— 于是**改用网络后端的
    机器上，那个本机后端被 pid 记录永远看护着**：杀了 30 秒又被拉回来，一直占着 8900/8901
    （2026-10-13 现场）。新版按配置回答：三个槽要不要能力后端 + 这棵树连的是谁
    （配对文件优先，隐藏设置兜底）+ 本机后端配置齐不齐。这一批把每条判据逐一钉住。
    """

    def _wanted(self, *, pair_url="", paired=True, runtime=True, cfg_exists=True, pid=0,
                slots=("echo-server", "auto", "auto"), echo_url=""):
        # Isolate the PAIRING FILE too: `_backend_target()` reads `<data root>/data/backend.json`
        # itself, and this machine really is paired (with a network backend) - reading that
        # would turn every "no clue" case into a false "remote".
        tmp = tempfile.mkdtemp(prefix="echo-watch-")
        self.addCleanup(shutil.rmtree, tmp, True)
        view = {
            "pairFile": {"found": bool(pair_url), "baseUrl": pair_url},
            "paired": {"paired": paired, "baseUrl": pair_url},
            "runtime": {"ready": runtime},
            "config": {"exists": cfg_exists},
        }
        conf = {"capabilityMeetingAsrBackend": slots[0],
                "capabilityDiarizeBackend": slots[1],
                "capabilityEmbedBackend": slots[2],
                "capabilityEchoServerUrl": echo_url}
        with patch("app.backend_admin.view", lambda: view), \
                patch("app.paths.data_root", lambda: tmp), \
                patch("app.backend_pid.read_pid", lambda: pid), \
                patch("app.config.settings.get", lambda k, d=None: conf.get(k, d)):
            return backend_watch.wanted()

    def test_a_remote_pairing_is_not_guarded_even_with_a_pid_record(self):
        """**这条就是 2026-10-13 的回归**：配的是远端后端，pid 记录不许再把它变成"该守"。"""
        want, why = self._wanted(pair_url="http://221.216.62.2:8900", pid=28876)
        self.assertFalse(want, why)
        self.assertIn("远端", why)
        self.assertIn("28876", why, "要如实报出那个残留记录，人才知道去哪停")

    def test_a_loopback_pairing_is_guarded_without_any_pid_record(self):
        """配对指向回环 = 用的就是本机后端 → 该守（pid 记录有没有都不影响）。"""
        want, why = self._wanted(pair_url="http://127.0.0.1:8900", pid=0)
        self.assertTrue(want, why)
        self.assertIn("本机后端", why)

    def test_no_pairing_but_a_pid_record_and_a_configured_backend_is_guarded(self):
        """没有任何配对线索时，pid 记录 + 本机后端配置齐 才算"我们起的"。"""
        want, why = self._wanted(pair_url="", paired=False, pid=99,
                                 runtime=True, cfg_exists=True)
        self.assertTrue(want, why)
        self.assertIn("99", why)

    def test_a_pid_record_without_a_configured_backend_is_not_guarded(self):
        """有 pid 记录但本机后端没配置齐 → 判不出，不许动手。"""
        want, why = self._wanted(pair_url="", paired=False, pid=99,
                                 runtime=False, cfg_exists=True)
        self.assertFalse(want, why)
        want2, _why2 = self._wanted(pair_url="", paired=False, pid=99,
                                    runtime=True, cfg_exists=False)
        self.assertFalse(want2)

    def test_no_pairing_and_no_pid_record_is_not_guarded(self):
        want, why = self._wanted(pair_url="", paired=False, pid=0)
        self.assertFalse(want, why)
        self.assertIn("不碰", why)

    def test_slots_that_need_no_capability_backend_win_over_everything(self):
        """三个槽都不要能力后端 → 本机后端毫无用处：哪怕配对在回环、还有 pid 记录也不守。"""
        want, why = self._wanted(pair_url="http://127.0.0.1:8900", pid=123,
                                 slots=("asr-provider", "off", "off"))
        self.assertFalse(want, why)
        self.assertIn("三个槽都不要", why)

    def test_the_hidden_setting_is_the_fallback_without_a_pairing(self):
        """没有配对记录时看隐藏设置 `capabilityEchoServerUrl`：远端 → 不守。"""
        want, why = self._wanted(pair_url="", paired=False, pid=5,
                                 echo_url="http://10.100.0.24:8900")
        self.assertFalse(want, why)
        self.assertIn("设置", why)

    def test_tick_records_the_decision_for_the_panel(self):
        """`tick()` 把结论存进 state（面板读它）—— `state()` 自己不能现算：会递归。"""
        out, calls = self._tick(wanted=(False, "配对指向远端后端（http://10.100.0.24:8900）"),
                                healthy=(False, "数据口 8900 没在听", "missing"))
        st = backend_watch.state(now=1000.0)
        self.assertEqual("skip", out["action"], out)
        self.assertEqual(0, calls["start"], "配置说不需要时，一个指头都不许动")
        self.assertIs(st["wanted"]["want"], False)
        self.assertIn("远端", st["wanted"]["why"])


if __name__ == "__main__":
    unittest.main()
