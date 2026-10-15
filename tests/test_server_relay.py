# -*- coding: utf-8 -*-
"""中继（`server/relay.py`）的契约与护栏。

中继是**唯一一条**让公网上的请求进到办公电脑里的路，所以这里钉的几乎全是"不许"：

  * 不许访问 `/api/*` 与 `/health` 以外的任何路径（两处都判：后端 + 主机侧）；
  * 不许冒充别人的主机（`hostId` 取自已认证的 `client_id`，**不接受自报**）；
  * 不许把"主机没在线"变成"请求排着队"（离线就 503，说清原因）；
  * 不许无限刷（按来源 IP 限流）、不许传超大 body；
  * **没有令牌**的 `/api/*` 请求直接 401（真正鉴权在 ECHO 那枚手机令牌上，
    但这一层不能连粗筛都不做）。

真链路用「两个 TestClient + 一个线程」端到端跑一遍：主机 poll 走的是真 HTTP。
"""
import asyncio
import base64
import json
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient                              # noqa: E402

from server import relay as relay_mod                                  # noqa: E402
from server import main as server_main                                 # noqa: E402


# ---------------------------------------------------------------- 纯函数

class PathAllowlistTests(unittest.TestCase):
    """只放行 `/health` 与 `api/*` —— 这是中继最要紧的一条线。"""

    def test_api_and_health_pass(self):
        for p in ("api/status", "/api/status", "api/commands", "api/assistant/voice-command",
                  "health", "/health"):
            with self.subTest(p=p):
                self.assertTrue(relay_mod.path_allowed(p), p)

    def test_everything_else_is_refused(self):
        for p in ("", "/", "etc/passwd", "/etc/passwd", "admin/", "v1/asr",
                  "apifoo", "api", "API/status", "../api/status", "api/../../etc/passwd",
                  "static/index.html"):
            with self.subTest(p=p):
                self.assertFalse(relay_mod.path_allowed(p), p)

    def test_token_is_only_waived_for_health_and_pairing(self):
        self.assertFalse(relay_mod._needs_token("health"))
        self.assertFalse(relay_mod._needs_token("/health"))
        self.assertFalse(relay_mod._needs_token("api/pair/phone/claim"))
        self.assertTrue(relay_mod._needs_token("api/status"))
        self.assertTrue(relay_mod._needs_token("api/meetings/1/audio"))


class RateLimiterTests(unittest.TestCase):

    def test_it_allows_then_blocks_within_the_window(self):
        now = [1000.0]
        lim = relay_mod.RateLimiter(per_minute=3, now=lambda: now[0])
        self.assertTrue(lim.allow("1.2.3.4"))
        self.assertTrue(lim.allow("1.2.3.4"))
        self.assertTrue(lim.allow("1.2.3.4"))
        self.assertFalse(lim.allow("1.2.3.4"), "第 4 次该被挡")
        self.assertTrue(lim.allow("5.6.7.8"), "限流按来源分开")
        now[0] += 61.0
        self.assertTrue(lim.allow("1.2.3.4"), "窗口滑过去就该放行")


# ---------------------------------------------------------------- 中枢（异步）

class HubTests(unittest.TestCase):

    def test_offline_host_is_a_clear_503(self):
        hub = relay_mod.RelayHub(call_timeout_s=0.5)
        with self.assertRaises(relay_mod.RelayError) as ctx:
            asyncio.run(hub.submit("nobody", {"id": "x"}))
        self.assertEqual(503, ctx.exception.status)
        self.assertEqual("relay_host_offline", ctx.exception.code)
        self.assertIn("没连上", ctx.exception.message)

    def test_a_call_goes_out_and_the_result_comes_back(self):
        hub = relay_mod.RelayHub(call_timeout_s=5.0)

        async def scenario():
            hub.touch("host1")                       # 主机先挂上
            task = asyncio.create_task(
                hub.submit("host1", {"id": "c1", "method": "GET", "path": "api/status"}))
            await asyncio.sleep(0.05)
            calls = await hub.poll("host1", wait_s=1.0)
            self.assertEqual(1, len(calls))
            self.assertEqual("api/status", calls[0]["path"])
            ok = hub.push_result("host1", "c1", {"id": "c1", "status": 200, "body": "eA=="})
            self.assertTrue(ok)
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(200, result["status"])
        self.assertEqual("eA==", result["body"])

    def test_a_late_result_is_not_accepted_twice(self):
        hub = relay_mod.RelayHub(call_timeout_s=0.1)

        async def scenario():
            hub.touch("host1")
            with self.assertRaises(relay_mod.RelayError) as ctx:
                await hub.submit("host1", {"id": "c9"})
            self.assertEqual(504, ctx.exception.status)
            self.assertEqual("relay_timeout", ctx.exception.code)
            return hub.push_result("host1", "c9", {"status": 200})

        self.assertFalse(asyncio.run(scenario()), "超时之后交回来的结果该被丢掉")

    def test_the_queue_has_a_ceiling(self):
        hub = relay_mod.RelayHub(call_timeout_s=0.05)

        async def scenario():
            hub.touch("host1")
            for i in range(relay_mod.MAX_QUEUED):
                hub.hosts["host1"].queue.append({"id": "q%d" % i})
            with self.assertRaises(relay_mod.RelayError) as ctx:
                await hub.submit("host1", {"id": "overflow"})
            return ctx.exception

        exc = asyncio.run(scenario())
        self.assertEqual(503, exc.status)
        self.assertEqual("relay_busy", exc.code)

    def test_a_stale_host_counts_as_offline(self):
        hub = relay_mod.RelayHub(call_timeout_s=0.1, host_stale_s=0.01)
        hub.touch("host1")
        time.sleep(0.03)
        self.assertIsNone(hub.live_host("host1"))
        self.assertEqual([], hub.snapshot(), "掉线的不该出现在在线清单里")


# ---------------------------------------------------------------- HTTP 面

class _FakeAuth:
    """只认静态令牌的假鉴权：够测"hostId 来自认证"这一条，不拖数据库进来。"""

    def __init__(self, tokens):
        self.tokens = dict(tokens)

    def authenticate(self, authorization, need_scope=""):
        raw = str(authorization or "")
        tok = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
        cid = self.tokens.get(tok)
        if not cid:
            from server import errors
            raise errors.unauthorized("缺少 Bearer 令牌")
        return {"client_id": cid, "scopes": "", "token_version": 1, "disabled": 0}


def setUpModule():
    """快照 `app.paths` 的取值源。

    `create_app()` 里的 `install_paths_seam` 是**进程内全局** Monkey-patch：
    不还原会连累排在后面的测试文件（2026-09-24 真出过事故，护栏就是
    `tests/test_server_contract.py::PathsSeamTests`）。这里与 `_AppCase` 同一个手法。
    """
    from app import paths
    global _PATHS_SEAM
    _PATHS_SEAM = getattr(paths, "_settings_get", None)


def tearDownModule():
    from app import paths
    if _PATHS_SEAM is None:
        try:
            delattr(paths, "_settings_get")
        except AttributeError:
            pass
    else:
        paths._settings_get = _PATHS_SEAM


def _app(hub=None, tokens=None):
    """建 app：**不跑 lifespan**（中继不需要模型池），state 里塞假的鉴权与中枢。"""
    app = server_main.create_app(_cfg())
    app.state.echo = SimpleNamespace(
        auth=_FakeAuth(tokens or {"tok-host": "host1", "tok-phone": "phone"}))
    app.state.relay_hub = hub or relay_mod.RelayHub(call_timeout_s=0.4)
    return app


def _cfg():
    """与其它服务端用例同一个起法：`load()` 拿默认，再改要用的那几项。"""
    from server import settings as settings_mod
    cfg = settings_mod.load()
    cfg.raw["server"]["listen"] = "127.0.0.1:8900"
    cfg.raw["auth"]["enabled"] = True
    return cfg


HOST_HDR = {"Authorization": "Bearer tok-host"}
PHONE_HDR = {"Authorization": "Bearer tok-phone"}


class HttpSurfaceTests(unittest.TestCase):

    def setUp(self):
        self.app = _app()
        self.client = TestClient(self.app)

    def test_status_lists_who_is_online(self):
        self.client.post("/v1/relay/host/poll", json={"wait": 0.01}, headers=HOST_HDR)
        r = self.client.post("/v1/relay/status", headers=HOST_HDR)
        self.assertEqual(200, r.status_code)
        hosts = r.json()["hosts"]
        self.assertEqual(["host1"], [h["hostId"] for h in hosts])

    def test_status_needs_a_token(self):
        """它有主机名与 hostId（= 往哪台电脑转发的钥匙），不该匿名公开。"""
        r = self.client.post("/v1/relay/status")
        self.assertEqual(401, r.status_code)

    def test_the_host_endpoints_need_a_valid_token(self):
        for path in ("/v1/relay/host/poll", "/v1/relay/host/result"):
            with self.subTest(path=path):
                r = self.client.post(path, json={})
                self.assertEqual(401, r.status_code, path)
                self.assertEqual("relay_unauthorized", r.json()["code"])

    def test_a_bearer_from_another_client_cannot_impersonate(self):
        """拿着"手机"的令牌来挂主机通道 → 认成 phone，不是 host1。"""
        self.client.post("/v1/relay/host/poll", json={"wait": 0.01}, headers=PHONE_HDR)
        hosts = self.client.post("/v1/relay/status", headers=HOST_HDR).json()["hosts"]
        self.assertEqual(["phone"], [h["hostId"] for h in hosts])

    def test_paths_outside_the_allowlist_are_403(self):
        for path in ("/v1/relay/host1/etc/passwd", "/v1/relay/host1/admin/secret"):
            with self.subTest(path=path):
                r = self.client.get(path, headers=PHONE_HDR)
                self.assertEqual(403, r.status_code)
                self.assertEqual("relay_forbidden_path", r.json()["code"])

    def test_api_without_a_token_is_401(self):
        r = self.client.get("/v1/relay/host1/api/status")
        self.assertEqual(401, r.status_code)
        self.assertEqual("relay_unauthorized", r.json()["code"])

    def test_health_and_pairing_go_through_without_a_token(self):
        """两条豁免：ECHO 自己的探针与首次配对（此时手机还没有令牌）。"""
        for path in ("/v1/relay/host1/health", "/v1/relay/host1/api/pair/phone/claim"):
            with self.subTest(path=path):
                r = self.client.post(path, json={})
                # 主机不在线 → 503；**不是 401** —— 也就是说"拿令牌"这一步没拦住它
                self.assertEqual(503, r.status_code, path)
                self.assertEqual("relay_host_offline", r.json()["code"])

    def test_the_phone_entry_is_503_when_the_host_is_offline(self):
        r = self.client.get("/v1/relay/host1/api/status", headers=PHONE_HDR)
        self.assertEqual(503, r.status_code)
        self.assertEqual("relay_host_offline", r.json()["code"])

    def test_a_huge_body_is_refused(self):
        with mock.patch.object(relay_mod, "MAX_BODY_BYTES", 64):
            r = self.client.post("/v1/relay/host1/api/assistant/voice-command",
                                 content=b"x" * 200,
                                 headers={**PHONE_HDR, "Content-Type": "audio/wav"})
        self.assertEqual(413, r.status_code)
        self.assertEqual("relay_too_large", r.json()["code"])

    def test_rate_limit_kicks_in(self):
        self.app.state.relay_hub.limiter.per_minute = 1
        first = self.client.get("/v1/relay/host1/api/status", headers=PHONE_HDR)
        second = self.client.get("/v1/relay/host1/api/status", headers=PHONE_HDR)
        self.assertEqual(503, first.status_code, "第一条只是主机离线")
        self.assertEqual(429, second.status_code)
        self.assertEqual("relay_rate_limited", second.json()["code"])


class EndToEndTests(unittest.TestCase):
    """手机 → 后端 → 主机（真 HTTP、真线程）→ 手机。"""

    def test_a_phone_request_reaches_the_host_and_comes_back(self):
        app = _app(hub=relay_mod.RelayHub(call_timeout_s=8.0))
        host_client = TestClient(app)
        phone_client = TestClient(app)
        seen = {}

        def host_loop():
            r = host_client.post("/v1/relay/host/poll", json={"wait": 5}, headers=HOST_HDR)
            calls = (r.json() or {}).get("calls") or []
            if not calls:
                return
            call = calls[0]
            seen["call"] = call
            body = base64.b64encode(json.dumps({"ok": True, "from": "host"}).encode()).decode()
            host_client.post("/v1/relay/host/result",
                             json={"id": call["id"], "status": 200,
                                   "headers": {"content-type": "application/json"},
                                   "body": body},
                             headers=HOST_HDR)

        t = threading.Thread(target=host_loop, daemon=True)
        t.start()
        time.sleep(0.4)                      # 让主机先挂上（poll 已经等在长轮询里）
        r = phone_client.get("/v1/relay/host1/api/status?x=1", headers=PHONE_HDR)
        t.join(10)

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({"ok": True, "from": "host"}, r.json())
        self.assertEqual("application/json", r.headers.get("content-type"))

        call = seen.get("call") or {}
        self.assertEqual("GET", call.get("method"))
        self.assertEqual("api/status", call.get("path"))
        self.assertEqual("x=1", call.get("query"))
        self.assertEqual("Bearer tok-phone", (call.get("headers") or {}).get("authorization"),
                         "手机那枚令牌要原样带给主机（端到端鉴权就靠它）")

    def test_a_binary_body_survives_the_trip(self):
        """语音那条路是**裸 wav 字节**：base64 来回一趟必须一字不差。"""
        app = _app(hub=relay_mod.RelayHub(call_timeout_s=8.0))
        host_client = TestClient(app)
        phone_client = TestClient(app)
        payload = bytes(range(256)) * 4
        seen = {}

        def host_loop():
            r = host_client.post("/v1/relay/host/poll", json={"wait": 5}, headers=HOST_HDR)
            calls = (r.json() or {}).get("calls") or []
            if not calls:
                return
            call = calls[0]
            seen["body"] = base64.b64decode(call.get("body") or "")
            host_client.post("/v1/relay/host/result",
                             json={"id": call["id"], "status": 200,
                                   "headers": {"content-type": "audio/wav"},
                                   "body": base64.b64encode(b"RIFF-ok").decode()},
                             headers=HOST_HDR)

        t = threading.Thread(target=host_loop, daemon=True)
        t.start()
        time.sleep(0.4)
        r = phone_client.post("/v1/relay/host1/api/assistant/voice-command",
                              content=payload,
                              headers={**PHONE_HDR, "Content-Type": "audio/wav"})
        t.join(10)

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(b"RIFF-ok", r.content)
        self.assertEqual("audio/wav", r.headers.get("content-type"))
        self.assertEqual(payload, seen.get("body"), "上传的字节必须原样到主机")


if __name__ == "__main__":
    unittest.main()
