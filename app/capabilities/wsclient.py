# -*- coding: utf-8 -*-
"""最小 WebSocket 客户端（RFC 6455 的客户端那一半），**只用 stdlib**。

## 为什么要自己写

设计 §8.2 已经更正：内网那台公共 ASR 是 **WebSocket 流式**（不是 OpenAI 兼容），
而客户端**不能为它引一个第三方库** —— 铁律 L1 是"默认档装机要短、不许有编译步骤"，
为一个可选后端（§8.2.1 的岔路）把依赖树撑大，方向不对。

代价是这里的协议细节得自己担。所以它配了一个**自测的桩服务端**
（`tests/test_ws_client.py`），把握手、掩码、分片、ping/pong、关闭、长度分档都验一遍。

## 一条纪律：**没验过的协议实现不该进主链路**

这个模块**目前是独立的**：没有任何地方 import 它。等同事把真协议确认下来
（帧格式 / 是否先发一条 JSON 配置 / 采样率与声道 / 给不给时间戳，见设计 §8.2.2），
再由「网络服务商」适配器用它 —— 那时会顺手删掉"没人 import 它"那条护栏用例。

## 它与 `pairing` 的关系

`wss://` 时调用方传一个**固定的证书上下文**（`pairing.pinned_context`）。
这个模块不认识 `pairing`，也不知道什么叫"配对"—— 它只收一个 `ssl.SSLContext`。
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Dict, List, Optional, Tuple

#: 帧类型（RFC 6455 §5.2）
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: 握手用的魔数（RFC 6455 §1.3）——**不能改**，服务端按它算 accept。
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: 单帧上限。默认 8 MB：够放一段几十秒的 16 kHz 音频，又不至于让一个坏服务端
#: 让我们去分配任意大的内存（"收到 1 GB 的帧头就照着分配"是这类实现的经典坑）。
MAX_FRAME_BYTES = 8 * 1024 * 1024


class WsError(Exception):
    """WebSocket 层的失败。**带一句能读的话** —— 上层要原样报给用户。"""

    def __init__(self, message: str, *, code: int = 0):
        super().__init__(message)
        self.code = int(code or 0)


def accept_key(client_key: str) -> str:
    """客户端 key → 服务端该回的 `Sec-WebSocket-Accept`。"""
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocket:
    """一个连接。**同步阻塞**（与能力层其余部分一致：并发由调用方组织）。

    典型用法::

        ws = WebSocket("wss://10.100.0.24:10095/ws/", ssl_context=ctx)
        ws.connect()
        ws.send_text('{"mode":"offline"}')
        ws.send_binary(chunk)
        op, data = ws.recv_message()
        ws.close()
    """

    def __init__(self, url: str, *, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 10.0, max_frame: int = MAX_FRAME_BYTES,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.url = str(url or "")
        self.timeout = float(timeout)
        self.max_frame = int(max_frame)
        self.ssl_context = ssl_context
        self.headers = dict(headers or {})
        self.sock: Optional[socket.socket] = None
        self._buf = b""                    # 握手之后可能已经粘了一部分帧数据
        self._closed = False
        self._close_code = 0
        self._close_reason = ""

    # ---------------------------------------------------------------- 连接

    def connect(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise WsError("只认 ws:// 与 wss://，收到的是 %r" % self.url)
        host = parts.hostname or ""
        if not host:
            raise WsError("地址里没有主机名：%r" % self.url)
        port = int(parts.port or (443 if parts.scheme == "wss" else 80))
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ["GET %s HTTP/1.1" % path,
               "Host: %s:%d" % (host, port),
               "Upgrade: websocket",
               "Connection: Upgrade",
               "Sec-WebSocket-Key: %s" % key,
               "Sec-WebSocket-Version: 13"]
        for k, v in self.headers.items():
            req.append("%s: %s" % (k, v))
        raw = ("\r\n".join(req) + "\r\n\r\n").encode("utf-8")

        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as e:
            raise WsError("连不上 %s:%d（%s）" % (host, port, e)) from None
        if parts.scheme == "wss":
            if self.ssl_context is None:
                # **不静默跳过校验**（与 pairing 同一条纪律）：没给上下文宁可失败。
                sock.close()
                raise WsError("wss:// 必须给一个 ssl_context（不静默跳过证书校验）")
            try:
                sock = self.ssl_context.wrap_socket(sock, server_hostname=host)
            except ssl.SSLError as e:
                sock.close()
                raise WsError("TLS 握手失败（%s）—— 证书对不上？" % e) from None
        sock.settimeout(self.timeout)
        self.sock = sock
        try:
            sock.sendall(raw)
            head, rest = self._read_handshake()
        except Exception:
            self.close()
            raise
        if " 101" not in head.split("\r\n")[0]:
            self.close()
            raise WsError("服务端没有升到 websocket：%s" % head.split("\r\n")[0].strip())
        got = ""
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "sec-websocket-accept":
                    got = v.strip()
        if got != accept_key(key):
            # 服务端回的 accept 不对 = 对面不是 websocket（或中间有人在改包）。
            self.close()
            raise WsError("Sec-WebSocket-Accept 对不上 —— 对面不是 websocket，或有人在中间改包")
        self._buf = rest

    def _read_handshake(self) -> Tuple[str, bytes]:
        """读到 `\\r\\n\\r\\n` 为止。**多读到的字节要留下**（那是帧数据，丢了就错位）。"""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._recv(4096)
            if not chunk:
                raise WsError("握手中途连接被关掉")
            data += chunk
            if len(data) > 64 * 1024:
                raise WsError("握手响应太大（>64 KB）—— 对面不像 websocket 服务端")
        head, _, rest = data.partition(b"\r\n\r\n")
        return head.decode("latin-1"), rest

    # ---------------------------------------------------------------- 收发

    def send_text(self, text: str) -> None:
        self.send_frame(OP_TEXT, str(text).encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self.send_frame(OP_BINARY, bytes(data))

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """发一帧。**客户端发的帧必须掩码**（RFC 6455 §5.3）——不掩码服务端会直接关连接。"""
        if self.sock is None:
            raise WsError("还没连接")
        payload = bytes(payload)
        if len(payload) > self.max_frame:
            raise WsError("要发的帧太大（%d 字节 > 上限 %d）" % (len(payload), self.max_frame))
        head = bytearray([0x80 | (opcode & 0x0F)])       # FIN=1（不分片发）
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def recv_message(self) -> Tuple[int, bytes]:
        """收一条**完整的**消息，返回 `(opcode, payload)`。

        控制帧就地处理（ping → 立刻回 pong），分片在这里拼起来 —— 上层只看到整条消息。
        """
        chunks: List[bytes] = []
        first_op = 0
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._close_code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0
                self._close_reason = payload[2:].decode("utf-8", "replace")
                self._closed = True
                self.close()
                raise WsError("服务端关闭了连接（code=%d %s）"
                              % (self._close_code, self._close_reason), code=self._close_code)
            if opcode == OP_CONT:
                if not chunks:
                    raise WsError("先收到续帧（没有开头帧）—— 协议错")
                chunks.append(payload)
            else:
                if chunks:
                    raise WsError("上一帧还是分片的，又来了一条新消息 —— 协议错")
                first_op = opcode
                chunks.append(payload)
            if fin:
                return first_op, b"".join(chunks)

    def _read_frame(self) -> Tuple[bool, int, bytes]:
        b0, b1 = self._recv_exact(2)
        fin = bool(b0 & 0x80)
        if b0 & 0x70:
            raise WsError("帧头里 RSV 位被置了（我们没协商扩展）—— 协议错")
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._recv_exact(8))[0]
        if n > self.max_frame:
            raise WsError("服务端要发 %d 字节的帧，超过上限 %d —— 断开" % (n, self.max_frame))
        if masked:
            # 服务端**不许**掩码（RFC 6455 §5.1）。真收到就断开：说明对面不是合规实现，
            # 继续读下去只会把后面的字节解释错。
            raise WsError("服务端发来了带掩码的帧 —— 协议错")
        if opcode >= OP_CLOSE and (not fin or n > 125):
            raise WsError("控制帧不许分片、载荷不许超过 125 字节 —— 协议错")
        return fin, opcode, self._recv_exact(n)

    def _recv(self, n: int) -> bytes:
        if self.sock is None:
            raise WsError("还没连接")
        try:
            return self.sock.recv(n)
        except socket.timeout:
            raise WsError("等数据超时（%.1f 秒）" % self.timeout) from None
        except OSError as e:
            raise WsError("连接出错：%s" % e) from None

    def _recv_exact(self, n: int) -> bytes:
        """**从缓冲区里先取**（握手粘包、以及一次 recv 里可能有多帧）。"""
        while len(self._buf) < n:
            chunk = self._recv(max(1, n - len(self._buf)))
            if not chunk:
                raise WsError("连接被对端关掉了（还差 %d 字节）" % (n - len(self._buf)))
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    # ---------------------------------------------------------------- 关闭

    def close(self, code: int = 1000) -> None:
        """发关闭帧并关掉 socket。**重复调用是安全的**（finally 里通常会再调一次）。"""
        if self.sock is None:
            return
        try:
            if not self._closed:
                self.send_frame(OP_CLOSE, struct.pack("!H", int(code)))
        except Exception:
            pass                      # 关连接失败没什么可做的，也不该盖住原来的错
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
        self._buf = b""

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
