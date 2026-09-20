# -*- coding: utf-8 -*-
"""P5：纪要的**直连 LLM** 路径（"不装 agent 也能出纪要"的验收点）

背景：1.x 的纪要**必须**经 agent（DSH 会话 + read 工具读 transcript.md）。P5 的承诺是：
配一个在线 LLM（或让 ECHO AUTO 路由可用）就能出纪要，**装不装 agent 都不影响**。

本文件钉住五件事：
1. **选择判据**（2026-09-20 改成 agent-first）：**agent（DSH）可用就一律走 agent**（纪要/分段/
   归档共用同一会话）；只有 agent 不可用而 LLM provider 就绪时才切直连。**显式设了
   `providerLlm` 不再抢走 agent 的活** —— 因为 `providerLlm` 的 `""` 与 `"echo-auto"` 生效
   provider 完全一样（`default_id("llm") == "echo-auto"`），按"原始设置非空"判会静默关掉
   agent 路径；
2. **材料内联**：直连路径必须把转写正文放进 prompt（LLM 没有 read 工具），且超长要**说明被截断**；
3. **落盘纪律与 agent 路径一致**：过短/疑似占位话**不回写**，失败写 error 日志；
4. **不串台**：走直连时**完全不动** DSH 客户端（用"一调用就炸"的假客户端证明）；
5. 共享的格式要求（Mermaid 那些坑）两条路**引用同一份**，不许各写一遍。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import meeting                                      # noqa: E402

TRANSCRIPT = "# 会议转写\n\n## 第 1 段 [00-00-00]\n[00.00.12] 张三：今天讨论验收流程。\n"


class _FakeLlm:
    """假的 LLM provider：记录收到的 messages，按脚本回复。"""

    def __init__(self, reply="## 会议纪要\n\n- 议题一：验收流程\n- 待办：张三 负责初验证书",
                 error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def ready(self):
        return True

    def chat(self, messages, timeout=60, **kw):
        self.calls.append({"messages": messages, "timeout": timeout})
        if self.error:
            raise self.error
        return self.reply


class _BoomClient:
    """DSH 客户端替身：**任何调用都炸** —— 用来证明直连路径不碰 agent。"""

    def __getattr__(self, name):
        raise AssertionError("直连路径不该调用 DSH 客户端（.%s）" % name)


def _mk_meeting_dir(tmp, transcript=TRANSCRIPT):
    folder = os.path.join(tmp, "2026-09-19_10-00-00")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "transcript.md"), "w", encoding="utf-8") as fh:
        fh.write(transcript)
    return folder


def _wait_for_file(path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isfile(path):
            return True
        time.sleep(0.02)
    return os.path.isfile(path)


class DecisionTests(unittest.TestCase):
    """选择判据（纯逻辑，不落盘）。"""

    def _settings(self, provider_llm=""):
        return patch("app.config.settings.get",
                     lambda k, d=None: provider_llm if k == "providerLlm" else d)

    def test_agent_available_always_wins_even_with_an_explicit_provider(self):
        """**关键守卫**：agent 可用时，显式设了 `providerLlm` 也**不许**抢走纪要。

        原判据是"`providerLlm` 非空就强制直连"，但 `""` 与 `"echo-auto"` 的生效 provider
        完全一样（`default_id("llm") == "echo-auto"`）—— 于是"在面板里选了 ECHO AUTO"
        会静默把纪要从 agent 切到直连（2026-09-20 实测踩到）。
        """
        fake = _FakeLlm()
        for chosen in ("openai-llm", "echo-auto"):
            with self._settings(chosen), \
                    patch("app.manager.dsh_ready", lambda: True), \
                    patch.object(meeting, "_llm_provider_for_summary", lambda: (fake, chosen)):
                use, why = meeting.direct_llm_decision()
            self.assertFalse(use, "providerLlm=%r 时也不该绕过 agent" % chosen)
            self.assertIn("agent", why)

    def test_agent_available_keeps_the_old_path(self):
        with self._settings(""), \
                patch("app.manager.dsh_ready", lambda: True), \
                patch.object(meeting, "_llm_provider_for_summary", lambda: (_FakeLlm(), "echo-auto")):
            use, why = meeting.direct_llm_decision()
        self.assertFalse(use, "agent 可用时不该抢它的活（老用户行为零变化）")
        self.assertIn("agent", why)

    def test_no_agent_but_provider_ready_switches_to_direct(self):
        with self._settings(""), \
                patch("app.manager.dsh_ready", lambda: False), \
                patch.object(meeting, "_llm_provider_for_summary",
                             lambda: (_FakeLlm(), "echo-auto")):
            use, why = meeting.direct_llm_decision()
        self.assertTrue(use, "P5 的承诺：不装 agent 也能出纪要")
        self.assertIn("echo-auto", why)

    def test_neither_available_falls_back_to_agent_path(self):
        with self._settings(""), \
                patch("app.manager.dsh_ready", lambda: False), \
                patch.object(meeting, "_llm_provider_for_summary",
                             lambda: (None, "没有可用的 LLM provider")):
            use, why = meeting.direct_llm_decision()
        self.assertFalse(use, "两边都不可用时保持老路（错误由原路径报）")
        self.assertIn("没有可用", why)

    def test_provider_choice_no_longer_participates_in_routing(self):
        """`providerLlm` 已不再参与这条路的路由（老判据 "非空就强制直连" 已删）。

        agent 可用时，无论这个设置是空、是坏的（ghost）、还是路由（echo-auto），结果都一样。
        """
        for chosen in ("", "ghost", "echo-auto", "openai-llm"):
            with self._settings(chosen), \
                    patch("app.manager.dsh_ready", lambda: True):
                use, why = meeting.direct_llm_decision()
            self.assertFalse(use, "providerLlm=%r 不该影响判定" % chosen)
        # agent 不可用时仍然认 provider（P5 的兜底）
        with self._settings("ghost"), \
                patch("app.manager.dsh_ready", lambda: False), \
                patch.object(meeting, "_llm_provider_for_summary",
                             lambda: (None, "没有可用的 LLM provider")):
            use, why = meeting.direct_llm_decision()
        self.assertFalse(use, "兜底 provider 取不到时保持 agent 路（由原路径报错）")
        self.assertTrue(why)


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summary-prov-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.folder = _mk_meeting_dir(self.tmp)

    def test_transcript_is_inlined(self):
        text = meeting._provider_summary_text(self.folder)
        self.assertIn("今天讨论验收流程", text, "LLM 没有 read 工具，正文必须内联")

    def test_missing_transcript_is_reported(self):
        empty = os.path.join(self.tmp, "empty-meeting")
        os.makedirs(empty, exist_ok=True)
        with self.assertRaises(RuntimeError) as cm:
            meeting._provider_summary_text(empty)
        self.assertIn("没有可用的转写文本", str(cm.exception))

    def test_long_transcript_is_truncated_and_says_so(self):
        big = _mk_meeting_dir(self.tmp, transcript="x" * (meeting._PROVIDER_TRANSCRIPT_LIMIT + 500))
        text = meeting._provider_summary_text(big)
        self.assertLessEqual(len(text), meeting._PROVIDER_TRANSCRIPT_LIMIT + 200)
        self.assertIn("截断", text, "截断必须写明，不能让模型以为这就是全部")

    def test_shared_requirements_cover_the_mermaid_lessons(self):
        """两条路共用同一份格式要求（Mermaid 的坑只修一处）。"""
        self.assertIn("Mermaid", meeting._SUMMARY_REQUIREMENTS)
        self.assertIn("timeline", meeting._SUMMARY_REQUIREMENTS)
        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "meeting.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count("timeline 专项规范"), 1,
                         "格式要求只能定义一次（各写一遍 = 将来只修好一条路）")


class SpawnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summary-spawn-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.folder = _mk_meeting_dir(self.tmp)
        self.logs = []
        self._log = patch.object(meeting.db, "add_log",
                                 lambda level, source, msg: self.logs.append((level, msg)))
        self._log.start()
        self.addCleanup(self._log.stop)

    def _run(self, fake):
        with patch.object(meeting, "_llm_provider_for_summary", lambda: (fake, "openai-llm")), \
                patch.object(meeting, "get_client", lambda: _BoomClient()):
            meeting._spawn_provider_summary(7, self.folder)
            return _wait_for_file(os.path.join(self.folder, "summary.md"))

    def test_writes_summary_and_logs_success(self):
        fake = _FakeLlm()
        self.assertTrue(self._run(fake), "纪要应该落盘")
        with open(os.path.join(self.folder, "summary.md"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("议题一", body)
        prompt = fake.calls[0]["messages"][1]["content"]
        self.assertIn("今天讨论验收流程", prompt)
        self.assertIn("Mermaid", prompt, "格式要求要带进 prompt")
        self.assertEqual(fake.calls[0]["messages"][0]["role"], "system")
        self.assertTrue(any("纪要已生成" in m for _lv, m in self.logs), self.logs)

    def test_placeholder_reply_is_not_written(self):
        fake = _FakeLlm(reply="好的，我先把转写文件读一遍再写纪要。")
        self.assertFalse(self._run(fake))
        self.assertFalse(os.path.isfile(os.path.join(self.folder, "summary.md")))
        self.assertTrue(any(lv == "warn" for lv, _m in self.logs), self.logs)

    def test_short_reply_is_not_written(self):
        fake = _FakeLlm(reply="好的")
        self.assertFalse(self._run(fake))
        self.assertTrue(any("过短" in m for _lv, m in self.logs), self.logs)

    def test_existing_summary_is_kept_when_reply_is_bad(self):
        path = os.path.join(self.folder, "summary.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("## 旧纪要\n\n- 有效内容，不能被占位话覆盖")
        fake = _FakeLlm(reply="我先读一下文件。")
        self._run(fake)
        with open(path, encoding="utf-8") as fh:
            kept = fh.read()
        self.assertIn("旧纪要", kept, "坏回复不能覆盖已有纪要")

    def test_provider_error_is_logged_not_swallowed(self):
        fake = _FakeLlm(error=RuntimeError("openai-llm: HTTP 401 密钥无效"))
        self._run(fake)
        self.assertTrue(any(lv == "error" and "401" in m for lv, m in self.logs),
                        "失败必须留痕，否则面板上表现为'点了没反应'：%s" % self.logs)

    def test_no_provider_is_logged(self):
        with patch.object(meeting, "_llm_provider_for_summary",
                          lambda: (None, "没有可用的 LLM provider")):
            meeting._spawn_provider_summary(7, self.folder)
            time.sleep(0.15)
        self.assertTrue(any(lv == "error" for lv, _m in self.logs), self.logs)


class RoutingTests(unittest.TestCase):
    """`request_summary()` 的分流：直连被选中时不许碰 DSH。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summary-route-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.folder = _mk_meeting_dir(self.tmp)
        self._log = patch.object(meeting.db, "add_log", lambda *a, **k: None)
        self._log.start()
        self.addCleanup(self._log.stop)
        self._get = patch.object(meeting.db, "get_meeting", lambda mid: {"id": mid, "name": "m"})
        self._get.start()
        self.addCleanup(self._get.stop)

    def test_direct_path_used_when_selected(self):
        fake = _FakeLlm()
        with patch.object(meeting, "direct_llm_decision", lambda: (True, "按设置走")), \
                patch.object(meeting, "_llm_provider_for_summary", lambda: (fake, "openai-llm")), \
                patch.object(meeting, "get_client", lambda: _BoomClient()), \
                patch.object(meeting, "_spawn_summary_waiter",
                             lambda *a, **k: (_ for _ in ()).throw(
                                 AssertionError("直连选中时不该走 agent 路径"))):
            self.assertTrue(meeting.request_summary(1, folder=self.folder))
            self.assertTrue(_wait_for_file(os.path.join(self.folder, "summary.md")))

    def test_agent_path_used_when_not_selected(self):
        seen = {}
        with patch.object(meeting, "direct_llm_decision", lambda: (False, "agent 可用")), \
                patch.object(meeting, "_spawn_summary_waiter",
                             lambda mid, folder, out, text, label: seen.update(
                                 folder=folder, out=out, text=text, label=label)):
            self.assertTrue(meeting.request_summary(1, folder=self.folder))
        self.assertEqual(seen["label"], "纪要")
        self.assertIn("transcript.md", seen["text"], "agent 路径给的是文件路径（它自己会 read）")
        self.assertNotIn("今天讨论验收流程", seen["text"],
                         "agent 路径不该把正文塞进 prompt（那会让它读文件与内联重复）")

    def test_extra_prompt_reaches_the_direct_provider(self):
        """「追加要求」必须真的传下去（2026-09-19 修的老 bug：填了没用）。"""
        fake = _FakeLlm()
        with patch.object(meeting, "direct_llm_decision", lambda: (True, "测试")), \
                patch.object(meeting, "_llm_provider_for_summary", lambda: (fake, "openai-llm")):
            meeting.request_summary(1, folder=self.folder, extra="重点写待办与责任人")
            self.assertTrue(_wait_for_file(os.path.join(self.folder, "summary.md")))
        self.assertIn("重点写待办与责任人", fake.calls[0]["messages"][1]["content"])

    def test_extra_prompt_reaches_the_agent_path(self):
        seen = {}
        with patch.object(meeting, "direct_llm_decision", lambda: (False, "agent 可用")), \
                patch.object(meeting, "_spawn_summary_waiter",
                             lambda mid, folder, out, text, label: seen.update(text=text)):
            meeting.request_summary(1, folder=self.folder, extra="只要结论，不要过程")
        self.assertIn("只要结论，不要过程", seen["text"])

    def test_regenerate_passes_extra_prompt_through(self):
        """`regenerate_summary` 的追加要求以前根本没传（记了 summary_runs 就丢了）。"""
        seen = {}
        with patch.object(meeting.db, "add_summary_run", lambda mid, extra: 1), \
                patch.object(meeting.db, "finish_summary_run", lambda *a: None), \
                patch.object(meeting.db, "get_meeting",
                             lambda mid: {"id": mid, "name": os.path.basename(self.folder)}), \
                patch.object(meeting, "meetings_dir", lambda: os.path.dirname(self.folder)), \
                patch.object(meeting, "request_summary",
                             lambda mid, folder=None, extra="": seen.update(extra=extra) or True):
            ok, msg = meeting.regenerate_summary(1, extra_prompt="加上风险点")
        self.assertTrue(ok)
        self.assertEqual(seen["extra"], "加上风险点")
        self.assertIn("已发送", msg)


if __name__ == "__main__":
    unittest.main()
