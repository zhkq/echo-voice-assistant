# -*- coding: utf-8 -*-
"""主机侧中继（`app/relay.py`）的契约与护栏。

这一侧最要紧的两件事，都在这里钉住：

  1. **转发出去的请求长什么样** —— 只带 `Authorization` + `Content-Type`
     + 那个 `X-Echo-Relay` 标记，不带 Host/别的头（不给头注入留口子）；
  2. **那个标记必须真的让 ECHO 不再把回环当免检** —— 这是整个中继功能的安全命门：
     少了它，中继就是一个绕过令牌的后门。

另外钉住：只看 `/health` 与 `api/*`、4xx/5xx 原样带回、二进制一字不差、
后端不在/没配对时**什么都不发**。
"""
import base64
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import relay as relay_mod                                 # noqa: E402


# ---------------------------------------------------------------- 纯判据

class PathAllowlistTests(unittest.TestCase):

    def test_api_and_health_only(self):
        for p in ("api/status", "/api/commands", "api/assistant/voice-command", "health"):
            with self.subTest(p=p):
                self.assertTrue(relay_mod.path_allowed(p), p)
        for p in ("", "/", "etc/passwd", "admin/x", "api/../../etc/passwd", "apifoo"):
            with self.subTest(p=p):
                self.assertFalse(relay_mod.path_allowed(p), p)


# ---------------------------------------------------------------- 假"本机 ECHO"

class _LocalEcho(BaseHTTPRequestHandler):
    """假装是本机的 8970：记下收到的请求，回一段可辨认的内容。"""

    seen = []

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _LocalEcho.seen.append({
            "method": method,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        if self.path.startswith("/api/boom"):
            payload = b'{"detail":"\\u65e0\\u6548\\u6216\\u7f3a\\u5931 API \\u5bc6\\u94a5"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = b"RIFF" + body[:16]
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                                             # noqa: N802
        self._handle("GET")

    def do_POST(self):                                            # noqa: N802
        self._handle("POST")

    def log_message(self, *a):                                    # 静音
        pass


class _FakeBackend(BaseHTTPRequestHandler):
    """假装是后端：poll 给一条调用、收起结果。"""

    call = {}
    results = []
    polls = 0

    def do_POST(self):                                            # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        if self.path.endswith("/host/poll"):
            _FakeBackend.polls += 1
            calls = [_FakeBackend.call] if _FakeBackend.polls == 1 else []
            out = {"ok": True, "hostId": "cli-test", "calls": calls, "timeoutSec": 600}
        elif self.path.endswith("/host/result"):
            _FakeBackend.results.append(payload)
            out = {"ok": True, "accepted": True}
        else:
            out = {"ok": False}
        body = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _stop(srv):
    """真的把这个假服务器停掉。

    ⚠️ 只调 `shutdown()` **不够**（2026-10-15 实测）：它停的是 accept 循环，
    **监听套接字还开着** —— 之后 connect 照样能握手成功，然后没人应答，
    调用方一直等到超时（这里就是 570 秒）。必须再 `server_close()`，
    连接才会被**拒绝**（那正是"本机 API 挂了"该有的行为）。
    """
    import contextlib
    with contextlib.suppress(Exception):
        srv.shutdown()
    with contextlib.suppress(Exception):
        srv.server_close()


class ExecuteTests(unittest.TestCase):
    """`relay._execute`：把一条中继调用在本机执行掉。"""

    def setUp(self):
        _LocalEcho.seen = []
        self.srv = _serve(_LocalEcho)
        self.port = self.srv.server_address[1]
        self._orig = relay_mod._local_base
        relay_mod._local_base = lambda: "http://127.0.0.1:%d" % self.port
        self.addCleanup(_stop, self.srv)
        self.addCleanup(lambda: setattr(relay_mod, "_local_base", self._orig))

    def test_it_forwards_the_token_and_marks_the_request(self):
        out = relay_mod._execute({
            "id": "c1", "method": "GET", "path": "api/status", "query": "limit=5",
            "headers": {"authorization": "Bearer phone-token", "content-type": ""},
        })
        self.assertEqual(200, out["status"])
        self.assertNotIn("error", out)
        req = _LocalEcho.seen[-1]
        self.assertEqual("GET", req["method"])
        self.assertEqual("/api/status?limit=5", req["path"])
        self.assertEqual("Bearer phone-token", req["headers"].get("authorization"))
        self.assertEqual("1", req["headers"].get("x-echo-relay"),
                         "标记必须带上：ECHO 靠它不再把回环当免检")

    def test_only_a_few_headers_are_forwarded(self):
        relay_mod._execute({
            "id": "c2", "method": "POST", "path": "api/commands",
            "headers": {"authorization": "Bearer t", "content-type": "application/json",
                        "x-forwarded-for": "1.2.3.4", "cookie": "secret=1"},
            "body": base64.b64encode(b'{"x":1}').decode(),
        })
        headers = _LocalEcho.seen[-1]["headers"]
        self.assertEqual("application/json", headers.get("content-type"))
        self.assertNotIn("x-forwarded-for", headers, "手机塞的头不许透传")
        self.assertNotIn("cookie", headers)
        self.assertNotIn("1.2.3.4", json.dumps(headers))

    def test_a_binary_body_arrives_byte_for_byte(self):
        payload = bytes(range(256)) * 3
        relay_mod._execute({
            "id": "c3", "method": "POST", "path": "api/assistant/voice-command",
            "headers": {"authorization": "Bearer t", "content-type": "audio/wav"},
            "body": base64.b64encode(payload).decode(),
        })
        self.assertEqual(payload, _LocalEcho.seen[-1]["body"])

    def test_an_error_status_is_handed_back_not_swallowed(self):
        out = relay_mod._execute({
            "id": "c4", "method": "GET", "path": "api/boom",
            "headers": {"authorization": "Bearer wrong"},
        })
        self.assertEqual(401, out["status"], "401 是正常的业务回答，要原样带回")
        self.assertIn("API", base64.b64decode(out["body"]).decode("utf-8"))
        self.assertNotIn("error", out)

    def test_a_forbidden_path_never_reaches_the_local_api(self):
        out = relay_mod._execute({"id": "c5", "method": "GET", "path": "etc/passwd"})
        self.assertIn("error", out)
        self.assertEqual([], _LocalEcho.seen, "白名单外的一条都不许发出去")

    def test_local_failure_becomes_a_plain_error(self):
        _stop(self.srv)                       # 真把监听套接字关掉 → 连接被拒
        out = relay_mod._execute({"id": "c6", "method": "GET", "path": "api/status"})
        self.assertIn("error", out)


# ---------------------------------------------------------------- 端到端（线程 + 假后端）

class WorkerLoopTests(unittest.TestCase):
    """整条环：poll 取活 → 本机执行 → 交回结果。"""

    def setUp(self):
        _LocalEcho.seen = []
        _FakeBackend.call = {}
        _FakeBackend.results = []
        _FakeBackend.polls = 0
        self.local = _serve(_LocalEcho)
        self.backend = _serve(_FakeBackend)
        self._orig_local = relay_mod._local_base
        relay_mod._local_base = lambda: "http://127.0.0.1:%d" % self.local.server_address[1]
        self.addCleanup(_stop, self.local)
        self.addCleanup(_stop, self.backend)
        self.addCleanup(lambda: setattr(relay_mod, "_local_base", self._orig_local))
        self.addCleanup(relay_mod.stop)

        # 假后端地址 / 假令牌 / 开关：把中继"骗"成可用（不起真 HTTP 客户端）
        self._orig_settings = relay_mod.settings.get

        def fake_get(key, default=None):
            if key == "relayEnabled":
                return True
            if key == "apiAuthEnabled":
                return True
            return self._orig_settings(key, default)

        relay_mod.settings.get = fake_get
        self.addCleanup(lambda: setattr(relay_mod.settings, "get", self._orig_settings))

        import app.capabilities.echo_server as echo_mod
        self._orig_endpoint = echo_mod.backend_endpoint
        base = "http://127.0.0.1:%d" % self.backend.server_address[1]
        echo_mod.backend_endpoint = lambda slot="": (base, "Bearer host-token")
        self.addCleanup(lambda: setattr(echo_mod, "backend_endpoint", self._orig_endpoint))

    def test_one_call_goes_all_the_way_through(self):
        _FakeBackend.call = {"id": "c1", "method": "GET", "path": "api/status",
                             "query": "", "headers": {"authorization": "Bearer phone"}, "body": ""}
        relay_mod.STATE.update({"calls": 0, "hostId": "", "publicUrl": ""})
        self.assertTrue(relay_mod.ensure_running())

        deadline = time.time() + 8.0
        while time.time() < deadline and not _FakeBackend.results:
            time.sleep(0.1)

        self.assertTrue(_FakeBackend.results, "主机该把结果交回后端")
        result = _FakeBackend.results[0]
        self.assertEqual("c1", result.get("id"))
        self.assertEqual(200, result.get("status"))
        self.assertEqual("/api/status", _LocalEcho.seen[-1]["path"])
        self.assertEqual("cli-test", relay_mod.STATE.get("hostId"),
                         "hostId 要用后端认下来的那个")
        self.assertTrue(str(relay_mod.STATE.get("publicUrl")).endswith("/v1/relay/cli-test/"))
        relay_mod.stop()

    def test_it_refuses_to_start_without_api_auth(self):
        """鉴权关着时**不许**开：中继进来的请求来自回环，不开鉴权=对外开放。"""
        def fake_get(key, default=None):
            if key == "relayEnabled":
                return True
            if key == "apiAuthEnabled":
                return False
            return self._orig_settings(key, default)

        relay_mod.settings.get = fake_get
        ok, why = relay_mod.ready()
        self.assertFalse(ok)
        self.assertIn("API 鉴权", why)

    def test_off_by_default_sends_nothing(self):
        def fake_get(key, default=None):
            if key == "relayEnabled":
                return False
            return self._orig_settings(key, default)

        relay_mod.settings.get = fake_get
        relay_mod.stop()
        self.assertFalse(relay_mod.ensure_running())
        self.assertEqual(0, _FakeBackend.polls, "关着的时候一个请求都不该发")


class LoopbackExemptionTests(unittest.TestCase):
    """**整个中继功能的安全命门**：带标记的请求不许再享受"回环免令牌"。

    少了这一条，中继转发进来的请求会被 `optional_auth` 当成"本机面板"直接放行 ——
    于是任何人只要能连到那台后端，就能在办公电脑上执行任何 `/api/*`。
    这里直接测那个判据本身（`_is_loopback_call`），比绕 TestClient 更准：
    TestClient 的 peer 是 "testclient"（本来就不算回环），测不出这条。
    """

    def _req(self, headers, peer="127.0.0.1"):
        from types import SimpleNamespace
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer),
                               url=SimpleNamespace(path="/api/daily-review"))

    def test_a_plain_loopback_call_is_still_exempt(self):
        from app import api as api_mod
        self.assertTrue(api_mod._is_loopback_call(self._req({"host": "127.0.0.1:8970"})))

    def test_a_marked_call_is_never_treated_as_local(self):
        from app import api as api_mod
        marked = {"host": "127.0.0.1:8970", relay_mod.RELAY_MARKER_HEADER: "1"}
        self.assertFalse(
            api_mod._is_loopback_call(self._req(marked)),
            "中继进来的请求来自手机（可能在网上），必须走完整鉴权",
        )

    def test_a_marked_call_from_a_public_peer_is_still_remote(self):
        from app import api as api_mod
        marked = {"host": "127.0.0.1:8970", relay_mod.RELAY_MARKER_HEADER: "1"}
        self.assertFalse(api_mod._is_loopback_call(self._req(marked, peer="203.0.113.9")))


class PairingThroughRelayTests(unittest.TestCase):
    """走中继配对时，回给手机的 `baseUrl` 必须是**它这次用的中继地址**。

    回局域网地址的话，手机配对完立刻失联 —— 它正是因为连不上局域网才走的中继
    （这是"配好了却一直连不上"里最难查的一种）。
    """

    def _req(self, marked=True):
        from types import SimpleNamespace
        headers = {"host": "127.0.0.1:8970"}
        if marked:
            headers[relay_mod.RELAY_MARKER_HEADER] = "1"
        return SimpleNamespace(headers=headers, client=SimpleNamespace(host="127.0.0.1"),
                               url=SimpleNamespace(path="/api/pair/phone/claim"))

    def test_relayed_pairing_gets_the_relay_url(self):
        from app import api as api_mod
        from unittest import mock
        with mock.patch.object(relay_mod, "public_url",
                               lambda: "http://be.example:8900/v1/relay/cli-1/"):
            self.assertEqual("http://be.example:8900/v1/relay/cli-1",
                             api_mod._phone_base_url(self._req()))

    def test_relayed_pairing_gets_nothing_when_the_relay_url_is_unknown(self):
        """认不出中继地址时回**空串**：客户端有"服务端没给就用我自己填的"兜底，
        宁可不给，也不能给一个连不上的局域网地址。"""
        from app import api as api_mod
        from unittest import mock
        with mock.patch.object(relay_mod, "public_url", lambda: ""):
            self.assertEqual("", api_mod._phone_base_url(self._req()))

    def test_a_local_pairing_still_gets_the_lan_address(self):
        from app import api as api_mod
        from app import netguard, ports
        from unittest import mock
        with mock.patch.object(netguard, "preferred_address", lambda: "192.168.1.5"), \
             mock.patch.object(ports, "active_port", lambda *a, **k: 8970):
            self.assertEqual("http://192.168.1.5:8970", api_mod._phone_base_url(self._req(False)))


if __name__ == "__main__":
    unittest.main()
