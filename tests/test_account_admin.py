# -*- coding: utf-8 -*-
"""客户端账号/登录即绑（issue #23）：`app/account_admin.py` + `/api/account/*`。

不连真实后端：起一个本地 HTTP 桩，钉住四件事：

1. `userToken` 只落 `account.json`（整个 JSON 走与 `backend.json` 同一套信封），
   **不落 `backend.json`**；
2. `bind_device()` 带的是 `Authorization: Bearer <userToken>`，成功后写
   `backend.json`；已有凭据时**跳过**、不重复占名额；
3. 401 只翻成"重新登录"，**绝不**自动去调 `/v1/account/devices/code` 或 `/v1/pair`；
4. 402/403/409 保留分类，界面能分别引导续费/解绑/换账号。
"""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import account_admin                                             # noqa: E402
from app.capabilities import credentials as cred                          # noqa: E402
from app.capabilities.credentials import BackendCredentials               # noqa: E402

USER_TOKEN = "user-token-abcdef0123456789"


class _FakeState:
    def __init__(self):
        self.calls = []
        self.bind_status = 200
        self.bind_code = ""
        self.bind_detail = ""
        self.me_status = 200
        self.logout_status = 200
        self.me_body = {"user": {"userId": "usr-1", "email": "u@example.com"},
                        "devices": [{"clientId": "cli-new", "deviceName": "PC",
                                     "platform": "linux", "status": "active"}]}

    def bind_payload(self):
        return {"clientId": "cli-new", "clientSecret": "secret-new",
                "accessToken": "access-new", "expiresIn": 3600,
                "scopes": ["asr", "tts"], "serverName": "fake",
                "protocol": 1, "rebound": False,
                "device": {"clientId": "cli-new", "deviceName": "PC",
                           "platform": "linux", "status": "active"},
                "devices": 1, "deviceLimit": 3}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _dispatch(self):
        state = self.server.state
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        path = urlsplit(self.path).path
        state.calls.append({"method": self.command, "path": path,
                            "authorization": self.headers.get("authorization") or "",
                            "body": body})
        if path == "/v1/account/login" and self.command == "POST":
            out = {"userId": "usr-1", "user": {"userId": "usr-1",
                                               "email": "u@example.com"},
                   "devices": [], "userToken": USER_TOKEN,
                   "expiresAt": 4102444800}
            self._reply(200, out)
        elif path == "/v1/account/register" and self.command == "POST":
            self._reply(200, {"userId": "usr-1", "user": {"email": "u@example.com"},
                              "devices": [], "userToken": USER_TOKEN,
                              "expiresAt": 4102444800})
        elif path == "/v1/account/email/send-code" and self.command == "POST":
            self._reply(200, {"ok": True, "expiresIn": 600})
        elif path == "/v1/account/password/reset" and self.command == "POST":
            self._reply(200, {"ok": True})
        elif path == "/v1/account/logout" and self.command == "POST":
            if state.logout_status != 200:
                self._reply(state.logout_status, {"code": "error", "detail": "服务端失败"})
            else:
                self._reply(200, {"ok": True})
        elif path == "/v1/account/me" and self.command == "GET":
            if state.me_status != 200:
                self._reply(state.me_status, {"code": "unauthorized",
                                              "detail": "登录已失效"})
            else:
                self._reply(200, state.me_body)
        elif path == "/v1/account/devices/bind" and self.command == "POST":
            if state.bind_status != 200:
                payload = {"code": state.bind_code or "error"}
                if state.bind_detail:
                    payload["detail"] = state.bind_detail
                self._reply(state.bind_status, payload)
            else:
                self._reply(200, state.bind_payload())
        elif path == "/v1/account/devices/unbind" and self.command == "POST":
            self._reply(200, {"ok": True, "clientId": body.get("clientId")})
        else:
            self._reply(404, {"code": "not_found", "detail": path})

    def _reply(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _dispatch
    do_POST = _dispatch


class _AccountAdminCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-account-admin-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        patcher = patch.object(cred, "credentials_path",
                               lambda: os.path.join(self.tmp, "backend.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = _FakeState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = self.fake
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base_url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def login(self):
        return account_admin.login("u@example.com", "Passw0rd", self.base_url)

    def calls(self, path):
        return [c for c in self.fake.calls if c["path"] == path]


class LoginSessionTests(_AccountAdminCase):
    def test_login_saves_the_token_sealed_and_never_returns_it(self):
        out = self.login()
        self.assertEqual(out["userId"], "usr-1")
        self.assertNotIn("userToken", out)
        view = account_admin.session_view()
        self.assertTrue(view["signedIn"])
        self.assertEqual(view["email"], "u@example.com")
        self.assertEqual(view["serverUrl"], self.base_url)
        self.assertNotIn("userToken", view)

        path = cred.data_path(account_admin.FILENAME)
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(USER_TOKEN.encode("utf-8"), raw)
        session = account_admin.load_session()
        self.assertEqual(session["userToken"], USER_TOKEN)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_logout_clears_the_local_session_even_if_the_server_fails(self):
        self.login()
        self.fake.logout_status = 500
        account_admin.logout()
        self.assertFalse(account_admin.session_view()["signedIn"])
        self.assertIsNone(account_admin.load_session())

    def test_me_uses_the_stored_user_token(self):
        self.login()
        out = account_admin.me()
        self.assertEqual(out["devices"][0]["clientId"], "cli-new")
        call = self.calls("/v1/account/me")[-1]
        self.assertEqual(call["authorization"], "Bearer " + USER_TOKEN)

    def test_expired_session_is_reported_not_signed_in(self):
        self.login()
        session = account_admin.load_session()
        session["expiresAt"] = 1
        cred.write_sealed_json(account_admin.FILENAME, session)
        view = account_admin.session_view()
        self.assertFalse(view["signedIn"])
        self.assertTrue(view["expired"])


class DeviceIdTests(_AccountAdminCase):
    def test_device_id_is_stable_and_written_once(self):
        first = account_admin.device_id()
        second = account_admin.device_id()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        path = cred.data_path(account_admin.DEVICE_ID_FILENAME)
        self.assertTrue(os.path.isfile(path))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_corrupt_device_id_file_is_regenerated(self):
        path = cred.data_path(account_admin.DEVICE_ID_FILENAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("bad id with spaces\n")
        value = account_admin.device_id()
        self.assertEqual(len(value), 32)
        self.assertNotIn(" ", value)


class BindTests(_AccountAdminCase):
    def test_bind_uses_the_user_token_and_writes_backend_credentials(self):
        self.login()
        out = account_admin.bind_device()
        self.assertTrue(out["ok"])
        self.assertFalse(out["skipped"])
        call = self.calls("/v1/account/devices/bind")[-1]
        self.assertEqual(call["authorization"], "Bearer " + USER_TOKEN)
        self.assertEqual(call["body"]["deviceId"], account_admin.device_id())
        self.assertEqual(call["body"]["platform"], __import__("app.platform",
                                                              fromlist=["current"]).current())

        saved = cred.load()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.client_id, "cli-new")
        self.assertEqual(saved.secret, "secret-new")
        # 短期令牌只在内存对象里；`backend.json` 里不落它。
        with open(cred.credentials_path(), encoding="utf-8") as fh:
            self.assertNotIn("accessToken", fh.read())

    def test_existing_backend_credentials_skip_bind(self):
        cred.save(BackendCredentials(base_url="http://gpu-01:8900",
                                     client_id="cli-old", secret="old-secret"))
        self.login()
        out = account_admin.bind_device()
        self.assertTrue(out["skipped"])
        self.assertEqual(self.calls("/v1/account/devices/bind"), [])

    def test_force_passes_existing_client_id_for_adoption(self):
        cred.save(BackendCredentials(base_url="http://gpu-01:8900",
                                     client_id="cli-old", secret="old-secret"))
        self.login()
        out = account_admin.bind_device(force=True)
        self.assertFalse(out["skipped"])
        body = self.calls("/v1/account/devices/bind")[-1]["body"]
        self.assertEqual(body["existingClientId"], "cli-old")

    def test_401_is_relogin_and_never_falls_back_to_pairing_codes(self):
        self.fake.bind_status = 401
        self.fake.bind_code = "unauthorized"
        self.login()
        with self.assertRaises(account_admin.AccountError) as caught:
            account_admin.bind_device()
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("重新登录", caught.exception.message)
        paths = [c["path"] for c in self.fake.calls]
        self.assertNotIn("/v1/account/devices/code", paths)
        self.assertNotIn("/v1/pair", paths)
        self.assertIsNone(cred.load(), "失败不能留下半份 backend.json")

    def test_402_403_409_keep_their_categories(self):
        cases = [
            (402, "entitlement_expired", "续费"),
            (403, "device_limit_reached", "设备数"),
            (409, "device_bound_to_other_account", "另一个账号"),
        ]
        self.login()
        for status, code, needle in cases:
            self.fake.bind_status = status
            self.fake.bind_code = code
            self.fake.bind_detail = ""
            with self.assertRaises(account_admin.AccountError) as caught:
                account_admin.bind_device()
            self.assertEqual(caught.exception.status, status, code)
            self.assertIn(needle, caught.exception.message, code)
        self.assertIsNone(cred.load())

    def test_404_is_reported_as_an_old_server_not_a_network_error(self):
        self.fake.bind_status = 404
        self.fake.bind_code = "not_found"
        self.login()
        with self.assertRaises(account_admin.AccountError) as caught:
            account_admin.bind_device()
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("版本较旧", caught.exception.message)
        self.assertIn("手动配对", caught.exception.message)

    def test_unbind_own_device_clears_local_credentials(self):
        cred.save(BackendCredentials(base_url="http://gpu-01:8900",
                                     client_id="cli-old", secret="old-secret"))
        self.login()
        out = account_admin.unbind_device("cli-old")
        self.assertTrue(out["ok"])
        self.assertTrue(out["cleared"])
        self.assertIsNone(cred.load())
        call = self.calls("/v1/account/devices/unbind")[-1]
        self.assertEqual(call["authorization"], "Bearer " + USER_TOKEN)


class LocalApiTests(_AccountAdminCase):
    """`/api/account/*` 只是薄薄一层；这里钉字段名与错误状态。"""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_login_session_bind_and_me_via_local_api(self):
        r = self.client.post("/api/account/login",
                             json={"email": "u@example.com",
                                   "password": "Passw0rd",
                                   "serverUrl": self.base_url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["serverUrl"], self.base_url)
        self.assertTrue(self.client.get("/api/account/session").json()["signedIn"])

        r = self.client.post("/api/account/devices/bind", json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["skipped"])

        r = self.client.get("/api/account/me")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["devices"][0]["clientId"], "cli-new")

    def test_bind_error_keeps_the_http_status(self):
        self.login()
        self.fake.bind_status = 402
        self.fake.bind_code = "entitlement_expired"
        r = self.client.post("/api/account/devices/bind", json={})
        self.assertEqual(r.status_code, 402)
        self.assertIn("续费", r.json()["detail"])

    def test_signed_out_session_is_a_plain_view_not_an_error(self):
        r = self.client.get("/api/account/session")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["signedIn"])

    def test_login_accepts_snake_case_too(self):
        r = self.client.post("/api/account/login",
                             json={"email": "u@example.com",
                                   "password": "Passw0rd",
                                   "server_url": self.base_url})
        self.assertEqual(r.status_code, 200, r.text)


class BasicWireTests(unittest.TestCase):
    def test_the_sealed_json_round_trip_is_shared_with_backend_credentials(self):
        """`account.json` 与 `backend.json` 必须共用同一套信封（不是两套）。"""
        tmp = tempfile.mkdtemp(prefix="echo-sealed-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        with patch.object(cred, "credentials_path",
                          lambda: os.path.join(tmp, "backend.json")):
            cred.write_sealed_json("account.json", {"userToken": "tok", "x": 1})
            got = cred.read_sealed_json("account.json")
            self.assertEqual(got, {"userToken": "tok", "x": 1})
            with open(os.path.join(tmp, "account.json"), "rb") as fh:
                raw = fh.read()
            self.assertNotIn(b"tok", raw)
            # 用同一套信封解回来（Windows 是 DPAPI 密文，POSIX 是 base64 明文）。
            self.assertEqual(cred.unseal_text(json.loads(raw)),
                             json.dumps(got, ensure_ascii=False, sort_keys=True))
