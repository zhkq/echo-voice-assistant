# -*- coding: utf-8 -*-
"""P5 provider 抽象与注册表的测试（D25）

钉住四件事：
1. **形状**：三类 kind、spec 必备字段、出网的 provider 必须写出网说明（不写就注册失败）；
2. **选择**：配置项优先（`provider<Kind>`）、指到不存在的 id 时回落默认，且不静默吞掉；
3. **本地适配器**：转写/朗读委托给 `app/audio/stt.py` / `tts.py`，**空结果要带 reason**
   （§19 发现③的教训：不能把"没人说话"和"引擎挂了"混成一个空串）；
4. **路由 provider（D25）**：说 OpenAI 兼容协议、能解析回复、失败时抛带原因的异常、
   **令牌不出现在清单里**。

不需要网络与重依赖：用 stdlib 起一个假的上游 HTTP 服务；重依赖模块按需 patch。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import providers as P                              # noqa: E402
from app.providers import router as router_mod              # noqa: E402
from app.providers.local import (LocalAsrProvider, LocalTtsProvider,   # noqa: E402
                                 EdgeTtsProvider)


class RegistryShapeTests(unittest.TestCase):
    def test_builtin_providers_are_registered(self):
        ids = {(p["kind"], p["id"]) for p in P.specs()}
        self.assertIn(("asr", "local-asr"), ids)
        self.assertIn(("tts", "local-tts"), ids)
        self.assertIn(("tts", "edge-tts"), ids)
        self.assertIn(("llm", "echo-auto"), ids)

    def test_every_kind_has_a_default(self):
        for kind in P.KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(P.default_id(kind), "%s 必须有一个默认 provider" % kind)
                self.assertIsNotNone(P.describe(kind, P.default_id(kind)))

    def test_specs_require_a_kind_and_source(self):
        with self.assertRaises(ValueError):
            P.ProviderSpec(id="x", kind="nope", name="x")
        with self.assertRaises(ValueError):
            P.ProviderSpec(id="x", kind="asr", name="x", source="who")

    def test_egress_must_be_explained(self):
        """出网 = 用户数据离开本机，必须写清"发什么、给谁"；这是规划里的硬要求。"""
        with self.assertRaises(ValueError):
            P.ProviderSpec(id="x", kind="llm", name="x", source="online", egress=True)
        spec = P.ProviderSpec(id="y", kind="llm", name="y", source="online",
                              egress=True, egress_note="发到某服务")
        self.assertTrue(spec["egress"])

    def test_local_providers_are_marked_offline(self):
        for pid in ("local-asr", "local-tts"):
            with self.subTest(pid=pid):
                spec = P.describe("tts" if pid.startswith("local-tts") else "asr", pid)
                self.assertFalse(spec["egress"])
                self.assertEqual(spec["source"], "local")

    def test_edge_tts_is_marked_online(self):
        spec = P.describe("tts", "edge-tts")
        self.assertTrue(spec["egress"])
        self.assertIn("微软", spec["egress_note"])

    def test_echo_auto_is_online_and_mentions_multi_upstream(self):
        spec = P.describe("llm", "echo-auto")
        self.assertTrue(spec["egress"])
        self.assertIn("上游", spec["egress_note"])
        self.assertEqual(spec["details"]["model"], "echo-auto")

    def test_unknown_provider_raises_with_the_available_list(self):
        with self.assertRaises(KeyError) as cm:
            P.create("asr", "no-such-provider")
        self.assertIn("local-asr", str(cm.exception), "报错要列出可选项，便于排查")


class ActiveSelectionTests(unittest.TestCase):
    def test_default_is_used_when_config_is_empty(self):
        with patch("app.config.settings.get", lambda k, d=None: ""):
            self.assertEqual(P.active_id("asr"), "local-asr")
            self.assertEqual(P.active_id("llm"), "echo-auto")

    def test_config_wins(self):
        def fake_get(key, default=None):
            return {"providerAsr": "local-asr", "providerLlm": "echo-auto"}.get(key, default)
        with patch("app.config.settings.get", fake_get):
            self.assertEqual(P.active_id("asr"), "local-asr")
            self.assertEqual(P.active_id("llm"), "echo-auto")

    def test_unknown_configured_id_falls_back_to_default(self):
        with patch("app.config.settings.get", lambda k, d=None: "ghost-provider"):
            self.assertEqual(P.active_id("asr"), P.default_id("asr"),
                             "配了不存在的 id 要回落默认（并且不抛异常）")

    def test_active_returns_id_and_instance(self):
        with patch("app.config.settings.get", lambda k, d=None: ""):
            pid, inst = P.active("tts")
        self.assertEqual(pid, "local-tts")
        self.assertIsInstance(inst, LocalTtsProvider)

    def test_readiness_never_raises(self):
        for spec in P.specs():
            with self.subTest(provider=spec["id"]):
                self.assertIn(P.readiness(spec["kind"], spec["id"]), (True, False, None))

    def test_catalog_carries_egress_and_active_flags(self):
        with patch("app.config.settings.get", lambda k, d=None: ""):
            cat = P.catalog(ready=False)
        self.assertEqual([k["id"] for k in cat["kinds"]], list(P.KINDS))
        by_id = {p["id"]: p for p in cat["providers"]}
        self.assertTrue(by_id["edge-tts"]["egress"])
        self.assertTrue(by_id["local-asr"]["active"])
        self.assertFalse(by_id["edge-tts"]["active"])
        # 清单里不许出现任何疑似凭据字段
        blob = json.dumps(cat, ensure_ascii=False).lower()
        for bad in ("token", "api_key", "apikey", "secret", "password"):
            self.assertNotIn(bad, blob, "provider 清单里不该出现凭据字段：%s" % bad)


class LocalAdapterTests(unittest.TestCase):
    def test_asr_delegates_to_stt_and_reports_reason_on_empty(self):
        calls = {}

        def fake_transcribe(wav, engine, model, lang, device):
            calls.update(wav=wav, engine=engine, model=model, lang=lang, device=device)
            return ""                       # 引擎"成功"但没内容

        with patch("app.audio.stt.transcribe", fake_transcribe), \
                patch("app.audio.stt.resolve_engine", lambda c: ("whisper", "small")), \
                patch("app.config.settings.get",
                      lambda k, d=None: {"sttModel": "small", "device": "cuda"}.get(k, d)):
            out = LocalAsrProvider().transcribe("a.wav", lang="zh")
        self.assertEqual(calls["engine"], "whisper")
        self.assertEqual(calls["device"], "cuda")
        self.assertEqual(out["text"], "")
        self.assertEqual(out["reason"], "empty-or-unknown",
                         "空结果必须能被调用方识别，不能混成'成功'" % ())

    def test_asr_returns_text_when_engine_produced_something(self):
        with patch("app.audio.stt.transcribe", lambda *a, **k: "你好"), \
                patch("app.audio.stt.resolve_engine", lambda c: ("whisper", "small")):
            out = LocalAsrProvider().transcribe("a.wav")
        self.assertEqual(out["text"], "你好")
        self.assertNotIn("reason", out)

    def test_tts_local_uses_the_platform_offline_engine(self):
        seen = []
        with patch("app.audio.tts.speak", lambda text, engine, timeout=60: seen.append(engine) or True), \
                patch("app.providers.local._offline_label", lambda: "say"):
            self.assertTrue(LocalTtsProvider().speak("你好"))
        self.assertEqual(seen, ["say"])

    def test_edge_tts_uses_the_online_engine(self):
        seen = []
        with patch("app.audio.tts.speak", lambda text, engine, timeout=60: seen.append(engine) or True):
            self.assertTrue(EdgeTtsProvider().speak("你好"))
        self.assertEqual(seen, ["edge-tts"])

    def test_tts_failure_is_reported_as_false_not_an_exception(self):
        with patch("app.audio.tts.speak", side_effect=RuntimeError("device gone")):
            self.assertFalse(LocalTtsProvider().speak("你好"))


class _StubUpstream(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容上游：记录收到的请求，按脚本回复。"""

    reply = {"choices": [{"message": {"content": "好的"}}]}
    status = 200
    seen = []

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        _StubUpstream.seen.append({"path": self.path, "body": json.loads(body or "{}"),
                                   "auth": self.headers.get("Authorization")})
        payload = json.dumps(_StubUpstream.reply).encode("utf-8")
        self.send_response(_StubUpstream.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):                           # 静音
        pass


class EchoAutoLlmProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _StubUpstream)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _StubUpstream.seen = []
        _StubUpstream.status = 200
        _StubUpstream.reply = {"choices": [{"message": {"content": "好的"}}]}

    def test_chat_sends_openai_shape_and_returns_content(self):
        out = router_mod.EchoAutoLlmProvider().chat(
            [{"role": "user", "content": "你好"}], base_url=self.base, api_key="tok")
        self.assertEqual(out, "好的")
        sent = _StubUpstream.seen[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["body"]["model"], "echo-auto")
        self.assertFalse(sent["body"]["stream"])
        self.assertEqual(sent["auth"], "Bearer tok")

    def test_http_error_raises_with_status(self):
        _StubUpstream.status = 502
        with self.assertRaises(RuntimeError) as cm:
            router_mod.EchoAutoLlmProvider().chat(
                [{"role": "user", "content": "hi"}], base_url=self.base)
        self.assertIn("502", str(cm.exception))
        self.assertIn("echo-auto", str(cm.exception))

    def test_unreachable_router_raises_a_helpful_error(self):
        with self.assertRaises(RuntimeError) as cm:
            router_mod.EchoAutoLlmProvider().chat(
                [{"role": "user", "content": "hi"}],
                base_url="http://127.0.0.1:1/v1", timeout=1)
        self.assertIn("路由", str(cm.exception))

    def test_unknown_payload_shape_raises_not_empty_string(self):
        _StubUpstream.reply = {"unexpected": True}
        with self.assertRaises(RuntimeError):
            router_mod.EchoAutoLlmProvider().chat(
                [{"role": "user", "content": "hi"}], base_url=self.base)

    def test_empty_upstream_reply_raises(self):
        _StubUpstream.reply = {"choices": [{"message": {"content": "   "}}]}
        with self.assertRaises(RuntimeError) as cm:
            router_mod.EchoAutoLlmProvider().chat(
                [{"role": "user", "content": "hi"}], base_url=self.base)
        self.assertIn("空回复", str(cm.exception))

    def test_empty_messages_is_rejected_locally(self):
        with self.assertRaises(ValueError):
            router_mod.EchoAutoLlmProvider().chat([])

    def test_base_url_follows_the_router_port(self):
        with patch("app.failover_proxy.proxy_port", lambda: 18899):
            self.assertEqual(router_mod.EchoAutoLlmProvider().base_url(),
                             "http://127.0.0.1:18899/v1")

    def test_token_is_never_in_the_catalog(self):
        with patch("app.llm_router.router_token", lambda: "SUPER-SECRET-TOKEN"):
            cat = json.dumps(P.catalog(ready=False), ensure_ascii=False)
        self.assertNotIn("SUPER-SECRET-TOKEN", cat)


class ProviderApiTests(unittest.TestCase):
    """`GET /api/providers`：面板要用的只读清单（默认不探测）。"""

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_endpoint_lists_providers_without_credentials(self):
        r = self.client.get("/api/providers")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual([k["id"] for k in data["kinds"]], list(P.KINDS))
        blob = json.dumps(data, ensure_ascii=False).lower()
        for bad in ("token", "api_key", "secret", "password"):
            self.assertNotIn(bad, blob)
        by_id = {p["id"]: p for p in data["providers"]}
        self.assertTrue(by_id["edge-tts"]["egress"])
        self.assertFalse(by_id["local-tts"]["egress"])

    def test_default_call_does_not_probe_readiness(self):
        """默认 ready=false：不得触发网络探测（否则打开设置页就会打外网）。"""
        data = self.client.get("/api/providers").json()
        self.assertTrue(all(p["ready"] is None for p in data["providers"]))

    def test_ready_true_probes(self):
        with patch("app.providers.readiness", lambda kind, pid=None: True):
            data = self.client.get("/api/providers?ready=true").json()
        self.assertTrue(all(p["ready"] is True for p in data["providers"]))


if __name__ == "__main__":
    unittest.main()
