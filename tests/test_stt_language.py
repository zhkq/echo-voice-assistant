# -*- coding: utf-8 -*-
"""转写语言的收口 + whisper 单一入口（2026-09-15 issue #2 的回归测试）。

背景
----
1. `sttLanguage` 是给"命令与会议"共用的自由文本设置，而 **faster-whisper 只认 ISO-639-1
   小写码**（zh/en/ja…）：填 `Chinese` / `ZH` / `auto` 都会抛 `ValueError`，调用方又把异常
   吞成"转写结果为空"——界面上只看到"没文字"，很难查。
2. `app/meeting.py` 曾经自己复制了一份 `wmodel.transcribe(...)` 参数，**漏了
   `initial_prompt`**，于是会议转写（作者在 macOS 上用 Whisper）比命令转写更容易出繁体字。

所以这里钉两件事：语言值必须被规范化/回退；whisper 只能有一个调用入口。
"""
import os
import unittest

import app.audio.stt as stt


class NormalizeLangTests(unittest.TestCase):
    def test_iso_codes_pass_through(self):
        for value in ("zh", "en", "ja", "ko", "yue", "de", "fr"):
            self.assertEqual(stt.normalize_lang(value), value)

    def test_full_names_and_case_are_mapped(self):
        self.assertEqual(stt.normalize_lang("Chinese"), "zh")
        self.assertEqual(stt.normalize_lang("ZH"), "zh")
        self.assertEqual(stt.normalize_lang("English"), "en")
        self.assertEqual(stt.normalize_lang("  ja  "), "ja")

    def test_auto_and_empty_mean_detect(self):
        """whisper 的"自动识别"是 language=None，写 'auto' 会被它拒收。"""
        for value in ("auto", "AUTO", "", None, "none"):
            self.assertIsNone(stt.normalize_lang(value))

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(stt.normalize_lang("中文（普通话）"), "zh")
        self.assertEqual(stt.normalize_lang("klingon-ish"), "zh")
        self.assertEqual(stt.normalize_lang("x" * 10, default="en"), "en")

    def test_funasr_map_uses_full_names(self):
        self.assertEqual(stt._LANG_MAP.get("zh"), "Chinese")
        self.assertEqual(stt._LANG_MAP.get("chinese"), "Chinese")
        self.assertEqual(stt._LANG_MAP.get("ja"), "Japanese")


class WhisperSingleEntryTests(unittest.TestCase):
    class _FakeModel:
        def __init__(self):
            self.kwargs = None

        def transcribe(self, wav, **kwargs):  # noqa: ARG002 - 模仿 faster-whisper 的签名
            self.kwargs = kwargs
            return [], None

    def test_helper_always_passes_simplified_prompt(self):
        model = self._FakeModel()
        stt.transcribe_whisper(model, "x.wav", "Chinese")
        self.assertEqual(model.kwargs["language"], "zh")
        self.assertTrue(model.kwargs["vad_filter"])
        self.assertEqual(model.kwargs["beam_size"], 5)
        self.assertIn("普通话", model.kwargs["initial_prompt"])

    def test_auto_language_becomes_none(self):
        model = self._FakeModel()
        stt.transcribe_whisper(model, "x.wav", "auto")
        self.assertIsNone(model.kwargs["language"])

    def test_meeting_module_uses_the_single_entry(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "meeting.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn(
            "wmodel.transcribe(", src,
            "会议转写必须走 stt.transcribe_whisper()：自己拼参数会漏 initial_prompt（繁体字成因）")


if __name__ == "__main__":
    unittest.main()
