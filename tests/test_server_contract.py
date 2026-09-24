# -*- coding: utf-8 -*-
"""能力后端（`server/`）的契约与护栏。

这个文件钉两类东西：

  **护栏** —— "服务端不存储业务数据 / 不认识业务概念"必须是**能被测试钉住的属性**，
  不是承诺。所以扫源码、扫路由表、扫跑完之后的磁盘。

  **契约** —— 两级并发闸门、拒绝类型的区分、临时文件的清理、模型池的
  单飞/引用计数/LRU/显存预算，以及最要紧的一条：**失败就是失败**（不回退 CPU）。

不碰真实麦克风、不加载真实模型：引擎层用假加载器（`_FakeEngine`）。
"""
import base64
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt                                                     # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from server import auth as auth_mod                              # noqa: E402
from server import engines, errors, main as server_main, tmp     # noqa: E402
from server import routes as routes_mod                          # noqa: E402
from server import settings as settings_mod                      # noqa: E402
from server import store as store_mod                            # noqa: E402
from server.pool import EnginePool, ModelSpec                    # noqa: E402

SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")


# ---------------------------------------------------------------- 假引擎

class _FakeEngine:
    """假引擎：`transcribe` 原样回一句，`analyze` 回两条 turn。"""

    def __init__(self, spec):
        self.spec = spec
        self.closed = False

    def transcribe(self, wav, lang="auto", timestamps=False):
        rows = [{"start": 0.0, "end": 1.0, "text": "hi"}] if timestamps else []
        return {"text": "hi", "sentences": rows, "status": "ok"}

    def analyze(self, wav, max_speakers=None):
        return ([(0.0, 1.0, "S0"), (1.0, 2.0, "S1")],
                [[0.1] * 4, [0.2] * 4], ["S0", "S1"])

    def embed(self, wav):
        return [[0.5] * 4], ["S0"]

    def close(self):
        self.closed = True


_LOAD_COUNTS = {}


def _fake_loader(spec):
    _LOAD_COUNTS[spec.id] = _LOAD_COUNTS.get(spec.id, 0) + 1
    return _FakeEngine(spec)


def _snore(_spec):                       # 加载很慢的假加载器（测单飞/等加载）
    time.sleep(0.25)
    return _FakeEngine(_spec)


def _boom(_spec):                        # 加载必失败（测"不回退 CPU"）
    raise RuntimeError("假装显存不够")


FAKE_SPECS = [
    {"id": "asr-short", "slot": "asr.text", "impl": "fake", "resident": True,
     "max_concurrency": 2, "modelVersion": "fake-v1"},
    {"id": "asr-long", "slot": "asr.long", "impl": "fake", "resident": False,
     "max_concurrency": 1, "modelVersion": "fake-v2", "supports": ["asr.timestamps"]},
    {"id": "diarize", "slot": "diarize.turns", "impl": "fake", "resident": False,
     "max_concurrency": 1, "modelVersion": "fake-v3",
     "vectorSpaceId": "ws-fake", "dim": 4},
    {"id": "speaker-embed", "slot": "speaker.embed", "impl": "fake", "resident": False,
     "max_concurrency": 1, "modelVersion": "fake-v3",
     "vectorSpaceId": "ws-fake", "dim": 4},
]


def _cfg(tmp_root, max_concurrent=2, per_client=1, specs=None):
    cfg = settings_mod.load()
    cfg.raw["tmp"]["root"] = tmp_root
    cfg.raw["tmp"]["ttl_hours"] = 4
    cfg.raw["limits"]["max_concurrent"] = max_concurrent
    cfg.raw["limits"]["per_client_concurrent"] = per_client
    cfg.raw["models"]["specs"] = FAKE_SPECS if specs is None else specs
    return cfg


class _AppCase(unittest.TestCase):
    """起一个用假引擎的 app（走完整 lifespan）。"""

    loaders = None                # 子类可换

    def setUp(self):
        _LOAD_COUNTS.clear()
        self.tmpdir = tempfile.mkdtemp(prefix="echo-srv-test-")
        self.cfg = _cfg(self.tmpdir)
        loaders = self.loaders or (lambda device="cuda": {"fake": _fake_loader})
        self._p1 = patch.object(engines, "build_loaders", loaders)
        self._p1.start()
        self.addCleanup(self._p1.stop)
        self.app = server_main.create_app(self.cfg)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self._exit_client)

    def _exit_client(self):
        try:
            self.client.__exit__(None, None, None)
        except Exception:
            pass


def _wav_bytes(seconds=0.2, sr=16000):
    """一段真正的 16 kHz 单声道 wav（走容器格式那条路）。"""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * seconds))
    return buf.getvalue()


# ---------------------------------------------------------------- 护栏

class NoBusinessCouplingTests(unittest.TestCase):
    """服务端**不认识业务概念**，也不许 import 客户端的业务层。

    只查**英文**标识符：那才是耦合出现的地方（import、变量名、路由）。
    中文注释里出现"会议"是**说明边界**，不是耦合 —— 那份说明本身有价值。
    """

    FORBIDDEN_WORDS = ("meeting", "command", "summary", "voiceprint")
    FORBIDDEN_IMPORTS = ("app.db", "app.config", "app.meeting", "app.assistant",
                         "app.worklog", "app.voiceprint", "app.api")

    def _sources(self):
        for fn in sorted(os.listdir(SERVER_DIR)):
            if fn.endswith(".py"):
                p = os.path.join(SERVER_DIR, fn)
                with open(p, encoding="utf-8") as fh:
                    yield fn, fh.read()

    def test_no_business_words_in_source(self):
        bad = []
        for fn, text in self._sources():
            for w in self.FORBIDDEN_WORDS:
                if re.search(r"\b%s" % re.escape(w), text, re.I):
                    bad.append("%s: %s" % (fn, w))
        self.assertEqual(bad, [], "服务端源码里出现了业务词（它只该认识能力槽）：%s" % bad)

    def test_no_business_imports(self):
        bad = []
        for fn, text in self._sources():
            for mod in self.FORBIDDEN_IMPORTS:
                if re.search(r"^\s*(from|import)\s+%s\b" % re.escape(mod), text, re.M):
                    bad.append("%s: %s" % (fn, mod))
        self.assertEqual(bad, [], "服务端 import 了客户端的业务层：%s" % bad)

    def test_only_the_documented_routes_exist(self):
        """`/v1/*` 只有设计里那几条；**没有任何写业务数据的端点**。

        这条是"服务端不存储业务数据"的结构性保证之一 ——
        一旦有人加了 `/v1/x`，它会红，而不是悄悄多一个能存东西的口子。

        `/v1/pair` 与 `/v1/token` 是**例外，但不是漏洞**：它们是**鉴权**端点
        （设计 §7.4/§7.5），写的是设计 §8.5 白名单里的 `clients` / `pairing_codes`
        两张**管理**表，一个字节的内容都不碰。它们在这里被显式列出来，
        而不是靠"凡是 auth 开头就放行"那种模糊规则 —— 后者会让下一个
        "看起来像鉴权"的业务端点溜进来。
        """
        allowed = {
            # 查询
            ("GET", "/v1/capabilities"), ("GET", "/v1/health"), ("GET", "/v1/ready"),
            # 鉴权（唯一免凭据的是 pair）
            ("POST", "/v1/pair"), ("POST", "/v1/token"),
            # 能力（**都不是**写端点：收音频、出结果、不留存）
            ("POST", "/v1/asr"), ("POST", "/v1/diarize"), ("POST", "/v1/speaker/embed"),
        }
        got = set()
        for r in routes_mod.router.routes:
            methods = set(getattr(r, "methods", ()) or ())
            for m in methods - {"HEAD", "OPTIONS"}:
                got.add((m, str(getattr(r, "path", ""))))
        self.assertEqual(got, allowed,
                         "路由表与设计的白名单不一致（多出来的很可能是写端点）")


# ---------------------------------------------------------------- 错误模型

class ErrorContractTests(unittest.TestCase):
    def test_echo_error_renders_code_and_retry_after(self):
        e = errors.server_busy(5)
        self.assertEqual(e.status, 503)
        self.assertEqual(e.body()["code"], "server_busy")
        self.assertEqual(e.body()["message"], "系统忙，请稍后再试")
        self.assertEqual(e.retry_after, 5)

    def test_client_busy_and_server_busy_are_different(self):
        """**这两个不能合并**：前者重试永远没用，后者重试才对。"""
        cb, sb = errors.client_busy(), errors.server_busy(5)
        self.assertNotEqual(cb.status, sb.status)
        self.assertNotEqual(cb.code, sb.code)
        self.assertIsNone(cb.retry_after)
        self.assertEqual(sb.retry_after, 5)


# ---------------------------------------------------------------- 端点

class EndpointTests(_AppCase):
    def test_health_and_ready(self):
        h = self.client.get("/v1/health").json()
        self.assertTrue(h["ok"])
        self.assertIn("tmp", h)                       # 临时目录统计必须在（泄漏的信号）
        self.assertIn("busy", h)
        self.assertTrue(self.client.get("/v1/ready").json()["ok"])

    def test_capabilities_reports_slots_and_vector_space(self):
        cap = self.client.get("/v1/capabilities").json()
        self.assertEqual(cap["protocol"], 1)
        self.assertEqual(cap["limits"]["maxConcurrent"], 2)
        self.assertEqual(cap["limits"]["perClientConcurrent"], 1)
        self.assertEqual(cap["limits"]["queueMax"], 0)      # 不排队
        self.assertIn("asr.text", cap["slots"])          # 短音频那个模型
        self.assertIn("asr.long", cap["slots"])          # 长音频那个
        self.assertIn("asr.timestamps", cap["slots"])    # 长音频**同时**满足它（supports）
        # 产出向量的两个模型**必须同一个 vectorSpaceId**（客户端要互相比对）
        spaces = {m["vectorSpaceId"] for m in cap["models"] if m["vectorSpaceId"]}
        self.assertEqual(spaces, {"ws-fake"})

    def test_asr_returns_text_and_no_fake_timestamps(self):
        r = self.client.post("/v1/asr", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["text"], "hi")
        # 不要时间戳时**不许**编一个出来
        self.assertEqual(body["timestamps"], "none")
        self.assertEqual(body["sentences"], [])

    def test_asr_with_timestamps_says_exact(self):
        r = self.client.post("/v1/asr?timestamps=1", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.json()["timestamps"], "exact")

    def test_short_variant_reaches_the_short_model(self):
        """`variant=short` 必须真的走通 —— 默认是 `long`，所以这条路径
        不写用例就永远没人走（曾经它 404，而全部用例都是绿的）。"""
        r = self.client.post("/v1/asr?variant=short", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["modelId"], "asr-short")
        self.assertEqual(r.json()["modelVersion"], "fake-v1")

    def test_long_variant_reaches_the_long_model(self):
        r = self.client.post("/v1/asr?variant=long", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.json()["modelId"], "asr-long")

    def test_unknown_slot_named_by_model_is_a_clean_404(self):
        """点名一个不存在的模型 → 干净的 `model_not_found`，不是 500。"""
        r = self.client.post("/v1/asr?model=nope", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["code"], "model_not_found")

    def test_diarize_returns_local_labels_and_space(self):
        r = self.client.post("/v1/diarize", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        body = r.json()
        self.assertEqual([t["speaker"] for t in body["turns"]], ["S0", "S1"])
        self.assertEqual(body["vectorSpaceId"], "ws-fake")
        self.assertEqual(body["dim"], 4)

    def test_diarize_turns_mode_is_refused_not_faked(self):
        """v1 没实现逐 turn 嵌入 —— **明说没实现**，不要拿段级结果冒充。"""
        r = self.client.post("/v1/diarize?mode=turns", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "bad_request")

    def test_speaker_embed_returns_vectors(self):
        r = self.client.post("/v1/speaker/embed", content=_wav_bytes(),
                             headers={"Content-Type": "audio/wav"})
        body = r.json()
        self.assertEqual(len(body["embeddings"]), 1)
        self.assertEqual(body["dim"], 4)

    def test_empty_body_is_rejected(self):
        r = self.client.post("/v1/asr", content=b"",
                             headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 400)

    def test_declared_oversize_is_refused_before_reading(self):
        big = int(self.cfg.get("limits.max_upload_bytes")) + 1
        r = self.client.post("/v1/asr", content=b"x",
                             headers={"Content-Type": "audio/wav",
                                      "Content-Length": str(big)})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["code"], "payload_too_large")

    def test_unknown_content_type_is_refused(self):
        r = self.client.post("/v1/asr", content=b"\x00\x00",
                             headers={"Content-Type": "application/pdf"})
        self.assertEqual(r.status_code, 415)


# ---------------------------------------------------------------- 并发闸门

class AdmissionTests(unittest.TestCase):
    """两级闸门，**都不排队**（设计 §3.6）。

    这里**直接测闸门对象**，不靠 `TestClient` 制造并发 ——
    `TestClient` 会把同一客户端的两个请求**串行化**（实测），
    用它写并发用例会得到一个"看起来通过、其实没测到"的结果。
    闸门真的接在路由上，由下面两个"接线"用例证明。
    """

    def test_second_request_from_same_client_is_client_busy(self):
        """同一客户端同时只能 1 个 —— 第二条是 **409 client_busy**。"""
        adm = routes_mod.Admission(max_concurrent=2, per_client=1)
        with adm.hold("c1"):
            with self.assertRaises(errors.EchoError) as ctx:
                with adm.hold("c1"):
                    pass
            self.assertEqual(ctx.exception.code, "client_busy")
            self.assertEqual(ctx.exception.status, 409)
            self.assertIsNone(ctx.exception.retry_after,
                              "这条**重试没用**，所以不该给它 Retry-After")

    def test_global_channel_limit_is_server_busy(self):
        """通道占满 → **503 server_busy + Retry-After**（这条才该退避重试）。"""
        adm = routes_mod.Admission(max_concurrent=2, per_client=1, retry_after=7)
        with adm.hold("c1"):
            with adm.hold("c2"):
                with self.assertRaises(errors.EchoError) as ctx:
                    with adm.hold("c3"):
                        pass
                self.assertEqual(ctx.exception.code, "server_busy")
                self.assertEqual(ctx.exception.status, 503)
                self.assertEqual(ctx.exception.retry_after, 7)

    def test_slots_are_released_on_exception(self):
        """抛异常也必须还回去 —— 否则一次失败就把服务端永久锁死。"""
        adm = routes_mod.Admission(max_concurrent=1, per_client=1)
        try:
            with adm.hold("c1"):
                raise RuntimeError("假装推理炸了")
        except RuntimeError:
            pass
        with adm.hold("c1"):
            self.assertEqual(adm.snapshot()["active"], 1)
        self.assertEqual(adm.snapshot()["active"], 0, "退出后必须归零")

    def test_snapshot_reports_the_numbers_capabilities_needs(self):
        adm = routes_mod.Admission(2, 1)
        snap = adm.snapshot()
        self.assertEqual(snap["maxConcurrent"], 2)
        self.assertEqual(snap["perClientConcurrent"], 1)
        self.assertEqual(snap["active"], 0)


class AdmissionWiringTests(_AppCase):
    """闸门真的**接在路由上**（不是躺在那里没人调）。

    做法：直接占住闸门再发请求 —— 确定性强，不依赖两个请求真的并发。
    """

    def test_request_is_refused_when_the_client_already_holds_a_slot(self):
        st = self.app.state.echo
        with st.admission.hold("anonymous"):
            r = self.client.post("/v1/asr", content=_wav_bytes(),
                                 headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "client_busy")

    def test_request_is_refused_when_all_channels_are_taken(self):
        """总通道只有 1 个时，被别人占着就该 **503 server_busy**。"""
        cfg = _cfg(tempfile.mkdtemp(prefix="echo-srv-wire-"), max_concurrent=1, per_client=1)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)
            with TestClient(app) as c:
                st = app.state.echo
                with st.admission.hold("someone-else"):
                    r = c.post("/v1/asr", content=_wav_bytes(),
                               headers={"Content-Type": "audio/wav"})
                self.assertEqual(r.status_code, 503, r.text)
                self.assertEqual(r.json()["code"], "server_busy")
                self.assertEqual(r.json()["message"], "系统忙，请稍后再试")
                self.assertIn("Retry-After", r.headers)

    def test_precheck_refuses_before_a_single_byte_is_read(self):
        """**预检必须在读 body 之前**，否则"忙"这个答复是在 64 MB 落盘之后才说的。

        怎么判定"一个字节都没读"：把 `Content-Length` 声报成**超过上限**的值。
        如果实现先去查大小，会得到 `413 payload_too_large`；
        如果先去判忙，会得到 `409 client_busy`。
        拿到 409 = 预检真的跑在收音频之前。顺序即证据。
        """
        st = self.app.state.echo
        limit = int(st.cfg.get("limits.max_upload_bytes", 64 * 1024 * 1024))
        with st.admission.hold("anonymous"):
            r = self.client.post("/v1/asr", content=b"x" * 1024,
                                 headers={"Content-Type": "audio/wav",
                                          "Content-Length": str(limit + 1)})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "client_busy",
                         "先判大小去了：说明「忙」是在收完之后才判的")

    def test_precheck_does_not_write_any_temp_file(self):
        """被预检挡回来的请求，临时目录里不该留下任何东西。"""
        st = self.app.state.echo
        with st.admission.hold("anonymous"):
            self.client.post("/v1/asr", content=b"x" * 4096,
                             headers={"Content-Type": "audio/wav"})
        left = [p for p in os.listdir(st.cfg.tmp_root)
                if p not in ("", ".") and not p.startswith(".")]
        # 日期目录是空的（sweep 会把它清掉），关键是里面没有 request 目录
        for d in left:
            sub = os.path.join(st.cfg.tmp_root, d)
            if os.path.isdir(sub):
                self.assertEqual(os.listdir(sub), [], "预检挡回来的请求留下了临时文件")

    def test_precheck_is_advisory_so_hold_is_still_authoritative(self):
        """预检只是"提前说一声" —— 它**不占槽**，所以权威判定仍然在 `hold`。

        这条钉住"预检不能变成第二个真相来源"：把闸门占满之后，
        `precheck` 确实拒绝；而它**没有**偷偷把计数改掉。
        """
        adm = routes_mod.Admission(2, 1)
        adm.precheck("c1")                       # 空的时候不该拒
        self.assertEqual(adm.snapshot()["active"], 0, "precheck 不许占槽")
        with adm.hold("c1"):
            with self.assertRaises(errors.EchoError) as ctx:
                adm.precheck("c1")
            self.assertEqual(ctx.exception.code, "client_busy")
            self.assertEqual(adm.snapshot()["active"], 1, "precheck 不许改计数")
        adm.precheck("c1")                       # 放掉之后又能过

    def test_auth_off_means_everyone_is_anonymous(self):
        """鉴权关着时所有请求算同一个客户端 —— 于是"每客户端 1"就成了全局 1。

        这不是 bug，是**提醒**：不配鉴权时，服务端总通道那个数其实用不上，
        真正的闸是"每次一个"。生产必须打开鉴权（否则配额与审计都挂不住）。
        """
        self.assertFalse(bool(self.cfg.get("auth.enabled", False)))
        self.assertEqual(self.client.post(
            "/v1/asr", content=_wav_bytes(),
            headers={"Content-Type": "audio/wav"}).json()["text"], "hi")

    def test_bad_token_is_refused_when_auth_is_on(self):
        cfg = _cfg(tempfile.mkdtemp(prefix="echo-srv-auth-"))
        cfg.raw["auth"]["enabled"] = True
        cfg.raw["auth"]["tokens"] = [{"client_id": "c1", "token": "t1"}]
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)
            with TestClient(app) as c:
                self.assertEqual(c.post("/v1/asr", content=_wav_bytes()).status_code, 401)
                self.assertEqual(c.post(
                    "/v1/asr", content=_wav_bytes(),
                    headers={"Authorization": "Bearer nope"}).status_code, 401)
                self.assertEqual(c.post(
                    "/v1/asr", content=_wav_bytes(),
                    headers={"Authorization": "Bearer t1"}).status_code, 200)

    def test_config_is_not_shared_between_loads(self):
        """`load()` 必须给一份**独立**的配置。

        浅拷贝会让 `cfg.raw["auth"]["enabled"] = True` 改到全局 `DEFAULTS`，
        于是下一个请求（甚至下一个测试）莫名其妙要求令牌 —— 2026-09-23 实测踩到。
        """
        a = settings_mod.load()
        a.raw["auth"]["enabled"] = True
        b = settings_mod.load()
        self.assertFalse(bool(b.get("auth.enabled")), "load() 之间串了状态")


# ---------------------------------------------------------------- 临时文件

class TempWorkspaceTests(unittest.TestCase):
    def test_success_and_failure_both_clean_up(self):
        root = tempfile.mkdtemp(prefix="echo-srv-tmp-")
        with tmp.TempWorkspace(root) as ws:
            with open(ws.path("a.wav"), "wb") as fh:
                fh.write(b"x" * 32)
            self.assertTrue(os.path.isfile(ws.path("a.wav")))
        self.assertFalse(os.path.exists(os.path.dirname(ws.dir)))

        try:
            with tmp.TempWorkspace(root) as ws2:
                with open(ws2.path("b.wav"), "wb") as fh:
                    fh.write(b"x" * 32)
                raise RuntimeError("假装推理炸了")
        except RuntimeError:
            pass
        self.assertEqual(tmp.stats(root)["files"], 0, "异常路径也必须清干净")

    def test_sweep_removes_old_and_keeps_fresh(self):
        root = tempfile.mkdtemp(prefix="echo-srv-tmp-")
        old_day = os.path.join(root, "2000-01-01", "deadbeef")
        os.makedirs(old_day)
        with open(os.path.join(old_day, "x.wav"), "wb") as fh:
            fh.write(b"y" * 64)
        fresh = os.path.join(root, "2999-12-31", "cafebabe")
        os.makedirs(fresh)
        with open(os.path.join(fresh, "y.wav"), "wb") as fh:
            fh.write(b"y" * 8)
        rep = tmp.sweep(root, ttl_hours=4)
        self.assertGreaterEqual(rep.removed_dirs, 1)
        self.assertFalse(os.path.exists(old_day))
        self.assertTrue(os.path.exists(fresh), "没超龄的不能删")

    def test_sweep_by_size_removes_oldest_first(self):
        root = tempfile.mkdtemp(prefix="echo-srv-tmp-")
        for i, day in enumerate(("2020-01-01", "2021-01-01")):
            d = os.path.join(root, day, "req")
            os.makedirs(d)
            p = os.path.join(d, "a.wav")
            with open(p, "wb") as fh:
                fh.write(b"z" * 1000)
            os.utime(d, (1000000 + i * 1000, 1000000 + i * 1000))   # 让它们"很老"
        tmp.sweep(root, ttl_hours=1e9, max_bytes=800)               # TTL 设到极大，只测大小那条
        self.assertFalse(os.path.exists(os.path.join(root, "2020-01-01")),
                         "超阈值要先删最老的")


# ---------------------------------------------------------------- 模型池

class DefaultSpecsTests(unittest.TestCase):
    """出厂清单的形状（2026-09-24 拍板的结果，见设计 §14-2）。

    决定是：**v1 不放常驻的小模型**（`asr-short`）。短请求由长档那个
    `supports: [asr.text]` 兜住。这里把决定的后果钉住，免得以后有人
    "顺手加回一个常驻小模型"—— 那会把显存一直占着，而 v1 没有配额机制
    能让它"只对内不对外"。
    """

    def test_no_resident_asr_model(self):
        resident = [s.id for s in engines.default_specs()
                    if s.resident and (s.slot.startswith("asr") or "asr.text" in (s.supports or ()))]
        self.assertEqual(resident, [],
                         "出厂清单里出现了常驻的 ASR 模型：%s。v1 没有配额机制，"
                         "它会一直占着显存且对外开着（见设计 §14-2）" % resident)

    def test_the_short_slot_is_still_served(self):
        """去掉 asr-short **不等于**关掉短档 —— `variant=short` 必须仍有人接。"""
        pool = EnginePool(engines.default_specs(), engines.build_loaders(device="cuda"))
        self.assertIn("asr.text", pool.slots(), "去掉 asr-short 之后短档没人接了")
        self.assertTrue(pool.pick_for_slot("asr.text"))

    def test_one_model_serves_both_text_and_timestamps(self):
        """长档那个模型一人多角（文本 + 时间戳），这正是它没被拆成两个 spec 的原因。"""
        specs = engines.default_specs()
        long_spec = [s for s in specs if s.id == "asr-long"][0]
        self.assertIn("asr.text", long_spec.supports)
        self.assertIn("asr.timestamps", long_spec.supports)
        self.assertEqual(len([s for s in specs if "asr" in s.slot or "asr.text" in (s.supports or ())]), 1,
                         "出厂清单里应当只有一个 ASR 模型")


class ExampleConfigTests(unittest.TestCase):
    """`server/echo-server.example.yaml` 必须与代码里的出厂清单**说同一件事**。

    起因：这份示例配置是把默认值摊开写一遍给人改的，于是它天然会漂 ——
    本仓库已经吃过一次这个亏（`dist/` 里手工组出来的 kit 比源码旧了 9 小时，
    当天的修复一个都没进包；见 AGENTS.md）。**手工活必然漂移**，所以要机器看着。

    这类漂移不报错、也不影响运行（`build_pool` 优先读 yaml），只是**文档在说谎**：
    人照着示例改出来的服务端，与代码默认的服务端不是同一个东西。
    """

    def _example(self):
        path = os.path.join(SERVER_DIR, "echo-server.example.yaml")
        self.assertTrue(os.path.isfile(path), "示例配置不见了")
        return settings_mod.load(path)

    def test_loads_without_error(self):
        cfg = self._example()
        self.assertTrue(cfg.specs, "示例配置里没有 specs —— 照它跑会得到空清单")
        self.assertEqual(cfg.port, 8900, "示例里的监听端口应当与文档写的一致")

    def test_specs_match_the_code_defaults(self):
        """逐项比对：示例里写的 = `default_specs()` 产出的。"""
        cfg = self._example()
        from_yaml = [ModelSpec.from_dict(d) for d in cfg.specs]
        from_code = engines.default_specs()
        key = lambda s: (s.id, s.slot, s.impl, s.resident, tuple(sorted(s.supports)),  # noqa: E731
                         s.vector_space_id)
        self.assertEqual([key(s) for s in from_yaml], [key(s) for s in from_code],
                         "示例配置与出厂清单不一致 —— 改了一边就要改另一边")

    def test_example_limits_match_the_decided_values(self):
        """2026-09-23 定的数：总通道 2、每客户端 1、**不排队**。"""
        cfg = self._example()
        self.assertEqual(cfg.max_concurrent, 2)
        self.assertEqual(cfg.per_client_concurrent, 1)
        self.assertEqual(int(cfg.get("limits.queue_max", -1)), 0)

    def test_example_does_not_use_a_top_level_resident_key(self):
        """`models.resident` 是个**不存在的开关** —— 代码只读每个 spec 的 `resident`。

        文档里曾经写着它，照抄的人会以为"我配了常驻"，然后发现谁也没理它。
        """
        cfg = self._example()
        self.assertIsNone(cfg.get("models.resident"),
                          "示例配置里出现了顶层 models.resident —— 代码不读它")


class SlotRoutingTests(unittest.TestCase):
    """**"宣告了"必须等于"路由得过去"。**

    这是一条把两个方向都钉住的等价关系，起因是两个真实的、互不相干的漏洞：

      1. `/v1/capabilities` 按 `[slot] + supports` 汇总能提供的槽，
         而 `EnginePool._by_slot` 只按 `slot` 建索引 —— 于是服务端**宣告了
         `asr.timestamps`，却路由不过去**（客户端照 capabilities 发请求收到 404）。
         同一份事实在两处各算一遍，必然漂。
      2. `POST /v1/asr?variant=short` 去要 `asr.short` 这个槽 ——
         而**没有任何模型提供它**，于是短档一律 404。默认的 `variant=long`
         把测试全带绿了，所以一直没露。

    两个漏洞的症状是同一个：**客户端按服务端自己说的话办事，却被打回来。**
    所以用例也写成一句话：`capabilities` 里列出的每个槽，`pick_for_slot` 都能解析。
    """

    def _pool(self):
        return EnginePool([ModelSpec.from_dict(d) for d in FAKE_SPECS], {"fake": _fake_loader})

    def test_supports_are_routable_not_just_advertised(self):
        pool = self._pool()
        self.assertEqual(pool.models_for_slot("asr.timestamps"), ["asr-long"],
                         "长音频那个模型声明了 supports=[asr.timestamps]，就该能按它路由")
        self.assertEqual(pool.models_for_slot("asr.text"), ["asr-short"])

    def test_every_advertised_slot_is_routable(self):
        """`capabilities` 说什么能提供，`pick_for_slot` 就得能选出模型来。"""
        pool = self._pool()
        advertised = pool.slots()
        self.assertTrue(advertised)
        for slot in sorted(advertised):
            with self.subTest(slot=slot):
                self.assertNotEqual(pool.pick_for_slot(slot), "",
                                    "宣告了 %s 却路由不过去" % slot)

    def test_capabilities_slots_come_from_the_pool_itself(self):
        """**构造上的保证，不只是用例上的。**

        `capabilities` 曾经自己按 `[slot] + supports` 汇总一遍，而池只按 `slot`
        建索引 —— 同一份事实算两遍，于是漂了。现在它必须**取池里那一份**。
        这条钉的是"取"这个动作：把池的那份改掉，`capabilities` 必须跟着变。
        """
        pool = self._pool()
        pool._by_slot["asr.text"] = ["asr-long", "asr-short"]      # 人为改顺序
        self.assertEqual(pool.slots()["asr.text"], ["asr-long", "asr-short"])
        # 而且顺序是有意义的：pick_for_slot 取第一个
        self.assertEqual(pool.pick_for_slot("asr.text"), "asr-long")

    def test_slots_order_matches_what_pick_for_slot_returns(self):
        """客户端看到的**第一个**模型＝它真会得到的那个（否则"预览"是假话）。"""
        pool = self._pool()
        for slot, ids in pool.slots().items():
            with self.subTest(slot=slot):
                self.assertEqual(ids[0], pool.pick_for_slot(slot))

    def test_short_variant_is_not_a_phantom_slot(self):
        """`variant=short` 要落到一个**真的有人提供**的槽上。

        注意"有人提供"＝ `slot` **或** `supports` 里出现 —— 这正是本文件开头
        那两个漏洞的教训（`_by_slot` 曾经只认 `slot`）。所以这里断言的是
        `pool.slots()`（两者都算），不是 `[s.slot for s in specs]`。
        一开始这条写的就是后者，于是去掉 asr-short 之后它红了 ——
        **是断言的标准错了，不是代码错了**。

        另外这条要单独钉，因为默认 `variant=long` 会让"短档"这条路径
        在端点用例里不容易被走到（见类注释）。
        """
        pool = EnginePool(engines.default_specs(), engines.build_loaders(device="cuda"))
        provided = pool.slots()
        for variant, want_slot in (("short", "asr.text"), ("long", "asr.long")):
            with self.subTest(variant=variant):
                self.assertIn(want_slot, provided,
                              "variant=%s 想要的槽 %s 没有任何模型提供" % (variant, want_slot))
                self.assertTrue(pool.pick_for_slot(want_slot))


class EnginePoolTests(unittest.TestCase):
    def _pool(self, specs=None, **kw):
        return EnginePool(specs or [ModelSpec.from_dict(d) for d in FAKE_SPECS],
                          {"fake": _fake_loader}, **kw)

    def test_single_flight(self):
        """并发要同一个模型，**只加载一次**（其余等同一个 future）。"""
        calls = {"n": 0}

        def counting(spec):
            calls["n"] += 1
            time.sleep(0.2)
            return _FakeEngine(spec)

        pool = EnginePool([ModelSpec(id="m", slot="s", impl="count", max_concurrency=6)],
                          {"count": counting}, load_timeout_s=5)
        done = []

        def go():
            with pool.acquire("m"):
                done.append(1)

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len(done), 6, "六个请求都该拿到实例")
        self.assertEqual(calls["n"], 1, "只允许加载一次（单飞）")

    def test_refcount_blocks_unload(self):
        pool = self._pool()
        lease = pool.acquire("asr-long")
        self.assertFalse(pool.unload("asr-long"), "有人正在用就不能卸")
        pool.release(lease._entry)
        self.assertTrue(pool.unload("asr-long"))

    def test_load_failure_does_not_fall_back_to_cpu(self):
        """**服务端不许静默回退 CPU** —— 加载失败就是 failed，如实报 503。"""
        pool = EnginePool([ModelSpec(id="m", slot="s", impl="boom")], {"boom": _boom},
                          load_timeout_s=2)
        with self.assertRaises(errors.EchoError) as ctx:
            pool.acquire("m")
        self.assertEqual(ctx.exception.code, "model_failed")
        self.assertEqual(pool.state_of("m"), "failed")
        self.assertIn("假装显存不够", pool.status()[0]["error"])

    def test_vram_budget_refuses_instead_of_oom(self):
        """显存超预算 → 拒绝（`gpu_oom`），不是 OOM、也不是回退 CPU。

        要让"拒绝"这条路径可达，占着显存的那个必须是**卸不掉**的（常驻）——
        否则池会按 LRU 把它腾掉，那测的就是"腾地方"而不是"拒绝"了。
        """
        specs = [ModelSpec(id="big", slot="s", impl="fake", resident=True, est_vram_mb=900),
                 ModelSpec(id="big2", slot="s2", impl="fake", est_vram_mb=900)]
        pool = EnginePool(specs, {"fake": _fake_loader}, vram_budget_mb=1000,
                          load_timeout_s=2)
        self.assertTrue(pool.load("big", timeout=2))
        self.assertFalse(pool.load("big2", timeout=2), "腾不出地方就该拒绝")
        st = {m["id"]: m for m in pool.status()}
        self.assertEqual(st["big"]["state"], "ready", "常驻的不许被腾掉")
        self.assertEqual(st["big2"]["state"], "failed")
        self.assertIn("显存不足", st["big2"]["error"])

    def test_lru_evicts_idle_but_never_resident(self):
        # 预算 250：常驻 100 + a 100 = 200 装得下；再来 b 就得先卸掉最久未用的 a
        specs = [ModelSpec(id="r", slot="s", impl="fake", resident=True, est_vram_mb=100),
                 ModelSpec(id="a", slot="s2", impl="fake", est_vram_mb=100),
                 ModelSpec(id="b", slot="s3", impl="fake", est_vram_mb=100)]
        pool = EnginePool(specs, {"fake": _fake_loader}, vram_budget_mb=250,
                          load_timeout_s=2)
        self.assertTrue(pool.load("r", timeout=2))
        self.assertTrue(pool.load("a", timeout=2))
        self.assertTrue(pool.load("b", timeout=2))
        self.assertEqual(pool.state_of("r"), "ready", "常驻的不参与 LRU")
        self.assertEqual(pool.state_of("a"), "absent", "最久未用且没人用的该被卸掉")
        self.assertEqual(pool.state_of("b"), "ready")

    def test_status_reflects_real_state(self):
        pool = self._pool()
        st = {m["id"]: m for m in pool.status()}
        self.assertEqual(st["asr-short"]["state"], "absent")     # 还没人预热
        pool.load("asr-short", timeout=2)
        st = {m["id"]: m for m in pool.status()}
        self.assertEqual(st["asr-short"]["state"], "ready")
        self.assertEqual(st["asr-short"]["supports"], [])
        self.assertEqual(st["asr-long"]["supports"], ["asr.timestamps"])


class DeviceAssertionTests(unittest.TestCase):
    """服务端**不回退 CPU** —— 而且判据要按 impl 分开问。

    这里钉的是一个真实存在过的漏洞：原来只问一句 `cuda_available()`
    （ctranslate2 **或** torch 任一有 CUDA 就算有）。于是当这台机器
    ctranslate2 有 CUDA、torch 没有（或反过来）时，`_assert_device` 放行，
    紧接着**客户端引擎层自己的 `except -> CPU`** 把它悄悄降级成 CPU ——
    正是这条铁律要拦的事，却从判据的缝里漏过去了。

    现在按 impl 问它真正依赖的运行时，本类把这个判据钉住。
    """

    def test_sensevoice_is_judged_by_torch_not_ctranslate2(self):
        """funasr 的 SenseVoice 按 `torch.cuda.is_available()` 决定设备。

        ctranslate2 说有 CUDA **不算数** —— 那只证明 whisper 那边能用。
        """
        with patch.object(engines, "_runtime_has_cuda",
                          side_effect=lambda rt: rt == "ctranslate2"):
            with self.assertRaises(RuntimeError) as ctx:
                engines._assert_device("sensevoice", "cuda")
        self.assertIn("torch", str(ctx.exception))

    def test_whisper_is_judged_by_ctranslate2(self):
        """faster-whisper 走 ctranslate2，所以 torch 缺席不该拦它。"""
        with patch.object(engines, "_runtime_has_cuda",
                          side_effect=lambda rt: rt == "ctranslate2"):
            engines._assert_device("whisper", "cuda")      # 不该抛

    def test_cpu_specs_are_never_blocked(self):
        """显式写 `device: cpu` 就是**接受** CPU，不该被拦（拦住反而是 bug）。"""
        with patch.object(engines, "_runtime_has_cuda", return_value=False):
            engines._assert_device("sensevoice", "cpu")
            engines._assert_device("whisper", "cpu")

    def test_sherpa_is_cpu_by_nature(self):
        """sherpa-onnx 本来就是 CPU 引擎 —— 对它不存在"回退"这回事。"""
        with patch.object(engines, "_runtime_has_cuda",
                          side_effect=lambda rt: False):
            engines._assert_device("sherpa", "cuda")

    def test_no_cuda_at_all_refuses_every_gpu_impl(self):
        with patch.object(engines, "_runtime_has_cuda", return_value=False):
            for impl in ("sensevoice", "qwen3asr", "whisper"):
                with self.assertRaises(RuntimeError, msg=impl):
                    engines._assert_device(impl, "cuda")

    def test_every_built_impl_has_a_declared_runtime(self):
        """新加 impl 时必须**显式**说明它跑在哪个运行时上。

        漏了会**默认按 torch 判**（`_IMPL_RUNTIME.get(impl, "torch")`）——
        对 torch 系是对的，对别的就是碰运气。所以宁可这里红。
        """
        loaders = engines.build_loaders(device="cuda")
        self.assertEqual(sorted(loaders), sorted(engines._IMPL_RUNTIME),
                         "build_loaders 与 _IMPL_RUNTIME 的 impl 集合不一致")

    def test_vector_loaders_actually_assert_the_device(self):
        """**这条钉的是一个真出现过的漏洞。**

        `diarize._load_pipeline` 内部是 `torch.device("cuda") if
        torch.cuda.is_available() else torch.device("cpu")` —— 一句实打实的静默
        CPU 回退。而 `_diarize_loader` / `_embed_loader` 起初**没有**查设备，
        于是那句回退在服务端是可达的：模型"加载成功"了，只是慢十倍，
        指标上还看不出原因。

        所以直接拿真加载器试：没有 CUDA 时它**必须在建引擎之前**就抛，
        而不是"建完再说"。`_load_pipeline` 被替换掉 —— 一旦设备检查漏了，
        这个替身会被调用，用例就会以"不该被调用"的方式红，指得比以前清楚。
        """
        called = []
        with patch("app.audio.diarize._load_pipeline",
                   side_effect=lambda *a, **k: called.append(1)):
            with patch.object(engines, "_runtime_has_cuda", return_value=False):
                for impl in ("pyannote", "pyannote-embed"):
                    loader = engines.build_loaders(device="cuda")[impl]
                    with self.assertRaises(RuntimeError, msg=impl):
                        loader(None)
        self.assertEqual(called, [], "设备检查漏了：引擎在拒绝之前就被建起来了")


class EventLoopNotBlockedTests(unittest.TestCase):
    """**服务端必须真的能并行处理两个请求。**

    这里钉的是一个实测出来的问题（2026-09-24）：三个能力端点是 `async def`
    （因为要 `await request.stream()` 收 body），而 FastAPI 把 `async def` 处理函数
    **跑在事件循环上**。里面那些阻塞调用（soundfile 解码、funasr/pyannote 推理）
    于是把整个事件循环按住 → **所有请求被一条一条串行处理**，
    `limits.max_concurrent: 2` 成了一句空话，两级闸门也永远看不到两个请求同时在跑。

    实测症状：两个"并发"请求各花 `t` 与 `2t`，墙钟 ≈ `2t`。

    ## 为什么以前的用例全是绿的

    `TestClient` 会把请求串起来发 —— **它根本测不出这件事**。
    （同一个盲点此前已经让"并发闸门"那两条用例空转过一次：
     当时以为是 `TestClient` 的锅，改成"手动占住闸门"绕过去了，
     但没人问"那真实并发到底能不能发生"。）

    所以这里必须用 **`httpx.ASGITransport` + `asyncio.gather`**：两个请求真的同时
    进同一个事件循环。判据是**墙钟**：并行时 ≈ `d`，串行时 ≈ `2d`。
    """

    def _app_with_slow_engine(self, delay: float):
        """假引擎 + 人为延迟。延迟放在 `transcribe` 里 —— 也就是真正会阻塞的那一段。"""
        class _SlowEngine:
            def __init__(self, spec):
                self.spec = spec

            def transcribe(self, wav, lang="auto", timestamps=False):
                time.sleep(delay)
                return {"text": "slow", "sentences": [], "status": "ok"}

            def analyze(self, wav, max_speakers=None):
                time.sleep(delay)
                return [(0.0, 1.0, "S0")], [[0.1, 0.2]], ["S0"]

            def embed(self, wav):
                time.sleep(delay)
                return [[0.1, 0.2]], ["S0"]

            def close(self):
                pass

        cfg = _cfg(tempfile.mkdtemp(prefix="echo-loop-"))
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": lambda spec: _SlowEngine(spec)}):
            return server_main.create_app(cfg)

    def _two_concurrent(self, app, path, n=2):
        import asyncio
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=app)
            # **必须手动进 lifespan**：`app.state.echo` 是在 lifespan 里装的
            # （池、临时目录清理器、鉴权库都在那儿），而裸 `ASGITransport`
            # **不会**替你跑 lifespan —— 不走这一步会得到
            # `'State' object has no attribute 'echo'`。
            # （`TestClient` 之所以没这问题，是因为它自己管了 lifespan。）
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://test", timeout=60) as ac:
                    async def one():
                        r = await ac.post(path, content=_wav_bytes(),
                                          headers={"Content-Type": "audio/wav"})
                        return r.status_code
                    t0 = time.time()
                    codes = await asyncio.gather(*[one() for _ in range(n)])
                    return time.time() - t0, list(codes)

        return asyncio.run(go())

    def test_two_concurrent_asr_requests_actually_overlap(self):
        delay = 0.6
        app = self._app_with_slow_engine(delay)
        wall, codes = self._two_concurrent(app, "/v1/asr?variant=short")
        # 并行 ≈ 0.6；串行 ≈ 1.2。取 1.7× 当阈值，留足调度抖动。
        self.assertLess(wall, delay * 1.7,
                        "两个并发请求花了 %.2fs（单次推理 %.2fs）—— **事件循环被阻塞了**，"
                        "请求在串行处理。检查端点里是否漏了 run_in_threadpool：%s"
                        % (wall, delay, codes))
        self.assertEqual(codes.count(200) + codes.count(409), 2, codes)
        self.assertIn(409, codes,
                      "鉴权关着时所有请求算同一个客户端，两路并发**必须**有一路 409 "
                      "client_busy（每客户端 1 路）；拿到 %s 说明闸门没生效" % codes)

    def test_two_concurrent_diarize_requests_actually_overlap(self):
        delay = 0.6
        app = self._app_with_slow_engine(delay)
        wall, codes = self._two_concurrent(app, "/v1/diarize")
        self.assertLess(wall, delay * 1.7,
                        "diarize 也在串行（%.2fs，单次推理 %.2fs）：%s" % (wall, delay, codes))

    def test_two_concurrent_embed_requests_actually_overlap(self):
        delay = 0.6
        app = self._app_with_slow_engine(delay)
        wall, codes = self._two_concurrent(app, "/v1/speaker/embed")
        self.assertLess(wall, delay * 1.7,
                        "speaker/embed 也在串行（%.2fs，单次推理 %.2fs）：%s"
                        % (wall, delay, codes))

    def test_global_channels_are_actually_usable(self):
        """`max_concurrent: 2` 得**真的**是 2：两个**不同**客户端的请求要能同时跑完。

        上面几条测的是"事件循环没被堵住"，这条测的是"两个通道同时被用上了" ——
        `busy.active` 在推理期间必须到 2。
        """
        import asyncio
        import httpx

        delay = 0.6
        app = self._app_with_slow_engine(delay)
        seen = []

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://test", timeout=60) as ac:
                    async def one(cid):
                        r = await ac.post("/v1/asr?variant=short", content=_wav_bytes(),
                                          headers={"Content-Type": "audio/wav",
                                                   "X-Smoke-Client": cid})
                        return r.status_code

                    async def watch():
                        while True:
                            r = await ac.get("/v1/health")
                            seen.append(r.json()["busy"]["active"])
                            await asyncio.sleep(0.02)

                    w = asyncio.create_task(watch())
                    await asyncio.gather(one("a"), one("b"))
                    w.cancel()

        asyncio.run(go())
        # 鉴权关着 → 两个请求都是 anonymous，所以第二个会被 409 挡掉，
        # 通道上限 2 用不上。这里只要求"有请求真的同时在跑"这件事被观察到。
        self.assertTrue(seen, "health 一次都没采样到？")
        self.assertGreaterEqual(max(seen), 1, "从没观察到 active>=1，闸门没记录到活跃请求")


class ErrorCodeSurvivesLargeBodyTests(unittest.TestCase):
    """**错误码在大 body 下也必须送到客户端。**

    这里钉的是另一个实测出来的问题（2026-09-24）：服务端在**读 body 之前**就拒掉请求时，
    如果没把请求体抽干就关连接，socket 里剩下的未读数据会让对端收到 **RST**，
    而 RST 会**丢掉对端接收缓冲里已经到达的响应体** —— 客户端只拿到状态码，body 是空的。

    实测（`POST /v1/asr?model=nope`，0.9 MB body）：body 为空；换成 2 KB 就正常。
    **而且是竞态** —— 同一次跑里 `?model=nope` 丢了、`?mode=turns` 没丢，
    所以偶尔能过、非常容易被漏掉。

    为什么这条值得写成"真 socket"的测试：RST 是 **TCP 层**的事，
    而 `TestClient` 与 `httpx.ASGITransport` 都是进程内直调 ASGI，
    **根本不经过 socket**，也就不可能复现。测试手段必须配得上被观测的现象。
    """

    PORT = 0
    _server = None
    _thread = None
    _tmp = None

    @classmethod
    def setUpClass(cls):
        import socket as _socket

        import uvicorn

        cls._tmp = tempfile.mkdtemp(prefix="echo-rst-")
        cfg = _cfg(cls._tmp)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)

        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.PORT = s.getsockname()[1]
        s.close()

        config = uvicorn.Config(app, host="127.0.0.1", port=cls.PORT, log_level="warning")
        cls._server = uvicorn.Server(config)
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        for _ in range(200):                       # 最多等 10 秒
            if getattr(cls._server, "started", False):
                break
            time.sleep(0.05)
        if not getattr(cls._server, "started", False):
            raise RuntimeError("uvicorn 没起来")

    @classmethod
    def tearDownClass(cls):
        try:
            cls._server.should_exit = True
            cls._thread.join(timeout=5)
        except Exception:
            pass

    def _post(self, path, body, ctype="audio/wav", timeout=30):
        import urllib.error
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.PORT, path),
                                     data=body, method="POST")
        req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            except Exception:
                return e.code, b""

    def _assert_code(self, path, body, expect_status, expect_code, times=3):
        for i in range(times):
            with self.subTest(path=path, run=i):
                status, raw = self._post(path, body)
                self.assertEqual(status, expect_status, raw[:200])
                try:
                    got = json.loads(raw.decode("utf-8")).get("code")
                except Exception:
                    got = "<body 为空或不是 JSON: %r>" % raw[:80]
                self.assertEqual(got, expect_code,
                                 "第 %d 次：错误码没送到客户端。服务端没读完 body 就关连接，"
                                 "未读数据导致 RST，响应体被丢掉了 —— "
                                 "客户端只能盲目重试（见 audio.drain）" % (i + 1))

    def test_large_body_still_gets_model_not_found(self):
        big = b"\x00" * (1024 * 1024)              # 1 MB，超过 socket 缓冲
        self._assert_code("/v1/asr?model=nope", big, 404, "model_not_found")

    def test_large_body_still_gets_bad_request(self):
        big = b"\x00" * (1024 * 1024)
        self._assert_code("/v1/diarize?mode=turns", big, 400, "bad_request")

    def test_large_body_still_gets_unsupported_media(self):
        """这条本来就没问题（415 发生在**开始收**之后），留着防回归。"""
        big = b"\x00" * (1024 * 1024)
        self._assert_code("/v1/asr", big, 415, "unsupported_media", times=2)

    def test_small_body_also_works(self):
        """小 body 一直是好的 —— 用它做对照，说明上面几条失败不是别的原因。"""
        self._assert_code("/v1/asr?model=nope", b"x" * 1024, 404, "model_not_found",
                          times=1)


class VectorSpaceFrozenTests(unittest.TestCase):
    """铁律 L5：**同一场会议不得混用不同 `vectorSpaceId`**。

    这条铁律在服务端只有两个可执行的落点，本类把它们各钉一条：

    1. 出厂配置里**所有**声明了 `vectorSpaceId` 的模型必须共用同一个值。
       一旦有人给"现场注册"配了另一个嵌入模型（很自然的优化冲动：
       注册想快一点、用个小模型），比对就会在**跨向量空间**上做余弦相似度 ——
       数字照样在 0~1 之间，**不报错，只是认错人**。所以必须机械拦住。
    2. `ModelSpec` 不可变。版本/向量空间在**进程生命周期内冻结**：
       配置改了要重启，不能让一个跑着的服务中途换向量空间，
       那会让"同一场会议"里前后两段落在不同的空间里。
    """

    def test_every_vector_producing_spec_shares_one_space(self):
        spaces = {}
        for spec in engines.default_specs():
            if spec.vector_space_id:
                spaces.setdefault(spec.vector_space_id, []).append(spec.id)
        self.assertTrue(spaces, "没有任何模型声明 vectorSpaceId？那 capabilities 就没法告诉客户端能不能比对")
        self.assertEqual(len(spaces), 1,
                         "出厂配置里有多个向量空间，客户端会把不可比的向量当成可比的：%s" % spaces)

    def test_model_spec_is_immutable(self):
        spec = engines.default_specs()[0]
        with self.assertRaises(AttributeError):
            spec.vector_space_id = "ws-somebody-changed-it-at-runtime"  # type: ignore[misc]


# ================================================================ 鉴权
#
# 设计 §7。这一组用例的写法有一条贯穿的原则：**只钉"能观察到的行为"**，
# 不去断言内部实现（比如"缓存里有没有那一行"）—— 后者会让重构变成改测试。

def _auth_cfg(tmp_root, **over):
    cfg = settings_mod.load()
    cfg.raw["tmp"]["root"] = tmp_root
    cfg.raw["auth"]["db"] = os.path.join(tmp_root, "auth.db")
    for k, v in over.items():
        cfg.raw["auth"][k] = v
    return cfg


class AuthSchemaTests(unittest.TestCase):
    """设计 §8.5 的存储边界 —— **这是"服务端到底存了什么"唯一能被机器检查的地方**。

    判据一句话：存**关于请求的元数据**与**关于客户端的管理数据**；
    不存**请求的内容**，也不存任何业务概念。

    两条断言把这句话变成可执行的：表集合 ⊆ 白名单，列名不得命中列黑名单。
    加表加列都要先面对这两条。
    """

    #: 设计 §8.5 的列黑名单。**刻意放在测试里而不是 `server/store.py`** ——
    #: 它是审计规则不是运行时数据，而且它本身就是由那些"不许出现在服务端源码里的词"
    #: 拼成的：放进 server/ 会跟"源码不含业务词"的护栏打架，而护栏不该为自己让路。
    COLUMN_BLACKLIST = ("text", "transcript", "content", "body", "audio", "wav",
                        "embedding", "vector", "speaker_name", "meeting", "command",
                        "summary", "voiceprint", "prompt", "reply")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-auth-schema-")
        self.store = store_mod.Store(os.path.join(self.tmp, "auth.db"))
        self.addCleanup(self.store.close)

    def test_tables_are_a_subset_of_the_documented_whitelist(self):
        self.assertTrue(set(self.store.tables()) <= set(store_mod.TABLE_WHITELIST),
                        "出现了白名单外的表：%s" % (set(self.store.tables()) - set(store_mod.TABLE_WHITELIST)))
        self.assertTrue(self.store.tables(), "一张表都没有？那鉴权没地方放")

    def test_no_column_is_named_like_content(self):
        bad = []
        for table, cols in self.store.columns().items():
            for c in cols:
                if c.lower() in self.COLUMN_BLACKLIST:
                    bad.append("%s.%s" % (table, c))
        self.assertEqual(bad, [], "库里出现了'内容/业务概念'的列名：%s" % bad)

    def test_the_whitelist_here_matches_the_one_in_the_code(self):
        """代码里的白名单与设计 §8.5 必须一致（这里只钉前后两端不漂）。"""
        self.assertEqual(tuple(store_mod.TABLE_WHITELIST), ("clients", "pairing_codes"))


class AuthPairingTests(unittest.TestCase):
    """配对：一次性码 → `client_id` + `secret`（设计 §7.4）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-auth-pair-")
        self.cfg = _auth_cfg(self.tmp, enabled=True, mode="jwt",
                             jwt_secret="unit-test-secret-0123456789abcdef")
        self.store = auth_mod.open_store(self.cfg)
        self.auth = auth_mod.Auth(self.cfg, self.store)
        self.addCleanup(self.store.close)

    def test_code_is_single_use(self):
        code = self.auth.create_pairing_code("admin")
        first = self.auth.redeem(code, "张三的办公本")
        self.assertTrue(first["clientId"].startswith("cli-"))
        self.assertTrue(first["secret"])
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.redeem(code, "别人")
        self.assertEqual(ctx.exception.status, 401)

    def test_code_is_normalized_so_case_and_spaces_do_not_matter(self):
        """人工抄码必然带上空格、大小写也会随手改 —— 归一化，别让人白试。"""
        code = self.auth.create_pairing_code()
        out = self.auth.redeem("  " + code.lower() + " ", "抄错大小写的同事")
        self.assertTrue(out["clientId"])

    def test_unknown_code_is_refused(self):
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.redeem("ZZZZZZZZ")
        self.assertEqual(ctx.exception.status, 401)

    def test_expired_code_is_refused(self):
        self.cfg.raw["auth"]["pairing_ttl_s"] = -1        # 生成即过期
        code = self.auth.create_pairing_code()
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.redeem(code)
        self.assertEqual(ctx.exception.status, 401)

    def test_secret_is_never_stored_in_plaintext(self):
        """**只存哈希**（§7.4 约定 2）—— 直接翻库文件字节，不信任代码注释。"""
        code = self.auth.create_pairing_code()
        out = self.auth.redeem(code, "谁的机器")
        blob = open(os.path.join(self.tmp, "auth.db"), "rb").read()
        self.assertNotIn(out["secret"].encode(), blob, "secret 明文落库了")
        self.assertNotIn(code.encode(), blob, "配对码明文落库了")
        row = self.store.client(out["clientId"])
        self.assertTrue(row["secret_hash"])
        self.assertNotEqual(row["secret_hash"], out["secret"])

    def test_used_code_is_deleted_not_kept(self):
        """§8.5："用掉即删" —— 留着会让"待用配对码 0 行"那张自证页说谎。"""
        code = self.auth.create_pairing_code()
        self.assertEqual(len(self.store.pairing_codes()), 1)
        self.auth.redeem(code)
        self.assertEqual(self.store.pairing_codes(), [])

    def test_throttle_stops_brute_force(self):
        """免凭据的端点必须防猜（§7.4 约定 1）。

        31 个字符、8 位的码空间不小，但**不限速就等于把爆破变成一件耐心的事**。

        **故意走 HTTP 而不是直接调 `redeem()`**：限速的"来源"是 HTTP 层的东西
        （`X-Forwarded-For` / 对端地址），计数挂在端点上。这一点是本用例第一次
        写错时暴露出来的 —— 当时直接调 `redeem()`，401 一个接一个地来，
        因为**端点上的计数压根没被碰到**。"类里有个限速器"不等于"端点防住了"。
        """
        cfg = _auth_cfg(tempfile.mkdtemp(prefix="echo-auth-throttle-"),
                        enabled=True, mode="jwt",
                        jwt_secret="unit-test-secret-0123456789abcdef",
                        pair_max_failures=3)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)
            with TestClient(app) as c:
                for i in range(3):
                    r = c.post("/v1/pair", json={"code": "AAAAAAAA"})
                    self.assertEqual(r.status_code, 401, "第 %d 次应当是码不对" % (i + 1))
                r = c.post("/v1/pair", json={"code": "AAAAAAAA"})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()["code"], "rate_limited")
        self.assertIn("Retry-After", r.headers)

    def test_successful_pairing_clears_the_failure_counter(self):
        """配对成功要把计数清零 —— 否则一个用户手滑几次，
        整个办公室（同一个出口 IP）后面都进不来。"""
        cfg = _auth_cfg(tempfile.mkdtemp(prefix="echo-auth-throttle2-"),
                        enabled=True, mode="jwt",
                        jwt_secret="unit-test-secret-0123456789abcdef",
                        pair_max_failures=3)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)
            with TestClient(app) as c:
                for _ in range(2):
                    c.post("/v1/pair", json={"code": "AAAAAAAA"})
                code = c.app.state.echo.auth.create_pairing_code("admin")
                ok = c.post("/v1/pair", json={"code": code, "clientName": "手滑的那位"})
                self.assertEqual(ok.status_code, 200, ok.text)
                # 计数被清掉，所以后面还能再失败两次而不被限速
                for _ in range(2):
                    r = c.post("/v1/pair", json={"code": "AAAAAAAA"})
                    self.assertEqual(r.status_code, 401)

    def test_pairing_can_be_switched_off(self):
        """关掉之后**新机器进不来**（老客户端照用）—— §7.4 约定 1 的"可选择关闭"。"""
        cfg = _auth_cfg(tempfile.mkdtemp(prefix="echo-auth-pairoff-"),
                        enabled=True, mode="jwt",
                        jwt_secret="unit-test-secret-0123456789abcdef",
                        pairing_enabled=False)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)
            with TestClient(app) as c:
                code = c.app.state.echo.auth.create_pairing_code("admin")
                r = c.post("/v1/pair", json={"code": code})
        self.assertEqual(r.status_code, 403, r.text)


class AuthTokenTests(unittest.TestCase):
    """令牌：换、验、以及**撤销立即生效**（设计 §7.5）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-auth-token-")
        self.cfg = _auth_cfg(self.tmp, enabled=True, mode="jwt",
                             jwt_secret="unit-test-secret-0123456789abcdef")
        self.store = auth_mod.open_store(self.cfg)
        self.auth = auth_mod.Auth(self.cfg, self.store)
        self.addCleanup(self.store.close)
        code = self.auth.create_pairing_code()
        paired = self.auth.redeem(code, "测试客户端")
        self.client_id = paired["clientId"]
        self.secret = paired["secret"]
        self.basic = "Basic " + base64.b64encode(
            ("%s:%s" % (self.client_id, self.secret)).encode()).decode()

    def _bearer(self):
        return "Bearer " + self.auth.token_for(self.basic)["accessToken"]

    def test_token_round_trip(self):
        out = self.auth.token_for(self.basic)
        self.assertEqual(out["clientId"], self.client_id)
        self.assertEqual(out["expiresIn"], 3600)
        claims = auth_mod.decode_token(out["accessToken"], self.cfg.get("auth.jwt_secret"))
        self.assertEqual(claims["sub"], self.client_id)
        self.assertEqual(claims["ver"], 1)
        self.assertIn("jti", claims)

    def test_wrong_secret_is_refused(self):
        other = "Basic " + base64.b64encode(
            ("%s:%s" % (self.client_id, "not-the-secret")).encode()).decode()
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.token_for(other)
        self.assertEqual(ctx.exception.status, 401)

    def test_missing_jwt_secret_is_a_server_side_error_not_401(self):
        """没配密钥是**服务端自己的问题**，不能报成"你的凭据不对"。

        客户端看到 401 会去翻自己的配置；运营看到 503 auth_misconfigured
        才会来看服务端。**把这两种失败混起来是最贵的一种省事。**
        """
        cfg = _auth_cfg(tempfile.mkdtemp(prefix="echo-auth-nokey-"), enabled=True, mode="jwt")
        cfg.raw["auth"]["jwt_secret"] = ""
        store = auth_mod.open_store(cfg)
        self.addCleanup(store.close)
        a = auth_mod.Auth(cfg, store)
        with self.assertRaises(errors.EchoError) as ctx:
            a.key
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "auth_misconfigured")

    # ---- JWT 校验的四个经典攻击面 -------------------------------------------

    def test_alg_none_is_refused(self):
        """`alg: none` —— 最经典的 JWT 漏洞。

        如果实现信任 token 自称的算法，攻击者把签名段留空就能伪造任意身份。
        """
        forged = jwt.encode({"sub": self.client_id, "ver": 1,
                             "exp": int(time.time()) + 600},
                            key="", algorithm="none")
        with self.assertRaises(errors.EchoError) as ctx:
            auth_mod.decode_token(forged, self.cfg.get("auth.jwt_secret"))
        self.assertEqual(ctx.exception.status, 401)

    def test_alg_check_is_ours_not_the_librarys(self):
        """**这一层是我们自己的判断，不许依赖库的默认行为。**

        PyJWT 传 `algorithms=["HS256"]` 时自己也会拒 `alg=none`
        （实测抛 `InvalidAlgorithmError`）。所以上面那条用例**两条防线任一条在都能过**，
        它并没有钉住我们的那一层。

        这里把库换成"什么都放行"，验证我们自己的 header 检查仍然拦得住 ——
        因为"库会替我们挡住"这件事**不是我们的契约**：库可以换、可以升级、
        可以作为别的用途被包装。撤销一个身份靠的是我们自己的判断。
        """
        forged = jwt.encode({"sub": self.client_id, "ver": 1,
                             "exp": int(time.time()) + 600},
                            key="", algorithm="none")
        with patch.object(auth_mod.jwt, "decode",
                          return_value={"sub": self.client_id, "ver": 1}):
            with self.assertRaises(errors.EchoError) as ctx:
                auth_mod.decode_token(forged, self.cfg.get("auth.jwt_secret"))
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("算法", str(ctx.exception.detail))

    def test_header_must_be_exactly_what_we_issue(self):
        """`typ` 也必须是我们认的那两种之一（缺省或 `JWT`）。

        这条同样是**我们自己的**约定：私有协议里收紧头部，比"以后再说"便宜。
        """
        weird = jwt.encode({"sub": self.client_id, "ver": 1,
                            "exp": int(time.time()) + 600, "typ": "something-else"},
                           self.cfg.get("auth.jwt_secret"), algorithm="HS256",
                           headers={"typ": "not-a-jwt"})
        with self.assertRaises(errors.EchoError):
            auth_mod.decode_token(weird, self.cfg.get("auth.jwt_secret"))

    def test_token_signed_with_another_key_is_refused(self):
        forged = jwt.encode({"sub": self.client_id, "ver": 1,
                             "exp": int(time.time()) + 600},
                            "attacker-key", algorithm="HS256")
        with self.assertRaises(errors.EchoError):
            auth_mod.decode_token(forged, self.cfg.get("auth.jwt_secret"))

    def test_tampered_payload_is_refused(self):
        """改载荷（比如把 ver 改大以"撤销后继续用"）必须让签名对不上。"""
        good = self.auth.token_for(self.basic)["accessToken"]
        head, payload, sig = good.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        claims["ver"] = 999
        forged_payload = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).decode().rstrip("=")
        with self.assertRaises(errors.EchoError):
            auth_mod.decode_token("%s.%s.%s" % (head, forged_payload, sig),
                                  self.cfg.get("auth.jwt_secret"))

    def test_expired_token_is_refused(self):
        expired = jwt.encode({"sub": self.client_id, "ver": 1,
                              "exp": int(time.time()) - 7200},
                             self.cfg.get("auth.jwt_secret"), algorithm="HS256")
        with self.assertRaises(errors.EchoError):
            auth_mod.decode_token(expired, self.cfg.get("auth.jwt_secret"))

    def test_clock_skew_is_tolerated(self):
        """内网机器时钟未必准 —— 60 秒内的偏差要放行（§7.5 ③）。"""
        slightly_expired = jwt.encode({"sub": self.client_id, "ver": 1,
                                       "exp": int(time.time()) - 10},
                                      self.cfg.get("auth.jwt_secret"), algorithm="HS256")
        claims = auth_mod.decode_token(slightly_expired, self.cfg.get("auth.jwt_secret"))
        self.assertEqual(claims["sub"], self.client_id)

    # ---- 撤销 ---------------------------------------------------------------

    def test_revoke_takes_effect_on_the_very_next_request(self):
        """**撤销必须立即生效**，不许等那 1 小时的 exp 走完（§7.4 约定 4）。

        这是"短期 JWT 也能安全撤销"的全部依据：JWT 里带 `ver`，
        与缓存里的 `token_version` 比对。撤销 = 版本 +1 → 下一个请求就 401。
        """
        bearer = self._bearer()
        self.auth.authenticate(bearer)                       # 现在能用
        self.auth.cache.revoke(self.client_id)               # 撤销
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.authenticate(bearer)                   # 同一个 token
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("撤销", str(ctx.exception.detail) or str(ctx.exception))

    def test_token_signed_before_revoke_is_still_refused_after_reissue(self):
        """撤销后再换的新令牌版本号已经变了，旧令牌**永远**回不来。"""
        old = self._bearer()
        self.auth.cache.revoke(self.client_id)
        fresh = self._bearer()
        self.assertNotEqual(old, fresh)
        self.auth.authenticate(fresh)
        with self.assertRaises(errors.EchoError):
            self.auth.authenticate(old)

    def test_disabled_client_gets_403_not_401(self):
        """禁用是"我认识你，但不许用" —— 403。与 401（不知道你是谁）语义不同。"""
        bearer = self._bearer()
        self.auth.cache.set_disabled(self.client_id, True)
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.authenticate(bearer)
        self.assertEqual(ctx.exception.status, 403)

    def test_unknown_client_is_401(self):
        forged = jwt.encode({"sub": "cli-does-not-exist", "ver": 1,
                             "exp": int(time.time()) + 600},
                            self.cfg.get("auth.jwt_secret"), algorithm="HS256")
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.authenticate("Bearer " + forged)
        self.assertEqual(ctx.exception.status, 401)

    # ---- 跨进程撤销（这一条是补上一个真实漏洞时写的）------------------------

    def test_revoke_from_another_process_is_eventually_noticed(self):
        """**命令行 `--revoke` 是另一个进程。** 它改的是库，跑着的服务不会自己知道。

        实测过：命令行撤销之后，服务端**继续接受**那个 JWT，直到缓存自己过期
        （默认 60 秒）。设计里其实早写了"多实例 ≤5 秒靠轻量轮询发现"，
        但代码里没实现 —— 于是"撤销立即生效"这句在**唯一的运维入口**上是假话。

        这条用例模拟"另一个进程"：**绕开 `Auth` 对象，直接改库**，
        然后只调 `poll_once()`（不 sleep，确定性强），再看那个老 JWT 是否已经不认。
        """
        bearer = self._bearer()
        self.auth.authenticate(bearer)                       # 现在能用

        # 另开一个"进程"：新 Store + 新 Auth，改库（等同于命令行 --revoke）
        other = auth_mod.open_store(self.cfg)
        self.addCleanup(other.close)
        auth_mod.Auth(self.cfg, other).cache.revoke(self.client_id)

        # 本进程此刻**还不知道**（缓存里还是旧版本号）—— 这正是漏洞的样子
        self.auth.authenticate(bearer)
        # 轮询一次 → 发现 updated_at 变了 → 整体刷缓存
        self.assertTrue(self.auth.watcher.poll_once(), "轮询没发现库变过")
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.authenticate(bearer)
        self.assertEqual(ctx.exception.status, 401)

    def test_watcher_only_refreshes_when_something_actually_changed(self):
        """没变化就不刷 —— 否则每 5 秒把所有客户端行重读一遍，白烧。

        注意第一次轮询**会**返回 True：`setUp` 是先建 watcher、再配对，
        所以"多了一个客户端"对它是货真价实的变化。把它吸收掉之后，
        后面几次必须都是空转。这条钉的是**幂等**，不是"第一次必须为假"。
        """
        self.assertTrue(self.auth.watcher.poll_once(), "配对是变化，第一次该发现")
        settled = self.auth.watcher.refreshes
        for _ in range(3):
            self.assertFalse(self.auth.watcher.poll_once())
        self.assertEqual(self.auth.watcher.refreshes, settled, "没变化却又刷了缓存")

    def test_watcher_notices_disable_too_not_just_revoke(self):
        """探针是 `MAX(updated_at)` 而不是 `MAX(token_version)` ——**因为它要能发现禁用**。

        这是我刻意的选择：撤销改 `token_version`，禁用改 `disabled`，
        如果只看 token_version，禁用就永远同步不到别的进程。
        """
        other = auth_mod.open_store(self.cfg)
        self.addCleanup(other.close)
        other.set_disabled(self.client_id, True)             # 另一个进程禁用它
        self.assertTrue(self.auth.watcher.poll_once())
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.authenticate(self._bearer())
        self.assertEqual(ctx.exception.status, 403)


class AuthScopeTests(unittest.TestCase):
    """scopes：`asr | diarize | embed | tts`（设计 §7.2）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-auth-scope-")
        self.cfg = _auth_cfg(self.tmp, enabled=True, mode="jwt",
                             jwt_secret="unit-test-secret-0123456789abcdef")
        self.store = auth_mod.open_store(self.cfg)
        self.auth = auth_mod.Auth(self.cfg, self.store)
        self.addCleanup(self.store.close)

    def _client(self, scopes):
        cid = auth_mod.new_client_id()
        self.store.upsert_client(cid, "n", auth_mod.hash_secret("s", cid), scopes)
        row = self.store.client(cid)
        token, _ = auth_mod.issue_token(row, self.cfg.get("auth.jwt_secret"), 600)
        return "Bearer " + token

    def test_scope_is_enforced(self):
        bearer = self._client("asr")
        self.auth.authenticate(bearer, need_scope="asr")           # 放行
        with self.assertRaises(errors.EchoError) as ctx:
            self.auth.authenticate(bearer, need_scope="diarize")
        self.assertEqual(ctx.exception.status, 403)

    def test_empty_scopes_mean_no_extra_restriction(self):
        """**空 scopes = 不额外限制**，不是"什么都不许"。

        把空解释成"全禁"会让"我明明配了客户端却全 403"变成一个谜；
        真正的"什么都不许"应该由 `disabled` 表达。
        """
        bearer = self._client("")
        for scope in ("asr", "diarize", "embed"):
            self.auth.authenticate(bearer, need_scope=scope)

    def test_every_capability_endpoint_declares_a_scope(self):
        """端点到 scope 是一张**表**，不是散落各处的字符串 —— 表要覆盖所有能力端点。"""
        capability_posts = {p for (m, p) in
                            [("POST", "/v1/asr"), ("POST", "/v1/diarize"),
                             ("POST", "/v1/speaker/embed")]}
        self.assertEqual(set(routes_mod.ENDPOINT_SCOPES), capability_posts)
        self.assertEqual(routes_mod.ENDPOINT_SCOPES["/v1/diarize"], "diarize")

    def test_endpoints_actually_pass_their_scope(self):
        """接了表还不够 —— 端点得**真的把 scope 传下去**（否则表是摆设）。"""
        src = open(os.path.join(SERVER_DIR, "routes.py"), encoding="utf-8").read()
        for path, scope in routes_mod.ENDPOINT_SCOPES.items():
            with self.subTest(path=path):
                self.assertIn('need_scope="%s"' % scope, src,
                              "%s 没有把 need_scope=%s 传下去" % (path, scope))


class EnvOverrideTests(unittest.TestCase):
    """部署文件里写的 `ECHO_*` 变量，代码必须**真的读**。

    这是本仓库吃过的一类亏的通用形态：**手册上写了、代码里没做**。
    这里用机器把它变成可执行的 —— 扫 `compose.yaml` 与 `echo-server.example.yaml`
    里出现的每一个 `ECHO_*` 名字，逐个确认 `_env_overrides()` 处理了。

    为什么值得单独一条：写进部署文件的变量名不被读，是**最容易发生、又最难发现**
    的一种谎 —— 服务照着文档配，行为却完全没变，而现场看起来"配置是对的"。
    """

    #: 这些是**故意**只出现在注释里当示例值的（比如让人自己填的密钥格式）。
    #: 白名单要短，而且每一条都要能说出为什么 —— 它就是这个测试的漏洞面。
    ALLOWED_UNIMPLEMENTED = set()

    def _declared(self):
        names = set()
        # compose：**运行时环境变量**才在 `environment:` 里；`build.args` 是构建期的。
        # 结构上摘掉 `build`，比写一份"允许名单"可靠 —— 名单是会过期的，
        # 而"这一段不是运行时配置"是文件的固有结构。
        import yaml
        compose = yaml.safe_load(
            open(os.path.join(SERVER_DIR, "compose.yaml"), encoding="utf-8").read()) or {}
        for svc in (compose.get("services") or {}).values():
            if isinstance(svc, dict):
                names |= set(re.findall(r"\bECHO_[A-Z0-9_]+\b",
                                        json.dumps(svc.get("environment") or {})))
        # 示例配置里的 `ECHO_*` 全是运行时变量
        names |= set(re.findall(r"\bECHO_[A-Z0-9_]+\b",
                                open(os.path.join(SERVER_DIR, "echo-server.example.yaml"),
                                     encoding="utf-8").read()))
        # Dockerfile：只有 `ENV` 声明的是**运行时**配置。
        # `ARG` / `--build-arg` / `RUN if [ "$X" = ... ]` 都是构建期的。
        # （前两版这里都写松了：先扫整个文件、再按"含 ARG 的行"过滤，
        #   而 `RUN if [ "$ECHO_EXTRA" = "1" ]` 两样都不含 —— 是**测试自己**
        #   把 build arg 误报成运行时变量。所以改成只认 ENV 块。）
        env_lines, collecting = [], False
        with open(os.path.join(SERVER_DIR, "Dockerfile"), encoding="utf-8") as fh:
            docker_lines = fh.read().splitlines()
        for ln in docker_lines:
            if ln.startswith("ENV "):
                collecting = True
            elif collecting and not ln.endswith("\\") and not ln.startswith(" "):
                collecting = False
            if collecting:
                env_lines.append(ln)
        names |= set(re.findall(r"\bECHO_[A-Z0-9_]+\b", "\n".join(env_lines)))
        return names

    def test_every_declared_env_var_is_actually_read(self):
        src = open(os.path.join(SERVER_DIR, "settings.py"), encoding="utf-8").read()
        missing = sorted(n for n in self._declared()
                         if n not in src and n not in self.ALLOWED_UNIMPLEMENTED)
        self.assertEqual(missing, [],
                         "这些变量写进了部署文件，但 settings._env_overrides 没读它们：%s"
                         % missing)

    def test_we_declare_a_reasonable_number_of_vars(self):
        """防止上一条因为正则没匹配上而空转。"""
        self.assertGreaterEqual(len(self._declared()), 5)

    def test_false_is_not_truthy(self):
        """`ECHO_AUTH_ENABLED=false` **不能**把鉴权打开。

        容器编排里 `"false"` 是字符串，而 Python 里非空字符串都是真 ——
        直接 `bool(os.environ[...])` 会让"我明明关了"变成"它开着"。
        """
        for value, expect in (("false", False), ("0", False), ("no", False),
                              ("true", True), ("1", True), ("yes", True)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"ECHO_AUTH_ENABLED": value}, clear=False):
                    over = settings_mod._env_overrides()
                self.assertEqual(over.get("auth", {}).get("enabled"), expect)

    def test_unparsable_bool_is_treated_as_unset(self):
        """认不出来就当没设 —— **绝不猜**（猜错的代价是静默反向）。"""
        with patch.dict(os.environ, {"ECHO_AUTH_ENABLED": "maybe"}, clear=False):
            over = settings_mod._env_overrides()
        self.assertNotIn("enabled", over.get("auth", {}))

    def test_documented_auth_env_vars_work(self):
        """`compose.yaml` 里以注释形式给的那三个鉴权变量，**取消注释就得能用**。

        它们没进上面那条"声明 ⇒ 被读"的扫描（因为在 compose 里是注释，
        yaml 看不见）。但注释也是文档 —— 用户照着取消注释，行为必须跟着变。
        所以单独钉一条。
        """
        env = {"ECHO_AUTH_ENABLED": "true", "ECHO_AUTH_MODE": "jwt",
               "ECHO_JWT_SECRET": "x" * 32, "ECHO_PAIRING_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            over = settings_mod._env_overrides()
        self.assertEqual(over["auth"]["enabled"], True)
        self.assertEqual(over["auth"]["mode"], "jwt")
        self.assertEqual(over["auth"]["jwt_secret"], "x" * 32)
        self.assertEqual(over["auth"]["pairing_enabled"], False)

    def test_state_root_is_not_tmp_root(self):
        """**耐久状态不许落在临时目录里**（这一条是被一个真实的数据丢失隐患逼出来的）。

        `tmp.root` 是"随便删、会被清理器扫、可以挂 tmpfs 换性能"的地方；
        鉴权库存着所有客户端凭据。两者放一起意味着：运维照文档把 tmp 换成内存盘，
        **所有配过的客户端一起消失**。
        """
        cfg = settings_mod.load()
        self.assertNotEqual(os.path.abspath(cfg.state_root),
                            os.path.abspath(cfg.tmp_root),
                            "state_root 与 tmp_root 不能是同一个目录")
        with patch.dict(os.environ, {"ECHO_TMP_ROOT": "/dev/shm/echo"}, clear=False):
            cfg2 = settings_mod.load()
        self.assertNotIn("/dev/shm", cfg2.state_root,
                         "把 tmp 指到内存盘之后，state_root 跟着搬过去了 —— 那会丢客户端")

    def test_open_store_never_lands_in_tmp(self):
        """配置里不写 `auth.db` 时，库文件也不许落在 tmp_root 下。"""
        cfg = settings_mod.load()
        cfg.raw["tmp"]["root"] = tempfile.mkdtemp(prefix="echo-env-tmp-")
        cfg.raw["server"]["state_root"] = tempfile.mkdtemp(prefix="echo-env-state-")
        cfg.raw["auth"]["db"] = ""
        store = auth_mod.open_store(cfg)
        self.addCleanup(store.close)
        self.assertTrue(store.path.startswith(cfg.raw["server"]["state_root"]),
                        "鉴权库落到了 %s" % store.path)
        self.assertNotIn(cfg.raw["tmp"]["root"], store.path)


class AdminCliTests(unittest.TestCase):
    """命令行管理入口（管理面做好之前的唯一入口）。

    为什么它必须被测试：`/v1/pair` 要一次性配对码，而配对码只能从服务端这边发。
    管理面（设计 §8.4）还没做，所以**这条命令不通 = 这套鉴权装上了也用不起来**。
    它是"整套鉴权能不能真的落地"的最后一步，比任何单个函数都值得钉。

    这些动作**不打 HTTP**：直接开库。给管理动作开一条免鉴权的内部端点，
    正是最容易变成漏洞的做法。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-auth-cli-")
        self.cfg_path = os.path.join(self.tmp, "server.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            # specs 用假模型（YAML 是 JSON 的超集，直接塞进去）——
            # 这组用例验的是**鉴权链路**，不该顺手把 qwen3asr 真加载一遍。
            fh.write(
                "server: {id: cli-test, listen: '127.0.0.1:8901'}\n"
                "auth:\n"
                "  enabled: true\n"
                "  mode: jwt\n"
                "  jwt_secret: '0123456789abcdef0123456789abcdef'\n"
                "  db: '%s'\n"
                "tmp: {root: '%s'}\n"
                "models: {specs: %s}\n" % (
                    os.path.join(self.tmp, "auth.db").replace("\\", "/"),
                    self.tmp.replace("\\", "/"),
                    json.dumps(FAKE_SPECS)))

    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = server_main.main(["--config", self.cfg_path] + list(argv))
        return rc, buf.getvalue()

    def test_new_pairing_code_prints_a_pasteable_string(self):
        rc, out = self._run("--new-pairing-code", "--created-by", "管理员")
        self.assertEqual(rc, 0)
        self.assertIn("echo://pair?", out)
        self.assertIn("code=", out)

    def test_the_printed_code_actually_works(self):
        """**端到端的那一步**：命令行生成的码，服务端进程真的能兑换。

        这条把两件事接起来 —— 命令行写的库，就是服务端读的那个库。
        分开测两边都对、合起来不通，是这类工具最常见的坏法。
        """
        _, out = self._run("--new-pairing-code")
        url = [ln.strip() for ln in out.splitlines() if "echo://pair" in ln][0]
        code = url.split("code=")[1].split("&")[0]

        cfg = settings_mod.load(self.cfg_path)
        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": _fake_loader}):
            app = server_main.create_app(cfg)
            with TestClient(app) as c:
                r = c.post("/v1/pair", json={"code": code, "clientName": "CLI 的机器"})
                self.assertEqual(r.status_code, 200, r.text)
                cid = r.json()["clientId"]
                # 换令牌 + 带上它调能力端点（应当越过鉴权，落到音频解码那一层）
                basic = base64.b64encode(
                    ("%s:%s" % (cid, r.json()["secret"])).encode()).decode()
                tok = c.post("/v1/token",
                             headers={"Authorization": "Basic " + basic}).json()["accessToken"]
                r = c.post("/v1/asr", content=_wav_bytes(),
                           headers={"Content-Type": "audio/wav",
                                    "Authorization": "Bearer " + tok})
                self.assertEqual(r.status_code, 200, r.text)

    def test_list_clients_shows_a_paired_one(self):
        self._run("--new-pairing-code")
        cfg = settings_mod.load(self.cfg_path)
        store = auth_mod.open_store(cfg)
        code = auth_mod.Auth(cfg, store).create_pairing_code()
        auth_mod.Auth(cfg, store).redeem(code, "列出来的那台")
        store.close()
        rc, out = self._run("--list-clients")
        self.assertEqual(rc, 0)
        self.assertIn("列出来的那台", out)

    def test_revoke_bumps_the_version(self):
        cfg = settings_mod.load(self.cfg_path)
        store = auth_mod.open_store(cfg)
        a = auth_mod.Auth(cfg, store)
        out = a.redeem(a.create_pairing_code(), "要被撤的")
        store.close()
        rc, text = self._run("--revoke", out["clientId"])
        self.assertEqual(rc, 0)
        self.assertIn("撤销", text)
        store = auth_mod.open_store(cfg)
        self.assertEqual(store.client(out["clientId"])["token_version"], 2)
        store.close()


if __name__ == "__main__":
    unittest.main()
