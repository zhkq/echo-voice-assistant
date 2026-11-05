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
PAGES = ("index.html", "pricing.html", "pay.html", "download.html")
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


if __name__ == "__main__":
    unittest.main()
