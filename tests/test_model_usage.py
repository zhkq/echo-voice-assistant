# -*- coding: utf-8 -*-
"""模型使用账本（2026-10-31 口径）：客户端已无本地模型。

账本模块保留为历史兼容层：`known_ids()` / `in_use_ids()` 必须为空（清理页据此
不给建议），`note_used()` 等写入函数保留签名但不再有生产调用点。
"""
import unittest

from app import model_usage


class EmptyCatalogTests(unittest.TestCase):
    def test_no_known_local_models(self):
        self.assertEqual(model_usage.known_ids(), [])

    def test_nothing_is_in_use(self):
        self.assertEqual(model_usage.in_use_ids(), {})

    def test_engine_to_model_mapping_is_gone(self):
        for value in ("sensevoice-onnx", "sherpa", "qwen3asr", "whisper-small", ""):
            self.assertEqual(model_usage.engine_model_id(value), "", value)
        self.assertEqual(model_usage.model_id_for_engine("sherpa", "sherpa"), "")
        self.assertEqual(model_usage.model_id_for_engine("sensevoice-onnx", ""), "")


if __name__ == "__main__":
    unittest.main()
