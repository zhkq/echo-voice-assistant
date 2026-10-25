# -*- coding: utf-8 -*-
"""手机「开始回顾 + 准备」在**电脑面板**上必须有可见状态（2026-10-25 用户实测）。

现场：手机点开始回顾，DSH 里看到了准备提示，但仪表盘那张「每日回顾」卡仍然显示
「今天还没回顾」，回顾历史里也一条都没有 —— 用户的理解是"做了事却什么都没留下"。

事实：
  * 准备不是一轮回顾（`commands.source='review'` 一行都没有），所以**历史确实是空的**；
  * 但面板卡不能因此说"今天还没回顾"：它已经拿到了 `GET /api/daily-review` 的
    `sessionId` 与 `pre`，应该显示"准备中 / 已就绪 / 准备未完成"。

这里钉住面板的两处：
  1. `refreshReviewCard()` 优先看 `pre.phase` 与会话是否存在，再决定文案；
  2. 「回顾历史」在没有今天轮次时，给今天一条**明确写"还没口述"**的占位（不计数）。
"""
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class DashboardPrepStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        i = cls.js.index("async function refreshReviewCard()")
        j = cls.js.index("/* 面板上的「开始回顾」走", i)
        cls.card = cls.js[i:j]

    def test_it_shows_preparing_instead_of_today_not_reviewed(self):
        self.assertIn('pre.phase === "pre"', self.card)
        self.assertIn('short = "准备中"', self.card)
        self.assertIn("手机正在准备今天的回顾素材", self.card)
        self.assertIn("手机回顾准备中", self.card)
        # 顺序：pre.phase=pre 必须排在 "今天还没回顾" 那个兜底之前
        self.assertLess(
            self.card.index('pre.phase === "pre"'),
            self.card.index('short = "今天还没回顾"'),
            "准备中必须优先于『今天还没回顾』兜底",
        )

    def test_a_session_without_turns_is_not_reported_as_nothing(self):
        self.assertIn("hasSession", self.card)
        self.assertIn('short = "已就绪"', self.card)
        self.assertIn("今天的回顾已准备好", self.card)
        self.assertIn("还没有口述", self.card)
        # 有会话时不能再落到"今天还没回顾"
        self.assertIn("else if (hasSession)", self.card)
        self.assertIn('info.textContent = "今天还没回顾"', self.card)

    def test_timeout_failed_and_skipped_are_visible(self):
        for token in ('pre.phase === "timeout"', 'pre.phase === "failed"',
                      'pre.phase === "stopped"', "准备未完成", "已跳过准备"):
            self.assertIn(token, self.card)

    def test_history_shows_a_today_placeholder_until_the_first_turn(self):
        i = self.js.index("async function loadReviewHistory(")
        j = self.js.index("/** 某一天的详情", i)
        block = self.js[i:j]
        self.assertIn("还没口述", block)
        self.assertIn("pending", block)
        self.assertIn('items.length && !pending', block)
        self.assertIn("data-review-date", self.js)   # 真记录仍可点开


if __name__ == "__main__":
    unittest.main()
