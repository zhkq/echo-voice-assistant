# -*- coding: utf-8 -*-
"""Qwen3-ASR 的**切片/分批**与**强制对齐器接线**（2026-09-26 修的四处回归点）。

背景（同一段 600 秒真实会议音频实测）
------------------------------------
1. **整段 600 秒进模型**：qwen-asr 只在 1200 秒才自己切片（`MAX_ASR_INPUT_SECONDS`），
   而 `funasr.AutoModel.generate` 在没有 `vad_model` 时是一条一条喂的 ——
   于是 forward 236.6 秒、转写期间 peak 显存 **11.4 GB**（这块卡只有 8 GB，溢出到共享内存）、
   600 秒音频只转出 **36 个字**（"不是不是吗？对，他他那个一点吧…"）。
   切成 60 秒一片、一次 4 片之后同一段音频才既能装下、又有正常长度的文本。
2. **对齐器没接上**：`_qwen3asr_sentences()` 要走 `return_time_stamps=True`，
   而没有 `forced_aligner` 时 funasr 只会打印一句
   "return_time_stamps requires forced_aligner. Skipping timestamps." ——
   服务端 `default_specs()` 却给这个 spec 宣告了 `supports: [asr.text, asr.timestamps]`。
3. **key 必须三处同源**：`engine_key()` / `load_engine()` / `_get_qwen3asr()` 各算一次 key，
   判据不一致就会出现"加载成功但 `key_loaded()` 假红"，或者同一张卡上出现**两份** 0.6B 权重
   （文本路一份、时间戳路一份）—— 8 GB 卡上那是必然溢出。
4. **没有权重时不许联网**：本机两个缓存（modelscope / HF hub）都没有对齐器时，
   传模型名会让 funasr 去下 1.7 GB；离线机器上就是一次加载失败。这时应当降级（时间戳不可用）。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import app.audio.stt as stt


# ---------------------------------------------------------------- 切片

class Qwen3ChunkingTests(unittest.TestCase):
    def test_short_audio_stays_one_piece(self):
        audio = np.zeros(16000 * 5, dtype=np.float32)
        pieces = stt._qwen3asr_chunks(audio)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0][1], 0.0)

    def test_long_audio_is_split_without_losing_samples(self):
        """切片必须**不重叠、不丢样点**：总样点数相等，偏移单调递增。"""
        audio = np.random.RandomState(0).randn(16000 * 130).astype(np.float32) * 0.1
        pieces = stt._qwen3asr_chunks(audio, chunk_sec=60.0)
        self.assertEqual(len(pieces), 3, "130 秒 / 60 秒 → 3 片")
        self.assertEqual(sum(p.shape[0] for p, _o in pieces), audio.shape[0])
        offsets = [o for _p, o in pieces]
        self.assertEqual(offsets[0], 0.0)
        self.assertEqual(offsets, sorted(offsets))
        self.assertAlmostEqual(offsets[1], 60.0, delta=2.1, msg="切点只在 ±2 秒窗口里挪")
        for p, _o in pieces:
            # 切点可以往**右**挪最多 2 秒（往安静处挪），所以上界是 chunk_sec + 2
            self.assertLessEqual(p.shape[0], 16000 * 62)

    def test_cut_lands_in_the_quietest_spot(self):
        """切点要找**最安静**的位置 —— 硬切会落在词中间，那一两个音节当场丢掉。"""
        audio = np.full(16000 * 100, 0.5, dtype=np.float32)
        audio[16000 * 59:16000 * 61] = 0.0        # 59–61 秒有个静音口
        pieces = stt._qwen3asr_chunks(audio, chunk_sec=60.0)
        self.assertAlmostEqual(pieces[1][1], 59.0, delta=0.2)

    def test_empty_and_unreadable_audio(self):
        self.assertEqual(stt._qwen3asr_chunks(None), [])
        self.assertEqual(stt._qwen3asr_chunks(np.zeros(0, dtype=np.float32)), [])


# ---------------------------------------------------------------- 分批推理

class _FakeQwen:
    """假 funasr AutoModel：只实现 `generate(input=..., batch_size=..., **kw)`。"""

    def __init__(self):
        self.calls = []
        self._n = 0

    def generate(self, input=None, batch_size=1, language=None, **kw):
        parts = input if isinstance(input, list) else [input]
        self.calls.append({"n": len(parts), "batch_size": batch_size,
                           "ts": bool(kw.get("return_time_stamps"))})
        out = []
        for _p in parts:
            text = "第%d片。" % self._n
            self._n += 1
            row = {"text": text}
            if kw.get("return_time_stamps"):
                row["timestamp"] = [[float(j), float(j) + 0.5] for j in range(len(text))]
            out.append(row)
        return out


class Qwen3BatchingTests(unittest.TestCase):
    def test_chunks_are_batched_and_offsets_are_applied(self):
        audio = np.zeros(16000 * 130, dtype=np.float32)
        fake = _FakeQwen()
        with patch.object(stt, "_wav16k_mono", return_value=audio), \
             patch.object(stt, "QWEN3_BATCH", 2):
            parts = stt._qwen3asr_transcribe(fake, "x.wav", None, timestamps=True)
        self.assertEqual(len(parts), 3)
        self.assertEqual([c["n"] for c in fake.calls], [2, 1], "一次喂的片数 = QWEN3_BATCH")
        self.assertEqual([c["batch_size"] for c in fake.calls], [2, 1],
                         "batch_size 必须传给 funasr，否则它会一片一次前向")
        self.assertTrue(all(c["ts"] for c in fake.calls))
        self.assertEqual("".join(t for t, _ts, _o in parts), "第0片。第1片。第2片。")
        self.assertAlmostEqual(parts[1][2], 60.0, delta=2.1)

    def test_timestamps_are_relative_to_the_chunk_and_the_offset_comes_with_it(self):
        audio = np.zeros(16000 * 130, dtype=np.float32)
        fake = _FakeQwen()
        with patch.object(stt, "_wav16k_mono", return_value=audio), \
             patch.object(stt, "QWEN3_BATCH", 2):
            parts = stt._qwen3asr_transcribe(fake, "x.wav", None, timestamps=True)
        _text, ts, off = parts[1]
        self.assertTrue(ts)
        self.assertAlmostEqual(ts[0][0], 0.0, places=6,
                               msg="片内时间轴是相对片头的")
        self.assertGreater(off, 0.0)
        # 偏移由 `_sentences_of()` 加回去 → 句级时间轴落在整段音频的坐标上
        sents = stt._sentences_of(_text, ts, off)
        self.assertAlmostEqual(sents[0][0], off, delta=0.6)

    def test_readable_but_odd_format_falls_back_to_the_whole_file(self):
        """读不出 16k 单声道 PCM 时退回整段（= 改动前的行为），**不猜**、也不报错。"""
        fake = _FakeQwen()
        with patch.object(stt, "_wav16k_mono", return_value=None):
            parts = stt._qwen3asr_transcribe(fake, "x.mp3", None)
        self.assertEqual(fake.calls, [{"n": 1, "batch_size": 1, "ts": False}])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][2], 0.0)

    def test_sentences_are_offset_by_the_chunk(self):
        text = "你好。再见。"
        ts = [(0.0, 0.4), (0.4, 0.8), (1.0, 1.4), (1.4, 1.8), (1.8, 2.2), (2.2, 2.6)]
        got = stt._sentences_of(text, ts, offset=100.0)
        self.assertEqual([t for _s, _e, t in got], ["你好。", "再见。"])
        self.assertAlmostEqual(got[0][0], 100.0)
        self.assertAlmostEqual(got[-1][1], 102.6)

    def test_no_timestamps_means_no_invented_sentences(self):
        """拿不到时间轴就**如实**返回整段一行（时间 0），绝不按字数编时间。"""
        got = stt._sentences_of("一句话", [], offset=7.0)
        self.assertEqual(got, [(7.0, 7.0, "一句话")])

    def test_overlapping_token_times_do_not_make_the_timeline_go_backwards(self):
        """funasr 的 qwen3asr 包装把秒**截断成整数**（`int(ts.start_time)`），
        于是相邻句偶尔会重叠整整 1 秒（600 秒真音频实测 205 句里有 2 处）。
        行与行不许交叠的下游（按行铺时间轴）会因此出错，所以这里必须夹住。"""
        text = "你好。再见。"
        ts = [(0.0, 1.0), (1.0, 5.0), (5.0, 5.0), (5.0, 5.0), (5.0, 6.0), (5.0, 6.0)]
        got = stt._sentences_of(text, ts, offset=0.0)
        self.assertEqual([t for _s, _e, t in got], ["你好。", "再见。"])
        self.assertTrue(all(got[i][1] <= got[i + 1][0] + 1e-9
                            for i in range(len(got) - 1)),
                        "句级时间轴不许重叠：%s" % (got,))


# ---------------------------------------------------------------- 对齐器接线

class Qwen3AlignerWiringTests(unittest.TestCase):
    def setUp(self):
        # 假装本机两个缓存都在（否则判据依赖这台机器装没装对齐器权重）
        self._patch = patch.object(stt, "_resolve_local_model",
                                   side_effect=lambda m: ("/cache/" + m) if m else m)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_default_carries_the_aligner(self):
        self.assertEqual(stt.qwen3asr_aligner(), stt.QWEN3_FORCED_ALIGNER)
        self.assertIn(stt.QWEN3_FORCED_ALIGNER,
                      stt.engine_key("qwen3asr", "Qwen/Qwen3-ASR-0.6B"))

    def test_explicit_none_opts_out(self):
        self.assertEqual(stt.qwen3asr_aligner(None), "")
        self.assertEqual(stt.engine_key("qwen3asr", "Qwen/Qwen3-ASR-0.6B", None),
                         "qwen3asr:Qwen/Qwen3-ASR-0.6B:")

    def test_get_qwen3asr_defaults_to_the_aligner(self):
        """默认值就是关键：`transcribe_ex()` 调它时**不传**这个参数 ——
        服务端先按 `(impl, model)` 加载好（带对齐器），文本请求必须复用**同一个**实例，
        否则同一张卡上会站两份 0.6B 权重。"""
        import inspect
        default = inspect.signature(stt._get_qwen3asr).parameters["forced_aligner"].default
        self.assertEqual(default, stt.QWEN3_FORCED_ALIGNER)

    def test_missing_weights_degrade_instead_of_downloading(self):
        with patch.object(stt, "_resolve_local_model", side_effect=lambda m: m):
            self.assertEqual(stt.qwen3asr_aligner(), "")
            self.assertEqual(stt.engine_key("qwen3asr", "Qwen/Qwen3-ASR-0.6B"),
                             "qwen3asr:Qwen/Qwen3-ASR-0.6B:")

    def test_load_engine_key_comes_from_engine_key(self):
        """`load_engine()` 返回的 key 必须能被 `key_loaded()` 认出来 ——
        否则 `server/engines.py` 会报一句"引擎没有加载起来"的**假故障**。"""
        with patch.object(stt, "_get_qwen3asr") as got:
            key = stt.load_engine("qwen3asr", "Qwen/Qwen3-ASR-0.6B", "cuda")
        self.assertEqual(key, stt.engine_key("qwen3asr", "Qwen/Qwen3-ASR-0.6B"))
        self.assertEqual(got.call_args[0][2], stt.QWEN3_FORCED_ALIGNER,
                         "加载时用的对齐器必须与 key 里那个一致")

    def test_server_loader_passes_the_forced_aligner(self):
        """**修的就是这一处**：能力后端的加载路径原来没把 forced_aligner 传进去。"""
        from server import engines
        seen = {}

        def fake_load_engine(engine, model, device="auto", **kw):
            seen.update(dict(engine=engine, model=model, device=device))
            seen.update(kw)
            return "k"

        with patch("app.audio.stt.load_engine", side_effect=fake_load_engine), \
             patch("app.audio.stt.key_loaded", return_value=True), \
             patch.object(engines, "_assert_device"):
            engines.build_loaders(device="cuda")["qwen3asr"](None)
        self.assertEqual(seen.get("forced_aligner"), stt.QWEN3_FORCED_ALIGNER)
        self.assertEqual(seen.get("model"), "Qwen/Qwen3-ASR-0.6B")


# ---------------------------------------------------------------- 文本路/时间戳路同一个实例

class Qwen3SingleInstanceTests(unittest.TestCase):
    def test_transcribe_ex_asks_for_the_same_engine_the_loader_loaded(self):
        """文本路（`transcribe_ex`）与加载路（`load_engine`）必须落到**同一个**
        `(impl, model, 对齐器)` —— 差一点就是同一张卡上第二份 0.6B 权重。"""
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        seen = {}

        def spy(device="auto", model_name="", forced_aligner=stt.QWEN3_FORCED_ALIGNER):
            seen["key"] = "qwen3asr:%s:%s" % (model_name, stt.qwen3asr_aligner(forced_aligner))
            return object()

        try:
            with patch.object(stt, "_resolve_local_model",
                              side_effect=lambda m: ("/cache/" + m) if m else m), \
                 patch.object(stt, "_get_qwen3asr", side_effect=spy), \
                 patch.object(stt, "_qwen3asr_transcribe", return_value=[("喂", [], 0.0)]):
                out = stt.transcribe_ex(wav, engine="qwen3asr",
                                        model="Qwen/Qwen3-ASR-0.6B", device="cuda")
                loaded = stt.engine_key("qwen3asr", "Qwen/Qwen3-ASR-0.6B")
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
        self.assertEqual(out["status"], stt.TRANSCRIBE_OK)
        self.assertEqual(out["text"], "喂")
        self.assertEqual(seen["key"], loaded)


if __name__ == "__main__":
    unittest.main()
