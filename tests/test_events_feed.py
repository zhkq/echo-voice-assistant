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

**测试卫生（2026-10-16 根治，那段"随运行顺序变红"的历史）：**

`test_it_needs_a_token`（不带令牌打 `/api/events/feed` 必须 401）曾经**吃别人留下的状态**：
`optional_auth()` 只在 `settings.get("apiAuthEnabled")` 为真时才要令牌，而**本文件原来既没清
进程级缓存、也没在临时库里写过这一项** —— 它靠的正是"缓存里恰好是**本机真实库**的
`apiAuthEnabled=true`"（那台开发机配对过手机，所以是真）。于是：

  * `tests.test_events_feed` 单跑 → 绿；
  * 前面跑过任何一个"把 `settings._cache` 按**自己的临时库**读满（默认 `False`）、
    再原样交出去"的模块（`tests.test_capability_admin` / `tests.test_meeting_capability`
    就是）→ 这条变成"不带令牌也能打" → 断言红 ✗。

根因不是接口坏了，而是**这条用例没有自己把前提摆好**（典型的"红的地方不是坏的地方"）。
现在的做法：`setUp` 里**先清缓存**、再往**自己的临时库**写死 `apiAuthEnabled=True`，
并当场断言 `settings.get()` 读到的是它 —— 于是无论前面跑过什么、真实库是什么档，判据都成立。
`tearDown` 再 `_drop_cache()` 一次（与 `tests/test_model_cleanup.py` 同一个理由：缓存是
**进程级**的，按临时库读满过就不能原样交给下一个模块）。

⚠️ 断言本身**一个字没动**：仍然是"不带令牌 → 401、带令牌 → 200"这条安全护栏。
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
from app.config import settings                                      # noqa: E402


class EventsFeedTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-events-")
        self._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        # ---- 前提自己摆好（2026-10-16 根治"随运行顺序变红"）------------------
        # 两级静态状态都**不受**上面的 db 补丁约束：
        #   * `settings._cache` 挂在进程级单例上 —— 前一个模块按**它的临时库**读满的
        #     那份会原样留在这里（`apiAuthEnabled` 是默认的 False）；
        #   * 库本身 —— `settings.get()` 只会去**当前**的 `db.DB_FILE` 读。
        # 所以两样都要在**这里**摆好，不靠"谁来跑过、留下什么"：
        settings._drop_cache()                       # 丢掉别人留下的缓存快照
        db.set_setting("apiAuthEnabled", True)       # 写进**本用例自己的**临时库
        # 摆好之后当场读一次：顺带让 `_cache` 按**这个临时库**重建（`_load()` 在锁内
        # 一次性建完整份，见 `app/config.py` 那段并发说明）。真实的 `optional_auth()`
        # 走的就是 `settings.get("apiAuthEnabled")` 这一条路。
        # ⚠️ 没清缓存时，这一读会落回**真库**的档（那台开发机是真 → 用例"看着绿"，
        # 但换个不配手机的机器就红）—— 所以"清缓存"这一步不能省。
        self.assertTrue(settings.get("apiAuthEnabled", False),
                        "setUp 没把 apiAuthEnabled 摆成 True：401 那条会失去意义")
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
        # 缓存按**本用例的临时库**读满过（上面那句断言就是读它），交出去之前必须丢掉：
        # 下一个模块要是没像 `tests/test_model_cleanup.py` 那样"读方自保"，
        # 就会读到这里的 `apiAuthEnabled=True`（连带整份临时库的值）——
        # 那正是本文件当年踩过的同一个坑，只是方向反过来。
        settings._drop_cache()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_case_puts_its_own_prerequisite_in_place(self):
        """护栏（2026-10-16）：这条用例的前提必须是**它自己**摆的。

        病灶不是接口，而是"用例吃前面模块留下的缓存"：`test_it_needs_a_token` 曾经
        只有在缓存里恰好是本机**真库**的 `apiAuthEnabled=true` 时才绿 —— 前面跑过
        `tests.test_capability_admin` / `tests.test_meeting_capability`（它们把缓存按
        临时库的默认 `False` 交出去）就红。这里钉住 `setUp` 的隔离动作本身：
        缓存按本临时库重建过、读到的就是 True（而不是"碰巧是真库那份"）。
        """
        self.assertTrue(settings.get("apiAuthEnabled", False),
                        "setUp 没摆好 apiAuthEnabled：这条用例会随前面跑过谁而变红/失去意义")
        # 断言读的确实是**本用例的临时库**，不是碰巧命中了真库
        self.assertEqual(True, db.get_setting("apiAuthEnabled"))

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
