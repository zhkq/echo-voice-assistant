# -*- coding: utf-8 -*-
"""gh-proxy.py - minimal CONNECT proxy for pushing to GitHub through DNS pollution.

WHY THIS EXISTS
  The corporate network blackholes the bare `github.com` name, so a plain `git push`
  cannot even resolve. scripts/gh-push.ps1 first tries
  `git -c http.curloptResolve=github.com:443:<ip>`, which keeps SNI but requires git to
  connect STRAIGHT to the IP. On this network that path is flaky: the probe answers 401
  and the real connection still dies with "Failed to connect to github.com port 443"
  (hit repeatedly on 2026-09-21). This proxy is the fallback for that case.

  It tunnels `CONNECT github.com:443` to a reachable GitHub front IP. The tunnel is raw
  TCP, so TLS - and therefore SNI and the certificate - stay end-to-end github.com; only
  the TCP target moves. A raw-IP push is NOT a substitute: GitHub answers 302 back to the
  canonical host and git resolves the blocked name again.

  Same technique as the `github-dns-bypass-push` skill; it lives here as Python because
  the repo is a Python app and always has an interpreter, while node is not a dependency
  of ECHO (the skill's managed node path has already rotated once).

USAGE (gh-push.ps1 starts it for you)
  python scripts/gh-proxy.py <bind_port> <target_ip> [host]
  git -c http.proxy=http://127.0.0.1:<bind_port> push origin main

  Only `host` (default github.com) is redirected to <target_ip>; every other CONNECT
  target is connected normally, so this cannot silently reroute unrelated traffic.

ASCII-only, same rule as this repo's .ps1 files: no encoding games, safe to print anywhere.
"""
import socket
import sys
import threading

RECV_SIZE = 65536
MAX_HEADER = 65536


def _pipe(src, dst):
    """Copy src -> dst until EOF. One thread per direction (no select loop) so a slow
    direction cannot deadlock the other: `git push` is bulk one way with ACKs the other."""
    try:
        while True:
            data = src.recv(RECV_SIZE)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def handle(client, target_ip, proxy_host):
    upstream = None
    established = False
    try:
        client.settimeout(15)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > MAX_HEADER:
                raise ValueError("request header too large")

        line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return

        host, _sep, port_text = parts[1].partition(":")
        port = int(port_text) if port_text else 443

        if host.lower() == proxy_host:
            upstream = socket.create_connection((target_ip, port), timeout=15)
            route = target_ip
        else:
            upstream = socket.create_connection((host, port), timeout=15)
            route = host

        client.settimeout(None)
        upstream.settimeout(None)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        established = True
        print("[gh-proxy] CONNECT %s:%d -> %s" % (host, port, route), flush=True)

        back = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
        back.start()
        _pipe(upstream, client)
        back.join(timeout=5)
    except Exception as exc:
        print("[gh-proxy] error: %s" % exc, flush=True)
        if not established:
            # Only report a gateway failure while the client is still speaking HTTP;
            # once the tunnel is up the bytes belong to TLS and must not be corrupted.
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
    finally:
        for sock in (client, upstream):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


def main():
    if len(sys.argv) < 3:
        print("usage: gh-proxy.py <bind_port> <target_ip> [host]", file=sys.stderr)
        return 2
    port = int(sys.argv[1])
    target_ip = sys.argv[2]
    proxy_host = (sys.argv[3] if len(sys.argv) > 3 else "github.com").lower()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(16)
    print("[gh-proxy] listening 127.0.0.1:%d  %s -> %s" % (port, proxy_host, target_ip),
          flush=True)

    while True:
        client, _addr = srv.accept()
        threading.Thread(target=handle, args=(client, target_ip, proxy_host),
                         daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
