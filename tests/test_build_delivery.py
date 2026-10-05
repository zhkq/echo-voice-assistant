# -*- coding: utf-8 -*-
"""交付资料夹（`scripts/build_delivery.py`）：**一个脚本 + 1~3 个 zip**。

用户 2026-10-01 定的交付形态：把这几样摆在一个资料夹里交出去，同事**双击 `装我.cmd`**
就完成解压与安装（不用先右键解压 kit zip，也不用自己去翻后端包）。

这一组钉的是"发出去之前必须是对的"那几条：
  ① 脚本 ASCII + CRLF（cmd.exe 按码页读，中文与 LF-only 都会静默走错分支）；
  ② kit zip 里**真的有** `装我.cmd` 与 `install-all.ps1`（脚本才有东西可解、可调）；
  ③ 后端 zip 的名字**命中安装期认包的模式** —— 这是"出包侧"与"面板侧"唯一没有别的东西
     连接的地方，也是本次新增离线包时最容易漏的一环（名字对不上 → 面板永远找不到它）；
  ④ 缺件要**响亮失败且不留半个资料夹**（半份交付比没有更贵：同事会照着它装）。
"""
import fnmatch
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_delivery, build_kit                        # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-delivery-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dist = self.tmp / "dist"
        self.dist.mkdir()

    def write_kit_zip(self, name="ECHO-kit-20261001-0026.zip", *, launcher=True,
                      installer=True, bundle=True):
        p = self.dist / name
        top = name[:-4]
        with zipfile.ZipFile(p, "w") as zf:
            if installer:
                zf.writestr("%s/echo-core/scripts/install-all.ps1" % top, "# install\n")
            if launcher:
                zf.writestr("%s/%s" % (top, build_kit.KIT_CMD), "@echo off\n")
            # 交付标准（2026-10-05）：客户端包**必须自带载荷**，所以夹具默认就带上 ——
            # 本文件这些用例测的是"组层"的机制，不该被标准判红；"裸包必须被拒"由
            # tests/test_delivery_standard.py 专门钉（那边用 bundle=False 造裸包）。
            if bundle:
                zf.writestr("%s/bundle/wheels/sherpa_onnx-1.0.whl" % top, "x")
                zf.writestr("%s/bundle/models/sherpa-onnx-streaming/encoder.onnx" % top, "x")
        return p

    def write_backend_zip(self, name, *, deps=False):
        p = self.dist / name
        top = name[:-4]
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("%s/server/requirements.txt" % top, "fastapi>=0.115\n")
            zf.writestr("%s/runtime/python.exe" % top, "")
            if deps:
                zf.writestr("%s/runtime/Lib/site-packages/fastapi/__init__.py" % top, "")
        return p

    def assemble(self, **kw):
        kw.setdefault("stamp", "20261001-1200")
        return build_delivery.assemble(kw.pop("stamp"), self.dist, **kw)


class AssemblyTests(_Case):
    def test_it_gathers_the_launcher_the_kit_and_the_backend_zips(self):
        kit = self.write_kit_zip()
        thin = self.write_backend_zip("ECHO-backend-portable-20261001.zip")
        offline = self.write_backend_zip("ECHO-backend-offline-cu126-20261001.zip", deps=True)
        dest, out_zip = self.assemble()
        names = sorted(p.name for p in dest.iterdir())
        self.assertIn(build_kit.KIT_CMD, names)
        self.assertIn(kit.name, names)
        self.assertIn(thin.name, names)
        self.assertIn(offline.name, names)
        self.assertIn("清单.txt", names)
        self.assertIsNotNone(out_zip, "默认要套一层 zip（--no-zip 才跳过）")
        self.assertTrue(out_zip.is_file())

    def test_the_launcher_is_ascii_and_crlf(self):
        self.write_kit_zip()
        dest, _ = self.assemble(make_the_zip=False)
        raw = (dest / build_kit.KIT_CMD).read_bytes()
        self.assertFalse([b for b in raw if b > 127], "cmd 必须纯 ASCII")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), "必须统一成 CRLF")

    def test_the_outer_zip_carries_a_top_level_folder(self):
        """少了顶层前缀，解出来是一堆散文件（2026-09-22 真踩到，且**不报错**）。"""
        self.write_kit_zip()
        dest, out_zip = self.assemble()
        with zipfile.ZipFile(out_zip) as zf:
            names = zf.namelist()
        self.assertTrue(names, "空 zip？")
        self.assertTrue(all(n.startswith(dest.name + "/") for n in names), names[:3])

    def test_no_backend_zip_is_still_a_valid_delivery(self):
        """后端 zip 是可选的（以后在面板里配也行）—— 不该因此拒绝出包，但要说清楚。"""
        self.write_kit_zip()
        dest, _ = self.assemble(make_the_zip=False)
        text = (dest / "清单.txt").read_text(encoding="utf-8")
        self.assertIn("起本机后端", text)

    def test_the_manifest_says_what_each_zip_is_for(self):
        self.write_kit_zip()
        self.write_backend_zip("ECHO-backend-portable-20261001.zip")
        self.write_backend_zip("ECHO-backend-offline-cu126-20261001.zip", deps=True)
        dest, _ = self.assemble(make_the_zip=False)
        text = (dest / "清单.txt").read_text(encoding="utf-8")
        self.assertIn("零下载", text)
        self.assertIn("配对串", text)
        self.assertIn("本机自己跑", text)

    def test_a_backend_zip_deeper_under_dist_is_found(self):
        """后端包常带自己的子目录（`dist/delivery-clean/ECHO-backend-portable-*.zip`）。"""
        self.write_kit_zip()
        sub = self.dist / "delivery-clean"
        sub.mkdir()
        with zipfile.ZipFile(sub / "ECHO-backend-portable-clean-room.zip", "w") as zf:
            zf.writestr("ECHO-backend-portable-clean-room/server/requirements.txt", "fastapi\n")
        dest, _ = self.assemble(make_the_zip=False)
        self.assertTrue((dest / "ECHO-backend-portable-clean-room.zip").is_file())


class LoudFailureTests(_Case):
    def test_a_missing_kit_zip_says_what_to_run(self):
        with self.assertRaises(build_delivery.DeliveryError) as cm:
            self.assemble(make_the_zip=False)
        self.assertIn("build_kit.py", str(cm.exception))

    def test_a_kit_zip_without_the_launcher_inside_is_refused(self):
        self.write_kit_zip(launcher=False)
        with self.assertRaises(build_delivery.DeliveryError) as cm:
            self.assemble(make_the_zip=False)
        self.assertIn(build_kit.KIT_CMD, str(cm.exception))

    def test_a_kit_zip_without_the_installer_inside_is_refused(self):
        self.write_kit_zip(installer=False)
        with self.assertRaises(build_delivery.DeliveryError) as cm:
            self.assemble(make_the_zip=False)
        self.assertIn("install-all.ps1", str(cm.exception))

    def test_a_backend_zip_the_installer_cannot_recognize_is_refused(self):
        """名字认不出来 = 面板那侧永远找不到它 —— 在**发出去之前**炸，别等同事点。"""
        self.write_kit_zip()
        bad = self.write_backend_zip("后端-给同事-0713.zip")   # 一条模式都不命中
        with self.assertRaises(build_delivery.DeliveryError) as cm:
            self.assemble(make_the_zip=False, backend_zip=bad)
        self.assertIn("认不出来", str(cm.exception))

    def test_a_renamed_backend_zip_sitting_in_dist_is_caught_too(self):
        """**没显式指名**、就躺在 `dist/` 里但名字认不出来 → 也要拦住。

        为什么要这一条：那种包会被**静默漏掉**（交付里没有它、同事装完才发现要下 3 GB）。
        交付汇总目录里那份被改名成 `3-本机GPU后端包-20MB.zip` 的包就是这么来的 ——
        不过那个名字现在**认得出**（`PACKAGE_GLOBS` 里有 `*后端包*.zip`），所以这里用一个
        真认不出的名字。
        """
        self.write_kit_zip()
        self.write_backend_zip("后端-给同事-0713.zip")
        with self.assertRaises(build_delivery.DeliveryError) as cm:
            self.assemble(make_the_zip=False)
        self.assertIn("后端-给同事-0713.zip", str(cm.exception))
        self.assertIn("ECHO-backend-portable", str(cm.exception))

    def test_a_container_kit_is_not_mistaken_for_a_stray(self):
        """容器交付（`ECHO-backend-kit-*`）里也有 `server/`，但它**不是**这条路的东西。"""
        self.write_kit_zip()
        with zipfile.ZipFile(self.dist / "ECHO-backend-kit-cu126-20261001.zip", "w") as zf:
            zf.writestr("ECHO-backend-kit-cu126-20261001/server/requirements.txt", "fastapi\n")
            zf.writestr("ECHO-backend-kit-cu126-20261001/compose.yaml", "services: {}\n")
        dest, _ = self.assemble(make_the_zip=False)
        self.assertTrue((dest / build_kit.KIT_CMD).is_file())

    def test_a_failed_run_leaves_no_half_folder(self):
        self.write_kit_zip(launcher=False)
        stamp = "20261001-halfway"
        with self.assertRaises(build_delivery.DeliveryError):
            build_delivery.assemble(stamp, self.dist, make_the_zip=False)
        self.assertFalse((self.dist / ("%s-%s" % (build_delivery.PKG_PREFIX, stamp))).exists(),
                         "半份交付比没有更贵：同事会照着它装")


class ProducerMeetsConsumerTests(_Case):
    """**出包命名 ↔ 安装期认包**：这两边就是靠 glob 焊在一起的。"""

    def test_the_globs_come_from_the_app_source(self):
        thin, offline = build_delivery.consumer_globs()
        self.assertTrue(thin and offline, "读不到 app/backend_fetch.py 里的两组 glob")
        self.assertTrue(fnmatch.fnmatch("ECHO-backend-portable-cu126-20261001-1200.zip", thin[0])
                        or any(fnmatch.fnmatch("ECHO-backend-portable-cu126-20261001-1200.zip", g)
                               for g in thin),
                        "推荐的薄包名要能被安装期认出来：%s" % thin)
        self.assertTrue(any(fnmatch.fnmatch("ECHO-backend-offline-cu126-20261001-1200.zip", g)
                            for g in offline),
                        "推荐的离线包名要能被安装期认出来：%s" % offline)

    def test_the_renamed_delivery_zip_still_matches(self):
        """交付汇总目录里那份被人工改名成 `3-本机GPU后端包-20MB.zip` —— 也要认。"""
        thin, _offline = build_delivery.consumer_globs()
        self.assertTrue(any(fnmatch.fnmatch("3-本机GPU后端包-20MB.zip", g) for g in thin),
                        thin)


class DeliveryFolderLayoutTests(_Case):
    """**交付目录 + 一层日期**（用户 2026-10-01 定的最终交付形态）。

    用户原话：*"客户端的包，后端薄包和安装脚本放到 delivery 目录，以后我就给用户这样发安装包"*，
    随后又补：*"为了方便区分，你可以再 delivery 目录下加一层交付日期编码，区分不同版本"*。
    于是落点是：``<交付目录>\\ECHO-delivery-<日期>-<时分>\\`` 里放着
    ``装我.cmd`` + 客户端包 + 后端薄包（+ 说明）。这一组钉三件：
      ① 那一层**带日期**、建在用户的交付目录下（不是 `dist/`）；
      ② 同一目录里**几版能并存**（各自的日期层互不干扰）；
      ③ 在用户的交付目录里干活**不动别人的东西**（那是他的目录，可能还放着别的包）。
    """

    def test_it_builds_a_dated_layer_under_the_delivery_folder(self):
        kit = self.write_kit_zip()
        thin = self.write_backend_zip("ECHO-backend-portable-20261001.zip")
        out = self.tmp / "ECHO-delivery"
        dest, out_zip = build_delivery.assemble("20261001-0900", self.dist, out_dir=out)
        self.assertEqual(dest.parent, out.resolve(), "要建在交付目录**下**")
        self.assertEqual(dest.name, "ECHO-delivery-20261001-0900", "那一层要带日期/时分")
        for name in (build_kit.KIT_CMD, kit.name, thin.name, "清单.txt"):
            self.assertTrue((dest / name).is_file(), name)
        self.assertIsNotNone(out_zip, "外层 zip 默认要给（方便整体传出去）")
        self.assertEqual(out_zip.parent, out.resolve(), "zip 与日期层并排放在交付目录里")
        self.assertFalse(os.path.isdir(self.dist / dest.name),
                         "给了 --out 就不该再往 dist/ 里塞一份")

    def test_two_versions_can_live_side_by_side(self):
        """**加日期层就是为了这个**：同一个交付目录里并存好几版，互不覆盖。"""
        self.write_kit_zip()
        out = self.tmp / "ECHO-delivery"
        a, _ = build_delivery.assemble("20261001-0800", self.dist, out_dir=out, make_the_zip=False)
        b, _ = build_delivery.assemble("20261001-0900", self.dist, out_dir=out, make_the_zip=False)
        self.assertNotEqual(a, b)
        self.assertTrue((a / build_kit.KIT_CMD).is_file())
        self.assertTrue((b / build_kit.KIT_CMD).is_file())
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["ECHO-delivery-20261001-0800", "ECHO-delivery-20261001-0900"])

    def test_it_does_not_touch_unrelated_files_in_the_delivery_folder(self):
        self.write_kit_zip()
        out = self.tmp / "ECHO-delivery"
        out.mkdir()
        keep = out / "同事给我的另一个包.zip"
        keep.write_bytes(b"x")
        dest, _ = build_delivery.assemble("20261001-0900", self.dist, out_dir=out,
                                          make_the_zip=False)
        self.assertTrue(keep.is_file(), "用户的交付目录里别的东西一个都不许动")
        self.assertTrue((dest / build_kit.KIT_CMD).is_file())

    def test_a_failed_run_leaves_no_half_layer(self):
        self.write_kit_zip(launcher=False)                 # 触发自检失败
        out = self.tmp / "ECHO-delivery"
        out.mkdir()
        keep = out / "别删我.txt"
        keep.write_text("mine", encoding="utf-8")
        with self.assertRaises(build_delivery.DeliveryError):
            build_delivery.assemble("20261001-0900", self.dist, out_dir=out)
        self.assertTrue(keep.is_file(), "失败也不许动目录里的别的东西")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["别删我.txt"],
                         "失败时那一层要收干净，不留半个包")

    def test_the_launcher_next_to_the_zips_is_what_makes_it_one_click(self):
        """这一层要靠**脚本自己在旁边解包**才有意义（否则同事还得先手动解压）。"""
        self.write_kit_zip()
        out = self.tmp / "ECHO-delivery"
        dest, _ = build_delivery.assemble("20261001-0900", self.dist, out_dir=out,
                                          make_the_zip=False)
        text = (dest / build_kit.KIT_CMD).read_text(encoding="ascii")
        self.assertIn("ECHO-kit-*.zip", text, "脚本要自己找客户端包")
        self.assertIn("tar -xf", text, "自己解开（不用同事右键解压）")
        self.assertIn("install-all.ps1", text, "解完之后接着走安装")


if __name__ == "__main__":
    unittest.main()
