# -*- coding: utf-8 -*-
"""独立 DeepSeek Harness 适配器与进程管理的测试（2026-09-19）

用户要求："在智能体那里增加一个新的 agent 类型，然后随着 echo 一起启动"。

实测背景（docs/独立harness接入.md）：
  * 独立发行版是 npm 包 `@deepseek-ai/dsh`（0.1.5-rc.2，与 DSH Desktop 自带的 dsh 同版本）；
  * `dsh web --port N --no-open` 起的就是 Desktop 那套 web profile —— `/api` 接口面完全一致
    （session/* workspace/* settings/*），ECHO 现有请求体原封不动可用；
  * 鉴权不同：Desktop 校验"逆向出来的签名 Cookie"，独立 harness 用启动时打印的 token
    访问一次 `/?token=…` 换 Cookie。

本文件用一个假 harness（ThreadingHTTPServer）钉住这条链路：
  登录换 Cookie、登录失败的人话原因、401 自动重登重试、可用性判据、以及"随 ECHO 启动"
  的进程管理（幂等 / 冷却 / token 解析 / 只停自己起的）。
"""
import json
import os
import shutil
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                              # noqa: E402
from app import harness_proc                                     # noqa: E402
from app.config import DEFAULTS, settings                         # noqa: E402

TOKEN = "probe-token-1234"
COOKIE_NAME = "dsh-auth-TESTSERVER"
COOKIE_NAME_PREFIX = "dsh-auth-"
COOKIE_VALUE = "v1.test.cookie"


class _HarnessStub(BaseHTTPRequestHandler):
    """最小假 harness：`/?token=` 换 Cookie；`/api/*` 校验 Cookie 并回 JSON-RPC 信封。"""

    server_version = "fake-harness/0.1"
    state = {"logins": 0, "expire_next": False, "rpc": [], "gets": []}

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                            # noqa: N802
        if self.path.startswith("/?token="):
            tok = self.path.split("token=", 1)[1]
            if tok != TOKEN:
                self._json(403, {"error": "bad token"})
                return
            _HarnessStub.state["logins"] += 1
            # 真实 harness 就是这么答的：**303 + Set-Cookie，Location: /**
            # （跟去 / 就丢了这枚 Cookie → 401；见 harness_agent._login 的注释）
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly" % (COOKIE_NAME, COOKIE_VALUE))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # 探活用：任何响应都算"在监听"；这里也用来证明"登录没有跟跳转"
        _HarnessStub.state["gets"].append(self.path)
        self._json(401, {"error": "unauthorized"})

    def do_POST(self):                                           # noqa: N802
        if not self.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        cookie = self.headers.get("Cookie") or ""
        if _HarnessStub.state["expire_next"] or (COOKIE_NAME + "=" + COOKIE_VALUE) not in cookie:
            _HarnessStub.state["expire_next"] = False
            self._json(401, {"error": "unauthorized"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        method = req.get("method", "")
        _HarnessStub.state["rpc"].append(method)
        self._json(200, {"type": "server-response", "rpcId": req.get("rpcId"),
                         "result": {"ok": True, "value": {"items": []}}})


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _HarnessStub)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _HarnessStub.state.update(logins=0, expire_next=False, rpc=[], gets=[])
        harness_proc.set_token("")
        self._ports = patch.object(harness_proc, "port", lambda: self.port)
        self._ports.start()
        self.addCleanup(self._ports.stop)
        self._tok = patch.object(harness_proc, "token", lambda: TOKEN)
        self._tok.start()
        self.addCleanup(self._tok.stop)


class HarnessAuthTests(_Base):
    def _agent(self):
        from app.agents.harness_agent import HarnessAgent
        a = HarnessAgent(base_url="http://127.0.0.1:%d" % self.port)
        a._cookie, a._cookie_ts = None, 0.0
        return a

    def test_login_exchanges_token_for_cookie(self):
        a = self._agent()
        cookie = a._cookie_header()
        self.assertTrue(cookie.startswith(COOKIE_NAME + "="), cookie)
        self.assertEqual(_HarnessStub.state["logins"], 1)

    def test_login_303_is_not_followed(self):
        """harness 的登录响应是 303（Set-Cookie 只在 303 上）。

        跟跳转就会丢掉 Cookie 并撞 401 —— 这正是最初那个"token 明明对却报 401"的坑，
        所以这里把"没有请求过 /"钉住。
        """
        a = self._agent()
        a._cookie_header()
        self.assertNotIn("/", _HarnessStub.state["gets"],
                         "登录不该跟随 303 跳到 /（会丢 Set-Cookie）")

    def test_rpc_works_after_login(self):
        a = self._agent()
        res = a.rpc("session/list", {"_request": {}}, timeout=5)
        self.assertIsInstance(res, dict)
        self.assertEqual(_HarnessStub.state["rpc"], ["session/list"])

    def test_no_token_falls_back_to_the_home_secret(self):
        """没有 token 也能连：用 harness **自己家目录**里的 browser-session 密钥铸 Cookie。

        2026-09-19 实测发现独立 harness 的 `.credentials.yaml` 与桌面版同构，
        于是"用户自己起的实例 / token 轮换"这些情况都不再需要用户去拿 token。
        """
        import base64
        import tempfile

        home = tempfile.mkdtemp(prefix="echo-hn-secret-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        secret = base64.urlsafe_b64encode(b"\x02" * 32).rstrip(b"=").decode()
        with open(os.path.join(home, ".credentials.yaml"), "w", encoding="utf-8") as fh:
            fh.write("version: 1\nrecords:\n  client-connection/browser-session:\n"
                     "    kind: secret\n    payload:\n      version: 1\n"
                     "      secret: %s\n" % secret)
        with patch.object(harness_proc, "home", lambda: home), \
                patch.object(harness_proc, "token", lambda: ""):
            a = self._agent()
            cookie = a._cookie_header()
        self.assertTrue(cookie.startswith(COOKIE_NAME_PREFIX), cookie)
        self.assertIn("=v1.", cookie)

    def test_no_token_and_no_file_says_how_to_set_it(self):
        """两条路都没有时，错误信息要指到面板上那一栏（而不是含糊的 401）。"""
        import tempfile
        home = tempfile.mkdtemp(prefix="echo-hn-empty-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with patch.object(harness_proc, "home", lambda: home), \
                patch.object(harness_proc, "token", lambda: ""):
            with self.assertRaises(Exception) as cm:
                self._agent()._cookie_header()
        msg = str(cm.exception)
        self.assertIn("harness 访问 token", msg)
        self.assertIn("选中本智能体", msg)

    def test_token_wins_over_the_home_secret(self):
        """有 token 就走 token（ECHO 自己拉起的实例，token 才是最新鲜的）。"""
        called = {"secret": 0}
        a = self._agent()
        with patch.object(type(a), "_secret_cookie", lambda self: called.__setitem__("secret", 1) or "x=1"):
            a._cookie_header()
        self.assertEqual(called["secret"], 0, "有 token 时不该走密钥退路")

    def test_401_triggers_relogin_and_retry(self):
        """cookie 过期不该让一次命令白跑：401 → 重登一次 → 成功。"""
        a = self._agent()
        a._cookie_header()                       # 先登录一次
        _HarnessStub.state["expire_next"] = True  # 下一次 RPC 假装 cookie 过期
        res = a.rpc("session/create", {"request": {}}, timeout=5)
        self.assertIsInstance(res, dict)
        self.assertEqual(_HarnessStub.state["logins"], 2, "应当重登了一次")
        self.assertEqual(_HarnessStub.state["rpc"], ["session/create"])

    def test_bad_token_gives_a_human_reason(self):
        """token 错了、家目录里也没有可用密钥时 → 给出人话（而不是裸 403）。"""
        import tempfile
        home = tempfile.mkdtemp(prefix="echo-hn-bad-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with patch.object(harness_proc, "home", lambda: home), \
                patch.object(harness_proc, "token", lambda: "wrong-token"):
            a = self._agent()
            with self.assertRaises(Exception) as cm:
                a._cookie_header()
        self.assertIn("token", str(cm.exception))

    def test_missing_token_tells_you_what_to_do(self):
        import tempfile
        home = tempfile.mkdtemp(prefix="echo-hn-none-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with patch.object(harness_proc, "home", lambda: home), \
                patch.object(harness_proc, "token", lambda: ""):
            a = self._agent()
            with self.assertRaises(Exception) as cm:
                a._cookie_header()
        msg = str(cm.exception)
        self.assertIn("选中", msg, "要告诉用户怎么让它有 token")
        self.assertIn("harness 访问 token", msg, "并指出面板上那一栏")

    def test_available_reports_live_service(self):
        a = self._agent()
        ok, why = a.available()
        self.assertTrue(ok, why)
        self.assertIn(str(self.port), why)

    def _dead_agent(self):
        """指向一个没人监听的端口（模拟"harness 没在跑"）。"""
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        from app.agents.harness_agent import HarnessAgent
        a = HarnessAgent(base_url="http://127.0.0.1:%d" % dead)
        a._cookie, a._cookie_ts = None, 0.0
        return a

    def test_available_explains_when_not_running(self):
        with patch.object(harness_proc, "online", lambda timeout=1.0: False), \
                patch.object(harness_proc, "requested", lambda: False):
            ok, why = self._dead_agent().available()
        self.assertFalse(ok)
        self.assertIn("没在运行", why)
        self.assertIn("选为当前智能体", why, "要给出可操作的做法")

    def test_available_explains_after_being_requested(self):
        with patch.object(harness_proc, "online", lambda timeout=1.0: False), \
                patch.object(harness_proc, "requested", lambda: True):
            ok, why = self._dead_agent().available()
        self.assertFalse(ok)
        self.assertIn("harness.log", why, "要指向日志，并提示 npx 全路径这个常见坑")


class HarnessAgentRegistryTests(unittest.TestCase):
    """注册表/面板数据：新 agent 出现、配置项到位、token 不回显。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = None
        cls._old = (db.DATA_DIR, db.DB_FILE)
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="echo-harness-")
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_harness_is_registered_and_listed(self):
        from app import agents
        self.assertIn("harness", agents.names())
        got = {a["name"]: a for a in agents.list_agents()}
        h = got["harness"]
        self.assertTrue(h["displayName"])
        # **展示名必须与桌面版不同**：本适配器继承 DshAgent，漏写 display_name 就会继承父类的
        # "DSH Desktop"，面板上两行一模一样（2026-09-22 用户看着截图报的就是这个）。
        self.assertNotEqual(h["displayName"], got["dsh"]["displayName"],
                            "标准版 harness 不能与 DSH Desktop 同名（漏写就继承父类）")
        self.assertIn("标准版", h["displayName"])
        from app.agents.dsh_agent import DshAgent
        from app.agents.harness_agent import HarnessAgent
        self.assertEqual(HarnessAgent.display_name, h["displayName"])
        self.assertNotEqual(HarnessAgent.display_name, DshAgent.display_name,
                            "必须在子类里显式覆盖 display_name")
        self.assertFalse(h["enabled"], "默认不启用（选了才随 ECHO 启动）")
        keys = {s["key"] for s in h["settings"]}
        self.assertIn("harnessCommand", keys)
        self.assertIn("harnessPort", keys)
        # 密钥行要下发（面板得有那一栏让用户填），但只报"配没配"，值永不出接口
        token_row = [s for s in h["settings"] if s["key"] == "harnessToken"]
        self.assertEqual(len(token_row), 1, "token 行应在，供面板渲染密码框")
        self.assertTrue(token_row[0].get("secret"))
        self.assertEqual(token_row[0].get("value"), "")
        self.assertIn("hasValue", token_row[0])

    def test_config_keys_are_agent_group_and_hidden(self):
        for key in ("agentHarnessEnabled", "harnessHome", "harnessPort",
                    "harnessCommand", "harnessToken"):
            with self.subTest(key=key):
                meta = DEFAULTS[key]
                self.assertEqual(meta["grp"], "agent")
                self.assertTrue(meta.get("hidden"))
        self.assertTrue(DEFAULTS["harnessToken"].get("secret"))
        self.assertIn("harness", DEFAULTS["agentBackend"]["options"])


class HarnessProcTests(unittest.TestCase):
    """进程管理：随 ECHO 启动的判据、幂等、token 解析、只停自己起的。"""

    def setUp(self):
        harness_proc.set_token("")
        harness_proc._proc, harness_proc._proc_pid = None, 0
        harness_proc._last_launch = 0.0

    def test_requested_needs_both_selection_and_switch(self):
        """判定是"与"：切走就停（否则 node 会一直挂着 —— 实测踩过）。"""
        def fake(**kw):
            return lambda k, d=None: kw.get(k, d)
        # 选中 + 开关开 → 起
        with patch("app.config.settings.get",
                   fake(agentBackend="harness", agentHarnessEnabled=True)):
            self.assertTrue(harness_proc.requested())
        # 切走（开关还开着）→ 停
        with patch("app.config.settings.get",
                   fake(agentBackend="dsh", agentHarnessEnabled=True)):
            self.assertFalse(harness_proc.requested())
        # 选中但被关掉 → 停
        with patch("app.config.settings.get",
                   fake(agentBackend="harness", agentHarnessEnabled=False)):
            self.assertFalse(harness_proc.requested())

    def test_online_true_for_any_http_response(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _HarnessStub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with patch.object(harness_proc, "port", lambda: srv.server_address[1]):
                self.assertTrue(harness_proc.online())
        finally:
            srv.shutdown()

    def test_ensure_running_is_idempotent_when_online(self):
        with patch.object(harness_proc, "online", lambda timeout=1.0: True), \
                patch("subprocess.Popen") as popen:
            ok, msg = harness_proc.ensure_running()
        self.assertTrue(ok)
        self.assertIn("已在运行", msg)
        popen.assert_not_called()

    def test_ensure_running_reports_missing_npx(self):
        with patch.object(harness_proc, "online", lambda timeout=1.0: False), \
                patch.object(harness_proc, "command", lambda: "definitely-not-a-real-npx web"), \
                patch("shutil.which", lambda name: None):
            ok, msg = harness_proc.ensure_running()
        self.assertFalse(ok)
        self.assertIn("Node", msg, "要说清需要 Node / npx")

    def test_token_is_parsed_from_child_output(self):
        line = "dsh web: http://127.0.0.1:43199/?token=%s" % TOKEN
        m = harness_proc.TOKEN_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), TOKEN)

    def test_stop_does_nothing_for_foreign_instances(self):
        with patch.object(harness_proc, "_load_pid", lambda: 0):
            ok, msg = harness_proc.stop()
        self.assertTrue(ok)
        self.assertIn("未做处理", msg)

    def test_stop_kills_the_adopted_instance(self):
        """ECHO 重启后接手别人起的实例（pid 文件还在）→ 切走时必须能停掉它。

        实测踩过：只认内存里的 `_proc` 时，切到 DSH 后 43199 一直挂着。
        """
        state = {"killed": []}

        def fake_kill(pid):
            state["killed"].append(pid)

        with patch.object(harness_proc, "_load_pid", lambda: 4242), \
                patch.object(harness_proc, "online",
                             lambda timeout=1.0: not state["killed"]), \
                patch.object(harness_proc, "_kill_tree", fake_kill), \
                patch.object(harness_proc, "_listener_pid", lambda p: 4242), \
                patch.object(harness_proc, "_clear_pid", lambda: None):
            ok, msg = harness_proc.stop()
        self.assertTrue(ok, msg)
        self.assertIn(4242, state["killed"], "要按 pid 文件杀掉那个实例")
        self.assertIn("已停止", msg)

    def test_reserved_ports_are_refused(self):
        """不许占 Desktop 的 43120 / ECHO 自己的 18060 —— 占了就是互相打架。"""
        for p in (43120, 18060):
            with self.subTest(port=p):
                with patch.object(harness_proc, "port", lambda p=p: p):
                    self.assertTrue(harness_proc.port_conflict())
                    ok, msg = harness_proc.ensure_running()
                    self.assertFalse(ok)
                    self.assertIn("端口", msg)

    def test_status_detail_speaks_plainly(self):
        with patch.object(harness_proc, "online", lambda timeout=1.0: False), \
                patch.object(harness_proc, "requested", lambda: False):
            status, detail = harness_proc.status_detail()
        self.assertEqual(status, "disabled")
        self.assertIn("未启用", detail)

    def test_home_is_separate_from_desktop(self):
        """独立 harness 的 DSH_HOME 不能是 Desktop 的 ~/.dsh（否则两边抢同一份会话）。"""
        h = harness_proc.home()
        self.assertTrue(h)
        self.assertNotIn(os.path.join(".dsh", ""), h + os.sep)


class HarnessBootTests(unittest.TestCase):
    """随 ECHO 启动：boot 里有这个组件；没选中时不拉起。"""

    def test_boot_registers_the_harness_component(self):
        from app import boot
        boot.setup()
        ids = [c["id"] for c in boot.snapshot()["components"]]
        self.assertIn("harness", ids)
        c = [x for x in boot.snapshot()["components"] if x["id"] == "harness"][0]
        self.assertTrue(c["can_start"])
        self.assertTrue(c["can_stop"], "ECHO 自己起的进程要能停")

    def test_start_step_skips_when_not_requested(self):
        from app import boot
        seen = {}
        with patch.object(harness_proc, "requested", lambda: False), \
                patch.object(harness_proc, "_load_pid", lambda: 0), \
                patch.object(harness_proc, "started_by_echo", lambda: False):
            boot._start_harness(lambda **kw: seen.update(kw))
        self.assertEqual(seen.get("status"), "disabled")

    def test_start_step_cleans_up_a_leftover_when_not_requested(self):
        """没选中但上次是我们起的 → 启动时收尾（自愈，免得 node 一直挂着）。"""
        from app import boot
        seen = {}
        with patch.object(harness_proc, "requested", lambda: False), \
                patch.object(harness_proc, "_load_pid", lambda: 4242), \
                patch.object(harness_proc, "stop", lambda: (True, "已停止独立 harness")):
            boot._start_harness(lambda **kw: seen.update(kw))
        self.assertEqual(seen.get("status"), "disabled")
        self.assertIn("收尾", seen.get("detail", ""))

    def test_start_step_launches_when_requested(self):
        from app import boot
        seen = {}
        with patch.object(harness_proc, "requested", lambda: True), \
                patch.object(harness_proc, "ensure_running", lambda: (True, "已启动(测试)")):
            boot._start_harness(lambda **kw: seen.update(kw))
        self.assertEqual(seen.get("status"), "online")
        self.assertIn("测试", seen.get("detail", ""))


class ClientFollowsSelectionTests(unittest.TestCase):
    """命令/纪要必须发给**当前选中的**智能体（2026-09-19 用户实测反馈：

    "我配置了独立 dsh 但是命令还是发到了 desktop" —— 原因是 `app/dsh.get_client()`
    固定返回 DSH Desktop 适配器，而命令路径（assistant/meeting/worklog）都用它。
    另一个同样致命的暗坑：`dsh_sessions` 里存着**旧后端**的 session_id，
    切过去以后命令会带着那个 id 发（新后端根本没这个会话）→ 会话要按后端归属。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="echo-agent-owner-")
        self._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        settings.seed_defaults()
        from app import agents
        agents.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        from app import agents
        agents.reset()
        db.DATA_DIR, db.DB_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_client_follows_the_selected_agent(self):
        from app.agents import dsh_agent as _dsh_mod
        from app.agents import harness_agent as _harness_mod
        from app.dsh import get_client, get_desktop_client

        def fake_get(key, default=None):
            if key == "agentBackend":
                return "harness"
            if key == "agentHarnessEnabled":
                return True
            return default

        from app import agents
        with patch("app.config.settings.get", fake_get), \
                patch.object(_harness_mod.HarnessAgent, "available",
                             lambda self, probe=False: (True, "")), \
                patch.object(_dsh_mod.DshAgent, "available",
                             lambda self, probe=False: (True, "")):
            agents.reset()
            self.assertEqual(get_client().name, "harness",
                             "选了独立 harness，命令就该发给它")
        # 桌面版进程管理仍要指名 DSH（与用户选谁无关）
        self.assertEqual(get_desktop_client().name, "dsh")

    def test_session_is_invalidated_when_backend_changes(self):
        """同一个 kind 的会话属于另一个后端时 → 当没有会话。"""
        db.upsert_session("command", "session-from-desktop", "命令会话", agent="dsh")
        self.assertIsNotNone(db.get_session("command"))
        self.assertIsNotNone(db.get_session("command", agent="dsh"))
        self.assertIsNone(db.get_session("command", agent="harness"),
                          "换后端后不能复用旧 session_id")

    def test_new_session_records_its_owner(self):
        db.upsert_session("command", "session-x", "命令会话", agent="harness")
        row = dict(db.get_session("command"))
        self.assertEqual(row["agent"], "harness")
        self.assertEqual(row["session_id"], "session-x")

    def test_ensure_session_creates_a_new_one_for_the_new_backend(self):
        """端到端语义：库里是 Desktop 的会话 → 切到 harness 后要**新建**一个。"""
        from app.agents.harness_agent import HarnessAgent
        db.upsert_session("command", "session-desktop", "命令会话", agent="dsh")
        a = HarnessAgent()
        created = {"n": 0}

        def fake_new(self, ws):
            created["n"] += 1
            return "session-harness"

        with patch.object(HarnessAgent, "_new_default_session", fake_new):
            sid = a.ensure_command_session("测试一下")
        self.assertEqual(sid, "session-harness")
        self.assertEqual(created["n"], 1, "必须新建，而不是沿用 Desktop 的会话")
        row = dict(db.get_session("command"))
        self.assertEqual(row["agent"], "harness")
        self.assertEqual(row["session_id"], "session-harness")

    def test_meeting_session_is_also_owner_scoped(self):
        db.upsert_meeting_session(7, "m-session", "ws-1", agent="dsh")
        self.assertIsNotNone(db.get_meeting_session(7, agent="dsh"))
        self.assertIsNone(db.get_meeting_session(7, agent="harness"))
        self.assertIsNotNone(db.get_meeting_session(7), "不传 agent 时保持旧语义（归档要用）")

    def test_targets_endpoint_follows_agent_and_degrades(self):
        """命令目标下拉的数据源也要跟着选中的智能体；取不到时降级而不是 5xx。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        class _Fake:
            name = "harness"

            def list_workspaces(self):
                return [{"workspaceId": "w1", "title": "T"}]

            def list_sessions_for(self):
                return [{"sessionId": "s1"}]

        with patch("app.dsh.get_client", lambda: _Fake()):
            body = client.get("/api/dsh/targets").json()
        self.assertEqual(body["agent"], "harness")
        self.assertEqual(len(body["workspaces"]), 1)

        class _Boom:
            name = "harness"

            def list_workspaces(self):
                from app.agents.dsh_agent import DshError
                raise DshError("没有 harness 访问 token")

            def list_sessions_for(self):
                return []

        with patch("app.dsh.get_client", lambda: _Boom()):
            resp = client.get("/api/dsh/targets")
        self.assertEqual(resp.status_code, 200, "取不到目标不该让下拉炸掉")
        body = resp.json()
        self.assertEqual(body["workspaces"], [])
        self.assertIn("token", body["note"])


class HarnessBrowserOpenTests(unittest.TestCase):
    """仪表盘那个"用浏览器打开它的 Web 端"的小图标（2026-09-19 用户要求）。

    要点：URL 里的 token 是密钥 → **服务端拼好直接调系统浏览器**，响应里只回不含 token 的地址。
    """

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_opens_the_browser_with_the_token_url(self):
        opened = []
        with patch.object(harness_proc, "online", lambda timeout=1.0: True), \
                patch.object(harness_proc, "base_url", lambda: "http://127.0.0.1:43199"), \
                patch.object(harness_proc, "token", lambda: "tok-abc"), \
                patch("app.platform.shell_open",
                      lambda url, params="": opened.append(url) or True):
            body = self.client.post("/api/harness/browser").json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(opened, ["http://127.0.0.1:43199/?token=tok-abc"],
                         "要用带 token 的 URL 打开（浏览器才能直接进界面）")
        self.assertNotIn("tok-abc", str(body), "响应里不许回 token")

    def test_online_without_token_still_opens(self):
        """能拿到家目录密钥铸 Cookie 的场景：没有 token 时打开裸地址也够用。"""
        opened = []
        with patch.object(harness_proc, "online", lambda timeout=1.0: True), \
                patch.object(harness_proc, "base_url", lambda: "http://127.0.0.1:43199"), \
                patch.object(harness_proc, "token", lambda: ""), \
                patch("app.platform.shell_open",
                      lambda url, params="": opened.append(url) or True):
            body = self.client.post("/api/harness/browser").json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(opened, ["http://127.0.0.1:43199/"])

    def test_offline_tells_you_what_to_do_without_opening(self):
        opened = []
        with patch.object(harness_proc, "online", lambda timeout=1.0: False), \
                patch.object(harness_proc, "requested", lambda: False), \
                patch("app.platform.shell_open", lambda url, params="": opened.append(url)):
            body = self.client.post("/api/harness/browser").json()
        self.assertFalse(body["ok"])
        self.assertEqual(opened, [], "没在跑就不该弹浏览器")
        self.assertIn("设置 → 智能体", body["message"])

    def test_shell_failure_is_reported(self):
        with patch.object(harness_proc, "online", lambda timeout=1.0: True), \
                patch.object(harness_proc, "base_url", lambda: "http://127.0.0.1:43199"), \
                patch.object(harness_proc, "token", lambda: "t"), \
                patch("app.platform.shell_open", lambda url, params="": False):
            body = self.client.post("/api/harness/browser").json()
        self.assertFalse(body["ok"])
        self.assertIn("打开浏览器失败", body["message"])

    def test_missing_token_triggers_a_restart_to_get_one(self):
        """手里没有 token 时（实例是上一轮 ECHO 拉起的）→ 重启一次换一枚新的，
        因为**浏览器**必须带 token 才能真正进界面。"""
        opened = []
        with patch.object(harness_proc, "online", lambda timeout=1.0: True), \
                patch.object(harness_proc, "base_url", lambda: "http://127.0.0.1:43199"), \
                patch.object(harness_proc, "ensure_token",
                             lambda timeout=45.0: ("fresh-token-0123456789", "已重新拉起并拿到新 token")), \
                patch.object(harness_proc, "token", lambda: ""), \
                patch("app.platform.shell_open",
                      lambda url, params="": opened.append(url) or True):
            body = self.client.post("/api/harness/browser").json()
        self.assertTrue(body["ok"], body)
        self.assertIn("token=fresh-token-0123456789", opened[0])

    def test_ensure_token_keeps_an_existing_one(self):
        with patch.object(harness_proc, "token", lambda: "already-here-0123456"):
            tok, note = harness_proc.ensure_token()
        self.assertEqual(tok, "already-here-0123456")
        self.assertIn("已有", note)

    def test_ensure_token_does_not_touch_foreign_instances(self):
        """不是 ECHO 起的（没有 pid 记录）→ 不能为了拿 token 去重启别人的实例。"""
        with patch.object(harness_proc, "token", lambda: ""), \
                patch.object(harness_proc, "_load_pid", lambda: 0), \
                patch.object(harness_proc, "started_by_echo", lambda: False), \
                patch.object(harness_proc, "stop") as stop_mock, \
                patch.object(harness_proc, "ensure_running") as run_mock:
            tok, note = harness_proc.ensure_token()
        self.assertEqual(tok, "")
        stop_mock.assert_not_called()
        run_mock.assert_not_called()
        self.assertIn("不是 ECHO 起的", note)

    def test_short_junk_token_is_ignored(self):
        """设置里可能留着垃圾值（实测撞到过 `"echo"`）→ 当没填，别拿去登录或拼 URL。"""
        with patch.object(harness_proc, "_token", ""), \
                patch.object(harness_proc, "load_saved_token", lambda: ""), \
                patch("app.config.settings.get", lambda k, d=None: "echo" if k == "harnessToken" else d):
            self.assertEqual(harness_proc.token(), "")
        self.assertTrue(harness_proc._looks_like_token("x" * harness_proc.MIN_TOKEN_LEN))
        self.assertFalse(harness_proc._looks_like_token("echo"))


if __name__ == "__main__":
    unittest.main()
