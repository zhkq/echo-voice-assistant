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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(_ROOT, "web", name), encoding="utf-8") as fh:
        return fh.read()

#: 这张卡里**要被人点的**元素（必须真接上事件，否则点了没反应）。
_WIRED_IDS = ("btnCapPair", "btnCapUnpair", "btnCapRouteSave", "btnCapRouteProbe",
              "capPairUrl", "capPairCode", "capPairState", "capBackendList",
              "capRouteSettings", "capRouteBadge")


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
        missing = sorted(card_ids - self.html_ids)
        self.assertEqual(missing, [], "JS 引用了 HTML 里不存在的 id：%s" % missing)

    def test_the_things_people_click_are_actually_wired(self):
        """反过来：卡里要被人点的元素，JS 必须真的引用它。

        （HTML 里留一个没人用的 id 是无害的容器锚点；但一个**该接事件却没接**的按钮
        在界面上看不出区别 —— 点了没反应才是最难查的那种。）
        """
        for el in _WIRED_IDS:
            with self.subTest(id=el):
                self.assertIn('id="%s"' % el, self.html, "HTML 里没有这个元素")
                self.assertIn(el, self.js_ids, "HTML 里有，但 JS 从来没引用它（点了没反应）")

    def test_the_card_lives_inside_the_capabilities_tab(self):
        """**不新开页签**，长在「能力」页签里。

        用户 2026-09-19 定的规矩："每个能力只在这一处出现" —— 当时的模型页签 / 组件页签 /
        设置里的 provider 卡片三处都在讲同一件事，因此被合并。再开一个「能力路由」页签
        就是走回合并之前。
        """
        start = self.html.index('id="view-capabilities"')
        end = self.html.index("</section>", start)
        self.assertIn('id="capRouteCard"', self.html[start:end],
                      "GPU 后端卡跑到「能力」页签外面去了")

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

    def test_the_unpair_button_is_hidden_until_there_is_something_to_unpair(self):
        """没配对时不该有一个"解除配对"按钮杵在那儿。"""
        self.assertIn('class="btn hidden" id="btnCapUnpair"', self.html)


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


if __name__ == "__main__":
    unittest.main()
