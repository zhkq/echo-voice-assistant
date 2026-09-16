# -*- coding: utf-8 -*-
"""tests/test_api_voiceprints.py — 声纹相关 REST 端点的端到端测试（TestClient）

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_api_voiceprints.py

只依赖项目自身依赖（fastapi/starlette/httpx/numpy），不需要模型或网络。
数据库与会议目录都用临时目录，不碰真实 data/。

覆盖：
  * GET/POST/DELETE /api/voiceprints（列表 / 入库 / 删除单条 / 按联系人删除）
  * POST /api/meetings/{id}/speaker/rename 改名自动入库（默认名不入库）
  * POST /api/meetings/{id}/speaker/recognize 识别本场（改名 + 重导出 transcript.md）
"""
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db  # noqa: E402
from app import meeting as meeting_mod  # noqa: E402
from app import voiceprint as vp  # noqa: E402
from app.config import settings  # noqa: E402


def e(i, size=256):
    v = np.zeros(size, dtype=np.float32)
    v[i] = 1.0
    return v


class ApiVoiceprintTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="echo-vp-api-")
        cls._old = (db.DATA_DIR, db.DB_FILE, meeting_mod.MEETINGS_DIR)
        db.DATA_DIR = cls._tmp
        db.DB_FILE = os.path.join(cls._tmp, "test.db")
        meeting_mod.MEETINGS_DIR = os.path.join(cls._tmp, "meetings")
        os.makedirs(meeting_mod.MEETINGS_DIR, exist_ok=True)
        db.init()
        # 声纹默认是关闭的（opt-in，生物特征数据，见 app/config.py）：这些用例假设已开启，
        # 就显式打开 —— 别依赖默认值，否则默认值一改测试就跟着变红。
        settings.update({"voiceprintEnabled": True, "voiceprintAutoEnroll": True})

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE, meeting_mod.MEETINGS_DIR = cls._old
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        for t in ("lines", "speakers", "speaker_embeddings", "voiceprints",
                  "meetings", "logs"):
            db._exec(f"DELETE FROM {t}")

    def _meeting(self, name, speakers, emb_map):
        mid = db.create_meeting(name, started_at="2026-09-15 10:00:00")
        os.makedirs(os.path.join(meeting_mod.MEETINGS_DIR, name), exist_ok=True)
        db.replace_speakers(mid, {s: s.replace("S", "说话人") for s in speakers})
        db.replace_speaker_embeddings(
            mid, {s: (*vp.pack(v), 2) for s, v in emb_map.items()})
        db.add_lines(mid, [(1, 0.0, 1.0, s, f"{s} 说了一句") for s in speakers])
        return mid

    def test_voiceprints_lifecycle(self):
        mid = self._meeting("m1", ["S1"], {"S1": e(0)})
        # 空库
        r = self.client.get("/api/voiceprints").json()
        self.assertEqual((r["contacts"], r["total"]), (0, 0))
        self.assertEqual(r["items"], [])
        self.assertTrue(r["enabled"])
        self.assertAlmostEqual(r["threshold"], 0.65, places=6)
        # 入库
        r = self.client.post("/api/voiceprints/enroll",
                             json={"meeting_id": mid, "label": "S1",
                                   "name": "张总"}).json()
        self.assertTrue(r["ok"], r)
        self.assertIn("张总", r["message"])
        # 列表
        r = self.client.get("/api/voiceprints").json()
        self.assertEqual((r["contacts"], r["total"]), (1, 1))
        self.assertEqual(r["items"][0]["name"], "张总")
        vid = r["items"][0]["samples"][0]["id"]
        # 删除单条
        r = self.client.delete(f"/api/voiceprints/{vid}").json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.client.get("/api/voiceprints").json()["total"], 0)
        # 按联系人删除：库里已空 → 返回失败说明
        r = self.client.delete("/api/voiceprints", params={"name": "张总"}).json()
        self.assertFalse(r["ok"])

    def test_rename_auto_enrolls(self):
        mid = self._meeting("m2", ["S1"], {"S1": e(0)})
        r = self.client.post(f"/api/meetings/{mid}/speaker/rename",
                             json={"label": "S1", "name": "张总"}).json()
        self.assertTrue(r["ok"], r)
        self.assertIn("入库", r["message"])
        self.assertEqual(self.client.get("/api/voiceprints").json()["total"], 1)
        rows = {s["label"]: s["name"] for s in db.get_speakers(mid)}
        self.assertEqual(rows, {"S1": "张总"})

    def test_rename_to_default_name_not_enrolled(self):
        mid = self._meeting("m3", ["S1"], {"S1": e(0)})
        r = self.client.post(f"/api/meetings/{mid}/speaker/rename",
                             json={"label": "S1", "name": "说话人1"}).json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["message"], "")
        self.assertEqual(self.client.get("/api/voiceprints").json()["total"], 0)

    def test_recognize_endpoint(self):
        # 先入库「张总」（等于 e(0) 的声纹）
        src = self._meeting("src", ["S1"], {"S1": e(0)})
        self.client.post("/api/voiceprints/enroll",
                         json={"meeting_id": src, "label": "S1", "name": "张总"})
        mid = self._meeting("m4", ["S1", "S2"], {"S1": e(0), "S2": e(1)})
        r = self.client.post(f"/api/meetings/{mid}/speaker/recognize").json()
        self.assertTrue(r["ok"], r.get("message"))
        self.assertEqual(r["renamed"], 1)
        rows = {s["label"]: s["name"] for s in db.get_speakers(mid)}
        self.assertEqual(rows, {"S1": "张总", "S2": "说话人2"})
        # 识别后 re-export transcript.md，里面带联系人名
        path = os.path.join(meeting_mod.MEETINGS_DIR, "m4", "transcript.md")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            self.assertIn("张总", f.read())

    def test_recognize_404_for_missing_meeting(self):
        r = self.client.post("/api/meetings/999999/speaker/recognize")
        self.assertEqual(r.status_code, 404)

    def test_enroll_endpoint_failure_message(self):
        mid = self._meeting("m5", ["S1"], {"S1": e(0)})
        r = self.client.post("/api/voiceprints/enroll",
                             json={"meeting_id": mid, "label": "S1",
                                   "name": "说话人1"}).json()
        self.assertFalse(r["ok"])
        self.assertIn("默认", r["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
