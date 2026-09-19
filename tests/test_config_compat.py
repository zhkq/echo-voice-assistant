# -*- coding: utf-8 -*-
"""配置兼容测试（§13.8 安全网第 4 项，P3 前置）

硬要求（原文）：**库里用户显式改过的值，不得被 2.0 的新默认值/平台默认值覆盖。**

这条比看上去更容易踩：
  * `seed_defaults()` 每次启动都跑，写错分支就会把用户配置刷回默认；
  * `DEFAULT_MIGRATIONS` 是"旧默认值 → 新默认值"的改写机制，判据必须严格是
    "当前值仍等于**旧默认值**"，用户改过的一律不动；
  * 2.0 新增的 `meetingsDir` / `modelsDir` 是**空串默认 = 用平台默认值**，
    不能把它们当成"非空默认值"写进去，否则用户一设就又变回默认（这正是
    `panelOpenMode` 当年踩过的坑，见 DEFAULT_MIGRATIONS 的注释）；
  * 类型强转（bool False / list / int）在往返里不能丢：`False` 掉成默认 `True`
    会让"关掉语音简报"失效。

性能说明：一次 `seed_defaults()` 要写 ~90 行、约 0.5~1.1 秒（每行一个短连接 +
WAL），所以"已经种好默认值的库"用**模板库复制**得到（setUpModule 只种一次），
不再让每个用例各跑一遍 seed —— 否则这个文件就要跑 40 秒。

DB 重定向到临时目录，不碰真实 data/。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                          # noqa: E402
from app import paths                                        # noqa: E402
from app.config import (DEFAULT_MIGRATIONS, DEFAULTS,        # noqa: E402
                        Settings, settings)

_TEMPLATE = {"dir": None, "db": None, "old": None}


def setUpModule():
    """种一份"默认值已入库"的模板库（等价于老用户跑过一次的库）。"""
    tmp = tempfile.mkdtemp(prefix="echo-config-template-")
    old = (db.DATA_DIR, db.DB_FILE)
    db.DATA_DIR = tmp
    db.DB_FILE = os.path.join(tmp, "template.db")
    db.init()
    settings.seed_defaults()
    settings._cache = None
    _TEMPLATE.update(dir=tmp, db=db.DB_FILE, old=old)
    db.DATA_DIR, db.DB_FILE = old


def tearDownModule():
    if _TEMPLATE["dir"]:
        shutil.rmtree(_TEMPLATE["dir"], ignore_errors=True)


class _ConfigTestCase(unittest.TestCase):
    """每个用例一个全新的库；`seeded = True` 时从模板复制（已是"种过默认值"的状态）。"""

    seeded = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-config-compat-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        for suffix in ("", "-wal", "-shm"):
            path = db.DB_FILE + suffix
            if os.path.exists(path):
                os.remove(path)
        if self.seeded:
            shutil.copy2(_TEMPLATE["db"], db.DB_FILE)
        else:
            db.init()
        settings._cache = None
        self.addCleanup(setattr, settings, "_cache", None)


class SeededConfigTestCase(_ConfigTestCase):
    seeded = True


class SeedDefaultsTests(_ConfigTestCase):
    def test_first_seed_writes_every_default(self):
        settings.seed_defaults()
        for key, meta in DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(db.get_setting(key), meta["value"])
        self.assertEqual(db.get_setting("meetingsDir"), "")
        self.assertEqual(db.get_setting("modelsDir"), "")
        self.assertEqual(db.get_setting("serverPort"), 8970)

    def test_user_values_are_never_overwritten(self):
        """核心断言：用户改过的值，重新 seed 也不许动。"""
        user = {
            "sttModel": "small", "device": "cpu", "serverPort": 18060,
            "panelOpenMode": "browser", "sendEnvContext": False,
            "triggerKeys": ["vol_up", "next"], "meetingsDir": "D:\\我的会议",
            "modelsDir": "{ECHO}/models-big",
            "minimalReplyHint": "我自己的回复要求", "meetingWorkspace": "D:\\会议工作区",
        }
        settings.update(user)
        settings.seed_defaults()
        for key, expected in user.items():
            with self.subTest(key=key):
                self.assertEqual(db.get_setting(key), expected, "库里存的原始值")
        # 读出来的值走占位符展开，所以单独确认一次（写进去的是占位符，读出来是绝对路径）
        self.assertEqual(settings.get("modelsDir"),
                         os.path.normpath(os.path.join(paths.echo_root(), "models-big")))
        self.assertIs(settings.get("sendEnvContext"), False)
        self.assertEqual(settings.get("triggerKeys"), ["vol_up", "next"])

    def test_seed_fills_only_missing_keys(self):
        """1.x 库里没有 2.0 新增的键 → seed 补上，但不能顺手改别的。"""
        db.set_setting("sttModel", "medium", grp="voice", label="命令转写引擎")
        settings.seed_defaults()
        self.assertEqual(db.get_setting("sttModel"), "medium")
        self.assertEqual(db.get_setting("meetingsDir"), "")
        self.assertEqual(db.get_setting("modelsDir"), "")

    def test_seed_syncs_panel_metadata_without_touching_value(self):
        db.set_setting("meetingsDir", "D:\\会议", grp="general", label="旧标签")
        settings.seed_defaults()
        row = {r["key"]: r for r in db.all_settings()}["meetingsDir"]
        self.assertEqual(row["value"], "D:\\会议", "value 必须保留")
        self.assertEqual(row["grp"], "paths", "元数据要同步成新的")
        self.assertEqual(row["label"], DEFAULTS["meetingsDir"]["label"])


class SeededSeedTests(SeededConfigTestCase):
    """在"已经种过默认值"的库上再 seed（= 老用户升级后的第一次启动）。"""

    def test_reseeding_is_idempotent(self):
        before = {r["key"]: r["value"] for r in db.all_settings()}
        settings.seed_defaults()
        after = {r["key"]: r["value"] for r in db.all_settings()}
        self.assertEqual(after, before)

    def test_paths_follow_the_user_value_after_reseeding(self):
        """跨层确认：seed 之后路径层仍然读到用户指定的会议目录。"""
        with tempfile.TemporaryDirectory() as custom:
            settings.update({"meetingsDir": custom})
            settings.seed_defaults()
            self.assertEqual(paths.meetings_root(), os.path.realpath(custom))


class MigrationTests(SeededConfigTestCase):
    def test_old_defaults_are_migrated(self):
        for key, (old, _new) in DEFAULT_MIGRATIONS.items():
            db.set_setting(key, old)
        settings.seed_defaults()
        for key, (_old, new) in DEFAULT_MIGRATIONS.items():
            with self.subTest(key=key):
                self.assertEqual(db.get_setting(key), new)

    def test_user_edited_value_is_not_migrated(self):
        """判据是"仍等于旧默认值"，不是"存在这一行"。"""
        cases = {"panelOpenMode": "browser",
                 "minimalReplyHint": "用户自己写的文案",
                 "meetingWorkspace": "D:\\我的会议工作区"}
        settings.update(cases)
        settings.seed_defaults()
        for key, value in cases.items():
            with self.subTest(key=key):
                self.assertEqual(db.get_setting(key), value)

    def test_migration_does_not_repeat_forever(self):
        """迁移后新值本身不能被当成"旧默认值"再改一次（尤其是新默认=空串的情况）。"""
        settings.seed_defaults()
        first = {k: db.get_setting(k) for k in DEFAULT_MIGRATIONS}
        settings.seed_defaults()
        self.assertEqual({k: db.get_setting(k) for k in DEFAULT_MIGRATIONS}, first)

    def test_every_migration_targets_a_real_key(self):
        for key in DEFAULT_MIGRATIONS:
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS)


class TypeRoundTripTests(SeededConfigTestCase):
    def test_false_is_not_lost(self):
        """False 掉成默认 True 会让"关掉某功能"静默失效。"""
        for key in ("sendEnvContext", "voiceBrief", "beepOnStart", "panelAutoStart"):
            with self.subTest(key=key):
                settings.update({key: False})
                self.assertIs(settings.get(key), False)
                settings.update({key: "false"})     # 面板可能提交字符串
                self.assertIs(settings.get(key), False)

    def test_true_string_variants(self):
        for value in ("1", "true", "yes", "on", True):
            with self.subTest(value=value):
                settings.update({"sendEnvContext": value})
                self.assertIs(settings.get("sendEnvContext"), True)
        settings.update({"sendEnvContext": "no"})
        self.assertIs(settings.get("sendEnvContext"), False)

    def test_numeric_coercion(self):
        settings.update({"serverPort": "18060", "silenceThreshold": "0.25",
                         "meetingSegmentMinutes": 15.9})
        self.assertEqual(settings.get("serverPort"), 18060)
        self.assertIsInstance(settings.get("serverPort"), int)
        self.assertAlmostEqual(settings.get("silenceThreshold"), 0.25)
        self.assertEqual(settings.get("meetingSegmentMinutes"), 15)

    def test_list_coercion(self):
        settings.update({"triggerKeys": "vol_up, play_pause , next"})
        self.assertEqual(settings.get("triggerKeys"), ["vol_up", "play_pause", "next"])
        settings.update({"triggerKeys": ["next"]})
        self.assertEqual(settings.get("triggerKeys"), ["next"])

    def test_unknown_keys_are_dropped(self):
        cleaned = settings.update({"notARealKey": 1, "serverPort": 18060})
        self.assertEqual(cleaned, {"serverPort": 18060})
        self.assertIsNone(db.get_setting("notARealKey"))

    def test_bad_numeric_value_is_skipped_not_fatal(self):
        cleaned = settings.update({"serverPort": "not-a-port"})
        self.assertEqual(cleaned, {})
        self.assertEqual(settings.get("serverPort"), 8970, "保持原值")

    def test_missing_row_falls_back_to_default(self):
        db._exec("DELETE FROM settings WHERE key='device'")
        settings._cache = None
        self.assertEqual(settings.get("device"), DEFAULTS["device"]["value"])

    def test_empty_string_value_is_a_real_value_not_a_missing_one(self):
        """空串是"用平台默认"的语义（meetingsDir/modelsDir），不能被当成没设置。"""
        settings.update({"meetingsDir": ""})
        self.assertEqual(settings.get("meetingsDir"), "")
        self.assertIn("meetingsDir", {r["key"] for r in db.all_settings()})


class PanelVisibilityTests(SeededConfigTestCase):
    def test_deprecated_and_hidden_keys_stay_out_of_the_panel(self):
        keys = {r["key"] for r in settings.all()}
        for key, meta in DEFAULTS.items():
            with self.subTest(key=key):
                if meta.get("deprecated") or meta.get("hidden"):
                    self.assertNotIn(key, keys)
                else:
                    self.assertIn(key, keys)

    def test_deprecated_keys_still_readable(self):
        for key in settings.deprecated_keys():
            with self.subTest(key=key):
                self.assertEqual(settings.get(key), DEFAULTS[key]["value"],
                                 "弃用只是不展示，值仍要能读（老代码/迁移比对要用）")

    def test_deprecated_rows_are_not_deleted_from_the_library(self):
        stored = {r["key"] for r in db.all_settings()}
        for key in settings.deprecated_keys():
            with self.subTest(key=key):
                self.assertIn(key, stored)


class ResetTests(SeededConfigTestCase):
    def test_reset_one_key_restores_default(self):
        settings.update({"sttModel": "medium", "device": "cpu"})
        settings.reset("sttModel")
        self.assertEqual(settings.get("sttModel"), DEFAULTS["sttModel"]["value"])
        self.assertEqual(settings.get("device"), "cpu", "只重置指定项")

    def test_reset_all_restores_every_default(self):
        settings.update({"sttModel": "medium", "serverPort": 18060, "meetingsDir": "D:\\x"})
        settings.reset()
        self.assertEqual(settings.get("sttModel"), DEFAULTS["sttModel"]["value"])
        self.assertEqual(settings.get("serverPort"), 8970)
        self.assertEqual(settings.get("meetingsDir"), "")

    def test_reset_unknown_key_is_a_noop(self):
        settings.reset("notARealKey")
        self.assertEqual(settings.get("serverPort"), 8970)


class FreshSettingsInstanceTests(SeededConfigTestCase):
    """同一个库、新的 Settings 实例：读出来的值必须与旧实例一致（重启等价）。"""

    def test_new_instance_reads_the_same_values(self):
        settings.update({"sttModel": "medium", "meetingsDir": "D:\\会议"})
        other = Settings()
        self.assertIsNone(other._cache, "别在构造时就缓存（配置可能在别处被改）")
        self.assertEqual(other.get("sttModel"), "medium")
        self.assertEqual(other.get("meetingsDir"),
                         os.path.normpath(os.path.join("D:\\", "会议"))
                         if os.name == "nt" else "D:\\会议")


class PlaceholderTests(SeededConfigTestCase):
    def test_placeholders_resolve_at_call_time(self):
        settings.update({"meetingsDir": "{DATA}/meetings", "modelsDir": "{ECHO}/models"})
        self.assertEqual(settings.get("meetingsDir"),
                         os.path.normpath(os.path.join(paths.data_root(), "meetings")))
        self.assertEqual(settings.get("modelsDir"),
                         os.path.normpath(os.path.join(paths.echo_root(), "models")))

    def test_placeholder_result_follows_environment_override(self):
        settings.update({"modelsDir": "{ECHO}/models-x"})
        saved = os.environ.get("ECHO_ROOT")
        other = tempfile.mkdtemp(prefix="echo-root-override-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        if saved is None:
            self.addCleanup(os.environ.pop, "ECHO_ROOT", None)
        else:
            self.addCleanup(os.environ.__setitem__, "ECHO_ROOT", saved)
        os.environ["ECHO_ROOT"] = other
        with patch.object(db, "DATA_DIR", db.DATA_DIR), patch.object(db, "DB_FILE", db.DB_FILE):
            self.assertEqual(settings.get("modelsDir"),
                             os.path.normpath(os.path.join(other, "models-x")))


if __name__ == "__main__":
    unittest.main()
