# -*- coding: utf-8 -*-
"""手机**离线补传收尾**的服务端判据（2026-10-19 真机 bug 的回归）。

真机现场（别再复现）：手机 10:11–10:54 离线录完，联网后
`POST /api/meeting/start?...&clientId=…&startedAt=10:11:09&endedAt=10:54:48` → **200**，
紧接着 `POST /api/meeting/segment?...&seq=1` → **409**，手机一直显示"正在同步 0/5"。
那场会议目录里 `segments=[]`，库里 `status=interrupted`（`id=87`）。

根因（`app/meeting.py`，2026-10-19 复核）：
  * `start_meeting_phone_sync` 建会时把"这一场在收段"只记在**进程内**（`_state["active"]`）；
  * 服务端重启一次（真机那场 12:32 建会、夜里起过新进程，`recover_orphaned_meetings()`
    把它标成 interrupted），`_state` 就什么都没了；
  * 手机重试 `start` 命中的是**幂等分支**（只回那一场，不碰内存态）→ `active` 仍为假；
  * `_apply_segment` 只有在"这一段早就收下过"时才兜底，**新段没有落点** → 每一段都 409。

要钉的语义（用户口径）：补传会话 = **已结束、但还在收段** ——
  1. `start(clientId, startedAt, endedAt)` 建/取回那场，并落一个**盘上的**
     "补传未完成"标记（`meta["uploading"]`；进程重启也认）；
  2. 标记期间 `segment` 准写（编号/幂等/落盘仍是同一份实现：`(clientId, seq)` 重传不存第二遍）；
  3. 客户端 `stop` 清标记、**之后**才触发转写（**没传完不许转写**）；
  4. 收尾之后新段一律拒绝（不许写坏已收尾的那场）；本机麦那场仍拒绝外来段；
     两台设备不同 `clientId` 互不影响；
  5. 不带 `endedAt` 的正常"边录边传"路径行为不变（另有既有用例钉着，这里只做一条冒烟）。

测试卫生：临时库 + 临时会议目录 + 转写打桩；**绝不碰**真库/真会议目录。
`settings._cache` 是进程级的，`setUp/cleanUp` 都要丢掉（见 `tests/test_meeting_offline_sync.py`）。
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

#: 真机那个客户端稳定 id（安卓侧 `phone-<yyyyMMddHHmmss>-<4 位>`）。
CID = "phone-20261019101109-aca9"
OTHER_CID = "phone-20261019101109-b7c7"
#: 客户端缓存的开始/结束时间：手机 10:11 开录、10:54 录完，12:32 才联网补传。
T0 = "2026-10-19T10:11:09"
T1 = "2026-10-19T10:54:48"


def _tiny_wav(frames=160):
    """一个最小的合法 WAV（44 字节头 + `frames` 个静音采样）。

    `frames` 可变是刻意的：不同的长度让"这一段**没有被重写**"能被**字节数**证明
    （重传时发一个更长的 body，盘上那份必须一个字都不变）。
    """
    data = b"\x00\x00" * int(frames)
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
           struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
           b"data" + struct.pack("<I", len(data)))
    return hdr + data


class PhoneFinalizeUploadTests(unittest.TestCase):
    """补传"已结束但还在收段"那条路（走**真 API**：真机撞的就是 HTTP 409）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-finalize-mtg-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-finalize-db-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        settings._drop_cache()

        self._orig_dir = meeting_mod.meetings_dir
        meeting_mod.meetings_dir = lambda: self.tmp
        self._reset_process_state()

        # 转写打桩：真跑会去连 ASR/能力后端（几秒到几分钟，还可能写日志/建会话）。
        # 这里只钉"**什么时候**才发起转写"，所以把那条链路的入口换掉。
        self.transcribe_calls = []
        self._transcribed = threading.Event()
        self._orig_transcribe = meeting_mod._transcribe_meeting

        def _fake_transcribe(folder, *a, **kw):
            self.transcribe_calls.append(folder)
            self._transcribed.set()

        meeting_mod._transcribe_meeting = _fake_transcribe
        self.addCleanup(lambda: setattr(meeting_mod, "_transcribe_meeting",
                                        self._orig_transcribe))

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        def _clean():
            self._reset_process_state()
            meeting_mod.meetings_dir = self._orig_dir
            db.DATA_DIR, db.DB_FILE = self._old_db
            # 缓存是按**这个临时库**读满的：交出去之前必须丢掉，否则下一个模块会
            # 拿着这份快照当"本机设置"（`tests/test_events_feed.py` 记着这条教训）。
            settings._drop_cache()
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    # ------------------------------------------------------------ 小工具

    def _reset_process_state(self):
        """把进程内会议状态清成"刚重启"的样子（`_state` 只活在进程里）。"""
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0)

    def _simulate_restart(self):
        """真机那场：12:32 建会后服务端重启过。

        两件事都要做：内存态清零（`_state` 是进程级的）+ 启动恢复
        （`recover_orphaned_meetings()` 会把库里还在 `recording` 的那场标成 interrupted）。
        """
        self._reset_process_state()
        meeting_mod.recover_orphaned_meetings()

    def _start(self, client_id=CID, started_at=T0, ended_at=T1, source="phone"):
        r = self.client.post("/api/meeting/start",
                             params={"source": source, "clientId": client_id,
                                     "startedAt": started_at, "endedAt": ended_at})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _post_segment(self, seq, client_id=CID, wav=None):
        return self.client.post("/api/meeting/segment",
                                params={"clientId": client_id, "seq": seq,
                                        "startedAt": T0, "source": "phone"},
                                content=wav if wav is not None else _tiny_wav(),
                                headers={"Content-Type": "audio/wav"})

    def _segment(self, seq, client_id=CID, wav=None):
        r = self._post_segment(seq, client_id=client_id, wav=wav)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        return body

    def _stop(self, client_id=""):
        params = {"clientId": client_id} if client_id else {}
        r = self.client.post("/api/meeting/stop", params=params)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _meta(self, name):
        with open(os.path.join(self.tmp, name, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _wavs(self, name):
        return sorted(f for f in os.listdir(os.path.join(self.tmp, name))
                      if f.lower().endswith(".wav"))

    def _wait_calls(self, n, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.transcribe_calls) >= n:
                return True
            time.sleep(0.01)
        return False

    # ------------------------------------------------------------ ① 核心回归

    def test_all_segments_are_accepted_after_a_server_restart(self):
        """**今天这个 409 必须消失**：重启后手机重试 start，1..3 段全部收下，stop 收尾。

        这条就是真机的形状：
          `start(clientId, startedAt, endedAt)` →（服务端重启过）→ 手机重试 `start`
          → `segment seq=1..3`（原来每一段都 409）→ `stop` → 段数 3、meta 对得上、
          这才开始转写。
        """
        started = self._start()
        self.assertTrue(started["ok"], started)
        self.assertFalse(started["duplicate"])
        name = started["message"]
        self.assertTrue(self._meta(name).get(meeting_mod.UPLOADING_KEY),
                        "start 带了 endedAt = 客户端已录完但还在上传：必须落'补传未完成'标记")

        # 真机那次就在建会之后重启过：内存态清零、库里那场被标 interrupted
        self._simulate_restart()

        # 手机重试 start：幂等命中同一场（原来这一步之后 active 仍然是假）
        again = self._start()
        self.assertTrue(again["duplicate"], again)
        self.assertEqual(started["meetingId"], again["meetingId"])

        # 核心：连续 1..3 段**全部成功**（原来全部 409）
        for seq in (1, 2, 3):
            body = self._segment(seq)
            self.assertTrue(body["ok"], body)
            self.assertEqual(seq, body["segmentCount"], body)
            self.assertEqual(seq, body["seqCount"], body)
            self.assertEqual(seq, len(body["segments"]), body)
        self.assertFalse(self._transcribed.is_set(),
                         "段还没传完就转写了：那会把半场音频转成'纪要少一半'")
        self.assertEqual(["01.wav", "02.wav", "03.wav"], self._wavs(name))

        # 客户端收尾 → 清标记 → 才发起转写
        stop = self._stop(CID)
        self.assertTrue(stop["ok"], stop)
        self.assertTrue(self._transcribed.wait(5), "stop 之后必须发起转写（否则永远没文字）")

        meta = self._meta(name)
        self.assertEqual(["01.wav", "02.wav", "03.wav"], meta["segments"])
        self.assertNotIn(meeting_mod.UPLOADING_KEY, meta, "收尾了就不许再声称'还在收段'")
        self.assertEqual(T1, meta["end"], "结束时间用客户端录完的那个时刻")
        self.assertTrue(meta.get("stoppedAt"), "服务端收尾时刻另记一份")
        self.assertGreater(meta.get("durationSeconds"), 0)
        row = db.get_meeting(started["meetingId"])
        self.assertEqual("transcribing", row["status"])
        self.assertEqual(T1, row["ended_at"])
        self.assertEqual(3, row["segments"])
        self.assertEqual([os.path.join(self.tmp, name)], self.transcribe_calls)

    def test_the_upload_marker_alone_survives_the_restart(self):
        """手机**没有**重试 start、直接续传：盘上的标记也必须够用（不靠内存态）。

        补传客户端的重试粒度不一定是"先 start 再传段"（它可能记得这场已经开过）。
        只要那场的 `meta.json` 还写着"没传完"，段就必须收得下。
        """
        started = self._start()
        name = started["message"]
        self._simulate_restart()
        body = self._segment(1)
        self.assertEqual(1, body["segmentCount"], body)
        self.assertTrue(self._stop(CID)["ok"])

    def test_a_legacy_upload_without_the_marker_is_still_served(self):
        """修复之前建的补传会议（没有 `uploading` 键）也要能被续传。

        真机那场 `2026-10-19_12-32-01` 就是这样：它带着 `clientEndedAt`、
        却始终没有 `end`（从没收尾过）—— 那就是"录完了、还没传完"。
        不认这一格 = 用户手机里那 5 段**永远传不上来**（只能重录）。
        """
        started = self._start()
        name = started["message"]
        # 模拟"标记引入之前建的那场"：把键删掉，其余（clientEndedAt / clientId）照旧
        meta = self._meta(name)
        meta.pop(meeting_mod.UPLOADING_KEY, None)
        with open(os.path.join(self.tmp, name, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        self._simulate_restart()
        self._segment(1)
        self._segment(2)
        self.assertTrue(self._stop(CID)["ok"])
        self.assertEqual(["01.wav", "02.wav"], self._meta(name)["segments"])

    # ------------------------------------------------------------ ② 幂等

    def test_retransmitting_a_seq_after_the_restart_is_idempotent(self):
        """重传 `seq=2`：成功 + `duplicate: true`，**盘上那份一个字节都不变**。"""
        started = self._start()
        name = started["message"]
        self._simulate_restart()
        self._segment(1, wav=_tiny_wav(320))
        self._segment(2, wav=_tiny_wav(640))
        self._segment(3, wav=_tiny_wav(960))
        size_before = os.path.getsize(os.path.join(self.tmp, name, "02.wav"))

        dup = self._segment(2, wav=_tiny_wav(1600))       # 换一个更长的 body 重传
        self.assertTrue(dup["duplicate"], dup)
        self.assertEqual(3, dup["segmentCount"], "重传不改累计段数")
        self.assertEqual(3, len(self._wavs(name)), "同一个 (clientId, seq) 不许存第二份")
        self.assertEqual(size_before,
                         os.path.getsize(os.path.join(self.tmp, name, "02.wav")),
                         "重传不许重写已经收下的那一段")
        self.assertEqual(["01.wav", "02.wav", "03.wav"], self._meta(name)["segments"])

    # ------------------------------------------------------------ ③ 收尾之后

    def test_a_new_segment_after_stop_is_refused_and_nothing_is_rewritten(self):
        """`stop` 之后：新段 409、老段幂等命中、重试 start **不会**把标记再挂上。"""
        started = self._start()
        name = started["message"]
        self._simulate_restart()
        self._segment(1, wav=_tiny_wav(320))
        self._segment(2, wav=_tiny_wav(640))
        self.assertTrue(self._stop(CID)["ok"])
        self.assertTrue(self._transcribed.wait(5))

        meta_before = self._meta(name)
        files_before = {f: os.path.getsize(os.path.join(self.tmp, name, f))
                        for f in self._wavs(name)}

        # 新段：这场已经收尾 → 按既有语义 409，绝不写进已收尾的那场
        r = self._post_segment(4)
        self.assertEqual(409, r.status_code, r.text)
        self.assertIn("没有在录音", r.json()["detail"])
        self.assertEqual(files_before,
                         {f: os.path.getsize(os.path.join(self.tmp, name, f))
                          for f in self._wavs(name)})
        self.assertEqual(meta_before, self._meta(name))

        # 老段重传：既有的幂等语义（成功 + duplicate），也**不重写**
        dup = self._segment(1, wav=_tiny_wav(1600))
        self.assertTrue(dup["duplicate"], dup)
        self.assertEqual(meta_before, self._meta(name))

        # 手机重走整条流程（stop 的回包丢了）：start 幂等命中，但**不许再挂标记**
        again = self._start()
        self.assertTrue(again["duplicate"], again)
        self.assertFalse(self._meta(name).get(meeting_mod.UPLOADING_KEY),
                         "已经收尾的那场绝不能再被 arm 成'还在收段'")
        r = self._post_segment(4)
        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual(1, len(self.transcribe_calls), "重试不许再触发一次转写")

    # ------------------------------------------------------------ ④ 互不影响

    def test_two_client_ids_keep_their_own_segments_after_a_restart(self):
        """两台设备各自的补传互不影响：段各落各的目录；收尾 A 不许停掉正在补传的 B。"""
        a = self._start(CID)
        self._simulate_restart()
        b = self._start(OTHER_CID)
        self.assertFalse(b["duplicate"], b)
        self.assertNotEqual(a["meetingId"], b["meetingId"])

        self._segment(1, client_id=CID, wav=_tiny_wav(160))
        self._segment(1, client_id=OTHER_CID, wav=_tiny_wav(320))
        self._segment(2, client_id=OTHER_CID, wav=_tiny_wav(480))
        self._segment(2, client_id=CID, wav=_tiny_wav(640))
        self.assertEqual(["01.wav", "02.wav"], self._wavs(a["message"]))
        self.assertEqual(["01.wav", "02.wav"], self._wavs(b["message"]))
        self.assertEqual([1, 2], [int(s.split(".")[0])
                                  for s in self._meta(a["message"])["segments"]])
        self.assertEqual([1, 2], [int(s.split(".")[0])
                                  for s in self._meta(b["message"])["segments"]])

        # 收尾 A：只结束 A 这一场；B 那场（此刻在 `_state` 里是 "active"）一个字节不动
        self.assertTrue(self._stop(CID)["ok"])
        self.assertNotIn(meeting_mod.UPLOADING_KEY, self._meta(a["message"]))
        self.assertTrue(self._meta(b["message"]).get(meeting_mod.UPLOADING_KEY))
        self.assertTrue(meeting_mod._state["active"], "收尾 A 把正在补传的 B 停掉了")
        # B 还能接着传、并且能自己收尾
        self._segment(3, client_id=OTHER_CID)
        self.assertTrue(self._stop(OTHER_CID)["ok"])
        self.assertNotIn(meeting_mod.UPLOADING_KEY, self._meta(b["message"]))
        self.assertTrue(self._wait_calls(2), self.transcribe_calls)

    # ------------------------------------------------------------ ⑤ 既有判据不放宽

    def test_the_local_mic_meeting_still_refuses_foreign_segments(self):
        """本机麦那场 + 外来段 → 仍拒（两路音频混进同一场 = 转写里两个人同时说话）。"""
        from app.audio import recorder as recorder_mod

        class _FakeStream:
            device = "fake"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                import numpy as np
                import time as _t
                _t.sleep(0.02)
                return np.zeros(min(int(n), 1600), dtype="int16"), False

        orig = recorder_mod.input_stream
        recorder_mod.input_stream = lambda *a, **kw: _FakeStream()
        try:
            ok, name = meeting_mod.start_meeting()
            self.assertTrue(ok, name)
            foreign = _tiny_wav()
            r = self._post_segment(1, wav=foreign)
            self.assertEqual(409, r.status_code, r.text)
            self.assertIn("clientId", r.json()["detail"])
            # 落盘判据不能只看"有没有 wav"：本机录音器自己会写 `01.wav`（stop 时把
            # 最后半段刷出去）。所以按**内容**判：目录里不许出现**这一段外来 body**。
            for f in self._wavs(name):
                with open(os.path.join(self.tmp, name, f), "rb") as fh:
                    self.assertNotEqual(foreign, fh.read(),
                                        "外来段被写进了本机麦那场（%s）" % f)
        finally:
            meeting_mod.stop_meeting()
            recorder_mod.input_stream = orig

    def test_the_normal_path_without_ended_at_is_unchanged(self):
        """不带 `endedAt` 的"边录边传"：仍是 active、不落标记、段照收、stop 即转写。"""
        started = self._start(ended_at="")
        name = started["message"]
        self.assertFalse(started["duplicate"])
        self.assertNotIn(meeting_mod.UPLOADING_KEY, self._meta(name),
                         "还没录完的会不该有'补传未完成'标记")
        self.assertTrue(meeting_mod._state["active"], "边录边传仍是'正在录'（既有行为）")
        self.assertEqual(1, self._segment(2)["segmentCount"])
        self.assertEqual(2, self._segment(1)["segmentCount"])
        self.assertTrue(self._stop(CID)["ok"])
        self.assertTrue(self._transcribed.wait(5))
        self.assertEqual(["01.wav", "02.wav"], self._meta(name)["segments"])


if __name__ == "__main__":
    unittest.main()
