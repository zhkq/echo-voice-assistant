#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集「邮箱注册 / 登录 / 忘记密码」的 Before/After 截图（issue #15）。

Before：目标分支基线 `16d61a3` 的 `site/account.html`（邀请码 + 工号登录/注册）。
After ：当前工作树（邮箱 + 邮件验证码 + 密码 + 邀请码；忘记密码走邮件验证码）。

两侧都用**同一套**本地静态站 + 同一视口（1440×900 桌面 / 375×812 移动）、浅色、中文。
After 用**真实** `server.main`（临时库、dev 邮件 provider、mock 支付、通用邀请码
TEAM2026）走一遍注册 / 登出 / 重置密码；验证码从后端日志读取，不发真实邮件。

用法（仓库根目录）：
    python scripts/capture-account-email-evidence.py
产物：`docs/account-email-evidence/*.png` + `capture.json` + `README.md`。
依赖：Python + `playwright` + 本机 Chrome。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SITE_DIR = ROOT / "site"
OUT_DIR = ROOT / "docs" / "account-email-evidence"
BASELINE_REF = "16d61a3"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 375, "height": 812}
EMAIL = "evidence@example.com"
PASSWORD = "Passw0rd"
NEW_PASSWORD = "NewPassw0rd"
CODE_RE = re.compile(
    r"\[account\.email\]\[dev\].*?email=(\S+)\s+purpose=(\S+)\s+code=(\d{6})")


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class SiteHandler(SimpleHTTPRequestHandler):
    root = SITE_DIR
    backend_port = 0

    def translate_path(self, path):
        clean = unquote(urlsplit(path).path)
        rel = {
            "": "index.html", "/": "index.html",
            "/account": "account.html", "/account/": "account.html",
            "/pricing": "pricing.html", "/pricing/": "pricing.html",
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
        if os.environ.get("ACCOUNT_EVIDENCE_VERBOSE"):
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
        conn = __import__("http.client", fromlist=["HTTPConnection"]).HTTPConnection(
            "127.0.0.1", self.backend_port, timeout=30)
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
        finally:
            conn.close()

    def do_GET(self):
        if self.backend_port and urlsplit(self.path).path.startswith("/v1/"):
            return self._proxy()
        return super().do_GET()

    def do_POST(self):
        if self.backend_port and urlsplit(self.path).path.startswith("/v1/"):
            return self._proxy()
        self.send_error(404, "Not Found")


def _serve(root: Path, backend_port: int = 0) -> ThreadingHTTPServer:
    handler = type("Bound%s" % os.urandom(3).hex(), (SiteHandler,),
                   {"root": root, "backend_port": backend_port})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, name="account-evidence-site",
                     daemon=True).start()
    return server


def _wait_health(port: int, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/v1/health" % port,
                                        timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:                                   # pragma: no cover
            last = exc
            time.sleep(0.4)
    raise RuntimeError("后端没起来：%s" % last)


def _start_backend(tmp: Path, port: int) -> subprocess.Popen:
    db_path = tmp / "auth.db"
    from server.store import Store
    store = Store(str(db_path))
    try:
        store.put_invite("TEAM2026", max_uses=1000, note="evidence 采集")
    finally:
        store.close()
    cfg = {
        "server": {"listen": "127.0.0.1:%d" % port, "admin_listen": "",
                   "state_root": str(tmp / "state"), "local_pair": False},
        "tmp": {"root": str(tmp / "tmp"), "ttl_hours": 4},
        "auth": {"enabled": True, "mode": "jwt",
                 "jwt_secret": "0123456789abcdef0123456789abcdef",
                 "db": str(db_path), "clock_skew_s": 0},
        "billing": {"enabled": True, "timezone": "Asia/Shanghai"},
        "payment": {"provider": "mock"},
        "invite": {"required": True},
        "account": {"email": {"enabled": True, "provider": "dev"}},
        "models": {"device": "cpu"},
    }
    path = tmp / "server.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    log = open(tmp / "backend.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.main", "--config", str(path),
         "--listen", "127.0.0.1:%d" % port, "--log-level", "info"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    proc._evidence_log = log  # type: ignore[attr-defined]
    proc._evidence_log_path = tmp / "backend.log"  # type: ignore[attr-defined]
    return proc


def _stop(proc, server) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    log = getattr(proc, "_evidence_log", None)
    if log is not None:
        try:
            log.close()
        except Exception:
            pass
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass


def _wait_code(proc, purpose: str, email: str, after: int, timeout: float = 20.0) -> str:
    path = getattr(proc, "_evidence_log_path")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # `after` 是**字节**偏移（stat().st_size）；不能拿它去切 str —— 中文日志
            # 的字节数 ≠ 字符数，切错会把验证码行整个跳过。
            text = path.read_bytes()[after:].decode("utf-8", errors="replace")
        except OSError:
            text = ""
        found = ""
        for line in text.splitlines():
            match = CODE_RE.search(line)
            if not match:
                continue
            if match.group(1) == email and match.group(2) == purpose:
                found = match.group(3)
        if found:
            return found
        time.sleep(0.25)
    raise RuntimeError("没从后端日志里拿到 purpose=%s 的验证码" % purpose)


def _launch(playwright, chrome_path: str | None):
    attempts = ([{"executable_path": chrome_path}] if chrome_path else
                [{"channel": "chrome"}, {"channel": "msedge"}, {"channel": "chromium"}])
    attempts.append({})
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(headless=True, **kwargs)
        except Exception:
            continue
    raise RuntimeError("启动浏览器失败")


def _shot(page, name: str) -> str:
    target = OUT_DIR / name
    # 全页截图时先把滚动位置归零、把残留 toast 收起：否则 sticky 顶栏 / fixed toast
    # 会被截在页面中段，看起来像页面坏了。
    try:
        page.evaluate(
            "() => { window.scrollTo(0, 0);"
            " const t = document.getElementById('toast');"
            " if (t) t.classList.remove('show'); }")
        page.wait_for_timeout(120)
    except Exception:
        pass
    page.screenshot(path=str(target), full_page=True, animations="disabled")
    # manifest 里只记文件名，不把本机绝对路径带进仓库。
    return name


def _baseline(browser, base: str) -> dict:
    out = {}
    context = browser.new_context(viewport=DESKTOP, device_scale_factor=1,
                                  locale="zh-CN", timezone_id="Asia/Shanghai",
                                  color_scheme="light", reduced_motion="reduce")
    page = context.new_page()
    page.goto(base + "/account", wait_until="load")
    page.wait_for_selector("#authView", state="visible")
    out["before-login.png"] = _shot(page, "before-login.png")
    page.click("#tabRegister")
    page.wait_for_selector("#fieldNameRow", state="visible")
    out["before-register.png"] = _shot(page, "before-register.png")
    context.close()

    context = browser.new_context(viewport=MOBILE, device_scale_factor=1,
                                  locale="zh-CN", timezone_id="Asia/Shanghai",
                                  color_scheme="light", reduced_motion="reduce")
    page = context.new_page()
    page.goto(base + "/account", wait_until="load")
    page.wait_for_selector("#authView", state="visible")
    out["before-login-mobile.png"] = _shot(page, "before-login-mobile.png")
    page.click("#tabRegister")
    page.wait_for_selector("#fieldNameRow", state="visible")
    out["before-register-mobile.png"] = _shot(page, "before-register-mobile.png")
    context.close()
    return out


def _after(browser, base: str, backend, log_path: Path) -> dict:
    out = {}
    context = browser.new_context(viewport=DESKTOP, device_scale_factor=1,
                                  locale="zh-CN", timezone_id="Asia/Shanghai",
                                  color_scheme="light", reduced_motion="reduce")
    page = context.new_page()
    page.goto(base + "/account", wait_until="load")
    page.wait_for_selector("#authView", state="visible")
    out["after-login.png"] = _shot(page, "after-login.png")
    page.click("#tabRegister")
    page.wait_for_selector("#authInviteRow", state="visible")
    out["after-register.png"] = _shot(page, "after-register.png")

    page.fill("#authEmail", EMAIL)
    before = log_path.stat().st_size if log_path.exists() else 0
    page.click("#sendCodeBtn")
    code = _wait_code(backend, "register", EMAIL, before)
    page.fill("#authCode", code)
    page.fill("#authPassword", PASSWORD)
    page.fill("#authPassword2", PASSWORD)
    page.fill("#authInviteCode", "TEAM2026")
    page.click("#authSubmit")
    page.wait_for_selector("#dashboardView", state="visible", timeout=30000)
    page.wait_for_function(
        "() => { const el = document.querySelector('#profileMeta');"
        " return !!el && el.innerText.includes('@'); }", timeout=15000)
    out["after-account.png"] = _shot(page, "after-account.png")

    # 登出 → 忘记密码 → 重置 → 用新密码登录
    page.click("#logoutBtn")
    page.wait_for_selector("#authView", state="visible", timeout=15000)
    page.click("#forgotPassword")
    page.wait_for_selector("#authBack", state="visible")
    page.fill("#authEmail", EMAIL)
    out["after-reset.png"] = _shot(page, "after-reset.png")
    before = log_path.stat().st_size if log_path.exists() else 0
    page.click("#sendCodeBtn")
    reset_code = _wait_code(backend, "reset", EMAIL, before)
    page.fill("#authCode", reset_code)
    page.fill("#authPassword", NEW_PASSWORD)
    page.fill("#authPassword2", NEW_PASSWORD)
    page.click("#authSubmit")
    page.wait_for_function(
        "() => !document.querySelector('#authForgot').hidden", timeout=15000)
    page.fill("#authEmail", EMAIL)
    page.fill("#authPassword", NEW_PASSWORD)
    page.click("#authSubmit")
    page.wait_for_selector("#dashboardView", state="visible", timeout=30000)
    out["after-reset-success.png"] = _shot(page, "after-reset-success.png")
    context.close()

    context = browser.new_context(viewport=MOBILE, device_scale_factor=1,
                                  locale="zh-CN", timezone_id="Asia/Shanghai",
                                  color_scheme="light", reduced_motion="reduce")
    page = context.new_page()
    page.goto(base + "/account", wait_until="load")
    page.wait_for_selector("#authView", state="visible")
    out["after-login-mobile.png"] = _shot(page, "after-login-mobile.png")
    page.click("#tabRegister")
    page.wait_for_selector("#authInviteRow", state="visible")
    out["after-register-mobile.png"] = _shot(page, "after-register-mobile.png")
    context.close()
    return out


def main(args) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                       # pragma: no cover
        print("缺少 Playwright：%s" % exc, file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="echo-account-evidence-"))
    backend = None
    before_server = after_server = None
    artifacts = {}
    try:
        baseline_dir = tmp / "baseline"
        baseline_dir.mkdir()
        subprocess.run(
            ["git", "archive", BASELINE_REF, "site"],
            cwd=str(ROOT), check=True,
            stdout=open(baseline_dir / "site.tar", "wb"))
        subprocess.run(["tar", "-xf", "site.tar"], cwd=str(baseline_dir), check=True)
        baseline_site = baseline_dir / "site"
        if not (baseline_site / "account.html").is_file():
            raise RuntimeError("基线里没有 site/account.html")
        before_server = _serve(baseline_site, 0)
        before_base = "http://127.0.0.1:%d" % before_server.server_address[1]

        port = _free_port()
        backend = _start_backend(tmp, port)
        _wait_health(port)
        after_server = _serve(SITE_DIR, port)
        after_base = "http://127.0.0.1:%d" % after_server.server_address[1]

        with sync_playwright() as playwright:
            browser = _launch(playwright, args.chrome or
                              os.environ.get("ACCOUNT_EVIDENCE_CHROME", ""))
            artifacts.update(_baseline(browser, before_base))
            artifacts.update(_after(browser, after_base, backend,
                                    getattr(backend, "_evidence_log_path")))
            browser.close()

        manifest = {
            "issue": 15,
            "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "baseline": {
                "ref": BASELINE_REF,
                "source": "git archive %s site" % BASELINE_REF,
                "description": "目标分支基线：邀请码 + 工号登录/注册",
            },
            "after": {
                "source": "working tree site/",
                "backend": "真实 server.main（临时 auth.db、account.email.provider=dev、"
                           "billing mock、invite.required=true）",
                "description": "邮箱 + 邮件验证码 + 密码 + 通用邀请码；忘记密码用邮件验证码",
            },
            "scenario": "登录 → 注册（真实发码/注册）→ 登出 → 忘记密码 → 重置 → 新密码登录",
            "viewports": {"desktop": DESKTOP, "mobile": MOBILE},
            "theme": "light",
            "locale": "zh-CN",
            "testData": {"email": EMAIL, "invite": "TEAM2026"},
            "artifacts": artifacts,
            "notes": [
                "Before/After 都是实际页面截图；Before 由 git archive 取基线分支 site/ 渲染，不是 mockup。",
                "After 用真实后端；验证码从临时后端日志读取（dev provider），没有发真实邮件。",
                "没有真实支付；注册赠送试用由 billing mock 提供，仅用于展示已登录后的账户页。",
                "未提交任何真实邮箱/凭据；测试邮箱是 example.com 保留域。",
            ],
        }
        (OUT_DIR / "capture.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("OK: %d 张截图 + capture.json 写入 %s" % (len(artifacts), OUT_DIR))
        return 0
    finally:
        if backend is not None:
            _stop(backend, after_server) if after_server else _stop(backend, None)
        if before_server is not None:
            try:
                before_server.shutdown()
                before_server.server_close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", default="", help="Chrome 可执行文件路径（可选）")
    raise SystemExit(main(parser.parse_args()))
