# -*- coding: utf-8 -*-
"""纪要回复开头的「开场白 + 结构化元数据 JSON」必须被剪掉（2026-10-13 用户实测）。

用户原话："稳定版今天上午的会议纪要上面也展示结构化内容了"。
那一场的 `summary.md` 开头是：一句英文开场白 + `## 结构化元数据 JSON` + 一个 ```json 块
（标题/简介/摘要/分段，与 `topics.md` 几乎一模一样）。元数据有它自己的落点
（`request_topic_segments` → topics.md），纪要靠它就成了"面板上先给用户看一坨 JSON"。

这里钉两件事：
  1. `_strip_summary_head` 的**保守边界** —— 只剪正文之前那一段，真纪要一个字不能少；
  2. 落盘路径（`_spawn_summary_waiter`）**真的调了它** —— 光有函数没人用等于没修。
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from app import meeting


#: 复刻这一次真实的坏形状（开场白 + 元数据小节 + JSON 块 + 分隔线 + 真纪要）。
LEAKED = (
    "Now I have the full transcript. Let me analyze it and generate both the structured "
    "metadata JSON and the meeting minutes markdown.\n"
    "\n"
    "---\n"
    "\n"
    "## 结构化元数据 JSON\n"
    "\n"
    "```json\n"
    "{\n"
    '  "标题": "智能体平台2.0方案评审",\n'
    '  "简介": "评审智能体平台2.0功能设计",\n'
    '  "摘要": "会议围绕智能体平台2.0版本的功能设计展开评审。",\n'
    '  "分段": [\n'
    '    {"标题": "1.0遗留问题与2.0功能概览", "开始": "00:00", "结束": "10:00", "摘要": "讨论1.0现状。"}\n'
    "  ]\n"
    "}\n"
    "```\n"
    "\n"
    "---\n"
    "\n"
    "# 智能体平台 2.0 方案评审会议纪要\n"
    "\n"
    "## 一、会议背景与议题\n"
    "\n"
    "本次会议对智能体平台 2.0 版本的功能设计方案进行集中评审。\n"
    "\n"
    "## 三、待办事项及责任人\n"
    "\n"
    "| 序号 | 待办事项 | 责任人 |\n"
    "|:---:|:---|:---:|\n"
    "| 1 | 重新设计团队开发工作台 | 开发团队 |\n"
)

MINUTES_HEAD = "# 智能体平台 2.0 方案评审会议纪要"


class StripSummaryHeadTests(unittest.TestCase):

    def test_the_leaked_preamble_and_metadata_are_gone(self):
        out = meeting._strip_summary_head(LEAKED)
        self.assertTrue(out.startswith(MINUTES_HEAD),
                        "纪要必须从正文标题开始：%r" % out[:80])
        self.assertNotIn("结构化元数据", out)
        self.assertNotIn('"分段"', out)
        self.assertNotIn("Now I have the full transcript", out)

    def test_the_real_minutes_are_kept_whole(self):
        out = meeting._strip_summary_head(LEAKED)
        for keep in ("## 一、会议背景与议题",
                     "本次会议对智能体平台 2.0 版本的功能设计方案进行集中评审。",
                     "| 序号 | 待办事项 | 责任人 |",
                     "| 1 | 重新设计团队开发工作台 | 开发团队 |"):
            self.assertIn(keep, out, "真纪要内容被剪掉了：%s" % keep)

    def test_a_clean_summary_is_left_byte_identical(self):
        clean = "# 会议纪要\n\n## 一、背景\n\n正文一段。\n"
        self.assertEqual(meeting._strip_summary_head(clean), clean,
                         "本来就干净的纪要不该被动")

    def test_a_json_example_after_the_body_is_not_eaten(self):
        """正文里也有 ```json 例子时，绝不能把真纪要整段吃掉（这是最危险的误剪）。"""
        src = ("# 会议纪要\n\n## 一、背景\n\n下面是个示例：\n\n"
               "```json\n{\"分段\": [], \"标题\": \"示例\"}\n```\n\n正文结尾。\n")
        self.assertEqual(meeting._strip_summary_head(src), src)

    def test_a_summary_starting_with_content_is_left_alone(self):
        src = "## 会议纪要\n\n好的，以下是本次讨论的要点。\n"
        self.assertEqual(meeting._strip_summary_head(src), src,
                         "正文里的『好的』不在行首，不该被当开场白剪掉")

    def test_leading_chatter_without_a_metadata_block_is_still_dropped(self):
        src = "好的，我先把转写文件读一遍再写纪要。\n\n# 智能体平台 2.0 评审纪要\n\n正文。\n"
        out = meeting._strip_summary_head(src)
        self.assertTrue(out.startswith("# 智能体平台 2.0 评审纪要"), out[:60])

    def test_nothing_to_strip_returns_the_original_object_unchanged(self):
        src = "# 纪要\n\n正文。"
        self.assertIs(meeting._strip_summary_head(src), src,
                      "剪不动时要原样返回（连对象都不该换）")


class _FakeClient:
    """只兑现 `_spawn_summary_waiter` 用到的那三个方法。"""

    def __init__(self, reply):
        self.reply = reply

    def clear_stuck(self, sid):
        pass

    def prompt(self, sid, text, mode="queue"):
        pass

    def wait_for_reply(self, sid, timeout=0, poll=0):
        return (self.reply, True)


def _wait_for_file(path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(0.02)
    return False


class TheWritePathUsesItTests(unittest.TestCase):
    """落盘路径必须真调用（函数写了没人用 = 没修）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summary-head-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.logs = []
        patcher = mock.patch.object(
            meeting.db, "add_log", lambda level, source, msg: self.logs.append((level, msg)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_agent_path_writes_a_clean_summary(self):
        folder = self.tmp
        path = os.path.join(folder, "summary.md")
        fake = _FakeClient(LEAKED)
        with mock.patch.object(meeting, "get_client", lambda: fake), \
                mock.patch.object(meeting, "_summary_session", lambda client, mid: "sid-1"):
            meeting._spawn_summary_waiter(7, folder, "summary.md", "写纪要", "纪要")
            self.assertTrue(_wait_for_file(path), "纪要应该落盘")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertTrue(body.startswith(MINUTES_HEAD), body[:80])
        self.assertNotIn('"分段"', body)
        self.assertTrue(any("结构化元数据" in m for _lv, m in self.logs),
                        "剪掉了要留痕，否则下次没人知道面板上那坨 JSON 去哪了：%s" % self.logs)

    def test_a_clean_reply_is_written_as_before(self):
        folder = self.tmp
        path = os.path.join(folder, "summary.md")
        clean = "# 会议纪要\n\n## 一、背景\n\n正文一段。\n"
        fake = _FakeClient(clean)
        with mock.patch.object(meeting, "get_client", lambda: fake), \
                mock.patch.object(meeting, "_summary_session", lambda client, mid: "sid-1"):
            meeting._spawn_summary_waiter(7, folder, "summary.md", "写纪要", "纪要")
            self.assertTrue(_wait_for_file(path))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), clean,
                             "干净回复的落盘内容不该有任何变化（含结尾换行）")


if __name__ == "__main__":
    unittest.main()
