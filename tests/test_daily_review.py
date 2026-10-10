# -*- coding: utf-8 -*-
"""每日回顾（app/daily_review.py）的契约与编排测试。

本文件盯的是四件事，每一件都对应一次真实会出问题的场景：

  1. **播报提取**：车内听到什么由技能的 `【播报】` 段决定；技能没按契约写时必须
     降级（只念第一段），**绝不能把一整篇整理稿念出来** —— 用户 2026-10-06
     明确要求"语音反馈不能过于冗长"。
  2. **口令识别**：开始/结束要说得出、听得懂；误判的代价是"模式开着关不掉"。
  3. **提交链路**：建工作区 → 建当天会话 → 发原始转写 → 等回复 → 取播报，
     以及**每一步失败时给用户的那句人话**（车里看不到面板，沉默最差）。
  4. **每天一个会话**：`dsh_sessions` 表对 kind 有 UNIQUE 约束，用固定 kind 会把
     昨天的回顾覆盖掉 —— 这条是需求"每天单独一个会话"的根，必须钉住。

DB 重定向到临时目录；DSH 客户端整体换成替身（不打 HTTP、不建真工作区）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                                # noqa: E402
from app import assistant                                         # noqa: E402
from app import daily_review                                      # noqa: E402
from app.config import settings                                   # noqa: E402


class _FakeClient:
    """DSH 适配器的替身：记录调用，返回可编排的结果。"""

    name = "dsh"

    def __init__(self, reply="【播报】记好了。还有别的吗？【/播报】", cwd=None,
                 create_ok=True, session_cwd=None):
        self.reply = reply
        #: 建会话之后 `session_cwd` 该返回什么。真 DSH 里"按 workspaceId 建会话"的结果
        #: 就是会话 cwd == 工作区路径，所以这里默认回落成最近一次 ensure_workspace 的路径 ——
        #: 若写成固定的 None，会让"同一天复用会话"看起来像"会话跑到了别处"，
        #: 于是每次都重建（那是替身失真，不是产品 bug）。
        self._session_cwd_override = session_cwd
        self.cwd = cwd
        self.create_ok = create_ok
        self.calls = []
        self.prompted = []
        self.workspaces = []

    # ---- 工作区 / 会话 ----
    def ensure_workspace(self, path, title=""):
        self.calls.append(("ensure_workspace", path, title))
        self.workspaces.append((path, title))
        self.cwd = path                      # 真 DSH：会话 cwd 就是工作区 path
        return "ws-1", True

    def create_session(self, cwd=None, workspace_id=None):
        self.calls.append(("create_session", cwd, workspace_id))
        return "session-abc" if self.create_ok else ""

    def session_cwd(self, session_id):
        self.calls.append(("session_cwd", session_id))
        if self._session_cwd_override is not None:
            return self._session_cwd_override
        return self.cwd or ""

    # ---- 收发 ----
    def prompt(self, session_id, text, mode="queue"):
        self.calls.append(("prompt", session_id))
        self.prompted.append(text)
        return True

    def wait_for_reply(self, session_id, timeout=90, poll=0.5):
        self.calls.append(("wait_for_reply", session_id, timeout))
        return self.reply, True


class _MismatchClient(_FakeClient):
    """`create_session(workspace_id=…)` 会失败（工作区不存在），按 cwd 才成功。"""

    def create_session(self, cwd=None, workspace_id=None):
        self.calls.append(("create_session", cwd, workspace_id))
        if workspace_id:
            raise RuntimeError("DSH RPC session/create 返回错误: workspace/not-found "
                               'workspace "ebf15c43" not found')
        return "session-fallback"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-review-")
        self._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        settings.seed_defaults()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, "01-工作日志"), exist_ok=True)
        settings.update({
            "dailyReviewEnabled": True,
            "dailyReviewVaultRoot": self.vault,
            "dailyReviewWorkspace": os.path.join(self.tmp, "review"),
            "worklogEnsureSessionAccess": False,   # 不让替身去校正真权限
        })

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old
        settings._cache = None
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- 1. 播报提取

class BroadcastTests(unittest.TestCase):
    """`extract_broadcast` 的三层兜底 —— 本功能最影响体验的一处。"""

    def test_prefers_the_explicit_broadcast_marker(self):
        reply = ("## 今天的工作\n\n- 上午对了设备到货\n- 下午填了绩效表\n\n"
                 "【播报】今天记了两件事。设备到货那边有书面确认吗？【/播报】")
        spoken, source = daily_review.extract_broadcast(reply)
        self.assertEqual(source, "broadcast")
        self.assertIn("设备到货", spoken)
        self.assertNotIn("##", spoken)
        self.assertNotIn("上午对了", spoken)

    def test_marker_tolerates_spaces_and_missing_close(self):
        for text in ("【 播报 】记好了。【 / 播报 】", "【播报】记好了。"):
            with self.subTest(text=text):
                spoken, source = daily_review.extract_broadcast(text)
                self.assertEqual(source, "broadcast")
                self.assertIn("记好了", spoken)

    def test_falls_back_to_first_paragraph_only(self):
        """没有标记时**只念第一段** —— 绝不把整篇整理稿念出来。"""
        reply = ("今天记了三件事，都登好了。\n\n"
                 "## 详情\n- 第一条\n- 第二条\n- 第三条\n" + "很长的正文。" * 60)
        spoken, source = daily_review.extract_broadcast(reply)
        self.assertEqual(source, "first_paragraph")
        self.assertIn("今天记了三件事", spoken)
        self.assertNotIn("第一条", spoken)

    def test_empty_reply_speaks_something(self):
        """回复为空也不能静默：用户在车里，沉默 = 以为坏了。"""
        for text in ("", "   ", "\n\n"):
            with self.subTest(text=repr(text)):
                spoken, source = daily_review.extract_broadcast(text)
                self.assertEqual(source, "fallback")
                self.assertTrue(spoken.strip())

    def test_long_broadcast_is_truncated(self):
        long_sentence = "第一句话讲完了。" + "第二句话特别长" * 40
        spoken, source = daily_review.extract_broadcast(
            "【播报】%s【/播报】" % long_sentence, limit_chars=40)
        self.assertEqual(source, "broadcast")
        # 允许截断标记那一个字符的溢出；关键是**不许原样念完**
        self.assertLessEqual(len(spoken), 42)
        self.assertLess(len(spoken), len(long_sentence))
        self.assertTrue(spoken.endswith("。") or spoken.endswith("……"))

    def test_markdown_is_stripped_from_spoken_text(self):
        reply = ("【播报】- **今天**记了 `3` 条\n- 见 https://example.com/x 【/播报】")
        spoken, _ = daily_review.extract_broadcast(reply)
        for bad in ("*", "`", "http", "- "):
            self.assertNotIn(bad, spoken)
        self.assertIn("今天", spoken)

# ---------------------------------------------------------------- 2. 口令

class IntentTests(unittest.TestCase):
    def test_start_phrases(self):
        for text in ("我们来回顾一下今天", "回顾今天", "每日回顾", "开始回顾吧"):
            with self.subTest(text=text):
                self.assertTrue(daily_review.wants_start(text), text)

    def test_non_start_phrases(self):
        for text in ("开始记录", "记录一下", "今天下午开会", "帮我看看天气", ""):
            with self.subTest(text=text):
                self.assertFalse(daily_review.wants_start(text), text)

    def test_stop_phrases(self):
        for text in ("结束回顾", "今天就这些", "回顾结束", "就这些了"):
            with self.subTest(text=text):
                self.assertTrue(daily_review.wants_stop(text), text)

    def test_non_stop_phrases(self):
        for text in ("结束录音", "我回顾一下上午的事", "停止"):
            with self.subTest(text=text):
                self.assertFalse(daily_review.wants_stop(text), text)


# ---------------------------------------------------------------- 3. 会话与提交

class SessionTests(_Base):
    def test_kind_is_per_day(self):
        """每天一个 kind —— 否则 UNIQUE(kind) 会把昨天的回顾覆盖掉。"""
        import datetime
        d1 = datetime.datetime(2026, 10, 5, 18, 0)
        d2 = datetime.datetime(2026, 10, 6, 18, 0)
        self.assertNotEqual(daily_review.today_kind(d1), daily_review.today_kind(d2))
        self.assertEqual(daily_review.today_kind(d2), "review:2026-10-06")

    def test_session_is_created_through_the_workspace(self):
        """必须走 workspaceId：传 cwd 只建目录、**不登记**，侧栏里会落到「未分组」。"""
        client = _FakeClient()
        sid, wid, info = daily_review.ensure_session(client)
        self.assertEqual(sid, "session-abc")
        self.assertEqual(wid, "ws-1")
        create = [c for c in client.calls if c[0] == "create_session"][0]
        self.assertEqual(create[2], "ws-1", "create_session 必须带 workspace_id")
        self.assertTrue(info["workspace"])

    def test_same_day_reuses_the_session(self):
        client = _FakeClient()
        sid1, _, _ = daily_review.ensure_session(client)
        sid2, _, _ = daily_review.ensure_session(client)
        self.assertEqual(sid1, sid2)
        self.assertEqual(len([c for c in client.calls if c[0] == "create_session"]), 1,
                         "同一天不该重复建会话")

    def test_force_new_rebuilds_the_session(self):
        client = _FakeClient()
        daily_review.ensure_session(client)
        daily_review.ensure_session(client, force_new=True)
        self.assertEqual(len([c for c in client.calls if c[0] == "create_session"]), 2)

    def test_session_outside_the_workspace_is_rebuilt(self):
        """会话还在、但**不在**目标工作区里 → 当天也重建。

        不重建的后果：用户改了工作区设置后，"侧栏里看不到今天的回顾"会静默存在一整天。
        """
        client = _FakeClient(session_cwd=os.path.join(self.tmp, "somewhere-else"))
        daily_review.ensure_session(client)
        daily_review.ensure_session(client)
        self.assertEqual(len([c for c in client.calls if c[0] == "create_session"]), 2)


class WorkspaceMismatchTests(_Base):
    """工作区失配**不能变成 500**（2026-10-10 用户实测，两次撞上）。

    现场：切到稳定版之后 ECHO 复用了 dev 的 harness，本树那个 workspace 在对方家里不存在
    → `session/create` 报 `workspace/not-found` → 点「开始回顾」直接 500。
    `dsh_agent._workspace_registry_path()` 的注释早就写明这条路该"回退 cwd"，
    但 `ensure_session` 原来把异常直接放走 —— 下面那个 `cwd=` 兜底永远走不到。
    """

    def test_it_falls_back_to_cwd_instead_of_raising(self):
        c = _MismatchClient()
        sid, wid, info = daily_review.ensure_session(c)          # 关键：不许抛
        self.assertEqual(sid, "session-fallback", "应当按 cwd 重建会话")
        self.assertEqual(wid, "", "失效的 workspace 必须被丢掉")
        tries = [x for x in c.calls if x[0] == "create_session"]
        self.assertEqual(len(tries), 2, tries)
        self.assertEqual(tries[0][2], "ws-1", "第一次按 workspace 试")
        self.assertIsNone(tries[1][2], "第二次不带 workspace（按 cwd）")
        self.assertTrue(tries[1][1], "按 cwd 兜底时要带路径")


class SubmitTests(_Base):
    def _patch_client(self, client):
        self._orig = daily_review._client
        daily_review._client = lambda: client
        self.addCleanup(lambda: setattr(daily_review, "_client", self._orig))

    def test_happy_path_returns_the_spoken_line(self):
        client = _FakeClient(reply="【播报】今天记了两件事。设备那边定了吗？【/播报】")
        self._patch_client(client)
        out = daily_review.submit("今天上午对了设备到货，下午填了绩效表")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["source"], "broadcast")
        self.assertIn("设备那边定了吗", out["spoken"])
        self.assertEqual(out["session_id"], "session-abc")

    def test_transcript_is_passed_through_verbatim(self):
        """原始转写是唯一真相 —— 提交时必须原样带下去，不做摘要、不改写。"""
        client = _FakeClient()
        self._patch_client(client)
        raw = "嗯……今天上午那个，就是设备到货那事，跟供应商对了；下午绩效表填完了"
        daily_review.submit(raw)
        self.assertEqual(len(client.prompted), 1)
        self.assertIn(raw, client.prompted[0])
        self.assertIn("【今日口述·原始转写】", client.prompted[0])

    def test_prompt_carries_the_broadcast_contract(self):
        client = _FakeClient()
        self._patch_client(client)
        daily_review.submit("随便说一句")
        sent = client.prompted[0]
        self.assertIn("【播报】", sent)
        self.assertIn(self.vault, sent)

    def test_timeout_speaks_a_sentence_instead_of_silence(self):
        class _NoReply(_FakeClient):
            def wait_for_reply(self, session_id, timeout=90, poll=0.5):
                return "", False

        self._patch_client(_NoReply())
        out = daily_review.submit("说了点东西")
        self.assertFalse(out["ok"])
        self.assertTrue(out["spoken"].strip(), "超时也必须给用户一句话")
        self.assertIn("超时", out["error"])

    def test_not_ready_speaks_a_sentence(self):
        settings.update({"dailyReviewEnabled": False})
        client = _FakeClient()
        self._patch_client(client)
        out = daily_review.submit("说了点东西")
        self.assertFalse(out["ok"])
        self.assertTrue(out["spoken"].strip())
        self.assertEqual(client.prompted, [], "没配好就不该发出去")

    def test_broken_client_does_not_raise(self):
        class _Boom(_FakeClient):
            def create_session(self, cwd=None, workspace_id=None):
                raise RuntimeError("DSH 炸了")

        self._patch_client(_Boom())
        out = daily_review.submit("说了点东西")       # 不允许抛
        self.assertFalse(out["ok"])
        self.assertTrue(out["spoken"].strip())

    def test_missing_broadcast_marker_is_logged(self):
        """技能没按契约输出要留痕 —— 否则"念错内容"永远查不出来。"""
        self._patch_client(_FakeClient(reply="今天记了三件事，都登好了。"))
        out = daily_review.submit("说了点东西")
        self.assertTrue(out["ok"])
        self.assertEqual(out["source"], "first_paragraph")
        rows = db._query("SELECT * FROM logs WHERE source='daily_review' ORDER BY id DESC")
        self.assertTrue(any("播报" in (r["message"] or "") for r in rows),
                        "未按契约输出时必须在日志里留痕")


class SettingsTests(_Base):
    def test_limits_come_from_settings(self):
        settings.update({"dailyReviewMaxRecordSec": 90, "dailyReviewSilenceMs": 2000,
                         "dailyReviewReplyTimeoutSec": 240, "dailyReviewBroadcastChars": 80})
        self.assertEqual(daily_review.record_limit_ms(), 90_000)
        self.assertEqual(daily_review.silence_ms(), 2000)
        self.assertEqual(daily_review.reply_timeout_s(), 240)
        self.assertEqual(daily_review.broadcast_limit_chars(), 80)

    def test_limits_have_a_floor(self):
        """乱填（0 / 负数 / 文字）不许把功能弄死。"""
        settings.update({"dailyReviewSilenceMs": 0, "dailyReviewBroadcastChars": "abc"})
        self.assertGreaterEqual(daily_review.silence_ms(), 300)
        self.assertGreaterEqual(daily_review.broadcast_limit_chars(), 20)

    def test_vault_falls_back_to_the_worklog_setting(self):
        settings.update({"dailyReviewVaultRoot": "", "worklogVaultRoot": r"D:\some\vault"})
        self.assertEqual(daily_review.vault_root(), r"D:\some\vault")

    def test_ready_reports_why_not(self):
        settings.update({"dailyReviewEnabled": False})
        ok, why = daily_review.ready()
        self.assertFalse(ok)
        self.assertIn("未启用", why)

        settings.update({"dailyReviewEnabled": True, "dailyReviewVaultRoot": ""})
        ok, why = daily_review.ready()
        self.assertFalse(ok)
        self.assertIn("笔记库", why)

        settings.update({"dailyReviewVaultRoot": os.path.join(self.tmp, "nope")})
        ok, why = daily_review.ready()
        self.assertFalse(ok)
        self.assertIn("不存在", why)

        settings.update({"dailyReviewVaultRoot": self.vault})
        ok, why = daily_review.ready()
        self.assertTrue(ok, why)


class PromptTemplateTests(_Base):
    def test_placeholders_are_filled(self):
        settings.update({"dailyReviewPrompt":
                         "{vault}|{date}|{time}|星期{weekday}|{chars}|{transcript}"})
        import datetime
        out = daily_review.build_prompt("原话", vault="V",
                                        now=datetime.datetime(2026, 10, 6, 18, 30))
        self.assertIn("V|2026-10-06|18:30", out)
        self.assertIn("星期二", out)
        self.assertIn("|120|", out)
        self.assertTrue(out.endswith("原话"))

    def test_backslash_in_the_template_is_not_eaten(self):
        """`re.sub` 的替换串会把 `\\` 当转义 —— 契约段里的 `【/播报】` 曾被写成 `【\\播报】`。

        这是 2026-10-06 在**真实默认模板**上复现出来的：整段 `【播报】…【/播报】`
        的斜杠都被吃掉，技能收到的契约就是错的。判据是"斜杠必须原样保留"。
        """
        raw_path = "C:" + chr(92) + "a" + chr(92) + "b"
        settings.update({"dailyReviewPrompt": "【播报】x【/播报】 路径 " + raw_path + " {transcript}"})
        out = daily_review.build_prompt("原话", vault="V")
        self.assertIn("【/播报】", out)
        self.assertIn(raw_path, out)

    def test_transcript_containing_placeholder_text_is_not_rewritten(self):
        """用户口述里出现 `{chars}` 字样时**不许**被当成占位符替换（顺带钉住 bug ①）。"""
        settings.update({"dailyReviewPrompt": "{chars}|{transcript}"})
        out = daily_review.build_prompt("我说的是 {chars} 这个写法", vault="V")
        self.assertIn("我说的是 {chars} 这个写法", out)

    def test_empty_template_still_carries_the_contract(self):
        """模板被清空时也得保住"转写 + 播报契约"两件事。"""
        settings.update({"dailyReviewPrompt": ""})
        out = daily_review.build_prompt("原话", vault="V")
        self.assertIn("原话", out)
        self.assertIn("【播报】", out)
        self.assertIn("【/播报】", out)


class ReviewSpeakGuardTests(_Base):
    """`assistant._review_speak` 的最后一道长度闸。

    为什么值得单独钉：这是"无论如何都不会念长稿"的兜底。上游已经有两道
    （技能侧契约 + `extract_broadcast` 按 `dailyReviewBroadcastChars` 截断），
    但**任何一环失效（技能不听话、设置被改成很大的值）都会让车里听到一整篇** ——
    而这是用户 2026-10-06 明确提的唯一一条体验硬要求。
    """

    def setUp(self):
        super().setUp()
        from unittest.mock import patch as _patch
        self.spoken = []
        self._p = _patch("app.assistant.providers_mod.speak_text",
                         side_effect=lambda text, timeout=60: self.spoken.append(text))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_short_text_is_spoken_verbatim(self):
        from app import assistant
        assistant._review_speak("今天记了两件事。还有别的吗？")
        self.assertEqual(self.spoken, ["今天记了两件事。还有别的吗？"])

    def test_long_text_is_clamped(self):
        from app import assistant
        settings.update({"maxBriefChars": 60})
        assistant._review_speak("这是一句很长的整理稿。" * 30)
        self.assertEqual(len(self.spoken), 1)
        self.assertLess(len(self.spoken[0]), 70, "超长播报没有被截断：%r" % self.spoken[0])

    def test_clamp_happens_even_if_the_setting_is_huge(self):
        """设置被改成很大的值时，仍然不许无限长（下限 60 字兜底）。"""
        from app import assistant
        settings.update({"maxBriefChars": 100000})
        assistant._review_speak("句子。" * 2000)
        self.assertEqual(len(self.spoken), 1)
        self.assertLess(len(self.spoken[0]), 220)


class ApiTests(_Base):
    """面板侧的三个端点 —— 面板不能因为"回顾没配好"就 500。"""

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def _patch_client(self, client):
        self._orig = daily_review._client
        daily_review._client = lambda: client
        self.addCleanup(lambda: setattr(daily_review, "_client", self._orig))

    def test_status_is_readable_even_when_disabled(self):
        settings.update({"dailyReviewEnabled": False})
        r = self.client.get("/api/daily-review")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ready"])
        self.assertIn("未启用", body["reason"])
        self.assertFalse(body["running"])

    def test_status_is_readable_when_configured(self):
        r = self.client.get("/api/daily-review")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ready"], body.get("reason"))
        self.assertEqual(body["vault"], self.vault)

    def test_start_creates_today_session(self):
        self._patch_client(_FakeClient())
        r = self.client.post("/api/daily-review/start", json={"force_new": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["sessionId"], "session-abc")

    def test_start_rejects_when_not_configured(self):
        settings.update({"dailyReviewEnabled": False})
        self._patch_client(_FakeClient())
        r = self.client.post("/api/daily-review/start", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("未启用", r.json()["detail"])

    def test_submit_returns_the_spoken_line(self):
        self._patch_client(_FakeClient(reply="【播报】记好了。还有吗？【/播报】"))
        r = self.client.post("/api/daily-review/submit", json={"text": "今天开了个会"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("记好了", body["spoken"])

    def test_submit_rejects_empty_text(self):
        r = self.client.post("/api/daily-review/submit", json={"text": "   "})
        self.assertEqual(r.status_code, 400)

    def test_stop_is_idempotent(self):
        r1 = self.client.post("/api/daily-review/stop")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("stopped", r1.json())

    # ---- 「真的开始」那个按钮（2026-10-07 用户实测暴露的缺口）----
    #
    # 现场：*"我点了开始回顾，提示了回顾已经开始但是没有其他反应了"*。
    # 根因：面板两个按钮都调 `/start`，而它**只建会话、什么都不跑**；
    # 真正开麦克风进回顾模式的是 `assistant.start_review()`。所以这里钉住：
    # **点了按钮必须真的进回顾模式**，而且进不去时要说明原因。

    def test_go_actually_enters_review_mode(self):
        """`/go` 不能只建会话 —— 必须调到 `assistant.start_review()`。"""
        self._patch_client(_FakeClient())
        called = []
        with mock.patch.object(assistant, "start_review",
                               lambda source="wake": called.append(source) or True), \
             mock.patch.object(assistant, "review_running", lambda: True):
            r = self.client.post("/api/daily-review/go", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual(called, ["web"], "必须真的进了回顾模式（source=web）")
        self.assertTrue(body["running"], "回给面板的 running 要是真状态，否则面板会干等")
        self.assertEqual(body["error"], "")

    def test_go_prepares_the_session_too(self):
        """进回顾模式之前要先把当天那条会话准备好（否则第一轮就报"会话没建起来"）。"""
        self._patch_client(_FakeClient())
        with mock.patch.object(assistant, "start_review", lambda source="wake": True), \
             mock.patch.object(assistant, "review_running", lambda: True):
            body = self.client.post("/api/daily-review/go", json={}).json()
        self.assertEqual(body["sessionId"], "session-abc")

    def test_go_says_why_when_the_assistant_is_busy(self):
        """助手正忙（在跑命令/在录音）时进不去 —— **要说清是谁占着**，
        不能让面板沉默（那正是这次现场的体感："没有其他反应"）。"""
        self._patch_client(_FakeClient())
        with mock.patch.object(assistant, "start_review", lambda source="wake": False), \
             mock.patch.object(assistant, "review_running", lambda: False), \
             mock.patch.dict(assistant._busy_owner, {"name": "hotkey"}):
            body = self.client.post("/api/daily-review/go", json={}).json()
        self.assertFalse(body["running"])
        self.assertIn("hotkey", body["error"])
        self.assertIn("正忙", body["error"])

    def test_go_reports_when_not_configured(self):
        """没配好时（未启用 / 没笔记库）**不建会话、不进模式**，并把原因带回去。"""
        settings.update({"dailyReviewEnabled": False})
        self._patch_client(_FakeClient())
        called = []
        with mock.patch.object(assistant, "start_review",
                               lambda source="wake": called.append(source) or True):
            r = self.client.post("/api/daily-review/go", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("未启用", body["error"])
        self.assertEqual(called, [], "没配好就不该进回顾模式")

    def test_go_refuses_while_a_meeting_is_recording(self):
        """正在录会议时**当场拒绝**：回顾要独占麦克风，而录音设备不排他 ——
        否则要等到第一轮录音才失败，用户看到的是"点了没反应"（2026-10-07 的体感）。"""
        from app import meeting
        self._patch_client(_FakeClient())
        called = []
        with mock.patch.object(meeting, "meeting_status", lambda: {"active": True}), \
             mock.patch.object(assistant, "start_review",
                               lambda source="wake": called.append(source) or True):
            body = self.client.post("/api/daily-review/go", json={}).json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["running"])
        self.assertIn("会议", body["error"])
        self.assertEqual(called, [], "在录会议时不该去抢麦克风")


if __name__ == "__main__":
    unittest.main()
