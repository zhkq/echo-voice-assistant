# -*- coding: utf-8 -*-
"""语音合成那句状态标签的判据（`app/audio/tts.py` 的 `describe` / `sync_status`）。

**它为什么值得一组用例**：2026-10-15 用户实测 —— 面板底部那格写着「自动」，
而在线那条路（edge-tts）已经被微软服务端 **403** 拒了、每次朗读其实都在跑本机离线引擎。
根子是那句话**没问现实**（按设置值映射出来的）。用户原话：

    仪表盘，最下方的语音合成卡片现在显示的是自动，改成自动（在线），或者自动（离线）

所以这里钉三件事：
  1. `auto` 必须明确写成「自动（在线）」或「自动（离线）」，**不许只写"自动"**；
  2. 第一段是**短值**（面板那三格只取 `detail.split(" · ")[0]`），解释放第二段；
  3. `sync_status` **只在标签变了的时候写库** —— 它是被高频轮询调的，不能每轮写一次。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio import tts as tts_mod                                # noqa: E402


def _short(label: str) -> str:
    """面板那三格看到的短值（与 `web/app.js::refreshRunStatus` 同一个取法）。"""
    return label.split(" · ")[0]


class DescribeTests(unittest.TestCase):

    def test_auto_says_online_or_offline_never_just_auto(self):
        online = tts_mod.describe("auto", True, "SAPI")
        offline = tts_mod.describe("auto", False, "SAPI")
        self.assertEqual("自动（在线）", _short(online))
        self.assertEqual("自动（离线）", _short(offline))
        for label in (online, offline):
            self.assertNotEqual("自动", _short(label), "只写'自动'正是用户报的那个毛病")

    def test_explicit_engines_keep_their_own_wording(self):
        self.assertEqual("在线", _short(tts_mod.describe("edge-tts", True, "SAPI")))
        self.assertEqual("在线（不可达）", _short(tts_mod.describe("edge-tts", False, "SAPI")))
        self.assertEqual("本机离线", _short(tts_mod.describe("sapi", True, "SAPI")))
        self.assertEqual("已关闭", _short(tts_mod.describe("off", True, "SAPI")))

    def test_the_platform_offline_engine_is_recognised_by_name_too(self):
        """macOS 的离线值是 `say`（由接缝给），也得认成"本机离线"。"""
        self.assertEqual("本机离线", _short(tts_mod.describe("say", True, "say")))
        self.assertEqual("本机离线",
                         _short(tts_mod.describe("say", True, "SAPI", offline_ids=("sapi", "say"))))

    def test_unknown_engine_values_are_treated_as_auto(self):
        """库里可能存着老值/手改的值 —— 按 `auto` 讲，而不是编一个"未知"。"""
        self.assertEqual("自动（在线）", _short(tts_mod.describe("", True, "SAPI")))
        self.assertEqual("自动（离线）", _short(tts_mod.describe("whatever", False, "SAPI")))

    def test_the_explanation_is_not_lost(self):
        """短值给面板，解释给人看（tip 里显示整句）—— 第二段必须真的说了为什么。"""
        label = tts_mod.describe("auto", False, "SAPI")
        self.assertIn("SAPI", label)
        self.assertIn(" · ", label)


class SyncStatusTests(unittest.TestCase):
    """`sync_status` 只在**变了**的时候写库（这个接口是高频轮询调的）。"""

    def setUp(self):
        self.calls = []
        import app.services as services_mod
        self._orig_report = services_mod.report_tts
        services_mod.report_tts = lambda status, detail="": self.calls.append((status, detail))
        self.addCleanup(lambda: setattr(services_mod, "report_tts", self._orig_report))
        self._orig_label = tts_mod.engine_label
        self.addCleanup(lambda: setattr(tts_mod, "engine_label", self._orig_label))
        # 每次用例从"没报过"开始 —— 那个缓存是进程级的
        self._orig_reported = tts_mod._reported_label
        tts_mod._reported_label = None
        self.addCleanup(lambda: setattr(tts_mod, "_reported_label", self._orig_reported))

    def test_same_label_is_reported_only_once(self):
        tts_mod.engine_label = lambda engine=None: "自动（在线） · 首选 edge-tts"
        for _ in range(5):
            tts_mod.sync_status()
        self.assertEqual([("online", "自动（在线） · 首选 edge-tts")], self.calls)

    def test_a_changed_label_gets_reported(self):
        tts_mod.engine_label = lambda engine=None: "自动（在线） · x"
        tts_mod.sync_status()
        tts_mod.engine_label = lambda engine=None: "自动（离线） · y"
        tts_mod.sync_status()
        self.assertEqual(["自动（在线） · x", "自动（离线） · y"], [c[1] for c in self.calls])

    def test_a_failing_status_write_never_raises(self):
        """写不进状态也不能把朗读带崩（那一行只是"给人看"的）。"""
        def boom(*a, **kw):
            raise RuntimeError("db 挂了")
        import app.services as services_mod
        services_mod.report_tts = boom
        tts_mod.engine_label = lambda engine=None: "自动（离线） · z"
        self.assertEqual("自动（离线） · z", tts_mod.sync_status())


if __name__ == "__main__":
    unittest.main()
