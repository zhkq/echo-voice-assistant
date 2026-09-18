# -*- coding: utf-8 -*-
"""路径层测试（app/paths.py，P1 / D18–D21）。

这一层是 2.0 让"会议目录 / 模型目录可配置"的地基，所以把语义钉住：

  1. 占位符与 ``~`` 的展开、相对路径补全、``..`` 拒绝；
  2. ``data_root()`` 的分平台默认值与环境变量覆盖（测试/多实例隔离靠它）；
  3. 配置了 ``meetingsDir`` / ``modelsDir`` 时以配置为准，否则回落到默认值；
  4. ``validate_dir()`` 拦住磁盘根 / 主目录本身 / 非目录 / 不可写；
  5. ``preflight()`` 在任何异常下都不抛（面板要拿它渲染体检页）。
"""
import os
import tempfile
import unittest

from app import paths


class ExpandResolveTests(unittest.TestCase):
    def test_expand_placeholders(self):
        # expand() 只做替换、不做路径归一化（归一化是 resolve() 的职责），
        # 所以这里比较 normpath 之后的值。
        out = paths.expand("{ECHO}/models")
        self.assertEqual(os.path.normpath(out), os.path.normpath(os.path.join(paths.echo_root(), "models")))
        self.assertTrue(os.path.normpath(paths.expand("{DATA}/logs")).startswith(os.path.normpath(paths.data_root())))

    def test_expand_tilde(self):
        self.assertEqual(paths.expand("~"), os.path.expanduser("~"))

    def test_resolve_relative_uses_echo_root(self):
        self.assertEqual(paths.resolve("models"),
                         os.path.normpath(os.path.join(paths.echo_root(), "models")))

    def test_resolve_absolute_untouched(self):
        tmp = tempfile.mkdtemp(prefix="echo-paths-")
        self.assertEqual(paths.resolve(tmp), os.path.normpath(tmp))

    def test_resolve_empty_is_empty(self):
        self.assertEqual(paths.resolve(""), "")

    def test_resolve_rejects_traversal(self):
        for bad in ("../etc", "models/../../secret", r"..\windows"):
            with self.assertRaises(ValueError, msg="必须拒绝 %r" % bad):
                paths.resolve(bad)

    def test_extra_placeholders(self):
        self.assertEqual(os.path.normpath(paths.expand("{VAULT}/notes", {"VAULT": r"C:\vault"})),
                         os.path.normpath(os.path.join(r"C:\vault", "notes")))


class RootTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("ECHO_DATA")
        self._get = paths._settings_get
        paths._settings_get = lambda name: ""          # 默认无配置

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ECHO_DATA", None)
        else:
            os.environ["ECHO_DATA"] = self._saved
        paths._settings_get = self._get

    def test_data_root_honours_env_override(self):
        tmp = tempfile.mkdtemp(prefix="echo-data-")
        os.environ["ECHO_DATA"] = tmp
        self.assertEqual(paths.data_root(), os.path.abspath(tmp))

    def test_windows_default_is_under_echo_root(self):
        if os.name != "nt":
            self.skipTest("Windows 专属默认值")
        self.assertEqual(paths.data_root(), os.path.join(paths.echo_root(), "data"))

    def test_macos_default_is_application_support(self):
        # data_root() 自己不再判平台：默认值来自 app/platform/<os>/env.py（D10–D12），
        # 所以这里替换的是"取平台默认值"这一层，而不是去改全局的 sys.platform。
        from app.platform import darwin
        self.assertEqual(darwin.NAME, "darwin")
        self.assertIn("Library", darwin.PLATFORM_DEFAULTS["dataDir"])
        saved = paths._platform_defaults
        paths._platform_defaults = lambda: dict(darwin.PLATFORM_DEFAULTS)
        try:
            os.environ.pop("ECHO_DATA", None)
            self.assertEqual(paths.data_root(),
                             os.path.normpath(darwin.PLATFORM_DEFAULTS["dataDir"]))
        finally:
            paths._platform_defaults = saved

    def test_linux_default_is_xdg(self):
        from app.platform import linux
        self.assertIn("ECHO", linux.PLATFORM_DEFAULTS["dataDir"])

    def test_platform_package_reports_a_known_name(self):
        from app import platform as plat
        self.assertIn(plat.current(), plat.NAMES)
        self.assertTrue(plat.defaults().get("dataDir"))
        self.assertTrue(plat.dangerous_prefixes())

    def test_data_root_falls_back_when_platform_defaults_missing(self):
        saved = paths._platform_defaults
        paths._platform_defaults = lambda: {}
        try:
            os.environ.pop("ECHO_DATA", None)
            self.assertEqual(paths.data_root(),
                             os.path.normpath(os.path.join(paths.echo_root(), "data")))
        finally:
            paths._platform_defaults = saved

    def test_meetings_and_models_defaults(self):
        self.assertEqual(paths.meetings_root(), os.path.join(paths.data_root(), "meetings"))
        self.assertEqual(paths.models_root(), os.path.join(paths.echo_root(), "models"))

    def test_configured_dirs_win(self):
        tmp_m = tempfile.mkdtemp(prefix="echo-meet-")
        tmp_w = tempfile.mkdtemp(prefix="echo-models-")
        paths._settings_get = lambda name: {"meetingsDir": tmp_m, "modelsDir": tmp_w}.get(name, "")
        self.assertEqual(paths.meetings_root(), os.path.normpath(tmp_m))
        self.assertEqual(paths.models_root(), os.path.normpath(tmp_w))

    def test_configured_dir_supports_placeholder(self):
        paths._settings_get = lambda name: "{ECHO}/my-meetings" if name == "meetingsDir" else ""
        self.assertEqual(paths.meetings_root(),
                         os.path.normpath(os.path.join(paths.echo_root(), "my-meetings")))

    def test_active_roots_reports_flags(self):
        r = paths.active_roots()
        self.assertFalse(r["meetingsConfigured"])
        paths._settings_get = lambda name: tempfile.mkdtemp(prefix="echo-x-")
        self.assertTrue(paths.active_roots()["meetingsConfigured"])


class ValidateTests(unittest.TestCase):
    def test_accepts_writable_temp_dir(self):
        tmp = tempfile.mkdtemp(prefix="echo-ok-")
        ok, why = paths.validate_dir(tmp)
        self.assertTrue(ok, why)

    def test_rejects_empty(self):
        self.assertFalse(paths.validate_dir("")[0])

    def test_rejects_drive_root(self):
        if os.name != "nt":
            self.skipTest("Windows 专属")
        ok, why = paths.validate_dir("C:\\")
        self.assertFalse(ok)
        self.assertIn("根目录", why)

    def test_rejects_home_itself(self):
        ok, why = paths.validate_dir(os.path.expanduser("~"))
        self.assertFalse(ok)
        self.assertIn("主目录", why)

    def test_rejects_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            name = fh.name
        try:
            ok, why = paths.validate_dir(name)
            self.assertFalse(ok)
            self.assertIn("不是目录", why)
        finally:
            os.remove(name)

    def test_missing_dir_needs_create_flag(self):
        target = os.path.join(tempfile.mkdtemp(prefix="echo-mk-"), "sub", "dir")
        self.assertFalse(paths.validate_dir(target)[0])
        ok, why = paths.validate_dir(target, create=True)
        self.assertTrue(ok, why)
        self.assertTrue(os.path.isdir(target))

    def test_rejects_traversal(self):
        self.assertFalse(paths.validate_dir("../evil")[0])

    def test_ascii_warning(self):
        self.assertEqual(paths.ascii_warning("C:\\echo\\models"), "")
        self.assertTrue(paths.ascii_warning("C:\\echo\\模型"))
        self.assertTrue(paths.is_ascii("plain"))
        self.assertFalse(paths.is_ascii("中文"))


class PreflightTests(unittest.TestCase):
    def test_preflight_never_raises_and_lists_four_roots(self):
        out = paths.preflight()
        names = [r["name"] for r in out["roots"]]
        self.assertEqual(names, ["ECHO", "DATA", "MEETINGS", "MODELS"])
        for item in out["roots"]:
            self.assertIn("writable", item)
            self.assertIn("freeGB", item)

    def test_preflight_survives_broken_config(self):
        saved = paths._settings_get

        def boom(_name):
            raise RuntimeError("config 挂了")

        paths._settings_get = boom
        try:
            out = paths.preflight()
            self.assertEqual(len(out["roots"]), 4)
        finally:
            paths._settings_get = saved


if __name__ == "__main__":
    unittest.main()
