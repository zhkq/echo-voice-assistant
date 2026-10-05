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
#: 2026-09-25 移除了 `btnCapRouteSave`：转写/分离/声纹三项（+ 出网许可、后端地址）已经收进
#: 本页顶部的「会议转写服务」卡（一个单选 + 一个「会议产出」下拉 = 原来那三个键），
#: 那张卡的行随页签顶部的「保存」落库，所以这个"第二套下拉的保存按钮"被删掉了
#: （删的是**元素本身**，不是放宽这条断言的意图：要点的东西仍然必须接线）。
_WIRED_IDS = ("btnCapPair", "btnCapUnpair", "btnCapRouteProbe",
              "capPairUrl", "capPairCode", "capPairState", "capBackendList",
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

    def test_the_backend_status_line_and_admin_link_are_rendered(self):
        """**设置分家**的客户端那一半（2026-09-28，设计 §6.6）。

        客户端只给"状态 + 一个入口"：状态里的每个数字都来自后端 `/v1/health`，
        管理面地址由后端宣告（`adminUrl`）。**客户端不展示也不编辑后端的设置**
        （端口 / 显存预算 / 模型档位 / 配额 / TLS）—— 那些归后端的 `server.yaml` 与管理面，
        在客户端再放一份就是"两处都能改、改完不知道谁生效"。

        远程后端那一条必须如实说清"为什么点不开"：管理面只发布在**后端那台机器**的回环上。
        """
        self.assertIn("capBackendHealthLine", self.js)
        self.assertIn("capBackendAdminRow", self.js)
        self.assertIn("adminUrl", self.js, "深链要读后端宣告的地址，不许猜端口")
        self.assertIn("ssh -L 8901:127.0.0.1:8901", self.js,
                      "远程后端要给出进管理面的办法，而不是留一个点不开的链接")

    def test_the_things_people_click_are_actually_wired(self):
        """反过来：卡里要被人点的元素，JS 必须真的引用它。

        （HTML 里留一个没人用的 id 是无害的容器锚点；但一个**该接事件却没接**的按钮
        在界面上看不出区别 —— 点了没反应才是最难查的那种。）
        """
        for el in _WIRED_IDS:
            with self.subTest(id=el):
                self.assertIn('id="%s"' % el, self.html, "HTML 里没有这个元素")
                self.assertIn(el, self.js_ids, "HTML 里有，但 JS 从来没引用它（点了没反应）")

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
                   "capBackendPlan", "capBackendReady", "capBackendNotes"):
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
        self.assertIn("_capBackendPath = String(p.path", self._fn("renderBackendPlan"))

    def test_the_ready_line_is_rendered_from_the_servers_last_result(self):
        """三层就绪（批 3）：面板显示**服务端保留的上一次结论**，不自己再跑一遍真推理。"""
        render = self._fn("renderBackendReady")
        for token in ("headline", "state", "at"):
            self.assertIn(token, render, "就绪那一行没读服务端的 %s" % token)
        self.assertIn("renderBackendReady(be.ready)", self._fn("renderBackendOneClick"))
        self.assertIn("自测中", self._fn("doCapabilityBackendReady"),
                      "自测是一次真推理，点了要给等待反馈")

    def test_the_panel_shows_the_read_only_plan_before_anything_is_clicked(self):
        """批 2 的只读计划：**点按钮之前**就要看得见走哪条路、缺什么、先验哪一步。

        （"没有 Docker 的机器点按钮得到的是扩展包路计划"这条验收，落在界面上就是这一格。）
        """
        self.assertIn("/api/capability/backend/plan", self.js)
        self.assertIn('id="capBackendPlan"', self.html)
        body = self.js[self.js.index("async function loadCapabilityRouting("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("loadBackendPlan()", body)
        render = self._fn("renderBackendPlan")
        for token in ("missing", "notes", "verify", "reasons"):
            self.assertIn(token, render, "计划渲染没读 %s" % token)

    def test_the_status_is_loaded_with_the_capability_tab(self):
        """`loadCapabilityRouting()` 里要真的去拉这张卡的状态 —— 否则它永远是空的。"""
        body = self.js[self.js.index("async function loadCapabilityRouting("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("loadBackendOneClick()", body)

    def test_the_progress_lines_come_from_the_servers_job(self):
        """进度**逐条来自服务端的 `job`**，面板不自己编"正在启动中…"那种没有信息量的话。

        （"界面说在起、其实早失败了"就是各写一套话术的必然结果。）
        """
        body = self._fn("capBackendJobLines")
        for token in ("job.steps", "stage"):
            self.assertIn(token, body, "进度渲染没读服务端的 %s" % token)
        render = self._fn("renderBackendOneClick")
        for token in ("job.running", "job.doneAt", "job.message"):
            self.assertIn(token, render, "渲染没读服务端的 %s" % token)

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


if __name__ == "__main__":
    unittest.main()
