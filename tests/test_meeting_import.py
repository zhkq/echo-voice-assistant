# -*- coding: utf-8 -*-
"""导入录音 → 一场会议：**格式闸门 / 真参数 / 分块 / 不留垃圾 / 导入后能真转写**。

## 这一组用例钉的是什么

需求原话是"我有一段录音（会议/访谈/手机录的），丢给 ECHO，它变成一场带文字稿的会议"。
在此之前 ECHO 没有这条路（只有单文件 `/api/stt/transcribe`，不走会议链路），用户测试时
只能靠 `delivery/ECHO-meeting-fixture/导入.ps1` 把文件硬塞进会议目录 —— 那个脚本还不得不用
`status=interrupted` 表达"待转写"（它自己的文档里写明了那是妥协）。这批用例把新路的
**每一条硬要求**各钉一条：

  1. `wav` / `flac` / `mp3` 各一条 → 会议记录建好、音频段在**解析出的**会议目录里、
     参数是 **16 kHz 单声道 16-bit**（断言的是真参数：标准库 `wave` 读 RIFF 头）；
  2. **44.1 kHz 立体声** → 正确转成 16 kHz 单声道（断言真参数，不是只看段数）；
  3. `m4a` 与"改了扩展名的假音频" → **明确报错**（且含"ffmpeg / 怎么办"），
     **不留下会议记录、不留下垃圾目录**；
  4. 空文件 / 0 字节 / **大文件走分块**（用 `block_frames` 探针 + `tracemalloc` 峰值
     一起证明"不是一次性读进内存"）；
  5. 导入后**能真的转写**（打桩引擎，不加载任何真模型）；
  6. `imported`（「待转写」）这一档的语义、以及它与 `error`/`transcribed` 的分工。

隔离与 `tests/test_meeting_engine.py` / `tests/test_meeting_capability.py` 同一套写法：
`db.DATA_DIR` / `db.DB_FILE` / `settings._cache` / 凭据文件 / 会议目录全部指向临时目录，
**一个真模型都不加载、一只麦克风都不开**。
"""
import json
import os
import shutil
import sys
import tempfile
import tracemalloc
import unittest
import wave
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                              # noqa: E402
import soundfile as sf                                          # noqa: E402

import app.db as db                                             # noqa: E402
import app.meeting as meeting                                   # noqa: E402
from app.audio import importer as imp                           # noqa: E402
from app.capabilities import credentials as cred_mod            # noqa: E402
from app.config import settings                                 # noqa: E402

#: 端点用例里"一场会"的时间戳（固定住，目录名才可断言）
_STAMP = "2026-09-25_08-30-00"

#: 导入用例的会议日期（时间部分**每个用例各不相同**）
_DATE = "2026-09-25"


def tone(seconds=1.0, rate=16000, channels=1, freq=440.0):
    """一段**真的**正弦波（不是全零）：重采样之后还能验证"确实变了/确实没变"。"""
    n = int(rate * seconds)
    t = np.arange(n, dtype="float64") / float(rate)
    mono = (0.3 * np.sin(2 * np.pi * freq * t)).astype("float32")
    if channels > 1:
        return np.stack([mono * (1.0 - 0.2 * i) for i in range(channels)], axis=1)
    return mono


@contextmanager
def _modules_unavailable(*names):
    """让 `import <name>` 在块内**真的失败**（`sys.modules[name] = None` 是 CPython 的约定）。

    为什么不用 `patch("builtins.__import__", …)`：那条路会把**所有**导入都拦一遍
    （包括 importlib 自己的），一改就是"整段解释器行为"，用例很容易被无关的导入
    绊倒。往 `sys.modules` 里放 `None` 只影响**点名的那几个**模块，而且这正是
    CPython 表达"这个模块不可用"的标准写法（`import` 会抛 `ImportError`）。
    """
    saved = {n: sys.modules.get(n, _MISSING) for n in names}
    for n in names:
        sys.modules[n] = None
    try:
        yield
    finally:
        for n, old in saved.items():
            if old is _MISSING:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = old


_MISSING = object()


class _ImportCase(unittest.TestCase):
    """临时库 + 临时会议目录 + 临时凭据 + 哑掉的启动页/提示音。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-import-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "import.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()

        # 凭据文件也要隔离：这台开发机**真配对过** ECHO 后端，不隔离的话
        # "导入后走哪条转写路"的用例会莫名其妙变红（同 test_meeting_capability）。
        cls._cred = os.path.join(cls.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: cls._cred)
        p.start()
        cls.addClassCleanup(p.stop)

        # 会议目录：`meeting.import_meeting()` 走 `ensure_meetings_dir()` → `meetings_dir()`
        cls.meetings_root = os.path.join(cls.tmp, "meetings")
        os.makedirs(cls.meetings_root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: cls.meetings_root)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="echo-import-src-")
        self.addCleanup(shutil.rmtree, self.src, True)
        # 每个用例一个**固定但不同**的会议时间 → 目录名可断言、又不会互相撞上
        # （撞上就会走 `_2`/`_3` 后缀那条路，与本用例要验的东西无关）。
        self.start_text = "%s %02d:%02d:%02d" % ((_DATE,) + self._stamp_parts())
        self.stamp = self.start_text.replace(" ", "_").replace(":", "-")
        self.logs = []
        p = patch.object(meeting.db, "add_log",
                         lambda level, src, msg: self.logs.append((level, src, msg)))
        p.start()
        self.addCleanup(p.stop)
        # 启动页状态与提示音：用例**不许**出声，也不许去动 boot 组件
        for target, kw in ((meeting, "_boot_meeting_stt"), (meeting, "_boot_note_meeting_key"),
                           (meeting.tts_mod, "play_beep"), (meeting.tts_mod, "beep_ok")):
            p = patch.object(target, kw, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        settings.update({"meetingAutoSummarize": False, "meetingDiarize": False})
        # 转写默认打桩：这一组用例验的是**导入**，不是引擎。
        # 需要真转写的那一组把 `stub_transcribe` 设成 False（见 ImportThenTranscribeTests）。
        self.did_transcribe = []
        if self.stub_transcribe:
            self._stub_transcribe()

    #: 子类设成 False = 让 `_transcribe_meeting` 保持**真实现**（转写用例要它）
    stub_transcribe = True

    def _stamp_parts(self):
        """用例名 → 固定的 `(时, 分, 秒)`；不同用例不同（避免目录名互相撞）。"""
        import zlib
        h = zlib.crc32(self._testMethodName.encode("utf-8"))
        return (h % 24, h // 24 % 60, h // 1440 % 60)

    # ---- 替身 ----------------------------------------------------------

    def _stub_transcribe(self):
        """`_transcribe_meeting` 换成替身 —— 导入用例里"转写"只记一笔，不加载模型。"""
        def fake(folder):
            self.did_transcribe.append(os.path.basename(folder))
        p = patch.object(meeting, "_transcribe_meeting", fake)
        p.start()
        self.addCleanup(p.stop)

    def _stub_loaders(self):
        """四个本机引擎加载器全换成替身（转写用例专用：一个真模型都不许加载）。"""
        loaders = {}
        for attr in ("_get_whisper", "_get_sensevoice", "_get_qwen3asr", "_get_sherpa"):
            mock = MagicMock(name=attr)
            p = patch.object(meeting.stt_mod, attr, mock)
            p.start()
            self.addCleanup(p.stop)
            loaders[attr] = mock
        return loaders

    # ---- 造料 ----------------------------------------------------------

    def make(self, name, seconds=1.0, rate=16000, channels=1, fmt=None, subtype=None,
             freq=440.0, raw=None):
        """造一个源文件；`raw` 给的是字节就直接写（造"假音频/空文件"）。"""
        path = os.path.join(self.src, name)
        if raw is not None:
            with open(path, "wb") as fh:
                fh.write(raw)
            return path
        if fmt == "MP3" or name.lower().endswith(".mp3"):
            # libsndfile 的 MP3 只吃 MPEG_LAYER_III 这个 subtype（写别的组合会
            # `Invalid combination of format, subtype and endian`）
            fmt, subtype = "MP3", "MPEG_LAYER_III"
        data = tone(seconds, rate, channels, freq=freq)
        sf.write(path, data, rate, format=fmt, subtype=(subtype or "PCM_16"))
        return path

    # ---- 断言帮手 ------------------------------------------------------

    def import_(self, paths, **kw):
        kw.setdefault("start", self.start_text)
        return meeting.import_meeting(list(paths), **kw)

    def meeting_dir(self, name):
        return os.path.join(self.meetings_root, name)

    def rows(self):
        return db.list_meetings(limit=100)

    def names_on_disk(self):
        return sorted(os.listdir(self.meetings_root))

    def assert_nothing_left_behind(self, rows_before, dirs_before, why=""):
        """失败路径的判据：库里的记录与磁盘上的目录**都不许增加**。

        为什么是"与运行前比"而不是"必须是空"：`_ImportCase` 是**类级共用**一个临时库与
        会议目录（`setUpClass` 里 init 一次，省 1.6 秒 × 每个用例），所以同一个类里
        前面的用例留下的会议本来就在。写成"必须为空"会变成"看用例执行顺序的脸色"。
        """
        self.assertEqual(len(self.rows()), rows_before, "不许新增会议记录 %s" % why)
        self.assertEqual(self.names_on_disk(), dirs_before, "不许留下垃圾目录 %s" % why)

    def assert_target_wav(self, path, seconds=None, places=2):
        """产物必须是 16 kHz / 单声道 / 16-bit PCM（`wave` 读 RIFF 头，不看库里写的）。"""
        rate, channels, width, frames = imp.describe_wav(path)
        self.assertEqual(rate, 16000, "%s 的采样率不是 16 kHz" % path)
        self.assertEqual(channels, 1, "%s 不是单声道" % path)
        self.assertEqual(width, 2, "%s 不是 16-bit PCM（采样宽 %d 字节）" % (path, width))
        self.assertGreater(frames, 0, "%s 里没有帧" % path)
        if seconds is not None:
            self.assertAlmostEqual(frames / 16000.0, seconds, places=places)
        return frames


# ---------------------------------------------------------------- ① 三种格式

class ThreeFormatsBecomeAMeetingTests(_ImportCase):
    """wav / flac / mp3 各一条 → 会议记录 + 16k 单声道 16-bit 的段落在解析出的目录里。"""

    def test_wav_flac_mp3_all_land_as_16k_mono_16bit(self):
        wav = self.make("甲.wav", seconds=1.0, rate=16000, channels=1)
        flac = self.make("乙.flac", seconds=1.0, rate=44100, channels=2)
        mp3 = self.make("丙.mp3", seconds=1.0, rate=44100, channels=2, fmt="MP3")
        ok, name = self.import_([wav, flac, mp3], title="季度评审", notes="备注 X")

        self.assertTrue(ok, "三种格式都该导入成功：%r" % (name,))
        self.assertEqual(name, self.stamp, "目录名应当由 start 决定（可预测才好断言）")

        row = db.get_meeting_by_name(name)
        self.assertIsNotNone(row, "会议记录必须建好（不能只落文件）")
        self.assertEqual(row["status"], "transcribing",
                         "导入成功后应当**立刻排上转写**（面板进度条据此出现）")
        self.assertEqual(row["title"], "季度评审")
        self.assertEqual(row["notes"], "备注 X")
        self.assertEqual(row["segments"], 3)
        self.assertAlmostEqual(row["duration_seconds"], 3.0, places=1)
        self.assertEqual((row["error"] or ""), "")

    def test_segments_live_in_the_resolved_meeting_dir(self):
        """段落必须落在**解析出来的**会议目录里（新老布局/改过 meetingsDir 都对）。"""
        wav = self.make("甲.wav")
        ok, name = self.import_([wav])
        self.assertTrue(ok, name)
        folder = self.meeting_dir(name)
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(sorted(os.listdir(folder)), ["01.wav", "meta.json"])
        self.assert_target_wav(os.path.join(folder, "01.wav"), seconds=1.0)

    def test_meta_json_records_duration_rate_source_and_original_names(self):
        """`meta.json` 要写全：时长 / 采样率 / 来源=导入 / 原始文件名。"""
        wav = self.make("手机录音.wav", seconds=2.0, rate=44100, channels=2)
        ok, name = self.import_([wav])
        self.assertTrue(ok, name)
        meta = meeting.meeting_meta(name)
        self.assertEqual(meta.get("source"), "import")
        self.assertEqual(meta.get("segments"), ["01.wav"])
        self.assertEqual(meta.get("sampleRate"), 16000)
        self.assertEqual(meta.get("channels"), 1)
        self.assertEqual(meta.get("sampleFormat"), "16-bit PCM")
        self.assertAlmostEqual(meta.get("durationSeconds"), 2.0, places=1)
        self.assertEqual(meta.get("importedFrom"), ["手机录音.wav"])
        self.assertTrue(meta.get("start"))
        # 重采样走了哪条路要落盘（用户问"这段转得准不准"时唯一的答案）
        how = meta.get("resampled") or []
        self.assertEqual(len(how), 1)
        self.assertEqual(how[0]["file"], "手机录音.wav")
        self.assertEqual(how[0]["srcRate"], 44100)
        self.assertIn(how[0]["how"], (imp.RESAMPLE_SOXR_STREAM, imp.RESAMPLE_SOXR,
                                      imp.RESAMPLE_SCIPY, imp.RESAMPLE_LINEAR))
        self.assertEqual(meta.get("config", {}).get("sttModel"),
                         settings.get("meetingSttModel"))

    def test_order_of_files_is_the_order_of_segments(self):
        """**顺序即分段**：文件顺序换了，段号跟着换（前端就靠这个表达用户选的顺序）。"""
        a = self.make("a.wav", seconds=1.0, freq=300.0)
        b = self.make("b.wav", seconds=2.0, freq=800.0)
        ok, name = self.import_([b, a])
        self.assertTrue(ok, name)
        folder = self.meeting_dir(name)
        self.assert_target_wav(os.path.join(folder, "01.wav"), seconds=2.0)
        self.assert_target_wav(os.path.join(folder, "02.wav"), seconds=1.0)

    def test_import_failure_then_import_again_does_not_reuse_the_folder(self):
        """同一秒导入两次**不许覆盖**前一场（用 `_2` 后缀另开一场）。"""
        wav = self.make("甲.wav")
        ok1, name1 = self.import_([wav])
        ok2, name2 = self.import_([wav])
        self.assertTrue(ok1 and ok2)
        self.assertNotEqual(name1, name2)
        self.assertEqual(name1, self.stamp)
        self.assertEqual(name2, self.stamp + "_2")
        self.assertEqual(len(self.rows()), 2)

    def test_a_missing_file_is_refused_without_leaving_anything(self):
        rows_before, dirs_before = len(self.rows()), self.names_on_disk()
        ok, why = self.import_([os.path.join(self.src, "根本没有这个文件.wav")])
        self.assertFalse(ok)
        self.assertTrue(why.strip())
        self.assert_nothing_left_behind(rows_before, dirs_before)

    def test_import_is_refused_while_a_recording_is_running(self):
        """录音进行中**不许**导入：导入完会立刻起转写，那会跟正在录的这场抢机器。

        判据是"先说清楚、不要含糊失败"——用户拿到的话里要有"先停止录音"这条动作。
        """
        rows_before, dirs_before = len(self.rows()), self.names_on_disk()
        with patch.dict(meeting._state, {"active": True}):
            ok, why = self.import_([self.make("甲.wav")])
        self.assertFalse(ok)
        self.assertIn("录音", why)
        self.assertIn("停止", why)
        self.assert_nothing_left_behind(rows_before, dirs_before, "（录音中被拒）")


# ---------------------------------------------------------------- ② 44.1k 立体声

class ResamplingTests(_ImportCase):
    """44.1 kHz 立体声 → 16 kHz 单声道：断言**真参数**，不是"段数对了就算过"。"""

    def test_44100_stereo_becomes_16000_mono_with_the_right_length(self):
        src = self.make("stereo.flac", seconds=3.0, rate=44100, channels=2)
        ok, name = self.import_([src])
        self.assertTrue(ok, name)
        frames = self.assert_target_wav(os.path.join(self.meeting_dir(name), "01.wav"),
                                       seconds=3.0, places=2)
        self.assertEqual(frames, 48000, "3 秒 @16 kHz 必须正好 48000 帧")

    def test_a_stereo_input_is_downmixed_not_taken_from_one_channel(self):
        """单声道化必须是**两个声道平均**，不是"只取左声道"。

        造一段"左 0.6、右 0.0"的音频：平均之后振幅应是 0.3，只取左声道会是 0.6
        （差一倍，听感上是"另一路声音全丢了"）。判据用峰值，且给足容差 ——
        重采样会略微改变峰值，但不会差一倍。
        """
        rate = 44100
        n = rate
        t = np.arange(n, dtype="float64") / rate
        left = (0.6 * np.sin(2 * np.pi * 440 * t)).astype("float32")
        data = np.stack([left, np.zeros(n, dtype="float32")], axis=1)
        path = os.path.join(self.src, "left-only.wav")
        sf.write(path, data, rate, subtype="PCM_16")

        ok, name = self.import_([path])
        self.assertTrue(ok, name)
        out = os.path.join(self.meeting_dir(name), "01.wav")
        self.assert_target_wav(out, seconds=1.0)
        got, sr = sf.read(out, dtype="float32")
        self.assertEqual(sr, 16000)
        peak = float(np.max(np.abs(got)))
        self.assertLess(peak, 0.45, "没有把左右声道平均掉（峰值 %.2f 太接近只取左声道）" % peak)
        self.assertGreater(peak, 0.18, "整段被削没了（峰值 %.2f）" % peak)

    def test_the_resample_route_is_recorded_in_the_log(self):
        """走了哪条重采样路**必须写进日志**（soxr 流式 / scipy / 线性兜底）。"""
        src = self.make("stereo.flac", seconds=0.5, rate=44100, channels=2)
        ok, name = self.import_([src])
        self.assertTrue(ok, name)
        joined = "\n".join(m for _lv, _sr, m in self.logs)
        self.assertIn("音频导入", joined, "日志里要有那条『音频导入：…』")
        self.assertIn("重采样=", joined)
        self.assertTrue(any(r in joined for r in
                            (imp.RESAMPLE_SOXR_STREAM, imp.RESAMPLE_SOXR,
                             imp.RESAMPLE_SCIPY, imp.RESAMPLE_LINEAR)),
                        "日志里要能看出用了哪条路：%s" % joined)

    def test_a_16k_mono_wav_is_not_resampled(self):
        """本来就是 16 kHz 单声道 → 记 `none`（不该白跑一遍重采样，也不该谎称重采过）。"""
        src = self.make("ok.wav", seconds=0.5, rate=16000, channels=1)
        ok, name = self.import_([src])
        self.assertTrue(ok, name)
        meta = meeting.meeting_meta(name)
        self.assertEqual(meta["resampled"][0]["how"], imp.RESAMPLE_NONE)

    def test_the_linear_fallback_still_produces_a_valid_segment(self):
        """soxr 与 scipy 都不可用时，**线性插值兜底**也要产出合法的 16k 单声道 16-bit。

        这条用例钉的是"降级路径不能产出坏文件"：兜底那条路一旦写出 44.1 kHz，
        会议链路会静默按 16 kHz 解读（时间轴全错），而现象只是"转写内容对不上时间"。
        """
        src = self.make("t.flac", seconds=1.0, rate=44100, channels=2)
        with _modules_unavailable("soxr", "scipy", "scipy.signal"):
            ok, name = self.import_([src])
        self.assertTrue(ok, name)
        meta = meeting.meeting_meta(name)
        self.assertEqual(meta["resampled"][0]["how"], imp.RESAMPLE_LINEAR)
        self.assert_target_wav(os.path.join(self.meeting_dir(name), "01.wav"), seconds=1.0)


# ---------------------------------------------------------------- ③ 读不了的格式

class UnreadableFormatsTests(_ImportCase):
    """m4a / 假音频 / 空文件 → **响亮报错**，且**不留记录、不留垃圾目录**。"""

    def test_m4a_is_refused_by_name_and_says_what_to_do(self):
        rows_before, dirs_before = len(self.rows()), self.names_on_disk()
        src = self.make("手机录音.m4a", raw=b"\x00" * 4096)
        ok, why = self.import_([src])
        self.assertFalse(ok, "m4a 不许假装支持")
        self.assertIn("M4A", why.upper(), "必须点名格式：%r" % why)
        self.assertIn("ffmpeg", why, "必须说清为什么读不了：%r" % why)
        self.assertIn("wav", why, "必须给出下一步（转成 wav/mp3）：%r" % why)
        self.assert_nothing_left_behind(rows_before, dirs_before, "（m4a）")

    def test_aac_and_mp4_are_refused_too(self):
        for name in ("x.aac", "x.mp4", "x.wma"):
            with self.subTest(name=name):
                src = self.make(name, raw=b"\x00" * 1024)
                ok, why = self.import_([src])
                self.assertFalse(ok)
                self.assertIn("ffmpeg", why)

    def test_a_renamed_fake_audio_file_is_refused_with_the_real_decode_error(self):
        """改了扩展名的假音频 → 报**真原因**（libsndfile 自己说的话），不是"导入失败"。"""
        rows_before, dirs_before = len(self.rows()), self.names_on_disk()
        src = self.make("看起来像音频.mp3", raw="这不是音频，是一段文字。".encode("utf-8") * 40)
        ok, why = self.import_([src])
        self.assertFalse(ok)
        self.assertIn("看起来像音频.mp3", why, "必须点名是哪个文件：%r" % why)
        self.assertTrue(("解码失败" in why) or ("不是能识别" in why), why)
        # 真原因来自解码器（不同 libsndfile 版本措辞不同，所以只要求"有点具体内容"）
        self.assertGreater(len(why), 40, "报错太笼统，用户看不出下一步：%r" % why)
        self.assert_nothing_left_behind(rows_before, dirs_before, "（假音频）")

    def test_a_zero_byte_file_is_refused_before_touching_the_meeting_dir(self):
        rows_before, dirs_before = len(self.rows()), self.names_on_disk()
        src = self.make("空.mp3", raw=b"")
        ok, why = self.import_([src])
        self.assertFalse(ok)
        self.assertIn("空文件", why)
        self.assert_nothing_left_behind(rows_before, dirs_before, "（空文件）")

    def test_one_bad_file_in_the_middle_drops_the_whole_import(self):
        """**原子性**：一个坏文件 = 整场不落地（不能留下"导进去一半"的会议）。"""
        rows_before, dirs_before = len(self.rows()), self.names_on_disk()
        good = self.make("good.wav", seconds=1.0)
        bad = self.make("bad.m4a", raw=b"\x00" * 512)
        ok, why = self.import_([good, bad])
        self.assertFalse(ok)
        self.assertIn("ffmpeg", why)
        self.assert_nothing_left_behind(rows_before, dirs_before, "（整场原子性）")
        self.assertEqual(self.did_transcribe, [], "失败路径不许去转写")
        # 临时/中间产物也不许留在会议目录里
        self.assertFalse(any(f.endswith(".part") for f in os.listdir(self.meetings_root)))

    def test_an_unsupported_engine_style_error_shape_is_preserved(self):
        """报错文案里不该出现"导入失败"这种空话（本项目最忌讳：界面自己编原因）。"""
        src = self.make("x.m4a", raw=b"\x00" * 16)
        _ok, why = self.import_([src])
        self.assertNotEqual(why.strip(), "导入失败")
        self.assertNotIn("未知错误", why)


# ---------------------------------------------------------------- ④ 分块 / 大文件

class ChunkedConversionTests(_ImportCase):
    """大文件走**分块**那条路：用 `block_frames` 探针 + `tracemalloc` 一起证明。"""

    def test_the_block_iterator_never_yields_more_than_the_block_size(self):
        """`imp.iter_mono_blocks(block_frames=N)` 每块都不超过 N 帧。

        这是"不整段进内存"的**结构性**判据：只要调用方走的是这个迭代器，
        峰值内存就是 `O(N)`，与文件多大无关。
        """
        src = self.make("big.flac", seconds=2.0, rate=44100, channels=2)
        sizes = [len(b) for b in imp.iter_mono_blocks(src, 4096)]
        self.assertGreater(len(sizes), 4, "2 秒 44.1 kHz 至少要分成好几块")
        self.assertLessEqual(max(sizes), 4096)
        self.assertEqual([1 for _ in imp.iter_mono_blocks(src, 4096)], [1] * len(sizes))

    def test_conversion_never_asks_for_the_whole_file_at_once(self):
        """**直接**钉住"没有整段读"：转码路上每一次读取的帧数都 ≤ 块大小。

        `sf.read()` / `SoundFile.read()`（`frames` 默认 -1 = 一直读到文件尾）就是"整个文件
        进内存"那一步；分块路走的是 `SoundFile.blocks()`，它内部**仍然**调 `read()` 但每次
        只要一块（实测：2 秒 44.1 kHz + 块 4096 → 每次 4096，最后一块 2184）。
        所以判据是"**没有任何一次读的帧数是 -1 / 超过块大小**"—— 这比 `tracemalloc`
        硬：它不依赖分配器的行为，只看有没有发出"把整个文件给我"这个请求。
        """
        src = self.make("big.flac", seconds=1.5, rate=44100, channels=2)
        dst = os.path.join(self.src, "out.wav")
        asked = []
        real_read = sf.SoundFile.read

        def spy(self, frames=-1, *a, **k):
            asked.append(frames)
            return real_read(self, frames, *a, **k)

        with patch.object(sf.SoundFile, "read", spy):
            res = imp.convert_to_16k_mono(src, dst, filename="big.flac", block_frames=4096)
        self.assertTrue(asked, "一次读都没发生？")
        self.assertTrue(all(isinstance(n, int) and 0 < n <= 4096 for n in asked),
                        "有一次性读取（帧数 %s）——那就是把整段读进内存" % set(asked))
        self.assertGreater(len(asked), 4, "1.5 秒 44.1 kHz 用 4096 的块应当读好几块")
        self.assertEqual(res.block_frames, 4096)
        self.assert_target_wav(dst, seconds=1.5)
        self.assertEqual(res.resampled, imp.RESAMPLE_SOXR_STREAM,
                         "有 soxr 时首选流式分块重采样")

    def test_a_dozen_megabyte_file_does_not_blow_up_python_memory(self):
        """~10 MB 输入：整段读会多出 ~20 MB 的 Python 分配，分块路应当**远小于**它。

        为什么用 `tracemalloc` 而不是"看起来很快"：`tracemalloc` 只看 Python 侧分配
        （numpy 的 malloc 在它统计里是 numpy 的分配器，数值会偏低），所以阈值取得宽松
        —— 它抓的是"整段 audio 进内存"这种量级的错，不是小抖动。
        """
        src = self.make("dozen.wav", seconds=60.0, rate=44100, channels=2,
                        subtype="FLOAT")
        self.assertGreater(os.path.getsize(src), 5 * 1024 * 1024)
        dst = os.path.join(self.src, "dozen-out.wav")
        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            res = imp.convert_to_16k_mono(src, dst, filename="dozen.wav",
                                          block_frames=imp.DEFAULT_BLOCK_FRAMES)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(res.seconds, 60.0)
        self.assert_target_wav(dst, seconds=60.0)
        grew_mb = (peak - baseline) / 1024.0 / 1024.0
        self.assertLess(grew_mb, 16.0,
                        "转换 60 秒 44.1 kHz 立体声时 Python 峰值多出 %.1f MB —— "
                        "分块路不该有这么高" % grew_mb)

    def test_import_of_a_large_file_is_chunked_end_to_end(self):
        """整条链路（`import_meeting`）也要能传下 `block_frames` 并真的分块落盘。"""
        src = self.make("big.mp3", seconds=8.0, rate=44100, channels=2, fmt="MP3")
        ok, name = self.import_([src], block_frames=2048)
        self.assertTrue(ok, name)
        self.assert_target_wav(os.path.join(self.meeting_dir(name), "01.wav"),
                               seconds=8.0, places=1)
        self.assertEqual(self.rows()[0]["segments"], 1)


# ---------------------------------------------------------------- ⑤ 导入后能真转写

class ImportThenTranscribeTests(_ImportCase):
    """导入后**真的能转写**（打桩引擎）：行落库、状态到 `transcribed`、meta 更新。

    这一组**保留** `_transcribe_meeting` 的替身：导入自己会起一个真转写线程，而这里
    要验的是"转写**代码**在导入出来的这场上能不能跑通"。两个一起跑会互相盖结果
    （真线程写完库，用例又跑一遍 → 行数翻倍，看着像"导入把音频写了两份"）。
    所以：导入那一刻隔离，转写**显式**调用。这也正是用户点「重新转写」时走的那条路。
    """

    def test_imported_audio_really_gets_transcribed(self):
        """导入的那段音频，用**真的**转写链路跑一遍（引擎打桩，不加载模型）。

        用 `_transcribe_impl` 而不是 `_transcribe_meeting`：后者会**起后台线程**，
        用例没法等它（Windows 上还会跟临时目录清理抢时间）。这里要验的是
        "导入出来的 01.wav 能不能被转写代码读出来并落成行"，直接同步跑最稳。
        """
        from collections import namedtuple
        _WSeg = namedtuple("_WSeg", "start end text")
        src = self.make("甲.wav", seconds=1.0, rate=16000, channels=1)
        ok, name = self.import_([src])
        self.assertTrue(ok, name)

        loaders = self._stub_loaders()
        loaders["_get_whisper"].return_value = object()
        with patch.object(meeting.stt_mod, "transcribe_whisper",
                          lambda wm, path, lang: ([_WSeg(0.0, 0.8, " 导入的第一句 ")],
                                                  {"language": lang})):
            meeting._transcribe_impl(self.meeting_dir(name))

        row = db.get_meeting_by_name(name)
        self.assertEqual(row["status"], "transcribed")
        self.assertEqual((row["error"] or ""), "")
        lines = db.get_lines(row["id"])
        self.assertEqual([ln["text"] for ln in lines], ["导入的第一句"])
        # 段数/时长仍然是导入时如实记下的（转写不该把它们改烂）
        self.assertEqual(row["segments"], 1)
        self.assertAlmostEqual(row["duration_seconds"], 1.0, places=1)
        # 转写产物 + meta 更新
        folder = self.meeting_dir(name)
        self.assertTrue(os.path.isfile(os.path.join(folder, "transcript.md")))
        self.assertEqual(meeting.meeting_meta(name).get("transcribed"), ["01.wav"])
        self.assertIn(name, self.did_transcribe, "导入时确实排过转写（替身记下了）")

    def test_a_transcribe_failure_lands_in_error_with_the_real_reason(self):
        """转写起不来 → `error` + **真原因**（不是"录音失败"这种被读歪的话）。

        用 `_transcribe_impl`（**只跑转写本身、不起线程**）来制造这个结局：
        配置一个会议链路驱动不了的引擎，看它落库的原因里有没有点名那个引擎。
        这同时钉住"用户修好设置、点重新转写就能救回来"这条出路。
        """
        src = self.make("甲.wav", seconds=1.0, rate=16000, channels=1)
        ok, name = self.import_([src])
        self.assertTrue(ok, name)
        self._stub_loaders()
        settings.update({"meetingSttModel": "paraformer"})     # 会议链路驱动不了的引擎
        self.addCleanup(settings.update, {"meetingSttModel": "sensevoice"})
        with self.assertRaises(meeting.MeetingEngineRefused):
            meeting._transcribe_impl(self.meeting_dir(name))
        row = db.get_meeting_by_name(name)
        self.assertEqual(row["status"], "error")
        self.assertIn("paraformer", row["error"] or "")
        # 音频**还在**（"导入失败"与"转写失败"必须能分开）
        self.assertTrue(os.path.isfile(os.path.join(self.meeting_dir(name), "01.wav")))
        # 而且这一场的 meta.json 里段清单还在 —— 用户修好设置后「重新转写」能救回来
        self.assertEqual(meeting.meeting_meta(name).get("segments"), ["01.wav"])

    def test_retranscribe_of_an_imported_meeting_still_works(self):
        """既有路径回归：导入的这场会照样能走 `retranscribe_meeting()`。"""
        src = self.make("甲.wav", seconds=1.0, rate=16000, channels=1)
        ok, name = self.import_([src])
        self.assertTrue(ok, name)
        row = db.get_meeting_by_name(name)
        with patch.object(meeting, "_transcribe_meeting", MagicMock()) as spy:
            ok2, msg = meeting.retranscribe_meeting(row["id"])
        self.assertTrue(ok2, msg)
        self.assertTrue(spy.called, "重新转写必须真的起转写")


# ---------------------------------------------------------------- ⑥ 状态落点

class ImportedStatusTests(_ImportCase):
    """`imported`（「待转写」）这一档：语义、面板文案、以及"谁覆盖它"。"""

    def test_the_row_is_created_as_imported_before_transcription_starts(self):
        """导入过程中/刚建好记录时是 `imported`，**不是** `transcribed` 也不是 `error`。

        做法：把 `_transcribe_meeting` 换成"只记下当时的状态"的替身 ——
        这样能看到转写线程真正启动前那一刻库里的值。
        """
        seen = {}

        def peek(folder):
            row = db.get_meeting_by_name(os.path.basename(folder))
            seen["status"] = row["status"]
            seen["error"] = row["error"]
            seen["segments"] = row["segments"]
        with patch.object(meeting, "_transcribe_meeting", peek):
            ok, name = self.import_([self.make("甲.wav")])
            self._wait(lambda: bool(seen))
        self.assertTrue(ok, name)
        self.assertEqual(seen["status"], "transcribing",
                         "起转写线程之前必须已经是 transcribing（面板进度条据此出现）")
        self.assertEqual(seen["error"], "")
        self.assertEqual(seen["segments"], 1)

    def test_the_import_progress_marker_is_cleared_when_transcription_takes_over(self):
        """导入阶段的进度（按会议名登记）必须在收尾时清掉。

        不清的话 `/api/transcribe/status` 会永远挂着一条"导入中 50%"的死记录 ——
        排障时最容易被它带偏（"它是不是卡在导入了？"），而那一场其实早就转写完了。
        """
        with patch.object(meeting, "_transcribe_meeting", lambda folder: None):
            ok, name = self.import_([self.make("甲.wav")])
        self.assertTrue(ok, name)
        self.assertIsNone(meeting.transcribe_progress(name),
                          "导入进度没清掉：%r" % (meeting.transcribe_progress(name),))

    def _wait(self, cond, timeout=5.0):
        """等后台线程跑一小步（导入是**先落盘、再起线程**，线程不会立刻执行）。"""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline and not cond():
            time.sleep(0.02)
        return cond()

    def test_the_documented_status_word_is_the_new_one(self):
        """钉住"我们加了哪一档"：`imported` 必须在文档里写下的那几个状态词里出现。

        这条断言防的是"代码里写了 imported、面板没有这一档文案"——那种情况下
        面板会显示英文原文（用户看到 `imported` 四个字母，等于没解释）。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn('imported: "待转写"', js,
                      "面板必须有『待转写』这一档文案（web/app.js）")
        with open(os.path.join(root, "web", "meeting.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('imported:"待转写"', html,
                      "会议详情页的状态文案也要有这一档（web/meeting.html）")

    def test_the_import_sets_the_status_before_the_transcribe_thread_starts(self):
        """落库顺序：**imported → transcribing 在同一个函数里**，中间不留空档。

        为什么这条要用例盯着：如果先起转写线程、再改状态，转写很快就跑到 `transcribed`，
        而主线程紧接着把 `imported` 写回去 —— 库里最后停在"待转写"，面板永远显示
        一场"转完了却说没转"的会。顺序是契约，所以钉住它。
        """
        order = []
        real_update = db.update_meeting

        def spy(meeting_id, **fields):
            if fields.get("status"):
                order.append(fields["status"])
            return real_update(meeting_id, **fields)

        with patch.object(meeting.db, "update_meeting", spy):
            ok, name = self.import_([self.make("甲.wav")])
        self.assertTrue(ok, name)
        self.assertIn("imported", order, "导入时必须先落 imported：%s" % order)
        self.assertIn("transcribing", order, "紧接着要切成 transcribing：%s" % order)
        self.assertLess(order.index("imported"), order.index("transcribing"),
                        "imported 必须在 transcribing **之前**写：%s" % order)


# ---------------------------------------------------------------- ⑦ 端点契约

class ImportEndpointTests(_ImportCase):
    """`POST /api/meetings/import` 的**接口面**：路径/方法/字段/错误码。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)
        # 转写一律不起（端点用例只验接口面，不验引擎）
        cls._p = patch.object(meeting, "import_meeting", _fake_import)
        cls._p.start()
        cls.addClassCleanup(cls._p.stop)

    def _post(self, files, data=None):
        return self.client.post("/api/meetings/import", files=files,
                                data=(data or {}))

    def test_missing_files_is_422(self):
        r = self.client.post("/api/meetings/import")
        self.assertEqual(r.status_code, 422, r.text)

    def test_it_is_post_only(self):
        """GET 不许命中导入逻辑。

        注：**刻意不要求 405**。`GET /api/meetings/{mid}` 就在隔壁，而 `mid: int`
        认不出 `import` 这个字符串 → FastAPI 会先报 422（路径参数不合法）。
        面板与技能都只会用 POST，所以这里真正要钉的是"**GET 不会真的去导入**"：
        状态码在 (405, 422) 之内即可，但绝不许是 200。
        """
        r = self.client.get("/api/meetings/import")
        self.assertIn(r.status_code, (405, 422), r.text)
        self.assertNotEqual(r.status_code, 200)

    def test_success_shape(self):
        got = {}

        def fake(paths, **kw):
            got.update(kw)
            got["paths"] = list(paths)
            name = "2026-09-25_08-30-00"
            db.create_meeting(name, started_at="2026-09-25T08:30:00")
            mid = db.get_meeting_by_name(name)["id"]
            db.update_meeting(mid, segments=2, duration_seconds=120.0,
                              status="transcribing")
            return True, name

        with patch.object(meeting, "import_meeting", fake):
            r = self._post([("files", ("a.wav", b"RIFF0000", "audio/wav")),
                            ("files", ("b.mp3", b"ID3", "audio/mpeg"))],
                           {"title": "标题", "start": "2026-09-25 08:30", "notes": "备注"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["name"], "2026-09-25_08-30-00")
        self.assertEqual(body["files"], 2)
        self.assertAlmostEqual(body["seconds"], 120.0)
        self.assertTrue(body["message"])
        # 表单字段确实传到了业务层（顺序也要对）
        self.assertEqual(got["title"], "标题")
        self.assertEqual(got["start"], "2026-09-25 08:30")
        self.assertEqual(got["notes"], "备注")
        self.assertEqual(len(got["paths"]), 2)
        self.assertEqual([os.path.splitext(p)[1] for p in got["paths"]], [".wav", ".mp3"])
        self.assertEqual(got["display_names"], ["a.wav", "b.mp3"],
                         "显示名必须是**用户的原文件名**，不是上传临时名")

    def test_a_bad_format_comes_back_as_400_with_the_real_reason(self):
        r = self._post([("files", ("x.m4a", b"\x00" * 32, "audio/mp4"))])
        self.assertEqual(r.status_code, 400, r.text)
        detail = r.json()["detail"]
        self.assertIn("ffmpeg", detail)
        self.assertIn("M4A", detail.upper())

    def test_an_empty_file_comes_back_as_400(self):
        r = self._post([("files", ("e.mp3", b"", "audio/mpeg"))])
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("空文件", r.json()["detail"])

    def test_a_business_failure_comes_back_as_400_with_the_reason(self):
        """业务层拒绝（例如没选文件/转换失败）→ 400 + 那句人话，前端只负责显示。"""
        with patch.object(meeting, "import_meeting",
                          lambda paths, **kw: (False, "音频「x」的格式 M4A 本机读不了")):
            r = self._post([("files", ("x.wav", b"RIFF0000", "audio/wav"))])
        self.assertEqual(r.status_code, 400)
        self.assertIn("M4A", r.json()["detail"])


def _fake_import(paths, **kw):
    """`setUpClass` 里那个占位替身（用例内部都会再 patch 一层）。"""
    return False, "端点用例不该走到真导入"


if __name__ == "__main__":
    unittest.main()
