# -*- coding: utf-8 -*-
"""「录音是本地的事，转写是远端的事」——两者混在一个状态里就会把音频谎报成失败。

用户 2026-10-16 原话：**"停止录音不必依赖后端，可以先把数据保存好，不叫录音失败，
应该是转写失败"**。

现场（同一天真机）：后端不可达时停掉一场 9 段的会议，面板上这场会显示**失败** ✗，
而音频（段文件 / `meta.json` / 时长）完好地躺在磁盘上 —— 状态与事实相反。
第二处现场是 09:33 那场：转写跑到一半后端掉线，烂尾在错的状态上（音频在、状态错）。

这一组用例把下面这张表钉死：

| 情形 | `meetings.status` | 原因词（`meta.transcribeFallbackReason`） |
| --- | --- | --- |
| 后端连不上 | `imported`（面板「待转写」） | `waiting-backend` |
| 压根没配后端 | `imported` | `not-configured` |
| 被「允许音频去哪」挡住 | `imported` | `blocked-by-privacy` |
| 转写**中途**后端掉线 | `imported`（已转出的行保留） | `waiting-backend` |
| **引擎真的报了错** | `error` + 引擎原话 | ——（原话落 `meetings.error`） |
| 转写成功 | `transcribed` | —— |

`imported` 为什么是"待转写"那一档、而不是新造一个词：见
`app/meeting.py::MEETING_STATUS_WAITING` 的说明（面板两侧早就把它翻成「待转写」了）。

隔离（照 `tests/test_meeting_pause.py` / `test_meeting_speakers_standard.py` 的写法）：
临时库（`db.DATA_DIR` / `db.DB_FILE`）+ 临时会议目录（`meeting.meetings_dir`）
+ **临时凭据文件**（`capabilities.credentials.credentials_path` —— 不隔离的话，
这台开发机**真配对过**的后端会被算进这一场，用例就不是在验"连不上"了）。
跑完一律还原，一个字节都不写进真库 / 真会议目录。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                                       # noqa: E402
from app import meeting as meeting_mod                                   # noqa: E402
from app.capabilities import credentials as cred_mod                     # noqa: E402
from app.capabilities import (                                           # noqa: E402
    BACKEND_ECHO_SERVER,
    SOURCE_LAN,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    CapabilityRouter,
    Provenance,
)
from app.config import settings                                          # noqa: E402


def _write_wav(path, seconds=1.0):
    """一段**真的** wav（16k 单声道 int16，与 recorder.py 的产出同格式）。"""
    import struct
    n = int(16000 * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


class _InlineThread:
    """把 `threading.Thread(...).start()` 变成**当场跑完**（判据要确定性，不靠 sleep 等）。

    只在这一个调用的窗口里替（`with patch.object(...)`）：`stop_meeting()` /
    `retranscribe_meeting()` 都用它起后台转写，替掉之后"停止录音 → 转写收尾"这条链
    在同一个线程里跑完，断言就不必靠轮询去猜它跑到哪了。
    """

    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self._target = target
        self._args = tuple(args or ())
        self._kwargs = dict(kwargs or {})

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class _AsrStub(CapabilityClient):
    """只会转写的能力后端替身；按脚本在第几段失败、以及失败成什么样。"""

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"asr.text"})

    def __init__(self, text="后端文本。", fail_from=None, fail_with=("offline", "连不上")):
        self.text = text
        self.calls = []
        self.fail_from = fail_from          # 从第几段（1 起）开始失败；None = 不失败
        self.fail_with = fail_with          # (reason, detail)

    def transcribe(self, wav, **kw):
        self.calls.append(os.path.basename(wav))
        if self.fail_from is not None and len(self.calls) >= self.fail_from:
            reason, detail = self.fail_with
            raise CapabilityError(reason, detail, backend_id=self.backend_id,
                                  slot="asr.text")
        return AsrResult(text=self.text, sentences=(), timestamps="none",
                         provenance=Provenance(self.backend_id, "stub-v1"),
                         audio_seconds=1.0)


class _WaitingCase(unittest.TestCase):
    """一场真会议（目录 + meta.json + 库记录 + 真 wav）+ 全套隔离 + 后端可配可不配。"""

    SEGS = 9            # 用户真机上那场就是 9 段

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-waiting-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-waitingdb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        settings._cache = None

        self.meetings_root = os.path.join(self.tmp, "meetings")
        os.makedirs(self.meetings_root, exist_ok=True)
        p = patch.object(meeting_mod, "meetings_dir", lambda: self.meetings_root)
        p.start()
        self.addCleanup(p.stop)

        # **这台机器真配对过**也不能漏进来：凭据文件指到临时目录。
        self._cred = os.path.join(self.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: self._cred)
        p.start()
        self.addCleanup(p.stop)

        # 用例不许出声、也不许真去生成纪要（那会去连 DSH）。
        for target, kw in ((meeting_mod.tts_mod, "play_beep"),
                           (meeting_mod.tts_mod, "beep_ok")):
            p = patch.object(target, kw, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        for fn in ("request_summary", "request_topic_segments"):
            p = patch.object(meeting_mod, fn, lambda *a, **k: True)
            p.start()
            self.addCleanup(p.stop)
        # `seed_defaults()` 在这台机器上要 2 秒（一百多条设置各自一个事务）——
        # 用例只关心会议状态，直接改这一条键就够（别为了一条设置付整批的代价）。
        settings.update({"meetingAutoSummarize": False})
        settings._cache = None

        # 每个用例从"没在录音"开始
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0)

        def _clean():
            meeting_mod._state.update(active=False, folder=None, recorder=None,
                                      paused=False, pauses=[], source="pc", seg_count=0)
            db.DATA_DIR, db.DB_FILE = self._old_db
            # 进程级设置缓存必须**按真实库重建后**再交出去（本类把它按临时库读满过）
            settings._cache = None
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    # ---------------------------------------------------------------- 小工具

    def _make_meeting(self, name, segs=SEGS, seconds=1.0):
        """造一场"录完了"的会议：目录 + 真 wav + meta.json + 库记录。"""
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        names = ["%02d.wav" % (i + 1) for i in range(segs)]
        for seg in names:
            _write_wav(os.path.join(folder, seg), seconds)
        meta = {"segments": list(names),
                "start": "2026-10-16T09:33:00",
                "end": "2026-10-16T10:12:00",
                "durationSeconds": seconds * len(names),
                "config": {"sttModel": "small", "sttDevice": "auto", "diarize": True},
                "pauses": []}
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        mid = db.create_meeting(name, started_at=meta["start"], stt_model="small",
                                stt_device="auto", diarize=1)
        db.update_meeting(mid, segments=len(names),
                          duration_seconds=meta["durationSeconds"])
        return mid, folder, names

    def _dead_backend(self):
        """**配了**一个连不上的后端（真地址没人监听：这正是"后端不可达"那条真路径）。"""
        settings.update({"capabilityEchoServerUrl": "http://127.0.0.1:1"})
        settings._cache = None

    def _use_backend(self, backend):
        """把能力路由换成"只有这个替身"（一个真后端都不连）。"""
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        p = patch("app.capabilities.build_default_router", lambda **kw: router)
        p.start()
        self.addCleanup(p.stop)

    def _meta(self, folder):
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _stop(self, folder, segs):
        """走**真的** `stop_meeting()`（只把后台转写线程换成当场跑完）。"""
        class _Rec:
            def __init__(self):
                self.segments = list(segs)
                self.error = ""

            def stop(self):
                return True

        rec = _Rec()
        meeting_mod._state.update(active=True, folder=folder, recorder=rec,
                                  started_at="2026-10-16T09:33:00", error="",
                                  paused=False, pauses=[], seg_count=len(segs))
        with patch.object(meeting_mod.threading, "Thread", _InlineThread):
            ok, msg = meeting_mod.stop_meeting()
        return ok, msg


# ---------------------------------------------------------------- ① 停止录音永远成功

class StopRecordingIsALocalActionTests(_WaitingCase):
    """停止录音是**本地动作**：音频落盘就算成功，后端通不通与它无关。"""

    def test_stop_returns_ok_and_the_audio_is_kept_verbatim(self):
        """判据 ①：后端不可达时 `stop_meeting()` 仍然 `ok=true`，段数/时长/meta/end 逐字完好。"""
        name = "2026-10-16_09-33-00"
        mid, folder, names = self._make_meeting(name)
        self._dead_backend()

        ok, msg = self._stop(folder, names)
        self.assertTrue(ok, "停止录音不该依赖后端：%r" % (msg,))

        meta = self._meta(folder)
        self.assertEqual(names, meta["segments"], "段清单必须逐字完好")
        # `end` 由**停止这一刻**写下（这是它本来的语义），只要写下来了、而且是真时间即可
        self.assertEqual(db.get_meeting(mid)["ended_at"], meta["end"],
                         "库里的 ended_at 与 meta 的 end 必须是同一个时刻")
        import datetime
        datetime.datetime.fromisoformat(meta["end"])
        self.assertAlmostEqual(9.0, float(meta["durationSeconds"]), places=2)
        for seg in names:
            self.assertTrue(os.path.isfile(os.path.join(folder, seg)),
                            "音频段必须还在：%s" % seg)
        row = db.get_meeting(mid)
        self.assertEqual(9, row["segments"], "库里的段数也要在")
        self.assertAlmostEqual(9.0, float(row["duration_seconds"]), places=2)

    def test_the_meeting_reads_waiting_not_error(self):
        """判据 ②：同一场会的状态是**待转写**，不是 `error`（面板不许说"录音失败"）。"""
        name = "2026-10-16_09-33-00"
        mid, folder, _names = self._make_meeting(name)
        self._dead_backend()

        ok, _msg = self._stop(folder, _names)
        self.assertTrue(ok)
        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"],
                         "后端连不上时这场是「待转写」：%r" % (row["status"],))
        self.assertNotEqual("error", row["status"],
                            "把「音频已安全保存」标成 error 就是这次要修的谎")
        self.assertIn("录音已完成，音频已保存", row["error"] or "")
        self.assertIn("后端暂不可用", row["error"] or "")
        meta = self._meta(folder)
        self.assertEqual("waiting-backend", meta.get("transcribeFallbackReason"))
        self.assertEqual("", meta.get("transcribeEngine"),
                         "文字没出来，就不许记成「本场转写由能力后端完成」")
        self.assertTrue(meta["diarize"]["waiting"], meta["diarize"])

    def test_nothing_configured_also_lands_in_waiting(self):
        """判据 ②的另一半："没配后端"同样落**待转写**（不是 error，也不是"连不上"）。"""
        name = "2026-10-16_11-00-00"
        mid, folder, _names = self._make_meeting(name)
        meeting_mod._transcribe_impl(folder)          # 等价于 stop 之后那次后台转写

        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertIn("录音已完成，音频已保存", row["error"] or "")
        self.assertIn("还没配转写后端", row["error"] or "")
        self.assertEqual("not-configured",
                         self._meta(folder).get("transcribeFallbackReason"))

    def test_privacy_blocked_is_not_reported_as_offline(self):
        """判据 ②的第三档：被「允许音频去哪」挡住 → `blocked-by-privacy`（去改许可，不是等）。"""
        name = "2026-10-16_12-00-00"
        mid, folder, _names = self._make_meeting(name)
        # 后端在**内网**（不是回环），而许可只要"不出机" → 连得上，但不许发
        settings.update({"capabilityEchoServerUrl": "http://gpu-01:8900",
                         "capabilityPrivacy": "none"})
        settings._cache = None
        meeting_mod._transcribe_impl(folder)

        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertIn("「允许音频去哪」", row["error"] or "")
        self.assertNotIn("连不上", row["error"] or "",
                         "说成「连不上」会把用户引去查网络/端口：%r" % row["error"])
        self.assertEqual("blocked-by-privacy",
                         self._meta(folder).get("transcribeFallbackReason"))


# ---------------------------------------------------------------- ② 转写中途掉线

class BackendDropsInTheMiddleTests(_WaitingCase):
    """转写**中途**后端掉线 → 待转写（不是 error），音频与已转出的行都在。"""

    def test_the_backend_dies_on_the_second_segment(self):
        """判据 ③：一段成了、下一段连不上 → 结论是"还没转完"，不是"已转写"、也不是"失败"。"""
        name = "2026-10-16_13-00-00"
        mid, folder, names = self._make_meeting(name, segs=3)
        stub = _AsrStub(text="第一段的话。", fail_from=2,
                        fail_with=("offline", "第二段后端就挂了"))
        self._use_backend(stub)

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("waiting", code, "中途掉线不是 error，也不是 transcribed")
        # 段内**不换引擎、也不提前放弃整场**：每段各自留痕（既有纪律），
        # 所以三段都试过，只是第 2、3 段没有内容。
        self.assertEqual(["01.wav", "02.wav", "03.wav"], stub.calls)
        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertNotEqual("error", row["status"])
        self.assertIn("录音已完成，音频已保存", row["error"] or "")
        # 已转出来的那一段**保留**（能看多少是多少）
        self.assertEqual(["第一段的话。"], [ln["text"] for ln in db.get_lines(mid)])
        # 音频一个字节都没动
        for seg in names:
            self.assertTrue(os.path.isfile(os.path.join(folder, seg)), seg)
        self.assertEqual(names, self._meta(folder)["segments"])
        self.assertEqual("waiting-backend",
                         self._meta(folder).get("transcribeFallbackReason"))
        self.assertEqual("", self._meta(folder).get("transcribeEngine"),
                         "一个字都没转全，就不许说这场走了后端")

    def test_every_segment_failing_is_still_waiting(self):
        """判据 ③的极端：**9 段全掉**（真机上那场就是 9 段）→ 待转写，音频完好。"""
        name = "2026-10-16_14-00-00"
        mid, folder, names = self._make_meeting(name, segs=9)
        stub = _AsrStub(fail_from=1, fail_with=("offline", "整台后端都不在"))
        self._use_backend(stub)

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("waiting", code)
        self.assertEqual(9, len(stub.calls), "九段都该试过（不是第一段就放弃整场）")
        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertEqual(9, row["segments"], "段数不许被转写改坏")
        self.assertAlmostEqual(9.0, float(row["duration_seconds"]), places=2)
        for seg in names:
            self.assertTrue(os.path.isfile(os.path.join(folder, seg)), seg)


# ---------------------------------------------------------------- ③ 引擎真的报错

class ARealEngineErrorStaysAnErrorTests(_WaitingCase):
    """**只有引擎真的报了错**才是 `error`，而且必须带上它的原话（不许吞成"待转写"）。"""

    def test_an_engine_error_keeps_the_engine_wording(self):
        """判据 ④：服务端回 `model_failed`/CUDA 报错 → `error` + 原话。"""
        name = "2026-10-16_15-00-00"
        mid, folder, _names = self._make_meeting(name, segs=2)
        stub = _AsrStub(fail_from=1,
                        fail_with=("error", "模型加载失败：CUDA out of memory"))
        self._use_backend(stub)

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("error", code, "引擎真报错这一档不许被吞成「待转写」")
        row = db.get_meeting(mid)
        self.assertEqual("error", row["status"], row)
        self.assertIn("CUDA out of memory", row["error"] or "",
                      "引擎的原话必须带给用户：%r" % row["error"])
        self.assertIn("转写引擎报错", row["error"] or "")
        self.assertNotIn("录音已完成，音频已保存", row["error"] or "",
                         "引擎报错那一档不是「音频已保存、在等后端」")
        # 音频照样在（error 说的是"转写失败"，不是"录音失败"，但音频本来也不该动）
        for seg in ("01.wav", "02.wav"):
            self.assertTrue(os.path.isfile(os.path.join(folder, seg)), seg)

    def test_the_engine_error_is_not_reported_as_offline(self):
        """引擎**答了话**（它报错，而不是连不上）→ 不许再说"在等后端/连不上"。"""
        name = "2026-10-16_15-30-00"
        _mid, folder, _names = self._make_meeting(name, segs=1)
        self._use_backend(_AsrStub(fail_from=1,
                                   fail_with=("error", "模型坏了：model_failed")))
        meeting_mod._transcribe_impl(folder)

        meta = self._meta(folder)
        self.assertFalse(meta["diarize"]["waiting"],
                         "后端答了话就不是「在等它」：%s" % meta["diarize"])
        self.assertNotEqual("waiting-backend", meta.get("transcribeFallbackReason"))


# ---------------------------------------------------------------- ④ 重新转写

class RetranscribeFromWaitingTests(_WaitingCase):
    """待转写的会议**点得动**「重新转写」；后端仍不可用还是待转写，可用就走通。"""

    def _waiting_meeting(self, name="2026-10-16_16-00-00", segs=2):
        mid, folder, names = self._make_meeting(name, segs=segs)
        self._dead_backend()
        meeting_mod._transcribe_impl(folder)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING,
                         db.get_meeting(mid)["status"], "前置：这场在待转写")
        return mid, folder, names

    def _retranscribe(self, mid):
        with patch.object(meeting_mod.threading, "Thread", _InlineThread):
            return meeting_mod.retranscribe_meeting(mid)

    def test_backend_still_down_goes_back_to_waiting_with_the_reason(self):
        """判据 ⑤前半：后端仍不可用 → 仍「待转写」+ 原因，**不是** error。"""
        mid, folder, names = self._waiting_meeting()
        ok, msg = self._retranscribe(mid)
        self.assertTrue(ok, msg)

        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertNotEqual("error", row["status"])
        self.assertIn("录音已完成，音频已保存", row["error"] or "")
        self.assertEqual("waiting-backend",
                         self._meta(folder).get("transcribeFallbackReason"))
        self.assertEqual(names, self._meta(folder)["segments"])

    def test_a_working_backend_gets_it_transcribed(self):
        """判据 ⑤后半：后端可用（打桩）→ 走通并变 `transcribed`。"""
        mid, folder, _names = self._waiting_meeting("2026-10-16_17-00-00")
        self._use_backend(_AsrStub(text="后端回来了。"))
        ok, msg = self._retranscribe(mid)
        self.assertTrue(ok, msg)

        row = db.get_meeting(mid)
        self.assertEqual("transcribed", row["status"], row)
        self.assertEqual("", row["error"] or "",
                         "转成功了，上一次「为什么没转」的原因必须清掉")
        self.assertEqual(["后端回来了。", "后端回来了。"],
                         [ln["text"] for ln in db.get_lines(mid)])
        self.assertEqual("", self._meta(folder).get("transcribeFallbackReason"))

    def test_a_waiting_meeting_is_not_blocked_by_the_busy_gate(self):
        """待转写的会议**不占**「正在转写」那道闸 —— 否则用户永远救不回这一场。"""
        mid, _folder, _names = self._waiting_meeting("2026-10-16_18-00-00")
        self.assertEqual("", meeting_mod.transcribe_busy_reason(
            db.get_meeting(mid)["name"]))
        self.assertFalse(meeting_mod.is_transcribing(db.get_meeting(mid)["name"]))


if __name__ == "__main__":
    unittest.main()
