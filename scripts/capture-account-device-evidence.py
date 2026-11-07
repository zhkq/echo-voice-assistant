#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集「账户页设备列表 / 解绑」的 Before/After 截图（issue #23）。

Before：`c09884b`（`integration/billing-vibepay` 基线；账户页只有权益/订单，没有设备列表）。
After ：当前工作树（账户页新增「我的设备」：设备名/平台/绑定时间/最近活跃 + 解绑）。

两侧都用**同一个**本地 mock 能力后端、同一份账号/权益/设备数据、同一视口
（1440×900 桌面 + 375×812 移动）、浅色、中文；不是真实账号，也不涉及任何真实凭据。

用法（仓库根目录）：

    python scripts/capture-account-device-evidence.py
    # 产物：docs/account-device-evidence/*.png + capture.json + README.md

依赖：Python + `playwright` + 本机 Chrome。

复用 `capture-renewal-evidence.py` 的站点服务 / 浏览器启动 / 截图工具：
那两个脚本的 mock 站点行为必须一致，抄一份迟早漂开。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "account-device-evidence"
DEFAULT_BASELINE_REF = "c09884b"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 375, "height": 812}


def _renewal_module():
    path = ROOT / "scripts" / "capture-renewal-evidence.py"
    spec = importlib.util.spec_from_file_location("_echo_renewal_evidence", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state_with_devices(renewal, base_state):
    """续费证据里的同一份 mock 状态 + 两台真实形状的设备。"""
    now = time.time()
    state = base_state()
    entitlement = state["entitlement"]
    entitlement["devices"] = 2
    entitlement["maxDevices"] = 3
    state["me"]["entitlement"] = dict(entitlement)
    state["me"]["devices"] = [
        {"clientId": "cli-demo-1", "deviceName": "DESKTOP-ABC", "platform": "win32",
         "boundAt": now - 10 * 86400, "lastSeen": now - 60, "status": "active"},
        {"clientId": "cli-demo-2", "deviceName": "MacBook-Pro", "platform": "darwin",
         "boundAt": now - 3 * 86400, "lastSeen": now - 3600, "status": "active"},
    ]
    return state


def capture(args) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        print("缺少 Playwright：%s" % exc, file=sys.stderr)
        return 2

    renewal = _renewal_module()
    base_state = renewal._state
    renewal._state = lambda: _state_with_devices(renewal, base_state)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    files: list = []
    tmp = Path(tempfile.mkdtemp(prefix="echo-device-before-"))
    before_server = after_server = None
    browser_name = "unknown"
    try:
        before_root = renewal._extract_ref(args.baseline_ref, tmp)
        before_server = renewal.start_server(before_root, args.port)
        after_server = renewal.start_server(renewal.SITE_DIR, 0)
        before_base = "http://127.0.0.1:%s" % before_server.server_address[1]
        after_base = "http://127.0.0.1:%s" % after_server.server_address[1]
        print("Before %s（%s）  After %s" % (args.baseline_ref, before_base, after_base))

        with sync_playwright() as playwright:
            browser = renewal._launch_browser(playwright, args.chrome)
            browser_name = "%s %s" % (browser.browser_type.name, browser.version)

            desktop = browser.new_context(
                viewport=DESKTOP, device_scale_factor=1, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            renewal._capture(
                desktop, before_base, out, "before-account-devices", "/account",
                files, viewport=DESKTOP, wait_for=("#dashboardView",),
                note="基线 c09884b：账户页只有权益/订单，没有设备列表。")
            renewal._capture(
                desktop, after_base, out, "after-account-devices", "/account",
                files, viewport=DESKTOP,
                wait_for=("#devicesList", "#devicesList .device-item"),
                wait_text="我的设备",
                note="本分支：账户页新增「我的设备」列表（名称/平台/绑定/最近活跃）与解绑。")
            desktop.close()

            mobile = browser.new_context(
                viewport=MOBILE, device_scale_factor=2, locale="zh-CN",
                timezone_id="Asia/Shanghai", color_scheme="light",
                reduced_motion="reduce")
            renewal._capture(
                mobile, before_base, out, "before-account-devices-mobile", "/account",
                files, viewport=MOBILE, wait_for=("#dashboardView",),
                note="基线 c09884b 移动端账户页（无设备列表）。")
            renewal._capture(
                mobile, after_base, out, "after-account-devices-mobile", "/account",
                files, viewport=MOBILE,
                wait_for=("#devicesList", "#devicesList .device-item"),
                wait_text="我的设备",
                note="本分支移动端：设备列表与解绑按钮。")
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
        "baseline": {
            "ref": args.baseline_ref,
            "commit": subprocess.check_output(
                ["git", "rev-parse", args.baseline_ref], cwd=str(ROOT),
                text=True).strip(),
        },
        "browser": browser_name,
        "mode": "mock backend",
        "notes": [
            "Before 与 After 使用同一份 mock 账号/权益/设备数据、同一视口、浅色、中文。",
            "设备是 mock 数据（cli-demo-1 / cli-demo-2），不是真实账号，也不含凭据。",
            "Before=c09884b 是 ticket 分支基线（integration/billing-vibepay），不是 main。",
        ],
        "files": files,
    }
    (out / "capture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_readme(out, manifest)
    print("已写 %s" % (out / "capture.json"))
    return 0


def _write_readme(out: Path, manifest: dict) -> None:
    base = manifest["baseline"]["commit"][:7]
    rows = []
    for item in manifest["files"]:
        rows.append("| %s | ![%s](%s) |" % (item["page"], item["page"], item["file"]))
    text = """# 账户页设备列表 · Before/After 截图（issue #23）

对应 `POST /v1/account/devices/bind` 与账户页「我的设备」。

- **Before**：`%s`（`integration/billing-vibepay` 基线；账户页只有权益/订单，没有设备列表）
- **After**：本分支（账户页新增设备名、平台、绑定时间、最近活跃与解绑）
- 同一份 mock 账号/权益/设备数据、同一视口（桌面 1440×900，移动 375×812）、浅色、中文。
- 设备是 mock 数据（`cli-demo-1` / `cli-demo-2`），不是真实账号，也不含凭据。

| 场景 | 截图 |
|---|---|
%s

## 复采

```bash
python scripts/capture-account-device-evidence.py
```

依赖 Python + `playwright` + 本机 Chrome。
""" % (base, "\n".join(rows))
    (out / "README.md").write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--chrome", default=os.environ.get("RENEWAL_CHROME", ""))
    return capture(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
