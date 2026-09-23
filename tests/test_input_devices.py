# -*- coding: utf-8 -*-
"""按用途分输入设备 + **稳定键**（2026-09-23 用户需求与追问）。

两件事：
  * 会议与指令各用哪个麦（原来只有一个 `inputDeviceId`）；
  * 设置里存的是**设备名**而不是 PortAudio 的索引 —— 索引会随"当前在位设备"的增减
    整体平移（同一个硬件还会按 host API 各出现一次），用户问的就是"序号漂移"。
    配置的设备不在位时**回退系统默认并记一条 warn**（用户定的策略：宁可回退也别打不开，
    但绝不静默换麦）。
"""
import unittest
from unittest.mock import patch

from app import config
from app.audio import recorder

#: 假设备表：MAXHUB 这个名字故意出现在两套 API 下（WASAPI 应胜出），另有虚拟端点。
FAKE_DEVICES = [
    {"index": 2, "name": "耳机 (MAXHUB BM12)", "channels": 1,
     "hostapi": "MME", "samplerate": 44100, "virtual": False},
    {"index": 14, "name": "耳机 (MAXHUB BM12)", "channels": 1,
     "hostapi": "Windows WASAPI", "samplerate": 16000, "virtual": False},
    {"index": 29, "name": "耳机 (Hands-Free OpenRun Pro by Shokz)", "channels": 1,
     "hostapi": "Windows WDM-KS", "samplerate": 8000, "virtual": False},
    {"index": 0, "name": "Microsoft 声音映射器 - Input", "channels": 2,
     "hostapi": "MME", "samplerate": 44100, "virtual": True},
]


def _fake_list():
    return [dict(d) for d in FAKE_DEVICES]


class FindInputDeviceTests(unittest.TestCase):
    def _find(self, value):
        with patch.object(recorder, "list_input_devices", _fake_list):
            recorder._DEVICE_CACHE["at"] = 0.0
            recorder._DEVICE_CACHE["items"] = []
            return recorder.find_input_device(value)

    def test_name_resolves_to_the_current_index(self):
        self.assertEqual(29, self._find("耳机 (Hands-Free OpenRun Pro by Shokz)"))

    def test_same_name_prefers_wasapi(self):
        """同名出现在多套 API 里 → 选 WASAPI 那条（不写索引就不会挑错序号）。"""
        self.assertEqual(14, self._find("耳机 (MAXHUB BM12)"))

    def test_legacy_numeric_index_is_still_accepted(self):
        self.assertEqual(7, self._find("7"))
        self.assertEqual(-1, self._find("-1"))
        self.assertEqual(0, self._find("0"))

    def test_unknown_name_is_none(self):
        self.assertIsNone(self._find("已经拔掉的麦克风"))

    def test_empty_is_none(self):
        self.assertIsNone(self._find(""))
        self.assertIsNone(self._find(None))

    def test_device_query_failure_is_not_fatal(self):
        def boom():
            raise RuntimeError("PortAudio 没起来")
        with patch.object(recorder, "list_input_devices", boom):
            recorder._DEVICE_CACHE["at"] = 0.0
            recorder._DEVICE_CACHE["items"] = []
            self.assertIsNone(recorder.find_input_device("任意"))


class ResolveInputDeviceTests(unittest.TestCase):
    def _res(self, table, purpose, logs=None):
        patches = [patch.object(config.settings, "get",
                                side_effect=lambda k, d=None: table.get(k, d)),
                   patch.object(recorder, "list_input_devices", _fake_list)]
        if logs is not None:
            patches.append(patch("app.db.add_log",
                                 lambda level, source, msg: logs.append((level, source, msg))))
        recorder._DEVICE_CACHE["at"] = 0.0
        recorder._DEVICE_CACHE["items"] = []
        for p in patches:
            p.start()
        try:
            return recorder.resolve_input_device(purpose)
        finally:
            for p in patches:
                p.stop()

    def test_purpose_specific_wins(self):
        self.assertEqual(29, self._res({"commandInputDeviceId": "耳机 (Hands-Free OpenRun Pro by Shokz)",
                                        "inputDeviceId": "耳机 (MAXHUB BM12)"}, "command"))
        self.assertEqual(14, self._res({"meetingInputDeviceId": "耳机 (MAXHUB BM12)",
                                        "inputDeviceId": ""}, "meeting"))

    def test_falls_back_to_the_shared_setting(self):
        self.assertEqual(14, self._res({"commandInputDeviceId": "", "inputDeviceId": "耳机 (MAXHUB BM12)"},
                                       "command"))

    def test_both_unset_is_system_default(self):
        self.assertEqual(-1, self._res({}, "command"))
        self.assertEqual(-1, self._res({"meetingInputDeviceId": "", "inputDeviceId": ""}, "meeting"))

    def test_missing_device_falls_back_to_default_and_logs(self):
        """用户定的策略：设备不在位 → 用回默认设备，但**必须留痕**。"""
        logs = []
        got = self._res({"meetingInputDeviceId": "小米会议宝 mini", "inputDeviceId": ""},
                        "meeting", logs)
        self.assertEqual(-1, got)
        self.assertEqual(1, len(logs))
        self.assertEqual("warn", logs[0][0])
        self.assertIn("小米会议宝 mini", logs[0][2])
        self.assertIn("回退", logs[0][2])

    def test_missing_purpose_device_does_not_fall_back_to_another_named_device(self):
        """指定了设备但不在位时，不能悄悄改用通用项里那台**别的**设备。"""
        logs = []
        got = self._res({"commandInputDeviceId": "小米会议宝 mini",
                         "inputDeviceId": "耳机 (MAXHUB BM12)"}, "command", logs)
        self.assertEqual(-1, got, "应回退到系统默认，而不是 inputDeviceId 指定的另一台")
        self.assertEqual(1, len(logs))

    def test_legacy_index_still_works(self):
        self.assertEqual(3, self._res({"meetingInputDeviceId": "3"}, "meeting"))
        # index 0 在 FAKE_DEVICES 里是「Microsoft 声音映射器」= 虚拟设备，
        # 2026-09-23（D2）起显式指定也过黑名单 → 被拒。见下面的虚拟设备用例。
        self.assertEqual(-1, self._res({"meetingInputDeviceId": "0"}, "meeting"))

    def test_zero_is_a_real_device_not_a_fallback(self):
        """索引 0 是**真实设备**，不能被当成"未设置"的哨兵值（`if idx:` 这类写法会犯的错）。

        FAKE_DEVICES 里 index 0 恰好是虚拟设备，所以这里的结果是 -1 ——
        但**理由必须是"虚拟设备被拒"，而不是"回退到了 inputDeviceId 那台"**：
        下面两条断言就是在钉这件事（拿不到"回退"的证据）。
        """
        logs = []
        got = self._res({"meetingInputDeviceId": "0", "inputDeviceId": "9"}, "meeting", logs)
        self.assertEqual(-1, got)
        self.assertEqual(1, len(logs))
        self.assertIn("虚拟", logs[0][2], "应当是黑名单拒的")
        self.assertNotIn("回退到系统默认麦克风", logs[0][2])  # 用的是黑名单那条文案

    # ---------------------------------------------------------------- D2：设备黑名单
    #
    # 2026-09-23（D2）：挡的范围从"兜底遍历"扩到**用户显式指定**。
    # 为什么值得单独几组用例：`_VIRTUAL_HINTS` 原来只管兜底遍历，而 AGENTS.md 记着的
    # 那次事故（macOS 打开 Oray / iPhone 麦克风把 CoreAudio HAL 锁死，之后**任何**
    # 麦克风操作永久超时、只能重启 ECHO）走的正是"用户手动选了它"这条路。

    def test_explicit_virtual_by_name_is_refused(self):
        logs = []
        got = self._res({"meetingInputDeviceId": "Microsoft 声音映射器 - Input"}, "meeting", logs)
        self.assertEqual(-1, got, "虚拟设备不该被打开")
        self.assertEqual(1, len(logs))
        self.assertEqual("warn", logs[0][0])
        self.assertIn("虚拟", logs[0][2])
        # 日志里给的是**设置页上那个标签**（不是设置键）—— 那才是用户找得到的东西。
        # 顺手把"标签"这层耦合钉住：谁改了 config 里的 label，这里就红，
        # 免得日志指着一个设置页上已经不存在的东西。
        label = config.DEFAULTS["allowVirtualInputDevice"]["label"]
        self.assertIn(label, logs[0][2], "被拦时必须说清怎么放行，否则用户无路可走")

    def test_legacy_index_is_judged_by_the_resolved_name(self):
        """老配置存的是**索引**：黑名单要按反查出来的**当前名字**判，不能拿索引字符串去比。"""
        self.assertEqual(-1, self._res({"meetingInputDeviceId": "0"}, "meeting"))

    def test_override_setting_lets_it_through(self):
        """逃生口：确实要回环测试的人可以打开它 —— 放行，不是永久封死。"""
        got = self._res({"meetingInputDeviceId": "0", "allowVirtualInputDevice": True}, "meeting")
        self.assertEqual(0, got, "打开逃生口后应当放行（它确实是个设备，只是危险）")

    def test_real_devices_are_unaffected(self):
        self.assertEqual(14, self._res({"meetingInputDeviceId": "耳机 (MAXHUB BM12)"}, "meeting"))
        self.assertEqual(29, self._res({"commandInputDeviceId":
                                        "耳机 (Hands-Free OpenRun Pro by Shokz)"}, "command"))

    def test_missing_device_still_reports_missing_not_virtual(self):
        """不存在的设备要报"不在位" —— 理由不能被黑名单抢走（否则排障会走错方向）。"""
        logs = []
        got = self._res({"meetingInputDeviceId": "已经拔掉的麦克风"}, "meeting", logs)
        self.assertEqual(-1, got)
        self.assertIn("不可用", logs[0][2])
        self.assertNotIn("虚拟", logs[0][2])

    def test_settings_failure_is_not_fatal(self):
        with patch.object(config.settings, "get", side_effect=RuntimeError("db 挂了")):
            self.assertEqual(-1, recorder.resolve_input_device("meeting"))

    def test_unknown_purpose_uses_the_shared_setting(self):
        self.assertEqual(14, self._res({"inputDeviceId": "耳机 (MAXHUB BM12)"}, "whatever"))


class DeviceOptionsTests(unittest.TestCase):
    def setUp(self):
        config._INPUT_OPTIONS["at"] = 0.0
        config._INPUT_OPTIONS["items"] = []
        recorder._DEVICE_CACHE["at"] = 0.0
        recorder._DEVICE_CACHE["items"] = []

    def _opts(self, current=None):
        with patch.object(recorder, "list_input_devices", _fake_list):
            return config._audio_input_options(current)

    def test_values_are_names_not_indices(self):
        opts = self._opts()
        self.assertEqual("", opts[0]["value"], "系统默认用空串")
        vals = [o["value"] for o in opts]
        self.assertIn("耳机 (MAXHUB BM12)", vals)
        self.assertNotIn(14, vals, "不该再存会漂的索引")

    def test_duplicate_names_are_deduped_keeping_wasapi(self):
        opts = self._opts()
        hits = [o for o in opts if o["value"] == "耳机 (MAXHUB BM12)"]
        self.assertEqual(1, len(hits), "同名只保留一条")
        self.assertIn("WASAPI", hits[0]["label"])
        self.assertIn("16 kHz", hits[0]["label"])
        self.assertIn("推荐", hits[0]["label"])

    def test_virtual_endpoint_is_marked(self):
        opts = self._opts()
        v = [o for o in opts if "声音映射器" in str(o["value"])]
        self.assertTrue(v)
        self.assertIn("别选", v[0]["label"])

    def test_current_value_is_always_listed(self):
        """旧索引 / 已拔掉的设备名必须出现在列表里 —— 否则面板显示成"系统默认"，
        用户一保存就把真值覆盖成空（静默丢配置）。"""
        legacy = self._opts(current="14")
        self.assertIn("14", [str(o["value"]) for o in legacy])
        gone = self._opts(current="小米会议宝 mini")
        self.assertIn("小米会议宝 mini", [str(o["value"]) for o in gone])

    def test_current_system_default_is_not_duplicated(self):
        opts = self._opts(current="-1")
        self.assertEqual(1, len([o for o in opts if str(o["value"]) == ""]))

    def test_device_query_failure_still_offers_the_default(self):
        def boom():
            raise RuntimeError("PortAudio 没起来")
        with patch.object(recorder, "list_input_devices", boom):
            config._INPUT_OPTIONS["at"] = 0.0
            config._INPUT_OPTIONS["items"] = []
            opts = config._audio_input_options()
        self.assertEqual(1, len(opts))
        self.assertEqual("", opts[0]["value"])

    def test_result_is_cached(self):
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return _fake_list()

        with patch.object(recorder, "list_input_devices", fake):
            config._INPUT_OPTIONS["at"] = 0.0
            config._INPUT_OPTIONS["items"] = []
            config._audio_device_options()
            config._audio_device_options()
        self.assertEqual(1, calls["n"], "30 秒内不该重复查设备")


class SettingsWiringTests(unittest.TestCase):
    def test_the_three_device_settings_exist_and_are_strings(self):
        for key in ("inputDeviceId", "commandInputDeviceId", "meetingInputDeviceId"):
            meta = config.DEFAULTS[key]
            self.assertEqual("", meta["value"], key)
            self.assertEqual("str", meta["value_type"], key)
            self.assertEqual("audio_inputs", meta.get("options_from"), key)
            self.assertTrue(meta.get("label"), key)

    def test_all_injects_device_options_at_runtime(self):
        rows = [{"key": "meetingInputDeviceId", "value": "耳机 (MAXHUB BM12)", "grp": "voice",
                 "label": "x", "description": "", "value_type": "str", "options": []},
                {"key": "ttsEngine", "value": "auto", "grp": "tts",
                 "label": "y", "description": "", "value_type": "str", "options": ["auto"]}]
        config._INPUT_OPTIONS["at"] = 0.0
        config._INPUT_OPTIONS["items"] = []
        recorder._DEVICE_CACHE["at"] = 0.0
        recorder._DEVICE_CACHE["items"] = []
        with patch.object(config.db, "all_settings", lambda: rows), \
                patch.object(recorder, "list_input_devices", _fake_list):
            out = {r["key"]: r for r in config.settings.all()}
        vals = [o["value"] for o in out["meetingInputDeviceId"]["options"]]
        self.assertIn("耳机 (MAXHUB BM12)", vals)
        self.assertEqual(["auto"], out["ttsEngine"]["options"], "别的设置的选项不该被动到")


if __name__ == "__main__":
    unittest.main()
