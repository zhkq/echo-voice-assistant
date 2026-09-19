# -*- coding: utf-8 -*-
"""API 契约测试（§13.8 安全网第 1 项，P3 前置）

为什么单独立一份
----------------
`.dsh/skills/meeting-record/SKILL.md`（**已入库**，随仓库分发）直接依赖两件事：

  1. `POST /api/meeting/start` / `POST /api/meeting/stop` / `GET /api/meeting/status`
     的路径与返回结构（`{ok, message}` / `active` 字段）；
  2. 端口的发现方式：`ECHO_PORT` → **cwd 相对**的 `data\\echo-port.txt` → 默认 8970。

P3 要动路径层与平台接缝（数据根在 macOS 上会变成 ~/Library/Application Support/ECHO），
这两条契约一旦漂移，技能会**静默失效**——用户说"开始录音"，ECHO 什么都没发生。
所以这里把契约钉死：路由、方法、返回结构、端口文件位置、以及"文档说的和代码做的一致"。

不碰真实 data/：DB 重定向到临时目录，会议动作用替身函数（真 start_meeting 会开麦克风）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                 # noqa: E402
from fastapi.testclient import TestClient                   # noqa: E402

import app.db as db                                         # noqa: E402
from app import paths                                       # noqa: E402
from app import ports                                       # noqa: E402
from app.api import router                                  # noqa: E402
from app.config import settings                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_MD = os.path.join(ROOT, ".dsh", "skills", "meeting-record", "SKILL.md")


def _restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _IsolatedDb:
    """把 db 重定向到临时目录（本文件里所有用例共用同一套 setup/teardown 写法）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-api-contract-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()


class MeetingEndpointContractTests(_IsolatedDb, unittest.TestCase):
    """技能依赖的三个端点的路径 / 方法 / 返回结构。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        """默认把两个有副作用的动作换成安全的替身。

        `meeting.start_meeting()` 真的会去开麦克风、建会议目录、写库；
        `stop_meeting()` 会去转写。契约测试只关心**接口面**，不关心录音，
        所以默认替身成功；需要别的语义时，在用例内部再 patch 一层。
        """
        import app.api as api_mod
        self._start = patch.object(api_mod.meeting, "start_meeting",
                                   return_value=(True, "2026-09-19_10-00-00"))
        self._stop = patch.object(api_mod.meeting, "stop_meeting",
                                  return_value=(True, "2026-09-19_10-00-00"))
        self._start.start()
        self._stop.start()
        self.addCleanup(self._start.stop)
        self.addCleanup(self._stop.stop)

    # ---- 方法与路径 ----------------------------------------------------

    def test_start_is_post_only(self):
        self.assertEqual(self.client.post("/api/meeting/start").status_code, 200)
        self.assertEqual(self.client.get("/api/meeting/start").status_code, 405)

    def test_stop_is_post_only(self):
        self.assertEqual(self.client.post("/api/meeting/stop").status_code, 200)
        self.assertEqual(self.client.get("/api/meeting/stop").status_code, 405)

    def test_status_is_get_only(self):
        self.assertEqual(self.client.get("/api/meeting/status").status_code, 200)
        self.assertEqual(self.client.post("/api/meeting/status").status_code, 405)

    def test_posts_accept_empty_json_body(self):
        """技能发的是 `-Body '{}'`，所以 body 必须可选/可为空对象。"""
        for path in ("/api/meeting/start", "/api/meeting/stop"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 200)

    # ---- 返回结构 ------------------------------------------------------

    def test_start_success_shape(self):
        r = self.client.post("/api/meeting/start", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "message": "2026-09-19_10-00-00"})

    def test_start_busy_shape_and_message(self):
        """已在录音时的提示文案是给用户看的（技能原样回复它）。"""
        import app.api as api_mod
        with patch.object(api_mod.meeting, "start_meeting",
                          return_value=(False, "会议录音已在进行中")):
            r = self.client.post("/api/meeting/start", json={})
        self.assertEqual(r.status_code, 200, "业务性失败必须是 200 + ok=false")
        self.assertEqual(r.json(), {"ok": False, "message": "会议录音已在进行中"})

    def test_stop_success_shape(self):
        r = self.client.post("/api/meeting/stop", json={})
        self.assertEqual(r.json(), {"ok": True, "message": "2026-09-19_10-00-00"})

    def test_stop_idle_shape_and_message(self):
        import app.api as api_mod
        with patch.object(api_mod.meeting, "stop_meeting",
                          return_value=(False, "没有进行中的会议")):
            r = self.client.post("/api/meeting/stop", json={})
        self.assertEqual(r.json(), {"ok": False, "message": "没有进行中的会议"})

    def test_status_keys_are_the_documented_ones(self):
        data = self.client.get("/api/meeting/status").json()
        self.assertEqual(set(data), {"active", "folder", "startedAt", "level", "error"})
        self.assertIsInstance(data["active"], bool)

    def test_endpoints_work_without_token_by_default(self):
        """apiAuthEnabled 默认 False：技能不带 Authorization 也必须能调。"""
        self.assertFalse(settings.get("apiAuthEnabled", False))
        self.assertEqual(self.client.post("/api/meeting/start").status_code, 200)
        self.assertEqual(self.client.get("/api/meeting/status").status_code, 200)


class PortFileContractTests(unittest.TestCase):
    """`data\\echo-port.txt`：名字、位置、读写语义（脚本与技能据此找服务）。"""

    def setUp(self):
        # 环境变量会让"默认位置"这类断言变得不确定，先收起来，tearDown 再还回去
        self._saved = {k: os.environ.get(k) for k in ("ECHO_DATA", "ECHO_ROOT")}
        os.environ.pop("ECHO_DATA", None)
        os.environ.pop("ECHO_ROOT", None)

    def tearDown(self):
        _restore_env(self._saved)

    def test_port_file_name_is_fixed(self):
        """技能与一堆 PowerShell 脚本写死了这个名字，改名等于全线断链。"""
        self.assertEqual(ports.PORT_FILE_NAME, "echo-port.txt")

    def test_accepts_both_install_root_and_data_root(self):
        for root in (paths.echo_root(), paths.data_root()):
            with self.subTest(root=root):
                self.assertEqual(ports.port_file(root), ports.port_file(paths.data_root()))

    def test_default_location_keeps_the_skill_discovery_working(self):
        """默认安装下，端口文件必须在 `<repo>\\data\\echo-port.txt`。

        技能的发现方式是 `Test-Path '.\\data\\echo-port.txt'`（cwd = 仓库根），
        所以数据根一旦在 Windows 上偏离 `{ECHO}/data`，技能就瞎了。
        """
        expected = os.path.join(paths.echo_root(), "data", "echo-port.txt")
        self.assertEqual(ports.port_file(paths.data_root()), expected)
        if os.name == "nt":
            # Windows 是唯一"默认数据根 = 安装根/data"的平台（D18 的分平台默认值）。
            self.assertEqual(paths.data_root(), os.path.join(paths.echo_root(), "data"))

    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(ports.write_port_file(tmp, 18060))
            self.assertEqual(ports.read_port_file(tmp), 18060)
            with open(ports.port_file(tmp), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "18060\n", "脚本要能直接 Read-Raw 再 Trim")

    def test_missing_or_garbage_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ports.read_port_file(tmp, default=8970), 8970)
            # 传安装根写法时 port_file() 会补一层 data/，先备好目录
            data_root = os.path.join(tmp, "data")
            os.makedirs(data_root, exist_ok=True)
            for bad in ("not-a-port\n", "99999\n", "0\n", "-1\n", ""):
                with self.subTest(bad=bad):
                    with open(ports.port_file(data_root), "w", encoding="utf-8") as fh:
                        fh.write(bad)
                    self.assertEqual(ports.read_port_file(data_root, default=8970), 8970)

    def test_resolve_port_writes_the_actual_port(self):
        """让位之后必须写回**实际**端口，否则脚本会去找旧端口（P1 事故类型）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ports, "pick", return_value=(18061, "首选 8970 已占用")):
                port, note = ports.resolve_port(8970, data_root=tmp)
            self.assertEqual(port, 18061)
            self.assertTrue(note)
            self.assertEqual(ports.read_port_file(tmp), 18061)


class SkillDocContractTests(unittest.TestCase):
    """文档说的 = 代码做的：技能文档与路由/端口层交叉核对。"""

    @classmethod
    def setUpClass(cls):
        with open(SKILL_MD, encoding="utf-8") as fh:
            cls.doc = fh.read()
        cls.routes = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}

    def test_skill_doc_is_in_the_repo(self):
        self.assertTrue(os.path.isfile(SKILL_MD), "技能文档已入库，缺失说明被误删")

    def test_documented_endpoints_exist(self):
        expected = {
            ("/api/meeting/start", "POST"),
            ("/api/meeting/stop", "POST"),
            ("/api/meeting/status", "GET"),
        }
        for route in expected:
            with self.subTest(route=route):
                self.assertIn(route, self.routes)

    def test_documented_paths_appear_in_the_doc(self):
        for path in ("/api/meeting/start", "/api/meeting/stop", "/api/meeting/status"):
            with self.subTest(path=path):
                self.assertIn(path, self.doc)

    def test_documented_port_discovery_matches_code(self):
        self.assertIn("ECHO_PORT", self.doc, "环境变量优先级要写在文档里")
        self.assertIn(ports.PORT_FILE_NAME, self.doc)
        # 相对路径写法（cwd = 仓库根）是技能脚本的实际做法
        self.assertIn(".\\data\\echo-port.txt", self.doc)

    def test_documented_default_port_matches_config_default(self):
        """文档里的兜底端口必须与配置默认值一致；两处漂移会让技能连错端口。"""
        from app.config import DEFAULTS
        self.assertEqual(DEFAULTS["serverPort"]["value"], 8970)
        self.assertIn(str(DEFAULTS["serverPort"]["value"]), self.doc)


if __name__ == "__main__":
    unittest.main()
