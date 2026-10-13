# -*- coding: utf-8 -*-
"""「GPU 后端」卡（能力路由）与面板的接线（纯静态断言，门禁里没有浏览器）。

为什么值得用静态断言钉：这块界面**只有真点开面板才看得见**，而它最容易出的错不是
"样式丑"，是**接线断了却没人知道** —— `$("#capPairCode")` 拼错一个字母、
卡片被放到一个没人切过去的页签里、`loadCapabilities()` 忘了调它。
这三种在浏览器里全是"安静的空白"，在代码上全都看得见。

（浏览器端的真实验证见 `docs/面板布局-窄边条.md` 的无头 Edge 配方；门禁不引浏览器。）
"""
import os
import re
import sys
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(_ROOT, "web", name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- 结构判据（html.parser）
# 有几条判据只能按**结构**判（"这块有没有被 .muted 包住"、"两块是不是兄弟"）。
# 数 `</div>` 那种写法在"少一个/多一个"时同样会数平 —— 2026-10-13 的事故就是这么发生的：
# 那个 `<div class="muted">` 从未闭合，整块后端设置被吞进 12px 灰字，而文件里的 div 数**仍然平衡**。
_VOID_TAGS = {"br", "input", "img", "meta", "link", "hr", "source", "col"}


class _HtmlTree(HTMLParser):
    """够用的解析器：记住每个 `id` 的**祖先链**，以及每段文字所在的位置。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.by_id = {}
        self.texts = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if d.get("id"):
            self.by_id[d["id"]] = list(self.stack)
        self.stack.append((tag, (d.get("class") or "").split(), d.get("id") or ""))
        if tag in _VOID_TAGS:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        t = data.strip()
        if t:
            self.texts.append((list(self.stack), t))


def _inside(chain, cls):
    return any(cls in classes for _tag, classes, _id in chain)


def _chain(chain):
    return " > ".join("%s%s" % (t, ("." + ".".join(c)) if c else "") for t, c, _i in chain)

#: 这张卡里**要被人点的**元素（必须真接上事件，否则点了没反应）。
#: 2026-09-25 移除了 `btnCapRouteSave`：转写/分离/声纹三项（+ 出网许可、后端地址）已经收进
#: 本页顶部的「会议转写服务」卡（一个单选 + 一个「会议产出」下拉 = 原来那三个键），
#: 那张卡的行随页签顶部的「保存」落库，所以这个"第二套下拉的保存按钮"被删掉了
#: （删的是**元素本身**，不是放宽这条断言的意图：要点的东西仍然必须接线）。
_WIRED_IDS = ("btnCapPair", "btnCapUnpair", "btnCapRouteProbe",
              "capPairUrl", "capPairHost", "capPairCode", "btnCapSetAddr", "capPairState", "capBackendList",
              "capRouteSettings", "capRouteBadge",
              # 「起本机后端」（2026-09-30，实施方案 §5 的批 1d）
              "btnCapBackendStart", "btnCapBackendStop", "btnCapBackendReady")


class CapabilityCardWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read("app.js")
        cls.html = _read("index.html")
        cls.html_ids = set(re.findall(r'id="([^"]+)"', cls.html))
        cls.js_ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', cls.js))

    def test_every_card_id_the_js_reaches_for_exists_in_the_html(self):
        """`$("#x")` 里的 id 必须在 HTML 里存在 —— 拼错一个字母就是一个安静的空白。

        **为什么只查 `cap*` / `btnCap*` 这一片，而不是全面板**：`app.js` 里另有 9 个 id
        （`wizBody` / `startupNotes` / `agentTable` …）是**运行时用 innerHTML 造出来**的，
        通用检查会在那些地方误报。把范围写清楚，比写一个要靠白名单才能过的通用检查更有意义。
        """
        card_ids = {i for i in self.js_ids if i.startswith(("cap", "btnCap"))}
        self.assertTrue(card_ids, "一个能力卡的元素都没引用到？")
        # 运行时由 JS 造出来的 id（**不是拼错**）：
        #   capBackendHost = 「转写服务 → 后端设置」里的占位，由 renderAiCardCommon() 画出来，
        #   随后静态卡 `#capRouteCard` 被搬进它（2026-10-02 用户给的层级）。
        dyn_ids = {"capBackendHost"}
        missing = sorted(card_ids - self.html_ids - dyn_ids)
        self.assertEqual(missing, [], "JS 引用了 HTML 里不存在的 id：%s" % missing)

    def test_the_backend_admin_link_is_rendered_and_the_hint_lives_in_a_title(self):
        """**设置分家**的客户端那一半（2026-09-28，设计 §6.6；2026-10-13 第二轮收紧）。

        客户端只给**一个入口**：管理面地址由后端宣告（`adminUrl`），客户端不猜端口、
        也不展示/编辑后端的设置（端口 / 显存 / 档位 / 配额 / TLS 全归后端 `server.yaml`
        与管理面）。**远程后端"怎么进去"那句（管理面只发布在它那台机器的回环上）挂
        `title`** —— 2026-10-13 用户口径："仅保留配置项，说明性文字都不需要展示"。
        """
        self.assertIn("capBackendAdminRow", self.js)
        self.assertIn("adminUrl", self.js, "深链要读后端宣告的地址，不许猜端口")
        # 2026-10-13：远端后端**不再展示**管理面入口（点不开），所以那句 ssh 办法也从
        # app.js 里拿掉了 —— 这里只钉"入口只在本地后端出现"（见下面那条）。
        row = self.js[self.js.index("function capBackendAdminRow("):]
        row = row[:row.index("\n}\n") + 3]
        self.assertIn("title=", row, "那句说明必须在 title 里")
        self.assertNotIn("capBackendHealthLine", self.js,
                         "版本/显存/模型数那行健康数字已按新口径下线")

    def test_the_things_people_click_are_actually_wired(self):
        """反过来：卡里要被人点的元素，JS 必须真的引用它。

        （HTML 里留一个没人用的 id 是无害的容器锚点；但一个**该接事件却没接**的按钮
        在界面上看不出区别 —— 点了没反应才是最难查的那种。）
        """
        for el in _WIRED_IDS:
            with self.subTest(id=el):
                self.assertIn('id="%s"' % el, self.html, "HTML 里没有这个元素")
                self.assertIn(el, self.js_ids, "HTML 里有，但 JS 从来没引用它（点了没反应）")

    def test_the_moved_card_survives_settings_rerenders(self):
        """「已配对后端连接情况」卡是**克隆**进占位的，不是搬节点（2026-10-09 修）。

        现场（用户原话）：*"设置-ai组件-转写服务-后端设置下没有内容，那个位置应该是选择
        本机还是网络，如果选择网络，就有配对情况或者配对框"* —— 他描述的正是这张卡的内容。

        真因：`renderSettingsPanes()` 里 `host.innerHTML = …` 会把 `#capBackendHost`
        **连同搬进去的静态卡一起销毁** —— 于是**第二次**重绘跑完之后 `$("#capRouteCard")`
        在文档里再也不存在，那张卡（本机/网络单选 + 配对框 + 启停）就永久消失了。
        判据因此三条：克隆而不是搬、模板变量要先声明、克隆上的按钮要重新接线。
        """
        fn = self.js[self.js.index("function renderSettingsPanes() {"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("cloneNode(true)", fn, "要克隆进占位 —— 搬进去会被 innerHTML 销毁")
        self.assertNotIn("parentElement !== beHost", fn, "「搬节点」那条判据必须已经删掉")
        self.assertIn("_capRouteCardTpl", fn, "重绘前存模板、重绘后放克隆")
        # 克隆之后原节点必须摘掉，否则同 id 并存（卡出现两遍，而 $("#…") 只命中第一个）
        self.assertIn("capCardNow.remove()", fn,
                      "原节点与克隆会同 id 并存 —— 必须把原节点移出文档")
        self.assertIn("bindCapabilityCardControls()", fn,
                      "克隆出来的按钮不带监听，必须重新接线（否则配对/检测/解除全是死按钮）")
        # 模板变量必须先声明：`"use strict"` 下漏声明 = ReferenceError，而外面裹着 try
        # → 表现成"静默的坏显示"（这正是本仓库 2026-10-01 那次事故的形状）
        self.assertRegex(self.js, r"let _capRouteCardTpl = null",
                         "面板状态变量先 let 再赋值（PanelStateDeclarationTests 也扫这条）")
        # 接线函数得真的接那四个按钮
        bf = self.js[self.js.index("function bindCapabilityCardControls() {"):]
        bf = bf[:bf.index("\n}")]
        for bid in ("btnCapPair", "btnCapPairLocal", "btnCapUnpair", "btnCapRouteProbe"):
            with self.subTest(button=bid):
                self.assertIn(bid, bf, "克隆之后这个按钮就没人接了")

    def test_the_card_lives_inside_the_settings_tab(self):
        """**不新开页签**，长在承载能力配置的那个顶层页签里。

        用户 2026-09-19 定的规矩："每个能力只在这一处出现" —— 当时的模型页签 / 组件页签 /
        设置里的 provider 卡片三处都在讲同一件事，因此被合并。这条断言守的是同一个意图，
        只是承载它的页签改名过两次：`view-capabilities` → `view-capability`（2026-09-25）。

        2026-10-02：IA 重构，「能力与智能体」并进顶层「设置」页，旧断言找
        `id="view-capability"` → 新断言找 `id="view-settings"`（GPU 后端卡仍在那一页里）。
        理由：`#view-capability` 已随合并删除，硬编码旧 id 会让用例直接抛 ValueError ——
        而它要守的意图（这张卡有且只有一个页签容器）没变。
        """
        start = self.html.index('id="view-settings"')
        end = self.html.index("</section>", start)
        self.assertIn('id="capRouteCard"', self.html[start:end],
                      "GPU 后端卡跑到「设置」页签外面去了")
        # 它**只在**这一页（并页签时最容易出的错是复制一份到新页签）
        self.assertEqual(self.html.count('id="capRouteCard"'), 1,
                         "GPU 后端卡只能有一处")

    def test_load_capabilities_actually_loads_the_card(self):
        """`loadCapabilities()` 必须真的去取这块的数据 —— 否则卡片永远是"读取中…"。"""
        body = self.js[self.js.index("async function loadCapabilities()"):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("loadCapabilityRouting()", body)

    def test_the_pair_payload_matches_the_api_model(self):
        """面板发出去的字段名要与 `PairBackendIn` **完全一致**。

        这是跨语言（JS ↔ pydantic）最容易悄悄断的一处：字段名写错时 pydantic 用默认值
        兜住，于是"配对"按钮点下去毫无反应（地址和码都成了空）。

        `client_name` **面板不发**，是故意的：浏览器读不到这台机器的计算机名，
        那本来就是服务端的事（`capability_admin.pair()` 用 `socket.gethostname()` 补），
        而且服务端只在配对码上没写名字时才用它。
        """
        from app.api import PairBackendIn
        body = self.js[self.js.index("async function doCapabilityPair()"):]
        body = body[:body.index("\n}\n") + 3]
        m = re.search(r"JSON\.stringify\(\{([^}]*)\}\)", body)
        self.assertIsNotNone(m, "没找到配对请求的载荷 —— 是不是改写法了？")
        sent = set(re.findall(r"(\w+)\s*:", m.group(1)))
        model = set(PairBackendIn.model_fields.keys())
        self.assertEqual(sorted(sent - model), [],
                         "面板送了模型没有的字段（拼错了？）：%s" % sorted(sent - model))
        for must in ("base_url", "code", "fingerprint"):
            self.assertIn(must, sent, "配对必须送 %s" % must)

    def test_the_pairing_string_is_parsed_on_the_panel_side(self):
        """管理员的**一整串** `echo://pair?host=…&code=…&fp=…` 要能直接粘进来。

        服务端 `--new-pairing-code` 打的就是这一串（含证书指纹，设计 §7.5 ①）。
        如果面板不认它，用户就得手工把 host/code/fp 三段拆开抄 —— 那正是
        `fp=` 会被抄错、抄漏的地方，而它恰好是防中间人的那一步。
        """
        self.assertIn("function parsePairString(", self.js, "没有配对串解析器")
        body = self.js[self.js.index("function parsePairString("):]
        body = body[:body.index("\n}\n") + 3]
        # 正则里的 `echo://pair` 是转义写法（`/^echo:\/\/pair\b/`），所以按源码里的样子找
        for token in ("echo:\\/\\/pair", "host", "code", "fp"):
            self.assertIn(token, body, "解析器不认 %s" % token)
        # 配对那条路要真的用它（写了不用等于没写）
        pair = self.js[self.js.index("async function doCapabilityPair()"):]
        pair = pair[:pair.index("\n}\n") + 3]
        self.assertIn("parsePairString(", pair)

    def test_the_pair_box_tells_people_they_can_paste_the_whole_string(self):
        """界面文案要说到"粘整串" —— 否则用户只会去抄那串里的数字。"""
        self.assertIn("echo://pair", self.html)
        self.assertIn("配对串", self.html)

    def test_pairing_failures_show_the_servers_own_sentence(self):
        """400 的 body 是 `{"detail": "配对码无效"}` —— 面板要显示那句话，而不是 `HTTP 400: {...}`。

        （这条是**弱**断言：真正的强约束在服务端那侧 —— `test_a_rejected_code_comes_back_as_one_readable_line`
        钉住"回的是 400 + detail"。这里只保证面板没有把那句话丢掉。）
        """
        body = self.js[self.js.index("async function doCapabilityPair()"):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("detail", body)

    def test_the_privacy_none_badge_explains_the_loopback_exception(self):
        """「不出机」许可下的口径必须写在**用户看得到的地方**（2026-09-29 用户拍板 A）。

        判据是地址：只绑回环的本机后端算「本机」，所以「不出机」放行它；而**地址分不出**
        "本机服务"与"本机隧道"—— 用 `ssh -L` 把远端后端映射到本机回环的人会被判成没出机。
        这两句只写在代码注释里等于没有：面板上要能看见，否则用户会得出
        "「不出机」就是用不了后端"这个错结论，去把许可放宽到内网。
        """
        self.assertIn("只绑回环的本机后端", self.js)
        self.assertIn("隧道", self.js)
        self.assertIn("ssh -L", self.js)

    def test_the_unpair_button_is_hidden_until_there_is_something_to_unpair(self):
        """没配对时不该有一个"解除配对"按钮杵在那儿。"""
        self.assertIn('class="btn hidden" id="btnCapUnpair"', self.html)

    def test_pair_and_unpair_share_one_slot(self):
        """**配对 / 解除配对 / 保存 在同一行**（2026-10-13 用户口径）。

        三行结构：① 配对串；② 地址（单独一行 —— 与配对码挤一行时窄边条里会折成三行）；
        ③ 配对码 + 「配对 / 解除配对」+「保存」（保存 = 只改地址，保留凭据）。
        JS 按"配没配上"二选一显示配对与解除配对（原来两个同时可见，看着像两个功能）。
        """
        box = self.html[self.html.index('id="capPairBox"'):]
        rows = box.split('class="cap-pair-row"')[1:]
        self.assertGreaterEqual(len(rows), 3, "配对框三行：配对串 / 地址 / 配对码+按钮")
        self.assertIn("capPairUrl", rows[0], "配对串要**单独一行**")
        self.assertNotIn("capPairCode", rows[0], "配对码不该和配对串挤在一起")
        self.assertIn("capPairHost", rows[1], "地址单独一行")
        self.assertIn("capPairCode", rows[2], "配对码与按钮一行")
        self.assertIn("btnCapPair", rows[2])
        self.assertIn("btnCapUnpair", rows[2])
        self.assertIn("btnCapSetAddr", rows[2], "「保存」（只改地址）要和它们同一行")
        self.assertIn(">保存</button>", rows[2], "按钮文案是「保存」")
        body = self.js[self.js.index("function applyTranscribeSettingsVisibility("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn('bPair.classList.toggle("hidden", paired)', body)
        self.assertIn('bUnpair.classList.toggle("hidden", !paired)', body)

    def test_the_paired_address_is_shown_in_the_address_fields(self):
        """配对完成后：地址行回填后端地址，`后端地址`那栏展示配对到的网址:端口（只展示）。"""
        body = self.js[self.js.index("function applyTranscribeSettingsVisibility("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("capPairHost", body, "地址行要回填")
        self.assertIn("pairBase", body)
        # 2026-10-13 用户："留上面的就行" —— `后端地址`（capabilityEchoServerUrl）那栏去掉，
        # 换地址改由配对行里的「改地址」按钮承担（保留凭据，不重新配对）。
        self.assertNotIn("set-capabilityEchoServerUrl", body)
        self.assertIn("btnCapSetAddr", body, "配对后要出现「改地址」按钮")

    def test_the_admin_link_only_shows_for_a_local_backend(self):
        """「打开后端管理面」只在**本地后端**（回环）时展示（2026-10-13 用户口径）。"""
        row = self.js[self.js.index("function capBackendAdminRow("):]
        row = row[:row.index("\n}\n") + 3]
        self.assertIn("if (!loop) return", row, "远端后端不许摆一个点不开的入口")

    def test_the_two_mandatory_backend_rows_are_no_longer_config_items(self):
        """「分离与声纹由谁做」是必备内容，不再是配置项 —— 转写卡里那份去掉。

        它的家仍在「业务配置 → 会议」卡（`tests/test_ia_panel.py` 钉着"会议卡一处"），
        所以这里判**只剩一处**。
        """
        self.assertEqual(self.js.count('sSub("分离与声纹由谁做"'), 0,
                         "两侧都不该再有它（会议标配，不是配置项）")
        # 2026-10-13：不再多套一层「后端设置」—— 静态卡直接落进转写服务下的占位，
        # 卡自己的标题就叫「后端设置」。
        self.assertNotIn('sSub("后端设置"', self.js)
        self.assertIn('<div id="capBackendHost"></div>', self.js)


class BackendOneClickWiringTests(unittest.TestCase):
    """「起本机后端」那张小卡的接线（2026-09-30，实施方案 §5 的批 1d）。

    这张卡是**唯一**能让"本机自建后端"这件事在界面上完成的地方（没有它，用户要自己写
    `server.yaml`、自己起进程、自己找配对文件）。它的错法与其它卡片一样是**安静的**：
    id 拼错、端点名写错、忘了在页签加载时拉一次状态、看不到进度 —— 在浏览器里全表现为
    "点了没反应"或"永远停在准备中"。这些都看得见，所以都用静态断言钉住。

    （浏览器端的真实验证见 `docs/面板布局-窄边条.md` 的无头 Edge 配方；门禁不引浏览器。）
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("app.js")
        cls.html = _read("index.html")
        cls.html_ids = set(re.findall(r'id="([^"]+)"', cls.html))
        cls.js_ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', cls.js))

    def _fn(self, name):
        body = self.js[self.js.index("function %s(" % name):]
        return body[:body.index("\n}\n") + 3]

    def test_the_card_has_every_element_it_reaches_for(self):
        for el in ("btnCapBackendStart", "btnCapBackendStop", "btnCapBackendReady",
                   "btnCapBackendCompose", "capBackendState", "capBackendJob",
                   "capBackendPlan", "capBackendNotes"):
            with self.subTest(id=el):
                self.assertIn('id="%s"' % el, self.html, "HTML 里没有这个元素")
                self.assertIn(el, self.js_ids, "HTML 里有，但 JS 从来没引用它")

    def test_the_panel_calls_the_three_endpoints(self):
        """端点分别是"读状态 / 起 / 停 / 就绪自测" —— 少一个这张卡就残了。"""
        for ep in ("/api/capability/backend",
                   "/api/capability/backend/start",
                   "/api/capability/backend/stop",
                   "/api/capability/backend/ready",
                   "/api/capability/backend/compose"):
            with self.subTest(endpoint=ep):
                self.assertIn(ep, self.js, "面板没调 %s" % ep)

    def test_the_compose_button_is_only_shown_on_the_container_path(self):
        """容器路才给「生成 compose」（批 4）—— 没 Docker 的机器上它没有意义。"""
        self.assertIn('class="btn hidden" id="btnCapBackendCompose"', self.html)
        render = self._fn("renderBackendOneClick")
        self.assertIn("_capBackendPath", render)
        self.assertIn("btnCapBackendCompose", render)
        self.assertIn("_capBackendPath = String((p || {}).path", self._fn("renderBackendPlan"))

    def test_the_ready_result_is_reported_by_a_toast(self):
        """三层就绪（批 3）：仍是**服务端跑的那一次真推理**，但结果不再平铺一行
        （2026-10-13 第二轮：卡片里只留配置项）—— 改走 toast，也不许面板自己再跑一遍。"""
        self.assertNotIn("renderBackendReady", self.js)
        body = self._fn("doCapabilityBackendReady")
        self.assertIn("toast(", body)
        self.assertIn("/api/capability/backend/ready", body)
    def test_the_plan_is_still_fetched_but_renders_nothing(self):
        """批 2 的只读计划仍在拉（按钮显隐靠它），但**卡片里不再渲染计划文字**
        —— 2026-10-13 第二轮用户口径："仅保留配置项，说明性文字都不需要展示"。
        "起不了为什么"由按钮 `title` 负责（见 `BackendCardHasNoFlatProseTests`）。"""
        self.assertIn("/api/capability/backend/plan", self.js)
        self.assertIn('id="capBackendPlan"', self.html)
        body = self.js[self.js.index("async function loadCapabilityRouting("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("loadBackendPlan()", body)
        render = self._fn("renderBackendPlan")
        self.assertIn("_capBackendPath", render, "按钮显隐还要看计划")
        for gone in ("missing", "notes", "verify", "reasons", "capDetails("):
            self.assertNotIn(gone, render, "计划还在渲染 %r" % gone)
    def test_the_status_is_loaded_with_the_capability_tab(self):
        """`loadCapabilityRouting()` 里要真的去拉这张卡的状态 —— 否则它永远是空的。"""
        body = self.js[self.js.index("async function loadCapabilityRouting("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("loadBackendOneClick()", body)

    def test_the_progress_is_not_rendered_and_the_reason_hangs_on_the_button(self):
        """进度**不再平铺**（2026-10-13 第二轮口径），但"为什么起不了"要够得着。

        判据三条：
          * 逐条进度与折叠块一起下线（`capBackendJobLines` / `capDetails("job"` 都不在）；
          * 服务端的 `job` 仍被读 —— 用它决定按钮禁用与**完成/失败的 toast**；
          * 真起不来时把 `be.notes`（"为什么"）挂到**按钮的 `title`** 上（悬浮可见）。
        """
        self.assertNotIn("capBackendJobLines", self.js)
        render = self._fn("renderBackendOneClick")
        for token in ("job.running", "job.doneAt", "job.message"):
            self.assertIn(token, render, "渲染没读服务端的 %s" % token)
        self.assertIn("be.notes", render)
        self.assertIn("btn.title", render, "起不了的原因要挂在按钮的 title 上")
        self.assertNotIn("capDetails(", render)
        self.assertIn('jobHost.innerHTML = ""', render, "进度容器要清空")

    def test_the_stop_button_starts_hidden(self):
        """没在跑的时候不该有一个"停掉它"杵在那儿（与服务端 `running` 一起决定显隐）。"""
        self.assertIn('class="btn hidden" id="btnCapBackendStop"', self.html)
        self.assertIn('classList.toggle("hidden"', self._fn("renderBackendOneClick"))

    def test_a_running_job_starts_the_poller(self):
        """起后端要等最长 60 秒（等本机配对文件）—— 没有轮询就等于没有进度。"""
        self.assertIn("startBackendPoll()", self._fn("renderBackendOneClick"))
        self.assertIn("stopBackendPoll()", self._fn("renderBackendOneClick"))
        self.assertIn("setInterval", self._fn("startBackendPoll"))

    def test_the_start_payload_matches_the_api_model(self):
        """面板发的字段名要与 `BackendStartIn` 一致（跨语言最容易悄悄断的一处）。

        面板现在发的是空对象（全用服务端默认值），所以这里钉两件事：
        ① 请求体是 JSON；② 模型里那三个键就是服务端认的名字（改名字要同时改两处）。
        """
        from app.api import BackendStartIn
        model = set(BackendStartIn.model_fields.keys())
        self.assertEqual(model, {"replace_pairing", "vram_budget_mb", "device"}, model)
        body = self._fn("doCapabilityBackendStart")
        self.assertIn('body: "{}"', body)
        self.assertIn("/api/capability/backend/start", body)

    def test_the_stop_action_asks_before_interrupting_a_meeting(self):
        """停后端会打断正在转写/分离的会议（不可逆）—— 必须问一句，并说清只停"我们起的"。"""
        body = self._fn("doCapabilityBackendStop")
        self.assertIn("confirm(", body)
        self.assertIn("手工启动", body)

    def test_the_failure_line_shows_the_servers_own_sentence(self):
        """400 的 body 是 `{"detail": "一句人话"}` —— 面板要显示那句话。"""
        self.assertIn("detail", self._fn("capBackendErrorText"))


class MeetingDetailShowsThePlanTests(unittest.TestCase):
    """会议详情页那块"这场会用了哪个后端"（3.0）。

    数据链路：`meta.json` → `meeting.meeting_meta()` → `api.get_meeting` 的 `capability`
    字段（服务端用 `capability_admin.plan_summary` 翻好中文）→ 面板渲染。
    最容易断在两头：接口忘了带、面板忘了渲染 —— 两者在界面上都表现为"什么都没有"，
    而人看不出是断了还是"这场会本来就没记"。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = _read("meeting.html")

    def _fn(self, name):
        body = self.html[self.html.index("function %s(" % name):]
        return body[:body.index("\n}\n") + 3]

    def test_the_block_exists_and_is_hidden_by_default(self):
        self.assertIn('id="capBox"', self.html)
        self.assertIn('id="capBox" class="hidden"', self.html)

    def test_load_actually_renders_it(self):
        body = self.html[self.html.index("async function load()"):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("renderCapability()", body,
                      "详情加载完了却没渲染那块 —— 界面永远是空的")

    def test_it_reads_the_servers_translation_instead_of_its_own(self):
        """**前端不许自己维护降级原因词汇表。**

        原因的中文在 `capability_admin.REASON_LABELS`（Python 侧，与权威词汇同源）。
        在 JS 里再写一份，迟早出现"后端说 vector-mismatch、面板显示成别的意思"。
        所以钉住：渲染只用服务端给的 `*Label`，不自己映射。
        """
        body = self._fn("renderCapability")
        for field in ("reasonLabel", "slotLabel", "backendLabel"):
            self.assertIn(field, body, "没用到服务端给的中文：%s" % field)
        self.assertNotIn("REASON_LABELS", self.html)

    def test_it_does_not_recompute_the_plan(self):
        """只用 `M.capability`（录音时的快照），不在前端重算"会选谁"。"""
        body = self._fn("renderCapability")
        self.assertIn("M.capability", body)
        self.assertNotIn("/api/capability", body)

    def test_the_rows_can_shrink(self):
        """窄边条里这块也要能收缩（长文案不许把布局顶宽，见 docs/面板布局-窄边条.md）。"""
        flat = self.html.replace(" ", "")            # CSS 里的空格不影响判定
        for sel in (".cap-box", ".cap-box.cap-line", ".cap-box.cap-more",
                    ".cap-box.cap-r"):
            with self.subTest(sel=sel):
                self.assertIn(sel + "{", flat, "CSS 里没有 %s 的规则" % sel)
                i = flat.index(sel + "{")
                self.assertIn("min-width:0", flat[i:i + 400],
                              "%s 缺 min-width:0（窄边条里会顶宽）" % sel)


class PanelStateDeclarationTests(unittest.TestCase):
    """面板状态变量（`_xxx`）**必须先声明再赋值** —— `app.js` 第 2 行是 `"use strict"`。

    为什么值得当门禁钉（2026-10-01 的 bug）：`_capBackendCache` 从没被声明过，于是
    `loadBackendOneClick()` 在**赋值那一行**抛 `ReferenceError`、`renderBackendPlan()`
    在**读它那一行**也抛；两处都在 `try` 里，浏览器上只剩「读不到本机后端的状态」与
    「读不到这台机器的后端计划」两句话 —— **功能没坏、显示坏了**，而且只有真点开那一页
    才看得见（正是本文件头说的那一类"安静的坏"）。

    非严格模式下面这种写法会**悄悄造一个全局**、什么错都不报；`"use strict"` 把它变成
    当场抛错 —— 所以这条闸门只在 `"use strict"` 的前提下才有意义，第 2 行那句话也一并钉住。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("app.js")

    def test_the_file_is_strict_so_an_undeclared_assignment_throws(self):
        head = "\n".join(self.js.splitlines()[:5])
        self.assertIn('"use strict"', head,
                      'app.js 开头必须是 "use strict"：否则下面那条检查形同虚设')

    def test_every_underscore_state_name_is_declared(self):
        assigned = {}
        for i, line in enumerate(self.js.splitlines(), 1):
            m = re.match(r"\s*(_[A-Za-z0-9_$]+)\s*=(?!=)", line)
            if m:
                assigned.setdefault(m.group(1), []).append(i)
        declared = set()
        for line in self.js.splitlines():
            m = re.match(r"\s*(?:let|const|var)\s+(.*)", line)
            if m:
                declared.update(re.findall(r"(_[A-Za-z0-9_$]*)", m.group(1)))
        # 非空校验：正则被改到抓不到东西时，这条断言会失败而不是"永远绿"
        self.assertGreater(len(assigned), 30,
                           "一条 `_x = …` 赋值都没抓到？正则或写法变了：%s" % sorted(assigned))
        undeclared = {k: v for k, v in assigned.items() if k not in declared}
        self.assertEqual(undeclared, {},
                         "这些面板状态名字**没声明**就被赋值了（严格模式下会当场抛 "
                         "ReferenceError，异常多半被 try 吞成一句「读不到…」）：%s"
                         % undeclared)


class BackendCardStaysTerseTests(unittest.TestCase):
    """「AI 组件 → 转写服务 → 后端设置」不摊长解释（2026-10-13 用户口径）。

    用户原话："后端设置的内容太臃肿了，不用那么多解释，必要的说明也要用问号标记悬浮展示"。
    所以这一块钉三件事：

      1. 说明走**问号浮窗**（`.sq` + `.spop`）—— 仓库现成的样式与全局点击委托，
         悬浮或点一下都出得来，不新增机制；
      2. 卡片里的**裸文字都是短的**（控件名 / 状态 / 占位符），长句必须在 `.spop` 里；
      3. `#capBackendModeRow` 等控件**不许被 `.muted` 包住** —— 2026-10-13 顺手挖出来的事故：
         那块说明的 `<div class="muted" style="font-size:12px">` **从未闭合**，把整个后端设置
         （单选、配对框、启停按钮、plan/notes）全吞进了 12px 灰字里，句子还断在半截
         （"…交给一台带显卡的"）。这条按**结构**判，不靠数 `</div>`。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls.tree = _HtmlTree()
        cls.tree.feed(cls.html)
        cls.tree.close()

    def _region(self):
        return self.html[self.html.index('id="capRouteCard"'):self.html.index('id="capLocalCard"')]

    def test_the_explanations_live_in_question_mark_popovers(self):
        region = self._region()
        self.assertGreaterEqual(region.count('class="spop"'), 2,
                                "这块的说明要收进 `?` 浮窗（用户 2026-10-13 的口径）")
        self.assertIn('class="sq"', region, "浮窗要有问号触发器")
        # 不再平铺：这两句原来就是裸着的长说明
        self.assertNotIn("整串粘在下面第一个框里即可", region, "这句该进浮窗了")
        self.assertIn("整串粘在第一个框里即可", region, "浮窗里要留一句能看懂的（配对串怎么用）")

    def test_no_long_bare_prose_is_left_in_the_card(self):
        region = self._region()
        ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', region))
        self.assertTrue(ids, "卡片区没解析出 id，锚点变了？")
        offenders = []
        for chain, text in self.tree.texts:
            if not any(i in ids for _t, _c, i in chain):
                continue
            if _inside(chain, "spop"):
                continue
            if len(text) > 40:
                offenders.append(text[:60])
        self.assertEqual(offenders, [],
                         "卡片里还有裸着的长说明（要收进 `?` 浮窗）：%s" % offenders)

    def test_the_controls_are_not_buried_in_a_muted_wrapper(self):
        for el in ("capBackendModeRow", "capPairSettings", "capLocalSettings"):
            chain = self.tree.by_id.get(el)
            self.assertIsNotNone(chain, "index.html 里没有 #%s" % el)
            self.assertFalse(_inside(chain, "muted"),
                             "#%s 被 .muted 包住了 —— 整块会变成小号灰字（2026-10-13 的事故）" % el)

    def test_pair_and_local_blocks_are_siblings(self):
        """两块必须是**兄弟**（同一个父节点）。

        历史事故（2026-09-25，`tests/test_js_syntax.py` 也记着）：`#capLocalSettings` 被错手
        塞进了「解除配对」按钮里、还套在 `#capPairSettings` 内 —— 于是"本机"那一组在
        两种状态下都显示不出来，而代码看着"没问题"。
        """
        a = self.tree.by_id["capPairSettings"]
        b = self.tree.by_id["capLocalSettings"]
        self.assertEqual(a[:-1], b[:-1],
                         "这两块不是兄弟 —— 显示条件会互相打架：\n  %s\n  %s"
                         % (_chain(a[:-1]), _chain(b[:-1])))


class BackendCardHasNoFlatProseTests(unittest.TestCase):
    """「后端设置」卡片**只留配置项**，说明性文字一律不平铺（2026-10-13 第二轮用户口径）。

    用户原话："后端设置卡片还需要进一步简化，仅保留配置项就行，说明性文字都不需要展示"。
    上一轮已经把长解释收进 `?` 浮窗、把过程收进折叠块；这一轮**连折叠块也不要了** ——
    卡片里只留控件（模式单选 / 配对输入 / 配对·检测·启停·compose·自测 / 管理面入口）
    与状态徽标。信息去哪儿了：控件的 `title`（悬浮）、`?` 浮窗、以及 toast（结果/失败）。

    这里钉四条：
      1. 状态行 / 进度 / 步骤 / 说明 / 计划**都不再渲染**（容器保留但清空）；
      2. "为什么起不了"进按钮 `title`（否则用户点完只有一个禁用按钮，什么都不知道）；
      3. 计划只用来决定按钮显隐（容器路才出「生成 compose」），不渲染任何文字；
      4. 折叠块机制（`capDetails` / `bindCapDetails`）随之下线 —— 没有内容可折了。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = _read("app.js")
        cls.html = _read("index.html")

    def _fn(self, name):
        body = self.js[self.js.index("function %s(" % name):]
        return body[:body.index("\n}\n") + 3]

    def test_the_prose_containers_are_cleared_not_filled(self):
        body = self._fn("renderBackendOneClick")
        self.assertIn('state.textContent = ""', body, "状态行要清空")
        self.assertIn('jobHost.innerHTML = ""', body, "进度要清空")
        self.assertIn('notesHost.innerHTML = ""', body, "说明要清空")
        for gone in ("capDetails(", "job.stage", "✓ 完成", "端口 ${ports}"):
            self.assertNotIn(gone, body, "卡片里还在平铺 %r" % gone)

    def test_the_blocked_reason_hangs_on_the_button(self):
        body = self._fn("renderBackendOneClick")
        self.assertIn("be.whyNot", body)
        self.assertIn("notes.join", body)
        self.assertIn("btn.title", body)

    def test_the_plan_only_drives_button_visibility(self):
        body = self._fn("renderBackendPlan")
        self.assertIn("_capBackendPath", body, "按钮显隐还要跟着计划走")
        self.assertIn('host.innerHTML = ""', body, "计划那行文字不再渲染")
        for gone in ("capDetails(", "计划：", "etaMinutes"):
            self.assertNotIn(gone, body, "计划还在渲染 %r" % gone)

    def test_the_ready_result_is_a_toast_not_a_line(self):
        self.assertNotIn("renderBackendReady", self.js, "就绪结论那一行已下线（改走 toast）")
        body = self._fn("doCapabilityBackendReady")
        self.assertIn("toast(", body, "自测结果必须仍然报出来（只是改走 toast）")

    def test_the_collapsed_block_mechanism_is_gone(self):
        for fn in ("capDetails", "bindCapDetails"):
            self.assertNotIn("function %s(" % fn, self.js, "%s 已随折叠块下线" % fn)
        self.assertNotIn("_capOpenBlocks", self.js)

    def test_the_mode_row_no_longer_writes_a_state_sentence(self):
        self.assertNotIn("capBackendModeState", self.js)
        self.assertNotIn("capBackendModeState", self.html,
                         "那个状态 span 已从静态卡里删掉")


if __name__ == "__main__":
    unittest.main()
