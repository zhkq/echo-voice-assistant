# -*- coding: utf-8 -*-
"""离线组件合集出包脚本（`scripts/build_offline_pack.py`，D23）。

这一层不造运行时（那要几分钟、几百 MB），只钉**三条契约** —— 它们错了会在目标机上变成
"装到一半失败"或"装上了但默认档里混进 torch"：

  1. **只打规格里声明过的组件**：id 不在 `components/offline-pack.json` 里 → 直接拒绝
     （与 `tests/test_default_profile.py` 的"没有孤儿组件"是同一枚硬币的两面）；
  2. **`runtime-core` 的依赖清单就是 `requirements-core.txt`**，而那份清单**不许含 torch**
     （铁律 L1：默认档零 torch）—— 这里静态核一遍，出包时的动态核在脚本里；
  3. **zip 带顶层目录前缀**（解开是一个文件夹，不是一堆散文件）。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_PATH = os.path.join(ROOT, "scripts", "build_offline_pack.py")
_spec = importlib.util.spec_from_file_location("echo_build_offline", _PATH)
packer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(packer)


class SpecTests(unittest.TestCase):
    def test_it_reads_the_declared_components(self):
        spec = packer.load_spec()
        self.assertIn("runtime-core", spec)
        self.assertIn("stt-sherpa", spec)
        self.assertEqual(spec["runtime-core"]["pack"]["kind"], "runtime")
        self.assertEqual(spec["stt-sherpa"]["pack"]["kind"], "files")

    def test_runtime_config_comes_from_requirements_core(self):
        """规格里 runtime-core 的依赖清单必须指向 `requirements-core.txt`（唯一权威）。"""
        self.assertEqual(packer.load_spec()["runtime-core"]["pack"]["requirements"],
                         "requirements-core.txt")
        self.assertEqual(packer.REQUIREMENTS, "requirements-core.txt")

    def test_the_core_requirements_have_no_torch(self):
        """**铁律 L1**（静态那一半）：默认档的依赖清单里不许出现 torch 系。

        动态那一半在 `build_runtime_core()` 里（装完实测 `find_spec('torch') is None`）——
        两边都要有：静态这半挡住"清单被改坏"，动态那半挡住"被别的包顺带拉进来"。
        """
        path = os.path.join(ROOT, packer.REQUIREMENTS)
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
        for ln in lines:
            self.assertNotIn("torch", ln.lower(), "默认档依赖清单里混进了 torch 系：%s" % ln)


class GuardTests(unittest.TestCase):
    def test_an_unknown_component_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(packer.PackError) as ctx:
                packer.main(["--out", tmp, "--components", "runtime-core,nope-xyz"])
        self.assertIn("nope-xyz", str(ctx.exception))
        self.assertIn("offline-pack.json", str(ctx.exception))

    def test_a_bogus_python_source_is_refused_before_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(packer.PackError) as ctx:
                packer.build_runtime_core({}, os.path.join(tmp, "runtime-core"),
                                          os.path.join(tmp, "not-a-python"), "https://x/simple")
        self.assertIn("CPython", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(tmp, "runtime-core")))

    def test_a_files_component_whose_source_is_missing_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(packer.PackError) as ctx:
                packer.build_files_component(
                    {"items": [{"from": "models/definitely-not-here", "to": "models/x"}]}, tmp)
        self.assertIn("definitely-not-here", str(ctx.exception))


class ZipTests(unittest.TestCase):
    def test_the_zip_has_a_top_level_folder(self):
        """**zip 条目必须带顶层前缀**（这个坑不报错：解开会变成一堆散文件）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "pack")
            os.makedirs(os.path.join(root, "runtime-core"))
            with open(os.path.join(root, "runtime-core", "python.exe"), "w") as fh:
                fh.write("x")
            zip_path = os.path.join(tmp, "ECHO-离线组件合集-test.zip")
            packer.make_zip(root, zip_path, "ECHO-离线组件合集-test")
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
        self.assertTrue(names)
        for n in names:
            self.assertTrue(n.startswith("ECHO-离线组件合集-test/"), n)
        self.assertIn("ECHO-离线组件合集-test/runtime-core/python.exe", names)

    def test_size_helper_counts_nested_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a", "b"))
            with open(os.path.join(tmp, "a", "b", "f.bin"), "wb") as fh:
                fh.write(b"0" * 2048)
            self.assertAlmostEqual(packer._size_mb(tmp), 2048 / (1 << 20), places=4)

    def test_uv_python_discovery_is_harmless_when_missing(self):
        """找不到就返回空串（出包时会报"找不到 CPython"），不许抛。"""
        got = packer.find_uv_python()
        self.assertIsInstance(got, str)


if __name__ == "__main__":
    unittest.main()
