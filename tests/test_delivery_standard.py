# -*- coding: utf-8 -*-
"""tests/test_delivery_standard.py — 交付包的标准：**客户端包必须自带载荷**（2026-10-05 固化）。

用户原话："把如果生成交付包的方案固化下来，未来新版本都按照这个标准来"。

背景（为什么要有这道闸）：2026-10-05 下午那层交付里，kit 从 285 MB 掉成了 **7 MB**
（没带 `bundle/`），于是同事装的时候变成**全量联网下载**（运行环境 + 依赖 + 模型）——
而当时**没有任何东西拦着**：`build_delivery.py` 用一张假的 0 MB kit 都能组出交付层。

标准：
  ① kit 里必须有 `<顶层>/bundle/wheels/`（2026-10-31 起客户端无本地模型，不要求 models/）；
  ② 没有就**拒绝出层**（除非显式 `--allow-no-bundle`，只给排障）；
  ③ `--scenario full-local` 时，后端离线包还必须**自带权重**（否则那不叫"零下载"）；
  ④ `--no-offline`（2026-10-16 加）：**这一层**不带后端离线包 —— 纯客户端那档
     （先配对别人的 GPU）本来是 ≈58 MB，而 `dist/` 里躺着 3.1 GB 的离线包时默认会一起带上，
     整层变成 ≈3.4 GB，只能靠"手工把离线包移出 dist"绕过（手工活，必漂）。
     开关**只管这一层放不放文件**（默认行为一字不变），`清单.txt` 要如实写"这一层不含离线包"；
     它与 `--scenario full-local`（语义 = 零下载）**矛盾 → 明确报错**，不许静默忽略其中一个。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
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


def _fake_offline(tmp, *, deps: bool = True, with_weights: bool = True) -> str:
    """一张**小小的假离线包**（照本文件既有夹具的手法：只写几条中央目录就够）。

    为什么绝不用真的那份 3.1 GB：这里要判的只是"**进没进层**"与"清单自不自洽"，
    而真离线包是固定产物（不是本用例造出来的）—— 拿它当夹具既慢又会随 `dist/` 变动而飘。
    内容要像真的：`<顶层>/server/requirements.txt`（`looks_like_backend_package` 认它）、
    `runtime/Lib/site-packages/fastapi/…`（安装期认"依赖已装好"）、
    `models/…`（`full-local` 要求"自带权重"）。
    """
    path = os.path.join(tmp, "ECHO-backend-offline-cu128-20260101-0000.zip")
    with zipfile.ZipFile(path, "w") as z:
        top = "ECHO-backend-offline-cu128-20260101-0000"
        z.writestr("%s/server/requirements.txt" % top, "fastapi>=0.115\n")
        z.writestr("%s/runtime/python.exe" % top, "")
        if deps:
            z.writestr("%s/runtime/Lib/site-packages/fastapi/__init__.py" % top, "")
        if with_weights:
            z.writestr("%s/models/Qwen--Qwen3-ASR-0.6B/model.bin" % top, "x")
    return path


def _fake_thin(tmp) -> str:
    """后端薄包（20 MB 那一档）—— 一起造出来，好让"纯客户端层"看着跟真的一样。"""
    path = os.path.join(tmp, "ECHO-backend-portable-20260101-0000.zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ECHO-backend-portable-20260101-0000/server/requirements.txt", "fastapi\n")
        z.writestr("ECHO-backend-portable-20260101-0000/runtime/python.exe", "")
    return path


class BundleGuardTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="echo-deliv-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_bare_kit_is_called_out(self):
        """裸 kit：缺 wheels 就该报缺 —— 这正是那天的回退形状。"""
        missing = bd.bundle_problems(bd.Path(_fake_kit(self.tmp, with_bundle=False)))
        self.assertEqual(["bundle/wheels"], missing)

    def test_wheels_without_models_is_enough_now(self):
        """2026-10-31：客户端不装本地模型 —— 只有 wheels 也算合格。"""
        missing = bd.bundle_problems(bd.Path(_fake_kit(self.tmp, with_bundle=True,
                                                       with_models=False)))
        self.assertEqual([], missing)

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


class NoOfflineSwitchTests(unittest.TestCase):
    """`--no-offline`：**纯客户端那一层**（≈58 MB）不许被 3.1 GB 的离线包拖成 ≈3.4 GB。

    背景（AGENTS.md「交付层的标准」里写明的已知缺口）：`dist/` 里有离线包时，`--scenario client`
    **总会**把它一起带上，要出纯客户端层只能手工先把离线包移出 `dist` —— 手工活，必漂。
    这个开关把"不带离线包"变成**可声明的选择**，代价是清单必须**如实**反映它
    （否则就是"清单说有、目录里没有"的假话，同事照它装会扑空）。

    三条判据（都走**真的 CLI**（`bd.main`），不是只调 `assemble` —— 参数有没有接上也算在这里）：
      ① 给开关 → 层里没有 `ECHO-backend-offline-*.zip`，且 `清单.txt` 写明这一层不含它；
      ② 不给   → **照旧带上**（默认行为一字不变的回归）；
      ③ 开关 + `full-local` → **明确报错**（"零下载"与"不带离线包"不可能同时成立）。

    夹具一律用**小小的假包**（`_fake_offline`，几 KB），绝不碰 `dist/` 里那份真包。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-deliv-noff-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dist = os.path.join(self.tmp, "dist")          # 交付层默认建在 dist 下
        self.out = os.path.join(self.tmp, "out")             # 给 --out 时建在用户的交付目录下
        os.makedirs(self.dist)
        os.makedirs(self.out)
        self.kit = _fake_kit(self.dist, with_bundle=True)
        self.thin = _fake_thin(self.dist)
        self.offline = _fake_offline(self.dist)              # ← 默认会被一起带上的那个 3.1 GB

    def _run(self, *extra) -> tuple[int, str]:
        """跑真的 CLI（`--no-zip --no-apk` 让产物只是那一层资料夹），返回 `(退出码, 输出)`。"""
        buf = io.StringIO()
        argv = ["--dist", self.dist, "--kit", self.kit, "--no-zip", "--no-apk"] + list(extra)
        with contextlib.redirect_stdout(buf):
            code = bd.main(argv)
        return code, buf.getvalue()

    def _layer(self) -> str:
        """那一层交付资料夹（`--out` 指出去时在 out 下，否则在 dist 下）。"""
        for parent in (self.out, self.dist):
            for name in sorted(os.listdir(parent)):
                if name.startswith(bd.PKG_PREFIX):
                    p = os.path.join(parent, name)
                    if os.path.isdir(p):
                        return p
        self.fail("没找到交付那一层：%s / %s" % (self.out, self.dist))

    def _names(self) -> list[str]:
        return sorted(os.listdir(self._layer()))

    def _manifest(self) -> str:
        with io.open(os.path.join(self._layer(), "清单.txt"), encoding="utf-8") as fh:
            return fh.read()

    def _offline_lines(self) -> list[str]:
        """清单里**提到离线包**的那几行（用来判"说了什么"，而不是"有没有这个词"）。"""
        return [ln for ln in self._manifest().splitlines() if "离线包" in ln]

    # ---- ① 给开关：这一层里没有它，清单也如实说

    def test_no_offline_keeps_the_offline_zip_out_of_the_layer(self):
        code, out = self._run("--no-offline", "--out", self.out)
        self.assertEqual(bd.build_kit.EXIT_OK, code, out)
        names = self._names()
        self.assertIn(os.path.basename(self.thin), names, "薄包照旧要带（纯客户端那档用它）")
        self.assertEqual([n for n in names if n.startswith("ECHO-backend-offline-")], [],
                         "`--no-offline` 时这层里不该有离线包：%s" % names)
        self.assertIsNotNone(bd.newest_matching(bd.Path(self.dist), bd.BACKEND_OFFLINE_GLOBS),
                             "夹具自己得是对的：dist 里**确实**有一份离线包（否则这条用例判了个寂寞）")
        self.assertEqual([], bd.strays_in(bd.Path(self.dist), bd.consumer_globs()),
                         "dist 里那份离线包的名字必须认得出来（不然这条用例判的是别的东西）")

    def test_the_manifest_admits_it_does_not_carry_the_offline_pack(self):
        """清单与目录必须**自洽**：目录里没有，清单就得说出来 —— 且不许说成"有"。"""
        import re
        self._run("--no-offline", "--out", self.out)
        names = self._names()
        self.assertEqual([n for n in names if n.startswith("ECHO-backend-offline-")], [])
        text = self._manifest()
        packed = re.search(r"^(ECHO-backend-offline-\S+)\s", text, re.M)
        self.assertIsNone(packed,
                          "清单不许把离线包当成'带上了'（目录里没有它）：%r" % (packed and packed.group(0)))
        lines = self._offline_lines()
        self.assertTrue(any("不含后端离线包" in ln for ln in lines),
                        "清单要写明这一层不含离线包：%s" % lines)
        self.assertTrue(any("联网下依赖" in ln for ln in lines),
                        "要说清代价（本机跑后端要联网下依赖）：%s" % lines)
        self.assertNotIn("有离线包就零下载", lines,
                         "对同事说的用法提示也得跟着变，不能还许诺零下载")

    # ---- ② 不给开关：默认行为**一字不变**（回归）

    def test_without_the_switch_the_offline_zip_is_still_carried(self):
        code, out = self._run("--out", self.out)             # 注意：没有 --no-offline
        self.assertEqual(bd.build_kit.EXIT_OK, code, out)
        names = self._names()
        self.assertIn(os.path.basename(self.offline), names,
                      "不给 --no-offline 时默认照旧带上离线包（既有标准不变）：%s" % names)
        self.assertIn(os.path.basename(self.thin), names)
        self.assertTrue(any(ln.startswith(os.path.basename(self.offline))
                            and "零下载" in ln for ln in self._offline_lines()),
                        "照旧带上时清单还是要写'零下载'：%s" % self._offline_lines())

    # ---- ③ 与 full-local 矛盾：明确报错，不静默忽略

    def test_no_offline_with_full_local_is_a_loud_conflict(self):
        code, out = self._run("--no-offline", "--scenario", "full-local", "--out", self.out)
        self.assertEqual(bd.build_kit.EXIT_ERROR, code, out)
        self.assertIn("[x]", out, out)
        for phrase in ("互相矛盾", "full-local", "零下载", "--no-offline"):
            self.assertIn(phrase, out, "错误文案里要能看出'矛盾'在哪：%s" % out)
        self.assertEqual([], [n for n in os.listdir(self.out) if n.startswith(bd.PKG_PREFIX)],
                         "报错了就不许留半个交付层")

    def test_the_library_raises_the_same_conflict(self):
        """CLI 之外的调用方（脚本/别的工具）也必须撞上这堵墙，不是只写在 argparse 里。"""
        with self.assertRaises(bd.DeliveryError) as cm:
            bd.assemble("20260101-0000", bd.Path(self.dist), kit_zip=bd.Path(self.kit),
                        no_offline=True, scenario="full-local",
                        make_the_zip=False, out_dir=bd.Path(self.out))
        self.assertIn("互相矛盾", str(cm.exception))

    # ---- ④ 本来就没有离线包时不报错；帮助要把取舍写清

    def test_no_offline_without_any_offline_zip_is_not_an_error(self):
        """离线包根本不存在时，`--no-offline` 无惊无险（本来就没什么可省的）。"""
        os.remove(self.offline)
        code, out = self._run("--no-offline", "--out", self.out)
        self.assertEqual(bd.build_kit.EXIT_OK, code, out)
        self.assertIn(os.path.basename(self.thin), self._names())

    def test_only_the_offline_zip_is_dropped(self):
        """开关**只管离线包**：客户端 kit、薄包、清单、启动脚本一样都不能少。"""
        self._run("--no-offline", "--out", self.out)
        names = self._names()
        for keep in (os.path.basename(self.kit), os.path.basename(self.thin), "清单.txt"):
            self.assertIn(keep, names, "%s 不该被这个开关带走：%s" % (keep, names))

    def test_the_help_says_why_and_gives_the_sizes(self):
        """帮助里要写清取舍：为什么要有它 + 两个尺寸对照（照本脚本既有的中文'为什么'风格）。

        ⚠️ argparse 会**按终端宽度折行**，所以"3.4 GB"可能在帮助里被断成两行 ——
        判据先把空白折起来再比（不然判的是"这台机器的列宽"，不是"帮助里写没写"）。
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                bd.main(["--help"])
        text = " ".join(buf.getvalue().split())          # 折行 → 单空格
        self.assertEqual(0, cm.exception.code)
        for phrase in ("--no-offline", "3.4 GB", "58 MB", "零下载", "full-local"):
            self.assertIn(phrase, text, "帮助里缺 %s：%s" % (phrase, text))

    def test_cli_and_library_agree_on_the_switch_name(self):
        """CLI 面与库面必须同名（少接一根线 = 开关给了却没生效，且**不报错**）。"""
        src = _read("scripts", "build_delivery.py")
        self.assertIn("--no-offline", src)
        self.assertIn("no_offline=bool(args.no_offline)", src, "argparse 要真的传到 assemble")
        self.assertIn("no_offline=no_offline", src, "清单要拿到这个事实才写得出'不含离线包'")


if __name__ == "__main__":
    unittest.main()
