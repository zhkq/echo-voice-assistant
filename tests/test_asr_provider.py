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
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import meeting                                      # noqa: E402
from app.audio import stt                                    # noqa: E402


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
        # 也会走它。所以这里必须把**本机分离**与**落库的说话人**都换成替身 ——
        # 不打桩会去加载本机 pyannote；而 `db.replace_speakers` 没打桩的话，
        # 这一组"用一个假 meeting_id=1"的用例会往**真实库**里写说话人行。
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
            patch("app.audio.diarize.diarize_wav_full",
                  lambda path, max_speakers=None: ([(0.0, 20.0, "SPEAKER_00")],
                                                   [[0.1] * 256], ["SPEAKER_00"])),
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
        with patch.object(meeting.stt_mod, "_get_whisper",
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
    """命令口述转写也走 provider（P5）：判据与会议转写**同一份**（providers.asr_if_configured）。

    2026-09-23 事故后这里多钉一件事：`_transcribe_command()` 的第三个返回值
    （ok/empty/error）必须把"引擎挂了"与"这段没人说话"分开 —— 稳定版没装 sherpa_onnx 时
    命令转写每次都抛 ModuleNotFoundError，可日志里只有一句「转写为空（engine=sherpa）」，
    于是被读成"没听清"，真正的根因一个字都没露。
    """

    def setUp(self):
        self.logs = []
        from app import assistant
        self.assistant = assistant
        p = patch.object(assistant.db, "add_log",
                         lambda level, source, msg: self.logs.append((level, msg)))
        p.start()
        self.addCleanup(p.stop)

    def test_uses_provider_when_configured(self):
        fake = _FakeAsr(text="打开浏览器")
        with patch("app.providers.asr_if_configured", lambda: (fake, "openai-asr")), \
                patch.object(self.assistant.stt_mod, "transcribe_ex",
                             side_effect=AssertionError("配了 provider 就不该调本地引擎")):
            text, note, status = self.assistant._transcribe_command("a.wav", {"sttLanguage": "zh"})
        self.assertEqual(text, "打开浏览器")
        self.assertEqual(status, self.assistant.ASR_OK)
        self.assertIn("provider=openai-asr", note)
        self.assertEqual(fake.calls[0][1], "zh")

    def test_provider_error_returns_note_not_exception(self):
        fake = _FakeAsr(error=RuntimeError("openai-asr: HTTP 401 密钥无效"))
        with patch("app.providers.asr_if_configured", lambda: (fake, "openai-asr")):
            text, note, status = self.assistant._transcribe_command("a.wav", {})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_ERROR)
        self.assertIn("401", note, "失败原因要能带到日志里")

    def test_provider_empty_result_keeps_the_reason(self):
        fake = _FakeAsr(text="", reason="empty-or-unknown")
        with patch("app.providers.asr_if_configured", lambda: (fake, "openai-asr")):
            text, note, status = self.assistant._transcribe_command("a.wav", {})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_EMPTY, "provider 没报错就是 empty，不能算 error")
        self.assertIn("reason=empty-or-unknown", note)

    def test_local_path_used_when_not_configured(self):
        seen = {}

        def fake_transcribe(wav, engine, model, lang, device):
            seen.update(engine=engine, model=model, lang=lang, device=device)
            return {"text": "  你好   世界 ", "status": stt.TRANSCRIBE_OK, "detail": ""}

        with patch("app.providers.asr_if_configured", lambda: (None, "未配置 providerAsr（用本地引擎）")), \
                patch.object(self.assistant.stt_mod, "transcribe_ex", fake_transcribe):
            text, note, status = self.assistant._transcribe_command(
                "a.wav", {"sttModel": "sensevoice", "device": "cuda", "sttLanguage": "zh"})
        self.assertEqual(text, "你好 世界", "空白要归一化（老行为）")
        self.assertEqual(status, self.assistant.ASR_OK)
        self.assertEqual(seen["engine"], "sensevoice")
        self.assertEqual(seen["device"], "cuda")
        self.assertIn("engine=sensevoice", note)

    def test_local_engine_failure_is_also_reported(self):
        """本地引擎抛异常 → error（且原因进 note）—— 不许再被当成"没人说话"。"""
        with patch("app.providers.asr_if_configured", lambda: (None, "未配置")), \
                patch.object(self.assistant.stt_mod, "transcribe_ex",
                             side_effect=RuntimeError("CUDA out of memory")):
            text, note, status = self.assistant._transcribe_command("a.wav", {"sttModel": "whisper"})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_ERROR)
        self.assertIn("CUDA out of memory", note)

    def test_missing_dependency_is_error_not_empty(self):
        """复现 2026-09-23 事故：引擎报"没有这个模块"必须是 error，并把原因带出来。"""
        def boom(wav, engine, model, lang, device):
            return {"text": "", "status": stt.TRANSCRIBE_ERROR,
                    "detail": "sherpa: No module named 'sherpa_onnx'"}

        with patch("app.providers.asr_if_configured", lambda: (None, "未配置")), \
                patch.object(self.assistant.stt_mod, "transcribe_ex", boom):
            text, note, status = self.assistant._transcribe_command("a.wav", {"sttModel": "sherpa"})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_ERROR)
        self.assertIn("status=error", note)
        self.assertIn("sherpa_onnx", note)
        hint = self.assistant.asr_failure_hint(note)
        self.assertIn("sherpa-onnx", hint, "给用户的话要说出缺哪个包（模块名对用户没用）")
        self.assertIn("能力", hint, "还要告诉他在面板哪儿装")

    def test_silence_is_empty_not_error(self):
        """引擎正常但没内容 → empty：这时才该说"没听清"。"""
        with patch("app.providers.asr_if_configured", lambda: (None, "未配置")), \
                patch.object(self.assistant.stt_mod, "transcribe_ex",
                             lambda *a, **kw: {"text": "", "status": stt.TRANSCRIBE_EMPTY,
                                               "detail": "whisper 返回空结果"}):
            text, note, status = self.assistant._transcribe_command("a.wav", {"sttModel": "whisper"})
        self.assertEqual(text, "")
        self.assertEqual(status, self.assistant.ASR_EMPTY)
        self.assertIn("status=empty", note)


if __name__ == "__main__":
    unittest.main()
