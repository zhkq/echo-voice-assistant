# -*- coding: utf-8 -*-
"""Vibe Pay 签约站点的守卫（2026-11-04，issue #7）。

这些用例不启动浏览器、不 import fastapi —— CI 的 static job 只装 ruff/numpy，
所以这里只用标准库读文件：把“三个页面存在、套餐数字与 issue 一致、支付页不是成功页、
只引用站内资源、截图证据与采集说明在库里”变成会红的东西。

真正的视觉复核看 `docs/vibepay-evidence/` 的截图，并需在公网 HTTPS 就绪后补
“带地址栏”的最终版本；本文件不把本地截图冒充公网证据。
"""
import json
import os
import py_compile
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
EVIDENCE = os.path.join(ROOT, "docs", "vibepay-evidence")
PAGES = ("index.html", "pricing.html", "pay.html")
# 页面只会引用这些站内前缀；出现 http(s) 字体/图片/脚本就是外链回归。
LOCAL_PREFIXES = ("/assets/", "#")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _read_site(name):
    with open(os.path.join(SITE, name), encoding="utf-8") as fh:
        return fh.read()


class _Collector(HTMLParser):
    """收集标签与属性，供“只引用站内资源”的断言使用。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.body_attrs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "body":
            self.body_attrs = attrs


def _parse(html):
    parser = _Collector()
    parser.feed(html)
    parser.close()
    return parser


class PlansTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(_read_site("assets/plans.json"))
        cls.plans = {p["code"]: p for p in cls.data["plans"]}

    def test_monthly_matches_the_server_plan_seed(self):
        """月卡必须与 server/billing.py DEFAULT_PLANS 的月卡一致（issue #7 验收项）。"""
        plan = self.plans["monthly"]
        self.assertEqual(plan["name"], "月卡")
        self.assertEqual(plan["priceCents"], 1000)
        self.assertEqual(plan["days"], 30)
        self.assertEqual(plan["dailyAudioMinutes"], 120)
        self.assertEqual(plan["ttsChars"], 50000)
        self.assertEqual(plan["qps"], 1)
        self.assertEqual(plan["maxDevices"], 3)
        self.assertEqual(plan["maxConcurrent"], 1)
        self.assertTrue(plan["active"])
        self.assertTrue(plan["purchasable"])

    def test_trial_is_seven_days_same_quota_and_not_self_serve(self):
        plan = self.plans["trial"]
        self.assertEqual(plan["priceCents"], 0)
        self.assertEqual(plan["days"], 7)
        for key in ("dailyAudioMinutes", "ttsChars", "qps", "maxDevices", "maxConcurrent"):
            self.assertEqual(plan[key], self.plans["monthly"][key], key)
        self.assertFalse(plan["purchasable"], "试用是邀请制，不能在商品页直接下单")

    def test_money_unit_and_timezone_are_documented(self):
        self.assertEqual(self.data["currency"], "CNY")
        self.assertEqual(self.data["timezone"], "Asia/Shanghai")


class PageTests(unittest.TestCase):
    def test_three_pages_exist_and_are_chinese(self):
        for name in PAGES:
            html = _read_site(name)
            self.assertIn('<html lang="zh-CN">', html, name)
            self.assertIn('name="viewport"', html, name)
            self.assertNotIn("lorem ipsum", html.lower(), name)

    def test_pages_reference_only_site_local_assets(self):
        """字体/图片/脚本必须站内；外链会让截图与审核不可控。"""
        for name in PAGES:
            parser = _parse(_read_site(name))
            for tag, attrs in parser.tags:
                if tag not in ("script", "img", "link", "use", "source"):
                    continue
                value = attrs.get("src") or attrs.get("href") or ""
                if not value:
                    continue
                self.assertTrue(
                    value.startswith(LOCAL_PREFIXES),
                    "%s 的 <%s> 引用了站外资源：%s" % (name, tag, value),
                )
        css = _read_site("assets/site.css").lower()
        self.assertNotIn("@import", css, "CSS 不许 @import 第三方字体")
        self.assertNotIn("url(http", css, "CSS 不许引用站外图片/字体")

    def test_homepage_has_product_logo_and_feature_points(self):
        html = _read_site("index.html")
        self.assertIn("/assets/echo-logo.png", html)
        for text in ("ECHO", "语音助手", "语音指令", "会议录音转写", "说话人分离",
                     "语音合成", "能力后端套餐", "隐私边界"):
            self.assertIn(text, html, text)
        self.assertIn('href="/pricing"', html)
        self.assertIn("echoserver.zhkq.online", html)

    def test_pricing_has_buy_entry_and_the_required_quota_copy(self):
        html = _read_site("pricing.html")
        self.assertIn("套餐与价格", html)
        self.assertIn("/pay?plan=monthly", html)
        js = _read_site("assets/site.js")
        self.assertIn("立即购买", js)
        self.assertIn("/pay?plan=", js)
        for text in ("每日 ASR", "TTS", "QPS", "台设备", "并发", "自然日恢复"):
            self.assertIn(text, js, text)

    def test_pay_page_is_waiting_not_a_success_page(self):
        html = _read_site("pay.html")
        for text in ("支付宝", "等待支付", "沙箱", "不会产生真实扣款",
                     "刷新订单状态", "不会自动跳转"):
            self.assertIn(text, html, text)
        self.assertNotIn('http-equiv="refresh"', html)
        self.assertIn('disabled>打开支付宝收银台', html)
        js = _read_site("assets/site.js")
        for redirect_api in ("location.href", "location.replace", "window.open("):
            self.assertNotIn(redirect_api, js, "支付页不许自动跳转")

    def test_pay_page_does_not_claim_launch_or_production(self):
        html = _read_site("pay.html") + _read_site("pricing.html") + _read_site("index.html")
        self.assertNotIn("已上线", html)
        self.assertNotIn("生产可用", html)
        self.assertNotIn("支付成功页", html)

    def test_mobile_breakpoints_are_declared(self):
        css = _read_site("assets/site.css")
        for width in ("720px", "980px"):
            self.assertIn("@media (max-width: %s)" % width, css)

    def test_each_page_has_the_shared_nav_footer_and_toast(self):
        for name in PAGES:
            html = _read_site(name)
            self.assertIn('class="site-header"', html, name)
            self.assertIn('class="site-footer"', html, name)
            self.assertIn('id="toast"', html, name)
            self.assertIn("/assets/site.js", html, name)


class EvidenceTests(unittest.TestCase):
    def test_before_and_after_screenshots_are_in_the_repo(self):
        expected = (
            "before-no-site.png",
            "after-home.png",
            "after-pricing.png",
            "after-pay.png",
            "after-pricing-mobile.png",
        )
        for name in expected:
            path = os.path.join(EVIDENCE, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 5000, name)

    def test_capture_manifest_lists_all_five_files(self):
        data = json.loads(_read("docs", "vibepay-evidence", "capture.json"))
        pages = {item["page"] for item in data["files"]}
        self.assertTrue({"home", "pricing", "pay", "pricing-mobile", "before-no-site"} <= pages)

    def test_manifest_does_not_claim_public_https(self):
        data = json.loads(_read("docs", "vibepay-evidence", "capture.json"))
        self.assertTrue(data["baseUrl"].startswith("http://127.0.0.1:"))
        joined = " ".join(data["notes"])
        self.assertIn("地址栏", joined)
        self.assertIn("公网 HTTPS", joined)

    def test_evidence_readme_covers_before_after_and_owner_defaults(self):
        text = _read("docs", "vibepay-evidence", "README.md")
        for needle in ("Before", "After", "1440", "375", "768",
                       "APP 名称", "最终域名", "沙箱", "先中文", "已拍板",
                       "带地址栏", "未完成"):
            self.assertIn(needle, text, needle)

    def test_capture_script_compiles(self):
        py_compile.compile(
            os.path.join(ROOT, "scripts", "capture-vibepay-evidence.py"),
            doraise=True,
        )


if __name__ == "__main__":
    unittest.main()
