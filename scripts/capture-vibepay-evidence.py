#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集 Vibe Pay 签约用站点（首页 / 商品页 / 支付页 / 下载页）的截图证据。

用法（在仓库根目录）：

    # 起一个带“干净 URL”的本地预览（/、/pricing、/pay、/download）
    python scripts/capture-vibepay-evidence.py --serve --port 8090

    # 用本地预览采集 After 截图 + Before（基线站点：下载页加入之前的那一版）
    python scripts/capture-vibepay-evidence.py

    # 站点已部署到公网后，直接对真实 HTTPS 地址采集（不伪造地址栏）
    python scripts/capture-vibepay-evidence.py --base-url https://echoserver.zhkq.online

产物落在 docs/vibepay-evidence/：after-home.png、after-pricing.png、after-pay.png、
after-download.png、after-pricing-mobile.png、after-download-mobile.png、
before-home.png、before-download-404.png、before-no-site.png、capture.json。

说明：
  * 截图用系统 Google Chrome（Playwright 驱动），视口 1440×900，浅色，中文；
    移动端用 375×812 验证无横向溢出，并额外截下载页。
  * Before 用 `--baseline-ref`（默认 origin/integration/billing-vibepay）从 git 取出
    基线站点实际渲染：首页（导航/页脚没有“下载”）+ /download 的真实 404。
  * 本脚本只截“页面内容”。支付宝要求的“带地址栏”截图必须等公网 HTTPS 就绪后，
    用真实浏览器窗口对最终域名补采；脚本不会伪造地址栏，也不把本地 127.0.0.1
    当作公网证据。
  * 支付页必须停在“等待支付”，脚本会断言它没有跳转、也没有把沙箱写成成功。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
OUT_DIR = ROOT / "docs" / "vibepay-evidence"
DEFAULT_BASELINE_REF = "origin/integration/billing-vibepay"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 375, "height": 812}
TABLET = {"width": 768, "height": 1024}

#: 每个页面要检查的文案与路径。文案检查是“截图可交付”的最低判据，
#: 不是页面渲染测试的替代品 —— 页面样式由人看截图复核。
PAGES = (
    {
        "name": "home",
        "path": "/",
        "title": "ECHO 语音助手",
        "must": ("ECHO", "语音助手", "语音指令", "会议录音转写", "说话人分离", "语音合成", "能力后端套餐"),
        "file": "after-home.png",
    },
    {
        "name": "pricing",
        "path": "/pricing",
        "title": "套餐与价格",
        "must": ("月卡", "10", "120", "5 万", "3 台设备", "并发 1", "立即购买"),
        "file": "after-pricing.png",
    },
    {
        "name": "pay",
        "path": "/pay?plan=monthly",
        "title": "确认订单并支付",
        "must": ("支付宝", "等待支付", "沙箱", "¥10.00", "刷新订单状态"),
        "file": "after-pay.png",
        "stay": "/pay",
    },
    {
        "name": "download",
        "path": "/download",
        "title": "下载",
        "must": (
            "Windows 10 / 11 x64",
            "macOS（Intel / Apple Silicon 通用）",
            "Android",
            "内测版 0.9.0-beta",
            "0.9.0-beta",
            "74.9 MB",
            "8.8 MB",
            "22.3 MB",
            "2026-11-04",
            "装我.cmd",
            "SHA-256 校验值",
            "内测阶段",
        ),
        "html_must": (
            "/downloads/ECHO-Windows-0.9.0-beta.zip",
            "/downloads/ECHO-macOS-0.9.0-beta.zip",
            "/downloads/ECHO-Android-0.9.0-beta-debug.apk",
            "CC6B5AE5850EAB786C1C98248DC76BAEBFB3E5BD4FB67B84218B5EF2B71FFAE8",
            "76BCD9690E9AAF1AB8FBB5147C62A4DB512605A725D1C4D95EB1BC1F11DD2B51",
            "5ADD81EBE1B86CD5D46968E36AFBB25C5FC2B61A38133C9BA7975202B7CE0C15",
            "github.com/zhkq/echo-voice-assistant/releases/download/v3.0.1/",
        ),
        "file": "after-download.png",
    },
)

for suffix, mime in (
    (".html", "text/html; charset=utf-8"),
    (".css", "text/css; charset=utf-8"),
    (".js", "application/javascript; charset=utf-8"),
    (".json", "application/json; charset=utf-8"),
    (".svg", "image/svg+xml"),
    (".png", "image/png"),
):
    mimetypes.add_type(mime.split(";", 1)[0], suffix)


class SiteHandler(SimpleHTTPRequestHandler):
    """把 /、/pricing、/pay、/download 映射到 site/ 下的静态文件，不列目录、不缓存。"""

    root = SITE_DIR

    def translate_path(self, path):  # noqa: D401 - stdlib hook
        parsed = urlsplit(path)
        clean = unquote(parsed.path)
        if clean in ("", "/"):
            rel = "index.html"
        elif clean in ("/pricing", "/pricing/"):
            rel = "pricing.html"
        elif clean in ("/pay", "/pay/"):
            rel = "pay.html"
        elif clean in ("/download", "/download/"):
            rel = "download.html"
        else:
            rel = clean.lstrip("/")
        target = (Path(self.root) / rel).resolve()
        root = Path(self.root).resolve()
        if target != root and root not in target.parents:
            return str(root / "__not_found__")
        return str(target)

    def list_directory(self, path):  # noqa: D401 - stdlib hook
        self.send_error(404, "Not Found")
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("VIBEPAY_SITE_VERBOSE"):
            super().log_message(fmt, *args)


def start_server(root: Path, host: str = "127.0.0.1", port: int = 0):
    handler = type("BoundSiteHandler", (SiteHandler,), {"root": root})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="vibepay-site", daemon=True)
    thread.start()
    return server


def serve(args) -> int:
    server = start_server(SITE_DIR, args.host, args.port)
    host, port = server.server_address[:2]
    print("ECHO 签约站点预览：http://%s:%s/" % (host, port), flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def _launch_browser(playwright, chrome_path: str | None):
    """优先用系统 Chrome / Edge（与人工复核一致），再退到 Playwright 自带浏览器。"""
    attempts = []
    if chrome_path:
        attempts.append({"executable_path": chrome_path})
    else:
        attempts.extend([
            {"channel": "chrome"},
            {"channel": "msedge"},
            {"channel": "chromium"},
        ])
    attempts.append({})
    errors = []
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # Playwright 的 Error 类型随版本变化，这里只需重试
            errors.append("%s: %s" % (kwargs, exc))
    raise RuntimeError("启动浏览器失败：\n" + "\n".join(errors))


def _wait_ready(page, timeout: int = 15000) -> None:
    page.wait_for_function(
        "() => ['1', 'error'].includes(document.documentElement.getAttribute('data-ready'))",
        timeout=timeout,
    )
    state = page.evaluate("() => document.documentElement.getAttribute('data-ready')")
    if state != "1":
        detail = page.inner_text("body")[:400].replace("\n", " ")
        raise RuntimeError("页面渲染失败（data-ready=%s）：%s" % (state, detail))
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.wait_for_timeout(120)


def _assert_no_horizontal_overflow(page, label: str) -> None:
    size = page.evaluate(
        "() => ({ scroll: document.documentElement.scrollWidth, view: window.innerWidth,"
        " body: document.body.scrollWidth })"
    )
    if size["scroll"] > size["view"] + 1 or size["body"] > size["view"] + 1:
        raise RuntimeError(
            "%s 在 %spx 视口下横向溢出：scrollWidth=%s bodyScrollWidth=%s"
            % (label, size["view"], size["scroll"], size["body"])
        )


def _assert_page_left_margin(page, label: str, minimum: float = 17.0) -> None:
    """375 宽下标题不能贴屏边：`h1.left` 至少要有 .wrap 的 18px 内边距。

    2026-11-04 真机复核：`@media (max-width: 720px)` 里 `.page-hero { padding: 40px 0 28px }`
    把 `.wrap` 的左右 18px 覆盖成 0，`/pricing`、`/pay` 的 h1.left = 0（首页正常 18）。
    横向溢出检查查不出这种“贴边”，所以单独断言。
    """
    metrics = page.evaluate(
        """() => {
            const h1 = document.querySelector('main h1') || document.querySelector('h1');
            const hero = document.querySelector('.page-hero');
            return {
                h1Left: h1 ? h1.getBoundingClientRect().left : null,
                heroPadLeft: hero ? parseFloat(getComputedStyle(hero).paddingLeft) : null,
            };
        }"""
    )
    if metrics["h1Left"] is None:
        raise RuntimeError("%s 找不到 h1，无法检查左边距" % label)
    if metrics["h1Left"] < minimum:
        raise RuntimeError(
            "%s 的 h1 左边距 %.1fpx < %.0fpx（移动端标题贴边）"
            % (label, metrics["h1Left"], minimum)
        )
    if metrics["heroPadLeft"] is not None and metrics["heroPadLeft"] <= 0:
        raise RuntimeError(
            "%s 的 .page-hero 横向内边距为 0（右/左被 padding 简写覆盖）" % label
        )


def _assert_shared_entries(page, label: str) -> None:
    """每个页面都必须有导航栏和页脚「下载」入口（issue #9 验收项）。"""
    counts = page.evaluate(
        """() => ({
            nav: document.querySelectorAll('.nav a[href="/download"]').length,
            footer: document.querySelectorAll('.footer-links a[href="/download"]').length,
        })"""
    )
    if counts["nav"] != 1 or counts["footer"] != 1:
        raise RuntimeError(
            "%s 的下载入口数量不对：导航 %s 个、页脚 %s 个（各应为 1 个）"
            % (label, counts["nav"], counts["footer"])
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(ROOT), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _base_commit(ref: str = "origin/main") -> str:
    got = _git("merge-base", "HEAD", ref)
    if got != "unknown":
        return got
    return _git("rev-parse", ref)


def _capture_previous_site(context, ref: str, out_dir: Path) -> list:
    """基线：`ref` 里还没有下载页，但站点本身在。

    从 git 取出 `ref` 的 `site/` 实际渲染，截图首页（导航/页脚都没有「下载」）
    和 `/download` 的真实 404 —— 不用别的页面冒充 Before。
    """
    tmp = Path(tempfile.mkdtemp(prefix="echo-vibepay-before-"))
    server = None
    try:
        try:
            blob = subprocess.check_output(
                ["git", "archive", ref, "site"], cwd=str(ROOT), stderr=subprocess.PIPE
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "replace").strip() or str(exc)
            raise RuntimeError("无法从 %s 取基线站点：%s" % (ref, detail)) from exc
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:  # Python < 3.12 没有 filter 参数
                tar.extractall(tmp)

        root = tmp / "site"
        if not (root / "index.html").is_file():
            raise RuntimeError("%s 里没有 site/index.html，不能作为基线" % ref)
        server = start_server(root)
        host, port = server.server_address[:2]
        base = "http://%s:%s" % (host, port)
        files = []

        page = context.new_page()
        page.goto(base + "/", wait_until="load")
        _wait_ready(page)
        _assert_no_horizontal_overflow(page, "before-home")
        nav_count = page.evaluate('() => document.querySelectorAll(".nav a[href=\\"/download\\"]").length')
        if nav_count:
            raise RuntimeError("基线 %s 的首页已经有下载入口，选择的上游不对" % ref)
        target = out_dir / "before-home.png"
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        files.append({
            "page": "before-home",
            "url": base + "/",
            "title": page.title(),
            "viewport": DESKTOP,
            "file": target.name,
            "sha256": _sha256(target),
            "note": "基线 %s：下载页加入之前的首页，导航/页脚还没有「下载」入口。" % ref,
        })
        print("已采集 before-home    %s" % target)
        page.close()

        page = context.new_page()
        response = page.goto(base + "/download", wait_until="load")
        status = response.status if response is not None else None
        if status != 404:
            raise RuntimeError("基线 %s 的 /download 应当是 404，实际 %s" % (ref, status))
        target = out_dir / "before-download-404.png"
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        files.append({
            "page": "before-download-404",
            "url": base + "/download",
            "title": page.title(),
            "status": status,
            "viewport": DESKTOP,
            "file": target.name,
            "sha256": _sha256(target),
            "note": "基线 %s 没有下载页；这是真实请求 /download 的 404 响应。" % ref,
        })
        print("已采集 before-download %s" % target)
        page.close()
        return files
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def _capture_baseline(context, base_url: str, out_dir: Path) -> dict:
    """更早的基线：本 PR 之前仓库里没有这个站点，访问 / 就是一个普通 404。

    保留给 issue #7 的证据；下载页的 Before 用 `_capture_previous_site`。
    """
    empty = Path(tempfile.mkdtemp(prefix="echo-vibepay-empty-"))
    server = start_server(empty)
    try:
        host, port = server.server_address[:2]
        page = context.new_page()
        page.goto("http://%s:%s/" % (host, port), wait_until="load")
        page.wait_for_timeout(150)
        path = out_dir / "before-no-site.png"
        page.screenshot(path=str(path), full_page=True, animations="disabled")
        title = page.title()
        page.close()
        return {"file": path.name, "url": "http://%s:%s/" % (host, port), "title": title}
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(empty, ignore_errors=True)


def capture(args) -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError  # noqa: F401
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print("缺少 Playwright：%s" % exc, file=sys.stderr)
        print("请先 pip install playwright；本脚本使用系统 Chrome，无需下载浏览器。", file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    local_server = None
    base_url = args.base_url.rstrip("/") if args.base_url else ""
    if not base_url:
        local_server = start_server(SITE_DIR, args.host, args.port)
        host, port = local_server.server_address[:2]
        base_url = "http://%s:%s" % (host, port)
        print("本地预览：%s" % base_url)

    files = []
    browser_name = "unknown"
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, args.chrome)
            browser_name = "%s %s" % (browser.browser_type.name, browser.version)
            context = browser.new_context(
                viewport=DESKTOP,
                device_scale_factor=1,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
                reduced_motion="reduce",
            )
            for spec in PAGES:
                page = context.new_page()
                url = base_url + spec["path"]
                page.goto(url, wait_until="load")
                _wait_ready(page)
                body = page.inner_text("body")
                missing = [needle for needle in spec["must"] if needle not in body]
                html = page.content()
                missing += [needle for needle in spec.get("html_must", ()) if needle not in html]
                if missing:
                    raise RuntimeError("%s 缺少必要文案或链接：%s" % (spec["name"], "、".join(missing)))
                if spec.get("stay") and spec["stay"] not in page.url:
                    raise RuntimeError("%s 发生了跳转：%s" % (spec["name"], page.url))
                _assert_no_horizontal_overflow(page, spec["name"])
                _assert_shared_entries(page, spec["name"])
                target = out_dir / spec["file"]
                page.screenshot(path=str(target), full_page=True, animations="disabled")
                files.append({
                    "page": spec["name"],
                    "url": url,
                    "title": page.title(),
                    "viewport": DESKTOP,
                    "file": target.name,
                    "sha256": _sha256(target),
                })
                print("已采集 %-8s %s" % (spec["name"], target))
                page.close()

            mobile = browser.new_context(
                viewport=MOBILE,
                device_scale_factor=2,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
                reduced_motion="reduce",
            )
            mobile_page = mobile.new_page()
            mobile_page.goto(base_url + "/pricing", wait_until="load")
            _wait_ready(mobile_page)
            _assert_no_horizontal_overflow(mobile_page, "pricing-mobile")
            _assert_page_left_margin(mobile_page, "pricing-mobile")
            mobile_target = out_dir / "after-pricing-mobile.png"
            mobile_page.screenshot(path=str(mobile_target), full_page=True, animations="disabled")
            files.append({
                "page": "pricing-mobile",
                "url": base_url + "/pricing",
                "title": mobile_page.title(),
                "viewport": MOBILE,
                "file": mobile_target.name,
                "sha256": _sha256(mobile_target),
            })
            print("已采集 mobile   %s" % mobile_target)

            download_page = mobile.new_page()
            download_page.goto(base_url + "/download", wait_until="load")
            _wait_ready(download_page)
            _assert_no_horizontal_overflow(download_page, "download-mobile")
            _assert_page_left_margin(download_page, "download-mobile")
            _assert_shared_entries(download_page, "download-mobile")
            download_target = out_dir / "after-download-mobile.png"
            download_page.screenshot(path=str(download_target), full_page=True, animations="disabled")
            files.append({
                "page": "download-mobile",
                "url": base_url + "/download",
                "title": download_page.title(),
                "viewport": MOBILE,
                "file": download_target.name,
                "sha256": _sha256(download_target),
            })
            print("已采集 mobile   %s" % download_target)
            download_page.close()

            for spec in PAGES:
                check = mobile.new_page()
                check.goto(base_url + spec["path"], wait_until="load")
                _wait_ready(check)
                _assert_no_horizontal_overflow(check, spec["name"] + "-mobile-check")
                _assert_page_left_margin(check, spec["name"] + "-mobile-check")
                check.close()
            mobile.close()

            tablet = browser.new_context(
                viewport=TABLET,
                device_scale_factor=1,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
                reduced_motion="reduce",
            )
            for spec in PAGES:
                check = tablet.new_page()
                check.goto(base_url + spec["path"], wait_until="load")
                _wait_ready(check)
                _assert_no_horizontal_overflow(check, spec["name"] + "-tablet-check")
                check.close()
            tablet.close()

            baseline = {"skipped": "外部 --base-url 模式不伪造 Before 截图"}
            if not args.base_url:
                baseline_files = _capture_previous_site(context, args.baseline_ref, out_dir)
                files.extend(baseline_files)
                baseline = {
                    "ref": args.baseline_ref,
                    "commit": _base_commit(args.baseline_ref),
                    "files": [item["file"] for item in baseline_files],
                }
                before = _capture_baseline(context, base_url, out_dir)
                files.append({
                    "page": "before-no-site",
                    "url": before["url"],
                    "title": before["title"],
                    "viewport": DESKTOP,
                    "file": before["file"],
                    "sha256": _sha256(out_dir / before["file"]),
                    "note": "更早的基线（站点加入之前）：空目录服务的真实 404 页面，不是仿造的旧 UI。",
                })
                print("已采集 before    %s" % (out_dir / before["file"]))
            context.close()
            browser.close()
    finally:
        if local_server is not None:
            local_server.shutdown()
            local_server.server_close()

    manifest = {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "baseCommit": _base_commit(args.baseline_ref),
        "baselineRef": args.baseline_ref,
        "baseUrl": base_url,
        "browser": browser_name,
        "desktopViewport": DESKTOP,
        "mobileViewport": MOBILE,
        "tableViewport": TABLET,
        "theme": "light",
        "notes": [
            "截图由 scripts/capture-vibepay-evidence.py 生成，页面实际渲染，不是拼图或 mockup。",
            "本地/预览截图不含浏览器地址栏；支付宝要求的带地址栏截图需在公网 HTTPS 就绪后对最终域名补采。",
            "支付页停留在“等待支付”的沙箱示例状态，不是支付成功页。",
            "下载页的 Before 是 baselineRef 里的真实站点：首页导航/页脚还没有「下载」入口，/download 是真实 404。",
            "下载页的 Windows/macOS/Android 链接指向站内 /downloads/，镜像文件按页面标注的 SHA-256 放置；GitHub Release 是备用入口。",
        ],
        "files": files,
    }
    manifest_path = out_dir / "capture.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("采集清单：%s" % manifest_path)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="采集 Vibe Pay 签约站点的首页 / 商品页 / 支付页 / 下载页截图。"
    )
    parser.add_argument("--serve", action="store_true", help="只起本地预览服务（不发截图）")
    parser.add_argument("--base-url", default="", help="已部署站点的根地址；省略则自动起本地服务")
    parser.add_argument("--out", default=str(OUT_DIR), help="截图输出目录（默认 docs/vibepay-evidence）")
    parser.add_argument("--host", default="127.0.0.1", help="本地预览监听地址")
    parser.add_argument("--port", type=int, default=0, help="本地预览端口（0 = 自动）")
    parser.add_argument("--chrome", default="", help="Chrome/Edge 可执行文件路径（默认自动探测）")
    parser.add_argument(
        "--baseline-ref",
        default=DEFAULT_BASELINE_REF,
        help="Before 截图用的 git ref（默认 %s）" % DEFAULT_BASELINE_REF,
    )
    args = parser.parse_args(argv)
    if args.serve:
        return serve(args)
    return capture(args)


if __name__ == "__main__":
    sys.exit(main())
