# -*- coding: utf-8 -*-
"""存储路径端点测试（P1）：GET /api/paths/env、POST /api/paths/migrate-meetings。

只依赖项目自身依赖（fastapi/starlette/httpx），不碰真实 data/ 与真实会议目录。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from app.api import router                                     # noqa: E402
from app.config import settings                                # noqa: E402


def _mk_meeting(root, name):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "01.wav"), "wb") as fh:
        fh.write(b"x")


class PathsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)
        cls.tmp = tempfile.mkdtemp(prefix="echo-api-paths-")
        cls.src = os.path.join(cls.tmp, "old")
        cls.dst = os.path.join(cls.tmp, "new")
        os.makedirs(cls.src, exist_ok=True)
        _mk_meeting(cls.src, "2026-09-18_10-00-00")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_env_endpoint_reports_every_root(self):
        r = self.client.get("/api/paths/env")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual([x["name"] for x in data["roots"]],
                         ["ECHO_BASE", "ECHO", "DATA", "MEETINGS", "MODELS",
                          "AIDE", "DSH", "DSH_HOME"])
        self.assertIn("configured", data)
        self.assertIn("port", data)
        self.assertIsInstance(data["meetingDirs"], int)

    def test_dry_run_reports_without_moving(self):
        r = self.client.post("/api/paths/migrate-meetings",
                             json={"target": self.dst, "source": self.src, "dryRun": True})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["moved"], ["2026-09-18_10-00-00"])
        self.assertTrue(os.path.isdir(os.path.join(self.src, "2026-09-18_10-00-00")),
                        "dry-run 不能真的搬")

    def test_migrate_moves_then_reports_skipped(self):
        dst2 = os.path.join(self.tmp, "new2")
        r = self.client.post("/api/paths/migrate-meetings",
                             json={"target": dst2, "source": self.src})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertTrue(os.path.isdir(os.path.join(dst2, "2026-09-18_10-00-00")))
        # 再来一次：源已经空了，不该报错
        r2 = self.client.post("/api/paths/migrate-meetings",
                              json={"target": dst2, "source": self.src})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["moved"], [])

    def test_migrate_rejects_bad_target(self):
        if os.name != "nt":
            self.skipTest("磁盘根用例仅在 Windows 上有意义")
        r = self.client.post("/api/paths/migrate-meetings",
                             json={"target": "C:\\", "source": self.src})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertTrue(r.json()["error"])

    def test_no_target_and_no_config_is_a_clean_failure(self):
        if settings.get("meetingsDir"):
            self.skipTest("本机配置里已设置 meetingsDir，跳过该分支")
        r = self.client.post("/api/paths/migrate-meetings", json={"dryRun": True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("meetingsDir", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
