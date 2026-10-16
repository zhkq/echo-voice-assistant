# -*- coding: utf-8 -*-
"""手机离线补传的**两个契约扩展**（2026-10-16 用户拍板）：

  ① **段也接受 FLAC**（无损压缩、体积约减半）：
     `POST /api/meeting/segment` 的 body 除了 `RIFF` 还认 **`fLaC` 魔数**（**不看
     Content-Type** —— 判据是"字节本身是什么"，见 `meeting._is_flac` 的说明），
     落盘成 `NN.flac`，与 WAV 段**同一套编号与幂等规则**（重复 `(clientId, seq)` 只存一份），
     WAV 那条路**逐字节不变**。
     **最重要的一条判据**：同一段音频，**WAV 路径与 FLAC 路径转出的文字必须逐字相同**。
     为什么必须相同（用户的核心要求）：FLAC 是**无损**压缩 —— 归档换格式后转写文本要是
     变了（Opus 那种 29% 差异就是这么来的），"省一半磁盘"就变成了"换了内容"，用户否掉
     的正是那个交换。所以这条用例既断言**文字逐字相同**，也断言**喂给引擎的音频字节
     完全相同**（前者是用户看得见的结果，后者才是"为什么必然相同"的证据）。

  ② 手机**补报暂停区间**：`POST /api/meeting/pauses`（JSON）把手机离线期间的暂停区间
     合并进那场会议的 `meta["pauses"]`，每条带 `by="手机"`；**幂等**（重复上报不产生
     重复条目）；`clientId` 与当前那场不符 → **拒绝且零改动**；没在录 → `ok=false` + 原因
     （**绝不顺手建会**）；**它不许开始/停止/改变任何录音状态**。
     ⚠️ 现行暂停语义（用户 2026-10-16 深夜拍板）是"**照常录、但写进去的是全零音频**，
     不影响总时长、不用剃掉"（见 `meeting.pause_meeting`）—— 所以 `meta["pauses"]` 是
     **纯标注**。补报接口因此更不能去碰 `_state["paused"]`/recorder：那等于后台替用户
     多按/松开一次静音，直接改变那段音频是不是全零。

测试卫生（照 `tests/test_meeting_offline_sync.py` 的写法）：临时库 + 临时会议目录 +
临时凭据（`apiAuthEnabled=True` + 一把临时令牌 + 中继标记）；临时解码目录也隔离
（默认那个在系统 %TEMP%，不许往那儿拉屎、更不许 `gc_temp()` 掉别人的临时文件）；
`settings._cache` 是**进程级**的，用完必须丢掉。**不碰**真库 / 真会议目录 / 真令牌。
⚠️ 本文件还比那份多两条隔离，都是实测出来的（共用时会得到与本功能无关的假红）：
**每个用例一个干净的库 + 一层干净的会议目录** —— `clientId` 是幂等键，共用库时前一条
用例建的那场会被后一条的 `start` 当"幂等命中"回掉（而且不设 `_state`）。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                              # noqa: E402
import soundfile as sf                                          # noqa: E402
from fastapi import FastAPI                                     # noqa: E402
from fastapi.testclient import TestClient                       # noqa: E402

import app.api as api_mod                                       # noqa: E402
from app import db                                              # noqa: E402
from app import meeting as meeting_mod                          # noqa: E402
from app import relay as relay_mod                              # noqa: E402
from app.api import router                                      # noqa: E402
from app.audio import audiofile as af                           # noqa: E402
from app.capabilities import (                                  # noqa: E402
    BACKEND_ECHO_SERVER,
    SOURCE_LAN,
    AsrResult,
    CapabilityClient,
    CapabilityRouter,
    DiarizeResult,
    Provenance,
)
from app.capabilities import credentials as cred_mod            # noqa: E402
from app.config import settings                                 # noqa: E402

#: 客户端稳定 id（形状与安卓侧 `phone-<yyyyMMddHHmmss>-<4 位>` 一致）。
CID = "phone-20261016203102-1a2b"
OTHER_CID = "phone-20261016203102-9f9f"
#: 客户端缓存的开始时间（这一场 10:00 开录）。
T0 = "2026-10-16T10:00:00"


# ==================================================================== 造料

def tone_wav_bytes(seconds=0.5, rate=16000, freq=440.0, amp=0.3):
    """一段**真的**正弦波 WAV（不是全零）：往返比对才有意义。"""
    n = int(rate * seconds)
    t = np.arange(n, dtype="float64") / float(rate)
    samples = (amp * np.sin(2 * np.pi * freq * t) * 32767.0).astype("<i2")
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def wav_to_flac_bytes(wav_bytes):
    """`RIFF` 字节 → `fLaC` 字节（**同采样率/位深**，这就是手机那条"无损压缩"搬到这里）。

    刻意用 `soundfile`（= `audiofile.decode_to_wav` 用的同一个 libsndfile）而不是手搓
    FLAC：用例要验的是**服务端那条路**，不是"我能不能造一个 flac"。
    """
    import io
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels)
    buf = io.BytesIO()
    sf.write(buf, samples, rate, subtype="PCM_16", format="FLAC")
    return buf.getvalue()


def flac_seconds(flac_bytes):
    """FLAC 的时长（秒）—— 用来断言服务端按**真音频**算了时长，而不是当空文件。"""
    import io
    info = sf.info(io.BytesIO(flac_bytes))
    return float(info.frames) / float(info.samplerate)


class _Case(unittest.TestCase):
    """临时库 + 临时会议目录 + 临时解码目录 —— 绝不碰用户的真数据。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-offline-flac-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "offline-flac.db")
        db.init()
        # ⚠️ 刻意**不**调 `settings.seed_defaults()`（要写上百行设置、本机实测 2 秒/次）：
        # 这些用例只读几个有内联默认值的键（`meetingSegmentMinutes` 之类）。
        settings._drop_cache()
        cls._base_root = os.path.join(cls.tmp, "meetings")
        os.makedirs(cls._base_root, exist_ok=True)
        cls._db_root = os.path.join(cls.tmp, "dbs")
        os.makedirs(cls._db_root, exist_ok=True)
        cls.decode_dir = os.path.join(cls.tmp, "decode")
        os.makedirs(cls.decode_dir, exist_ok=True)
        # 凭据文件隔离：开发机真配对过 ECHO 后端，不隔离的话"走哪条转写路"会飘。
        cls._cred = os.path.join(cls.tmp, "backend.json")

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._drop_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        # **每个用例一层干净的会议根目录 + 一个干净的库**（都在 `setUpClass` 的临时目录下）。
        # 为什么必须这样（本题实测出来的两条坑）：
        #   ① 会议目录名 = 时间戳（同一秒里两场会同名），而"段只该有一份"这类断言看的是
        #      **目录里的文件** —— 共用一个根目录时，前一条用例留下的段会被后一条读到；
        #   ② `clientId` 是**幂等键**：共用库时，前一条用例建的那场会被后一条的 `start`
        #      当成"幂等命中"直接回同一场（**且不设 `_state`**），于是后一条的传段会得到
        #      "现在没有在录音" —— 一个与被测行为完全无关的原因（假红）。
        self.meetings_root = os.path.join(self._base_root, self._testMethodName)
        os.makedirs(self.meetings_root, exist_ok=True)
        # ⚠️ 会议目录要在 `db.init()` **之前**指到本用例这一层：`db.init()` 会去建音频目录。
        p = patch.object(meeting_mod, "meetings_dir", lambda: self.meetings_root)
        p.start()
        self.addCleanup(p.stop)
        db.DB_FILE = os.path.join(self._db_root, "%s.db" % self._testMethodName)
        db.init()
        settings._drop_cache()
        self._patches = [
            patch.object(af, "TEMP_DECODE_DIR", self.decode_dir),
            patch.object(cred_mod, "credentials_path", lambda: self._cred),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0)
        self.addCleanup(self._reset_state)
        # ⚠️ 临时凭据：把鉴权**打开**（否则 `optional_auth()` 全放行，用例分不出带没带凭据）。
        # 要在每个用例里重置，因为 `add_api_key_row` 是往本用例的临时库里写的。
        db.set_setting("apiAuthEnabled", True)
        self.token = db.add_api_key_row("offline-flac-test", scopes=["device"])["token"]
        settings._drop_cache()
        # 转写打桩：真跑转写会去连 ASR/能力后端（几秒到几分钟）。
        self.transcribe_calls = []
        self._transcribe_done = threading.Event()
        self._orig_transcribe = meeting_mod._transcribe_meeting

        def _fake_transcribe(folder, *a, **kw):
            self.transcribe_calls.append(folder)
            self._transcribe_done.set()

        meeting_mod._transcribe_meeting = _fake_transcribe
        self.addCleanup(lambda: setattr(meeting_mod, "_transcribe_meeting",
                                        self._orig_transcribe))

    def _reset_state(self):
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  paused=False, pauses=[], source="pc", seg_count=0,
                                  client_id="", meeting_id=0)
        settings._drop_cache()

    # ------------------------------------------------------------ 小工具

    def _headers(self):
        """手机那条路的请求头：**令牌 + 中继标记**（中继是手机唯一能进来的路）。"""
        return {"Authorization": "Bearer %s" % self.token,
                relay_mod.RELAY_MARKER_HEADER: "1"}

    def _folder(self, name):
        return os.path.join(self.meetings_root, name)

    def _meta(self, name):
        with open(os.path.join(self._folder(name), "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _files(self, name, ext):
        return sorted(f for f in os.listdir(self._folder(name))
                      if f.lower().endswith(ext))

    def _meeting_names(self):
        try:
            return sorted(os.listdir(self.meetings_root))
        except OSError:
            return []


class _HttpCase(_Case):
    """走**真 API** 那一半（契约说的就是 HTTP 面）。"""

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def _start(self, client_id=CID, started_at=T0):
        r = self.client.post("/api/meeting/start",
                             params={"source": "phone", "clientId": client_id,
                                     "startedAt": started_at},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        # 启动这一步自己是"成没成"的判据：不成时后面每条断言都会得到一个与真相无关的
        # 原因（那就是"红的地方不是坏的地方"）。在这里钉住，失败原因一眼可见。
        self.assertTrue(body.get("ok"), "start 没成，后面的断言都会失真：%r" % body)
        self.assertEqual(client_id, body.get("clientId"), body)
        return body

    def _post_segment(self, seq, wav=None, client_id=CID, content_type="audio/wav"):
        r = self.client.post("/api/meeting/segment",
                             params={"clientId": client_id, "seq": seq, "startedAt": T0},
                             content=wav if wav is not None else tone_wav_bytes(),
                             headers={**self._headers(), "Content-Type": content_type})
        return r

    def _segment(self, seq, wav=None, content_type="audio/wav"):
        """传一段并断言被收下（段名读 `name`，见 `meeting._seg_result` 的说明）。"""
        r = self._post_segment(seq, wav=wav, content_type=content_type)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"), body)
        self.assertTrue(body.get("name"), "收下的段必须回文件名：%r" % body)
        self.assertEqual(body["name"], body.get("message"),
                         "段这条路上 message 与 name 是同一个段名（谁读哪一格都不会空）")
        return body

    def _status(self):
        r = self.client.get("/api/meeting/status", headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _stop(self, client_id=CID):
        r = self.client.post("/api/meeting/stop", params={"clientId": client_id},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _post_pauses(self, pauses, client_id=CID):
        r = self.client.post("/api/meeting/pauses",
                             json={"clientId": client_id, "pauses": pauses},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        return r.json()


# ==================================================================== ① FLAC 段

class FlacSegmentTests(_HttpCase):
    """`/meeting/segment` 的 FLAC 那一半（WAV 那条路不动）。"""

    def setUp(self):
        super().setUp()
        self.wav = tone_wav_bytes()
        self.flac = wav_to_flac_bytes(self.wav)

    def test_a_flac_body_is_accepted_and_lands_as_nn_flac(self):
        """`fLaC` body → 收下、落成 `01.flac`、段号/累计数/时长都对。"""
        started = self._start()
        name = started["message"]                      # 会议目录名
        body = self._segment(1, wav=self.flac, content_type="audio/flac")
        self.assertEqual("01.flac", body["name"], "回包里的段名必须是盘上那个 .flac")
        self.assertEqual(["01.flac"], self._files(name, ".flac"))
        self.assertEqual(["01.flac"], self._meta(name)["segments"])
        self.assertEqual(1, body["segmentIndex"])
        self.assertEqual(1, body["segmentCount"])
        self.assertEqual([1], body["segments"])
        self.assertFalse(body["duplicate"], "第一次不可能是'重复'")

    def test_the_flac_magic_is_what_counts_not_the_content_type(self):
        """**按魔数认、不看 Content-Type**：头里写 wav 的 flac 照样落成 `.flac`。"""
        started = self._start()
        self._segment(1, wav=self.flac, content_type="audio/wav")
        self.assertEqual(["01.flac"], self._meta(started["message"])["segments"],
                         "body 是 fLaC，就不该因为头里写着 audio/wav 被存成 .wav")

    def test_a_wav_body_still_lands_as_nn_wav(self):
        """回归：WAV 那条路**一个字没变**（后缀/段号/计数全同）。"""
        started = self._start()
        body = self._segment(1, wav=self.wav, content_type="audio/wav")
        self.assertEqual("01.wav", body["name"])
        self.assertEqual(["01.wav"], self._meta(started["message"])["segments"])
        self.assertEqual(1, body["segmentIndex"])

    def test_flac_segments_keep_the_same_numbering_and_dedupe(self):
        """编号与幂等规则和 WAV **同一套**：`seq` 定段号、重复 `(clientId, seq)` 只存一份。"""
        started = self._start()
        name = started["message"]
        self._segment(1, wav=self.flac, content_type="audio/flac")
        self._segment(2, wav=wav_to_flac_bytes(tone_wav_bytes(freq=660.0)),
                      content_type="audio/flac")
        self.assertEqual(["01.flac", "02.flac"], self._files(name, ".flac"))
        self.assertEqual(["01.flac", "02.flac"], self._meta(name)["segments"])
        again = self._segment(1, wav=self.flac, content_type="audio/flac")
        self.assertTrue(again["duplicate"], "同一 (clientId, seq) 再来一次 = 幂等命中")
        self.assertEqual("01.flac", again["name"],
                         "幂等命中要回**盘上那个名字**（客户端拿它对账）")
        self.assertEqual(["01.flac", "02.flac"], self._files(name, ".flac"),
                         "内存/磁盘都只该有一份 01")
        self.assertEqual(2, again["segmentCount"])

    def test_the_duration_is_computed_from_the_flac_itself(self):
        """收尾时按**真音频**算时长（FLAC 也要算得出来，不能退化成 0）。"""
        started = self._start()
        name = started["message"]
        self._segment(1, wav=self.flac, content_type="audio/flac")
        self.assertTrue(self._stop()["ok"])
        self.assertTrue(self._transcribe_done.wait(5.0), "stop 之后必须发起转写")
        meta = self._meta(name)
        self.assertAlmostEqual(flac_seconds(self.flac), meta["durationSeconds"], places=2)
        self.assertEqual(["01.flac"], meta["segments"])
        self.assertEqual(["01.flac"], self._files(name, ".flac"))

    def test_a_garbage_body_is_still_refused(self):
        """垃圾字节仍然必须被拒（**不是音频**：wav/flac 都不是）。"""
        started = self._start()
        name = started["message"]
        r = self._post_segment(1, wav=b"ID3\x04\x00\x00 not audio at all")
        self.assertEqual(409, r.status_code, r.text)
        self.assertIn("音频", r.json()["detail"])
        self.assertEqual([], self._files(name, ".wav") + self._files(name, ".flac"),
                         "拒绝了就不许落盘")

    def test_a_flac_shaped_body_too_short_is_refused(self):
        """只有魔数、连音频数据都没有的 body：同样拒（与 WAV 那条 44 字节线同一条口径）。"""
        self._start()
        r = self._post_segment(1, wav=b"fLaC\x00\x00\x00\x22")
        self.assertEqual(409, r.status_code, r.text)
        self.assertIn("音频", r.json()["detail"])

    def test_a_retried_flac_segment_of_a_finished_meeting_is_idempotent(self):
        """收尾过的那场：重传同一个 FLAC 段 → 幂等命中（不写第二遍、也不许改写那一场）。"""
        started = self._start()
        name = started["message"]
        self._segment(1, wav=self.flac, content_type="audio/flac")
        self.assertTrue(self._stop()["ok"])
        before = self._meta(name)
        again = self._segment(1, wav=self.flac, content_type="audio/flac")
        self.assertTrue(again["duplicate"], again)
        self.assertEqual("01.flac", again["name"])
        self.assertEqual(before["segments"], self._meta(name)["segments"])
        self.assertEqual(["01.flac"], self._files(name, ".flac"))
        # 这一场**没收到过**的段号：已经收尾了，不许再往里写音频
        missing = self._post_segment(9, wav=self.flac, content_type="audio/flac")
        self.assertEqual(409, missing.status_code, missing.text)
        self.assertIn("没有在录音", missing.json()["detail"])

    def test_the_same_seq_reported_in_the_other_format_is_the_same_segment(self):
        """同一段先按 WAV 传、重试时改按 FLAC 传（客户端升级/换编码）→ **仍是同一段**。

        为什么这条是承重墙：`(clientId, seq)` 才是幂等的判据，**格式不是**。要是拿
        "这次 body 是什么格式"去拼回包里的段名，第二个格式就会看起来像"服务端存的不是我
        这一份"，客户端于是永远对不上账（而盘上其实只有一份）。
        """
        started = self._start()
        name = started["message"]
        first = self._segment(1, wav=self.wav, content_type="audio/wav")
        self.assertEqual("01.wav", first["name"])
        again = self._segment(1, wav=self.flac, content_type="audio/flac")
        self.assertTrue(again["duplicate"], again)
        self.assertEqual("01.wav", again["name"],
                         "回包必须是**盘上那个**名字（先到的是 wav）")
        self.assertEqual(["01.wav"], self._files(name, ".wav"))
        self.assertEqual([], self._files(name, ".flac"), "不许因为换个格式就多存一份")
        self.assertEqual(1, again["segmentCount"])


# ==================================================================== ② WAV ≡ FLAC

class _ByteSpyBackend(CapabilityClient):
    """能力后端替身：**在它拿到路径的那一刻**把音频字节读出来，并回一句固定的话。

    为什么在这里量：`_transcribe_impl` 把段（wav 原样 / flac 先解成临时 WAV）交给后端，
    后端读到的字节就是"引擎听到的音频"。只有它逐字节相同，转写文本才**必然**逐字相同
    （用户要的那条判据）。
    """

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"asr.text", "diarize.turns"})
    vector_space_id = "ws-offline-flac-spy"

    def __init__(self, text="第一句。第二句。", sentences=None):
        self.text = text
        self._sentences = list(sentences or [(0.0, 1.0, "第一句。"), (1.0, 2.0, "第二句。")])
        self.paths = []
        self.blobs = []                 # 与 paths 一一对应的**音频字节**

    def transcribe(self, wav, *, lang="auto", want_timestamps=False, **kw):
        self.paths.append(wav)
        with open(wav, "rb") as fh:
            self.blobs.append(fh.read())
        return AsrResult(text=self.text, sentences=tuple(self._sentences),
                         timestamps="exact",
                         provenance=Provenance(self.backend_id, "offline-flac-spy"),
                         audio_seconds=2.0)

    def diarize(self, wav, *, max_speakers=None, **kw):
        return DiarizeResult(turns=((0.0, 2.0, "SPEAKER_00"),),
                             speakers={"SPEAKER_00": tuple([0.1] * 256)},
                             dim=256, vector_space_id=self.vector_space_id,
                             provenance=Provenance(self.backend_id, "offline-flac-spy-dia"),
                             audio_seconds=2.0)


class WavAndFlacTranscribeTheSameTextTests(_Case):
    """**用户的核心判据**：同一段音频，WAV 路径与 FLAC 路径转出的文字逐字相同。"""

    def _make_meeting(self, seg_name, blob):
        """造一场会：目录里就一段（`01.wav` 或 `01.flac`）+ `meta.json`，并建库里的行。"""
        folder = tempfile.mkdtemp(prefix="m-", dir=self.meetings_root)
        with open(os.path.join(folder, seg_name), "wb") as fh:
            fh.write(blob)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"start": "2026-10-16T10:00:00", "segments": [seg_name],
                       "durationSeconds": 2.0, "config": {}}, fh, ensure_ascii=False)
        mid = db.create_meeting(os.path.basename(folder), started_at="2026-10-16T10:00:00")
        self.addCleanup(db.delete_meeting, mid)
        return folder, mid

    def _transcribe(self, folder):
        """真跑 `_transcribe_impl`，只把**引擎**换成读字节的替身（其余全是真的）。"""
        backend = _ByteSpyBackend()
        settings.update({"capabilityEchoServerUrl": "http://spy-backend:8900"})
        settings._drop_cache()
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        p = patch("app.capabilities.build_default_router", lambda **kw: router)
        p.start()
        self.addCleanup(p.stop)
        # 本机那份分离**一次都不该被调到**（会议分离只走能力后端）
        p = patch("app.audio.diarize.diarize_wav_full",
                  MagicMock(side_effect=AssertionError("分离不该回落本机")))
        p.start()
        self.addCleanup(p.stop)
        with patch.object(meeting_mod, "_active_asr_provider", lambda: None):
            meeting_mod._transcribe_impl(folder)
        return backend

    def test_both_paths_produce_the_same_text(self):
        raw = tone_wav_bytes(seconds=0.5)
        flac = wav_to_flac_bytes(raw)
        self.assertEqual(raw[:4], b"RIFF")
        self.assertEqual(flac[:4], b"fLaC")

        wav_folder, wav_mid = self._make_meeting("01.wav", raw)
        flac_folder, flac_mid = self._make_meeting("01.flac", flac)

        wav_backend = self._transcribe(wav_folder)
        flac_backend = self._transcribe(flac_folder)

        wav_lines = [ln["text"] for ln in db.get_lines(wav_mid)]
        flac_lines = [ln["text"] for ln in db.get_lines(flac_mid)]
        # ① 用户要的那条：**文字逐字相同**
        self.assertTrue(wav_lines, "wav 那条路必须真的转出文字（否则下面的比较是空对空）")
        self.assertEqual(wav_lines, flac_lines,
                         "同一段音频：wav 与 flac 转出的文字必须逐字相同")
        # ② 为什么必然相同：**喂给引擎的音频字节完全相同**
        self.assertEqual(1, len(wav_backend.blobs))
        self.assertEqual(1, len(flac_backend.blobs))
        self.assertEqual(wav_backend.blobs[0], flac_backend.blobs[0],
                         "flac 必须先被解成与 wav 逐字节相同的 PCM，才谈得上'文字相同'")
        # ③ FLAC 段是**用时解码**：喂进去的是临时 WAV，而且用完就删
        self.assertTrue(flac_backend.paths[0].lower().endswith(".wav"))
        self.assertNotEqual(os.path.normcase(flac_backend.paths[0]),
                            os.path.normcase(os.path.join(flac_folder, "01.flac")))
        for path in flac_backend.paths + wav_backend.paths:
            if path.lower().endswith(".wav") and os.path.dirname(path) == self.decode_dir:
                self.assertFalse(os.path.exists(path), "临时解码文件用完必须删除")
        # ④ 时间轴/说话人也不因换格式而变
        self.assertEqual([ln["speaker_label"] for ln in db.get_lines(wav_mid)],
                         [ln["speaker_label"] for ln in db.get_lines(flac_mid)])
        self.assertAlmostEqual(meeting_mod._seg_duration_map(wav_folder)[1],
                               meeting_mod._seg_duration_map(flac_folder)[1], places=2)


# ==================================================================== ③ pauses

class PhonePausesTests(_HttpCase):
    """`/meeting/pauses`：合并 + 幂等 + 护栏 + 没在录时如实失败。"""

    def setUp(self):
        super().setUp()
        # ❶ **安卓契约那一档**：相对这一场开始时间的秒数（`dto/MeetingOfflineDtos.kt`）。
        self.p1 = {"from": 12.5, "to": 31.0}
        # ❷ 绝对时间的另一种写法（ISO 字符串）。
        self.p2 = {"from": "2026-10-16T10:20:00", "to": "2026-10-16T10:25:00"}

    def test_reported_pauses_are_merged_with_by_phone(self):
        started = self._start()
        name = started["message"]
        body = self._post_pauses([self.p1, self.p2])
        self.assertTrue(body["ok"], body)
        meta = self._meta(name)
        self.assertEqual(2, len(meta["pauses"]))
        for entry in meta["pauses"]:
            self.assertEqual("手机", entry["by"], "补报的区间必须带 by=手机（谁报的要看得见）")
        # 相对秒要**加到这一场的开始时间（T0 = 10:00:00）上**，存成与面板同一个语义
        # （绝对本地 ISO）—— 混两种语义读的人就得先猜是哪一种。
        self.assertEqual("2026-10-16T10:00:12", meta["pauses"][0]["from"],
                         "相对秒必须换算成绝对时刻（安卓契约就是相对秒）")
        self.assertEqual("2026-10-16T10:00:31", meta["pauses"][0]["to"])
        self.assertEqual(self.p2["from"], meta["pauses"][1]["from"], "ISO 原样收下")
        self.assertEqual(self.p2["to"], meta["pauses"][1]["to"])
        self.assertEqual(2, body["added"])
        self.assertEqual(2, len(body["pauses"]), "响应要回**合并之后**的整份列表")

    def test_a_large_number_is_read_as_epoch_seconds_not_relative(self):
        """大数字按 **epoch 秒**读（与相对秒分开，见 `PAUSE_RELATIVE_MAX_SECONDS`）。

        为什么要这条：两种数字共用 `from`/`to` 两个字段（安卓发相对秒、别处可能发 epoch），
        靠**量级**分开。分错的后果是静默的：一条 2026 年的暂停区间会变成 1970 年那一条。
        """
        started = self._start()
        name = started["message"]
        epoch = 1770000000
        body = self._post_pauses([{"from": epoch, "to": epoch + 60}])
        self.assertTrue(body["ok"], body)
        self.assertEqual(datetime.datetime.fromtimestamp(epoch).isoformat(timespec="seconds"),
                         self._meta(name)["pauses"][0]["from"])
        # 一个 30 天以内的数字则按**相对秒**算（不然它会被当成 1970 年）
        small = self._post_pauses([{"from": 3600.0, "to": 3660.0}])
        self.assertEqual(1, small["added"])
        self.assertEqual("2026-10-16T11:00:00", self._meta(name)["pauses"][1]["from"])

    def test_reporting_the_same_batch_again_does_not_duplicate(self):
        """幂等：同一批区间重复上报（客户端断网重试）不产生重复条目。"""
        started = self._start()
        name = started["message"]
        first = self._post_pauses([self.p1, self.p2])
        self.assertEqual(2, first["added"])
        again = self._post_pauses([self.p1, self.p2])
        self.assertTrue(again["ok"], again)
        self.assertEqual(0, again["added"], "第二次不该再新增")
        self.assertEqual(2, len(self._meta(name)["pauses"]), "meta 里也不该变成 4 条")
        # 同一时刻的**另一种写法**（带时区/带毫秒）不算新条目：归一之后是同一件事
        third = self._post_pauses([
            {"from": "2027-01-01T00:00:00+08:00", "to": "2027-01-01T00:01:00+08:00"}])
        self.assertEqual(1, third["added"])
        fourth = self._post_pauses([
            {"from": "2026-12-31T16:00:00Z", "to": "2026-12-31T16:01:00Z"}])
        self.assertEqual(0, fourth["added"],
                         "同一时刻的另一种写法必须认成同一条（幂等靠归一，不靠字符串）")
        self.assertEqual(3, len(self._meta(name)["pauses"]))

    def test_a_mismatched_client_id_is_refused_with_zero_writes(self):
        """`clientId` 对不上 → 拒绝，而且**一个字节都不动**（与 stop 同一护栏）。"""
        started = self._start()
        name = started["message"]
        with open(os.path.join(self._folder(name), "meta.json"), "rb") as fh:
            before = fh.read()
        body = self._post_pauses([self.p1], client_id=OTHER_CID)
        self.assertFalse(body["ok"], body)
        self.assertIn("clientId", body["message"], "拒绝必须说清是'对不上号'")
        with open(os.path.join(self._folder(name), "meta.json"), "rb") as fh:
            self.assertEqual(before, fh.read(), "被拒时 meta.json 不许被改写")
        self.assertEqual(0, body["added"])
        # 这场会**还在录**（护栏只管"记不记暂停"，不动任何录音状态）
        self.assertTrue(self._status()["active"])

    def test_no_recording_is_an_honest_failure_and_creates_nothing(self):
        """没在录 → `ok=false` + 原因，**绝不顺手建会**。"""
        before_names = self._meeting_names()
        before_ids = [m["id"] for m in db.list_meetings()]
        body = self._post_pauses([self.p1])
        self.assertFalse(body["ok"], body)
        self.assertTrue(body["message"])
        self.assertIn("没有在录音", body["message"])
        self.assertEqual(before_names, self._meeting_names(), "不许因为补报暂停而建一场会")
        # ⚠️ 断言"**没有新增**"而不是"库里是空的"：同一个临时库在这组用例里是共用的
        # （临时库的目的是不碰真库，不是每例一个新库），前面几条用例本来就会留下会议。
        self.assertEqual(before_ids, [m["id"] for m in db.list_meetings()],
                         "补报暂停不许往库里加会议")

    def test_the_endpoint_does_not_change_any_recording_state(self):
        """**只记状态、不动流程**：补报暂停不许开始/停止/改变录音状态。"""
        started = self._start()
        name = started["message"]
        self._segment(1)
        before = self._status()
        self.assertFalse(self._transcribe_done.is_set())
        body = self._post_pauses([self.p1, self.p2])
        self.assertTrue(body["ok"], body)
        after = self._status()
        for key in ("active", "paused", "source", "segments", "clientId", "folder",
                    "startedAt"):
            self.assertEqual(before.get(key), after.get(key),
                             "补报暂停改了 %s —— 它只该动 meta['pauses']" % key)
        self.assertEqual([], self.transcribe_calls, "补报暂停不许触发转写")
        self.assertEqual("recording", db.get_meeting(started["meetingId"])["status"],
                         "那场会必须还在录")
        self.assertFalse(self._transcribe_done.is_set())
        # 收尾时开区间还在（服务端不替它收尾，那是 stop 的事）
        self.assertEqual(2, len(self._meta(name)["pauses"]))

    def test_an_unreadable_time_is_skipped_not_fatal(self):
        """坏时间那条**跳过并如实计数**，不因为一条坏区间丢掉整批（其余照记）。"""
        started = self._start()
        name = started["message"]
        body = self._post_pauses([self.p1, {"from": "十月十六号", "to": ""},
                                  {"nonsense": 1}])
        self.assertTrue(body["ok"], body)
        self.assertEqual(1, body["added"])
        self.assertEqual(2, body["skipped"])
        self.assertEqual(1, len(self._meta(name)["pauses"]))

    def test_a_non_json_body_is_rejected(self):
        """body 不是 JSON / 不是对象 → 422（请求本身不合法，与"业务上干不了"分开）。"""
        self._start()
        r = self.client.post("/api/meeting/pauses", content=b"not json",
                             headers={**self._headers(), "Content-Type": "application/json"})
        self.assertEqual(422, r.status_code, r.text)
        r2 = self.client.post("/api/meeting/pauses", json=[1, 2, 3], headers=self._headers())
        self.assertEqual(422, r2.status_code, r2.text)
        r3 = self.client.post("/api/meeting/pauses", json={"clientId": CID, "pauses": "x"},
                              headers=self._headers())
        self.assertEqual(422, r3.status_code, r3.text)

    def test_the_endpoint_needs_a_token_like_any_other_phone_call(self):
        """开了鉴权就是开了：不带令牌的补报照样 401（别把这条路开成后门）。"""
        self._start()
        r = self.client.post("/api/meeting/pauses",
                             json={"clientId": CID, "pauses": [self.p1]})
        self.assertEqual(401, r.status_code, r.text)

    def test_pauses_reported_for_a_local_mic_meeting_cannot_forge_a_client_id(self):
        """电脑麦那场（没有 clientId）被带 clientId 的补报命中 → 拒绝（不许冒充）。"""
        meeting_mod._state.update(active=True, folder=self._folder("pc-meeting"),
                                  recorder=None, source="pc", client_id="", meeting_id=0)
        os.makedirs(self._folder("pc-meeting"), exist_ok=True)
        body = self._post_pauses([self.p1])
        self.assertFalse(body["ok"], body)
        self.assertIn("clientId", body["message"])
        self.assertFalse(os.path.exists(os.path.join(self._folder("pc-meeting"),
                                                     "meta.json")),
                         "被拒时不许碰那一场的目录")


if __name__ == "__main__":
    unittest.main()
