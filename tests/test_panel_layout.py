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

    def test_collapsible_cards_have_styles(self):
        """可折叠卡片：折叠时藏 body、箭头转向 —— `.card` 与能力页签的 `.mcard` 都支持。"""
        self.assertIn(".card.collapsible.collapsed > .card-body", self.css)
        self.assertIn(".mcard.collapsible.collapsed > .mcard-body", self.css)
        self.assertIn(".collapsible.collapsed > .card-title .set-arrow", self.css)
        self.assertIn(".collapsible.collapsed > .mcard-head .set-arrow", self.css)


class CollapsibleCardWiringTests(unittest.TestCase):
    """可折叠卡片的接线（2026-09-19：先"路由参数增加折叠"，再"能力下面的各卡片也增加折叠"）。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT, "web", "index.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(_ROOT, "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_router_params_card_is_collapsible(self):
        self.assertIn('class="card collapsible"', self.html)
        self.assertIn('data-collapse-id="router-params"', self.html)
        m = re.search(r'<div class="card collapsible"[^>]*data-collapse-id="router-params">(.*?)<div class="card-body">',
                      self.html, re.S)
        self.assertIsNotNone(m, "找不到路由参数卡的标题块")
        self.assertIn('class="set-arrow"', m.group(1), "标题里要有折叠箭头（与设置页分组同一套视觉）")

    def test_toggle_is_wired_with_persistence(self):
        for token in ("function toggleCollapsibleCard", "function applyCollapsedCards",
                      "function _collapsedCards", "echo.panel.collapsedCards"):
            with self.subTest(token=token):
                self.assertIn(token, self.js)
        self.assertIn("applyCollapsedCards();", self.js, "页面加载时要应用上次的折叠状态")
        self.assertIn("_cardTitleOf(e.target)", self.js, "点击/回车都要能切换")
        # 只认"直接子标题"：别把卡片内部嵌套的其它标题（设置页分组）也当成卡片折叠开关
        self.assertIn(".card.collapsible > .card-title, .mcard.collapsible > .mcard-head", self.js)

    def test_capability_cards_are_collapsible_too(self):
        """能力页签的卡片（转写/朗读/唤醒/分离/声纹/设备/运行环境）也能折。"""
        for token in ('data-collapse-id="cap-${esc(kind)}"',        # 能力卡
                      'data-collapse-id="cap-func-${esc(f.id)}"',   # 功能卡（唤醒/分离/声纹/设备）
                      'data-collapse-id="cap-env"',                 # 运行环境
                      'class="mcard collapsible"',
                      'class="mcard${cls} collapsible"'):
            with self.subTest(token=token):
                self.assertIn(token, self.js)
        self.assertIn('applyCollapsedCards($("#view-capabilities"))', self.js,
                      "动态卡片每次重绘后都要重新应用折叠状态")

    def test_capability_top_buttons_are_compact(self):
        """顶部「下载缺失 / 刷新」要短、用 mini、同行不换行（用户实测反馈）。"""
        m = re.search(r'<button class="btn mini" id="btnCapDownloadMissing"(.*?)</button>', self.html, re.S)
        self.assertIsNotNone(m, "「下载缺失」应是 btn mini")
        self.assertIn("下载缺失", m.group(0))
        self.assertIn('class="btn mini" id="btnCapReload"', self.html)
        self.assertNotIn("一键下载缺失</button>", self.html, "长标签要缩短（完整说明进 title）")


if __name__ == "__main__":
    unittest.main()
