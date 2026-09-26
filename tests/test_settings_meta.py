# -*- coding: utf-8 -*-
"""配置项元数据测试（面板渲染依赖它）。

背景（Round 5 的疑问）：`/api/settings` 里的 `grp` / `label` 来自**数据库元数据**
（由 `settings.seed_defaults()` 在启动时同步），不是直接读 `config.DEFAULTS`。
所以新增配置项时，"声明的 grp/label" 与"面板看到的 grp/label"之间隔了一次引导。

Round 6 实测结论：只要走过 `seed_defaults()`，**即便是已存在的键**（例如用户先改过一次
`settings.update()`），`sync_setting_meta` 分支也会把 grp/label 补齐 —— 没有 bug。
本文件把这条结论钉住，免得以后有人改引导逻辑时把面板搞成"通用 + 无标签"。

数据库重定向到临时目录，不碰真实 data/。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

import app.db as db                                                  # noqa: E402
from app.api import router                                           # noqa: E402
from app.config import settings                                      # noqa: E402


class SettingsMetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-meta-")
        cls._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()                     # 等价于 ECHO 每次启动做的事
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        # 进程级配置缓存（`settings` 是单例，`_cache` 挂在实例上）**不受** db 补丁约束：
        # 本类从临时库读出去的设置会留在缓存里给后面的模块。恢复库之后一并丢掉
        # （详见 tests/test_settings_wiring.py 的 `_drop_settings_cache()`）。
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _by_key(self):
        data = self.client.get("/api/settings").json()
        items = data.get("settings") or data.get("items") or []
        return {i["key"]: i for i in items}

    def test_paths_items_land_in_the_paths_group(self):
        by = self._by_key()
        for key in ("meetingsDir", "modelsDir"):
            self.assertIn(key, by, "%s 必须出现在 /api/settings 里" % key)
            self.assertEqual(by[key]["grp"], "paths",
                             "%s 应归入「存储路径」分组（面板按 grp 分组渲染）" % key)
            self.assertTrue(by[key]["label"], "%s 必须有非空 label" % key)
            self.assertEqual(by[key]["value"], "", "%s 默认值应为空（留空 = 平台默认）" % key)

    def test_meta_survives_a_pre_existing_row(self):
        """键先被写过（用户改过一次）时，引导仍要把 grp/label 补对。"""
        db.set_setting("meetingsDir", "", grp="general", label="")
        settings.seed_defaults()
        self.assertEqual(self._by_key()["meetingsDir"]["grp"], "paths")

    def test_every_active_default_has_label_and_group(self):
        from app import config
        for key, meta in config.DEFAULTS.items():
            if meta.get("deprecated"):
                continue
            self.assertTrue(meta.get("label"), "%s 缺少 label（面板会显示空标题）" % key)
            self.assertTrue(meta.get("grp"), "%s 缺少 grp（面板会归到未分组）" % key)


if __name__ == "__main__":
    unittest.main()
