# -*- coding: utf-8 -*-
"""A3：启动时收拾被强杀留下的 echo.db-wal / -shm（同事 2026-09-21 反馈）。

同事的原话：关机后重开，卡在 `Waiting for application startup`。排查发现
`data/echo.db-wal`（和 `-shm`）是上次被强制结束时留下的。这里钉住三件事：

1. 没有残留时**什么都不做**（不打扰、不开库）；
2. 残留可用时**走 checkpoint 归位**（数据不丢），且不留 `.stale-*` 备份；
3. 残留不可用（库锁死 / 库文件都不在）时**改名留证**，绝不当场删除 —— 并且
   返回一句**人话**，同时进 `startup_notes()` 供面板展示。

另外钉住"幂等 + 不抛异常"：自愈失败也不能反过来挡住启动。
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app.db as db


class HealStaleWalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.db_file = os.path.join(self.tmp, "echo.db")
        self._patches = [
            patch.object(db, "DATA_DIR", self.tmp),
            patch.object(db, "DB_FILE", self.db_file),
        ]
        for p in self._patches:
            p.start()
        # startup_notes 是模块级累积的：测试之间要清干净，否则互相污染
        self._notes = list(db._STARTUP_NOTES)
        db._STARTUP_NOTES.clear()

    def tearDown(self):
        db._STARTUP_NOTES.clear()
        db._STARTUP_NOTES.extend(self._notes)
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    # ---- 1
    def test_no_leftovers_is_a_noop(self):
        self.assertEqual(db.heal_stale_wal(), "")
        self.assertEqual(db.startup_notes(), [])
        self.assertFalse(os.path.exists(self.db_file))   # 连库都不该建

    # ---- 2
    def test_checkpoint_when_wal_is_usable(self):
        db.init()
        keep = db.get_conn()                             # 握住一个连接，WAL 才会留在盘上
        db.add_command("hi")
        wal = self.db_file + "-wal"
        self.assertTrue(os.path.exists(wal))             # 前提：确实有 WAL 残留

        note = db.heal_stale_wal()

        self.assertEqual(note, "")                       # 归位成功就不吓唬用户
        self.assertEqual(db.startup_notes(), [])
        self.assertEqual([p for p in os.listdir(self.tmp) if ".stale-" in p], [])
        keep.close()
        # 数据还在（checkpoint 是落盘，不是清库）
        rows = db._query("SELECT text FROM commands")
        self.assertEqual([r["text"] for r in rows], ["hi"])

    # ---- 3
    def test_locked_wal_is_moved_aside_with_human_note(self):
        db.init()
        wal = self.db_file + "-wal"
        with open(wal, "ab") as f:                       # 造出"半截 WAL"残留
            f.write(b"\x00" * 32)

        def locked_connect(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(db.sqlite3, "connect", locked_connect):
            note = db.heal_stale_wal()

        self.assertIn("已移开备份", note)
        self.assertIn("echo.db-wal.stale-", note)
        self.assertEqual(db.startup_notes(), [note])
        # 原文件已让位、备份还在（留证，不删）
        self.assertFalse(os.path.exists(wal))
        stale = [p for p in os.listdir(self.tmp) if ".stale-" in p]
        self.assertEqual(len(stale), 1)
        # 让位之后库还能正常打开（这才是"不卡启动"）
        self.assertEqual(db.heal_stale_wal(), "")

    # ---- 4
    def test_orphan_wal_without_db_file(self):
        wal = self.db_file + "-wal"
        with open(wal, "ab") as f:
            f.write(b"\x00" * 16)

        note = db.heal_stale_wal()

        self.assertIn("数据库文件不在", note)
        self.assertFalse(os.path.exists(wal))

    # ---- 5
    def test_never_raises_when_replace_fails(self):
        wal = self.db_file + "-wal"
        with open(wal, "ab") as f:
            f.write(b"\x00" * 16)

        def boom(*a, **kw):
            raise OSError("拒绝访问")

        with patch.object(db.os, "replace", boom):
            note = db.heal_stale_wal()

        self.assertIn("移不动", note)
        self.assertIn("手动删除", note)

    # ---- 6
    def test_idempotent(self):
        wal = self.db_file + "-wal"
        with open(wal, "ab") as f:
            f.write(b"\x00" * 16)
        self.assertTrue(db.heal_stale_wal())
        self.assertEqual(db.heal_stale_wal(), "")         # 第二次没东西可做


class StartupNoteTests(unittest.TestCase):
    def setUp(self):
        self._notes = list(db._STARTUP_NOTES)
        db._STARTUP_NOTES.clear()

    def tearDown(self):
        db._STARTUP_NOTES.clear()
        db._STARTUP_NOTES.extend(self._notes)

    def test_notes_are_deduped_and_copied(self):
        db.add_startup_note("一句话")
        db.add_startup_note("一句话")
        db.add_startup_note("")
        self.assertEqual(db.startup_notes(), ["一句话"])
        snap = db.startup_notes()
        snap.append("外部改动")
        self.assertEqual(db.startup_notes(), ["一句话"])    # 返回的是拷贝


if __name__ == "__main__":
    unittest.main()
