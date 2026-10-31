# -*- coding: utf-8 -*-
"""回顾轮次的**上下文标记**（用户 2026-10-20 原话）：

  > **"回顾的历史对话，指令里也有能不重复吗？"**

现场：手机那一场回顾走的是既有指令链（`voice-command` / `assistant/command`），
落库记 `source='mobile'`，于是同一句话**在回顾页出现一次、在指令历史又出现一次**；
而「回顾历史」只读 `source='review'`，这几轮反倒不在里面。

本文件钉住修好之后的口径（每一条都对应一种会回退的写法）：

  1. `context=review` → 落库 **`source='review'`**（回顾轮次属于回顾，不属于发指令），
     而且**执行照旧发生**（智能体真的收到原话）—— 标签不改执行；
  2. 这些轮次**进「回顾历史」**（`/api/daily-review/history*` 看得到）；
  3. `GET /api/commands`（指令历史）**不包含**它们（两处只出现一次）；
  4. 不带 `context` 的**老客户端**行为一个字节都不变（语音仍是 `mobile`）；
  5. 发起方口径不变：手机发起的回顾轮次 `origin=mobile`（电脑喇叭不念，
     手机按 id 取到的行仍是 `origin=mobile`）；
  6. **幂等**：同一轮（同一个 `requestId`）重复发送只产生一条回顾记录、
     只执行一次。

隔离照 `tests/test_command_speak_routing.py`：临时库 + DSH/TTS/通知全打桩，
**不写真库、不真响一声、不联网**。
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
import wave
from types import SimpleNamespace
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

import app.db as db                                                  # noqa: E402
from app import assistant as assistant_mod                           # noqa: E402
from app import daily_review as dr                                   # noqa: E402
from app import providers as providers_mod                           # noqa: E402
from app.api import REVIEW_CONTEXT, router                           # noqa: E402
from app.audio import stt as stt_mod                                 # noqa: E402
from app.audio import tts as audio_tts                               # noqa: E402
from app.config import settings                                      # noqa: E402

REPLY = "好的，这一段我记下了。"


def _wav_bytes(seconds=0.2, sr=16000):
    """一段真 wav（16k 单声道 PCM）：`_audio_to_wav16k` 要能认它。"""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(sr * seconds)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * ((i % 40) - 20) / 20))
                               for i in range(n)))
    return buf.getvalue()


class _FakeDshClient:
    """DSH 替身：只够 `_dispatch` 走完"发出 → 等回复 → 写 command_done"。"""

    def __init__(self, reply=REPLY):
        self.reply = reply
        self.prompted = []

    def ping(self):
        return True

    def ensure_command_session(self, text):
        return "sid-review-tag"

    def clear_stuck(self, sid):
        pass

    def prompt(self, sid, text, mode="queue"):
        self.prompted.append(text)

    def wait_for_reply(self, sid, timeout=0, poll=0):
        return (self.reply, True)


class _Spoken:
    """TTS 记录器：只记不响。"""

    def __init__(self):
        self.calls = []

    def __call__(self, text, *a, **kw):
        self.calls.append(text)
        return True


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-reviewtag-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        # 进程级设置缓存不受 db 补丁约束：不清就会读到真库（比如真的开着 apiAuthEnabled）
        settings._drop_cache()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, dr.VAULT_NOTE_DIR), exist_ok=True)
        settings.update({"dailyReviewVaultRoot": self.vault})

        self.dsh = _FakeDshClient()
        self.speak_async = _Spoken()
        self.speak_text = _Spoken()
        self._audio_speak = _Spoken()

        self._patchers = [
            mock.patch.object(assistant_mod, "get_client", lambda: self.dsh),
            mock.patch.object(assistant_mod, "_agent_snapshot", lambda: ""),
            mock.patch.object(assistant_mod, "build_env_context", lambda: ""),
            mock.patch.object(assistant_mod, "build_reply_requirement", lambda: ""),
            mock.patch.object(assistant_mod, "notify", lambda *a, **kw: None),
            mock.patch.object(assistant_mod, "tts_mod",
                              SimpleNamespace(play_beep=lambda *a, **kw: None,
                                              beep_ok=lambda *a, **kw: None,
                                              speak=lambda *a, **kw: True)),
            mock.patch.object(providers_mod, "speak_async", self.speak_async),
            mock.patch.object(providers_mod, "speak_text", self.speak_text),
            mock.patch.object(audio_tts, "speak", self._audio_speak),
        ]
        for p in self._patchers:
            p.start()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self._clean)

    def _clean(self):
        # ⚠️ 必须等 `_dispatch` 线程**真的退出**再还 DB：它最后还要读设置
        # （`beepOnSend`）并写 `command_done`。不等的话，线程会在下一个用例
        # 换了 DB 之后继续写 —— 那是典型的"跨用例污染"（本轮真踩到了：
        # `no such table: settings` + 上一条用例的事件跑到下一条里）。
        self._join_dispatches()
        for p in self._patchers:
            p.stop()
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._drop_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _join_dispatches(self, timeout=10.0):
        """等所有 `assistant._dispatch` 线程结束（`send_text` 是起线程就返回的）。"""
        deadline = time.time() + timeout
        for t in list(threading.enumerate()):
            if t is threading.current_thread():
                continue
            if getattr(t, "_target", None) is not assistant_mod._dispatch:
                continue
            t.join(max(0.0, deadline - time.time()))

    # ---------------------------------------------------------------- 小工具

    def _wait(self, fn, timeout=4.0):
        """等后台 `_dispatch` 线程把状态写出来（`send_text` 是起线程就返回的）。"""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = fn()
            if last:
                return last
            time.sleep(0.02)
        return last

    def _row(self, cmd_id):
        return db.get_command(int(cmd_id))

    def _done_row(self, cmd_id):
        row = self._row(cmd_id)
        if row and str(row.get("status") or "") in ("done", "failed"):
            return row
        return None

    def _review_rows(self):
        return db._query(                                            # noqa: SLF001
            "SELECT * FROM commands WHERE source=? ORDER BY id ASC", (dr.REVIEW_SOURCE,))

    def _events(self, type_):
        out = []
        for row in reversed(db.list_events(limit=200)):
            if row.get("type") != type_:
                continue
            try:
                out.append(json.loads(row.get("payload") or "{}"))
            except Exception:                                      # noqa: BLE001
                out.append({})
        return out

    def _event_for(self, cmd_id, type_="command_done"):
        """等这一行的 `command_done`（状态 done 先写、事件后写，中间有一个窗口）。"""
        return self._wait(lambda: next(
            (e for e in self._events(type_) if e.get("id") == int(cmd_id)), None))

    def _post_text(self, text, context=None, request_id=None, source="mobile"):
        params = {}
        if context is not None:
            params["context"] = context
        if request_id is not None:
            params["requestId"] = request_id
        return self.client.post("/api/assistant/command", params=params,
                                json={"text": text, "source": source})

    def _post_voice(self, hear="今天整理了三份文档", context=None, request_id=None):
        params = {}
        if context is not None:
            params["context"] = context
        if request_id is not None:
            params["requestId"] = request_id
        with mock.patch("app.assistant._transcribe_command",
                        lambda *a, **k: (hear, "backend=echo-server", "ok")):
            return self.client.post("/api/assistant/voice-command", params=params,
                                    content=_wav_bytes(),
                                    headers={"Content-Type": "audio/wav"})


# ===================================================================== 文字指令
class TextReviewTurnTests(_Base):

    def test_context_review_lands_as_review_and_still_executes(self):
        """判据 1/2/3/5：落 review、真的执行、进回顾历史、不进指令历史、origin 仍是手机。"""
        r = self._post_text("今天做了三件事", context="review")
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(REVIEW_CONTEXT, body["source"], "回顾轮次要记成 review")
        self.assertIsNotNone(body["commandId"], "要回 commandId：手机按 id 取这一轮的结果")

        row = self._wait(lambda: self._done_row(body["commandId"]))
        self.assertIsNotNone(row, "回顾轮次没有跑完")
        self.assertEqual("review", row["source"], "落库该是 source='review'")
        self.assertEqual("今天做了三件事", row["text"])

        # 执行照旧发生：原话真的交给了智能体（`source` 只是标签）
        self.assertEqual(1, len(self.dsh.prompted), "回顾轮次没有真的执行")
        self.assertIn("今天做了三件事", self.dsh.prompted[0])

        # 进「回顾历史」：按天列表 + 当天详情都看得到这一轮
        hl = self.client.get("/api/daily-review/history").json()
        today = time.strftime("%Y-%m-%d")
        self.assertEqual(1, hl["total"], hl)
        self.assertEqual(today, hl["items"][0]["date"])
        self.assertEqual(1, hl["items"][0]["turns"])
        detail = self.client.get("/api/daily-review/history/%s" % today).json()
        self.assertTrue(detail["ok"], detail)
        self.assertEqual(1, detail["count"])
        self.assertEqual("今天做了三件事", detail["turns"][0]["text"])
        self.assertEqual("done", detail["turns"][0]["status"])

        # 指令历史**不再重复**这一轮（默认列表与搜索都不该有它）
        lst = self.client.get("/api/commands").json()
        self.assertEqual(0, lst["total"], "回顾轮次混进了指令历史：%s" % (lst,))
        self.assertEqual([], lst["items"])
        self.assertEqual(0, self.client.get("/api/commands?q=三件事").json()["total"],
                         "搜索也不该把回顾轮次当成指令")

        # 发起方口径不变：手机发起的任务 → origin=mobile（电脑不念，手机自己念）
        one = self.client.get("/api/commands/%d" % body["commandId"]).json()
        self.assertEqual("mobile", one["origin"], "回顾轮次仍是手机发起的")
        ev = self._event_for(body["commandId"])
        self.assertIsNotNone(ev, "没有 command_done 事件")
        self.assertEqual("mobile", ev.get("origin"))
        self.assertEqual([], self.speak_async.calls, "手机发起的任务，电脑不该朗读")

    def test_without_context_old_clients_are_unchanged(self):
        """判据 4：不带 `context` → 仍是 `source=mobile`，且它**在**指令历史里。"""
        r = self._post_text("普通指令", context=None)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual("mobile", body["source"], "老客户端行为不许变")

        row = self._wait(lambda: self._done_row(body["commandId"]))
        self.assertEqual("mobile", row["source"])
        lst = self.client.get("/api/commands").json()
        self.assertEqual(1, lst["total"], "普通指令必须还在指令历史里")
        self.assertEqual("普通指令", lst["items"][0]["text"])
        self.assertEqual(0, self.client.get("/api/daily-review/history").json()["total"])

    def test_unknown_context_is_ignored(self):
        """认不出的 context 不许改变落库（老/新客户端混跑时不能悄悄改行为）。"""
        r = self._post_text("另一条", context="something-else")
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual("mobile", body["source"])

    def test_the_tag_matches_the_review_source_constant(self):
        """`context=review` 的落库值必须就是回顾历史读的那个值（单一出处）。"""
        self.assertEqual(dr.REVIEW_SOURCE, REVIEW_CONTEXT)


# ===================================================================== 语音指令
class VoiceReviewTurnTests(_Base):

    def test_voice_with_context_review_is_tagged_and_still_runs(self):
        """判据 1/2/3/5：语音回顾轮次同样记 review、真的执行、进回顾历史。"""
        r = self._post_voice(hear="这一轮我说的是车上的事", context="review")
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual("review", body["source"])
        self.assertEqual("这一轮我说的是车上的事", body["text"])
        self.assertIsNotNone(body["commandId"])

        row = self._wait(lambda: self._done_row(body["commandId"]))
        self.assertIsNotNone(row, "语音回顾轮次没有跑完")
        self.assertEqual("review", row["source"])
        self.assertEqual("这一轮我说的是车上的事", row["text"])
        self.assertIn("这一轮我说的是车上的事", self.dsh.prompted[0], "回顾轮次没有真的执行")

        detail = self.client.get(
            "/api/daily-review/history/%s" % time.strftime("%Y-%m-%d")).json()
        self.assertEqual(1, detail["count"])
        self.assertEqual("这一轮我说的是车上的事", detail["turns"][0]["text"])
        self.assertEqual(0, self.client.get("/api/commands").json()["total"])
        ev = self._event_for(body["commandId"])
        self.assertIsNotNone(ev, "没有 command_done 事件")
        self.assertEqual("mobile", ev.get("origin"))

    def test_voice_without_context_still_mobile(self):
        """判据 4：老客户端（不带标记）的语音仍记 `mobile`，也仍进指令历史。"""
        r = self._post_voice(hear="普通的一句话", context=None)
        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertEqual("mobile", body["source"])
        row = self._wait(lambda: self._done_row(body["commandId"]))
        self.assertEqual("mobile", row["source"])
        self.assertEqual(1, self.client.get("/api/commands").json()["total"])
        self.assertEqual(0, self.client.get("/api/daily-review/history").json()["total"])


# ===================================================================== 幂等
class ReviewTurnIdempotencyTests(_Base):

    def test_the_same_round_twice_makes_one_review_row_and_one_execution(self):
        """判据 6：同一轮（同一个 requestId）重复发送 → 一条回顾、只执行一次。"""
        key = "session-1:7"
        first = self._post_text("同一轮", context="review", request_id=key)
        b1 = first.json()
        self.assertTrue(b1["ok"], b1)
        self._wait(lambda: self._done_row(b1["commandId"]))

        second = self._post_text("同一轮", context="review", request_id=key)
        self.assertEqual(200, second.status_code, second.text)
        b2 = second.json()
        self.assertTrue(b2["ok"], "重复发送仍是成功（幂等），不能报错")
        self.assertTrue(b2.get("duplicate"), "第二下要被认成重复：%s" % (b2,))
        self.assertEqual(b1["commandId"], b2["commandId"], "要回同一行的 id")

        rows = self._review_rows()
        self.assertEqual(1, len(rows), "同一轮记成了两条回顾：%s" % (rows,))
        self.assertEqual(1, len(self.dsh.prompted), "同一轮被执行了两次")

    def test_two_different_rounds_are_both_recorded(self):
        """幂等键是"这一轮"，不是"这句话"：两轮内容相同也要各记一条。"""
        a = self._post_text("嗯", context="review", request_id="session-1:1").json()
        self._wait(lambda: self._done_row(a["commandId"]))
        b = self._post_text("嗯", context="review", request_id="session-1:2").json()
        self._wait(lambda: self._done_row(b["commandId"]))
        self.assertNotEqual(a["commandId"], b["commandId"])
        self.assertEqual(2, len(self._review_rows()), "不同的轮次该各记一条")
        self.assertEqual(2, len(self.dsh.prompted))


if __name__ == "__main__":
    unittest.main()
