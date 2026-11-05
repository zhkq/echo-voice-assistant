#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集 Phase 3（账号 / 套餐 / 真实支付页）的 Before/After 截图。

Before：`integration/billing-vibepay` 基线（静态占位支付页、没有 /account）。
After：当前工作树 + 一个**本地 mock 能力后端**（订单/权益/支付状态都是演示数据，
        不是真实付款）。真实支付宝沙箱付款由 Lead 用沙箱凭据实测，本脚本不伪造。

用法（仓库根目录）：

    python scripts/capture-phase3-evidence.py
    # 产物：docs/vibepay-evidence/phase3/*.png + capture.json + README.md

依赖：Python + `playwright` + 本机 Chrome（与 Phase 2 截图脚本一致）。
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
OUT_DIR = ROOT / "docs" / "vibepay-evidence" / "phase3"
DEFAULT_BASELINE_REF = "origin/integration/billing-vibepay"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 375, "height": 812}
TABLET = {"width": 768, "height": 1024}

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
    """干净 URL + 不列目录 + 不缓存。子类可叠 /v1/* mock。"""

    root = SITE_DIR
    api_enabled = False
    state = None

    def translate_path(self, path):  # noqa: D401 - stdlib hook
        parsed = urlsplit(path)
        clean = unquote(parsed.path)
        if clean in ("", "/"):
            rel = "index.html"
        elif clean in ("/pricing", "/pricing/"):
            rel = "pricing.html"
        elif clean in ("/account", "/account/"):
            rel = "account.html"
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
        if os.environ.get("PHASE3_EVIDENCE_VERBOSE"):
            super().log_message(fmt, *args)

    # ---- mock API ------------------------------------------------------

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):  # noqa: N802 - stdlib hook
        if self.api_enabled and urlsplit(self.path).path.startswith("/v1/"):
            return self._api("GET")
        return super().do_GET()

    def do_POST(self):  # noqa: N802 - stdlib hook
        if self.api_enabled and urlsplit(self.path).path.startswith("/v1/"):
            return self._api("POST")
        self.send_error(404, "Not Found")

    def _api(self, method):
        path = urlsplit(self.path).path.rstrip("/")
        state = self.state
        if state is None:
            return self._json(500, {"code": "mock_not_ready", "message": "mock 未初始化"})

        if method == "GET" and path == "/v1/billing/plans":
            return self._json(200, {
                "plans": state["plans"],
                "trial": {"enabled": True, "days": 7},
                "payment": {"provider": "alipay", "configured": True, "sandbox": True},
                "serverTime": time.time(),
            })
        if method == "GET" and path == "/v1/account/me":
            return self._json(200, state["me"]())
        if method == "GET" and path == "/v1/billing/entitlement":
            return self._json(200, state["entitlement"]())
        if path == "/v1/billing/orders":
            if method == "GET":
                return self._json(200, {"orders": state["order_list"]()})
            payload = self._body()
            return self._json(200, state["create_order"](str(payload.get("planCode") or "monthly")))
        if path.startswith("/v1/billing/orders/"):
            parts = path.split("/")
            # /v1/billing/orders/<id>[/sync|/cancel]
            if len(parts) < 5:
                return self._json(404, {"code": "not_found", "message": "not found"})
            order_id = parts[4]
            action = parts[5] if len(parts) > 5 else ""
            if method == "GET" and not action:
                order = state["orders"].get(order_id)
                if order is None:
                    return self._json(404, {"code": "order_not_found", "message": "订单不存在"})
                return self._json(200, order)
            if method == "POST" and action == "sync":
                order = state["orders"].get(order_id)
                if order is None:
                    return self._json(404, {"code": "order_not_found", "message": "订单不存在"})
                if order_id == "ord-demo-sync" and order["status"] == "paying":
                    order.update({"status": "paid", "paidAt": time.time()})
                return self._json(200, order)
            if method == "POST" and action == "cancel":
                order = state["orders"].get(order_id)
                if order is None:
                    return self._json(404, {"code": "order_not_found", "message": "订单不存在"})
                order.update({"status": "closed"})
                return self._json(200, {"ok": True, "orderId": order_id, "status": "closed"})
            return self._json(404, {"code": "not_found", "message": "not found"})
        if method == "POST" and path in ("/v1/account/login", "/v1/account/register"):
            payload = self._body()
            user = state["user"]
            return self._json(200, {
                "userId": user["userId"],
                "user": user,
                "userToken": "demo-user-token",
                "expiresAt": time.time() + 30 * 86400,
                "expiresIn": 30 * 86400,
                "devices": [],
                "trial": path.endswith("register"),
            })
        if method == "POST" and path == "/v1/account/logout":
            return self._json(200, {"ok": True, "tokenVersion": 2})
        return self._json(404, {"code": "not_found", "message": "mock 没有这个端点"})


def _make_state():
    now = time.time()
    plans = json.loads((SITE_DIR / "assets" / "plans.json").read_text(encoding="utf-8"))["plans"]
    user = {"userId": "usr-demo", "name": "张三", "employeeNo": "E001",
            "phone": "", "status": "active", "createdAt": now - 40 * 86400}
    entitlement = {
        "status": "active", "plan": "monthly", "paidUntil": now + 20 * 86400,
        "dailyMinutes": 120.0, "dailyMinutesLeft": 87.5,
        "ttsCharsLeft": 32100, "qps": 1, "maxDevices": 3, "maxConcurrent": 1,
        "devices": 1, "expiringSoon": False,
    }

    def order(order_id, status, *, paid_at=0.0, created=None, plan="monthly"):
        plan_row = next((p for p in plans if p["code"] == plan), plans[0])
        return {
            "orderId": order_id, "planCode": plan, "planName": plan_row["name"],
            "days": plan_row["days"], "amountCents": plan_row["priceCents"],
            "status": status, "createdAt": created or (now - 7200),
            "expiresAt": now + 900, "paidAt": paid_at,
            "payUrl": "https://sandbox.example/alipay/cashier?order=" + order_id,
            "qrCode": "", "sandbox": True,
        }

    orders = {
        "ord-demo-paying": order("ord-demo-paying", "paying"),
        "ord-demo-sync": order("ord-demo-sync", "paying"),
        "ord-demo-paid": order("ord-demo-paid", "paid", paid_at=now - 3600),
        "ord-demo-closed": order("ord-demo-closed", "closed", created=now - 2 * 86400),
        "ord-demo-refunded": order("ord-demo-refunded", "refunded", paid_at=now - 5 * 86400),
        "ord-demo-timeout": order("ord-demo-timeout", "paying", created=now - 2 * 86400),
    }
    orders["ord-demo-timeout"]["expiresAt"] = now - 3600
    state = {
        "plans": plans,
        "user": user,
        "orders": orders,
    }

    def me():
        return {**user, "user": user, "entitlement": dict(entitlement),
                "devices": [{"clientId": "cli-demo", "deviceName": "本机",
                             "boundAt": now - 10 * 86400, "lastSeen": now - 60,
                             "status": "active"}]}

    def order_list():
        return sorted(orders.values(), key=lambda o: o["createdAt"], reverse=True)

    def create_order(plan_code):
        seq = len(orders) + 1
        row = order("ord-demo-%02d" % seq, "paying", created=now, plan=plan_code)
        orders[row["orderId"]] = row
        return row

    state.update({
        "me": me,
        "entitlement": lambda: dict(entitlement),
        "order_list": order_list,
        "create_order": create_order,
    })
    return state


def start_server(root: Path, *, api: bool = False, host: str = "127.0.0.1",
                 port: int = 0):
    attrs = {"root": root, "api_enabled": api, "state": _make_state() if api else None}
    handler = type("BoundPhase3Handler", (SiteHandler,), attrs)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="phase3-site", daemon=True)
    thread.start()
    return server


def _launch_browser(playwright, chrome_path: str | None):
    attempts = []
    if chrome_path:
        attempts.append({"executable_path": chrome_path})
    else:
        attempts.extend([{"channel": "chrome"}, {"channel": "msedge"}, {"channel": "chromium"}])
    attempts.append({})
    errors = []
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(headless=True, **kwargs)
        except Exception as exc:
            errors.append("%s: %s" % (kwargs, exc))
    raise RuntimeError("启动浏览器失败：\n" + "\n".join(errors))


def _wait_ready(page, timeout: int = 20000) -> None:
    page.wait_for_function(
        "() => ['1', 'error'].includes(document.documentElement.getAttribute('data-ready'))",
        timeout=timeout,
    )
    state = page.evaluate("() => document.documentElement.getAttribute('data-ready')")
    if state != "1":
        detail = page.inner_text("body")[:500].replace("\n", " ")
        raise RuntimeError("页面渲染失败（data-ready=%s）：%s" % (state, detail))
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.wait_for_timeout(180)


def _assert_no_overflow(page, label: str) -> None:
    size = page.evaluate(
        "() => ({ scroll: document.documentElement.scrollWidth, view: window.innerWidth,"
        " body: document.body.scrollWidth })"
    )
    if size["scroll"] > size["view"] + 1 or size["body"] > size["view"] + 1:
        raise RuntimeError("%s 横向溢出：scrollWidth=%s body=%s view=%s"
                           % (label, size["scroll"], size["body"], size["view"]))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=str(ROOT), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def _session_script() -> str:
    return (
        "localStorage.setItem('echo.userToken', 'demo-user-token');"
        "localStorage.setItem('echo.user', JSON.stringify("
        "{userId:'usr-demo',name:'张三',employeeNo:'E001'}));"
    )


def _capture(context, base_url: str, out_dir: Path, spec: dict, files: list) -> None:
    page = context.new_page()
    if spec.get("session"):
        page.add_init_script(_session_script())
    try:
        page.goto(base_url + spec["path"], wait_until="load")
        _wait_ready(page)
        for selector in spec.get("wait_for", ()):
            page.wait_for_selector(selector, state="visible", timeout=15000)
        if spec.get("wait_text"):
            page.wait_for_function(
                "txt => document.body.innerText.includes(txt)", arg=spec["wait_text"],
                timeout=15000)
        _assert_no_overflow(page, spec["name"])
        target = out_dir / spec["file"]
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        files.append({
            "page": spec["name"],
            "url": base_url + spec["path"],
            "title": page.title(),
            "viewport": spec["viewport"],
            "file": target.name,
            "sha256": _sha256(target),
            "note": spec.get("note", ""),
        })
        print("已采集 %-16s %s" % (spec["name"], target))
    finally:
        page.close()


def _capture_baseline(context, ref: str, out_dir: Path, files: list) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="echo-phase3-before-"))
    server = None
    try:
        try:
            blob = subprocess.check_output(
                ["git", "archive", ref, "site"], cwd=str(ROOT), stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "replace").strip() or str(exc)
            raise RuntimeError("无法从 %s 取基线站点：%s" % (ref, detail)) from exc
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            try:
                tar.extractall(tmp, filter="data")
            except TypeError:
                tar.extractall(tmp)
        root = tmp / "site"
        if not (root / "pricing.html").is_file():
            raise RuntimeError("%s 里没有 site/pricing.html" % ref)
        server = start_server(root, api=False)
        host, port = server.server_address[:2]
        base = "http://%s:%s" % (host, port)
        specs = (
            {"name": "before-home", "path": "/", "file": "before-home.png",
             "viewport": DESKTOP,
             "note": "基线 %s：首页（导航还没有「我的」/「我的账户」入口）。" % ref},
            {"name": "before-download", "path": "/download", "file": "before-download.png",
             "viewport": DESKTOP,
             "note": "基线 %s：下载页（导航/页脚还没有账号入口）。" % ref},
            {"name": "before-pricing", "path": "/pricing", "file": "before-pricing.png",
             "viewport": DESKTOP, "wait_for": ("#planCards",),
             "note": "基线 %s：套餐页（静态套餐数据，购买入口指向 /pay?plan=monthly）。" % ref},
            {"name": "before-pay", "path": "/pay?plan=monthly", "file": "before-pay.png",
             "viewport": DESKTOP, "wait_for": ("#payLayout",),
             "note": "基线 %s：支付页（静态示例订单 + 二维码占位）。" % ref},
        )
        for spec in specs:
            _capture(context, base, out_dir, spec, files)

        page = context.new_page()
        response = page.goto(base + "/account", wait_until="load")
        status = response.status if response is not None else None
        if status != 404:
            raise RuntimeError("基线 %s 的 /account 应为 404，实际 %s" % (ref, status))
        target = out_dir / "before-account-404.png"
        page.screenshot(path=str(target), full_page=True, animations="disabled")
        files.append({
            "page": "before-account-404", "url": base + "/account", "title": page.title(),
            "viewport": DESKTOP, "file": target.name, "sha256": _sha256(target),
            "status": status,
            "note": "基线 %s 没有账号页；这是真实请求 /account 的 404。" % ref,
        })
        print("已采集 %-16s %s" % ("before-account", target))
        page.close()
        return {"ref": ref, "commit": _git("rev-parse", ref), "files": [
            item["file"] for item in files if item["page"].startswith("before-")
        ]}
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def capture(args) -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError  # noqa: F401
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print("缺少 Playwright：%s" % exc, file=sys.stderr)
        print("请先 pip install playwright；脚本使用系统 Chrome，无需下载浏览器。",
              file=sys.stderr)
        return 2

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    server = start_server(SITE_DIR, api=True, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    base_url = "http://%s:%s" % (host, port)
    print("Phase 3 mock 后端 + 静态站：%s" % base_url)

    files = []
    browser_name = "unknown"
    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, args.chrome)
            browser_name = "%s %s" % (browser.browser_type.name, browser.version)
            comments = {
                "viewport": DESKTOP, "session": True,
            }
            desktop = browser.new_context(
                viewport=DESKTOP, device_scale_factor=1, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")

            specs = (
                {"name": "after-home", "path": "/", "file": "after-home.png",
                 "viewport": DESKTOP,
                 "note": "After：首页（导航新增「我的」与登录 / 我的账户入口）。"},
                {"name": "after-download", "path": "/download", "file": "after-download.png",
                 "viewport": DESKTOP,
                 "note": "After：下载页（导航/页脚新增账号入口）。"},
                {"name": "after-pricing", "path": "/pricing", "file": "after-pricing.png",
                 "viewport": DESKTOP, "wait_for": ("#planCards .plan",),
                 "note": "After：套餐从 GET /v1/billing/plans 读取；购买入口要求登录。"},
                {"name": "after-account", "path": "/account", "file": "after-account.png",
                 "viewport": DESKTOP, "wait_for": ("#dashboardView",),
                 "note": "After：登录后的账户页（权益 + 最近订单，数据来自 mock API）。"},
                {"name": "after-pay", "path": "/pay?order=ord-demo-paying",
                 "file": "after-pay.png", "viewport": DESKTOP,
                 "wait_for": ("#payLayout", "#openCashier"),
                 "note": "After：真实订单驱动的支付页（等待支付，服务端 payUrl，测试环境标注）。"},
                {"name": "after-paid", "path": "/pay?order=ord-demo-paid",
                 "file": "after-paid.png", "viewport": DESKTOP,
                 "wait_for": ("#payResult",), "wait_text": "到期时间",
                 "note": "After：支付成功状态（权益来自 /v1/billing/entitlement）。"},
                {"name": "after-closed", "path": "/pay?order=ord-demo-closed",
                 "file": "after-closed.png", "viewport": DESKTOP,
                 "wait_for": ("#payResult",),
                 "note": "After：订单关闭状态（失败/取消共用这一页）。"},
                {"name": "after-timeout", "path": "/pay?order=ord-demo-timeout",
                 "file": "after-timeout.png", "viewport": DESKTOP,
                 "wait_for": ("#payResult",), "wait_text": "订单已超时",
                 "note": "After：支付超时状态（已过 expiresAt）。"},
            )
            for spec in specs:
                spec = dict(spec)
                spec["session"] = True
                spec["viewport"] = DESKTOP
                _capture(desktop, base_url, out_dir, spec, files)

            login = browser.new_context(
                viewport=DESKTOP, device_scale_factor=1, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            _capture(login, base_url, out_dir, {
                "name": "after-login", "path": "/account", "file": "after-login.png",
                "viewport": DESKTOP, "wait_for": ("#authView",),
                "note": "After：未登录时的登录 / 注册入口。"}, files)
            login.close()

            mobile = browser.new_context(
                viewport=MOBILE, device_scale_factor=2, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            for spec in (
                {"name": "after-account-mobile", "path": "/account",
                 "file": "after-account-mobile.png", "viewport": MOBILE,
                 "wait_for": ("#dashboardView",)},
                {"name": "after-pay-mobile", "path": "/pay?order=ord-demo-paying",
                 "file": "after-pay-mobile.png", "viewport": MOBILE,
                 "wait_for": ("#payLayout", "#openCashier")},
            ):
                spec = dict(spec)
                spec["session"] = True
                _capture(mobile, base_url, out_dir, spec, files)
            mobile.close()

            tablet = browser.new_context(
                viewport=TABLET, device_scale_factor=1, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            for path, name in (("/pricing", "pricing"), ("/account", "account"),
                               ("/pay?order=ord-demo-paying", "pay")):
                page = tablet.new_page()
                page.add_init_script(_session_script())
                page.goto(base_url + path, wait_until="load")
                _wait_ready(page)
                _assert_no_overflow(page, name + "-tablet-check")
                page.close()
            tablet.close()

            baseline = _capture_baseline(desktop, args.baseline_ref, out_dir, files)
            desktop.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    manifest = {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseUrl": base_url,
        "browser": browser_name,
        "baseline": baseline,
        "mode": "mock backend",
        "notes": [
            "After 使用本地 mock 能力后端（订单/权益/支付状态为演示数据），不是真实付款。",
            "Before 是 %s 的真实静态站渲染；/account 的真实响应是 404。" % args.baseline_ref,
            "真实支付宝沙箱 10.00 元付款由 Lead 在 2026-11-05 用沙箱 app 实测；"
            "本目录不冒充那次真机证据。",
            "带地址栏的公网 HTTPS 截图需部署完成后补采，本脚本不伪造地址栏。",
        ],
        "files": files,
    }
    (out_dir / "capture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已写 %s" % (out_dir / "capture.json"))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--chrome", default=os.environ.get("PHASE3_CHROME", ""))
    return capture(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
