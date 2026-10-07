# -*- coding: utf-8 -*-
"""`app/speaker_agg.py`（跨会议说话人聚合建议）的用例。

为什么这个功能需要用例钉住：
  * 它是**认人**的入口 —— 并错了会把两个人当成一个人，而且**改了名、入了库**，
    后患不止一次会议；
  * 判据（阈值、相似度口径）必须与 `voiceprint` **同一套**，两边漂移就会出现
    "聚合说是一个人、认人时又说不像"这种自相矛盾；
  * 它要能**只建议不自动做**（用户拍板）。
"""
import unittest
from unittest.mock import patch

import numpy as np

from app import speaker_agg


def _unit(*vals):
    """造一个归一化向量（不足补零）。"""
    v = np.zeros(256, dtype=np.float32)
    for i, x in enumerate(vals):
        v[i] = x
    n = np.linalg.norm(v)
    return v / n if n else v


def _blob(v):
    return v.astype(np.float32).tobytes()


class SuggestionTests(unittest.TestCase):
    """建议的算与不算。"""

    def setUp(self):
        #: 两场会议：M1 里 S1/S2 其实是同一个人（向量几乎相同），S3 是另一个人
        self.rows = {
            1: {"S1": {"label": "S1", "embedding": _blob(_unit(1.0, 0.0)),
                       "dim": 256, "segments": 5},
                "S2": {"label": "S2", "embedding": _blob(_unit(1.0, 0.02)),
                       "dim": 256, "segments": 2},
                "S3": {"label": "S3", "embedding": _blob(_unit(0.0, 1.0)),
                       "dim": 256, "segments": 9}},
            2: {"S1": {"label": "S1", "embedding": _blob(_unit(1.0, 0.01)),
                       "dim": 256, "segments": 4}},
        }
        self.names = {(1, "S1"): "说话人1", (1, "S2"): "说话人2", (1, "S3"): "说话人3",
                      (2, "S1"): "说话人1"}
        self.meetings = {1: {"id": 1, "name": "M1"}, 2: {"id": 2, "name": "M2"}}

    def _patches(self, voiceprints=(), meetings=None, names=None):
        m = meetings if meetings is not None else self.meetings
        n = names if names is not None else self.names
        return [
            patch.object(speaker_agg.db, "list_meetings", lambda limit=500: list(m.values())),
            patch.object(speaker_agg.db, "get_speaker_embeddings",
                         lambda mid: dict(self.rows.get(mid, {}))),
            patch.object(speaker_agg.db, "_query_one",
                         lambda sql, params=(): {"name": n.get((int(params[0]), params[1]), "")}),
            patch.object(speaker_agg.db, "list_voiceprints", lambda: list(voiceprints)),
            patch.object(speaker_agg, "_threshold", lambda: 0.75),
        ]

    def test_groups_similar_speakers_across_meetings(self):
        """M1/S1、M1/S2、M2/S1 应当并成一组（它们是同一个人）。"""
        ctx = self._patches()
        for p in ctx:
            p.start()
            self.addCleanup(p.stop)
        out = speaker_agg.suggestions()
        self.assertEqual(out["threshold"], 0.75)
        self.assertEqual(len(out["items"]), 1, out)
        labels = {(m["meetingId"], m["label"]) for m in out["items"][0]["members"]}
        self.assertEqual(labels, {(1, "S1"), (1, "S2"), (2, "S1")})
        self.assertEqual(out["items"][0]["meetings"], 2)

    def test_dissimilar_speaker_stays_out(self):
        """S3 与别人不像 → 绝不能出现在建议里（并错了就是认错人）。"""
        ctx = self._patches()
        for p in ctx:
            p.start()
            self.addCleanup(p.stop)
        out = speaker_agg.suggestions()
        for it in out["items"]:
            self.assertNotIn((1, "S3"),
                             {(m["meetingId"], m["label"]) for m in it["members"]})

    def test_already_named_contacts_are_not_suggested(self):
        """已经认过人的说话人（名字是联系人）不再进建议 —— 再提一遍是噪音。"""
        names = dict(self.names)
        names[(1, "S1")] = "张总"
        ctx = self._patches(names=names)
        for p in ctx:
            p.start()
            self.addCleanup(p.stop)
        out = speaker_agg.suggestions()
        for it in out["items"]:
            self.assertNotIn((1, "S1"),
                             {(m["meetingId"], m["label"]) for m in it["members"]})

    def test_single_speaker_yields_nothing(self):
        """只有一个说话人时没有可并的 —— 不能凭空造一条建议出来。"""
        self.rows = {1: {"S1": self.rows[1]["S1"]}}
        ctx = self._patches()
        for p in ctx:
            p.start()
            self.addCleanup(p.stop)
        out = speaker_agg.suggestions()
        self.assertEqual(out["items"], [])

    def test_candidates_are_capped(self):
        """一条建议最多列 MAX_CANDIDATES 个候选（用户要求 1–3）。"""
        # 16 个几乎一样的说话人 → 全并成一组，但只列前 3 个
        self.rows = {1: {}}
        names = {}
        for i in range(16):
            lb = "S%d" % (i + 1)
            self.rows[1][lb] = {"label": lb, "embedding": _blob(_unit(1.0, i * 0.001)),
                                "dim": 256, "segments": i + 1}
            names[(1, lb)] = "说话人%d" % (i + 1)
        self.names = names
        ctx = self._patches(names=names)
        for p in ctx:
            p.start()
            self.addCleanup(p.stop)
        out = speaker_agg.suggestions()
        self.assertTrue(out["items"])
        self.assertLessEqual(len(out["items"][0]["members"]), speaker_agg.MAX_CANDIDATES)


class ApplyTests(unittest.TestCase):
    """改名 + 入库的口径。"""

    def test_refuses_default_or_empty_name(self):
        """默认名/空名不算联系人 —— 不许把"说话人1"当联系人写进库。"""
        ok, msg, _ = speaker_agg.apply([{"meetingId": 1, "label": "S1"}], "")
        self.assertFalse(ok)
        ok, msg, _ = speaker_agg.apply([{"meetingId": 1, "label": "S1"}], "说话人1")
        self.assertFalse(ok)
        self.assertIn("默认名", msg)

    def test_refuses_empty_members(self):
        ok, msg, _ = speaker_agg.apply([], "张总")
        self.assertFalse(ok)

    def test_renames_every_member_and_enrolls_once(self):
        """每个成员都要改名；入库**只写一条**（取片段最多的那场当样本源）。"""
        renamed = []
        enrolled = []
        rows = {1: {"S1": {"label": "S1", "segments": 2}},
                2: {"S1": {"label": "S1", "segments": 9}}}
        with patch.object(speaker_agg.db, "get_speaker_embeddings",
                          lambda mid: dict(rows.get(mid, {}))), \
                patch.object(speaker_agg.db, "rename_speaker",
                             lambda mid, label, name: renamed.append((mid, label, name))), \
                patch.object(speaker_agg.db, "get_meeting",
                             lambda mid: {"id": mid, "name": "M%d" % mid}), \
                patch.object(speaker_agg.db, "get_speaker_embedding",
                             lambda mid, label: {"embedding": _blob(_unit(1.0)),
                                                 "dim": 256}), \
                patch.object(speaker_agg.db, "replace_voiceprint_sample",
                             lambda name, blob, dim=256, meeting_name="", source_label="":
                             enrolled.append((name, meeting_name, source_label))), \
                patch.object(speaker_agg.db, "count_voiceprints", lambda name=None: 1):
            ok, msg, detail = speaker_agg.apply(
                [{"meetingId": 1, "label": "S1"}, {"meetingId": 2, "label": "S1"}], "张总")
        self.assertTrue(ok, msg)
        self.assertEqual([r[:2] for r in renamed], [(1, "S1"), (2, "S1")])
        self.assertTrue(all(r[2] == "张总" for r in renamed))
        self.assertEqual(len(enrolled), 1, "只该写一条样本（片段最多的那场）")
        self.assertEqual(enrolled[0][1], "M2", "样本该取自片段最多的那场")
        self.assertTrue(detail["enrolled"])

    def test_enroll_can_be_turned_off(self):
        """`enroll=False` 时**一个字节都不许写库**（调用方可能只想改名）。"""
        enrolled = []
        with patch.object(speaker_agg.db, "get_speaker_embeddings",
                          lambda mid: {"S1": {"label": "S1", "segments": 1}}), \
                patch.object(speaker_agg.db, "rename_speaker", lambda *a: None), \
                patch.object(speaker_agg.db, "replace_voiceprint_sample",
                             lambda *a, **k: enrolled.append(a)):
            ok, msg, detail = speaker_agg.apply([{"meetingId": 1, "label": "S1"}],
                                                "张总", enroll=False)
        self.assertTrue(ok, msg)
        self.assertEqual(enrolled, [])
        self.assertFalse(detail["enrolled"])


class ThresholdTests(unittest.TestCase):
    """阈值必须与声纹识别同一套（否则"聚合说是一个人、认人说不像"）。"""

    def test_uses_voiceprint_threshold(self):
        with patch("app.voiceprint.thresholds", lambda: {"threshold": 0.62, "margin": 0.1}):
            self.assertEqual(speaker_agg._threshold(), 0.62)

    def test_falls_back_when_unavailable(self):
        with patch("app.voiceprint.thresholds", side_effect=RuntimeError("boom")):
            self.assertEqual(speaker_agg._threshold(), speaker_agg.DEFAULT_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
