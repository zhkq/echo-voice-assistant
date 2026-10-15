# -*- coding: utf-8 -*-
"""事件游标 `GET /api/events/feed`（手机订阅"电脑上发生了什么"）。

2026-10-15：用户要"手机收"—— 会议转写好了、纪要好了、指令有回复了，手机该**自己知道**，
而不是让人回去翻页面。这一组钉三件事：

  1. **游标语义**：`since < 0` 只给游标、不推历史（新装的手机不该被历史事件淹一遍）；
     `since >= 0` 给"id 大于它"的新事件，**正序、不重复**（重复推 = 手机上同一条通知刷屏）；
  2. **长轮询真的会等**（`wait`），而不是立刻空手回来 —— 否则手机只能一秒一次空转；
  3. **不许遮住老路由**：`/api/events`（面板看"最近 N 条"那个）必须原样还在 ——
     我第一版把新接口就叫 `/events`，同路径同方法时**先注册的赢**，面板那条就会被换成
     另一种形状（2026-10-15 活体测试时想通的：当时 8970 上跑的是稳定版旧代码，
     恰好没被伤到；现在用一条用例把这件事钉住）。
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

from app import db                                                   # noqa: E402
from app.api import router                                           # noqa: E402


class EventsFeedTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-events-")
        self._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        # 这条接口**本来就该要令牌**（它从"网上"来）。造一把临时令牌，
        # 顺带把"没令牌 = 401"这条也验了（`test_api_paths` 那套 fail-closed 的同一口径）。
        row = db.add_api_key_row("events-test", scopes=["device"])
        self.token = row["token"]
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def _get(self, **params):
        return self.client.get("/api/events/feed", params=params,
                               headers={"Authorization": "Bearer %s" % self.token})

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_needs_a_token(self):
        r = self.client.get("/api/events/feed")
        self.assertEqual(401, r.status_code, "事件流是网上来的调用，必须带令牌")

    def test_the_panel_event_list_is_not_shadowed(self):
        """老的 `/api/events`（面板看"最近 N 条"）必须原样可用。

        同路径同方法时 FastAPI **先注册的赢** —— 新接口要是也叫 `/events`，面板那一格
        拿到的就是"游标形状"的数据（有 cursor、但只有增量）。旧那条只回 `{items: [...]}`，
        所以判据是**没有 cursor 键**。
        """
        db.add_event("meeting_stopped", {"meeting": "panel"})
        r = self.client.get("/api/events",
                            headers={"Authorization": "Bearer %s" % self.token})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertIn("items", body)
        self.assertNotIn("cursor", body, "老的 /api/events 被新接口遮住了")

    # ---- 库那一层 ----------------------------------------------------

    def test_cursor_helpers_are_ordered_and_dedup(self):
        start = db.latest_event_id()
        db.add_event("meeting_stopped", {"meeting": "a"})
        db.add_event("meeting_transcribed", {"meeting": "a"})
        db.add_event("command_done", {"id": 7})

        rows = db.list_events_after(start, limit=10)
        self.assertEqual(3, len(rows))
        self.assertEqual([r["id"] for r in rows], sorted(r["id"] for r in rows),
                         "必须是**正序**（老的在前）")
        self.assertEqual("meeting_stopped", rows[0]["type"])

        # 拿最后一条的 id 再问一次：不该重复给
        again = db.list_events_after(rows[-1]["id"], limit=10)
        self.assertEqual([], again, "游标之后的重复推送会让手机通知刷屏")
        self.assertTrue(db.latest_event_id() >= rows[-1]["id"])

    # ---- 接口那一层 --------------------------------------------------

    def test_first_call_only_hands_out_the_cursor(self):
        db.add_event("meeting_stopped", {"meeting": "old"})
        r = self._get().json()
        self.assertEqual([], r["items"], "默认不该把历史事件当成新的推一遍")
        self.assertGreaterEqual(r["cursor"], 1)

    def test_new_events_come_back_and_cursor_advances(self):
        r0 = self._get().json()
        db.add_event("meeting_transcribed", {"meeting": "b", "id": 42})
        r1 = self._get(since=r0["cursor"]).json()
        self.assertEqual(1, len(r1["items"]))
        item = r1["items"][0]
        self.assertEqual("meeting_transcribed", item["type"])
        self.assertEqual("b", (item["payload"] or {}).get("meeting"))
        self.assertGreater(r1["cursor"], r0["cursor"])

    def test_wait_actually_waits_then_returns(self):
        r0 = self._get().json()
        t0 = time.monotonic()
        r1 = self._get(since=r0["cursor"], wait=0.8).json()
        took = time.monotonic() - t0
        self.assertEqual([], r1["items"])
        self.assertGreaterEqual(took, 0.6, "长轮询该真的等一会儿，而不是立刻空手回来")

    def test_wait_returns_early_when_an_event_shows_up(self):
        r0 = self._get().json()

        import threading

        def later():
            time.sleep(0.3)
            db.add_event("command_done", {"id": 99})

        threading.Thread(target=later, daemon=True).start()
        t0 = time.monotonic()
        r1 = self._get(since=r0["cursor"], wait=5).json()
        took = time.monotonic() - t0
        self.assertEqual(1, len(r1["items"]), "有事件就该立刻回，不该等满 wait")
        self.assertLess(took, 3.0, "有事件却等满了：took=%.2fs" % took)


if __name__ == "__main__":
    unittest.main()
