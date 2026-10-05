# -*- coding: utf-8 -*-
"""tests/test_dashboard_backend_card.py — 「后端」卡要说清**转写到底走哪台**（2026-10-05）。

用户实测（测试笔记本，配对到我这台机器的 GPU 后端）：
    "仪表盘上的后端显示没接上，但是转写成功了"

根因：那张卡原来只看"**本机**那份后端"（`/api/capability/backend` 的 running/ready）。而
配对到远端时，本机那份本来就不该在跑 —— 于是卡片显示"待启动/未就绪"，而转写走的是**远端**，
好好的。修法：读 `/api/capability`，若 `echo-server` 那条的 `source` **不是** local，
就按**远端**报（短值 `远端` / `远端未接`），细节（地址、本机那份在不在跑）仍放 title。

为什么这些断言成立（而不是"看着像"）：
  * `source` 是 `app/capabilities/echo_server.py` 按 `base_url` 算出来的（回环=local，其余=lan）；
  * 本机那份的 `adminUrl` 只对**本机**后端有意义（管理面只绑回环），所以远端时不挂那个 ↗。
"""
import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _js():
    with io.open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8") as fh:
        return fh.read()


def _fn(name="async function refreshRunStatus()"):
    js = _js()
    body = js[js.index(name):]
    # 到下一个顶层 function 为止（这个函数的结尾是 `}` 顶格）
    end = body.index("\n}\n")
    return body[:end + 2]


class BackendCardTests(unittest.TestCase):

    def test_it_reads_the_route_not_only_the_local_backend(self):
        fn = _fn()
        self.assertIn('api("/api/capability")', fn,
                      "要知道转写走哪台，就得看 /api/capability 的 backends[]")
        self.assertIn('b.backendId === "echo-server"', fn)
        self.assertIn("es.source", fn, "判据是 source：local=本机，其余=远端")

    def test_remote_short_values_and_tips(self):
        fn = _fn()
        self.assertIn('"远端"', fn, "远端可用时短值就是「远端」")
        self.assertIn('"远端未接"', fn, "远端不可用要说「未接」，别含糊成「未就绪」")
        self.assertIn("配对来的远端", fn, "title 要说清它是配对来的")

    def test_local_route_keeps_the_old_short_values(self):
        """本机那条路一个字都不许变（用户口径：后端：本机 / 待启动 / 未就绪）。"""
        fn = _fn()
        for short in ('"本机"', '"待启动"', '"未就绪"'):
            with self.subTest(short=short):
                self.assertIn(short, fn)

    def test_the_admin_link_is_only_for_the_local_backend(self):
        """管理面只绑回环 —— 远端时那个 ↗ 会指向本机空端口，所以不许挂。"""
        fn = _fn()
        m = re.search(r'_runCard\("backend"[^\n]*\n?[^\n]*', fn)
        self.assertTrue(m, "找不到后端卡的调用")
        call = m.group(0)
        self.assertIn("!remote", call, "远端时不挂本机管理页的入口")
        self.assertIn("be.adminUrl", call)

    def test_it_is_throttled(self):
        """5 秒 TTL —— 别让仪表盘每条节拍都多打一个请求。"""
        fn = _fn()
        self.assertIn("_capRouteAt", fn)
        self.assertRegex(fn, r"Date\.now\(\) - _capRouteAt > \d+")


class DeclarationTests(unittest.TestCase):
    """`"use strict"` 下未声明就赋值 = ReferenceError（AGENTS 里记过这个坑）。"""

    def test_module_level_state_is_declared(self):
        js = _js()
        for name in ("_capRoute", "_capRouteAt"):
            with self.subTest(name=name):
                self.assertRegex(js, r"let %s\b" % re.escape(name))


if __name__ == "__main__":
    unittest.main()
