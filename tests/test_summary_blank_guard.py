# -*- coding: utf-8 -*-
"""**数据丢失**修复的判据：空白正文绝不许覆盖已有纪要（2026-10-16）。

## 修的是什么（这条风险是复核另一件事时顺出来的）

纪要生成**只有一条写盘点**（2026-10-22 直连 LLM 已删除）：

  * agent（配置的智能体）路径 `_spawn_summary_waiter._run()`。

写盘**之前**的守卫是：① 回复非空且长度 > 10；② `_looks_placeholder(reply)` 拦"占位话"。
**但**模型整段只回"开场白 + 一段元数据 JSON"时（长度 > 80，**占位判据拦不住**），
经 `_strip_summary_head`（剪开场白/去元数据）之后**正文可能只剩空白** ——
旧代码照样 `open(path, "w")`，**把旧的（很可能正是用户想要的那份）纪要覆盖成空白**。
在「重新生成纪要」这条路上，用户会**当场丢掉原来那份纪要**，而且没有任何提示。

## 修法与取舍（判据就钉在这里）

抽一个**唯一**的落盘判据 `app/meeting.py::_write_summary_body`（"复制一份 = 将来只修好
一条"），语义三种：

  1. 新正文**非空白** → 照旧写盘；
  2. 新正文**空白** 且 目标文件**已有非空白内容** → **不覆盖**（一个字都不动）+ warn，
     **绝不静默**；
  3. 新正文**空白** 且 本来就没有内容/文件不存在 → 没有可丢的东西，**保持原有行为**
     （仍然写，即留下一个空白文件），但**如实**记一条 warn 说本次未产出。

**为什么情形 3 选"照旧写 + 如实告警"，而不是顺手改成"不写文件"**：
   * 那个空白文件**本来就骗不过任何人** —— `_notify_summary_ready` 不会为它发事件，
     面板/手机那边的判据都是"文件有非空白内容才算有纪要"；
   * 而 `_file_ready()` 这类判据（`tests/test_summary_ready_event.py` 用）在文件不存在时
     直接 False，**连"过程中到底写了什么"都观测不到**了；已有那条钉住"空白不发事件"的用例
     `test_summary_ready_event.py::test_a_summary_that_ends_up_blank_sends_nothing`
     正是靠"文件在、内容是空白"来断言前提的；
   * 任务允许两种语义，这里选**改动面最小**的那种：只把"没产出"如实记进日志
     （**不静默**），不动落盘行为；真正要防的"覆盖已有好纪要"由情形 2 挡住。

## 用例覆盖

  * 已有好纪要 + 本次空白 → 原文**逐字不变** + warn 日志；
  * 本来没有纪要 + 本次空白 → 不产生"看起来成功"的空白纪要 + **事件不发** + warn 日志；
  * 已有**空白**文件 + 本次空白 → 同样按"没有可保留的内容"处理（保持"写 + 告警"）；
  * 正常非空生成 → 照旧写盘、照旧发事件（**回归**，别把功能修坏）。

隔离：临时库（`db.DATA_DIR` / `db.DB_FILE`）＋ 临时会议目录，**绝不写进真库/真会议目录**。
打桩方式照 `tests/test_summary_ready_event.py`（那个文件**只读不改**）：`get_client` /
`_summary_session` 两个桩，桩**覆盖线程整个生命期**
（`addCleanup` 才撤），否则异步线程会去碰真的 DSH 客户端。
"""
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
              "[00.00.12] 张三：今天讨论一下纪要的落盘纪律。\n"
              "[00.00.20] 李四：好，别把旧纪要覆盖成空白。\n")

#: 一段**空白收场**的回复：开场白 + 元数据 JSON 围栏，**句句都有实质字符**，
#: 所以长度 > 80、`_looks_placeholder` 判"长度够、有实质内容" —— **两道守卫都拦不住它**；
#: 而 `_strip_summary_head` 会把这两段全剪掉，剩空白。这正是 2026-10-13 用户实测过的坏形状。
BLANK_ENDING_REPLY = (
    "Now I have the full transcript. Let me analyze it and generate the structured "
    "metadata JSON and the meeting minutes markdown.\n"
    "\n"
    "## 结构化元数据 JSON\n"
    "\n"
    "```json\n"
    "{\n"
    '  "标题": "纪要落盘纪律",\n'
    '  "简介": "确认空纪要的处理口径",\n'
    '  "摘要": "会议确认空白纪要不得覆盖已有纪要。",\n'
    '  "分段": [\n'
    '    {"标题": "开场白与元数据", "开始": "00:00", "结束": "01:00", "摘要": "只回了元数据。"}\n'
    "  ]\n"
    "}\n"
    "```\n"
)


class _FakeClient:
    """DSH 客户端替身（只兑现 waiter 用到的三个方法）。`reply` = "DSH 回了什么"。"""

    def __init__(self, reply):
        self.reply = reply

    def clear_stuck(self, sid):
        pass

    def prompt(self, sid, text, mode="queue"):
        pass

    def wait_for_reply(self, sid, timeout=0, poll=0):
        return (self.reply, True)


class SummaryBlankGuardTests(unittest.TestCase):
    """两条写盘点都必须过 `_write_summary_body`（临时库 + 临时目录）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summary-blank-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-summary-blankdb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        # 本用例开始时已经活着的线程（判"我起的那条纪要线程收口了没"）
        self._baseline = set(threading.enumerate())

        self.folder = os.path.join(self.tmp, "2026-10-16_09-00-00")
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "transcript.md"), "w", encoding="utf-8") as fh:
            fh.write(TRANSCRIPT)

        # 日志 / 事件都收进列表：① 不往真日志写；② 给"线程走到哪一步"留判据。
        # ⚠️ 顺序（LIFO）：先注册日志桩、后注册 `_restore_db` → cleanup 时先跑
        # `_restore_db`（那时日志桩还活着，排空线程期间的日志还能被看见），最后才撤日志桩。
        self.logs = []
        self.events = []
        self._log = mock.patch.object(
            meeting.db, "add_log",
            lambda level, source, msg: self.logs.append((level, source, msg)))
        self._log.start()
        self.addCleanup(self._log.stop)
        self._event = mock.patch.object(
            meeting.db, "add_event",
            lambda type_, payload=None: self.events.append((type_, payload)))
        self._event.start()
        self.addCleanup(self._event.stop)

        # 还路径之前必须确认纪要线程已经不在了，否则它会写进真库/别的用例的库。
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

    def _path(self, name="summary.md"):
        return os.path.join(self.folder, name)

    def _write_old(self, text, name="summary.md"):
        """铺一份"已有的纪要"（好内容或空白），返回它，最后给"逐字比对"用。"""
        with open(self._path(name), "w", encoding="utf-8") as fh:
            fh.write(text)
        return text

    def _read(self, name="summary.md"):
        with open(self._path(name), encoding="utf-8") as fh:
            return fh.read()

    def _start_agent_waiter(self, meeting_id, reply, out_name="summary.md"):
        """起 agent 路径的纪要 waiter，返回"起跑前的线程集合"（给 `_drain` 用）。"""
        before = set(threading.enumerate())
        self._patch("get_client", lambda: _FakeClient(reply))
        self._patch("_summary_session", lambda client, mid: "sid-1")
        self._patch("_refresh_archived_note", lambda mid: None)   # 别去碰归档材料
        meeting._spawn_summary_waiter(meeting_id, self.folder, out_name, "写纪要", "纪要")
        return before

    def _drain(self, before, timeout=5.0):
        """等**本次新起的那几个线程**都退出。

        为什么必须等：断言"**没有**写/没有发事件"时，若线程还在跑，红的是"太早看"、
        绿的是"真没写" —— 那种用例挡不住假报。等它收口，结论才有意义。
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

    def _wait(self, predicate, what, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _warns(self):
        return [msg for lv, _src, msg in self.logs if lv == "warn"]

    def _summary_events(self):
        """本次运行里发过的「纪要好了」事件（含真库里存的）。"""
        out = [p for t, p in self.events if t == "meeting_summary_ready"]
        for row in db.list_events(limit=200):
            if row.get("type") == "meeting_summary_ready":
                out.append(row.get("payload"))
        return out

    def _assert_blank_ending_premise(self, reply):
        """前提自检：这条回复确实"长度够、不像占位话"，但剪完只剩空白。

        没有这层自检，这个文件会在**别处**改了 `_looks_placeholder` / `_strip_summary_head`
        之后悄悄变成"其实走的是占位话分支"，于是用例还绿、结论却不再是它声称的那件事。
        """
        self.assertTrue(len(reply.strip()) > 80, "前提：长度要够，好让占位判据放行")
        self.assertFalse(meeting._looks_placeholder(reply), "前提：占位判据必须拦不住它")
        self.assertFalse(meeting._strip_summary_head(reply).strip(),
                         "前提：剪开场白/元数据之后必须是空白")

    # ------------------------------------------------ 1) 已有好纪要 + 本次空白 → 不许覆盖

    def test_premise_the_blank_reply_really_slips_past_the_old_guards(self):
        """前提自检（`ONLY_METADATA` 那种回复正是"两道守卫都拦不住"的形状）。"""
        self._assert_blank_ending_premise(BLANK_ENDING_REPLY)

    def test_agent_path_keeps_the_existing_summary_byte_for_byte(self):
        """agent 路径：已有好纪要 + 这次生成空白 → **旧文件一字不变** + warn 日志。"""
        old = ("# %s\n\n## 一、旧纪要\n\n"
               "这一份是用户已经看过、认得的那份纪要，绝不能被空白覆盖。\n" % 9201)
        self._write_old(old)

        before = self._start_agent_waiter(9201, BLANK_ENDING_REPLY)
        got = self._wait(lambda: any("不覆盖" in m for m in self._warns()), "不覆盖那条 warn")
        self._drain(before)
        self.assertTrue(got, "不覆盖必须告警（不许静默）：%s" % self.logs)

        self.assertEqual(old, self._read(), "旧纪要必须逐字不变（这是数据丢失那道闸）")
        self.assertEqual([], self._summary_events(), "没写就没成稿，不许发「纪要好了」")

    # ------------------------------------------------ 2) 本来没有纪要 + 本次空白

    def test_agent_path_produces_nothing_that_looks_like_a_summary(self):
        """本来没有纪要 + 这次空白 → **不许**留下"看起来成功"的空白纪要，且事件不发。

        这里的语义（与实体行为一致）见模块 docstring 的情形 3：**保持原有行为**
        （确实会写下一个空白文件 —— 空白文件本来就骗不过任何人：它 `strip()` 之后是空的、
        事件不会为它发），但必须**如实**记一条 warn 说本次未产出。
        """
        self.assertFalse(os.path.exists(self._path()), "前提：本来没有纪要")

        before = self._start_agent_waiter(9202, BLANK_ENDING_REPLY)
        got = self._wait(lambda: any("未产出" in m for m in self._warns()), "未产出那条 warn")
        self._drain(before)
        self.assertTrue(got, "空白没产出必须如实告警（不许静默）：%s" % self.logs)

        # 情形 3 的语义：仍然写（留下一个空白文件），但它**不构成"看起来成功的纪要"** ——
        # 内容 strip() 之后是空的，事件不为它发（旧行为就是如此，这里如实钉住它）。
        self.assertTrue(os.path.isfile(self._path()), "情形 3 保持原行为：写下一个空白文件")
        self.assertFalse(self._read().strip(), "那份文件必须是空白的（没有假正文混进去）")
        self.assertFalse(any("已保留原有纪要" in m for m in self._warns()),
                         "没有旧纪要可保留时不该说「已保留」")
        self.assertEqual([], self._summary_events(), "空白不是纪要，事件不许发")

    def test_an_existing_blank_file_is_not_a_summary_to_preserve(self):
        """已有的就是一个**空白**文件 → 没有"可保留的内容"，按情形 3 处理（写 + 如实告警）。

        免得这个判据被理解成"只要文件在就一律不覆盖"——那样一次遗留的空白文件会让
        之后的每次生成都写不进去。
        """
        self._write_old("   \n\n", name="summary.md")

        before = self._start_agent_waiter(9205, BLANK_ENDING_REPLY)
        got = self._wait(lambda: any("未产出" in m for m in self._warns()), "未产出那条 warn")
        self._drain(before)
        self.assertTrue(got, self.logs)
        self.assertFalse(any("不覆盖" in m for m in self._warns()),
                         "旧文件本身是空白 = 没有可保留的东西，不该报「已保留原有纪要」")
        self.assertEqual([], self._summary_events())

    # ------------------------------------------------ 3) 正常非空生成 → 照旧（回归）

    def test_a_real_summary_is_still_written_and_notified_on_both_paths(self):
        """回归：非空白正文**照旧**写盘、照旧发事件 —— 这次改动只该挡住空白。"""
        agent_body = ("# %s\n\n## 一、真纪要\n\n"
                      "这是 agent 路径的正常产出，必须原样落盘。\n" % 9206)
        before = self._start_agent_waiter(9206, agent_body)
        self.assertTrue(self._wait(lambda: os.path.isfile(self._path()), "agent 纪要落盘"))
        self._drain(before)
        self.assertEqual(agent_body, self._read(), "非空正文必须照旧写盘")
        self.assertEqual(1, len(self._summary_events()),
                         "写成功照旧要发「纪要好了」：%s" % self._summary_events())
    def test_a_real_summary_overwrites_an_old_one_as_before(self):
        """回归：正常的"重新生成"必须**照旧覆盖**旧纪要（不然这次修复就修坏了功能）。"""
        self._write_old("# 9208\n\n## 旧纪要\n\n要被新的替换掉。\n")
        new = ("# 9208\n\n## 新纪要\n\n"
               "重新生成拿到的是一份正常的正文，应当覆盖旧的那份。\n")
        before = self._start_agent_waiter(9208, new)
        self.assertTrue(self._wait(lambda: new == self._read(), "新纪要覆盖旧纪要"))
        self._drain(before)
        self.assertEqual(new, self._read())


if __name__ == "__main__":
    unittest.main()
