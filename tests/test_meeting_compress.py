# -*- coding: utf-8 -*-
"""历史会议音频**无损压缩（FLAC）**的护栏用例（2026-09-26）。

## 需求原话与这一组用例的对应关系

用户看到实测数据后定了方案：**只做 FLAC（无损）**、否掉 Opus ——

    同一段真会议、同一引擎：
      FLAC：省 ~50%，转写文本**逐字不变**
      Opus：省 89%，转写文本差异 **29%**

`sherpa` 只认 RIFF/WAV（喂 FLAC 会报 `file does not start with RIFF id`），
所以必须"**用时解码**"：归档存 FLAC，喂引擎前解成临时 WAV，用完删。

本文件把需求里点名的**七条硬约束**各钉一条：

  1. FLAC 往返：`wav → flac → wav` 与原件**逐样本相等**（时长也一致）；
  2. **解压后引擎可用**：`.flac` 段能被转写路径读取（打桩引擎，断言喂给引擎的是
     **WAV/RIFF**，而不是 flac 本体）；
  3. 损坏/截断的 FLAC → **不删原件** + 明确报错；
  4. `meetingKeepRawAudio` 命中时**不删原件**；
  5. **录音中 / 转写中一律跳过**；
  6. 幂等：对已压缩的会议重复跑**不重复压、不报错**；
  7. 面板入口的"能省多少"（纯函数级）与"已压缩"文案。

## 隔离

`db.DATA_DIR` / `db.DB_FILE` / `settings._cache` / 会议目录 / 临时解码目录全部指向
临时目录，**不碰用户真实会议数据**（与 `tests/test_meeting_import.py` 同一套写法）。
用到的音频全在 `tempfile` 下自造（正弦波），一只麦克风都不开、一个真模型都不加载。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                              # noqa: E402
import soundfile as sf                                          # noqa: E402

import app.db as db                                             # noqa: E402
import app.meeting as meeting                                   # noqa: E402
from app.audio import audiofile as af                           # noqa: E402
from app.capabilities import credentials as cred_mod            # noqa: E402
from app.config import settings                                 # noqa: E402


def tone(seconds=1.0, rate=16000, freq=440.0, amp=0.3):
    """一段**真的**正弦波（不是全零）：往返比对才有意义。"""
    n = int(rate * seconds)
    t = np.arange(n, dtype="float64") / float(rate)
    return (amp * np.sin(2 * np.pi * freq * t) * 32767.0).astype("<i2")


def write_wav(path, samples, rate=16000, channels=1):
    """按录音器那条路写 wav（标准库 `wave`，16-bit PCM）—— 与真产物同形。"""
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def read_wav_samples(path):
    """读 wav 的**原始样本**（逐样本比对用；不经过任何重采样/浮点转换）。"""
    with wave.open(path, "rb") as w:
        return (w.getframerate(), w.getnchannels(),
                np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"))


class _CompressCase(unittest.TestCase):
    """临时库 + 临时会议目录 + 临时解码目录 + 哑掉日志/提示音。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-compress-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "compress.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()

        # 凭据文件隔离：这台开发机真配对过 ECHO 后端，不隔离的话"走哪条转写路"会飘
        cls._cred = os.path.join(cls.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: cls._cred)
        p.start()
        cls.addClassCleanup(p.stop)

        cls.meetings_root = os.path.join(cls.tmp, "meetings")
        os.makedirs(cls.meetings_root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: cls.meetings_root)
        p.start()
        cls.addClassCleanup(p.stop)

        # **临时解码目录也隔离**：默认那个在系统 %TEMP% 下，用例不许往那儿拉屎，
        # 也不许把别人（或正在跑的 ECHO）的临时文件 `gc_temp()` 掉。
        cls.decode_dir = os.path.join(cls.tmp, "decode")
        os.makedirs(cls.decode_dir, exist_ok=True)
        p = patch.object(af, "TEMP_DECODE_DIR", cls.decode_dir)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # 每场会议一个**唯一**目录名（段文件不会互相串）
        self.stamp = "2026-09-%02d_10-00-00" % (len(self._testMethodName) % 28 + 1)
        self.folder = tempfile.mkdtemp(prefix="m-", dir=self.meetings_root)
        self.logs = []
        p = patch.object(meeting.db, "add_log",
                         lambda level, src, msg: self.logs.append((level, src, msg)))
        p.start()
        self.addCleanup(p.stop)
        for target, kw in ((meeting, "_boot_meeting_stt"), (meeting, "_boot_note_meeting_key"),
                           (meeting.tts_mod, "play_beep"), (meeting.tts_mod, "beep_ok")):
            p = patch.object(target, kw, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        settings.update({"meetingAutoSummarize": False,
                         "meetingKeepRawAudio": False})
        # 2026-09-26：分离是会议的必备环节（不再是开关）——这一组用例与分离无关，
        # 不打桩会去加载本机 pyannote 权重（开发机上装着 → 白等几十秒）。
        p = patch("app.audio.diarize.diarize_wav_full",
                  lambda path, max_speakers=None: ([(0.0, 1.0, "SPEAKER_00")],
                                                   [[0.1] * 256], ["SPEAKER_00"]))
        p.start()
        self.addCleanup(p.stop)
        self.name = os.path.basename(self.folder)

    # ---- 造料 ----------------------------------------------------------

    def make_meeting(self, segments=2, seconds=1.0, freq=440.0):
        """在会议目录里造 `01.wav`… + `meta.json`（与录音/导入产物同形）。"""
        segs = []
        for i in range(1, segments + 1):
            name = "%02d.wav" % i
            write_wav(os.path.join(self.folder, name), tone(seconds, freq=freq + i))
            segs.append(name)
        meta = {"start": "2026-09-20T10:00:00", "segments": segs,
                "durationSeconds": seconds * segments,
                "config": {"sttModel": "sensevoice", "segmentMinutes": 10}}
        with open(os.path.join(self.folder, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return segs

    def meta(self):
        with open(os.path.join(self.folder, "meta.json"), encoding="utf-8") as f:
            return json.load(f)

    def seg_path(self, i=1, ext=".wav"):
        return os.path.join(self.folder, "%02d%s" % (i, ext))

    def fail(self, name, folder=None):
        """`_compress_one_meeting` 的薄包装（默认用本用例的会议）。"""
        return meeting._compress_one_meeting(name, folder or self.folder,
                                             keep_raw=bool(settings.get("meetingKeepRawAudio")))

    def preview_for(self, names=None):
        """只算**本用例关心的那几场**（见 `meeting.compression_preview(items=…)`）。

        为什么必须显式点名：预览的默认入口扫的是**整个库**，而本文件的用例共用
        一个临时库（每场会议一个目录）—— 不点名的话断言会依赖"此刻库里恰好有几场"，
        于是用例之间互相干扰（这是实机踩过的假红来源）。
        """
        names = names or [self.name]
        rows = []
        for name in names:
            row = db.get_meeting_by_name(name)
            if not row:
                continue
            kind, why = meeting.compression_state(name)
            rows.append((name, os.path.join(self.meetings_root, name), kind, why,
                         row.get("title") or name))
        return meeting.compression_preview(items=rows)


# ==================================================================== ① 往返

class FlacRoundTripTests(_CompressCase):
    """① `wav → flac → wav` 与原件**逐样本相等**（这是"无损"的唯一定义）。"""

    def test_roundtrip_is_sample_exact_and_same_duration(self):
        path = self.make_meeting(segments=1, seconds=2.0)[0]
        src = os.path.join(self.folder, path)
        before = read_wav_samples(src)
        size_before = os.path.getsize(src)

        res = af.compress_segment(src, keep_raw=False)
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["verified"], "lossless")
        self.assertEqual(res["rmsDelta"], 0.0, "RMS 差必须为 0（逐样本相等）")
        self.assertTrue(os.path.isfile(res["flac"]))
        self.assertFalse(os.path.exists(src), "校验通过后应删掉原件（keep_raw=False）")
        self.assertTrue(res["deleted"])

        # 用时解码 → 与**原字节**逐样本比
        back = af.decode_to_wav(res["flac"])
        self.addCleanup(lambda: os.path.exists(back) and os.remove(back))
        after = read_wav_samples(back)
        self.assertEqual(before[0], after[0], "采样率必须一致")
        self.assertEqual(before[1], after[1], "声道数必须一致")
        self.assertEqual(len(before[2]), len(after[2]), "帧数（时长）必须一致")
        self.assertTrue(np.array_equal(before[2], after[2]), "逐样本必须完全相等")

        # 体积确实降了（省一半左右；FLAC 是**无损**，省多少取决于素材）
        size_after = os.path.getsize(res["flac"])
        self.assertLess(size_after, size_before, "FLAC 必须比 WAV 小")
        self.assertGreater(af.saving_percent(size_before, size_after), 0)

    def test_compare_lossless_detects_a_changed_sample(self):
        """反面对照：动一个样本，`compare_lossless` 必须报"不是无损"。

        没有这条，"逐样本相等"可能只是因为比较函数**永远返回真**
        （那种假绿比红更危险：它会让整条安全链看起来是通的）。
        """
        src = os.path.join(self.folder, self.make_meeting(segments=1, seconds=0.5)[0])
        flac = os.path.splitext(src)[0] + ".flac"
        data, rate = sf.read(src, dtype="int16")
        sf.write(flac, data, rate, format="FLAC", subtype="PCM_16")
        ok, why = af.compare_lossless(src, flac)
        self.assertTrue(ok, why)
        # 改掉一个样本（-1 个 LSB）
        bad = data.copy()
        bad[len(bad) // 2] = bad[len(bad) // 2] - 1
        sf.write(flac, bad, rate, format="FLAC", subtype="PCM_16")
        ok2, why2 = af.compare_lossless(src, flac)
        self.assertFalse(ok2, "改了一个样本却报'无损' —— 比较函数失效了")
        self.assertIn("样本", why2)

    def test_truncated_source_is_refused_and_kept(self):
        """③ 截断的 wav：**原件保留** + 报错里说清为什么。

        ⚠️ 这条用例挡的是一个**真实存在**的坑（2026-09-26 实测）：把 wav 砍掉一半，
        `sf.info()` 与 `wave.getnframes()` **都照样报 16000 帧**（它们信的是头里的
        声明长度）—— 只看这两个数，"截断的段"会一路通过校验，压出一个
        "时长写着 1 秒、实际只有半秒"的 flac。所以判据必须是**字节数对不上**。
        """
        src = os.path.join(self.folder, self.make_meeting(segments=1, seconds=1.0)[0])
        with open(src, "rb") as fh:
            raw = fh.read()
        with open(src, "wb") as f:                  # 砍掉后半截（RIFF 头还在）
            f.write(raw[:len(raw) // 2])
        ok, why = af.can_compress(src)
        self.assertFalse(ok, "截断的 wav 不许被判成可压缩")
        self.assertIn("截断", why)
        res = af.compress_segment(src, keep_raw=False)
        self.assertTrue(os.path.isfile(src), "压缩失败绝不许删原件")
        self.assertFalse(res["deleted"])
        self.assertFalse(res["ok"])
        self.assertIn("截断", res["reason"])
        self.assertFalse(os.path.exists(os.path.splitext(src)[0] + ".flac"),
                         "失败时不许留下半截 flac")
        self.assertFalse(os.path.exists(os.path.splitext(src)[0] + ".flac.part"))
        # 整场跑：这一场不许被算成"已压缩"
        out = self.fail(self.name)
        self.assertFalse(out["ok"])
        self.assertIn("截断", out["reason"])
        self.assertTrue(os.path.isfile(src))
        self.assertIsNone(meeting.compression_info(self.name))

    def test_meeting_level_compression_reports_real_bytes(self):
        """一场会：压完 `meta.json` 里有**真实**的前后字节，面板据此显示"已压缩"。"""
        self.make_meeting(segments=3, seconds=1.0)
        res = self.fail(self.name)
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["deletedSegments"], 3)
        self.assertGreater(res["savedBytes"], 0)
        info = meeting.compression_info(self.name)
        self.assertIsNotNone(info)
        self.assertEqual(info["beforeBytes"], res["beforeBytes"])
        self.assertEqual(info["afterBytes"], res["afterBytes"])
        self.assertTrue(info["deletedRaw"])
        self.assertGreater(info["savedPercent"], 0)
        # 段列表必须同步成盘上真实的那一份（否则重转会去找已删掉的 01.wav）
        self.assertEqual(sorted(self.meta()["segments"]), ["01.flac", "02.flac", "03.flac"])
        self.assertTrue(all(os.path.isfile(self.seg_path(i, ".flac")) for i in (1, 2, 3)))


# ==================================================================== ② 引擎可用

class DecodedAudioIsEngineReadyTests(_CompressCase):
    """② 解压后引擎可用：喂给引擎的**必须是 WAV/RIFF**，不是 flac 本体。"""

    def setUp(self):
        super().setUp()
        # 引擎与"喂进去的路径"全打桩：一个真模型都不加载
        self.seen = []
        self.seen_is_riff = []
        self.transcript = [(0.0, 1.0, "第一句"), (1.0, 2.0, "第二句")]

        def fake_rows(seg_path, seg_idx, seg_min, cfg, cap_kinds):
            self.seen.append(seg_path)
            # **在引擎那一刻**验它拿到的确实是 RIFF/WAV（临时文件在 `with` 里还活着；
            # 退出 `decoded_segments` 才会被删 —— 出了那段再读就是"文件不存在"）。
            try:
                with open(seg_path, "rb") as fh:
                    self.seen_is_riff.append(fh.read(4) == b"RIFF")
            except OSError:
                self.seen_is_riff.append(False)
            return [(seg_idx, s, e, t) for s, e, t in self.transcript], ""

        p = patch.object(meeting, "_sherpa_rows", fake_rows)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(meeting.stt_mod, "_get_sherpa", lambda *a, **k: object())
        p.start()
        self.addCleanup(p.stop)
        settings.update({"meetingSttModel": "sherpa",
                         "meetingAutoCompressAudio": False})

    def test_flac_segment_is_decoded_to_riff_wav_before_reaching_the_engine(self):
        self.make_meeting(segments=2, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.fail(self.name)                       # 先压成 flac（原件删掉）
        self.assertFalse(os.path.exists(self.seg_path(1)), "前置：wav 应已被压掉")

        meeting._transcribe_impl(self.folder)

        self.assertEqual(len(self.seen), 2, "两段都该被喂进引擎")
        self.assertEqual(self.seen_is_riff, [True, True],
                         "喂给引擎的必须是真 RIFF/WAV（sherpa 只认它）：%s" % self.seen)
        for path in self.seen:
            self.assertTrue(path.lower().endswith(".wav"),
                            "喂给引擎的必须是 .wav：%s" % path)
            self.assertFalse(os.path.exists(path), "临时文件用完必须删除")
        # 转写结果正常落库（证明"用时解码"没有把内容弄丢）
        lines = db.get_lines(mid)
        self.assertEqual([ln["text"] for ln in lines], ["第一句", "第二句"])
        # 时间轴不受影响：段时长按 flac 也能算出来（详情页/导出都要它）
        seg_dur = meeting._seg_duration_map(self.folder)
        self.assertEqual(sorted(seg_dur), [1, 2])
        self.assertAlmostEqual(seg_dur[1], 1.0, places=2)

    def test_engine_still_gets_the_original_path_when_segment_is_a_wav(self):
        """回归：**没压缩过的会议，喂进去的还是原来那个路径**（不复制、不解码）。"""
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        meeting._transcribe_impl(self.folder)
        self.assertEqual(self.seen, [self.seg_path(1)])

    def test_corrupt_flac_raises_at_decode_time_and_keeps_the_archive(self):
        """③ 损坏/截断的 FLAC：解码**报错**，且绝不删归档文件本身。"""
        self.make_meeting(segments=1, seconds=1.0)
        self.fail(self.name)
        flac = self.seg_path(1, ".flac")
        with open(flac, "rb") as fh:
            raw = fh.read()
        with open(flac, "wb") as f:                 # 只留文件头 + 一点点数据
            f.write(raw[:64])
        with self.assertRaises(af.CompressionError) as ctx:
            af.decode_to_wav(flac)
        self.assertIn(os.path.basename(flac), str(ctx.exception))
        self.assertTrue(os.path.isfile(flac), "解不开也不能删用户的归档音频")
        # 转写这条路必须**当场失败并留痕**，而不是静悄悄出 0 行
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        with self.assertRaises(af.CompressionError):
            meeting._transcribe_impl(self.folder)


# ==================================================================== ④ keepRaw

class KeepRawAudioTests(_CompressCase):
    """④ `meetingKeepRawAudio` 命中时**不删原件**（压缩照做 —— 它是无损的）。"""

    def test_keep_raw_true_compresses_but_never_deletes(self):
        self.make_meeting(segments=2, seconds=1.0)
        settings.update({"meetingKeepRawAudio": True})
        res = self.fail(self.name)
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertEqual(res["deletedSegments"], 0)
        self.assertTrue(all(os.path.isfile(self.seg_path(i)) for i in (1, 2)),
                        "「保留原始音频」勾着时原件必须还在")
        self.assertTrue(all(os.path.isfile(self.seg_path(i, ".flac")) for i in (1, 2)),
                        "压缩本身照做（FLAC 是无损的，压了不吃亏）")
        info = meeting.compression_info(self.name)
        self.assertFalse(info["deletedRaw"])
        self.assertTrue(info["keptRaw"])

    def test_preview_says_nothing_is_reclaimed_when_keep_raw(self):
        """预览必须把"能少占"与"能腾出来"分开说 —— 否则用户以为压缩骗了他。"""
        self.make_meeting(segments=2, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)

        settings.update({"meetingKeepRawAudio": True})
        keep = self.preview_for()
        row = keep["meetings"][0]
        self.assertEqual(row["compressible"], 2)
        self.assertGreater(row["beforeBytes"], 0)
        self.assertGreater(keep["estimateBytes"], 0)
        self.assertEqual(keep["reclaimBytes"], 0, "保留原件时腾不出空间")
        self.assertIn("不删原件", keep["note"])

        settings.update({"meetingKeepRawAudio": False})
        drop = self.preview_for()
        self.assertGreater(drop["reclaimBytes"], 0, "不保留原件时应当能腾出空间")

    def test_delete_meeting_still_honours_the_setting(self):
        """既有语义不许被这次改动碰坏：勾着时删会议**不删音频目录**。"""
        self.make_meeting(segments=1, seconds=0.5)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        settings.update({"meetingKeepRawAudio": True})
        ok, _msg = meeting.delete_meeting(mid)
        self.assertTrue(ok)
        self.assertTrue(os.path.isdir(self.folder), "勾着「保留原始音频」时不许删目录")


# ==================================================================== ⑤ 正在用

class InUseMeetingsAreSkippedTests(_CompressCase):
    """⑤ 录音中 / 转写中一律跳过（不许动正在用的文件）。"""

    def test_recording_meeting_is_skipped(self):
        self.make_meeting(segments=1, seconds=0.5)
        with meeting._state_lock:
            old = (meeting._state["active"], meeting._state["folder"])
            meeting._state["active"] = True
            meeting._state["folder"] = self.folder
        self.addCleanup(lambda: meeting._state.update(active=old[0], folder=old[1]))
        kind, why = meeting.compression_state(self.name)
        self.assertEqual(kind, "recording")
        self.assertIn("录音", why)
        res = self.fail(self.name)
        self.assertTrue(res["ok"])
        self.assertIn("跳过", res["reason"])
        self.assertIn("录音", res["reason"])
        self.assertEqual(res["segments"], [])
        self.assertTrue(os.path.isfile(self.seg_path(1)), "正在录音的那一场不许动")
        self.assertFalse(os.path.exists(self.seg_path(1, ".flac")))

    def test_transcribing_meeting_is_skipped(self):
        self.make_meeting(segments=1, seconds=0.5)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        db.update_meeting(mid, status="transcribing")
        kind, why = meeting.compression_state(self.name)
        self.assertEqual(kind, "transcribing")
        res = self.fail(self.name)
        self.assertTrue(res["ok"])
        self.assertIn("跳过", res["reason"])
        self.assertTrue(os.path.isfile(self.seg_path(1)))
        self.assertFalse(os.path.exists(self.seg_path(1, ".flac")))

    def test_retranscribing_meeting_is_skipped(self):
        self.make_meeting(segments=1, seconds=0.5)
        with meeting._retranscribing["lock"]:
            meeting._retranscribing["set"].add(self.name)
        self.addCleanup(meeting._retranscribing["set"].discard, self.name)
        kind, _why = meeting.compression_state(self.name)
        self.assertEqual(kind, "retranscribing")

    def test_batch_run_skips_the_in_use_meeting_but_compresses_the_others(self):
        """整批跑：**正在录音的那一场不动**，同时别的会议照压。"""
        self.make_meeting(segments=1, seconds=0.5)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        other = tempfile.mkdtemp(prefix="m2-", dir=self.meetings_root)
        write_wav(os.path.join(other, "01.wav"), tone(1.0))
        other_name = os.path.basename(other)
        with open(os.path.join(other, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"segments": ["01.wav"]}, f)
        mid2 = db.create_meeting(other_name, started_at="2026-09-20T11:00:00")
        self.addCleanup(db.delete_meeting, mid2)

        with meeting._state_lock:
            old = (meeting._state["active"], meeting._state["folder"])
            meeting._state["active"] = True
            meeting._state["folder"] = self.folder
        self.addCleanup(lambda: meeting._state.update(active=old[0], folder=old[1]))

        with patch.object(meeting, "_compress_worker") as worker:
            ok, _msg = meeting.compress_meetings()
            self.assertTrue(ok)
            args = worker.call_args[0]
        # 直接把 worker 跑一遍（拿到"跳过了谁"的真实数字；`compress_meetings`
        # 起的是后台线程，用例里同步跑完才可断言）
        meeting._compress_worker(*args)
        last = meeting.compress_progress()["last"]
        self.assertEqual(last["skipped"], 1, "正在录音的那一场必须被跳过")
        self.assertEqual(last["compressed"], 1)
        self.assertTrue(os.path.isfile(self.seg_path(1)), "录音中的会议不许被动")
        self.assertTrue(os.path.isfile(os.path.join(other, "01.flac")))
        self.assertFalse(os.path.exists(os.path.join(other, "01.wav")))


# ==================================================================== ⑥ 幂等

class IdempotentTests(_CompressCase):
    """⑥ 重复跑不重复压、不报错（且不动已经压好的东西）。"""

    def test_second_run_is_a_noop(self):
        self.make_meeting(segments=2, seconds=1.0)
        first = self.fail(self.name)
        self.assertTrue(first["ok"])
        flac = self.seg_path(1, ".flac")
        stat1 = (os.path.getsize(flac), os.path.getmtime(flac))
        info1 = meeting.compression_info(self.name)

        second = self.fail(self.name)
        self.assertTrue(second["ok"], "重复跑不许报错")
        self.assertEqual(second["reason"], "已经是压缩状态，无需处理")
        self.assertEqual(second["segments"], [])
        self.assertEqual((os.path.getsize(flac), os.path.getmtime(flac)), stat1,
                         "已压好的 flac 不许被重写")
        self.assertEqual(meeting.compression_info(self.name), info1,
                         "压缩记录不许被第二次跑改坏")

    def test_plan_marks_a_compressed_meeting_as_compressed(self):
        self.make_meeting(segments=2, seconds=1.0)
        plan = af.plan_for_meeting(self.folder)
        self.assertEqual(len(plan["compressible"]), 2)
        self.assertFalse(plan["compressed"])
        self.fail(self.name)
        plan2 = af.plan_for_meeting(self.folder)
        self.assertEqual(plan2["compressible"], [])
        self.assertTrue(plan2["compressed"])

    def test_preview_after_compression_counts_nothing_left(self):
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.fail(self.name)
        prev = self.preview_for()
        row = prev["meetings"][0]
        self.assertEqual(row["compressible"], 0)
        self.assertEqual(row["beforeBytes"], 0)
        self.assertTrue(row["compressed"])
        self.assertIn("compressedBytes", row)
        self.assertEqual(prev["count"], 0)
        self.assertEqual(prev["alreadyMeetings"], 1)


# ==================================================================== ⑦ 面板

class PanelTextAndPreviewTests(_CompressCase):
    """⑦ 面板入口的"能省多少"（纯函数级）与"已压缩"文案。"""

    def test_preview_is_read_only(self):
        """预览**一个字节都不许写**（它可能被面板每秒轮询）。"""
        self.make_meeting(segments=2, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        before = sorted(os.listdir(self.folder))
        with open(os.path.join(self.folder, "meta.json"), "rb") as fh:
            meta_before = fh.read()
        prev = self.preview_for()
        self.assertEqual(sorted(os.listdir(self.folder)), before)
        with open(os.path.join(self.folder, "meta.json"), "rb") as fh:
            self.assertEqual(fh.read(), meta_before)
        row = prev["meetings"][0]
        self.assertEqual(row["compressible"], 2)
        self.assertGreater(row["beforeBytes"], 0)
        self.assertGreater(row["estimateBytes"], 0)
        self.assertLess(row["estimateBytes"], row["beforeBytes"], "估算必须小于原始")
        self.assertTrue(prev["beforeText"].endswith(("B", "KB", "MB", "GB")))
        self.assertIn("压缩后会删除原始 WAV", prev["note"])

    def test_preview_excludes_the_meeting_that_is_being_recorded(self):
        self.make_meeting(segments=1, seconds=0.5)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        with meeting._state_lock:
            old = (meeting._state["active"], meeting._state["folder"])
            meeting._state["active"] = True
            meeting._state["folder"] = self.folder
        self.addCleanup(lambda: meeting._state.update(active=old[0], folder=old[1]))
        prev = self.preview_for()
        self.assertEqual(prev["count"], 0, "正在录音的那一场不该算进「可压缩 N 场」")
        row = prev["meetings"][0]
        self.assertEqual(row["skipKind"], "recording")
        self.assertIn("录音", row["skippedReason"])

    def test_size_and_percent_helpers(self):
        self.assertEqual(af.human_size(0), "0 B")
        self.assertEqual(af.human_size(1024), "1.0 KB")
        self.assertEqual(af.human_size(149 * 1024 * 1024), "149.0 MB")
        self.assertEqual(af.saving_percent(149, 76), 49)
        self.assertEqual(af.saving_percent(0, 0), 0)
        self.assertEqual(af.saving_percent(100, 120), 0)

    def test_compressed_marker_text(self):
        """"已压缩"标记的形状与文案（面板直接拼这一份，不许自己再算一遍）。"""
        self.make_meeting(segments=2, seconds=1.0)
        self.assertIsNone(meeting.compression_info(self.name), "没压过就不该有标记")
        self.fail(self.name)
        info = meeting.compression_info(self.name)
        self.assertEqual(info["segments"], 2)
        self.assertTrue(info["at"])
        self.assertEqual(info["beforeText"], af.human_size(info["beforeBytes"]))
        self.assertEqual(info["afterText"], af.human_size(info["afterBytes"]))
        self.assertEqual(
            info["savedText"],
            af.human_size(max(info["beforeBytes"] - info["afterBytes"], 0)))
        self.assertIn("MB", info["beforeText"])
        # 面板拼出来的那句话（`web/app.js` 用的是同一组字段）
        text = "已压缩：原 %s → 现 %s（省 %d%%）" % (
            info["beforeText"], info["afterText"], info["savedPercent"])
        self.assertIn("已压缩：原", text)
        self.assertIn("%", text)

    def test_auto_compress_setting_defaults_to_off(self):
        from app.config import DEFAULTS
        self.assertFalse(DEFAULTS["meetingAutoCompressAudio"]["value"],
                         "「转写完成后自动压缩」必须默认关")
        self.assertTrue(DEFAULTS["meetingKeepRawAudio"]["value"],
                        "「保留原始音频」的既有默认值不许被这次改动翻转")

    def test_auto_compress_toggle_sits_beside_its_siblings(self):
        """「转写完成后自动压缩」必须挂在「录音与产出」小节里，**不许掉进「其他」**。

        `sAdvSection()` 的判据链是：`SET_ADV_SEC[键]` → 后端 `sub` → 卡的 `advDefault`
        → `"其他"`。`meetingAutoCompressAudio` 既没有后端 `sub`（`config.py` 里只写了
        `grp="meeting"`），会议卡也没写 `advDefault` —— 所以**不显式登记就一定会被
        追加成一个小节名叫「其他」的组**，与它的两个同类（自动生成纪要 / 保留原始音频）
        在界面上分家。功能上它还在（`renderAdvSections` 会把没排进 `advOrder` 的小节
        追加在后面），所以这是个**不会报错、只会显得莫名其妙**的坑 —— 正是需要用例
        盯住的那一类。2026-09-26 修的就是它。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        block = re.search(r"const SET_ADV_SEC = \{(.*?)\n\};", js, re.S)
        self.assertIsNotNone(block, "找不到 SET_ADV_SEC —— 落点表的形状变了，用例要跟着改")
        hit = re.search(r"meetingAutoCompressAudio:\s*\"([^\"]+)\"", block.group(1))
        self.assertIsNotNone(
            hit, "meetingAutoCompressAudio 没有显式小节名 —— 它会被画到「其他」小节里")
        self.assertEqual(hit.group(1), "录音与产出",
                         "它应当与「自动生成纪要 / 保留原始音频」同一个小节")

    def test_detail_page_renders_the_compression_marker(self):
        """会议**详情页**（`web/meeting.html`）必须真的把 `compression` 画出来。

        接口给了字段、页面不画 = 用户还是看不见（这正是 2026-09-26 复查时的状态：
        列表卡片有标记、详情页一个字都没有）。这条用例盯的是页面那一段 ——
        落点在、渲染函数在、且它读的是与列表卡片**同一组字段名**
        （`beforeText` / `afterText` / `savedPercent`），不允许自己重算一遍。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "meeting.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('id="compressMark"', html, "详情页没有「已压缩」标记的落点")
        self.assertIn("renderCompressionMark()", html, "落点在，但没有渲染它的调用")
        body = re.search(r"function renderCompressionMark\(\)\s*\{(.*?)\n\}",
                         html, re.S)
        self.assertIsNotNone(body, "找不到 renderCompressionMark 的实现")
        for token in ("M.compression", "beforeText", "afterText", "savedPercent"):
            self.assertIn(token, body.group(1),
                          "详情页的标记少了 %s —— 数字必须来自后端那一份" % token)


class AutoCompressAfterTranscribeTests(_CompressCase):
    """可选项：`meetingAutoCompressAudio`（默认关）—— 开了才压，且只压这一场。"""

    def test_off_by_default_does_nothing(self):
        self.make_meeting(segments=1, seconds=0.5)
        settings.update({"meetingAutoCompressAudio": False})
        meeting._maybe_auto_compress(self.folder)
        self.assertTrue(os.path.isfile(self.seg_path(1)))
        self.assertFalse(os.path.exists(self.seg_path(1, ".flac")))

    def test_on_compresses_only_this_meeting(self):
        self.make_meeting(segments=1, seconds=0.5)
        settings.update({"meetingAutoCompressAudio": True, "meetingKeepRawAudio": False})
        meeting._maybe_auto_compress(self.folder)
        self.assertTrue(os.path.isfile(self.seg_path(1, ".flac")))
        self.assertFalse(os.path.exists(self.seg_path(1)))

    def test_on_but_keep_raw_keeps_the_original(self):
        self.make_meeting(segments=1, seconds=0.5)
        settings.update({"meetingAutoCompressAudio": True, "meetingKeepRawAudio": True})
        meeting._maybe_auto_compress(self.folder)
        self.assertTrue(os.path.isfile(self.seg_path(1, ".flac")))
        self.assertTrue(os.path.isfile(self.seg_path(1)))

    def test_failure_never_propagates(self):
        """自动压缩**绝不许**把异常抛回转写那条路（用户听到"叮叮"）。"""
        settings.update({"meetingAutoCompressAudio": True})
        with patch.object(meeting, "_compress_one_meeting",
                          side_effect=RuntimeError("boom")):
            meeting._maybe_auto_compress(self.folder)      # 不抛 = 通过


class ApiEndpointTests(_CompressCase):
    """接口面：三个新端点 + **播放那一路读 flac**（面板直接依赖这几个）。

    为什么单独立一组：面板点的是**接口**而不是函数，`compress_segments` 全绿也
    不代表接口通了（路由顺序/返回形状/媒体类型都可能错）。特别是
    `/meetings/compress/*` —— FastAPI 按声明顺序匹配，写晚了会被 `{mid}: int`
    吃掉并回一个查不出原因的 422。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_preview_endpoint_shape(self):
        self.make_meeting(segments=2, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        r = self.client.get("/api/meetings/compress/preview")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for key in ("count", "beforeBytes", "estimateBytes", "reclaimBytes",
                    "keepRawAudio", "note", "beforeText", "alreadyMeetings", "meetings"):
            self.assertIn(key, body, "预览少了字段 %s" % key)
        row = [m for m in body["meetings"] if m["name"] == self.name]
        self.assertEqual(len(row), 1, "预览必须逐场列出（面板要显示每场能省多少）")
        self.assertEqual(row[0]["compressible"], 2)

    def test_compress_endpoints_are_not_eaten_by_the_int_route(self):
        """路由顺序守卫：三个**真用**的端点必须是 200，绝不能是 422。

        这是"改完看着挺好、点下去报 422"的那类坑：`/meetings/{mid}` 里 `mid: int`
        声明在前面的话，`compress` 会被它先吃掉、由 int 校验拒掉（422）——
        而静态路由永远到不了。所以三条声明必须排在那条之前（`app/api.py` 里有注释）。

        顺带钉住一条**故意保留**的行为：`GET /meetings/compress` 没有静态路由
        （只有 `POST`），于是它落到 `{mid}: int` 上得到 **422**（而不是 405）。
        这不影响用户 —— 面板一次都不会这么调；但写清楚，免得日后有人
        看到 422 以为是这次的 bug 又去"修"一遍。
        """
        self.assertEqual(self.client.get("/api/meetings/compress/preview").status_code, 200)
        self.assertEqual(self.client.get("/api/meetings/compress/status").status_code, 200)
        self.assertEqual(self.client.post("/api/meetings/compress", json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/meetings/compress/preview").status_code, 405)
        self.assertIn(self.client.get("/api/meetings/compress").status_code, (405, 422))

    def test_status_endpoint_reports_progress_and_last(self):
        self.make_meeting(segments=1, seconds=0.5)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.client.post("/api/meetings/compress", json={})
        # 后台线程：等它跑完再断言（用例里不等就会看到 running=True 的中间态）
        for _ in range(60):
            body = self.client.get("/api/meetings/compress/status").json()
            if not body.get("running"):
                break
            time.sleep(0.2)
        self.assertFalse(body["running"])
        self.assertEqual(body["percent"], 100)
        self.assertIsNotNone(body["last"])
        self.assertIn("message", body["last"])

    def test_list_endpoint_carries_the_compression_marker(self):
        """列表接口必须带 `compression`（面板卡片读的就是它）。"""
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        items = self.client.get("/api/meetings").json()["items"]
        row = [it for it in items if it["id"] == mid][0]
        self.assertIn("compression", row)
        self.assertIsNone(row["compression"], "没压过时必须是 None（面板据此不显示标记）")
        self.fail(self.name)
        row = [it for it in self.client.get("/api/meetings").json()["items"]
               if it["id"] == mid][0]
        cp = row["compression"]
        self.assertIsNotNone(cp)
        self.assertGreater(cp["beforeBytes"], 0)
        self.assertGreater(cp["afterBytes"], 0)
        self.assertLess(cp["afterBytes"], cp["beforeBytes"])
        self.assertIn("beforeText", cp)
        self.assertIn("afterText", cp)
        self.assertGreater(cp["savedPercent"], 0)
        self.assertIn("MB", cp["beforeText"] + cp["afterText"])

    def test_detail_endpoint_carries_the_compression_marker(self):
        """详情接口必须**真的带** `compression` —— 会议详情页读的就是它。

        2026-09-26 复查发现的一个真缺口：这个字段原来只有**列表**接口有
        （`api.list_meetings` 里那句 `compression_info()`），详情接口没有 ——
        而 `openMeetingDetail()` 打开的是独立页 `web/meeting.html`，它读的是这份
        detail。于是表现是"列表卡片上看得见、点进详情就没了"。

        上一版这条用例断的是 `meeting.compression_info(detail["name"])` ——
        那只证明**那个函数能跑**，接口漏了字段它照样绿。现在断的是**接口字段本身**。
        """
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)

        d0 = self.client.get("/api/meetings/%d" % mid).json()
        self.assertIn("compression", d0, "详情接口少了 compression 字段")
        self.assertIsNone(d0["compression"], "没压过时必须是 None（详情页据此不显示标记）")

        self.fail(self.name)
        detail = self.client.get("/api/meetings/%d" % mid).json()
        cp = detail["compression"]
        self.assertIsNotNone(cp, "压过之后详情接口必须带 compression")
        for key in ("beforeBytes", "afterBytes", "beforeText", "afterText",
                    "savedPercent", "deletedRaw", "keptRaw", "at", "segments"):
            self.assertIn(key, cp, "详情页要用的字段少了 %s" % key)
        self.assertGreater(cp["beforeBytes"], cp["afterBytes"])
        self.assertGreater(cp["afterBytes"], 0)
        # 与列表卡片**同源**：两处必须是同一份数字（否则同一个会议两个说法）
        row = [it for it in self.client.get("/api/meetings").json()["items"]
               if it["id"] == mid][0]
        self.assertEqual(row["compression"]["beforeBytes"], cp["beforeBytes"])
        self.assertEqual(row["compression"]["afterText"], cp["afterText"])
        self.assertEqual(row["compression"]["savedPercent"], cp["savedPercent"])

    def test_audio_endpoint_serves_a_playable_wav_after_compression(self):
        """**面板播放**那一路：`01.wav` 已被压成 `01.flac` 也必须能播，且回的是 WAV。"""
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        with open(self.seg_path(1), "rb") as fh:
            original = fh.read()
        self.fail(self.name)                       # 原件删掉，只剩 flac
        self.assertFalse(os.path.exists(self.seg_path(1)))

        r = self.client.get("/api/meetings/%d/audio?seg=1" % mid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("content-type", "").split(";")[0], "audio/wav",
                         "浏览器对 audio/flac 支持不一致 —— 这条接口必须回 WAV")
        self.assertEqual(r.content[:4], b"RIFF")
        self.assertEqual(len(r.content), len(original), "解回来的 WAV 必须与原文件等长")
        self.assertTrue(os.path.isfile(self.seg_path(1, ".flac")), "归档文件不许被动")

    def test_audio_endpoint_404s_for_a_segment_that_does_not_exist(self):
        self.make_meeting(segments=1, seconds=0.5)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.assertEqual(self.client.get("/api/meetings/%d/audio?seg=99" % mid).status_code, 404)


class TempDecodeHygieneTests(_CompressCase):
    """临时解码文件的卫生：**用完即删**、`gc_temp` 只删自己的东西。"""

    def test_decoded_context_manager_removes_the_temp_file(self):
        path = os.path.join(self.folder, "01.flac")
        sf.write(path, tone(0.5) / 32768.0, 16000, format="FLAC", subtype="PCM_16")
        with af.decoded(path) as wav_path:
            self.assertTrue(os.path.isfile(wav_path))
            self.assertEqual(wav_path.lower()[-4:], ".wav")
        self.assertFalse(os.path.exists(wav_path), "退出 with 就该删掉临时文件")

    def test_wav_passes_through_untouched(self):
        src = os.path.join(self.folder, "01.wav")
        write_wav(src, tone(0.2))
        with af.decoded(src) as wav_path:
            self.assertEqual(wav_path, src, "本来就是 wav 的段不复制、不解码")

    def test_gc_temp_only_touches_its_own_prefixed_files(self):
        mine = os.path.join(self.decode_dir, af.TEMP_PREFIX + "junk.wav")
        theirs = os.path.join(self.decode_dir, "someone-else.wav")
        for p in (mine, theirs):
            open(p, "wb").close()
        os.utime(mine, (0, 0))
        os.utime(theirs, (0, 0))
        removed = af.gc_temp(max_age=1)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.exists(theirs), "只许删自己前缀的文件")

    def test_dry_run_does_not_touch_other_meetings(self):
        """硬约束：**不碰其它会议、不删数据目录里的其它文件**。"""
        self.make_meeting(segments=1, seconds=0.5)
        other = tempfile.mkdtemp(prefix="m3-", dir=self.meetings_root)
        other_wav = os.path.join(other, "01.wav")
        write_wav(other_wav, tone(0.5))
        stray = os.path.join(self.folder, "handwritten-notes.txt")
        with open(stray, "w", encoding="utf-8") as f:
            f.write("别删我")
        self.fail(self.name)
        self.assertTrue(os.path.isfile(other_wav), "别的会议的音频不许被动")
        self.assertTrue(os.path.isfile(stray), "会议目录里别的东西不许被删")


class DiarizeOnFlacSegmentTests(_CompressCase):
    """② 的**第三条**调用点：说话人分离那一路也必须拿到解出来的 WAV。

    为什么单独立一条（2026-09-26 复查补的）：转写那条路（`_sherpa_rows` 收到的是不是
    RIFF）与播放那条路（接口回的是不是 WAV）本来就有用例，**唯独分离没有** ——
    而分离恰恰是三条里最容易漏的一条：它在 `_transcribe_impl` 的循环里拿的是同一个
    `seg_path`，看着"顺手就对了"；可一旦有人把 `seg_path` 换回原路径，它未必报错
    （pyannote 那条路对容器的宽容度与 sherpa 不同），表现会是"转写正常、分离悄悄
    与转写分家"。所以这里断言的**不是"分离成功了"**，而是"喂给分离的就是那条解出来
    的临时 WAV，而且用完就没了"。
    """

    def setUp(self):
        super().setUp()
        # ASR 打桩：本用例只关心分离拿到什么，不加载真模型。
        p = patch.object(meeting, "_sherpa_rows",
                         lambda path, idx, m, cfg, kinds: ([(idx, 0.0, 1.0, "一句话")], ""))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(meeting.stt_mod, "_get_sherpa", lambda *a, **k: object())
        p.start()
        self.addCleanup(p.stop)
        settings.update({"meetingSttModel": "sherpa"})

    def test_diarize_receives_the_decoded_riff_wav_not_the_flac(self):
        self.make_meeting(segments=2, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.fail(self.name)                       # 先压成 flac（原件删掉）
        self.assertFalse(os.path.exists(self.seg_path(1)), "前置：wav 应已被压掉")

        seen = []

        def spy(path, max_speakers=None):
            # 在**分离那一刻**读头：临时文件还在；出了 `decoded_segments` 就没了。
            with open(path, "rb") as fh:
                seen.append((path, fh.read(4)))
            return [(0.0, 1.0, "SPEAKER_00")], [[0.1] * 256], ["SPEAKER_00"]

        p = patch("app.audio.diarize.diarize_wav_full", spy)
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        self.assertEqual(len(seen), 2, "两段都该被送进分离")
        for path, head in seen:
            self.assertEqual(head, b"RIFF", "分离拿到的必须是 RIFF/WAV，不是 flac：%s" % path)
            self.assertTrue(path.lower().endswith(".wav"), path)
            self.assertFalse(os.path.exists(path), "分离用完，临时 WAV 必须已经删掉")


class PlaybackDecodeHygieneTests(_CompressCase):
    """播放那一路的"用完即删"：临时 WAV 在**响应发完之后**立刻删，不留到下一小时。

    修的是什么（2026-09-26 复查发现的两处"看着有其实没有"）：
      * 此前 `meeting_audio` 把 `decode_to_wav()` 的产物**登记了却从不删**，
        只靠 `gc_temp()`（TTL 1 小时）兜底；
      * 而注释里写的"退出时的清理兜底"当时**并不存在** ——
        `audiofile.cleanup_registered()` 只被定义、全仓没有一个调用方。
    现在：响应挂 `BackgroundTask` 发完即删（`api._drop_temp_decode`），
    `app/main.py` 的 lifespan 关闭段补上 `cleanup_registered()` 当第二道闸。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def _temp_leftovers(self):
        try:
            return sorted(n for n in os.listdir(self.decode_dir)
                          if n.startswith(af.TEMP_PREFIX))
        except OSError:
            return []

    def test_response_is_wav_and_the_temp_decode_is_deleted(self):
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.fail(self.name)

        r = self.client.get("/api/meetings/%d/audio?seg=1" % mid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content[:4], b"RIFF")
        self.assertEqual(self._temp_leftovers(), [],
                         "播放完临时解码文件必须已经删掉（不许留到 gc_temp）")
        self.assertTrue(os.path.isfile(self.seg_path(1, ".flac")), "归档文件不许被动")

    def test_the_same_segment_plays_twice_without_accumulating(self):
        """连播两次：每次都回得出内容，且**每次都不留临时文件**（防"删早了"回归）。"""
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.fail(self.name)
        for _ in range(2):
            r = self.client.get("/api/meetings/%d/audio?seg=1" % mid)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content[:4], b"RIFF")
            self.assertEqual(self._temp_leftovers(), [])

    def test_the_decode_is_logged(self):
        """日志留痕：翻日志要能看出"这一段为什么在 %TEMP% 里落了文件"。"""
        self.make_meeting(segments=1, seconds=1.0)
        mid = db.create_meeting(self.name, started_at="2026-09-20T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        self.fail(self.name)
        self.client.get("/api/meetings/%d/audio?seg=1" % mid)
        texts = [m for _lv, _src, m in self.logs]
        hit = [t for t in texts if "FLAC" in t and "用时解码" in t and "删" in t]
        self.assertTrue(hit, "应当有一条说明「解了哪段 / 多大 / 用完即删」的日志：%s" % texts)


class TempRegistryTests(_CompressCase):
    """`drop_temp()` / `cleanup_registered()` 本身：删得掉、登记也撤得干净。"""

    def test_drop_temp_removes_the_file_and_unregisters_it(self):
        # 直接走真解码造一个登记过的临时文件（`decode_to_wav(cleanup=True)` 会登记它）
        src = self.make_meeting(segments=1, seconds=0.2)[0]
        path = af.decode_to_wav(os.path.join(self.folder, src))
        self.assertTrue(os.path.isfile(path))
        self.assertIn(path, af._TEMP_REGISTRY)
        self.assertTrue(af.drop_temp(path))
        self.assertFalse(os.path.exists(path), "drop_temp 必须真的删掉文件")
        self.assertNotIn(path, af._TEMP_REGISTRY, "登记也必须撤掉（否则 cleanup_registered 白跑）")
        # 再删一次不抛（幂等）
        self.assertTrue(af.drop_temp(path))

    def test_cleanup_registered_removes_everything_it_registered(self):
        src = self.make_meeting(segments=1, seconds=0.2)[0]
        paths = [af.decode_to_wav(os.path.join(self.folder, src)) for _ in range(2)]
        self.assertTrue(all(os.path.isfile(p) for p in paths))
        af.cleanup_registered()
        self.assertTrue(all(not os.path.exists(p) for p in paths),
                        "进程退出兜底必须删掉登记过的临时文件")
        self.assertEqual(af._TEMP_REGISTRY, set())


if __name__ == "__main__":
    unittest.main()
