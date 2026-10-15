# -*- coding: utf-8 -*-
"""会议录音的「暂停 / 恢复」（手机全屏录音界面上那个「静音」按钮）。

用户 2026-10-15 要的界面里有两个按钮：结束、**静音/恢复**。这条能力在电脑侧，
语义必须写死、不能含糊：

  * 暂停期间**不写盘、不产生音频** —— 所以转写里那段是**空白**，不是一段静音噪声；
  * 采集流**不关**（恢复要立刻，重开设备会有竞态，还会丢开头半秒）；
  * 暂停区间记进 meta 的 `pauses`（`from/to/by`）—— **"少了一段"必须留下痕迹**，
    否则事后谁也说不清那是暂停还是故障。

这一组用例钉的就是上面三条。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio import recorder as recorder_mod                      # noqa: E402
from app import db                                                  # noqa: E402
from app import meeting as meeting_mod                              # noqa: E402


class _FakeStream:
    """假装是麦克风输入流：每次 read 给一小块 PCM，直到被关掉。

    用它就能把 `MeetingRecorder._loop` 真跑起来（**不碰真麦克风**）——
    这正是不装声卡也能验"暂停期间不写盘"的办法。
    """

    def __init__(self, chunk=1600, block=0.02):
        self.chunk = chunk
        self.block = block
        self.device = "fake"
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self, n):
        if self.closed:
            raise RuntimeError("stream closed")
        time.sleep(self.block)
        import numpy as np
        return np.zeros(min(int(n), self.chunk), dtype="int16"), False


class RecorderPauseTests(unittest.TestCase):
    """录音器那一层：暂停 = 不写盘；恢复 = 接着写。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pause-")
        self._orig = recorder_mod.input_stream
        recorder_mod.input_stream = lambda *a, **kw: _FakeStream()
        self.addCleanup(lambda: setattr(recorder_mod, "input_stream", self._orig))

    def test_pause_and_resume_are_idempotent(self):
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=10)
        self.assertTrue(rec.pause(), "第一次暂停该算有变化")
        self.assertFalse(rec.pause(), "第二次暂停该算没变化（幂等）")
        self.assertTrue(rec.paused)
        self.assertTrue(rec.resume(), "第一次恢复该算有变化")
        self.assertFalse(rec.resume(), "第二次恢复该算没变化")
        self.assertFalse(rec.paused)

    def test_paused_frames_are_not_written_and_resume_continues(self):
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=10)
        rec.pause()                       # 一开始就暂停
        rec.start()
        self.assertTrue(rec.wait_started(timeout=3))
        time.sleep(0.25)
        self.assertEqual([], rec.segments, "暂停期间不该产生任何段")
        self.assertEqual([], [f for f in os.listdir(self.tmp) if f.endswith(".wav")],
                         "暂停期间不该写出任何 wav")
        rec.resume()
        deadline = time.time() + 3
        while time.time() < deadline and not rec.segments:
            time.sleep(0.05)
        self.assertTrue(rec.segments, "恢复之后该开始写段")
        rec.stop()
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, rec.segments[0])))


class MeetingPauseTests(unittest.TestCase):
    """会议那一层：状态接口要如实带 `paused`，meta 要留下暂停区间。

    ⚠️ **每个用例一个临时库**（`db.DATA_DIR/DB_FILE`）：会议名是"秒级时间戳"，
    同一秒里开两场就会撞 `meetings.name` 的 UNIQUE —— 而且**不许**把测试会议写进真库
    （2026-10-15 补：第一版就是这么红的）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pausemtg-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-pausedb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()

        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        self._orig_stream = recorder_mod.input_stream
        recorder_mod.input_stream = lambda *a, **kw: _FakeStream()

        # 每个用例从"没在录"开始
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[])

        def _clean():
            meeting_mod._state.update(active=False, folder=None, recorder=None,
                                      paused=False, pauses=[])
            meeting_mod.meetings_dir = self._orig_dir
            recorder_mod.input_stream = self._orig_stream
            db.DATA_DIR, db.DB_FILE = self._old_db
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    def _start(self):
        ok, msg = meeting_mod.start_meeting()
        self.assertTrue(ok, msg)
        return msg

    def test_pause_without_a_meeting_says_so(self):
        ok, msg = meeting_mod.pause_meeting(by="手机")
        self.assertFalse(ok)
        self.assertIn("没有在录音", msg)

    def test_status_reports_paused_and_meta_keeps_the_window(self):
        name = self._start()
        ok, msg = meeting_mod.pause_meeting(by="手机")
        self.assertTrue(ok, msg)
        self.assertTrue(meeting_mod.meeting_status()["paused"], "状态接口必须如实说已暂停")
        time.sleep(0.05)
        ok, msg = meeting_mod.resume_meeting(by="手机")
        self.assertTrue(ok, msg)
        self.assertFalse(meeting_mod.meeting_status()["paused"])

        meeting_mod.stop_meeting()
        folder = os.path.join(self.tmp, name)
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        pauses = meta.get("pauses") or []
        self.assertEqual(1, len(pauses), "暂停区间该记下来：%r" % (pauses,))
        self.assertEqual("手机", pauses[0]["by"], "谁按的也要记（手机 / 面板）")
        for key in ("from", "to"):
            self.assertTrue(pauses[0].get(key), "%s 该有值：%r" % (key, pauses[0]))
            datetime.datetime.fromisoformat(pauses[0][key])       # 必须是能解析的时间

    def test_stop_while_paused_closes_the_open_window(self):
        name = self._start()
        meeting_mod.pause_meeting(by="面板")
        time.sleep(0.05)
        meeting_mod.stop_meeting()          # 暂停着直接停
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        pauses = meta.get("pauses") or []
        self.assertEqual(1, len(pauses))
        self.assertTrue(pauses[0].get("to"),
                        "停止时还在暂停 → 那个区间要收尾，否则看不出暂停到什么时候")


if __name__ == "__main__":
    unittest.main()
