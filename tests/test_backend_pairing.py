# -*- coding: utf-8 -*-
"""配对 / 换令牌 / 401 自动续期（设计 §7.4、§7.5 ①②③）。

分两层，**两层都要**：

  1. `WireShapeTests` —— 一个几十行的 stdlib `http.server`，只回答形状。
     它管的是"我们发出去的字节对不对"：路径、JSON 字段名、Basic 头的拼法、
     重试了几次。这些**协议字节**是客户端与服务端唯一的耦合面，值得逐字钉住。
  2. `RealServerTests` —— 真 uvicorn + 打开鉴权的真服务端（假引擎，不加载模型）。
     它管的是"两边合起来是不是真的能用"：配对 → 换令牌 → 调用，
     以及令牌被拒时会**自己换一次再试一次**。

为什么第 2 层不能用第 1 层代替：第 1 层是我按自己对协议的印象写的桩 ——
桩写错了，第一层照样全绿，而错的印象会被"测试通过"加固。只有真服务端能证伪它。
"""
import base64
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import credentials as cred          # noqa: E402
from app.capabilities import pairing                      # noqa: E402
from app.capabilities.credentials import BackendCredentials  # noqa: E402
from app.capabilities.pairing import PairingError         # noqa: E402


# ================================================================ 第 1 层：线路形状

class _StubHandler(http.server.BaseHTTPRequestHandler):
    """按 `script` 列表依次回答，并把每个请求原样记下来。

    `script` 的每一项是 `(status, body_dict)`；用完之后一直重复最后一项。
    """

    def do_POST(self):                                       # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization") or "",
            "ctype": self.headers.get("Content-Type") or "",
            "body": raw,
        })
        idx = min(len(self.server.requests) - 1, len(self.server.script) - 1)
        status, payload = self.server.script[idx]
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):                               # 安静
        pass


class _Stub:
    def __init__(self, script):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.httpd.script = script
        self.httpd.requests = []
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def requests(self):
        return self.httpd.requests

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_PAIR_OK = {"clientId": "cli-1", "secret": "shh-1", "name": "测试机",
            "scopes": ["asr"], "serverName": "gpu-01", "protocol": 1}
_TOKEN_OK = {"accessToken": "jwt-abc", "expiresIn": 3600, "scopes": ["asr"],
             "clientId": "cli-1"}


class _CredCase(unittest.TestCase):
    """把凭据路径打桩到临时目录 —— **绝不碰真实的 `{DATA}/backend.json`**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pair-")
        p = patch.object(cred, "credentials_path",
                         lambda: os.path.join(self.tmp, "backend.json"))
        p.start()
        self.addCleanup(p.stop)
        # 本机设置里万一填着令牌，会让"凭据这条路"整体被绕过 —— 用例要的是后者，
        # 所以明确地把"手填令牌"这条路关掉。
        p2 = patch("app.capabilities.echo_server._token_from_settings", lambda: "")
        p2.start()
        self.addCleanup(p2.stop)

    def stub(self, script):
        s = _Stub(script)
        self.addCleanup(s.stop)
        return s


class WireShapeTests(_CredCase):
    def test_pair_posts_the_documented_path_and_body(self):
        """`POST /v1/pair` + `{code, clientName}`，且配对码规整成大写去空白。

        服务端 `redeem()` 会 `"".join(code.split()).upper()` ——
        用户在面板上抄配对码时带空格/小写是常事，两边都规整才无缝。
        """
        s = self.stub([(200, _PAIR_OK)])
        pairing.pair(s.url, " ab12 -cd34 ", client_name="张三的本")
        req = s.requests[0]
        self.assertEqual(req["path"], "/v1/pair")
        self.assertEqual(req["ctype"], "application/json")
        body = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(body["code"], "AB12-CD34")
        self.assertEqual(body["clientName"], "张三的本")
        self.assertEqual(req["auth"], "", "配对是免凭据端点，不该带 Authorization")

    def test_pair_stores_credentials_where_the_panel_can_read_them(self):
        s = self.stub([(200, _PAIR_OK)])
        creds = pairing.pair(s.url, "ABC123")
        self.assertEqual(creds.client_id, "cli-1")
        self.assertEqual(creds.secret, "shh-1")
        self.assertEqual(creds.server_name, "gpu-01")
        st = pairing.state()
        self.assertTrue(st["paired"])
        self.assertEqual(st["clientId"], "cli-1")
        self.assertEqual(st["baseUrl"], s.url)

    def test_pair_does_not_leave_a_file_behind_when_it_fails(self):
        """**成功之前不写盘**：半路失败留个坏文件，下次启动会表现成"配过了但连不上"。"""
        s = self.stub([(401, {"code": "unauthorized", "message": "配对码无效"})])
        with self.assertRaises(PairingError):
            pairing.pair(s.url, "WRONG")
        self.assertIsNone(cred.load())

    def test_pair_failure_reads_like_a_sentence_with_the_server_code(self):
        s = self.stub([(429, {"code": "rate_limited", "message": "试太多次了",
                              "retryAfter": 42})])
        with self.assertRaises(PairingError) as ctx:
            pairing.pair(s.url, "ABC")
        e = ctx.exception
        self.assertEqual(e.code, "rate_limited")
        self.assertEqual(e.retry_after, 42)
        self.assertIn("试太多次了", str(e))

    def test_bad_address_is_rejected_before_any_network_call(self):
        for bad in ("", "   ", "ftp://x/y", "不是地址"):
            with self.subTest(bad=bad):
                with self.assertRaises(PairingError):
                    pairing.pair(bad, "ABC")

    def test_bare_host_and_port_is_accepted(self):
        """面板上那个输入框，用户十有八九填 `10.100.0.24:8900`。"""
        self.assertEqual(pairing.normalize_base_url("10.100.0.24:8900"),
                         "http://10.100.0.24:8900")
        self.assertEqual(pairing.normalize_base_url(" https://gpu-01:8900/ "),
                         "https://gpu-01:8900")
        self.assertEqual(pairing.normalize_base_url(""), "")

    def test_unreachable_host_says_which_host(self):
        with self.assertRaises(PairingError) as ctx:
            pairing.pair("http://127.0.0.1:9", "ABC")     # 9 = discard，必然连不上
        self.assertEqual(ctx.exception.code, "offline")
        self.assertIn("127.0.0.1:9", str(ctx.exception))

    def test_fetch_token_uses_basic_auth_with_client_id_and_secret(self):
        """Basic 的拼法必须与服务端 `parse_basic` 逐字对齐（它开了 `validate=True`）。"""
        s = self.stub([(200, _TOKEN_OK)])
        c = BackendCredentials(base_url=s.url, client_id="cli-1", secret="shh-1")
        token, expires_in = pairing.fetch_token(c)
        self.assertEqual(token, "jwt-abc")
        self.assertEqual(expires_in, 3600)
        req = s.requests[0]
        self.assertEqual(req["path"], "/v1/token")
        self.assertEqual(req["body"], b"", "换令牌没有请求体")
        raw = req["auth"][len("Basic "):]
        self.assertEqual(base64.b64decode(raw).decode("utf-8"), "cli-1:shh-1")

    def test_ensure_token_reuses_a_fresh_one_and_renews_a_stale_one(self):
        s = self.stub([(200, _TOKEN_OK)])
        c = BackendCredentials(base_url=s.url, client_id="cli-1", secret="shh-1")
        pairing.ensure_token(c)
        pairing.ensure_token(c)
        self.assertEqual(len(s.requests), 1, "新鲜的令牌不该每调一次就换一个")
        c.token_expires_at = time.time() - 1                  # 装作过期
        pairing.ensure_token(c)
        self.assertEqual(len(s.requests), 2, "过期了就该换")

    def test_expires_in_zero_is_treated_as_expired_not_eternal(self):
        """服务端没给 `expiresIn` 时**别把令牌当永久** —— 那会一路撞 401。"""
        s = self.stub([(200, {"accessToken": "jwt-x"})])
        c = BackendCredentials(base_url=s.url, client_id="cli-1", secret="shh-1")
        pairing.ensure_token(c)
        self.assertFalse(c.token_fresh(), "没有期限的令牌不该被当成新鲜的")

    def test_fetch_token_without_credentials_is_a_local_error(self):
        with self.assertRaises(PairingError) as ctx:
            pairing.fetch_token(BackendCredentials(base_url="http://x"))
        self.assertEqual(ctx.exception.code, "absent")

    def test_state_never_carries_the_secret(self):
        s = self.stub([(200, _PAIR_OK)])
        creds = pairing.pair(s.url, "ABC123")
        text = json.dumps(pairing.state(), ensure_ascii=False)
        self.assertNotIn(creds.secret, text)

    def test_unpair_forgets_the_local_credentials(self):
        s = self.stub([(200, _PAIR_OK)])
        pairing.pair(s.url, "ABC123")
        self.assertTrue(pairing.unpair())
        self.assertFalse(pairing.state()["paired"])

    # ---- 令牌与地址的来源顺序 ------------------------------------------------

    def test_a_manually_filled_token_wins_over_the_paired_credentials(self):
        """手填令牌的用途就是"把凭据那一路整个绕开" —— 排障时要能这么干。

        所以顺序是**先到先得、不合并**，而且手填的那个**不自动续期**
        （我们不知道它的期限）。这条也顺带保证：手填时**一个 `/v1/token` 都不会发**。
        """
        from app.capabilities.echo_server import EchoServerClient
        s = self.stub([(200, _PAIR_OK)])
        creds = pairing.pair(s.url, "ABC123")
        c = EchoServerClient(base_url=s.url, token="manual-token", creds=creds)
        self.assertEqual(c._auth_header(), "Bearer manual-token")
        self.assertFalse(c._renewable())
        self.assertEqual(len(s.requests), 1, "只该有配对那一次（不该去换令牌）")
        self.assertEqual(c.describe()["auth"], "manual")

    def test_the_paired_address_is_used_when_the_setting_is_empty(self):
        """**只配对、什么都没配**也得能用 —— 地址随凭据一起来。"""
        from app.capabilities import echo_server as es
        s = self.stub([(200, _PAIR_OK)])
        pairing.pair(s.url, "ABC123")
        with patch.object(es, "_setting", lambda key, default=None: ""):
            self.assertEqual(es.client_from_settings().base_url, s.url)

    def test_an_explicit_address_in_settings_wins(self):
        """设置里填的地址是**显式配置**（"运营让我连这台"），该赢过配对时记下的那个。"""
        from app.capabilities import echo_server as es
        s = self.stub([(200, _PAIR_OK)])
        pairing.pair(s.url, "ABC123")
        with patch.object(es, "_setting",
                          lambda key, default=None: ("http://typed-by-hand:1"
                                                     if key == "capabilityEchoServerUrl"
                                                     else "")):
            self.assertEqual(es.client_from_settings().base_url, "http://typed-by-hand:1")


# ================================================================ 第 2 层：真服务端

_FAKE_SPECS = [
    {"id": "asr-fake", "slot": "asr.long", "impl": "fake", "max_concurrency": 2,
     "modelVersion": "fake-asr-v1", "supports": ["asr.text", "asr.timestamps"]},
]


class _FakeEngine:
    def __init__(self, spec):
        self.spec = spec

    def transcribe(self, wav, lang="auto", timestamps=False):
        return {"text": "真服务端回来了", "sentences": [], "status": "ok"}

    def close(self):
        pass


def _write_wav(path):
    import struct
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % 1600, *([100] * 1600)))
    return path


class RealServerTests(_CredCase):
    """真 uvicorn + **打开鉴权**。这一层是"客户端与服务端真的能配上"唯一的证据。"""

    PORT = 0

    @classmethod
    def setUpClass(cls):
        import uvicorn
        from server import engines, main as server_main, settings as settings_mod
        from app import paths

        # `create_app()` 会全局 Monkey-patch `app.paths`（见 contract 测试里的长说明）——
        # 不还原会连累排在后面的测试文件，而报出来的现象完全指不到这里。
        cls._paths_seam = getattr(paths, "_settings_get", None)

        cls.tmp = tempfile.mkdtemp(prefix="echo-pair-srv-")
        cls.wav_path = _write_wav(os.path.join(cls.tmp, "a.wav"))
        cfg = settings_mod.load()
        cfg.raw["tmp"]["root"] = os.path.join(cls.tmp, "tmp")
        cfg.raw["server"]["state_root"] = os.path.join(cls.tmp, "state")
        cfg.raw["models"]["specs"] = _FAKE_SPECS
        cfg.raw["auth"]["enabled"] = True
        cfg.raw["auth"]["mode"] = "jwt"
        # ≥32 字节：PyJWT 对短密钥会告警（`InsecureKeyLengthWarning`），
        # 而告警混在测试输出里会掩盖真问题
        cfg.raw["auth"]["jwt_secret"] = "test-secret-0123456789abcdef0123456789ab"
        cfg.raw["auth"]["default_scopes"] = "asr diarize embed"

        with patch.object(engines, "build_loaders",
                          lambda device="cuda": {"fake": lambda spec: _FakeEngine(spec)}):
            cls.app = server_main.create_app(cfg)

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        cls.PORT = s.getsockname()[1]
        s.close()
        cls.base_url = "http://127.0.0.1:%d" % cls.PORT
        cls._server = uvicorn.Server(uvicorn.Config(cls.app, host="127.0.0.1",
                                                    port=cls.PORT, log_level="warning"))
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
        from app import paths
        if cls._paths_seam is None:
            try:
                delattr(paths, "_settings_get")
            except AttributeError:
                pass
        else:
            paths._settings_get = cls._paths_seam

    # ---- 工具 --------------------------------------------------------------

    def _pair(self, name="测试机"):
        auth = self.app.state.echo.auth
        code = auth.create_pairing_code(created_by="test", name=name, scopes="asr")
        return pairing.pair(self.base_url, code, client_name="忽略我")

    def _client(self, creds=None):
        from app.capabilities.echo_server import EchoServerClient
        return EchoServerClient(base_url=self.base_url, creds=creds)

    # ---- 用例 --------------------------------------------------------------

    def test_pair_code_name_wins_over_what_the_client_calls_itself(self):
        """管理员在码上写的名字优先于对端自报的（服务端 §7.4 的理由：那份清单是清点）。"""
        creds = self._pair(name="张三的办公本")
        row = self.app.state.echo.auth.store.client(creds.client_id)
        self.assertEqual(row["name"], "张三的办公本")

    def test_pair_then_call_works_with_no_manual_token(self):
        """**这是整条路的验收点**：除了配对码，人什么都不用填。"""
        self._pair()
        c = self._client()
        self.assertTrue(c.refresh(force=True), "capabilities 拉不下来：%s" % c._caps_error)
        self.assertIn("asr.text", c.provides)
        r = c.transcribe(self.wav_path, lang="zh")
        self.assertEqual(r.text, "真服务端回来了")
        self.assertEqual(r.provenance.backend_id, "echo-server")

    def test_a_token_that_the_server_rejects_is_renewed_once_and_the_call_still_works(self):
        """手上那个"看着还新鲜"的令牌被服务端拒了 → **换一次再试一次**。

        真实成因：管理员 `--rotate-secret`（`token_version` +1）、或者两台机器时钟差了几分钟。
        这两种都是"再换一个就好"，不该让用户看到一次失败。
        """
        creds = self._pair()
        c = self._client(creds)
        self.assertTrue(c.refresh(force=True))
        creds.access_token = "stale-but-looks-fresh"      # 过期时间在未来，所以不会提前换
        creds.token_expires_at = time.time() + 3600
        calls = []
        real = pairing.fetch_token
        with patch.object(pairing, "fetch_token",
                          lambda cc, **kw: (calls.append(1), real(cc, **kw))[1]):
            r = c.transcribe(self.wav_path, lang="zh")
        self.assertEqual(r.text, "真服务端回来了")
        self.assertEqual(len(calls), 1, "换了 %d 次 —— 只该换一次" % len(calls))

    def test_a_disabled_client_reports_blocked_with_a_readable_reason(self):
        """被禁用之后**不能一直拿废凭据去撞**：报 `blocked`（不可重试）+ 一句人话。"""
        from app.capabilities.base import CapabilityError
        creds = self._pair()
        self.app.state.echo.auth.set_disabled(creds.client_id, True)
        self.addCleanup(self.app.state.echo.auth.set_disabled, creds.client_id, False)
        c = self._client(creds)
        with self.assertRaises(CapabilityError) as ctx:
            c.transcribe(self.wav_path, lang="zh")
        e = ctx.exception
        self.assertEqual(e.reason, "blocked")
        self.assertFalse(e.retryable, "凭据被拒是可重试的错误吗？重试一万次也还是被拒")
        self.assertTrue(e.detail, "总得给人一句能照着做的话")

    def test_no_credentials_at_all_is_blocked_not_a_crash(self):
        from app.capabilities.base import CapabilityError
        c = self._client(creds=None)
        with self.assertRaises(CapabilityError) as ctx:
            c.transcribe(self.wav_path, lang="zh")
        self.assertEqual(ctx.exception.reason, "blocked")

    def test_a_token_with_chinese_in_it_says_so_instead_of_blaming_the_network(self):
        """令牌里混进中文/全角空格时，urllib 报的是 `'latin-1' codec can't encode…`，
        我们的兜底会把它说成"连不上 <地址>" —— **一个指错方向的诊断**。
        实测踩到过（上一版用例里我自己随手写了句中文件当令牌）。"""
        from app.capabilities.base import CapabilityError
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient(base_url=self.base_url, token="令牌里有中文")
        with self.assertRaises(CapabilityError) as ctx:
            c.transcribe(self.wav_path, lang="zh")
        e = ctx.exception
        self.assertNotEqual(e.reason, "offline", "网络是好的，别让人去查网络")
        self.assertIn("不能放进 HTTP 头", e.detail)

    # ---- 公开端点（跑真服务端时发现的一处设计与实现不一致）------------------

    #: 客户端**不带凭据**就能用的端点（其余一律要 Bearer）。
    #: 起因：设计 §6.1 写着 `/v1/pair` 是"唯一免凭据的端点"，实际这三个也是敞开的。
    #:
    #: 查下来结论是**敞开是对的，设计那句话写窄了**：
    #:   * `health`/`ready` 是给 Docker healthcheck 与监控的探针 —— 要它们带令牌，
    #:     等于让探针也配一套凭据，而配错的表现是"服务端显示不健康"，比 401 更难查；
    #:   * `capabilities` 得在**配对之前**就能看：面板要先告诉人"这台能干什么"，
    #:     而且它只有模型名/限制/忙闲，没有业务数据。
    #: `/v1/pair` 仍然是**唯一免凭据的写端点**，所以它单独防猜（限速 + 退避 + 可关闭）。
    PUBLIC = {"/v1/health", "/v1/ready", "/v1/capabilities"}

    #: 免凭据、但**要配对码**的端点（唯一一个）。它靠"一次性码 + 失败退避 + 可整个关掉"
    #: 防猜，而不是靠 Bearer —— 它本来就得在"还没有凭据"的时候被调用。
    #: 所以对它只要求一件事：**匿名不能成功**。
    CODE_GUARDED = {"/v1/pair"}

    def test_every_other_endpoint_rejects_an_anonymous_request(self):
        """**遍历路由表**，而不是逐个记死路径。

        GPU 活必须带凭据，这条不能靠"我记得给新端点接上鉴权"。以后有人加一个
        业务端点忘了接 `client_of`，这里会红。
        """
        import urllib.error
        import urllib.request
        # 从 OpenAPI 里取端点，而不是遍历 `app.routes`：新版 FastAPI 把 `include_router`
        # 的结果包成 `_IncludedRouter`（`path=None`），直接遍历会**一个端点都枚举不到**
        # —— 那样这条用例就成了空转的绿灯。（实测踩到过。）
        paths = sorted(p for p in (self.app.openapi().get("paths") or {})
                       if p.startswith("/v1/"))
        self.assertTrue(paths, "一个 /v1 端点都没枚举到？那这条用例是空转的")

        leaked = []
        for p in paths:
            if p in self.PUBLIC:
                continue
            req = urllib.request.Request(self.base_url + p, data=b"", method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = resp.status
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    e.read()
                except Exception:
                    pass
            if p in self.CODE_GUARDED:
                if status == 200:
                    leaked.append((p, status))
                continue
            if status not in (401, 403):
                leaked.append((p, status))
        self.assertEqual(leaked, [],
                         "这些端点匿名可达（状态码不是 401/403）：%s\n"
                         "要么给它接上 client_of(need_scope=…)，要么把它写进 "
                         "设计 §6.1 的公开端点表并说明为什么可以敞开" % (leaked,))

    def test_the_documented_public_endpoints_are_actually_reachable(self):
        """反过来也要钉：公开表里写了的，**必须真能匿名用** —— 否则探针会失败。"""
        import urllib.request
        for p in sorted(self.PUBLIC):
            with self.subTest(path=p):
                with urllib.request.urlopen(self.base_url + p, timeout=10) as resp:
                    self.assertEqual(resp.status, 200, p)
                    resp.read()

    def test_describe_says_where_the_token_comes_from(self):
        """面板要能回答"为什么这台连不上"：是没配对、手填了令牌，还是配对好了。"""
        c = self._client(creds=None)
        self.assertEqual(c.describe()["auth"], "none")
        self.assertFalse(c.describe()["paired"])
        c2 = self._client(self._pair())
        self.assertEqual(c2.describe()["auth"], "paired")
        self.assertTrue(c2.describe()["paired"])


if __name__ == "__main__":
    unittest.main()
