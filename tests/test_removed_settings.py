# -*- coding: utf-8 -*-
"""已彻底下线的设置项：启动时必须从旧库里清掉（2026-10-23 真机 bug）。

现场：一台从旧版本升级到 2.1.8 的机器，设置页「未分类」里多出四条垃圾配置
（语言模型 provider / 在线 LLM 地址 / 密钥 / 模型名）。根因不是升级脚本写坏了，
而是**旧库里还留着 `providerLlm*` 的行**：这些键随"ECHO 不直连 LLM"一起从
DEFAULTS 删掉了，但 `Settings.all()` 是按库行出的，于是 unknown 行被原样返回，
面板的未归类兜底把它们当设置显示了出来。

修法：`REMOVED_SETTINGS` + `seed_defaults()` 每次启动清一次（幂等）。
"""
import os
import shutil
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, _ROOT)

import app.db as db                                              # noqa: E402
from app.config import REMOVED_SETTINGS, settings                # noqa: E402


class RemovedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-removed-")
        self._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        # 造出旧库里的四条（模拟从更早版本升级上来的库）
        for k in REMOVED_SETTINGS:
            db.set_setting(k, "old-value", grp="provider", label="旧项", description="")
        # 一个正常项：清理不许误伤
        db.set_setting("sttModel", "sensevoice-onnx", grp="model",
                       label="转写引擎", description="")
        settings._drop_cache()

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old
        settings._drop_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_they_are_listed_as_removed(self):
        """名单本身就是契约：providerLlm* 随 LLM 收敛下线，wake* 随语音唤醒下线。"""
        self.assertEqual(
            set(REMOVED_SETTINGS),
            {
                "providerLlm", "providerLlmBaseUrl", "providerLlmApiKey", "providerLlmModel",
                "wakeAliases", "wakeConfirmN", "wakeConfirmX", "wakeCooldownSec",
                "wakeEnabled", "wakeEngine", "wakeKeywords", "wakePaused",
                "wakeSilenceFloor", "wakeThreshold",
            })

    def test_startup_deletes_the_rows(self):
        settings.seed_defaults()
        for k in REMOVED_SETTINGS:
            self.assertIsNone(db.get_setting(k), "%s 应被清理（否则面板会多出未分类项）" % k)

    def test_all_no_longer_serves_them(self):
        settings.seed_defaults()
        keys = {r["key"] for r in settings.all()}
        self.assertFalse(keys & set(REMOVED_SETTINGS),
                         "all() 还在返回已下线项：%s" % sorted(keys & set(REMOVED_SETTINGS)))

    def test_unknown_key_is_never_served_to_the_panel(self):
        """兜底：未来再删键、甚至忘了登记 REMOVED_SETTINGS，也不能掉进「未归类」。"""
        db.set_setting("someRetiredKeyFromTheFuture", "x", grp="", label="旧项", description="")
        settings._drop_cache()
        keys = {r["key"] for r in settings.all()}
        self.assertNotIn("someRetiredKeyFromTheFuture", keys)

    def test_a_normal_setting_is_not_touched(self):
        settings.seed_defaults()
        self.assertEqual(db.get_setting("sttModel"), "sensevoice-onnx")

    def test_it_is_idempotent(self):
        settings.seed_defaults()
        settings.seed_defaults()          # 第二次启动：没有行可删，不许报错
        self.assertEqual([r["key"] for r in settings.all() if r["key"] in REMOVED_SETTINGS], [])


if __name__ == "__main__":
    unittest.main()
