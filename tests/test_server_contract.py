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
