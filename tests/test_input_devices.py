# -*- coding: utf-8 -*-
"""按用途分输入设备（2026-09-23 需求："指令用耳机收音、会议用全向麦"）。

以前只有 `inputDeviceId` 一个键，会议与指令只能共用一个麦。现在：
  * `commandInputDeviceId` —— 指令 / 唤醒 / 麦克风按钮
  * `meetingInputDeviceId` —— 会议录音
  * 两者为 -1 时**跟随通用 `inputDeviceId`**（所以老配置零迁移，行为不变）
"""
import unittest
from unittest.mock import patch

from app import config
from app.audio import recorder


class ResolveInputDeviceTests(unittest.TestCase):
    def _res(self, table, purpose):
        with patch.object(config.settings, "get", side_effect=lambda k, d=None: table.get(k, d)):
            return recorder.resolve_input_device(purpose)

    def test_purpose_specific_wins(self):
        self.assertEqual(3, self._res({"commandInputDeviceId": 3, "inputDeviceId": 1}, "command"))
        self.assertEqual(7, self._res({"meetingInputDeviceId": 7, "inputDeviceId": 1}, "meeting"))

    def test_falls_back_to_the_shared_setting(self):
        """自己的设成 -1（= 没指定）时跟随通用项 —— 这是"老配置不用改"的关键。"""
        self.assertEqual(1, self._res({"commandInputDeviceId": -1, "inputDeviceId": 1}, "command"))
        self.assertEqual(2, self._res({"meetingInputDeviceId": -1, "inputDeviceId": 2}, "meeting"))

    def test_both_unset_is_system_default(self):
        self.assertEqual(-1, self._res({}, "command"))
        self.assertEqual(-1, self._res({"commandInputDeviceId": -1, "inputDeviceId": -1}, "meeting"))

    def test_zero_is_a_real_device_not_a_fallback(self):
        """0 是合法设备索引，不能被当成"没设"（真踩过这种坑的下场：静默用了别的麦）。"""
        self.assertEqual(0, self._res({"meetingInputDeviceId": 0, "inputDeviceId": 5}, "meeting"))

    def test_garbage_value_falls_back_instead_of_crashing(self):
        self.assertEqual(4, self._res({"meetingInputDeviceId": "abc", "inputDeviceId": 4}, "meeting"))
        self.assertEqual(-1, self._res({"commandInputDeviceId": None, "inputDeviceId": "x"}, "command"))

    def test_settings_failure_is_not_fatal(self):
        """录音路径不该因为读配置失败就打不开麦。"""
        with patch.object(config.settings, "get", side_effect=RuntimeError("db 挂了")):
            self.assertEqual(-1, recorder.resolve_input_device("meeting"))

    def test_unknown_purpose_uses_the_shared_setting(self):
        self.assertEqual(9, self._res({"inputDeviceId": 9}, "whatever"))


class DeviceOptionsTests(unittest.TestCase):
    def setUp(self):
        config._INPUT_OPTIONS["at"] = 0.0
        config._INPUT_OPTIONS["items"] = []

    def test_options_carry_value_and_label(self):
        fake = [{"index": 0, "name": "麦克风 (Realtek)", "channels": 2},
                {"index": 3, "name": "MAXHUB 全向麦", "channels": 1}]
        with patch.object(recorder, "list_input_devices", lambda: fake):
            opts = config._audio_input_options()
        self.assertEqual(-1, opts[0]["value"])            # 系统默认永远在第一位
        self.assertEqual([-1, 0, 3], [o["value"] for o in opts])
        self.assertIn("MAXHUB 全向麦", opts[2]["label"])
        self.assertIn("3", opts[2]["label"])

    def test_device_query_failure_still_offers_the_default(self):
        def boom():
            raise RuntimeError("PortAudio 没起来")
        with patch.object(recorder, "list_input_devices", boom):
            opts = config._audio_input_options()
        self.assertEqual(1, len(opts))
        self.assertEqual(-1, opts[0]["value"])
        self.assertIn("PortAudio", opts[0]["label"])

    def test_result_is_cached_but_failure_is_not(self):
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return [{"index": 0, "name": "mic", "channels": 1}]

        with patch.object(recorder, "list_input_devices", fake):
            config._audio_input_options()
            config._audio_input_options()
        self.assertEqual(1, calls["n"], "30 秒内不该重复查设备")


class SettingsWiringTests(unittest.TestCase):
    def test_the_three_device_settings_exist_and_default_to_follow(self):
        for key in ("inputDeviceId", "commandInputDeviceId", "meetingInputDeviceId"):
            meta = config.DEFAULTS[key]
            self.assertEqual(-1, meta["value"], key)
            self.assertEqual("int", meta["value_type"], key)
            self.assertEqual("audio_inputs", meta.get("options_from"), key)
            self.assertTrue(meta.get("label"), key)

    def test_all_injects_device_options_at_runtime(self):
        """设备会插拔，选项必须运行时给 —— 静态写进库会过期。"""
        rows = [{"key": "meetingInputDeviceId", "value": -1, "grp": "voice",
                 "label": "x", "description": "", "value_type": "int", "options": []},
                {"key": "ttsEngine", "value": "auto", "grp": "tts",
                 "label": "y", "description": "", "value_type": "str", "options": ["auto"]}]
        fake = [{"index": 2, "name": "耳机麦克风", "channels": 1}]
        config._INPUT_OPTIONS["at"] = 0.0
        config._INPUT_OPTIONS["items"] = []
        with patch.object(config.db, "all_settings", lambda: rows), \
                patch.object(recorder, "list_input_devices", lambda: fake):
            out = {r["key"]: r for r in config.settings.all()}
        self.assertEqual([-1, 2], [o["value"] for o in out["meetingInputDeviceId"]["options"]])
        self.assertEqual(["auto"], out["ttsEngine"]["options"], "别的设置的选项不该被动到")


if __name__ == "__main__":
    unittest.main()
