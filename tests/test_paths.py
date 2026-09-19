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


class ConfigurableRootTests(unittest.TestCase):
    """两个"用户可指定的根"必须**每次访问都跟着配置走**（D20/D21）。

    1.x 里 `MEETINGS_DIR` / `MODELS_DIR` 是 import 期常量，
    用户改不了；2.0 改成函数。如果哪天有人把它改回常量，这一组会红。

    注意 `db.DATA_DIR` 是**故意**保持常量的：数据根不是用户可配置项（D18 的分平台
    留到 P3，连同 mac 的布局迁移一起做），而且现有测试直接用"给模块属性赋值"来隔离
    数据目录。所以这里不测它。
    """

    def setUp(self):
        self._get = paths._settings_get

    def tearDown(self):
        paths._settings_get = self._get

    def test_meetings_dir_follows_config(self):
        import app.meeting as meeting
        tmp = tempfile.mkdtemp(prefix="echo-meet-")
        paths._settings_get = lambda name: tmp if name == "meetingsDir" else ""
        self.assertEqual(meeting.meetings_dir(), os.path.normpath(tmp))
        self.assertEqual(meeting.ensure_meetings_dir(), os.path.normpath(tmp))
        # 切回默认后必须立刻跟着变（import 期常量做不到这件事）
        paths._settings_get = lambda name: ""
        self.assertEqual(meeting.meetings_dir(), os.path.join(paths.data_root(), "meetings"))

    def test_stt_and_modelinfo_models_dir_follow_config(self):
        import app.audio.stt as stt
        import app.modelinfo as mi
        tmp = tempfile.mkdtemp(prefix="echo-models-")
        paths._settings_get = lambda name: tmp if name == "modelsDir" else ""
        self.assertEqual(stt.models_dir(), os.path.normpath(tmp))
        self.assertEqual(mi.models_dir(), os.path.normpath(tmp))
        paths._settings_get = lambda name: ""
        self.assertEqual(stt.models_dir(), os.path.join(paths.echo_root(), "models"))

    def test_no_module_level_path_constants_left_for_configurable_roots(self):
        """这两个根不能再以模块常量形式出现（半成品状态比没做更危险）。"""
        for rel, name in (("meeting.py", "MEETINGS_DIR"),
                          ("audio/stt.py", "MODELS_DIR"),
                          ("modelinfo.py", "MODELS_DIR")):
            p = os.path.join(paths.echo_root(), "app", rel)
            with open(p, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines()]
            bad = [i for i, ln in enumerate(lines, 1)
                   if ln.strip().startswith(name + " =")]
            self.assertEqual(bad, [], "%s 里不应再有模块级 %s 常量（行 %s）" % (rel, name, bad))

    def test_paths_config_items_are_declared(self):
        """两个新配置项必须在 DEFAULTS 里，且默认值为空（留空 = 用平台默认）。"""
        from app import config
        for key in ("meetingsDir", "modelsDir"):
            self.assertIn(key, config.DEFAULTS, "%s 必须登记进 DEFAULTS" % key)
            self.assertEqual(config.DEFAULTS[key]["value"], "")
            self.assertEqual(config.DEFAULTS[key]["grp"], "paths")


class EchoRootOverrideTests(unittest.TestCase):
    """安装根（``{ECHO}``）的推导与覆盖（P3 第一步）。

    默认必须由 ``__file__`` 推导——"代码在哪，根就在哪"是全系统内部一律相对根书写的
    前提；``ECHO_ROOT`` 只做**环境变量**覆盖（打包分发 / 多实例 / 测试），刻意不做成
    面板配置项：安装根配错 = 全盘静默跑偏（模型找不到、数据写错地方、门禁测的不是
    这棵树）。用户该配的是数据类目录：``ECHO_DATA`` / ``meetingsDir`` / ``modelsDir``。
    """

    def setUp(self):
        self._root = os.environ.get("ECHO_ROOT")
        self._data = os.environ.get("ECHO_DATA")
        self._get = paths._settings_get
        paths._settings_get = lambda name: ""

    def tearDown(self):
        for key, saved in (("ECHO_ROOT", self._root), ("ECHO_DATA", self._data)):
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        paths._settings_get = self._get

    def test_default_derived_from_module_file(self):
        os.environ.pop("ECHO_ROOT", None)
        expected = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))
        self.assertEqual(os.path.normpath(paths.echo_root()), os.path.normpath(expected))

    def test_env_override_wins_and_roots_follow(self):
        tmp = tempfile.mkdtemp(prefix="echo-root-")
        os.environ["ECHO_ROOT"] = tmp
        os.environ.pop("ECHO_DATA", None)
        try:
            self.assertEqual(os.path.normpath(paths.echo_root()), os.path.normpath(tmp))
            # 相对路径补全以覆盖后的根为基准
            self.assertEqual(paths.resolve("models"),
                             os.path.normpath(os.path.join(tmp, "models")))
            # 没单独指定 ECHO_DATA 时，数据根跟着安装根走（Windows 默认 {ECHO}/data）
            self.assertEqual(os.path.normpath(paths.data_root()),
                             os.path.normpath(os.path.join(tmp, "data")))
            # 显式 ECHO_DATA 依然优先
            other = tempfile.mkdtemp(prefix="echo-data-")
            os.environ["ECHO_DATA"] = other
            self.assertEqual(os.path.normpath(paths.data_root()), os.path.normpath(other))
        finally:
            os.environ.pop("ECHO_ROOT", None)
            os.environ.pop("ECHO_DATA", None)

    def test_config_placeholders_go_through_the_paths_layer(self):
        """``config.expand_path()`` 的 {ECHO}/{DATA} 必须与路径层同一个来源。

        各推一遍的后果很实在：设了 ECHO_ROOT 时 {ECHO} 指向老树、paths 指向新树
        （split-brain）；{DATA} 写成 ``join(ECHO_ROOT, "data")`` 则 macOS 会把数据根
        算进 .app 里，而 D18 要求写到 ``~/Library/Application Support/ECHO``。
        """
        from app import config
        os.environ.pop("ECHO_DATA", None)
        try:
            tmp = tempfile.mkdtemp(prefix="echo-root-")
            os.environ["ECHO_ROOT"] = tmp
            self.assertEqual(os.path.normpath(config.expand_path("{ECHO}/models")),
                             os.path.normpath(os.path.join(tmp, "models")))
            other = tempfile.mkdtemp(prefix="echo-data-")
            os.environ["ECHO_DATA"] = other
            self.assertEqual(os.path.normpath(config.expand_path("{DATA}/meetings")),
                             os.path.normpath(os.path.join(other, "meetings")))
        finally:
            os.environ.pop("ECHO_ROOT", None)
            os.environ.pop("ECHO_DATA", None)
        # 老行为不能变：非字符串、无占位符一律原样返回
        self.assertEqual(config.expand_path(""), "")
        self.assertIsNone(config.expand_path(None))

    def test_data_root_is_resolved_by_the_paths_layer(self):
        """``db.py`` / ``manager.py`` 的数据根必须来自 paths 层，不许各推导一遍。

        为什么用源码断言而不是断言运行期值：现有测试用"给模块属性赋值"来隔离数据
        目录（值会被别的用例改过），断言运行期常量会变成顺序相关的假红。半成品状态
        比没做更危险，所以把"安装根/数据根的推导只有一处"钉在源码上。
        """
        for rel in ("db.py", "manager.py"):
            p = os.path.join(paths.echo_root(), "app", rel)
            with open(p, encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("paths.data_root()", src, "%s 应通过路径层取数据根" % rel)
            for bad in ('os.path.join(BASE_DIR, "data")',
                        'os.path.dirname(BASE_DIR), "data"'):
                self.assertNotIn(bad, src, "%s 里不应再自己推导数据根（%s）" % (rel, bad))

    def test_captures_and_beeps_roots_come_from_the_layer(self):
        """录音落盘走**数据根**、提示音 wav 走**安装根**：两处都不许自己推导。

        这两个是"根用错"的典型：录音写进安装目录 → macOS 上落进 .app（D18 不允许）；
        提示音 wav 是代码资产，跟着安装根走才对（数据根搬走它也得还在）。
        同样用源码断言，理由见上一个用例。
        """
        checks = (
            ("assistant.py", "paths.data_root()",
             ('os.path.join(BASE_DIR, "data", "captures")',)),
            ("audio/tts.py", "paths.echo_root()",
             ("os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",)),
        )
        for rel, must, must_not in checks:
            with open(os.path.join(paths.echo_root(), "app", rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn(must, src, "%s 应通过路径层取根" % rel)
            for bad in must_not:
                self.assertNotIn(bad, src, "%s 里不应再自己推导（%s）" % (rel, bad))

    def test_converted_modules_take_the_install_root_from_the_layer(self):
        """已收口的模块：不许再有 ``BASE_DIR = ...dirname...(__file__)`` 那一行，且必须
        从路径层取根。这一行就是"每个模块各推一遍安装根"的指纹。

        全量正式守卫已落在 ``tests/test_path_seam.py``（扫 app/ 全部，白名单只留
        app/paths.py 与 app/platform/）；本用例保留为这 11 个模块的定点回归。
        """
        converted = ("config.py", "db.py", "manager.py", "assistant.py", "audio/tts.py",
                     "main.py", "meeting.py", "audio/stt.py", "runtime.py", "modelinfo.py",
                     "llm_router.py")
        for rel in converted:
            with open(os.path.join(paths.echo_root(), "app", rel), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn("paths.echo_root()", src, "%s 应从路径层取安装根" % rel)
            bad = [ln.strip() for ln in src.splitlines()
                   if "BASE_DIR" in ln and "dirname" in ln]
            self.assertEqual(bad, [], "%s 里仍有自己推导的 BASE_DIR：%s" % (rel, bad))


class ModelSubdirsFollowModelsDir(unittest.TestCase):
    """``wake`` 的 KWS 目录与 ``diarize`` 的四个 pyannote 目录必须跟随 modelsDir。

    这两处在 2.0 之前是模块级常量（``{ECHO}/models/…``）：用户改了 modelsDir 也不
    生效，而 ``config.py`` 里 modelsDir 的说明明确承诺"唤醒词 / pyannote"随它走
    （D20/D21）——所以这是**真 bug**，不是纯一致性。断言的是"请求时解析"，
    因此直接替换 ``paths._settings_get`` 模拟配置，不依赖真实 settings。
    """

    def test_model_subdirs_follow_models_dir(self):
        from app.audio import diarize, wake

        tmp = tempfile.mkdtemp(prefix="echo-models-")
        old = paths._settings_get
        paths._settings_get = lambda name: tmp if name == "modelsDir" else ""
        try:
            self.assertEqual(wake.kws_model_dir(),
                             os.path.join(tmp, "wakeword", "kws-zh-en-3m"))
            self.assertEqual(diarize.pyannote_dir(), os.path.join(tmp, "pyannote"))
            self.assertEqual(diarize.segmentation_dir(),
                             os.path.join(tmp, "pyannote", "pyannote-segmentation-3.0-local"))
            self.assertEqual(diarize.embedding_dir(),
                             os.path.join(tmp, "pyannote", "pyannote-wespeaker-local"))
            self.assertEqual(diarize.plda_dir(),
                             os.path.join(tmp, "pyannote", "pyannote-plda-local", "plda"))
        finally:
            paths._settings_get = old


class HuggingFaceHomeTests(unittest.TestCase):
    """HF_HOME 的落点约定（D30）：只有用户显式配置了 modelsDir 才覆盖。

    默认安装下不覆盖 = 零行为变化（HF_HOME 已经是 {ECHO}/models）；一旦覆盖错，
    huggingface 会找不到已下好的权重并**重新下载**，所以这条语义必须钉住。
    """

    def test_hf_home_is_a_noop_unless_models_dir_is_configured(self):
        old = paths._settings_get
        paths._settings_get = lambda name: ""
        try:
            self.assertEqual(paths.hf_home(), "", "未配置 modelsDir 时不得改 HF_HOME")
        finally:
            paths._settings_get = old

    def test_hf_home_follows_a_configured_models_dir(self):
        from app.audio import stt

        tmp = tempfile.mkdtemp(prefix="echo-hf-")
        old = paths._settings_get
        paths._settings_get = lambda name: tmp if name == "modelsDir" else ""
        try:
            self.assertEqual(paths.hf_home(), os.path.normpath(tmp))
            # modelinfo / stt 与路径层必须得到**同一个**缓存根（消灭“谁先 import 谁生效”）
            self.assertEqual(stt.models_dir(), paths.hf_home())
        finally:
            paths._settings_get = old


if __name__ == "__main__":
    unittest.main()
