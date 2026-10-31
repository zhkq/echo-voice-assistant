# -*- coding: utf-8 -*-
"""install_state（2026-10-31 口径）：客户端不再有本地 STT 引擎/模型。

这里钉住仍然有效的部分：
* 安装报告/安装模式/首次启用标记的读写；
* `wanted()` / `missing()` 不再报"缺本地引擎/模型/唤醒/分离"，只剩智能体；
* 客户端引擎表为空（历史设置值不再参与判断）。
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app import install_state


class InstallReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-install-state-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.report = os.path.join(self.tmp, "install-report.json")
        self.mode = os.path.join(self.tmp, "install-mode.json")
        self.first = os.path.join(self.tmp, "first-run.json")

    def test_report_roundtrip_and_declared(self):
        with patch.object(install_state, "_wizard_file_exists", lambda: False):
            self.assertFalse(install_state.declared(self.report))
        saved = install_state.save_report({"agent": "harness", "engines": ["stt-sherpa"]},
                                          path=self.report)
        self.assertEqual(saved["schema"], install_state.REPORT_SCHEMA)
        self.assertTrue(install_state.declared(self.report))
        self.assertEqual(install_state.load_report(self.report)["agent"], "harness")

    def test_broken_report_does_not_raise(self):
        with open(self.report, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(install_state.load_report(self.report), {})

    def test_install_mode_and_first_run(self):
        self.assertEqual(install_state.install_mode(self.mode), "")
        install_state.save_install_mode("fresh", path=self.mode)
        self.assertEqual(install_state.install_mode(self.mode), "fresh")
        self.assertFalse(install_state.first_run_done(self.first))
        install_state.mark_first_run_done(mode="fresh", path=self.first)
        self.assertTrue(install_state.first_run_done(self.first))

    def test_report_never_stores_secret_values(self):
        install_state.save_report({"agent": "harness"}, path=self.report)
        with open(self.report, encoding="utf-8") as fh:
            self.assertNotIn("jwt", fh.read().lower())


class NoLocalEnginesTests(unittest.TestCase):
    def test_engine_tables_are_empty(self):
        self.assertEqual(install_state.ENGINE_SPECS, {})
        self.assertEqual(install_state.STT_TO_ENGINE, {})

    def test_engine_spec_is_empty(self):
        for mid in ("sensevoice-onnx", "sherpa", "qwen3asr", "kws"):
            self.assertEqual(install_state.engine_spec(mid), {}, mid)

    def test_engine_problem_never_blocks_settings(self):
        for choice in ("sensevoice-onnx", "sherpa", "qwen3asr", ""):
            self.assertEqual(install_state.engine_problem(choice), "")

    def test_wanted_ignores_legacy_report_fields(self):
        report = {"engines": ["stt-sherpa"], "wake": True, "diarize": True,
                  "agent": "harness"}
        want = install_state.wanted(report)
        self.assertEqual(want["engines"], [])
        self.assertFalse(want["wake"])
        self.assertFalse(want["diarize"])
        self.assertEqual(want["agent"], "harness")

    def test_missing_has_no_local_model_items(self):
        with patch.object(install_state, "_node_ok", lambda: True), \
                patch.object(install_state, "_harness_online", lambda: True), \
                patch("app.config.settings.get",
                      lambda k, d=None: "harness" if k == "agentBackend" else d):
            miss = install_state.missing({"agent": "harness"})
        self.assertEqual(miss, [])

    def test_state_exposes_empty_engine_list(self):
        with patch.object(install_state, "_node_ok", lambda: True), \
                patch.object(install_state, "_harness_online", lambda: True), \
                patch("app.config.settings.get",
                      lambda k, d=None: "harness" if k == "agentBackend" else d):
            st = install_state.state()
        self.assertEqual(st["engines"], [])
        self.assertEqual(st["wanted"]["engines"], [])
        self.assertTrue(st["ready"])


if __name__ == "__main__":
    unittest.main()
