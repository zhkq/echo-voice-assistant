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
import re
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                          # noqa: E402
from app import providers as P                              # noqa: E402
from app.config import settings                              # noqa: E402
from app.providers import openai as online_mod               # noqa: E402
from app.providers import presets as presets_mod             # noqa: E402
from app.providers import router as router_mod               # noqa: E402
from app.providers.local import (LocalAsrProvider, LocalTtsProvider,   # noqa: E402
                                 EdgeTtsProvider)

#: 凭据类**字段名**（不是值）。清单里可以出现"哪个配置项喂给哪个 provider"
#: （那是名字，如 details.settings=["providerLlmApiKey"]），但**不许有字段专门装密钥**。
CRED_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|passwd|authorization)", re.I)


class _NoCredentialFields:
    """Mixin：递归检查结构里有没有凭据类字段名（值是否泄漏由 SecretHandlingTests 单独测）。"""

    def assertNoCredentialFields(self, obj, path="catalog"):
        if isinstance(obj, dict):
            for k, v in obj.items():
                self.assertNotRegex(str(k), CRED_KEY_RE,
                                    "%s 里出现了凭据字段名：%s" % (path, k))
                self.assertNoCredentialFields(v, "%s.%s" % (path, k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self.assertNoCredentialFields(v, "%s[%d]" % (path, i))


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


class ActiveSelectionTests(_NoCredentialFields, unittest.TestCase):
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
        # 清单里不许有"专门装密钥"的字段（配置项名字允许出现：面板要知道哪个配置喂给谁）
        self.assertNoCredentialFields(cat)


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


class ProviderApiTests(_NoCredentialFields, unittest.TestCase):
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
        self.assertNoCredentialFields(data, "api/providers")
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


class SecretHandlingTests(unittest.TestCase):
    """凭据管理（P5）：密钥只进库、**永不从接口出去**，但 provider 内部要拿得到真值。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-provider-secret-")
        cls._old = (db.DATA_DIR, db.DB_FILE)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "%s.db" % self._testMethodName)
        db.init()
        settings.seed_defaults()
        settings._cache = None
        self.addCleanup(setattr, settings, "_cache", None)

    SECRET = "sk-super-secret-value-123"

    def test_secret_keys_are_declared_with_metadata(self):
        from app.config import DEFAULTS
        for key in ("providerLlmApiKey", "providerAsrApiKey"):
            with self.subTest(key=key):
                self.assertTrue(DEFAULTS[key].get("secret"), "%s 必须标 secret" % key)
                self.assertEqual(DEFAULTS[key]["grp"], "provider")
                self.assertTrue(DEFAULTS[key]["label"])

    def _config_endpoint(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def _cfg_row(self, key):
        rows = self._config_endpoint().get("/api/providers/config").json()["settings"]
        return {r["key"]: r for r in rows}[key]

    def test_generic_form_no_longer_carries_the_secrets(self):
        """这九个键是 hidden 的：通用设置表单里**看不到**它们（统一由卡片承载）。"""
        settings.update({"providerLlmApiKey": self.SECRET})
        keys = {r["key"] for r in settings.all()}
        for key in ("providerLlmApiKey", "providerAsrApiKey", "providerAsrBaseUrl"):
            self.assertNotIn(key, keys, "通用表单不该再出现 provider 配置（两套界面）")

    def test_config_endpoint_masks_secrets(self):
        settings.update({"providerLlmApiKey": self.SECRET})
        r = self._cfg_row("providerLlmApiKey")
        self.assertEqual(r["value"], "", "卡片拿到的必须是空的")
        self.assertTrue(r["secret"])
        self.assertTrue(r["hasValue"], "要告诉卡片库里其实有值")

    def test_settings_get_still_returns_the_real_value(self):
        """provider 组装请求头时用的是真值 —— 遮罩只发生在出口。"""
        settings.update({"providerLlmApiKey": self.SECRET})
        self.assertEqual(settings.get("providerLlmApiKey"), self.SECRET)

    def test_empty_secret_reports_has_value_false(self):
        settings.update({"providerLlmApiKey": ""})
        self.assertFalse(self._cfg_row("providerLlmApiKey")["hasValue"])

    # ---- 防误清空（2026-09-19 发现的数据丢失风险）----
    def test_empty_string_never_clears_a_secret(self):
        """面板"整批保存"会把遮罩后的空串回传 —— 那**不能**当成"清空密钥"。"""
        settings.update({"providerLlmApiKey": self.SECRET})
        settings.update({"providerLlmApiKey": ""})
        self.assertEqual(settings.get("providerLlmApiKey"), self.SECRET,
                         "空串 = 不改（否则用户一保存设置，密钥就静默没了）")
        settings.update({"providerLlmApiKey": "   "})
        self.assertEqual(settings.get("providerLlmApiKey"), self.SECRET)

    def test_panel_like_bulk_save_keeps_the_secret(self):
        """模拟面板：把 settings.all() 里所有项原样回传（密钥是空串）→ 密钥必须还在。"""
        settings.update({"providerLlmApiKey": self.SECRET})
        echoed = {r["key"]: r["value"] for r in settings.all()}
        settings.update(echoed)
        self.assertEqual(settings.get("providerLlmApiKey"), self.SECRET)

    def test_clear_sentinel_is_the_only_way_to_wipe(self):
        from app.config import CLEAR_SECRET
        settings.update({"providerLlmApiKey": self.SECRET})
        settings.update({"providerLlmApiKey": CLEAR_SECRET})
        self.assertEqual(settings.get("providerLlmApiKey"), "")
        self.assertFalse(self._cfg_row("providerLlmApiKey")["hasValue"])

    def test_non_secret_keys_still_accept_empty(self):
        """闸只对 secret 生效：普通项（如 meetingsDir）空串仍是合法值。"""
        settings.update({"meetingsDir": "D:\\会议"})
        settings.update({"meetingsDir": ""})
        self.assertEqual(settings.get("meetingsDir"), "")

    def test_api_settings_never_returns_the_secret(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        settings.update({"providerLlmApiKey": self.SECRET})
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        self.assertNotIn(self.SECRET, client.get("/api/settings").text)
        self.assertNotIn(self.SECRET, client.get("/api/providers/config").text,
                         "卡片端点回显了密钥！")

    def test_api_put_with_masked_values_does_not_wipe(self):
        """端到端：面板那套请求（含空密钥）打过来，密钥必须还在。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        settings.update({"providerLlmApiKey": self.SECRET})
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        payload = {r["key"]: r["value"] for r in client.get("/api/settings").json()["settings"]}
        payload["providerLlmApiKey"] = ""                 # 遮罩后的空串（最危险的那个）
        payload["userLocation"] = "北京"
        r = client.put("/api/settings", json={"values": payload})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(settings.get("providerLlmApiKey"), self.SECRET,
                         "整批保存后密钥被清掉了 —— 这正是要防的事故")

    def test_secret_never_reaches_the_provider_catalog(self):
        settings.update({"providerLlmApiKey": self.SECRET})
        self.assertNotIn(self.SECRET, json.dumps(P.catalog(ready=False), ensure_ascii=False))


class _StubOpenAI(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容服务：同时应付 chat/completions 与 audio/transcriptions。"""

    reply = {"choices": [{"message": {"content": "纪要正文"}}]}
    asr_reply = {"text": "转写文本"}
    status = 200
    seen = []

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        _StubOpenAI.seen.append({
            "path": self.path,
            "ctype": self.headers.get("Content-Type") or "",
            "auth": self.headers.get("Authorization"),
            "body": body,
        })
        payload = json.dumps(_StubOpenAI.asr_reply if "transcriptions" in self.path
                             else _StubOpenAI.reply).encode("utf-8")
        self.send_response(_StubOpenAI.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


class OnlineProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _StubOpenAI)
        cls.base = "http://127.0.0.1:%d/v1" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _StubOpenAI.seen = []
        _StubOpenAI.status = 200
        _StubOpenAI.reply = {"choices": [{"message": {"content": "纪要正文"}}]}
        _StubOpenAI.asr_reply = {"text": "转写文本"}
        self.cfg = {"providerLlmBaseUrl": self.base, "providerLlmApiKey": "k-llm",
                    "providerLlmModel": "deepseek-chat",
                    "providerAsrBaseUrl": self.base, "providerAsrApiKey": "k-asr",
                    "providerAsrModel": ""}
        self.patcher = patch("app.config.settings.get",
                             lambda k, d=None: self.cfg.get(k, d))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_llm_chat_uses_config_and_returns_content(self):
        out = online_mod.OpenAICompatLlmProvider().chat([{"role": "user", "content": "写纪要"}])
        self.assertEqual(out, "纪要正文")
        sent = _StubOpenAI.seen[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer k-llm")
        self.assertIn(b"deepseek-chat", sent["body"])

    def test_llm_ready_reflects_configuration(self):
        self.assertTrue(online_mod.OpenAICompatLlmProvider().ready())
        self.cfg["providerLlmBaseUrl"] = ""
        self.assertFalse(online_mod.OpenAICompatLlmProvider().ready())

    def test_llm_without_base_url_gives_a_clear_error(self):
        self.cfg["providerLlmBaseUrl"] = ""
        with self.assertRaises(RuntimeError) as cm:
            online_mod.OpenAICompatLlmProvider().chat([{"role": "user", "content": "x"}])
        self.assertIn("在线 LLM 地址", str(cm.exception))

    def test_llm_http_error_raises(self):
        _StubOpenAI.status = 401
        with self.assertRaises(RuntimeError) as cm:
            online_mod.OpenAICompatLlmProvider().chat([{"role": "user", "content": "x"}])
        self.assertIn("401", str(cm.exception))

    def test_asr_uploads_multipart_with_model_and_language(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(b"RIFF....WAVE")
            wav = fh.name
        self.addCleanup(os.remove, wav)
        out = online_mod.OpenAICompatAsrProvider().transcribe(wav, lang="zh")
        self.assertEqual(out["text"], "转写文本")
        self.assertEqual(out["engine"], "openai-asr")
        self.assertEqual(out["model"], "whisper-1", "模型名留空时用默认 whisper-1")
        sent = _StubOpenAI.seen[0]
        self.assertEqual(sent["path"], "/v1/audio/transcriptions")
        self.assertIn("multipart/form-data; boundary=", sent["ctype"])
        self.assertIn(b'name="model"', sent["body"])
        self.assertIn(b"whisper-1", sent["body"])
        self.assertIn(b'name="language"', sent["body"])
        self.assertIn(b"RIFF....WAVE", sent["body"], "音频本体要真的上传")
        self.assertEqual(sent["auth"], "Bearer k-asr")

    def test_asr_empty_result_carries_a_reason(self):
        _StubOpenAI.asr_reply = {"text": ""}
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(b"RIFF")
            wav = fh.name
        self.addCleanup(os.remove, wav)
        out = online_mod.OpenAICompatAsrProvider().transcribe(wav)
        self.assertEqual(out["text"], "")
        self.assertEqual(out["reason"], "empty-or-unknown")

    def test_asr_missing_file_is_reported(self):
        with self.assertRaises(RuntimeError) as cm:
            online_mod.OpenAICompatAsrProvider().transcribe("no-such-file.wav")
        self.assertIn("不存在", str(cm.exception))

    def test_asr_unconfigured_is_reported(self):
        self.cfg["providerAsrBaseUrl"] = ""
        with self.assertRaises(RuntimeError) as cm:
            online_mod.OpenAICompatAsrProvider().transcribe(__file__)
        self.assertIn("在线转写地址", str(cm.exception))

    def test_asr_http_error_raises(self):
        _StubOpenAI.status = 500
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(b"RIFF")
            wav = fh.name
        self.addCleanup(os.remove, wav)
        with self.assertRaises(RuntimeError) as cm:
            online_mod.OpenAICompatAsrProvider().transcribe(wav)
        self.assertIn("500", str(cm.exception))

    def test_online_providers_are_registered_and_labelled(self):
        for kind, pid in (("llm", "openai-llm"), ("asr", "openai-asr")):
            with self.subTest(provider=pid):
                spec = P.describe(kind, pid)
                self.assertTrue(spec["egress"])
                self.assertTrue(spec["egress_note"])
                self.assertEqual(spec["source"], "online")

    def test_asr_egress_note_mentions_audio_upload(self):
        """最容易忽略的出网：会议音频是**整段**上传的，必须写明。"""
        self.assertIn("音频", P.describe("asr", "openai-asr")["egress_note"])

    def test_active_llm_can_be_pointed_at_the_online_provider(self):
        self.cfg["providerLlm"] = "openai-llm"
        self.assertEqual(P.active_id("llm"), "openai-llm")


class PresetTests(unittest.TestCase):
    def test_presets_are_public_only(self):
        data = presets_mod.catalog()
        blob = json.dumps(data, ensure_ascii=False)
        for bad in ("sk-", "api_key", "apikey", "token", "secret"):
            self.assertNotIn(bad, blob.lower(), "预设里不该出现凭据类字样")
        for p in data["presets"]:
            with self.subTest(preset=p["id"]):
                self.assertTrue(p["note"], "每条预设都要有出网说明")
                if p["base_url"]:
                    self.assertTrue(p["base_url"].startswith("https://"),
                                    "公开预设只放 https 公网地址（内网地址不写进仓库）")

    def test_intranet_preset_leaves_the_url_for_the_user(self):
        p = presets_mod.find("intranet-gateway")
        self.assertIsNotNone(p)
        self.assertEqual(p["base_url"], "", "内网地址属内部信息，必须留空让用户填")

    def test_presets_endpoint_is_read_only_and_secret_free(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        r = TestClient(app).get("/api/providers/presets")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("presets", r.json())


class PanelWiringTests(unittest.TestCase):
    """面板接线守卫（P5 + 2026-09-19 合并成一个「能力」页签）。

    面板 JS 在本仓库没有单测基础设施（只跑 `node --check`），所以用"源码断言"把
    **接线必须存在**这件事钉住 —— 否则将来重构页签很容易把这块界面变成孤儿：
    端点还在、函数还在，但没人调用，用户看不到任何 provider 选择界面。

    2026-09-19 变化：原「模型」页签 + 原「组件」页签 + 设置页的「能力 provider」卡片
    三处都在回答"每个功能由什么实现、装好了没"，用户指出设计重叠 → 合并成一个
    一级页签「能力」（`#view-capabilities`）。provider 的选择/在线服务编辑搬进该页签。
    """

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "app.js"), encoding="utf-8") as fh:
            cls.js = fh.read()
        with open(os.path.join(root, "web", "index.html"), encoding="utf-8") as fh:
            cls.html = fh.read()

    def test_capability_page_uses_all_three_endpoints(self):
        self.assertIn("function loadCapabilities", self.js)
        self.assertIn("/api/providers?ready=true", self.js)
        self.assertIn("/api/providers/presets", self.js)
        self.assertIn("/api/providers/config", self.js)
        self.assertIn("/api/components?includeBlocked=true", self.js)

    def test_capability_page_is_a_first_level_tab_and_loaded(self):
        self.assertIn('data-view="capabilities"', self.html)
        self.assertIn('id="view-capabilities"', self.html)
        self.assertIn('if (name === "capabilities") loadCapabilities();', self.js,
                      "切到能力页签时必须加载（否则页面永远空白）")
        # 两个旧页签已合并进来，不该再有各自的挂载点/分发
        for gone in ('data-view="models"', 'data-view="components"',
                     'id="providersHost"', 'id="componentsHost"'):
            self.assertNotIn(gone, self.html, "%s 应已被「能力」页签合并" % gone)
        self.assertNotIn("loadProviders()", self.js)
        self.assertNotIn("loadModels()", self.js.split("function downloadMissingModels")[0])

    def test_capability_page_hosts_exist_in_the_html(self):
        """JS 里挂载点/按钮的 id 必须真在 index.html 里 —— 否则渲染静默失败（用户看到空白页签）。"""
        for host in ("capKindCards", "capFuncCards", "capEnvHost", "capOverview",
                     "capOvSummary", "btnCapReload", "btnCapDownloadMissing"):
            with self.subTest(id=host):
                self.assertIn('id="%s"' % host, self.html, "index.html 缺少 #%s" % host)
                self.assertIn('$("#%s")' % host, self.js, "app.js 没有用 #%s" % host)

    def test_capability_page_owns_the_editing_ui(self):
        """用户实测指出「同一个功能两套界面」→ 配置项 hidden、编辑搬进能力页签。"""
        self.assertIn("保存在线服务设置", self.js)
        self.assertIn("data-cap-save", self.js, "页签要有自己的保存按钮")
        self.assertIn("data-provider-kind", self.js)
        self.assertIn("data-tts-engine", self.js, "TTS 的选择就是 ttsEngine（唯一开关）")

    def test_provider_settings_are_hidden_from_the_generic_form(self):
        """这八个键**不再**出现在通用设置表单里（否则又变成两套界面）。"""
        from app.config import DEFAULTS
        for key in ("providerAsr", "providerLlm",
                    "providerLlmBaseUrl", "providerLlmApiKey", "providerLlmModel",
                    "providerAsrBaseUrl", "providerAsrApiKey", "providerAsrModel"):
            with self.subTest(key=key):
                meta = DEFAULTS.get(key)
                self.assertIsNotNone(meta, "%s 必须存在" % key)
                self.assertTrue(meta.get("hidden"),
                                "%s 应由「能力」页签承载，不出现在通用表单" % key)
                self.assertEqual(meta["grp"], "provider")
                self.assertTrue(meta["label"] and meta["description"])
        # providerTts 更进一步：与 ttsEngine 重复 → 已弃用（既不出现在表单，也不出现在页签）
        self.assertTrue(DEFAULTS["providerTts"].get("deprecated"))

    def test_provider_config_endpoint_serves_them_masked(self):
        """卡片要靠这个端点拿到值；密钥仍是遮罩过的；TTS 那格只给只读的 ttsEngine。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        app.include_router(router)
        r = TestClient(app).get("/api/providers/config")
        self.assertEqual(r.status_code, 200, r.text)
        rows = {s["key"]: s for s in r.json()["settings"]}
        for key in ("providerAsr", "providerLlm", "providerLlmBaseUrl",
                    "providerLlmApiKey", "providerAsrApiKey"):
            self.assertIn(key, rows)
        self.assertTrue(rows["providerLlmApiKey"]["secret"])
        self.assertEqual(rows["providerLlmApiKey"]["value"], "")
        self.assertIn("hasValue", rows["providerLlmApiKey"])
        self.assertNotIn("sk-", json.dumps(r.json()))
        # TTS：ttsEngine 以"只读"随卡片下发（卡片只显示当前实现，编辑仍在设置里）
        self.assertIn("ttsEngine", rows)
        self.assertTrue(rows["ttsEngine"]["read_only"])
        self.assertTrue(rows["ttsEngine"]["platform_options"],
                        "要带本平台候选项（macOS 的离线引擎是 say 而不是 sapi）")
        self.assertNotIn("providerTts", rows, "弃用项不该再出现在卡片数据里")

    def test_secret_rows_render_as_password_inputs(self):
        """密钥行必须是密码框且默认空值（配服务端的"空串=不改"那道闸）。"""
        self.assertIn('type="password"', self.js)
        self.assertIn("data-secret", self.js)
        self.assertIn("data-clear-secret", self.js, "要有显式清除入口（哨兵值那条路）")


class TtsProviderWiringTests(unittest.TestCase):
    """TTS 只有一个开关：`ttsEngine`（2026-09-19 设置收敛）。

    背景：`providerTts`（卡片上的 TTS 下拉）与 `ttsEngine`（设置里的语音合成引擎）曾是
    同一个选择的两个入口，而且会互相打架 —— 配了 providerTts 时 `ttsEngine=off` 关不掉朗读。
    现在 providerTts 已弃用，朗读统一由 `ttsEngine` 决定，`providers.speak_text()` 仍是唯一门面。
    """

    def setUp(self):
        from app import providers as P
        self.P = P

    def test_speak_text_follows_tts_engine(self):
        seen = {}

        def fake_speak(text, engine="auto", timeout=60):
            seen.update(text=text, engine=engine, timeout=timeout)
            return True

        with patch("app.config.settings.get",
                   lambda k, d=None: "edge-tts" if k == "ttsEngine" else d), \
                patch("app.audio.tts.speak", fake_speak):
            out = self.P.speak_text("你好", timeout=5)
        self.assertTrue(out)
        self.assertEqual(seen, {"text": "你好", "engine": "edge-tts", "timeout": 5})

    def test_off_means_offline_path_is_still_asked_to_stay_silent(self):
        """`ttsEngine=off` 必须真的把"关"传下去（旧实现会被 providerTts 覆盖掉）。"""
        seen = {}

        def fake_speak(text, engine="auto", timeout=60):
            seen.update(engine=engine)
            return engine != "off"

        with patch("app.config.settings.get",
                   lambda k, d=None: "off" if k == "ttsEngine" else d), \
                patch("app.audio.tts.speak", fake_speak):
            out = self.P.speak_text("你好")
        self.assertEqual(seen["engine"], "off")
        self.assertFalse(out, "off = 不朗读")

    def test_provider_tts_is_deprecated_and_not_read(self):
        from app.config import DEFAULTS, DEPRECATION_MIGRATIONS
        self.assertTrue(DEFAULTS["providerTts"].get("deprecated"),
                        "providerTts 与 ttsEngine 重复，应已弃用")
        self.assertIn("providerTts", DEPRECATION_MIGRATIONS,
                      "弃用要带值迁移：用户选过的 edge-tts / local-tts 要搬到 ttsEngine")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "providers", "__init__.py"), encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        self.assertNotIn('settings.get("providerTts"', code,
                         "providerTts 不能再参与朗读决策（只留 ttsEngine 一个开关）")

    def test_speak_failure_returns_false_not_exception(self):
        with patch("app.config.settings.get", lambda k, d=None: ""), \
                patch("app.audio.tts.speak", side_effect=RuntimeError("device gone")):
            self.assertFalse(self.P.speak_text("你好"))

    def test_callers_go_through_the_facade(self):
        """朗读的三个调用点（助手复述/简报/提示语、面板试听）都要走门面。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app", "assistant.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("tts_mod.speak", src, "助手里不该再直接调 tts.speak（绕过朗读门面）")
        self.assertIn("providers_mod.speak_text", src)
        self.assertIn("providers_mod.speak_async", src)
        with open(os.path.join(root, "app", "api.py"), encoding="utf-8") as fh:
            api_src = fh.read()
        self.assertIn("providers_mod.speak_async", api_src, "面板「语音测试」也要走门面")


if __name__ == "__main__":
    unittest.main()
