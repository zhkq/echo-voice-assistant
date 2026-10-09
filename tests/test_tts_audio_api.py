# -*- coding: utf-8 -*-
"""手机 / 手表触点的两条 ECHO 侧接口（设计 §3「新增 2 / 新增 3」）

  * `POST /api/tts` —— 合成**音频字节**（**不**在本机喇叭播）；
  * `POST /api/assistant/voice-command` —— 裸 wav → 一句话（转写 + 下发，一步到位）。

钉六条判据（都是设计里明写的，每条都能单独判"坏没坏"）：

  1. `/api/tts` 回来的字节能被 `soundfile` 解码出**非空**波形
     —— "接口 200 但其实没声音"是手机侧最坏的失败形状；
  2. `ttsEngine=off` → **409**，与"合成失败"**分开**（"我关了朗读"不能被显示成"坏了"）；
  3. 合成失败 → 5xx **且留痕**（`warn/tts`）—— 否则手机端"没声音"又是无头案；
  4. 文本为空 → 422；
  5. `voice-command` 收**裸 body**（不是 multipart），回 `text` + `commandId`，`source=mobile`；
  6. 识别不出文本 → 422（不是"成功但空"）。

隔离：DB/设置缓存指向临时目录；**不联网、不真合成**（`synthesize` / `transcribe` / 在线探针都打桩）；
`/api/tts` 的离线分支由"接缝写一个真 wav"驱动 —— 这样连 SAPI 都不用真跑。
"""
import io
import os
import struct
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                              # noqa: E402
from app.audio import tts as tts_mod                             # noqa: E402
from app.config import settings                                  # noqa: E402


def _wav_bytes(seconds=0.3, sr=16000):
    """一段真 wav（16k 单声道 PCM）：给"能解码出非空波形"当判据用。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(sr * seconds)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * ((i % 40) - 20) / 20)) for i in range(n)))
    return buf.getvalue()


class _ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-tts-")
        cls._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()
        settings._cache = None
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        settings._cache = None
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        settings.update({"ttsEngine": "auto"})
        settings._cache = None


# ================================================================ /api/tts
class TtsEndpointTests(_ApiCase):
    def test_it_returns_audio_that_decodes_to_a_non_empty_waveform(self):
        """判据 1：**能解码出非空波形**（这是"手机能听到声音"的唯一硬判据）。"""
        import soundfile as sf
        wav = _wav_bytes()
        with patch.object(tts_mod, "synthesize", lambda text, engine="auto", timeout=60: ("audio/wav", wav)):
            r = self.client.post("/api/tts", json={"text": "明天天气怎么样"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers.get("content-type", "").startswith("audio/wav"), r.headers)
        self.assertGreater(len(r.content), 44, "回来的音频只有 wav 头那么大 = 等于没合成")
        data, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        self.assertGreater(data.size, 0, "解出来是空波形")
        self.assertEqual(sr, 16000)

    def test_off_engine_is_409_and_not_a_failure(self):
        """判据 2：**关掉朗读**（409）与**合成失败**（5xx）必须分得开。"""
        settings.update({"ttsEngine": "off"})
        r = self.client.post("/api/tts", json={"text": "你好"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("off", r.json()["detail"])

    def test_a_failure_is_5xx_and_leaves_a_trace(self):
        """判据 3：失败要**留痕**（`warn/tts`），否则"没声音"查无实据。"""
        before = len([x for x in db.list_logs(limit=200, level="warn", source="tts")])

        def _boom(text, engine="auto", timeout=60):
            raise tts_mod.TtsError("模拟：引擎返回空")

        with patch.object(tts_mod, "synthesize", _boom):
            r = self.client.post("/api/tts", json={"text": "你好"})
        self.assertGreaterEqual(r.status_code, 500, r.text)
        self.assertIn("模拟", r.json()["detail"])
        after = db.list_logs(limit=200, level="warn", source="tts")
        self.assertGreater(len(after), before, "合成失败没有写 warn/tts 日志")
        self.assertTrue(any("模拟" in str(x.get("message", "")) for x in after),
                        "日志里没带上真正的原因")

    def test_empty_text_is_422(self):
        r = self.client.post("/api/tts", json={"text": "   "})
        self.assertEqual(r.status_code, 422, r.text)


class SynthesizeUnitTests(_ApiCase):
    """`tts.synthesize` 本身（不经 HTTP）：两条引擎分支各一条判据。"""

    def test_off_raises_the_dedicated_exception(self):
        with self.assertRaises(tts_mod.TtsOff):
            tts_mod.synthesize("你好", engine="off")

    def test_the_offline_branch_writes_bytes_through_the_platform_seam(self):
        """离线分支：接缝"写文件"→ 我们读成字节（**不真跑 SAPI**）。"""
        wav = _wav_bytes()

        def _render(text, path, timeout=60):
            with open(path, "wb") as fh:
                fh.write(wav)
            return True

        with patch.object(tts_mod.echo_platform, "offline_tts_render", _render), \
                patch.object(tts_mod, "probe_online", lambda force=False: False):
            mime, data = tts_mod.synthesize("你好", engine="auto")
        self.assertEqual(mime, "audio/wav")
        self.assertEqual(data, wav)

    def test_the_offline_branch_that_produces_nothing_raises_with_the_reason(self):
        with patch.object(tts_mod.echo_platform, "offline_tts_render", lambda *a, **k: False), \
                patch.object(tts_mod, "probe_online", lambda force=False: False):
            with self.assertRaises(tts_mod.TtsError) as ctx:
                tts_mod.synthesize("你好", engine="auto")
        self.assertIn("离线引擎", str(ctx.exception))


# ================================================================ 语音往返
class VoiceCommandEndpointTests(_ApiCase):
    def _post(self, data=None, ctype="audio/wav"):
        return self.client.post("/api/assistant/voice-command", content=data,
                                headers={"Content-Type": ctype})

    def test_a_raw_wav_becomes_one_command_marked_mobile(self):
        """判据 5：**裸 body**（不是 multipart）→ 文本 + commandId，source=mobile。"""
        calls = {}

        def _send_text(text, source="web", workspace=None, session_id=None):
            calls["text"], calls["source"] = text, source
            return True, "已发送"

        with patch("app.audio.stt.transcribe", lambda *a, **k: "明天天气怎么样"), \
                patch("app.assistant.send_text", _send_text):
            r = self._post(_wav_bytes())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["text"], "明天天气怎么样")
        self.assertEqual(body["source"], "mobile")
        self.assertTrue(body["ok"])
        self.assertIn("commandId", body)
        self.assertEqual(calls.get("source"), "mobile", "下发给智能体时要标 mobile")

    def test_multipart_is_not_the_contract(self):
        """这条接口**只收裸 body**：multipart 应当被当成"没听懂"（422），而不是静默成功。"""
        r = self.client.post("/api/assistant/voice-command",
                             files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
        self.assertEqual(r.status_code, 422, r.text)

    def test_no_text_recognised_is_422(self):
        """判据 6：识别不出就 422（不是"成功但空"）。"""
        with patch("app.audio.stt.transcribe", lambda *a, **k: "   "):
            r = self._post(_wav_bytes())
        self.assertEqual(r.status_code, 422, r.text)

    def test_empty_body_is_422(self):
        r = self._post(b"")
        self.assertEqual(r.status_code, 422, r.text)


if __name__ == "__main__":
    unittest.main()
