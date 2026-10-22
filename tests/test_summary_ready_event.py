# -*- coding: utf-8 -*-
"""「纪要好了」事件 `meeting_summary_ready` —— 只在**纪要真的写出来**时发（2026-10-16）。

背景（半截管道）：手机侧的通道早就通了 —— `GET /api/events/feed` 给新事件，安卓
`EventNotify.summarize()` 里**已经映射**了 `meeting_summary_ready`（标题「会议纪要好了」）。
但电脑侧**只发过** `meeting_transcribed`（转写正文好了那条），`meeting_summary_ready`
**从来没发过** → 手机永远收不到"纪要好了"。这次把发射点补在**纪要确认落到 `summary.md`
之后**（`app/meeting.py::_notify_summary_ready`，agent 路径 `_spawn_summary_waiter` 写盘后）。

本文件钉四件事：
  1. **写成功 → 发**：events 里出现 `meeting_summary_ready`，payload 带会议名与会议 id
     （字段名与 `meeting_transcribed` / `meeting_stopped` 一致：安卓按 `payload["meeting"]`
     取名字）；
  2. **失败/没写出内容 → 一条都不发**（**这条最重要**：挡住"假报"）。失败有好几种形状：
     等回复超时（`reply` 为空）、只回了"我这就去写"的占位话、回复过短、
     以及"剪掉开场白/元数据之后只剩空白"（等于写了个空文件）；
  3. **`topics.md` 不算**：议题分段是同一条 waiter 写出来的另一个产物，它不是"纪要好了"；
  4. **用临时库隔离**：`db.DATA_DIR` / `db.DB_FILE` 都指到 tmp 再 `db.init()`
     —— **绝不往真库写事件**（照 `tests/test_events_feed.py` 的 setUp 写）。

打桩了哪些函数 / 为什么它们代表"成功/失败"：
  * `meeting.get_client` → `_FakeClient`：**代表 DSH（agent）可用**；它的
    `wait_for_reply()` 返回值就是"DSH 回没回、回了什么" ——
    回一段真纪要 = 成功；回 `""` = **等回复超时**；回 "I'll read the transcript file first."
    这种短意图话 = **占位话**（命中 `_looks_placeholder`）。所以这一个桩同时代表成功与两种失败。
  * `meeting._summary_session` → 固定 `sid`：会话的建立与本次事件**无关**（事件不许挂在那里），
    换成假的只影响"用哪个会话发 prompt"，不影响判据。
  * `meeting.db.add_log` → 收进列表：不往真日志写，同时给"线程走到哪一步了"留判据
    （失败路径要靠日志确认线程已经收口，才不会把"还没来得及发"误当成"不发"）。

桩一律**覆盖线程的整个生命期**（`addCleanup` 停，而不是只包住 spawn 那一行）：
线程是异步跑的，提前撤桩会让它去碰真的 DSH 客户端。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                              # noqa: E402
from app import meeting                                         # noqa: E402


TRANSCRIPT = ("# 会议转写\n\n## 第 1 段 [00-00-00]\n"
              "[00.00.12] 张三：今天讨论一下手机侧的事件通知。\n"
              "[00.00.20] 李四：好，纪要也就靠它提醒。\n")

#: 一段"真纪要"（够长、不像占位话 —— 能写出 summary.md 的那种）。
REAL_MINUTES = ("# 会议纪要\n\n## 一、背景\n\n"
                "本次会议确认了手机侧事件通知的字段口径与落点。\n\n"
                "## 二、待办事项\n\n"
                "| 事项 | 责任人 |\n|:---|:---|\n| 补上纪要事件 | 开发 |\n")

#: 真实的坏形状（2026-10-13 用户实测过的那种）：**整段回复只有开场白 + 元数据 JSON**，
#: 一句正文都没有。它长度 > 80，所以 `_looks_placeholder` **拦不住**它 ——
#: 剪掉开场白/元数据之后只剩空白，等于"写了个空文件"。这时**绝不能**发「纪要好了」。
ONLY_METADATA_REPLY = (
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
    '    {"标题": "1.0遗留问题", "开始": "00:00", "结束": "10:00", "摘要": "讨论1.0现状。"}\n'
    "  ]\n"
    "}\n"
    "```\n"
)

#: 议题分段（`topics.md`）那一份的合法回复 —— 写它会落盘，但**不是**"纪要好了"。
TOPICS_REPLY = json.dumps({
    "标题": "事件通知口径",
    "简介": "确认手机事件字段",
    "摘要": "确认了 meeting_summary_ready 的字段与落点。",
    "分段": [{"标题": "事件字段", "开始": "00:00", "结束": "01:00", "摘要": "对齐 payload。"}],
}, ensure_ascii=False)


class _FakeClient:
    """DSH 客户端替身：`wait_for_reply` 的返回值 = "DSH 回没回、回了什么"。

    `gate` 给"顺序"用例用：它在 `wait_for_reply` 里把线程**扣住**，这样"请求已发出、
    答复还没来"这一瞬间可以被确定性地观察到（不然文件/事件可能在我断言之前就落好了）。
    """

    def __init__(self, reply, done=True, gate=None, started=None):
        self.reply = reply
        self.done = done
        self.gate = gate
        self.started = started

    def clear_stuck(self, sid):
        pass

    def prompt(self, sid, text, mode="queue"):
        if self.started is not None:
            self.started.set()

    def wait_for_reply(self, sid, timeout=0, poll=0):
        if self.gate is not None:
            self.gate.wait(5)
        return (self.reply, self.done)


class SummaryReadyEventTests(unittest.TestCase):
    """发射点与"不发射"的边界（**每个用例一个临时库**，绝不碰真库）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summary-ready-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-summary-readydb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        # 本用例开始时已经活着的线程（用来判"我起的那个纪要线程收口了没"）
        self._baseline = set(threading.enumerate())

        self.folder = os.path.join(self.tmp, "2026-10-16_09-00-00")
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "transcript.md"), "w", encoding="utf-8") as fh:
            fh.write(TRANSCRIPT)

        # 只把日志收起来（不往真日志写），并给"线程走到哪一步"留判据。
        self.logs = []
        _log = mock.patch.object(meeting.db, "add_log",
                                 lambda level, source, msg: self.logs.append((level, msg)))
        _log.start()
        self.addCleanup(_log.stop)

        # 顺序（LIFO）：先停各用例自己起的桩 → 再停日志桩 → 最后把真库路径还回去。
        # 还库路径之前必须确认纪要线程已经不在了，否则它会把事件写进**真库**。
        self.addCleanup(self._restore_db)

    def _restore_db(self):
        self._drain_all()
        db.DATA_DIR, db.DB_FILE = self._old_db
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    # ---------------------------------------------------------------- 小工具

    def _patch(self, name, new):
        """给 `app.meeting` 上的名字打桩，**整个用例期间**有效（到 cleanup 才撤）。"""
        p = mock.patch.object(meeting, name, new=new)
        p.start()
        self.addCleanup(p.stop)

    def _summary_events(self):
        """本场临时库里所有 `meeting_summary_ready`（payload 已解析）。"""
        out = []
        for row in db.list_events(limit=200):
            if row.get("type") != "meeting_summary_ready":
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:                                   # noqa: BLE001
                payload = {}
            out.append(payload)
        return out

    def _wait(self, predicate, what, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        self.assertTrue(predicate(), "等不到：%s（日志=%s）" % (what, self.logs[-6:]))

    def _drain(self, before, timeout=5.0):
        """等**本次新起的那几个线程**都退出。

        为什么必须等：断言"**没有**发事件"时，若线程还在跑，红的是"太早看"、
        绿的是"真没发" —— 那种用例挡不住假报。等它收口，结论才有意义。
        """
        end = time.time() + timeout
        while time.time() < end:
            if not [t for t in threading.enumerate() if t not in before and t.is_alive()]:
                return True
            time.sleep(0.02)
        alive = [t.name for t in threading.enumerate() if t not in before and t.is_alive()]
        self.fail("纪要线程没收口，判据不可信：%s" % alive)

    def _drain_all(self, timeout=5.0):
        """兜底：等本用例期间起的**所有**线程退出（cleanup 里还库路径之前调）。"""
        end = time.time() + timeout
        while time.time() < end:
            if not [t for t in threading.enumerate()
                    if t not in self._baseline and t.is_alive()
                    and t is not threading.current_thread()]:
                return True
            time.sleep(0.02)
        return True

    def _log_has(self, needle):
        return any(needle in msg for _lv, msg in self.logs)

    def _file_ready(self, name="summary.md"):
        path = os.path.join(self.folder, name)
        return os.path.isfile(path) and os.path.getsize(path) > 0

    def _path(self, name="summary.md"):
        return os.path.join(self.folder, name)

    def _start_agent_waiter(self, meeting_id, reply, out_name="summary.md",
                            done=True, gate=None, started=None):
        """起 agent 路径的纪要 waiter，返回"起跑前的线程集合"（给 `_drain` 用）。"""
        before = set(threading.enumerate())
        fake = _FakeClient(reply, done=done, gate=gate, started=started)
        self._patch("get_client", lambda: fake)
        self._patch("_summary_session", lambda client, mid: "sid-1")
        meeting._spawn_summary_waiter(meeting_id, self.folder, out_name, "写纪要", "纪要")
        return before

    # ---------------------------------------------------------------- 1) 写成功 → 发

    def test_agent_path_sends_it_once_the_summary_is_on_disk(self):
        """agent 路径写成功 → 事件带会议名与会议 id（手机就这么取名字）。"""
        mid = 9101
        before = self._start_agent_waiter(mid, REAL_MINUTES)
        self._wait(lambda: self._file_ready(), "summary.md 落盘")
        self._drain(before)
        events = self._summary_events()
        self.assertEqual(1, len(events),
                         "纪要写成功就该发且只发一条「纪要好了」：%s" % events)
        self.assertEqual(os.path.basename(self.folder), events[0].get("meeting"),
                         "payload 必须带会议名（安卓按 payload['meeting'] 取名字）")
        self.assertEqual(mid, events[0].get("id"), "payload 的 id 必须是这场会议的 id")

    def test_the_event_waits_for_the_write_not_for_the_request(self):
        """**顺序**判据：事件必须在 `summary.md` **写出来之后**才发。

        做法：用 `gate` 把 DSH 的"等回复"扣住 —— 此时 prompt 已经发出、会话也建好了
        （正是最容易"顺手发一条"的时刻），而纪要一个字还没写。这一刻 events 必须是空的；
        放行后才发。这条用例直接挡的就是"在请求刚发出/会话刚建立时发"那种假报。
        """
        mid = 9102
        gate = threading.Event()
        started = threading.Event()
        before = self._start_agent_waiter(mid, REAL_MINUTES, gate=gate, started=started)
        self.assertTrue(started.wait(5), "waiter 应该已经把 prompt 发出去了")
        self.assertFalse(self._file_ready(), "扣住回复时不该有纪要文件")
        self.assertEqual([], self._summary_events(),
                         "请求刚发出就发事件 = 假报（点开没内容）")
        gate.set()
        self._wait(lambda: self._file_ready(), "summary.md 落盘")
        self._drain(before)
        self.assertEqual(1, len(self._summary_events()), "写出来之后必须补发这一条")

    # ---------------------------------------------------------------- 2) 失败 → 一条都不发

    def test_a_reply_timeout_sends_nothing(self):
        """DSH 等回复超时（`reply` 为空）→ 没写到盘 → **一条都不发**（挡住假报）。"""
        mid = 9104
        before = self._start_agent_waiter(mid, "")
        self._wait(lambda: self._log_has("超时未收到回复"), "超时那条日志")
        self._drain(before)
        self.assertFalse(self._file_ready(), "不该有纪要文件")
        self.assertEqual([], self._summary_events(), "超时是假的『纪要好了』，不许发")

    def test_a_placeholder_reply_sends_nothing(self):
        """DSH 只回了"我这就去写"这种占位话 → **不回写** → 不许发（否则点开没内容）。"""
        mid = 9105
        before = self._start_agent_waiter(mid, "I'll read the transcript file first.")
        self._wait(lambda: self._log_has("疑似占位"), "占位话那条日志")
        self._drain(before)
        self.assertFalse(self._file_ready(), "占位话不该被当纪要落盘")
        self.assertEqual([], self._summary_events(), "占位话不是纪要，不许发")

    def test_a_summary_that_ends_up_blank_sends_nothing(self):
        """回复整段只有开场白 + 元数据 JSON（**长度够长，`_looks_placeholder` 拦不住**）：
        剪完之后剩空白 —— 那是"写了个空文件"，同样**不许**发。

        这是最容易漏的一种假报：文件"写出来了"、日志也写着"已写入"，但用户点开什么都没有。
        """
        mid = 9106
        before = self._start_agent_waiter(mid, ONLY_METADATA_REPLY)
        self._wait(lambda: self._log_has("已写入"), "空纪要文件被写出来")
        self._drain(before)
        with open(self._path(), encoding="utf-8") as fh:
            self.assertFalse(fh.read().strip(), "前提：这个文件确实是空白的")
        self.assertEqual([], self._summary_events(),
                         "只有元数据/开场白的空纪要也是假报，不许发")

    # ---------------------------------------------------------------- 3) topics.md 不算

    def test_writing_topics_does_not_count_as_the_summary_being_ready(self):
        """`topics.md`（议题分段）是同一条 waiter 写的另一个产物 —— 它不是「纪要好了」。

        2026-10-13 那场实测过：DSH 会把元数据 JSON 和纪要混在一条回复里。判据要是只按
        "这条 waiter 写成功了"来发，手机就会在**纪要还没有**的时候收到「会议纪要好了」。
        """
        mid = 9109
        before = self._start_agent_waiter(mid, TOPICS_REPLY, out_name="topics.md")
        self._wait(lambda: self._file_ready("topics.md"), "topics.md 落盘")
        self._drain(before)
        self.assertEqual([], self._summary_events(),
                         "议题分段不是纪要：不许拿它当『纪要好了』")

    # ---------------------------------------------------------------- 4) 公开入口也接上了

    def test_request_summary_is_wired_to_it(self):
        """公共入口 `request_summary()`（`_transcribe_impl` 收尾调的就是它）也发这条。"""
        mid = 9110
        before = set(threading.enumerate())
        fake = _FakeClient(REAL_MINUTES)
        self._patch("_agent_ready_for_reasoning", lambda: True)
        self._patch("get_client", lambda: fake)
        self._patch("_summary_session", lambda client, mid_: "sid-1")
        self.assertTrue(meeting.request_summary(mid, folder=self.folder))
        self._wait(lambda: self._file_ready(), "summary.md 落盘")
        self._drain(before)
        self.assertEqual(1, len(self._summary_events()),
                         "从 request_summary 进来的这条路必须发（_transcribe_impl 走的就是它）")


if __name__ == "__main__":
    unittest.main()
