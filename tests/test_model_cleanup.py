# -*- coding: utf-8 -*-
"""清理（2026-10-31 口径）：客户端已无本地模型 → 没有可清理项。

保留公开形状：preview() 仍返回完整字段（items/suggested/totalMb…），execute()
对任何 id 都拒绝并给原因；`/api/models/cleanup*` 调用不会 500。
"""
import unittest

from app import model_cleanup


class NoLocalModelCleanupTests(unittest.TestCase):
    def test_preview_is_empty(self):
        p = model_cleanup.preview()
        for key in ("days", "items", "suggested", "suggestedMb",
                    "suggestedBytes", "totalMb", "totalBytes", "note"):
            self.assertIn(key, p)
        self.assertEqual(p["items"], [])
        self.assertEqual(p["suggested"], [])
        self.assertEqual(p["totalBytes"], 0)
        self.assertIn("能力后端", p["note"])

    def test_execute_refuses_any_id_with_a_reason(self):
        res = model_cleanup.execute(["sensevoice-onnx"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["freedBytes"], 0)
        self.assertEqual(res["failed"][0]["id"], "sensevoice-onnx")
        self.assertIn("能力后端", res["failed"][0]["reason"])

    def test_execute_with_nothing_selected(self):
        res = model_cleanup.execute([])
        self.assertFalse(res["ok"])
        self.assertEqual(res["removed"], [])

    def test_pin_requires_an_id(self):
        self.assertFalse(model_cleanup.pin("")["ok"])


if __name__ == "__main__":
    unittest.main()
