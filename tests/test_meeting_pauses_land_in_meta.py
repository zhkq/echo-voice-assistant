# -*- coding: utf-8 -*-
"""真机 bug 回归（2026-10-17）：**`POST /api/meeting/pauses` 200 了，`meta.json` 里却是 `[]`**。

真机证据（`D:\\ECHO\\meeting\\2026-10-17_21-28-08` 与 `21-30-02`，两台手机麦会议）：
  * 服务端日志 21:28:19 / 21:30:02：**「手机补报了 1 条暂停区间（去重后新增 1 条）」**
    —— 这说明 `add_meeting_pauses()` **已经当场把区间写进盘了**；
  * 10 秒后 `stop` 收尾，最终 `meta.json` 的 `"pauses": []` ✗。

根因：`stop_meeting()` 收尾时拿**内存** `_state["pauses"]` 去覆盖 meta，而手机补报的区间
按设计只写进 meta（补报不是"按了一次静音"，不碰 `_state["paused"]`）→ 刚落盘的标注被
收尾这一步静默抹掉。

所以本文件的判据**一律从磁盘重新读 `meta.json`**（不是看接口回包、也不是看内存 `_state`）：
  ① 报一段 → 盘上有（`from`/`to`/`by="手机"`）；
  ② 同一批报两次 → 盘上不重复（幂等）；
  ③ 报完再 `stop` → 盘上仍在（**这条就是 bug 本身**）；
  ④ 端到端 `start → segment → pauses → stop` → 盘上完整；
  ⑤ 面板暂停/恢复与手机补报混在一起时，两边都不丢、同一件事也不重复。

测试卫生（照 `tests/test_meeting_offline_flac.py` 的写法）：临时库 + 临时会议目录 + 每个用例
一个干净的库/会议根目录（`clientId` 是幂等键，共用库会让下一条用例命中上一条的会）；
`settings._cache` 是**进程级**的，用完丢掉；转写打桩（真跑会去连能力后端）。**不碰**真库、
真会议目录、真令牌。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                     # noqa: E402
from fastapi.testclient import TestClient                       # noqa: E402

from app import db                                              # noqa: E402
from app import meeting as meeting_mod                          # noqa: E402
from app.api import router                                      # noqa: E402
from app.config import settings                                 # noqa: E402

#: 客户端稳定 id（形状与安卓侧 `phone-<yyyyMMddHHmmss>-<4 位>` 一致）。
CID = "phone-20261017212734-48cc"
#: 客户端缓存的开始时间：同一场里的相对秒都要加到它上面。
T0 = "2026-10-16T10:00:00"
#: 手机上报的两段（**相对这一场开始的秒数** —— 安卓契约就是这种写法）。
P1 = {"from": 12.5, "to": 31.0}
P2 = {"from": 120.0, "to": 300.0}


def tiny_wav_bytes(seconds=0.3, rate=16000):
    """一小段**真 WAV**（全零即可：这里只验 meta，不验音频内容）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _PauseLandingCase(unittest.TestCase):
    """临时库 + 临时会议目录（每例一套），走**真 API** 那一半。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-pauses-land-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        cls._base_root = os.path.join(cls.tmp, "meetings")
        os.makedirs(cls._base_root, exist_ok=True)
        cls._db_root = os.path.join(cls.tmp, "dbs")
        os.makedirs(cls._db_root, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._drop_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # 每个用例一层干净的会议根目录 + 一个干净的库：会议目录名是秒级时间戳、`clientId`
        # 是幂等键，共用时前一条用例的会会被后一条的 `start` 当"幂等命中"回掉（假红）。
        self.meetings_root = os.path.join(self._base_root, self._testMethodName)
        os.makedirs(self.meetings_root, exist_ok=True)
        p = patch.object(meeting_mod, "meetings_dir", lambda: self.meetings_root)
        p.start()
        self.addCleanup(p.stop)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self._db_root, "%s.db" % self._testMethodName)
        db.init()
        settings._drop_cache()
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0, _pause_events=[])
        self.addCleanup(self._reset_state)
        # 收尾会起后台转写：打桩，别去连能力后端（几秒到几分钟）。
        self.transcribe_calls = []
        self._orig_transcribe = meeting_mod._transcribe_meeting

        def _fake_transcribe(folder, *a, **kw):
            self.transcribe_calls.append(folder)

        meeting_mod._transcribe_meeting = _fake_transcribe
        self.addCleanup(lambda: setattr(meeting_mod, "_transcribe_meeting",
                                        self._orig_transcribe))
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def _reset_state(self):
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0)
        settings._drop_cache()

    # ------------------------------------------------------------ HTTP 小工具

    def _start(self, client_id=CID, started_at=T0):
        r = self.client.post("/api/meeting/start",
                             params={"source": "phone", "clientId": client_id,
                                     "startedAt": started_at})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        # start 成没成是后面所有断言的**前置判据**：不成时红的原因会与真相无关。
        self.assertTrue(body.get("ok"), "start 没成，后面的断言都会失真：%r" % body)
        self.assertEqual(client_id, body.get("clientId"), body)
        return body

    def _segment(self, seq=1):
        r = self.client.post("/api/meeting/segment",
                             params={"clientId": CID, "seq": seq, "startedAt": T0},
                             content=tiny_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        return body

    def _post_pauses(self, pauses, client_id=CID):
        r = self.client.post("/api/meeting/pauses",
                             json={"clientId": client_id, "pauses": pauses})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _pause_via_api(self):
        r = self.client.post("/api/meeting/pause")
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _resume_via_api(self):
        r = self.client.post("/api/meeting/resume")
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _stop(self, client_id=CID):
        r = self.client.post("/api/meeting/stop", params={"clientId": client_id})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    # ------------------------------------------------------------ 唯一判据：磁盘

    def _meta_on_disk(self, name):
        """**重新从磁盘读**那一场的 meta.json（本文件的判据只认这个，不认内存/回包）。"""
        path = os.path.join(self.meetings_root, name, "meta.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _pauses_on_disk(self, name):
        return self._meta_on_disk(name).get("pauses") or []

    # ------------------------------------------------------------ 用例

    def test_reported_pause_is_on_disk_right_after_report(self):
        """判据①：报一段 → **盘上**有该区间（`from`/`to`/`by="手机"`）。"""
        started = self._start()
        name = started["message"]
        body = self._post_pauses([P1])
        self.assertTrue(body["ok"], body)
        self.assertEqual(1, body["added"], body)

        pauses = self._pauses_on_disk(name)
        self.assertEqual(1, len(pauses), "报完必须能在磁盘 meta.json 里读到：%r" % (pauses,))
        self.assertEqual("手机", pauses[0]["by"], "谁报的必须看得见（by=手机）")
        # 相对秒要加到这一场的开始时间（T0）上，存成绝对本地 ISO（与面板那条路同语义）。
        self.assertEqual("2026-10-16T10:00:12", pauses[0]["from"])
        self.assertEqual("2026-10-16T10:00:31", pauses[0]["to"])

    def test_reporting_the_same_batch_twice_does_not_duplicate_on_disk(self):
        """判据②：同一批报两次 → 盘上不重复（客户端断网重试就该是幂等的）。"""
        started = self._start()
        name = started["message"]
        self.assertTrue(self._post_pauses([P1, P2])["ok"])
        again = self._post_pauses([P1, P2])
        self.assertTrue(again["ok"], again)
        self.assertEqual(0, again["added"], "第二次不该再新增")
        pauses = self._pauses_on_disk(name)
        self.assertEqual(2, len(pauses), "盘上不许变成 4 条：%r" % (pauses,))

    def test_stop_does_not_overwrite_reported_pauses_on_disk(self):
        """判据③（**这条就是真机 bug**）：报完 pauses 再 stop → 盘上的 pauses 仍在。

        旧实现里 `stop_meeting()` 用内存 `_state["pauses"]`（补报不写它 → 空表）覆盖 meta，
        于是接口 200、日志"补报了 1 条"，收尾后盘上却是 `[]`。
        """
        started = self._start()
        name = started["message"]
        # ⚠️ 先收一段音频：没有段时 `stop_meeting()` 会如实回"没有录到音频"（既有行为），
        # 那与本 bug 无关 —— 本用例要的是**正常收尾**那条路。
        self._segment(1)
        self.assertTrue(self._post_pauses([P1, P2])["ok"])
        self.assertEqual(2, len(self._pauses_on_disk(name)), "收尾前先确认已经落盘")

        stopped = self._stop()
        self.assertTrue(stopped["ok"], stopped)
        self.assertFalse(meeting_mod.meeting_status()["active"])

        pauses = self._pauses_on_disk(name)
        self.assertEqual(2, len(pauses),
                         "stop 收尾把刚补报的区间抹掉了（真机 bug 复现）：%r" % (pauses,))
        self.assertEqual(["手机", "手机"], [p["by"] for p in pauses])
        # 收尾只许**往上加**（end/segments/duration），不许动已经收下的标注。
        self.assertTrue(self._meta_on_disk(name).get("end"))

    def test_end_to_end_start_segment_pauses_stop_keeps_the_intervals(self):
        """判据④：`start → segment → pauses → stop` 之后，盘上 pauses 仍完整。"""
        started = self._start()
        name = started["message"]
        self._segment(1)
        self.assertTrue(self._post_pauses([P1, P2])["ok"])
        self.assertEqual(2, len(self._pauses_on_disk(name)))

        stopped = self._stop()
        self.assertTrue(stopped["ok"], stopped)

        meta = self._meta_on_disk(name)
        pauses = meta.get("pauses") or []
        self.assertEqual(2, len(pauses), "端到端收尾后盘上 pauses 该完整：%r" % (pauses,))
        self.assertEqual("2026-10-16T10:00:12", pauses[0]["from"])
        self.assertEqual("2026-10-16T10:00:31", pauses[0]["to"])
        self.assertEqual("2026-10-16T10:02:00", pauses[1]["from"])
        self.assertEqual("2026-10-16T10:05:00", pauses[1]["to"])
        self.assertEqual(["01.wav"], meta.get("segments"))
        self.assertTrue(meta.get("end"), "stop 该写下结束时间")

    def test_panel_pause_and_reported_pause_both_survive_stop(self):
        """判据⑤：面板暂停/恢复（走 `_state["pauses"]`）与手机补报（只写 meta）混在一起，
        收尾后**两边都在、同一件事不重复** —— 修复是"合并"而不是"二选一"。"""
        started = self._start()
        name = started["message"]
        self._segment(1)                 # 没段时 stop 会回"没有录到音频"（既有行为，与本题无关）
        self.assertTrue(self._pause_via_api()["ok"])
        self.assertTrue(self._resume_via_api()["ok"])
        self.assertTrue(self._post_pauses([P1])["ok"])
        self.assertEqual(2, len(self._pauses_on_disk(name)),
                         "面板那条 + 补报那条，收尾前盘上该是 2 条")

        self.assertTrue(self._stop()["ok"])
        pauses = self._pauses_on_disk(name)
        self.assertEqual(2, len(pauses), "收尾后两条都该在：%r" % (pauses,))
        by = sorted(str(p.get("by") or "") for p in pauses)
        self.assertEqual(["手机", "面板"], by, "两种来源都要留（谁按的要分得出）")
        for p in pauses:
            self.assertTrue(p.get("from") and p.get("to"),
                            "收尾后每条区间都该是完整的：%r" % (p,))


if __name__ == "__main__":
    unittest.main()
