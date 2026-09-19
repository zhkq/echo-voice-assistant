# -*- coding: utf-8 -*-
"""路径 / 平台接缝守卫（P3 / D12 + D29）。

范式照 tests/test_platform_contract.py：ROOT + 遍历 app/ + bad 列表 + assertEqual。

守卫三件事（对应 docs/P3-收口施工方案.md §4）：

  1. PATH_DERIVATION —— 安装根只能由 app/paths.py 推导（D29）；
  2. DRIVE_LITERAL   —— 代码里不得出现盘符绝对路径（改写配置项或 {ECHO}/{DATA}）；
  3. PLATFORM_TOKEN  —— 平台分支/特征串只能出现在 app/platform/<os>/（D12）。

白名单只有 app/platform/（全部规则）与 app/paths.py（推导规则），并被单独钉住：
放宽它 = 把接缝守卫关掉，所以这本身也要有一次断言。

规则实现**直接复用 scripts/audit-paths.py**（同一份实现），避免"报告说干净、
守卫其实没查"的漂移；另配一组自证用例，构造已知违规/合法的源码确认扫描器
分别报出/不报 —— 工具本身也要能自证（§28.9 的方法学）。
"""
import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_PATH = os.path.join(ROOT, "scripts", "audit-paths.py")


def _load_audit():
    spec = importlib.util.spec_from_file_location("echo_audit_paths", AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load_audit()

#: 白名单的**期望值**。缩到只剩这两项是 P3 的验收条件。
EXPECTED_ALLOWED = [("app/platform/", "*"), ("app/paths.py", "PATH_DERIVATION")]


def _violations(rule):
    """app/ 下所有未被白名单放行的该规则命中，格式 file:line: text。"""
    bad = []
    for path in audit._iter_app_files(ROOT):
        rel = audit._rel(ROOT, path)
        for r, ln, text in audit.scan_file(path):
            if r == rule and not audit._allowed(rel, r):
                bad.append("%s:%d: %s" % (rel, ln, text))
    return bad


class InstallRootDerivationStaysInThePathsLayer(unittest.TestCase):
    def test_only_paths_py_derives_the_install_root(self):
        bad = _violations("PATH_DERIVATION")
        self.assertEqual(
            bad, [],
            "安装根只能由 app/paths.py 推导（D29）；请改走 paths.echo_root()/"
            "data_root()/models_root()：\n" + "\n".join(bad))

    def test_no_drive_letter_literals_in_business_code(self):
        bad = _violations("DRIVE_LITERAL")
        self.assertEqual(
            bad, [],
            "代码里不得出现盘符绝对路径；改成配置项或 {ECHO}/{DATA} 占位符：\n"
            + "\n".join(bad))


class PlatformDifferencesStayInTheSeam(unittest.TestCase):
    def test_no_platform_branches_outside_app_platform(self):
        bad = _violations("PLATFORM_TOKEN")
        self.assertEqual(
            bad, [],
            "平台差异只允许出现在 app/platform/<os>/（D12）；业务代码请通过 "
            "app.platform 取平台默认值/原语，不要自己写 os.name / LOCALAPPDATA / "
            "platform.system() 分支：\n" + "\n".join(bad))


class WhitelistStaysMinimal(unittest.TestCase):
    def test_allowlist_is_only_platform_and_paths(self):
        self.assertEqual(
            audit.ALLOWED, EXPECTED_ALLOWED,
            "白名单只能有 app/platform/ 与 app/paths.py；放宽它等于把接缝守卫关掉")


class EveryPlatformImplementsThePrimitives(unittest.TestCase):
    """三个平台的 env.py 必须实现同一组接缝原语。

    否则在 Windows 上写好的调用（如 codebuddy 用 no_window_creationflags()）会在 mac
    上 ImportError / AttributeError —— 而本机没有 mac 可测，所以用这条静态断言钉住。
    三个 env 模块在 Windows 上都能安全 import（fcntl / ctypes.WinDLL 都是函数内 import）。
    """

    PRIMITIVES = ("display_name", "no_window_creationflags", "chromium_candidates",
                  "agent_cli_candidates", "tcp_excluded_port_range_output",
                  "hf_executable", "shell_script",
                  "acquire_named_lock", "named_lock_held", "release_named_lock")

    def test_all_platform_env_modules_expose_every_primitive(self):
        import importlib

        missing = []
        for name in ("win32", "darwin", "linux"):
            mod = importlib.import_module("app.platform.%s.env" % name)
            for fn in self.PRIMITIVES:
                if not callable(getattr(mod, fn, None)):
                    missing.append("%s.%s" % (name, fn))
        self.assertEqual(missing, [],
                         "每个平台的 env.py 都要实现同一组接缝原语：\n" + "\n".join(missing))


class ScannerSelfTest(unittest.TestCase):
    """扫描器自证：已知违规必须报、已知合法必须不报（防止规则被改瞎而静默放行）。"""

    def _rules(self, source):
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(source)
        self.addCleanup(os.remove, path)
        return sorted({rule for rule, _ln, _text in audit.scan_file(path)})

    def test_flags_bare_file_root_derivation(self):
        src = "import os\nX = os.path.dirname(os.path.abspath(__file__))\n"
        self.assertIn("PATH_DERIVATION", self._rules(src))

    def test_ignores_a_third_party_package_location(self):
        # speechbrain.__file__ 是依赖包自己的位置，与"安装根"无关，不得误报。
        src = "import os, speechbrain\nX = os.path.dirname(speechbrain.__file__)\n"
        self.assertNotIn("PATH_DERIVATION", self._rules(src))

    def test_flags_os_name_branch(self):
        self.assertIn("PLATFORM_TOKEN", self._rules("import os\nif os.name == 'nt':\n    X = 1\n"))

    def test_flags_platform_system_inside_an_fstring(self):
        # Python 3.11 把 f-string 当一个 STRING token，里面的分支最容易被漏掉。
        self.assertIn("PLATFORM_TOKEN", self._rules('import platform\nX = f"a {platform.system()}"\n'))

    def test_flags_env_platform_token_in_a_string(self):
        src = 'import os\nX = os.environ.get("LOCALAPPDATA", "")\n'
        self.assertIn("PLATFORM_TOKEN", self._rules(src))

    def test_ignores_url_scheme_as_a_drive_literal(self):
        self.assertNotIn("DRIVE_LITERAL", self._rules('X = "http://127.0.0.1:1/"\n'))

    def test_flags_a_real_drive_literal(self):
        self.assertIn("DRIVE_LITERAL", self._rules('X = r"C:\\Windows"\n'))

    def test_ignores_docstring_prose(self):
        self.assertEqual(self._rules('"""example C:\\Windows in prose"""\n'), [])


if __name__ == "__main__":
    unittest.main()
