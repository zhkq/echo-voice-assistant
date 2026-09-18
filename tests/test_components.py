# -*- coding: utf-8 -*-
"""组件内核测试（app/components.py，P2）。

要钉住的语义（每条都对应 D22/D23 的一处要求）：
  1. 内置清单必须覆盖 ECHO 现有的模型与引擎 —— "已有模型都能被识别为组件"；
  2. 平台过滤：`accel-cuda` 在 mac 上**不出现**（P2 验收项之一）；
  3. `min_os`：`agent-dsh` 在 darwin < 14 上被拦，≥ 14 放行；
  4. 目录清单：`<root>/components/*.json` 同 id 覆盖字段、新 id 追加；
  5. 就绪探测：按 models 根下的路径、按 Python 模块存在性；
  6. `/api/components` 可用，且 `/api/models` **仍然兼容**（1.x 面板与技能还在用）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                        # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

from app import components, paths                                  # noqa: E402
from app.api import router                                         # noqa: E402


class ManifestTests(unittest.TestCase):
    def test_builtin_covers_known_models_and_engines(self):
        ids = {i["id"] for i in components.load_manifests()}
        for want in ("runtime-core", "agent-dsh", "accel-cuda", "stt-sensevoice",
                     "stt-sherpa", "stt-whisper-small", "wake-kws", "diarize-pyannote"):
            self.assertIn(want, ids, "内置清单缺少 %s" % want)
        # 1.x 面板里能看到的 whisper 档位，至少要覆盖常用两档
        self.assertIn("stt-whisper-large-v3", ids)

    def test_every_item_has_the_fields_the_panel_needs(self):
        for i in components.load_manifests():
            self.assertTrue(i.get("id"))
            self.assertIn(i.get("kind"), components.KINDS, i)
            self.assertTrue(i.get("name"), i)
            self.assertIsInstance(i.get("platforms"), list)
            self.assertIsInstance(i.get("detect"), dict)

    def test_required_set(self):
        items = {i["id"]: i for i in components.load_manifests()}
        self.assertTrue(items["runtime-core"]["required"])
        self.assertFalse(items["diarize-pyannote"]["required"])

    def test_directory_manifest_overrides_and_appends(self):
        tmp = tempfile.mkdtemp(prefix="echo-comp-")
        os.makedirs(components.components_dir(tmp), exist_ok=True)
        with open(os.path.join(components.components_dir(tmp), "extra.json"), "w", encoding="utf-8") as fh:
            json.dump([{"id": "stt-sensevoice", "name": "改过的名字", "size_mb": 1},
                       {"id": "custom-thing", "kind": "stt", "name": "自建组件",
                        "platforms": ["win32"], "detect": {}}], fh)
        items = {i["id"]: i for i in components.load_manifests(root=tmp)}
        self.assertEqual(items["stt-sensevoice"]["name"], "改过的名字")
        self.assertEqual(items["stt-sensevoice"]["size_mb"], 1)
        self.assertEqual(items["stt-sensevoice"]["kind"], "stt", "未覆盖的字段应保留内置值")
        self.assertIn("custom-thing", items)


class PlatformFilterTests(unittest.TestCase):
    def test_accel_cuda_does_not_appear_on_macos(self):
        data = components.catalog(platform="macos")
        self.assertNotIn("accel-cuda", [i["id"] for i in data["items"]],
                         "mac 上不该出现 CUDA 组件（P2 验收项）")
        self.assertIn("accel-cuda", [i["id"] for i in components.catalog(platform="win32")["items"]])

    def test_agent_dsh_requires_macos_14(self):
        item = {"platforms": ["macos", "win32"], "min_os": {"macos": "14.0"}}
        self.assertFalse(components.applicable(item, platform="macos", os_version=(13, 5))[0])
        self.assertTrue(components.applicable(item, platform="macos", os_version=(14, 0))[0])
        self.assertTrue(components.applicable(item, platform="macos", os_version=(15, 2))[0])
        self.assertTrue(components.applicable(item, platform="win32", os_version=(10, 0))[0],
                        "min_os 只约束声明的平台")

    def test_blocked_items_can_be_included_with_reason(self):
        data = components.catalog(platform="macos", include_blocked=True)
        accel = [i for i in data["items"] if i["id"] == "accel-cuda"]
        self.assertEqual(len(accel), 1)
        self.assertFalse(accel[0]["applicable"])
        self.assertTrue(accel[0]["blockedReason"])

    def test_unknown_os_version_does_not_block(self):
        item = {"platforms": ["macos"], "min_os": {"macos": "14.0"}}
        self.assertTrue(components.applicable(item, platform="macos", os_version=())[0],
                        "取不到系统版本时宁可放行，也不要把组件误判为不可用")


class ReadyDetectionTests(unittest.TestCase):
    def setUp(self):
        self._get = paths._settings_get
        self.tmp = tempfile.mkdtemp(prefix="echo-models-")
        paths._settings_get = lambda name: self.tmp if name == "modelsDir" else ""

    def tearDown(self):
        paths._settings_get = self._get

    def test_path_detection(self):
        os.makedirs(os.path.join(self.tmp, "sensevoice"), exist_ok=True)
        self.assertTrue(components._detect({"detect": {"path": "sensevoice"}}))
        self.assertFalse(components._detect({"detect": {"path": "no-such-dir"}}))

    def test_any_detection_matches_file_or_dir(self):
        os.makedirs(os.path.join(self.tmp, "faster-whisper", "small"), exist_ok=True)
        with open(os.path.join(self.tmp, "faster-whisper", "small", "model.bin"), "wb") as fh:
            fh.write(b"x")
        probe = {"detect": {"any": ["faster-whisper/small/model.bin",
                                    "hub/models--Systran--faster-whisper-small"]}}
        self.assertTrue(components._detect(probe))

    def test_python_detection(self):
        self.assertTrue(components._detect({"detect": {"python": "json"}}))
        self.assertFalse(components._detect({"detect": {"python": "definitely_not_a_module_xyz"}}))

    def test_no_detect_rule_is_unknown_not_false(self):
        self.assertIsNone(components._detect({"detect": {}}))

    def test_catalog_marks_missing_required(self):
        s = components.summary(platform="win32")
        self.assertIn("runtime-core", s["required"])
        self.assertIsInstance(s["missingRequired"], list)


class ComponentsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_components_endpoint(self):
        r = self.client.get("/api/components")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("platform", data)
        self.assertIn("osVersion", data)
        ids = [i["id"] for i in data["items"]]
        self.assertIn("runtime-core", ids)
        for i in data["items"]:
            self.assertTrue(i["applicable"])
            self.assertIn("ready", i)

    def test_components_endpoint_supports_platform_override(self):
        r = self.client.get("/api/components", params={"platform": "macos"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("accel-cuda", [i["id"] for i in r.json()["items"]])

    def test_models_endpoint_still_compatible(self):
        r = self.client.get("/api/models")
        self.assertEqual(r.status_code, 200, "1.x 的 /api/models 必须继续可用")
        self.assertIn("items", r.json())


if __name__ == "__main__":
    unittest.main()
