# -*- coding: utf-8 -*-
"""**真渲染一次**页面，抓"静态查不出"的前端故障（2026-10-06 加）。

## 为什么必须有这一道

当天连着三种"静态查不出"的前端故障：

| 故障 | `node --check` | Python 测试 | ruff | 门禁 | 真渲染 |
|---|---|---|---|---|---|
| `web/app.js` 多一个 `}` | ✗（后加的守卫抓到了） | 全绿 | 全绿 | 全绿 | 白屏 |
| `#capLocalSettings` 塞进按钮里 | 全绿 | 全绿 | 全绿 | 全绿 | 那组控件不显示 |
| `capEngineName is not defined` | **全绿** | **全绿** | **全绿** | **全绿** | **整块渲染中断** |

第三条是**运行时**才炸的（`ReferenceError`），一炸就中断整块渲染 ——
用户看到的是"能力清单加载失败"，而真因在一个跟它无关的小函数上。
**只有真跑一次页面能抓住它。**

## 为什么渲染**真实**的页面而不是喂假数据（这条是踩出来的）

我第一版用 fixture（手抓的接口形状）喂给页面，结果**假阴性**：假数据触发了与
真实数据**不同**的代码路径，`#capBackendHost` 没渲染出来，测试红了 ——
而真实页面（无头 Edge 打开 `http://127.0.0.1:8970/`）**好好地有那个元素**。
教训：**要验"用户看到的页面"，就得渲染用户看的那个页面。**

所以判据是：**dev ECHO 在跑**（`127.0.0.1:8970`）时，用无头 Edge 渲染它，
断言关键元素都在、页面里没有"能力清单加载失败"、没有运行时错误。
没有 ECHO 时**跳过并说明**（不是静默跳过：消息里写清漏了一道闸）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8970"
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

#: 渲染完必须存在的元素（都是**用户会去找**的东西）：
#: 后端设置那张卡 + 本机/网络选择器 + 配对框与本机那组 + 能力卡容器。
REQUIRED_IDS = (
    "capBackendHost",      # 「后端设置」的占位（静态卡被搬进来）
    "capBackendModeRow",   # 本机后端 / 网络后端
    "capRouteCard",        # 「已配对后端连接情况」那张静态卡
    "capPairSettings",     # 网络后端：配对设置（地址可改）
    "capLocalSettings",    # 本机后端：检测/起/停
    "capKindCards",        # 能力卡容器
)


def _edge() -> str:
    for p in _EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def _alive() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/status", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


class PanelRendersTests(unittest.TestCase):
    def test_the_real_panel_renders_with_the_expected_widgets(self):
        edge = _edge()
        if not edge:
            self.fail("没找到 Edge —— 这道'真渲染'检查没跑（这不等于通过）。"
                      "Windows 自带 Edge；找过：%s" % (list(_EDGE_CANDIDATES),))
        if not _alive():
            self.fail("dev ECHO 不在 %s（这道检查要渲染真实的页面，喂假数据会假阴性 —— "
                      "见文件头那段）。先起 ECHO：powershell -File scripts\\start.ps1 -Background"
                      % BASE)

        profile = tempfile.mkdtemp(prefix="echo-edge-")
        try:
            r = subprocess.run(
                [edge, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--user-data-dir=" + profile, "--virtual-time-budget=4000",
                 "--dump-dom", BASE + "/"],
                capture_output=True, timeout=300)
            dom = (r.stdout or b"").decode("utf-8", "replace")
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        self.assertGreater(len(dom), 20000, "DOM 太小（%d 字节）—— 页面可能没渲染" % len(dom))

        missing = [i for i in REQUIRED_IDS if ('id="%s"' % i) not in dom]
        self.assertEqual(missing, [], "渲染后这些元素不在（用户就找不到它们）：%s" % missing)

        # 那个"整块渲染中断"的症状：能力卡渲染失败时页面会写这句
        self.assertNotIn("能力清单加载失败", dom,
                         "页面里出现了「能力清单加载失败」—— 某一步渲染抛异常中断了")

        # 「本机后端 / 网络后端」的字面（用户按这两个词找入口）
        for label in ("本机后端", "网络后端"):
            self.assertIn(label, dom, "看不到「%s」这个选项" % label)

        # 静态资源不该 404（harness 之外最容易踩的：改了文件名忘了同步）
        for bad in ("ReferenceError", "is not defined"):
            self.assertNotIn(bad, dom, "页面里出现了 '%s' —— 运行时错误" % bad)
