# -*- coding: utf-8 -*-
"""2026-10-22 架构守卫：ECHO 的纪要/推理只走配置的智能体，不直连 LLM。

这些用例钉住三件事：
1. 智能体不可用时，`request_summary` / `request_topic_segments` **直接失败**，
   不会静默改走任何 LLM provider；
2. 智能体可用时，只调用 `_spawn_summary_waiter`（agent 路径），prompt 里带追加要求；
3. 直连 LLM 的实现函数已彻底不存在（防止以后被"顺手加回来"）。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import meeting


class AgentOnlySummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name

    def test_summary_requires_an_agent(self):
        with patch.object(meeting, "_agent_ready_for_reasoning", return_value=False), \
                patch.object(meeting.db, "get_meeting", return_value={"name": "m"}), \
                patch.object(meeting.db, "add_log") as log, \
                patch.object(meeting, "_spawn_summary_waiter") as spawn:
            self.assertFalse(meeting.request_summary(1, folder=self.folder))
        spawn.assert_not_called()
        self.assertTrue(log.called)

    def test_summary_goes_to_the_agent_and_keeps_extra(self):
        with patch.object(meeting, "_agent_ready_for_reasoning", return_value=True), \
                patch.object(meeting.db, "get_meeting", return_value={"name": "m"}), \
                patch.object(meeting, "_spawn_summary_waiter") as spawn:
            self.assertTrue(meeting.request_summary(1, folder=self.folder, extra="只要结论"))
        spawn.assert_called_once()
        args = spawn.call_args[0]
        self.assertEqual(args[2], "summary.md")
        self.assertIn("只要结论", args[3])
        self.assertIn("read", args[3])

    def test_topic_segments_require_an_agent(self):
        with patch.object(meeting, "_agent_ready_for_reasoning", return_value=False), \
                patch.object(meeting.db, "get_meeting", return_value={"name": "m"}), \
                patch.object(meeting.db, "add_log"), \
                patch.object(meeting, "_spawn_summary_waiter") as spawn:
            self.assertFalse(meeting.request_topic_segments(1, folder=self.folder))
        spawn.assert_not_called()

    def test_topic_segments_go_to_the_agent(self):
        with patch.object(meeting, "_agent_ready_for_reasoning", return_value=True), \
                patch.object(meeting.db, "get_meeting", return_value={"name": "m"}), \
                patch.object(meeting, "_spawn_summary_waiter") as spawn:
            self.assertTrue(meeting.request_topic_segments(1, folder=self.folder))
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args[0][2], "topics.md")

    def test_direct_llm_helpers_are_gone(self):
        for name in ("direct_llm_decision", "_llm_provider_for_summary",
                     "_provider_summary_text", "_spawn_provider_summary"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(meeting, name), "%s 不应再存在" % name)


if __name__ == "__main__":
    unittest.main()
