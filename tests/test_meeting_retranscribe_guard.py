# -*- coding: utf-8 -*-
"""「这场会正在转写」时不许再起第二个转写任务（2026-09-28 用户实测 bug）。

用户原话：

> 发现一个 bug：转写中，转写按钮还能再次被点击，容易误触。

表面是"按钮没禁用"，真问题是**后端也不拦**：
`retranscribe_meeting()` 当时只看了进程内标记 `_retranscribing`，而
`stop_meeting()`（录音结束后的自动转写）与 `import_meeting()`（导入后自动转写）
起转写时**都不进那个标记**，只把库里的 `meetings.status` 改成 `transcribing`。
于是那两种状态下点「重新转写」会真的起第二个线程：两个线程并发写同一份
`transcript.md` / `meta.json`（`_transcribe_impl` 开头还会 `clear_meeting_lines`
把对方正在写的转写行清掉）—— 互相覆盖，是最难查的那类错。

这批用例钉四件事：

  1. **按会议判重**：这一场在转写 → `retranscribe_meeting()` 抛
     `MeetingTranscribeBusy`、`POST /api/meetings/{mid}/retranscribe` 回 **409**
     + 中文文案，且**一个转写任务都不多起**（替身调用次数 = 1）；
  2. 判据**两份都要**：库状态那一份（`stop_meeting` / `import_meeting` 那条路，
     也就是原来漏掉的那一份）与进程内标记那一份（点了还没轮到改库的空档）；
  3. **另一场会议不受影响** —— 按会议判重，不是全局禁止（跨会议的并发上限仍由
     既有的 `max_concurrent` 那套管）；
  4. 面板的 `transcribing` 字段由**列表与详情接口**下发（前端按钮禁用读它）。
     前端那一条在 `tests/test_ia_panel.py::MeetingRetranscribeButtonTests`。

隔离与 `tests/test_meeting_import.py` / `tests/test_meeting_compress.py` 同一套：
`db.DATA_DIR` / `db.DB_FILE` / `settings._cache` / 凭据文件 / 会议目录全部指向
**临时目录**，**不加载任何模型、不开麦克风、不起真转写线程**
（`_transcribe_meeting` 一律换成替身）。本仓库有"单测误碰真实数据"的血泪史，
所以这里一个真实路径都不碰。
"""
import itertools
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                                 # noqa: E402
import app.meeting as meeting                                       # noqa: E402
from app.capabilities import credentials as cred_mod                # noqa: E402
from app.config import settings                                     # noqa: E402


class _GuardCase(unittest.TestCase):
    """临时库 + 临时会议目录 + 临时凭据（与 `tests/test_meeting_import.py` 同一套）。"""

    #: 会议名必须**全类唯一**（`meetings.name` 上有 UNIQUE）：同一个临时库里
    #: 每个用例都可能建好几场，用用例内的序号会撞名。
    _seq = itertools.count(1)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-txguard-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "guard.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()

        # 凭据文件隔离：这台开发机**真配对过** ECHO 后端，不隔离的话
        # "转写走哪条路"会被真实凭据带偏（同 test_meeting_import / test_meeting_capability）。
        cls._cred = os.path.join(cls.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: cls._cred)
        p.start()
        cls.addClassCleanup(p.stop)

        # 会议目录：`retranscribe_meeting()` 走 `meetings_dir()`
        cls.meetings_root = os.path.join(cls.tmp, "meetings")
        os.makedirs(cls.meetings_root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: cls.meetings_root)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # 进程内标记是**模块级**状态：上一个用例留下的名字会污染下一个（本类里的
        # 替身转写线程收尾是异步的），所以每个用例开始前清空它。
        with meeting._retranscribing["lock"]:
            meeting._retranscribing["set"].clear()
        self.addCleanup(meeting._retranscribing["set"].clear)
        self._n = 0

    # ---- 造料 ----------------------------------------------------------

    def make_meeting(self, status="transcribing", with_audio=True):
        """建一场会议（库里一行 + 盘上一个目录）。

        `with_audio` 造一个**只有文件名对**的空 `01.wav`：段存在性检查只认名字
        （`audiofile.segment_files`），而转写本身一律被替身挡住 —— 不读音频、
        不加载引擎。这样用例跑起来是毫秒级，也不会碰任何真实数据。
        """
        self._n += 1
        i = next(self._seq)
        name = "2026-09-28_10-%02d-%02d" % (i // 60, i % 60)
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        if with_audio:
            with open(os.path.join(folder, "01.wav"), "wb") as fh:
                fh.write(b"RIFF0000WAVE")
        mid = db.create_meeting(name, started_at="2026-09-28T10:00:00")
        db.update_meeting(mid, status=status, segments=1 if with_audio else 0)
        return mid, name, folder

    def _wait_until(self, cond, timeout=5.0):
        """等一个条件成立（转写替身跑在后台线程里，落点要等一下）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def _blocking_transcribe(self):
        """`_transcribe_meeting` 的替身：**占住**直到测试放行（模拟一场真转写）。

        返回 `(spy, release)`；`spy.call_count` 就是"起了几个转写任务"。
        """
        release = threading.Event()
        self.addCleanup(release.set)

        def fake(folder):
            release.wait(10.0)

        spy = MagicMock(side_effect=fake)
        return spy, release


# ---------------------------------------------------------------- ① 判据本身

class BusyReasonTests(_GuardCase):
    """`transcribe_busy_reason()` / `is_transcribing()`：两份判据、按会议看。"""

    def test_db_status_alone_is_enough(self):
        """**这条就是 bug 的回归**：`stop_meeting()` 起的那条自动转写只写库状态、
        不进进程内标记 —— 当时点「重新转写」会真的再起一个线程。"""
        mid, name, _folder = self.make_meeting(status="transcribing")
        self.assertNotIn(name, meeting._retranscribing["set"],
                         "前提：这一场**没有**进程内标记（正是 stop_meeting 那条路）")
        self.assertEqual(meeting.transcribe_busy_reason(name), "transcribing")
        self.assertTrue(meeting.is_transcribing(name))

    def test_the_in_process_marker_alone_is_enough(self):
        """反过来那一半：刚点过「重新转写」、线程还没轮到改库时的空档也要拦住。"""
        mid, name, _folder = self.make_meeting(status="transcribed")
        with meeting._retranscribing["lock"]:
            meeting._retranscribing["set"].add(name)
        self.assertEqual(meeting.transcribe_busy_reason(name), "retranscribing")
        self.assertTrue(meeting.is_transcribing(name))

    def test_a_finished_meeting_is_not_busy(self):
        mid, name, _folder = self.make_meeting(status="transcribed")
        self.assertEqual(meeting.transcribe_busy_reason(name), "")
        self.assertFalse(meeting.is_transcribing(name))

    def test_a_recording_meeting_is_not_reported_as_transcribing(self):
        """录音中 ≠ 转写中：`transcribing` 字段不许把正在录的那一场也算进去
        （面板据此禁用按钮，误报会让用户以为"已经在转了"）。"""
        mid, name, _folder = self.make_meeting(status="recording")
        self.assertFalse(meeting.is_transcribing(name))

    def test_it_is_per_meeting_not_global(self):
        """按会议判重：**另一场**在转，不该让这一场也被判成忙。"""
        _m1, busy, _f1 = self.make_meeting(status="transcribing")
        _m2, free, _f2 = self.make_meeting(status="imported")
        self.assertTrue(meeting.is_transcribing(busy))
        self.assertFalse(meeting.is_transcribing(free))

    def test_an_unknown_name_is_never_busy(self):
        self.assertEqual(meeting.transcribe_busy_reason(""), "")
        self.assertFalse(meeting.is_transcribing("2026-01-01_00-00-00"))


# ---------------------------------------------------------------- ② 不起第二个任务

class NoSecondTaskTests(_GuardCase):
    """被拒的那次请求**不许**产生第二个转写任务（这才是真正的伤害）。"""

    def test_a_second_call_while_transcribing_raises_and_starts_nothing(self):
        mid, name, _folder = self.make_meeting(status="transcribing")
        spy = MagicMock()
        with patch.object(meeting, "_transcribe_meeting", spy):
            with self.assertRaises(meeting.MeetingTranscribeBusy) as ctx:
                meeting.retranscribe_meeting(mid)
        self.assertEqual(ctx.exception.kind, "transcribing")
        self.assertEqual(ctx.exception.message, meeting.TRANSCRIBE_BUSY_MESSAGE)
        self.assertFalse(spy.called, "被拒绝的请求绝不能起第二个转写任务")
        self.assertEqual(db.get_meeting(mid)["status"], "transcribing",
                         "被拒绝的请求不许改动会议状态")

    def test_two_calls_in_a_row_produce_exactly_one_task(self):
        """连点两下（面板上最容易发生的那种）：第二个必须被拦，任务数 = 1。

        判据是**真起过的任务数**（替身调用次数），不是返回文案 ——
        "返回了 False 但线程已经起了"正是要防的那种假拦截。
        """
        mid, _name, folder = self.make_meeting(status="imported")
        spy, _release = self._blocking_transcribe()
        with patch.object(meeting, "_transcribe_meeting", spy):
            ok, msg = meeting.retranscribe_meeting(mid)
            self.assertTrue(ok, msg)
            self.assertTrue(self._wait_until(lambda: spy.call_count >= 1),
                            "第一次请求必须真的起转写")
            with self.assertRaises(meeting.MeetingTranscribeBusy):
                meeting.retranscribe_meeting(mid)
            self.assertEqual(spy.call_count, 1, "同一场会只能有一个转写任务")
        self.assertEqual(spy.call_args[0][0], folder)

    def test_truly_concurrent_requests_produce_exactly_one_task(self):
        """**并发**（不只是连续两次）：四个线程同时发起，只有一个能占住。

        这条钉的是"先判后占必须在**同一个**临界区"：拆成"先查、再占"两步的话，
        四个线程会双双通过判断，各起一个转写线程去写同一份文件。
        """
        mid, _name, _folder = self.make_meeting(status="imported")
        spy, _release = self._blocking_transcribe()
        results = []
        guard = threading.Lock()

        def fire():
            try:
                ok, _msg = meeting.retranscribe_meeting(mid)
            except meeting.MeetingTranscribeBusy:
                ok = False
            with guard:
                results.append(ok)

        with patch.object(meeting, "_transcribe_meeting", spy):
            threads = [threading.Thread(target=fire) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10.0)
        self.assertEqual(len(results), 4, "四个线程都要有结论：%r" % (results,))
        self.assertEqual(results.count(True), 1, "并发时只许一个成功：%r" % (results,))
        self.assertEqual(spy.call_count, 1, "同一场会只能有一个转写任务")

    def test_another_meeting_still_starts(self):
        """另一场会议该转写照样转写（不是全局禁止）。"""
        _busy_mid, busy_name, _bf = self.make_meeting(status="transcribing")
        free_mid, free_name, _ff = self.make_meeting(status="imported")
        spy = MagicMock()
        with patch.object(meeting, "_transcribe_meeting", spy):
            ok, msg = meeting.retranscribe_meeting(free_mid)
            self.assertTrue(ok, msg)
            self.assertTrue(self._wait_until(lambda: spy.call_count >= 1))
        self.assertEqual(spy.call_count, 1)
        self.assertTrue(meeting.is_transcribing(busy_name),
                        "另一场转写不受影响：它本来就在转")

    def test_the_claim_is_released_when_the_task_ends(self):
        """转写收工后必须放行（否则一场会**永远**不能再转，比重复点击更糟）。"""
        mid, name, _folder = self.make_meeting(status="imported")
        with patch.object(meeting, "_transcribe_meeting", MagicMock()):
            self.assertTrue(meeting.retranscribe_meeting(mid)[0])
        self.assertTrue(self._wait_until(lambda: not meeting.is_transcribing(name)),
                        "转写线程收尾后必须把占位放掉：%r" % (meeting.transcribe_busy_reason(name),))
        # 放行之后再发起应当又能成功（不是"一次之后就锁死"）
        with patch.object(meeting, "_transcribe_meeting", MagicMock()):
            self.assertTrue(meeting.retranscribe_meeting(mid)[0])

    def test_a_meeting_without_audio_is_still_a_business_failure(self):
        """"没有音频片段"不能被误判成 409：它是业务失败（200 + `{ok:false}`），
        两种结局的语义必须分得开（这正是引入 `MeetingTranscribeBusy` 的理由）。"""
        mid, _name, _folder = self.make_meeting(status="imported", with_audio=False)
        ok, msg = meeting.retranscribe_meeting(mid)
        self.assertFalse(ok)
        self.assertIn("没有音频片段", msg)

    def test_a_missing_meeting_is_still_a_business_failure(self):
        ok, msg = meeting.retranscribe_meeting(999999)
        self.assertFalse(ok)
        self.assertEqual(msg, "会议不存在")


# ---------------------------------------------------------------- ③ 接口面

class RetranscribeEndpointTests(_GuardCase):
    """`POST /api/meetings/{mid}/retranscribe`：409 + 那句中文。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_a_busy_meeting_is_409_with_the_chinese_reason(self):
        mid, _name, _folder = self.make_meeting(status="transcribing")
        spy = MagicMock()
        with patch.object(meeting, "_transcribe_meeting", spy):
            r = self.client.post("/api/meetings/%d/retranscribe" % mid)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json(), {"detail": "这场会议正在转写中，请等它完成"})
        self.assertFalse(spy.called, "409 的那次请求不许起转写任务")

    def test_a_free_meeting_is_still_200(self):
        mid, _name, _folder = self.make_meeting(status="imported")
        with patch.object(meeting, "_transcribe_meeting", MagicMock()):
            r = self.client.post("/api/meetings/%d/retranscribe" % mid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertIn("已开始重新转写", r.json()["message"])

    def test_no_audio_is_still_200_ok_false(self):
        """与 409 分开：业务失败沿用既有的 200 + `{ok:false}`（不悄悄改契约）。"""
        mid, _name, _folder = self.make_meeting(status="imported", with_audio=False)
        r = self.client.post("/api/meetings/%d/retranscribe" % mid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["ok"])

    def test_detail_exposes_the_transcribing_field(self):
        """详情接口下发 `transcribing` —— 面板按钮的禁用**只**读它。"""
        busy_mid, _bn, _bf = self.make_meeting(status="transcribing")
        free_mid, _fn, _ff = self.make_meeting(status="transcribed")
        self.assertIs(self.client.get("/api/meetings/%d" % busy_mid).json()["transcribing"], True)
        self.assertIs(self.client.get("/api/meetings/%d" % free_mid).json()["transcribing"], False)

    def test_the_list_exposes_the_transcribing_field_too(self):
        """列表也要有（列表卡片据此画进度条；两个接口同一个字段，不许各说各话）。"""
        mid, _name, _folder = self.make_meeting(status="transcribing")
        items = {it["id"]: it for it in self.client.get("/api/meetings").json()["items"]}
        self.assertIn(mid, items)
        self.assertIs(items[mid]["transcribing"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
