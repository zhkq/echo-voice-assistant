# -*- coding: utf-8 -*-
"""让出麦克风：**会议可以中止语音指令**（D1 单向抢占，2026-09-23）。

为什么只允许这一个方向（docs/统一路由-模型能力与设备.md §3.6.1）：

    会议 → 指令：被中断的是一句 ≤30 秒的语音指令，代价是"重说一遍" → 可以
    指令 → 会议：被中断的可能是两小时的录音，代价是"白录一场" → 绝对不行

判据是**被中断的代价**，不是"谁优先级高"。

这里只做故障注入：不碰真实麦克风、不碰用户库。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import assistant                                          # noqa: E402
from app.audio import mic, recorder                                # noqa: E402


class _Stream:
    """假的输入流：`read()` 返回静音（跟 test_mic_reliability 的做法一致）。"""

    device = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, n):
        return np.zeros((n, 1), dtype=np.int16), False


class YieldForMeetingTests(unittest.TestCase):
    """`yield_capture_for_meeting` / `cancel_capture` 的契约。"""

    def setUp(self):
        assistant._capture_stop.clear()
        assistant._capture_active.clear()
        self.addCleanup(assistant._capture_stop.clear)
        self.addCleanup(assistant._capture_active.clear)

    def test_noop_when_nothing_is_recording(self):
        """没在收音时**什么都不做** —— 尤其不能留下中止标记，
        否则下一条指令一开始就会被它打断（那会变成"永远录不到"）。"""
        self.assertFalse(assistant.yield_capture_for_meeting(0.01))
        self.assertFalse(assistant._capture_stop.is_set())

    def test_waits_for_a_natural_finish_instead_of_cancelling(self):
        """多数情况应当**等它自己收尾** —— 用户无感，那句指令也不丢。"""
        assistant._capture_active.set()
        threading.Timer(0.05, assistant._capture_active.clear).start()
        self.assertFalse(assistant.yield_capture_for_meeting(2.0),
                         "自己收尾了 → 不该中止")
        self.assertFalse(assistant._capture_stop.is_set())

    def test_cancels_when_it_will_not_finish_in_time(self):
        """等不及了才中止 —— 会议不能因为一句指令卡住。"""
        assistant._capture_active.set()          # 假装一直在收音
        self.assertTrue(assistant.yield_capture_for_meeting(0.2))
        self.assertTrue(assistant._capture_stop.is_set())

    def test_cancel_capture_is_noop_when_idle(self):
        self.assertFalse(assistant.cancel_capture("测试"))

    def test_cancel_capture_reports_true_while_recording(self):
        assistant._capture_active.set()
        self.assertTrue(assistant.cancel_capture("测试"))
        self.assertTrue(assistant._capture_stop.is_set())


class RecordCommandStopEventTests(unittest.TestCase):
    """`stop_event` 真的能让采集停下来（接线之前它是个没人用的参数）。"""

    def test_already_set_event_stops_before_reading_anything(self):
        ev = threading.Event()
        ev.set()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(mic, "_new_stream", return_value=_Stream()):
                ok = recorder.record_command(os.path.join(d, "a.wav"), stop_event=ev)
        self.assertFalse(ok, "置位的 stop_event 应当让采集立刻返回 False")

    def test_event_set_midway_stops_soon(self):
        """收音到一半被置位 → 在**下一帧**就停，不会等到静音收尾或 max_ms。"""
        ev = threading.Event()

        def fire():
            threading.Event().wait(0.15)
            ev.set()

        threading.Thread(target=fire, daemon=True).start()
        with tempfile.TemporaryDirectory() as d:
            with patch.object(mic, "_new_stream", return_value=_Stream()):
                ok = recorder.record_command(
                    os.path.join(d, "b.wav"), max_ms=20000,
                    no_speech_abort_ms=20000,      # 确保不是因为"开口前无语音"而退出
                    stop_event=ev)
        self.assertFalse(ok)

    def test_without_the_event_it_falls_back_to_no_speech_abort(self):
        """对照：不给 stop_event 时，静音输入应当走"开口前无语音放弃"（而不是被中止）。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.object(mic, "_new_stream", return_value=_Stream()):
                ok = recorder.record_command(
                    os.path.join(d, "c.wav"), no_speech_abort_ms=200, max_ms=20000)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
