# -*- coding: utf-8 -*-
"""**手机当麦克风**那条录音路（`meeting.start_meeting_phone` / `append_meeting_segment`）。

用户 2026-10-15 的口径：*"手机发起的指令，手机收，电脑是配合执行和存储"* ——
所以"从手机发起录音"时，采集该是**手机那支麦**（手机在会议室、电脑可能合着盖）。

这条路的判据：
  1. **电脑不开麦**（没有 recorder）：不能因为"手机在录"就把电脑的声卡也打开；
  2. 手机传上来的段**必须是 wav**（下游按 wav 读，塞 mp3 进去会在转写时炸出一句很难追的错）；
  3. 段号**由电脑排**（手机只保证顺序）—— 存储那一方定编号；
  4. **本机麦那场不接受外来段**：两路音频混进同一场 = 转写里两个人同时说话，查不清；
  5. 停止后 meta/段列表与"本机麦"那条路**完全一样**（下游一条都不用改）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                                     # noqa: E402
from app import meeting as meeting_mod                                 # noqa: E402


def _tiny_wav() -> bytes:
    """一个最小的合法 WAV（44 字节头 + 160 个静音采样）。"""
    import struct
    data = b"\x00\x00" * 160
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


class PhoneMicTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-phonemic-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-phonemicdb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0)

        # 别让 stop_meeting() 真的去起转写线程：那会在用例结束（临时库已拆）之后才跑，
        # 报一堆 no such table: meetings —— 那是测试卫生问题，不是被测行为。
        self._orig_transcribe = meeting_mod._transcribe_meeting
        meeting_mod._transcribe_meeting = lambda *a, **kw: None
        self.addCleanup(lambda: setattr(meeting_mod, '_transcribe_meeting', self._orig_transcribe))

        def _clean():
            meeting_mod._state.update(active=False, folder=None, recorder=None,
                                      paused=False, pauses=[], source="pc", seg_count=0)
            meeting_mod.meetings_dir = self._orig_dir
            db.DATA_DIR, db.DB_FILE = self._old_db
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    def test_start_phone_does_not_open_a_recorder(self):
        ok, name = meeting_mod.start_meeting_phone()
        self.assertTrue(ok, name)
        self.assertIsNone(meeting_mod._state["recorder"], "手机在录，电脑不该开麦")
        st = meeting_mod.meeting_status()
        self.assertEqual("phone", st["source"])
        self.assertTrue(st["active"])

    def test_segments_are_stored_and_numbered_by_the_computer(self):
        ok, name = meeting_mod.start_meeting_phone()
        self.assertTrue(ok, name)
        for _ in range(3):
            good, seg = meeting_mod.append_meeting_segment(_tiny_wav())
            self.assertTrue(good, seg)
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            segs = json.load(fh)["segments"]
        self.assertEqual(["01.wav", "02.wav", "03.wav"], segs,
                         "段号该由**电脑**排（手机只保证顺序）")
        self.assertEqual(3, meeting_mod.meeting_status()["segments"],
                         "状态接口的段数要给手机显示进度")
        for seg in ("01.wav", "02.wav", "03.wav"):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, name, seg)))

    def test_a_non_audio_segment_is_refused(self):
        """裸 body 但**不是音频**：下游按文件真格式读，塞 mp3/垃圾进去会在转写时炸。

        2026-10-16：这条接口现在**也收 FLAC**（`fLaC` 魔数，见
        `tests/test_meeting_offline_flac.py`），所以拒绝理由从"不是 WAV"改成
        "不是音频（WAV/FLAC 都不是）"。判据本身（**认不出的字节一律拒**）没动。
        """
        meeting_mod.start_meeting_phone()
        ok, why = meeting_mod.append_meeting_segment(b"ID3\x04\x00\x00 this is an mp3")
        self.assertFalse(ok)
        self.assertIn("不是音频", why)
        self.assertIn("WAV", why)
        self.assertIn("FLAC", why)

    def test_the_local_mic_meeting_refuses_phone_segments(self):
        """两路音频混进同一场 = 转写里两个人同时说话 —— 必须明确拒绝。"""
        from app.audio import recorder as recorder_mod

        class _FakeStream:
            device = "fake"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                import numpy as np
                import time as _t
                _t.sleep(0.02)
                return np.zeros(min(int(n), 1600), dtype="int16"), False

        orig = recorder_mod.input_stream
        recorder_mod.input_stream = lambda *a, **kw: _FakeStream()
        try:
            ok, name = meeting_mod.start_meeting()
            self.assertTrue(ok, name)
            good, why = meeting_mod.append_meeting_segment(_tiny_wav())
            self.assertFalse(good)
            self.assertIn("电脑的麦克风", why)
        finally:
            meeting_mod.stop_meeting()
            recorder_mod.input_stream = orig

    def test_stop_finishes_the_phone_meeting_like_the_other_path(self):
        ok, name = meeting_mod.start_meeting_phone()
        self.assertTrue(ok, name)
        meeting_mod.append_meeting_segment(_tiny_wav())
        meeting_mod.append_meeting_segment(_tiny_wav())
        ok, msg = meeting_mod.stop_meeting()
        self.assertTrue(ok, msg)
        meta = json.loads(open(os.path.join(self.tmp, name, "meta.json"),
                               encoding="utf-8").read())
        self.assertEqual("phone", meta.get("source"))
        self.assertEqual(["01.wav", "02.wav"], meta.get("segments"))
        self.assertTrue(meta.get("end"), "停止时必须写下结束时间")
        self.assertFalse(meeting_mod.meeting_status()["active"])

    def test_pause_works_without_a_recorder_and_lands_in_meta(self):
        """手机那条路没有 recorder，但"暂停"这件事仍然成立（区间照样记进 meta）。"""
        ok, name = meeting_mod.start_meeting_phone()
        self.assertTrue(ok, name)
        ok, msg = meeting_mod.pause_meeting(by="手机")
        self.assertTrue(ok, msg)
        self.assertTrue(meeting_mod.meeting_status()["paused"])
        ok, msg = meeting_mod.resume_meeting(by="手机")
        self.assertTrue(ok, msg)
        meeting_mod.stop_meeting()
        meta = json.loads(open(os.path.join(self.tmp, name, "meta.json"),
                               encoding="utf-8").read())
        pauses = meta.get("pauses") or []
        self.assertEqual(1, len(pauses))
        self.assertTrue(pauses[0].get("from") and pauses[0].get("to"))


if __name__ == "__main__":
    unittest.main()
