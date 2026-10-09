# -*- coding: utf-8 -*-
"""存储路径端点测试（P1）：GET /api/paths/env、POST /api/paths/migrate-meetings。

只依赖项目自身依赖（fastapi/starlette/httpx），不碰真实 data/ 与真实会议目录。
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock

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
        # ⚠️ **用例隔离**（2026-10-09）：开发机的真实设置里 `apiAuthEnabled=true`
        # —— 配对手机时切到 `lan` 档会被**强制**打开鉴权，而且 lan 期间关不掉
        # （见 tests/test_phone_pairing.py）。本类用的是**裸 TestClient**：不带令牌、
        # 对端也不是回环（`is_loopback_peer` 对 "testclient" 判不出 → fail closed），
        # 于是每一个 `/api/*` 都 401，四条用例全红 —— **而接口本身没坏**。
        # 这正是本仓库记过的"红的地方不是坏的地方"：漏的不是接口，是**用例没屏蔽机器状态**。
        #
        # 做法：在**内存里**把这一项遮成 False。刻意**不写库** ——
        # `settings.update({"serverBindMode": "loopback", "apiAuthEnabled": False})`
        # 那种写法（别的用例在用）会改**用户的真实设置**，万一崩在中间就把
        # 手机那条路（LAN + 鉴权）弄断了。这里的补丁随用例结束自动还原。
        real_get = settings.get

        def _get(key, *a, **kw):
            if key == "apiAuthEnabled":
                return False
            return real_get(key, *a, **kw)

        cls._auth_patch = mock.patch.object(settings, "get", side_effect=_get)
        cls._auth_patch.start()
        cls.addClassCleanup(cls._auth_patch.stop)

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

    def test_the_class_shields_itself_from_the_machines_auth_setting(self):
        """开关鉴权**只在本类内被遮住**，不改机器状态。

        这条用例是给后人看的：看到本类"凭空"把 `apiAuthEnabled` 读成 False 时，
        要知道那是**故意的隔离**，不是设置丢了。
        """
        self.assertFalse(settings.get("apiAuthEnabled"),
                         "本类内应当看不到鉴权（否则所有 /api/* 都会 401）")
        # 别的键必须照常透传（补丁只拦这一个 key）
        self.assertIsInstance(settings.get("serverPort"), int)
        self.assertEqual(settings.get("一个不存在的键", "兜底"), "兜底")

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
