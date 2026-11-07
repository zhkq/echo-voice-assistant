# -*- coding: utf-8 -*-
"""Windows 单文件安装包（阶段 1）的静态契约。

真机安装/卸载/升级与截图由 Lead 在 Windows 上做；这里钉住**不能漂移**的那几条：
  * 当前用户安装（Inno `PrivilegesRequired=lowest`，不写 HKLM，不弹 UAC）；
  * 数据固定 `%LOCALAPPDATA%\\ECHO\\data` 且卸载不删；
  * 安装包不含 DSH / 能力后端 / 模型；
  * 不接自签名证书；
  * 启动器是 NativeAOT WinExe，manifest 是 asInvoker；
  * 首次向导的 DSH 走官方桌面端，安装失败会如实报错。
"""
import importlib.util
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "ECHO.iss"
BUILD = ROOT / "scripts" / "build_installer.py"
LAUNCHER_CSPROJ = ROOT / "launcher" / "ECHO.Launcher" / "ECHO.Launcher.csproj"
MANIFEST = ROOT / "launcher" / "ECHO.Launcher" / "app.manifest"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-installer.yml"


def load_build_module():
    spec = importlib.util.spec_from_file_location("echo_build_installer", BUILD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InnoContractTests(unittest.TestCase):
    def setUp(self):
        self.text = ISS.read_text(encoding="utf-8")
        self.low = self.text.lower()

    def test_current_user_install_only(self):
        self.assertIn("PrivilegesRequired=lowest", self.text)
        self.assertNotRegex(self.text, r"(?im)^\s*PrivilegesRequired\s*=\s*admin\b")
        self.assertNotRegex(self.text, r"(?im)^\s*PrivilegesRequiredOverridesAllowed\s*=")
        self.assertNotIn("HKLM", self.text)
        self.assertIn('DefaultDirName={localappdata}\\Programs\\ECHO', self.text)

    def test_installer_is_single_file_and_modern(self):
        self.assertIn("Compression=lzma2/ultra64", self.text)
        self.assertIn("SolidCompression=yes", self.text)
        self.assertIn("OutputBaseFilename=ECHO-Setup-{#AppVersion}", self.text)
        self.assertIn("WizardStyle=modern", self.text)
        self.assertIn("ArchitecturesAllowed=x64compatible", self.text)

    def test_appid_is_stable(self):
        self.assertIn("AppId={{0343EF50-9805-4751-B4E2-30CE838AD4C6}", self.text)

    def test_data_root_is_fixed_and_never_uninstalled(self):
        self.assertIn('{localappdata}\\ECHO\\data', self.text)
        self.assertIn("uninsneveruninstall", self.text)
        self.assertNotIn("[UninstallDelete]", self.text)

    def test_dsh_and_backend_are_not_payload(self):
        self.assertNotIn("ECHO-backend", self.text)
        self.assertNotIn("DeepSeek Harness", self.text)
        self.assertNotRegex(self.text, r"(?im)^\s*Source:.*(bundle|models|backend|dsh)")
        self.assertNotIn("dsh-latest", self.text)

    def test_no_signing_material_of_any_kind(self):
        self.assertNotRegex(self.low, r"(?im)^\s*SignTool\s*=")
        self.assertNotIn(".pfx", self.low)
        self.assertNotIn("signtool", self.low)
        self.assertNotIn("self-signed certificate", self.low)

    def test_launcher_is_used_for_stop_and_upgrade_mode(self):
        self.assertIn("ECHO.exe", self.text)
        self.assertIn("--stop", self.text)
        self.assertIn("install-mode.json", self.text)
        self.assertIn("'fresh'", self.text)
        self.assertIn("'upgrade'", self.text)
        self.assertIn("skipifsilent", self.text)

    def test_minimum_windows_is_10(self):
        self.assertIn("MinVersion=10.0", self.text)


class LauncherContractTests(unittest.TestCase):
    def test_launcher_is_nativeaot_winexe_without_admin(self):
        csproj = LAUNCHER_CSPROJ.read_text(encoding="utf-8")
        self.assertIn("<OutputType>WinExe</OutputType>", csproj)
        self.assertIn("<PublishAot>true</PublishAot>", csproj)
        self.assertIn("<RuntimeIdentifier>win-x64</RuntimeIdentifier>", csproj)
        self.assertIn("app.manifest", csproj)
        manifest = MANIFEST.read_text(encoding="utf-8")
        self.assertIn('requestedExecutionLevel level="asInvoker"', manifest)
        self.assertNotIn("requireAdministrator", manifest)

    def test_launcher_sets_the_state_root_explicitly(self):
        program = (ROOT / "launcher" / "ECHO.Launcher" / "Program.cs").read_text(encoding="utf-8")
        job = (ROOT / "launcher" / "ECHO.Launcher" / "JobObject.cs").read_text(encoding="utf-8")
        self.assertIn("ECHO_BASE", program)
        self.assertIn("ECHO_DATA", program)
        self.assertIn("PYTHONUTF8", program)
        self.assertIn("KILL_ON_JOB_CLOSE", job)
        self.assertIn("--stop", program)


class BuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_build_module()

    def test_version_comes_from_app_init(self):
        app_version = re.search(
            r'^__version__\s*=\s*"([^"]+)"',
            (ROOT / "app" / "__init__.py").read_text(encoding="utf-8"),
            re.MULTILINE).group(1)
        self.assertEqual(self.mod.read_version(), app_version)

    def test_numeric_version_is_always_four_parts(self):
        self.assertEqual(self.mod.numeric_version("3.0.1"), "3.0.1.0")
        self.assertEqual(self.mod.numeric_version("3.0.1-beta"), "3.0.1.0")
        self.assertEqual(self.mod.numeric_version("3"), "3.0.0.0")

    def test_explicit_missing_iscc_is_rejected(self):
        with self.assertRaises(self.mod.BuildError):
            self.mod.find_iscc(str(ROOT / "no-such-iscc.exe"))

    def test_script_never_invokes_a_signing_tool(self):
        text = BUILD.read_text(encoding="utf-8").lower()
        for forbidden in ("signtool", ".pfx"):
            self.assertNotIn(forbidden, text)

    def test_runtime_zip_stages_only_runtime_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runtime_zip = tmp_path / "runtime.zip"
            with zipfile.ZipFile(runtime_zip, "w") as zf:
                zf.writestr("runtime-core/python.exe", b"x")
                zf.writestr("runtime-core/Lib/site-packages/fastapi/__init__.py", b"x")
                zf.writestr("pack-manifest.json", "{}")
                zf.writestr("SHA256SUMS.txt", "")
            stage = tmp_path / "stage"
            stage.mkdir()
            self.mod.stage_runtime(stage, runtime_zip, dry_run=False)
            self.assertTrue((stage / "runtime-core" / "python.exe").is_file())
            self.assertTrue((stage / "runtime-core" / "Lib" / "site-packages" /
                             "fastapi" / "__init__.py").is_file())
            self.assertFalse((stage / "pack-manifest.json").exists())

    def test_main_zip_stages_the_code_as_echo_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main_zip = tmp_path / "main.zip"
            with zipfile.ZipFile(main_zip, "w") as zf:
                zf.writestr("ECHO/app/main.py", b"print('x')")
                zf.writestr("ECHO/manifest.json", "{}")
            stage = tmp_path / "stage"
            stage.mkdir()
            self.mod.stage_main(stage, main_zip, dry_run=False)
            self.assertTrue((stage / "echo-core" / "app" / "main.py").is_file())
            self.assertTrue((stage / "echo-core" / "manifest.json").is_file())

    def test_dry_run_does_not_need_iscc(self):
        self.assertEqual(self.mod.main([
            "--dry-run", "--skip-main", "--skip-runtime",
            "--skip-sidebar", "--skip-launcher"]), 0)


class WorkflowTests(unittest.TestCase):
    def test_ci_builds_and_smoke_tests_the_installer(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("windows-latest", text)
        self.assertIn("build_installer.py", text)
        self.assertIn("smoke-install.ps1", text)
        self.assertIn("a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c",
                      text)
        self.assertIn("ECHO-Setup-*.exe", text)


class DshWizardUiTests(unittest.TestCase):
    def test_the_first_run_overlay_has_a_dsh_step(self):
        text = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('{ id: "dsh", name: "装好 DSH"', text)
        self.assertIn("onboardRenderDsh", text)
        self.assertIn("/api/wizard/dsh/install", text)
        self.assertIn("/api/wizard/dsh/state", text)
        # The DSH step must not silently claim success when the service is offline.
        self.assertIn("服务没有在线", text)


class DshContractTests(unittest.TestCase):
    def test_helper_can_return_a_utf8_command_file(self):
        text = (ROOT / ".dsh" / "skills" / "echo-install" / "scripts" /
                "harness-install-desktop.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$CommandFile", text)
        self.assertIn("WriteAllText($CommandFile", text)
        self.assertNotIn("npx -y", text.lower())

    def test_api_exposes_dsh_wizard_endpoints(self):
        text = (ROOT / "app" / "api.py").read_text(encoding="utf-8")
        for route in ('"/wizard/dsh"', '"/wizard/dsh/detect"',
                      '"/wizard/dsh/state"', '"/wizard/dsh/install"'):
            self.assertIn(route, text)

    def test_setup_module_does_not_revive_the_npm_path(self):
        text = (ROOT / "app" / "dsh_setup.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("npm install", text)
        self.assertNotIn("npx -y", text)
        self.assertIn("harness-install-desktop.ps1", text)
        self.assertIn("agentbackend", text)
        self.assertIn("dshautostart", text)


if __name__ == "__main__":
    unittest.main()
