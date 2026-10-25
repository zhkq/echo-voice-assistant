# -*- coding: utf-8 -*-
"""手机回顾的「准备任务」：`/api/daily-review/pre*`（2026-10-25）。

背景（App 开发 Agent 的诊断与用户实测）：
    POST /api/daily-review/pre 在旧 ECHO（≤2.1.7）里根本不存在 → App 按 §7.4
    兼容矩阵降级成「这台电脑的 ECHO 还不支持回顾准备」。服务端补上这三条接口，
    手机才能"开始回顾后先看到准备提示、等准备结束再反馈、然后多轮完善"。

本文件钉住的事（每条都对应一种"看起来通、其实没做"的假象）：
  1. `POST /pre` **立即**返回 `ack/topics/etaSec`，不是等扫完才返回；
  2. 后台只给智能体发**一条**准备 prompt；再点一次 → `already=true`，不重发；
  3. 今天已完成 → `done=true`，不重新扫；
  4. `GET /pre` 只读（不建 client / 不建会话），`phase` 从 `pre` 走到 `done`，
     `reply` 带计数；
  5. `/pre/stop` 与 `/stop` 都停准备，并如实回 `preStopped`；
  6. **全程不碰麦克风**（不调 `assistant.start_review`、不开录）；
  7. PC 的 `preflight()` 与手机的 `/pre` 共用一份准备（只发一条 prompt）。
"""
import threading
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_daily_review import _Base
from app import assistant, daily_review
from app.api import router


class _FakeClient:
    """替身：记录 prompt；`wait_for_reply` 按脚本回（可被 gate 卡住）。"""

    def __init__(self, gate=None, replies=None):
        self.gate = gate
        self.replies = (list(replies) if replies is not None
                        else ["前置环节完成：文件 2 份（专项 1）、会议 1 场"])
        self.prompts = []
        self._i = 0

    def prompt(self, session_id, text):
        self.prompts.append((session_id, text))
        return True

    def wait_for_reply(self, session_id, timeout=0, poll=0.5, cancel_on_timeout=True):
        if self.gate is not None:
            self.gate.wait(2.0)
        if self._i < len(self.replies):
            reply = self.replies[self._i]
            self._i += 1
            return (reply, True)
        return ("", False)


class _PrepApi(_Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self._patches = [
            mock.patch.object(daily_review, "ready", lambda: (True, "")),
            mock.patch.object(daily_review, "ensure_access", lambda *a, **k: (True, "")),
            mock.patch.object(daily_review, "ensure_session",
                              lambda *a, **k: ("S1", "W1", {"workspace": "W1"})),
            mock.patch.object(daily_review, "vault_root", lambda: "V"),
            mock.patch.object(daily_review, "build_pre_prompt", lambda vault="": "PRE-PROMPT"),
            mock.patch.object(daily_review, "today_meetings", lambda now=None: []),
            mock.patch.object(daily_review, "PRE_POLL_S", 0.01),
            mock.patch.object(daily_review, "PRE_MAX_S", 2.0),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        self._busy = dict(assistant._busy_owner)
        assistant._busy_owner.update({"name": None, "phase": None})
        self.addCleanup(lambda: assistant._busy_owner.update(self._busy))

        self._reset_pre()
        self.addCleanup(self._reset_pre)

    def _reset_pre(self):
        daily_review.stop_pre()
        with daily_review._PRE_LOCK:
            daily_review._PRE.update({
                "phase": "idle", "day": "", "startedAt": 0.0, "seconds": 0.0,
                "finished": False, "etaSec": 60, "error": "", "nudges": 0,
                "ack": "", "topics": [], "reply": "", "sessionId": "",
                "gen": 0, "owner": "", "doneEvent": None, "stopEvent": None,
                "thread": None,
            })

    def _use(self, client):
        p = mock.patch.object(daily_review, "_client", lambda: client)
        p.start()
        self.addCleanup(p.stop)

    def _wait_done(self, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            st = daily_review.pre_state()
            if st["phase"] != "pre":
                return st
            time.sleep(0.02)
        return daily_review.pre_state()


class ImmediateAckTests(_PrepApi):
    def test_post_pre_returns_ack_topics_eta_without_waiting(self):
        fake = _FakeClient()
        self._use(fake)

        t0 = time.time()
        r = self.client.post("/api/daily-review/pre")
        elapsed = time.time() - t0
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["started"], "第一次必须真的启动准备")
        self.assertFalse(body["already"])
        self.assertFalse(body["done"])
        self.assertIn("企微", body["ack"], "ack 要说清在扫什么")
        self.assertIn("回顾准备", body["ack"])
        self.assertIn("一分钟", body["ack"], "ack 要说预计多久")
        self.assertEqual(4, len(body["topics"]), "本次回顾会梳理：最多 4 条")
        self.assertEqual("今天的会议：ECHO 里没有会议记录", body["topics"][0])
        self.assertEqual(60, body["etaSec"])
        self.assertLess(elapsed, 1.5, "准备必须立刻返回，不能等扫完")

        st = self._wait_done()
        self.assertEqual("done", st["phase"], st)
        self.assertTrue(st["finished"])
        self.assertIn("前置环节完成", st["reply"])
        self.assertEqual(1, len(fake.prompts), "只允许发一条准备 prompt")
        self.assertEqual("PRE-PROMPT", fake.prompts[0][1])
        self.assertEqual("S1", fake.prompts[0][0])

    def test_get_pre_is_read_only_and_reports_progress(self):
        r = self.client.get("/api/daily-review/pre")
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual("idle", body["pre"]["phase"])
        # 只读：没有 client、没有 prompt
        fake = _FakeClient()
        self._use(fake)
        self.client.get("/api/daily-review/pre")
        self.assertEqual([], fake.prompts)


class IdempotencyTests(_PrepApi):
    def test_second_post_is_already_and_does_not_prompt_again(self):
        gate = threading.Event()
        fake = _FakeClient(gate=gate)
        self._use(fake)
        try:
            first = self.client.post("/api/daily-review/pre").json()
            self.assertTrue(first["started"], first)
            time.sleep(0.05)
            second = self.client.post("/api/daily-review/pre").json()
            self.assertTrue(second["ok"], second)
            self.assertFalse(second["started"])
            self.assertTrue(second["already"], "已经在跑 → already=true")
            self.assertEqual(1, len(fake.prompts), "第二次不许再发 prompt")
        finally:
            gate.set()
            self._wait_done()

    def test_today_done_returns_done_without_rescanning(self):
        fake = _FakeClient()
        self._use(fake)
        self.client.post("/api/daily-review/pre")
        st = self._wait_done()
        self.assertEqual("done", st["phase"])

        again = self.client.post("/api/daily-review/pre").json()
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["done"], "今天已完成 → done=true")
        self.assertFalse(again["started"])
        self.assertEqual(1, len(fake.prompts), "已完成不许重新扫")
        self.assertIn("前置环节完成", again["pre"]["reply"])


class StopTests(_PrepApi):
    def test_pre_stop_only_stops_prep_and_reports_pre_stopped(self):
        fake = _FakeClient(replies=[])          # 永远不给完成标记 → 一直循环
        self._use(fake)
        started = self.client.post("/api/daily-review/pre").json()
        self.assertTrue(started["started"], started)
        time.sleep(0.05)

        body = self.client.post("/api/daily-review/pre/stop").json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["preStopped"], "真的停了一个准备")
        self.assertEqual("stopped", body["pre"]["phase"])
        self.assertIn("跳过", body["pre"]["error"])

        again = self.client.post("/api/daily-review/pre/stop").json()
        self.assertFalse(again["preStopped"], "本来就没在跑 → false")

    def test_endpoint_stop_also_stops_prep(self):
        fake = _FakeClient(replies=[])
        self._use(fake)
        started = self.client.post("/api/daily-review/pre").json()
        self.assertTrue(started["started"], started)
        time.sleep(0.05)
        with mock.patch.object(assistant, "stop_review", lambda: False):
            body = self.client.post("/api/daily-review/stop").json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["stopped"], "PC 那场本来就没在跑")
        self.assertTrue(body["preStopped"], "但准备确实被这次结束停掉了")


class GuardTests(_PrepApi):
    def test_not_ready_returns_ok_false_and_no_prompt(self):
        fake = _FakeClient()
        self._use(fake)
        with mock.patch.object(daily_review, "ready", lambda: (False, "没配笔记库")):
            body = self.client.post("/api/daily-review/pre").json()
        self.assertFalse(body["ok"])
        self.assertIn("没配笔记库", body["error"])
        self.assertEqual([], fake.prompts)

    def test_busy_returns_reason_and_no_prompt(self):
        fake = _FakeClient()
        self._use(fake)
        assistant._busy_owner["name"] = "meeting"
        self.addCleanup(lambda: assistant._busy_owner.update({"name": None}))
        body = self.client.post("/api/daily-review/pre").json()
        self.assertFalse(body["ok"])
        self.assertIn("电脑正忙（meeting）", body["error"])
        self.assertEqual([], fake.prompts)

    def test_it_never_opens_the_microphone(self):
        fake = _FakeClient()
        self._use(fake)
        called = []
        with mock.patch.object(assistant, "start_review",
                               lambda source="wake": called.append(source) or True):
            self.client.post("/api/daily-review/pre")
            self._wait_done()
        self.assertEqual([], called, "准备绝不许进回顾模式/开麦")

    def test_phone_ack_is_not_the_pc_ack(self):
        phone = daily_review.pre_phone_ack_text(60)
        pc = daily_review.pre_ack_text(60)
        self.assertIn("扫完我就提示你", phone)
        self.assertIn("录你说的内容", pc)
        self.assertNotEqual(phone, pc)


class TopicsTests(_PrepApi):
    def test_topics_include_todays_meetings_and_are_short(self):
        meetings = [
            {"time": "10:00", "title": "周会", "hasSummary": True},
            {"time": "15:30", "title": "需求评审", "hasSummary": False},
            {"time": "17:00", "title": "技术评审", "hasSummary": True},
            {"time": "18:00", "title": "第四场", "hasSummary": False},
        ]
        with mock.patch.object(daily_review, "today_meetings", lambda now=None: meetings):
            topics = daily_review.pre_topics()
        self.assertEqual(4, len(topics))
        self.assertIn("今天的会议（4 场）", topics[0])
        self.assertIn("周会", topics[0])
        self.assertIn("（纪要已生成）", topics[0])
        self.assertIn("等", topics[0], "超过 3 场要说明还有")
        for line in topics:
            self.assertLessEqual(len(line), 60, "每条 ≤60 字：%s" % line)


class SharedWithPcTests(_PrepApi):
    def test_pc_preflight_joins_the_running_phone_prep(self):
        gate = threading.Event()
        fake = _FakeClient(gate=gate)
        self._use(fake)
        try:
            self.client.post("/api/daily-review/pre")
            time.sleep(0.05)
            ack = []
            result = {}

            def run_pc():
                result.update(daily_review.preflight(client=fake, on_ack=ack.append))

            th = threading.Thread(target=run_pc)
            th.start()
            time.sleep(0.05)
            gate.set()
            th.join(3.0)
            self.assertFalse(th.is_alive(), "PC 的 preflight 应加入等待并结束")
            self.assertEqual(1, len(fake.prompts), "只允许一条准备 prompt")
            self.assertTrue(result.get("finished"), result)
            self.assertTrue(ack, "PC 加入时也要先出声")
        finally:
            gate.set()


if __name__ == "__main__":
    unittest.main()
