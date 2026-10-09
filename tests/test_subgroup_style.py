# -*- coding: utf-8 -*-
"""tests/test_subgroup_style.py — 二级分组的样式统一（2026-10-02）

用户原话："AI组件下面的 Agent，转写服务，语音合成等二级分组也改进一下样式，
其他二级分组也同样改造"。

做法：二级分组不再是一行灰色小标题（`.ssec`），而是**一张内嵌小卡片** ——
`app.js` 的 `sSub(标题, 内容)` 产出 `div.ssub > div.ssub-h + 内容`。
关键在于**共享渲染器**：所有卡片的「高级」小节都走 `renderAdvSections()`，
所以那一处改成 `sSub`，全卡片的二级分组一起变；函数自绘的那几处（AI 组件的四个小节、
智能体卡的产品开关/参数、会议转写卡的两段、组件进程）单独套；
模型路由卡那 4 处是静态 HTML，手写同一套标记。

这组用例钉两件事：**都在用 sSub/ssub**，以及**旧的 `.ssec` 不许回流**（回流就会出现两套视觉）。
"""
import io
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class SubgroupStyleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.css = _read("web", "app.css")
        cls.html = _read("web", "index.html")

    def test_the_helper_exists_and_emits_one_card(self):
        self.assertRegex(self.js, r"function sSub\(title, body\)")
        fn = self.js[self.js.index("function sSub(title, body)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('class="ssub collapsible"', fn)
        self.assertIn('class="ssub-h"', fn)
        self.assertIn("esc(title)", fn, "标题要转义")

    def test_the_shared_advanced_renderer_uses_it(self):
        """**这一处**决定"其他二级分组也同样改造"是否成立 —— 所有卡片的高级小节都走它。"""
        fn = self.js[self.js.index("function renderAdvSections(card, items)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("sSub(nm, body)", fn, "高级区的小节要包成小卡片")
        self.assertIn("showHead ? sSub(nm, body) : body", fn,
                      "只有一个小节时不包（多一层没标题的框更碎）")

    def test_hand_drawn_groups_are_wrapped_too(self):
        """函数自绘的几处（渲染器管不到的）也一处处套上了。"""
        for call in ('sSub("产品开关"', 'sSub("转写走哪条路"', 'sSub("分离与声纹由谁做"',
                     'sSub("能力选择"', 'sSub("在线服务"',
                     'sSub("组件进程（启停）"',
                     # 2026-10-02 用户给的层级：AI 组件 = Agent / 转写服务 / 语音合成（各自再分小组）
                     'sSub("Agent"', 'sSub("选择开关"', 'sSub("工作区"', 'sSub("归档"',
                     'sSub("转写服务"', 'sSub("后端设置"', 'sSub("语音合成"'):
            with self.subTest(call=call):
                self.assertIn(call, self.js, "漏了：" + call)
        self.assertIn('sSub(SET_GROUP_NAMES[g] || g', self.js, "「未归类」兜底卡的按组渲染也要套")

    def test_the_chevron_is_a_downward_double_arrow_on_the_right(self):
        """二级卡片的折叠标记：**右侧向下双箭头**（与一级卡片左侧单箭头 `▶` 区分）。"""
        chev = self.js[self.js.index("const SSUB_CHEV"):]
        chev = chev[:chev.index(";")]
        self.assertIn('class="ssub-chev"', chev)
        self.assertIn("<svg", chev, "用 SVG 画（不用字符箭头）")
        self.assertEqual(chev.count("<path"), 2, "**双**箭头 = 两条 path")
        self.assertIn("M6 6.5 12 11l6-4.5", chev, "朝下")
        css = _read("web", "app.css")
        self.assertIn(".ssub-h { justify-content: space-between; }", css, "标记靠右")
        self.assertRegex(css, r"\.ssub\.collapsible\.collapsed > \.ssub-h \.ssub-chev "
                              r"\{ transform: rotate\(180deg\); \}", "收起时翻过来")
        self.assertIn('class="set-arrow"', self.js, "一级卡片仍是那个单箭头（两套要能区分）")

    def test_the_static_router_card_uses_the_same_markup(self):
        """模型路由卡是静态 HTML（调不到 sSub），所以手写同一套标记。"""
        for title in ("语言模型", "通道成员（顺序 = 优先级）", "派发情况", "路由参数"):
            with self.subTest(title=title):
                # 静态那几处也带上了折叠结构（2026-10-02 用户："二级卡片也能折叠"）
                self.assertRegex(self.html,
                                 r'<div class="ssub collapsible" data-collapse-id="ssub-[^"]+">\s*'
                                 r'<div class="ssub-h"><span>%s</span><span class="ssub-chev"'
                                 % title)
                self.assertIn('<div class="ssub-body">', self.html)
        self.assertNotIn('class="ssec"', self.html)

    def test_the_old_ssec_style_is_gone(self):
        """`.ssec` 不许回流：两套视觉并存就白改了（注释里提到它是允许的）。"""
        self.assertNotIn('class="ssec"', self.js)
        self.assertNotIn(".ssec {", self.css, "旧的 .ssec 规则要删掉")
        self.assertIn(".ssub {", self.css)
        self.assertIn(".ssub-h {", self.css)
        sub = self.css[self.css.index(".ssub {"):]
        sub = sub[:sub.index("}")]
        self.assertIn("background: var(--bg)", sub, "小卡片要内嵌底色（与仪表盘那三格同一套）")
        self.assertIn("border: 1px solid var(--border)", sub)
        self.assertIn("border-radius", sub)


if __name__ == "__main__":
    unittest.main()

class AiCardHierarchyTests(unittest.TestCase):
    """「AI 组件」卡按用户 2026-10-02 给的层级重排后的硬指标。

    用户原话（要点）：Agent → 选择开关（不同 agent 显示对应配置，如端口/CLI）+ 工作区 + 归档；
    转写服务 → 本机后端还是网络后端 + 后端设置（本机：启停·安装／网络：配对·连接状态）；
    语音合成 → 在线还是本地。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_the_three_top_groups_in_order(self):
        fn = self.js[self.js.index("function renderAiCardCommon()"):]
        fn = fn[:fn.index("\n}")]
        order = [fn.index('sSub("Agent"'), fn.index('sSub("转写服务"'), fn.index('sSub("语音合成"')]
        self.assertEqual(order, sorted(order), "三组的顺序：Agent → 转写服务 → 语音合成")
        for group in ("选择开关", "工作区", "归档", "后端设置"):
            with self.subTest(group=group):
                self.assertIn('sSub("%s"' % group, fn, "少了二级分组：" + group)

    def test_the_agent_group_carries_its_own_config(self):
        """「选择开关」里要带**该 agent 自己的参数**（端口 / CLI 那些，由 agentDetailHtml 画）。"""
        fn = self.js[self.js.index("function renderAiCardCommon()"):]
        fn = fn[:fn.index("\n}")]
        self.assertRegex(fn, r'sSub\("选择开关",\s*renderAgentCardCommon\(\) \+ agentDetailHtml\(\)\)')
        adv = self.js[self.js.index("function renderAgentCardAdv()"):]
        adv = adv[:adv.index("\n}")]
        self.assertNotIn("agentDetailHtml", adv, "参数已搬进 Agent → 选择开关，高级里不该再画一遍")

    def test_the_workspace_group_has_the_four_rows_the_user_listed(self):
        fn = self.js[self.js.index("function renderAiCardCommon()"):]
        fn = fn[:fn.index("\n}")]
        for key in ("meetingWorkspace", "meetingWorkspaceTitle",
                    "commandWorkspace", "commandWorkspaceTitle"):
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, fn, "工作区少了 %s" % key)
        for key in ("worklogVaultRoot", "worklogPrompt"):
            with self.subTest(key=key):
                self.assertIn('"%s"' % key, fn, "归档少了 %s" % key)

    def test_the_backend_settings_host_gets_the_static_card(self):
        """「后端设置」是个占位；静态卡 `#capRouteCard` 在渲染末尾被**克隆**进去（ids 不变）。

        ⚠️ **2026-10-09 改的：原来是"搬节点"（`appendChild(beCard)`），那是错的。**
        上面那行 `host.innerHTML = …` 会把 `#capBackendHost` 连同搬进去的卡**一起销毁**
        —— 于是第二次重绘之后 `$("#capRouteCard")` 在文档里再也不存在，这张卡
        （本机/网络单选 + 配对框 + 启停）就永久消失。用户报的正是这个：
        *"设置-ai组件-转写服务-后端设置下没有内容"*。
        现在：重绘前存一份模板、重绘后放一份**新克隆**，并重新接线（克隆不带监听）。
        """
        self.assertIn('sSub("后端设置", `<div id="capBackendHost"></div>`)', self.js)
        panes = self.js[self.js.index("Object.keys(hosts).forEach"):]
        panes = panes[:panes.index("const rp =")]
        self.assertIn('$("#capBackendHost")', panes)
        self.assertIn("cloneNode(true)", panes,
                      "要**克隆**进占位 —— 搬节点会被上面的 innerHTML 销毁（2026-10-09 实测）")
        self.assertNotIn("appendChild(beCard)", panes,
                         "「搬节点」那条已经废弃：那张卡会被重绘销毁、永久消失")
        self.assertIn("_capRouteCardTpl", panes, "克隆用的模板要存在")
        self.assertIn("bindCapabilityCardControls()", panes, "克隆上的按钮要重新接线")
        self.assertIn("applyTranscribeSettingsVisibility();", panes)

    def test_local_and_network_halves_switch_by_the_radio(self):
        """本机 → 启停/安装；网络 → 配对/连接状态。**必须用单选当前值**判定（缓存是异步的）。"""
        self.assertIn('id="capPairSettings"', self.html)
        self.assertIn('id="capLocalSettings"', self.html)
        fn = self.js[self.js.index("function applyTranscribeSettingsVisibility(explicit)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("explicit ||", fn, "刚点单选时要用显式取值，别读还没回写的缓存")
        self.assertIn('$("#capPairSettings")', fn)
        self.assertIn('$("#capLocalSettings")', fn)
        self.assertRegex(self.js, r"applyTranscribeSettingsVisibility\(rb\.value\)",
                         "单选变了的处理器要传 rb.value")


if __name__ == "__main__":
    unittest.main()

class SubgroupCollapseTests(unittest.TestCase):
    """二级分组小卡片**也能折叠**（2026-10-02 用户："二级卡片也能折叠"）。

    做法是**并进整卡那套**折叠机制（三个选择器常量各加一个 `.ssub.collapsible` 分支），
    所以 localStorage、箭头、aria-expanded、键盘都直接复用 —— 不新造第二套。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.css = _read("web", "app.css")
        cls.html = _read("web", "index.html")

    def test_the_shared_collapse_selectors_cover_subcards(self):
        for name in ("COLLAPSE_TITLE_SEL", "COLLAPSE_ANY_SEL", "COLLAPSE_TITLE_CHILD"):
            with self.subTest(const=name):
                seg = self.js[self.js.index("const %s" % name):]
                seg = seg[:seg.index(";")]
                self.assertIn(".ssub", seg, "%s 要覆盖 .ssub.collapsible" % name)

    def test_ssub_emits_the_collapsible_structure(self):
        fn = self.js[self.js.index("function sSub(title, body)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("const SSUB_CHEV", self.js, "折叠标记是独立常量（sSub 引用它）")
        self.assertIn("${SSUB_CHEV}", fn, "标题右侧挂的就是那个标记")
        for piece in ('class="ssub collapsible"', "data-collapse-id=", 'class="ssub-h"',
                      'class="ssub-body"'):
            with self.subTest(piece=piece):
                self.assertIn(piece, fn, "sSub 少了 %s" % piece)
        self.assertNotIn("data-collapse-default", fn, "二级分组默认**展开**（是内容，不该默认藏）")

    def test_the_static_router_subcards_are_collapsible_too(self):
        for cid in ("ssub-语言模型", "ssub-通道成员", "ssub-派发情况", "ssub-路由参数"):
            with self.subTest(cid=cid):
                self.assertIn('data-collapse-id="%s"' % cid, self.html)
        # 静态一共 5 处：模型路由卡那 4 节 + 页面级「高级」（2026-10-02 收进去的）
        self.assertEqual(self.html.count('class="ssub-body"'), 5,
                         "静态这 5 处要各有一层 .ssub-body")

    def test_the_css_hides_the_collapsed_body_and_rotates_the_arrow(self):
        self.assertRegex(self.css, r"\.ssub\.collapsible\.collapsed > \.ssub-body \{ display: none; \}")
        self.assertIn(".ssub.collapsible > .ssub-h { cursor: pointer;", self.css)
        # 折叠标记是**右侧向下双箭头**（用户 2026-10-02），收起时翻 180°
        self.assertRegex(self.css, r"\.ssub\.collapsible\.collapsed > \.ssub-h \.ssub-chev "
                                  r"\{ transform: rotate\(180deg\); \}")
        self.assertIn(".ssub-h { justify-content: space-between; }", self.css, "标记靠右")


if __name__ == "__main__":
    unittest.main()

class PageLevelAdvancedGroupTests(unittest.TestCase):
    """页面级「高级」也是二级分组（用户 2026-10-02："这个也是二级分组"）。

    它原来是把四张卡**散着**摆在设置页末尾（本地组件状态及启停 / 启动日志 / 环境体检及清理 /
    安装向导），现在收进一个 `.ssub`（同一套视觉 + 同一个折叠机制），顺序照用户给的。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = _read("web", "index.html")

    def test_it_is_a_subcard_with_the_four_blocks_in_order(self):
        self.assertIn('data-collapse-id="ssub-高级"', self.html)
        i = self.html.index('data-collapse-id="ssub-高级"')
        seg = self.html[i:self.html.index("</section>", i)]
        order = [seg.index('id="%s"' % cid)
                 for cid in ("capLocalCard", "bootLogCard", "cleanupCard", "wizCard")]
        self.assertEqual(order, sorted(order),
                         "四块顺序：本地组件状态及启停 → 启动日志 → 环境体检及清理 → 安装向导")
        self.assertIn('<div class="ssub-h"><span>高级</span>', seg)
        self.assertIn('class="ssub-chev"', seg, "折叠标记与别的二级分组一致（右侧双箭头）")
        self.assertIn('<div class="ssub-body">', seg)


if __name__ == "__main__":
    unittest.main()

class DefaultCollapsedAndResidueTests(unittest.TestCase):
    """设置页 / 业务配置页**打开时默认全折叠**（用户 2026-10-03），外加两处残留的清理。

    默认值由 `data-collapse-default="closed"` 表达，只在"用户没表过态"时生效一次
    （见 app.js 的 `_seedCollapseDefaults`）——所以不会把用户手动展开过的卡又收回去。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.html = _read("web", "index.html")

    def test_rendered_cards_default_to_collapsed(self):
        fn = self.js[self.js.index("function renderCard(card)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('data-collapse-default="closed"', fn,
                      "设置页/业务配置页的卡片（都由 renderCard 画）默认收起")

    def test_the_two_static_ones_default_to_collapsed_too(self):
        self.assertRegex(self.html, r'data-collapse-id="ssub-高级" data-collapse-default="closed"')
        self.assertRegex(self.html, r'id="rtMergeCard"[\s\S]{0,80}data-collapse-default="closed"')

    def test_the_router_note_residue_is_gone(self):
        """用户圈出的多余残留：卡片不显示时还挂一句"模型路由：只有 Agent 选…"。"""
        self.assertNotIn("rtAgentNote", self.html)
        self.assertNotIn("rtAgentNote", self.js)
        self.assertNotIn("只有 Agent 选「标准版 harness」时才需要它", self.js)

    def test_the_history_header_card_residue_is_gone(self):
        """历史页那张"历史 / 说过的指令 · 开过的会"头卡（连里面藏着的 `.tabs-sm`）都删了。"""
        self.assertNotIn('class="tabs-sm', self.html)
        self.assertNotIn('<span class="muted" style="font-size:12px">说过的指令', self.html)
        self.assertIn('id="htab-commands"', self.html, "两页的面板本身要留着")
        self.assertIn('id="htab-meetings"', self.html)


if __name__ == "__main__":
    unittest.main()
