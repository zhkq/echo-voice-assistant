# -*- coding: utf-8 -*-
"""首次启用向导 S1 的「下一步」：**真的测一次模型能不能把话答回来**（2026-10-11 用户口径）。

为什么不能只回 "配置已保存"：用户的原话是"点下一步要**测试模型是否可用**"——
配好 key/命令之后，唯一的判据是"让当前智能体答一句话"。
（`available(probe=...)` 只证明进程/凭据在，不证明模型答得出来 —— 两者是两回事。）

用例全打桩：**不打网络、不建真会话、不花 token**。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                            # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from app import agents as agents_mod                                    # noqa: E402
from app.api import router                                              # noqa: E402


class _FakeAgent:
    def __init__(self, reply="可用", available=(True, ""), raise_on=(), sessions=None):
        self.reply = reply
        self._available = available
        self._raise_on = set(raise_on)
        self.created = 0
        self.asked = []

    def available(self, probe=False):
        return self._available

    def create_session(self, cwd=None, workspace_id=None):
        self.created += 1
        if "create_session" in self._raise_on:
            raise RuntimeError("DSH RPC session/create 失败: workspace/not-found")
        return "session-1"

    def ask(self, session_id, text, timeout=90, poll=0.5, mode="queue"):
        self.asked.append((session_id, text))
        if "ask" in self._raise_on:
            raise RuntimeError("harness 登录失败：拒绝连接")
        return (self.reply, True)


def _call(fake, name="harness"):
    """跑一次端点（TestClient + 把当前智能体换成假的；auth 在内存里遮掉）。"""
    from tests.auth_off import api_auth_off
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    with api_auth_off(), \
            patch.object(agents_mod, "active_agent", lambda: fake), \
            patch.object(agents_mod, "active_name", lambda: name):
        r = client.post("/api/wizard/test-agent")
    return r


class TestAgentEndpointTests(unittest.TestCase):
    def test_a_real_answer_is_the_only_pass(self):
        fake = _FakeAgent(reply="可用")
        r = _call(fake)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual("可用", body["reply"])
        self.assertEqual("harness", body["agent"])
        self.assertEqual(1, fake.created, "要真的开一个会话")
        self.assertEqual(1, len(fake.asked), "要真的问一句")
        self.assertLess(body["elapsed"], 60)

    def test_it_says_which_agent_and_what_to_do_when_unavailable(self):
        fake = _FakeAgent(available=(False, "标准版 harness 没在运行：在 设置 → 智能体 里选中它"))
        body = _call(fake).json()
        self.assertFalse(body["ok"])
        self.assertEqual("unavailable", body["reason"])
        self.assertIn("没在运行", body["message"])
        self.assertEqual(0, fake.created, "不可用就别去开会话（会白等几十秒）")

    def test_no_agent_selected_is_its_own_reason(self):
        body = _call(None).json()
        self.assertFalse(body["ok"])
        self.assertEqual("no-agent", body["reason"])
        self.assertIn("设置", body["message"])

    def test_a_broken_session_is_reported_not_swallowed(self):
        fake = _FakeAgent(raise_on=("create_session",))
        body = _call(fake).json()
        self.assertFalse(body["ok"])
        self.assertEqual("session", body["reason"])
        self.assertIn("session/create", body["message"])

    def test_a_failing_ask_is_reported(self):
        fake = _FakeAgent(raise_on=("ask",))
        body = _call(fake).json()
        self.assertFalse(body["ok"])
        self.assertEqual("error", body["reason"])
        self.assertIn("拒绝连接", body["message"])

    def test_an_empty_reply_does_not_count_as_success(self):
        """智能体"答了但什么都没说"不算通过 —— 那正是"模型没接上"的样子。"""
        body = _call(_FakeAgent(reply="")).json()
        self.assertFalse(body["ok"])
        self.assertEqual("no-reply", body["reason"])

    def test_the_prompt_is_short_and_says_what_to_reply(self):
        """这条会**花钱**：提示词必须极短，且明确要求几个字 —— 用户看得懂、也便宜。"""
        fake = _FakeAgent()
        _call(fake)
        sid, text = fake.asked[0]
        self.assertLess(len(text), 60, "提示词太长就是在烧 token")
        self.assertIn("回复", text)


if __name__ == "__main__":
    unittest.main()
