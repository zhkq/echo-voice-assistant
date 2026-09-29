#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在单位那台上做一次真实调用验收：/v1/asr 与 /v1/diarize。

只用标准库（宿主 python3 没有 numpy）：wave 生成 20 秒 16 kHz 单声道正弦波当输入音频
（目的是验"模型能加载并算完"，**不验识别准确率**）。
鉴权链：管理员登录拿 csrf → 用 CLI 发一张码 → /v1/pair 换凭据 → /v1/token 拿 Bearer。
"""
import http.cookiejar
import json
import math
import os
import re
import struct
import subprocess
import urllib.request
import wave

BASE = "http://127.0.0.1:8900"
ADMIN = "http://127.0.0.1:8901"
USER, PWD = "zysk", "be726f-e1f857-2640ae"
WAV = "/home/zysk/echo/tmp/verify.wav"

# ---- 1) 生成 20 秒测试音频 ----
os.makedirs(os.path.dirname(WAV), exist_ok=True)
with wave.open(WAV, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    frames = bytearray()
    for i in range(16000 * 20):
        v = int(12000 * math.sin(2 * math.pi * 220 * i / 16000))
        frames += struct.pack("<h", v)
    w.writeframes(bytes(frames))
print("  音频: %s (%.1f KB, 20s/16kHz)" % (WAV, os.path.getsize(WAV) / 1024.0))


def post_json(url, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    return urllib.request.urlopen(req, timeout=20)


def post_file(url, path, token, field="file"):
    """服务端收的是**裸音频 body**（Content-Type: audio/wav），不是 multipart（415 实测）。"""
    with open(path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "audio/wav",
        "Authorization": "Bearer " + token,
        "X-ECHO-Client": "verify"})
    return urllib.request.urlopen(req, timeout=600)


# ---- 2) 发一张码（走 CLI，最可靠）----
out = subprocess.run(["docker", "exec", "echo-backend", "python", "-m", "server.main",
                      "--config", "/etc/echo/server.yaml", "--new-client", "verify-newimg",
                      "--scopes", "asr,diarize,speaker"],
                     capture_output=True, text=True).stdout
m = re.search(r"code=([A-Z0-9]+)", out)
if not m:
    print("  [fail] 没能从 CLI 输出里解析出 code：\n%s" % out[:400])
    raise SystemExit(2)
code = m.group(1)
print("  配对码: %s" % code)

# ---- 3) pair → token ----
r = post_json(BASE + "/v1/pair", {"code": code, "clientName": "verify-newimg"})
pair = json.loads(r.read().decode())
cid = pair.get("clientId") or pair.get("client_id")
sec = pair.get("secret") or pair.get("clientSecret")
print("  pair: clientId=%s secret=%s…" % (cid, (sec or "")[:8]))
import base64
basic = base64.b64encode(("%s:%s" % (cid, sec)).encode()).decode()
req = urllib.request.Request(BASE + "/v1/token", data=b"", headers={
    "Authorization": "Basic " + basic, "Content-Type": "application/json"})
tok_json = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
print("  token 响应字段: %s" % list(tok_json.keys()))
tok = (tok_json.get("token") or tok_json.get("accessToken") or tok_json.get("access_token")
       or tok_json.get("jwt") or "")
print("  token: %s…" % str(tok)[:12])
if not tok:
    raise SystemExit("没拿到 token，上面是响应字段名")

# ---- 4) 真跑两个端点 ----
for name, path in (("asr", "/v1/asr"), ("diarize", "/v1/diarize")):
    try:
        resp = post_file(BASE + path, WAV, tok)
        body = resp.read().decode("utf-8", "replace")
        print("  %-8s -> HTTP %s | %s" % (name, resp.status, body[:220]))
    except urllib.error.HTTPError as e:
        print("  %-8s -> HTTP %s | %s" % (name, e.code, e.read().decode("utf-8", "replace")[:300]))
    except Exception as e:
        print("  %-8s -> 异常 %s: %s" % (name, type(e).__name__, e))

# ---- 5) capabilities 里各模型状态 ----
r = urllib.request.urlopen(BASE + "/v1/capabilities", timeout=10)
for mm in json.loads(r.read().decode())["models"]:
    print("  %-14s state=%-8s error=%r" % (mm["id"], mm["state"], mm["error"][:80]))
