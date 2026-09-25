# -*- coding: utf-8 -*-
"""能力层的契约与铁律。

这个文件钉两类东西：

  **契约** —— 同一份断言**跑在两个后端上**（本机 / ECHO 后端），
  两边必须给出形状一致的结果与同一套降级原因。协议漂了，这里就该红。

  **铁律** —— L2（产出向量必须能声明向量空间）、L3（指令链路必须本机）、
  L5（同一场会议不许换向量空间）。这三条的共同点是：**违反了不报错，
  只是结果悄悄不对**（认错人 / 服务端一挂就没法说话 / 前后半场不同空间），
  所以必须机械拦住。

ECHO 后端那一半用**真 uvicorn + 真 HTTP**，不是打桩：客户端与服务端之间那份协议
只有真的走一遍线才验证得了（这正是"同一份契约跑两个后端"的意义）。
"""
import io
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import (                                     # noqa: E402
    BACKEND_ECHO_SERVER,
    BACKEND_ASR_PROVIDER,
    BACKEND_LOCAL,
    SERVER_CODE_RETRY,
    SERVER_CODE_TO_REASON,
    SKIP_REASONS,
    SLOTS,
    SOURCE_LAN,
    SOURCE_LOCAL,
    SOURCE_WAN,
    VECTOR_SLOTS,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    CapabilityRouter,
    DiarizeResult,
    EmbedResult,
    Need,
    error_from_server,
)
from app.capabilities.router import _sources_for_privacy                 # noqa: E402
from app.capabilities.assemble import (                                  # noqa: E402
    TIMESTAMPS_EXACT,
    TIMESTAMPS_KINDS,
    TIMESTAMPS_NONE,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 测试素材

def wav_bytes(seconds=0.5, sr=16000):
    """一段合法的 16k 单声道 wav（静音）。服务端要真能解码它。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % int(sr * seconds), *([0] * int(sr * seconds))))
    return buf.getvalue()


def write_wav(path, seconds=0.5):
    with open(path, "wb") as fh:
        fh.write(wav_bytes(seconds))
    return path


# ---------------------------------------------------------------- 假引擎

class _FakeEngine:
    """服务端用的假引擎（**不加载任何模型**，只回答形状）。"""

    def __init__(self, spec):
        self.spec = spec

    def transcribe(self, wav, lang="auto", timestamps=False):
        return {"text": "假文本", "sentences": [{"start": 0.0, "end": 0.5, "text": "假文本"}]
                if timestamps else [], "status": "ok"}

    def analyze(self, wav, max_speakers=None):
        return [(0.0, 0.5, "SPEAKER_00")], [[0.1] * 4], ["SPEAKER_00"]

    def embed(self, wav):
        return [[0.2] * 4], ["SPEAKER_00"]

    def close(self):
        pass


_FAKE_SPECS = [
    {"id": "asr-fake", "slot": "asr.long", "impl": "fake", "max_concurrency": 2,
     "modelVersion": "fake-asr-v1", "supports": ["asr.text", "asr.timestamps"]},
    {"id": "diarize-fake", "slot": "diarize.turns", "impl": "fake", "max_concurrency": 1,
     "modelVersion": "fake-dia-v1", "vectorSpaceId": "ws-fake-v1", "dim": 4,
     "supports": ["diarize.embeddings"]},
    {"id": "embed-fake", "slot": "speaker.embed", "impl": "fake", "max_concurrency": 1,
     "modelVersion": "fake-dia-v1", "vectorSpaceId": "ws-fake-v1", "dim": 4},
]


# ================================================================ 契约
#
# 同一个 `_Contract` 混入，被两个后端的用例类各继承一次。**这就是"行为等价"的写法**：
# 断言只写一遍，两边都跑。要加契约就加在这里，别只加在某一边。

class _Contract:
    """子类要提供 `make_client()` → 一个已就绪的 CapabilityClient，和 `wav()`。"""

    def make_client(self):
        raise NotImplementedError

    def wav(self):
        raise NotImplementedError

    # ---- 形状 --------------------------------------------------------------

    def test_declares_slots_from_the_shared_vocabulary(self):
        c = self.make_client()
        self.assertTrue(c.provides, "什么都没声明？那路由没法派活")
        unknown = set(c.provides) - set(SLOTS)
        self.assertEqual(unknown, set(),
                         "声明了不在 SLOTS 里的槽：%s —— 客户端与服务端必须用同一套词汇"
                         % sorted(unknown))

    def test_ready_never_raises(self):
        c = self.make_client()
        self.assertIn(c.ready(), (True, False, None))

    def test_describe_is_serializable_and_says_where_it_is(self):
        c = self.make_client()
        d = c.describe()
        json.dumps(d, ensure_ascii=False)          # 面板要能直接下发
        self.assertEqual(d["backendId"], c.backend_id)
        self.assertIn(d["source"], (SOURCE_LOCAL, SOURCE_LAN, SOURCE_WAN))

    # ---- ASR ---------------------------------------------------------------

    def test_asr_result_carries_provenance(self):
        c = self.make_client()
        if not c.supports("asr.text"):
            self.skipTest("这个后端不提供 asr.text")
        r = c.transcribe(self.wav(), lang="zh")
        self.assertIsInstance(r, AsrResult)
        self.assertGreater(len(r.text), 0, "假引擎应当给出文本（真引擎可能是空 = 没人说话）")
        self.assertTrue(r.provenance.backend_id, "结果必须带出处")
        self.assertTrue(r.provenance.model_version, "结果必须带模型版本")

    def test_timestamps_label_always_matches_the_data(self):
        """**标签必须与数据一致** —— 这是"两个后端行为等价"最值钱的一条。

        原本这条写的是"只能是 `exact` / `none`"，那是**引入拼装层之前**的写法。
        现在客户端后端可以自己装配，于是多了 `aligned`（有精确骨架、文本按字对齐）
        与 `estimated`（按字数均摊）。所以真正要钉的不再是"哪两个值"，
        而是**标签不许与数据打架**：

          * 说 `none` ⟺ 不给句子轴（不能说了没有又给一份）
          * 说 `exact`/`aligned`/`estimated` ⟹ **必须有**句子轴（不能空口说精确）

        为什么这条比"限定取值"更值钱：取值集合以后还会变（服务端将来可能给
        `estimated` 之外的档），但"说到做不到"永远是错的。
        """
        c = self.make_client()
        if not c.supports("asr.text"):
            self.skipTest("不提供 asr.text")
        for want in (False, True):
            with self.subTest(want_timestamps=want):
                r = c.transcribe(self.wav(), lang="zh", want_timestamps=want)
                self.assertIn(r.timestamps, TIMESTAMPS_KINDS)
                if r.timestamps == TIMESTAMPS_NONE:
                    self.assertEqual(r.sentences, (), "说了没有时间戳，就不该再给句子轴")
                else:
                    self.assertTrue(r.sentences, "说了 %s，就得真有句子轴" % r.timestamps)
                    for a, b, t in r.sentences:
                        self.assertLessEqual(a, b, "句子轴的起点不该晚于终点")

    def test_a_remote_backend_never_invents_estimated(self):
        """**服务端不装配**，所以它只会说 `exact` / `none`。

        拼装是**客户端**的事（设计 §4.4），服务端只回答"我这句时间轴是真给了还是没给"。
        这条钉住 `from_server` 的规整：服务端说了别的值也不认，一律当 `none`
        —— 免得下游把服务端随口一个词当成"精确时间戳"。
        """
        r = AsrResult.from_server({"text": "x", "timestamps": "estimated"}, "srv")
        self.assertEqual(r.timestamps, TIMESTAMPS_NONE)
        r2 = AsrResult.from_server({"text": "x", "timestamps": "exact",
                                    "sentences": [{"start": 0, "end": 1, "text": "x"}]}, "srv")
        self.assertEqual(r2.timestamps, TIMESTAMPS_EXACT)
        self.assertTrue(r2.sentences)

    # ---- 说话人 ------------------------------------------------------------

    def test_diarize_uses_local_labels_and_reports_a_space(self):
        c = self.make_client()
        if not c.supports("diarize.turns"):
            self.skipTest("这个后端不做说话人分离")
        r = c.diarize(self.wav())
        self.assertIsInstance(r, DiarizeResult)
        self.assertTrue(r.turns, "假引擎应当给出时间轴")
        for _a, _b, spk in r.turns:
            self.assertTrue(spk, "每个 turn 都要有说话人标签")
        self.assertTrue(r.vector_space_id, "产出向量的结果必须说明属于哪个向量空间")
        self.assertGreater(r.dim, 0)

    def test_embed_reports_the_same_space_as_diarize(self):
        """**注册与识别必须同源**：同一条音频的 embed 与 diarize 必须在同一个空间。

        不同源的表现是"余弦相似度照算，只是认错人" —— 不报错，所以只能钉住。
        """
        c = self.make_client()
        if not (c.supports("speaker.embed") and c.supports("diarize.turns")):
            self.skipTest("这个后端不同时提供 embed 与 diarize")
        e = c.embed(self.wav(), count=1)
        d = c.diarize(self.wav())
        self.assertIsInstance(e, EmbedResult)
        self.assertTrue(e.vectors)
        self.assertEqual(e.vector_space_id, d.vector_space_id,
                         "同一个后端的 embed 与 diarize 必须同一个向量空间")
        self.assertEqual(e.dim, d.dim)

    # ---- 失败 --------------------------------------------------------------

    def test_missing_file_is_a_classified_error_not_an_empty_result(self):
        """**失败必须分类**，而且不许拿空结果冒充成功。"""
        c = self.make_client()
        slot = "asr.text" if c.supports("asr.text") else (
            "diarize.turns" if c.supports("diarize.turns") else "")
        if not slot:
            self.skipTest("这个后端什么都不提供")
        with self.assertRaises(CapabilityError) as ctx:
            (c.transcribe if slot == "asr.text" else c.diarize)(
                os.path.join(tempfile.gettempdir(), "definitely-not-here-12345.wav"))
        self.assertIn(ctx.exception.reason, SKIP_REASONS)
        self.assertTrue(ctx.exception.detail, "分类之外还要给人话")


class LocalContractTests(_Contract, unittest.TestCase):
    """本机后端。用**打桩的引擎**跑契约 —— 要验的是这层的形状，不是模型准不准。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-cap-local-")
        cls.wav_path = write_wav(os.path.join(cls.tmp, "a.wav"))

    def make_client(self):
        from app.capabilities.local import LocalCapabilityClient
        # 只声明"我提供这些"，具体调用由下面的打桩接管 ——
        # 这样契约用例不依赖本机装没装 pyannote / whisper。
        return LocalCapabilityClient(provides={
            "asr.text", "asr.timestamps", "diarize.turns",
            "diarize.embeddings", "speaker.embed"})

    def wav(self):
        return self.wav_path

    def setUp(self):
        self._patches = [
            patch("app.audio.stt.transcribe_ex",
                  lambda wav, **kw: {"text": "本机假文本", "status": "ok", "detail": ""}),
            patch("app.audio.stt.transcribe_whisper",
                  lambda inst, wav, lang: ([_Seg(0.0, 0.5, "本机假句子")], None)),
            patch("app.audio.stt._ENGINES", {"whisper:small": object()}),
            patch("app.audio.diarize.diarize_wav_full",
                  lambda path, max_speakers=None: (
                      [(0.0, 0.5, "SPEAKER_00")],
                      [[0.1, 0.2, 0.3, 0.4]], ["SPEAKER_00"])),
            patch("app.capabilities.local._device", lambda: "cpu"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_local_engine_failure_becomes_a_classified_error(self):
        """本机引擎炸了 → `open-failed`（换个后端可能就好），**不是空文本**。

        这条特别重要：老的 `transcribe()` 把"引擎挂了"和"这段没人说话"都变成一个空串，
        调用方只能猜。`transcribe_ex` 就是为了修这个才存在的。
        """
        c = self.make_client()
        with patch("app.audio.stt.transcribe_ex",
                   lambda wav, **kw: {"text": "", "status": "error", "detail": "引擎炸了"}):
            with self.assertRaises(CapabilityError) as ctx:
                c.transcribe(self.wav())
        self.assertEqual(ctx.exception.reason, "open-failed")

    def test_empty_text_is_not_a_failure(self):
        """**空文本不是失败**：这段就是没人说话。它必须原样返回，不能抛。

        拿它当失败去"换后端"，会做出"安静片段 → 换后端 → 还是安静 → 再换"这种
        既浪费又难查的行为。
        """
        c = self.make_client()
        with patch("app.audio.stt.transcribe_ex",
                   lambda wav, **kw: {"text": "", "status": "empty", "detail": "没人说话"}):
            r = c.transcribe(self.wav())
        self.assertEqual(r.text, "")


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class EchoServerContractTests(_Contract, unittest.TestCase):
    """ECHO 后端。**真 uvicorn + 真 HTTP**，客户端与服务端之间那份协议真走一遍线。"""

    PORT = 0
    _server = None
    _thread = None

    @classmethod
    def setUpClass(cls):
        import uvicorn
        from server import engines, main as server_main, settings as settings_mod
        from app import paths

        # ⚠️ **快照 `app.paths` 的取值源。**
        # `create_app()` 会调 `settings.install_paths_seam()`，那是**进程内全局**
        # Monkey-patch：装完之后整个进程的 `paths.*` 都改读服务端配置
        # （`meetings_root()` 于是返回空 → 掉回默认目录）。
        # 不还原的话，**排在后面的** `test_config_compat` 会红，而它报的是 paths 的默认值，
        # 现象完全联想不到是这里留下的状态 —— 这个坑 2026-09-24 真踩过。
        cls._paths_seam = getattr(paths, "_settings_get", None)

        cls.tmp = tempfile.mkdtemp(prefix="echo-cap-srv-")
        cls.wav_path = write_wav(os.path.join(cls.tmp, "a.wav"))
        cfg = settings_mod.load()
        cfg.raw["tmp"]["root"] = os.path.join(cls.tmp, "tmp")
        cfg.raw["server"]["state_root"] = os.path.join(cls.tmp, "state")
        cfg.raw["auth"]["enabled"] = False
        cfg.raw["models"]["specs"] = _FAKE_SPECS

        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": lambda spec: _FakeEngine(spec)}):
            app = server_main.create_app(cfg)

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.PORT = s.getsockname()[1]
        s.close()
        cls._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.PORT,
                                                    log_level="warning"))
        cls._thread = threading.Thread(target=cls._server.run, daemon=True)
        cls._thread.start()
        for _ in range(200):
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
        # 把全局 Monkey-patch 放回去（见 setUpClass 的说明 —— 不还原会连累别的测试文件）
        from app import paths
        if cls._paths_seam is None:
            try:
                delattr(paths, "_settings_get")
            except AttributeError:
                pass
        else:
            paths._settings_get = cls._paths_seam

    def make_client(self):
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient(base_url="http://127.0.0.1:%d" % self.PORT)
        self.assertTrue(c.refresh(force=True), "capabilities 拉不下来")
        return c

    def wav(self):
        return self.wav_path

    def test_capabilities_intersect_with_our_slot_vocabulary(self):
        """服务端报 `asr.long`（它模型的主槽名），但**那不是客户端的词汇**。

        客户端只认 `base.SLOTS` 那一套。这条钉住"取交集"这个动作：
        如果哪天把服务端的槽名原样搬过来，路由就会拿一个没人认识的槽去派活。
        """
        c = self.make_client()
        from server import engines
        server_slots = set()
        for spec in engines.default_specs():
            server_slots.add(spec.slot)
            server_slots |= set(spec.supports)
        self.assertTrue(set(c.provides) <= set(SLOTS))
        # 假 spec 里有 asr-fake（服务端主槽 asr.long）、diarize、embed
        self.assertIn("asr.text", c.provides)
        self.assertIn("asr.timestamps", c.provides)
        self.assertIn("diarize.turns", c.provides)
        self.assertIn("speaker.embed", c.provides)

    def test_server_error_body_is_translated_with_reason_and_code(self):
        """服务端回一个错误体时，适配器要**按 code 翻译**（不是按 HTTP 状态猜）。

        这里打桩 `_request` 而不是真发请求：要测的是**翻译**这一步，
        让真服务端去造 `client_busy` 需要两个并发请求，测的是别的东西。
        （真服务端的形状由 `ServerCodeCoverageTests` 从 `server/errors.py` 直接对表。）
        """
        c = self.make_client()
        body = {"code": "client_busy", "message": "你已有一个请求正在处理",
                "detail": "每客户端同时只允许 1 个"}
        with patch.object(type(c), "_request", return_value=(409, body)):
            with self.assertRaises(CapabilityError) as ctx:
                c.transcribe(self.wav())
        e = ctx.exception
        self.assertEqual(e.reason, "busy")
        self.assertEqual(e.code, "client_busy")
        self.assertFalse(e.retryable, "client_busy 重试永远不会成功")
        self.assertEqual(e.backend_id, BACKEND_ECHO_SERVER)

    def test_server_busy_is_retryable_but_client_busy_is_not(self):
        c = self.make_client()
        for code, retryable in (("client_busy", False), ("server_busy", True)):
            with self.subTest(code=code):
                body = {"code": code, "message": "忙", "retryAfter": 5}
                with patch.object(type(c), "_request", return_value=(503, body)):
                    with self.assertRaises(CapabilityError) as ctx:
                        c.transcribe(self.wav())
                self.assertEqual(ctx.exception.retryable, retryable)
                self.assertEqual(ctx.exception.retry_after, 5)

    def test_unreachable_server_is_offline_not_error(self):
        """连不上 → `offline`（可重试/可跳过），**不是**含糊的 `error`。"""
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient(base_url="http://127.0.0.1:1", timeout_infer=2)
        with self.assertRaises(CapabilityError) as ctx:
            c.transcribe(self.wav())
        self.assertIn(ctx.exception.reason, ("offline", "absent"))

    def test_not_configured_is_absent_and_says_so(self):
        # `creds=False` = **就当本机没配对**（地址也不许从凭据里来）。
        # 这一句不能省：没有它，"配没配"就取决于**跑测试的那台机器**配过对没有 ——
        # 2026-09-24 真机上配对一次之后这条用例当场变红（`refresh()` 返回 True）。
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient(base_url="", creds=False)
        self.assertFalse(c.refresh(force=True))
        self.assertFalse(c.ready())
        with self.assertRaises(CapabilityError) as ctx:
            c.transcribe(self.wav())
        self.assertEqual(ctx.exception.reason, "absent")

    def test_the_non_paired_switch_really_ignores_credentials(self):
        """`creds=False` 的**地址**也不许来自凭据 —— 注释与行为必须一致。

        背景：这个开关原来只挡住 `self._creds`，而 `base_url` 照旧从凭据文件里取。
        于是"没配对"这件事**取决于跑测试的机器**：一台真配过对的开发机上，
        `test_not_configured_is_absent_and_says_so` 会红，而报错信息
        （`refresh()` 返回 True）完全指不到原因。
        """
        import types
        from unittest.mock import patch

        from app.capabilities import credentials as cred_mod
        from app.capabilities import echo_server as es
        fake = types.SimpleNamespace(base_url="http://127.0.0.1:9", client_id="cli-x",
                                     secret="s", cert_pem="", cert_fingerprint="")
        with patch.object(cred_mod, "load", lambda: fake), \
                patch.object(es, "_setting", lambda key, default=None: ""):
            # 反面：默认（creds=None）时"只配对、什么都没配"是**能用状态**（§2.7 的取舍）
            self.assertEqual(es.EchoServerClient().base_url, "http://127.0.0.1:9")
            # 正面：说了"就当没配对"，地址也必须是空的
            self.assertEqual(es.EchoServerClient(base_url="", creds=False).base_url, "")
            # 显式传地址永远赢
            self.assertEqual(es.EchoServerClient(base_url="http://x:1", creds=False).base_url,
                             "http://x:1")


# ================================================================ 铁律

class _StubClient(CapabilityClient):
    """测试用后端：能精确控制"声明什么、属于哪个源、向量空间是啥"。"""

    def __init__(self, backend_id, provides, source=SOURCE_LOCAL, vspace="",
                 fail_with=None):
        self.backend_id = backend_id
        self.provides = frozenset(provides)
        self.source = source
        self.vector_space_id = vspace
        self.fail_with = fail_with
        self.calls = 0

    def transcribe(self, wav, *, lang="auto", want_timestamps=False, variant="long", **kw):
        self.calls += 1
        if self.fail_with:
            raise CapabilityError(self.fail_with, "stub 故意失败",
                                  backend_id=self.backend_id, slot="asr.text")
        return AsrResult(text="stub-%s" % self.backend_id,
                         provenance=_prov(self.backend_id),
                         timestamps="none")

    def diarize(self, wav, *, max_speakers=None, **kw):
        self.calls += 1
        if self.fail_with:
            raise CapabilityError(self.fail_with, "stub 故意失败",
                                  backend_id=self.backend_id)
        return DiarizeResult(turns=((0.0, 1.0, "S0"),), speakers={"S0": (0.1, 0.2)},
                             dim=2, vector_space_id=self.vector_space_id,
                             provenance=_prov(self.backend_id))

    def embed(self, wav, *, count=1, **kw):
        self.calls += 1
        return EmbedResult(vectors=((0.1, 0.2),), dim=2,
                           vector_space_id=self.vector_space_id,
                           provenance=_prov(self.backend_id))


def _prov(backend_id):
    from app.capabilities import Provenance
    return Provenance(backend_id, "stub-v1")


def _router(*clients, settings=None):
    return CapabilityRouter(list(clients),
                            settings_get=(lambda k, d=None: (settings or {}).get(k, d)))


class IronLawTests(unittest.TestCase):
    """L2 / L3 / L5 —— 三条"违反了不报错，只是结果悄悄不对"的铁律。"""

    def test_L2_vector_slots_require_a_declared_vector_space(self):
        """**产出向量的能力，不许落在不能声明向量空间的后端上。**

        反例就是"网络服务商"那类服务：它能转写，但你不知道它的嵌入属于哪个空间。
        拿它去比余弦相似度**不报错**，只是认错人。
        """
        blind = _StubClient("blind", {"diarize.turns"}, source=SOURCE_WAN, vspace="")
        r = _router(blind, settings={"capabilityPrivacy": "wan",
                                     "capabilityDiarizeBackend": "auto"})
        plan = r.plan(Need(slots=("diarize.turns",)))
        self.assertNotIn("diarize.turns", plan.picks)
        reasons = [s.reason for s in plan.skipped if s.slot == "diarize.turns"]
        self.assertIn("vector-mismatch", reasons)
        self.assertTrue(any("认错人" in s.detail for s in plan.skipped),
                        "拒绝的理由要说得清后果，不能只说'不行'")

    def test_L3_command_path_is_always_local(self):
        """**指令链路的 asr.text 只许本机** —— 哪怕设置里点名要 ECHO 后端。

        理由（L3）：指令转写是"说句话让它干活"的核心链路，
        **不允许依赖服务端可用性**。所以这条不看设置、直接钉死。
        """
        echo = _StubClient(BACKEND_ECHO_SERVER, {"asr.text"}, source=SOURCE_LAN)
        local = _StubClient(BACKEND_LOCAL, {"asr.text"})
        r = _router(echo, local, settings={
            "capabilityPrivacy": "lan",
            "capabilityMeetingAsrBackend": "echo-server",     # 点名要服务端
            "capabilityEchoServerUrl": "http://x:8900"})
        cmd = r.plan(Need(slots=("asr.text",), purpose="command"))
        self.assertEqual(cmd.backend_for("asr.text"), BACKEND_LOCAL,
                         "指令链路被路由到服务端了 —— 那服务端一挂就说不了话")
        # 同一份设置，会议链路才允许用服务端
        mtg = r.plan(Need(slots=("asr.text",), purpose="meeting"))
        self.assertEqual(mtg.backend_for("asr.text"), BACKEND_ECHO_SERVER)

    def test_L5_locked_vector_space_never_falls_back_to_another_space(self):
        """**宁可没有向量，也不要跨空间。** 这是 L5 真正的保证。

        场景：本场已经锁到 `ws-A`，而 `ws-A` 那个后端此刻不可用（没注册/挂了），
        只剩一个 `ws-B` 的。此时**必须什么都没有**，而不是"换个能用的" ——
        换了之后前半场与后半场的嵌入不可比，而**不可比是不报错的**，只会认错人。
        """
        a = _StubClient("backendA", {"diarize.turns"}, vspace="ws-A")
        b = _StubClient("backendB", {"diarize.turns"}, vspace="ws-B")
        # 第一次：锁到 A
        plan1 = _router(a, b, settings={"capabilityDiarizeBackend": "auto"}).plan(
            Need(slots=("diarize.turns",)))
        locked = plan1.vector_space_id
        self.assertEqual(locked, "ws-A")

        # 带上锁定值，但只注册 B（A 不可用了）
        r2 = _router(b, settings={"capabilityDiarizeBackend": "auto"})
        plan2 = r2.plan(Need(slots=("diarize.turns",), vector_space_id=locked))
        self.assertNotIn("diarize.turns", plan2.picks, "跨空间回退了 —— 会认错人")
        self.assertTrue([s for s in plan2.skipped if s.reason == "vector-mismatch"],
                        "拒绝了却没说清是 vector-mismatch")
        with self.assertRaises(CapabilityError) as ctx:
            r2.call("diarize.turns", Need(slots=("diarize.turns",),
                                          vector_space_id=locked), wav="x.wav")
        self.assertEqual(ctx.exception.reason, "vector-mismatch")

    def test_L5_candidates_list_excludes_other_spaces(self):
        """计划里的候选池**也**要排除跨空间的后端 —— 否则 `call()` 会换过去。"""
        a = _StubClient("backendA", {"diarize.turns"}, vspace="ws-A")
        b = _StubClient("backendB", {"diarize.turns"}, vspace="ws-B")
        r = _router(a, b, settings={"capabilityDiarizeBackend": "auto"})
        plan = r.plan(Need(slots=("diarize.turns",), vector_space_id="ws-A"))
        self.assertEqual(plan.candidates["diarize.turns"], ["backendA"])
        self.assertEqual(plan.backend_for("diarize.turns"), "backendA")

    def test_privacy_none_blocks_every_network_backend(self):
        """`privacy=none` = **不出机**。这条是"哪些后端根本不被考虑"的判据。"""
        echo = _StubClient(BACKEND_ECHO_SERVER, {"asr.text"}, source=SOURCE_LAN)
        local = _StubClient(BACKEND_LOCAL, {"asr.text"})
        r = _router(echo, local, settings={"capabilityPrivacy": "none",
                                           "capabilityEchoServerUrl": "http://x:8900"})
        plan = r.plan(Need(slots=("asr.text",), purpose="meeting", privacy="none"))
        self.assertEqual(plan.backend_for("asr.text"), BACKEND_LOCAL)
        self.assertTrue(any(s.reason == "blocked" for s in plan.skipped))

    def test_privacy_ladder(self):
        self.assertEqual(_sources_for_privacy("none"), {SOURCE_LOCAL})
        self.assertEqual(_sources_for_privacy("lan"), {SOURCE_LOCAL, SOURCE_LAN})
        self.assertEqual(_sources_for_privacy("wan"),
                         {SOURCE_LOCAL, SOURCE_LAN, SOURCE_WAN})

    def test_degradation_tries_the_next_backend_and_says_why(self):
        """一个后端失败 → **换下一个**，并且留下 `skipped` + 原因。"""
        bad = _StubClient("bad", {"asr.text"}, fail_with="open-failed")
        good = _StubClient("good", {"asr.text"})
        r = _router(bad, good, settings={"capabilityMeetingAsrBackend": "auto",
                                         "capabilityEchoServerUrl": ""})
        result, plan = r.call("asr.text", Need(slots=("asr.text",)), wav="x.wav")
        self.assertEqual(result.provenance.backend_id, "good")
        self.assertEqual(plan.backend_for("asr.text"), "good")
        self.assertTrue([s for s in plan.skipped if s.backend_id == "bad"],
                        "换后端了却没记下'原来那个为什么不行'")

    def test_all_backends_failing_raises_with_the_reason(self):
        a = _StubClient("a", {"asr.text"}, fail_with="open-failed")
        b = _StubClient("b", {"asr.text"}, fail_with="open-failed")
        r = _router(a, b, settings={"capabilityEchoServerUrl": ""})
        with self.assertRaises(CapabilityError) as ctx:
            r.call("asr.text", Need(slots=("asr.text",)), wav="x.wav")
        self.assertIn(ctx.exception.reason, SKIP_REASONS)

    def test_no_backend_for_a_slot_says_which_and_why(self):
        only_asr = _StubClient("only-asr", {"asr.text"})
        r = _router(only_asr, settings={"capabilityDiarizeBackend": "auto"})
        with self.assertRaises(CapabilityError) as ctx:
            r.call("diarize.turns", Need(slots=("diarize.turns",)), wav="x.wav")
        self.assertEqual(ctx.exception.reason, "unsupported")
        self.assertIn("only-asr", str(ctx.exception))

    def test_explicit_backend_choice_is_respected_not_silently_swapped(self):
        """点名用某个后端时**只试它** —— 否则"我要用这个"会变成一句空话。"""
        a = _StubClient("a", {"asr.text"}, fail_with="open-failed")
        b = _StubClient("b", {"asr.text"})
        r = _router(a, b, settings={"capabilityMeetingAsrBackend": "a"})
        with self.assertRaises(CapabilityError):
            r.call("asr.text", Need(slots=("asr.text",)), wav="x.wav")
        self.assertEqual(b.calls, 0, "点名了 a，就不该偷偷换到 b")


class ServerCodeCoverageTests(unittest.TestCase):
    """**服务端每一个错误码，客户端都要有翻译。**

    这条是跨进程契约的机械保证：服务端加了新错误码而客户端没跟上，
    客户端会把它当 `error`（"其它失败"）—— 于是"该退避重试"和"该换后端"
    分不出来，退化成盲目重试。这里直接从 `server/errors.py` 读出全部 code 来对。
    """

    @classmethod
    def setUpClass(cls):
        src = open(os.path.join(ROOT, "server", "errors.py"), encoding="utf-8").read()
        cls.codes = set(re.findall(r'EchoError\(\s*\d+,\s*"([a-z_]+)"', src))
        # 工厂函数里的多行写法也要抓到
        cls.codes |= set(re.findall(r'return EchoError\(\s*\d+,\s*"([a-z_]+)"', src))

    def test_we_found_the_server_codes(self):
        self.assertGreaterEqual(len(self.codes), 10, "没从 server/errors.py 读出错误码？")

    def test_every_server_code_has_a_reason(self):
        missing = sorted(self.codes - set(SERVER_CODE_TO_REASON))
        self.assertEqual(missing, [],
                         "这些服务端错误码客户端没有翻译，会被当成含糊的 error：%s" % missing)

    def test_every_server_code_has_a_retry_policy(self):
        missing = sorted(self.codes - set(SERVER_CODE_RETRY))
        self.assertEqual(missing, [],
                         "这些错误码没有'能不能重试'的判断：%s —— "
                         "缺了它，客户端只能盲目重试" % missing)

    def test_client_busy_and_server_busy_are_not_merged(self):
        """服务端**明确要求**这两档分开（合并了客户端就只能盲目重试）。

        我们的权威降级词汇里只有一个 `busy`，所以这条分开体现在 **retry 策略**上 ——
        这正是"两套词各管一件事"的落点。
        """
        self.assertFalse(SERVER_CODE_RETRY["client_busy"])
        self.assertTrue(SERVER_CODE_RETRY["server_busy"])

    def test_quota_and_rate_limit_differ(self):
        self.assertFalse(SERVER_CODE_RETRY["quota_exceeded"])   # 今天别再试
        self.assertTrue(SERVER_CODE_RETRY["rate_limited"])      # 等几秒再来

    def test_unknown_code_degrades_to_error_not_a_guess(self):
        """认不出的 code 一律 `error` —— 宁可信息少，也不要按猜的原因做出有副作用的决定。"""
        e = error_from_server({"code": "something_brand_new", "message": "?"}, 500)
        self.assertEqual(e.reason, "error")

    def test_gateway_html_without_code_is_offline(self):
        """反代回的 HTML（没有 code）→ `offline`，因为那多半是网关不通。"""
        self.assertEqual(error_from_server(None, 502).reason, "offline")
        self.assertEqual(error_from_server(None, 400).reason, "error")


class VocabularyTests(unittest.TestCase):
    def test_reason_vocabulary_is_exactly_the_authoritative_ten(self):
        """降级原因**只有这十个**（`docs/统一路由` §2 的权威清单）。

        四域必须同一套词，否则面板与排障没法统一。加一个就要先改文档。
        """
        self.assertEqual(SKIP_REASONS, (
            "absent", "blocked", "busy", "offline", "circuit-open", "quota",
            "unsupported", "vector-mismatch", "open-failed", "error"))

    def test_slot_vocabulary_matches_the_design(self):
        """槽清单与设计 §4.2 **逐字对齐**（加一个都要改文档）。"""
        self.assertEqual(SLOTS, (
            "wake", "asr.text", "asr.timestamps", "asr.streaming",
            "diarize.turns", "diarize.embeddings", "diarize.turn_embeddings",
            "speaker.embed", "tts"))

    def test_asr_long_is_not_a_client_slot(self):
        """`asr.long` 是**服务端模型自己的主槽名**，不是客户端的词汇。

        这条单独钉一次，因为它是本层最容易被"顺手统一"搞错的地方：
        服务端 capabilities 里会出现 `asr.long`，但客户端不该认识它。
        """
        self.assertNotIn("asr.long", SLOTS)
        self.assertIn("asr.text", SLOTS)

    def test_vector_slots_are_a_subset_of_slots(self):
        self.assertTrue(set(VECTOR_SLOTS) <= set(SLOTS))

    def test_rejecting_a_bogus_reason_is_loud(self):
        """原因写错要**当场炸**，不能默默传下去 —— 面板会按它聚合。"""
        with self.assertRaises(ValueError):
            CapabilityError("something-made-up", "x")
        with self.assertRaises(ValueError):
            Need(slots=("not-a-slot",))


if __name__ == "__main__":
    unittest.main()
