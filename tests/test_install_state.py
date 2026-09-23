# -*- coding: utf-8 -*-
"""安装状态（技能优先）与下载状态词的守卫。

背景（2026-09-21 实测反馈）
--------------------------
同事在一台新机器上跑安装技能，反馈三件事，其中两件在这里钉住：

1. **技能装完，进 ECHO 还是进向导页** —— 面板的首装判据只有 `installed-components.json`，
   而那个文件只有向导末页才写。技能现在会 `POST /api/install/report`，`install_state.declared()`
   也认它，面板就不再提示"没装完"、也不再自动进向导。
2. **选 qwen3asr 一直显示"正在准备中 / 排队中"** —— 根因是状态词表不一致：
   `modelinfo._download_worker` 失败写 `"failed"`，而 `wizard.execution_state` 判断 `"error"`，
   于是**失败被当成"还没开始"**。这里用真实的 `jobs()` 形状把它钉死。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import install_state, wizard                      # noqa: E402


class DownloadStateVocabularyTests(unittest.TestCase):
    """失败必须显示"没成 + 原因"，不能显示"排队中"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-installstate-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.plan_file = os.path.join(self.tmp, "plan.json")
        wizard.save_plan({
            "state": "running",
            "built": {"downloads": [
                {"component": "stt-qwen3asr", "modelId": "qwen3asr", "label": "Qwen3-ASR", "approxMb": 3600},
                {"component": "stt-sherpa", "modelId": "sherpa", "label": "sherpa", "approxMb": 189},
            ]},
            "execution": {"skipped": [], "failed": []},
        }, self.plan_file)

    def _state_with(self, jobs):
        with patch("app.modelinfo.jobs", lambda: {"active": None, "items": jobs}):
            return wizard.execution_state(self.plan_file)

    def test_failed_job_is_reported_as_failed_with_reason(self):
        """**这就是同事看到的那个 bug**：worker 写 "failed"，向导原来只认 "error"。"""
        st = self._state_with({
            "qwen3asr": {"status": "failed", "message": "ImportError: No module named 'modelscope'"},
        })
        row = [r for r in st["items"] if r["modelId"] == "qwen3asr"][0]
        self.assertEqual("error", row["state"], "失败必须显示成失败")
        self.assertEqual("没成", row["text"])
        self.assertIn("modelscope", row["message"], "要把原因带出来，别只说'没成'")

    def test_legacy_error_spelling_still_works(self):
        st = self._state_with({"qwen3asr": {"status": "error", "error": "boom"}})
        row = [r for r in st["items"] if r["modelId"] == "qwen3asr"][0]
        self.assertEqual("error", row["state"])

    def test_unknown_status_is_queued_and_done_is_done(self):
        st = self._state_with({"sherpa": {"status": "done"}})
        rows = {r["modelId"]: r for r in st["items"]}
        self.assertEqual("done", rows["sherpa"]["state"])
        self.assertEqual("queued", rows["qwen3asr"]["state"], "没见过的状态保守显示为排队中")
        self.assertEqual(1, st["finished"])

    def test_modelinfo_job_state_normalises(self):
        from app import modelinfo
        self.assertEqual("failed", modelinfo.job_state({"status": "failed"}))
        self.assertEqual("failed", modelinfo.job_state({"status": "error"}))
        self.assertEqual("running", modelinfo.job_state({"status": "running"}))
        self.assertEqual("done", modelinfo.job_state({"status": "done"}))
        self.assertEqual("queued", modelinfo.job_state({}))
        self.assertEqual("queued", modelinfo.job_state(None))


class InstallReportTests(unittest.TestCase):
    """技能登记 → declared() 为真 → 面板不再当"没装完"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-installreport-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "install-report.json")

    def test_no_report_no_wizard_file_means_not_declared(self):
        with patch.object(wizard, "installed_path", lambda: os.path.join(self.tmp, "nope.json")):
            self.assertFalse(install_state.declared(self.path))

    def test_wizard_file_alone_counts_as_declared(self):
        legacy = os.path.join(self.tmp, "installed-components.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with patch.object(wizard, "installed_path", lambda: legacy):
            self.assertTrue(install_state.declared(self.path),
                            "老安装（向导装的）不能被当成首装")

    def test_report_roundtrip_and_declared(self):
        saved = install_state.save_report({"engines": ["sherpa"], "agent": "harness"}, self.path)
        self.assertEqual(install_state.REPORT_SCHEMA, saved["schema"])
        self.assertTrue(saved.get("savedAt"))
        with patch.object(wizard, "installed_path", lambda: os.path.join(self.tmp, "nope.json")):
            self.assertTrue(install_state.declared(self.path))
            back = install_state.load_report(self.path)
        self.assertEqual(["sherpa"], back["engines"])

    def test_broken_report_file_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual({}, install_state.load_report(self.path))

    def test_wanted_falls_back_to_settings_when_no_report(self):
        """没有报告时（老安装）从设置推"用户想要什么"。"""
        report = {}
        self.assertEqual({}, report)
        with patch("app.config.settings.get", side_effect=lambda k, d=None: {
                "sttModel": "base", "meetingSttModel": "sherpa",
                "wakeEnabled": True, "agentBackend": "harness"}.get(k, d)):
            want = install_state.wanted(report)
        self.assertIn("whisper-base", want["engines"])
        self.assertIn("sherpa", want["engines"])
        self.assertTrue(want["wake"])
        self.assertEqual("harness", want["agent"])

    def test_missing_calls_out_a_missing_module(self):
        report = {"engines": ["qwen3asr"], "agent": "none"}
        # 依赖探测注入成"没有"，模型探测注入成"就绪" → 必须报"缺依赖"
        with patch.object(install_state, "_module_ok", lambda name: False), \
                patch.object(install_state, "_model_ready", lambda mid: True):
            miss = install_state.missing(report)
        self.assertTrue(miss)
        # 报的必须是**真正的引擎包** qwen_asr：写 transformers 时，装了 transformers 5.x
        # 而没装 qwen-asr 也会判"就绪"，用户拿到的是一句莫名其妙的运行时错
        # （2026-09-23 实测：RuntimeError: qwen-asr package is required for Qwen3-ASR）。
        self.assertIn("qwen_asr", miss[0]["reason"])

    def test_missing_is_empty_when_everything_checks_out(self):
        report = {"engines": ["sherpa"], "agent": "none"}
        with patch.object(install_state, "_module_ok", lambda name: True), \
                patch.object(install_state, "_model_ready", lambda mid: True):
            self.assertEqual([], install_state.missing(report))

    def test_desktop_choice_is_judged_by_the_desktop_not_the_harness(self):
        """选了 DSH Desktop 时，在线与否要看**桌面版**（2026-09-23 迁移实测撞到）。

        `_harness_online()` 在 `harness_proc.requested()` 为假（= agentBackend 不是 harness）
        时**必然返回 False**，而原来 dsh 分支也去问它 —— 于是选了 Desktop 的机器永远被告知
        "harness 没在运行、还没装完"。与同事报过的「拿另一个适配器的状态判断」是同一个病。
        """
        report = {"engines": [], "agent": "dsh"}
        # Desktop 在线 + harness 必然"不在线"：不该报任何缺失
        with patch.object(install_state, "_dsh_online", lambda: True), \
                patch.object(install_state, "_harness_online", lambda: False), \
                patch.object(install_state, "_node_ok", lambda: False):
            self.assertEqual([], install_state.missing(report),
                             "选了 Desktop 却拿 harness 的状态报缺失")
        # Desktop 不在线：要把原因说成桌面版，别再提 harness
        with patch.object(install_state, "_dsh_online", lambda: False):
            miss = install_state.missing(report)
        self.assertTrue(miss)
        self.assertIn("DSH Desktop 没在运行", miss[0]["reason"])
        self.assertNotIn("harness 没在运行", miss[0]["reason"])

    def test_harness_choice_still_checks_node_and_harness(self):
        report = {"engines": [], "agent": "harness"}
        with patch.object(install_state, "_dsh_online", lambda: True), \
                patch.object(install_state, "_node_ok", lambda: False):
            miss = install_state.missing(report)
        self.assertTrue(miss)
        self.assertIn("Node.js", miss[0]["reason"])

    def test_state_exposes_engine_detail(self):
        report = {"engines": ["sherpa"], "agent": "none"}
        with patch.object(install_state, "load_report", lambda path="": report), \
                patch.object(install_state, "declared", lambda path="": True), \
                patch.object(install_state, "_module_ok", lambda name: True), \
                patch.object(install_state, "_model_ready", lambda mid: mid == "sherpa"):
            st = install_state.state()
        self.assertTrue(st["declared"])
        self.assertTrue(st["ready"])
        self.assertEqual("sherpa", st["engines"][0]["id"])
        self.assertTrue(st["engines"][0]["modelReady"])

    def test_engine_specs_match_the_app_engine_choices(self):
        """权威表里的 stt 值必须是 app 认得的（与安装器镜像由另一个测试盯）。"""
        from app.audio import stt as stt_mod
        for engine, spec in install_state.ENGINE_SPECS.items():
            got_engine, _model = stt_mod.resolve_engine(spec["stt"])
            if engine.startswith("whisper-"):
                self.assertEqual("whisper", got_engine, engine)
                self.assertIn(spec["stt"], stt_mod.WHISPER_MODELS, engine)
            elif engine == "sherpa":
                self.assertEqual("sherpa", got_engine)
            elif engine == "sensevoice":
                self.assertEqual("sensevoice", got_engine)
            elif engine == "qwen3asr":
                self.assertEqual("qwen3asr", got_engine)
            else:
                self.fail("测试没覆盖这个引擎：%s" % engine)

    def test_report_never_stores_secret_values(self):
        """报告是明文，可能被拷来拷去 —— 结构里不许出现"值"的设置键。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "install_state.py"), encoding="utf-8").read()
        for risky in ("api_key", "apiKey", "token", "secret", "password"):
            self.assertNotIn(risky, src.lower(), "安装状态里不该出现任何密钥字段")
        # 报告里只记"装了什么"，不记设置值 —— 顺便确认 json 是唯一的持久化方式
        self.assertIn("install-report.json", src)


class PyannoteSourceTests(unittest.TestCase):
    """说话人分离的权重：**改成从 ModelScope 自动装**（2026-09-21）。

    背景：HF 上 pyannote 三个仓库是 gated（要同意条款 + Token），以前只能让用户自己去
    HF 同意再拉 —— 同事选了"说话人分离"就卡在这一步。实测 ModelScope 上**同名仓库匿名可下**
    （segmentation-3.0 / wespeaker-voxceleb-resnet34-LM / speaker-diarization-community-1，
    含 plda/plda.npz 与 plda/xvec_transform.npz），于是能和别的引擎一样一键下载。

    这里钉三件事：① 下载闸门不再拒绝；② 落盘目录与 `app/audio/diarize.py` 期望的一致；
    ③ 权重**仍然不随交付包分发**（never_ship 不动）—— 那是"再分发"与"用户机器上下载"的分界。
    """

    def setUp(self):
        from app import modelinfo
        self.modelinfo = modelinfo

    def test_download_gate_no_longer_refuses_pyannote(self):
        entry = self.modelinfo._by_id("pyannote")
        self.assertIsNotNone(entry, "目录里得有 pyannote")
        self.assertIsNot(entry.get("downloadable"), False,
                         "downloadable=False 会让面板和技能都下不了它")

    def test_assets_land_where_diarize_looks(self):
        import re
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "audio", "diarize.py"), encoding="utf-8").read()
        folders = {folder for _repo, folder, _files in self.modelinfo.PYANNOTE_ASSETS}
        for folder in folders:
            self.assertIn('"%s"' % folder, src,
                          f"diarize.py 里找不到 {folder} —— 下载下来也用不上")
        # plda 要落在 <folder>/plda/ 下（diarize 的 plda_dir 多拼了一层）
        plda_files = [f for _r, folder, files in self.modelinfo.PYANNOTE_ASSETS
                      if folder == "pyannote-plda-local" for f in files]
        self.assertTrue(any(f.startswith("plda/") for f in plda_files),
                        "plda 的两个文件必须在 plda/ 子目录里")

    def test_weights_are_still_never_shipped(self):
        from app import components
        entry = [i for i in components.load_manifests() if i["id"] == "diarize-pyannote"][0]
        self.assertTrue(entry.get("never_ship"),
                        "权重仍不许随交付包分发 —— 只是允许在用户机器上下载")
        self.assertEqual("modelscope", entry.get("source"))

    def test_skill_installers_wire_diarize_to_the_download(self):
        """技能里 --diarize 必须真的**触发下载**，不能只打一句提示。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ps1 = open(os.path.join(root, ".dsh", "skills", "echo-install", "scripts",
                                "echo-install-components.ps1"), encoding="utf-8").read()
        sh = open(os.path.join(root, ".dsh", "skills", "echo-install", "scripts",
                               "echo-install-components.sh"), encoding="utf-8").read()
        self.assertIn("@('pyannote',", ps1, "Windows 技能要把 pyannote 加进待下载清单")
        self.assertIn("pyannote", sh, "mac 技能同理")


class VCRuntimeGuidanceTests(unittest.TestCase):
    """Windows 上"包在、导不进来"要指向 VC++ 运行库（2026-09-21 同事实测卡在这）。"""

    def test_skill_preflights_and_installs_vc_runtime(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ps1 = open(os.path.join(root, ".dsh", "skills", "echo-install", "scripts",
                                "echo-install-components.ps1"), encoding="utf-8").read()
        # 官方检测点（注册表 14.0\VC\Runtimes\x64 的 Installed=1）
        self.assertIn("VC\\Runtimes\\x64", ps1, "要用官方注册表检测点判断装没装")
        self.assertIn("aka.ms/vs/17/release/vc_redist.x64.exe", ps1, "要给官方下载地址")
        self.assertIn("function Install-VCRuntime", ps1, "要能自己把它装上")
        # 失败要区分"缺包"和"缺 DLL"
        self.assertIn("DLL load failed", ps1)
        self.assertIn("function Test-DllLoadFailure", ps1)

    def test_skill_docs_mention_vc_runtime(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        md = open(os.path.join(root, ".dsh", "skills", "echo-install", "SKILL.md"),
                  encoding="utf-8").read()
        self.assertIn("vc_redist.x64.exe", md, "技能文档的失败表要写这一条")
        self.assertIn("DLL load failed", md)

    def test_missing_hint_mentions_vc_runtime_on_windows(self):
        report = {"engines": ["sherpa"], "agent": "none"}
        with patch.object(install_state, "_module_ok", lambda name: False), \
                patch.object(install_state, "_model_ready", lambda mid: True), \
                patch.object(install_state, "_is_windows", lambda: True):
            miss = install_state.missing(report)
        self.assertIn("Visual C++", miss[0]["fix"], "Windows 上要顺手提示 VC++ 运行库")

    def test_windows_detection_goes_through_the_platform_seam(self):
        """app/ 里不许直接写 os.name 分支（audit-paths 会拦，2026-09-21 实测）。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "install_state.py"), encoding="utf-8").read()
        self.assertNotIn('os.name == "nt"', src, "要走 platform.current()，别自己判 os.name")
        self.assertIn("platform as echo_platform", src)


class EngineProblemTests(unittest.TestCase):
    """`engine_problem()` —— "这个 sttModel 取值现在能不能真用"（2026-09-23 事故）。

    事故：用户在面板上把命令转写引擎改成 sherpa，而稳定版 runtime-core 里没有
    `sherpa_onnx`。安装报告里登记的是另一个引擎，所以 `missing()` 一句话都不说；
    设置热生效也绕过了校验 —— 直到说第一句命令才静默转写出空串。
    """

    def test_missing_module_is_reported(self):
        with patch("app.install_state._module_ok", lambda name: name != "sherpa_onnx"), \
                patch("app.install_state._model_ready", lambda mid: True):
            problem = install_state.engine_problem("sherpa")
        self.assertIn("sherpa_onnx", problem, "要说清缺哪个模块")
        self.assertIn("能力", problem, "还要告诉用户去哪儿装")

    def test_missing_model_is_reported(self):
        with patch("app.install_state._module_ok", lambda name: True), \
                patch("app.install_state._model_ready", lambda mid: False):
            problem = install_state.engine_problem("sherpa")
        self.assertIn("模型", problem)

    def test_ready_engine_has_no_problem(self):
        with patch("app.install_state._module_ok", lambda name: True), \
                patch("app.install_state._model_ready", lambda mid: True):
            self.assertEqual(install_state.engine_problem("sherpa"), "")
            self.assertEqual(install_state.engine_problem("sensevoice"), "")

    def test_unknown_choice_is_not_judged(self):
        """认不出的值交给 stt.resolve_engine 的既有回退（它自己有告警），这里不表态。"""
        self.assertEqual(install_state.engine_problem("不存在的引擎"), "")
        self.assertEqual(install_state.engine_problem(""), "")

    def test_engine_spec_maps_model_id_back_to_the_engine(self):
        self.assertEqual(install_state.engine_spec("sherpa")["module"], "sherpa_onnx")
        self.assertEqual(install_state.engine_spec("whisper-base")["stt"], "base")
        self.assertEqual(install_state.engine_spec("kws"), {}, "KWS 不是转写引擎")

    def test_problem_never_raises_on_a_broken_probe(self):
        with patch("app.install_state._module_ok", side_effect=RuntimeError("boom")):
            try:
                install_state.engine_problem("sherpa")
            except Exception as exc:                       # pragma: no cover - 失败即测试失败
                self.fail("校验不能把调用方（保存设置）带崩：%s" % exc)


if __name__ == "__main__":
    unittest.main()
