# -*- coding: utf-8 -*-
"""tests/test_card_links.py — 仪表盘「后端 / Agent」两张小卡的 ↗ 网页入口（2026-10-05 用户要求）。

用户原话："仪表盘的后端和agent两个小卡片上增加个网页入口，点击后跳到对应的网页"。

三条要点，各自都有"为什么"：
  1. **后端**卡的地址由**后端自己**给（`backend_admin.view()` 的 `adminUrl`，端口取"一处权威"
     的 `configured_ports()`）—— 前端不许自己猜端口（那条规矩见 `configured_ports()` 的说明）。
  2. **Agent**卡在"标准版 harness 在跑"时走**服务端**端点 `POST /api/control/harness/browser`：
     那条 URL 带 `?token=…`，按 `app/api.py` 里写明的原因（token 是密钥，会进浏览器历史与前端
     内存）**不许**下发给前端。其余情况（DSH Desktop 是 cookie 鉴权的同端口 GUI、或没在跑）
     跳面板自己的「智能体」页 —— 永远不落到死链。
  3. `_runCard` 的 `go` 参数是**可选**的：没给就不渲染 ↗（语音合成那张卡就不该多一个）。
"""
import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import backend_admin   # noqa: E402


def _read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class BackendAdminUrlTests(unittest.TestCase):

    def test_view_hands_the_panel_the_admin_console_url(self):
        """管理页地址由后端给，且**只能**是回环 + `/admin/`（管理面只绑回环是设计）。"""
        view = backend_admin.view()
        self.assertIn("adminUrl", view, "面板那张卡要靠它拿地址")
        self.assertRegex(view["adminUrl"], r"^http://127\.0\.0\.1:\d+/admin/$")
        self.assertIn(str(view["adminPort"]), view["adminUrl"],
                      "端口必须来自同一处权威（configured_ports），不许前端另猜")


class PanelWiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.css = _read("web", "app.css")

    def test_card_renders_the_link_only_when_asked(self):
        fn = self.js[self.js.index("function _runCard("):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("if (go && (go.href || go.post))", fn, "没给 go 就不该多一个 ↗")
        self.assertIn('class="sb-go"', fn)
        self.assertRegex(fn, r"data-go-href=", "href 与 post 分开存，点击时再决定怎么开")
        self.assertRegex(fn, r"data-go-post=")

    def test_both_cards_get_a_link_and_tts_does_not(self):
        # 一次调用可能跨两行 → 用 `(?s)` 让 `.` 也匹配换行
        self.assertRegex(self.js, r'(?s)_runCard\("backend".*?be\.adminUrl',
                         "后端卡要带上管理页地址")
        self.assertRegex(self.js, r'(?s)_runCard\("agent".*?_agentLink\(ag\)',
                         "Agent 卡的入口由 _agentLink 决定")
        # 语音合成那张卡**不**带 go（用户只要求两张）
        m = re.search(r'_runCard\("tts"[^\n]*\)', self.js)
        self.assertTrue(m, "找不到 tts 卡片调用")
        self.assertNotIn("_agentLink", m.group(0))
        self.assertNotIn("adminUrl", m.group(0))

    def test_agent_link_keeps_the_token_server_side(self):
        fn = self.js[self.js.index("function _agentLink("):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn('name === "harness"', fn)
        self.assertIn('"/api/control/harness/browser"', fn,
                      "harness 必须走服务端端点（token 不下发）")
        self.assertIn('"?view=agent"', fn, "其余情况要有兜底，不能是死链")
        self.assertNotIn("token=", fn, "**不许**在前端拼带 token 的 URL")

    def test_clicks_are_bound_once_by_delegation(self):
        self.assertIn("host.dataset.goBound", self.js, "只绑一次")
        self.assertIn('node.closest(".sb-go")', self.js, "事件委托")
        self.assertRegex(self.js, r"window\.open\(href, \"_blank\"")

    def test_the_arrow_has_a_style(self):
        self.assertIn(".sb-go", self.css)


class ServerRouteTests(unittest.TestCase):

    def test_the_harness_browser_route_still_exists(self):
        """Agent 卡的 harness 分支靠它；不在了就该换入口，而不是静默失效。"""
        self.assertIn('@router.post("/harness/browser")', _read("app", "api.py"))


if __name__ == "__main__":
    unittest.main()
