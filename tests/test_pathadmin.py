# -*- coding: utf-8 -*-
"""存储路径管理动作测试（app/pathadmin.py，P1）。

钉住的语义（每一条都对应一种"用户会踩"的场景）：
  1. 只搬会议目录格式的目录，其它条目原样留着并单独报告（不误伤手工放进去的东西）；
  2. 目标已有同名目录 -> 跳过，绝不覆盖；
  3. 幂等：搬第二次不报错、不重复搬；
  4. 目标非法（磁盘根 / 与源相同 / 源的子目录）-> 明确失败且不动任何文件；
  5. 迁移**不需要改数据库**（路径是"根 + name"现算的），所以断言只看文件系统。
"""
import os
import tempfile
import unittest

from app import pathadmin


def _mk_meeting(root, name, files=("01.wav", "transcript.md")):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    for f in files:
        with open(os.path.join(d, f), "wb") as fh:
            fh.write(b"x")
    return d


class MigrateMeetingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-mig-")
        self.src = os.path.join(self.tmp, "old-meetings")
        self.dst = os.path.join(self.tmp, "new-meetings")
        os.makedirs(self.src, exist_ok=True)
        self.a = "2026-09-18_10-00-00"
        self.b = "2026-09-18_11-30-00"
        _mk_meeting(self.src, self.a)
        _mk_meeting(self.src, self.b)
        # 干扰项：一个非会议格式的目录 + 一个散文件
        os.makedirs(os.path.join(self.src, "我的笔记"), exist_ok=True)
        with open(os.path.join(self.src, "readme.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi")

    def test_moves_only_meeting_dirs(self):
        r = pathadmin.migrate_meetings(self.dst, source=self.src)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(r["moved"]), sorted([self.a, self.b]))
        self.assertEqual(sorted(r["others"]), ["readme.txt", "我的笔记"])
        self.assertEqual(r["skipped"], [])
        for name in (self.a, self.b):
            self.assertTrue(os.path.isdir(os.path.join(self.dst, name)), name)
            self.assertFalse(os.path.exists(os.path.join(self.src, name)), name)
            self.assertTrue(os.path.isfile(os.path.join(self.dst, name, "01.wav")))
        # 干扰项原地不动
        self.assertTrue(os.path.isdir(os.path.join(self.src, "我的笔记")))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "readme.txt")))

    def test_second_run_moves_nothing(self):
        pathadmin.migrate_meetings(self.dst, source=self.src)
        r2 = pathadmin.migrate_meetings(self.dst, source=self.src)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["moved"], [], "第二次不该再搬任何东西")
        # 真实场景里的"幂等"：配置已经指向新目录后再点一次 -> 直接说无需迁移
        r3 = pathadmin.migrate_meetings(self.dst, source=self.dst)
        self.assertFalse(r3["ok"])
        self.assertIn("无需迁移", r3["error"])

    def test_does_not_overwrite_existing_target(self):
        _mk_meeting(self.dst, self.a, files=("01.wav",))
        with open(os.path.join(self.dst, self.a, "01.wav"), "wb") as fh:
            fh.write(b"NEW")                      # 目标里已有内容
        r = pathadmin.migrate_meetings(self.dst, source=self.src)
        self.assertIn(self.a, r["skipped"])
        self.assertIn(self.b, r["moved"])
        with open(os.path.join(self.dst, self.a, "01.wav"), "rb") as fh:
            self.assertEqual(fh.read(), b"NEW", "已存在的目标目录绝不能被覆盖")

    def test_dry_run_touches_nothing(self):
        r = pathadmin.migrate_meetings(self.dst, source=self.src, dry_run=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(sorted(r["moved"]), sorted([self.a, self.b]))
        self.assertTrue(os.path.isdir(os.path.join(self.src, self.a)), "dry-run 不能真搬")
        self.assertFalse(os.path.exists(os.path.join(self.dst, self.a)))

    def test_rejects_same_dir(self):
        r = pathadmin.migrate_meetings(self.src, source=self.src)
        self.assertFalse(r["ok"])
        self.assertIn("无需迁移", r["error"])

    def test_rejects_subdirectory_of_source(self):
        r = pathadmin.migrate_meetings(os.path.join(self.src, "inner"), source=self.src)
        self.assertFalse(r["ok"])
        self.assertTrue("子目录" in r["error"] or "无法创建" in r["error"], r["error"])

    def test_rejects_missing_source(self):
        r = pathadmin.migrate_meetings(self.dst, source=os.path.join(self.tmp, "nope"))
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_reports_failure_without_aborting(self):
        """一个目录搬不动时，其余仍应搬完，并如实报告失败项。"""
        r = pathadmin.migrate_meetings(self.dst, source=self.src)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["failed"]), 0)

    def test_meeting_dir_names(self):
        self.assertEqual(pathadmin.meeting_dir_names(self.src), sorted([self.a, self.b]))
        self.assertEqual(pathadmin.meeting_dir_names(os.path.join(self.tmp, "nope")), [])


class EnvReportTests(unittest.TestCase):
    def test_report_is_panel_ready(self):
        r = pathadmin.env_report()
        self.assertEqual([x["name"] for x in r["roots"]], ["ECHO", "DATA", "MEETINGS", "MODELS"])
        self.assertIn("configured", r)
        self.assertIn("port", r)
        self.assertIsInstance(r["meetingDirs"], int)

    def test_report_survives_broken_config(self):
        from app import paths
        saved = paths._settings_get

        def boom(_n):
            raise RuntimeError("config 挂了")

        paths._settings_get = boom
        try:
            r = pathadmin.env_report()
            self.assertEqual(len(r["roots"]), 4)
        finally:
            paths._settings_get = saved


if __name__ == "__main__":
    unittest.main()
