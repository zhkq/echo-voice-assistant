# -*- coding: utf-8 -*-
"""「回顾历史」的契约测试（2026-10-06 加）。

本文件盯四件事，每件都对应一个真会出问题的场景：

  1. **一轮一条 + 按天分组**：回顾是**多轮**的（车里说一段、DSH 整理一次），
     所以库里一轮一行；而"历史"要按天看 —— 一天一行、轮数留给详情。
     分组错了会让用户看到"同一天出现十几条"，那正是他不想看的。
  2. **失败也留痕**：用户说"昨天那次没记上"时，必须查得出**是哪一轮失败、为什么**。
     只记成功的实现会让这种问题永远查不清。
  3. **摘要取当天最新那条播报**（不是最早那条）：行是按 `ts DESC` 取的，
     实现里最容易写成"被后面更早的覆盖掉"，于是摘要永远显示当天**第一句**。
  4. **日期格式校验**：`history_detail` 收到 `2026/10/06` 这种要**如实说格式不对**，
     而不是静默返回空列表（那会让人以为"那天没回顾"）。

DB 重定向到临时目录（复用 `test_daily_review._Base`），不碰真库。
"""
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from tests.test_daily_review import _Base                            # noqa: E402
from app import daily_review as dr                                   # noqa: E402
import app.db as db                                                  # noqa: E402


def _out(ok=True, **kw):
    """造一份 `submit()` 会返回的结果（只填历史功能关心的字段）。"""
    o = {"ok": ok, "session_id": "sess-1", "workspace": "/w", "reply": "整理稿",
         "spoken": "记好了。", "source": "broadcast", "error": "", "seconds": 2.0}
    o.update(kw)
    return o


class PersistTests(_Base):
    def test_one_row_per_turn_and_grouped_by_day(self):
        dr._persist("第一轮：做了 A", _out())
        dr._persist("第二轮：做了 B", _out())
        rows = db._query("SELECT * FROM commands WHERE source=?", (dr.REVIEW_SOURCE,))
        self.assertEqual(len(rows), 2, "一轮该是一条记录")
        self.assertEqual([r["text"] for r in rows], ["第一轮：做了 A", "第二轮：做了 B"])

        hl = dr.history_list()
        self.assertEqual(hl["total"], 1, "同一天该只出现一行")
        self.assertEqual(hl["items"][0]["turns"], 2)
        self.assertEqual(hl["items"][0]["date"], time.strftime("%Y-%m-%d"))

    def test_a_failed_turn_is_recorded_with_its_reason(self):
        """失败**必须**留痕（status=failed + error 原文）。"""
        dr._persist("这轮没成", _out(ok=False, error="DSH 在超时内没有回复",
                                    spoken="这次整理没等到结果。"))
        row = db._query_one("SELECT * FROM commands WHERE source=?", (dr.REVIEW_SOURCE,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("超时", row["error"])
        self.assertEqual(dr.history_list()["items"][0]["failed"], 1)

    def test_persist_carries_the_meta_the_panel_needs(self):
        """`meta` 里要有 `date` 与 `kind`：前者供按天等值查询，后者对上 DSH 会话。"""
        import json
        dr._persist("口述", _out())
        row = db._query_one("SELECT * FROM commands WHERE source=?", (dr.REVIEW_SOURCE,))
        meta = json.loads(row["meta"])
        self.assertTrue(meta.get("review"))
        self.assertRegex(meta.get("date") or "", r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(str(meta.get("kind") or "").startswith("review:"))
        self.assertEqual(row["brief"], "记好了。", "播报要落在 brief 里")

    def test_other_commands_are_not_counted_as_reviews(self):
        """别的指令（热键/语音唤醒）**不能**被算进回顾历史 —— 靠 `source` 区分。"""
        db.add_command("普通指令", source="wake", status="done")
        self.assertEqual(dr.history_list()["total"], 0)
        self.assertEqual(dr.history_summary()["todayTurns"], 0)


class SummaryTests(_Base):
    def test_summary_counts_today_and_reports_the_latest(self):
        dr._persist("今天第一段", _out(spoken="第一句"))
        time.sleep(1.1)                      # `ts` 是秒级文本，插一条要拉开时间
        dr._persist("今天第二段", _out(spoken="第二句"))
        s = dr.history_summary()
        self.assertEqual(s["todayTurns"], 2)
        self.assertEqual(s["todayFailed"], 0)
        self.assertEqual(s["lastBrief"], "第二句", "摘要要取最新那条的播报")
        self.assertTrue(s["lastAt"])
        self.assertEqual(s["lastStatus"], "done")

    def test_empty_state_is_all_zeros_not_an_error(self):
        """没回顾过时是**空态**，不是错误 —— 面板据此显示"今天还没回顾"。"""
        s = dr.history_summary()
        self.assertEqual(s["todayTurns"], 0)
        self.assertEqual(s["lastAt"], "")
        self.assertEqual(s["lastBrief"], "")
        self.assertEqual(dr.history_list()["items"], [])


class ListBriefTests(_Base):
    def test_the_brief_comes_from_the_newest_turn(self):
        """⚠️ 这条专治一个真实的写法陷阱：行按 `ts DESC` 来，
        用 `if not item["brief"]: item["brief"] = ...` 会被**更早**那条覆盖，
        于是列表永远是当天第一句播报。"""
        dr._persist("早", _out(spoken="早先那句"))
        time.sleep(1.1)
        dr._persist("晚", _out(spoken="最后那句"))
        self.assertEqual(dr.history_list()["items"][0]["brief"], "最后那句")


class DetailTests(_Base):
    def test_turns_are_returned_oldest_first_with_their_own_status(self):
        dr._persist("先说的", _out())
        time.sleep(1.1)
        dr._persist("后说的", _out(ok=False, error="没等到回复"))
        d = dr.history_detail(time.strftime("%Y-%m-%d"))
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 2)
        self.assertEqual([t["text"] for t in d["turns"]], ["先说的", "后说的"],
                         "详情要按「先说的在前」读")
        self.assertEqual([t["status"] for t in d["turns"]], ["done", "failed"])
        self.assertEqual(d["turns"][1]["error"], "没等到回复")

    def test_the_vault_note_path_is_reported_even_when_missing(self):
        """工作日志**不在**也要给出路径 —— 面板据此说"日志还没生成"，
        而不是把按钮藏起来让人猜。"""
        d = dr.history_detail(time.strftime("%Y-%m-%d"))
        self.assertTrue(d["vaultNote"].endswith(".md"))
        self.assertIn(os.path.join("01-工作日志"), d["vaultNote"])
        self.assertFalse(d["vaultNoteExists"])
        # 造一个出来，再看它认不认
        os.makedirs(os.path.dirname(d["vaultNote"]), exist_ok=True)
        with open(d["vaultNote"], "w", encoding="utf-8") as fh:
            fh.write("# 今天\n")
        self.assertTrue(dr.history_detail(time.strftime("%Y-%m-%d"))["vaultNoteExists"])

    def test_a_bad_date_says_so_instead_of_returning_empty(self):
        for bad in ("2026/10/06", "", "20261006", "2026-10"):
            got = dr.history_detail(bad)
            self.assertFalse(got.get("ok"), "坏日期 %r 竟然被当成正常" % bad)
            self.assertIn("YYYY-MM-DD", got.get("error") or "")

    def test_a_day_without_reviews_is_an_empty_but_valid_answer(self):
        got = dr.history_detail("2020-01-01")
        self.assertTrue(got["ok"])
        self.assertEqual(got["count"], 0)
        self.assertEqual(got["turns"], [])
