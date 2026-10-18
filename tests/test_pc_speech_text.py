# -*- coding: utf-8 -*-
"""电脑端「念之前」的数字区间归一化（`app/audio/speech_text.py` + `app/audio/tts.py`）。

## 为什么要有这组用例（用户 2026-10-18 实测，手机与电脑都错）

手机和电脑都把 `8~19℃` 念成「819 度」。真实原文取自 `D:\\ECHO\\data\\echo.db` 的
`commands.reply`（不是猜的）：

    明天（10月19日，周一）北京天气晴转多云，气温 8~19℃，白天南风、夜间北风…

**这是念法问题，不是数据问题** —— 所以 `commands.reply` / 界面 / 日志里的一律保持原文，
只有"送去合成的那串"换成「到」。手机那条线已按这个口径修了
（`android/.../service/SpeechText.kt`）；电脑这条线原来**没有任何归一化**，
本文件就是它的判据。

## 三条判据（每条都能单独判"坏没坏"）

  1. **真实那句**：`气温 8~19℃` → 送去合成的那串含 `8 到 19℃`；
  2. **表格**：形形色色的区间都要改，而**日期/时间/版本/负数/无单位区间**一个字都不许动
     （改错这些比不改更糟：`10-18` 念成"10 到 18"、`-5℃` 念成"5 到"都是错的）；
  3. **接线**：电脑朗读入口 `tts.speak` / `tts.synthesize` **确实**调用了归一化
     （源码级 + 行为级各一条 —— 只写模块不接线等于没修）。

## 隔离

**不联网、不真合成、不真出声**：离线引擎接缝换成记录器（只记下"引擎收到了哪串"）。
"""
import inspect
import io
import os
import re
import struct
import sys
import unittest
import wave
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app.audio import speech_text                                  # noqa: E402
from app.audio import tts as tts_mod                               # noqa: E402


#: `D:\ECHO\data\echo.db` 里那句的真实开头（用户实测被念错的那句）
REAL_REPLY = "明天（10月19日，周一）北京天气晴转多云，气温 8~19℃，白天南风、夜间北风"

#: 同一批真实回复里另外两种区间形状（湿度是"两边都带 %"，气压是"右边带 hPa"）
REAL_HUMIDITY = "相对湿度约60%–78%，气压1013–1017hPa"


def _wav_bytes(seconds=0.2, sr=16000):
    """一段真 wav（给 `synthesize` 的离线分支当"引擎产出"）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(1000 * ((i % 20) - 10) / 10))
                               for i in range(int(sr * seconds))))
    return buf.getvalue()


# ================================================================ 1) 真实那句
class RealSentenceRegressionTests(unittest.TestCase):
    """先复现"没改"，再钉住"改了" —— 用库里那句真实原文。"""

    def test_before_the_fix_the_sentence_really_carries_the_broken_range(self):
        """**复现**：原文里就是 `8~19℃` 这个会被念成「819 度」的形状。

        这条不测我们的代码，它钉的是"样本没变味"：原文一旦被谁改写成别的形状，
        后面那条回归就失去意义了。
        """
        self.assertIn("8~19℃", REAL_REPLY)
        self.assertNotIn("8 到 19", REAL_REPLY, "原文本来就该是没归一化的样子")

    def test_the_real_sentence_reads_as_dao(self):
        """**判据 1**：真实那句 → 念法里含 `8 到 19℃`。"""
        spoken = speech_text.normalize(REAL_REPLY)
        self.assertIn("8 到 19℃", spoken)
        self.assertNotIn("8~19", spoken, "送去合成的那串里不该还剩波浪号")

    def test_only_that_one_place_changes(self):
        """**只改念法**：整句里除了那一段，其余字节（含 `10月19日`、`℃`）逐字不动。"""
        self.assertEqual(speech_text.normalize(REAL_REPLY),
                         REAL_REPLY.replace("8~19℃", "8 到 19℃"))

    def test_the_other_two_real_shapes_also_change(self):
        """同一批真实回复里的另外两种形状：`60%–78%` 与 `1013–1017hPa`。"""
        self.assertEqual(speech_text.normalize(REAL_HUMIDITY),
                         "相对湿度约60% 到 78%，气压1013 到 1017hPa")

    def test_the_original_string_is_not_mutated_in_place(self):
        """归一化只产出新串，不回头改原文（`commands.reply` 里存的那句必须还是原样）。"""
        before = REAL_REPLY
        speech_text.normalize(before)
        self.assertEqual(before, REAL_REPLY)


# ================================================================ 2) 表格
class RangeTableTests(unittest.TestCase):
    """要改的：各种分隔符 / 空格 / 单位。"""

    #: (原文, 期望念法)。分隔符六种全在：`-` `－` `—` `–` `~` `～`。
    CHANGED = [
        ("气温 8~19℃", "气温 8 到 19℃"),
        ("气温8-19度", "气温8 到 19度"),
        ("最低2℃，比明天骤降6–10℃", "最低2℃，比明天骤降6 到 10℃"),
        ("清晨5–6℃", "清晨5 到 6℃"),
        ("午后20~22℃", "午后20 到 22℃"),
        ("体感约5～9℃", "体感约5 到 9℃"),              # 全角波浪号
        ("气温 8－19℃", "气温 8 到 19℃"),              # 全角减号
        ("气温 8—19℃", "气温 8 到 19℃"),               # 破折号
        ("气压 1013–1017hPa", "气压 1013 到 1017hPa"),   # hPa：见模块注释里的取舍
        ("相对湿度约60%–78%", "相对湿度约60% 到 78%"),   # 单位在两边
        ("8℃–19℃", "8℃ 到 19℃"),                       # 两边都带单位
        ("8 - 19 ℃", "8 到 19 ℃"),                      # 分隔符两边有空格
        ("湿度 10~20%", "湿度 10 到 20%"),
        ("风力5-6级", "风力5 到 6级"),
        ("能见度3~5公里", "能见度3 到 5公里"),
        ("相距2-3米", "相距2 到 3米"),
        ("限速60-70公里/小时", "限速60 到 70公里/小时"),
        ("全程5-12km", "全程5 到 12km"),
        ("8.5~9.5℃", "8.5 到 9.5℃"),                    # 小数
    ]

    #: **明确不该改的**：改了会让语义错。
    UNCHANGED = [
        "10-18",                      # 日期（月-日）
        "2026-10-18",                 # 完整日期
        "2026-10-18 12:53:24",        # 日期 + 时间
        "（10-18 20:00发布）",         # 日期 + 时刻
        "18:00–19:30",                # 时间区间（右侧不是单位）
        "v2.1.7",                     # 版本号
        "2.1.7",                      # 版本号（无 v 前缀）
        "最低-5℃",                     # 负数（左边没有数字）
        "-5℃",                        # 纯负数
        "-5-3℃",                      # 负数区间：宁可少改，也不能把零下念错
        "5—8时",                      # 时间范围（"时"不在单位白名单里）
        "11—12 月",                   # 月份范围
        "2—3 人",                     # 人数范围
        "第8-19条",                    # 序号范围（没有单位）
        "8~19",                       # 没有单位的区间：规则要窄，不动
        "链接 2026-10-15-AI工具",      # 文件名里的日期
    ]

    def test_every_changed_shape_becomes_dao(self):
        for src, want in self.CHANGED:
            with self.subTest(src=src):
                self.assertEqual(speech_text.normalize(src), want)

    def test_every_protected_shape_is_byte_for_byte_unchanged(self):
        """日期 / 时间 / 版本 / 负数 / 无单位区间：一个字节都不许动。"""
        for src in self.UNCHANGED:
            with self.subTest(src=src):
                self.assertEqual(speech_text.normalize(src), src)

    def test_it_is_idempotent(self):
        """**幂等**：念之前可能不止一处调用（手机自己一次 + 电脑合成入口再做一次），
        套两遍必须等于套一遍，绝不能出现"8 到 到 19"。"""
        for src, _ in self.CHANGED:
            with self.subTest(src=src):
                once = speech_text.normalize(src)
                self.assertEqual(speech_text.normalize(once), once)
        for src in self.UNCHANGED:
            with self.subTest(src=src):
                self.assertEqual(speech_text.normalize(speech_text.normalize(src)),
                                 speech_text.normalize(src))

    def test_empty_and_non_string_input_is_passed_through(self):
        """空串/None 原样返回（朗读入口会因为空文本提前返回，不该在这里炸）。"""
        self.assertEqual(speech_text.normalize(""), "")
        self.assertIsNone(speech_text.normalize(None))

    def test_a_whole_sentence_with_several_ranges(self):
        """一句话里多个区间都要改（真实回复就有这种：气温 + 风 + 湿度 + 气压）。"""
        src = "8~19℃，南风2~3级，湿度60%–78%，气压1013–1017hPa"
        self.assertEqual(speech_text.normalize(src),
                         "8 到 19℃，南风2 到 3级，湿度60% 到 78%，气压1013 到 1017hPa")


# ================================================================ 3) 接线
class PcSpeechEntryWiringTests(unittest.TestCase):
    """电脑到底有没有把归一化接上 —— 源码级 + 行为级各一条。"""

    def test_speak_source_calls_normalize(self):
        """**源码级判据**：`tts.speak`（电脑本机朗读的那一站）里必须出现 normalize。"""
        src = inspect.getsource(tts_mod.speak)
        self.assertIn("normalize(", src,
                      "tts.speak 没调用归一化 → 电脑又把 8~19℃ 念成 819 度了")

    def test_synthesize_source_calls_normalize(self):
        """`tts.synthesize`（手机/手表来要音频字节的那一站）同样要归一化。"""
        src = inspect.getsource(tts_mod.synthesize)
        self.assertIn("normalize(", src,
                      "tts.synthesize 没调用归一化 → 手机拿到的音频还是 819 度")

    def test_the_engine_really_receives_the_normalized_text(self):
        """**行为级判据**：真的走一遍 `speak`，看引擎那一步收到的是哪串。"""
        seen = []
        with patch.object(tts_mod, "_speak_offline",
                          lambda text: seen.append(text) or True):
            self.assertTrue(tts_mod.speak(REAL_REPLY, "sapi"))
        self.assertEqual(seen, [REAL_REPLY.replace("8~19℃", "8 到 19℃")],
                         "引擎收到的仍是原文 = 归一化没生效")

    def test_synthesize_hands_the_normalized_text_to_the_offline_engine(self):
        """合成那条路（`/api/tts` 背后）同样要改。"""
        wav = _wav_bytes()
        seen = []

        def _render(text, path, timeout=60):
            seen.append(text)
            with open(path, "wb") as fh:
                fh.write(wav)
            return True

        with patch.object(tts_mod.echo_platform, "offline_tts_render", _render):
            mime, data = tts_mod.synthesize(REAL_REPLY, "sapi")
        self.assertEqual(mime, "audio/wav")
        self.assertEqual(data, wav)
        self.assertEqual(seen, [REAL_REPLY.replace("8~19℃", "8 到 19℃")])

    def test_only_the_speech_station_imports_the_normalizer(self):
        """**只改念的那串**：`app/` 里只有语音那一站 import 归一化。

        原文（`commands.reply`）、界面、日志都不许被顺手改掉 —— 一旦有人把
        `speech_text` 接进 db/api/assistant，用户看到的文字就会跟着变，
        那是这个改动**明确不该有**的副作用。
        """
        app_dir = os.path.join(_ROOT, "app")
        pat = re.compile(r"^\s*(?:from|import)\b.*\bspeech_text\b", re.M)
        hits = set()
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8") as fh:
                    if pat.search(fh.read()):
                        hits.add(os.path.relpath(path, _ROOT).replace("\\", "/"))
        self.assertEqual(hits, {"app/audio/tts.py"},
                         "归一化只该出现在念/合成那一站，别的地方拿到的是原文")


if __name__ == "__main__":
    unittest.main()
