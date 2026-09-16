# -*- coding: utf-8 -*-
"""tests/test_voiceprint.py — 常用联系人声纹库的单元测试

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_voiceprint.py

只依赖 numpy（项目已有依赖），不需要 pyannote 模型、不访问网络。
数据库用临时目录，不会碰真实 data/echo.db。

覆盖：
  * pack/unpack：归一化、维数校验、坏数据拒绝
  * 匹配语义：命中 / 低于阈值 / 歧义（阈值与间隔两道门都要真拦得住）
  * identify：一段分离结果按标签与嵌入的对应关系识别
  * duplicate_merges：同一联系人被分成多簇时的合并映射
  * 入库 / 库视图 / 删除（含"同事同人只留最新样本"的替换语义）
  * recognize_meeting 端到端：改名 + 同人合并 + 未命中保持默认名 + 手动名不被覆盖
"""
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db  # noqa: E402
from app import voiceprint as vp  # noqa: E402


def e(i, size=256):
    """第 i 个单位基向量。"""
    v = np.zeros(size, dtype=np.float32)
    v[i] = 1.0
    return v


def unit(*pairs, size=256):
    """按 (下标, 值) 构造归一化向量。"""
    v = np.zeros(size, dtype=np.float32)
    for i, x in pairs:
        v[i] = x
    return v / np.linalg.norm(v)


def pack1(vec):
    blob, dim = vp.pack(vec)
    return blob, dim


class PackTest(unittest.TestCase):
    def test_normalizes_and_roundtrips(self):
        blob, dim = vp.pack(unit((0, 3.0), (1, 4.0)))
        self.assertEqual(dim, 256)
        back = vp.unpack(blob, dim)
        self.assertAlmostEqual(float(np.linalg.norm(back)), 1.0, places=5)
        self.assertAlmostEqual(float(back[0]), 0.6, places=5)

    def test_rejects_dim_mismatch(self):
        blob, _ = vp.pack(e(0))
        self.assertIsNone(vp.unpack(blob, 128))

    def test_rejects_garbage(self):
        self.assertIsNone(vp.unpack(None))
        self.assertIsNone(vp.unpack(b"\x01\x02"))      # 非 4 字节对齐
        self.assertIsNone(vp.unpack(b"", 256))


class MatcherTest(unittest.TestCase):
    def test_hit(self):
        m = vp.VoiceMatcher([(1, "张总", e(0)), (2, "王总", e(1))],
                            threshold=0.65, margin=0.05)
        r = m.match(e(0))
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "张总")
        self.assertAlmostEqual(r["sim"], 1.0, places=3)
        self.assertAlmostEqual(r["runner"], 0.0, places=3)

    def test_below_threshold_is_rejected(self):
        # 查询与「张总」相似度 0.5 < 阈值 0.65 → 不认
        q = unit((0, 0.5), (1, np.sqrt(0.75)))
        m = vp.VoiceMatcher([(1, "张总", e(0))], threshold=0.65)
        r = m.match(q)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "below-threshold")
        # 把阈值降到 0.45 才会认（证明拦截来自阈值这一道门，而不是别的）
        m2 = vp.VoiceMatcher([(1, "张总", e(0))], threshold=0.45)
        self.assertTrue(m2.match(q)["ok"])

    def test_ambiguous_margin_is_rejected(self):
        # 两个联系人：0.80 vs 0.78，间隔 0.02 < 0.05 → 歧义不认
        a = unit((0, 0.80), (1, 0.60))                  # dot(a, e0) = 0.80
        b = unit((0, 0.78), (1, np.sqrt(1 - 0.78 ** 2)))  # dot(b, e0) = 0.78
        m = vp.VoiceMatcher([(1, "张总", a), (2, "王总", b)],
                            threshold=0.65, margin=0.05)
        r = m.match(e(0))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "ambiguous")
        self.assertEqual(r["name"], "张总")
        # 间隔放宽到 0.01 → 认（证明拦截来自间隔这一道门）
        m2 = vp.VoiceMatcher([(1, "张总", a), (2, "王总", b)],
                             threshold=0.65, margin=0.01)
        self.assertTrue(m2.match(e(0))["ok"])

    def test_single_contact_skips_margin(self):
        a = unit((0, 0.80), (1, 0.60))
        m = vp.VoiceMatcher([(1, "张总", a)], threshold=0.65, margin=0.05)
        self.assertTrue(m.match(e(0))["ok"])

    def test_empty_library(self):
        m = vp.VoiceMatcher([])
        self.assertFalse(m.match(e(0))["ok"])
        self.assertEqual(m.match(e(0))["reason"], "empty")

    def test_match_accepts_blob(self):
        m = vp.VoiceMatcher([(1, "张总", e(0))], threshold=0.65)
        blob, _ = vp.pack(e(0))
        self.assertTrue(m.match(blob)["ok"])

    def test_best_of_multiple_samples(self):
        # 同一联系人两条样本（不同会议），取最好成绩
        m = vp.VoiceMatcher([(1, "张总", e(1)), (2, "张总", e(0))],
                            threshold=0.65)
        r = m.match(e(0))
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["sim"], 1.0, places=3)

    def test_non_finite_embedding_never_matches(self):
        """NaN/Inf 不得因为比较恒为 False 而穿透两道门（维护者补充，2026-09-15）。

        NaN 相似度下 `best < threshold` 恒 False、`(best - runner) < margin` 也恒 False，
        等于"NaN 必中"——与"宁可不认"的意图相反。_normalize 现在把非有限值归一成零向量，
        相似度 0，必然低于阈值。
        """
        nan = np.full(256, np.nan, dtype=np.float32)
        inf = np.full(256, np.inf, dtype=np.float32)

        # ① 查询向量含 NaN/Inf → 不认
        m = vp.VoiceMatcher([(1, "张总", e(0))], threshold=0.65)
        self.assertFalse(m.match(nan)["ok"])
        self.assertFalse(m.match(inf)["ok"])

        # ② 库里的样本含 NaN（历史脏数据）→ 打包时就该归一成零向量，之后谁都不会命中
        blob, _ = vp.pack(nan)
        stored = np.frombuffer(blob, dtype=np.float32)
        self.assertTrue(bool(np.all(np.isfinite(stored))))
        self.assertEqual(float(np.linalg.norm(stored)), 0.0)
        m2 = vp.VoiceMatcher([(1, "张总", stored)], threshold=0.65)
        self.assertFalse(m2.match(e(0))["ok"])


class PrivacyDefaultsTest(unittest.TestCase):
    """声纹是生物特征：默认必须关闭（opt-in），防止默认值悄悄开始收集样本。"""

    def test_voiceprint_is_off_by_default(self):
        from app.config import DEFAULTS
        self.assertFalse(DEFAULTS["voiceprintEnabled"]["value"])
        self.assertFalse(DEFAULTS["voiceprintAutoEnroll"]["value"])


class RetentionGateContractTest(unittest.TestCase):
    """没开声纹就不该留下任何声纹样本（生物特征）——用源码契约钉住这道门。

    背景：转写结束本来是无条件留存各说话人的平均嵌入（便于以后「识别本场」），
    那等于"开关关了也照样收集"。维护者要求：整块留存必须在 voiceprint.enabled() 之内。
    """

    def test_speaker_embeddings_retention_is_gated(self):
        import ast

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "meeting.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        gated = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "voiceprint.enabled()" in ast.unparse(node.test):
                if any(isinstance(sub, ast.Call)
                       and "replace_speaker_embeddings" in ast.unparse(sub.func)
                       for sub in ast.walk(node)):
                    gated = True
        self.assertTrue(
            gated,
            "app/meeting.py 里 db.replace_speaker_embeddings 必须整块位于 voiceprint.enabled() 分支内")


class IdentifyTest(unittest.TestCase):
    def test_aligns_label_and_embedding(self):
        # labels[0]="A" 的嵌入是 e(1)（无关），labels[1]="B" 的嵌入是 e(0)（张总）
        embs = np.stack([e(1), e(0)])
        labels = ["A", "B"]
        label_map = {"A": "说话人1", "B": "说话人2"}
        m = vp.VoiceMatcher([(1, "张总", e(0))], threshold=0.65)
        out = vp.identify(embs, labels, label_map, m)
        self.assertTrue(out["说话人2"]["ok"])
        self.assertEqual(out["说话人2"]["name"], "张总")
        self.assertFalse(out["说话人1"]["ok"])
        self.assertEqual(out["说话人1"]["reason"], "below-threshold")

    def test_no_matcher_returns_empty(self):
        embs = np.stack([e(0)])
        self.assertEqual(vp.identify(embs, ["A"], {"A": "说话人1"}, None), {})


class MergeTest(unittest.TestCase):
    def test_merges_to_smallest_number(self):
        self.assertEqual(
            vp.duplicate_merges({"说话人2": "张总", "说话人4": "张总",
                                 "说话人3": "王总"}),
            {"说话人4": "说话人2"})

    def test_order_independent(self):
        self.assertEqual(
            vp.duplicate_merges({"说话人4": "张总", "说话人2": "张总"}),
            {"说话人4": "说话人2"})

    def test_works_for_s_keys(self):
        self.assertEqual(vp.duplicate_merges({"S3": "张总", "S1": "张总"}),
                         {"S3": "S1"})

    def test_no_duplicates(self):
        self.assertEqual(vp.duplicate_merges({"说话人1": "张总", "说话人2": "王总"}), {})


class DefaultNameTest(unittest.TestCase):
    def test_default_names(self):
        for bad in ("", "  ", "说话人1", "说话人12", "S2"):
            self.assertTrue(vp.is_default_name(bad, "S2"), bad)
        for good in ("张总", "王工"):
            self.assertFalse(vp.is_default_name(good, "S2"), good)


class LibraryTest(unittest.TestCase):
    """入库/视图/删除；整类共享一个临时数据库。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="echo-vp-test-")
        cls._old_data, cls._old_db = db.DATA_DIR, db.DB_FILE
        db.DATA_DIR = cls._tmp
        db.DB_FILE = os.path.join(cls._tmp, "test.db")
        db.init()

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_data, cls._old_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        for t in ("lines", "speakers", "speaker_embeddings", "voiceprints",
                  "meetings", "logs"):
            db._exec(f"DELETE FROM {t}")

    def _meeting(self, name, speakers=("S1",), emb=True):
        mid = db.create_meeting(name, started_at="2026-09-15 10:00:00")
        db.replace_speakers(mid, {s: s.replace("S", "说话人") for s in speakers})
        if emb:
            db.replace_speaker_embeddings(
                mid, {s: (*pack1(e(i)), 3) for i, s in enumerate(speakers)})
        return mid

    def test_enroll_and_library_view(self):
        mid = self._meeting("2026-09-15_10-00-00")
        ok, msg = vp.enroll_from_meeting(mid, "S1", "张总")
        self.assertTrue(ok, msg)
        view = vp.library_view()
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["name"], "张总")
        self.assertEqual(view[0]["count"], 1)
        self.assertEqual(view[0]["samples"][0]["meeting_name"], "2026-09-15_10-00-00")
        self.assertEqual(view[0]["samples"][0]["source_label"], "S1")
        self.assertEqual(vp.library_stats(), {"contacts": 1, "samples": 1})

    def test_enroll_rejects_default_names(self):
        mid = self._meeting("m")
        for bad in ("", "   ", "说话人1", "S1"):
            ok, _ = vp.enroll_from_meeting(mid, "S1", bad)
            self.assertFalse(ok, bad)
        self.assertEqual(vp.library_stats()["samples"], 0)

    def test_enroll_requires_embedding(self):
        mid = self._meeting("m", emb=False)
        ok, msg = vp.enroll_from_meeting(mid, "S1", "张总")
        self.assertFalse(ok)
        self.assertIn("样本", msg)

    def test_rename_replaces_sample(self):
        # 同一会议同一说话人只对应一个名字：改成「李四」后不留「张总」的脏样本
        mid = self._meeting("2026-09-15_10-00-00")
        vp.enroll_from_meeting(mid, "S1", "张总")
        vp.enroll_from_meeting(mid, "S1", "李四")
        view = {it["name"]: it for it in vp.library_view()}
        self.assertNotIn("张总", view)
        self.assertEqual(view["李四"]["count"], 1)

    def test_multiple_meetings_accumulate(self):
        mid1 = self._meeting("m1")
        mid2 = self._meeting("m2")
        vp.enroll_from_meeting(mid1, "S1", "张总")
        vp.enroll_from_meeting(mid2, "S1", "张总")
        self.assertEqual(vp.library_stats(), {"contacts": 1, "samples": 2})

    def test_delete_sample_and_contact(self):
        mid = self._meeting("m")
        vp.enroll_from_meeting(mid, "S1", "张总")
        vid = vp.library_view()[0]["samples"][0]["id"]
        ok, _ = vp.delete_sample(vid)
        self.assertTrue(ok)
        self.assertEqual(vp.library_stats()["samples"], 0)
        self.assertFalse(vp.delete_sample(vid)[0])       # 已删的再删要失败

        vp.enroll_from_meeting(mid, "S1", "张总")
        ok, msg = vp.delete_contact("张总")
        self.assertTrue(ok, msg)
        self.assertEqual(vp.library_stats()["samples"], 0)
        self.assertFalse(vp.delete_contact("张总")[0])


class RecognizeTest(unittest.TestCase):
    """「识别本场」端到端（临时库）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="echo-vp-test-")
        cls._old_data, cls._old_db = db.DATA_DIR, db.DB_FILE
        db.DATA_DIR = cls._tmp
        db.DB_FILE = os.path.join(cls._tmp, "test.db")
        db.init()

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_data, cls._old_db
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        for t in ("lines", "speakers", "speaker_embeddings", "voiceprints",
                  "meetings", "logs"):
            db._exec(f"DELETE FROM {t}")

    def _enroll(self, name="张总", vec=None):
        """建一个人工会议把样本存进库。"""
        mid = db.create_meeting("enroll-src", started_at="")
        db.replace_speaker_embeddings(mid, {"S1": (*pack1(vec if vec is not None else e(0)), 2)})
        db.replace_speakers(mid, {"S1": "说话人1"})
        ok, msg = vp.enroll_from_meeting(mid, "S1", name)
        self.assertTrue(ok, msg)
        return mid

    def _meeting(self, name, speakers, emb_map):
        mid = db.create_meeting(name, started_at="")
        db.replace_speakers(mid, {s: s.replace("S", "说话人") for s in speakers})
        db.replace_speaker_embeddings(
            mid, {s: (*pack1(v), 2) for s, v in emb_map.items()})
        db.add_lines(mid, [(1, 0.0, 1.0, s, f"{s} 说了一句") for s in speakers])
        return mid

    def test_rename_and_merge(self):
        self._enroll()                                   # 张总 = e(0)
        near = unit((0, 0.95), (2, np.sqrt(1 - 0.95 ** 2)))   # 与张总 0.95（同一人）
        mid = self._meeting("m1", ["S1", "S2", "S3"],
                            {"S1": e(0), "S2": e(1), "S3": near})
        res = vp.recognize_meeting(mid)
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(res["renamed"], 2)
        self.assertEqual(res["merged"], [{"from": "S3", "to": "S1", "name": "张总"}])
        rows = {s["label"]: s["name"] for s in db.get_speakers(mid)}
        self.assertEqual(rows, {"S1": "张总", "S2": "说话人2"})
        # S3 的转写行已并到 S1
        labels = {ln["speaker_label"] for ln in db.get_lines(mid)}
        self.assertEqual(labels, {"S1", "S2"})

    def test_manual_name_kept_and_no_merge(self):
        self._enroll()
        mid = self._meeting("m2", ["S1", "S2"], {"S1": e(0), "S2": e(0)})
        db.rename_speaker(mid, "S2", "王总")             # 手动名优先
        res = vp.recognize_meeting(mid)
        self.assertTrue(res["ok"])
        rows = {s["label"]: s["name"] for s in db.get_speakers(mid)}
        # S2 的手动名保留；同声纹但含手动名的说话人不做自动合并（不吞用户改的名字）
        self.assertEqual(rows, {"S1": "张总", "S2": "王总"})
        self.assertEqual(res["merged"], [])

    def test_no_embeddings(self):
        self._enroll()
        mid = db.create_meeting("m3", started_at="")
        res = vp.recognize_meeting(mid)
        self.assertFalse(res["ok"])
        self.assertIn("样本", res["message"])

    def test_empty_library(self):
        mid = self._meeting("m4", ["S1"], {"S1": e(0)})
        res = vp.recognize_meeting(mid)
        self.assertFalse(res["ok"])
        self.assertIn("声纹库为空", res["message"])

    def test_disabled_switch(self):
        self._enroll()
        mid = self._meeting("m5", ["S1"], {"S1": e(0)})
        try:
            from app.config import settings
            settings.update({"voiceprintEnabled": False})
            res = vp.recognize_meeting(mid)
            self.assertFalse(res["ok"])
            self.assertIn("关闭", res["message"])
        finally:
            from app.config import settings
            settings.update({"voiceprintEnabled": True})
        self.assertTrue(vp.recognize_meeting(mid)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
