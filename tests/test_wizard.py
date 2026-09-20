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

from app import components
from app import modelinfo
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


#: 组件清单的替身：只保留测试要用的形状（真清单会随产品演进，测试不该跟着抖）
_FIXTURE_COMPONENTS = [
    {"id": "stt-sherpa", "name": "sherpa-onnx 流式转写", "model_id": "sherpa",
     "size_mb": 189, "how": "面板下载"},
    {"id": "stt-whisper-base", "name": "Whisper base", "model_id": "whisper-base",
     "size_mb": 141, "how": "面板下载"},
    {"id": "wake-kws", "name": "唤醒词 KWS", "model_id": "kws", "size_mb": 40, "how": ""},
    {"id": "diarize-pyannote", "name": "说话人分离（pyannote）", "model_id": "pyannote",
     "size_mb": 32, "never_ship": True, "how": "需要 HF 授权"},
    {"id": "accel-cuda", "name": "CUDA 加速", "size_mb": 2500,
     "command": "python -m pip install torch", "how": "按显卡驱动安装"},
    {"id": "agent-harness", "name": "独立 DeepSeek Harness（本机服务）", "size_mb": 0,
     "command": "npx -y @deepseek-ai/dsh web", "how": "随选随起"},
]


class _PlanTestCase(_WizardTestCase):
    """把组件清单与就绪探测换成替身：测的是**计划的形状与顺序**，不是本机装了什么。

    约定：只有 ``sherpa`` 是"已就绪"的 —— 用来钉"已就绪的不重复下载"。
    """

    def setUp(self):
        super().setUp()
        self._orig_manifests = components.load_manifests
        self._orig_ready = modelinfo.ready
        components.load_manifests = lambda root=None: [dict(x) for x in _FIXTURE_COMPONENTS]
        modelinfo.ready = lambda mid: (mid == "sherpa")

    def tearDown(self):
        components.load_manifests = self._orig_manifests
        modelinfo.ready = self._orig_ready
        super().tearDown()


class BuildPlanTests(_PlanTestCase):

    def test_only_missing_packages_are_scheduled(self):
        plan = wizard.build_plan({"engines": ["stt-sherpa", "stt-whisper-base"]})
        self.assertEqual([d["component"] for d in plan["downloads"]],
                         ["stt-sherpa", "stt-whisper-base"])
        self.assertEqual(plan["downloadCount"], 1, "sherpa 已就绪，不该再排一次")
        self.assertEqual(plan["todoMb"], 141)
        self.assertEqual(plan["readyMb"], 189)
        self.assertIn("1 项已经装好", plan["summary"]["alreadyReady"])

    def test_wake_and_diarize_join_the_plan(self):
        plan = wizard.build_plan({"wake": True, "diarize": True})
        self.assertEqual([d["component"] for d in plan["downloads"]], ["wake-kws"])
        diarize = [m for m in plan["manual"] if m["component"] == "diarize-pyannote"][0]
        self.assertIn("许可证", diarize["reason"],
                      "gated 的能力包要说清「不能随包分发」，而不是假装能装")

    def test_accel_is_manual_and_comes_last(self):
        plan = wizard.build_plan({"engines": ["stt-whisper-base"], "accel": True})
        self.assertEqual([d["phase"] for d in plan["downloads"]], ["engines"])
        accel = [m for m in plan["manual"] if m["component"] == "accel-cuda"][0]
        self.assertEqual(accel["phase"], "accel")
        self.assertTrue(accel["command"], "面板不代装的项必须给出可粘贴的命令")

    def test_three_locations_become_config_writes_and_notes_enables_worklog(self):
        plan = wizard.build_plan({"locations": {"models": self.tmp, "notes": self.tmp}})
        keys = [c["key"] for c in plan["config"]]
        self.assertIn("modelsDir", keys)
        self.assertIn("worklogVaultRoot", keys)
        self.assertIn("worklogEnabled", keys, "指了笔记库就要把归档打开")
        self.assertNotIn("meetingsDir", keys, "没填的位置不该被写进去（不覆盖已有配置）")

    def test_online_asr_is_a_provider_choice_not_a_download(self):
        plan = wizard.build_plan({"asrOnline": True})
        self.assertEqual(plan["downloads"], [], "在线转写不占本机空间，不该产生下载项")
        asr = [p for p in plan["providers"] if p["kind"] == "asr"]
        self.assertTrue(asr, "在线转写应作为 provider 选择出现")
        self.assertTrue(asr[0]["egress"], "在线转写必须标出网")
        self.assertTrue(plan["summary"]["egress"], "确认页要能直接拿到出网声明")
        self.assertIn("providerAsr", [c["key"] for c in plan["config"]])

    def test_agent_choice_writes_backend(self):
        plan = wizard.build_plan({"agent": "agent-harness"})
        self.assertIn({"key": "agentBackend", "value": "agent-harness"}, plan["config"])

    def test_unknown_component_is_reported_not_installed(self):
        plan = wizard.build_plan({"engines": ["nope"]})
        self.assertEqual(plan["downloads"], [])
        self.assertEqual([u["component"] for u in plan["unavailable"]], ["nope"])
        self.assertIn("清单里没有", plan["unavailable"][0]["reason"])


class ExecutePlanTests(_PlanTestCase):

    def _plan_file(self):
        return os.path.join(self.tmp, "wizard-plan.json")

    def test_config_is_written_before_any_download(self):
        order = []
        plan = wizard.build_plan({"locations": {"models": self.tmp},
                                  "engines": ["stt-whisper-base"]})
        result = wizard.execute_plan(
            plan, plan_file=self._plan_file(),
            settings_update=lambda values: (order.append("settings"), list(values))[1],
            start_download=lambda mid: (order.append(mid), (True, "ok"))[1],
            ready=lambda mid: False)
        self.assertEqual(order[0], "settings", "配置必须先行：下载要知道往哪写")
        self.assertIn("whisper-base", order)
        self.assertEqual(result["config"], ["modelsDir"])
        self.assertTrue(result["ok"])

    def test_ready_items_are_skipped_and_not_downloaded(self):
        calls = []
        plan = wizard.build_plan({"engines": ["stt-sherpa"]})
        result = wizard.execute_plan(
            plan, plan_file=self._plan_file(),
            settings_update=lambda values: list(values),
            start_download=lambda mid: (calls.append(mid), (True, "ok"))[1],
            ready=lambda mid: True)
        self.assertEqual(calls, [], "已就绪的项不该再触发下载")
        self.assertEqual([r["component"] for r in result["skipped"]], ["stt-sherpa"])

    def test_one_failure_does_not_stop_the_others(self):
        plan = wizard.build_plan({"engines": ["stt-whisper-base", "wake-kws"]})
        result = wizard.execute_plan(
            plan, plan_file=self._plan_file(),
            settings_update=lambda values: list(values),
            start_download=lambda mid: (False, "network down") if mid == "whisper-base"
                                       else (True, "ok"),
            ready=lambda mid: False)
        self.assertFalse(result["ok"])
        self.assertEqual([r["modelId"] for r in result["failed"]], ["whisper-base"])
        self.assertEqual([r["modelId"] for r in result["downloads"]], ["kws"],
                         "前一项失败不能挡住后面的项")

    def test_a_raising_download_is_recorded_not_propagated(self):
        def boom(mid):
            raise RuntimeError("kaboom")

        plan = wizard.build_plan({"engines": ["stt-whisper-base"]})
        result = wizard.execute_plan(plan, plan_file=self._plan_file(),
                                     settings_update=lambda values: list(values),
                                     start_download=boom, ready=lambda mid: False)
        self.assertFalse(result["ok"])
        self.assertIn("kaboom", result["failed"][0]["error"])

    def test_plan_file_remembers_state_built_and_execution(self):
        target = self._plan_file()
        plan = wizard.build_plan({"engines": ["stt-whisper-base"]})
        wizard.execute_plan(plan, plan_file=target,
                            settings_update=lambda values: list(values),
                            start_download=lambda mid: (True, "ok"),
                            ready=lambda mid: False)
        saved = wizard.load_plan(target)
        self.assertEqual(saved["state"], "running")
        self.assertIn("built", saved)
        self.assertIn("execution", saved)

    def test_state_view_speaks_the_ui_language(self):
        target = self._plan_file()
        plan = wizard.build_plan({"engines": ["stt-sherpa", "stt-whisper-base"]})
        wizard.execute_plan(plan, plan_file=target,
                            settings_update=lambda values: list(values),
                            start_download=lambda mid: (True, "ok"),
                            ready=lambda mid: mid == "sherpa")   # 只有 sherpa 已就绪
        state = wizard.execution_state(target)
        rows = {r["component"]: r for r in state["items"]}
        self.assertEqual(rows["stt-sherpa"]["text"], "已经装好，跳过")
        self.assertIn(rows["stt-whisper-base"]["text"], ("排队中", "正在下载", "好了"))
        self.assertTrue(state["summary"].endswith("项已就绪"))


if __name__ == "__main__":
    unittest.main()
