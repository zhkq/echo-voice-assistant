# -*- coding: utf-8 -*-
"""tests/test_delivery_standard.py — 交付包的标准：**客户端包必须自带载荷**（2026-10-05 固化）。

用户原话："把如果生成交付包的方案固化下来，未来新版本都按照这个标准来"。

背景（为什么要有这道闸）：2026-10-05 下午那层交付里，kit 从 285 MB 掉成了 **7 MB**
（没带 `bundle/`），于是同事装的时候变成**全量联网下载**（运行环境 + 依赖 + 模型）——
而当时**没有任何东西拦着**：`build_delivery.py` 用一张假的 0 MB kit 都能组出交付层。

标准：
  ① kit 里必须有 `<顶层>/bundle/wheels/` 与 `<顶层>/bundle/models/sherpa-onnx-streaming/`；
  ② 没有就**拒绝出层**（除非显式 `--allow-no-bundle`，只给排障）；
  ③ `--scenario full-local` 时，后端离线包还必须**自带权重**（否则那不叫"零下载"）。
"""
import io
import os
import sys
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import build_delivery as bd   # noqa: E402


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _fake_kit(tmp, *, with_bundle: bool, with_models: bool = True) -> str:
    path = os.path.join(tmp, "ECHO-kit-20260101-0000.zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ECHO-kit-20260101-0000/echo-core/scripts/install-all.ps1", "# x")
        z.writestr("ECHO-kit-20260101-0000/装我.cmd", "@echo off")
        if with_bundle:
            z.writestr("ECHO-kit-20260101-0000/bundle/wheels/sherpa_onnx-1.0.whl", "x")
            if with_models:
                z.writestr("ECHO-kit-20260101-0000/bundle/models/"
                           "sherpa-onnx-streaming/encoder.onnx", "x")
    return path


def _fake_backend(tmp, *, with_weights: bool) -> str:
    path = os.path.join(tmp, "ECHO-backend-portable-x.zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ECHO-backend-portable-x/server/requirements.txt", "fastapi")
        if with_weights:
            z.writestr("ECHO-backend-portable-x/models/Qwen--Qwen3-ASR-0.6B/model.bin", "x")
    return path


class BundleGuardTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="echo-deliv-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_bare_kit_is_called_out(self):
        """裸 kit：两条载荷都该报缺 —— 这正是那天的回退形状。"""
        missing = bd.bundle_problems(bd.Path(_fake_kit(self.tmp, with_bundle=False)))
        self.assertIn("bundle/wheels", missing)
        self.assertIn("bundle/models/sherpa-onnx-streaming", missing)

    def test_wheels_without_the_model_is_still_incomplete(self):
        """只带了 wheel 没带模型 → 还是不合格（语音指令那条路会联网下模型）。"""
        missing = bd.bundle_problems(bd.Path(_fake_kit(self.tmp, with_bundle=True,
                                                       with_models=False)))
        self.assertEqual(["bundle/models/sherpa-onnx-streaming"], missing)

    def test_a_proper_bundle_kit_passes(self):
        self.assertEqual([], bd.bundle_problems(bd.Path(_fake_kit(self.tmp, with_bundle=True))))

    def test_unreadable_zip_counts_as_missing(self):
        bad = os.path.join(self.tmp, "ECHO-kit-broken.zip")
        io.open(bad, "w", encoding="utf-8").write("not a zip")
        self.assertEqual(len(bd.BUNDLE_REQUIRED_ENTRIES), len(bd.bundle_problems(bd.Path(bad))))


class WeightsGuardTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="echo-deliv-w-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_offline_pack_without_weights(self):
        """现在那份 3.1 GB 离线包就是这一档：只有运行时+依赖，模型要联网下。"""
        self.assertFalse(bd.offline_pack_has_weights(
            bd.Path(_fake_backend(self.tmp, with_weights=False))))

    def test_offline_pack_with_weights(self):
        self.assertTrue(bd.offline_pack_has_weights(
            bd.Path(_fake_backend(self.tmp, with_weights=True))))


class ApkInDeliveryTests(unittest.TestCase):
    """手机触点 APK 要真的进交付层（2026-10-15 用户："apk 也放到交付包里"）。

    手机那条路（`android/`，包名 `com.echo.contact`）是交付层里唯一的"非 PC"触点：
    资料夹给它一份 APK，同事拷到手机点了就装 —— 少了它，交付里就只有主机那一半。
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="echo-deliv-apk-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dist = os.path.join(self.tmp, "dist")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.dist)
        os.makedirs(self.out)

    def _apk(self, name="app-debug.apk"):
        p = os.path.join(self.dist, name)
        with io.open(p, "wb") as fh:
            fh.write(b"PK\x03\x04 fake apk")
        return p

    def test_it_defaults_to_the_newest_versioned_apk_in_dist(self):
        import time
        self._apk("ECHO-android-old.apk")
        time.sleep(0.02)
        self._apk("ECHO-android-new.apk")
        self.assertEqual(bd.default_apk(bd.Path(self.dist)).name, "ECHO-android-new.apk")

    def test_the_delivered_name_says_it_is_the_phone_package(self):
        self.assertEqual(bd.apk_dest_name(bd.Path("app-debug.apk")),
                         "ECHO-android-app-debug.apk")
        self.assertEqual(bd.apk_dest_name(bd.Path("ECHO-android-x.apk")),
                         "ECHO-android-x.apk")

    def test_a_layer_actually_carries_the_apk_and_says_so(self):
        kit = bd.Path(_fake_kit(self.dist, with_bundle=True))
        dest, out_zip = bd.assemble("20260101-0000", bd.Path(self.dist), kit_zip=kit,
                                    apk_zip=bd.Path(self._apk()), make_the_zip=False,
                                    out_dir=bd.Path(self.out))
        self.assertIsNone(out_zip)
        self.assertTrue((dest / "ECHO-android-app-debug.apk").is_file())
        self.assertIn("ECHO-android-app-debug.apk",
                      (dest / "清单.txt").read_text(encoding="utf-8"))

    def test_no_apk_keeps_it_out(self):
        kit = bd.Path(_fake_kit(self.dist, with_bundle=True))
        dest, _ = bd.assemble("20260101-0001", bd.Path(self.dist), kit_zip=kit,
                              no_apk=True, make_the_zip=False, out_dir=bd.Path(self.out))
        self.assertEqual([p.name for p in dest.iterdir() if p.suffix == ".apk"], [])


class WordingTests(unittest.TestCase):
    """标准要**写得出来**：场景名与配方都在，报错里要能直接照着做。"""

    def test_scenarios(self):
        self.assertEqual(("client", "full-local"), bd.SCENARIOS)

    def test_the_howto_has_the_two_commands(self):
        self.assertIn("build_offline_pack.py --bundle", bd.STANDARD_HOWTO)
        self.assertIn("build_kit.py", bd.STANDARD_HOWTO)
        self.assertIn("--bundle-from", bd.STANDARD_HOWTO)

    def test_assemble_enforces_it(self):
        src = _read("scripts", "build_delivery.py")
        self.assertIn("bundle_problems(kit)", src, "assemble 里要真的判载荷")
        self.assertIn("allow_no_bundle", src)
        self.assertIn("offline_pack_has_weights(offline)", src, "full-local 要判权重")

    def test_cli_surface(self):
        src = _read("scripts", "build_delivery.py")
        for flag in ("--scenario", "--allow-no-bundle", "--allow-downloads",
                     "--apk", "--no-apk"):
            with self.subTest(flag=flag):
                self.assertIn(flag, src)


if __name__ == "__main__":
    unittest.main()
