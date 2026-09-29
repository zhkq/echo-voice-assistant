# -*- coding: utf-8 -*-
"""向导数据面的测试（``app/wizard.py``，设计见 docs/向导-分步设计.md）。

重点不在"本机探得准不准"（那取决于机器），而在三条硬要求：

1. 环境报告**永不抛异常** —— 环境坏掉的时候，体检页恰恰最需要能打开；
2. **三处位置**（模型文件 / 会议文件 / 笔记库）都在报告里，且**各自**报所在盘的剩余空间；
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
_PATCHED = ("locations_report", "network_report", "audio_report", "node_report", "agent_report",
            # 写配置后的联动：真跑会去起/停 harness 与唤醒监听，测试里必须哑掉（见 _PlanTestCase）
            "apply_settings_effects")


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
        """模型文件/会议文件不可写 = 硬阻塞（向导据此拦住"开始准备"）。"""
        self.stub_quiet()
        wizard.locations_report = lambda: [
            {"key": "models", "label": "模型文件", "settingKey": "modelsDir", "path": "",
             "configured": False, "exists": False, "ascii": True, "writable": False,
             "freeGB": None, "note": "配置为空"},
            {"key": "notes", "label": "笔记库", "settingKey": "worklogVaultRoot", "path": "",
             "configured": False, "exists": False, "ascii": True, "writable": False,
             "freeGB": None, "note": "还没设置"},
        ]
        report = wizard.environment_report()
        self.assertEqual([r["key"] for r in report["blocked"]], ["models"],
                         "笔记库没设不算阻塞；模型文件不可写才算")

    def test_user_facing_term_is_model_files_not_capability_packages(self):
        """界面用词是**模型文件**（2026-09-20 按用户要求由"能力包"改的）。

        钉住后端给界面的标签：三处位置的第一处、以及网络体检的两条下载目标。
        前端那半由 `tests/test_wizard_ui.py` 的术语守卫看着。
        """
        labels = {key: label for key, label, _setting, _required in wizard.LOCATIONS}
        self.assertEqual(labels["models"], "模型文件")
        for _tid, label, _url in wizard.NET_TARGETS[:2]:
            self.assertIn("模型文件", label, "下载目标的说法要跟界面用词一致")

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
        # 写配置后的联动在测试里哑掉：真做会去起/停独立 harness（npx）与唤醒监听，
        # 既慢又动本机进程。要验它的测试自己把它换成记录器（见 EffectsTests）。
        wizard.apply_settings_effects = lambda updated, **kw: []

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
                      "gated 的模型文件要说清「不能随包分发」，而不是假装能装")

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

    def test_agent_choice_writes_the_backend_name_not_the_component_id(self):
        """选「标准版」要写**注册表里的名字** + 它自己的启用开关（与面板那套一致）。

        2026-09-20 实测踩到两处：向导原来把**组件 id** 当名字写（`agent-harness`），
        `agents.active_name()` 不认这个名字 → 静默降级回 dsh，用户的选择等于没生效；
        而且漏了 `agentHarnessEnabled`，`harness_proc.requested()` 恒假 → harness 永不拉起。
        """
        cfg = {c["key"]: c["value"]
               for c in wizard.build_plan({"agent": "agent-harness"})["config"]}
        self.assertEqual(cfg["agentBackend"], "harness")
        self.assertIs(cfg["agentHarnessEnabled"], True)
        # 没有 config_key 的产品（dsh）不需要额外开关
        cfg2 = {c["key"]: c["value"] for c in wizard.build_plan({"agent": "dsh"})["config"]}
        self.assertEqual(cfg2["agentBackend"], "dsh")
        self.assertNotIn("agentHarnessEnabled", cfg2)

    def test_unknown_agent_name_writes_nothing(self):
        """认不出的名字一个字都不写 —— 绝不往 `agentBackend` 里塞无效值。"""
        keys = [c["key"] for c in wizard.build_plan({"agent": "no-such-agent"})["config"]]
        self.assertNotIn("agentBackend", keys)

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

    def test_config_write_runs_the_shared_effects(self):
        """写配置后必须跑**联动**（唤醒 / 路由 / 独立 harness）。

        2026-09-20 实测踩到：这段联动原先只长在 `PUT /api/settings` 里，而向导执行相直接调
        `settings.update()` —— 于是"在向导里选了标准版"只留下一行设置，harness 永远不起来。
        """
        calls = []

        def fake_effects(updated, **kw):
            calls.append(list(updated))
            return [{"scope": "agent", "ok": False, "detail": "找不到 npx：需要本机有 Node.js"}]

        wizard.apply_settings_effects = fake_effects
        plan = wizard.build_plan({"agent": "agent-harness"})
        result = wizard.execute_plan(plan, plan_file=self._plan_file(),
                                     settings_update=lambda values: list(values),
                                     start_download=lambda mid: (True, "ok"),
                                     ready=lambda mid: True)
        self.assertEqual(calls, [["agentBackend", "agentHarnessEnabled"]])
        self.assertFalse(result["ok"], "联动失败要登记进结果（末页据此回答「还不能做什么」）")
        self.assertTrue(any("agent" in str(f.get("component") or "") for f in result["failed"]))

    def test_effects_success_leaves_the_run_ok(self):
        wizard.apply_settings_effects = lambda updated, **kw: [
            {"scope": "agent", "ok": True, "detail": "独立 harness 启动中"}]
        plan = wizard.build_plan({"agent": "agent-harness"})
        result = wizard.execute_plan(plan, plan_file=self._plan_file(),
                                     settings_update=lambda values: list(values),
                                     start_download=lambda mid: (True, "ok"),
                                     ready=lambda mid: True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["effects"][0]["scope"], "agent")

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


class LlmCredentialsTests(_PlanTestCase):
    """S6：纪要**默认走智能体**，直连大模型是**显式打开**的、不推荐的兜底。

    2026-09-20 用户定调：纪要、归档、语音指令都靠智能体（只有它有 skill 机制做灵活扩展）。
    这里最要紧的是那条**反面守卫** —— 光填地址/密钥**不许**替用户把 `providerLlm` 写下去：
    那是个全局开关（别的功能也读），写了就等于替用户选定"没有智能体时用哪个直连 provider"。
    键名则取自 provider **自己声明**的清单，向导不猜。
    """

    def _cfg(self, choices):
        return {r["key"]: r["value"] for r in wizard.build_plan(choices)["config"]}

    def test_filling_the_fields_alone_never_switches_to_direct_llm(self):
        """**关键回归守卫**：填了地址与密钥、但没打开兜底开关 → 一个 providerLlm* 都不许写。"""
        cfg = self._cfg({"llm": {"baseUrl": "http://10.1.2.3:8000/v1", "apiKey": "sk-abc"}})
        for key in ("providerLlm", "providerLlmBaseUrl", "providerLlmApiKey", "providerLlmModel"):
            self.assertNotIn(key, cfg,
                             "填了字段就写 %s = 把用户从推荐的智能体路径上踢走" % key)

    def test_explicit_direct_fallback_lands_on_the_declared_keys(self):
        cfg = self._cfg({"llm": {"direct": True, "baseUrl": "http://10.1.2.3:8000/v1",
                                 "apiKey": "sk-abc", "model": "deepseek-chat"}})
        self.assertEqual(cfg["providerLlm"], wizard.DEFAULT_ONLINE_LLM)
        self.assertEqual(cfg["providerLlmBaseUrl"], "http://10.1.2.3:8000/v1")
        self.assertEqual(cfg["providerLlmApiKey"], "sk-abc")
        self.assertEqual(cfg["providerLlmModel"], "deepseek-chat")

    def test_empty_fields_do_not_overwrite_existing_config(self):
        cfg = self._cfg({"llm": {"direct": True, "baseUrl": "http://x/v1"}})
        self.assertEqual(cfg["providerLlmBaseUrl"], "http://x/v1")
        self.assertNotIn("providerLlmApiKey", cfg, "没填密钥就不该写（不覆盖已有配置）")
        self.assertNotIn("providerLlmModel", cfg)

    def test_explicit_provider_wins_and_undeclared_keys_are_not_invented(self):
        """显式选了别的 provider 就以他为准；没声明这些键的 provider 更不许瞎写。"""
        cfg = self._cfg({"llm": {"provider": "echo-auto", "baseUrl": "http://x/v1"}})
        self.assertEqual(cfg["providerLlm"], "echo-auto")
        self.assertNotIn("providerLlmBaseUrl", cfg,
                         "echo-auto 没声明 providerLlm* 这些键 → 向导不该发明键名")

    def test_minutes_need_the_agent_or_an_explicit_fallback(self):
        """「能不能写纪要」= **有智能体**（默认路径）或打开并填全了直连兜底。"""
        def missing(choices):
            return [m["feature"] for m in wizard.build_plan(choices)["missing"]]

        self.assertIn("自动写会议纪要", missing({}), "什么都没配就该提示缺")
        self.assertNotIn("自动写会议纪要", missing({"agent": "agent-harness"}),
                         "有智能体就算能写纪要 —— 这就是默认路径")
        self.assertIn("自动写会议纪要",
                      missing({"llm": {"baseUrl": "http://x/v1", "apiKey": "k"}}),
                      "只填字段、没打开开关 ≠ 能写纪要")
        self.assertNotIn("自动写会议纪要",
                         missing({"llm": {"direct": True, "baseUrl": "http://x/v1",
                                          "apiKey": "k"}}),
                         "打开并填全兜底才算用得上")

    def test_egress_declaration_reaches_the_confirm_page(self):
        plan = wizard.build_plan({"llm": {"direct": True, "baseUrl": "http://x/v1"}})
        self.assertEqual([p["id"] for p in plan["providers"]], [wizard.DEFAULT_ONLINE_LLM])
        self.assertTrue(plan["summary"]["egress"], "在线服务必须在确认页声明出网")


class MeetingRouteTests(_PlanTestCase):
    """S8「开会时的录音交给谁」（2026-09-28，设计 §6.6 / 向导 §2.2）。

    四条要守的：

      ① 前两条路（本机后端 / 同事的后端）写**同一个** `echo-server` —— 它们在代码里
         就是同一个后端，差别只是地址从哪来（`pair_local()` vs 配对串）；
      ② 在线那一档**一个键都不写**，并进末页 —— 适配器还没实现，写下去就是
         "能选却一定失败"（`base.UNIMPLEMENTED_BACKENDS`）；
      ③ 没选也要进末页（"录完不会变成文字"必须说出来）；
      ④ 「不出机」与"后端在进程外"是**冲突**，向导只如实说，**不许**顺手把许可放宽。
    """

    def _config(self, choices):
        return {r["key"]: r["value"] for r in wizard.build_plan(choices)["config"]}

    def test_both_connect_routes_write_the_same_backend(self):
        for route in ("local", "paired"):
            with self.subTest(route=route):
                cfg = self._config({"meeting": {"route": route}})
                self.assertEqual(cfg.get("capabilityMeetingAsrBackend"), "echo-server")
                self.assertNotIn("capabilityPrivacy", cfg, "向导不许替用户放宽出网许可")

    def test_the_online_route_writes_the_online_backend_and_the_wan_permission(self):
        """方案 3 落地后（2026-09-28 晚）：选它要**同时**写后端与出网许可 —— 缺一个都跑不起来。

        `capabilityPrivacy` 在这里写 `wan` 不是"顺手放宽"：用户**显式选了公网服务**，
        那正是"允许音频去哪"这个许可要表达的意思（而前两条路相反，不许碰它）。
        密钥没填时**照写设置**（用户可能稍后在面板里填），但末页要说出来。
        """
        plan = wizard.build_plan({"meeting": {"route": "online", "onlineApiKey": "sk-x"}})
        cfg = {r["key"]: r["value"] for r in plan["config"]}
        self.assertEqual(cfg.get("capabilityMeetingAsrBackend"), "asr-provider")
        self.assertEqual(cfg.get("capabilityPrivacy"), "wan")
        self.assertEqual(cfg.get("capabilityAsrProviderApiKey"), "sk-x")
        reasons = " ".join(m["reason"] for m in plan["missing"])
        self.assertIn("认不出是谁", reasons, "边界（认不了联系人）必须进末页")

    def test_the_online_route_without_a_key_says_so(self):
        # 库里也没配过密钥（**显式打桩**：默认 `_settings` 会读这台机器真实的库，
        # 那会让这条用例在"开发机自己配了在线转写"时红）
        wizard._settings = lambda name: ""
        plan = wizard.build_plan({"meeting": {"route": "online"}})
        self.assertTrue(any("密钥" in m["reason"] for m in plan["missing"]), plan["missing"])


class SecretsNeverLandInThePlanFileTests(_PlanTestCase):
    """**密钥不落计划文件**（2026-09-28）。

    计划文件（`data/wizard-plan.json`）是"关掉面板还能接着改"的草稿 —— 明文 JSON。
    而密钥只该有**一个**权威副本：`settings`（`secret=True`，接口永不回显）。
    这条以前是漏的：`llm.apiKey` 与方案 ③ 的 `onlineApiKey` 都明文躺在里面
    （`built.config` 里那份同样是明文）。

    钉四件事：

      * `choices` 里的密钥值不落盘（**键名照留** —— 确认页要列出"将写入哪些键"）；
      * `built.config` 里那几行同样不落盘；
      * 别的选择**一个都不能丢**（收口不是把计划清空）；
      * 抹掉之后不许**误报**：库里已经有密钥时，不许再说"还没填密钥 / 不能自动写纪要"。
    """

    def setUp(self):
        super().setUp()
        # 计划文件写到临时目录 —— 绝不碰这台机器真实的 data/wizard-plan.json
        self.plan_file = os.path.join(self.tmp, "wizard-plan.json")

    def _saved(self, data):
        wizard.save_plan(data, self.plan_file)
        with open(self.plan_file, encoding="utf-8") as fh:
            return json.load(fh)

    def test_choice_secrets_never_reach_the_file(self):
        saved = self._saved({
            "state": "draft",
            "choices": {
                "agent": "agent-harness",
                "locations": {"models": "D:/models"},
                "llm": {"direct": True, "baseUrl": "http://x/v1", "apiKey": "sk-llm-secret",
                        "model": "m1"},
                "meeting": {"route": "online", "onlineApiKey": "sk-meeting-secret"},
            }})
        blob = json.dumps(saved, ensure_ascii=False)
        self.assertNotIn("sk-llm-secret", blob, "LLM 密钥明文落进了计划文件")
        self.assertNotIn("sk-meeting-secret", blob, "在线转写密钥明文落进了计划文件")
        # 键名留着（确认页要列"将写入哪些键"），别的选择一个都不能丢
        self.assertIn("apiKey", blob)
        self.assertEqual(saved["choices"]["agent"], "agent-harness")
        self.assertEqual(saved["choices"]["locations"]["models"], "D:/models")
        self.assertEqual(saved["choices"]["llm"]["baseUrl"], "http://x/v1")
        self.assertEqual(saved["choices"]["llm"]["model"], "m1")
        self.assertEqual(saved["choices"]["meeting"]["route"], "online")

    def test_built_config_secrets_never_reach_the_file(self):
        built = wizard.build_plan({"meeting": {"route": "online", "onlineApiKey": "sk-online"},
                                   "llm": {"direct": True, "baseUrl": "http://x/v1",
                                           "apiKey": "sk-direct"}})
        saved = self._saved({"state": "reviewing", "choices": {}, "built": built})
        blob = json.dumps(saved, ensure_ascii=False)
        self.assertNotIn("sk-online", blob)
        self.assertNotIn("sk-direct", blob)
        keys = [r["key"] for r in saved["built"]["config"]]
        self.assertIn("capabilityAsrProviderApiKey", keys, "键名要留着，确认页靠它列清单")

    def test_the_panel_can_still_write_them_at_execute_time(self):
        """收口只针对**落盘**：执行相用的是请求体里那份 choices（前端内存），不受影响。

        这条是这段收口的**前提** —— 如果 execute 也从计划文件取值，抹掉就等于装不上了。
        """
        choices = {"meeting": {"route": "online", "onlineApiKey": "sk-live"}}
        built = wizard.build_plan(choices)
        values = {r["key"]: r["value"] for r in built["config"]}
        self.assertEqual(values.get("capabilityAsrProviderApiKey"), "sk-live",
                         "执行相必须在内存里拿得到真实值")
        self.assertEqual(values.get("capabilityMeetingAsrBackend"), "asr-provider")

    def test_a_redacted_plan_is_not_mistaken_for_a_missing_key(self):
        """抹掉之后**不许误报**：库里已经有密钥时，末页不能说"还没填密钥 / 不能写纪要"。"""
        wizard._settings = lambda name: "sk-stored" if name.endswith("ApiKey") else ""
        online = wizard.build_plan({"meeting": {"route": "online"}})
        self.assertNotIn("还没填密钥", " ".join(m["reason"] for m in online["missing"]))
        direct = wizard.build_plan({"llm": {"direct": True, "baseUrl": "http://x/v1"}})
        self.assertTrue(wizard.minutes_capable({"agent": "",
                                                "llm": {"direct": True,
                                                        "baseUrl": "http://x/v1"}}),
                        "库里已有密钥时不该判成「不能自动写纪要」")
        self.assertEqual(direct["schema"], wizard.PLAN_BUILD_SCHEMA)

    def test_no_route_is_reported_on_the_last_page(self):
        feats = [m["feature"] for m in wizard.build_plan({})["missing"]]
        self.assertTrue(any("录音变成文字" in f for f in feats), feats)

    def test_privacy_none_is_a_conflict_not_a_silent_override(self):
        wizard._settings = lambda name: "none" if name == "capabilityPrivacy" else ""
        plan = wizard.build_plan({"meeting": {"route": "paired"}})
        cfg = {r["key"]: r["value"] for r in plan["config"]}
        self.assertEqual(cfg.get("capabilityMeetingAsrBackend"), "echo-server",
                         "选择本身照写 —— 冲突要说出来，不是把选择丢掉")
        self.assertNotIn("capabilityPrivacy", cfg, "不许顺手改成 lan（那是用户的许可）")
        reasons = " ".join(m["reason"] + m["fix"] for m in plan["missing"])
        self.assertIn("不出机", reasons, plan["missing"])

    def test_the_step_exists_in_the_six_step_plan_schema(self):
        """计划里 `meeting` 是**新加的一块**，老计划文件没有它 —— 缺了不许炸。"""
        self.assertEqual(wizard.build_plan({"meeting": None})["schema"], wizard.PLAN_BUILD_SCHEMA)
        self.assertEqual(wizard.build_plan({"meeting": "garbage"})["schema"],
                         wizard.PLAN_BUILD_SCHEMA)


class InstalledComponentsTests(_PlanTestCase):
    """首装判据 + 执行后真值（设计 §4/§5）。"""

    def _installed(self):
        return os.path.join(self.tmp, wizard.INSTALLED_FILE)

    def test_first_run_follows_the_installed_file(self):
        target = self._installed()
        self.assertTrue(wizard.first_run(target), "这个文件不存在 = 首装")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertFalse(wizard.first_run(target), "写过了 = 面板不再自动进向导")

    def test_finalize_records_truth_and_never_the_secret(self):
        target = self._installed()
        plan_file = os.path.join(self.tmp, "wizard-plan.json")
        choices = {"engines": ["stt-sherpa", "stt-whisper-base"],
                   "llm": {"direct": True, "baseUrl": "http://x/v1",
                           "apiKey": "sk-SECRET"}}
        built = wizard.build_plan(choices)
        wizard.save_plan({"state": "running", "choices": choices, "built": built}, plan_file)

        payload = wizard.finalize(plan_file, path=target, ready=lambda mid: mid == "sherpa")

        with open(target, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("sk-SECRET", raw, "密钥**绝不能**落进这个明文文件")
        self.assertIn("providerLlmApiKey", raw, "但要记下「写过哪些键」（只记键名）")
        self.assertNotIn("sk-SECRET", json.dumps(payload, ensure_ascii=False),
                         "返回值里也不该带密钥")
        self.assertFalse(wizard.first_run(target), "写完就不再是首装")
        self.assertTrue(payload["modelsDir"], "要记下模型文件放在哪")
        # 模型文件「已装」来自 ready() 这个真值，不是计划里的乐观期待
        components = {c["id"]: c for c in payload["components"]}
        self.assertTrue(components["stt-sherpa"]["ready"])
        self.assertFalse(components["stt-whisper-base"]["ready"])

    def test_finalize_survives_a_broken_plan_file(self):
        """计划文件坏掉也要能写出真值：这是向导末页的调用，不能被一个坏文件卡住。"""
        target = self._installed()
        payload = wizard.finalize(os.path.join(self.tmp, "nope.json"), path=target,
                                  ready=lambda mid: None)
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(payload["components"], [])


if __name__ == "__main__":
    unittest.main()
