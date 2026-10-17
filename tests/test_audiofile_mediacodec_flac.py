# -*- coding: utf-8 -*-
"""回归：**手机 MediaCodec 编的 FLAC**（头里不写总样本数）必须能解出来。

## 这份样本是**合成的**（2026-10-17 换成，不含任何真人语音）

`tests/fixtures/synthetic-mediacodec-flac-unknown-total-samples.flac` 不是任何人的录音，
而是一段 **1.0 秒 / 440 Hz 合成正弦**（16 kHz / 单声道 / 16-bit），用 ffmpeg 编成 FLAC
之后**原地改写头部**，让它和真机那份"同形状"：

  * `STREAMINFO.total_samples = 0`（FLAC 规范里 0 = 未知）；
  * `STREAMINFO.md5 = 全零`（编码器写头时还不知道整段内容）；
  * `STREAMINFO.min/max frame size = 0`（同样表示未知）；
  * 带 `VORBIS_COMMENT` 元数据块。

**为什么用合成样本**（这是本文件的立意，别再把真件放回来）：原来的回归样本是用户真机
上一场失败会议的**原件拷贝**（`D:\\ECHO\\meeting\\2026-10-17_21-30-02\\02.flac`，
91 KB / 6.3 秒）。它确实能复现问题，但**把真人的声音放进 git 是不能接受的** ——
隐私风险与仓库体积都不该由"测试价值"来换。合成样本复现的是**同一个头部形状**，
也就是同一个坑（libsndfile 把 total_samples=0 读成 `SF_COUNT_MAX` 并 seek 失败），
测试价值完全一样；它只有约 6 KB，且一个真人样本都没有。

## 历史现场（真机原始报错；样本现在已经换成上面那份合成件）

    [2026-10-17T21:30:10] 转写异常: CompressionError('音频段01.flac解码失败：Internal psf_fseek() failed.')
    Traceback (most recent call last):
      File "D:\\ECHO\\echo-core\\app\\audio\\audiofile.py", line 349, in decode_to_wav
        for block in fh.blocks(blocksize=VERIFY_BLOCK_FRAMES, dtype="int16",
      ...
      File "D:\\ECHO\\runtime-core\\Lib\\site-packages\\soundfile.py", line 867, in seek
        self.seek(curr + frames, SEEK_SET)  # Update read & write position
      ...
      soundfile.LibsndfileError: Internal psf_fseek() failed.

## 根因（**不是坏文件**，是解码器太挑）

FLAC 规范里 `STREAMINFO.total_samples == 0` 就是"未知"（合法取值）。Android
`MediaCodec` 在**开头**就得写这块头，那时还不知道整段会有多长 → 写 0（`md5` 同样是 0，
`min/max frame size` 也是 0）。libsndfile 1.2.2 把它读成 `SF_COUNT_MAX`（2**63-1），
随后连"读完一块、把位置记回去"的那个 seek 都失败 —— 于是每一块都解不出来。
这份合成样本用 ffmpeg 能**完整**解出 16000 帧（1.0 秒），所以它是**好文件**：
绝不能因此拒收、删除，更不能报"转写成功"。

## 判据（任务里"二选一"的**第一种**：能解码成 16 kHz 单声道）

修法见 `app/audio/audiofile.py::decode_to_wav` 的第②条路（ffmpeg）。本文件钉住五件事：
  1. 这份"长度未知"的样本能解成 16 kHz / 单声道 / 16-bit，帧数完整；
  2. `audio_seconds()` 给的是**真时长 1.0 秒**，而不是哨兵值（真机 `meta.json` 里那个
     `durationSeconds: 1152921504606847.0`）；
  3. **截断的拷贝仍然如实失败** —— 换了更宽容的解码器，也绝不把半截音频当成功；
  4. 解不开时给的原因**可照做**（这台机器没 ffmpeg → 明说装 ffmpeg 或让手机改传 WAV）；
  5. 原件**一个字节都不动**（解不开也保留音频 —— "宁可慢一半，也别丢音频"）。
"""
import array
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio import audiofile                                        # noqa: E402
from tests.test_meeting_offline_flac import _HttpCase                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "tests", "fixtures",
                       "synthetic-mediacodec-flac-unknown-total-samples.flac")

#: 合成样本的参数：1.0 秒 × 16000 = 16000 帧（ffmpeg 解出来的就是它）。
EXPECTED_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_FRAMES = 16000
EXPECTED_SECONDS = 1.0


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_wav(path):
    """`(rate, channels, sampwidth, frames, peak)` —— 用标准库 `wave` 读，
    不借任何"解码器自己说的话"（那正是被这份样本骗过的东西）。"""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.getnframes()
        raw = w.readframes(frames)
    samples = array.array("h")
    samples.frombytes(raw)
    peak = max((abs(s) for s in samples), default=0)
    return rate, channels, width, frames, peak


class MediaCodecFlacDecodeTests(unittest.TestCase):
    """“长度未知”的 FLAC → 能解成引擎要的 WAV（这条路径被 `meeting.decoded_segments` 用着）。"""

    def setUp(self):
        self.assertTrue(os.path.isfile(FIXTURE), "合成样本不见了：%s" % FIXTURE)
        self.tmp = tempfile.mkdtemp(prefix="echo-mcflac-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---------------------------------------------------------------- ① 能解
    def test_the_sample_is_the_unknown_length_variant(self):
        """先钉住"这份样本到底坏在哪"：头里**总样本数未知**（不是文件损坏）。

        这条要是红了，说明样本被换成了"正常的 FLAC" —— 那后面几条就都在测别的东西了。
        """
        import soundfile as sf
        info = sf.info(FIXTURE)
        self.assertEqual(EXPECTED_RATE, int(info.samplerate))
        self.assertEqual(EXPECTED_CHANNELS, int(info.channels))
        self.assertEqual("PCM_16", str(info.subtype))
        self.assertGreaterEqual(int(info.frames), audiofile.SF_COUNT_MAX,
                                "libsndfile 应把'长度未知'报成 SF_COUNT_MAX（哨兵值）")
        # 而真因在**头里**：STREAMINFO.total_samples == 0
        with open(FIXTURE, "rb") as fh:
            head = fh.read(4 + 4 + 34)
        self.assertEqual(b"fLaC", head[:4])
        body = head[8:8 + 34]
        packed = int.from_bytes(body[10:18], "big")
        total_samples = packed & ((1 << 36) - 1)
        self.assertEqual(0, total_samples,
                         "样本的 STREAMINFO.total_samples 该是 0（MediaCodec 的写法）")
        self.assertEqual(0, int.from_bytes(body[18:34], "big"),
                         "md5 该是全零（MediaCodec 也算不出来）")

    def test_the_synthetic_sample_decodes_to_16k_mono_wav(self):
        """**判据**：这份样本 → 16 kHz / 单声道 / 16-bit PCM，且帧数完整（不是半截）。"""
        before = _sha256(FIXTURE)
        if not audiofile.ffmpeg_exe():
            self.skipTest("这台机器上没有 ffmpeg：长度未知的 FLAC 需要它（见模块注释）")
        out = ""
        try:
            out = audiofile.decode_to_wav(FIXTURE, cleanup=False)
            rate, channels, width, frames, peak = _probe_wav(out)
        finally:
            if out and os.path.exists(out):
                os.remove(out)
        self.assertEqual(EXPECTED_RATE, rate, "引擎要的是 16 kHz（sherpa 只认这个采样率）")
        self.assertEqual(EXPECTED_CHANNELS, channels)
        self.assertEqual(2, width, "必须是 16-bit PCM")
        self.assertEqual(EXPECTED_FRAMES, frames,
                         "帧数必须与真实音频一致（少一帧都说明被截断了）")
        self.assertGreater(peak, 200, "解出来的是真声音，不是一段静音（%d）" % peak)
        self.assertEqual(before, _sha256(FIXTURE), "解码**不许改**原件")

    def test_decoded_segments_yields_the_wav_and_cleans_up(self):
        """走转写真正用的那个入口（`decoded_segments`）：拿到 WAV、退出即删。"""
        if not audiofile.ffmpeg_exe():
            self.skipTest("这台机器上没有 ffmpeg")
        old_dir = audiofile.TEMP_DECODE_DIR
        audiofile.TEMP_DECODE_DIR = self.tmp
        self.addCleanup(setattr, audiofile, "TEMP_DECODE_DIR", old_dir)
        with audiofile.decoded_segments([(1, FIXTURE)]) as items:
            self.assertEqual([1], [i for i, _p in items])
            wav_path = items[0][1]
            self.assertTrue(os.path.isfile(wav_path))
            rate, channels, _width, frames, _peak = _probe_wav(wav_path)
            self.assertEqual((EXPECTED_RATE, EXPECTED_CHANNELS, EXPECTED_FRAMES),
                             (rate, channels, frames))
        self.assertFalse(os.path.exists(wav_path), "临时 WAV 出了 with 就该删掉")

    # ---------------------------------------------------------------- ② 时长不撒谎
    def test_audio_seconds_is_the_real_duration_not_the_sentinel(self):
        """时长 = 1.0 秒。**不是** `SF_COUNT_MAX/16000`（那会让面板显示天文数字）。"""
        if not audiofile.ffmpeg_exe():
            self.skipTest("这台机器上没有 ffmpeg")
        sec = audiofile.audio_seconds(FIXTURE)
        self.assertAlmostEqual(EXPECTED_SECONDS, sec, places=2)
        self.assertLess(sec, 1e6, "哨兵值（≈1.15e15 秒）漏出来了：%r" % sec)

    # ---------------------------------------------------------------- ③ 不许放松校验
    def test_a_truncated_copy_still_fails_honestly_and_keeps_the_audio(self):
        """截断的 FLAC：**照样报错**、**原件不动**。

        为什么这条必须有：ffmpeg 对截断文件会解出前半段并**以退出码 0 结束**（实测），
        所以我们的判据是 stderr 必须干净 —— 只听退出码就会把"少了半截的音频"当成功，
        再转写出一份"看着正常、其实缺了一大段"的文字。
        """
        if not audiofile.ffmpeg_exe():
            self.skipTest("这台机器上没有 ffmpeg")
        trunc = os.path.join(self.tmp, "truncated.flac")
        with open(FIXTURE, "rb") as src, open(trunc, "wb") as dst:
            dst.write(src.read(os.path.getsize(FIXTURE) // 2))   # 只留一半
        size_before = os.path.getsize(trunc)
        with self.assertRaises(audiofile.CompressionError) as ctx:
            audiofile.decode_to_wav(trunc, cleanup=False)
        msg = str(ctx.exception)
        self.assertIn(os.path.basename(trunc), msg, "失败原因里必须有文件名：%r" % msg)
        self.assertIn("解码失败", msg)
        self.assertTrue(os.path.isfile(trunc), "解不开也**不许删**用户的音频")
        self.assertEqual(size_before, os.path.getsize(trunc), "原件一个字节都不许动")
        self.assertFalse([n for n in os.listdir(self.tmp) if n.endswith(".part")],
                         "失败后不许留下半截临时文件")

    def test_the_reason_is_actionable_when_ffmpeg_is_missing(self):
        """本机没有 ffmpeg 时：原因里要明说"装 ffmpeg / 让手机改传 WAV"。

        为什么钉它：默认分支会把原因吞成一句"解码失败"，用户既不知道发生了什么，
        也不知道下一步能做什么（而这条路在同事那种没装 ffmpeg 的机器上会真的走到）。
        """
        old = audiofile.ffmpeg_exe
        audiofile.ffmpeg_exe = lambda: ""
        self.addCleanup(setattr, audiofile, "ffmpeg_exe", old)
        with self.assertRaises(audiofile.CompressionError) as ctx:
            audiofile.decode_to_wav(FIXTURE, cleanup=False)
        msg = str(ctx.exception)
        self.assertIn("ffmpeg", msg, msg)
        self.assertIn("WAV", msg, "要给出第二条出路（改传 WAV）")
        self.assertIn(os.path.basename(FIXTURE), msg)
        self.assertTrue(os.path.isfile(FIXTURE))


class NormalFlacStillUsesLibsndfileTests(unittest.TestCase):
    """回归：**头里有总样本数**的正常 FLAC 照旧走 libsndfile（别把所有解码都推给外部程序）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-normflac-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _make_flac(self, seconds=0.5):
        import math
        import struct
        rate = 16000
        frames = int(rate * seconds)
        pcm = b"".join(struct.pack("<h", int(3000 * math.sin(i / 12.0)))
                       for i in range(frames))
        wav = os.path.join(self.tmp, "tone.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        res = audiofile.compress_segment(wav, keep_raw=True)
        self.assertTrue(res["ok"], res.get("reason"))
        return res["flac"]

    def test_a_known_length_flac_is_not_reported_as_unknown(self):
        flac = self._make_flac()
        self.assertFalse(audiofile._flac_length_unknown(flac),
                         "正常 FLAC 头里有总样本数，不该被判成'长度未知'")
        out = audiofile.decode_to_wav(flac, cleanup=False)
        try:
            rate, channels, _width, frames, _peak = _probe_wav(out)
        finally:
            if os.path.exists(out):
                os.remove(out)
        self.assertEqual((16000, 1, 8000), (rate, channels, frames))

    def test_a_known_length_flac_works_even_without_ffmpeg(self):
        """没 ffmpeg 的机器上，**正常** FLAC 必须照旧能解（主路径不依赖外部程序）。"""
        flac = self._make_flac()
        old = audiofile.ffmpeg_exe
        audiofile.ffmpeg_exe = lambda: ""
        self.addCleanup(setattr, audiofile, "ffmpeg_exe", old)
        out = audiofile.decode_to_wav(flac, cleanup=False)
        try:
            self.assertTrue(os.path.isfile(out))
        finally:
            if os.path.exists(out):
                os.remove(out)


class MeetingDurationFromTheSyntheticSampleTests(_HttpCase):
    """端到端：手机传这一段 → 收尾时写进 `meta.json` 的时长必须是 **1.0 秒**。

    这是用户**真的会看到**的那个数字（面板/详情页/导出都读 `durationSeconds`）。真机上
    它写成了 `1152921504606847.0`（= `SF_COUNT_MAX`/8000，见模块注释）—— 判据在这里钉住。
    """

    def test_stop_writes_the_real_duration_not_the_sentinel(self):
        if not audiofile.ffmpeg_exe():
            self.skipTest("这台机器上没有 ffmpeg：量这条 FLAC 的真时长需要它")
        started = self._start()
        with open(FIXTURE, "rb") as fh:
            body = fh.read()
        self._segment(1, wav=body, content_type="audio/flac")
        self.assertTrue(self._stop()["ok"])
        seconds = self._meta(started["message"])["durationSeconds"]
        self.assertAlmostEqual(EXPECTED_SECONDS, seconds, places=2)
        self.assertLess(seconds, 1e6, "SF_COUNT_MAX 的哨兵值漏进 meta 了：%r" % seconds)


if __name__ == "__main__":
    unittest.main()
