# -*- coding: utf-8 -*-
"""客户端本地模型清单（2026-10-31 口径）：**已经退役**。

用户拍板客户端不装本地 STT 模型/引擎（转写全走能力后端），所以 modelinfo 的
CATALOG 为空、download 被拒、就绪探测返回 None。这里钉住这些公开形状，
保证 `/api/models`、向导、清理页调用它们不会 500。
"""
import unittest

from app import modelinfo


class NoLocalModelsTests(unittest.TestCase):
    def test_catalog_is_empty(self):
        self.assertEqual(modelinfo.CATALOG, [])

    def test_inventory_is_empty(self):
        self.assertEqual(modelinfo.inventory(), [])

    def test_ready_returns_none_for_any_id(self):
        for mid in ("sensevoice-onnx", "sherpa", "qwen3asr", "whisper-small", "kws", "pyannote"):
            self.assertIsNone(modelinfo.ready(mid), mid)

    def test_model_paths_is_empty(self):
        self.assertEqual(modelinfo.model_paths("sensevoice-onnx"), [])

    def test_start_download_is_refused_with_a_reason(self):
        ok, msg = modelinfo.start_download("sensevoice-onnx")
        self.assertFalse(ok)
        self.assertIn("能力后端", msg)

    def test_jobs_shape_is_still_stable(self):
        jobs = modelinfo.jobs()
        self.assertEqual(jobs, {"active": None, "items": {}})

    def test_job_state_normalisation_is_unchanged(self):
        self.assertEqual(modelinfo.job_state({"status": "failed"}), "failed")
        self.assertEqual(modelinfo.job_state({"status": "error"}), "failed")
        self.assertEqual(modelinfo.job_state({"status": "running"}), "running")
        self.assertEqual(modelinfo.job_state({"status": "done"}), "done")
        self.assertEqual(modelinfo.job_state({}), "queued")
        self.assertEqual(modelinfo.job_state(None), "queued")

    def test_dependency_problem_is_empty(self):
        self.assertEqual(modelinfo.dependency_problem("sensevoice-onnx"), {})

    def test_ready_pyannote_is_false_on_client(self):
        self.assertFalse(modelinfo.ready_pyannote())


if __name__ == "__main__":
    unittest.main()
