# -*- coding: utf-8 -*-
"""向导前端接线的守卫测试（纯字符串断言，不需要浏览器）。

这个仓库对前端就是这么测的（见 ``tests/test_settings_wiring.py``）：面板没有端到端测试，
但"页签清单 ↔ html 容器 ↔ 分发函数 ↔ 接口依赖"这几处一致性可以钉住 ——
2026-09-20 我自己就在这儿翻过车：改 ``_VIEWS`` 时把那行 **整行替换掉**，
页签与容器都对，唯独"页签清单"没了，而 `node --check` 是查不出这种错的。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")
INDEX = os.path.join(ROOT, "web", "index.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class WizardUiWiringTests(unittest.TestCase):

    def setUp(self):
        self.js = _read(APP_JS)
        self.html = _read(INDEX)

    def test_tab_and_view_container_exist(self):
        self.assertIn('data-view="wizard"', self.html, "页签按钮必须存在，否则进不去向导")
        self.assertIn('id="view-wizard"', self.html, "视图容器必须存在，否则 switchView 会取到 null")
        self.assertIn('id="wizHost"', self.html, "向导的渲染宿主必须存在")

    def test_declared_views_all_have_a_container(self):
        """``_VIEWS`` 里的每个页签都要有对应的 ``#view-…`` 容器。

        这条同时钉住两件事：① 新页签不能只加一半；② ``_VIEWS`` 那一行本身还在
        （2026-09-20：它被整行替换掉过，页签点进去什么都不发生）。
        """
        m = re.search(r"const _VIEWS = \[(.*?)\]", self.js, re.S)
        self.assertIsNotNone(m, "找不到 _VIEWS 声明 —— 它必须存在且只有一处")
        views = re.findall(r'"([a-z]+)"', m.group(1))
        self.assertIn("wizard", views, "_VIEWS 里要登记 wizard")
        for name in views:
            self.assertIn('id="view-%s"' % name, self.html,
                          "页签 %s 没有对应的 #view-%s 容器" % (name, name))

    def test_switch_view_dispatches_to_the_wizard_loader(self):
        """切到向导页签必须真的加载向导。

        2026-09-25：`switchView()` 里那句从 `if (name === …)` 改成了
        `if (view === …)`（先把老页签名折算成新 id，见 VIEW_ALIASES）——
        断言的意图不变，只跟着变量名走。
        """
        self.assertRegex(self.js, r'if \(view === "wizard"\) loadWizard\(\);')
        self.assertIn("async function loadWizard()", self.js)

    def test_wizard_calls_only_the_wizard_endpoints_and_saves_choices(self):
        for url in ("/api/wizard/env", "/api/wizard/plan", "/api/wizard/preview",
                    "/api/wizard/execute", "/api/wizard/state",
                    "/api/wizard/finalize"):
            self.assertIn(url, self.js, "向导要用 %s" % url)
        # `/api/wizard/first-run` **不在**这个清单里：2026-09-21 起面板不再用它决定页签
        # （那正是"技能装完还被塞进向导"的原因），接口本身留着给别的调用方/老客户端。
        self.assertNotIn("/api/wizard/first-run", self.js,
                         "面板不该再用 first-run 决定进哪个页签")
        # 决策相只写计划（PUT /api/wizard/plan），不直接写设置、不直接触发下载
        self.assertIn('api("/api/wizard/plan", {', self.js)

    def test_s6_leads_with_the_agent_and_hides_direct_llm_as_a_fallback(self):
        """S6 的主线是**智能体**：纪要、归档、语音指令都靠它（用户 2026-09-20 定调），
        直连大模型只做**折叠兜底**，而且必须**显式打开**才生效。

        为什么这条重要：`providerLlm` 是个**全局**开关（别的功能也读），纪要在
        `meeting.direct_llm_decision()` 里又是 agent-first —— 向导不该因为用户随手填了
        地址，就替他选定"没有智能体时用哪个直连 provider"。
        """
        block = self.js[self.js.index("function wizRenderLlm"):]
        block = block[:block.index("\n}\n")]
        # 主线：指向第 7 步准备智能体
        self.assertIn('id="wizGoAgent"', block, "S6 要有「去第 7 步准备智能体」的入口")
        self.assertIn('findIndex((s) => s.id === "agent")', block,
                      "该入口要真的跳到智能体那一步")
        # 兜底：显式开关 + 默认收起 + 写明不推荐
        self.assertIn('data-wizdirect="1"', block, "直连要有一个显式开关")
        self.assertIn("不推荐", block, "直连必须写明不推荐")
        self.assertIn('id="wizDirectBox"', block)
        self.assertIn('${direct ? "" : "hidden"}', block, "兜底区要默认收起")
        self.assertIn("_wizChoices.llm.direct", block, "开关要落到 choices.llm.direct")
        # 三个字段仍在（打开兜底后才填）
        for field in ("wizLlmBase", "wizLlmKey", "wizLlmModel"):
            self.assertIn('id="%s"' % field, block, "兜底区缺少 %s 输入框" % field)
        self.assertIn('type="password"', block, "密钥框必须是密码框")
        self.assertIn("wizSaveChoices", block, "改动要存进计划文件（关掉面板不丢）")
        self.assertNotIn("wizGoLlm", self.js, "不该再有「跳去模型路由」当唯一入口")

    def test_boot_never_forces_the_wizard(self):
        """**首装不再自动进向导**（2026-09-21 改，取代原来的
        `test_first_install_enters_the_wizard_automatically`）。

        为什么改：安装现在由助手按 `echo-install` 技能完成（见 `docs/安装-技能优先.md`），
        而"首装"的判据 `installed-components.json` **只有向导末页才写** —— 于是技能装完，
        用户打开面板还是被塞进向导页（2026-09-21 同事实测反馈）。
        新的契约：默认进仪表盘；"登记了没有 / 还缺什么"由顶部横幅说清；
        向导保留但降级为手动入口（`?view=wizard`），永不自动弹出。
        """
        block = self.js[self.js.index("async function bootView()"):]
        block = block[:block.index("\n}")]
        self.assertNotIn("wizard/first-run", block,
                         "bootView 不该再拿 first-run 当进向导的理由")
        self.assertIn('switchView("dashboard")', block, "默认进仪表盘")
        self.assertIn("renderInstallNotice()", block, "要用安装状态横幅说清下一步")
        self.assertIn("async function renderInstallNotice()", self.js)
        self.assertIn('api("/api/install/state")', self.js, "横幅读的是安装状态接口")
        # 横幅要给出两条明确去处，而不是只报个错
        self.assertIn('switchView("wizard")', self.js, "手动向导仍要可达")
        # 2026-09-25 页签整合：「能力」页签改名「能力后端」（id capability），出口跟着改
        self.assertIn('switchView("capability")', self.js, "补能力要可达")
        # 显式指定页签（?view=… / echo.gotoView）最优先，不许被默认逻辑抢走
        block = self.js[self.js.index("async function bootView()"):]
        block = block[:block.index("applyCollapsedCards()")]
        self.assertIn("if (_bootView) { switchView(_bootView); renderInstallNotice(); return; }",
                      block, "显式指定页签时不许抢")
        # 走完向导末页仍要写 installed-components.json（老的首装判据，install_state 也认它）
        self.assertIn('post("/api/wizard/finalize", {})', self.js,
                      "走到末页要写 installed-components.json")

    def test_every_step_has_a_renderer(self):
        steps = re.findall(r'\{ id: "[a-z]+", name: "[^"]+", render: (\w+) \}', self.js)
        self.assertGreaterEqual(len(steps), 10, "步骤表看起来被截断了")
        for fn in steps:
            self.assertRegex(self.js, r"function %s\(host\)" % fn,
                             "步骤表里的 %s 没有对应实现" % fn)

    def test_copy_is_plain_language_not_jargon(self):
        """面向小白：**用户看得见的文案**里不该出现技术词（设计文档 §0.3 的术语表）。

        只扫"含中文的字符串字面量" —— 代码里的标识符与接口字段名（如 JSON 里的
        ``providers``）不是文案，不该被这条拦住；注释同理。
        """
        start = self.js.index("首装向导")
        # 切片终点必须是**向导代码之后**的锚点：`_VIEWS` 2026-09-25 挪到了文件开头
        # （页签整合时和 switchView 放在一起），所以这里改用底部那段的起点。
        block = self.js[start:self.js.index("async function bootView()")]
        han = "[\u4e00-\u9fff]"
        # 只看**单行双引号字符串**：向导里所有"说给用户听"的话都是这种（what / lose / label /
        # note）。模板串基本是 markup + ${} 代码，扫它容易把 JSON.stringify 这类代码误判成文案。
        texts = re.findall(r'"([^"\n]*%s[^"\n]*)"' % han, block)
        self.assertGreater(len(texts), 30, "没扫到多少中文文案，正则可能写错了")
        for word in ("runtime-core", "modelsDir", "provider", "manifest", "payload",
                     "torch", "pip install", "API", "JSON"):
            for text in texts:
                self.assertNotIn(word, text,
                                 "文案里不该出现技术词「%s」：%s" % (word, text[:60]))
        # 用词（2026-09-20 按用户要求）：说「模型文件」，不再说「能力包」——
        # "能力包"是向导自己造的词，看不出里面是什么；"模型文件"一眼就懂。
        for text in texts:
            self.assertNotIn("能力包", text,
                             "旧用词「能力包」不该再出现，改说「模型文件」：%s" % text[:60])
        self.assertIn("模型文件", block, "三处位置的第一处要叫「模型文件」")
        # 人话标志：体积要带换算，而不是只给 MB 数字
        self.assertIn("首歌", block, "体积要给一个人话换算")

    def test_confirm_page_masks_secret_settings(self):
        """确认页会列出「将写入哪些设置」——密钥必须打码。

        S6 支持就地填地址与密钥之后，密钥第一次走到这个列表上；明文列出来
        （面板可能在共享屏幕/被截图）就是泄漏。
        """
        self.assertIn("function wizMaskSetting(", self.js, "缺打码函数")
        block = self.js[self.js.index("async function wizRenderConfirm"):]
        block = block[:block.index("\n}\n")]
        self.assertIn("wizMaskSetting(c.key, c.value)", block,
                      "确认页的设置列表要走打码，不能直接 esc(c.value)")
        self.assertNotIn("String(c.value)", block, "不允许再明文渲染设置值")

    def test_three_locations_are_all_present(self):
        for key in ("models", "meetings", "notes"):
            self.assertIn('key: "%s"' % key, self.js, "三处位置里的 %s 不能少" % key)


if __name__ == "__main__":
    unittest.main()
