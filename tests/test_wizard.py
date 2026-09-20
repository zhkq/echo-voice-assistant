# -*- coding: utf-8 -*-
"""向导数据面的测试（``app/wizard.py``，设计见 docs/向导-分步设计.md）。

重点不在"本机探得准不准"（那取决于机器），而在三条硬要求：

1. 环境报告**永不抛异常** —— 环境坏掉的时候，体检页恰恰最需要能打开；
2. **三处位置**（能力包 / 会议文件 / 笔记库）都在报告里，且**各自**报所在盘的剩余空间；
3. 计划文件能往返、能容错、且**只写它自己那一个文件**（不碰设置、不下载）。

探测函数一律被打桩：测试不该依赖本机有没有显卡、能不能上网。
"""
import json
import os
import tempfile
import unittest

from app import paths
from app import wizard


#: 会被打桩的模块级函数（一个都不能漏还原：`wizard.plat` 就是共享的 `app.platform`，
#: 不还原会污染其它测试，造成顺序相关的假红）
_PATCHED = ("locations_report", "network_report", "audio_report", "node_report", "agent_report")


class _WizardTestCase(unittest.TestCase):
    """打桩 + 还原：探测函数、共享的 `app.platform.gpu_info`、两处 settings 读取。"""

    def setUp(self):
        self._orig = {name: getattr(wizard, name) for name in _PATCHED}
        self._orig_gpu = wizard.plat.gpu_info
        self._orig_wizard_settings = wizard._settings
        self._orig_paths_settings = paths._settings_get
        self.tmp = tempfile.mkdtemp(prefix="echo-wizard-")

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(wizard, name, fn)
        wizard.plat.gpu_info = self._orig_gpu
        wizard._settings = self._orig_wizard_settings
        paths._settings_get = self._orig_paths_settings

    def stub_quiet(self):
        """把"会碰网络/硬件/进程"的几节换成固定值，只留位置这一节是真的。"""
        wizard.network_report = lambda: {"targets": [], "anyReachable": True, "verdict": "stub"}
        wizard.audio_report = lambda: {"available": True, "inputs": 1, "names": ["stub"]}
        wizard.node_report = lambda: {"npx": "C:/stub/npx", "ok": True, "note": ""}
        wizard.agent_report = lambda: {
            "dsh": {"online": False, "detail": "stub"},
            "harness": {"online": False, "detail": "stub", "command": "stub"},
        }


class EnvironmentReportTests(_WizardTestCase):

    def test_report_never_raises_when_every_section_fails(self):
        """每一节都炸，报告仍要能生成 —— 这是"永不 500"的直接钉法。"""
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        for name in ("locations_report", "network_report", "audio_report",
                     "node_report", "agent_report"):
            setattr(wizard, name, boom)
        wizard.plat.gpu_info = boom
        report = wizard.environment_report()
        self.assertIn("locations", report)
        self.assertEqual(report["blocked"], [])
        self.assertIn("recommend", report)

    def test_report_has_three_locations_each_with_own_free_space(self):
        self.stub_quiet()
        wizard._settings = lambda name: ""
        paths._settings_get = lambda name: ""
        report = wizard.environment_report()
        keys = [row["key"] for row in report["locations"]]
        self.assertEqual(keys, ["models", "meetings", "notes"])
        for row in report["locations"]:
            self.assertIn("freeGB", row, "每条位置都要单独报空间（设计 §2 的 S1）")
            self.assertIn("writable", row)
            self.assertIn("label", row)

    def test_notes_location_follows_config_and_is_writable(self):
        """笔记库是"用户已有的库"，所以它必须跟着 worklogVaultRoot 走。"""
        self.stub_quiet()
        wizard._settings = lambda name: (
            self.tmp if name == "worklogVaultRoot" else "")
        paths._settings_get = lambda name: ""
        report = wizard.environment_report()
        notes = [r for r in report["locations"] if r["key"] == "notes"][0]
        self.assertEqual(notes["path"], os.path.normpath(self.tmp))
        self.assertTrue(notes["configured"])
        self.assertTrue(notes["exists"])
        self.assertTrue(notes["writable"])

    def test_unwritable_required_location_is_blocked(self):
        """能力包/会议文件不可写 = 硬阻塞（向导据此拦住"开始准备"）。"""
        self.stub_quiet()
        wizard.locations_report = lambda: [
            {"key": "models", "label": "能力包", "settingKey": "modelsDir", "path": "",
             "configured": False, "exists": False, "ascii": True, "writable": False,
             "freeGB": None, "note": "配置为空"},
            {"key": "notes", "label": "笔记库", "settingKey": "worklogVaultRoot", "path": "",
             "configured": False, "exists": False, "ascii": True, "writable": False,
             "freeGB": None, "note": "还没设置"},
        ]
        report = wizard.environment_report()
        self.assertEqual([r["key"] for r in report["blocked"]], ["models"],
                         "笔记库没设不算阻塞；能力包不可写才算")

    def test_recommend_hides_accel_without_gpu(self):
        self.stub_quiet()
        wizard._settings = lambda name: ""
        paths._settings_get = lambda name: ""
        wizard.plat.gpu_info = lambda: {"vendor": "", "name": "", "vramMb": 0,
                                        "driver": "", "source": ""}
        report = wizard.environment_report()
        self.assertFalse(report["recommend"]["showAccel"])
        self.assertEqual(report["recommend"]["engine"], "stt-sherpa",
                         "默认建议必须是不需要独立显卡的那一档（设计 §0.4）")

    def test_recommend_shows_accel_with_gpu(self):
        self.stub_quiet()
        wizard._settings = lambda name: ""
        paths._settings_get = lambda name: ""
        wizard.plat.gpu_info = lambda: {"vendor": "nvidia", "name": "RTX 3060",
                                        "vramMb": 8192, "driver": "555.1", "source": "nvidia-smi"}
        report = wizard.environment_report()
        self.assertTrue(report["recommend"]["showAccel"])
        self.assertIn("RTX 3060", report["recommend"]["accelReason"])


class PlanStoreTests(_WizardTestCase):

    def _plan_file(self):
        return os.path.join(self.tmp, "wizard-plan.json")

    def test_roundtrip(self):
        target = self._plan_file()
        wizard.save_plan({"state": "reviewing",
                          "choices": {"engine": ["stt-sherpa"], "notes": "C:/vault"}}, target)
        loaded = wizard.load_plan(target)
        self.assertEqual(loaded["state"], "reviewing")
        self.assertEqual(loaded["choices"]["engine"], ["stt-sherpa"])
        self.assertEqual(loaded["schema"], wizard.PLAN_SCHEMA)
        self.assertTrue(loaded["updatedAt"], "保存时必须盖时间戳")

    def test_missing_or_broken_file_falls_back_to_defaults(self):
        target = self._plan_file()
        self.assertEqual(wizard.load_plan(target)["state"], "draft")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(wizard.load_plan(target)["state"], "draft")
        self.assertEqual(wizard.load_plan(target)["choices"], {})

    def test_unknown_state_is_normalised(self):
        target = self._plan_file()
        saved = wizard.save_plan({"state": "whatever", "choices": {}}, target)
        self.assertEqual(saved["state"], "draft")
        self.assertEqual(wizard.load_plan(target)["state"], "draft")

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        target = self._plan_file()
        wizard.save_plan({"choices": {"a": 1}}, target)
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(target + ".tmp"), "原子写不该留下 .tmp")
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["choices"], {"a": 1})

    def test_non_dict_choices_are_dropped(self):
        target = self._plan_file()
        saved = wizard.save_plan({"choices": "nope"}, target)
        self.assertEqual(saved["choices"], {})


if __name__ == "__main__":
    unittest.main()
