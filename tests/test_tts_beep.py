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


class BeepSpeechSerializationTests(unittest.TestCase):
    """提示音与朗读必须**串起来**（2026-09-19 用户提问："语音复述是不是和停录提示音有冲突"）。

    机制：提示音是**异步**播的（winsound SND_ASYNC / afplay），朗读走另一套音频通路
    （sounddevice / SAPI / say），两套互不知情 —— 转写只要几百毫秒（和 done 提示音
    0.29 s 同量级），复述就可能在提示音还没播完时起播，听感是糊的；会议指令路径更直接
    （`play_beep` 紧接着 `speak_async`，同一瞬间起播）。
    现在 `play_beep` 记下"预计播完时刻"，`speak()` 起播前 `wait_beep_done()` 避让。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-beepser-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._dir = patch.object(tts, "BEEPS_DIR", self.tmp)
        self._dir.start()
        self.addCleanup(self._dir.stop)
        self._log = patch.object(db, "add_log", lambda *a, **k: None)
        self._log.start()
        self.addCleanup(self._log.stop)
        self._reset = patch.object(tts, "_beep_until", 0.0)
        self._reset.start()
        self.addCleanup(self._reset.stop)
        with open(os.path.join(self.tmp, "done.wav"), "wb") as fh:
            fh.write(b"RIFF....WAVE")

    def test_play_beep_records_when_it_will_finish(self):
        with patch.object(tts.echo_platform, "play_wav_async", return_value=True), \
                patch.object(tts, "_beep_seconds", lambda name, default=0.22: 0.29):
            self.assertTrue(tts.play_beep("done"))
        self.assertGreater(tts.beep_pending(), 0.0, "播完时刻应被记下")
        self.assertLess(tts.beep_pending(), 0.30)

    def test_failed_beep_records_nothing(self):
        with patch.object(tts.echo_platform, "play_wav_async", return_value=False):
            self.assertFalse(tts.play_beep("done"))
        self.assertEqual(tts.beep_pending(), 0.0, "没播出去就不该让朗读白等")

    def test_wait_sleeps_only_the_remaining_time(self):
        slept = []
        with patch.object(tts.echo_platform, "play_wav_async", return_value=True), \
                patch.object(tts, "_beep_seconds", lambda name, default=0.22: 0.3):
            tts.play_beep("done")
            with patch.object(tts.time, "sleep", lambda s: slept.append(s)):
                tts.wait_beep_done()
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0.0)
        self.assertLess(slept[0], 0.31)

    def test_wait_is_a_noop_without_a_pending_beep(self):
        slept = []
        with patch.object(tts.time, "sleep", lambda s: slept.append(s)):
            tts.wait_beep_done()
        self.assertEqual(slept, [], "没有提示音在播时必须零成本返回")

    def test_wait_is_capped(self):
        """时间戳坏掉（等过头）时不能拖住主流程：上限 1 秒。"""
        slept = []
        with patch.object(tts, "beep_pending", lambda: 30.0), \
                patch.object(tts.time, "sleep", lambda s: slept.append(s)):
            tts.wait_beep_done()
        self.assertEqual(slept, [1.0])

    def test_speak_waits_for_the_beep_first(self):
        """朗读入口必须先避让提示音（这是用户问的那条冲突的正面修复）。"""
        order = []
        with patch.object(tts, "wait_beep_done", lambda *a, **k: order.append("wait")), \
                patch.object(tts, "_speak_offline", lambda text: order.append("speak") or True):
            self.assertTrue(tts.speak("你好", "sapi"))
        self.assertEqual(order, ["wait", "speak"], "顺序必须是先避让、再出声")

    def test_off_engine_still_does_not_speak(self):
        with patch.object(tts, "wait_beep_done") as wait, \
                patch.object(tts, "_speak_offline", lambda text: True):
            self.assertFalse(tts.speak("你好", "off"))
        wait.assert_not_called()


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

    def test_no_beep_is_shorter_than_200ms(self):
        """所有提示音都不得短于 200 ms（2026-09-19 之后的硬底线）。

        来历：`done.wav` 原来是 **700 Hz / 90 ms**，用户实测**三条通路全都听不到**
        （winsound 接缝 / sounddevice / 垫 200ms 静音），而 120 ms 以上的 start/ok/ok2/err
        都听得到；文件格式与电平与其它四个逐项相同。用户拍板"换个提示音、时间更长一点"
        -> 已用 `scripts/make_beeps.py --write done` 重生成（880→660 Hz、0.29 s、峰值 0.150）。

        这条断言就是那次事故的护栏：提示音是**给耳朵用的反馈**，宁可长一点、
        也不能短到听不见。要改短必须同时改这里与 docs/2.0-PROGRESS §34。
        """
        import soundfile as sf
        shortest = []
        for name in self.BEEPS:
            data, sr = sf.read(os.path.join(tts.BEEPS_DIR, name + ".wav"), dtype="float32")
            dur = len(data) / float(sr)
            shortest.append((name, round(dur, 3)))
            with self.subTest(name=name):
                self.assertGreaterEqual(dur, 0.20,
                                        "%s.wav 只有 %.3fs，短提示音实测会被听漏" % (name, dur))
        print("beep durations: %s" % shortest)

    def test_done_beep_is_the_new_two_tone(self):
        """`done` 是"停止录音"的反馈，必须是那个可复现的下行两音（不许被换回 90ms 短音）。"""
        import numpy as np
        import soundfile as sf
        data, sr = sf.read(os.path.join(tts.BEEPS_DIR, "done.wav"), dtype="float32")
        dur = len(data) / float(sr)
        self.assertGreaterEqual(dur, 0.25, "done 应为两音约 0.29s，实际 %.3fs" % dur)
        for i, (lo, hi, want) in enumerate([(0, int(sr * 0.13), 880),
                                            (int(sr * 0.16), int(sr * 0.29), 660)]):
            seg = data[lo:hi] * np.hanning(len(data[lo:hi]))
            freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
            peak = freqs[int(np.argmax(np.abs(np.fft.rfft(seg))))]
            with self.subTest(tone=i + 1):
                self.assertLess(abs(peak - want), 25,
                                "第%d个音应为 %dHz 附近，实测 %.0fHz" % (i + 1, want, peak))

    def test_all_five_are_reproducible_from_the_generator(self):
        """`scripts/make_beeps.py` 是**全部五个**提示音的权威来源（2026-09-19 起）。

        参数表与实际文件一旦分叉（有人手改 wav、或改了参数忘了重生成），这条就红。
        旧波形（90ms 的 done 等）在 git 历史里可取回。
        """
        import importlib.util
        import soundfile as sf
        spec_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "scripts", "make_beeps.py")
        spec = importlib.util.spec_from_file_location("echo_make_beeps", spec_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(set(mod.GENERATED), set(self.BEEPS),
                         "五个提示音都应由生成脚本产出")
        for name in self.BEEPS:
            with self.subTest(name=name):
                want = mod.synth(mod.SPECS[name])
                got, sr = sf.read(os.path.join(tts.BEEPS_DIR, name + ".wav"), dtype="float32")
                self.assertEqual(sr, mod.SR)
                self.assertLessEqual(abs(len(got) - len(want)), 16,
                                     "%s.wav 与参数表长度不符（是不是手改过？）" % name)


class BeepOkPairTests(unittest.TestCase):
    """`beep_ok()`：ok → ok2 两连音，间隔必须够第一个音播完。"""

    def test_pair_plays_in_order_with_enough_gap(self):
        calls = []
        sleeps = []
        with patch.object(tts, "play_beep", lambda n: calls.append(n)), \
                patch.object(tts.time, "sleep", lambda s: sleeps.append(s)):
            tts.beep_ok()
        self.assertEqual(calls, ["ok", "ok2"])
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 0.20,
                                "间隔太短会把第一个音截断（异步通路下一次调用会打断上一次）")

    def test_gap_follows_the_file_length(self):
        with patch.object(tts, "_beep_seconds", return_value=0.5):
            calls, sleeps = [], []
            with patch.object(tts, "play_beep", lambda n: calls.append(n)), \
                    patch.object(tts.time, "sleep", lambda s: sleeps.append(s)):
                tts.beep_ok()
        self.assertAlmostEqual(sleeps[0], 0.54, places=2)


if __name__ == "__main__":
    unittest.main()
