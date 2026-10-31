# -*- coding: utf-8 -*-
"""清单对齐守卫（2026-10-31 口径）：客户端不再有本地模型组件。

旧版的守卫是"`modelinfo.CATALOG` ↔ 组件清单不能分叉"；现在两边都应该是
**空模型清单**：客户端不装任何本地 STT/唤醒/分离模型（转写全走能力后端）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import components, modelinfo  # noqa: E402


class CatalogAlignmentTests(unittest.TestCase):
    def test_modelinfo_has_no_local_models(self):
        self.assertEqual(modelinfo.CATALOG, [])

    def test_components_have_no_local_model_entries(self):
        items = components.load_manifests()
        with_model = [i["id"] for i in items if i.get("model_id")]
        self.assertEqual(with_model, [], "组件清单里还有本地模型条目：%s" % with_model)

    def test_components_have_no_stt_wake_or_diarize_kinds(self):
        items = components.load_manifests()
        bad = [(i["id"], i.get("kind")) for i in items
               if i.get("kind") in ("stt", "wake", "diarize")]
        self.assertEqual(bad, [], "组件清单里还有本地 STT/唤醒/分离组件：%s" % bad)

    def test_required_components_are_only_the_runtime(self):
        items = {i["id"]: i for i in components.load_manifests()}
        required = [i["id"] for i in items.values() if i.get("required")]
        self.assertEqual(required, ["runtime-core"])
        self.assertEqual(tuple(components.REQUIRED_IDS), ("runtime-core",))

    def test_model_ready_has_nothing_to_probe(self):
        for mid in ("sensevoice-onnx", "sherpa", "qwen3asr", "kws", "pyannote"):
            self.assertIsNone(modelinfo.ready(mid), mid)


if __name__ == "__main__":
    unittest.main()
