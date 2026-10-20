# -*- coding: utf-8 -*-
"""「指令历史」读面 + 单条取回的契约（2026-10-20 回顾轮次不再重复之后）。

背景（用户原话）：*"回顾的历史对话，指令里也有能不重复吗？"*

回顾轮次现在落 `source='review'`，属于「回顾历史」—— 所以：

  1. `GET /api/commands`（指令历史）**不包含**回顾轮次，`total` 与分页也跟着它们
     一起变（不能在取完再过滤：那样"加载更多"会翻到空洞、`total` 也对不上）；
  2. 但手机回顾那一场仍要拿到**自己那一轮**的结果，所以保留
     `GET /api/commands/{id}`（按 id 取一条，不进列表）；
  3. 老记录（没有 `meta.origin`）的 `origin` 仍按 `source` 推（`mobile` / 其余=pc），
     与 `assistant._command_origin` 同口径 —— 行为不变。

DB 重定向到临时目录；不联网、不跑 `_dispatch`（这里只验读面）。
"""
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                                  # noqa: E402
from app import daily_review as dr                                   # noqa: E402
from app.api import REVIEW_CONTEXT, router                           # noqa: E402
from app.config import settings                                      # noqa: E402


class CommandsApiContractTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-commands-api-")
        self._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        settings._drop_cache()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self._clean)

        self.normal_id = db.add_command("普通指令", source="wake", status="done",
                                        meta={"origin": "pc"})
        self.review_id = db.add_command("回顾这一轮", source=dr.REVIEW_SOURCE,
                                        status="done", meta={"context": REVIEW_CONTEXT})

    def _clean(self):
        db.DATA_DIR, db.DB_FILE = self._old
        settings._drop_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_review_turns_are_not_in_command_history(self):
        """回顾轮次不许出现在指令历史里（它属于回顾历史，两处只出现一次）。"""
        body = self.client.get("/api/commands").json()
        ids = [it["id"] for it in body["items"]]
        self.assertNotIn(self.review_id, ids, "回顾轮次混进了指令历史")
        self.assertIn(self.normal_id, ids, "普通指令必须还在")
        self.assertEqual(1, body["total"], "total 要跟着排除（不是取完再过滤）")

    def test_review_turns_stay_out_of_paging_and_search(self):
        """分页与关键词也看不到回顾轮次 —— 否则"加载更多"会翻到被过滤掉的空洞。"""
        page2 = self.client.get("/api/commands?limit=1&offset=1").json()
        self.assertEqual(1, page2["total"])
        self.assertEqual([], page2["items"], "第二页应当已经没有了")
        self.assertEqual(0, self.client.get("/api/commands?q=回顾这一轮").json()["total"])

    def test_a_review_turn_can_still_be_fetched_by_id(self):
        """手机回顾那一场要能按 id 取到这一轮（它不在列表里，但必须读得到）。"""
        r = self.client.get("/api/commands/%d" % self.review_id)
        self.assertEqual(200, r.status_code, r.text)
        row = r.json()
        self.assertEqual(self.review_id, row["id"])
        self.assertEqual("回顾这一轮", row["text"])
        self.assertEqual("done", row["status"])
        self.assertIn("speakText", row)
        self.assertIn("origin", row)

    def test_a_missing_command_is_404(self):
        r = self.client.get("/api/commands/999999")
        self.assertEqual(404, r.status_code, r.text)

    def test_old_rows_without_meta_origin_fall_back_to_source(self):
        """老记录没有 `meta.origin` → 仍按 `source` 推（mobile / 其余=pc），行为不变。"""
        old_mobile = db.add_command("老手机指令", source="mobile", status="done")
        old_web = db.add_command("老面板指令", source="web", status="done")
        try:
            self.assertEqual("mobile",
                             self.client.get("/api/commands/%d" % old_mobile).json()["origin"])
            self.assertEqual("pc",
                             self.client.get("/api/commands/%d" % old_web).json()["origin"])
        finally:
            db._exec("DELETE FROM commands WHERE id IN (?,?)", (old_mobile, old_web))

    def test_review_rows_with_meta_origin_keep_the_mobile_origin(self):
        """回顾轮记的是标签 `review`，但发起方是手机 —— 按 id 取回时要是 `mobile`。

        否则手机侧 `SpeakPlan` 会把它当成电脑发起的任务，**这一轮的回复就不念了**。
        """
        rid = db.add_command("手机回顾轮", source=dr.REVIEW_SOURCE, status="done",
                             meta={"context": REVIEW_CONTEXT, "origin": "mobile"})
        try:
            self.assertEqual("mobile",
                             self.client.get("/api/commands/%d" % rid).json()["origin"])
        finally:
            db._exec("DELETE FROM commands WHERE id=?", (rid,))


if __name__ == "__main__":
    unittest.main()
