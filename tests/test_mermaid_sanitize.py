# -*- coding: utf-8 -*-
"""mermaid 清洗的回归测试（``app/meeting.py`` 的 ``_sanitize_mermaid`` / ``_quote_mm_text``）。

背景（2026-09-20）：最新一条会议的纪要在面板里"mermaid 报错"，根因是模型把引号标签写成
``G["…过滤"他人说话"等环境音"]`` —— 标签里的**内层半角引号**把字符串提前闭合了。
老逻辑只在"文本带括号"时才补引号，这种没有括号的文本一路放行，于是坏语法原样落盘。

这里钉住两件事：
  1. 内层引号必须被规范化（换成 mermaid 实体 ``#quot;``，渲染出来仍是 ``"``）；
  2. 清洗器原有的本事不能被改坏（补括号引号、timeline 的 title 冒号与时间戳点号）。
"""
import unittest

from app import meeting


def _mm(body: str) -> str:
    return "```mermaid\n" + body + "\n```"


class QuoteInsideLabelTests(unittest.TestCase):
    """引号标签里再嵌半角引号 —— 本次报错的那一类。"""

    def test_the_real_failing_line_is_fixed(self):
        """真实用例：2026-09-20_15-53-00 的 summary.md 第 14 行。"""
        src = _mm('flowchart TD\n    F --> G["结论: 消费级麦克风优先过滤"他人说话"等环境音"]')
        out = meeting._sanitize_mermaid(src)
        self.assertNotIn('过滤"他人说话"等', out, "内层半角引号必须被处理掉，否则整张图报错")
        self.assertIn("#quot;他人说话#quot;", out, "应换成 mermaid 实体 #quot;（渲染仍是 \"）")

    def test_decision_node_with_inner_quotes(self):
        src = _mm('flowchart TD\n    A{"他说"你好"了吗"} --> B[结束]')
        out = meeting._sanitize_mermaid(src)
        self.assertNotIn('"他说"你好"了吗"', out)
        self.assertIn("#quot;你好#quot;", out)

    def test_unquoted_label_with_inner_quotes_is_quoted_too(self):
        src = _mm('flowchart TD\n    A[他说"你好"] --> B[结束]')
        out = meeting._sanitize_mermaid(src)
        self.assertIn('A["他说#quot;你好#quot;"]', out)

    def test_wellformed_quoted_label_is_left_alone(self):
        src = _mm('flowchart TD\n    A["他说「你好」"] --> B["结束(收尾)"]')
        out = meeting._sanitize_mermaid(src)
        self.assertIn('A["他说「你好」"]', out, "本来就对的标签不该被改动")
        self.assertIn('B["结束(收尾)"]', out)


class ExistingSanitizerBehaviourTests(unittest.TestCase):
    """清洗器原来的本事（PROGRESS §27 那批）不能被改坏。"""

    def test_parens_get_quoted(self):
        out = meeting._sanitize_mermaid(_mm('flowchart TD\n    A[初验(出验)证书] --> B[结束]'))
        self.assertIn('A["初验(出验)证书"]', out)

    def test_timeline_title_gets_colon(self):
        out = meeting._sanitize_mermaid(_mm('timeline\ntitle 录音设备技术特征对比\n消费级麦克风 : 主打通话清晰'))
        self.assertIn("title: 录音设备技术特征对比", out)

    def test_timeline_timestamp_colons_become_dots(self):
        out = meeting._sanitize_mermaid(_mm('timeline\ntitle X\n00:00:12 : 发起试音'))
        self.assertIn("00.00.12 : 发起试音", out)

    def test_other_code_blocks_and_prose_are_untouched(self):
        src = ("正文里有 A[初验(出验)证书] 这样的字面量，不该被动。\n\n"
               "```python\nx = '他说\"你好\"'\n```\n")
        self.assertEqual(meeting._sanitize_mermaid(src), src)


class PromptLessonTests(unittest.TestCase):
    """提示词侧也要写清这个坑：光靠清洗器是兜底，别让模型习惯性写错。"""

    def test_requirements_mention_the_inner_quote_rule(self):
        req = meeting._SUMMARY_REQUIREMENTS
        self.assertIn("半角双引号", req)
        self.assertIn("他人说话", req, "要给出反例，模型才看得懂")


if __name__ == "__main__":
    unittest.main()
