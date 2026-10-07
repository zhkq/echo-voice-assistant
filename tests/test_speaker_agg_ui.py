# -*- coding: utf-8 -*-
"""会议详情页（`web/meeting.html`）里「说话人聚合建议」的结构判据。

为什么用**静态判据**而不是真渲染：这个页面是 `web/meeting.html` + 内联脚本，
面板通过 `/meeting.html?id=…` 打开；它的**位置要求**（在声纹库下方）是 DOM 顺序问题，
静态读源文件就能钉住，而且稳定、不需要起浏览器。

真正需要真渲染的那部分（页面能不能跑起来）由 `tests/test_panel_renders.py` 覆盖
（它会渲染 index 页并检查没有 `ReferenceError` 之类的运行时错误）。
"""
import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "meeting.html")


class SpeakerAggUiTests(unittest.TestCase):
    def setUp(self):
        self.html = io.open(PAGE, encoding="utf-8").read()

    def test_block_sits_below_the_voiceprint_list(self):
        """位置判据（用户明确要求："放到声纹库列表下方"）。"""
        i_vp = self.html.find('id="vpList"')
        i_agg = self.html.find('id="aggList"')
        self.assertGreater(i_vp, -1, "找不到声纹库列表 #vpList")
        self.assertGreater(i_agg, -1, "找不到聚合建议容器 #aggList")
        self.assertGreater(i_agg, i_vp,
                           "聚合建议必须在声纹库列表**下方**（用户指定的位置）")

    def test_has_refresh_and_meta(self):
        """要有「重新计算」与一行说明（不然用户不知道那数字是什么）。"""
        self.assertIn('id="aggRefresh"', self.html)
        self.assertIn('id="aggMeta"', self.html)
        self.assertIn("说话人聚合建议", self.html)

    def test_calls_the_three_endpoints(self):
        """前端必须真的接上那三个端点（只画空壳是最容易犯的错）。"""
        self.assertIn("/api/speakers/agg/suggestions", self.html)
        self.assertIn("/api/speakers/agg/apply", self.html)
        self.assertIn("/api/speakers/agg/audition", self.html)

    def test_offers_audition_before_merging(self):
        """**必须先能试听**再合并 —— 并错了就是把两个人当成一个。"""
        self.assertIn("data-aud", self.html, "没有试听按钮（data-aud）")
        self.assertIn("data-do", self.html, "没有合并按钮（data-do）")

    def test_caps_suggestions_at_three(self):
        """用户要求"合并建议 1–3"：渲染时只列前 3 条。"""
        self.assertRegex(self.html, r"items\.slice\(0,\s*3\)",
                         "没有把建议限制在 3 条（用户要求 1–3）")

    def test_enroll_happens_on_merge(self):
        """`apply` 调用要带上姓名（改名 + 自动入库是这一步的全部意义）。"""
        m = re.search(r"agg/apply[^\n]*\n([^\n]*\n){0,4}", self.html)
        self.assertIsNotNone(m)
        self.assertIn("name", m.group(0))


if __name__ == "__main__":
    unittest.main()
