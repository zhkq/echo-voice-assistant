# -*- coding: utf-8 -*-
"""tests/test_dashboard_layout.py — 仪表盘结构与底部状态条（2026-10-02）

用户给的《ECHO边条页签功能设计》里，仪表盘是四组：
    超级助理 · 会议录音 · 运行情况（后端 / Agent / 语音合成 / 路由） · 操作区（重启 / 关闭 / 折叠边条）

**同日追加的口径（用户原话）**："运行情况和操作区做成仪表盘最下方的一个小横条，
用状态灯和图标表达不用现在这样两个卡片组" —— 所以后两组不再各占一张卡，而是合进
**仪表盘最后一条小横条** `#statusBar`：状态只出「灯 + 短名」（细节在 title 里悬停），
动作只留图标按钮。下面钉的就是这件事，另外钉两条用户明确的要求：
  * **一个动作只有一处入口** —— 重启/关闭/折叠边条在整份 HTML 里各出现一次；
  * **关闭 ECHO 走 `/api/control/echo/stop`**，服务端拒绝时如实显示理由（闸在
    `tests/test_control_echo.py` 里单独验）。
"""
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _between(text, a, b):
    i = text.index(a)
    return text[i:text.index(b, i)]


def _read_css():
    with io.open(os.path.join(ROOT, "web", "app.css"), encoding="utf-8") as fh:
        return fh.read()


class DashboardLayoutTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with io.open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()
        cls.dash = _between(cls.html, 'id="view-dashboard"', "<!-- ============ ② 设置")
        cls.bar = _between(cls.dash, 'id="statusBar"', "</section>")

    def test_three_groups_with_the_status_bar_last(self):
        """超级助理 → 会议录音 → **底部状态条**（最后一条；后两组并进它）。"""
        want = ['id="cmdCard"', 'id="meetingCard"', 'id="statusBar"']
        pos = [self.dash.index(w) for w in want]
        self.assertEqual(pos, sorted(pos), "三块的顺序不对：%s" % want)
        for title in ("超级助理", "会议录音"):
            self.assertIn(title, self.dash)
        # 两张独立的卡片组已经收掉（用户明确说"不用现在这样两个卡片组"）
        for gone in ('id="runCard"', 'id="opsCard"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, self.html, "%s 应当已经并进底部状态条" % gone)

    def test_the_bar_carries_three_status_cards_and_the_router_block(self):
        """三格小卡片：**后端 / Agent / 语音合成**（路由不再占一格），外加路由统计块（受开关控制）。

        用户原话（2026-10-02 第二次细化）："在仪表盘下端横着摆3个卡片分别展示三个状态
        后端：本机　Agent：DSH标准版，语音合成：在线，在三个卡片的右侧竖着摆三个按钮"。
        """
        self.assertIn('id="runStatusHost"', self.bar)
        self.assertIn('id="foWrap"', self.bar, "路由统计块也在这条横条里（开关关着时不显示）")
        fn = self.js[self.js.index("async function refreshRunStatus()"):]
        fn = fn[:fn.index("\n  applyDashboardRouterVisibility();")]
        for label in ("后端", "Agent", "语音合成"):
            with self.subTest(label=label):
                self.assertIn('"%s"' % label, fn, "状态条少了「%s」这一格" % label)
        self.assertNotIn('"路由",', fn, "路由不再单独占一格（它有自己的统计块）")
        self.assertIn("_runCard(", fn, "状态用三格小卡片表达")
        # 2026-10-05：多了第 6 个参数 `go`（卡片上的 ↗ 网页入口，可选；见 tests/test_card_links.py）
        self.assertRegex(self.js, r"function _runCard\(key, label, value, state, tip, go\)")
        # 值要**短**（"本机 / DSH标准版 / 在线"这种一眼能读的），细节在 title
        for short in ('"本机"', '"待启动"', '"未就绪"', '"在线"', '"关闭"', '"自动"'):
            with self.subTest(value=short):
                self.assertIn(short, fn, "短值里少了 %s" % short)
        # 细节（端口/URL/原因）走 title 悬停 —— 按**整个函数体**判断（原来只看前 400 字符，
        # 2026-10-05 加了注释与 ↗ 入口后窗口不够了）
        card_fn = self.js[self.js.index("function _runCard"):]
        card_fn = card_fn[:card_fn.index("\n}")]
        self.assertIn("title=", card_fn, "细节（端口/URL/原因）走 title 悬停")

    def test_the_cards_use_the_existing_dot_colors_and_the_router_block_follows_the_toggle(self):
        """灯色沿用现有那套 `.dot online/idle/offline/unknown`；路由**块**跟着开关收放。"""
        fn = self.js[self.js.index("function _runCard(key, label, value, state, tip, go)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('class="dot ${esc(state', fn)
        vis = self.js[self.js.index("function applyDashboardRouterVisibility()"):]
        vis = vis[:vis.index("\n}")]
        self.assertIn('settingValue("dashboardShowRouter")', vis)
        self.assertIn('$("#foWrap")', vis)

    def test_the_three_buttons_are_stacked_on_the_right(self):
        """三个按钮**竖着**摆在卡片右侧（用户给的样张：右侧一条竖向窄条，铺满同样高度）。"""
        css = _read_css()
        blk = css[css.index(".status-bar > .sb-ops {"):]
        blk = blk[:blk.index("}")]
        self.assertIn("flex-direction: column", blk, "右侧那三个按钮要竖排")
        self.assertIn("justify-content: space-between", blk, "三个按钮铺满那条的高度（样张里上中下各一个）")
        for bid in ("btnRestartEcho", "btnStopEcho", "btnRailCollapse"):
            with self.subTest(button=bid):
                self.assertIn('id="%s"' % bid, self.bar, "状态条里少了 %s" % bid)

    def test_the_bar_is_a_grid_of_three_equal_cards_plus_the_button_column(self):
        """**底部细条**：三格等宽 + 右侧按钮列，用**网格**钉死（别再退回 flex 继承的坑）。

        2026-10-02 这一条是**截屏 + 量几何**改出来的：`.dashboard-grid .card` 那边有
        `display:flex; flex-direction:column` 和 `flex: 1 1 0` 的继承规则，会把状态条排成竖的、
        把三格压成 16px。所以改网格：三格各 `minmax(0,1fr)`、按钮列第 4 列 `auto`，
        `#runStatusHost` 用 `display: contents` 让三格直接当网格项。
        """
        css = _read_css()
        bar = css[css.index(".dashboard-grid .card.status-bar {"):]
        bar = bar[:bar.index("}")]
        self.assertIn("display: grid", bar, "细条用网格，别用会被继承规则搅乱的 flex")
        self.assertIn("repeat(3, minmax(0, 1fr)) auto", bar, "三格等宽 + 右侧一列按钮")
        self.assertIn("padding: 2px 6px", bar, "细条的内边距（别用卡片默认的 14px）")
        self.assertIn("flex: 0 0 auto", bar, "不许跟着网格长高（`flex: 1 1 0` 会把它撑成一整块）")
        # 三格是**小卡片**的样子（用户 2026-10-02："变成三个小卡片试试"）：内嵌底色 + 边框 + 圆角
        card = css[css.index(".status-bar .sb-card {"):]
        card = card[:card.index("}")]
        self.assertIn("background: var(--bg)", card, "卡片要有底色（不然就是几根线，看着糙）")
        self.assertIn("border: 1px solid var(--border)", card)
        self.assertIn("border-radius", card)
        self.assertIn("display: contents", css[css.index(".status-bar > .sb-status"):][:80])
        self.assertIn("grid-column: 4", css[css.index(".status-bar > .sb-ops"):][:120],
                      "按钮列固定在最后一列")
        ops = css[css.index(".status-bar .sb-ops .btn.icon {"):]
        ops = ops[:ops.index("}")]
        self.assertIn("height: 14px", ops, "三个小图标决定细条高度")
        fn = self.js[self.js.index("function _runCard(key, label, value, state, tip, go)"):]
        fn = fn[:fn.index("\n}")]
        for piece in ('class="dot ', 'class="sb-lb"', 'class="sb-val"'):
            with self.subTest(piece=piece):
                self.assertIn(piece, fn, "一格卡片要有：灯 / 名 / 短值")
        self.assertIn('class="sb-top"', fn, "两行版：上面「灯 + 名」")
        card = css[css.index(".status-bar .sb-card {"):]
        card = card[:card.index("}")]
        self.assertIn("flex-direction: column", card, "两行版卡片内部竖排（2026-10-02 用户要试的）")
        # 第二行**靠右**、并按状态上色（用户口径："第二行靠右更好看"）
        val = css[css.index(".status-bar .sb-val {"):]
        val = val[:val.index("}")]
        self.assertIn("text-align: right", val, "第二行靠右")
        for cls, var in (("is-online", "--green"), ("is-idle", "--yellow"), ("is-offline", "--red")):
            with self.subTest(state=cls):
                self.assertRegex(css, r"\.sb-card\.%s \.sb-val \{ color: var\(%s\)" % (cls, var))
        self.assertIn("is-${esc(state", fn, "卡片要带状态类（值才能按状态上色）")
        self.assertNotIn('class="spacer"', self.bar, "flex 时代的占位元素在网格里会抢走一格")

    def test_the_router_row_cannot_collide_with_the_button_column(self):
        """路由块与按钮列**各行其位** —— 2026-10-09 用户实测："打开 dashboardShowRouter 之后布局乱了"。

        真因两条（都是"自动放置"惹的，改的时候别再交回去）：

        ① `.sb-fo` 原来只写 `grid-column: 1 / -1`，**行号交给自动放置**；`.sb-ops` 是
           `grid-column: 4`，行号也自动。两者谁先占行会随 DOM/内容变 —— 于是挤进同一行互相压
           （截图里路由文字与「详情 ›」叠在一起、按钮列被顶歪）。→ 两边都写死 `grid-row`。
        ② `.fo-right` 的 `flex: 1 1 100%` 是给**独立卡片**用的（让各通道计数独占第二行），
           复用到状态条这条横排里就索要整行宽度，把「详情 ›」挤到 0 宽、文字竖着断行。
           → 状态条里改成按内容收缩。
        """
        css = _read_css()
        fo = css[css.index(".status-bar > .sb-fo {"):]
        fo = fo[:fo.index("}")]
        self.assertIn("grid-row: 2", fo, "路由块的行号要写死（否则会和按钮列撞行）")
        self.assertIn("grid-column: 1 / -1", fo, "路由块占整行")

        ops = css[css.index(".status-bar > .sb-ops {"):]
        ops = ops[:ops.index("}")]
        self.assertIn("grid-row: 1", ops, "按钮列固定在第 1 行")
        self.assertIn("grid-column: 4", ops)

        right = css[css.index(".status-bar .sb-fo .fo-right {"):]
        right = right[:right.index("}")]
        self.assertIn("flex: 0 1 auto", right,
                      "状态条里的通道计数要按内容收缩，不能索要整行（否则挤坏「详情 ›」）")

        more = css[css.index(".status-bar .sb-fo .more {"):]
        more = more[:more.index("}")]
        self.assertIn("white-space: nowrap", more, "「详情 ›」不许被折成两行")

    def test_the_actions_are_icon_buttons_with_exactly_one_entry_each(self):
        """动作只留图标按钮；每个动作在整个页面里**恰好一处**（这条同时防重复 id）。"""
        for bid in ("btnRestartEcho", "btnStopEcho", "btnRailCollapse"):
            with self.subTest(button=bid):
                self.assertIn('id="%s"' % bid, self.bar, "状态条里少了 %s" % bid)
                self.assertEqual(self.html.count('id="%s"' % bid), 1,
                                 "%s 在页面上出现了 %d 次" % (bid, self.html.count('id="%s"' % bid)))
        # 图标按钮：说明走 title（悬停），不是写在脸上的长句
        for bid, tip in (("btnRestartEcho", "重启"), ("btnStopEcho", "关闭"), ("btnRailCollapse", "折叠")):
            with self.subTest(button=bid):
                blk = _between(self.bar, 'id="%s"' % bid, "</button>")
                self.assertIn("title=", blk, "%s 要有悬停说明" % bid)
                self.assertIn(tip, blk)

    def test_restart_has_exactly_one_entry_point(self):
        """用户口径（"冗余的配置不要"）：重启只许有一处 —— 底部状态条。"""
        self.assertEqual(self.html.count('id="btnRestartEcho"'), 1)
        settings = _between(self.html, 'id="view-settings"', "<!-- ============ 历史")
        self.assertNotIn('id="btnRestartEcho"', settings, "设置页里不该再有重启按钮")

    def test_stop_echo_calls_the_control_endpoint(self):
        fn = self.js[self.js.index("async function stopEcho(btn)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("/api/control/echo/stop", fn)
        self.assertIn("r.ok", fn, "服务端拒绝时要如实显示理由，不能一律说成功")
        self.assertIn("confirmDialog", fn, "关闭是不可逆动作，必须先确认")

    def test_the_stop_button_is_wired(self):
        handler = self.js[self.js.index('const btn = e.target.closest("#btnRestartEcho");'):]
        handler = handler[:handler.index("});")]
        self.assertIn("#btnStopEcho", handler)
        self.assertIn("stopEcho(stopBtn)", handler)


if __name__ == "__main__":
    unittest.main()
