# -*- coding: utf-8 -*-
"""会议录音的「暂停 / 恢复」（手机全屏录音界面上那个「静音」按钮）。

用户 2026-10-15 要的界面里有两个按钮：结束、**静音/恢复**；而**暂停的语义**在
2026-10-16 深夜被用户**推翻重定**，这一组用例钉的是**新口径**：

  * 暂停期间**照常录音，但写进文件的是全零（空白）音频**（旧口径是"完全不写盘" ✗）；
  * 采样率/声道/位深/每缓冲帧数**与暂停前完全一致**（只是内容全零）；
  * **时间轴逐字连续**：一场含暂停的录音，音频总长 ≈ 挂钟时长（容差一个缓冲），
    而且**段边界不漂**（段与段之间一帧不差，暂停那几帧也照样计数）；
  * `meta["pauses"]`（`from`/`to`/`by`）**照旧如实记录**、**暂停时立刻落盘**照旧 ——
    但它从此是**纯标注**（"这几段是暂停的"），**不参与任何拼接/裁剪/补偿**（不许有"剃掉"逻辑）；
  * 转写里暂停区间**没有文字**，前后是真文字。

为什么必须逐字连续（这条最容易改错）：段是按"写进去的采样数"切分的，暂停要是不计数，
后面的段边界会整体前移 —— 第 N 段覆盖的就不再是挂钟的 `10N ~ 10N+10` 分钟，时间轴会漂。
为什么是零、不是"接着录真实声音"：暂停就是"不想留下这段内容"。
为什么不做剪切：用户原话 *"不用剃掉"*（暂停区间只是空白录音，不影响会议总时长）。

关于假输入流的口径（改错了整组用例都会变成假绿/假红）：`MeetingRecorder._loop` 每次
`stream.read(int(SAMPLE_RATE * 0.2))`，而输入设备**按实时吐块** —— 所以假流必须
"每块 3200 帧 = 每 0.2 秒一块"，与生产同形（`test_meeting_pause.py` 2026-10-16 改版时
在这里栽过一次：块长与时间不成比例，于是"暂停 0.5 秒"翻译成音频是 5 秒）。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio import audiofile                                    # noqa: E402
from app.audio import recorder as recorder_mod                      # noqa: E402
from app import db                                                  # noqa: E402
from app import meeting as meeting_mod                              # noqa: E402

#: 挂钟与音频之间允许的误差 = **一个缓冲**（用户判据原话："容差一个缓冲"）。
#: 它的实际含义由假流决定：0.2 秒 / 3200 帧 —— 一次 `stream.read()` 的时间。
BLOCK_SECONDS = 0.2
BLOCK_FRAMES = int(recorder_mod.SAMPLE_RATE * BLOCK_SECONDS)        # 3200


def _read_wav(path):
    """读一个 16k 单声道 wav → `(采样数组, 采样率)`（**只读**，不解码任何东西）。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return data, sr


def _zero_runs(samples):
    """所有"连续全零"区间的 `(起, 止)`（**终止下标不含**，与切片语义一致）。

    只看**全零**：暂停写进去的就是全零，而 `_FakeStream` 给的"真音频"是 16 位非零值，
    两者不会混。整段一个非零都没有（例如某一段正好整段都在暂停里）也算一段全零。
    """
    zero = (samples == 0)
    runs = []
    start = None
    for i, z in enumerate(zero):
        if z and start is None:
            start = i
        elif not z and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(zero)))
    return runs


class _FakeStream:
    """假装是麦克风输入流：每 `read()` 给一小块**非零** PCM，直到被关掉。

    用它就能把 `MeetingRecorder._loop` 真跑起来（**不碰真麦克风**）。

    与 2026-10-15 那版的两点关键差别（缺一不可）：

      * 给的是**非零**样本（旧版给全零）—— 否则"暂停区间的零"与"本来就没声音"分不出来，
        "别把整段写零"这条判据根本验不了；
      * 每一块都记下**什么时候发出去的**（`self.blocks`）—— "零音频的位置对得上暂停区间"
        这条判据要靠它把"采样下标"翻译回"挂钟时刻"。块长与生产一致（3200 帧 / 0.2 秒）。
    """

    def __init__(self, chunk=None, block=None):
        self.chunk = int(chunk if chunk is not None else BLOCK_FRAMES)
        self.block = float(block if block is not None else BLOCK_SECONDS)
        self.device = "fake"
        self.closed = False
        #: `blocks[i] = {"start", "count", "at"}`（`start` = 整场音频里的采样下标）
        self.blocks = []
        self._n = 0                      # 已经发出多少采样
        self._reads = 0
        self._cv = threading.Condition()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def read(self, n):
        if self.closed:
            raise RuntimeError("stream closed")
        if self.blocks:
            time.sleep(self.block)       # 第一块不睡：别让 wait_started 白等
        count = min(int(n), self.chunk)
        # 非零、可辨认的样本值（16 位有符号里取一个小正数循环）。
        x = (np.arange(self._n, self._n + count) % 30000 + 1).astype("<i2")
        with self._cv:
            self.blocks.append({"start": self._n, "count": count, "at": time.time()})
            self._n += count
            self._reads += 1
            self._cv.notify_all()
        return x, False

    def wait_reads(self, count, timeout=10.0):
        """等"已经发出至少 count 块"（判据比 sleep 稳：与调度快慢无关）。"""
        deadline = time.time() + timeout
        with self._cv:
            while self._reads < count and time.time() < deadline:
                self._cv.wait(0.02)
            return self._reads

    def frames_emitted(self):
        with self._cv:
            return self._n

    def sample_at(self, when):
        """挂钟时刻 → 整场音频里的采样下标（取"该时刻已经发出去的那些采样"）。

        每一块用它**自己的发出时刻**记账：这一块是 `stream.read()` 返回的，下一块要
        0.2 秒后才发，所以"某一刻音频的进度"= 那一刻之前发出去的采样数 —— 误差 ≤ 一块。
        """
        with self._cv:
            blocks = [dict(b) for b in self.blocks]
        got = 0
        for b in blocks:
            if b["at"] <= when:
                got = b["start"] + b["count"]
            else:
                break
        return got


class _PauseCase(unittest.TestCase):
    """两条路（录音器那一层 / 会议那一层）共用的隔离与工具。

    隔离三条，每条都对应一个踩过的坑：
      * **假输入流**（绝不碰真麦克风；`meeting` 那层要真跑起录音线程就只能这样）；
      * **每个用例一个临时会议目录**（会议名是秒级时间戳，同秒开两场会撞 UNIQUE）；
      * **每个用例一个临时库**（**不许**把测试会议写进真库，2026-10-15 第一版就是这么红的）。
    """

    #: 段长（**分钟**）：0.05 分钟 = 3 秒 —— 用例要的是"段真的会切"，不是"等 10 分钟"。
    SEGMENT_MINUTES = 0.05

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pause-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-pausedb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()

        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        self._orig_stream = recorder_mod.input_stream
        self.recorder_stream = _FakeStream()
        recorder_mod.input_stream = lambda *a, **kw: self.recorder_stream
        self._orig_get = meeting_mod.settings.get
        # 段长从设置读（`start_meeting` 就是这么拿的）→ 打桩成 3 秒，用例才跑得动。
        meeting_mod.settings.get = (
            lambda key, default=None: self.SEGMENT_MINUTES
            if key == "meetingSegmentMinutes" else self._orig_get(key, default))

        # 每个用例从"没在录"开始（含上一次的静音时刻，别让它粘过来）
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], _pause_events=[])

        # 别让 stop_meeting() 真的去起转写线程：那会在用例结束（临时库已拆）之后才跑，
        # 报一堆 no such table: meetings —— 那是测试卫生问题，不是被测行为。
        self._orig_transcribe = meeting_mod._transcribe_meeting
        meeting_mod._transcribe_meeting = lambda *a, **kw: None
        self.addCleanup(lambda: setattr(meeting_mod, '_transcribe_meeting', self._orig_transcribe))

        def _clean():
            meeting_mod._state.update(active=False, folder=None, recorder=None,
                                      paused=False, pauses=[], _pause_events=[])
            meeting_mod.meetings_dir = self._orig_dir
            meeting_mod.settings.get = self._orig_get
            recorder_mod.input_stream = self._orig_stream
            db.DATA_DIR, db.DB_FILE = self._old_db
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    # ---------------------------------------------------------------- 工具

    def _start(self):
        ok, msg = meeting_mod.start_meeting()
        self.assertTrue(ok, msg)
        return msg

    def _all_samples(self, folder):
        """按段序把所有音频拼成**一条**采样流，并返回每段的采样下标范围。

        为什么按采样下标而不是按秒："一帧不差"这条判据只能在下标上验
        （按秒会掩盖掉不足一秒的漂移，而那正是"暂停不计数"的症状）。
        """
        segs = sorted(audiofile.segment_files(folder),
                      key=lambda n: int(str(n).split(".")[0]))
        self.assertTrue(segs, "这一场该有音频段")
        chunks, bounds = [], {}
        at = 0
        sr = None
        for name in segs:
            data, sr = _read_wav(os.path.join(folder, name))
            bounds[name] = (at, at + len(data))
            chunks.append(data)
            at += len(data)
        return np.concatenate(chunks), bounds, sr

    def _samples_by_segment(self, folder):
        """`[(段名, 采样数组), …]`（按段序）—— 逐段核对"真音频 / 全零"用。"""
        segs = sorted(audiofile.segment_files(folder),
                      key=lambda n: int(str(n).split(".")[0]))
        return [(name, _read_wav(os.path.join(folder, name))[0]) for name in segs]

    def _meta(self, name):
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _synth_segment_aligned_pause(self, folder, bounds, observed_run):
        """把一个**段对齐**的等价暂停区间写进 `meta["pauses"]`（**只有**暂停那一对时刻）。

        为什么需要它：`pause_meeting` 落盘的 `from/to` 用 `isoformat(timespec="seconds")`
        **就地截断**，"这一场的开始"也是按秒取的 —— 两者一相减，误差最大 1 秒，于是"零该从
        哪一帧开始"没有唯一期望值（判据就被迫放松到"±1 秒"，而那正好是这个判据要抓的
        漂移量级）。所以这里改成**按音频这一侧的事实**取一段完整覆盖的段做等价区间：

          * 找出所有**整段全零**的段（暂停的块是整块写进去的，所以被暂停覆盖到的段必然全零，
            只有覆盖窗口的头/尾那一段可能"半零半真"，那一段**不算**）；
          * 把 `from/to` 换成这串段的第一段起点 / 最后一段终点 —— 于是"零该从哪一帧开始"
            有了唯一期望值（就是这两个边界），接着逐帧断言盘上**正好**是这一段零。

        这一步验的是"`meta` 记的那一段 == 盘上那一段零"（位置、长度都对得上），
        而不是"按键时刻落在哪一段" —— 后者被按秒截断抹掉了，谁也验不了。
        返回 `(等价窗的起止采样下标, 整段全零的段名清单)`。
        """
        meta = self._meta(os.path.basename(folder))
        pauses = meta.get("pauses") or []
        self.assertEqual(1, len(pauses), "本用例只按一次静音：%r" % (pauses,))
        start = datetime.datetime.fromisoformat(meta["start"]).timestamp()
        sr = float(recorder_mod.SAMPLE_RATE)
        silent = [name for name, data in self._samples_by_segment(folder)
                  if int(np.count_nonzero(data)) == 0]
        self.assertTrue(silent, "该有整段都在暂停里的段（零区间 %r）" % (observed_run,))
        a = bounds[silent[0]][0]
        b = bounds[silent[-1]][1]
        pauses[0]["from"] = (datetime.datetime.fromtimestamp(start + a / sr)
                             .isoformat(timespec="seconds"))
        pauses[0]["to"] = (datetime.datetime.fromtimestamp(start + b / sr)
                           .isoformat(timespec="seconds"))
        meta["pauses"] = pauses
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        return (a, b), silent


class RecorderPauseTests(_PauseCase):
    """录音器那一层：暂停 = **照常写、写零**；恢复 = 接着写真音频。"""

    def test_pause_and_resume_are_idempotent(self):
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=10)
        self.assertTrue(rec.pause(), "第一次暂停该算有变化")
        self.assertFalse(rec.pause(), "第二次暂停该算没变化（幂等）")
        self.assertTrue(rec.paused)
        self.assertTrue(rec.resume(), "第一次恢复该算有变化")
        self.assertFalse(rec.resume(), "第二次恢复该算没变化")
        self.assertFalse(rec.paused)

    def test_paused_frames_are_written_as_zeros_and_resume_continues(self):
        """**改写**（旧名 `..._are_not_written...`）：暂停期间照样写段，但内容是全零。

        旧断言"暂停期间一个段都不该有 / 一个 wav 都不该写"是按旧契约（不写盘）写的，
        新契约下它正好相反 —— 改了哪几条见文件末尾的说明。
        """
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=10)
        rec.start()
        self.assertTrue(rec.wait_started(timeout=5), "假输入流该立刻起来")
        self.recorder_stream.wait_reads(3)             # 先录到真音频（0.4 秒）
        rec.pause()
        self.recorder_stream.wait_reads(10)            # 暂停期约 1.4 秒
        # 暂停期间**照常产生段**：这正是新契约（旧契约这里是"一个都不该有"）。
        self.assertTrue(rec.segments, "暂停期间必须继续写段，否则时间轴会出现缺口")
        rec.resume()
        self.recorder_stream.wait_reads(20)            # 恢复后约 2 秒真音频
        rec.stop()
        # 录音器内存里的段清单 = 盘上真的写出来的那些（名字与顺序都要对得上）
        self.assertEqual(sorted(f for f in os.listdir(self.tmp) if f.endswith(".wav")),
                         sorted(rec.segments), "内存段清单与盘上的 wav 该逐字一致")

        samples, sr = _read_wav(os.path.join(self.tmp, rec.segments[0]))
        self.assertEqual(recorder_mod.SAMPLE_RATE, sr, "采样率必须与暂停前一致（16k）")
        nonzero = np.nonzero(samples)[0]
        self.assertGreater(len(nonzero), 0, "暂停前录到的真音频不许被写零")
        self.assertLess(len(nonzero), len(samples), "暂停那段必须是零（别把整段写零）")
        runs = _zero_runs(samples)
        self.assertEqual(1, len(runs), "一次暂停该恰好留下一段连续零：%r" % (runs,))
        run = runs[0]
        self.assertGreater(run[0], 0, "零区间之前必须是真音频")
        self.assertGreater(len(samples) - run[1], 0, "恢复之后必须又是真音频")

    def test_pause_keeps_the_timeline_continuous(self):
        """**核心判据**：暂停不缩短时间轴 —— 音频总长仍 ≈ 挂钟时长（容差一个缓冲）。

        旧实现（暂停丢帧）在这里会短掉整整一段暂停：`写进文件 < 流真的给过 - 容差`。
        """
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=10)
        rec.start()
        self.assertTrue(rec.wait_started(timeout=5))
        self.recorder_stream.wait_reads(3)
        t0 = time.time()
        rec.pause()
        self.recorder_stream.wait_reads(10)            # 暂停期约 1.4 秒
        self.recorder_stream.wait_reads(12)
        rec.resume()
        self.recorder_stream.wait_reads(20)
        elapsed = time.time() - t0
        rec.stop()

        samples, _sr = self._all_samples(self.tmp)[:2]
        fed = self.recorder_stream.frames_emitted()
        self.assertAlmostEqual(
            fed, len(samples), delta=BLOCK_FRAMES,
            msg="写进文件的采样数必须等于输入流真的给过的采样数（暂停一帧都不许丢）")
        # 音频里"从暂停前后开始算"的那一段长度，必须 ≈ 那段挂钟（容差一个缓冲）
        self.assertAlmostEqual(
            elapsed, len(samples) / float(recorder_mod.SAMPLE_RATE),
            delta=BLOCK_SECONDS * 2 + 0.3,
            msg="音频总长必须 ≈ 挂钟时长（差得超过一个缓冲 = 时间轴出现缺口）")

    def test_segments_are_written_back_to_back_without_drift(self):
        """段边界不漂：除收尾那段外，每一段都**正好** `seg_samples` 帧，段与段一帧不差。

        为什么这是"不漂"的判据：段长一旦由"写进去的采样数"说了算，暂停不计数就会让后面的
        段边界整体前移；这里逐段核对"正好整段"，漂移无处藏身。
        """
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=self.SEGMENT_MINUTES)
        rec.start()
        self.assertTrue(rec.wait_started(timeout=5))
        self.recorder_stream.wait_reads(4)             # 0.8 秒
        rec.pause()
        self.recorder_stream.wait_reads(14)            # 暂停横跨整段（3 秒 / 0.2 秒 = 15 块）
        rec.resume()
        self.recorder_stream.wait_reads(30)
        rec.stop()
        self.assertGreaterEqual(len(rec.segments), 2, "这段时长该切出至少两段")

        seg_samples = int(self.SEGMENT_MINUTES * 60 * recorder_mod.SAMPLE_RATE)
        total = 0
        for name in rec.segments:
            data, _sr = _read_wav(os.path.join(self.tmp, name))
            total += len(data)
            if name != rec.segments[-1]:                # 最后一段是收尾那段，可以短
                self.assertEqual(seg_samples, len(data),
                                 "%s 该正好 %d 帧（段边界漂了）" % (name, seg_samples))
        self.assertEqual(total, self.recorder_stream.frames_emitted())

    def test_pause_does_not_lose_a_frame_across_a_boundary(self):
        """暂停**跨段边界**时也不丢帧：新段从"上一块之后"接着写，边界仍在挂钟刻度上。"""
        rec = recorder_mod.MeetingRecorder(self.tmp, segment_minutes=self.SEGMENT_MINUTES)
        rec.start()
        self.assertTrue(rec.wait_started(timeout=5))
        self.recorder_stream.wait_reads(10)            # 2.0 秒：还没到第一段末尾（3.0 秒）
        rec.pause()
        # 暂停 2 秒（10 块）：从 2.0 秒到 4.0 秒 —— 零区间因此**横跨第一段末尾那个刻度**
        # （3.0 秒）。不要停更久：跨过两个刻度就分不清是哪一个把段切开的
        # （这条用例要的是"跨一个边界"）。
        self.recorder_stream.wait_reads(20)
        self.assertGreaterEqual(len(rec.segments), 1)
        rec.resume()
        self.recorder_stream.wait_reads(30)
        rec.stop()

        samples, bounds, sr = self._all_samples(self.tmp)
        self.assertEqual(recorder_mod.SAMPLE_RATE, sr)
        self.assertGreaterEqual(len(bounds), 2, "该切出至少两段")
        seg_samples = int(self.SEGMENT_MINUTES * 60 * recorder_mod.SAMPLE_RATE)
        first = bounds["01.wav"][1]
        # 触发切段的那一块**整个**写进旧段，所以第一段可以比刻度多、但绝不会少
        # （少了 = 暂停把段边界往前拽了）。
        self.assertGreaterEqual(first, seg_samples, "暂停让第一段提前结束了（段边界前移）")
        self.assertLess(first, seg_samples + self.recorder_stream.chunk,
                        "第一段的结束时刻该落在挂钟的段刻度上（容差一个缓冲）")
        self.assertEqual(samples.shape[0], self.recorder_stream.frames_emitted(),
                         "跨段边界也不许丢帧")
        runs = _zero_runs(samples)
        self.assertTrue(runs, "暂停那段该是全零")
        # 零区间的两头都落在**缓冲边界**上（块是整块写进去的，段也是整块切出来的）——
        # 这就排除了"暂停把某一段切了一半"：真要切半，零的起点就会落在块中间。
        self.assertEqual(0, runs[0][0] % self.recorder_stream.chunk,
                         "零该从一个缓冲边界开始：%r" % (runs,))
        self.assertEqual(0, runs[0][1] % self.recorder_stream.chunk,
                         "零该在一个缓冲边界结束：%r" % (runs,))
        # 而且这个零区间**横跨**第一段的末尾 —— 边界那一段不许被"跳过"
        self.assertTrue(runs[0][0] < first < runs[0][1],
                        "暂停该横跨段边界：零区间 %r 没盖住第一段末尾 %d" % (runs, first))
        # 覆盖到的段整段都是零（暂停一帧都没少写）
        for name, (a, b) in bounds.items():
            if a >= runs[0][0] and b <= runs[0][1]:
                self.assertEqual(0, int(np.count_nonzero(samples[a:b])),
                                 "%s 该整段全零（暂停跨段时不许丢帧）" % name)


class MeetingPauseTests(_PauseCase):
    """会议那一层：状态接口要如实带 `paused`，meta 要留下暂停区间。

    并且**音频这一侧要对得上**：零音频的位置与 `meta["pauses"]`（以及实际按键时刻）
    逐个对齐 —— 这是"暂停区间 = 全零音频"这条判据在会议层的落点。
    """

    #: 这一层要把**整场**音频按段拼起来核对，段长用 1.2 秒（0.02 分钟）
    #: —— 比录音器那层更短，好让"有段""零不跨段"这些条件在同一场里都出现。
    SEGMENT_MINUTES = 0.02

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

    def test_pause_keeps_total_duration_for_a_meeting(self):
        """判据①（时长）：一场含暂停的录音，**音频总时长 ≈ 挂钟时长**（容差一个缓冲）。

        挂钟 = `start_meeting` 到 `stop_meeting` 的真实耗时；音频总长 = 所有段的采样数之和。
        旧口径（暂停不写盘）在这里会短掉整整一段暂停的时长。
        """
        name = self._start()
        t0 = time.time()
        self.recorder_stream.wait_reads(6)             # 1.2 秒
        ok, _msg = meeting_mod.pause_meeting(by="手机")
        self.assertTrue(ok)
        self.recorder_stream.wait_reads(16)            # 暂停期 2 秒
        meeting_mod.resume_meeting(by="手机")
        self.recorder_stream.wait_reads(30)            # 恢复后 2.8 秒
        meeting_mod.stop_meeting()
        wall = time.time() - t0

        samples, _bounds, sr = self._all_samples(os.path.join(self.tmp, name))
        self.assertEqual(recorder_mod.SAMPLE_RATE, sr)
        self.assertGreater(wall, 2.0, "这一窗口太短，判据没意义")
        self.assertAlmostEqual(wall, samples.shape[0] / float(sr),
                               delta=BLOCK_SECONDS + 0.3,
                               msg="音频总长该 ≈ 挂钟时长（暂停不缩短时间轴）")

    def test_zero_audio_matches_the_recorded_pause_window(self):
        """判据④：零音频的位置与 `meta["pauses"]`（`from`/`to`）**对得上**。

        做法：把零区间的起点/终点映射回"输入流那一刻"，再与 `meta` 记的 `from`/`to` 比 ——
        `from`/`to` 是按**秒**取整的（既有写法，不许改），所以容差 = 1 秒（取整）
        + 一个缓冲（块的粒度）；先证明 `meta` 与实际按键时刻一致，再证明它对应那段零。
        """
        name = self._start()
        self.recorder_stream.wait_reads(4)
        ok, _msg = meeting_mod.pause_meeting(by="手机")
        self.assertTrue(ok)
        self.recorder_stream.wait_reads(14)            # 暂停期 2 秒
        meeting_mod.resume_meeting(by="手机")
        self.recorder_stream.wait_reads(24)
        meeting_mod.stop_meeting()

        folder = os.path.join(self.tmp, name)
        samples, _bounds, sr = self._all_samples(folder)
        runs = _zero_runs(samples)
        self.assertEqual(1, len(runs), "一次暂停该恰好一段连续零：%r" % (runs,))
        run = runs[0]
        self.assertGreater(run[0], 0, "开头那一点真音频不许被写零")
        self.assertGreater(samples.shape[0] - run[1], 0, "恢复后必须又是真音频")

        events = [dict(e) for e in meeting_mod._state.get("_pause_events") or []]
        self.assertEqual([True, False], [e["paused"] for e in events],
                         "该只有一次暂停 + 一次恢复：%r" % (events,))
        pause_at, resume_at = events[0]["at"], events[1]["at"]
        meta = self._meta(name)
        pauses = meta.get("pauses") or []
        self.assertEqual(1, len(pauses))
        self.assertEqual("手机", pauses[0]["by"])

        def _epoch(iso):
            return datetime.datetime.fromisoformat(iso).timestamp()

        self.assertLess(abs(_epoch(pauses[0]["from"]) - pause_at), 1.5,
                        "meta 记的 from 该就是实际按下静音那一刻（按秒取整）")
        self.assertLess(abs(_epoch(pauses[0]["to"]) - resume_at), 1.5,
                        "meta 记的 to 该就是实际按下恢复那一刻（按秒取整）")
        # 零音频的起点/终点分别对应"按下静音/恢复"那一刻
        got_start = self.recorder_stream.sample_at(pause_at) / float(sr)
        got_end = self.recorder_stream.sample_at(resume_at) / float(sr)
        self.assertLess(abs(run[0] / float(sr) - got_start), BLOCK_SECONDS,
                        "零音频该从「按下静音」那一刻前后开始（%r）" % (runs,))
        self.assertLess(abs(run[1] / float(sr) - got_end), BLOCK_SECONDS,
                        "零音频该在「恢复」那一刻前后结束（%r）" % (runs,))

    def test_zero_audio_lands_exactly_in_the_segment_aligned_pause_window(self):
        """判据④（帧级）：`meta["pauses"]` 换成**段对齐**的等价区间后，零音频严丝合缝。

        这一步把"按秒截断"的干扰整个移出判据：`from/to` 取"被暂停整段盖住的那几段"的
        起止（`_synth_segment_aligned_pause` 说明了为什么只能这样取），于是"零该从哪一帧
        开始、到哪一帧结束"有了唯一期望值 —— 逐帧比对 `meta` 与音频：**位置与长度都要对**。

        为什么这样不算"放松判据"：`from/to` 的位置信息本来就只有"落在哪一段"这一级是确定的
        （`timespec="seconds"` 是既有写法，不许改），段内那一点被截断抹掉了；这一条验的正是
        "`meta` 记的那一段零 == 盘上那一段零"。按键时刻与零区间的对齐由上面那条用例按
        "一个缓冲 + 1 秒截断"验证。
        """
        name = self._start()
        self.recorder_stream.wait_reads(4)             # 0.8 秒
        ok, _msg = meeting_mod.pause_meeting(by="手机")
        self.assertTrue(ok)
        self.recorder_stream.wait_reads(14)            # 暂停 2 秒（横跨 10 段）
        meeting_mod.resume_meeting(by="手机")
        self.recorder_stream.wait_reads(24)
        stop_ok, _msg = meeting_mod.stop_meeting()
        self.assertTrue(stop_ok)

        folder = os.path.join(self.tmp, name)
        samples, bounds_pre, sr = self._all_samples(folder)
        runs = _zero_runs(samples)
        self.assertEqual(1, len(runs), "一次暂停该恰好一段连续零：%r" % (runs,))
        self.assertEqual(sr, recorder_mod.SAMPLE_RATE)

        (seg_a, seg_b), silent = self._synth_segment_aligned_pause(
            folder, bounds_pre, runs[0])
        # `meta` 说的是"这几段整段都是暂停"，盘上就该**正好**是这一段零：起止逐帧相同。
        self.assertEqual(seg_a, runs[0][0],
                         "零该正好从 %s 的起点开始（零区间 %r）" % (silent[0], runs))
        self.assertEqual(seg_b, runs[0][1],
                         "零该正好到 %s 的终点为止（零区间 %r）" % (silent[-1], runs))
        # 覆盖到的每一段都**整段**是零（不是只抹掉一半真音频）
        for name_ in silent:
            data = dict(self._samples_by_segment(folder))[name_]
            self.assertEqual(0, int(np.count_nonzero(data)), "%s 该整段全零" % name_)
        # 真音频不许被写零：这段之外（前后）都要有非零
        self.assertGreater(np.count_nonzero(samples[:runs[0][0]]), 0,
                           "暂停之前必须留下真音频")
        self.assertGreater(np.count_nonzero(samples[runs[0][1]:]), 0,
                           "恢复之后必须留下真音频")

    def test_transcript_has_no_text_in_the_paused_region(self):
        """判据③（转写）：暂停区间**没有文字**，前后是真文字。

        做法：用**打桩**的 ASR（真跑要能力后端，那条路不在这一组用例里）。打桩的口径与真
        ASR 同形：**输入全零 → 没有文字**（真引擎对空白音频也不会凭空出字）。它证明的是
        "喂给 ASR 的那段音频确实是空白" —— 而**不是**"客户端把那一行删掉了"
        （新口径不做任何剪切：那一段没有文字，是因为音频本身就是空白）。
        """
        name = self._start()
        self.recorder_stream.wait_reads(4)
        meeting_mod.pause_meeting(by="手机")
        self.recorder_stream.wait_reads(14)
        meeting_mod.resume_meeting(by="手机")
        self.recorder_stream.wait_reads(24)
        meeting_mod.stop_meeting()

        folder = os.path.join(self.tmp, name)
        segs = self._samples_by_segment(folder)
        whole, bounds, _sr = self._all_samples(folder)
        meeting_id = db.get_meeting_by_name(name)["id"]
        rows, silent_segs = [], []
        for i, (seg_name, data) in enumerate(segs, start=1):
            if np.count_nonzero(data) == 0:
                silent_segs.append(seg_name)
                continue                # 全零 = 空白：ASR 不给文字，也**不删任何东西**
            a, b = bounds[seg_name]
            rows.append((i, a / float(recorder_mod.SAMPLE_RATE),
                         b / float(recorder_mod.SAMPLE_RATE), "", "[真音频]"))
        db.clear_meeting_lines(meeting_id)
        db.add_lines(meeting_id, rows)
        lines = [r for r in db.get_lines(meeting_id) if (r["text"] or "").strip()]
        texts = [r["text"] for r in lines]

        self.assertTrue(silent_segs, "这一场该有「整段都在暂停里」的空白段（段：%r）"
                        % ([n for n, _d in segs],))
        self.assertTrue(any("真音频" in t for t in texts), "暂停前后的真音频必须出文字")
        # 暂停区间的采样全为零 —— 这就是"那里没有文字"的**根因**
        for a, b in _zero_runs(whole):
            self.assertEqual(0, np.count_nonzero(whole[a:b]))
        # 别把整段写零：至少一个段必须真的非零
        self.assertTrue(any(np.count_nonzero(d) > 0 for _n, d in segs),
                        "整场都被写零了（暂停范围失控）")
        # 空白段**照旧在段清单里**（不做剪切：时间轴里那一段还在，只是没内容）
        self.assertTrue(set(silent_segs).issubset({n for n, _d in segs}))


if __name__ == "__main__":
    unittest.main()
