# -*- coding: utf-8 -*-
"""「已有 DSH」安装路径的判据（2026-10-27 用户要求）。

交付包安装 DSH 时要先问一句"这台机器上已经有 DSH 吗"：
  * 没有 -> 走原来的本地永久安装（npm / npx 缓存）；
  * 有   -> 让用户指定目录，**探测** `@deepseek-ai/dsh/lib/bin.js`，
            然后把它写进 `harnessCommand`，不再下载 npm 包。

这里钉住两件事：
  1. 探测脚本对 Windows 路径形状的容忍度：引号（资源管理器"复制为路径"）、
     尾反斜杠、空格、`&`/括号、`%VAR%`、直接指到 bin.js、ECHO 自己的布局；
  2. 探测出来的命令必须能被 `harness_proc.argv()` 用的 `shlex.split(posix=False)`
     重新拆成「node / bin.js / web」——反斜杠**不能被多转义一层**。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / ".dsh" / "skills" / "echo-install" / "scripts" / "find-existing-dsh.ps1"
AUTO = ROOT / ".dsh" / "skills" / "echo-install" / "scripts" / "find-existing-dsh-auto.ps1"
INSTALL_ALL = ROOT / "scripts" / "install-all.ps1"
COMPONENTS = ROOT / ".dsh" / "skills" / "echo-install" / "scripts" / "echo-install-components.ps1"
KIT_CMD = ROOT / "delivery" / "kit-install.cmd"


def _powershell() -> str:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _fake_dsh_tree(base: Path, *, with_package: bool = True) -> dict:
    dsh = base / "node_modules" / "@deepseek-ai" / "dsh"
    (dsh / "lib").mkdir(parents=True, exist_ok=True)
    entry = dsh / "lib" / "bin.js"
    entry.write_text("// fake dsh entry\n", encoding="utf-8")
    node = base / "node.exe"
    node.write_text("", encoding="utf-8")
    if with_package:
        (dsh / "package.json").write_text(
            json.dumps({"name": "@deepseek-ai/dsh", "version": "9.9.9"}),
            encoding="utf-8",
        )
    return {"base": base, "entry": entry, "node": node, "dsh": dsh}


@unittest.skipUnless(os.name == "nt", "Windows 路径/反斜杠形状只在 Windows 上测")
class ExistingDshProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ps = _powershell()
        if not cls.ps:
            raise unittest.SkipTest("找不到 PowerShell")
        if not HELPER.is_file():
            raise unittest.SkipTest("缺探测脚本：%s" % HELPER)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-dsh-probe-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, path_input: str, node: Path | None = None) -> dict:
        argv = [self.ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HELPER),
                "-Path", path_input]
        if node is not None:
            argv += ["-NodeExe", str(node)]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, "探测脚本没有输出 JSON：\n%s\n%s" % (proc.stdout, proc.stderr))
        return json.loads(lines[-1])

    def _assert_roundtrip(self, got: dict, node: Path, entry: Path):
        self.assertTrue(got["ok"], got)
        parts = [p.strip('"') for p in shlex.split(got["command"], posix=False)]
        self.assertEqual(parts, [str(node), str(entry), "web"],
                         "命令里的 Windows 路径被转义坏了：%r" % got["command"])
        self.assertIn('"', got["command"], "含空格的路径必须加引号")

    def test_install_dir_with_spaces_ampersand_and_brackets(self):
        base = self.tmp / "DSH & Tools (x64)"
        tree = _fake_dsh_tree(base)
        got = self._run(str(base), tree["node"])
        self._assert_roundtrip(got, tree["node"].resolve(), tree["entry"].resolve())
        self.assertEqual(got["version"], "9.9.9")

    def test_explorer_copied_quoted_path_with_trailing_backslash(self):
        base = self.tmp / "quoted path"
        tree = _fake_dsh_tree(base)
        raw = '"%s\\"' % base
        got = self._run(raw, tree["node"])
        self._assert_roundtrip(got, tree["node"].resolve(), tree["entry"].resolve())
        self.assertEqual(Path(got["normalized"]), base.resolve())

    def test_percent_variable_is_expanded(self):
        base = self.tmp / "env path"
        tree = _fake_dsh_tree(base)
        env = dict(os.environ)
        env["ECHO_TEST_DSH_DIR"] = str(base)
        argv = [self.ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HELPER),
                "-Path", "%ECHO_TEST_DSH_DIR%", "-NodeExe", str(tree["node"])]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, env=env)
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, proc.stderr)
        got = json.loads(lines[-1])
        self._assert_roundtrip(got, tree["node"].resolve(), tree["entry"].resolve())

    def test_direct_bin_js_path(self):
        base = self.tmp / "direct"
        tree = _fake_dsh_tree(base)
        got = self._run(str(tree["entry"]), tree["node"])
        self._assert_roundtrip(got, tree["node"].resolve(), tree["entry"].resolve())

    def test_echo_3_layout_is_found(self):
        base = self.tmp / "ECHO root"
        entry = base / "dsh" / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake\n", encoding="utf-8")
        node = base / "node.exe"
        node.write_text("", encoding="utf-8")
        got = self._run(str(base), node)
        self._assert_roundtrip(got, node.resolve(), entry.resolve())

    def test_missing_path_is_reported_not_raised(self):
        got = self._run(str(self.tmp / "does-not-exist"))
        self.assertFalse(got["ok"])
        self.assertTrue(got["error"])

    def test_auto_probe_checks_the_echo_root_we_pass(self):
        base = self.tmp / "auto candidate"
        tree = _fake_dsh_tree(base)
        proc = subprocess.run(
            [self.ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(AUTO),
             "-ExtraDir", str(base), "-NodeExe", str(tree["node"])],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, proc.stderr)
        got = json.loads(lines[-1])
        self.assertTrue(got["ok"], got)
        self.assertEqual(Path(got["entry"]), tree["entry"].resolve())
        self.assertTrue(got.get("auto"))
        self._assert_roundtrip(got, tree["node"].resolve(), tree["entry"].resolve())


class ExistingDshWiringTests(unittest.TestCase):
    def test_install_all_exposes_and_forwards_the_new_options(self):
        text = INSTALL_ALL.read_text(encoding="utf-8-sig")
        self.assertIn("[string]$DshDir", text)
        self.assertIn("[switch]$AskDsh", text)
        self.assertIn("$argv['DshDir'] = $DshDir", text)
        self.assertIn("$argv['AskDsh'] = $true", text)
        # 升级链路不应该再问"已有 DSH 目录"（它只问要不要升级 DSH）
        self.assertIn("$DshDir = ''", text)
        self.assertIn("$AskDsh = $false", text)

    def test_components_probe_and_pin_the_external_entry(self):
        text = COMPONENTS.read_text(encoding="utf-8-sig")
        self.assertIn("find-existing-dsh.ps1", text)
        self.assertIn("find-existing-dsh-auto.ps1", text)
        self.assertIn("function Ask-ExistingDsh", text)
        self.assertIn("Resolve-ExistingDsh", text)
        self.assertIn("Resolve-AutoDsh", text)
        self.assertIn("自动探测到已有 DSH", text)
        self.assertIn("$script:ExistingDsh = $true", text)
        # 已有外部目录时必须写 harnessCommand，不能只靠 ECHO 自己找本地入口
        self.assertIn("$script:ExistingDsh", text)
        self.assertIn("$values['harnessCommand']", text)
        # 原有 -PinHarnessCommand 契约仍要保留
        self.assertIn("and $PinHarnessCommand", text)

    def test_kit_passes_ask_dsh_only_for_fresh_installs(self):
        cmd = KIT_CMD.read_text(encoding="ascii")
        self.assertIn('if not defined EXISTING set "ARGS=%ARGS% -Yes"', cmd)
        self.assertIn('if not defined EXISTING set "ARGS=%ARGS% -AskDsh"', cmd)
        # 升级仍然是交互问"要不要升级 DSH"，不能传 -AskDsh
        self.assertLess(cmd.index("if not defined EXISTING set \"ARGS=%ARGS% -Yes\""),
                        cmd.index("if not defined EXISTING set \"ARGS=%ARGS% -AskDsh\""))


if __name__ == "__main__":
    unittest.main()
