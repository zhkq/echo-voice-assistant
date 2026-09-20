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
        self.assertRegex(self.js, r'if \(name === "wizard"\) loadWizard\(\);')
        self.assertIn("async function loadWizard()", self.js)

    def test_wizard_calls_only_the_wizard_endpoints_and_saves_choices(self):
        for url in ("/api/wizard/env", "/api/wizard/plan", "/api/wizard/preview",
                    "/api/wizard/execute", "/api/wizard/state"):
            self.assertIn(url, self.js, "向导要用 %s" % url)
        # 决策相只写计划（PUT /api/wizard/plan），不直接写设置、不直接触发下载
        self.assertIn('api("/api/wizard/plan", {', self.js)

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
        block = self.js[start:self.js.index("const _VIEWS")]
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
        # 人话标志：体积要带换算，而不是只给 MB 数字
        self.assertIn("首歌", block, "体积要给一个人话换算")

    def test_three_locations_are_all_present(self):
        for key in ("models", "meetings", "notes"):
            self.assertIn('key: "%s"' % key, self.js, "三处位置里的 %s 不能少" % key)


if __name__ == "__main__":
    unittest.main()
