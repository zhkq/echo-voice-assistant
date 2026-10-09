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
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
import zipfile
from pathlib import Path

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


# --------------------------------------------------------------- bundle 方言（B）
# 契约在 `scripts/install-all.ps1` 的三个消费点上（`<KitRoot>\bundle` 自动探测、
# `bundle\wheels` 的 -Offline 硬检查、`bundle\models\*` 与 `bundle\runtime\<名字>`）。
# 这一组用例**不联网**：wheels/模型/嵌入包那三件全部打桩，钉的是"摆出来的布局对不对"
# 与"缺东西时该不该响亮失败"。

#: 假 wheelhouse 里**贴近真名**的那几份（可读性：报错里看到的是真包名）。
_FAKE_WHEELS_NAMED = ("fastapi-0.115.0-py3-none-any.whl",
                      "uvicorn-0.30.0-py3-none-any.whl",
                      "sherpa_onnx-1.13.8-cp311-cp311-win_amd64.whl",
                      "modelscope-1.20.0-py3-none-any.whl",
                      "soundfile-0.12.1-py3-none-any.whl",
                      "pip-24.0-py3-none-any.whl",
                      "setuptools-70.0.0-py3-none-any.whl",
                      "wheel-0.43.0-py3-none-any.whl",
                      "packaging-24.0-py3-none-any.whl")


def _fake_wheels():
    """夹具的 wheel 清单 = 上面那份真名清单 **+ `BUNDLE_REQUIRED_WHEELS` 里还没覆盖的**。

    为什么不再手抄（2026-10-09）：白名单补 `pypinyin` 之后这份手抄清单没跟上，
    `verify_bundle()` 于是报"bundle\\wheels 里缺 pypinyin"，**6 条用例一起红** ——
    而红的不是出包逻辑，是夹具过期（典型"红的地方不是坏的地方"）。
    按契约补齐之后，白名单再加必需包，这里自动跟上。
    """
    have = [n.replace("_", "-").lower() for n in _FAKE_WHEELS_NAMED]
    out = list(_FAKE_WHEELS_NAMED)
    for need in packer.BUNDLE_REQUIRED_WHEELS:
        if need.replace("_", "-").lower() not in have:
            out.append("%s-1.0.0-py3-none-any.whl" % need)
    return tuple(out)


_FAKE_WHEELS = _fake_wheels()


class _BundleCase(unittest.TestCase):
    """把 ROOT 换成一个假的仓库树（`models/` 里有流沙模型），并给三件载荷打桩。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echobundle-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        model = os.path.join(self.repo, "models", "sherpa-onnx-streaming")
        os.makedirs(model)
        for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
            with open(os.path.join(model, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        # 2026-10-08：默认档现在**还要** `sensevoice-onnx`（指令转写的默认引擎），
        # 假仓库里得有它，否则出包会在"组件要的模型不在仓库里"处响亮失败 ——
        # 那条失败本身是对的（正是它拦住了"包里没模型就发出去"），所以这里补料而不是放松判据。
        sv = os.path.join(self.repo, "models", "sensevoice-onnx")
        os.makedirs(sv)
        for name in ("model.int8.onnx", "tokens.txt", "silero_vad.onnx"):
            with open(os.path.join(sv, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        self.patches = [
            mock.patch.object(packer, "ROOT", self.repo),
            mock.patch.object(packer, "pick_python", lambda override: ["fake-python"]),
            mock.patch.object(packer, "download_wheels", self._fake_wheels),
            mock.patch.object(packer, "copy_model", self._fake_copy_model),
            mock.patch.object(packer, "fetch", self._fake_fetch),
            mock.patch.object(packer, "offline_resolve_check", lambda py, d: (True, "ok")),
            # 出包的进度打印在用例里只是噪声（断言看的是产物）
            mock.patch.object(packer, "log", lambda msg: None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.wheel_specs = []

    def _fake_wheels(self, py, specs, dest, cache):
        self.wheel_specs = list(specs)
        os.makedirs(dest, exist_ok=True)
        out = []
        for name in _FAKE_WHEELS:
            path = os.path.join(dest, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("w")
            out.append(Path(path))
        return out

    def _fake_copy_model(self, src, dest):
        shutil.copytree(src, dest)

    def _fake_fetch(self, url, dest):
        os.makedirs(os.path.dirname(str(dest)), exist_ok=True)
        with open(str(dest), "w", encoding="utf-8") as fh:
            fh.write(url)

    @property
    def kit(self):
        return os.path.join(self.tmp, "ECHO-kit-20260930-1234")


class BundleLayoutTests(_BundleCase):
    def test_the_layout_is_exactly_what_install_all_consumes(self):
        out = os.path.join(self.tmp, "out")
        code = packer.main(["--bundle", "--out", out, "--stamp", "test"])
        self.assertEqual(code, 0)
        b = os.path.join(out, "ECHO-bundle-test", "bundle")
        self.assertTrue(os.path.isdir(os.path.join(b, "wheels")))
        self.assertTrue(os.path.isfile(
            os.path.join(b, "models", "sherpa-onnx-streaming", "tokens.txt")))
        self.assertTrue(os.path.isfile(os.path.join(b, "runtime", packer.EMBED_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(b, "runtime", "get-pip.py")))
        self.assertTrue(os.path.isfile(os.path.join(b, "BUNDLE-INFO.txt")))
        # 唤醒词：本机没有 → 跳过而不是失败
        self.assertFalse(os.path.exists(os.path.join(b, "models", "wakeword")))

    def test_the_zip_carries_a_top_level_folder(self):
        out = os.path.join(self.tmp, "out")
        packer.main(["--bundle", "--out", out, "--stamp", "test"])
        zip_path = os.path.join(out, "ECHO-bundle-test.zip")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        self.assertTrue(names)
        for n in names:
            self.assertTrue(n.startswith("ECHO-bundle-test/"), n)
        self.assertIn("ECHO-bundle-test/bundle/runtime/get-pip.py", names)

    def test_into_writes_straight_into_the_kit_and_skips_the_zip(self):
        """`--into <kit>`：直接进 kit 根（同事那个 .bat 就是在 kit 根发现 bundle\\ 的）。"""
        os.makedirs(self.kit)
        code = packer.main(["--bundle", "--into", self.kit, "--stamp", "test"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.kit, "bundle", "wheels")))
        self.assertFalse(os.path.exists(os.path.join(self.kit, "bundle", "bundle")))
        self.assertFalse(os.path.exists(self.kit + ".zip"))

    def test_into_a_missing_dir_is_refused(self):
        with self.assertRaises(packer.PackError) as ctx:
            packer.main(["--bundle", "--into", os.path.join(self.tmp, "nope")])
        self.assertIn("不存在", str(ctx.exception))

    def test_a_second_run_rewrites_the_bundle(self):
        """重出时**只清 bundle\\**（kit 里别的东西不许动）。"""
        os.makedirs(self.kit)
        with open(os.path.join(self.kit, "先读我.md"), "w", encoding="utf-8") as fh:
            fh.write("readme")
        packer.main(["--bundle", "--into", self.kit])
        stale = os.path.join(self.kit, "bundle", "wheels", "stale.whl")
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("old")
        packer.main(["--bundle", "--into", self.kit])
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isfile(os.path.join(self.kit, "先读我.md")))

    def test_wheels_come_from_requirements_core_and_the_engine_map(self):
        """wheel 清单不是手抄的：requirements-core 的行 + 技能脚本里的 ENGINE_MAP。"""
        packer.main(["--bundle", "--out", os.path.join(self.tmp, "out")])
        self.assertIn("sherpa-onnx>=1.10", self.wheel_specs)
        self.assertTrue(any(s.startswith("modelscope") for s in self.wheel_specs), self.wheel_specs)
        for bootstrap in ("pip", "setuptools", "wheel", "packaging"):
            self.assertIn(bootstrap, self.wheel_specs, "离线装 pip 要这几个：%s" % bootstrap)


class BundleGuardTests(_BundleCase):
    def test_a_missing_sherpa_model_is_a_loud_failure(self):
        shutil.rmtree(os.path.join(self.repo, "models", "sherpa-onnx-streaming"))
        with self.assertRaises(packer.PackError) as ctx:
            packer.main(["--bundle", "--out", os.path.join(self.tmp, "out")])
        self.assertIn("sherpa-onnx-streaming", str(ctx.exception))

    def test_a_thin_wheelhouse_is_caught_before_shipping(self):
        """wheelhouse 缺包 → 自检就说清楚（不能留到同事那边"装到一半失败"）。"""
        b = os.path.join(self.tmp, "b")
        os.makedirs(os.path.join(b, "wheels"))
        with open(os.path.join(b, "wheels", "fastapi-0.1-py3-none-any.whl"), "w") as fh:
            fh.write("x")
        problems = packer.verify_bundle(b)
        self.assertTrue(any("sherpa-onnx" in p for p in problems), problems)
        self.assertTrue(any("pip" in p for p in problems), problems)
        self.assertTrue(any("sherpa-onnx-streaming" in p for p in problems), problems)

    def test_a_failing_offline_resolve_is_a_failure(self):
        with mock.patch.object(packer, "offline_resolve_check",
                               lambda py, d: (False, "ERROR: No matching distribution found")):
            with self.assertRaises(packer.PackError) as ctx:
                packer.main(["--bundle", "--out", os.path.join(self.tmp, "out")])
        self.assertIn("No matching distribution", str(ctx.exception))

    def test_unknown_bundle_components_are_refused(self):
        with self.assertRaises(packer.PackError) as ctx:
            packer.main(["--bundle", "--out", os.path.join(self.tmp, "out"),
                         "--components", "runtime-core,nope-xyz"])
        self.assertIn("nope-xyz", str(ctx.exception))

    def test_no_models_and_no_runtime_are_honoured(self):
        out = os.path.join(self.tmp, "out")
        code = packer.main(["--bundle", "--out", out, "--stamp", "test",
                            "--no-models", "--no-runtime"])
        self.assertEqual(code, 0)
        b = os.path.join(out, "ECHO-bundle-test", "bundle")
        self.assertTrue(os.path.isdir(os.path.join(b, "wheels")))
        self.assertFalse(os.path.exists(os.path.join(b, "runtime")))
        self.assertFalse(os.path.exists(os.path.join(b, "models")))


class BundleContractWithInstallAll(unittest.TestCase):
    """**名字是契约**：`install-all.ps1` 是按名字找这几个东西的，改名就静默装不上。"""

    def setUp(self):
        self.text = Path(ROOT, "scripts", "install-all.ps1").read_text(encoding="utf-8")

    def test_the_embed_zip_name_matches_install_all(self):
        self.assertIn(packer.EMBED_NAME, self.text,
                      "bundle 里的嵌入包名字与 install-all.ps1 找的不一样")

    def test_install_all_reads_wheels_models_and_getpip_from_the_bundle(self):
        self.assertIn("$script:Bundle 'wheels'", self.text)
        self.assertIn("$script:Bundle 'models", self.text)
        self.assertIn("'runtime\\get-pip.py'", self.text)

    def test_the_kit_root_bundle_is_auto_detected(self):
        """`<KitRoot>\\bundle` 自动探测（第 183-186 行那条）——这是 .bat 能自动 -Offline 的地基。"""
        self.assertIn("Join-Path $script:KitRoot 'bundle'", self.text)

    def test_offline_hard_checks_the_wheels_dir(self):
        self.assertIn("离线载荷里没有 wheels", self.text)


if __name__ == "__main__":
    unittest.main()
