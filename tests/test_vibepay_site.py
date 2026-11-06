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
import re
import subprocess
import sys
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
EVIDENCE = os.path.join(ROOT, "docs", "vibepay-evidence")
PAGES = ("index.html", "pricing.html", "account.html", "pay.html", "download.html")
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
    def test_all_pages_exist_and_are_chinese(self):
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
        self.assertIn('id="planCards"', html)
        js = _read_site("assets/site.js")
        self.assertIn("立即购买", js)
        self.assertIn("/v1/billing/plans", js)
        self.assertIn("/v1/billing/orders", js)
        self.assertIn('"/pay?order="', js)
        self.assertNotIn("/pay?plan=", js + html)
        for text in ("每日 ASR", "TTS", "QPS", "台设备", "并发", "自然日恢复"):
            self.assertIn(text, js, text)

    def test_account_page_has_login_register_and_dashboard_sections(self):
        html = _read_site("account.html")
        for text in ("登录 / 注册", "邀请码", "工号 / 账号", "注册", "退出登录",
                     "当前权益", "最近订单", "购买 / 续费"):
            self.assertIn(text, html, text)
        for element in ('id="authForm"', 'id="dashboardView"', 'id="ordersList"',
                        'id="entitlementStatus"', 'id="entitlementRule"',
                        'id="entitlementPolicy"', 'data-account-cta'):
            self.assertIn(element, html, element)
        js = _read_site("assets/site.js")
        for path in ("/v1/account/login", "/v1/account/register", "/v1/account/me",
                     "/v1/account/logout", "/v1/billing/orders"):
            self.assertIn(path, js, path)

    def test_pay_page_is_order_driven_with_real_cashier_link(self):
        """支付页必须从订单读金额/订单号，并用服务端 payUrl 打开收银台。

        Phase 3 的硬约束：不能再用静态二维码占位图，也不能把某个固定沙箱订单的
        signed URL 写进 HTML。真实收银台链接只来自 `GET /orders/{id}` 的响应。
        """
        html = _read_site("pay.html")
        for text in ("支付宝", "等待支付", "刷新订单状态", "前往支付宝收银台",
                     "订单号", "应付金额", "取消订单", "测试环境"):
            self.assertIn(text, html, text)
        for forbidden in ("预览版", "尚未接入", "示例订单", "qr-placeholder",
                          "alipay-sandbox-qr", "openapi-sandbox.dl.alipaydev.com"):
            self.assertNotIn(forbidden, html, forbidden)
        self.assertNotIn('id="payQr"', html, "支付页不再使用二维码图片；收银台由真实 payUrl 打开")
        self.assertIn('id="payWaiting"', html)
        self.assertIn('id="payResult"', html)
        self.assertIn('id="testNotice"', html)
        self.assertNotIn('http-equiv="refresh"', html)
        # issue #13：支付前展示“当前到期 / 本次续费 / 支付后预计到期”。
        for text in ("当前到期", "本次续费", "支付后预计到期"):
            self.assertIn(text, html, text)
        for element in ('id="orderCurrentUntil"', 'id="orderProjectedUntil"',
                        'id="renewalRule"'):
            self.assertIn(element, html, element)
        js = _read_site("assets/site.js")
        for text in ("/v1/billing/orders/", "/sync", "payUrl", "visibilitychange",
                     "订单已关闭", "订单已退款", "支付成功", "sandbox",
                     "projectedPaidUntil", "currentPaidUntil", "renewalRule"):
            self.assertIn(text, js, text)
        self.assertNotIn("支付成功后起算", js + html,
                         "起算口径必须由服务端 renewalRule/renewalBasis 给出")
        for redirect_api in ("location.href", "location.replace", "window.open("):
            self.assertNotIn(redirect_api, js, "支付页不许自动跳转")

    def test_renewal_rule_is_server_driven_on_the_account_page(self):
        html = _read_site("account.html")
        self.assertIn('id="entitlementRule"', html)
        self.assertIn('id="entitlementPolicy"', html)
        js = _read_site("assets/site.js")
        for text in ("entitlementRule", "entitlementPolicy", "renewalRule",
                     "planChangeRule", "refundRule", "autoRenew"):
            self.assertIn(text, js, text)
        # 订单列表不能只渲染 paid：三种终态都要有文案。
        for text in ("已退款", "已关闭", "等待支付", "已支付"):
            self.assertIn(text, js, text)

    def test_no_static_qr_asset_is_shipped(self):
        self.assertFalse(os.path.exists(os.path.join(SITE, "assets", "qr-placeholder.svg")))
        self.assertFalse(os.path.exists(os.path.join(SITE, "assets", "alipay-sandbox-qr.png")))
        for name in PAGES:
            html = _read_site(name)
            self.assertNotIn("qr-placeholder", html, name)
            self.assertNotIn("alipay-sandbox-qr", html, name)

    def test_public_pages_are_not_marked_as_preview_or_sandbox(self):
        """正式页面（首页/套餐/下载）不出现预览/沙箱/未接入；支付页只保留
        由服务端 `sandbox` 字段驱动的"测试环境"标注（切生产自动消失）。"""
        html = _read_site("index.html") + _read_site("pricing.html") + _read_site("download.html")
        for forbidden in ("预览版", "沙箱", "尚未接入", "签约材料预览", "支付示例"):
            self.assertNotIn(forbidden, html, forbidden)
        pay_html = _read_site("pay.html")
        self.assertNotIn("沙箱", pay_html)
        self.assertNotIn("尚未接入", pay_html)
        self.assertIn("测试环境", pay_html)

    def test_pay_page_does_not_claim_launch_or_production(self):
        html = _read_site("pay.html") + _read_site("pricing.html") + _read_site("index.html")
        self.assertNotIn("已上线", html)
        self.assertNotIn("生产可用", html)
        self.assertNotIn("支付成功页", html)

    def test_mobile_breakpoints_are_declared(self):
        css = _read_site("assets/site.css")
        for width in ("720px", "980px"):
            self.assertIn("@media (max-width: %s)" % width, css)

    def test_mobile_page_hero_does_not_zero_horizontal_padding(self):
        """375 宽下 /pricing、/pay 的标题不能贴屏边。

        2026-11-04 复核：`.page-hero { padding: 40px 0 28px }` 会把 `.wrap` 的
        左右 18px 覆盖成 0，h1.left 变成 0（首页正常 18）。这里钉住 720px 媒体查询里
        `.page-hero` 的左右内边距不能是 0；真正的 375 宽实测在截图脚本里。
        """
        css = _read_site("assets/site.css")
        media = re.search(r"@media \(max-width: 720px\)\s*\{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(media, "CSS 里找不到 720px 媒体查询")
        block = media.group(1)
        wrap = re.search(r"\.wrap\s*\{([^}]*)\}", block)
        self.assertIsNotNone(wrap, "720px 媒体查询里没有 .wrap 规则")
        self.assertIn("padding: 0 18px", wrap.group(1),
                      "移动端 .wrap 的左右内边距必须保留")
        hero = re.search(r"\.page-hero\s*\{([^}]*)\}", block)
        self.assertIsNotNone(hero, "720px 媒体查询里没有 .page-hero 规则")
        body = hero.group(1)
        for raw in re.findall(r"padding\s*:\s*([^;]+);", body):
            parts = raw.split()
            if len(parts) == 1:
                horizontal = [parts[0]]
            elif len(parts) == 2:
                horizontal = [parts[1]]
            elif len(parts) == 3:
                horizontal = [parts[1]]
            else:
                horizontal = [parts[1], parts[3]]
            for value in horizontal:
                self.assertNotIn(value, ("0", "0px", "0.0px"),
                                 "720px 下 .page-hero 的左右 padding 被清零：%r" % raw)
        for prop in ("padding-left", "padding-right"):
            for value in re.findall(prop + r"\s*:\s*([^;]+);", body):
                self.assertNotIn(value.strip(), ("0", "0px", "0.0px"),
                                 "720px 下 .page-hero 的 %s 被清零" % prop)

    def test_each_page_has_the_shared_nav_footer_and_toast(self):
        for name in PAGES:
            html = _read_site(name)
            self.assertIn('class="site-header"', html, name)
            self.assertIn('class="site-footer"', html, name)
            self.assertIn('id="toast"', html, name)
            self.assertIn("/assets/site.js", html, name)

    def test_each_page_has_a_download_entry_in_nav_and_footer(self):
        """issue #9：导航栏和页脚在首页/套餐页/支付页/下载页都要有「下载」入口。"""
        for name in PAGES:
            html = _read_site(name)
            self.assertIn('class="nav-download" href="/download"', html, name)
            self.assertIn('<a href="/download">下载客户端</a>', html, name)


class DownloadSourceTests(unittest.TestCase):
    """`site/assets/downloads.json` 是下载页与 README 的唯一事实源（PR #10 review）。"""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(_read("site", "assets", "downloads.json"))
        cls.files = {item["platform"]: item for item in cls.data["files"]}

    def test_source_has_the_required_identity_and_android_build_fields(self):
        self.assertEqual(self.data["product"], "ECHO 语音助手")
        self.assertEqual(self.data["version"], "0.9.0-beta")
        self.assertEqual(self.data["date"], "2026-11-04")
        self.assertEqual(set(self.files), {"Windows", "macOS", "Android"})
        android = self.files["Android"]
        self.assertEqual(android["versionName"], "1.2")
        self.assertEqual(android["versionCode"], 3)
        for platform, item in self.files.items():
            self.assertTrue(item["name"].endswith((".zip", ".apk")), platform)
            self.assertIsInstance(item["size"], int, platform)
            self.assertGreater(item["size"], 0, platform)
            self.assertRegex(item["sha256"], r"^[0-9A-Fa-f]{64}$", platform)

    def test_page_version_date_names_sizes_and_hashes_match_source(self):
        html = _read_site("download.html")
        self.assertIn("内测版 " + self.data["version"], html)
        self.assertIn(self.data["date"], html)
        for platform, item in self.files.items():
            self.assertIn(item["name"], html, platform)
            self.assertIn("/downloads/" + item["name"], html, platform)
            self.assertIn("%.1f MB" % (item["size"] / 1_000_000.0), html, platform)
            self.assertIn("{:,} 字节（{:.1f} MiB）".format(
                item["size"], item["size"] / 1_048_576.0), html, platform)
            self.assertIn(item["sha256"], html, platform)
        self.assertEqual(len(re.findall(r"\b[0-9A-F]{64}\b", html)), 3)

    def test_readme_table_matches_source(self):
        readme = _read("site", "downloads", "README.md")
        start = readme.index("<!-- downloads-table:start -->")
        end = readme.index("<!-- downloads-table:end -->", start)
        table = readme[start:end]
        for platform, item in self.files.items():
            row = "| %s | `%s` | %s | `%s` |" % (
                platform, item["name"], format(item["size"], ","), item["sha256"])
            self.assertIn(row, table, platform)

    def test_no_zip_or_apk_is_committed_under_site_downloads(self):
        """安装包只放服务器；仓库里可以有 README，但不能有 zip/apk。"""
        proc = subprocess.run(
            ["git", "ls-files", "site/downloads"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tracked = proc.stdout.splitlines()
        offenders = [name for name in tracked if name.lower().endswith((".zip", ".apk"))]
        self.assertEqual(offenders, [], "安装包只能部署到服务器，不能提交进 git")

    def test_sync_script_reports_page_and_readme_in_sync(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "update-download-page.py"), "--check"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("一致", proc.stdout)

    def test_verify_downloads_script_has_local_and_remote_checks(self):
        script = _read("scripts", "verify-downloads.ps1")
        for needle in ("$Dir", "$BaseUrl", "[switch]$Full", "Get-FileHash",
                       "Content-Length", "exit 1"):
            self.assertIn(needle, script, needle)


class DownloadPageTests(unittest.TestCase):
    def test_download_page_has_three_platform_cards(self):
        html = _read_site("download.html")
        for text in ("Windows 10 / 11 x64",
                     "macOS（Intel / Apple Silicon 通用）",
                     "Android",
                     "内测 debug 包；安装前需在系统设置里允许「安装未知应用」。"):
            self.assertIn(text, html, text)

    def test_download_page_uses_the_build_date_wording(self):
        html = _read_site("download.html")
        self.assertIn("安装包构建日期", html)
        self.assertNotIn("更新日期", html)

    def test_download_page_links_the_installers_and_github_backup(self):
        html = _read_site("download.html")
        data = json.loads(_read("site", "assets", "downloads.json"))
        for item in data["files"]:
            self.assertIn("/downloads/" + item["name"], html, item["platform"])
        for target in (
            "https://github.com/zhkq/echo-voice-assistant/releases/download/v3.0.1/"
            "ECHO-delivery-20261104-0018.zip",
            "https://github.com/zhkq/echo-voice-assistant/releases/download/v3.0.1/"
            "ECHO-delivery-macos-20261104-0017.zip",
        ):
            self.assertIn(target, html, target)

    def test_download_page_has_install_steps_and_requirements(self):
        html = _read_site("download.html")
        for text in ("装我.cmd", "先读我.md", "echo-core/mac/setup_mac.sh",
                     "系统要求与内测说明", "Windows 10 / 11，64 位",
                     "macOS 14.0 或更新", "Android 8.0 或更新", "磁盘可用空间",
                     "能力后端"):
            self.assertIn(text, html, text)

    def test_download_page_has_no_preview_sandbox_or_launch_claims(self):
        html = _read_site("download.html")
        for forbidden in ("预览版", "沙箱", "尚未接入", "已上线", "生产可用"):
            self.assertNotIn(forbidden, html, forbidden)

    def test_android_is_a_debug_beta_download_with_unknown_sources_note(self):
        html = _read_site("download.html")
        self.assertIn("/downloads/ECHO-Android-0.9.0-beta-debug.apk", html)
        self.assertIn("debug 包", html)
        self.assertIn("未知应用", html)
        self.assertIn("release 签名版", html)


class EvidenceTests(unittest.TestCase):
    def test_before_and_after_screenshots_are_in_the_repo(self):
        expected = (
            "before-no-site.png",
            "before-home.png",
            "before-download-404.png",
            "after-home.png",
            "after-pricing.png",
            "after-pay.png",
            "after-download.png",
            "after-pricing-mobile.png",
            "after-download-mobile.png",
        )
        for name in expected:
            path = os.path.join(EVIDENCE, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 5000, name)

    def test_capture_manifest_lists_all_files(self):
        data = json.loads(_read("docs", "vibepay-evidence", "capture.json"))
        pages = {item["page"] for item in data["files"]}
        self.assertTrue({
            "home", "pricing", "pay", "download",
            "pricing-mobile", "download-mobile",
            "before-home", "before-download-404", "before-no-site",
        } <= pages)

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
                       "带地址栏", "未完成", "提交前必须做", "payUrl", "去掉预览标注"):
            self.assertIn(needle, text, needle)

    def test_capture_script_compiles(self):
        py_compile.compile(
            os.path.join(ROOT, "scripts", "capture-vibepay-evidence.py"),
            doraise=True,
        )


class Phase3EvidenceTests(unittest.TestCase):
    """Phase 3 的 Before/After 证据与 mock 边界（issue #11）。"""

    PHASE3 = os.path.join(EVIDENCE, "phase3")

    def test_before_and_after_files_exist(self):
        expected = (
            "before-home.png", "before-download.png", "before-pricing.png", "before-pay.png",
            "before-account-404.png", "after-home.png", "after-download.png",
            "after-pricing.png", "after-account.png", "after-login.png",
            "after-pay.png", "after-paid.png", "after-closed.png", "after-timeout.png",
            "after-account-mobile.png", "after-pay-mobile.png",
            "e2e-account.png", "e2e-paid.png", "e2e-closed.png",
        )
        for name in expected:
            path = os.path.join(self.PHASE3, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 5000, name)

    def test_manifest_records_baseline_mock_and_real_sandbox_boundary(self):
        data = json.loads(_read("docs", "vibepay-evidence", "phase3", "capture.json"))
        self.assertEqual(data["mode"], "mock backend")
        self.assertTrue(data["baseUrl"].startswith("http://127.0.0.1:"))
        self.assertIn("ref", data["baseline"])
        joined = " ".join(data["notes"])
        self.assertIn("mock", joined)
        self.assertIn("Lead", joined)
        self.assertIn("2026-11-05", joined)
        self.assertIn("不冒充", joined)
        pages = {item["page"] for item in data["files"]}
        self.assertTrue({
            "before-pricing", "before-pay", "before-account-404",
            "before-home", "before-download", "before-pricing", "before-pay",
            "before-account-404", "after-home", "after-download", "after-pricing",
            "after-account", "after-pay", "after-paid", "after-closed", "after-timeout",
        } <= pages)

    def test_phase3_readme_states_the_evidence_limits(self):
        text = _read("docs", "vibepay-evidence", "phase3", "README.md")
        for needle in ("Before", "After", "1440", "375", "mock", "Lead",
                       "2026-11-05", "生产", "app_id", "e2e"):
            self.assertIn(needle, text, needle)

    def test_e2e_manifest_ran_against_the_real_backend(self):
        data = json.loads(_read("docs", "vibepay-evidence", "phase3", "e2e.json"))
        self.assertTrue(data["ok"])
        self.assertIn("server.main", data["backend"])
        self.assertIn("payment.provider=mock", data["backend"])
        joined = " ".join(data["notes"])
        self.assertIn("Mock", joined)
        self.assertIn("Lead", joined)
        steps = {item["step"] for item in data["steps"]}
        self.assertTrue({"register", "create_order", "paid", "account_after_paid",
                         "login_gate", "logout_login", "cancel"} <= steps)

    def test_phase3_capture_scripts_compile(self):
        for name in ("capture-phase3-evidence.py", "e2e-vibepay-mock.py"):
            py_compile.compile(os.path.join(ROOT, "scripts", name), doraise=True)


class RenewalEvidenceTests(unittest.TestCase):
    """issue #13 的 Before/After 证据（续费规则 / 预计到期）。"""

    DIR = os.path.join(ROOT, "docs", "billing-renewal-evidence")

    def test_before_and_after_files_exist(self):
        expected = (
            "before-pay.png", "after-pay.png",
            "before-account.png", "after-account.png",
            "before-pay-mobile.png", "after-pay-mobile.png",
            "before-account-mobile.png", "after-account-mobile.png",
            "after-refunded-order.png",
        )
        for name in expected:
            path = os.path.join(self.DIR, name)
            self.assertTrue(os.path.isfile(path), name)
            self.assertGreater(os.path.getsize(path), 5000, name)

    def test_manifest_records_baseline_and_mock_boundary(self):
        data = json.loads(_read("docs", "billing-renewal-evidence", "capture.json"))
        self.assertEqual(data["mode"], "mock backend")
        self.assertEqual(data["baseline"]["ref"], "2d07bc3")
        joined = " ".join(data["notes"])
        self.assertIn("mock", joined)
        self.assertIn("真实扣款", joined)
        pages = {item["page"] for item in data["files"]}
        self.assertTrue({
            "before-pay", "after-pay", "before-account", "after-account",
            "before-pay-mobile", "after-pay-mobile", "after-refunded-order",
        } <= pages)

    def test_readme_states_the_evidence_limits(self):
        text = _read("docs", "billing-renewal-evidence", "README.md")
        for needle in ("Before", "After", "1440", "375", "mock", "2d07bc3",
                       "真实扣款", "限制"):
            self.assertIn(needle, text, needle)

    def test_capture_script_compiles(self):
        py_compile.compile(
            os.path.join(ROOT, "scripts", "capture-renewal-evidence.py"),
            doraise=True,
        )


if __name__ == "__main__":
    unittest.main()
