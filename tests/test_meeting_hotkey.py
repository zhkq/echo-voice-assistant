# -*- coding: utf-8 -*-
"""tests/test_meeting_hotkey.py — 会议热键与「仪表盘显示模型路由」开关（2026-10-02）

用户《ECHO边条页签功能设计》里：
  * 「设置 → 快捷键 → 会议热键」要能**开始录制 / 结束录制**；
  * 「设置 → 通用 → 高级」里的「仪表盘是否展示模型路由」**默认关**。

这里只验**判据**，不起真监听线程（AGENTS 记过一次教训：`test_runtime_hotkey` 真起过
`HotkeyListener`，漏线程还占住全局热键）——所以：
  * 派发只在进程内调 `runtime._hotkey_cb(...)` / `runtime.meeting_hotkey(...)`；
  * `meeting.start_meeting` / `stop_meeting` / `services.report_meeting` 全部换替身。
"""
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import runtime  # noqa: E402
from app.config import DEFAULTS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MeetingHotkeySettingTests(unittest.TestCase):

    def test_both_keys_exist_and_default_to_empty(self):
        """默认空 = **不注册**（两个平台实现都会跳过空组合键），别出厂就占用户键位。"""
        for key in ("meetingStartHotkey", "meetingStopHotkey"):
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS, "少了设置项 %s" % key)
                self.assertEqual(DEFAULTS[key]["value"], "", "%s 的出厂值应当是空串" % key)
                self.assertEqual(DEFAULTS[key]["value_type"], "str")
                self.assertEqual(DEFAULTS[key]["grp"], "meeting", "它属于会议那一族")

    def test_both_platforms_register_them(self):
        """Windows 与 macOS/Linux 各有**一份**注册清单 —— 别只加一边（另一边静默失效）。"""
        win = io.open(os.path.join(ROOT, "app", "platform", "win32", "hotkey.py"),
                      encoding="utf-8").read()
        for key in ("meetingStartHotkey", "meetingStopHotkey"):
            with self.subTest(platform="win32", key=key):
                self.assertIn('"%s"' % key, win, "win32 的注册清单里少了 %s" % key)
        from app.platform import _posix_hotkey
        for key in ("meetingStartHotkey", "meetingStopHotkey"):
            with self.subTest(platform="posix", key=key):
                self.assertIn(key, _posix_hotkey.HOTKEY_SETTING_KEYS,
                              "posix 的注册清单里少了 %s" % key)


class MeetingHotkeyDispatchTests(unittest.TestCase):

    def setUp(self):
        self.calls = []
        self.reports = []
        self._patches = [
            patch("app.meeting.start_meeting", lambda: (self.calls.append("start"), (True, "已开始"))[1]),
            patch("app.meeting.stop_meeting", lambda: (self.calls.append("stop"), (True, "已结束"))[1]),
            patch("app.services.report_meeting",
                  lambda status, detail="": self.reports.append((status, detail))),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_callback_routes_the_two_keys(self):
        runtime._hotkey_cb("hotkey", "meetingStartHotkey")
        runtime._hotkey_cb("hotkey", "meetingStopHotkey")
        self.assertEqual(self.calls, ["start", "stop"])
        self.assertEqual([s for s, _ in self.reports], ["active", "transcribing"],
                         "报给组件状态的口径要和 /api/meeting/start|stop 一致")

    def test_other_hotkeys_still_go_to_the_assistant(self):
        """加分支不许把原来的 wake/fallback 热键带走 —— 它们仍走 assistant.capture。"""
        with patch("app.assistant.capture") as cap:
            runtime._hotkey_cb("hotkey", "wakeHotkey")
            runtime._hotkey_cb("hotkey", "fallbackHotkey")
            self.assertEqual(cap.call_count, 2, "wake/fallback 热键必须仍然触发收音")

    def test_meeting_hotkey_never_raises(self):
        """热键跑在监听线程里：抛出去会把监听带停，所以这里必须吞掉并返回 False。"""
        with patch("app.meeting.start_meeting", side_effect=RuntimeError("设备被占用")):
            ok = runtime.meeting_hotkey("start")
        self.assertFalse(ok)

    def test_start_refused_by_the_meeting_layer_is_reported_honestly(self):
        """会议层拒绝（比如已经在录）时不要谎报 active。"""
        with patch("app.meeting.start_meeting", return_value=(False, "已经在录音了")):
            ok = runtime.meeting_hotkey("start")
        self.assertFalse(ok)
        self.assertEqual(self.reports[-1][0], "idle", "失败就不能报成 active")


class DashboardRouterToggleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with io.open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_setting_defaults_to_off(self):
        self.assertIn("dashboardShowRouter", DEFAULTS)
        self.assertFalse(DEFAULTS["dashboardShowRouter"]["value"], "用户口径：默认关")
        self.assertEqual(DEFAULTS["dashboardShowRouter"]["value_type"], "bool")

    def test_it_sits_in_the_general_card_advanced_section(self):
        gen = self.js[self.js.index('{ id: "gen", title: "通用"'):]
        gen = gen[:gen.index("},")]
        self.assertIn('"dashboardShowRouter"', gen)
        self.assertIn('advOrder: ["面板鉴权"]', gen)
        self.assertIn("dashboardShowRouter: \"面板鉴权\"", self.js)

    def test_the_dashboard_hides_the_router_row_and_block_when_off(self):
        fn = self.js[self.js.index("function applyDashboardRouterVisibility()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('settingValue("dashboardShowRouter")', fn)
        self.assertIn('$("#foWrap")', fn, "关着时要收起路由统计块")
        self.assertIn("hidden", fn)

    def test_the_toggle_is_applied_after_the_rows_are_rendered(self):
        fn = self.js[self.js.index("async function refreshRunStatus()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("applyDashboardRouterVisibility()", fn)
        self.assertLess(fn.index("host.innerHTML"), fn.index("applyDashboardRouterVisibility()"))

    def test_settings_are_loaded_before_reading_the_toggle(self):
        """开机第一屏就是仪表盘时 `_settingsCache` 是空的 —— 必须先加载，否则开关永远读成"关"。"""
        fn = self.js[self.js.index("async function refreshRunStatus()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("await loadSettings()", fn)


class HotkeyReregisterEffectTests(unittest.TestCase):
    """改热键设置要**重挂监听**才生效（组合键是监听器启动时读一次注册的）。

    2026-10-02 补这条联动：以前只有 `wake*` 那条，而且它只看 `wakeEnabled` ——
    于是改了「仪表盘热键」或新加的「会议热键」都得重启 ECHO 才生效，表现就是"设了没用"。
    """

    def test_changing_a_hotkey_setting_re_registers_the_listener(self):
        from app import settings_effects
        with patch("app.runtime.stop_hotkey") as stop, \
                patch("app.runtime.start_hotkey", return_value=(True, "热键监听已启动")):
            out = settings_effects.apply(["meetingStartHotkey"])
        self.assertTrue(stop.called, "改组合键必须先停掉旧注册")
        self.assertEqual(out[-1]["scope"], "hotkey")
        self.assertTrue(out[-1]["ok"])
        self.assertIn("重挂", out[-1]["detail"])

    def test_unrelated_settings_do_not_touch_the_hotkey_listener(self):
        from app import settings_effects
        with patch("app.runtime.stop_hotkey") as stop, \
                patch("app.runtime.start_hotkey") as start:
            settings_effects.apply(["meetingSegmentMinutes"])
        self.assertFalse(stop.called, "无关设置不该重挂热键监听")
        self.assertFalse(start.called)

    def test_a_failing_restart_is_reported_not_raised(self):
        """热键重挂失败不许把 PUT /api/settings 打成 500 —— 设置本身已经写进去了。"""
        from app import settings_effects
        with patch("app.runtime.stop_hotkey"), \
                patch("app.runtime.start_hotkey", side_effect=RuntimeError("被占用")):
            out = settings_effects.apply(["panelHotkey"])
        self.assertFalse(out[-1]["ok"])
        self.assertIn("RuntimeError", out[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
