# -*- coding: utf-8 -*-
"""手机回顾的一轮：`POST /api/daily-review/turn`（2026-10-22）。

用户报的现场：*"我点了之后命令仍然发到了指令空间，整个逻辑也不是跟 pc 完全一样"*。

旧实现让每一轮走 `POST /api/assistant/voice-command?context=review`：
`context` 只改**落库标签**，执行仍然发到 `assistant.send_text()` 的**命令目标**
（DSH 指令空间），而且没走 daily-review 技能。这一轮改成与 PC 回顾**同一条下游**：
回顾转写 → `daily_review.submit()`（整理 / 播报 / 落 `source='review'`）。

本文件钉住四件事：
  1. 音频轮次真的交给 `daily_review.submit()`，并落**一条 review 行**；
  2. **绝不**调用 `assistant.send_text()`（= 不再进指令空间）；
  3. "没听清"与"转写引擎坏了"是两句不同的人话，而且都不落一轮；
  4. 请求体形状不对（空 / 不是音频）是 4xx，不能伪装成"回顾失败"。

全程临时库 + 临时笔记库（复用 `tests.test_daily_review._Base`），客户端是替身。
"""
import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.api as api_mod                                           # noqa: E402
import app.db as db                                                 # noqa: E402
from app import assistant                                           # noqa: E402
from app import daily_review as dr                                  # noqa: E402
from tests.test_daily_review import _Base, _FakeClient              # noqa: E402


def _write_wav(src, dst):
    with open(dst, "wb") as fh:
        fh.write(b"x")


class _ApiBase(_Base):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(api_mod.router)
        cls.client = TestClient(app)

    def _patch_client(self, client):
        self.addCleanup(setattr, dr, "_client", dr._client)
        dr._client = lambda: client

    def _rows(self):
        return db._query("SELECT * FROM commands ORDER BY id ASC")

    @staticmethod
    def _write(path, data=b"x"):
        with open(path, "wb") as fh:
            fh.write(data)

    def _post(self, body=b"RIFF....WAVEfmt "):
        """打一轮 turn，并把"解码音频"和"转写"换成替身（不碰真 ffmpeg / 模型）。"""
        def _fake_decode(src, dst):
            self._write(dst, b"decoded-wav")

        self._transcribe = mock.patch.object(
            assistant, "_transcribe_command",
            lambda *a, **k: ("今天把回顾改成走回顾接口了", "fake-stt", assistant.ASR_OK))
        self._transcribe.start()
        self.addCleanup(self._transcribe.stop)
        dec = mock.patch.object(api_mod, "_audio_to_wav16k", side_effect=_fake_decode)
        dec.start()
        self.addCleanup(dec.stop)
        return self.client.post(
            "/api/daily-review/turn", content=body,
            headers={"Content-Type": "audio/wav"})


class TurnGoesThroughReviewTests(_ApiBase):
    def test_a_voice_turn_lands_in_review_history_and_never_in_commands_as_mobile(self):
        fake = _FakeClient(reply="【播报】记好了，还有吗？【/播报】")
        self._patch_client(fake)
        sent = []
        with mock.patch.object(assistant, "send_text",
                               side_effect=lambda *a, **k: sent.append((a, k)) or (True, "x")):
            r = self._post()

        self.assertEqual(200, r.status_code, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual("今天把回顾改成走回顾接口了", body["text"])
        self.assertIn("记好了", body["spoken"])
        self.assertGreater(body["seconds"], 0.0)

        # ① **绝不**走命令链（那正是"命令发到指令空间"的入口）
        self.assertEqual([], sent, "回顾轮次又走了 assistant.send_text（= 指令空间）")
        # ② 落库只有一条 review 行，没有 mobile 行
        rows = self._rows()
        self.assertEqual(1, len(rows), rows)
        self.assertEqual(dr.REVIEW_SOURCE, rows[0]["source"])
        self.assertEqual("今天把回顾改成走回顾接口了", rows[0]["text"])
        self.assertEqual("done", rows[0]["status"])
        self.assertIn("记好了", rows[0]["brief"])
        self.assertEqual(0, db._query(
            "SELECT COUNT(*) AS n FROM commands WHERE source='mobile'")[0]["n"])
        # ③ 回顾历史里看得到（不是"指令历史"）
        self.assertEqual(1, dr.history_summary()["todayTurns"])
        self.assertEqual(1, dr.history_list()["items"][0]["turns"])
        detail = dr.history_detail(time.strftime("%Y-%m-%d"))
        self.assertEqual(1, detail["count"])
        self.assertEqual("今天把回顾改成走回顾接口了", detail["turns"][0]["text"])

    def test_it_reports_the_broadcast_from_submit(self):
        fake = _FakeClient(reply="【播报】这一轮只念这句。【/播报】")
        self._patch_client(fake)
        body = self._post().json()
        # `spoken` 是 submit 的三层兜底结果；没写工作日志时服务端会**照实**补一句
        # （那是回顾本来就有的口径，不属于 turn 这一层）。
        self.assertTrue(body["spoken"].startswith("这一轮只念这句。"), body["spoken"])
        self.assertEqual("broadcast", body["source"])

    def test_no_speech_does_not_create_a_review_row_and_says_so(self):
        self._patch_client(_FakeClient())
        with mock.patch.object(assistant, "_transcribe_command",
                               lambda *a, **k: ("", "engine=sensevoice status=empty",
                                                assistant.ASR_EMPTY)), \
             mock.patch.object(api_mod, "_audio_to_wav16k", _write_wav):
            r = self.client.post(
                "/api/daily-review/turn", content=b"RIFF....",
                headers={"Content-Type": "audio/wav"})

        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual("", body["text"])
        self.assertIn("没听清", body["spoken"], "没听到内容时要说人话，不能静默")
        self.assertEqual([], self._rows(), "没听清不是一轮，不许落库")

    def test_engine_failure_is_not_reported_as_i_did_not_hear_you(self):
        self._patch_client(_FakeClient())
        note = "engine=sensevoice status=error detail=ModuleNotFoundError: No module named 'sherpa_onnx'"
        with mock.patch.object(assistant, "_transcribe_command",
                               lambda *a, **k: ("", note, assistant.ASR_ERROR)), \
             mock.patch.object(api_mod, "_audio_to_wav16k", _write_wav):
            body = self.client.post(
                "/api/daily-review/turn", content=b"RIFF....",
                headers={"Content-Type": "audio/wav"}).json()

        self.assertFalse(body["ok"])
        self.assertIn("转写引擎没装", body["spoken"],
                      "引擎坏了说成'没听清'会让人反复重说：%s" % body["spoken"])
        self.assertNotIn("我没听清", body["spoken"])
        self.assertEqual([], self._rows())

    def test_request_shape_errors_are_4xx_not_a_failed_review_turn(self):
        # 空 body
        r = self.client.post("/api/daily-review/turn", content=b"",
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(422, r.status_code)
        # 不是音频（解码失败）
        with mock.patch.object(api_mod, "_audio_to_wav16k",
                               side_effect=RuntimeError("Format not recognised")):
            r = self.client.post("/api/daily-review/turn", content=b"not-audio",
                                 headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(422, r.status_code)
        self.assertIn("音频解码失败", r.json()["detail"])
        self.assertEqual([], self._rows())

    def test_a_broken_transcriber_is_reported_not_500(self):
        self._patch_client(_FakeClient())
        with mock.patch.object(assistant, "_transcribe_command",
                               side_effect=RuntimeError("模型炸了")), \
             mock.patch.object(api_mod, "_audio_to_wav16k", _write_wav):
            r = self.client.post("/api/daily-review/turn", content=b"RIFF....",
                                 headers={"Content-Type": "audio/wav"})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("模型炸了", body["error"])
        self.assertTrue(body["spoken"])


if __name__ == "__main__":
    unittest.main()
