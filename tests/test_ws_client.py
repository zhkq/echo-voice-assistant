# -*- coding: utf-8 -*-
"""`app/capabilities/wsclient.py` 的自测：**一个桩 WebSocket 服务端**跑真 socket。

为什么值得这么写：这个模块是"自己实现协议"，而协议实现的错误**在真服务端上表现为
各种莫名其妙的断连**（掩码少了、长度分档写错、粘包没处理、控制帧当成消息）。
所以这一组用例不测"函数返回了什么"，而是**两边真的握一次手、真的收发帧**。

桩服务端只实现另一半必需的东西（握手 + 帧），刻意**写得笨一点**：它按 RFC 逐字节
拼/解，而不是复用被测代码 —— 复用就变成了"自己跟自己一致"，测不出协议错。

内网那台真服务的协议（帧格式 / 是否先发 JSON 配置 / 采样率）**还没确认**
（设计 §8.2.2），所以这里只验**传输层**；消息语义留给「网络服务商」适配器。
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import wsclient                                  # noqa: E402
from app.capabilities.wsclient import (OP_BINARY, OP_CLOSE, OP_CONT,   # noqa: E402
                                       OP_PING, OP_PONG, OP_TEXT, WebSocket, WsError)
from tests.tls_test_cert import CERT_PEM, KEY_PEM                      # noqa: E402


class _StubServer:
    """桩服务端。`on_open(conn)` 里决定这次怎么应答。

    刻意**不复用** `wsclient` 的拼帧代码：那等于拿被测实现去验被测实现。
    """

    def __init__(self, on_open, *, tls=False, bad_accept=False, send_masked=False):
        self.on_open = on_open
        self.bad_accept = bad_accept
        self.send_masked = send_masked
        self.received = []            # [(opcode, payload), ...]，用例可以断言客户端发了什么
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._ctx = None
        self._tmp = []
        if tls:
            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            import tempfile
            c = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8")
            k = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="utf-8")
            c.write(CERT_PEM)
            k.write(KEY_PEM)
            c.close()
            k.close()
            self._tmp = [c.name, k.name]
            self._ctx.load_cert_chain(certfile=c.name, keyfile=k.name)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    # ---- 服务端侧的帧处理（独立实现，见类文档） ----

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        if self._ctx is not None:
            try:
                conn = self._ctx.wrap_socket(conn, server_side=True)
            except ssl.SSLError:
                return
        try:
            self._handshake(conn)
            self.on_open(_ServerConn(conn, self))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                raise WsError("客户端没发完握手")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        key = ""
        for line in head.decode("latin-1").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "sec-websocket-key":
                    key = v.strip()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if self.bad_accept:
            accept = "wrong-accept-value"
        resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode()
        conn.sendall(resp)
        self._rest = rest

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for p in self._tmp:
            try:
                os.remove(p)
            except OSError:
                pass


class _ServerConn:
    """桩服务端这一侧的连接（同一套逐字节实现）。"""

    def __init__(self, conn, server):
        self.conn = conn
        self.server = server
        self.buf = getattr(server, "_rest", b"")

    def send(self, opcode, payload=b"", *, fin=True, mask=False):
        b0 = (0x80 if fin else 0) | opcode
        n = len(payload)
        head = bytearray([b0])
        if n < 126:
            head.append((0x80 if mask else 0) | n)
        elif n < 65536:
            head.append((0x80 if mask else 0) | 126)
            head += struct.pack("!H", n)
        else:
            head.append((0x80 if mask else 0) | 127)
            head += struct.pack("!Q", n)
        body = payload
        if mask:
            m = b"\x01\x02\x03\x04"
            head += m
            body = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
        self.conn.sendall(bytes(head) + body)

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise WsError("客户端关了")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b0, b1 = self._exact(2)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._exact(8))[0]
        mask = self._exact(4) if masked else b""
        payload = self._exact(n)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.server.received.append((opcode, payload, masked))
        return opcode, payload, masked


class HandshakeTests(unittest.TestCase):
    def _client(self, srv, **kw):
        ws = WebSocket("ws://127.0.0.1:%d/ws/" % srv.port, timeout=5, **kw)
        return ws

    def test_it_connects_and_reads_a_text_message(self):
        def on_open(c):
            c.send(OP_TEXT, "你好".encode("utf-8"))
        srv = _StubServer(on_open)
        self.addCleanup(srv.stop)
        with self._client(srv) as ws:
            op, data = ws.recv_message()
        self.assertEqual(op, OP_TEXT)
        self.assertEqual(data.decode("utf-8"), "你好")

    def test_a_wrong_accept_key_is_refused(self):
        """accept 对不上 = 对面不是 websocket（或有人在改包）——**必须拒**，不能将就。"""
        srv = _StubServer(lambda c: None, bad_accept=True)
        self.addCleanup(srv.stop)
        with self.assertRaises(WsError) as ctx:
            self._client(srv).connect()
        self.assertIn("Accept", str(ctx.exception))

    def test_a_non_101_response_is_refused(self):
        def on_open(c):
            pass
        srv = _StubServer(on_open)
        self.addCleanup(srv.stop)
        # 直接连一个"不是 websocket 的服务端"：拿一个只会回 HTTP 的 socket 冒充
        plain = socket.socket()
        plain.bind(("127.0.0.1", 0))
        plain.listen(1)
        port = plain.getsockname()[1]

        def serve():
            conn, _ = plain.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            conn.close()
            plain.close()

        threading.Thread(target=serve, daemon=True).start()
        with self.assertRaises(WsError) as ctx:
            WebSocket("ws://127.0.0.1:%d/x" % port, timeout=5).connect()
        self.assertIn("没有升到 websocket", str(ctx.exception))

    def test_bad_scheme_and_missing_host_are_reported(self):
        for url in ("http://x/y", "ws:///ws/", ""):
            with self.subTest(url=url):
                with self.assertRaises(WsError):
                    WebSocket(url, timeout=1).connect()

    def test_wss_without_a_context_is_refused(self):
        """**不静默跳过证书校验**（与 pairing 同一条纪律）。"""
        srv = _StubServer(lambda c: None, tls=True)
        self.addCleanup(srv.stop)
        with self.assertRaises(WsError) as ctx:
            WebSocket("wss://127.0.0.1:%d/ws/" % srv.port, timeout=5).connect()
        self.assertIn("ssl_context", str(ctx.exception))


class FrameTests(unittest.TestCase):
    def _roundtrip(self, on_open, *, client=None):
        srv = _StubServer(on_open)
        self.addCleanup(srv.stop)
        ws = WebSocket("ws://127.0.0.1:%d/ws/" % srv.port, timeout=5)
        ws.connect()
        self.addCleanup(ws.close)
        (client or (lambda w: None))(ws)
        return srv, ws

    def test_binary_roundtrip_and_masking(self):
        """客户端发的帧**必须带掩码**（RFC 6455 §5.3）—— 桩这边直接看掩码位。"""
        def on_open(c):
            op, payload, masked = c.recv()
            c.send(OP_BINARY, payload[::-1])
        srv, ws = self._roundtrip(on_open, client=lambda w: w.send_binary(b"\x01\x02\x03"))
        op, data = ws.recv_message()
        self.assertEqual((op, data), (OP_BINARY, b"\x03\x02\x01"))
        self.assertTrue(srv.received[0][2], "客户端发的帧没掩码")

    def test_payload_length_boundaries(self):
        """125 / 126 / 65536 三个分档各验一次 —— 长度编码写错只会在某一档上炸。"""
        for n in (0, 125, 126, 65535, 65536):
            with self.subTest(n=n):
                def on_open(c, n=n):
                    op, payload, _ = c.recv()
                    c.send(OP_BINARY, struct.pack("!I", len(payload)))
                payload = bytes(n)
                srv, ws = self._roundtrip(on_open, client=lambda w: w.send_binary(payload))
                op, data = ws.recv_message()
                self.assertEqual(struct.unpack("!I", data)[0], n)

    def test_fragmented_messages_are_reassembled(self):
        def on_open(c):
            c.send(OP_TEXT, b"part1", fin=False)
            c.send(OP_CONT, b"part2", fin=False)
            c.send(OP_CONT, b"part3", fin=True)
        srv, ws = self._roundtrip(on_open)
        op, data = ws.recv_message()
        self.assertEqual((op, data), (OP_TEXT, b"part1part2part3"))

    def test_a_ping_is_answered_with_a_pong(self):
        seen = {}

        def on_open(c):
            c.send(OP_PING, b"hi")
            op, payload, _ = c.recv()             # 应该等到 pong
            seen["op"] = op
            seen["payload"] = payload
            c.send(OP_TEXT, b"done")

        srv, ws = self._roundtrip(on_open)
        op, data = ws.recv_message()
        self.assertEqual((op, data), (OP_TEXT, b"done"))
        self.assertEqual(seen["op"], OP_PONG)
        self.assertEqual(seen["payload"], b"hi")

    def test_a_masked_server_frame_is_a_protocol_error(self):
        """服务端**不许**掩码。真收到就断开 —— 继续读只会把后面的字节解释错。"""
        def on_open(c):
            c.send(OP_TEXT, b"oops", mask=True)
        srv, ws = self._roundtrip(on_open)
        with self.assertRaises(WsError) as ctx:
            ws.recv_message()
        self.assertIn("掩码", str(ctx.exception))

    def test_a_server_close_is_reported_with_its_code(self):
        def on_open(c):
            c.send(OP_CLOSE, struct.pack("!H", 1001) + b"going away")
        srv, ws = self._roundtrip(on_open)
        with self.assertRaises(WsError) as ctx:
            ws.recv_message()
        self.assertEqual(ctx.exception.code, 1001)
        self.assertIn("going away", str(ctx.exception))

    def test_an_oversized_frame_is_refused_before_allocating(self):
        """**收到大帧头不许照着分配**：那是"一个坏服务端把客户端 OOM 掉"的经典路径。"""
        def on_open(c):
            c.send(OP_BINARY, b"x" * 4096)
        srv = _StubServer(on_open)
        self.addCleanup(srv.stop)
        ws = WebSocket("ws://127.0.0.1:%d/ws/" % srv.port, timeout=5, max_frame=64)
        ws.connect()
        self.addCleanup(ws.close)
        with self.assertRaises(WsError) as ctx:
            ws.recv_message()
        self.assertIn("超过上限", str(ctx.exception))

    def test_a_continuation_without_a_start_is_a_protocol_error(self):
        def on_open(c):
            c.send(OP_CONT, b"no-start", fin=True)
        srv, ws = self._roundtrip(on_open)
        with self.assertRaises(WsError):
            ws.recv_message()

    def test_rsv_bits_are_refused(self):
        """我们没协商任何扩展，RSV 位被置就是协议错（否则会把扩展数据当载荷）。"""
        def on_open(c):
            c.conn.sendall(bytes([0x80 | 0x40 | OP_TEXT, 0x01]) + b"x")
        srv, ws = self._roundtrip(on_open)
        with self.assertRaises(WsError) as ctx:
            ws.recv_message()
        self.assertIn("RSV", str(ctx.exception))

    def test_silence_times_out_with_a_readable_message(self):
        def on_open(c):
            import time
            time.sleep(1.5)
        srv = _StubServer(on_open)
        self.addCleanup(srv.stop)
        ws = WebSocket("ws://127.0.0.1:%d/ws/" % srv.port, timeout=0.3)
        ws.connect()
        self.addCleanup(ws.close)
        with self.assertRaises(WsError) as ctx:
            ws.recv_message()
        self.assertIn("超时", str(ctx.exception))

    def test_close_is_idempotent(self):
        """`finally: ws.close()` 很常见 —— 它不该因为已经关过就抛。"""
        srv, ws = self._roundtrip(lambda c: None)
        ws.close()
        ws.close()
        self.assertIsNone(ws.sock)


class WssTests(unittest.TestCase):
    """`wss://` 用固定证书真握一次手（与 pairing 的证书固定是同一条路）。"""

    def test_it_connects_when_the_context_pins_the_server_certificate(self):
        from app.capabilities import pairing
        ctx = pairing.pinned_context(CERT_PEM)

        def on_open(c):
            c.send(OP_TEXT, b"secure")

        srv = _StubServer(on_open, tls=True)
        self.addCleanup(srv.stop)
        ws = WebSocket("wss://127.0.0.1:%d/ws/" % srv.port, timeout=5, ssl_context=ctx)
        ws.connect()
        self.addCleanup(ws.close)
        op, data = ws.recv_message()
        self.assertEqual(data, b"secure")

    def test_a_context_pinned_to_another_certificate_fails(self):
        from app.capabilities import pairing
        from tests.tls_test_cert import OTHER_CERT_PEM
        ctx = pairing.pinned_context(OTHER_CERT_PEM)
        srv = _StubServer(lambda c: None, tls=True)
        self.addCleanup(srv.stop)
        with self.assertRaises(WsError) as ctx_exc:
            WebSocket("wss://127.0.0.1:%d/ws/" % srv.port, timeout=5,
                      ssl_context=ctx).connect()
        self.assertIn("TLS 握手失败", str(ctx_exc.exception))


class NothingImportsItYetTests(unittest.TestCase):
    """**这个模块现在是独立的**（设计 §8.2.2：真协议还没确认）。

    这条用例不是"防别人用"，而是把那句状态**写成可执行的**：等「网络服务商」适配器
    接上它时，这条会红 —— 那时顺手把它删掉，并在适配器的提交里说明协议已确认。
    没有它的话，"还没接"这件事只存在于文档里，而文档会漂。
    """

    def test_no_module_in_app_or_server_imports_wsclient(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for base in ("app", "server"):
            for dirpath, _dirs, files in os.walk(os.path.join(root, base)):
                if "__pycache__" in dirpath:
                    continue
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, fn)
                    if os.path.basename(path) == "wsclient.py":
                        continue
                    with open(path, encoding="utf-8") as fh:
                        src = fh.read()
                    if "wsclient" in src:
                        hits.append(os.path.relpath(path, root))
        self.assertEqual(hits, [],
                         "有人 import 了 wsclient —— 说明网络服务商适配器接上了？"
                         "那请把这条用例删掉，并在提交里写明真协议已确认（设计 §8.2.2）")


if __name__ == "__main__":
    unittest.main()
