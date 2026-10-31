# -*- coding: utf-8 -*-
"""交付档位（2026-10-31 口径）：**客户端默认档不装任何本地 STT 模型/引擎**。

用户拍板：指令 / 回顾 / 会议转写统一走能力后端（echo-server + Qwen3-ASR），
语音唤醒取消；客户端安装包从 ~430 MB 缩到 ~58 MB 量级。

这组用例把交付承诺变成会红的东西：
1. 默认档组件只剩 `runtime-core`，依赖清单里没有任何本地 STT 引擎/模型下载客户端；
2. 组件清单/档位表/离线包规格里不再有 stt-* / wake-* / diarize-* 本地模型组件；
3. 体积口径可加：runtime-core + DSH（extraMb）≈ 342 MB，落在缩包后的预算内；
4. 安装脚本用的仍是默认档的 requirements-core.txt。
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(ROOT, "components", "profiles.json")
OFFLINE_PACK = os.path.join(ROOT, "components", "offline-pack.json")

#: 客户端默认档**不许**出现的本地 STT/唤醒/分离组件前缀。
LOCAL_MODEL_PREFIXES = ("stt-", "wake-", "diarize-")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _package_names(text: str):
    out = set()
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip().lower()
        if name:
            out.add(name)
    return out


class ProfileTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = _load(PROFILES)
        cls.profiles = {p["id"]: p for p in cls.doc["profiles"]}
        cls.pack = {c["id"]: c for c in _load(OFFLINE_PACK)}

    def test_the_two_profiles_still_exist(self):
        self.assertEqual(sorted(self.profiles), ["default", "offline"])

    def test_every_profile_has_the_fields_the_installer_needs(self):
        for pid, p in self.profiles.items():
            with self.subTest(profile=pid):
                for key in ("label", "summary", "requirements", "components", "accept"):
                    self.assertIn(key, p, "%s 缺 %s" % (pid, key))
                self.assertTrue(os.path.isfile(os.path.join(ROOT, p["requirements"])))
                self.assertTrue(p["accept"])

    def test_no_local_model_component_anywhere(self):
        for pid, p in self.profiles.items():
            with self.subTest(profile=pid):
                bad = [c for c in p["components"] if c.startswith(LOCAL_MODEL_PREFIXES)]
                self.assertEqual(bad, [], "%s 里还有本地模型组件：%s" % (pid, bad))

    def test_offline_pack_spec_has_no_local_models(self):
        bad = [cid for cid in self.pack if cid.startswith(LOCAL_MODEL_PREFIXES)]
        self.assertEqual(bad, [], "offline-pack.json 里还有本地模型组件：%s" % bad)

    def test_no_component_belongs_to_two_profiles(self):
        seen = {}
        for pid, p in self.profiles.items():
            for cid in p["components"]:
                self.assertNotIn(cid, seen)
                seen[cid] = pid

    def test_every_offline_component_belongs_to_a_profile(self):
        orphans = sorted(set(self.pack) - {c for p in self.profiles.values()
                                           for c in p["components"]})
        self.assertEqual(orphans, [], "这些组件不属于任何档：%s" % orphans)

    def test_a_profile_only_names_components_that_exist(self):
        unknown = sorted({c for p in self.profiles.values() for c in p["components"]}
                         - set(self.pack))
        self.assertEqual(unknown, [], "档位表里出现了没声明的组件：%s" % unknown)


class DefaultProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = {p["id"]: p for p in _load(PROFILES)["profiles"]}
        cls.pack = {c["id"]: c for c in _load(OFFLINE_PACK)}
        cls.default = cls.profiles["default"]
        with open(os.path.join(ROOT, cls.default["requirements"]), encoding="utf-8") as fh:
            cls.req_text = fh.read()
        cls.names = _package_names(cls.req_text)

    def test_the_default_profile_only_installs_the_runtime(self):
        self.assertEqual(self.default["components"], ["runtime-core"])

    def test_the_default_profile_has_no_local_stt_or_download_client(self):
        banned = {"sherpa-onnx", "modelscope", "pypinyin", "funasr", "faster-whisper",
                  "pyannote.audio", "torch", "onnxruntime"}
        hits = sorted(banned & self.names)
        self.assertEqual(hits, [], "默认档依赖里还有本地 STT/模型下载依赖：%s" % hits)

    def test_the_default_profile_has_no_torch_family(self):
        hits = sorted(n for n in self.default["forbid"] if n in self.names)
        self.assertEqual(hits, [])

    def test_the_default_profile_has_no_local_model_component(self):
        hits = sorted(c for c in self.default.get("forbidComponents", [])
                      if c in self.default["components"])
        self.assertEqual(hits, [])

    def test_required_deps_are_declared(self):
        missing = sorted(n for n in self.default["require"] if n not in self.names)
        self.assertEqual(missing, [], "默认档缺了这些：%s" % missing)

    def test_the_default_size_is_within_the_slim_budget(self):
        """缩包后的口径：runtime-core ≈120 MB + DSH(extraMb 222) ≈ 342 MB。"""
        comp = sum(int(self.pack[c]["pack"].get("approx_mb") or 0)
                   for c in self.default["components"])
        total = comp + int(self.default.get("extraMb") or 0)
        self.assertLessEqual(total, 500, "默认档声明 %d MB，超了缩包后的预算" % total)
        self.assertGreaterEqual(total, 250, "默认档只有 %d MB？八成是漏了运行时" % total)

    def test_the_declared_size_has_exactly_one_source(self):
        for pid, p in self.profiles.items():
            self.assertNotIn("approxMb", p, "%s 里又存了一份合计" % pid)

    def test_offline_profile_is_deprecated_and_empty(self):
        offline = self.profiles["offline"]
        self.assertEqual(offline["components"], [])
        self.assertIn("废弃", offline["summary"])


class InstallerUsesTheDefaultProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "scripts", "install.ps1"), encoding="utf-8") as fh:
            cls.install = fh.read()
        cls.default = {p["id"]: p for p in _load(PROFILES)["profiles"]}["default"]

    def test_the_installer_installs_the_default_profiles_requirements(self):
        self.assertIn(self.default["requirements"], self.install)

    def test_the_installer_does_not_install_the_heavy_list_by_default(self):
        self.assertNotIn("requirements.txt", self.install.replace(
            "requirements-core.txt", ""))

    def test_the_multi_platform_packer_packs_the_profile_table(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "components", "profiles.json")))
        with open(os.path.join(ROOT, "scripts", "build-package.ps1"), encoding="utf-8") as fh:
            packer = fh.read()
        self.assertIn("'components'", packer)


if __name__ == "__main__":
    unittest.main()
