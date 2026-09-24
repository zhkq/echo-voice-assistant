# -*- coding: utf-8 -*-
"""能力后端（`server/`）的契约与护栏。

这个文件钉两类东西：

  **护栏** —— "服务端不存储业务数据 / 不认识业务概念"必须是**能被测试钉住的属性**，
  不是承诺。所以扫源码、扫路由表、扫跑完之后的磁盘。

  **契约** —— 两级并发闸门、拒绝类型的区分、临时文件的清理、模型池的
  单飞/引用计数/LRU/显存预算，以及最要紧的一条：**失败就是失败**（不回退 CPU）。

不碰真实麦克风、不加载真实模型：引擎层用假加载器（`_FakeEngine`）。
"""
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient                        # noqa: E402

from server import engines, errors, main as server_main, tmp     # noqa: E402
from server import routes as routes_mod                          # noqa: E402
from server import settings as settings_mod                      # noqa: E402
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
        """`/v1/*` 只有设计里那几条；**没有任何写端点**。

        这条是"服务端不存储业务数据"的结构性保证之一 ——
        一旦有人加了 `/v1/x`，它会红，而不是悄悄多一个能存东西的口子。
        """
        allowed = {
            ("GET", "/v1/capabilities"), ("GET", "/v1/health"), ("GET", "/v1/ready"),
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


if __name__ == "__main__":
    unittest.main()
