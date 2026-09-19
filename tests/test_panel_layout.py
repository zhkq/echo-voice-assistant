# -*- coding: utf-8 -*-
"""面板窄边条布局的守卫（2026-09-19）。

背景（用户实测："这个页面的宽度有问题，需要重新整理"）：DSH 边条里（150% 缩放时
CSS 视口 ≈ 645px）模型路由页**整页右侧被裁掉** —— 卡片、按钮、下拉全都出血。
在无头浏览器里量出来的真因：

    body { display: flex; flex-direction: column }        ← 窄边条样式
    main { margin: 12px auto }                              ← 交叉轴 auto 外边距
    → flex 子项 `main` 的 align-self: stretch **失效**，
      main 的宽度退化成 max-content（实测 508px @384 视口 / 更大 @645），
      于是 645 的视口里整页按 508+ 排版并被裁掉。

修复：`main { width: 100% }`（给确定宽度，auto 外边距没有可吸收的自由空间）+
一排 `min-width: 0`（卡片/标题/下拉：默认 `min-width: auto` 会被内容顶住不让收缩）。
本文件把这几条**关键约束**钉住 —— 它们看起来像"多余的 CSS"，删掉就会重演上面那次出血。

为什么不用浏览器做端到端断言：门禁里没有浏览器（也不该为了 CSS 引入一个）。
真正的视觉验证方式见 `docs/面板布局-窄边条.md`（无头 Edge 截图配方）。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _css():
    with open(os.path.join(_ROOT, "web", "app.css"), encoding="utf-8") as fh:
        return fh.read()


class PanelLayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = _css()

    def _props(self, selector):
        """某个选择器的**全部**规则体拼起来（同一个选择器可能分几条写，例如基础样式 + 补丁）。"""
        bodies = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertTrue(bodies, "CSS 里找不到规则：%s" % selector)
        return "\n".join(bodies)

    def _assert_has(self, selector, *props):
        have = self._props(selector)
        for p in props:
            self.assertIn(p, have, "%s 的规则里缺少 %r（窄边条里会整页顶宽/被裁）"
                          % (selector, p))

    def test_main_has_a_definite_width(self):
        """`main` 必须有确定宽度：否则交叉轴 auto 外边距会让它按 max-content 排版（整页出血）。"""
        self._assert_has("main", "width: 100%", "min-width: 0", "margin: 12px auto")

    def test_body_is_a_shrinkable_flex_column(self):
        self._assert_has("body", "min-width: 0")

    def test_cards_and_titles_can_shrink_and_wrap(self):
        self._assert_has(".card", "min-width: 0")
        self._assert_has(".card-title", "flex-wrap: wrap", "min-width: 0")

    def test_selects_and_inputs_never_push_the_layout_wider(self):
        """`<select>` 的 min-content = 最宽选项文字，默认不让收缩（就是它把卡片顶宽的）。"""
        self._assert_has("select.ctl, input.ctl", "min-width: 0", "max-width: 100%")

    def test_capability_pick_row_can_shrink(self):
        """能力卡里"下拉 + 出网图标"那一行也要能收缩（图标是 flex 兄弟节点）。"""
        self._assert_has(".cap-pick", "min-width: 0")
        self._assert_has(".cap-prov", "min-width: 0")


if __name__ == "__main__":
    unittest.main()
