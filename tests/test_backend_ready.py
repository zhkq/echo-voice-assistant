# -*- coding: utf-8 -*-
"""三层就绪（`app/backend_ready.py`，批 3）。

这一层的全部价值在一个反例上：**`/v1/health` 绿 ≠ 能干活**。
这个项目真踩过"health 全绿、每个 `/v1/asr` 都 503 `model_failed`"（权重缺失 / torch 与
torchaudio 的 CUDA ABI 不符，见 `AGENTS.md`），所以用例的核心是
`ReadyProbeTests::test_a_green_health_is_not_enough` —— 而且它**用真 HTTP 服务端**验
（一个几十行的 stdlib 桩），不是打桩掉 HTTP 层：三层之间的判据（谁先谁后、503 的内容
怎么读）恰恰是打桩最容易"验成另一件事"的地方。

桩服务端只回答三件事：`/v1/health`、`/v1/ready`、`/v1/asr`（外加 `/v1/diarize`）。
"""
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backend_ready                                  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):                                  # 静音
        pass

    def _serve(self):
        path = self.path.split("?")[0]
        status, payload = self.server.routes.get(path, (404, {"detail": "no such route"}))
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        # 读掉请求体（POST 音频），免得客户端写不进去
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        except Exception:
            pass
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve


class _StubServer:
    def __init__(self, routes):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.routes = dict(routes)
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


_HEALTH = {"status": "ok", "version": "0.1.0", "models": {"asr-long": "ready"}}
_READY_OK = {"ready": True, "failed": []}
_ASR_OK = {"text": "测试测试", "sentences": [], "modelVersion": "qwen3-asr-0.6b"}


class _ReadyCase(unittest.TestCase):
    def stub(self, routes):
        s = _StubServer(routes)
        self.addCleanup(s.stop)
        return s

    def probe(self, routes, token="t0ken", **kw):
        # 用例里默认**不等** health（wait_health=0）：桩服务端要么立刻答、要么根本不答，
        # 等 20 秒只会让"没应答"那几条用例白等。等待本身另有一条专门的用例。
        kw.setdefault("wait_health", 0)
        s = self.stub(routes)
        return backend_ready.probe(s.url, token=token, **kw), s

    def setUp(self):
        # 自测音频落在临时目录（别往这台机器的真实临时目录里堆文件）
        self.tmp = tempfile.mkdtemp(prefix="echo-ready-test-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        # **令牌自己给**：`_token_for()` 会去读这台机器真实的配对凭据（开发机上真配过对），
        # 那样"没令牌"这条分支永远走不到，而且用例会偷偷依赖机器的状态。
        p = mock.patch.object(backend_ready, "_token_for", lambda base_url="": "")
        p.start()
        self.addCleanup(p.stop)


class ReadyProbeTests(_ReadyCase):
    def test_a_green_health_is_not_enough(self):
        """**这一批存在的理由**：health 200 + ready 200，但真实 /v1/asr 是 503 `model_failed`。

        （权重缺失 / torch 与 torchaudio 的 CUDA ABI 不符时就是这个形状。）
        结论必须是"没过"，而且那句话要指得出这一档是什么。
        """
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (503, {"detail": "模型没就绪", "error": "model_failed"}),
        })
        self.assertTrue(out["l1"]["ok"], out)
        self.assertTrue(out["l2"]["ok"], out)
        self.assertFalse(out["ok"], "health/ready 全绿就判「能用」，正是这一层要挡的事")
        self.assertEqual(out["state"], "asr-failed")
        self.assertIn("真实自测失败", out["headline"])
        self.assertIn("503", out["headline"])

    def test_all_three_layers_green_is_ok(self):
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (200, _ASR_OK),
        })
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "ok")
        self.assertIn("三层都过", out["headline"])
        self.assertIn("测试测试", out["headline"])

    def test_a_backend_that_is_not_answering_is_reported_as_not_running(self):
        # 用一个**没人监听**的端口（现取一个再关掉，避免撞上别的服务）
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        out = backend_ready.probe("http://127.0.0.1:%d" % port, token="t", wait_health=0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "not-running")
        self.assertIsNone(out["l2"], "L1 没过就不该再往下问")

    def test_a_ready_503_reports_the_servers_own_failed_list(self):
        """L2 没过时把服务端给的 `failed[]` **原样**报出（哪个模型、为什么）。"""
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (503, {"ready": False,
                                "failed": ["asr-long: 权重缺失 hub/models--Qwen--Qwen3-ASR-0.6B"]}),
            "/v1/asr": (200, _ASR_OK),
        })
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "not-ready")
        self.assertIn("Qwen3-ASR", out["headline"])
        self.assertIsNone(out["l3"], "模型没就绪时**不该**再跑 L3（必然 503，还白等一次加载超时）")

    def test_an_empty_transcript_still_counts_as_the_engine_ran(self):
        """**空文本不算失败**（2026-09-30 真机校准）。

        自测音频是合成音调；实测里 qwen3asr 对 1 秒音调回了**空文本**、SenseVoice 回了个
        "Yeah." —— 两者都是"引擎跑完了"。第一版把"非空文本"当判据，于是把一个**完全正常**
        的后端判成失败（用户会去修一个不存在的问题）。
        新的凭据是 `200 + modelId/modelVersion`（池子解析出 spec 并真的跑完了）。
        """
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (200, {"text": "   ", "sentences": [], "modelId": "asr-long",
                              "modelVersion": "qwen3-asr-0.6b"}),
        })
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["l3"]["textEmpty"])
        self.assertIn("合成音无文本", out["headline"])
        self.assertIn("正常的", out["l3"]["detail"])

    def test_a_200_without_any_model_identity_is_not_a_pass(self):
        """反过来：200 但**既没有文本也没有 modelId** → 看不出引擎跑没跑，不算过。"""
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (200, {"text": ""}),
        })
        self.assertFalse(out["ok"], out)
        self.assertIn("看不出引擎到底跑没跑", out["headline"])

    def test_no_token_is_told_apart_from_a_bad_model(self):
        """被鉴权拒了、而手里又没有令牌 → 两件事都要说（别让人去查模型）。"""
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (401, {"detail": "缺少 Bearer 令牌"}),
        }, token="")
        self.assertFalse(out["ok"])
        self.assertIn("没有可用令牌", out["headline"])
        self.assertIn("配对", out["headline"])

    def test_it_waits_for_the_port_to_start_answering(self):
        """**刚起好后端时的那个窗口**（2026-09-30 真机逮到）：uvicorn 先 lifespan 后绑端口，
        而本机配对文件正是在 lifespan 里写的 —— "文件在了"之后的一小段里 `/v1/health` 是连接被拒。
        实测那次表现为 L1/L2 报失败、几秒后 L3 却 200（自相矛盾），用户读到的是"后端没在应答"。
        这条钉住"先等它开始应答，再判 L1"。
        """
        s = self.stub({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (200, _ASR_OK),
        })
        calls = {"n": 0}
        real = backend_ready.probe_health

        def _flaky(url, timeout=backend_ready.L1_TIMEOUT_S):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "status": 0, "health": {},
                        "detail": "连不上（连接被拒）"}
            return real(url, timeout=timeout)

        with mock.patch.object(backend_ready, "probe_health", _flaky):
            out = backend_ready.probe(s.url, token="t0ken", wait_health=5.0)
        self.assertTrue(out["l1"]["ok"], out)
        self.assertGreaterEqual(calls["n"], 2, "第一次失败后应该再试一次")
        self.assertTrue(out["ok"], out)

    def test_the_diarize_layer_is_optional_and_its_failure_is_not_fatal(self):
        """L4（分离）失败**不算整体失败**：老卡本来就没有这一档，如实说即可。"""
        out, _s = self.probe({
            "/v1/health": (200, _HEALTH),
            "/v1/ready": (200, _READY_OK),
            "/v1/asr": (200, _ASR_OK),
            "/v1/diarize": (503, {"detail": "这块卡没有分离档"}),
        }, diarize=True)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["l4"]["ok"])
        self.assertIn("分离", out["headline"])

    def test_probe_never_raises_on_a_garbage_url(self):
        for url in ("", "not-a-url", "http://127.0.0.1:1", "ftp://x"):
            with self.subTest(url=url):
                out = backend_ready.probe(url, token="t", wait_health=0)
                self.assertIn("ok", out)
                self.assertFalse(out["ok"])


class SelfTestAudioTests(_ReadyCase):
    def test_the_wav_is_a_real_16k_mono_pcm(self):
        """自测音频要是**真的** 16k 单声道 PCM（服务端按裸 body 收，形状错了会 415）。"""
        path = os.path.join(self.tmp, "x.wav")
        backend_ready.make_selftest_wav(path)
        with wave.open(path, "rb") as fh:
            self.assertEqual(fh.getnchannels(), 1)
            self.assertEqual(fh.getsampwidth(), 2)
            self.assertEqual(fh.getframerate(), 16000)
            self.assertEqual(fh.getnframes(), 16000)
            data = fh.readframes(64)
        self.assertTrue(any(b for b in data), "不许是整段静音（有的实现会把静音判成无有效音频）")

    def test_it_can_write_a_shorter_clip(self):
        path = os.path.join(self.tmp, "short.wav")
        backend_ready.make_selftest_wav(path, seconds=0.25, rate=8000)
        with wave.open(path, "rb") as fh:
            self.assertEqual(fh.getframerate(), 8000)
            self.assertEqual(fh.getnframes(), 2000)


if __name__ == "__main__":
    unittest.main()
