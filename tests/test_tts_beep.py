# -*- coding: utf-8 -*-
"""提示音播放的行为测试（2026-09-19 音频排查的产物）

背景：`play_beep()` 原来是**全静默**的 —— 文件不存在直接 return、异常 `except: pass`、
`PlaySound` 的返回值没人看。于是"提示音没响"和"根本没播出去"从外面看一模一样，
用户实测反馈"只听到 5 声里的后 3 声"时，代码层完全给不出线索。

现在钉住三件事：
  1. 播放失败/文件缺失**返回值透出**，并写一条 debug 日志（不再静默）；
  2. 永远**不抛异常** —— 提示音不该影响录音主流程；
  3. 播放实现走接缝（`echo_platform.play_wav_async`），本模块不再关心 winsound/afplay。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                          # noqa: E402
from app.audio import tts                                    # noqa: E402


class PlayBeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-beep-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._dir = patch.object(tts, "BEEPS_DIR", self.tmp)
        self._dir.start()
        self.addCleanup(self._dir.stop)
        self.logs = []
        self._log = patch.object(db, "add_log", lambda level, source, msg: self.logs.append(
            (level, source, msg)))
        self._log.start()
        self.addCleanup(self._log.stop)
        with open(os.path.join(self.tmp, "ok.wav"), "wb") as fh:
            fh.write(b"RIFF....WAVE")

    def test_success_returns_true_and_logs_nothing(self):
        with patch.object(tts.echo_platform, "play_wav_async", return_value=True) as play:
            self.assertIs(tts.play_beep("ok"), True)
        play.assert_called_once()
        self.assertEqual(self.logs, [])

    def test_missing_file_returns_false_and_logs(self):
        self.assertIs(tts.play_beep("no-such-beep"), False)
        self.assertEqual(len(self.logs), 1)
        level, source, msg = self.logs[0]
        self.assertEqual((level, source), ("debug", "tts"))
        self.assertIn("文件不存在", msg)

    def test_seam_failure_returns_false_and_logs(self):
        with patch.object(tts.echo_platform, "play_wav_async", return_value=False):
            self.assertIs(tts.play_beep("ok"), False)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("播放失败", self.logs[0][2])

    def test_seam_exception_is_swallowed_but_logged(self):
        with patch.object(tts.echo_platform, "play_wav_async",
                          side_effect=RuntimeError("device gone")):
            self.assertIs(tts.play_beep("ok"), False)      # 不抛给录音主流程
        self.assertEqual(len(self.logs), 1)
        self.assertIn("播放异常", self.logs[0][2])

    def test_logging_failure_does_not_raise(self):
        """日志本身炸了也不能影响播放（db 不可用时）。"""
        with patch.object(db, "add_log", side_effect=RuntimeError("db down")):
            self.assertIs(tts.play_beep("no-such-beep"), False)

    def test_uses_the_platform_seam_not_winsound(self):
        """本模块不许再**直接**调用平台播放 API（守卫测试也管，这里是行为侧对照）。

        注意不能用"源码里出现 winsound 字样"来判 —— 文档串里解释历史原因是合法的；
        要判的是"有没有真的 import / 调用"。
        """
        import inspect
        src = inspect.getsource(tts)
        self.assertNotIn("import winsound", src)
        self.assertNotIn("winsound.PlaySound", src)
        self.assertNotIn("subprocess.Popen([\"afplay\"", src)
        self.assertIn("echo_platform.play_wav_async", src, "提示音必须走接缝")


class BeepFilesTests(unittest.TestCase):
    """五个提示音的物理属性（回归保护：别再把某个文件换成哑的/换掉格式）。"""

    BEEPS = ("start", "done", "ok", "ok2", "err")

    def test_all_five_exist_and_are_audible(self):
        import numpy as np
        import soundfile as sf
        for name in self.BEEPS:
            with self.subTest(name=name):
                path = os.path.join(tts.BEEPS_DIR, name + ".wav")
                self.assertTrue(os.path.isfile(path), path)
                data, sr = sf.read(path, dtype="float32")
                self.assertEqual(sr, 44100)
                peak = float(np.max(np.abs(data)))
                self.assertGreater(peak, 0.05, "%s 峰值太低，会被当成没声音" % name)
                self.assertLess(peak, 0.99, "%s 削顶了" % name)

    def test_shortest_beep_is_not_below_the_known_latency_floor(self):
        """`done.wav` 只有 90 ms，是实测**唯一**会被吞掉的一声。

        这条测试是**记录现状并加护栏**：任何一声短于 120 ms 都会被这条老通路
        （MME/waveOut）的启动延迟吃掉。要改短必须同时改这个断言 —— 逼人看一眼注释。
        """
        import soundfile as sf
        shortest = None
        for name in self.BEEPS:
            data, sr = sf.read(os.path.join(tts.BEEPS_DIR, name + ".wav"), dtype="float32")
            dur = len(data) / float(sr)
            if shortest is None or dur < shortest[1]:
                shortest = (name, dur)
        self.assertEqual(shortest[0], "done")
        self.assertLess(shortest[1], 0.12,
                        "done.wav 已不短于 120ms：若真是它被吞，说明原因不是时长，"
                        "请更新本条测试与 docs 里的结论")


if __name__ == "__main__":
    unittest.main()
