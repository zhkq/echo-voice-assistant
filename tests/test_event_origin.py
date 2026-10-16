# -*- coding: utf-8 -*-
"""事件 payload 的 `origin` —— "**这次任务是谁发起的**"（2026-10-16 用户口径）。

用户原话：**"手机侧发的任务，手机接受通知，电脑端发起的任务不用给手机"**。
难处在于手机订阅的是**所有**事件（`GET /api/events/feed`）—— 电脑上自己录的会、
自己在面板上敲的指令，不加区分都会推到手机上去。所以每条事件都必须自己说清
"这次任务是谁发起的"。

取值只有两个（**给机器看**）：`mobile` / `pc`（`meeting.ORIGIN_MOBILE` / `ORIGIN_PC`）。
面板看的那个中文 `by`（`pauses[].by` = 手机/面板）语义**不动**，两者判据同源、各管一处。

本文件钉五条（前四条是用户给的判据原文）：

  1. 带中继标记 `X-Echo-Relay` 调 `POST /api/meeting/start` → 这场会议的
     `meeting_started` / `meeting_stopped` / `meeting_transcribed` /
     `meeting_summary_ready` 事件 `origin` **全是 `mobile`**；
  2. 不带中继标记（本机面板）→ 全是 `pc`；
  3. **手机发起、但 `source=pc`（用电脑那支麦录）→ 仍然是 `mobile`** ——
     判据是"**谁调的接口**"，不是"音频从哪支麦来"。**这条最能挡住理解错**；
  4. `command_done`：`source=mobile` 的指令 → `origin=mobile`；`web` 的 → `pc`；
  5. 拿不到来源 → `pc`（保守取向：**宁可不打扰手机**）。

隔离（照 `tests/test_events_feed.py` 的写法）：`db.DATA_DIR` / `db.DB_FILE` / 会议目录 /
进程级 `settings._cache` 全部指向临时目录 —— **绝不写真库、绝不写真会议目录**。
麦克风用假输入流（照 `tests/test_meeting_pause.py`），转写/纪要线程打桩
（判据只看**事件**，不看转写结果）。

为什么会议那几条要"真跑"：`origin` 的判据分两截 —— api 层"谁调的接口"
（`api._meeting_origin`）与会议层"这场是谁的任务"（`meeting._event_origin`，读
`meta.json` 的 `initiatedBy`）。只测其中一截就会漏掉另一半（例如把判据写成
"`source=phone` 才算手机"时，只有走完整条路才看得出来）。所以这里从 HTTP 打进去，
再让转写/纪要那两处**真实发射点**各跑一次。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

from app import assistant as assistant_mod                           # noqa: E402
from app import db                                                   # noqa: E402
from app import meeting as meeting_mod                               # noqa: E402
from app import providers as providers_mod                           # noqa: E402
from app import relay as relay_mod                                   # noqa: E402
from app.api import router                                           # noqa: E402
from app.config import settings                                      # noqa: E402
from app.audio import recorder as recorder_mod                       # noqa: E402


class _FakeStream:
    """假装是麦克风输入流（**不碰真麦克风**）：每次 read 给一小块 PCM。

    照 `tests/test_meeting_pause.py` 的写法 —— 这样"本机麦"那条录音路能真跑起来。
    """

    def __init__(self, chunk=1600, block=0.02):
        self.chunk = chunk
        self.block = block
        self.device = "fake"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        import numpy as np
        time.sleep(self.block)
        return np.zeros(min(int(n), self.chunk), dtype="int16"), False


def _tiny_wav() -> bytes:
    """一个最小的合法 WAV（44 字节头 + 160 个静音采样）—— 给"手机传段"那条路。"""
    import struct
    data = b"\x00\x00" * 160
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


class _FakeAsrProvider:
    """外部/在线转写服务的替身：本场文字由它出（**不加载任何模型**）。"""

    def transcribe(self, path, lang="zh"):
        return {"text": "今天讨论一下手机侧的事件通知。"}


class _FakeDshClient:
    """DSH 替身：够 `_dispatch` 走完"发出 → 等回复 → 写 `command_done`"这一条路。

    刻意**不给** `list_sessions`：`_configured_command_target()` 会因此返回 `(None, None)`，
    不必再去打桩"命令目标"那两项设置。
    """

    def ping(self):
        return True

    def ensure_command_session(self, text):
        return "sid-origin"

    def clear_stuck(self, sid):
        pass

    def prompt(self, sid, text, mode="queue"):
        pass

    def wait_for_reply(self, sid, timeout=0, poll=0):
        return ("已经发好了。", True)


class EventOriginTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-origin-")          # 会议目录
        self.dbdir = tempfile.mkdtemp(prefix="echo-origindb-")      # 临时库
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        # 进程级设置缓存不受 db 补丁约束：不清的话会读到**真库**的值（别的类/真机留下的）
        settings._cache = None

        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        self._orig_stream = recorder_mod.input_stream
        recorder_mod.input_stream = lambda *a, **kw: _FakeStream()

        self._reset_state()

        self._patchers = []

        def _patch(name, new):
            p = mock.patch.object(meeting_mod, name, new=new)
            p.start()
            self._patchers.append(p)

        # 判据只看**事件**，不看转写结果 —— 所以不让 stop_meeting 真起转写线程
        # （那会在用例拆完临时库之后才跑，报一堆 no such table，属测试卫生问题）。
        _patch("_transcribe_meeting", lambda *a, **kw: None)
        # `_transcribe_impl` 里那一大段能力路由/分离全部绕开：只留"拿到文字 → 发事件"
        # 这一段（转写走 provider 那条路，不加载模型、不连后端）。
        _patch("_active_asr_provider", lambda: _FakeAsrProvider())
        _patch("_capability_asr_session", lambda *a, **kw: None)
        _patch("_asr_provider_id", lambda: "fake-asr")
        # 纪要/议题：真跑会去连 DSH —— 事件判据与它们无关，打桩掉。
        _patch("request_summary", lambda *a, **kw: True)
        _patch("request_topic_segments", lambda *a, **kw: True)
        # 提示音：用例不许在开发机上真响一声。
        _patch("tts_mod", SimpleNamespace(play_beep=lambda *a, **kw: None,
                                         beep_ok=lambda *a, **kw: None,
                                         speak_async=lambda *a, **kw: None,
                                         speak_text=lambda *a, **kw: None))

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self.addCleanup(self._clean)

    # ---------------------------------------------------------------- 隔离

    def _reset_state(self):
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  origin=meeting_mod.ORIGIN_PC)

    def _clean(self):
        for p in self._patchers:
            p.stop()
        self._reset_state()
        meeting_mod.meetings_dir = self._orig_dir
        recorder_mod.input_stream = self._orig_stream
        db.DATA_DIR, db.DB_FILE = self._old_db
        # 进程级设置缓存要**按真实库重建后**再交出去（不能留空）：
        # `settings._cache` 是进程级的，而本类把它按**临时库**读满过（测试要 `apiAuthEnabled`
        # 是默认的 False 才能不带令牌打接口）。留空的话，下一个模块的第一次读会按**它自己的
        # 临时库**重建 —— `tests/test_events_feed.py::test_it_needs_a_token` 依赖"缓存里是
        # **本机真实**的 `apiAuthEnabled=true`"（它没清缓存，是既有写法），于是会红。
        # 这里恢复的正是"没跑过本类"时的状态：缓存已按**真实库**填好。
        settings._cache = None
        try:
            settings.get("apiAuthEnabled")
        except Exception:                                          # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    # ---------------------------------------------------------------- 小工具

    def _start(self, source="pc", relay=False):
        headers = {relay_mod.RELAY_MARKER_HEADER: "1"} if relay else {}
        r = self.client.post("/api/meeting/start", params={"source": source},
                             headers=headers)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        return body

    def _stop(self):
        r = self.client.post("/api/meeting/stop")
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json().get("ok"), r.json())
        return r.json()

    def _events(self, type_):
        """临时库里某类事件（payload 已解析），按 id 正序。"""
        out = []
        for row in reversed(db.list_events(limit=200)):
            if row.get("type") != type_:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:                                      # noqa: BLE001
                payload = {}
            out.append(payload)
        return out

    def _one(self, type_):
        rows = self._events(type_)
        self.assertEqual(1, len(rows), "%s 该只有一条：%s" % (type_, rows))
        return rows[0]

    def _finish_the_meeting(self, folder):
        """把这场会**真的**走完：转写（ASR 打桩）→ 纪要好了 —— 两处都是真实发射点。"""
        code = meeting_mod._transcribe_impl(folder)
        self.assertEqual("transcribed", code, "前提：这场真的转出了文字")
        row = db.get_meeting_by_name(os.path.basename(folder))
        self.assertIsNotNone(row, "前提：这场会议在库里")
        self.assertTrue(meeting_mod._notify_summary_ready(row["id"], folder, "真纪要正文。\n"))
        return row["id"]

    def _assert_all_mobile_events(self, expected):
        for type_ in ("meeting_started", "meeting_stopped",
                      "meeting_transcribed", "meeting_summary_ready"):
            self.assertEqual(expected, self._one(type_).get("origin"),
                             "%s 的 origin 该是 %s" % (type_, expected))

    def _meta(self, name):
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------ 1) / 3) 手机调接口 = mobile

    def test_a_relayed_start_is_mobile_even_though_the_mic_is_the_computer(self):
        """**本条最能挡住理解错**：手机按「开始录音」但用**电脑的麦**录（`source=pc`）。

        判据是"**谁调的接口**"（带 `X-Echo-Relay` = 手机），不是"音频从哪支麦来" ——
        所以 `origin` 必须是 `mobile`：这是**手机发起的任务**，通知该回手机。
        反过来说，要是谁把判据写成 `source == "phone"`，这条就红。
        """
        self._start(source="pc", relay=True)
        self.assertEqual("pc", meeting_mod.meeting_status()["source"],
                         "前提：音频确实来自电脑那支麦（source=pc）")
        name = self._stop()["message"]
        folder = os.path.join(self.tmp, name)
        self.assertEqual("mobile", self._meta(name)["initiatedBy"],
                         "发起方要落进 meta（转写/纪要那两处从它读）")
        self.assertEqual("mobile", self._one("meeting_started").get("origin"))
        self.assertEqual("mobile", self._one("meeting_stopped").get("origin"))
        self._finish_the_meeting(folder)
        self._assert_all_mobile_events("mobile")

    def test_a_relayed_phone_mic_meeting_is_mobile_too(self):
        """手机那条经典路径（`source=phone`，音频由手机按段传上来）—— 同样是 `mobile`。"""
        self._start(source="phone", relay=True)
        ok, seg = meeting_mod.append_meeting_segment(_tiny_wav())
        self.assertTrue(ok, seg)
        name = self._stop()["message"]
        folder = os.path.join(self.tmp, name)
        self.assertEqual("phone", self._meta(name)["source"], "前提：这场是手机麦录的")
        self._finish_the_meeting(folder)
        self._assert_all_mobile_events("mobile")

    # ---------------------------------------------------- 2) 本机面板 = pc

    def test_a_meeting_the_panel_started_is_pc_on_every_event(self):
        """本机面板（不带中继标记）发起 → 这场所有事件 `origin` 都是 `pc`。

        口径的另一半：**电脑端发起的任务不用给手机** —— 用户自己在电脑前录的会，
        手机不该响。
        """
        self._start(source="pc")                      # 不带中继标记
        time.sleep(0.3)                               # 让假麦克风真写出一段音频
        name = self._stop()["message"]
        folder = os.path.join(self.tmp, name)
        self.assertEqual("pc", self._meta(name)["initiatedBy"])
        self._finish_the_meeting(folder)
        self._assert_all_mobile_events("pc")

    def test_a_phone_mic_meeting_started_from_the_panel_is_still_pc(self):
        """从**面板**发起、却要求用手机麦（`source=phone` 但不带中继标记）→ 仍然是 `pc`。

        这条把判据钉死：`source` 只描述"音频从哪来"，**发起方只看谁调的接口**。
        （现实中手机都经中继进来，所以这一格主要是"判据别写成 source"的护栏。）
        """
        self._start(source="phone")                   # 不带中继标记
        meeting_mod.append_meeting_segment(_tiny_wav())
        name = self._stop()["message"]
        self.assertEqual("phone", self._meta(name)["source"], "前提：音频来自手机麦")
        self.assertEqual("pc", self._meta(name)["initiatedBy"])
        self.assertEqual("pc", self._one("meeting_started").get("origin"))
        self.assertEqual("pc", self._one("meeting_stopped").get("origin"))

    # -------------------------------------------- 4) 指令：跟着命令来源走

    def test_command_done_origin_follows_the_command_source(self):
        """`command_done` 的 `origin`：`mobile` 的指令 → `mobile`；`web` 的 → `pc`。

        真跑 `_dispatch`（DSH 与提示音/通知/朗读都打桩）—— 判据落在**事件**上，
        不是落在某个小函数上：`command_received.source` 与 `command_done.origin`
        必须同源，不然手机没法拿它判断该不该提醒。
        """
        with mock.patch.object(assistant_mod, "get_client", lambda: _FakeDshClient()), \
                mock.patch.object(assistant_mod, "_agent_snapshot", lambda: ""), \
                mock.patch.object(assistant_mod, "build_env_context", lambda: ""), \
                mock.patch.object(assistant_mod, "build_reply_requirement", lambda: ""), \
                mock.patch.object(assistant_mod, "notify", lambda *a, **kw: None), \
                mock.patch.object(providers_mod, "speak_async", lambda *a, **kw: None):
            self.assertTrue(assistant_mod._dispatch("把灯关掉", "mobile"))
            self.assertTrue(assistant_mod._dispatch("看看天气", "web"))

        received = self._events("command_received")
        done = self._events("command_done")
        self.assertEqual(2, len(received), received)
        self.assertEqual(2, len(done), done)
        by_id = {r["id"]: r["source"] for r in received}
        got = {d["id"]: d.get("origin") for d in done}
        for cid, source in by_id.items():
            want = "mobile" if source == "mobile" else "pc"
            self.assertEqual(want, got.get(cid),
                             "指令 %s 的来源是 %r，command_done 的 origin 该是 %s"
                             % (cid, source, want))
        self.assertIn("mobile", by_id.values(), "前提：真的走了 `source=mobile` 那条")
        self.assertIn("web", by_id.values(), "前提：真的走了 `source=web` 那条")

    # ------------------------------------------------ 导入也是"一场任务的起点"

    def test_imported_meetings_carry_the_origin_too(self):
        """导入（`meeting_imported`）：手机上导入 = `mobile`，面板上导入 = `pc`。

        为什么也要验：导入**本身就是**一场任务的起点（不是某场录音的后续）——
        它得自己把发起方说清楚，否则"在电脑上导了一段录音"也会推手机。
        """
        wav = _tiny_wav()
        r = self.client.post("/api/meetings/import",
                             files={"files": ("meeting.wav", wav, "audio/wav")},
                             headers={relay_mod.RELAY_MARKER_HEADER: "1"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual("mobile", self._one("meeting_imported").get("origin"),
                         "手机上导入的会议 → mobile")

        r = self.client.post("/api/meetings/import",
                             files={"files": ("meeting.wav", wav, "audio/wav")})
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(["mobile", "pc"],
                         [p.get("origin") for p in self._events("meeting_imported")],
                         "面板上导入的会议 → pc（不推手机）")

    # ------------------------------------- 5) 拿不到来源 → pc（不打扰手机）

    def test_an_unknown_source_falls_back_to_pc(self):
        """拿不到/认不出发起方 → `pc`（保守：**宁可不打扰手机**）。

        两种错的代价不对称：多推一条 = 用户在电脑前忙活、手机响一声；
        少推一条 = 他回面板上看一眼。所以默认值只能选"不打扰"这一边。
        """
        for value in (None, "", "   ", "hotkey", "API", "MOBILE-PHONE"):
            self.assertEqual("pc", assistant_mod._command_origin(value),
                             "认不出来的来源 %r 该按 pc" % (value,))
        self.assertEqual("mobile", assistant_mod._command_origin("mobile"),
                         "只有明确的 mobile 才算手机（大小写不敏感）")
        self.assertEqual("mobile", assistant_mod._command_origin(" Mobile "))

        # 会议侧同一个取舍：meta 里没有 `initiatedBy`（老会议 / 手搓目录）→ pc
        bare = os.path.join(self.tmp, "no-origin")
        os.makedirs(bare, exist_ok=True)
        self.assertEqual(meeting_mod.ORIGIN_PC, meeting_mod._event_origin(bare),
                         "没有 meta.json 的会议目录 → pc")
        with open(os.path.join(bare, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"start": "2026-10-16T09:00:00", "source": "phone"}, fh)
        self.assertEqual(meeting_mod.ORIGIN_PC, meeting_mod._event_origin(bare),
                         "meta 里没有 initiatedBy（老会议）→ pc，哪怕 source=phone")
        self.assertEqual(meeting_mod.ORIGIN_PC, meeting_mod._event_origin(None),
                         "连目录都没有 → pc")


if __name__ == "__main__":
    unittest.main()
