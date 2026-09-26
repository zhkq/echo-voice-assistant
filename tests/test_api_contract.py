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
from app import meeting as meeting_mod                      # noqa: E402
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


class MeetingErrorFieldContractTests(_IsolatedDb, unittest.TestCase):
    """会议**失败原因**的接口契约：`meetings.error` 必须原样送到面板。

    为什么这条是契约（2026-09-25 用户报的真实故障）：库里原来只有 `status=error`、
    一个字的原因都没有，面板只好自己编一句"麦克风没打开（被占用/权限）或全程无声" ——
    而那一场其实是**会议链路驱动不了配置的转写引擎**。后端现在把原因落在
    `meetings.error` 上，面板要读它，所以这个字段出现在哪些接口、叫什么名字，
    必须有用例钉住：改了列名/忘了带出去，面板就会**又退回**去编假原因。

    面板卡片（`web/app.js` 的会议列表）用的是 **列表接口**，详情页用详情接口 ——
    两个都要有这一个键，所以两个都测。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.mid = db.create_meeting("2026-09-25_10-00-00", started_at="2026-09-25T10:00:00")
        self.reason = ("会议转写引擎「sherpa」不能用于整场会议的文件转写："
                       "这条链路只支持 whisper、sensevoice、qwen3asr、sherpa。")
        db.update_meeting(self.mid, status="error", error=self.reason)

    def tearDown(self):
        db.delete_meeting(self.mid)

    def test_the_detail_endpoint_returns_the_reason(self):
        detail = self.client.get("/api/meetings/%d" % self.mid).json()
        self.assertEqual(detail.get("error"), self.reason,
                         "详情接口必须把 meetings.error 原样带出来：%s" % sorted(detail))

    def test_the_list_endpoint_returns_the_reason_too(self):
        """面板的会议卡片渲染的是**列表**接口的条目（`m.status === "error"` 那一支）。"""
        items = self.client.get("/api/meetings").json()["items"]
        row = [it for it in items if it["id"] == self.mid]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0].get("error"), self.reason,
                         "列表接口必须带 error，否则面板卡片读不到原因：%s" % sorted(row[0]))

    def test_a_successful_meeting_says_nothing_in_that_field(self):
        """能用的路径行为不变：没失败时这个字段是空串（面板据此决定显不显示原因）。"""
        db.update_meeting(self.mid, status="transcribed", error="")
        detail = self.client.get("/api/meetings/%d" % self.mid).json()
        self.assertEqual((detail.get("error") or ""), "")


class HistoryEndpointsContractTests(_IsolatedDb, unittest.TestCase):
    """「历史」两个页的数据面（2026-09-26 第二轮）：

      * `GET /api/commands` 的 **分页 + 关键词 + 时间** 过滤（历史会有几千条，
        一次画完是不可能的，所以过滤必须在 SQL 里、`total` 必须是过滤后的条数）；
      * `GET /api/meetings` 每条要带 **转写档位**（exact/estimated）与 **说话人**，
        以及既有的 **compression**（「已压缩」标记）。

    为什么这些是契约：面板不自己算这些数 —— 它只渲染接口给的东西。
    接口把字段改名/漏带，用户看到的就是"这一格永远是空的"，而且不会报错。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # **会议目录也必须隔离**：不隔离的话 `meeting.meetings_dir()` 会指到这台机器的
        # 真实 `data/meetings`（用户有 9 场真会议）—— 本用例要往会议目录里写 meta.json，
        # 那是绝对不许发生的事（docs/AGENTS.md：真实会议数据只读）。
        cls.meetings_root = os.path.join(cls.tmp, "meetings")
        os.makedirs(cls.meetings_root, exist_ok=True)
        p = patch.object(meeting_mod, "meetings_dir", lambda: cls.meetings_root)
        p.start()
        cls.addClassCleanup(p.stop)
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.ids = [
            db.add_command("帮我把今天的待办整理成列表", source="web", status="done"),
            db.add_command("明天天气怎么样", source="wake", status="done"),
            db.add_command("这段没听清", source="hotkey", status="failed"),
        ]
        db.update_command(self.ids[0], reply="整理好了：3 条待办", duration_ms=12438)
        db.update_command(self.ids[2], error="DSH 未运行")
        # 时间过滤的判据：把中间那条推到很久以前（`ts` 是与接口同一个定宽文本格式，
        # `update_command` 刻意不允许改 ts —— 它不该被业务代码改；这里只能直改一下）
        db._exec("UPDATE commands SET ts=? WHERE id=?", ("2020-01-01 08:00:00", self.ids[1]))

    def tearDown(self):
        for cid in self.ids:
            db._exec("DELETE FROM commands WHERE id=?", (cid,))

    def test_paging_returns_total_and_a_slice(self):
        r = self.client.get("/api/commands?limit=2&offset=0").json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(len(r["items"]), 2)
        r2 = self.client.get("/api/commands?limit=2&offset=2").json()
        self.assertEqual(len(r2["items"]), 1)
        # 倒序：最新的那条在第一个
        self.assertEqual(r["items"][0]["id"], self.ids[2])

    def test_keyword_filters_and_total_follows_the_filter(self):
        r = self.client.get("/api/commands?q=天气").json()
        self.assertEqual(r["total"], 1, "关键词过滤要发生在 SQL 里，total 也得跟着变")
        self.assertEqual([it["id"] for it in r["items"]], [self.ids[1]])
        # 回复正文也在搜索范围内（用户记得住助手回了什么，记不住原话时用得上）
        self.assertEqual(self.client.get("/api/commands?q=整理好了").json()["total"], 1)
        # 失败原因同理
        self.assertEqual(self.client.get("/api/commands?q=DSH").json()["total"], 1)
        # 过滤后没有：total=0 且 items 空（面板据此显示"没有符合条件的指令"）
        empty = self.client.get("/api/commands?q=不存在的词").json()
        self.assertEqual((empty["total"], empty["items"]), (0, []))

    def test_like_wildcards_are_not_special(self):
        """用户搜 `%` 不该变成"匹配一切"（LIKE 的通配符要转义）。"""
        self.assertEqual(self.client.get("/api/commands?q=%25").json()["total"], 0)

    def test_since_filters_by_time(self):
        r = self.client.get("/api/commands?since=2024-01-01 00:00:00").json()
        self.assertEqual(r["total"], 2, "since 之后只剩两条（那条 2020 年的被排掉）")
        old = self.client.get("/api/commands?since=2019-01-01 00:00:00").json()
        self.assertEqual(old["total"], 3)

    def test_command_rows_carry_the_backend_snapshot(self):
        """`meta.backend`（发送当时的智能体）要摊平成 `backend` 字段；老记录是空串。

        面板据此显示"走哪个后端"，**没有就一个字都不显示** —— 所以"没有"必须是空串，
        不能是 `None`（那会渲染成 `后端 null`）。
        """
        cid = db.add_command("走哪条路", source="web",
                             meta={"backend": "独立 harness", "workspace": "C:/x"})
        try:
            row = [it for it in self.client.get("/api/commands").json()["items"]
                   if it["id"] == cid][0]
            self.assertEqual(row["backend"], "独立 harness")
            old = [it for it in self.client.get("/api/commands").json()["items"]
                   if it["id"] == self.ids[0]][0]
            self.assertEqual(old["backend"], "", "老记录没有这个字段 → 空串，不是 None")
        finally:
            db._exec("DELETE FROM commands WHERE id=?", (cid,))

    def test_meeting_list_carries_timestamps_and_speakers(self):
        """会议历史那一行要的三格：转写档位 / 说话人 / 已压缩。"""
        name = "2026-09-26_10-00-00"
        mid = db.create_meeting(name, started_at="2026-09-26T10:00:00")
        db.update_meeting(mid, status="transcribed", segments=1, duration_seconds=60)
        db.replace_speakers(mid, {"S1": "张三", "S2": "说话人2"})
        # `timestampsKinds` 是录音当时写进 meta.json 的快照（这里造一份等价的）
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            fh.write('{"timestampsKinds": {"estimated": 3}}')
        try:
            row = [it for it in self.client.get("/api/meetings").json()["items"]
                   if it["id"] == mid][0]
            self.assertEqual(row["speakerNames"], ["张三", "说话人2"])
            self.assertNotIn("speakers", row,
                             "列表用 speakerNames（字符串数组）；`speakers` 是详情接口那份原始行")
            self.assertEqual(row["timestamps"]["kinds"], {"estimated": 3})
            # 中文由服务端翻好（面板不抄词汇表）；档位原文仍在 kinds 里，可核对
            self.assertIn("估算", row["timestamps"]["label"])
            self.assertIn("compression", row, "「已压缩」那格还在（没被这次改动挤掉）")
            self.assertIsNone(row["compression"], "没压过就是 None")
        finally:
            db.delete_meeting(mid)
            shutil.rmtree(folder, ignore_errors=True)

    def test_meeting_without_a_meta_file_says_nothing(self):
        """老会议没有 `meta.json` → `timestamps` 是 `None`，面板据此**不显示这一格**。

        （不许退化成空壳 `{}`：那会渲染成"时间轴 "半句话，看着像坏了。）
        """
        name = "2026-09-26_11-00-00"
        mid = db.create_meeting(name, started_at="2026-09-26T11:00:00")
        db.update_meeting(mid, status="transcribed", segments=1)
        try:
            row = [it for it in self.client.get("/api/meetings").json()["items"]
                   if it["id"] == mid][0]
            self.assertIsNone(row["timestamps"])
            self.assertEqual(row["speakerNames"], [])
        finally:
            db.delete_meeting(mid)

    def test_delete_meeting_route_still_exists(self):
        """删除会议本来就没有面板入口，但接口必须在（历史页并入不许把它碰掉）。"""
        routes = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
        self.assertIn(("/api/meetings/{mid}", "DELETE"), routes)
        self.assertIn(("/api/meetings/{mid}/retranscribe", "POST"), routes)


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
