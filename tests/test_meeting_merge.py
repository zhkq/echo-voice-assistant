# -*- coding: utf-8 -*-
"""停止录音时，目录里已有的 `*.wav` 也要并进本场一起转写。

2026-09-23 用户问："把文件改名 00.wav 然后拷到后一个会的目录，是不是会议结束后就可以
自动转了？" —— 之前的答案是**不会**：`_transcribe_impl` 优先用 `meta["segments"]`，而非空
就不再看目录，而 `stop_meeting()` 只写录音器内存里的那份清单，所以拷进去的那段被静默忽略。
这条用例把这个行为钉住：现在两份都会进 segments。
"""
import json
import os
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

from app import meeting


def _wav(path, seconds):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))


class _NoThread:
    """替掉后台转写线程：只测 stop_meeting 的账，不真去转写。"""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


class _FakeRecorder:
    def __init__(self, segments):
        self.segments = list(segments)
        self.error = ""

    def stop(self):
        return True


class StopMeetingSegmentsTests(unittest.TestCase):
    def _stop(self, tmp, recorded):
        state = {"active": True, "recorder": _FakeRecorder(recorded), "folder": tmp, "error": ""}
        with patch.object(meeting, "_state", state), \
                patch.object(meeting, "_state_lock", threading.RLock()), \
                patch.object(meeting.db, "get_meeting_by_name", lambda n: {"id": 1}), \
                patch.object(meeting.db, "update_meeting", lambda *a, **k: None), \
                patch.object(meeting.db, "add_event", lambda *a, **k: None), \
                patch.object(meeting.db, "add_log", lambda *a, **k: None), \
                patch.object(meeting.threading, "Thread", _NoThread):
            ok, msg = meeting.stop_meeting()
        meta = json.load(open(os.path.join(tmp, "meta.json"), encoding="utf-8"))
        return ok, msg, meta

    def test_a_copied_in_segment_is_included(self):
        """拷进来的 00.wav（上一场被中断的那段）必须和本场录的一起转。"""
        with tempfile.TemporaryDirectory() as tmp:
            _wav(os.path.join(tmp, "00.wav"), 2)     # 拷进来的
            _wav(os.path.join(tmp, "01.wav"), 1)     # 本场录的
            ok, _msg, meta = self._stop(tmp, ["01.wav"])
        self.assertTrue(ok)
        self.assertEqual(["00.wav", "01.wav"], meta["segments"])
        self.assertAlmostEqual(3.0, float(meta["durationSeconds"]), places=1)

    def test_only_the_recorded_segments_when_nothing_was_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            _wav(os.path.join(tmp, "01.wav"), 1)
            _wav(os.path.join(tmp, "02.wav"), 1)
            ok, _msg, meta = self._stop(tmp, ["01.wav", "02.wav"])
        self.assertTrue(ok)
        self.assertEqual(["01.wav", "02.wav"], meta["segments"])

    def test_non_segment_files_are_ignored(self):
        """meta.json / transcript.md 这类不能被当成分段。"""
        with tempfile.TemporaryDirectory() as tmp:
            _wav(os.path.join(tmp, "01.wav"), 1)
            for junk in ("meta.json", "transcript.md", "not-a-segment.wav", "00.wav.bak"):
                open(os.path.join(tmp, junk), "w").close()
            ok, _msg, meta = self._stop(tmp, ["01.wav"])
        self.assertTrue(ok)
        self.assertEqual(["01.wav"], meta["segments"], "只认 ^\\d+\\.wav$")


if __name__ == "__main__":
    unittest.main()
