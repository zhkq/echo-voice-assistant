#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集 issue #16 设计稿的关键页面截图（静态 mockup，不是生产页面）。

用法（仓库根目录）：
    python docs/admin-redesign/capture-mockup.py

产物在 docs/admin-redesign/ 下：
    login-1280.png / overview-1280.png / orders-1280.png /
    order-detail-1280.png / refund-confirm-1280.png / users-1280.png /
    user-detail-1280.png / notifies-1280.png / audit-1280.png /
    overview-768.png / orders-375.png / user-detail-375.png

依赖：Python + playwright + 本机 Chrome。不连后端、不写真实库。
"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
MOCK = HERE / "mockup.html"
URL = MOCK.as_uri()

SHOTS = [
    # name, hash/page, viewport, drawer/refund, full_page
    ("login-1280", "#login", (1280, 900), None, False),
    ("overview-1280", "#overview", (1280, 900), None, True),
    ("orders-1280", "#orders", (1280, 900), None, False),
    ("order-detail-1280", "#orders", (1280, 900), "order", False),
    ("refund-confirm-1280", "#orders", (1280, 900), "refund", False),
    ("users-1280", "#users", (1280, 900), None, False),
    ("user-detail-1280", "#users", (1280, 900), "user", False),
    ("notifies-1280", "#notifies", (1280, 900), None, False),
    ("audit-1280", "#audit", (1280, 900), None, False),
    ("overview-768", "#overview", (768, 1024), None, True),
    ("orders-768", "#orders", (768, 1024), None, False),
    ("login-375", "#login", (375, 812), None, False),
    ("overview-375", "#overview", (375, 812), None, True),
    ("orders-375", "#orders", (375, 812), None, True),
    ("user-detail-375", "#users", (375, 812), "user", False),
]


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        for name, hash_, viewport, action, full_page in SHOTS:
            page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]},
                                    device_scale_factor=1)
            page.goto(URL + hash_, wait_until="load")
            page.wait_for_timeout(250)
            if action == "order":
                page.evaluate("openOrder('ord-20261106-a1b2c3')")
                page.wait_for_timeout(300)
            elif action == "user":
                page.evaluate("openUser('usr-3f9a21')")
                page.wait_for_timeout(300)
            elif action == "refund":
                page.evaluate("openOrder('ord-20261106-a1b2c3')")
                page.wait_for_timeout(200)
                page.evaluate("openRefund('ord-20261106-a1b2c3')")
                page.wait_for_timeout(300)
            page.screenshot(path=str(HERE / (name + ".png")), full_page=full_page)
            page.close()
        browser.close()
    manifest = {
        "source": "docs/admin-redesign/mockup.html",
        "note": "静态设计稿截图；不是生产页面，不含真实数据。",
        "viewport": {"desktop": [1280, 900], "tablet": [768, 1024], "mobile": [375, 812]},
        "files": [s[0] + ".png" for s in SHOTS],
    }
    (HERE / "capture.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    print("wrote", len(SHOTS), "screenshots to", HERE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
