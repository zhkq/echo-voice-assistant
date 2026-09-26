# -*- coding: utf-8 -*-
"""会议转写 = **转写 + 说话人分离 + 声纹识别**（三件标配）—— 护栏用例。

## 概念（用户 2026-09-26 定的；设计见 `docs/3.0-设计总览与组件关系.md` §6.5）

  * 三件都是标配，本地跑或走 ECHO 后端**一样**；"ASR 一次、分离一次、嵌入一次"只是
    现有模型能力不足的**实现细节**，不是用户要理解的开关 —— 所以 `meetingDiarize`
    与 `voiceprintEnabled` 都被**废弃**（值留在库里兼容读取，但没人读、也改不动行为）。
  * 唯一可选的是「**改名即入库**」（`voiceprintAutoEnroll`，默认**关**）：
    隐私敏感的落点是**入库**（往本机库写生物特征模板），不是识别。
    识别是标配 —— 库里**已经有**这个人，会议就显示姓名（库空时静默无结果、零副作用）。
  * 分离**由谁做**由路由按槽决定（`capabilityDiarizeBackend` / `capabilityEmbedBackend`）；
    "要不要"这一档已经删掉。

## 这份用例钉十件事（每条都钉"意图"，不是字面表达式）

  1. 会议链路**一定**请求 `diarize.turns`（`meetingDiarize` 缺失 / false / true 都一样；
     本地与后端两条路都请求）；
  2. 遗留配置可读且被忽略（老库 `meetingDiarize=false` 不影响"会议一定请求分离"，也不报错）；
  3. 会议链路**一定**请求 `speaker.embed`，且顺序是 `diarize.turns` → `speaker.embed`（L5）；
  4. 分离不可用时**如实降级**：会议仍出文字，但记录 / API 里带「未执行 + **真原因**」
     （权威十词之一），面板文案能被断言到；
  5. 向量空间锁上之后**不再回落本机**（一场会不许混两套不可比的嵌入）；
  6. 「改名即入库」关着时改名**不写库**；打开后才写；
  7. 显式「声纹入库」动作与那个开关**无关**（关着也能入库）；
  8. 识别**不依赖**入库开关（库里有这个人就一定认出来、显示姓名）；
  9. 隐私：声纹模板只写本机库、**不出网**（把 socket 封死也照样入库）；
  10. 设置项收敛：`docs/设置项归属表.md` 与 `DEFAULTS` 一致，且废弃项在表里如实标注。

隔离（与 `tests/test_meeting_engine.py` / `tests/test_meeting_capability.py` 同一套）：
`db.DATA_DIR` / `db.DB_FILE` / `settings._cache` / 会议目录 / 凭据文件路径全部指向临时目录，
**一个真模型都不加载、一只麦克风都不开、一张真实会议数据都不碰**。
"""
import ast
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from collections import namedtuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                              # noqa: E402
import app.meeting as meeting                                    # noqa: E402
from app.capabilities import (                                   # noqa: E402
    BACKEND_ECHO_SERVER,
    SKIP_REASONS,
    SOURCE_LAN,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    CapabilityRouter,
    DiarizeResult,
    Provenance,
)
from app.capabilities import credentials as cred_mod             # noqa: E402
from app.config import DEFAULTS, settings                        # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NAME = "2026-09-26_10-00-00"
_WSeg = namedtuple("_WSeg", "start end text")

#: 三件标配在能力层的槽清单 —— 顺序是**契约**（L5 的向量空间锁沿槽顺序推进）。
WANT_SLOTS = ("asr.text", "asr.timestamps", "diarize.turns", "speaker.embed")


# ---------------------------------------------------------------- ① 槽清单

class SlotsAlwaysAskForSpeakersTests(unittest.TestCase):
    """会议要向能力层要哪些槽：**说话人那一族是无条件的**，顺序固定。"""

    def test_the_speaker_family_is_always_requested_in_this_order(self):
        for cfg in ({}, {"diarize": False}, {"diarize": True}, {"sttModel": "small"}):
            with self.subTest(cfg=cfg):
                self.assertEqual(meeting._session_slots(cfg), WANT_SLOTS)

    def test_diarize_comes_before_embed(self):
        """顺序不是审美：`router.plan()` 的向量空间锁**沿槽顺序**推进 ——
        把 `speaker.embed` 排在前面，锁就由它定，而语义上这场会先有分离才谈得上认人。"""
        slots = meeting._session_slots({})
        self.assertLess(slots.index("diarize.turns"), slots.index("speaker.embed"))

    def test_the_meeting_link_never_reads_the_deprecated_switch(self):
        """**源码级契约**：会议链路（含能力层）里不许再出现 `meetingDiarize`。

        只断言"行为没变"不够 —— 哪天有人为了兼容又把那个键读回来（"老用户关着就给他关着"），
        行为用例可能照样是绿的（比如读法藏在某个分支里），而这次要拆掉的正是那个开关本身。
        """
        offenders = []
        for rel in ("app/meeting.py", "app/capabilities/router.py",
                    "app/capabilities/local.py", "app/capabilities/echo_server.py",
                    "app/capabilities/__init__.py", "app/capability_admin.py"):
            with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
                src = fh.read()
            for i, line in enumerate(src.splitlines(), 1):
                code = line.split("#", 1)[0]
                # 只认**带引号的字面量**：读一个设置项必须是 `"meetingDiarize"` 这样的字符串，
                # 而注释/docstring 里用反引号提它的名字是允许的（那是在解释"为什么不再读"）。
                if '"meetingDiarize"' in code or "'meetingDiarize'" in code:
                    offenders.append("%s:%d" % (rel, i))
        self.assertEqual(offenders, [], "会议链路里又在读那个已废弃的开关了：%s" % offenders)

    def test_the_same_list_is_asked_whether_local_or_backend(self):
        """**本地与后端两条路都请求这三件**：后端那条路看计划，
        本地那条路看 `_transcribe_impl` 真的调了本机分离（见下面 DiarizeDegradesHonestly）。"""
        backend = _FakeBackend()
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        with patch.object(settings, "get",
                          lambda k, d=None: "http://gpu-01:8900"
                          if k == "capabilityEchoServerUrl" else d), \
             patch("app.capabilities.build_default_router", lambda **kw: router), \
             patch.multiple("app.meeting.db", add_log=lambda *a, **k: None):
            session = meeting._capability_asr_session({})
        self.assertIsNotNone(session, "配了可用后端却拿不到会话 —— 三件标配接不上")
        plan = session.plan()
        self.assertEqual(plan.backend_for("diarize.turns"), BACKEND_ECHO_SERVER)
        self.assertEqual(plan.backend_for("speaker.embed"), BACKEND_ECHO_SERVER,
                         "声纹嵌入槽也要在计划里（识别是标配，由路由决定谁做）")


class _FakeBackend(CapabilityClient):
    """假的能力后端：能转写、能分离（形状与 `app/audio/diarize.py` 一致）。"""

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"asr.text", "asr.timestamps", "diarize.turns",
                          "diarize.embeddings", "speaker.embed"})
    vector_space_id = "ws-fake-v1"

    def __init__(self, fail_diarize=None, speakers=None):
        self.diarize_calls = []
        self._fail = fail_diarize
        self._speakers = ({"SPEAKER_00": [0.1] * 256} if speakers is None else speakers)

    def transcribe(self, wav, **kw):
        return AsrResult(text="后端文本。", sentences=(), timestamps="none",
                         provenance=Provenance(self.backend_id, "fake-v1"),
                         audio_seconds=1.0)

    def diarize(self, wav, **kw):
        self.diarize_calls.append(os.path.basename(wav))
        if self._fail:
            raise CapabilityError(self._fail, "后端故意失败（分离）",
                                  backend_id=self.backend_id, slot="diarize.turns")
        return DiarizeResult(turns=((0.0, 1.0, "SPEAKER_00"),),
                             speakers={k: tuple(v) for k, v in self._speakers.items()},
                             dim=256, vector_space_id=self.vector_space_id,
                             provenance=Provenance(self.backend_id, "fake-dia-v1"),
                             audio_seconds=1.0)


# ---------------------------------------------------------------- ② 遗留配置

class _IsolatedState(unittest.TestCase):
    """库 / 设置缓存 / 凭据文件全指向临时目录（这台机器真配对过也不受影响）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-std-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "std.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()
        self._cred = os.path.join(self.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: self._cred)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._cache = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class LegacyConfigTests(_IsolatedState):
    """老库里的 `meetingDiarize`（以及老面板写下的 `capabilityDiarizeBackend=off`）
    **能读、被忽略、不报错**。"""

    def test_the_key_is_deprecated_but_still_readable(self):
        self.assertTrue(DEFAULTS["meetingDiarize"].get("deprecated"),
                        "meetingDiarize 必须标成已废弃（它不再是开关）")
        db.set_setting("meetingDiarize", False)          # 老装机：用户从没开过
        settings._cache = None
        self.assertFalse(settings.get("meetingDiarize"))  # 读得到
        self.assertNotIn("meetingDiarize",                # 但不再下发
                         [r["key"] for r in settings.all(include_hidden=True)])

    def test_writing_it_is_refused_without_an_exception(self):
        """写它不该报错（面板/老脚本可能还在写），只是不生效 —— 静默接受才是假象。"""
        got = settings.update({"meetingDiarize": True})
        self.assertNotIn("meetingDiarize", got)
        self.assertFalse(settings.get("meetingDiarize"))

    def test_legacy_off_means_auto_not_nobody(self):
        """老面板那句「只要文字」把它写成了 `off`；新语义下 `off` **不再表示"不要说话人"**。

        读时折成 `auto`（`config.VALUE_ALIASES`）→ 走默认链挑后端，而不是"谁都不许做"。
        库里的原值一个字都不动（用户随时能改回来）。
        """
        db.set_setting("capabilityDiarizeBackend", "off")
        settings._cache = None
        self.assertEqual(settings.get("capabilityDiarizeBackend"), "auto")
        rows = {r["key"]: r["value"] for r in settings.all(include_hidden=True)}
        self.assertEqual(rows.get("capabilityDiarizeBackend"), "auto",
                         "面板出口也要折算 —— 否则显示 off、路由按 auto 走，对不上")

    def test_the_diarize_slot_still_gets_a_backend_after_a_legacy_off(self):
        """折算之后真的能挑到后端（"off 被忽略"的**行为**证明，不只是读值相等）。"""
        db.set_setting("capabilityDiarizeBackend", "off")
        settings._cache = None
        backend = _FakeBackend()
        router = CapabilityRouter([backend], settings_get=settings.get)
        with patch.object(settings, "get",
                          lambda k, d=None: "http://gpu-01:8900"
                          if k == "capabilityEchoServerUrl" else d), \
             patch("app.capabilities.build_default_router", lambda **kw: router), \
             patch.multiple("app.meeting.db", add_log=lambda *a, **k: None):
            session = meeting._capability_asr_session({})
        self.assertIsNotNone(session)
        self.assertEqual(session.plan().backend_for("diarize.turns"), BACKEND_ECHO_SERVER)


# ---------------------------------------------------------------- ③④⑤ 分离链路

class _MeetingCase(_IsolatedState):
    """一场真会议（目录 + meta.json + 库记录 + 一段真 wav）+ 全套替身。

    **本机分离一律打桩**：真 pyannote 在开发机上是装着的，不打桩会去加载模型
    （几十秒 + 占显存），而这里要验的是"链路有没有请求分离、失败时怎么如实说"。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self.logs = []
        p = patch.object(meeting.db, "add_log",
                         lambda level, src, msg: self.logs.append((level, msg)))
        p.start()
        self.addCleanup(p.stop)
        for target, kw in ((meeting, "_boot_meeting_stt"), (meeting, "_boot_note_meeting_key"),
                           (meeting.tts_mod, "play_beep"), (meeting.tts_mod, "beep_ok")):
            p = patch.object(target, kw, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        settings.update({"meetingAutoSummarize": False})
        settings._cache = None
        self.meetings_root = os.path.join(self.tmp, "meetings")
        os.makedirs(self.meetings_root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: self.meetings_root)
        p.start()
        self.addCleanup(p.stop)
        self.name = "%s__%s" % (_NAME, self._testMethodName)
        self.folder = self._make_meeting(self.name)

    def _make_meeting(self, name, segs=("01.wav",), seconds=1.0):
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        for seg in segs:
            _write_wav(os.path.join(folder, seg), seconds)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"segments": list(segs), "config": {"sttModel": "small"},
                       "durationSeconds": seconds * len(segs)}, fh)
        self.mid = db.create_meeting(name, started_at="2026-09-26T10:00:00",
                                     stt_model="small", stt_device="auto")
        return folder

    def _stub_local_asr(self, text="第一句。第二句。"):
        """本机转写：**一个真模型都不加载**。

        用 SenseVoice 那条路（它在默认档里，且不需要 GPU），把加载器与"文本→逐句"那一步
        都换成替身 —— 这一组用例要验的是分离链路，不是转写引擎（引擎的护栏在
        `tests/test_meeting_engine.py`）。
        """
        p = patch.object(meeting.stt_mod, "_get_sensevoice", MagicMock(return_value=object()))
        p.start()
        self.addCleanup(p.stop)
        rows = [(1, 0.0, 0.4, "第一句。"), (1, 0.4, 0.9, "第二句。")]

        def fake_fallback(sv, wm, path, idx, seg_min, cfg, cap_kinds=None):
            # 签名与档位累加都照**真实现**来（`app/meeting.py::_fallback_sv_rows`）：
            # 替身少一个参数，就会把"调用点忘了传 cap_kinds"这个 bug 挡在门外
            # （2026-09-26：就是它让那条红用例在替身这里先炸了）。
            if cap_kinds is not None:
                cap_kinds["estimated"] = cap_kinds.get("estimated", 0) + 1
            return [r for r in rows]

        p = patch.object(meeting, "_fallback_sv_rows", fake_fallback)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(meeting, "_skeleton_model", lambda cfg, *a: None)
        p.start()
        self.addCleanup(p.stop)
        settings.update({"meetingSttModel": "sensevoice"})
        settings._cache = None
        return text

    def _stub_local_diarize(self, turns=None, embs=None, labels=None, raises=None):
        """本机分离替身。`raises` 用来演"本机没装 pyannote / 没权重"。"""
        def fake(path, max_speakers=None):
            if raises is not None:
                raise raises
            return (turns if turns is not None else [(0.0, 1.0, "SPEAKER_00")],
                    embs if embs is not None else [[0.1] * 256],
                    labels if labels is not None else ["SPEAKER_00"])
        p = patch("app.audio.diarize.diarize_wav_full", fake)
        p.start()
        self.addCleanup(p.stop)

    def _diarize_meta(self, name=None):
        return meeting.meeting_meta(name or self.name).get("diarize")


class HonestDegradationTests(_MeetingCase):
    """分离真跑不了 → **会议仍出文字**，但必须如实说「未执行 + 真原因」。"""

    def _run_without_any_separation(self):
        self._stub_local_asr()
        # 本机没有可用的分离（开发机上装着 pyannote，所以这里显式演"没装/没权重"）。
        self._stub_local_diarize(raises=RuntimeError("没有可用的 pyannote 权重"))
        # 也没配后端（凭据文件指向临时目录 → 不存在）。
        meeting._transcribe_impl(self.folder)

    def test_the_meeting_still_produces_text(self):
        self._run_without_any_separation()
        lines = db.get_lines(self.mid)
        self.assertEqual([ln["text"] for ln in lines], ["第一句。", "第二句。"],
                         "分离不可用**不该**连累转写 —— 文字必须照常落库")
        self.assertEqual(db.get_meeting(self.mid)["status"], "transcribed")

    def test_the_meeting_says_which_reason_with_an_authoritative_word(self):
        self._run_without_any_separation()
        dia = self._diarize_meta()
        self.assertIsNotNone(dia, "meta.json 里必须有分离的结论（没做也要有）")
        self.assertFalse(dia["executed"])
        self.assertIn(dia["reason"], SKIP_REASONS,
                      "原因必须是权威十词之一，不许自己编：%r" % dia["reason"])
        self.assertTrue(dia["detail"], "还要带上具体是什么原因（不能只有一个词）")
        self.assertTrue(any(lv == "warn" and "说话人分离未执行" in m for lv, m in self.logs),
                        "日志里也要有一句「说话人分离未执行」：%s" % self.logs)

    def test_the_api_and_the_panel_can_assert_the_sentence(self):
        """记录 / API / 面板三层都要能断言到那句「说话人分离未执行：<原因>」。"""
        from app import capability_admin
        self._run_without_any_separation()
        meta = meeting.meeting_meta(self.name)
        cap = capability_admin.plan_summary(meta.get("capability"),
                                            meta.get("timestampsKinds"),
                                            meta.get("diarize"))
        self.assertIsNotNone(cap, "只有分离这一条信息时也不许返回 None（面板会当没数据藏起来）")
        dia = cap["diarize"]
        self.assertTrue(dia["headline"].startswith(capability_admin.DIARIZE_NOT_EXECUTED),
                        "面板要显示的那句话必须如实：%r" % dia["headline"])
        self.assertIn(dia["reasonLabel"], dia["headline"])
        self.assertIn(dia["reason"], SKIP_REASONS)
        # 前端：那句话是**服务端给的 headline**，前端只渲染，不自己拼
        with open(os.path.join(_ROOT, "web", "meeting.html"), encoding="utf-8") as fh:
            html = fh.read()
        body = html[html.index("function renderCapability("):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("dia.headline", body, "面板没把服务端那句结论渲染出来")
        self.assertIn('data-testid="diarize-missing"', body,
                      "没做成的分离要有一个可断言的落点（截图/自动化都靠它）")

    def test_a_successful_separation_is_recorded_as_executed(self):
        """反过来：本机分离能跑 → `executed=True`，并且**说话人真的落进转写行**。"""
        self._stub_local_asr()
        self._stub_local_diarize()
        meeting._transcribe_impl(self.folder)
        dia = self._diarize_meta()
        self.assertTrue(dia["executed"], dia)
        self.assertEqual(dia["reason"], "")
        labels = {ln["speaker_label"] for ln in db.get_lines(self.mid)}
        self.assertTrue(labels and labels != {""}, "分离成功却没有说话人列：%s" % labels)


class VectorSpaceLockSurvivesFailureTests(_MeetingCase):
    """后端分离成功过一次 → 后面某一段失败时**不再回落本机**（L5：一场会不许混两套嵌入）。"""

    def test_the_local_engine_is_not_used_after_the_backend_locked_the_space(self):
        self._stub_local_asr()
        local = MagicMock(name="diarize_wav_full",
                          return_value=([(0.0, 1.0, "LOCAL")], [[0.9] * 256], ["LOCAL"]))
        p = patch("app.audio.diarize.diarize_wav_full", local)
        p.start()
        self.addCleanup(p.stop)
        # 两段：第一段走后端成功，第二段后端挂了
        self.name = "%s__%s" % (_NAME, self._testMethodName + "-2seg")
        self.folder = self._make_meeting(self.name, segs=("01.wav", "02.wav"))
        backend = _FakeBackend()
        calls = {"n": 0}
        real = backend.diarize

        def scripted(wav, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise CapabilityError("offline", "第二段后端挂了",
                                      backend_id=backend.backend_id, slot="diarize.turns")
            return real(wav, **kw)

        backend.diarize = scripted
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        with patch("app.capabilities.build_default_router", lambda **kw: router):
            meeting._transcribe_impl(self.folder)

        self.assertEqual(local.call_count, 0,
                         "锁上向量空间后仍回落本机 —— 一场会混两套嵌入会认错人且不报错")
        # 第一段有说话人、第二段没有（如实如此），整场结论是"至少执行过"
        labels = [(ln["seg_index"], ln["speaker_label"]) for ln in db.get_lines(self.mid)]
        self.assertTrue(any(sp for _seg, sp in labels), labels)
        self.assertTrue(any(not sp for _seg, sp in labels),
                        "第二段拿不到分离结果却标了说话人：%s" % labels)


# ---------------------------------------------------------------- ⑥⑦⑧⑨⑩ 声纹

class VoiceprintIsStandardTests(_IsolatedState):
    """识别是**标配**（没有开关）；可选的是**入库**（`voiceprintAutoEnroll`，默认关）。"""

    def setUp(self):
        super().setUp()
        self._meetings_root = os.path.join(self.tmp, "meetings")
        os.makedirs(self._meetings_root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: self._meetings_root)
        p.start()
        self.addCleanup(p.stop)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router as api_router
        app = FastAPI()
        app.include_router(api_router)
        self.client = TestClient(app)

    def _meeting(self, name, speakers=("S1",), vec=None):
        import numpy as np
        from app import voiceprint as vp
        mid = db.create_meeting(name, started_at="2026-09-26 10:00:00")
        os.makedirs(os.path.join(self._meetings_root, name), exist_ok=True)
        db.replace_speakers(mid, {s: s.replace("S", "说话人") for s in speakers})
        v = np.zeros(256, dtype=np.float32)
        v[0] = 1.0
        db.replace_speaker_embeddings(mid, {s: (*vp.pack(v if vec is None else vec), 2)
                                            for s in speakers})
        db.add_lines(mid, [(1, 0.0, 1.0, s, "%s 说了一句" % s) for s in speakers])
        return mid

    def test_the_disabled_switch_key_is_gone_from_the_ui_but_readable(self):
        self.assertTrue(DEFAULTS["voiceprintEnabled"].get("deprecated"))
        self.assertFalse(DEFAULTS["voiceprintEnabled"]["value"])
        self.assertFalse(DEFAULTS["voiceprintAutoEnroll"]["value"],
                         "「改名即入库」默认必须是关的（生物特征：写库要用户主动）")

    def test_rename_does_not_enroll_while_the_switch_is_off(self):
        """**开关关着时绝不自动入库**（不许静默写库）。"""
        from app import voiceprint as vp
        settings.update({"voiceprintAutoEnroll": False})
        settings._cache = None
        mid = self._meeting("m-off")
        r = self.client.post("/api/meetings/%d/speaker/rename" % mid,
                            json={"label": "S1", "name": "张总"}).json()
        self.assertTrue(r["ok"])
        self.assertEqual(vp.library_stats()["samples"], 0, "关着开关却写库了：%s" % r)
        rows = {s["label"]: s["name"] for s in db.get_speakers(mid)}
        self.assertEqual(rows, {"S1": "张总"}, "改名本身仍要生效（它只是不入库）")

    def test_rename_enrolls_once_the_switch_is_on(self):
        from app import voiceprint as vp
        settings.update({"voiceprintAutoEnroll": True})
        settings._cache = None
        mid = self._meeting("m-on")
        self.client.post("/api/meetings/%d/speaker/rename" % mid,
                         json={"label": "S1", "name": "张总"})
        self.assertEqual(vp.library_stats()["samples"], 1)

    def test_the_explicit_enroll_button_works_with_the_switch_off(self):
        """**入库的选择权在用户手里**：关着「改名即入库」，显式入库照样成功。"""
        from app import voiceprint as vp
        settings.update({"voiceprintAutoEnroll": False})
        settings._cache = None
        mid = self._meeting("m-explicit")
        r = self.client.post("/api/voiceprints/enroll",
                             json={"meeting_id": mid, "label": "S1", "name": "张总"}).json()
        self.assertTrue(r["ok"], r)
        self.assertIn("张总", r["message"])
        self.assertEqual(vp.library_stats(), {"contacts": 1, "samples": 1})

    def test_recognition_does_not_depend_on_the_enroll_switch(self):
        """库里已经有这个人 → 会议结果里就显示姓名，与"要不要入库"无关。"""
        from app import voiceprint as vp
        import numpy as np
        settings.update({"voiceprintAutoEnroll": False})
        settings._cache = None
        src = self._meeting("m-src")
        self.client.post("/api/voiceprints/enroll",
                         json={"meeting_id": src, "label": "S1", "name": "张总"})
        # 另一场：同一声音 → 应当认出「张总」
        v = np.zeros(256, dtype=np.float32)
        v[0] = 0.98
        v[1] = float(np.sqrt(1 - 0.98 ** 2))
        mid = self._meeting("m-recognize", vec=v)
        res = vp.recognize_meeting(mid)
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(res["renamed"], 1, res)
        rows = {s["label"]: s["name"] for s in db.get_speakers(mid)}
        self.assertEqual(rows, {"S1": "张总"})
        # 而且**没有**顺手入库（识别与入库是两件事）
        self.assertEqual(vp.library_stats()["samples"], 1)

    def test_enrollment_never_touches_the_network(self):
        """隐私边界：模板只写本机库 —— **把 socket 封死**也照样入库。"""
        import socket
        from app import voiceprint as vp
        settings.update({"voiceprintAutoEnroll": False})
        settings._cache = None
        mid = self._meeting("m-offline")

        def boom(*a, **k):
            raise AssertionError("入库过程试图联网：%r %r" % (a, k))

        with patch.object(socket.socket, "connect", boom), \
             patch.object(socket, "create_connection", boom), \
             patch.object(socket, "getaddrinfo", boom):
            ok, msg = vp.enroll_from_meeting(mid, "S1", "张总")
        self.assertTrue(ok, msg)
        self.assertEqual(vp.library_stats()["samples"], 1)

    def test_the_retention_of_this_meeting_is_local_and_not_a_switch(self):
        """本场各说话人的平均嵌入要**留存**（「声纹入库」按钮靠它），而它不再是开关：

        旧实现把整块留存包在 `voiceprint.enabled()` 里（识别关着就不留），
        那在今天会把用户**显式入库**的路堵掉。**隐私边界不变**：它只落本机库、不出网。
        """
        with open(os.path.join(_ROOT, "app", "meeting.py"), encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        gated = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = ast.unparse(node.test)
                if "enabled()" in test and any(
                        isinstance(sub, ast.Call)
                        and "replace_speaker_embeddings" in ast.unparse(sub.func)
                        for sub in ast.walk(node)):
                    gated = True
        self.assertFalse(gated, "留存又被某个'识别开关'包起来了 —— 那会堵掉用户显式入库的路")
        self.assertIn("db.replace_speaker_embeddings", src, "留存本身必须还在")


def _write_wav(path, seconds=1.0):
    """写一段**真的** wav（16k 单声道 int16，与 recorder.py 的产出同格式）。"""
    n = int(16000 * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


if __name__ == "__main__":
    unittest.main()
