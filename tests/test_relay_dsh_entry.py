# -*- coding: utf-8 -*-
"""经中继打开 **DSH 标准版网页**（只读）的契约与护栏（2026-10-18）。

用户原话："首页安排入口可以通过中转打开 dsh 标准版网页"。
背景：面板 / DSH 网页**经中继**打开是"要用户拍板才能做"的暴露面变化，
用户现在点头了 —— 但点头的是"**只读**看一眼"，不是把管理面交出去。

这一份钉住四件事：

  1. 状态接口里**有**那个地址（`dshWebUrl`），值来自 relay 的 `publicUrl`；
     没配对 / 没连中继 / 标准版没在跑时**如实为空 + 给原因**（绝不编一个打不开的）；
  2. 只读网页入口的**路径白名单**：页面要用的静态资源真能拿到，未放行的仍被拒；
  3. **写接口经该只读会话被拒**：改设置 / 发指令 / 建会话 / 开终端一律 403；
  4. 既有中继的行为不变（`path_allowed` 仍只放行 `/health` 与 `api/*`）。

为什么地址落在 `api/relaydsh/<会话>/`：中继服务端只放行 `api/*`，而 `server/**` 本轮
不许动（它已部署、改了不部署等于没改）。详见 `app/relay_web.py` 顶部。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                 # noqa: E402
from fastapi.testclient import TestClient                   # noqa: E402

import app.db as db                                         # noqa: E402
from app import relay as relay_mod                          # noqa: E402
from app import relay_web                                   # noqa: E402
from app.api import router                                  # noqa: E402
from app.config import settings                             # noqa: E402

BASE = "https://echoserver.example:8900/v1/relay/cli-test/"


# ---------------------------------------------------------------- 假 DSH 标准版

class _FakeDsh(BaseHTTPRequestHandler):
    """假装是本机的 DSH 标准版（独立 harness）：记请求、回可辨认的内容。"""

    seen = []

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        _FakeDsh.seen.append({
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": raw,
        })
        if self.path.split("?")[0] in ("/", "/index.html"):
            payload, ctype = b"<html>DSH standard web</html>", "text/html; charset=utf-8"
        elif self.path.split("?")[0] == "/api/session/list":
            payload, ctype = (b'{"ok":true,"result":{"items":[]}}',
                              "application/json; charset=utf-8")
        else:
            payload, ctype = ("upstream:" + self.path).encode("utf-8"), "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):                                             # noqa: N802
        self._handle("GET")

    def do_HEAD(self):                                            # noqa: N802
        self._handle("HEAD")

    def do_POST(self):                                            # noqa: N802
        self._handle("POST")

    def log_message(self, *a):                                    # 静音
        pass


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _stop(srv):
    import contextlib
    with contextlib.suppress(Exception):
        srv.shutdown()
    with contextlib.suppress(Exception):
        srv.server_close()


# ---------------------------------------------------------------- 状态接口 / 地址

class WebEntryTests(unittest.TestCase):
    """`dshWebUrl`：只在**真的能打开**时给，否则如实为空。"""

    def setUp(self):
        self._state = dict(relay_mod.STATE)
        self.addCleanup(lambda: relay_mod.STATE.update(self._state))
        relay_mod.STATE.update({"connected": True, "hostId": "cli-test",
                                "lastError": ""})

    def _entry(self, enabled=True, connected=True, base=BASE, harness=True):
        relay_mod.STATE["connected"] = connected
        with mock.patch.object(relay_mod, "enabled", lambda: enabled), \
             mock.patch.object(relay_mod, "public_url", lambda: base), \
             mock.patch.object(relay_mod, "_harness_online",
                               lambda timeout=0.4: harness):
            return relay_mod.web_entry()

    def test_the_url_is_public_url_plus_the_readonly_entry(self):
        entry = self._entry()
        token = relay_mod.web_session()
        self.assertEqual(BASE + "api/relaydsh/%s/" % token, entry["url"])
        self.assertEqual("", entry["reason"])
        self.assertTrue(entry["readOnly"])
        self.assertTrue(relay_mod.web_session_valid(token))

    def test_the_token_in_the_url_is_the_session_that_validates(self):
        url = self._entry()["url"]
        token = url[len(BASE + "api/relaydsh/"):-1]
        self.assertTrue(relay_mod.web_session_valid(token))
        self.assertFalse(relay_mod.web_session_valid(token + "x"))
        self.assertFalse(relay_mod.web_session_valid(""))

    def test_no_url_when_relay_is_off(self):
        entry = self._entry(enabled=False)
        self.assertEqual("", entry["url"])
        self.assertIn("中继没打开", entry["reason"])

    def test_no_url_when_relay_is_not_connected(self):
        entry = self._entry(connected=False)
        self.assertEqual("", entry["url"])
        self.assertIn("没连上", entry["reason"])

    def test_no_url_when_there_is_no_public_url(self):
        entry = self._entry(base="")
        self.assertEqual("", entry["url"])
        self.assertIn("中继地址", entry["reason"])

    def test_no_url_when_the_standard_web_is_not_running(self):
        entry = self._entry(harness=False)
        self.assertEqual("", entry["url"])
        self.assertIn("harness", entry["reason"])

    def test_status_exposes_the_field_even_when_it_is_empty(self):
        with mock.patch.object(relay_mod, "web_entry",
                               lambda: {"url": "", "reason": "中继还没连上", "readOnly": True}):
            st = relay_mod.status(refresh_ready=False)
        self.assertEqual("", st["webUrl"])
        self.assertEqual("中继还没连上", st["webReason"])
        self.assertTrue(st["webReadOnly"])

    def test_status_carries_the_url_when_available(self):
        want = BASE + "api/relaydsh/abc/"
        with mock.patch.object(relay_mod, "web_entry",
                               lambda: {"url": want, "reason": "", "readOnly": True}):
            st = relay_mod.status(refresh_ready=False)
        self.assertEqual(want, st["webUrl"])


class ExistingRelayAllowlistTests(unittest.TestCase):
    """既有中继白名单**不放松**：只放行 health 与 api/*。"""

    def test_the_readonly_entry_lives_under_api(self):
        self.assertTrue(relay_mod.path_allowed("api/relaydsh/abc/"))
        self.assertTrue(relay_mod.path_allowed("api/relaydsh/abc/assets/x.js"))
        self.assertTrue(relay_mod.WEB_ENTRY_PREFIX.startswith("api/"))

    def test_the_panel_static_paths_are_not_opened_through_the_relay(self):
        """`/`、`web/*` 这些**面板自己的**路径不在这里放行。

        为什么（这是安全口径，不是漏做）：面板前端写死了 `/api/...` 绝对路径，
        把它经中继打开 = 页面里的每个动作都会落到**普通中继会话**上（那一条是读写的），
        而"只读会话语义"根本盖不住它。所以只读入口只给 DSH 标准版前端
        （它的路由是**文档相对**的，能整页活在 `api/relaydsh/<会话>/` 下）。
        """
        for p in ("", "/", "web/app.js", "sw.js", "manifest.json"):
            with self.subTest(p=p):
                self.assertFalse(relay_mod.path_allowed(p))

    def test_the_panel_paths_still_require_the_normal_relay_auth(self):
        """未放行的路径仍被拒（中继那侧的第二道）。"""
        self.assertFalse(relay_mod.path_allowed("etc/passwd"))
        self.assertFalse(relay_mod.path_allowed("api/../../etc/passwd"))


# ---------------------------------------------------------------- 只读网页入口（纯函数 + 转发）

class ProxyTests(unittest.TestCase):
    """`relay_web.proxy`：静态白名单 + 读 RPC 白名单 + 只读。"""

    @classmethod
    def setUpClass(cls):
        cls.upstream = _serve(_FakeDsh)
        cls.base = "http://127.0.0.1:%d" % cls.upstream.server_address[1]

    @classmethod
    def tearDownClass(cls):
        _stop(cls.upstream)
        super().tearDownClass()

    def setUp(self):
        _FakeDsh.seen = []
        self.token = relay_mod.web_session()
        self._p1 = mock.patch.object(relay_web, "_upstream_base", lambda: self.base)
        self._p2 = mock.patch.object(relay_web, "_cookie", lambda: "dsh-auth-test=v1.x.y")
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def _call(self, method="GET", path="", body=b"", session=None):
        return relay_web.proxy(session if session is not None else self.token,
                               method, path, "", body, "application/json")

    def test_the_index_and_its_relative_assets_are_reachable(self):
        status, ctype, body = self._call("GET", "")
        self.assertEqual(200, status)
        self.assertIn("text/html", ctype)
        self.assertIn(b"DSH standard web", body)
        self.assertEqual("/", _FakeDsh.seen[-1]["path"])
        self.assertEqual("dsh-auth-test=v1.x.y",
                         _FakeDsh.seen[-1]["headers"].get("cookie"),
                         "上游只该看到 ECHO 本机自铸的 Cookie（浏览器带来的不转发）")

        status, _ctype, _body = self._call("GET", "assets/index-abc.js")
        self.assertEqual(200, status)
        self.assertEqual("/assets/index-abc.js", _FakeDsh.seen[-1]["path"])

        status, _ctype, _body = self._call("GET", "plugins/x/client.js")
        self.assertEqual(200, status)
        self.assertEqual("/plugins/x/client.js", _FakeDsh.seen[-1]["path"])

        status, _ctype, _body = self._call("GET", "manifest.webmanifest")
        self.assertEqual(200, status)

    def test_a_query_is_forwarded_untouched(self):
        # DSH 的插件包就是 `plugins/??<ids>&rev=…` 这种形状（查询串本身以 `?` 开头）。
        status, _ctype, _body = relay_web.proxy(
            self.token, "GET", "plugins/", "?@dsh/x/client.js&rev=1", b"", "")
        self.assertEqual(200, status)
        self.assertEqual("/plugins/??@dsh/x/client.js&rev=1", _FakeDsh.seen[-1]["path"])

    def test_an_unlisted_path_is_refused_and_never_reaches_the_upstream(self):
        status, _ctype, body = self._call("GET", "settings.yaml")
        self.assertEqual(403, status)
        self.assertIn("白名单", body.decode("utf-8"))
        self.assertEqual([], _FakeDsh.seen, "白名单外的路径一条都不许发到本机 harness")

    def test_a_path_with_dotdot_is_refused(self):
        status, _ctype, _body = self._call("GET", "../etc/passwd")
        self.assertEqual(403, status)
        self.assertEqual([], _FakeDsh.seen)

    def test_a_bad_session_is_a_plain_404(self):
        status, _ctype, _body = self._call("GET", "", session="not-a-session")
        self.assertEqual(404, status)
        self.assertEqual([], _FakeDsh.seen)

    def test_static_assets_are_read_only(self):
        status, _ctype, _body = self._call("POST", "assets/x.js", b"{}")
        self.assertEqual(403, status)
        self.assertEqual([], _FakeDsh.seen)

    def test_a_read_rpc_is_forwarded(self):
        body = json.dumps({"type": "client-request", "rpcId": "r1",
                           "method": "session/list", "payload": {"args": {}}}).encode()
        status, _ctype, payload = self._call("POST", "api/session/list", body)
        self.assertEqual(200, status)
        self.assertEqual("/api/session/list", _FakeDsh.seen[-1]["path"])
        self.assertEqual(body, _FakeDsh.seen[-1]["body"], "读请求体要原样带到上游")

    def test_writing_rpcs_are_refused(self):
        for method in ("session/prompt", "session/create", "settings/update",
                       "settings/mutate", "session/cancel", "workspace/create",
                       "terminal/create", "credentials/describe"):
            with self.subTest(method=method):
                body = json.dumps({"type": "client-request", "rpcId": "r1",
                                   "method": method, "payload": {"args": {}}}).encode()
                status, _ctype, _payload = self._call("POST", "api/" + method, body)
                self.assertEqual(403, status)
        self.assertEqual([], _FakeDsh.seen, "写 RPC 一条都不许发到本机 harness")

    def test_a_post_that_is_not_a_read_rpc_is_refused(self):
        for body in (b"", b"not json", b'{"method":""}', b'[]'):
            with self.subTest(body=body):
                self.assertEqual(403, self._call("POST", "api/session/list", body)[0])
        self.assertEqual([], _FakeDsh.seen)

    def test_put_patch_delete_are_refused(self):
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(403, self._call(method, "api/session/list", b"{}")[0])
        self.assertEqual([], _FakeDsh.seen)

    def test_without_the_harness_browser_secret_it_says_what_is_missing(self):
        with mock.patch.object(relay_web, "_cookie", lambda: ""):
            status, _ctype, body = self._call("GET", "")
        self.assertEqual(503, status)
        self.assertIn("浏览器会话密钥", body.decode("utf-8"))
        self.assertEqual([], _FakeDsh.seen)


# ---------------------------------------------------------------- 经 ECHO 路由（测试客户端）

class _IsolatedDb:
    """把 db 重定向到临时目录（与 `tests/test_api_contract.py` 同一套写法）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="echo-relay-dsh-")
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


class WebEntryThroughTheApiTests(_IsolatedDb, unittest.TestCase):
    """`/api/status` 的字段 + `/api/relaydsh/...` 的只读语义（真路由、测试客户端）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)
        cls.upstream = _serve(_FakeDsh)
        cls.base = "http://127.0.0.1:%d" % cls.upstream.server_address[1]

    @classmethod
    def tearDownClass(cls):
        _stop(cls.upstream)
        super().tearDownClass()

    def setUp(self):
        _FakeDsh.seen = []
        self.token = relay_mod.web_session()
        for patcher in (
            mock.patch.object(relay_web, "_upstream_base", lambda: self.base),
            mock.patch.object(relay_web, "_cookie", lambda: "dsh-auth-test=v1.x.y"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- 状态接口 ----

    def test_status_has_the_field_the_android_side_reads(self):
        data = self.client.get("/api/status").json()
        self.assertIn("dshWebUrl", data)
        self.assertIsInstance(data["dshWebUrl"], str)
        self.assertIn("dshWebReason", data)
        self.assertTrue(data["dshWebReadOnly"])

    def test_status_url_comes_from_the_relay_public_url(self):
        want = BASE + "api/relaydsh/abc/"
        with mock.patch.object(relay_mod, "web_entry",
                               lambda: {"url": want, "reason": "", "readOnly": True}):
            data = self.client.get("/api/status").json()
        self.assertEqual(want, data["dshWebUrl"])
        self.assertEqual("", data["dshWebReason"])

    def test_pair_state_carries_the_same_field(self):
        data = self.client.get("/api/pair/phone").json()
        self.assertIn("dshWebUrl", data)
        self.assertIn("dshWebReason", data)
        self.assertTrue(data["dshWebReadOnly"])

    def test_a_relay_marked_call_still_needs_the_phone_token(self):
        """中继进来的请求照样要手机令牌（**鉴权仍旧生效**，不因为它是网页就免检）。

        这一条钉的是"只读"与"免鉴权"是两件事：只读网页入口**不是**一个匿名后门，
        它只是把"拿着令牌进来的请求"限制成只能看。
        """
        key = db.add_api_key("test-phone")
        settings.update({"apiAuthEnabled": True})
        self.addCleanup(lambda: settings.update({"apiAuthEnabled": False}))
        marked = {relay_mod.RELAY_MARKER_HEADER: "1"}
        r = self.client.get("/api/relaydsh/%s/" % self.token, headers=marked)
        self.assertEqual(401, r.status_code, "没有令牌：中继这条通道必须拒")
        r = self.client.get("/api/relaydsh/%s/" % self.token,
                            headers={**marked, "Authorization": "Bearer " + key})
        self.assertEqual(200, r.status_code)

    # ---- 只读网页入口（真路由） ----

    def test_allowed_assets_are_reachable_through_the_real_route(self):
        r = self.client.get("/api/relaydsh/%s/assets/index-abc.js" % self.token)
        self.assertEqual(200, r.status_code)
        self.assertEqual("/assets/index-abc.js", _FakeDsh.seen[-1]["path"])

    def test_an_unlisted_path_is_refused_through_the_real_route(self):
        r = self.client.get("/api/relaydsh/%s/settings.yaml" % self.token)
        self.assertEqual(403, r.status_code)
        self.assertEqual([], _FakeDsh.seen)

    def test_a_read_rpc_is_forwarded_through_the_real_route(self):
        body = {"type": "client-request", "rpcId": "r1",
                "method": "session/list", "payload": {"args": {}}}
        r = self.client.post("/api/relaydsh/%s/api/session/list" % self.token, json=body)
        self.assertEqual(200, r.status_code)
        self.assertEqual("/api/session/list", _FakeDsh.seen[-1]["path"])

    def test_a_writing_rpc_is_refused_through_the_real_route(self):
        body = {"type": "client-request", "rpcId": "r1",
                "method": "session/prompt", "payload": {"args": {}}}
        r = self.client.post("/api/relaydsh/%s/api/session/prompt" % self.token, json=body)
        self.assertEqual(403, r.status_code)
        self.assertEqual([], _FakeDsh.seen)

    # ---- 那条只读会话碰任何写接口都不行 ----

    def test_a_write_api_is_refused_with_the_readonly_session(self):
        head = {relay_mod.WEB_SESSION_HEADER: self.token}
        for method, path in (("PUT", "/api/settings"),
                             ("POST", "/api/assistant/command"),
                             ("POST", "/api/settings/reset"),
                             ("POST", "/api/control/echo/stop"),
                             ("DELETE", "/api/meetings/x")):
            with self.subTest(method=method, path=path):
                r = self.client.request(method, path, headers=head, json={})
                self.assertEqual(403, r.status_code, "%s %s 必须被只读会话拒" % (method, path))
                self.assertIn("只读", r.json().get("detail", ""))

    def test_reads_still_work_with_the_readonly_session(self):
        r = self.client.get("/api/status",
                            headers={relay_mod.WEB_SESSION_HEADER: self.token})
        self.assertEqual(200, r.status_code)

    def test_an_invalid_session_header_does_not_get_the_readonly_umbrella(self):
        """不是那条会话就别拿它当挡箭牌（正常的鉴权/拒收清单照旧生效）。"""
        r = self.client.get("/api/status",
                            headers={relay_mod.WEB_SESSION_HEADER: "bogus"})
        self.assertEqual(200, r.status_code)


class WebSessionGuardTests(unittest.TestCase):
    """`relay.web_session_denied` 的纯判据（不绕 TestClient，直接测那一条）。"""

    def _req(self, method, path="/api/settings", token=None):
        headers = {}
        if token is not None:
            headers[relay_mod.WEB_SESSION_HEADER] = token
        return SimpleNamespace(method=method, headers=headers,
                               url=SimpleNamespace(path=path))

    def test_a_write_with_a_valid_session_is_denied(self):
        token = relay_mod.web_session()
        self.assertTrue(relay_mod.web_session_denied(self._req("PUT", token=token)))
        self.assertTrue(relay_mod.web_session_denied(self._req("POST", token=token)))

    def test_reads_are_never_denied_by_it(self):
        token = relay_mod.web_session()
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.assertFalse(relay_mod.web_session_denied(self._req(method, token=token)))

    def test_the_web_entry_itself_decides_by_its_own_method_allowlist(self):
        token = relay_mod.web_session()
        req = self._req("POST", path="/api/relaydsh/%s/api/session/list" % token,
                        token=token)
        self.assertFalse(relay_mod.web_session_denied(req),
                         "读 RPC 也是 POST，这一层不能一刀切")

    def test_an_unknown_token_denies_nothing(self):
        self.assertFalse(relay_mod.web_session_denied(self._req("PUT", token="bogus")))
        self.assertFalse(relay_mod.web_session_denied(self._req("PUT")))

    def test_stopping_the_relay_invalidates_the_session(self):
        token = relay_mod.web_session()
        self.assertTrue(relay_mod.web_session_valid(token))
        relay_mod.stop()
        self.assertFalse(relay_mod.web_session_valid(token),
                         "中继停了，旧地址必须立刻作废")


if __name__ == "__main__":
    unittest.main()
