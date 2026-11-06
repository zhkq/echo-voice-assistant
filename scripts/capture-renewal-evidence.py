#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集「续费规则 / 预计到期」的 Before/After 截图（issue #13）。

Before：本 ticket 的基线 `2d07bc3`（账号页与支付页已有，但支付前不显示
        “当前到期 / 本次续费 / 支付后预计到期”，权益卡也没有续费规则）。
After ：当前工作树（服务端下发 `projectedPaidUntil / currentPaidUntil /
        renewalRule`，页面只负责显示）。

两侧都用**同一个**本地 mock 能力后端、同一份订单/权益数据、同一视口
（1440×900 桌面 + 375×812 移动）、浅色、中文；不是真实扣款。

用法（仓库根目录）：

    python scripts/capture-renewal-evidence.py
    # 产物：docs/billing-renewal-evidence/*.png + capture.json + README.md

依赖：Python + `playwright` + 本机 Chrome。
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
OUT_DIR = ROOT / "docs" / "billing-renewal-evidence"
DEFAULT_BASELINE_REF = "2d07bc3"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 375, "height": 812}

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
    root = SITE_DIR
    api_enabled = True

    def translate_path(self, path):
        clean = unquote(urlsplit(path).path)
        rel = {
            "": "index.html", "/": "index.html",
            "/pricing": "pricing.html", "/pricing/": "pricing.html",
            "/account": "account.html", "/account/": "account.html",
            "/pay": "pay.html", "/pay/": "pay.html",
            "/download": "download.html", "/download/": "download.html",
        }.get(clean, clean.lstrip("/"))
        target = (Path(self.root) / rel).resolve()
        root = Path(self.root).resolve()
        if target != root and root not in target.parents:
            return str(root / "__not_found__")
        return str(target)

    def list_directory(self, path):
        self.send_error(404, "Not Found")
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("RENEWAL_EVIDENCE_VERBOSE"):
            super().log_message(fmt, *args)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if urlsplit(self.path).path.startswith("/v1/"):
            return self._api("GET")
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path.startswith("/v1/"):
            return self._api("POST")
        self.send_error(404, "Not Found")

    def _api(self, method):
        path = urlsplit(self.path).path.rstrip("/")
        state = _state()
        if method == "GET" and path == "/v1/billing/plans":
            return self._json(200, {
                "plans": state["plans"], "trial": {"enabled": True, "days": 7},
                "payment": {"provider": "mock", "configured": True, "sandbox": True},
            })
        if method == "GET" and path == "/v1/account/me":
            return self._json(200, state["me"])
        if method == "GET" and path == "/v1/billing/entitlement":
            return self._json(200, state["entitlement"])
        if path == "/v1/billing/orders":
            if method == "GET":
                return self._json(200, {"orders": state["order_list"]})
            return self._json(200, state["orders"]["ord-demo-paying"])
        if path.startswith("/v1/billing/orders/"):
            parts = path.split("/")
            order_id = parts[4] if len(parts) > 4 else ""
            order = state["orders"].get(order_id)
            if order is None:
                return self._json(404, {"code": "order_not_found",
                                        "message": "订单不存在"})
            return self._json(200, order)
        return self._json(404, {"code": "not_found", "message": "mock 没有这个端点"})


def _rules():
    return {
        "renewalRule": ("期内续费从当前到期日顺延；过期或退款后从支付成功时间起算；"
                        "试用不叠加；退款按剩余已支付订单重算。"),
        "planChangeRule": ("暂不支持升级/降级：有有效付费权益时只能续费同一套餐；"
                           "等当前套餐到期后，或联系管理员处理。"),
        "refundRule": "退款按剩余已支付订单重算到期时间；已消费的额度不退。",
        "autoRenew": False,
    }


def _state():
    now = time.time()
    plans = json.loads((SITE_DIR / "assets" / "plans.json").read_text(encoding="utf-8"))["plans"]
    monthly = next((p for p in plans if p["code"] == "monthly"), plans[0])
    entitlement = {
        "status": "active", "plan": "monthly", "planName": monthly["name"],
        "paidUntil": now + 20 * 86400, "renewalDays": 30,
        "dailyMinutes": 120.0, "dailyMinutesLeft": 87.5,
        "ttsCharsLeft": 32100, "qps": 1, "maxDevices": 3, "maxConcurrent": 1,
        "devices": 1, "expiringSoon": False,
    }
    entitlement.update(_rules())

    def order(order_id, status, *, paid_at=0.0, created=None, plan=monthly,
              projected=None, current=None, basis="paid_until"):
        row = {
            "orderId": order_id, "planCode": plan["code"], "planName": plan["name"],
            "days": plan["days"], "renewalDays": plan["days"],
            "amountCents": plan["priceCents"], "status": status,
            "createdAt": created or (now - 7200), "expiresAt": now + 900,
            "paidAt": paid_at,
            "payUrl": "https://sandbox.example/alipay/cashier?order=" + order_id,
            "qrCode": "", "sandbox": True,
            "currentPaidUntil": entitlement["paidUntil"] if current is None else current,
            "projectedPaidUntil": (now + 50 * 86400 if projected is None else projected),
            "renewalBasis": basis,
        }
        row.update(_rules())
        return row

    orders = {
        "ord-demo-paying": order("ord-demo-paying", "paying"),
        "ord-demo-paid": order("ord-demo-paid", "paid", paid_at=now - 3600,
                               projected=entitlement["paidUntil"]),
        "ord-demo-refunded": order("ord-demo-refunded", "refunded",
                                   paid_at=now - 5 * 86400,
                                   projected=0, current=entitlement["paidUntil"],
                                   basis="not_applicable"),
        "ord-demo-closed": order("ord-demo-closed", "closed", created=now - 2 * 86400,
                                 projected=0, current=entitlement["paidUntil"],
                                 basis="not_applicable"),
    }
    user = {"userId": "usr-demo", "name": "张三", "employeeNo": "E001",
            "phone": "", "status": "active", "createdAt": now - 40 * 86400}
    me = {**user, "user": user, "entitlement": dict(entitlement),
          "devices": [{"clientId": "cli-demo", "deviceName": "本机",
                       "boundAt": now - 10 * 86400, "lastSeen": now - 60,
                       "status": "active"}]}
    return {
        "plans": plans, "entitlement": entitlement, "orders": orders,
        "order_list": sorted(orders.values(), key=lambda o: o["createdAt"], reverse=True),
        "me": me,
    }


def start_server(root: Path, port: int = 0):
    handler = type("RenewalHandler", (SiteHandler,), {"root": root})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _launch_browser(playwright, chrome_path: str | None):
    attempts = ([{"executable_path": chrome_path}] if chrome_path
                else [{"channel": "chrome"}, {"channel": "msedge"},
                      {"channel": "chromium"}])
    attempts.append({})
    errors = []
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:
            errors.append("%s: %s" % (kwargs, exc))
    raise RuntimeError("启动浏览器失败：\n" + "\n".join(errors))


def _session_script() -> str:
    return (
        "localStorage.setItem('echo.userToken', 'demo-user-token');"
        "localStorage.setItem('echo.user', JSON.stringify("
        "{userId:'usr-demo',name:'张三',employeeNo:'E001'}));"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture(context, base: str, out: Path, name: str, path: str,
             files: list, **kwargs) -> None:
    page = context.new_page()
    try:
        page.add_init_script(_session_script())
        page.goto(base + path, wait_until="load")
        page.wait_for_function(
            "() => ['1','error'].includes(document.documentElement.getAttribute('data-ready'))",
            timeout=20000)
        if page.evaluate("() => document.documentElement.getAttribute('data-ready')") != "1":
            raise RuntimeError("%s 渲染失败：%s" % (name, page.inner_text("body")[:300]))
        for selector in kwargs.get("wait_for", ()):
            page.wait_for_selector(selector, state="visible", timeout=15000)
        if kwargs.get("wait_text"):
            page.wait_for_function("txt => document.body.innerText.includes(txt)",
                                   arg=kwargs["wait_text"], timeout=15000)
        size = page.evaluate(
            "() => ({s: document.documentElement.scrollWidth, v: window.innerWidth})")
        if size["s"] > size["v"] + 1:
            raise RuntimeError("%s 横向溢出：%s > %s" % (name, size["s"], size["v"]))
        target = out / (name + ".png")
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        files.append({
            "page": name, "url": base + path, "viewport": kwargs["viewport"],
            "file": target.name, "sha256": _sha256(target),
            "note": kwargs.get("note", ""),
        })
        print("已采集 %-26s %s" % (name, target.name))
    finally:
        page.close()


def _extract_ref(ref: str, tmp: Path) -> Path:
    blob = subprocess.check_output(
        ["git", "archive", ref, "site"], cwd=str(ROOT), stderr=subprocess.PIPE)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        try:
            tar.extractall(tmp, filter="data")
        except TypeError:
            tar.extractall(tmp)
    root = tmp / "site"
    if not (root / "pay.html").is_file():
        raise RuntimeError("%s 里没有 site/pay.html" % ref)
    return root


def capture(args) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print("缺少 Playwright：%s" % exc, file=sys.stderr)
        return 2

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    files: list = []
    tmp = Path(tempfile.mkdtemp(prefix="echo-renewal-before-"))
    before_server = after_server = None
    browser_name = "unknown"
    try:
        before_root = _extract_ref(args.baseline_ref, tmp)
        before_server = start_server(before_root, args.port)
        after_server = start_server(SITE_DIR, 0)
        before_base = "http://127.0.0.1:%s" % before_server.server_address[1]
        after_base = "http://127.0.0.1:%s" % after_server.server_address[1]
        print("Before %s（%s）  After %s" % (args.baseline_ref, before_base, after_base))

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, args.chrome)
            browser_name = "%s %s" % (browser.browser_type.name, browser.version)

            desktop = browser.new_context(
                viewport=DESKTOP, device_scale_factor=1, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            _capture(desktop, before_base, out, "before-pay",
                     "/pay?order=ord-demo-paying", files, viewport=DESKTOP,
                     wait_for=("#payLayout",),
                     note="基线 2d07bc3：支付页只有订单摘要，没有当前到期/预计到期。")
            _capture(desktop, after_base, out, "after-pay",
                     "/pay?order=ord-demo-paying", files, viewport=DESKTOP,
                     wait_for=("#orderProjectedUntil",),
                     wait_text="支付后预计到期",
                     note="本分支：支付前展示当前到期、本次续费天数、支付后预计到期与服务端规则句。")
            _capture(desktop, before_base, out, "before-account", "/account",
                     files, viewport=DESKTOP, wait_for=("#dashboardView",),
                     note="基线 2d07bc3：账户权益卡没有续费规则说明。")
            _capture(desktop, after_base, out, "after-account", "/account",
                     files, viewport=DESKTOP, wait_for=("#entitlementRule",),
                     wait_text="期内续费",
                     note="本分支：权益卡显示服务端下发的 renewalRule / planChangeRule / refundRule。")
            _capture(desktop, after_base, out, "after-refunded-order",
                     "/pay?order=ord-demo-refunded", files, viewport=DESKTOP,
                     wait_for=("#payResult",), wait_text="订单已退款",
                     note="本分支：已退款订单不再显示“预计到期”；退款按剩余订单重算。")
            desktop.close()

            mobile = browser.new_context(
                viewport=MOBILE, device_scale_factor=2, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            _capture(mobile, before_base, out, "before-pay-mobile",
                     "/pay?order=ord-demo-paying", files, viewport=MOBILE,
                     wait_for=("#payLayout",),
                     note="基线 2d07bc3 移动端支付页。")
            _capture(mobile, after_base, out, "after-pay-mobile",
                     "/pay?order=ord-demo-paying", files, viewport=MOBILE,
                     wait_for=("#orderProjectedUntil",),
                     note="本分支移动端：当前到期 / 本次续费 / 支付后预计到期。")
            _capture(mobile, before_base, out, "before-account-mobile", "/account",
                     files, viewport=MOBILE, wait_for=("#dashboardView",),
                     note="基线 2d07bc3 移动端权益卡。")
            _capture(mobile, after_base, out, "after-account-mobile", "/account",
                     files, viewport=MOBILE, wait_for=("#entitlementRule",),
                     note="本分支移动端：权益卡规则说明。")
            mobile.close()
            browser.close()
    finally:
        for server in (before_server, after_server):
            if server is not None:
                server.shutdown()
                server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    manifest = {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline": {"ref": args.baseline_ref,
                     "commit": subprocess.check_output(
                         ["git", "rev-parse", args.baseline_ref], cwd=str(ROOT),
                         text=True).strip()},
        "browser": browser_name,
        "mode": "mock backend",
        "notes": [
            "Before 与 After 使用同一份 mock 订单/权益数据、同一视口、浅色、中文。",
            "After 的订单/权益是 mock 数据，不是真实扣款；真实支付宝沙箱付款不在本证据内。",
            "Before=2d07bc3 是 ticket 分支基线（PR #12 的账号页/支付页），不是 main。",
        ],
        "files": files,
    }
    (out / "capture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已写 %s" % (out / "capture.json"))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--chrome", default=os.environ.get("RENEWAL_CHROME", ""))
    return capture(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
