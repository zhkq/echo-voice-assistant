#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ECHO 能力后端 —— 冒烟自测。

**为什么是 Python 而不是一串 curl**：这套自测里有一半是"构造一个**故意畸形**的请求"
（空 body、错的 Content-Type、声报超长的 Content-Length、两个并发请求）。
在 PowerShell / cmd 里写这些，光引号和 `@file` 的转义就能耗掉半小时，
而且每次都得重来一遍。这里一次写好，本机 / 远程 / Linux 部署都能用同一份。

跑法：

    # 本机（默认连 127.0.0.1:8900，自动找一段测试音频）
    python scripts/smoke-echo-backend.py

    # 连别人的机器 / 容器
    python scripts/smoke-echo-backend.py --base-url http://gpu-01:8900

    # 带鉴权（先拿配对码换 secret；脚本会自己换令牌）
    python scripts/smoke-echo-backend.py --base-url https://gpu-01:8900 \\
        --pair-code 7K2M9QX4 --insecure

    # 只跑受控的假引擎那部分（不加载真模型，秒级返回）
    python scripts/smoke-echo-backend.py --skip-inference

退出码：0 = 全部符合预期；1 = 有步骤不符合预期（每一步都会打印具体差异）。

**它不会修改服务端任何东西**（除了配对那一步会建一个客户端）——
所有请求都是能力端点或查询端点，服务端不留业务数据。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = RED = YELLOW = DIM = RESET = ""      # 老 conhost 不认 ANSI，别打出一堆乱码

RESULTS = []


def _report(ok, name, detail=""):
    RESULTS.append((bool(ok), name))
    mark = (GREEN + "PASS" + RESET) if ok else (RED + "FAIL" + RESET)
    print("  [%s] %s%s" % (mark, name, ("  " + DIM + detail + RESET) if detail else ""))
    return ok


class Client:
    """极简 HTTP 客户端。刻意只用标准库：这份脚本要在**别人的机器**上跑得起来，
    不该先让人 pip install requests。"""

    def __init__(self, base: str, token: str = "", timeout: float = 600.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method, path, body: bytes | None = None,
                ctype: str | None = None, headers: dict | None = None):
        url = self.base + path
        req = urllib.request.Request(url, data=body, method=method)
        if ctype:
            req.add_header("Content-Type", ctype)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, raw, dict(r.headers), time.time() - t0
        except urllib.error.HTTPError as e:          # 4xx/5xx 也要读到 body
            try:
                raw = e.read()
            except Exception:
                # 服务端在拒掉请求之后可能**已经关了连接**（大 body + 413 就会这样：
                # 我们声报 900 MB，服务端一个字节都不收就回 413 然后收摊，
                # 于是本地读响应体时报 WinError 10053）。这时保不住 body，
                # 但**状态码还在**，检查仍然做得完 —— 不该让脚本在这里崩。
                raw = b""
            return e.code, raw, dict(e.headers or {}), time.time() - t0
        except Exception as e:
            return 0, str(e).encode(), {}, time.time() - t0

    def json(self, method, path, **kw):
        status, raw, headers, dt = self.request(method, path, **kw)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_raw": raw[:400].decode("utf-8", "replace")}
        return status, body, headers, dt


def _find_sample(explicit: str) -> str:
    """找一段能用的测试音频。

    优先用 `data/backend-dev/sample*.wav`（本机留的样本，30 秒，够快）；
    否则从真实会议录音里现切一段 —— 用真实录音比合成正弦波有意义得多：
    合成音频测不出模型到底有没有在工作。
    """
    if explicit:
        return explicit
    for name in ("sample30s.wav", "sample.wav"):
        p = os.path.join(ROOT, "data", "backend-dev", name)
        if os.path.isfile(p):
            return p
    meetings = os.path.join(ROOT, "data", "meetings")
    if os.path.isdir(meetings):
        for root, _d, files in os.walk(meetings):
            for fn in sorted(files):
                if fn.lower().endswith(".wav"):
                    src = os.path.join(root, fn)
                    try:
                        return _slice(src)
                    except Exception as e:
                        print("  %s切样本失败（%s），换下一个%s" % (YELLOW, e, RESET))
    return ""


def _slice(src: str, seconds: float = 30.0) -> str:
    """从一段长录音里切前 N 秒，写到 backend-dev 下复用。"""
    import soundfile as sf
    out_dir = os.path.join(ROOT, "data", "backend-dev")
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "sample30s.wav")
    if os.path.isfile(dst):
        return dst
    data, sr = sf.read(src, dtype="float32")
    n = int(sr * seconds)
    seg = data[:n] if getattr(data, "ndim", 1) == 1 else data[:n, :]
    sf.write(dst, seg, sr)
    print("  %s样本：从 %s 切了 %.0f 秒 → %s%s"
          % (DIM, os.path.relpath(src, ROOT), len(seg) / sr, os.path.relpath(dst, ROOT), RESET))
    return dst


# ---------------------------------------------------------------- 各组检查

def check_basic(c: Client):
    print("\n== 查询端点")
    status, body, headers, _ = c.json("GET", "/v1/health")
    _report(status == 200 and body.get("ok") is True, "GET /v1/health → 200 且 ok=true",
            "uptime=%ss" % body.get("uptimeSeconds"))
    _report(isinstance(body.get("tmp"), dict), "health 里带临时目录统计（泄漏的信号）",
            "files=%s bytes=%s" % (body.get("tmp", {}).get("files"),
                                   body.get("tmp", {}).get("bytes")))
    _report(isinstance(body.get("busy"), dict), "health 里带并发快照",
            str(body.get("busy")))

    status, body, _, _ = c.json("GET", "/v1/ready")
    _report(status == 200 and body.get("ok") is True, "GET /v1/ready → 200")

    status, body, _, _ = c.json("GET", "/v1/capabilities")
    ok = status == 200 and body.get("protocol") == 1
    _report(ok, "GET /v1/capabilities → 200 且 protocol=1")
    slots = body.get("slots") or {}
    _report(bool(slots), "capabilities 声明了能力槽", str(sorted(slots)))
    lim = body.get("limits") or {}
    _report(lim.get("queueMax") == 0, "limits.queueMax=0（不排队，2026-09-23 定）",
            str(lim))
    spaces = {m.get("vectorSpaceId") for m in (body.get("models") or []) if m.get("vectorSpaceId")}
    _report(len(spaces) <= 1, "产出向量的模型只有一个 vectorSpaceId（铁律 L5）",
            str(spaces or "（没有向量类模型）"))
    return body


def check_inference(c: Client, wav: str, caps: dict):
    print("\n== 能力端点（会真的加载模型，第一次比较慢）")
    data = open(wav, "rb").read()
    print("  %s音频 %s（%.1f MB）%s" % (DIM, os.path.relpath(wav, ROOT),
                                       len(data) / 1048576, RESET))

    slots = caps.get("slots") or {}
    short_slot = "asr.text"
    if short_slot in slots:
        status, body, _, dt = c.json("POST", "/v1/asr?variant=short",
                                     body=data, ctype="audio/wav")
        ok = status == 200 and isinstance(body.get("text"), str) and body.get("modelId")
        _report(ok, "POST /v1/asr?variant=short → 200 + 文本",
                "modelId=%s %.2fs（含首次加载）" % (body.get("modelId"), dt))
        if ok:
            print("       %s文本：%s%s" % (DIM, (body["text"] or "")[:60].replace("\n", " "), RESET))
            # 冷/热两段的差别很大，值得单独看一眼
            status2, body2, _, dt2 = c.json("POST", "/v1/asr?variant=short",
                                            body=data, ctype="audio/wav")
            _report(status2 == 200, "第二次同一请求 → 200（模型已常驻，这才是真实延迟）",
                    "%.2fs  vs  首次 %.2fs" % (dt2, dt))
    else:
        _report(False, "capabilities 里没有 asr.text 槽 —— 这个后端做不了转写")

    if "asr.timestamps" in slots or short_slot in slots:
        status, body, _, _ = c.json("POST", "/v1/asr?variant=short&timestamps=1",
                                    body=data, ctype="audio/wav")
        ts = body.get("timestamps")
        # 关键：只允许 exact / none —— 服务端**不编** estimated（那是客户端的兜底）
        _report(ts in ("exact", "none"), "timestamps 只报 exact|none（服务端不编 estimated）",
                "timestamps=%s sentences=%d" % (ts, len(body.get("sentences") or [])))

    if "diarize.turns" in slots:
        status, body, _, dt = c.json("POST", "/v1/diarize", body=data, ctype="audio/wav")
        ok = status == 200 and isinstance(body.get("turns"), list)
        _report(ok, "POST /v1/diarize → 200 + 说话人时间轴",
                "turns=%s speakers=%s %.2fs" % (len(body.get("turns") or []),
                                                list((body.get("speakers") or {}).keys()), dt))
        _report(bool(body.get("vectorSpaceId")) and int(body.get("dim") or 0) > 0,
                "diarize 报出 vectorSpaceId 与 dim（客户端据此判断能不能比对）",
                "space=%s dim=%s" % (body.get("vectorSpaceId"), body.get("dim")))

    if "speaker.embed" in slots:
        status, body, _, dt = c.json("POST", "/v1/speaker/embed?count=1",
                                     body=data, ctype="audio/wav")
        embs = body.get("embeddings") or []
        _report(status == 200 and len(embs) == 1 and len(embs[0]) > 0,
                "POST /v1/speaker/embed → 200 + 嵌入",
                "n=%d dim=%s %.2fs" % (len(embs), body.get("dim"), dt))
    return data


def check_rejections(c: Client, data: bytes):
    """**这部分比成功路径更值钱**：客户端的降级逻辑全靠这些 code 分支。"""
    print("\n== 拒绝路径（错误码必须分得清）")

    status, body, _, _ = c.json("POST", "/v1/asr", body=b"", ctype="audio/wav")
    _report(status == 400 and body.get("code") == "bad_request",
            "空 body → 400 bad_request", "got %s %s" % (status, body.get("code")))

    # `audio/mpeg` **是**受支持的容器类型（mp3 也收），别拿它当"不认识"的例子 ——
    # 第一版就是这么写的，结果拿到 200 才发现是自己搞错了。
    status, body, _, _ = c.json("POST", "/v1/asr", body=data, ctype="application/pdf")
    _report(status == 415 and body.get("code") == "unsupported_media",
            "不认识的 Content-Type（application/pdf）→ 415 unsupported_media",
            "got %s %s" % (status, body.get("code")))

    # 声报一个超限的 Content-Length：应当在**读 body 之前**就被拒。
    # 注意这里常常**读不到响应体**：服务端一个字节都不收就回 413 然后收摊，
    # 本地读的时候会拿到 WinError 10053。那本身就是"读之前就拒"的证据 ——
    # 所以状态码是 413 就算过，body 读到了再顺手核对 code。
    status, body, _, _ = c.json("POST", "/v1/asr", body=data, ctype="audio/wav",
                                headers={"Content-Length": str(999 * 1024 * 1024)})
    code = body.get("code")
    if code is None:
        _report(status == 413,
                "声报超限 → 413（读之前就拒；连接随即关闭所以常常读不到 body）",
                "got %s，body 不可读 = 服务端没读我们的 body" % status)
    else:
        _report(status == 413 and code == "payload_too_large",
                "声报超限 → 413 payload_too_large（读之前就拒）",
                "got %s %s" % (status, code))

    status, body, _, _ = c.json("POST", "/v1/asr?model=definitely-not-a-model",
                                body=data, ctype="audio/wav")
    _report(status == 404 and body.get("code") == "model_not_found",
            "点名不存在的模型 → 404 model_not_found",
            "got %s %s" % (status, body.get("code")))

    status, body, _, _ = c.json("POST", "/v1/diarize?mode=turns", body=data,
                                ctype="audio/wav")
    # v1 只做 segment。**没实现的能力要如实拒绝，不能假装做了**（回一个假时间轴更糟）
    _report(status == 400 and body.get("code") == "bad_request",
            "mode=turns（v1 未实现）→ 400，如实拒绝而不是假装支持",
            "got %s %s" % (status, body.get("code")))


def check_gate(c: Client, data: bytes, slow_path="/v1/diarize"):
    """两路并发：**同一个客户端**发两个请求，应当恰好一个被 409 拦掉。

    为什么挑 diarize：它是秒级的，两个请求真的会重叠。
    asr 常驻之后只要 200 ms，用来测并发容易"根本没撞上"，
    那就变成一条永远绿的空转用例（这个坑在服务端的单测里踩过一次）。
    """
    print("\n== 并发闸门（每客户端 1 路）")
    out = []
    lock = threading.Lock()

    def go(i):
        cli = Client(c.base, token=c.token, timeout=600)
        status, body, _, dt = cli.json("POST", slow_path, body=data, ctype="audio/wav")
        with lock:
            out.append((i, status, body.get("code"), dt))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(700)
    # `code` 为 None = 这一路成功了（200）。别对 None 排序 —— 第一版就死在这里。
    busy = [r for r in out if r[2] == "client_busy"]
    done = [r for r in out if r[2] is None]
    summary = ["#%d %s%s %.2fs" % (i, s, "" if c is None else " " + c, d)
               for (i, s, c, d) in sorted(out)]
    _report(len(out) == 2 and len(busy) == 1 and len(done) == 1,
            "同一身份两路并发 → 恰好一路 409 client_busy（另一路正常做完）",
            " | ".join(summary))


def check_cleanup(c: Client):
    print("\n== 临时文件（服务端不存业务数据的落点之一）")
    status, body, _, _ = c.json("GET", "/v1/health")
    tmp = body.get("tmp") or {}
    _report(tmp.get("files") == 0 and tmp.get("bytes") == 0,
            "请求跑完之后临时目录归零",
            "files=%s bytes=%s" % (tmp.get("files"), tmp.get("bytes")))


def check_auth(c: Client, pair_code: str, insecure: bool):
    """配对 → 换令牌 → 带令牌调能力端点。"""
    print("\n== 鉴权（配对 → 令牌 → 带令牌调用）")
    anon = Client(c.base, timeout=60)
    status, body, _, _ = anon.json("POST", "/v1/pair",
                                   body=json.dumps({"code": pair_code,
                                                    "clientName": "smoke-test"}).encode(),
                                   ctype="application/json")
    ok = status == 200 and body.get("clientId") and body.get("secret")
    _report(ok, "POST /v1/pair → 200 + clientId/secret", "clientId=%s" % body.get("clientId"))
    if not ok:
        _report(False, "配对失败，后面的鉴权检查跳过", str(body)[:200])
        return ""

    basic = base64.b64encode(("%s:%s" % (body["clientId"], body["secret"])).encode()).decode()
    status, tok, _, _ = anon.json("POST", "/v1/token",
                                  headers={"Authorization": "Basic " + basic})
    ok = status == 200 and tok.get("accessToken")
    _report(ok, "POST /v1/token → 200 + accessToken",
            "expiresIn=%s scopes=%s" % (tok.get("expiresIn"), tok.get("scopes")))
    if not ok:
        return ""

    authed = Client(c.base, token=tok["accessToken"], timeout=60)
    status, body, _, _ = authed.json("GET", "/v1/capabilities")
    _report(status == 200, "带令牌 → GET /v1/capabilities 200")

    status, body, _, _ = Client(c.base, token="garbage", timeout=60).json(
        "GET", "/v1/capabilities")
    _report(status == 401 and body.get("code") == "unauthorized",
            "乱填令牌 → 401 unauthorized", "got %s %s" % (status, body.get("code")))
    return tok["accessToken"]


def _http_client(base: str, insecure: bool):
    if insecure:
        import ssl
        ctx = ssl._create_unverified_context()      # noqa: S323 —— 自签证书场景，见 --insecure
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        urllib.request.install_opener(opener)
    return Client(base)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ECHO 能力后端冒烟自测")
    ap.add_argument("--base-url", default=os.environ.get("ECHO_SERVER_URL",
                                                         "http://127.0.0.1:8900"))
    ap.add_argument("--wav", default="", help="测试音频；不给就自动找/切一段")
    ap.add_argument("--pair-code", default="", help="给一次性配对码，顺带验一遍鉴权链路")
    ap.add_argument("--token", default="", help="直接给 Bearer 令牌（静态令牌模式）")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（自签场景）")
    ap.add_argument("--skip-inference", action="store_true",
                    help="只跑查询与拒绝路径（不加载真模型）")
    args = ap.parse_args(argv)

    print("ECHO 能力后端冒烟：%s" % args.base_url)
    c = _http_client(args.base_url, args.insecure)
    c.token = args.token

    status, body, _, _ = c.json("GET", "/v1/health")
    if status != 200:
        print("%s连不上 /v1/health（%s）%s\n  先确认服务端起来了："
              "\n    python -m server.main --config data/backend-dev/server.yaml%s"
              % (RED, status or body, RESET, RESET))
        return 1

    caps = check_basic(c)
    data = b""
    if not args.skip_inference:
        wav = _find_sample(args.wav)
        if not wav:
            print("%s没找到测试音频：给 --wav，或先在 data/meetings 下放一段录音%s"
                  % (YELLOW, RESET))
        else:
            data = check_inference(c, wav, caps)
    if not data:
        data = b"\x00" * 2048          # 只为让拒绝路径能发出请求，不需要是真音频
    check_rejections(c, data)
    if not args.skip_inference and args.pair_code == "":
        check_gate(c, data)
    if args.pair_code:
        check_auth(c, args.pair_code, args.insecure)
    check_cleanup(c)

    bad = [n for ok, n in RESULTS if not ok]
    print("\n==== 小结：%d 项通过，%d 项不符合预期 ====" % (len(RESULTS) - len(bad), len(bad)))
    for n in bad:
        print("  %s✗ %s%s" % (RED, n, RESET))
    if bad:
        print("\n%s注意：失败项要按**原因**分。%s" % (DIM, RESET))
        print("%s  401/403 = 凭据或权限；409 = 自己并发（客户端 bug，别重试）；%s" % (DIM, RESET))
        print("%s  413/415 = 音频本身；503 = 系统忙或模型不可用（该退避/降级）；%s"
              % (DIM, RESET))
        print("%s  404 model_not_found = 这个后端没有你要的模型（该换后端）。%s" % (DIM, RESET))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
