# -*- coding: utf-8 -*-
"""TLS：服务端配置 + 客户端**证书固定**（设计 §7.5 ①、§13 的 TLS）。

设计原话是"配对串里带证书指纹，客户端记下它，之后连这个后端就校验它 —— **用户不需要
去装自签证书的根**"。这一组用例把这句话拆成可执行的三件事：

  1. **配对时把证书取回来并固定**（TOFU；给了 `fp=` 就一定要对上，那才是防中间人）；
  2. **之后每次连接都只认这张证书** —— 固定了 A 的客户端连不上只持 B 的服务端；
  3. **https 而没有固定证书 → 直接拒绝**，绝不静默跳过校验。

第 3 条是这里最要紧的一条：静默跳过校验的坏处不是"不安全"，而是**从现象上看不出来**
（照样能用）。所以它必须是一条会红的用例，而不是一段注释。

证书是内嵌的测试专用自签证书（`tests/tls_test_cert.py`），于是这一组用例
**不需要 openssl、也不需要 cryptography** —— 门禁不该为一个 TLS 用例多一个依赖。
"""
import http.server
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import credentials as cred              # noqa: E402
from app.capabilities import pairing                          # noqa: E402
from app.capabilities.credentials import BackendCredentials   # noqa: E402
from app.capabilities.pairing import PairingError             # noqa: E402
from tests.tls_test_cert import CERT_PEM, KEY_PEM, OTHER_CERT_PEM  # noqa: E402

_PAIR_OK = {"clientId": "cli-tls", "secret": "shh-tls", "name": "TLS 测试机",
            "scopes": ["asr"], "serverName": "gpu-tls", "protocol": 1}


class _Handler(http.server.BaseHTTPRequestHandler):
    """只回答 `/v1/pair` 与 `/v1/token`，够这条链路用。"""

    def _reply(self, payload, status=200, headers=None):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):                                         # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length) if length else b""
        if self.path.endswith("/v1/pair"):
            self._reply(_PAIR_OK)
        elif self.path.endswith("/v1/token"):
            self._reply({"accessToken": "jwt-tls", "expiresIn": 3600, "clientId": "cli-tls"})
        else:
            self._reply({"code": "not_found"}, 404)

    def log_message(self, *a):                                 # 安静
        pass


class _HttpsServer:
    """一个真 https 服务端（用内嵌的测试证书）。"""

    def __init__(self, cert_pem=CERT_PEM, key_pem=KEY_PEM):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cert = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8")
        key = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8")
        cert.write(cert_pem)
        key.write(key_pem)
        cert.close()
        key.close()
        self._files = (cert.name, key.name)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert.name, keyfile=key.name)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.url = "https://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        # Windows 上 `shutdown()` + `server_close()` 撞在一起会抛 WinError 10038
        # （在一个非套接字上操作）—— 它不影响结论，但会把门禁输出弄脏，所以兜住。
        for fn in (self.httpd.shutdown, self.httpd.server_close):
            try:
                fn()
            except OSError:
                pass
        for p in self._files:
            try:
                os.remove(p)
            except OSError:
                pass


class FingerprintTests(unittest.TestCase):
    def test_it_matches_the_openssl_style_format(self):
        fp = pairing.fingerprint_of(CERT_PEM)
        self.assertTrue(fp.startswith("sha256:"), fp)
        self.assertEqual(len(fp.split(":")[1]), 64, "应该是 sha256 的十六进制")

    def test_normalize_accepts_what_people_actually_paste(self):
        """人抄指纹时会带冒号、大写、`SHA256:` 前缀 —— 都要认。"""
        fp = pairing.fingerprint_of(CERT_PEM)
        hexpart = fp.split(":")[1]
        coloned = ":".join(hexpart[i:i + 2] for i in range(0, len(hexpart), 2))
        for form in (fp, fp.upper(), "SHA256:" + hexpart, coloned, coloned.upper(),
                     "  " + fp + "  "):
            with self.subTest(form=form[:20]):
                self.assertEqual(pairing.normalize_fingerprint(form), fp)

    def test_garbage_fingerprint_is_empty_not_a_wrong_match(self):
        self.assertEqual(pairing.normalize_fingerprint(""), "")
        self.assertNotEqual(pairing.normalize_fingerprint("sha256:"), "sha256:")

    def test_it_never_raises_on_a_broken_pem(self):
        self.assertEqual(pairing.fingerprint_of("这不是证书"), "")
        self.assertEqual(pairing.fingerprint_of(""), "")
        self.assertEqual(pairing.fingerprint_of(None), "")


class PinIsMandatoryTests(unittest.TestCase):
    """**https 而没有固定证书 = 拒绝连接。**"""

    def test_the_context_refuses_to_be_built_without_a_certificate(self):
        with self.assertRaises(PairingError) as ctx:
            pairing.pinned_context("")
        self.assertEqual(ctx.exception.code, "blocked")
        self.assertIn("拒绝连接", str(ctx.exception))

    def test_the_capability_client_refuses_too(self):
        """能力调用那条路同样拒绝 —— 而且要说人话（不是"连不上"）。"""
        from app.capabilities.base import CapabilityError
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient(base_url="https://backend.example:8900", token="t", creds=False)
        with self.assertRaises(CapabilityError) as ctx:
            c._request("GET", "/v1/health")
        self.assertEqual(ctx.exception.reason, "blocked")
        self.assertIn("https", ctx.exception.detail)

    def test_http_needs_no_certificate(self):
        from app.capabilities.echo_server import EchoServerClient
        c = EchoServerClient(base_url="http://backend.example:8900", token="t", creds=False)
        self.assertIsNone(c._ssl_context())


class PinningEndToEndTests(unittest.TestCase):
    """真 https 服务端 + 真握手。**这一层的意义是"固定真的在生效"。**"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-tls-")
        p = patch.object(cred, "credentials_path",
                         lambda: os.path.join(self.tmp, "backend.json"))
        p.start()
        self.addCleanup(p.stop)
        p2 = patch("app.capabilities.echo_server._token_from_settings", lambda: "")
        p2.start()
        self.addCleanup(p2.stop)
        self.srv = _HttpsServer()
        self.addCleanup(self.srv.stop)

    def test_pairing_over_https_pins_the_certificate(self):
        """配对顺带把证书固定下来，之后**不需要装任何根证书**。"""
        creds = pairing.pair(self.srv.url, "ABC123")
        self.assertEqual(creds.cert_fingerprint, pairing.fingerprint_of(CERT_PEM))
        self.assertIn("BEGIN CERTIFICATE", creds.cert_pem)
        stored = cred.load()
        self.assertEqual(stored.cert_pem.strip(), CERT_PEM.strip(), "证书没落盘")
        self.assertEqual(stored.cert_fingerprint, creds.cert_fingerprint)

    def test_a_matching_expected_fingerprint_is_accepted(self):
        fp = pairing.fingerprint_of(CERT_PEM)
        creds = pairing.pair(self.srv.url, "ABC123", cert_fingerprint=fp)
        self.assertEqual(creds.cert_fingerprint, fp)

    def test_a_mismatched_expected_fingerprint_stops_the_pairing(self):
        """**这才是防中间人的那一步**：配对串里带了指纹就必须对上。"""
        with self.assertRaises(PairingError) as ctx:
            pairing.pair(self.srv.url, "ABC123",
                         cert_fingerprint=pairing.fingerprint_of(OTHER_CERT_PEM))
        text = str(ctx.exception)
        self.assertIn("中间人", text)
        self.assertIsNone(cred.load(), "指纹对不上却把凭据写下去了")

    def test_a_client_pinned_to_another_certificate_cannot_connect(self):
        """固定了 A 的客户端连不上只持 B 的服务端 —— 固定不是装饰。"""
        c = BackendCredentials(base_url=self.srv.url, client_id="cli-tls",
                               secret="shh-tls", cert_pem=OTHER_CERT_PEM)
        with self.assertRaises(PairingError) as ctx:
            pairing.fetch_token(c)
        self.assertEqual(ctx.exception.code, "blocked")
        self.assertIn("证书校验失败", str(ctx.exception))

    def test_a_capability_call_succeeds_with_the_pinned_certificate(self):
        """正向也要有：固定对了就能真连上（否则上面那些拒绝可能是"反正都连不上"）。"""
        creds = pairing.pair(self.srv.url, "ABC123")
        token, expires_in = pairing.fetch_token(creds)
        self.assertEqual(token, "jwt-tls")
        self.assertEqual(expires_in, 3600)

    def test_pairing_over_plain_http_still_works(self):
        """http 的老路不许因为 TLS 的改动而坏掉。"""
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        def _down():
            for fn in (srv.shutdown, srv.server_close):
                try:
                    fn()
                except OSError:
                    pass

        self.addCleanup(_down)
        creds = pairing.pair("http://127.0.0.1:%d" % port, "ABC123")
        self.assertEqual(creds.client_id, "cli-tls")
        self.assertEqual(creds.cert_pem, "", "http 不该去取证书")
        self.assertEqual(creds.cert_fingerprint, "")


if __name__ == "__main__":
    unittest.main()
