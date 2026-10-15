# -*- coding: utf-8 -*-
"""面板里的接口链接**必须在路由表里**（2026-10-15 用户报的那个"点了什么也不发生"）。

## 现场

仪表盘 Agent 那格的 ↗，面板拼的是 ``post: "/api/control/harness/browser"`` ——
**这条路由不存在**（真名是 ``/api/harness/browser``），点下去就是 404：
"跳不到 dsh 标准版的网页"。而这道错**没有任何东西会拦**：

  * `tests/test_api_paths.py` 扫的是"路由有没有被挂上、会不会 404"（服务端侧）；
  * 面板那侧只有"文案/结构"的用例，没人拿面板里的地址去对路由表。

所以这条用例就做那一件事：**把面板源码里写死的接口地址，逐个拿去路由表里对账**。
它刻意只查**字面量**（`api("/api/…")` 与 `post: "/api/…"`），
带模板/变量的（`api("/api/meetings/" + id)`）由各自的联调用例管 —— 宁可少查，
也不要写一条"看起来在查、其实全是漏网"的用例（本仓库踩过这种假绿）。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app_js() -> str:
    with open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
        return fh.read()


def _route_paths() -> set:
    """`app/api.py` 里 router 的全部路径（含 `/api` 前缀）。"""
    from app.api import router
    out = set()
    for r in getattr(router, "routes", []):
        p = getattr(r, "path", None)
        if p:
            out.add(p)
    return out


class PanelApiLinksTests(unittest.TestCase):

    def test_every_literal_post_link_exists(self):
        """`{ post: "/api/…" }`（卡片上的 ↗ 就是走它）必须真存在。"""
        js = _app_js()
        refs = set(re.findall(r'post:\s*"(/api/[^"$]*)"\s*[,}]', js))
        self.assertTrue(refs, "一条 post: 链接都没扫到？那这条用例是空转的")
        routes = _route_paths()
        missing = sorted(r for r in refs if r not in routes)
        self.assertEqual(missing, [], "面板上的这些 post: 地址在路由表里不存在：%s" % missing)

    def test_every_literal_api_call_exists(self):
        """`api("/api/…")` 这种**整串写死**的调用也要对得上。

        ⚠️ 正则要求那个字符串是**整个参数**（后面紧跟 `)`）：`api("/api/keys/" + id)`
        这种拼接**不算**（它的前缀 `/api/keys/` 在路由表里当然查不到）——
        第一版就是没写这一条，于是报了三个假阳性（`/api/keys/`、`/api/daily-review/history/`）。
        宁可少查，也不要写一条"看起来在查、其实全是漏网/全是误报"的用例。
        """
        js = _app_js()
        refs = set(re.findall(r'api\(\s*"(/api/[^"$?]*)"\s*\)', js))
        self.assertTrue(refs, "一条整串写死的 api() 调用都没扫到？那这条用例是空转的")
        routes = _route_paths()
        missing = sorted(r for r in refs if r not in routes)
        self.assertEqual(missing, [], "面板调了不存在的接口：%s" % missing)

    def test_the_dashboard_agent_link_points_at_the_harness_browser_route(self):
        """那条具体的事故（2026-10-15）：Agent 那格的 ↗ 要打开 **标准版 harness** 的网页。"""
        js = _app_js()
        block = js[js.index("function _agentLink"):]
        block = block[:block.index("return { href: \"?view=agent\"")]
        self.assertIn('"/api/harness/browser"', block,
                      "Agent 那格的 ↗ 必须打 /api/harness/browser（服务端拼好 Cookie 再开页面）")
        self.assertNotIn("/api/control/harness/browser", js,
                         "这条路由不存在（404），别再写回来")


if __name__ == "__main__":
    unittest.main()
