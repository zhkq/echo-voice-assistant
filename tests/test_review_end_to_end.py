# -*- coding: utf-8 -*-
"""「回顾」整条链路的端到端判据（2026-10-18 复查"功能都正常不"时加）。

这个文件盯的是**用户点下去到底发生了什么**，不是内部函数长什么样：

  1. **口述提交真的产出东西**：HTTP 200 → 库里多一条 `source='review'` 的行 →
     `history_list/summary/history_detail` 都能看见 → 工作日志被写过时 `registered=true`；
  2. **"200 但什么都没做"不许再发生**：技能没写笔记库时，回复里要**如实说**
     （`registered=false` + 播报里补一句 + 日志留痕），而不是回一个漂亮的 `ok=true`；
  3. **依赖缺失如实说**：回顾没启用 / 智能体客户端拿不到 → `ok=false` + 人话原因，
     而且**失败也要留痕**（历史里查得到"那几次没成"）；
  4. **日期坏格式**（`2026-13-99` 这种日历上不存在的）要报错，而不是当成"那天没有回顾"。

全程用**临时库 + 临时笔记库**（复用 `tests.test_daily_review._Base` 的重定向），
DSH 客户端一律换成替身：不联网、不建真工作区、不碰用户真实的 Obsidian 库。
"""
import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                                  # noqa: E402
from app import assistant                                            # noqa: E402
from app import daily_review as dr                                   # noqa: E402
from app.config import settings                                      # noqa: E402
from tests.test_daily_review import _Base, _FakeClient               # noqa: E402


class _SkillClient(_FakeClient):
    """DSH 适配器替身：**像 daily-review 技能那样**在回复前把条目写进工作日志。

    `write=False` 模拟"技能没装 / 没写笔记库"——那是这一轮最危险的失败：
    智能体照样回了话，但用户的东西一个字都没登记。
    """

    def __init__(self, reply="【播报】记好了，还有别的吗？【/播报】",
                 note_path="", write=True):
        super().__init__(reply=reply)
        self.note_path = note_path
        self.write = write

    def wait_for_reply(self, session_id, timeout=90, poll=0.5, cancel_on_timeout=True):
        self.calls.append(("wait_for_reply", session_id, timeout, cancel_on_timeout))
        if self.write and self.note_path:
            os.makedirs(os.path.dirname(self.note_path), exist_ok=True)
            with open(self.note_path, "a", encoding="utf-8") as fh:
                fh.write("\n- 端到端测试登记的一条\n")
        return self.reply, True


class _BrokenClient:
    """`_client()` 本身就拿不到（DSH 没装 / harness 没起来）时的那种失败。"""

    def __init__(self, error="harness 没起来"):
        self.error = error

    def __call__(self):
        raise RuntimeError(self.error)


class _ApiBase(_Base):
    """带 TestClient 的基类（与 `tests.test_daily_review.ApiTests` 同一套做法）。"""

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def _patch_client(self, factory):
        """把 `daily_review._client` 换成替身。

        `factory` 必须是**零参 callable**（`submit()` 里是 `client or _client()`）：
        客户端实例本身不是工厂，传实例会得到 "'_SkillClient' object is not callable"。
        """
        self.addCleanup(setattr, dr, "_client", dr._client)
        dr._client = factory

    def _note_path(self):
        return dr.vault_note_path(time.strftime("%Y-%m-%d"))

    def _rows(self):
        return db._query("SELECT * FROM commands WHERE source=? ORDER BY id ASC", (dr.REVIEW_SOURCE,))


class SubmitEndToEndTests(_ApiBase):
    """口述提交：真的产出条目吗？失败时如实说吗？"""

    def test_a_real_submit_lands_a_row_and_reports_what_got_registered(self):
        note = self._note_path()
        fake = _SkillClient(note_path=note, write=True)
        self._patch_client(lambda: fake)

        text = "今天把回顾的端到端验了一遍"
        r = self.client.post("/api/daily-review/submit", json={"text": text})

        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertIn("记好了", body["spoken"])
        # **登记核实**：这一轮真的写过工作日志 → registered=true，并把路径带回去
        self.assertTrue(body["registered"], "技能写了日志却没被认出来")
        self.assertEqual(note, body["vaultNote"])
        self.assertTrue(os.path.isfile(note), "工作日志该真的在那个路径上")

        # 口述原文原样进了提示词（整理的是用户说的话，不是别的东西）
        self.assertEqual(1, len(fake.prompted))
        self.assertIn(text, fake.prompted[0])

        # 库里真的多了一条；历史三个接口都看得见
        rows = self._rows()
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("done", row["status"])
        self.assertEqual(text, row["text"])
        self.assertIn("记好了", row["brief"])
        self.assertTrue(row["session_id"])

        today = time.strftime("%Y-%m-%d")
        hist = dr.history_list()
        self.assertEqual(1, hist["total"])
        self.assertEqual(1, hist["items"][0]["turns"])
        self.assertEqual(0, hist["items"][0]["failed"])
        summary = dr.history_summary()
        self.assertEqual(1, summary["todayTurns"])
        self.assertIn("记好了", summary["lastBrief"])
        detail = dr.history_detail(today)
        self.assertEqual(1, detail["count"])
        self.assertEqual(text, detail["turns"][0]["text"])
        self.assertTrue(detail["turns"][0]["registered"])

    def test_it_says_so_when_the_note_was_not_written(self):
        """技能没写笔记库（最常见：没装技能）→ **不许**回一个漂亮的假成功。

        这里刻意让日志文件**事先就在**（别的来源建的）：只判"文件在不在"的实现
        会把它当成"这一轮登记好了" —— 那正是要防的假成功。
        """
        note = self._note_path()
        os.makedirs(os.path.dirname(note), exist_ok=True)
        with open(note, "w", encoding="utf-8") as fh:
            fh.write("# 今天\n\n- 早先由别的来源写的一条\n")

        self._patch_client(lambda: _SkillClient(note_path=note, write=False))
        r = self.client.post("/api/daily-review/submit", json={"text": "这一句没登记上"})

        self.assertEqual(200, r.status_code)
        body = r.json()
        # 整理确实有结果（播报是真的），所以 ok 仍是 True……
        self.assertTrue(body["ok"], body.get("error"))
        # ……但"有没有登记"必须单独、如实地回话
        self.assertFalse(body["registered"], "日志没变却被当成登记了")
        self.assertIn("工作日志没更新", body["spoken"], "车里看不到面板，必须在播报里说")
        # 历史详情里这一轮也带着这个事实（界面按它显示）
        detail = dr.history_detail(time.strftime("%Y-%m-%d"))
        self.assertFalse(detail["turns"][0]["registered"])
        # 日志里留痕（排障时能查"技能到底写没写"）
        msgs = [r0["message"] for r0 in db.list_logs(limit=20, source="daily_review")]
        self.assertTrue(any("工作日志" in m for m in msgs), msgs)

    def test_not_enabled_speaks_up_and_still_leaves_a_failed_row(self):
        settings.update({"dailyReviewEnabled": False})
        fake = _SkillClient()
        self._patch_client(lambda: fake)

        r = self.client.post("/api/daily-review/submit", json={"text": "没启用时说的这句"})

        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("未启用", body["error"])
        self.assertTrue(body["spoken"], "车里看不到面板，失败也要有一句人话")
        self.assertEqual([], fake.prompted, "没启用就不该往智能体发指令")
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("failed", rows[0]["status"])
        self.assertIn("未启用", rows[0]["error"])

    def test_a_broken_agent_client_is_reported_and_recorded(self):
        self._patch_client(_BrokenClient("DSH 没装"))

        r = self.client.post("/api/daily-review/submit", json={"text": "智能体没起来时说的这句"})

        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("取智能体客户端失败", body["error"])
        self.assertTrue(body["spoken"])
        # **失败也留痕**：以前这条早退分支跳过落库，历史里什么都查不到
        rows = self._rows()
        self.assertEqual(1, len(rows), "失败的一轮也必须能在历史里查到")
        self.assertEqual("failed", rows[0]["status"])
        self.assertIn("取智能体客户端失败", rows[0]["error"])

    def test_failed_and_successful_turns_are_counted_separately(self):
        dr._persist("成了一轮", {"ok": True, "session_id": "s", "reply": "r",
                                  "spoken": "记好了。", "source": "broadcast",
                                  "error": "", "seconds": 1.0})
        dr._persist("没成的一轮", {"ok": False, "session_id": "s", "reply": "",
                                    "spoken": "这次没等到结果。", "source": "fallback",
                                    "error": "超时", "seconds": 1.0})
        s = dr.history_summary()
        self.assertEqual(2, s["todayTurns"])
        self.assertEqual(1, s["todayFailed"], "失败轮必须单独数出来（卡片不能只说'已回顾 2 轮'）")
        self.assertEqual(1, dr.history_list()["items"][0]["failed"])


class StatusHonestyTests(_ApiBase):
    """`GET /api/daily-review`：`ready` 必须和 `reason` 说同一件事。"""

    def test_not_ready_when_the_agent_client_cannot_be_obtained(self):
        import app.dsh as dsh_mod

        def boom():
            raise RuntimeError("找不到智能体")

        with mock.patch.object(dsh_mod, "get_client", boom):
            r = self.client.get("/api/daily-review")

        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body["enabled"], "设置上是启用的")
        self.assertFalse(body["ready"], "客户端都拿不到却还报就绪 = 点下去才失败（假就绪）")
        self.assertIn("取智能体客户端失败", body["reason"])


class HistoryHonestyTests(_ApiBase):
    """历史：坏日期与"老记录"都不许被说成别的意思。"""

    def test_an_impossible_date_is_rejected_instead_of_looking_empty(self):
        for bad in ("2026-13-99", "2026-02-30", "2026-1-1", "2026/10/06"):
            got = dr.history_detail(bad)
            self.assertFalse(got.get("ok"), "日历上不存在的日期 %r 被当成正常日期了" % bad)
            self.assertIn("YYYY-MM-DD", got.get("error") or "")

    def test_registered_is_absent_for_old_rows_and_false_for_new_failed_registration(self):
        today = time.strftime("%Y-%m-%d")
        # 老记录：那时还没有登记核实 → meta 里没有这个键
        db.add_command("老的一轮", source=dr.REVIEW_SOURCE, status="done",
                       meta={"review": True, "date": today})
        # 新记录：服务端明确没检测到日志更新
        dr._persist("新的一轮", {"ok": True, "session_id": "s", "reply": "r",
                                  "spoken": "记好了。", "source": "broadcast",
                                  "error": "", "seconds": 1.0, "registered": False})

        turns = dr.history_detail(today)["turns"]
        self.assertEqual(2, len(turns))
        self.assertIsNone(turns[0]["registered"], "老记录不许被诬成'没登记'")
        self.assertFalse(turns[1]["registered"])


class StopTests(_ApiBase):
    """`POST /api/daily-review/stop`：把"到底停没停"如实带回去。"""

    def test_stop_reports_whether_a_review_was_actually_stopped(self):
        with mock.patch.object(assistant, "stop_review", lambda: False):
            body = self.client.post("/api/daily-review/stop").json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["stopped"], "没在回顾时不许说'停了'")

        with mock.patch.object(assistant, "stop_review", lambda: True):
            body = self.client.post("/api/daily-review/stop").json()
        self.assertTrue(body["stopped"], "真停了就要说停了")


if __name__ == "__main__":
    unittest.main()
