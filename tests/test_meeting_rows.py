# -*- coding: utf-8 -*-
"""转写行的形状契约：进 db.add_lines 之前必须是 5 元组。

2026-09-23 事故：`_transcribe_impl` 里"分离关闭"才补空说话人（写在 `if diarize:` 的
`else` 分支里），于是**分离一抛异常**（运行时缺 speechbrain）4 元组就一路走到
`db.add_lines`，报 `ValueError: not enough values to unpack (expected 5, got 4)`,
整场转写（几十分钟的 ASR）全部作废、状态变 error。这批用例把形状钉死。
"""
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app.db as db
from app import meeting


class EnsureSpeakerColumnTests(unittest.TestCase):
    def test_four_tuples_get_an_empty_speaker(self):
        rows = [(1, 0.0, 1.5, "第一句"), (1, 1.5, 3.0, "第二句")]
        self.assertEqual(
            [(1, 0.0, 1.5, "", "第一句"), (1, 1.5, 3.0, "", "第二句")],
            meeting._ensure_speaker_column(rows))

    def test_five_tuples_are_left_alone(self):
        rows = [(1, 0.0, 1.5, "S1", "第一句"), (2, 0.0, 2.0, "S2", "第二句")]
        self.assertEqual(rows, meeting._ensure_speaker_column(rows))

    def test_mixed_shapes_are_normalised(self):
        rows = [(1, 0.0, 1.0, "S1", "有说话人"), (1, 1.0, 2.0, "没有")]
        got = meeting._ensure_speaker_column(rows)
        self.assertEqual([len(r) for r in got], [5, 5])
        self.assertEqual(got[1][3], "")

    def test_empty_stays_empty(self):
        self.assertEqual([], meeting._ensure_speaker_column([]))


class AssignSpeakersThenNormaliseTests(unittest.TestCase):
    """`_assign_speakers(rows, [])`（= 分离失败/无 turn）之后仍然必须是 5 元组。"""

    def test_no_turns_yields_five_tuples(self):
        got = meeting._assign_speakers([(1, 0.0, 1.0, "句子")], [])
        self.assertEqual([(1, 0.0, 1.0, "", "句子")], got)

    def test_turns_are_attached(self):
        got = meeting._assign_speakers([(1, 0.0, 10.0, "句子")], [(0.0, 9.0, "S1")])
        self.assertEqual([(1, 0.0, 10.0, "S1", "句子")], got)


class AddLinesAcceptsNormalisedRowsTests(unittest.TestCase):
    """真正落库一次：归一后的行必须能被 db.add_lines 接受，且说话人列落对。"""

    def test_normalised_rows_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = f"{tmp}/echo.db"
            with patch.object(db, "DATA_DIR", tmp), patch.object(db, "DB_FILE", db_file):
                db.init()
                mid = db.create_meeting(name="2026-09-23_00-00-00")
                rows = meeting._ensure_speaker_column(
                    [(1, 0.0, 1.5, "第一句"), (1, 1.5, 3.0, "第二句")])
                db.add_lines(mid, rows)
                got = db.get_lines(mid)
        self.assertEqual(2, len(got))
        self.assertEqual("第一句", got[0]["text"])

    def test_raw_four_tuples_are_rejected(self):
        """钉住契约本身：db 层只认 5 元组 —— 所以归一必须发生在它之前。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = f"{tmp}/echo.db"
            with patch.object(db, "DATA_DIR", tmp), patch.object(db, "DB_FILE", db_file):
                db.init()
                mid = db.create_meeting(name="2026-09-23_00-00-01")
                with self.assertRaises(ValueError):
                    db.add_lines(mid, [(1, 0.0, 1.5, "只有四元")])


if __name__ == "__main__":
    unittest.main()
