# -*- coding: utf-8 -*-
"""会议链路的**本机引擎分派**：驱动不了的引擎必须当场响亮失败，支持的引擎一个都不能坏。

## 这一组用例钉的是哪次故障（2026-09-25 用户报的）

一台干净装机、默认档（只装了 sherpa）录一场会：电平有波动（麦克风没问题），结束后
卡片显示 ``0 · 1 段``、状态 ``error``，面板上写着"没录到音频：麦克风没打开（被占用/权限）
或全程无声" —— **而真实原因与麦克风毫无关系**：

  * `_transcribe_impl` 里原来是
    ``use_sv = cfg["sttModel"] in ("sensevoice","qwen3asr")`` … ``else:
    _get_whisper(cfg["sttModel"])``，于是 ``meetingSttModel=sherpa`` 走到
    "**拿引擎名当 whisper 模型名去加载**"（`WHISPER_MODELS` 里没有 sherpa）→ 每段 0 行；
  * 而失败时只写 ``status="error"``，原因一个字都不落库 → 面板只能自己编一句
    "麦克风没打开"。

所以这里钉四件事：

  1. **驱动不了的引擎**：配置一个会议链路不支持的取值 → 会议在**没有加载任何模型**的
     前提下失败，落库的原因里含**引擎名**与**该怎么办**（不是"录音失败"这种空话）；
     并断言**不会**去调 `_get_whisper(<引擎名>, …)`（那条被修掉的路径）。
  2. **sherpa 现在真的能用**（这次一并接上了）：`_get_sherpa` 被调用、
     `_get_whisper` **一次都不被调用**，逐句行按字数均摊、档位 `estimated`，不崩。
  3. **whisper / sensevoice / qwen3asr 的分派逐字不变**（老装机不许有回归）：
     谁去加载、用什么参数，一条一条断言。
  4. 失败原因**落到 `meetings.error`**（面板要读的那个字段；接口契约在
     `tests/test_api_contract.py` 里另外钉）。

隔离（与 `tests/test_meeting_capability.py` / `tests/test_meeting*.py` 同一套写法）：
`db.DATA_DIR` / `db.DB_FILE` / `settings._cache` / 凭据文件路径全部指向临时目录，
**一个真模型都不加载、一只麦克风都不开**（四个加载器全是替身）。
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from collections import namedtuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                            # noqa: E402
from app.capabilities import credentials as cred_mod           # noqa: E402
from app.config import settings                                # noqa: E402
from app.capabilities.assemble import TIMESTAMPS_ESTIMATED     # noqa: E402

import app.meeting as meeting                                  # noqa: E402

#: whisper 的假 segment（真实现是 faster_whisper 的 Segment，字段同名）。
_WSeg = namedtuple("_WSeg", "start end text")

#: 会议文件夹名（也是 `meetings.name`）。
_NAME = "2026-09-25_10-00-00"


def _write_wav(path, seconds=1.0):
    """写一段**真的** wav（16k 单声道 int16，与 recorder.py 的产出同格式）。

    为什么不用 `b"RIFF....WAVE"` 那种假文件：`_sherpa_rows` 要拿 `_wav_seconds()` 去摊时间，
    假文件会把它变成 0 秒 → 均摊时长退化成 `seg_min*60`，用例就验不到"按本段时长摊"。
    """
    n = int(16000 * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


class _MeetingCase(unittest.TestCase):
    """一场"会议"（目录 + meta.json + 库记录 + 一段真 wav），外加全套隔离。

    **隔离放在 `setUpClass`**（与 `tests/test_api_contract.py` 的 `_IsolatedDb` 同一套）：
    `db.init()` + `settings.seed_defaults()` 是 1.6 秒量级的活（106 次单独提交），
    每个用例重来一遍会让这个文件白白多花二十几秒，而"临时库"这件事本来就是**按类**成立的。
    每个用例各自造**不同名字**的会议，因此类级共用一个库是安全的。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-mtg-engine-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "engine.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()

        # 凭据文件指向临时目录：否则这台**真配对过**的开发机会"凭空"多出一个
        # ECHO 后端，"本场走能力后端"的用例会当场变红，而报的现象像是代码坏了
        # （`tests/test_meeting_capability.py` 的 `_IsolatedState` 就是为这个加的）。
        cls._cred = os.path.join(cls.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: cls._cred)
        p.start()
        cls.addClassCleanup(p.stop)

        # 会议目录也指向临时目录：`meeting.meeting_meta()`（用例要读 meta.json 里的档位）
        # 走的是 `meetings_dir()`，不指过来的话它会去读**这台机器真实**的会议目录。
        cls.meetings_root = os.path.join(cls.tmp, "meetings")
        p = patch.object(meeting, "meetings_dir", lambda: cls.meetings_root)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.logs = []
        p = patch.object(meeting.db, "add_log",
                         lambda level, src, msg: self.logs.append((level, msg)))
        p.start()
        self.addCleanup(p.stop)

        # 启动页/提示音与转写结果无关，一律哑掉（用例**不许**出声）。
        for target, kw in ((meeting, "_boot_meeting_stt"), (meeting, "_boot_note_meeting_key"),
                           (meeting.tts_mod, "play_beep"), (meeting.tts_mod, "beep_ok")):
            p = patch.object(target, kw, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)

        settings.update({"meetingAutoSummarize": False, "meetingDiarize": False})
        self.name = "%s__%s" % (_NAME, self._testMethodName)
        self.folder = self._make_meeting(self.name)

    def _make_meeting(self, name, segs=("01.wav",), seconds=1.0):
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        for seg in segs:
            _write_wav(os.path.join(folder, seg), seconds)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"segments": list(segs), "config": {"sttModel": "sensevoice"},
                       "durationSeconds": seconds * len(segs)}, fh)
        self.mid = db.create_meeting(name, started_at="2026-09-25T10:00:00",
                                     stt_model="sensevoice", stt_device="auto")
        return folder

    # ---- 替身 ----------------------------------------------------------

    def _stub_loaders(self):
        """四个本机引擎加载器全换成替身（**一个真模型都不许加载**）。"""
        self.loaders = {}
        for attr in ("_get_whisper", "_get_sensevoice", "_get_qwen3asr", "_get_sherpa"):
            mock = MagicMock(name=attr)
            p = patch.object(meeting.stt_mod, attr, mock)
            p.start()
            self.addCleanup(p.stop)
            self.loaders[attr] = mock
        return self.loaders

    def _meeting_row(self):
        return db.get_meeting(self.mid)

    def _set_engine(self, value):
        settings.update({"meetingSttModel": value})
        settings._cache = None


# ---------------------------------------------------------------- ① 解析层

class ResolveMeetingEngineTests(unittest.TestCase):
    """`meetingSttModel` → `(engine, model, problem)`：一份解析规则，四个能跑的引擎。"""

    def test_the_four_supported_values_resolve_without_a_problem(self):
        for value, want in (("sherpa", "sherpa"),
                            ("sensevoice", "sensevoice"),
                            ("qwen3asr", "qwen3asr"),
                            ("small", "whisper"),
                            ("medium", "whisper"),
                            ("large", "whisper"),
                            ("large-v3", "whisper")):
            with self.subTest(value=value):
                eng, _model, problem = meeting.resolve_meeting_engine(value)
                self.assertEqual(eng, want)
                self.assertEqual(problem, "", "支持的值不许报问题：%r" % value)

    def test_sherpa_is_not_turned_into_a_whisper_model_name(self):
        """正是被修掉的那条路径：`sherpa` 曾经被当成 whisper 的模型名。

        `resolve_engine()` 认得出它是**引擎名**，所以这里必须给出 `sherpa`，
        绝不能是 `("whisper", "sherpa")`（后者就是"拿引擎名当模型名加载"）。
        """
        eng, model, problem = meeting.resolve_meeting_engine("sherpa")
        self.assertEqual((eng, model, problem), ("sherpa", "", ""))

    def test_an_unknown_value_is_refused_instead_of_becoming_a_whisper_model(self):
        """`resolve_engine()` 对认不出的值**一律回退成 whisper** —— 所以必须再校验一次。

        不校验的话 `meetingSttModel=paraformer` 会变成"加载 whisper 模型 paraformer"，
        又回到"静默 0 行"那条路。这里断言它被解析层拦下（而不是变成一个模型名）。
        """
        for value in ("paraformer", "medium.en", "funasr", "whisper-large"):
            with self.subTest(value=value):
                eng, _model, problem = meeting.resolve_meeting_engine(value)
                self.assertEqual(eng, "", "认不出的值不许变成任何引擎：%r" % value)
                self.assertTrue(problem, "认不出的值必须带上原因：%r" % value)

    def test_the_reason_names_the_engine_and_says_what_to_do(self):
        """报错三要素：**哪个引擎** / **支持哪些** / **下一步怎么办**。

        缺哪一条用户都会被指到错方向：只有"转写失败"不知道该换什么；只说"不支持"
        不知道该换成什么；没有下一步就只能原样重试。
        """
        why = meeting.unsupported_engine_reason("paraformer")
        self.assertIn("paraformer", why, "必须点名是哪个引擎")
        for supported in ("whisper", "sensevoice", "qwen3asr", "sherpa"):
            self.assertIn(supported, why, "必须说清会议链路支持哪些（缺 %s）" % supported)
        # 可执行的下一步：换设置值，或到「能力」页签把会议转写后端指定为 ECHO 后端
        self.assertIn("会议转写引擎", why, "要说清改哪一个设置项（给人看的名字）")
        self.assertIn("capabilityMeetingAsrBackend", why, "要说清能力页签里那一项的键")
        self.assertIn("ECHO 后端", why)

    def test_the_list_of_supported_engines_matches_the_dispatcher(self):
        """`MEETING_LOCAL_ENGINES` 与 `_transcribe_impl` 里那四个分支必须对得上。

        报错文案（"支持哪些"）与分派逻辑是**同一份事实**：改了分派忘了改文案，
        用户又会被指到一个跑不了的引擎上。这里把两边的名字钉在一起。
        """
        self.assertEqual(set(meeting.MEETING_LOCAL_ENGINES),
                         {"whisper", "sensevoice", "qwen3asr", "sherpa"})
        for eng in meeting.MEETING_LOCAL_ENGINES:
            self.assertTrue(meeting.MEETING_ENGINE_LABELS.get(eng),
                            "每个能跑的引擎都要有一句给人看的说法：%s" % eng)


# ---------------------------------------------------------------- ② 驱动不了就响亮失败

class UnsupportedEngineFailsLoudlyTests(_MeetingCase):
    """配置一个会议链路不支持的引擎 → **不加载任何模型**、当场失败、原因落库。"""

    def _run(self):
        """跑一次转写；守卫必须**抛出** `MeetingEngineRefused`。

        为什么断言"抛出"而不是"返回"：`_transcribe_meeting` 把正常返回当成"转写完成"
        （报 idle + 响成功提示音）。拒绝启动必须是**另一种结局**，否则会出现
        "转写完成 · 引擎已加载"和一条失败原因同时挂在同一场会上。
        """
        with self.assertRaises(meeting.MeetingEngineRefused):
            meeting._transcribe_impl(self.folder)

    def test_the_meeting_fails_without_loading_any_model(self):
        self._set_engine("paraformer")
        loaders = self._stub_loaders()

        self._run()

        for attr, mock in loaders.items():
            self.assertFalse(mock.called,
                             "驱动不了的引擎不该加载任何模型，但 %s 被调了：%s"
                             % (attr, mock.call_args_list))
        row = self._meeting_row()
        self.assertEqual(row["status"], "error")

    def test_the_engine_name_is_never_passed_to_the_whisper_loader(self):
        """被修掉的那条路径的**直接**护栏：`_get_whisper(<引擎名>)` 不许再出现。

        只断言"没加载模型"还不够：真正的病是"引擎名被当成 whisper 模型名"，
        所以这里单独把 `_get_whisper` 的第一个参数拿出来看。
        """
        self._set_engine("paraformer")
        loaders = self._stub_loaders()

        self._run()

        loaders["_get_whisper"].assert_not_called()

    def test_the_reason_is_in_the_database_with_the_engine_and_the_way_out(self):
        """落库的原因必须是**具体**的：含引擎名 + 支持哪些 + 该怎么办。"""
        self._set_engine("paraformer")
        self._stub_loaders()

        self._run()

        why = (self._meeting_row()["error"] or "")
        self.assertIn("paraformer", why, "面板要显示的原因里必须点名引擎：%r" % why)
        self.assertIn("sensevoice", why, "必须告诉用户支持哪些：%r" % why)
        self.assertIn("ECHO 后端", why, "必须给出可执行的下一步：%r" % why)
        self.assertNotEqual(why.strip(), "失败", "不许只写'失败'两个字")
        self.assertTrue(any(lv == "error" and "paraformer" in msg for lv, msg in self.logs),
                        "日志里也要有这条：%s" % self.logs)

    def test_nothing_is_written_to_the_meeting_and_no_lines_are_added(self):
        self._set_engine("paraformer")
        self._stub_loaders()

        self._run()

        self.assertEqual(db.get_lines(self.mid), [], "一行都不该写")
        # 段数/时长如实保留（用户要看得出"音频是在的，是转写没开始"）
        self.assertEqual(self._meeting_row()["segments"], 1)

    def test_a_refused_start_is_not_reported_as_a_completed_transcription(self):
        """拒绝启动**不许**被上层报成"转写完成"（那两句会自相矛盾）。

        这条钉的是 `_transcribe_meeting` 的收尾：守卫的失败结局必须与"跑完了"分开，
        否则面板显示"转写完成 · 引擎已加载"，同时库里躺着一条失败原因 ——
        而提示音还会先响一声"咚"、再响一声"叮叮"。
        """
        self._set_engine("paraformer")
        self._stub_loaders()
        reported, ok_beeps = [], []
        with patch.object(meeting.services, "report_meeting",
                          lambda status, detail="": reported.append((status, detail))), \
                patch.object(meeting.tts_mod, "beep_ok", lambda: ok_beeps.append(1)):
            meeting._transcribe_meeting(self.folder)

        self.assertTrue(any(s == "error" for s, _ in reported),
                        "应当把这一场报成 error：%s" % reported)
        self.assertFalse(any("转写完成" in d for _s, d in reported),
                         "拒绝启动不许报'转写完成'：%s" % reported)
        self.assertEqual(ok_beeps, [], "拒绝启动不许响'转写完成'提示音")


# ---------------------------------------------------------------- ③ sherpa 真的能用了

class SherpaMeetingTests(_MeetingCase):
    """`meetingSttModel=sherpa`（安装器给"只装 sherpa"的默认档写的就是它）。"""

    def test_sherpa_text_becomes_rows_marked_estimated(self):
        """sherpa 不给句级时间戳 → 逐句行按字数均摊，档位 `estimated`，且不崩。"""
        self._set_engine("sherpa")
        loaders = self._stub_loaders()
        loaders["_get_sherpa"].return_value = object()
        p = patch.object(meeting.stt_mod, "transcribe_ex",
                         lambda path, **kw: {"text": "第一句话。第二句话。", "status": "ok",
                                             "detail": ""})
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        lines = db.get_lines(self.mid)
        self.assertEqual([ln["text"] for ln in lines], ["第一句话。", "第二句话。"])
        # 时间轴是均摊出来的：单调递增、铺满本段（1.0 秒）
        self.assertLess(lines[0]["start"], lines[1]["start"])
        self.assertAlmostEqual(lines[-1]["end"], 1.0, places=1)
        self.assertEqual(self._meeting_row()["status"], "transcribed")
        # 档位如实进 meta.json（详情页据此显示"估算（按字数均摊）"）
        meta = meeting.meeting_meta(self.name)
        self.assertEqual(meta.get("timestampsKinds"), {TIMESTAMPS_ESTIMATED: 1})
        self.assertNotIn("capability", meta, "本机转写不该伪造一份能力计划")

    def test_sherpa_never_touches_the_whisper_loader(self):
        """**这就是那次故障**：sherpa 曾经被当成 whisper 的模型名。

        所以这里断言 `_get_whisper` 一次都不被调用 —— sherpa 那条路也刻意**不借**
        whisper 的时间骨架（借了的话"档位"就取决于这台机器上恰好装没装 whisper，
        而只装 sherpa 的机器上必然拿不到，档位必须如实）。
        """
        self._set_engine("sherpa")
        loaders = self._stub_loaders()
        p = patch.object(meeting.stt_mod, "transcribe_ex",
                         lambda path, **kw: {"text": "甲。", "status": "ok", "detail": ""})
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        loaders["_get_whisper"].assert_not_called()
        loaders["_get_sherpa"].assert_called()

    def test_a_broken_sherpa_leaves_the_engine_s_own_reason(self):
        """sherpa 挂了（缺依赖/模型没就位）→ 空结果要留痕，并带上引擎自己说的原因。"""
        self._set_engine("sherpa")
        self._stub_loaders()
        p = patch.object(meeting.stt_mod, "transcribe_ex",
                         lambda path, **kw: {"text": "", "status": "error",
                                             "detail": "sherpa: 流式模型未就绪"})
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        self.assertTrue(any(lv == "warn" and "sherpa" in msg and "未就绪" in msg
                            for lv, msg in self.logs),
                        "空结果必须带上引擎自己说的原因：%s" % self.logs)
        self.assertEqual(self._meeting_row()["status"], "error")
        self.assertIn("没有转出任何文字", self._meeting_row()["error"] or "")

    def test_sherpa_rows_helper_is_shape_legal_and_counts_the_kind(self):
        """`_sherpa_rows` 直接调：返回 4 元组行 + 档位计数（拼装层之外的形状契约）。"""
        wav = os.path.join(self.folder, "01.wav")
        kinds = {}
        with patch.object(meeting.stt_mod, "transcribe_ex",
                          lambda path, **kw: {"text": "甲。乙。", "status": "ok", "detail": ""}):
            rows, why = meeting._sherpa_rows(wav, 3, 10, {"sttLanguage": "zh"}, kinds)
        self.assertEqual(why, "")
        self.assertEqual([r[0] for r in rows], [3, 3])
        self.assertEqual([r[3] for r in rows], ["甲。", "乙。"])
        self.assertEqual(kinds, {TIMESTAMPS_ESTIMATED: 1})

    def test_an_empty_sherpa_result_yields_no_rows_and_a_reason(self):
        wav = os.path.join(self.folder, "01.wav")
        kinds = {}
        with patch.object(meeting.stt_mod, "transcribe_ex",
                          lambda path, **kw: {"text": "", "status": "empty",
                                              "detail": "sherpa 返回空结果"}):
            rows, why = meeting._sherpa_rows(wav, 1, 10, {}, kinds)
        self.assertEqual(rows, [])
        self.assertIn("空结果", why)
        self.assertEqual(kinds, {}, "没转出东西就不该记档位")


# ---------------------------------------------------------------- ④ 支持的引擎不受影响

class SupportedEnginesUnchangedTests(_MeetingCase):
    """whisper / sensevoice / qwen3asr：**谁去加载、用什么参数**逐字不变。

    这三个引擎今天能跑，这次修 bug 不许碰它们的行为（硬约束："老装机逐字不变"）。
    加载参数就是"行为"里最容易悄悄改坏的那一半 —— `_get_whisper("small")` 被改成
    `_get_whisper(<配置值>)` 这种事不会有任何报错，只会变慢或变不准。
    """

    def test_whisper_uses_the_configured_model_name(self):
        self._set_engine("medium")
        loaders = self._stub_loaders()
        loaders["_get_whisper"].return_value = object()
        p = patch.object(meeting.stt_mod, "transcribe_whisper",
                         lambda wm, path, lang: ([_WSeg(0.0, 0.4, " 甲 ")],
                                                 {"language": lang}))
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        self.assertEqual(loaders["_get_whisper"].call_args.args[0], "medium")
        self.assertFalse(loaders["_get_sensevoice"].called)
        self.assertFalse(loaders["_get_qwen3asr"].called)
        self.assertEqual([ln["text"] for ln in db.get_lines(self.mid)], ["甲"])
        self.assertEqual(self._meeting_row()["status"], "transcribed")
        self.assertEqual((self._meeting_row()["error"] or ""), "")

    def test_sensevoice_still_borrows_the_small_whisper_skeleton(self):
        self._set_engine("sensevoice")
        loaders = self._stub_loaders()
        loaders["_get_whisper"].return_value = object()
        loaders["_get_sensevoice"].return_value = object()
        p = patch.object(meeting, "_fallback_sv_rows",
                         lambda sv, wm, path, idx, seg_min, cfg: [(idx, 0.0, 0.5, "乙。")])
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        self.assertEqual(loaders["_get_whisper"].call_args.args[0], "small",
                         "SenseVoice 的时间骨架固定用 small（与改动前一致）")
        self.assertTrue(loaders["_get_sensevoice"].called)
        self.assertFalse(loaders["_get_qwen3asr"].called)
        self.assertFalse(loaders["_get_sherpa"].called)
        self.assertEqual([ln["text"] for ln in db.get_lines(self.mid)], ["乙。"])

    def test_qwen3asr_still_uses_the_native_sentences_with_the_forced_aligner(self):
        self._set_engine("qwen3asr")
        loaders = self._stub_loaders()
        loaders["_get_whisper"].return_value = object()
        loaders["_get_qwen3asr"].return_value = object()
        p = patch.object(meeting.stt_mod, "_qwen3asr_sentences",
                         lambda sv, path, lang_hint: ("丙。", [(0.0, 0.6, "丙。")]))
        p.start()
        self.addCleanup(p.stop)

        meeting._transcribe_impl(self.folder)

        args, kwargs = loaders["_get_qwen3asr"].call_args
        self.assertEqual(args[1], "Qwen/Qwen3-ASR-0.6B", "qwen3asr 的模型名与改动前一致")
        self.assertEqual(kwargs.get("forced_aligner"), "Qwen/Qwen3-ForcedAligner-0.6B")
        self.assertFalse(loaders["_get_sensevoice"].called)
        self.assertEqual([ln["text"] for ln in db.get_lines(self.mid)], ["丙。"])

    def test_the_reason_is_cleared_when_a_retranscribe_succeeds(self):
        """失败原因不许留到下一次：重转成功后 `error` 必须清空，否则面板拿旧原因解释新结果。"""
        self._set_engine("paraformer")
        self._stub_loaders()
        with self.assertRaises(meeting.MeetingEngineRefused):
            meeting._transcribe_impl(self.folder)
        self.assertTrue((self._meeting_row()["error"] or "").strip())

        self._set_engine("small")
        loaders = self._stub_loaders()
        loaders["_get_whisper"].return_value = object()
        with patch.object(meeting.stt_mod, "transcribe_whisper",
                          lambda wm, path, lang: ([_WSeg(0.0, 0.4, "丁")], {})):
            meeting._transcribe_impl(self.folder)
        self.assertEqual(self._meeting_row()["status"], "transcribed")
        self.assertEqual((self._meeting_row()["error"] or ""), "")


if __name__ == "__main__":
    unittest.main()
