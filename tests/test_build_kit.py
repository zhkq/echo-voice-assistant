# -*- coding: utf-8 -*-
"""交付包脚本的契约：模板在仓库里、两边都走 main、推送时会检查是否过期。

背景（2026-09-22）
------------------
同事要测安装，dist/ 里的包比源码旧了 9 小时，13:30-15:00 的修复（含「标准版本地
永久安装」）根本没进包。根因是**组 kit 一直是手工活**：没有脚本、靠人记步骤，还要
从 dist 里翻上一代 kit 捡 `先读我.md`。这次把它固化成 `scripts/build_kit.py`，并让
`gh-push.ps1` 推送前跑 `--check`。

本文件钉住的是"固化"本身，防止将来又被改回手工/漂移：
  1. `先读我.md` 模板必须**在 git 里**（不是从 dist 捡 —— dist 不进 git，随时会被清空）；
  2. 两个平台都必须走 `-Profile main`（mac 曾经用 public：包里没有 components/，
     而 manifest.json 声明了它 —— 交付清单里的假话）；
  3. `--check` 的判据（哈希比对、kit 前缀区分、skill 一致性）真的有效；
  4. 推送流程里必须挂着这道检查。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = ROOT / "scripts"
DELIVERY = ROOT / "delivery"
SKILL = ROOT / ".dsh" / "skills" / "echo-install"


def _load_build_kit():
    """把 scripts/build_kit.py 当模块加载（它不在包路径里）。"""
    path = SCRIPTS / "build_kit.py"
    spec = importlib.util.spec_from_file_location("build_kit_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_kit = _load_build_kit()


class DeliveryTemplatesLiveInGit(unittest.TestCase):
    """`先读我.md` 模板必须在仓库里，不能靠从 dist 里捡。"""

    def test_templates_exist_and_are_not_empty(self):
        for name in ("kit-readme-win.md", "kit-readme-mac.md"):
            path = DELIVERY / name
            self.assertTrue(path.is_file(), f"缺少 {name} —— kit 组不出来")
            self.assertGreater(path.stat().st_size, 500, f"{name} 太小，像是空了")

    def test_templates_are_utf8_and_introduce_the_skill_flow(self):
        for name in ("kit-readme-win.md", "kit-readme-mac.md"):
            text = (DELIVERY / name).read_text(encoding="utf-8")
            self.assertIn("echo-install", text,
                          f"{name} 没提 echo-install —— 那不是给同事的那份说明")
            self.assertIn("交给你的 AI 助手", text,
                          f"{name} 少了「把文件夹交给助手」这条交互（两平台已统一）")

    def test_templates_are_not_inside_the_packed_tree(self):
        """delivery/ 不该被打进 ECHO/：它只是组装 kit 的输入。"""
        packed_dirs = {"app", "web", "mac", "scripts", "plugin", "docs",
                       "assets", "dsh-failover", ".dsh", "sidebar", "components"}
        self.assertNotIn(DELIVERY.name, packed_dirs,
                         "delivery/ 若进了打包白名单，模板会被塞进主包")


class BothPlatformsUseMainProfile(unittest.TestCase):
    """mac 曾经走 public：包里没有 components/，而 manifest 声明了它。"""

    def test_every_platform_is_built_with_profile_main(self):
        src = (SCRIPTS / "build_kit.py").read_text(encoding="utf-8")
        self.assertIn('"-Profile", "main"', src,
                      "build_kit.py 不再固定用 -Profile main —— mac 会退回 public")
        self.assertNotIn('"public"', src,
                         "build_kit.py 里不该再出现 public profile")

    def test_platform_matrix_is_win_plus_macos(self):
        keys = [p["key"] for p in build_kit.PLATFORMS]
        self.assertEqual(keys, ["win", "macos"])
        for plat in build_kit.PLATFORMS:
            self.assertTrue((DELIVERY / plat["readme"]).is_file(),
                            f"{plat['key']} 的说明模板不存在: {plat['readme']}")

    def test_macos_passes_the_platform_flag_and_win_does_not(self):
        by_key = {p["key"]: p for p in build_kit.PLATFORMS}
        self.assertIsNone(by_key["win"]["package_platform"],
                          "win 不该传 -Platform（应由构建机推断）")
        self.assertEqual(by_key["macos"]["package_platform"], "macos-universal")


class KitDiscovery(unittest.TestCase):
    """`ECHO-kit` 是 `ECHO-kit-macos` 的前缀，选择器必须分得开。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echokit-"))
        for name in ("ECHO-kit-20260922-2051", "ECHO-kit-macos-20260922-2051",
                     "ECHO-kit-20260922-2046", "not-a-kit", "ECHO-kit-bogus"):
            (self.tmp / name).mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_win_selector_ignores_the_macos_kit(self):
        found = [p.name for p in build_kit.find_kits(self.tmp, "ECHO-kit")]
        self.assertEqual(found, ["ECHO-kit-20260922-2046", "ECHO-kit-20260922-2051"])

    def test_macos_selector_only_matches_its_own(self):
        found = [p.name for p in build_kit.find_kits(self.tmp, "ECHO-kit-macos")]
        self.assertEqual(found, ["ECHO-kit-macos-20260922-2051"])

    def test_newest_kit_is_the_last_by_name(self):
        plat = {"kit_prefix": "ECHO-kit"}
        self.assertEqual(build_kit.newest_kit(self.tmp, plat).name,
                         "ECHO-kit-20260922-2051")

    def test_kit_zip_for_picks_this_platform_only(self):
        """`deploy-stable.ps1` 靠它拿"要部署哪个包"。

        2026-09-23 实测事故：部署脚本原来自己在 PowerShell 里按「dist 里最新那个
        ECHO-kit-*.zip」挑，而 do_build() 先 win 后 macos —— macOS 的 kit 永远最新，
        于是往 Windows 安装覆盖的是 **mac 包**：`D:\ECHO\manifest.json` 变成
        `platform: macos-universal`（两个 kit 之间只有它内容不同）。
        """
        for name in ("ECHO-kit-20260922-2051", "ECHO-kit-macos-20260922-2051"):
            shutil.copyfile(__file__, self.tmp / (name + ".zip"))
        win = build_kit.kit_zip_for(self.tmp, build_kit.PLATFORMS[0])
        mac = build_kit.kit_zip_for(self.tmp, build_kit.PLATFORMS[1])
        self.assertEqual(win.name, "ECHO-kit-20260922-2051.zip")
        self.assertEqual(mac.name, "ECHO-kit-macos-20260922-2051.zip")

    def test_kit_zip_for_returns_none_when_the_zip_is_missing(self):
        """有目录没 zip（kit 只组了目录 / zip 被清掉）时要说"没有"，不能瞎猜。"""
        self.assertIsNone(build_kit.kit_zip_for(self.tmp, build_kit.PLATFORMS[0]))


class DeployKitCli(unittest.TestCase):
    """`--deploy-kit <platform>`：stdout 只给路径，给部署脚本当唯一事实源。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echodeploykit-"))
        for name in ("ECHO-kit-20260922-2051", "ECHO-kit-macos-20260922-2051"):
            (self.tmp / name).mkdir()
            shutil.copyfile(__file__, self.tmp / (name + ".zip"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = build_kit.main(["--deploy-kit", argv[0], "--dist", str(self.tmp)])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_win_prints_the_win_zip_only(self):
        code, out, _err = self._run("win")
        self.assertEqual(code, build_kit.EXIT_OK)
        self.assertEqual(Path(out).name, "ECHO-kit-20260922-2051.zip",
                         "输出必须是**一个路径**（调用方直接拿它当文件路径）")

    def test_macos_prints_the_macos_zip_only(self):
        code, out, _err = self._run("macos")
        self.assertEqual(code, build_kit.EXIT_OK)
        self.assertEqual(Path(out).name, "ECHO-kit-macos-20260922-2051.zip")

    def test_unknown_platform_fails_without_touching_stdout(self):
        code, out, err = self._run("solaris")
        self.assertEqual(code, build_kit.EXIT_ERROR)
        self.assertEqual(out, "", "失败信息不许混进 stdout（会被当成 kit 路径）")
        self.assertIn("solaris", err)

    def test_missing_kit_reports_absent(self):
        code, out, _err = self._run("win")
        self.assertEqual(code, build_kit.EXIT_OK)
        (self.tmp / "ECHO-kit-20260922-2051.zip").unlink()
        code, out, err = self._run("win")
        self.assertEqual(code, build_kit.EXIT_ABSENT)
        self.assertEqual(out, "")
        self.assertIn("kit", err)


class DeployStableUsesTheKitSelector(unittest.TestCase):
    """部署脚本不许再自己挑包 —— 这是 2026-09-23 那次错覆盖的根因。

    `docs/tests` 里对 .ps1 的这类"契约"一向用文本断言（如 `test_gh_push_runs_the_check`）：
    脚本是 ASCII-only 的 PowerShell，没法 import，但把规则写在注释与代码里能被钉住。
    """

    def setUp(self):
        self.text = (SCRIPTS / "deploy-stable.ps1").read_text(encoding="utf-8")

    def test_asks_build_kit_for_the_kit(self):
        self.assertIn("--deploy-kit", self.text)
        self.assertIn("build_kit.py", self.text)

    def test_does_not_pick_by_newest_zip_any_more(self):
        self.assertNotIn("Sort-Object LastWriteTime | Select-Object -Last 1", self.text,
                         "按最新 zip 挑 = 永远挑到 macOS 的包")

    def test_verifies_the_kit_platform_before_overlaying(self):
        self.assertIn("Get-KitPlatform", self.text)
        self.assertIn("manifest.json", self.text)

    def test_is_still_ascii_only(self):
        """Windows PowerShell 5.1 把无 BOM 的 .ps1 当 ANSI 读：非 ASCII 会让它解析失败。"""
        raw = (SCRIPTS / "deploy-stable.ps1").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "这个脚本约定不带 BOM")
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            self.fail("deploy-stable.ps1 必须是纯 ASCII：%s" % exc)

    def test_overlays_into_the_code_dir_of_the_target(self):
        """3.0 新布局的目标机把代码放在 `<DestDir>\\echo-core`。

        原来这里无条件 `Copy-Item <kit>\\ECHO\\* -> DestDir` —— 往新布局的机器上部署会**拷错一层**
        （代码散在安装根上，`app` 包进不了 `sys.path`）。脚本按"目标里有没有
        `echo-core\\app\\main.py`"判布局（与 `app/paths.py:echo_base()` 同一条），
        覆盖目标与第 5 步验证的 `Push-Location` 都跟着走。
        """
        self.assertIn("$destCode", self.text, "没有按布局算出的覆盖目标")
        self.assertIn("echo-core\\app\\main.py", self.text, "判据不见了")
        self.assertIn("Copy-Tree $srcEcho $destCode", self.text,
                      "覆盖代码时必须落到代码目录，而不是无条件落到 DestDir")
        self.assertIn("Push-Location $destCode", self.text,
                      "验证那步也要在代码目录里跑（compileall app / import app）")

    def test_the_skill_lands_beside_the_code(self):
        """安装技能跟着代码走：新布局进 `<DestDir>\\echo-core\\.dsh\\skills\\echo-install`。"""
        self.assertIn("$destSkill", self.text)
        self.assertIn("Copy-Tree $srcSkill $destSkill", self.text)


class StalenessJudgement(unittest.TestCase):
    """`--check` 的判据：包里记的哈希 vs 仓库现在的内容。"""

    def test_read_sums_parses_the_build_package_format(self):
        with tempfile.TemporaryDirectory(prefix="echosums-") as tmp:
            kit = Path(tmp)
            (kit / "SHA256SUMS.txt").write_text(
                "abc123  app/api.py\n"
                "def456  web/app.js\n"
                "\n",
                encoding="utf-8")
            sums = build_kit.read_sums(kit)
            self.assertEqual(sums, {"app/api.py": "abc123", "web/app.js": "def456"})

    def test_read_sums_survives_a_missing_file(self):
        with tempfile.TemporaryDirectory(prefix="echosums-") as tmp:
            self.assertEqual(build_kit.read_sums(Path(tmp)), {})

    def test_compare_skill_passes_for_a_faithful_copy(self):
        with tempfile.TemporaryDirectory(prefix="echoskill-") as tmp:
            kit = Path(tmp)
            shutil.copytree(SKILL, kit / "echo-install")
            self.assertEqual(build_kit.compare_skill(kit), [])

    def test_compare_skill_catches_a_drifted_file(self):
        """包里的技能比仓库旧 —— 正是同事会照着一份过时说明安装的那种事故。"""
        with tempfile.TemporaryDirectory(prefix="echoskill-") as tmp:
            kit = Path(tmp)
            shutil.copytree(SKILL, kit / "echo-install")
            target = kit / "echo-install" / "SKILL.md"
            target.write_text(target.read_text(encoding="utf-8") + "\nstale\n",
                              encoding="utf-8")
            problems = build_kit.compare_skill(kit)
            self.assertTrue(any("不一致" in p for p in problems), problems)

    def test_compare_skill_catches_a_missing_file(self):
        with tempfile.TemporaryDirectory(prefix="echoskill-") as tmp:
            kit = Path(tmp)
            shutil.copytree(SKILL, kit / "echo-install")
            (kit / "echo-install" / "scripts" / "harness-install-local.ps1").unlink()
            problems = build_kit.compare_skill(kit)
            self.assertTrue(any("缺文件" in p for p in problems), problems)

    def test_compare_manifest_catches_a_declared_but_absent_component(self):
        """public 档曾经的假话：manifest 声明了包里没有的 components/*.json。"""
        with tempfile.TemporaryDirectory(prefix="echomf-") as tmp:
            kit = Path(tmp)
            (kit / "ECHO").mkdir()
            (kit / "ECHO" / "manifest.json").write_text(
                '{"componentManifests": ["components/offline-pack.json"]}',
                encoding="utf-8")
            problems = build_kit.compare_manifest(kit)
            self.assertTrue(any("components" in p for p in problems), problems)

    def test_compare_manifest_passes_when_the_declared_file_is_there(self):
        with tempfile.TemporaryDirectory(prefix="echomf-") as tmp:
            kit = Path(tmp)
            (kit / "ECHO" / "components").mkdir(parents=True)
            (kit / "ECHO" / "components" / "offline-pack.json").write_text("[]",
                                                                           encoding="utf-8")
            (kit / "ECHO" / "manifest.json").write_text(
                '{"componentManifests": ["components/offline-pack.json"]}',
                encoding="utf-8")
            self.assertEqual(build_kit.compare_manifest(kit), [])


class BuildPackageDeclaresOnlyPackedComponents(unittest.TestCase):
    """`build-package.ps1` 写 manifest 时要筛掉没进包的那些。"""

    def test_component_manifests_are_filtered_by_packed_files(self):
        src = (SCRIPTS / "build-package.ps1").read_text(encoding="utf-8")
        self.assertIn("$packedRels", src,
                      "manifest 的 componentManifests 没按实际打包内容过滤 —— "
                      "public 档会再次声明一个包里没有的文件")


class PushFlowChecksPackages(unittest.TestCase):
    """推送时必须查一眼 dist/ 是否过期（这正是 2026-09-22 漏掉的那一步）。"""

    def test_gh_push_runs_the_check(self):
        src = (SCRIPTS / "gh-push.ps1").read_text(encoding="utf-8")
        self.assertIn("build_kit.py", src)
        self.assertIn("--check", src)
        self.assertIn("kitScript", src)

    def test_check_mode_exists_and_has_its_own_exit_code(self):
        self.assertEqual(build_kit.EXIT_STALE, 2)
        self.assertEqual(build_kit.EXIT_ABSENT, 3)
        src = (SCRIPTS / "build_kit.py").read_text(encoding="utf-8")
        self.assertIn("--check", src)

    def test_quick_gate_really_forwards_quick(self):
        """`-QuickGate` 必须真的把 `-Quick` 传给 gate —— 否则"快档"只是个名字。

        为什么要这个开关：gate 的第 4 步是 927 个用例（约 9 分钟），而只改文档/注释时
        那 9 分钟什么也买不到。`check-windows.ps1` 早就支持 `-Quick`，缺的只是 gh-push
        没有透传的口子。
        """
        src = (SCRIPTS / "gh-push.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$QuickGate", src)
        self.assertIn("$gateArgs += '-Quick'", src)
        self.assertIn("& powershell.exe @gateArgs", src)

    def test_the_full_gate_is_still_the_default(self):
        """默认档位不许被悄悄降级 —— 全量测试是推送前唯一的闸。

        `-QuickGate` 抓不到行为回归（那正是那 927 个用例的活），所以它只能是**显式开关**。
        """
        src = (SCRIPTS / "gh-push.ps1").read_text(encoding="utf-8")
        self.assertIn("if ($QuickGate) {", src,
                      "只在显式传了 -QuickGate 时才允许加 -Quick")
        self.assertIn("Write-Host '=== Windows gate: scripts\\check-windows.ps1 ==='",
                      src, "默认那条分支不见了（默认必须跑全量）")


class KitZipCarriesATopLevelPrefix(unittest.TestCase):
    """zip 条目必须带 `<kit>/` 前缀，否则解包出来是一堆散文件（2026-09-22 踩过）。"""

    def test_make_zip_prefixes_every_entry(self):
        import zipfile
        with tempfile.TemporaryDirectory(prefix="echozip-") as tmp:
            src = Path(tmp) / "ECHO-kit-20260922-2100"
            (src / "ECHO").mkdir(parents=True)
            (src / "ECHO" / "hello.txt").write_text("hi", encoding="utf-8")
            (src / "先读我.md").write_text("read me", encoding="utf-8")
            out = Path(tmp) / "kit.zip"
            build_kit.make_zip(src, out, src.name)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            self.assertTrue(all(n.startswith(src.name + "/") for n in names), names)
            self.assertIn(f"{src.name}/ECHO/hello.txt", names)
            self.assertIn(f"{src.name}/先读我.md", names)


if __name__ == "__main__":
    unittest.main()
