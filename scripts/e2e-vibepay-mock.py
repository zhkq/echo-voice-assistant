#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3 端到端冒烟：真实后端 + 真实站点 + Mock 支付。

这条脚本不伪造接口：它起一个**真的** `server.main`（临时 state/auth 库、mock 支付、
`invite.required=false`），把 `site/` 静态站和 `/v1/*` 反代到同一个源，再用系统 Chrome：

  注册 → 账户页看到试用权益 → 套餐页下单 → 支付页读真实订单 → 调 mock 支付端点
  → 刷新/sync → 支付成功 + 权益 → 查看订单 → 退出再登录 → 再下一单并取消

它不是沙箱真机付款（没有沙箱凭据）；真实支付宝沙箱 10.00 元由 Lead 用沙箱 app 实测。
本脚本用于卡住"官网 UI 与真实后端契约漂移"，产物在 `docs/vibepay-evidence/phase3/`。

用法：python scripts/e2e-vibepay-mock.py
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"
OUT_DIR = ROOT / "docs" / "vibepay-evidence" / "phase3"
DESKTOP = {"width": 1440, "height": 900}


class SiteProxy(SimpleHTTPRequestHandler):
    """静态站 + `/v1/*` 反代到本机真实后端（同源，免 CORS）。"""

    root = SITE_DIR
    backend_port = 0

    def translate_path(self, path):
        clean = unquote(urlsplit(path).path)
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

    def list_directory(self, path):
        self.send_error(404, "Not Found")
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("E2E_VERBOSE"):
            super().log_message(fmt, *args)

    def _proxy(self):
        parsed = urlsplit(self.path)
        body = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length:
            body = self.rfile.read(length)
        headers = {}
        for key in ("authorization", "content-type", "accept"):
            value = self.headers.get(key)
            if value:
                headers[key.title()] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.backend_port, timeout=20)
        try:
            conn.request(self.command, parsed.path + (("?" + parsed.query) if parsed.query else ""),
                         body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            payload = json.dumps({"code": "proxy_error", "message": str(exc)},
                                 ensure_ascii=False).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            conn.close()

    def do_GET(self):
        if urlsplit(self.path).path.startswith("/v1/"):
            return self._proxy()
        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path.startswith("/v1/"):
            return self._proxy()
        self.send_error(404, "Not Found")


def _wait_health(port: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/v1/health" % port,
                                        timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(0.4)
    raise RuntimeError("后端没起来：%s" % last)


def _start_backend(tmp: Path, port: int) -> subprocess.Popen:
    cfg = {
        "server": {
            "listen": "127.0.0.1:%d" % port,
            "admin_listen": "",
            "state_root": str(tmp / "state"),
            "local_pair": False,
        },
        "tmp": {"root": str(tmp / "tmp"), "ttl_hours": 4},
        "auth": {
            "enabled": True,
            "mode": "jwt",
            "jwt_secret": "0123456789abcdef0123456789abcdef",
            "db": str(tmp / "auth.db"),
            "clock_skew_s": 0,
        },
        "billing": {"enabled": True, "timezone": "Asia/Shanghai"},
        "payment": {"provider": "mock"},
        "invite": {"required": False},
        "models": {"device": "cpu"},
    }
    path = tmp / "server.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    log = open(tmp / "backend.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--config", str(path),
         "--listen", "127.0.0.1:%d" % port, "--log-level", "warning"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
    )
    proc._e2e_log = log  # type: ignore[attr-defined]
    return proc


def _stop_backend(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    log = getattr(proc, "_e2e_log", None)
    if log is not None:
        try:
            log.close()
        except Exception:
            pass


def _mock_pay(port: int, order_id: str) -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:%d/v1/billing/mock/pay/%s" % (port, order_id),
        data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_text(page, selector: str, text: str, timeout: int = 15000) -> None:
    page.wait_for_function(
        "([sel, txt]) => { const el = document.querySelector(sel);"
        " return !!el && el.innerText.includes(txt); }",
        arg=[selector, text], timeout=timeout)


def _assert(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run(args) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print("缺少 Playwright：%s" % exc, file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="echo-phase3-e2e-"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    backend = None
    proxy = None
    steps = []
    try:
        backend_port = args.backend_port
        backend = _start_backend(tmp, backend_port)
        health = _wait_health(backend_port)
        _assert(health.get("ok") is True, "后端健康检查不是 ok：%s" % health)
        steps.append({"step": "backend_started", "port": backend_port,
                      "models": health.get("models")})

        handler = type("BoundSiteProxy", (SiteProxy,), {"backend_port": backend_port})
        proxy = ThreadingHTTPServer(("127.0.0.1", args.site_port), handler)
        site_port = proxy.server_address[1]
        threading.Thread(target=proxy.serve_forever, name="e2e-site", daemon=True).start()
        base = "http://127.0.0.1:%d" % site_port

        def launch(playwright):
            attempts = ([{"executable_path": args.chrome}] if args.chrome else
                        [{"channel": "chrome"}, {"channel": "msedge"}, {"channel": "chromium"}])
            attempts.append({})
            for kwargs in attempts:
                try:
                    return playwright.chromium.launch(headless=True, **kwargs)
                except Exception:
                    continue
            raise RuntimeError("启动浏览器失败")

        with sync_playwright() as playwright:
            browser = launch(playwright)
            context = browser.new_context(
                viewport=DESKTOP, device_scale_factor=1, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            page = context.new_page()
            page.on("dialog", lambda dialog: dialog.accept())
            page.on("console", lambda msg: print("BROWSER", msg.type, msg.text, flush=True))
            page.on("pageerror", lambda err: print("PAGEERROR", err, flush=True))

            print("STEP register", flush=True)
            # 1) 注册（invite.required=false）
            page.goto(base + "/account", wait_until="load")
            page.wait_for_selector("#authView", state="visible")
            page.click('#tabRegister')
            page.fill("#authName", "张三")
            page.fill("#authEmployeeNo", "E2E001")
            page.click("#authSubmit")
            page.wait_for_selector("#dashboardView", state="visible", timeout=20000)
            body = page.inner_text("#dashboardView")
            _assert("7 天试用" in body or "尚未开通" in body, "注册后没有权益区块：%s" % body[:200])
            steps.append({"step": "register", "entitlement": body.splitlines()[:4]})
            page.screenshot(path=str(OUT_DIR / "e2e-account.png"),
                            full_page=True, animations="disabled")

            print("STEP pricing", flush=True)
            # 2) 套餐页下单（登录态）
            page.goto(base + "/pricing", wait_until="load")
            page.wait_for_selector('#planCards [data-buy="monthly"]', state="visible")
            page.click('#planCards [data-buy="monthly"]')
            page.wait_for_url(re.compile(r"/pay\?order="), timeout=20000)
            order_id = urllib.parse.parse_qs(urlsplit(page.url).query).get("order", [""])[0]
            _assert(order_id.startswith("ord-"), "下单后 URL 没有订单号：%s" % page.url)
            page.wait_for_selector("#payLayout", state="visible", timeout=20000)
            _assert("¥10.00" in page.inner_text("#payLayout"), "支付页金额不是 ¥10.00")
            _assert(order_id in page.inner_text("#payLayout"), "支付页订单号不是服务端订单")
            steps.append({"step": "create_order", "orderId": order_id})

            print("STEP pay", flush=True)
            # 3) 调 mock 支付端点，再刷新/sync
            paid = _mock_pay(backend_port, order_id)
            _assert(paid.get("status") == "paid", "mock 支付没有成功：%s" % paid)
            page.click("#refreshOrder")
            _wait_text(page, "#resultTitle", "支付成功")
            _wait_text(page, "#resultRows", "到期时间")
            _assert(page.inner_text("#payStatusTag") == "已支付", "支付状态标签不是已支付")
            page.screenshot(path=str(OUT_DIR / "e2e-paid.png"),
                            full_page=True, animations="disabled")
            steps.append({"step": "paid", "orderId": order_id,
                          "payStatus": page.inner_text("#payStatusTag")})

            print("STEP account", flush=True)
            # 4) 我的账户：权益 + 订单
            page.click('#resultActions a[href="/account"]')
            page.wait_for_selector("#dashboardView", state="visible", timeout=20000)
            _wait_text(page, "#entitlementPlan", "月卡")
            _wait_text(page, "#ordersList", "已支付")
            steps.append({"step": "account_after_paid",
                          "plan": page.inner_text("#entitlementPlan")})

            print("STEP relogin", flush=True)
            # 5) 退出 → 未登录点购买必须被登录门挡住 → 登录后回到 next
            page.click("#logoutBtn")
            page.wait_for_selector("#authView", state="visible", timeout=20000)
            page.goto(base + "/pricing", wait_until="load")
            page.wait_for_selector('#planCards [data-buy="monthly"]', state="visible")
            page.click('#planCards [data-buy="monthly"]')
            page.wait_for_url(re.compile(r"/account\?next="), timeout=20000)
            page.wait_for_selector("#authView", state="visible", timeout=20000)
            steps.append({"step": "login_gate"})
            page.click("#tabLogin")
            page.fill("#authEmployeeNo", "E2E001")
            page.click("#authSubmit")
            page.wait_for_url(re.compile(r"/pricing$"), timeout=20000)
            page.goto(base + "/account", wait_until="load")
            page.wait_for_selector("#dashboardView", state="visible", timeout=20000)
            steps.append({"step": "logout_login"})

            print("STEP cancel", flush=True)
            # 6) 再下一单并取消
            page.goto(base + "/pricing", wait_until="load")
            page.wait_for_selector('#planCards [data-buy="monthly"]', state="visible")
            page.click('#planCards [data-buy="monthly"]')
            page.wait_for_url(re.compile(r"/pay\?order="), timeout=20000)
            page.wait_for_selector("#payLayout", state="visible", timeout=20000)
            order2 = urllib.parse.parse_qs(urlsplit(page.url).query).get("order", [""])[0]
            page.click("#cancelOrder")
            _wait_text(page, "#resultTitle", "订单已关闭")
            _assert(page.inner_text("#payStatusTag") == "已关闭", "取消后状态不是已关闭")
            page.screenshot(path=str(OUT_DIR / "e2e-closed.png"),
                            full_page=True, animations="disabled")
            steps.append({"step": "cancel", "orderId": order2})

            context.close()
            browser.close()

        result = {
            "ok": True,
            "baseUrl": base,
            "backend": "real server.main with payment.provider=mock",
            "screenshots": ["e2e-account.png", "e2e-paid.png", "e2e-closed.png"],
            "steps": steps,
            "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "notes": [
                "支付是 Mock 端点，不是真实支付宝扣款。",
                "真实支付宝沙箱 10.00 元由 Lead 于 2026-11-05 实测；本脚本不冒充。",
            ],
        }
        (OUT_DIR / "e2e.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        if backend is not None:
            _stop_backend(backend)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-port", type=int, default=18903)
    parser.add_argument("--site-port", type=int, default=0)
    parser.add_argument("--chrome", default=os.environ.get("PHASE3_CHROME", ""))
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
