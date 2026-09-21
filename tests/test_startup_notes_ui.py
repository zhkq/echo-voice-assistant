# -*- coding: utf-8 -*-
"""A3 的接线护栏：自愈提示要从后端一路走到面板。

前端没有构建期检查，所以按仓库惯例用**字符串断言**守住接线
（同 tests/test_wizard_ui.py / tests/test_settings_wiring.py 的做法）：
漏掉任何一段（后端不记、接口不发、面板不画），用户就看不到那句人话。
"""

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class StartupNotesWiringTests(unittest.TestCase):
    def test_backend_exposes_notes_in_status(self):
        api = _read("app", "api.py")
        self.assertIn('"startupNotes": db.startup_notes()', api)

    def test_main_heals_before_db_init(self):
        main = _read("app", "main.py")
        heal = main.index("db.heal_stale_wal()")
        init = main.index("db.init()", heal)           # 第一个"heal 之后的 init"
        self.assertLess(heal, init, "自愈必须在 db.init() 之前（且已在单实例锁之后）")
        # 自愈不能挡启动：异常要被吞掉
        self.assertIn("不影响启动", main)

    def test_panel_renders_notes_once_and_dismisses(self):
        js = _read("web", "app.js")
        self.assertIn("function renderStartupNotes(st)", js)
        self.assertIn("renderStartupNotes(st);", js)  # 必须在 status 刷新里被调用
        self.assertIn("st.startupNotes", js)
        # "只提示一次"的行为靠这个哨兵变量；顺带给一个人话出口
        self.assertIn("_startupNotesSeen", js)
        self.assertIn("知道了", js)

    def test_wal_heal_is_documented_in_repo(self):
        doc = _read("docs", "安装反馈-20260922-处置.md")
        self.assertIn("A3", doc)


if __name__ == "__main__":
    unittest.main()
