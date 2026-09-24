# -*- coding: utf-8 -*-
"""拼装层（`app/capabilities/assemble.py`）的契约。

这是设计 §4.4 说的那条"**要写死，别到时候各写一份**"的规则。
它值得单独一个文件，因为它的失败方式是**静默的**：
时间轴精度不一样，但两份数据长得一模一样 —— 用户只会觉得"时间戳有时准有时不准"。

所以这里钉三件事：
  1. **规则优先级**是设计里写的那五条，不是实现碰巧的顺序；
  2. **精度档位必须如实**（`exact` / `aligned` / `estimated` / `none`），
     而且它是**返回值的一部分**，不是注释里的悄悄话；
  3. **空文本不等于失败**（这段没人说话），不许抛异常。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import assemble                                  # noqa: E402
from app.capabilities.assemble import (                                # noqa: E402
    TIMESTAMPS_ALIGNED,
    TIMESTAMPS_ESTIMATED,
    TIMESTAMPS_EXACT,
    TIMESTAMPS_KINDS,
    TIMESTAMPS_NONE,
    align_sentences,
    assemble as run_assemble,
    describe_kind,
    estimate_sentences,
)


class VocabularyTests(unittest.TestCase):
    def test_the_four_kinds_are_exactly_these(self):
        """档位只有四个。加一个就要先想清楚"谁会读它、他会因此做什么"。"""
        self.assertEqual(TIMESTAMPS_KINDS,
                         ("exact", "aligned", "estimated", "none"))

    def test_aligned_is_its_own_kind_not_a_synonym(self):
        """`aligned` 必须与 `exact`、`estimated` **都**不同。

        并进 `exact` 是说大话（句界是在骨架里插值出来的）；
        并进 `estimated` 是漏报（它比"整段字数均摊"准得多）。
        两边都是"让用户以为他知道精度，其实不知道"。
        """
        self.assertNotEqual(TIMESTAMPS_ALIGNED, TIMESTAMPS_EXACT)
        self.assertNotEqual(TIMESTAMPS_ALIGNED, TIMESTAMPS_ESTIMATED)

    def test_every_kind_has_a_human_phrase(self):
        """面板不该直接显示 `estimated` 这种单词。"""
        for kind in TIMESTAMPS_KINDS:
            with self.subTest(kind=kind):
                phrase = describe_kind(kind)
                self.assertNotEqual(phrase, kind)
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in phrase),
                                "给面板的话要是中文：%r" % phrase)


class EstimateTests(unittest.TestCase):
    """没有时间轴时的兜底：按句切、按字数均摊。"""

    def test_splits_by_sentence_end_and_covers_the_whole_segment(self):
        rows = estimate_sentences("第一句。第二句！Third? 第四句；", 12.0)
        self.assertEqual([r[2] for r in rows],
                         ["第一句。", "第二句！", "Third?", "第四句；"])
        self.assertAlmostEqual(rows[0][0], 0.0, places=2)
        # 最后一句的结束**就是段长**（均摊的定义）
        self.assertAlmostEqual(rows[-1][1], 12.0, places=2)
        # 首尾相接、单调不减
        for a, b in zip(rows, rows[1:]):
            self.assertAlmostEqual(a[1], b[0], places=2)

    def test_longer_sentence_gets_more_time(self):
        """按**字数**分配，不是平均分配。"""
        rows = estimate_sentences("短。" + "长" * 30 + "。", 11.0)
        short, long_ = rows[0], rows[1]
        self.assertLess(short[1] - short[0], long_[1] - long_[0])

    def test_no_punctuation_is_one_row(self):
        rows = estimate_sentences("no punctuation at all", 7.5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (0.0, 7.5, "no punctuation at all"))

    def test_blank_text_is_empty_not_an_error(self):
        """**空文本不是失败**：这段就是没人说话。它必须安静地返回空，不抛。"""
        self.assertEqual(estimate_sentences("   ", 10.0), [])
        self.assertEqual(estimate_sentences("", 0.0), [])
        self.assertEqual(estimate_sentences(None, 5.0), [])

    def test_tiny_segment_still_keeps_a_minimum_duration(self):
        """极短/零长的段落：每句至少留 0.05 s，免得面板点不中、导出算出负宽度。"""
        rows = estimate_sentences("一。二。三。", 0.0)
        for a, b, _t in rows:
            self.assertGreaterEqual(round(b - a, 2), 0.05)


class AlignTests(unittest.TestCase):
    """有骨架有文本：按字对齐（SenseVoice 文本 + whisper 骨架那条路）。"""

    SKELETON = [(0.0, 2.0, "今天开会"), (2.0, 4.0, "讨论方案")]

    def test_splits_the_text_on_punctuation(self):
        rows = align_sentences("今天开会。讨论方案。", self.SKELETON)
        self.assertEqual([r[2] for r in rows], ["今天开会。", "讨论方案。"])
        # **第一句不从 0.0 开始，而是从第一个字的"字心"开始**（骨架说"今天开会"占 0–2 s，
        # 4 个字，第一个字的字心 = 2.0 × 0.5/4 = 0.25 s）。
        # 这是原实现（`meeting.py` 里那份，实机跑过很多会议）的行为，**刻意原样保留** ——
        # 为了"看起来更整齐"去把它改成 0.0，等于改生产路径的时间轴，不该混在搬运里做。
        self.assertLess(rows[0][0], 0.5)
        self.assertGreaterEqual(rows[0][0], 0.0)

    def test_times_come_from_the_skeleton_not_from_average(self):
        """**句界要落在骨架上**，不是按字数平摊 —— 这是 `aligned` 比 `estimated` 值钱的地方。

        骨架说"讨论方案"从 2.0 s 开始；对齐结果应当贴着它，而不是把 4 秒平分成两半。
        """
        rows = align_sentences("今天开会。讨论方案。", self.SKELETON)
        self.assertGreater(rows[1][0], 1.4)
        self.assertLess(rows[1][0], 2.6)

    def test_text_with_extra_words_still_gets_times(self):
        """文本与骨架不完全一致（识别结果本来就常有差异）也要出时间，不能整段丢。"""
        rows = align_sentences("今天开会啊。讨论一下方案。", self.SKELETON)
        self.assertEqual(len(rows), 2)
        for a, b, _t in rows:
            self.assertLessEqual(a, b)

    def test_missing_input_returns_empty(self):
        self.assertEqual(align_sentences("", self.SKELETON), [])
        self.assertEqual(align_sentences("文本", []), [])
        self.assertEqual(align_sentences("文本", [(0.0, 1.0, "  ")]), [])


class AssembleRuleTests(unittest.TestCase):
    """**规则优先级**：设计 §4.4 那五条，第一个能用的赢。"""

    def test_rule1_backend_sentences_win(self):
        got = run_assemble(text="整段文本。", sentences=[(0.0, 1.0, "后端给的。")],
                           skeleton=[(0.0, 9.0, "骨架")], seg_seconds=9.0)
        self.assertEqual(got.timestamps, TIMESTAMPS_EXACT)
        self.assertEqual(got.rule, "sentences")
        self.assertEqual([r[2] for r in got.sentences], ["后端给的。"])

    def test_rule2_text_plus_skeleton_aligns(self):
        got = run_assemble(text="今天开会。讨论方案。",
                           skeleton=[(0.0, 2.0, "今天开会"), (2.0, 4.0, "讨论方案")],
                           seg_seconds=4.0)
        self.assertEqual(got.timestamps, TIMESTAMPS_ALIGNED)
        self.assertEqual(got.rule, "text+skeleton")

    def test_rule3_skeleton_alone_is_exact(self):
        """没有文本、只有骨架：骨架**就是**结果，而且是精确的。"""
        got = run_assemble(skeleton=[(0.0, 2.0, "今天开会")], seg_seconds=2.0)
        self.assertEqual(got.timestamps, TIMESTAMPS_EXACT)
        self.assertEqual(got.rule, "skeleton")

    def test_rule4_text_alone_is_estimated(self):
        got = run_assemble(text="今天开会。讨论方案。", seg_seconds=10.0)
        self.assertEqual(got.timestamps, TIMESTAMPS_ESTIMATED)
        self.assertEqual(got.rule, "text-only")
        self.assertAlmostEqual(got.sentences[-1][1], 10.0, places=2)

    def test_rule5_nothing_is_none_and_not_an_error(self):
        """什么都没有 = 这段没人说话。**返回空 + `none`，不抛**。"""
        got = run_assemble(seg_seconds=10.0)
        self.assertEqual(got.sentences, [])
        self.assertEqual(got.timestamps, TIMESTAMPS_NONE)
        self.assertEqual(got.rule, "empty")
        self.assertFalse(got)                     # 空结果在 if 里算假，调用方好写

    def test_unusable_inputs_fall_through_to_the_next_rule(self):
        """给了一堆"看着有其实没用"的输入（空白文本、空骨架行），要**继续往下走**。"""
        got = run_assemble(text="   ", sentences=[(0.0, 1.0, "   ")],
                           skeleton=[(0.0, 1.0, "")], seg_seconds=3.0)
        self.assertEqual(got.timestamps, TIMESTAMPS_NONE)

    def test_skeleton_is_preferred_over_averaging_even_when_text_is_long(self):
        """有骨架时**绝不去均摊** —— 均摊会让"逐句跳转"跳出到错误的秒数。"""
        got = run_assemble(text="一。二。三。四。", skeleton=[(5.0, 6.0, "一二三四")],
                           seg_seconds=60.0)
        self.assertEqual(got.timestamps, TIMESTAMPS_ALIGNED)
        for a, _b, _t in got.sentences:
            self.assertGreaterEqual(a, 4.5, "用了均摊：时间轴从 0 开始而不是从骨架的 5 秒")


class MeetingIntegrationTests(unittest.TestCase):
    """`meeting.py` 里那两个薄壳必须仍然满足**既有契约**（它们被用例钉着）。"""

    def test_split_provider_text_keeps_its_pinned_behaviour(self):
        from app import meeting
        rows = meeting._split_provider_text("第一句。第二句！Third? 第四句；", 12.0)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][0], 0.0)
        self.assertAlmostEqual(rows[-1][1], 12.0, places=2)
        self.assertEqual(meeting._split_provider_text("   ", 10.0), [])

    def test_align_sentences_is_gone_from_meeting_but_the_feature_lives_on(self):
        """`_align_sentences` 已搬走（避免两份实现分叉）。

        这条钉的是"搬干净了"：`meeting.py` 里不该再有那份实现，
        而 `assemble` 里必须有 —— 否则就是**功能丢了**而不是搬家。
        """
        from app import meeting
        self.assertFalse(hasattr(meeting, "_align_sentences"),
                         "meeting.py 里还留着一份 _align_sentences？那就是两份实现了")
        self.assertTrue(callable(assemble.align_sentences))


if __name__ == "__main__":
    unittest.main()
