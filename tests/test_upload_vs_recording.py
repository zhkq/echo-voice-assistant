# -*- coding: utf-8 -*-
"""**仅上传 ≠ 在录音**：手机补传占着位时，电脑要能开一场真的会（2026-10-20 真机）。

真机现场（`2026-10-20`）：手机补传卡住 → 服务器 `active=true`（`source=phone`）→
用户在**电脑**上开录 → 被"会议录音已在进行中"挡住 → 最后只能手工 `POST
/api/meeting/stop` 收尾（代价：手机剩下 2 段进不来，见报告③）。

根因：`start_meeting()` 只看 `_state["active"]` 这一个布尔 —— 而"占着位"的那一场
是**已经录完、只剩收段**的手机补传（客户端明说了 `endedAt`；盘上
`meta["uploading"]=true`、没有 `end`）。它根本不是在录音，却把电脑挡在门外。

要钉住的语义（用户口径，三条都不能破）：

  1. **仅上传 ≠ 在录音**：占着位的是"只剩收段"的补传时，电脑 `start_meeting`（pc）
     **直接新建**一场，而且**不动它**（那场的目录 / `meta.json` / 已收段一个字节都不碰，
     它后面还要接着收段、还要能自己收尾）；
  2. **同一时间只能有一场真在录**：两场都是真录音（电脑本机麦 / 手机边录边传 /
     另一台手机）→ **仍拒**（"会议录音已在进行中"）。这条一个字不放宽；
  3. **它的段仍要落进它自己那场**：电脑抢了 `_state` 之后，手机后续的段靠
     `_apply_segment` → `_uploading_meeting_for(clientId)`（**读盘**，不依赖 `_state`）
     落回自己那场 —— 这里补用例把这件事钉住；
  4. **对外状态要能区分**：`/api/meeting/status` 与 `/api/status.meeting` 在"仅上传"时
     给 `uploading: true`；**`active` 语义保持不变**（不改 false —— 那会动 pause/stop/
     客户端契约），消费者按 `active && uploading` 认"只剩收段"。

测试卫生：临时库 + 临时会议目录 + 转写打桩 + 假麦克风流（同 `tests/test_phone_finalize_upload.py`）；
**绝不碰**真库 / 真会议目录 / 真声卡。`settings._cache` 是进程级的，setUp/cleanup 都要丢掉。
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

from app import db                                                   # noqa: E402
from app import meeting as meeting_mod                               # noqa: E402
from app.api import router                                           # noqa: E402
from app.config import settings                                      # noqa: E402

#: 手机那场补传的稳定 id 与它"在手机上录完"的时刻（真机形状：录完了才联网补传）。
CID = "phone-20261020125605-9f3c"
OTHER_CID = "phone-20261020131000-77aa"
T0 = "2026-10-20T12:56:05"
T1 = "2026-10-20T13:40:00"

BUSY = "会议录音已在进行中"


def _tiny_wav(frames=160):
    """一个最小的合法 WAV（44 字节头 + `frames` 个静音采样）。"""
    data = b"\x00\x00" * int(frames)
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


class _FakeStream:
    """假麦克风流：录音器要的东西就那么几样（上下文 / read / device）。"""

    device = "fake"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        import numpy as np
        import time as _t
        _t.sleep(0.02)                 # 给录音线程一点节拍，别把 CPU 打满
        return np.zeros(min(int(n), 1600), dtype="int16"), False


class UploadVsRecordingTests(unittest.TestCase):
    """走**真 API**（真机撞的就是 `POST /api/meeting/start` 这条路）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-upload-vs-rec-mtg-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-upload-vs-rec-db-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        settings._drop_cache()

        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        self._reset_process_state()

        # 转写打桩：真跑会去连 ASR/能力后端。这里只关心"事件顺序"。
        self.transcribe_calls = []
        self._orig_transcribe = meeting_mod._transcribe_meeting
        meeting_mod._transcribe_meeting = lambda folder, *a, **kw: \
            self.transcribe_calls.append(folder)
        self.addCleanup(lambda: setattr(meeting_mod, "_transcribe_meeting",
                                        self._orig_transcribe))

        # 假麦克风：只有"电脑真的开录"那一档会用到；不碰真声卡。
        from app.audio import recorder as recorder_mod
        self._recorder_mod = recorder_mod
        self._orig_input_stream = recorder_mod.input_stream
        recorder_mod.input_stream = lambda *a, **kw: _FakeStream()
        self.addCleanup(lambda: setattr(recorder_mod, "input_stream",
                                        self._orig_input_stream))

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self.addCleanup(self._clean)

    # ------------------------------------------------------------ 小工具

    def _reset_process_state(self):
        """把进程内会议状态清成"刚重启"的样子（`_state` 只活在进程里）。"""
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0)

    def _clean(self):
        try:
            if meeting_mod._state["active"]:
                meeting_mod.stop_meeting()
        except Exception:                                          # noqa: BLE001
            pass
        self._reset_process_state()
        meeting_mod.meetings_dir = self._orig_dir
        db.DATA_DIR, db.DB_FILE = self._old_db
        # 缓存是按**这个临时库**读满的：交出去之前必须丢掉，否则下一个模块会
        # 拿着这份快照当"本机设置"（`tests/test_events_feed.py` 记着这条教训）。
        settings._drop_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.dbdir, ignore_errors=True)

    def _start_upload(self, client_id=CID, started_at=T0, ended_at=T1):
        """手机补传建会：带了 `endedAt` = 客户端已录完、音频还在按段上传。"""
        r = self.client.post("/api/meeting/start",
                             params={"source": "phone", "clientId": client_id,
                                     "startedAt": started_at, "endedAt": ended_at})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        return body

    def _start_pc(self):
        """电脑开录（`/api/meeting/start` 一个 query 都不带 = 本机麦那条老路）。"""
        r = self.client.post("/api/meeting/start")
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _segment(self, seq, client_id=CID, wav=None):
        r = self.client.post("/api/meeting/segment",
                             params={"clientId": client_id, "seq": seq,
                                     "startedAt": T0, "source": "phone"},
                             content=wav if wav is not None else _tiny_wav(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        return body

    def _meta(self, name):
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _files(self, name):
        """目录里每个文件的字节数（用来证明"一个字节都没动"）。"""
        out = {}
        folder = os.path.join(self.tmp, name)
        for f in sorted(os.listdir(folder)):
            out[f] = os.path.getsize(os.path.join(folder, f))
        return out

    def _wavs(self, name):
        folder = os.path.join(self.tmp, name)
        if not os.path.isdir(folder):
            return []
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(".wav"))

    def _wait_call_for(self, folder, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if folder in self.transcribe_calls:
                return True
            time.sleep(0.01)
        return False

    # ------------------------------------------------------------ ① 核心：仅上传不挡电脑

    def test_a_pc_meeting_can_start_while_a_phone_upload_is_unfinished(self):
        """仅上传占位时，电脑新建**成功**；那场补传不受影响，剩下的段仍落它自己那场。

        这就是真机那次被挡住的路：`start(phone, endedAt)` → 电脑 `start(pc)`。
        """
        upload = self._start_upload()
        upload_name = upload["message"]
        before_meta = self._meta(upload_name)
        before_files = self._files(upload_name)
        self.assertTrue(before_meta.get(meeting_mod.UPLOADING_KEY),
                        "前提：这一场是'已结束、只剩收段'（盘上有 uploading 标记）")
        self.assertFalse(before_meta.get("end"), "前提：它还没收尾")

        # 电脑开一场真的会：**必须成功**，而且是另一场
        pc = self._start_pc()
        self.assertTrue(pc["ok"], pc)
        self.assertEqual("pc", pc["source"])
        pc_name = pc["message"]
        self.assertNotEqual(upload_name, pc_name, "电脑这场必须是**另一场**会")
        self.assertEqual(os.path.abspath(os.path.join(self.tmp, pc_name)),
                         os.path.abspath(meeting_mod._state["folder"]))
        self.assertEqual("pc", meeting_mod._state["source"])

        # **不动它**：那场补传的 meta 与文件一个字节都没变
        self.assertEqual(before_meta, self._meta(upload_name),
                         "电脑开会不许碰'只剩收段'那场的 meta（它还要接着收段）")
        self.assertEqual(before_files, self._files(upload_name),
                         "电脑开会不许碰它的目录/段")

        # 它的段仍要能落进**它自己那场**（`_apply_segment` 靠盘上的 clientId，不靠 `_state`）
        seg = self._segment(1)
        self.assertTrue(seg["ok"], seg)
        self.assertEqual(["01.wav"], self._wavs(upload_name),
                         "电脑已经在录了，手机这一段仍必须落回它自己那场")
        self.assertEqual(["01.wav"], self._meta(upload_name)["segments"])
        phone_body = _tiny_wav()
        for f in self._wavs(pc_name):
            with open(os.path.join(self.tmp, pc_name, f), "rb") as fh:
                self.assertNotEqual(phone_body, fh.read(),
                                    "手机那段被写进了电脑正在录的那一场（%s）" % f)

        # 电脑真在录时，状态接口**不许**再标成"仅上传"
        st = self.client.get("/api/meeting/status").json()
        self.assertTrue(st["active"])
        self.assertNotIn("uploading", st,
                         "电脑真的在录，不能被标成'只剩收段'（那会让面板以为可以再开一场）")

        # 收尾电脑这场：只结束它，补传那场还在等手机剩下的段
        stop = self.client.post("/api/meeting/stop")
        self.assertEqual(200, stop.status_code, stop.text)
        self.assertTrue(stop.json()["ok"], stop.json())
        meta_after_pc_stop = self._meta(upload_name)
        self.assertTrue(meta_after_pc_stop.get(meeting_mod.UPLOADING_KEY),
                        "停电脑那场不许顺手把手机补传收尾（手机还在传）")
        self.assertFalse(meta_after_pc_stop.get("end"))

        # 手机最后自己收尾：清标记、落下 end，才发起转写
        done = self.client.post("/api/meeting/stop", params={"clientId": CID})
        self.assertEqual(200, done.status_code, done.text)
        self.assertTrue(done.json()["ok"], done.json())
        meta_done = self._meta(upload_name)
        self.assertNotIn(meeting_mod.UPLOADING_KEY, meta_done)
        self.assertEqual(T1, meta_done.get("end"), "结束时间用客户端录完的那个时刻")
        self.assertTrue(self._wait_call_for(os.path.join(self.tmp, upload_name)),
                        self.transcribe_calls)

    def test_the_phone_can_finish_its_upload_while_the_pc_is_still_recording(self):
        """电话收尾那半边：电脑在录时，手机带自己的 `clientId` 收尾 → **只结束它那场**，
        电脑那场一个字节不动（`stop_meeting` 的既有护栏，本条不许被放宽）。"""
        upload = self._start_upload()
        upload_name = upload["message"]
        pc = self._start_pc()
        pc_name = pc["message"]
        self._segment(1)

        done = self.client.post("/api/meeting/stop", params={"clientId": CID})
        self.assertEqual(200, done.status_code, done.text)
        self.assertTrue(done.json()["ok"], done.json())
        self.assertNotIn(meeting_mod.UPLOADING_KEY, self._meta(upload_name))
        self.assertTrue(self._meta(upload_name).get("end"))
        # 电脑那场还在录（收尾没有把它停掉）
        self.assertTrue(meeting_mod._state["active"])
        self.assertEqual(os.path.join(self.tmp, pc_name), meeting_mod._state["folder"])
        st = self.client.get("/api/meeting/status").json()
        self.assertTrue(st["active"])
        self.assertNotIn("uploading", st, "收尾了手机那场，电脑仍是一场真录音")

        stop = self.client.post("/api/meeting/stop")
        self.assertTrue(stop.json()["ok"], stop.json())

    # ------------------------------------------------------------ ② 状态要能区分
    def test_status_says_uploading_but_keeps_active_as_before(self):
        """`/api/meeting/status` 与 `/api/status.meeting`：仅上传时 `uploading=true`，
        而 `active` **语义不变**（仍是"内存里有一场占着" —— 不改 false）。"""
        upload = self._start_upload()
        upload_name = upload["message"]

        st = self.client.get("/api/meeting/status").json()
        self.assertTrue(st["active"],
                        "`active` 语义不动：pause/stop/客户端契约都按它走，不能改成 false")
        self.assertIs(st.get("uploading"), True,
                      "仅上传时要能给 `uploading: true`（否则消费端分不出'在录'与'在收段'）")
        self.assertEqual(CID, st.get("clientId"))
        self.assertEqual(upload_name, st.get("folder"))

        status = self.client.get("/api/status").json()
        self.assertIs(status["meeting"].get("uploading"), True,
                      "/api/status.meeting 与 /api/meeting/status 必须是同一份判据")
        self.assertTrue(status["meeting"]["active"])

    # ------------------------------------------------------------ ③ 一场真录音仍然只许一场

    def test_two_pc_recordings_are_still_refused(self):
        """两场**真**录音 → 仍拒。②那一条只放过"只剩收段"，不许顺手放宽这一条。"""
        first = self._start_pc()
        self.assertTrue(first["ok"], first)
        second = self._start_pc()
        self.assertFalse(second["ok"], second)
        self.assertEqual(BUSY, second["message"])
        self.assertEqual("", second["source"])
        self.assertEqual(1, len([n for n in os.listdir(self.tmp)]),
                         "被拒的那次不该建出第二个会议目录")

    def test_a_live_phone_recording_still_blocks_the_pc(self):
        """手机**边录边传**（没有 `endedAt`）= 真在录 → 电脑仍被拒，而且它没有 uploading 标记。"""
        live = self._start_upload(started_at=T0, ended_at="")
        live_name = live["message"]
        meta = self._meta(live_name)
        self.assertNotIn(meeting_mod.UPLOADING_KEY, meta,
                         "还没录完的会不该有'补传未完成'标记")
        self.assertTrue(meeting_mod._state["active"])

        st = self.client.get("/api/meeting/status").json()
        self.assertTrue(st["active"])
        self.assertNotIn("uploading", st, "手机真的在录 —— 不是'仅上传'")

        blocked = self._start_pc()
        self.assertFalse(blocked["ok"], blocked)
        self.assertEqual(BUSY, blocked["message"])
        self.assertEqual(1, len(os.listdir(self.tmp)), "被拒的那次不该建出第二场")
        self.assertEqual(meeting_mod._state["folder"], os.path.join(self.tmp, live_name))

        # 手机自己收尾，一切照旧（先传一段，否则收尾会如实报"没有录到音频"）
        self._segment(1)
        stop = self.client.post("/api/meeting/stop", params={"clientId": CID})
        self.assertTrue(stop.json()["ok"], stop.json())

    def test_a_second_phone_upload_is_not_unlocked_by_this_change(self):
        """两台手机的补传之间仍是既有行为（本次不动）：第二台仍被"已在进行中"挡住。"""
        self._start_upload()
        r = self.client.post("/api/meeting/start",
                             params={"source": "phone", "clientId": OTHER_CID,
                                     "startedAt": T0, "endedAt": T1})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertFalse(body["ok"], body)
        self.assertEqual(BUSY, body["message"])


if __name__ == "__main__":
    unittest.main()
