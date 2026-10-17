# -*- coding: utf-8 -*-
"""**手机发起的会议录音，采集源必须是"手机麦"**（2026-10-17 真机事故的回归）。

用户真机现场（原话）：

    我用手机发起了一次会议录音，**电脑端也变成录音状态了**，结束录音后，先报了**后端不可用**，
    我又在电脑上手动点了一次重新转写才完成，而且我看转写内容应该是**电脑录的**，不包括我拿着
    手机去别的屋说的一些测试内容，所以**仍然不是手机录的**。

那场会议的取证（`D:\\ECHO\\meeting\\2026-10-17_13-18-27`）：`meta.json` 里**没有 `source`**、
`initiatedBy: "mobile"`，日志里 13:18:27 有一条 `audio` 的"回退到系统默认麦克风"（= 电脑的声卡
真的被打开了），`meeting_started` 事件也没有 `source: phone`。原因不是"客户端没发 source"，
而是**真机那条路一个 query 都不带**：

  * 安卓 `EchoApi` 里确实有 `@Query("source") source: String = "phone"` 的两个变体
    （`meetingStartPhone` / `meetingStartPhoneWithId`），但 App 的「开始录音」按钮打的是
    `api().meetingStart()`（`ui/MainViewModel.kt`）—— **没有 query 参数**；
  * 服务端 `meeting_start()` 当时把 `source` 的**缺省值**写成 `"pc"`，于是"**没说**"被读成
    "**明说要电脑的麦**" → 建出**本机麦**会议 → 电脑开麦、手机传的段被"本机麦拒收外来段"
    这条护栏拒掉、转写里全是电脑那支麦。访问日志里那条请求确实只有
    `POST /api/meeting/start HTTP/1.1`，**连 `?` 都没有**。

所以本文件钉的是**服务端**这条契约（`app/api.py::_meeting_capture_source`）：

  1. `source=phone`（可带 `clientId`）→ 这场会议**必须是手机麦**：
     `meeting_status()["source"] == "phone"`、**没有 `MeetingRecorder` 被创建**（打桩断言 0 次）、
     会议目录里**没有电脑麦写出来的段**；
  2. **手机发起却没带任何参数**（真机那条路）→ 同样按手机麦建会（判据 = 中继标记，与
     `_meeting_origin` 同源）—— 这是本次事故的**那条**回归用例；
  3. 同一个 `clientId` 再调一次 `start` → 幂等（同一场、`duplicate: true`、`source` 仍是
     `phone`），之后 `segment(clientId, seq)` **被接受**并落成 `NN.wav` / `NN.flac`，
     对账段数正确；
  4. **不带 `source` 的老调用（本机面板/技能）逐字不变**：仍是本机麦会议、仍是
     `source: "pc"`（`tests/test_api_contract.py` 逐字钉着那个回包）；
  5. **手机显式要电脑的麦**（`source=pc`）仍然算电脑麦 —— 不许把判据做成一刀切
     （`tests/test_event_origin.py::test_a_relayed_start_is_mobile_even_though_the_mic_is_the_computer`）；
  6. **"本机麦会议拒收外来段"这条护栏一个字都不许放宽**（两路音频混进同一场 = 转写里两个人
     同时说话，事后查不清）。它是**对的**，只是因为①的 bug 被"用错了地方"。

测试卫生：临时库 + 临时会议目录 + 临时令牌（`apiAuthEnabled=True`），声卡与转写全部打桩 ——
**绝不碰**真实库/真实会议目录/真实麦克风。`settings._cache` 是**进程级**的，用完必须丢掉
（`tests/test_events_feed.py` 记着这条教训）。
"""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

import app.api as api_mod                                            # noqa: E402
import app.assistant as assistant_mod                                # noqa: E402
from app import db                                                   # noqa: E402
from app import meeting as meeting_mod                               # noqa: E402
from app import relay as relay_mod                                   # noqa: E402
from app.api import router                                           # noqa: E402
from app.config import settings                                      # noqa: E402

#: 客户端稳定 id 的形状（安卓侧 `phone-<yyyyMMddHHmmss>-<4 位>`）。
CID = "phone-20261017131827-7c3d"
#: 客户端缓存的开始时间（真机那场是 13:18:27 起的）。
T0 = "2026-10-17T13:18:27"


def _tiny_wav() -> bytes:
    """一个最小的合法 WAV（44 字节头 + 160 个静音采样）—— 与既有的手机麦用例同一份。"""
    data = b"\x00\x00" * 160
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


def _wav_to_flac(wav_bytes: bytes) -> bytes:
    """`RIFF` 字节 → 真的 `fLaC` 字节（与 `tests/test_meeting_offline_flac.py` 同一手法）。

    为什么造**真的** FLAC：服务端按魔数认格式，但下游（时长/转写）会真去解它 ——
    塞一个只有 `fLaC` 四个字节的假货，验到的就不是那条路了。
    """
    import soundfile as sf
    import wave
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    import numpy as np
    samples = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels)
    buf = io.BytesIO()
    sf.write(buf, samples, rate, subtype="PCM_16", format="FLAC")
    return buf.getvalue()


class _FakeRecorder:
    """`MeetingRecorder` 的替身：**一次实例化就记一笔**，并假装麦打开了。

    为什么要计数而不是不许创建：`start_meeting()`（本机麦）真的会去开声卡，
    用例里要能区分"走了本机麦那条路"（计数 1）与"手机麦那条路"（计数 0）。
    `thread` 指向自己、`join()` 等 `_done`，这样 `_watch_recorder` 那个守护线程
    不会在用例中途把会议收尾（它只在 `_done` 之后才可能动 `_state`）。
    """

    def __init__(self, folder, segment_minutes=10, device_id=None, level_cb=None):
        self.folder = folder
        self.segments = []
        self.error = ""
        self.stopped = False
        self._done = threading.Event()
        self.thread = self

    def start(self):
        pass

    def wait_started(self, timeout=6):
        return True

    def stop(self):
        self.stopped = True
        self._done.set()
        return True

    def join(self):
        self._done.wait(10)


class _Base(unittest.TestCase):
    """临时库 + 临时会议目录 + 临时令牌 + 声卡/转写打桩。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-phone-src-mtg-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-phone-src-db-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        # 刻意**不**调 `settings.seed_defaults()`（要写上百行设置、本机实测 2 秒/次）；
        # 这些用例只读几个有内联默认值的键（`meetingSegmentMinutes` 之类）。
        settings._drop_cache()

        # ---- 临时凭据：把鉴权**打开**，这样"手机带令牌经中继进来"真的被验到 ----
        db.set_setting("apiAuthEnabled", True)
        self.token = db.add_api_key_row("phone-src-test", scopes=["device"])["token"]
        settings._drop_cache()
        self.assertTrue(settings.get("apiAuthEnabled", False),
                        "setUp 没把 apiAuthEnabled 摆成 True：这条路的鉴权前提不成立")

        # ---- 临时的会议目录（**绝不许**往真库/真目录里写一场会议） ----
        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        self._reset_state()

        # ---- ① 声卡：一次实例化就记一笔（断言"0 次"就是判据）----
        self.recorder_calls = []
        self._fakes = []

        def _make_recorder(folder, segment_minutes=10, device_id=None, level_cb=None):
            self.recorder_calls.append(folder)
            rec = _FakeRecorder(folder, segment_minutes, device_id, level_cb)
            self._fakes.append(rec)
            return rec

        self._orig_recorder = meeting_mod.MeetingRecorder
        meeting_mod.MeetingRecorder = _make_recorder
        self._orig_resolve = meeting_mod.resolve_input_device
        # 取输入设备会去问真实音频栈（可能慢、可能打印设备告警）：与"采集源"无关，打桩。
        meeting_mod.resolve_input_device = lambda purpose="meeting": None

        # ---- ② 转写：真跑会去连 ASR/能力后端（几秒到几分钟）----
        self._orig_transcribe = meeting_mod._transcribe_meeting
        meeting_mod._transcribe_meeting = lambda *a, **kw: None

        # ---- ③ 让出麦克风：不影响采集源判据，打桩免得等 3 秒 ----
        self._yield = patch.object(assistant_mod, "yield_capture_for_meeting",
                                   lambda timeout=3.0: False)
        self._yield.start()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self.addCleanup(self._clean)

    def _reset_state(self):
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0, _pause_events=[])

    def _clean(self):
        # 先把状态清干净、**再**放掉 `_watch_recorder`（顺序反过来会让它在收尾时看到
        # "还在录 + recorder 就是我"，去调 `stop_meeting()`，在已拆掉的临时库上多走一遍）。
        self._reset_state()
        for rec in self._fakes:
            rec._done.set()
        self._yield.stop()
        meeting_mod.MeetingRecorder = self._orig_recorder
        meeting_mod.resolve_input_device = self._orig_resolve
        meeting_mod._transcribe_meeting = self._orig_transcribe
        self._reset_state()
        meeting_mod.meetings_dir = self._orig_dir
        db.DATA_DIR, db.DB_FILE = self._old_db
        # 缓存是按**这个临时库**读满的（apiAuthEnabled=True 就在里面）：
        # 交出去之前必须丢掉，否则下一个模块会拿这份快照当"本机设置"。
        settings._drop_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    # ------------------------------------------------------------ 小工具

    def _headers(self, relay=True):
        """手机那条路的请求头：**令牌 + 中继标记**；本机面板只带令牌、不带标记。"""
        h = {"Authorization": "Bearer %s" % self.token}
        if relay:
            h[relay_mod.RELAY_MARKER_HEADER] = "1"
        return h

    def _start_raw(self, params=None, relay=True):
        r = self.client.post("/api/meeting/start", params=params or {},
                             headers=self._headers(relay=relay))
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _start(self, source=None, client_id="", started_at="", relay=True):
        params = {}
        if source is not None:
            params["source"] = source
        if client_id:
            params["clientId"] = client_id
            params["startedAt"] = started_at or T0
        return self._start_raw(params, relay=relay)

    def _status(self):
        r = self.client.get("/api/meeting/status", headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _segment(self, seq, client_id="", wav=None, content_type="audio/wav"):
        params = {"seq": seq}
        if client_id:
            params["clientId"] = client_id
        r = self.client.post("/api/meeting/segment", params=params,
                             content=wav if wav is not None else _tiny_wav(),
                             headers={**self._headers(), "Content-Type": content_type})
        return r

    def _meta(self, name):
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _files(self, name, suffix=None):
        out = sorted(f for f in os.listdir(os.path.join(self.tmp, name))
                     if f != "meta.json")
        if suffix:
            out = [f for f in out if f.lower().endswith(suffix)]
        return out

    def _started_event(self):
        """临时库里 `meeting_started` 事件（payload 已解析）。"""
        for row in reversed(db.list_events(limit=200)):
            if row.get("type") == "meeting_started":
                return json.loads(row.get("payload") or "{}")
        self.fail("没有 meeting_started 事件")


class PhoneSourceTests(_Base):
    """① `source=phone`（含带 `clientId` 的补传）必须建出**手机麦**会议。"""

    def test_explicit_source_phone_does_not_touch_the_sound_card(self):
        """`source=phone`：**不建 `MeetingRecorder`**、状态 `source=phone`、没有电脑麦段。"""
        body = self._start(source="phone")
        self.assertTrue(body["ok"], body)
        self.assertEqual("phone", body["source"], "回包要能说自己这一场是手机麦")
        self.assertEqual(0, len(self.recorder_calls),
                         "手机在录，电脑**绝不许**开麦（这就是那次真机事故的现场）")

        st = self._status()
        self.assertTrue(st["active"])
        self.assertEqual("phone", st["source"], "判据是 meeting_status() 的 source == phone")
        self.assertEqual(0, st["segments"], "还没传段，电脑侧不该凭空多出一段")

        name = body["message"]
        self.assertEqual([], self._files(name), "会议目录里不该有电脑麦写出来的音频")
        self.assertEqual("phone", self._meta(name).get("source"))
        self.assertEqual("phone", self._started_event().get("source"),
                         "事件里也要带采集源：手机要靠它确认'这场是手机麦'")

    def test_client_id_start_is_phone_mic_and_still_idempotent(self):
        """带 `clientId` 建会 = 手机麦；同一个 id 再调 → 同一场 + `duplicate: true`。"""
        first = self._start(source="phone", client_id=CID)
        self.assertTrue(first["ok"], first)
        self.assertFalse(first["duplicate"], "第一次不可能是'重复'")
        self.assertEqual(0, len(self.recorder_calls), "补传那条路也不许开电脑的麦")

        second = self._start(source="phone", client_id=CID)
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["duplicate"], "重试必须命中幂等，不能建第二场")
        self.assertEqual(first["meetingId"], second["meetingId"], "必须回同一场会议的 id")
        self.assertEqual("phone", second["source"], "幂等命中也要说清是手机麦")

        self.assertEqual(1, len(db.list_meetings()), "同一个 clientId 只该建一场会")
        self.assertEqual(1, len(os.listdir(self.tmp)), "磁盘上也只该有一个会议目录")
        self.assertEqual("phone", self._status()["source"], "幂等之后 source 仍是 phone")
        self.assertEqual(0, len(self.recorder_calls), "全程一次声卡都不许碰")

    def test_segments_are_accepted_after_a_phone_start_and_reconciled(self):
        """建会之后传段必须**被接受**（落成 `NN.wav` / `NN.flac`），对账段数正确。"""
        started = self._start(source="phone", client_id=CID)
        name = started["message"]

        wav = self._segment(1, client_id=CID)
        self.assertEqual(200, wav.status_code, wav.text)
        b1 = wav.json()
        self.assertTrue(b1["ok"], b1)
        self.assertEqual("01.wav", b1["name"])
        self.assertEqual(1, b1["segmentCount"])
        self.assertEqual([1], b1["segments"])

        flac = self._segment(2, client_id=CID, wav=_wav_to_flac(_tiny_wav()),
                             content_type="audio/flac")
        self.assertEqual(200, flac.status_code, flac.text)
        b2 = flac.json()
        self.assertTrue(b2["ok"], b2)
        self.assertEqual("02.flac", b2["name"], "FLAC body 落成 NN.flac（后缀由魔数定）")
        self.assertEqual(2, b2["segmentCount"])
        self.assertEqual([1, 2], b2["segments"], "对账等式：服务端段序号 == 客户端最大段序号")

        self.assertEqual(["01.wav", "02.flac"], self._files(name))
        self.assertEqual(["01.wav", "02.flac"], self._meta(name)["segments"])
        self.assertEqual(2, self._status()["segments"], "状态接口的段数要给手机显示进度")

    def test_the_phone_mic_meeting_has_no_computer_segments_on_disk(self):
        """整条路走完（建会 → 传段 → 收尾）：盘上只有**手机传的**那几段。"""
        started = self._start(source="phone", client_id=CID)
        name = started["message"]
        self._segment(1, client_id=CID)
        self._segment(2, client_id=CID)

        r = self.client.post("/api/meeting/stop", params={"clientId": CID},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["ok"], r.json())

        self.assertEqual(0, len(self.recorder_calls), "全程没有电脑麦")
        self.assertEqual(["01.wav", "02.wav"], self._files(name, ".wav"))
        self.assertEqual([], self._files(name, ".flac"), "这一场没传过 flac，就不该有 flac")
        meta = self._meta(name)
        self.assertEqual("phone", meta.get("source"))
        self.assertEqual(["01.wav", "02.wav"], meta.get("segments"))
        self.assertTrue(meta.get("end"), "停止时必须写下结束时间")


class RealDeviceRegressionTests(_Base):
    """② **真机那条路**：手机发起、却一个 query 都不带（本次事故的回归用例）。"""

    def test_a_relayed_start_without_any_query_is_a_phone_mic_meeting(self):
        """中继进来 + 无参数 → 手机麦（不是"电脑的麦"）。

        这就是 2026-10-17 真机那次：App 的「开始录音」打 `api().meetingStart()`，
        请求里连 `?` 都没有。旧代码把 `source` 的缺省值当 `"pc"` → 建成本机麦会议。
        判据全部在这里：状态 `source == "phone"`、`MeetingRecorder` **0 次**、
        目录里没有电脑麦段——而且**手机传的段必须被收下**（不再是"本机麦拒收外来段"）。
        """
        body = self._start_raw({})                    # 一个 query 都不带
        self.assertTrue(body["ok"], body)
        self.assertEqual("phone", body["source"],
                         "手机发起的录音要如实回'手机麦'（面板/手机都靠它显示采集源）")
        self.assertEqual(0, len(self.recorder_calls),
                         "电脑端绝不能因为手机发起就跟着开麦（用户原话：'电脑端也变成录音状态了'）")

        st = self._status()
        self.assertEqual("phone", st["source"])
        name = body["message"]
        self.assertEqual([], self._files(name), "没有电脑麦写出来的段")
        self.assertEqual("phone", self._meta(name).get("source"))
        self.assertEqual("mobile", self._meta(name).get("initiatedBy"),
                         "发起方仍是 mobile（与采集源是两件事，别混）")
        ev = self._started_event()
        self.assertEqual("phone", ev.get("source"))
        self.assertEqual("mobile", ev.get("origin"))

        # 手机按段传上来：必须**被接受**（旧行为会 409"这场录音用的是电脑的麦克风"）
        r = self._segment(1)
        self.assertEqual(200, r.status_code,
                         "手机发起的会却在拒收手机的段 —— 这正是那次真机事故")
        self.assertTrue(r.json()["ok"], r.json())
        self.assertEqual(["01.wav"], self._files(name))

    def test_a_relayed_phone_start_still_honours_an_explicit_pc(self):
        """手机**显式**要电脑的麦（`source=pc`）仍然算本机麦 —— 判据不许做成一刀切。

        用户 2026-10-16 的口径：发起方与采集源是两件事，"人在会议室、就用电脑那支麦录"
        是合法组合。`tests/test_event_origin.py` 也从事件那条线钉着同一格。
        """
        body = self._start(source="pc")
        self.assertTrue(body["ok"], body)
        self.assertEqual("pc", body["source"], "显式 pc 不许被'手机发起'改判")
        self.assertEqual(1, len(self.recorder_calls), "这条才是真的要开电脑的麦")
        self.assertEqual("pc", self._status()["source"])
        self.assertEqual("mobile", self._meta(body["message"]).get("initiatedBy"))


class LegacyPanelPathTests(_Base):
    """③ 不带 `source` 的老调用（面板 / `meeting-record` 技能）**逐字不变**。"""

    def test_a_panel_start_without_source_is_still_the_computer_mic(self):
        """本机面板（不带中继标记、不带 source）→ 本机麦 + 回包 `source: "pc"`。

        回包被 `tests/test_api_contract.py::test_start_success_shape` 逐字钉着
        （`{"ok", "message", "source": "pc"}`）—— 这次修 bug **一个字都没动它**。
        """
        body = self._start_raw({}, relay=False)
        self.assertEqual({"ok": True, "message": body["message"], "source": "pc"}, body,
                         "老调用（面板/技能）的回包形状与取值必须原样")
        self.assertEqual(1, len(self.recorder_calls), "面板那条路照旧开本机麦")
        st = self._status()
        self.assertEqual("pc", st["source"])
        self.assertEqual("pc", self._meta(body["message"]).get("initiatedBy"))


class TheLocalMicGuardIsNotRelaxedTests(_Base):
    """④ "本机麦会议拒收外来段"这条护栏**必须保留**（它是对的）。"""

    def test_a_computer_mic_meeting_still_refuses_phone_segments(self):
        """两路音频混进同一场 = 转写里两个人同时说话 —— 拒绝理由也要原样。"""
        opened = self._start_raw({}, relay=False)         # 本机麦那场
        self.assertEqual(1, len(self.recorder_calls))

        r = self._segment(1)
        self.assertEqual(409, r.status_code, "本机麦会议收下手机段 = 混音，必须拒")
        detail = str(r.json().get("detail") or "")
        self.assertIn("电脑的麦克风", detail)
        self.assertEqual([], self._files(opened["message"]),
                         "拒收时一个字节都不许落盘")


if __name__ == "__main__":
    unittest.main()
