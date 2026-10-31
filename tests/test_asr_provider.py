# -*- coding: utf-8 -*-
"""P5：转写链路接 `active("asr")` + `stt.transcribe_ex()` 的空结果显式化

两部分：

**A. 空结果显式化（§19 发现③的根治）**：原 `stt.transcribe()` 每个分支都
`except → print(stderr) → return ""`，于是"这段没人说话"和"引擎挂了"在调用方看来完全一样
（实测 whisper 对 23 秒静音音频无异常、无 stderr、直接返回空串）→ 可能静默产出空纪要。
`transcribe_ex()` 返回 `{"text","status","detail"}`，`transcribe()` 保持原签名不变。

**B. 转写走 provider（P5 验收点"配一个在线 ASR 即可完成一次转写"）**：
只有用户**显式**配了 `providerAsr` 才走外部/在线 provider —— 因为换引擎同时改变准确率、
耗时、费用，而且在线转写会把**整段音频**传出去，必须由用户选择（不做自动切换）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import meeting                                      # noqa: E402
from app.audio import stt                                    # noqa: E402
from app.capabilities import (                               # noqa: E402
    BACKEND_ECHO_SERVER,
    SOURCE_LAN,
    CapabilityClient,
    CapabilityRouter,
    DiarizeResult,
    Provenance,
)


class _DiarizeOnlyBackend(CapabilityClient):
    """**只做分离**的能力后端替身（转写走 provider，不归它）。

    2026-09-29：说话人那一族只落在能力后端上。这一步之前，这里靠打桩
    `app.audio.diarize.diarize_wav_full` 演"分离成功了"；那条本机路已经从会议链路上
    断掉，所以改成用一个**只声明 `diarize.turns`** 的后端替身 ——
    正好把"provider 转写 + 后端分离"这条真实组合钉住。
    """

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"diarize.turns"})
    vector_space_id = "ws-provider-spy"

    def __init__(self, turns=None):
        self.turns = list(turns or [(0.0, 20.0, "SPEAKER_00")])

    def diarize(self, wav, *, max_speakers=None, **kw):
        return DiarizeResult(turns=tuple(self.turns),
                             speakers={"SPEAKER_00": tuple([0.1] * 256)},
                             dim=256, vector_space_id=self.vector_space_id,
                             provenance=Provenance(self.backend_id, "spy-dia-v1"),
                             audio_seconds=20.0)


class TranscribeStatusTests(unittest.TestCase):
    """A 部分：状态要能区分 missing / error / empty / ok。"""

    def test_missing_file(self):
        out = stt.transcribe_ex("no-such-file.wav")
        self.assertEqual(out["status"], stt.TRANSCRIBE_MISSING)
        self.assertEqual(out["text"], "")
        self.assertIn("不存在", out["detail"])

    def test_engine_error_is_not_reported_as_empty(self):
        with patch.object(stt, "_get_whisper", side_effect=RuntimeError("CUDA out of memory")):
            out = stt.transcribe_ex(__file__, engine="whisper")
        self.assertEqual(out["status"], stt.TRANSCRIBE_ERROR)
        self.assertIn("CUDA out of memory", out["detail"])

    def test_empty_result_is_its_own_status(self):
        class _Seg:
            text = ""

        with patch.object(stt, "_get_whisper", lambda *a, **k: object()), \
                patch.object(stt, "transcribe_whisper", lambda *a, **k: ([_Seg()], None)):
            out = stt.transcribe_ex(__file__, engine="whisper")
        self.assertEqual(out["status"], stt.TRANSCRIBE_EMPTY)
        self.assertIn("空结果", out["detail"])

    def test_ok_result_carries_text(self):
        class _Seg:
            text = " 你好 世界 "

        with patch.object(stt, "_get_whisper", lambda *a, **k: object()), \
                patch.object(stt, "transcribe_whisper", lambda *a, **k: ([_Seg()], None)):
            out = stt.transcribe_ex(__file__, engine="whisper")
        self.assertEqual(out["status"], stt.TRANSCRIBE_OK)
        self.assertEqual(out["text"], "你好 世界")

    def test_transcribe_keeps_returning_text(self):
        """兼容性：老调用方拿到的仍是字符串（不许悄悄改签名）。"""
        with patch.object(stt, "transcribe_ex",
                          lambda *a, **k: {"text": "abc", "status": "ok", "detail": ""}):
            self.assertEqual(stt.transcribe("x.wav"), "abc")


class AsrProviderSelectionTests(unittest.TestCase):
    """B 部分：选择判据 —— 只有显式配置才走 provider。"""

    def setUp(self):
        self.logs = []
        self._log = patch.object(meeting.db, "add_log",
                                 lambda level, source, msg: self.logs.append((level, msg)))
        self._log.start()
        self.addCleanup(self._log.stop)

    def test_default_is_none(self):
        with patch("app.config.settings.get", lambda k, d=None: "" if k == "providerAsr" else d):
            self.assertIsNone(meeting._active_asr_provider(),
                              "没配 providerAsr 时必须返回 None（老用户零变化）")

    def test_explicit_choice_returns_the_provider(self):
        sentinel = object()
        with patch("app.config.settings.get",
                   lambda k, d=None: "openai-asr" if k == "providerAsr" else d), \
                patch("app.providers.create", lambda kind, pid: sentinel):
            self.assertIs(meeting._active_asr_provider(), sentinel)

    def test_broken_choice_warns_and_falls_back(self):
        def boom(kind, pid):
            raise KeyError("没有这个 provider")

        with patch("app.config.settings.get",
                   lambda k, d=None: "ghost" if k == "providerAsr" else d), \
                patch("app.providers.create", boom):
            self.assertIsNone(meeting._active_asr_provider())
        self.assertTrue(any(lv == "warn" and "providerAsr" in m for lv, m in self.logs),
                        "配错了要留痕，不能静默回退：%s" % self.logs)


class SplitProviderTextTests(unittest.TestCase):
    def test_splits_on_chinese_and_latin_punctuation(self):
        rows = meeting._split_provider_text("第一句。第二句！Third? 第四句；", 12.0)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r[2] for r in rows],
                         ["第一句。", "第二句！", "Third?", "第四句；"])

    def test_times_are_proportional_and_cover_the_segment(self):
        rows = meeting._split_provider_text("短。" + "长" * 30 + "。", 11.0)
        self.assertAlmostEqual(rows[0][0], 0.0)
        self.assertAlmostEqual(rows[-1][1], 11.0, places=1)
        self.assertLess(rows[0][1] - rows[0][0], rows[-1][1] - rows[-1][0],
                        "字的句子应该占更长时间（按字数均摊）")

    def test_no_punctuation_gives_one_row(self):
        rows = meeting._split_provider_text("no punctuation at all", 7.5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (0.0, 7.5, "no punctuation at all"))

    def test_empty_text_gives_no_rows(self):
        self.assertEqual(meeting._split_provider_text("   ", 10.0), [])


class _FakeAsr:
    def __init__(self, text="第一句。第二句。", reason=None, error=None):
        self.text = text
        self.reason = reason
        self.error = error
        self.calls = []

    def transcribe(self, wav_path, lang="zh", **kw):
        self.calls.append((wav_path, lang))
        if self.error:
            raise self.error
        out = {"text": self.text, "engine": "fake", "model": "", "sentences": []}
        if not self.text:
            out["reason"] = self.reason or "empty-or-unknown"
        return out


class TranscribeImplWiringTests(unittest.TestCase):
    """集成：`_transcribe_impl` 在配了 provider 时**不加载本地引擎**并落库 provider 文本。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-asr-prov-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.folder = os.path.join(self.tmp, "2026-09-19_10-00-00")
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "01.wav"), "wb") as fh:
            fh.write(b"RIFF....WAVE")
        with open(os.path.join(self.folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"segments": ["01.wav"], "config": {}}, fh)
        self.lines = []
        self.logs = []
        self.updates = []
        self.speakers = []
        # 2026-09-26：说话人分离是会议的**必备环节**（不再是开关），provider 那条路
        # 也会走它。2026-09-29 起它**只走能力后端** —— 所以下面把本机分离打桩成
        # "调用即红"（那条路已经不该参与会议链路），实际的说话人由
        # `_DiarizeOnlyBackend` 出（见 `test_provider_text_lands_in_the_db_...`）。
        # `db.replace_speakers` 必须打桩：这一组用假 meeting_id=1，不然会往**真实库**里写。
        patches = [
            patch.object(meeting.db, "get_meeting_by_name",
                         lambda name: {"id": 1, "name": name}),
            patch.object(meeting.db, "clear_meeting_lines", lambda mid: None),
            patch.object(meeting.db, "update_meeting",
                         lambda mid, **kw: self.updates.append(kw)),
            patch.object(meeting.db, "add_lines",
                         lambda mid, rows: self.lines.extend(rows)),
            patch.object(meeting.db, "cleanup_empty_speakers", lambda mid: None),
            patch.object(meeting.db, "replace_speakers",
                         lambda mid, names: self.speakers.append(names)),
            patch.object(meeting.db, "replace_speaker_embeddings",
                         lambda mid, m: None),
            patch.object(meeting.db, "add_log",
                         lambda level, source, msg: self.logs.append((level, msg))),
            patch.object(meeting, "export_transcript", lambda *a, **k: None),
            patch.object(meeting, "_set_progress", lambda *a, **k: None),
            patch.object(meeting, "_active_asr_provider", self._provider),
            patch.object(meeting, "_asr_provider_id", lambda: "openai-asr"),
            patch.object(meeting, "_wav_seconds", lambda path: 20.0),
            # 本机分离一次都不许被调到（客户端进程内已经不承担会议分离）
            patch("app.audio.diarize.diarize_wav_full",
                  MagicMock(side_effect=AssertionError("分离不该回落本机"))),
            patch.object(meeting.settings, "get",
                         lambda k, d=None: {"meetingSttModel": "sensevoice",
                                            "meetingAutoSummarize": False}.get(k, d)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _provider(self):
        return self.fake

    def test_provider_text_lands_in_the_db_without_loading_local_engines(self):
        self.fake = _FakeAsr()
        # 分离那一槽只落在能力后端上（2026-09-29）：把**路由**换成"只有这个后端"，
        # 并把 `configured()` 钉成真。`providerAsr` 那条路不经过能力层的 `asr.text`，
        # 所以这一场是"provider 转写 + 后端分离"的真实组合。
        from app.capabilities import echo_server
        router = CapabilityRouter([_DiarizeOnlyBackend()], settings_get=lambda k, d=None: d)
        with patch("app.capabilities.build_default_router", lambda **kw: router), \
             patch.object(echo_server, "configured", lambda: True), \
             patch.object(meeting.stt_mod, "_get_whisper",
                          side_effect=AssertionError("配了 provider 时不该加载本地模型")):
            meeting._transcribe_impl(self.folder)
        self.assertEqual(len(self.fake.calls), 1, "每段调一次 provider")
        # db_rows 是 5 元组 (seg_index, start, end, speaker_label, text)（meeting.py:641 转换）
        self.assertEqual([r[4] for r in self.lines], ["第一句。", "第二句。"])
        # 2026-09-26：分离是会议的必备环节 —— provider 转写那条路**同样**会标说话人
        # （"要不要分离"已经不是用户设置；谁做由路由决定）。
        self.assertEqual(self.lines[0][3], "S1",
                         "会议转写必须标说话人（分离是标配，不是开关）")
        self.assertEqual(self.speakers, [{"S1": "说话人1"}],
                         "本场说话人的显示名要落库")
        self.assertEqual(self.lines[0][0], 1)            # seg_index
        self.assertAlmostEqual(self.lines[-1][2], 20.0, places=1)   # 时间铺满本段
        self.assertTrue(any("provider" in m for _lv, m in self.logs))
        self.assertTrue(any(u.get("status") == "transcribed" for u in self.updates))

    def test_provider_failure_is_logged_and_does_not_crash_the_meeting(self):
        self.fake = _FakeAsr(error=RuntimeError("openai-asr: HTTP 401 密钥无效"))
        meeting._transcribe_impl(self.folder)
        self.assertEqual(self.lines, [])
        self.assertTrue(any(lv == "error" and "401" in m for lv, m in self.logs),
                        "provider 失败必须留痕：%s" % self.logs)

    def test_empty_provider_result_is_logged_as_warning(self):
        self.fake = _FakeAsr(text="", reason="empty-or-unknown")
        meeting._transcribe_impl(self.folder)
        self.assertEqual(self.lines, [])
        self.assertTrue(any(lv == "warn" and "为空" in m for lv, m in self.logs),
                        "空结果要与'引擎挂了'区分开且留痕：%s" % self.logs)


class CommandTranscribeTests(unittest.TestCase):
    """2026-10-31：命令/回顾转写只走能力后端（客户端已无本地 STT）。"""

    def setUp(self):
        from app import assistant
        self.assistant = assistant
        self.logs = []
        p = patch.object(assistant.db, "add_log",
                         lambda level, source, msg: self.logs.append((level, msg)))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _ok(text):
        return {"text": text, "status": "ok",
                "detail": "backend=echo-server variant=short"}

    def test_command_goes_to_backend_with_short_variant(self):
        seen = {}

        def fake(wav, cfg, lang="auto", *, variant="long", timeout=None):
            seen.update(lang=lang, variant=variant, timeout=timeout)
            return self._ok("打开浏览器")

        with patch.object(self.assistant, "_transcribe_via_backend", fake), \
                patch.object(self.assistant.stt_mod, "transcribe_ex",
                             side_effect=AssertionError("不该再有本地转写")):
            text, note, status = self.assistant._transcribe_command(
                "a.wav", {"sttLanguage": "zh"})
        self.assertEqual(text, "打开浏览器")
        self.assertEqual(status, self.assistant.ASR_OK)
        self.assertEqual(seen["variant"], "short")
        self.assertIn("backend=echo-server", note)

    def test_review_goes_to_backend_with_long_variant(self):
        seen = {}

        def fake(wav, cfg, lang="auto", *, variant="long", timeout=None):
            seen.update(variant=variant, timeout=timeout)
            return {"text": "一段口述", "status": "ok", "detail": "backend"}

        with patch.object(self.assistant, "_transcribe_via_backend", fake):
            text, _note, status = self.assistant._transcribe_command(
                "a.wav", {}, variant="long")
        self.assertEqual(text, "一段口述")
        self.assertEqual(seen["variant"], "long")
        self.assertEqual(status, self.assistant.ASR_OK)

    def test_backend_busy_is_its_own_status(self):
        def fake(wav, cfg, lang="auto", *, variant="long", timeout=None):
            return {"text": "", "status": self.assistant.ASR_BUSY,
                    "detail": "能力后端忙（code=client_busy）：上一条还没跑完"}

        with patch.object(self.assistant, "_transcribe_via_backend", fake):
            text, note, status = self.assistant._transcribe_command("a.wav", {})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_BUSY)
        self.assertIn("client_busy", note)
        self.assertIn("忙", self.assistant.asr_failure_hint(note))

    def test_no_local_fallback_when_backend_fails(self):
        with patch.object(self.assistant, "_transcribe_via_backend",
                          lambda *a, **kw: {"text": "",
                                            "status": self.assistant.ASR_ERROR,
                                            "detail": "能力后端没配对"}), \
                patch.object(self.assistant.stt_mod, "transcribe_ex",
                             side_effect=AssertionError("客户端已无本地 STT")):
            text, note, status = self.assistant._transcribe_command("a.wav", {})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_ERROR)
        self.assertIn("没配对", note)

    def test_unavailable_hint_tells_user_to_check_backend(self):
        hint = self.assistant.asr_failure_hint("能力后端没配对、也没在设置里填地址")
        self.assertIn("后端", hint)


class ReviewUsesTheMeetingEngineTests(unittest.TestCase):
    """2026-10-31：回顾恒走能力后端；本机选项/回落已删除。"""

    def test_every_value_maps_to_backend(self):
        from app import assistant
        for v in ("local", "echo-server", "", None, "whatever"):
            with self.subTest(v=v):
                self.assertEqual(
                    assistant.review_stt_prefer({"dailyReviewSttBackend": v}),
                    "backend")

    def test_review_worker_passes_long_variant(self):
        import io
        src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "app", "assistant.py"), encoding="utf-8").read()
        self.assertIn('_transcribe_command(wav, cfg, variant="long")', src,
                      "回顾那一步没有用 long 档")
        self.assertIn("def _transcribe_command", src)
        body = src.split("def _transcribe_command", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("transcribe_ex", body, "命令转写里不许再调本地引擎")

    def test_default_setting_is_echo_server(self):
        from app.config import DEFAULTS
        self.assertEqual(DEFAULTS["dailyReviewSttBackend"]["value"], "echo-server")


if __name__ == "__main__":
    unittest.main()
