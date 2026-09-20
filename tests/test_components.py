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
            # 就绪判据：模型类组件走 model_id（问 modelinfo，单一判据），
            # 运行时类组件走自己声明的 detect 规则（2026-09-19）
            self.assertTrue(isinstance(i.get("detect"), dict) or i.get("model_id"),
                            "既没有 detect 规则也没有 model_id：%s" % i.get("id"))
            if i.get("model_id"):
                self.assertIsInstance(i["model_id"], str)

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


class InstallCommandTests(unittest.TestCase):
    """pip 类组件的「下载命令」必须**可直接粘贴执行**（2026-09-19 用户实测提问）。

    用户问："`pip install deepseek-harness-sdk…` 这条我应该在哪个目录执行，cmd 还是 PowerShell？"
    两处都答错了：
      * 那个包**在 PyPI 上已经不存在**（同名新包 `deepseek-harness` 是另一个项目：DeepSeek V4
        的 API 客户端，与 DSH 桌面端无关），而且 ECHO 用的是 DSH Desktop 的本机 HTTP JSON-RPC，
        **不需要任何 Python SDK** → 该组件改成"本机服务"，就绪判据 = 配置里的 dshBaseUrl 通不通；
      * 裸 `pip install x` 会装到 PATH 上第一个 Python 里，ECHO 自己的 venv 看不到 →
        命令必须带**本机解释器全路径**（cwd 无所谓，pip 不看目录）。
    """

    def test_install_command_uses_the_current_interpreter(self):
        cat = {i["id"]: i for i in components.catalog(include_blocked=True)["items"]}
        cmd = cat["runtime-core"]["command"]
        self.assertIn("-m pip install", cmd)
        self.assertIn("requirements.txt", cmd)
        self.assertNotIn("pythonw.exe", cmd.lower(),
                         "不许用 pythonw（无控制台）跑 pip：看不到输出、像是卡住")

    def test_pythonw_is_swapped_for_python(self):
        """ECHO 服务跑在 pythonw.exe 下（无控制台）：命令必须换成同目录的 python.exe。

        否则用户复制到终端执行会"什么都不显示、像卡住"（实测：面板原样吐出 sys.executable）。
        """
        from unittest.mock import patch
        fake = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        with patch.object(sys, "executable", fake):
            cmd = components._install_command({"pkg": "some-pkg"})
        self.assertNotIn("pythonw.exe", cmd.lower())
        self.assertIn("python.exe", cmd.lower())

    def test_manual_components_get_no_command(self):
        """装客户端/自己动手的组件（DSH Desktop、CUDA）不给命令 —— 免得复制一条跑不通的。"""
        cat = {i["id"]: i for i in components.catalog(include_blocked=True)["items"]}
        self.assertEqual(cat["agent-dsh"]["command"], "")
        self.assertEqual(cat["accel-cuda"]["command"], "")

    def test_the_nonexistent_sdk_package_is_not_advertised_anywhere(self):
        """`deepseek-harness-sdk` 已从 PyPI 下线 —— 不许再作为"要装的包"出现在清单或面板里。

        只查**会展示/会被复制**的地方（清单字段、web/、scripts/）：
        代码注释里解释"它为什么被删掉"是允许的（`app/components.py` 就写着这段更正）。
        """
        bad = []
        for item in components.load_manifests():
            if "deepseek-harness-sdk" in json.dumps(item, ensure_ascii=False):
                bad.append("manifest:%s" % item["id"])
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for sub in ("web", "scripts"):
            for dirpath, dirnames, filenames in os.walk(os.path.join(root, sub)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    if not fn.endswith((".js", ".html", ".ps1", ".sh")):
                        continue
                    p = os.path.join(dirpath, fn)
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        if "deepseek-harness-sdk" in fh.read():
                            bad.append(os.path.relpath(p, root))
        self.assertEqual(bad, [], "仍在宣传一个不存在的包：%s" % bad)

    def test_dsh_component_probes_the_configured_service(self):
        """DSH 那条不是"装什么"，而是"服务在不在"：判据取配置里的 dshBaseUrl。"""
        items = {i["id"]: i for i in components.load_manifests()}
        self.assertEqual(items["agent-dsh"]["detect"], {"setting": "dshBaseUrl"})
        self.assertEqual(items["agent-dsh"]["source"], "manual")
        self.assertNotIn("deepseek_harness", json.dumps(items["agent-dsh"]))

    def test_setting_probe_reports_ready_for_a_live_local_server(self):
        import http.server
        import threading
        from unittest.mock import patch

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):             # 静音
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with patch("app.config.settings.get",
                       lambda k, d=None: "http://127.0.0.1:%d" % port if k == "dshBaseUrl" else d):
                self.assertIs(components._detect({"detect": {"setting": "dshBaseUrl"}}), True)
            with patch("app.config.settings.get",
                       lambda k, d=None: "http://127.0.0.1:1" if k == "dshBaseUrl" else d):
                self.assertIs(components._detect({"detect": {"setting": "dshBaseUrl"}}), False)
        finally:
            srv.shutdown()


class StandaloneHarnessComponentTests(unittest.TestCase):
    """运行环境里要有「独立 DeepSeek Harness（本机服务）」（2026-09-19 用户要求：

    "运行环境哪里要增加独立 DSH"）。它与 DSH Desktop 那条的区别：不装桌面客户端也能用，
    而且 ECHO 能把它作为子进程随自己拉起 → 清单里给它一条可复制的启动命令。
    """

    def setUp(self):
        items = {i["id"]: i for i in components.load_manifests()}
        self.item = items.get("agent-harness")
        self.assertIsNotNone(self.item, "清单里缺少独立 harness 那条")

    def test_manifest_shape(self):
        it = self.item
        self.assertEqual(it["kind"], "agent")
        self.assertIn("独立", it["name"])
        self.assertEqual(it["detect"], {"setting": "harnessPort"},
                         "就绪判据取配置里的端口（与 ECHO 实际连的地址一致）")
        self.assertIn("npx", it["command"])
        self.assertIn("--port", it["command"])
        self.assertEqual(it["command_label"], "复制启动命令")
        self.assertTrue(it.get("service"),
                        "它是本机服务（面板因此显示「未运行」而不是「未安装」）")
        self.assertNotIn("pkg", it, "它不是 pip 包")
        self.assertNotIn("requirements", it)

    def test_dsh_desktop_row_is_also_a_service(self):
        items = {i["id"]: i for i in components.load_manifests()}
        self.assertTrue(items["agent-dsh"].get("service"))

    def test_command_survives_catalog(self):
        """catalog() 会用 pip 命令覆盖 command —— 没有 pkg 的组件不能因此被清空。"""
        cat = {i["id"]: i for i in components.catalog(include_blocked=True)["items"]}
        self.assertIn("npx", cat["agent-harness"]["command"])
        self.assertEqual(cat["agent-harness"]["command_label"], "复制启动命令")
        self.assertEqual(cat["runtime-core"]["command_label"], "下载命令",
                         "pip 类组件的标签保持「下载命令」")

    def test_detect_accepts_a_bare_port(self):
        import http.server
        import threading
        from unittest.mock import patch

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                self.send_response(401)             # 任何响应都算"服务在跑"
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with patch("app.config.settings.get",
                       lambda k, d=None: str(port) if k == "harnessPort" else d):
                self.assertIs(components._detect({"detect": {"setting": "harnessPort"}}), True)
            with patch("app.config.settings.get",
                       lambda k, d=None: "1" if k == "harnessPort" else d):
                self.assertIs(components._detect({"detect": {"setting": "harnessPort"}}), False)
        finally:
            srv.shutdown()


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
