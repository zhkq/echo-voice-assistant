# -*- coding: utf-8 -*-
"""跨段说话人一致性（一场会议 = 一套说话人标签）—— 回归用例。

## 这条用例钉的是什么

手机/电脑都是 **10 分钟一段**、**边录边传**，而转写要等"结束录音"之后才开始
（那时所有段都在盘上）。转写循环会对**每一段各调一次** `/v1/diarize` ——
这是必须的（分离模型按段跑），但**"这一段的 S0 是不是上一段那个人"不能由后端回答**：
服务端的局部标签只在本次响应内有意义（见 `server/routes.py` 的 `/v1/diarize` 说明）。

跨段同一性由客户端在**整场**范围里做：`app/audio/diarize.py::SpeakerRegistry`
（每场会只建一次，逐段 `map(embeddings, labels)`，余弦相似度 > 阈值归入已有编号，
否则新建一个）。本文件把这条**意图**钉死：

  1. 同一个人在第 1 段与第 3 段（embedding 相同、**局部标签可以完全不同**）
     → 必须拿到**同一个**全局标签；
  2. 每段的局部标签都从 S0 重新开始时，**不能**把第 2 段另一个人并进 S0 那个人
     （这正是"按局部标签直接当显示名"会犯的错）；
  3. 3 段 3 个不同的人 → **3 个**标签（宁可多分，绝不错并）；
  4. 库里的 `lines.speaker_label`、`speakers` 表、导出的 `transcript.md` 三处
     **同一套**；`meta.json` 里的分离结论与向量空间也必须是这一场的事实。

## 隔离

临时库 + 临时会议目录 + **打桩的能力后端**（不联网、不加载任何模型、不碰真实会议）。
与 `tests/test_meeting_speakers_standard.py` 的 `_MeetingCase` 同一套隔离纪律。
"""
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                              # noqa: E402
import app.meeting as meeting                                    # noqa: E402
from app.capabilities import (                                   # noqa: E402
    BACKEND_ASR_PROVIDER,
    BACKEND_ECHO_SERVER,
    SOURCE_LAN,
    AsrResult,
    CapabilityClient,
    CapabilityRouter,
    DiarizeResult,
    Provenance,
)
from app.capabilities import credentials as cred_mod             # noqa: E402
from app.config import settings                                  # noqa: E402

_NAME = "2026-10-18_00-10-00"


# ---------------------------------------------------------------- 打桩后端

def _one_hot(i, dim=256):
    """第 i 维为 1 的单位向量：不同 i 的人余弦相似度 = 0（绝不误并）。

    同一个人在不同段用**同一个**向量 —— 这正是"embedding 相同"的那条判据。
    """
    v = [0.0] * dim
    v[i] = 1.0
    return v


class _SegmentBackend(CapabilityClient):
    """按段返回"局部标签 + 该说话人的嵌入"的假后端。

    `plan` = `{段文件名: (局部标签, 嵌入)}`。**局部标签由用例故意设成每段重启**
    （S0/S1/S5…），因为真实 pyannote 的 `SPEAKER_00` 就是只在本次响应内有意义。
    它让这条用例能区分"按标签编号拼"（错的）与"按嵌入认人"（对的）。
    """

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"asr.text", "diarize.turns", "speaker.embed"})
    vector_space_id = "ws-test-v1"

    def __init__(self, plan):
        self.plan = dict(plan)
        self.diarize_calls = []

    def transcribe(self, wav, **kw):
        seg = os.path.basename(wav)
        text = "第%s段的内容。" % seg[:2]
        return AsrResult(text=text,
                         sentences=((0.0, 0.9, text),),
                         timestamps="exact",
                         provenance=Provenance(self.backend_id, "test-v1"),
                         audio_seconds=1.0)

    def diarize(self, wav, *, max_speakers=None, **kw):
        seg = os.path.basename(wav)
        self.diarize_calls.append(seg)
        local_label, vec = self.plan[seg]
        return DiarizeResult(
            turns=((0.0, 1.0, local_label),),
            speakers={local_label: tuple(vec)},
            dim=256,
            vector_space_id=self.vector_space_id,
            provenance=Provenance(self.backend_id, "test-dia-v1"),
            audio_seconds=1.0)


# ---------------------------------------------------------------- 夹具

class _MeetingCase(unittest.TestCase):
    """临时库 / 临时会议目录 / 假后端 —— 不碰这台机器上任何真实数据。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-speaker-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "speaker.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()
        # 凭据文件也指到临时目录：这台机器真配对过也不影响用例。
        self._cred = os.path.join(self.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: self._cred)
        p.start()
        self.addCleanup(p.stop)
        self.logs = []
        p = patch.object(meeting.db, "add_log",
                         lambda level, src, msg: self.logs.append((level, src, msg)))
        p.start()
        self.addCleanup(p.stop)
        # 用例不许出声。
        for target, kw in ((meeting.tts_mod, "play_beep"), (meeting.tts_mod, "beep_ok")):
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
        self.mid = db.get_meeting_by_name(self.name)["id"]

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._cache = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 造一场会 ---------------------------------------------------------

    def _make_meeting(self, name, segs=("01.wav", "02.wav", "03.wav"), seconds=1.0):
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        for seg in segs:
            _write_wav(os.path.join(folder, seg), seconds)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"segments": list(segs),
                       "config": {"sttModel": "small"},
                       "durationSeconds": seconds * len(segs)}, fh)
        db.create_meeting(name, started_at="2026-10-18T00:10:00",
                          stt_model="small", stt_device="auto")
        return folder

    # ---- 跑转写 -----------------------------------------------------------

    def _run(self, backend):
        """配一个假后端 + 把路由换成"只有它"，然后**同步**跑完整场转写。"""
        url = "http://gpu-test:8900"
        settings.update({"capabilityEchoServerUrl": url})
        settings._cache = None
        real_get = settings.get

        def fake_get(key, default=None):
            if key == "capabilityEchoServerUrl":
                return url
            return real_get(key, default)

        p = patch.object(meeting.settings, "get", fake_get)
        p.start()
        self.addCleanup(p.stop)
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        p = patch("app.capabilities.build_default_router", lambda **kw: router)
        p.start()
        self.addCleanup(p.stop)
        meeting._transcribe_impl(self.folder)
        return backend

    # ---- 读结果 -----------------------------------------------------------

    def _db_labels_by_segment(self):
        out = {}
        for row in db.get_lines(self.mid):
            out.setdefault(int(row["seg_index"]), set()).add(row["speaker_label"])
        return out

    def _transcript_labels_by_segment(self):
        """从 transcript.md 解析 `[显示名] [hh:mm:ss] 文本`，按段分组。"""
        path = os.path.join(self.folder, "transcript.md")
        self.assertTrue(os.path.isfile(path), "没导出 transcript.md")
        out, cur = {}, None
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"## 第 (\d+) 段", line)
                if m:
                    cur = int(m.group(1))
                    out.setdefault(cur, [])
                    continue
                m = re.match(r"\[([^\]]*)\] \[\d\d:\d\d:\d\d\] ", line)
                if m and cur is not None:
                    out[cur].append(m.group(1))
        return {k: set(v) for k, v in out.items()}

    def _speaker_names(self):
        return {row["label"]: row["name"] for row in db.get_speakers(self.mid)}


class _NoEmbeddingBackend(CapabilityClient):
    """**只回时间轴、不回嵌入**的假后端（`speakers={}`）。

    两个变体由 `backend_id` 区分，它们对"局部标签是不是整场唯一"的回答**相反**：
      * `echo-server`：每段各跑一次分离，局部标签每段重新开始 → **不能**跨段用；
      * `asr-provider`：一次任务覆盖整场，`speaker_id` 整场一致 → 可以直接用。
    """

    source = SOURCE_LAN
    provides = frozenset({"asr.text", "diarize.turns"})
    vector_space_id = "ws-no-embed-v1"

    def __init__(self, plan, backend_id=BACKEND_ECHO_SERVER):
        self.plan = dict(plan)
        self.backend_id = backend_id

    def transcribe(self, wav, **kw):
        seg = os.path.basename(wav)
        text = "第%s段的内容。" % seg[:2]
        return AsrResult(text=text,
                         sentences=((0.0, 0.9, text),),
                         timestamps="exact",
                         provenance=Provenance(self.backend_id, "test-v1"),
                         audio_seconds=1.0)

    def diarize(self, wav, *, max_speakers=None, **kw):
        seg = os.path.basename(wav)
        local_label = self.plan[seg]
        return DiarizeResult(
            turns=((0.0, 1.0, local_label),),
            speakers={},                       # ← 没有嵌入
            dim=0,
            vector_space_id=self.vector_space_id,
            provenance=Provenance(self.backend_id, "test-dia-v1"),
            audio_seconds=1.0)


# ---------------------------------------------------------------- ① 同一人跨段

class SamePersonAcrossSegmentsTests(_MeetingCase):
    """同一个人在段 1 / 段 3 → 同一个标签；段 2 的另一个人不能并进来。"""

    def test_the_same_person_keeps_one_label_even_when_local_labels_differ(self):
        """段 1 与段 3 是**同一个人**：嵌入相同、局部标签故意不同（S0 vs S5）。

        这条判据要求实现**用 embedding 认人**：若按"后端给的标签编号"当全局编号，
        同一个人会变成两个标签（段 1 的 S0、段 3 的 S5）。
        """
        backend = _SegmentBackend({
            "01.wav": ("S0", _one_hot(0)),      # 甲
            "02.wav": ("S1", _one_hot(1)),      # 乙
            "03.wav": ("S5", _one_hot(0)),      # 甲（同一个人，换了局部标签）
        })
        self._run(backend)

        self.assertEqual(backend.diarize_calls, ["01.wav", "02.wav", "03.wav"],
                         "每段仍要各自做分离（这是模型按段跑的事实），"
                         "但标签由客户端跨段缝合")
        per_seg = self._db_labels_by_segment()
        seg1 = per_seg[1] - {""}
        seg3 = per_seg[3] - {""}
        seg2 = per_seg[2] - {""}
        self.assertEqual(len(seg1), 1, "段 1 只有一个人：%s" % per_seg)
        self.assertEqual(len(seg3), 1, "段 3 只有一个人：%s" % per_seg)
        self.assertEqual(seg1, seg3,
                         "同一个人（embedding 相同）在第 1/3 段拿到了不同标签：%s" % per_seg)
        self.assertNotEqual(seg1, seg2, "段 2 的另一个人被并进了段 1 那个人：%s" % per_seg)
        labels = {x for v in per_seg.values() for x in v if x}
        self.assertEqual(len(labels), 2, "整场应当只有 2 个说话人，实际 %s" % labels)

    def test_a_local_label_restarting_every_segment_does_not_merge_two_people(self):
        """三段都用局部标签 `S0`（真实后端每段都会这样重新编号）—— **不能**当成同一个人。

        如果实现直接把局部标签当显示名（或按"第几个标签"拼），
        段 1 的甲与段 2 的乙都会变成"说话人1" → 两个人被并成一个（最坏的错误）。
        """
        backend = _SegmentBackend({
            "01.wav": ("S0", _one_hot(0)),      # 甲
            "02.wav": ("S0", _one_hot(1)),      # 乙（局部标签与段 1 相同！）
            "03.wav": ("S0", _one_hot(0)),      # 甲
        })
        self._run(backend)

        per_seg = self._db_labels_by_segment()
        self.assertEqual(per_seg[1] - {""}, per_seg[3] - {""},
                         "同一个人在第 1/3 段标签不同：%s" % per_seg)
        self.assertNotEqual(per_seg[1] - {""}, per_seg[2] - {""},
                            "两个人共用同一个局部标签 S0 时被并成一个：%s" % per_seg)
        labels = {x for v in per_seg.values() for x in v if x}
        self.assertEqual(len(labels), 2, "整场应当只有 2 个标签，实际 %s" % labels)


# ---------------------------------------------------------------- ② 不许多并

class DifferentPeopleStayApartTests(_MeetingCase):
    """3 段 3 个不同的人 → 3 个标签（宁可多分，不可错并）。"""

    def test_three_people_are_never_merged_even_with_the_same_local_label(self):
        backend = _SegmentBackend({
            "01.wav": ("S0", _one_hot(0)),      # 甲
            "02.wav": ("S0", _one_hot(1)),      # 乙
            "03.wav": ("S0", _one_hot(2)),      # 丙
        })
        self._run(backend)

        per_seg = self._db_labels_by_segment()
        for seg in (1, 2, 3):
            self.assertEqual(len(per_seg[seg] - {""}), 1,
                             "第 %s 段应当只有一个人：%s" % (seg, per_seg))
        seg_labels = [next(iter(per_seg[i] - {""})) for i in (1, 2, 3)]
        self.assertEqual(len(set(seg_labels)), 3,
                         "3 个不同的人被并成了更少的标签：%s" % per_seg)
        names = self._speaker_names()
        self.assertEqual(len(names), 3, "speakers 表也必须是 3 个：%s" % names)


# ---------------------------------------------------------------- ③ 没有嵌入时

class NoEmbeddingBackendTests(_MeetingCase):
    """后端**不回嵌入**时两条相反的结论（"宁可多分不可错并"的硬边界）。

    这条边界值得单独钉住，因为"没有向量"时存在一个**静默错并**的陷阱：
    局部标签每段都从 `S0` 起，如果把它的编号直接当全局编号，
    第 2 段的另一个人会与第 1 段的甲共用 `S1` —— 两个人被并成一个，而且不报错。
    """

    def test_an_echo_backend_without_embeddings_never_merges_two_people(self):
        """每段各跑一次分离、又不回嵌入（echo-server 的异常档）：
        宁可让同一人跨段各算一个，也**绝不**把两个不同的人并成一个。"""
        backend = _NoEmbeddingBackend({
            "01.wav": "S0",                    # 甲
            "02.wav": "S0",                    # 乙（局部编号与段 1 相同）
            "03.wav": "S0",                    # 丙
        })
        self._run(backend)

        per_seg = self._db_labels_by_segment()
        labels = [next(iter(per_seg[seg] - {""})) for seg in (1, 2, 3)]
        self.assertEqual(len(set(labels)), 3,
                         "没有嵌入时按局部编号硬并，把不同的人并成了一个：%s" % per_seg)
        names = self._speaker_names()
        self.assertEqual(len(set(names.values())), 3,
                         "speakers 表里出现了重名（两个人共用一个显示名）：%s" % names)
        for seg in (1, 2, 3):
            self.assertIn("第%d段" % seg, " ".join(names.values()),
                          "段内编号要带段号，才不会被误读成整场同一个：%s" % names)
        self.assertTrue(any(lv == "warn" and "没有嵌入" in msg
                            for lv, _src, msg in self.logs),
                        "退化必须留痕（一场一次），不能看起来像正常结果：%s" % self.logs)

    def test_the_provider_path_keeps_global_ids_without_embeddings(self):
        """`asr-provider` 是**整场一次聚类**（`speaker_id` 整场一致）：
        没有嵌入也可以直接用它的编号，不能给它加段号、更不能把同一人拆开。"""
        settings.update({"capabilityMeetingAsrBackend": BACKEND_ASR_PROVIDER})
        settings._cache = None
        backend = _NoEmbeddingBackend({
            "01.wav": "0",                     # 甲
            "02.wav": "1",                     # 乙
            "03.wav": "0",                     # 甲（同一人，还是 0）
        }, backend_id=BACKEND_ASR_PROVIDER)
        self._run(backend)

        per_seg = self._db_labels_by_segment()
        self.assertEqual(per_seg[1] - {""}, per_seg[3] - {""},
                         "provider 的整场编号被拆开了：%s" % per_seg)
        self.assertNotEqual(per_seg[1] - {""}, per_seg[2] - {""},
                            "provider 的两个编号被并成一个：%s" % per_seg)
        names = self._speaker_names()
        self.assertEqual(set(names.values()), {"说话人1", "说话人2"},
                         "provider 的编号应当直接可用（不加段号）：%s" % names)
        self.assertFalse(any(lv == "warn" and "没有嵌入" in msg
                             for lv, _src, msg in self.logs),
                         "provider 的编号是整场一致的，不该报退化：%s" % self.logs)


# ---------------------------------------------------------------- ④ 三处一致

class OneLabelSetEverywhereTests(_MeetingCase):
    """`lines` / `speakers` / `transcript.md` 是同一套；`meta` 记的是这一场的事实。"""

    def test_the_label_set_is_the_same_in_the_database_and_the_transcript(self):
        backend = _SegmentBackend({
            "01.wav": ("S0", _one_hot(0)),      # 甲
            "02.wav": ("S1", _one_hot(1)),      # 乙
            "03.wav": ("S5", _one_hot(0)),      # 甲
        })
        self._run(backend)

        per_seg = self._db_labels_by_segment()
        names = self._speaker_names()
        line_labels = {x for v in per_seg.values() for x in v if x}
        self.assertEqual(line_labels, set(names),
                         "lines 里的标签与 speakers 表不是同一套：%s vs %s"
                         % (line_labels, names))
        self.assertEqual(len(names), 2, names)

        # meta.json：分离**确实执行了**，且这一场锁的是同一个向量空间。
        # （标签本身不进 meta —— 它落在库与导出的文字稿里；meta 记的是"这场做了什么"。）
        meta = meeting.meeting_meta(self.name)
        self.assertTrue(meta["diarize"]["executed"], meta.get("diarize"))
        self.assertEqual(meta["capability"]["vectorSpaceId"], "ws-test-v1",
                         "meta 里没记下这一场用的向量空间：%s"
                         % meta.get("capability"))
        self.assertEqual(meta["capability"]["picks"]["diarize.turns"]["backendId"],
                         BACKEND_ECHO_SERVER)

        # transcript.md：按段看显示名，必须与库里 label→name 的映射逐一对上。
        by_transcript = self._transcript_labels_by_segment()
        for seg, labels in per_seg.items():
            expect = {names[lab] for lab in labels if lab}
            self.assertEqual(by_transcript.get(seg), expect,
                             "第 %s 段的文字稿标签与库不一致：%s vs %s"
                             % (seg, by_transcript.get(seg), expect))
        # 同一个人（段 1 / 段 3）在文字稿里也是同一个显示名。
        self.assertEqual(by_transcript[1], by_transcript[3],
                         "文字稿里段 1/段 3 的说话人名字不同：%s" % by_transcript)
        self.assertNotEqual(by_transcript[1], by_transcript[2],
                            "文字稿里段 1/段 2 的说话人名字相同：%s" % by_transcript)


# ---------------------------------------------------------------- 工具

def _write_wav(path, seconds=1.0):
    """写一段真的 wav（16k 单声道 int16，与 recorder.py 的产出同格式）。"""
    n = int(16000 * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


if __name__ == "__main__":
    unittest.main()
