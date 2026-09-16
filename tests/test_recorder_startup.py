"""MeetingRecorder 启动校验：打不开麦克风要能被上层感知，而不是静默退出。"""
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from app.audio import recorder


class _FakeStream:
    device = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n):
        return np.zeros((n, 1), dtype=np.int16), False


class MeetingRecorderStartupTests(unittest.TestCase):
    def test_wait_started_true_when_stream_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(recorder, "_open_input", return_value=_FakeStream()):
                rec = recorder.MeetingRecorder(tmp, device_id=1)
                rec.start()
                try:
                    self.assertTrue(rec.wait_started(timeout=2))
                    self.assertIsNone(rec.error)
                finally:
                    rec.stop()

    def test_wait_started_false_and_error_when_device_fails(self):
        def boom(*args, **kwargs):
            raise RuntimeError("没有可用的输入设备")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(recorder, "_open_input", side_effect=boom):
                rec = recorder.MeetingRecorder(tmp, device_id=0)
                rec.start()
                self.assertFalse(rec.wait_started(timeout=2))
                self.assertIn("没有可用的输入设备", rec.error or "")

    def test_segments_empty_when_never_opened(self):
        def boom(*args, **kwargs):
            raise RuntimeError("设备打开失败")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(recorder, "_open_input", side_effect=boom):
                rec = recorder.MeetingRecorder(tmp, device_id=0)
                rec.start()
                rec.wait_started(timeout=2)
                time.sleep(0.05)
                rec.stop()
        self.assertEqual(rec.segments, [])


if __name__ == "__main__":
    unittest.main()
