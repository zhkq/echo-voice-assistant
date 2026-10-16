# -*- coding: utf-8 -*-
"""手机**离线录音补传**的服务端契约（`start → segment…（可续传）→ 对账 → stop`）。

用户的需求是"**手机断网也能录、联网补传**"：安卓侧断网时**完全本地**录音（音频留在手机上），
联网后把这一场补到电脑上。安卓那一半已经做完了（`OfflineMeetingSync` /
`OfflineMeetingTransport` / `OfflineMeetingDtos`），本文件钉的是**服务端这一半**：

  1. `POST /api/meeting/start` 收 **query**：`source=phone` + `clientId`（客户端稳定 id）
     + `startedAt`（**缓存的**开始时间）+ 可选 `endedAt`。**没有请求体**。
  2. **`clientId` 是给"重试"用的幂等键**：同一个 id 反复调只该建**一场**、回**同一个**
     `meetingId`（重复时 `duplicate: true`）；它同时落进那场会议的 `meta.json` 与库里
     （`db.meeting_name_by_client_id()` —— 下次就靠它把会议找回来）。
  3. **采用客户端的 `startedAt` 当会议开始时间**（10:00 录的、11:00 才联网 → 时间轴仍是
     10:00），并且**另外**记服务端自己的"收到时间"（两者都留，别混为一谈：手机时钟可能不准）。
  4. `POST /api/meeting/segment` 收 query `clientId` + `seq`（**1 起**）+ 可选
     `startedAt`/`source`，body 仍是**裸 `audio/wav`**（不是 multipart）；
     **重复 `(clientId, seq)` 不许存两遍**（已存在 = 成功 + `duplicate: true`）。
  5. 段响应要回**这一场累计段数**（客户端对账等式 = 服务端段数 == 本地最大段序号），
     并且同时给 `segmentCount` / `seqCount` / `segments` 三个别名（多档客户端解析）。
  6. `GET /api/meeting/status` 额外带 `clientId`（保留 `source`）：这是客户端判"服务端现在
     录的是不是我这一份"的**最强判据**。
  7. `POST /api/meeting/stop` 接受可选 `clientId`：**给了且与当前那场不符 → 拒绝且不停**
     （否则一个错序/重试的补传就能把电脑正在录的会停掉）；没给时保持既有行为。
  8. `stop` 之后这场**照既有流程发起转写**（"补传上去能出文字"的关键）。

测试卫生：临时库 + 临时会议目录 + 临时凭据（`apiAuthEnabled=True` + 一把临时令牌，
并带中继标记 —— 手机是唯二能这么进来的路），**绝不碰**真库/真会议目录/真令牌；
`settings._cache` 是**进程级**的，用完必须丢掉（`tests/test_events_feed.py` 记着这条教训）。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

import app.api as api_mod                                            # noqa: E402
from app import db                                                   # noqa: E402
from app import meeting as meeting_mod                               # noqa: E402
from app.api import router                                           # noqa: E402
from app.config import settings                                      # noqa: E402
from app import relay as relay_mod                                   # noqa: E402

#: 客户端稳定 id 的形状（安卓侧 `phone-<yyyyMMddHHmmss>-<4 位>`）。
CID = "phone-20261016203102-1a2b"
OTHER_CID = "phone-20261016203102-9f9f"
#: 客户端缓存的开始/结束时间：手机 10:00 开录、10:40 录完，11:00 才联网补传。
T0 = "2026-10-16T10:00:00"
T1 = "2026-10-16T10:40:00"


def _tiny_wav() -> bytes:
    """一个最小的合法 WAV（44 字节头 + 160 个静音采样）—— 与既有的手机麦用例同一份。"""
    import struct
    data = b"\x00\x00" * 160
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


class OfflineSyncTests(unittest.TestCase):
    """补传这条路的服务端契约（走**真 API**：契约说的就是 HTTP 面）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-offline-mtg-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-offline-db-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        # ⚠️ 刻意**不**调 `settings.seed_defaults()`：它要写上百行设置（本机实测 2 秒/次），
        # 而这些用例只读几个有内联默认值的键（`meetingSegmentMinutes` 之类）——
        # 种一遍默认值等于让每个用例白等两秒，门禁会慢得没必要。

        # ---- 临时凭据：把鉴权**打开**，这样"手机带令牌经中继进来"这件事真的被验到 ----
        # 不打开的话 `optional_auth()` 全放行，用例就分不出"带没带凭据"了。
        settings._drop_cache()
        db.set_setting("apiAuthEnabled", True)
        self.token = db.add_api_key_row("offline-sync-test", scopes=["device"])["token"]
        self.assertTrue(settings.get("apiAuthEnabled", False),
                        "setUp 没把 apiAuthEnabled 摆成 True：这条路的鉴权前提不成立")

        # ---- 临时的会议目录（**绝不许**往真库/真目录里写一场会议）----
        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0)

        # ---- 转写打桩：真跑转写会去连 ASR/能力后端（几秒到几分钟，还可能写日志/建会话）----
        # 这一步只钉"stop 之后**有没有**发起转写"，所以把那条链路的入口换掉。
        # 为什么打在最外层 `_transcribe_meeting`：`stop_meeting()` 起的线程就是它；
        # 换掉它等于把"ASR/后端"整条下游都替掉（与 `tests/test_meeting_phone_mic.py` 同一手法）。
        self.transcribe_calls = []
        self._transcribe_done = threading.Event()
        self._orig_transcribe = meeting_mod._transcribe_meeting

        def _fake_transcribe(folder, *a, **kw):
            self.transcribe_calls.append(folder)
            self._transcribe_done.set()

        meeting_mod._transcribe_meeting = _fake_transcribe
        self.addCleanup(lambda: setattr(meeting_mod, "_transcribe_meeting",
                                        self._orig_transcribe))

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        def _clean():
            meeting_mod._state.update(active=False, folder=None, recorder=None,
                                      paused=False, pauses=[], source="pc", seg_count=0,
                                      client_id="", meeting_id=0)
            meeting_mod.meetings_dir = self._orig_dir
            db.DATA_DIR, db.DB_FILE = self._old_db
            # 缓存是按**这个临时库**读满的（apiAuthEnabled=True 就在里面）：
            # 交出去之前必须丢掉，否则下一个模块会拿着这份快照当"本机设置"。
            settings._drop_cache()
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    # ------------------------------------------------------------ 小工具

    def _headers(self):
        """手机那条路的请求头：**令牌 + 中继标记**（中继是手机唯一能进来的路）。"""
        return {"Authorization": "Bearer %s" % self.token,
                relay_mod.RELAY_MARKER_HEADER: "1"}

    def _start(self, client_id=CID, started_at=T0, ended_at="", source="phone"):
        r = self.client.post("/api/meeting/start",
                             params={"source": source, "clientId": client_id,
                                     "startedAt": started_at, "endedAt": ended_at},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _start_plain_phone(self):
        """既有的那条"手机麦"路：`source=phone` 但**不带** clientId（2026-10-15 的契约）。"""
        r = self.client.post("/api/meeting/start", params={"source": "phone"},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _post_segment(self, seq, client_id=CID, wav=None, source="phone"):
        r = self.client.post("/api/meeting/segment",
                             params={"clientId": client_id, "seq": seq,
                                     "startedAt": T0, "source": source},
                             content=wav if wav is not None else _tiny_wav(),
                             headers={**self._headers(), "Content-Type": "audio/wav"})
        return r

    def _segment(self, seq, client_id=CID, wav=None):
        """传一段并断言被收下（返回响应体）。"""
        r = self._post_segment(seq, client_id=client_id, wav=wav)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        return body

    def _stop(self, client_id=""):
        params = {"clientId": client_id} if client_id else {}
        r = self.client.post("/api/meeting/stop", params=params, headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _status(self):
        r = self.client.get("/api/meeting/status", headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _meta(self, name):
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _wavs(self, name):
        return sorted(f for f in os.listdir(os.path.join(self.tmp, name))
                      if f.lower().endswith(".wav"))

    def _wait_transcribe(self, timeout=5.0):
        return self._transcribe_done.wait(timeout)

    # ------------------------------------------------------------ ① 幂等建会

    def test_the_same_client_id_builds_exactly_one_meeting(self):
        """同一个 clientId 连调两次 `start`：只有一场会、同一个 id、第二次 `duplicate`。"""
        first = self._start()
        second = self._start()
        self.assertTrue(first["ok"], first)
        self.assertFalse(first["duplicate"], "第一次不可能是'重复'")
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["duplicate"],
                        "补传会因为超时重发 —— 第二次必须是幂等命中，不能当成新建")
        self.assertEqual(first["meetingId"], second["meetingId"], "必须回同一场会议的 id")
        self.assertEqual(first["message"], second["message"])
        self.assertEqual(1, len(db.list_meetings()), "同一个 clientId 只该建一场会")
        self.assertEqual(1, len(os.listdir(self.tmp)), "磁盘上也只该有一个会议目录")

    def test_the_client_id_lands_in_meta_and_in_the_database(self):
        """`clientId` 要落进那场会议的 `meta.json` **与库里**（后面要能按它找回会议）。"""
        r = self._start()
        name = r["message"]
        self.assertEqual(CID, self._meta(name)[meeting_mod.CLIENT_ID_KEY],
                         "meta.json 里要有 clientId（人肉排查时打开目录就看得到）")
        self.assertEqual(name, db.meeting_name_by_client_id(CID),
                         "库里要能按 clientId 找回这场会议（`start` 的幂等就靠它）")
        # 幂等索引丢了也要认得出来：`meta.json` 那一遍是兜底
        self.assertEqual(name, meeting_mod._scan_name_by_client_id(CID))
        db._exec("DELETE FROM meta WHERE key LIKE ?", ("offline_meeting_client:%",))
        self.assertEqual(name, meeting_mod.meeting_name_by_client_id(CID),
                         "库里的索引没了，就必须从 meta.json 认出来 —— 否则会再建一场")

    def test_a_different_client_id_is_a_different_meeting(self):
        """不同 id = 不同的一场（幂等键不许把它当成重复）。"""
        first = self._start(CID)
        self._stop(CID)
        second = self._start(OTHER_CID)
        self.assertFalse(second["duplicate"], second)
        self.assertNotEqual(first["meetingId"], second["meetingId"])

    def test_start_while_another_meeting_is_recording_does_not_take_over(self):
        """电脑那场正在录时，补传不许抢占（既有行为：如实说"已在进行中"）。"""
        with patch.object(api_mod.meeting, "start_meeting",
                          return_value=(True, "2026-10-16_11-00-00")):
            self.assertEqual(200, self.client.post("/api/meeting/start",
                                                   headers=self._headers()).status_code)
        meeting_mod._state.update(active=True, folder=os.path.join(self.tmp, "x"),
                                  recorder=None, source="pc", client_id="", meeting_id=0)
        r = self._start()
        self.assertFalse(r["ok"], r)
        self.assertIn("已在进行中", r["message"])
        self.assertEqual(0, r["meetingId"], "别人那场的 id 不该回给这个客户端")
        self.assertEqual("", r["source"])

    # ------------------------------------------------------------ ② 采用客户端时间

    def test_the_cached_start_time_is_adopted_and_the_receive_time_is_kept(self):
        """会议开始时间 = 客户端缓存的 `startedAt`；服务端的"收到时间"**另有一份**。"""
        r = self._start(started_at=T0, ended_at=T1)
        row = db.get_meeting(r["meetingId"])
        self.assertEqual(T0, row["started_at"],
                         "10:00 录的、11:00 才补传 → 库里的开始时间必须是 10:00")
        meta = self._meta(r["message"])
        self.assertEqual(T0, meta["start"], "meta 里的开始时间同样采用客户端那个")
        # 服务端自己的"收到时间"：与客户端那个**不是一个东西**，两个都要留
        received = meta[meeting_mod.RECEIVED_AT_KEY]
        self.assertTrue(received, "必须同时记服务端的收到时间（手机时钟可能不准）")
        self.assertNotEqual(T0, received)
        got = datetime.datetime.fromisoformat(received)
        self.assertLess(abs(time.time() - got.timestamp()), 600,
                        "收到时间该是'刚刚'，不是别处抄来的：%s" % received)
        # 客户端说"已经录完了"的时刻也如实留着（与 `end`/`receivedAt` 三件事分开）
        self.assertEqual(T1, meta["clientEndedAt"])

    def test_an_unparseable_start_time_falls_back_to_now_without_failing(self):
        """时间字符串坏了也得能把会建起来（宁可时间轴有偏差，也不能补传不上来）。"""
        before = datetime.datetime.now()
        r = self._start(started_at="十月十六号晚上八点")
        self.assertTrue(r["ok"], r)
        row = db.get_meeting(r["meetingId"])
        self.assertTrue(row["started_at"])
        parsed = datetime.datetime.fromisoformat(row["started_at"])
        self.assertGreaterEqual(parsed, before - datetime.timedelta(seconds=5))

    # ------------------------------------------------------------ ③ 段幂等

    def test_the_same_seq_is_stored_only_once(self):
        """同一 `(clientId, seq)` 传两次：磁盘上**只有一份**，第二次 `duplicate: true`。"""
        r = self._start()
        name = r["message"]
        first = self._segment(1)
        self.assertFalse(first["duplicate"], first)
        second = self._segment(1)
        self.assertTrue(second["ok"], "重复的段**当成功**（那正是'重试不会存两遍'的证据）")
        self.assertTrue(second["duplicate"], second)
        self.assertEqual(["01.wav"], self._wavs(name), "磁盘上只该有一份（同一个文件名）")
        self.assertEqual(["01.wav"], self._meta(name)["segments"], "meta 里也不该有两条")
        self.assertEqual(1, second["segmentCount"])

    def test_a_segment_lands_as_the_seq_number_it_claims(self):
        """段号由**客户端的 seq** 定（1 起）：`NN.wav` 与 `seq` 一一对应，缺段看得见。"""
        r = self._start()
        name = r["message"]
        self._segment(1)
        self._segment(2)
        self._segment(3)
        self.assertEqual(["01.wav", "02.wav", "03.wav"], self._wavs(name))
        self.assertEqual(["01.wav", "02.wav", "03.wav"], self._meta(name)["segments"])

    def test_a_segment_without_client_id_keeps_the_old_numbering(self):
        """不带 clientId 的老路（既有契约）：段号仍由电脑排，行为不变。"""
        self._start_plain_phone()
        for _ in range(2):
            r = self.client.post("/api/meeting/segment", content=_tiny_wav(),
                                 headers={**self._headers(), "Content-Type": "audio/wav"})
            self.assertEqual(200, r.status_code, r.text)
            self.assertTrue(r.json()["ok"], r.text)
        name = self._status()["folder"]
        self.assertEqual(["01.wav", "02.wav"], self._wavs(name))

    def test_a_non_wav_segment_is_still_refused(self):
        """裸 body 但**不是** wav：既有判据不变（下游按 wav 读，塞 mp3 进去会在转写时炸）。"""
        self._start()
        r = self._post_segment(1, wav=b"ID3\x04\x00\x00 this is an mp3")
        self.assertEqual(409, r.status_code, r.text)
        self.assertIn("WAV", r.json()["detail"])

    def test_the_segment_is_uploaded_as_a_raw_wav_body_not_multipart(self):
        """这条接口收的是**裸 wav body**：multipart 进来只会被当成"这段不是 WAV"拒掉。

        为什么值得钉：安卓侧对这条接口的注释就写着"别发 multipart：那条接口不收"
        （`EchoApi.meetingSegmentWithId`）—— 两边口径必须一致，谁改了这一条都要看见红。
        """
        self._start()
        r = self.client.post("/api/meeting/segment", params={"clientId": CID, "seq": 1},
                             files={"file": ("01.wav", _tiny_wav(), "audio/wav")},
                             headers=self._headers())
        self.assertEqual(409, r.status_code, r.text)
        self.assertIn("WAV", r.json()["detail"])

    def test_a_segment_for_someone_elses_client_id_is_refused(self):
        """别把段灌进**别人**那场（两路音频混进同一场 = 转写里两个人同时说话）。"""
        r = self._start()
        bad = self._post_segment(1, client_id=OTHER_CID)
        self.assertEqual(409, bad.status_code, bad.text)
        self.assertEqual([], self._wavs(r["message"]), "拒绝了就不许落盘")

    # ------------------------------------------------------------ ④ 累计段数（对账）

    def test_the_reported_count_is_cumulative(self):
        """传 1、2、3 段之后，响应里的累计数是 1/2/3（不是"本轮收了几段"）。"""
        self._start()
        seen = []
        for seq in (1, 2, 3):
            body = self._segment(seq)
            seen.append((body["segmentCount"], body["seqCount"],
                         len(body["segments"]), body["segmentIndex"]))
        self.assertEqual([(1, 1, 1, 1), (2, 2, 2, 2), (3, 3, 3, 3)], seen,
                         "累计段数（含之前已传的）+ 三个别名都要对得上：%r" % (seen,))

    def test_the_count_survives_an_interrupted_upload(self):
        """中断后续传：先传 1、2，再传 3 → 累计 3（并集，不是"这一轮 1 段"）。"""
        self._start(CID)
        self._segment(1)
        body = self._segment(2)
        self.assertEqual(2, body["segmentCount"])
        # 模拟"断网时客户端歇了一会儿"：服务端状态一个字没动，接着传第 3 段
        resumed = self._segment(3)
        self.assertEqual(3, resumed["segmentCount"], resumed)
        self.assertEqual([1, 2, 3], resumed["segments"])
        self.assertEqual(3, self._status()["segments"], "状态接口的段数也要是累计值")

    # ------------------------------------------------------------ ⑤ status 带 clientId

    def test_status_reports_the_client_id_only_for_the_offline_meeting(self):
        """`clientId` 只在"这一场有客户端 id"时出现 —— 它就是"这场是我的"的判据。"""
        before = self._status()
        self.assertNotIn("clientId", before,
                         "没在录补传那场时不多给键：本机麦那条路的键集是既有契约")
        r = self._start()
        st = self._status()
        self.assertTrue(st["active"])
        self.assertEqual(CID, st["clientId"], "补传那场必须能按 clientId 认出来")
        self.assertEqual(r["meetingId"], st["meetingId"])
        self.assertEqual("phone", st["source"], "既有的 source 要保留")
        self._stop(CID)
        self.assertNotIn("clientId", self._status(), "停了就不再声称'现在录的是这一场'")

    # ------------------------------------------------------------ ⑥ stop 的 clientId 护栏

    def test_stop_with_a_mismatched_client_id_is_refused_and_stops_nothing(self):
        """给了 clientId 但对不上 → 拒绝、**那场会议仍在录**、也不许发起转写。"""
        r = self._start()
        self._segment(1)
        bad = self._stop(OTHER_CID)
        self.assertFalse(bad["ok"], bad)
        self.assertTrue(bad["message"], "拒绝必须给一句明确的原因")
        self.assertIn("clientId", bad["message"])
        # ⚠️ 原因里**不能**出现"没有进行中的会议"那种话：客户端把那句话当成
        # "这场早就停过了、算成功"，于是它会以为转写已经开始（那是谎报）。
        self.assertNotIn("没有进行中的会议", bad["message"])
        st = self._status()
        self.assertTrue(st["active"], "拒绝之后那场会议必须还在录")
        self.assertEqual(CID, st["clientId"])
        self.assertFalse(self._transcribe_done.is_set(), "拒绝的收尾不许发起转写")
        self.assertEqual("recording", db.get_meeting(r["meetingId"])["status"])

    def test_stop_with_the_matching_client_id_finishes_normally(self):
        """相符时正常停 + 收尾（照既有流程发起转写）。"""
        r = self._start()
        self._segment(1)
        body = self._stop(CID)
        self.assertTrue(body["ok"], body)
        self.assertFalse(self._status()["active"])
        self.assertTrue(self._wait_transcribe(), "收尾即转写：stop 之后必须发起转写")
        self.assertEqual("transcribing", db.get_meeting(r["meetingId"])["status"])

    def test_stop_without_a_client_id_keeps_the_old_behaviour(self):
        """没给 clientId → 既有行为一个字不改（面板按钮/技能都走这一条）。"""
        self._start_plain_phone()
        self.client.post("/api/meeting/segment", content=_tiny_wav(),
                         headers={**self._headers(), "Content-Type": "audio/wav"})
        body = self._stop()
        self.assertTrue(body["ok"], body)
        self.assertEqual({"ok", "message"}, set(body), "这条的响应体是既有契约")

    def test_stop_with_a_client_id_when_nothing_is_recording_is_idempotent(self):
        """已经停过（或压根没在录）：回既有那句话 —— 客户端把它当成"早就停过了"。"""
        body = self._stop(CID)
        self.assertFalse(body["ok"])
        self.assertEqual("没有进行中的会议", body["message"])

    def test_the_refusal_message_is_not_mistaken_for_already_stopped(self):
        """护栏那句拒绝原因**不能**被客户端误读成"这场早就停过了、算成功"。

        客户端的判据是**服务端原话里有没有那几句话**（`looksAlreadyStopped`）：只要命中，
        它就认为"收尾其实早就完成了"，于是**不会再重试**、也不会去转写 —— 一场没停的会
        会被记成"已完成"。所以这里把那份短语表照抄进来钉住（两边同源，改一边就红）。
        """
        self._start()
        bad = self._stop(OTHER_CID)
        self.assertFalse(bad["ok"])
        for phrase in ("没有进行中的会议", "没有正在进行的会议", "当前没有会议",
                       "未在录音", "没有在录音"):
            self.assertNotIn(phrase, bad["message"],
                             "这句话会被客户端读成'早就停过了'：%r" % bad["message"])

    def test_retrying_the_whole_flow_after_a_lost_stop_response_is_idempotent(self):
        """`stop` 的回包丢了 → 客户端重走整条流程：不许把同一场变成两场、也不许再传一遍。

        这条路上最要紧的不是"再传一次会不会坏"，而是**客户端能不能收敛**：
        它必须拿到"段都在、这次是重复"，否则一条其实已经完成的补传会永远卡在最后一步。
        """
        started = self._start()
        name, mid = started["message"], started["meetingId"]
        self._segment(1)
        self._segment(2)
        self.assertTrue(self._stop(CID)["ok"])
        self.assertTrue(self._wait_transcribe())
        calls_before = list(self.transcribe_calls)
        # ---- 客户端重试整条流程（它没收到 stop 的回包）----
        again = self._start()
        self.assertTrue(again["duplicate"], again)
        self.assertEqual(mid, again["meetingId"], "重试不许变出第二场")
        self.assertEqual(1, len(db.list_meetings()))
        for seq in (1, 2):
            dup = self._segment(seq)
            self.assertTrue(dup["duplicate"], dup)
            self.assertEqual(2, dup["segmentCount"], "重试时也要回这一场的累计段数（对账靠它）")
        # 这一场**一个字节都没被改写**，也没有第二次转写
        self.assertEqual(["01.wav", "02.wav"], self._wavs(name))
        self.assertEqual(["01.wav", "02.wav"], self._meta(name)["segments"])
        self.assertEqual(calls_before, self.transcribe_calls, "重试不许再触发一次转写")
        # 最后一步：`stop` 幂等 —— 客户端正是靠这句话把这一份标记成已完成
        last = self._stop(CID)
        self.assertFalse(last["ok"])
        self.assertEqual("没有进行中的会议", last["message"])

    def test_a_retried_segment_must_still_be_a_wav(self):
        """重试时也不能拿坏 body 冒充"我重传的是同一段"（收尾过的那场更不许被写坏）。"""
        self._start()
        self._segment(1)
        self._stop(CID)
        bad = self._post_segment(1, wav=b"ID3\x04\x00\x00 not a wav at all")
        self.assertEqual(409, bad.status_code, bad.text)
        # 那一段服务端**没**收过的（seq 9）：这场已经收尾了，不许再往里写音频
        missing = self._post_segment(9)
        self.assertEqual(409, missing.status_code, missing.text)
        self.assertIn("没有在录音", missing.json()["detail"])

    def test_stop_with_a_client_id_does_not_stop_the_local_mic_meeting(self):
        """电脑麦那场正在录时，带着 clientId 的收尾**绝不能**把它停掉。"""
        meeting_mod._state.update(active=True, folder=os.path.join(self.tmp, "pc-meeting"),
                                  recorder=None, source="pc", client_id="", meeting_id=0)
        body = self._stop(CID)
        self.assertFalse(body["ok"], body)
        self.assertTrue(meeting_mod._state["active"], "别人（电脑那场）还在录，不许被停")

    # ------------------------------------------------------------ ⑦ 端到端

    def test_end_to_end_offline_upload_reaches_transcription(self):
        """完整一遍：`start(T0)` → 传 2 段 → 对账 → `stop` → 这场进入转写流程。

        转写本身（ASR/能力后端）在这条用例里是**打桩**的（见 `setUp`），
        这里要钉的是：**音频真的落盘成 `NN.wav`、时间轴从 T0 起、对账能用、收尾真的
        把转写那条流程按下去了** —— 缺了最后一步，用户看到的就是"传上去了但永远没文字"。
        """
        started = self._start(started_at=T0, ended_at=T1)
        name, mid = started["message"], started["meetingId"]
        last = None
        for seq in (1, 2):
            last = self._segment(seq)
        # 客户端对账：服务端段数 == 本地最大段序号（段序号 1 起、连续）
        self.assertEqual(2, last["segmentCount"], last)
        self.assertEqual(2, max(last["segments"]))
        st = self._status()
        self.assertEqual(CID, st["clientId"])
        self.assertEqual(2, st["segments"])
        # 收尾
        body = self._stop(CID)
        self.assertTrue(body["ok"], body)
        # ① 转写流程真的起动了（打桩的那条入口被调用，参数就是这一场的目录）
        self.assertTrue(self._wait_transcribe(), "stop 之后没有发起转写 —— 音频永远不会出文字")
        self.assertEqual(os.path.join(self.tmp, name), self.transcribe_calls[0])
        self.assertEqual("transcribing", db.get_meeting(mid)["status"])
        # ② 音频落盘成 NN.wav，段列表与时长都写全了
        self.assertEqual(["01.wav", "02.wav"], self._wavs(name))
        meta = self._meta(name)
        self.assertEqual(["01.wav", "02.wav"], meta["segments"])
        self.assertGreater(meta["durationSeconds"], 0, "时长要按真的音频算出来")
        # ③ 时间轴从 T0 起；"录完"用客户端的时刻，服务端收尾时刻另记（两个都留）
        self.assertEqual(T0, meta["start"])
        self.assertEqual(T1, meta["end"])
        self.assertTrue(meta.get("stoppedAt"), "服务端收尾时刻也要留一份")
        self.assertEqual(T0, db.get_meeting(mid)["started_at"])
        self.assertEqual(T1, db.get_meeting(mid)["ended_at"])
        # ④ 这场是**手机发起**的（请求带中继标记）→ 之后的事件都得回手机
        events = [e for e in db.list_events_after(0, limit=200)
                  if e["type"] == "meeting_started"]
        payload = json.loads(events[-1]["payload"])
        self.assertEqual("mobile", payload["origin"])
        self.assertEqual(CID, payload["clientId"])

    def test_the_whole_flow_needs_a_token_like_any_other_phone_call(self):
        """开了鉴权就是开了：不带令牌的补传请求照样 401（别把这条路开成后门）。"""
        r = self.client.post("/api/meeting/start",
                             params={"source": "phone", "clientId": CID, "startedAt": T0})
        self.assertEqual(401, r.status_code, r.text)
        self.assertFalse(meeting_mod._state["active"])


if __name__ == "__main__":
    unittest.main()
