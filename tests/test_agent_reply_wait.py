# -*- coding: utf-8 -*-
"""`wait_for_reply` 的"超时要不要取消会话"契约（`app/agents/dsh_agent.py`）。

背景（2026-10-14 真机事故，"每日回顾不工作了"）
================================================

`DshAgent.wait_for_reply` 原来在超时且没拿到回复时**一律** `session/cancel` ——
对"发一条、等这一轮回复"的调用方（命令、回顾提交）是对的：等这么久没动静就是卡了。

但**小窗口轮询**的调用方（每日回顾的前置环节：每 3 秒探一次，等一条要跑几分钟的长活）
不是这样：`reply` 只看"本次调用之后的新消息"，所以"这 3 秒没有新的助手文本"极其常见，
而取消会**把正在干活的会话打断**。现场证据（DSH 会话事件）：

  * `turn/end` 的 reason 是 `{"kind":"aborted","reason":{"kind":"user"}}`
    （DSH 侧唯一的来源就是 `session/cancel`：`agent.cancel({kind:'user'})`）；
  * 工具结果报 `glob was aborted before completion (tool timeout or caller cancellation)`；
  * 每一轮都显示「已停止」，智能体永远做不完前置环节。

所以开关叫 `cancel_on_timeout`：默认 True（老行为不动），前置环节传 False。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.dsh_agent import DshAgent                                # noqa: E402


class _Stub(DshAgent):
    """只在 RPC 层上面打桩：不连真 harness、不发 HTTP。"""

    def __init__(self, running=False):
        super().__init__(base_url="http://127.0.0.1:1")
        self.sid = "session-x"
        self.running = running
        self.cancels = []

    def list_sessions(self):
        return [{"sessionId": self.sid, "running": self.running}]

    def history(self, session_id, max_messages=80):
        return []

    def cancel(self, session_id):
        self.cancels.append(session_id)


class _StubWithReply(_Stub):
    """第一次取游标时还没有事件，之后这一轮有助手文本（模拟"探到了回复"）。"""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def history(self, session_id, max_messages=80):
        self.calls += 1
        if self.calls == 1:
            return []
        return [
            {"event": {"seq": 7, "type": "assistant/message",
                       "data": {"message": {"role": "assistant",
                                            "content": [{"type": "text", "text": "好了"}]}}}},
            {"event": {"seq": 8, "type": "turn/end",
                       "data": {"turn": 1, "reason": {"kind": "completed"}}}},
        ]


class CancelOnTimeoutTests(unittest.TestCase):
    def test_by_default_a_silent_timeout_still_cancels(self):
        """默认行为不许变：发一条等一轮的调用方，卡住了就该取消（否则下一条进不来）。"""
        c = _Stub()
        reply, done = c.wait_for_reply(c.sid, timeout=0.2, poll=0.05)
        self.assertIsNone(reply)
        self.assertTrue(done, "会话空闲要如实报出来")
        self.assertEqual(c.cancels, [c.sid])

    def test_it_can_be_told_not_to_cancel(self):
        """前置环节就是这样调的：小窗口轮询，超时只代表"这段没有新消息"。"""
        c = _Stub()
        reply, done = c.wait_for_reply(c.sid, timeout=0.2, poll=0.05,
                                       cancel_on_timeout=False)
        self.assertIsNone(reply)
        self.assertTrue(done)
        self.assertEqual(c.cancels, [], "cancel_on_timeout=False 时绝不许取消会话")

    def test_a_reply_still_avoids_the_cancel(self):
        """两种调用方式下，拿到回复都不该取消（默认路径的既有语义也别改坏）。"""
        for flag in (True, False):
            with self.subTest(cancel_on_timeout=flag):
                c = _StubWithReply()
                reply, _done = c.wait_for_reply(c.sid, timeout=0.3, poll=0.05,
                                                cancel_on_timeout=flag)
                self.assertEqual(reply, "好了")
                self.assertEqual(c.cancels, [])


if __name__ == "__main__":
    unittest.main()
