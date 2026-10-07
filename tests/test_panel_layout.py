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

    def test_dashboard_cards_are_content_sized_not_stretched(self):
        """仪表盘的余量**只落在会议卡上**，不许在卡内留死空白（2026-10-07 用户两次实测）。

        第一轮现场（新增「每日回顾」卡之后）：*"会议卡片高度不够，每日回顾没显示全，
        可以加个滚动条"* —— 截图里更清楚：会议列表 4 条之后一大块空白、回顾卡下面
        也空一截。真因是"整页按视口高度纵向均分"：`grid{flex:1}` + `card{flex:1 1 0}`
        让**每张卡都吃掉一份剩余空间**，多一张卡就多一份空白。

        第二轮用户把意图说全了：*"超级助理卡片在上，每日回顾和状态条在最下，
        **中间的空间留给会议**"* —— 所以余量要有落点，落在**会议卡**上。

        这两件事同时成立才正确，判据因此是三条：
          ① 卡片默认不拉伸（`flex: 0 0 auto`）—— 否则又是"每张卡吃一份"；
          ② **网格要撑满**（`flex: 1 1 auto`）+ **会议卡要吃掉余量**（`flex: 1 1 auto`）
             —— 只让卡片长、网格不长的结果是"余量 = 0"：2026-10-07 实测过一次，
             网格 834px 而 `#view-dashboard` 1310px，中间 476px 谁也没拿到；
          ③ 网格不许被撑破（`overflow-y: auto` 兜底），CDP 实测 `scrollHeight == clientHeight`。
        """
        cards = self._props(".dashboard-grid .card")
        self.assertNotIn("flex: 1 1 0", cards, "卡片又被写成 flex:1 1 0 了（每张卡都吃一份 → 死空白）")
        self.assertIn("flex: 0 0 auto", cards, "卡片默认按内容定高")
        # ② 网格撑满 + 会议卡吃余量（缺一个，余量就无处可去）
        grid = self._props(".dashboard-grid")
        self.assertIn("flex: 1 1 auto", grid,
                      "网格要撑满仪表盘视图，否则卡片没有余量可吃（实测过 476px 悬空）")
        self.assertIn("overflow-y: auto", grid, "网格要能兜底滚动，别把内容裁掉")
        self.assertIn("flex: 1 1 auto", self._props("#meetingCard"),
                      "会议卡要吃掉中间余量（用户要求「中间的空间留给会议」）")
        # 其余两张与状态栏按内容定高，不许跟着长
        for sel in ("#cmdCard", "#reviewCard", "#statusBar"):
            with self.subTest(sel=sel):
                self.assertIn("flex: 0 0 auto", self._props(sel),
                              "%s 该按内容定高（不要抢会议的余量）" % sel)
        # 回顾摘要要能自己滚（用户明确要"加个滚动条"）
        rl = self._props(".dashboard-grid .review-last")
        self.assertIn("max-height", rl, "回顾摘要缺少高度上限")
        self.assertIn("overflow-y: auto", rl, "回顾摘要要能自己滚")

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

    def test_gpu_backend_rows_can_shrink(self):
        """「GPU 后端」卡（能力路由）的两行同理：地址输入框 + 后端名 + 徽章是一排 flex 兄弟。

        这几个选择器是 2026-09-24 加的；**加新行就顺手加上这一条** —— 那次整页出血
        就是从"看起来多余的 `min-width: 0`"被删掉开始的。
        """
        self._assert_has(".cap-pair-row", "display: flex", "flex-wrap: wrap", "min-width: 0")
        self._assert_has(".cap-be-row", "display: flex", "flex-wrap: wrap", "min-width: 0")

    def test_history_rows_can_shrink(self):
        """历史页（2026-09-26 第二轮）新加的两处也要能收缩 / 能换行。

        会议条目上的「说话人 / 转写档位 / 看转写」与指令条目上的「耗时 · 后端 · 来源」
        都是**长短不定**的一串字（说话人可能是几个中文姓名、档位可能是"估算（按字数均摊） × 3"）。
        不给 `min-width: 0` + 允许断行，窄边条里就会把这行顶宽（正是这份文件开头那次出血）。
        """
        self._assert_has(".meeting-item .m-spk, .meeting-item .m-ts",
                         "min-width: 0", "overflow-wrap: anywhere")
        self._assert_has(".meeting-item .m-acts", "min-width: 0")
        self._assert_has(".cmd-item .cmd-detail", "min-width: 0", "overflow-wrap: anywhere")

    def test_collapsible_cards_have_styles(self):
        """可折叠卡片：折叠时藏 body、箭头转向 —— `.card` 与能力页签的 `.mcard` 都支持。"""
        self.assertIn(".card.collapsible.collapsed > .card-body", self.css)
        self.assertIn(".mcard.collapsible.collapsed > .mcard-body", self.css)
        self.assertIn(".collapsible.collapsed > .card-title .set-arrow", self.css)
        self.assertIn(".collapsible.collapsed > .mcard-head .set-arrow", self.css)


class CollapsibleCardWiringTests(unittest.TestCase):
    """可折叠卡片的接线（2026-09-19：先"路由参数增加折叠"，再"能力下面的各卡片也增加折叠"）。

    2026-09-25（页签整合）：原来拿「模型路由 → 路由参数」卡当**静态样板** —— 那张卡已经删了
    （7 项路由参数搬进「智能体」页签的「通道设置」卡，由 JS 渲染），静态样板换成「常规」页签里
    的**启动日志卡**（`data-collapse-id="boot-logs"`，真的静态、真的可折）。
    这条断言的意图没变：index.html 里必须有一个货真价实的 `class="card collapsible"` 样板，
    标题里带箭头、后面跟 `card-body`。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT, "web", "index.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(_ROOT, "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()

    def test_static_collapsible_card_is_wired(self):
        self.assertIn('class="card collapsible"', self.html)
        self.assertIn('data-collapse-id="boot-logs"', self.html)
        # 属性可能换行排（2026-09-26 给这张卡加了 data-collapse-default="closed"）
        m = re.search(r'<div class="card collapsible"[^>]*data-collapse-id="boot-logs"[^>]*>(.*?)<div class="card-body">',
                      self.html, re.S)
        self.assertIsNotNone(m, "找不到启动日志卡的标题块")
        self.assertIn('class="set-arrow"', m.group(1), "标题里要有折叠箭头（与设置页卡片同一套视觉）")

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
        # 动态卡片每次重绘后都要重新应用折叠状态
        # （2026-09-26 IA 重构：四个设置页签 → 三个，id 从 voice/agent 变成 business/capability）
        # 2026-10-02：IA 重构，旧断言 `applyCollapsedCards($("#view-capability"))` +
        # `["#view-general", "#view-business", "#view-capability"]` → 新断言
        # `applyCollapsedCards($("#view-settings"))` + `["#view-settings", "#view-business"]`。
        # 理由：「通用」与「能力与智能体」并成一个顶层「设置」页，两个旧 id 都已删除；
        # 意图不变（**每个设置页签的卡片重绘后都要重新应用折叠状态**）。
        self.assertIn('applyCollapsedCards($("#view-settings"))', self.js)
        self.assertIn('["#view-settings", "#view-business"]', self.js,
                      "两个设置页签的卡片都要重新应用折叠状态")

    def test_capability_top_buttons_are_compact(self):
        """顶部「下载缺失 / 刷新」要短、用 mini、同行不换行（用户实测反馈）。"""
        m = re.search(r'<button class="btn mini" id="btnCapDownloadMissing"(.*?)</button>', self.html, re.S)
        self.assertIsNotNone(m, "「下载缺失」应是 btn mini")
        self.assertIn("下载缺失", m.group(0))
        self.assertIn('class="btn mini" id="btnCapReload"', self.html)
        self.assertNotIn("一键下载缺失</button>", self.html, "长标签要缩短（完整说明进 title）")


if __name__ == "__main__":
    unittest.main()
