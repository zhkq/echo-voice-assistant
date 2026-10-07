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
    # 每日回顾（2026-10-06）：仪表盘那张卡 + 「回顾历史」页签的两个宿主。
    # 它们是新加的界面 —— 漏掉一个用户就"找不到入口"，所以钉在这里。
    "reviewCard",          # 仪表盘的「每日回顾」卡
    "reviewHistList",      # 回顾历史：列表
    "reviewHistDetail",    # 回顾历史：详情
    "htab-reviews",        # 回顾历史那个子页签的容器
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

        # **临时种两条回顾**：只验元素存在是查不出"列表渲染不出来"的（空态与有数据
        # 走的是两条分支）。种进去 → 渲染 → 断言列表/详情出得来 → 一定清掉。
        # 为什么用真库（而不是替身）：这条检查的全部意义就是"用户看到的那条路"。
        seeded = self._seed_reviews()

        # ⚠️ **要渲染两个页面**：面板是"一个视图一个视图画"的 ——
        #   * 设置元素（`#capBackendHost` 那张搬进来的卡）只在**设置**页渲染；
        #   * 回顾卡与历史页签在**它们自己那页**。
        # 2026-10-07 踩过：这条用例原来用 `?view=reviews` 一个 URL 找两边的元素，
        # 而我刚把 `reviews` 修好（以前它会错误地回落成仪表盘）→ 落在历史页上，
        # 于是"设置元素找不到"而**红得很有道理**（错的是用例的 URL，不是代码）。
        try:
            dom_settings = self._render("?view=settings")
            dom_reviews = self._render("?view=reviews")
        finally:
            # 种进去的数据**一定清掉**（哪怕渲染/断言失败）：不该在用户库里留东西。
            self._unseed_reviews(seeded)

        self.assertGreater(len(dom_settings), 20000,
                           "设置页 DOM 太小（%d 字节）—— 页面可能没渲染" % len(dom_settings))

        missing = [i for i in REQUIRED_IDS if ('id="%s"' % i) not in dom_settings]
        self.assertEqual(missing, [],
                         "设置页渲染后这些元素不在（用户就找不到它们）：%s" % missing)

        # 那个"整块渲染中断"的症状：能力卡渲染失败时页面会写这句
        for name, dom in (("设置页", dom_settings), ("回顾历史页", dom_reviews)):
            self.assertNotIn("能力清单加载失败", dom,
                             "%s 出现了「能力清单加载失败」—— 某一步渲染抛异常中断了" % name)
            # 静态资源不该 404（harness 之外最容易踩的：改了文件名忘了同步）
            for bad in ("ReferenceError", "is not defined"):
                self.assertNotIn(bad, dom, "%s 出现了 '%s' —— 运行时错误" % (name, bad))

        # 「本机后端 / 网络后端」的字面（用户按这两个词找入口）
        for label in ("本机后端", "网络后端"):
            self.assertIn(label, dom_settings, "看不到「%s」这个选项" % label)

        # **回顾那两个界面真的渲染出内容了**（不是只有空壳）
        self.assertIn("每日回顾", dom_settings, "仪表盘/设置里没看到「每日回顾」卡片")
        self.assertIn("回顾历史", dom_reviews, "没看到「回顾历史」页签")
        # 2026-10-07 的 bug 就在这一条上：`?view=reviews` 以前会**错误回落成仪表盘**
        # （`reviews` 没登记进 VIEW_ALIASES），于是"点回顾历史跳不走"。
        # 判据：落到历史页时 `#htab-reviews` 必须是**可见**的那一个（不带 hidden）。
        self.assertRegex(dom_reviews, r'id="htab-reviews"(?![^>]*\bhidden\b)[^>]*>',
                         "?view=reviews 没有落到「回顾历史」子页签（它被 hidden 挡着）")
        self.assertIn("2077-", dom_reviews,
                      "回顾历史的列表里没有刚才种进去的那天（列表没渲染）")

    def _render(self, query):
        """无头 Edge 渲染一次真实页面，返回 DOM 文本。"""
        edge = _edge()
        profile = tempfile.mkdtemp(prefix="echo-edge-")
        try:
            r = subprocess.run(
                [edge, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--user-data-dir=" + profile, "--virtual-time-budget=6000",
                 "--dump-dom", BASE + "/" + query],
                capture_output=True, timeout=300)
            return (r.stdout or b"").decode("utf-8", "replace")
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    #: 种/清回顾数据用的日期：**故意用未来**，一眼能认出是测试造的，
    #: 也几乎不可能与用户真实回顾的那天撞上。
    SEED_DATE = "2077-01-02"

    def _seed_reviews(self):
        """往真库种两条回顾（一成功一失败）。返回 id 列表供清理。"""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import app.db as db
        ids = []
        ids.append(db.add_command("测试：今天修了后端归属", source="review", status="done",
                                  session_id="seed-sess",
                                  meta={"review": True, "date": self.SEED_DATE,
                                        "kind": "review:" + self.SEED_DATE}))
        db.update_command(ids[-1], brief="后端归属修好了。", reply="整理稿", duration_ms=1200)
        ids.append(db.add_command("测试：还有一件事没做", source="review", status="failed",
                                  meta={"review": True, "date": self.SEED_DATE,
                                        "kind": "review:" + self.SEED_DATE}))
        db.update_command(ids[-1], error="DSH 在超时内没有回复", duration_ms=180000)
        # `ts` 是 `datetime('now')` —— 种不出指定的日期，所以直接把 ts 改到那天
        for i in ids:
            db._exec("UPDATE commands SET ts=? WHERE id=?", (self.SEED_DATE + " 21:30:00", i))
        return ids

    def _unseed_reviews(self, ids):
        """**一定清掉**（哪怕断言失败）—— 这条用例不该在用户库里留东西。"""
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import app.db as db
            for i in ids:
                db._exec("DELETE FROM commands WHERE id=?", (i,))
        except Exception:                                          # pragma: no cover - 兜底
            pass
