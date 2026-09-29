#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 10.100.0.24（RTX 2060 SUPER / Turing 7.5 / 8GB）上生成后端运行目录。

做三件事（全部幂等）：
 1) ~/echo/backend/compose.yaml  ← 交付包的 compose.yaml，但补两处：
      * environment 里加 MODELSCOPE_CACHE / HOME 指向可写缓存目录
        （**必须**：SenseVoice 的 vad_model="fsmn-vad" 是按模型名现拉的，
         只读根 + 没有可写家目录时报 [Errno 30] → 每个 /v1/asr 都 model_failed。
         这是 2026-09-27 在 GTX 1070 上实测踩到的坑，交付包那份 compose 里还没有。）
      * volumes 里加 cache 卷（tmp 卷不能拿来放它：tmp.files/bytes 是泄漏判据）
 2) ~/echo/backend/server.yaml   ← models.specs 按 Turing 改：
      asr-long: impl sensevoice / supports [asr.text] / est_vram_mb 1800 / modelVersion sensevoice-small
      （Turing 没有 bf16，而 stt.py 里 qwen3asr 的 dtype 写死 bf16 → 跑不了）
      diarize + speaker-embed **保留**（要实测这台能不能做分离）
 3) ~/echo/backend/.env          ← JWT 密钥（随机）、卷路径、server id、健康检查走 http
"""
import os
import re
import secrets
import shutil

HOME = "/home/zysk/echo"
SRC = os.path.join(HOME, "src")
DST = os.path.join(HOME, "backend")
os.makedirs(DST, exist_ok=True)

# ---------- 1) compose.yaml ----------
comp = open(os.path.join(SRC, "compose.yaml"), encoding="utf-8").read()

if "MODELSCOPE_CACHE" not in comp:
    comp = comp.replace(
        '        ECHO_STATE_ROOT: "${ECHO_STATE_ROOT:-/var/echo/state}"',
        '        ECHO_STATE_ROOT: "${ECHO_STATE_ROOT:-/var/echo/state}"\n'
        '        # ModelScope/funasr 的可写缓存：vad 模型是按名现拉的，只读根会直接失败\n'
        '        MODELSCOPE_CACHE: "/var/echo/cache"\n'
        '        HOME: "/var/echo/cache"', 1)
if "/var/echo/cache" not in comp.split("volumes:")[-1]:
    comp = comp.replace(
        "        - ${ECHO_HOST_STATE_DIR:-/srv/echo/state}:${ECHO_STATE_ROOT:-/var/echo/state}",
        "        - ${ECHO_HOST_STATE_DIR:-/srv/echo/state}:${ECHO_STATE_ROOT:-/var/echo/state}\n"
        "        # 可写缓存（别塞进 tmp 卷：那卷的 files/bytes 是泄漏判据）\n"
        "        - ${ECHO_HOST_CACHE_DIR:-/srv/echo/cache}:/var/echo/cache", 1)
# 镜像已手工构建好（cu126 + 清华源），别让 compose 再去 build 一遍
comp = re.sub(r"^\s*build:\n(?:\s+.*\n)+", "", comp, count=1, flags=re.M)
open(os.path.join(DST, "compose.yaml"), "w", encoding="utf-8").write(comp)

# ---------- 2) server.yaml ----------
cfg = open(os.path.join(SRC, "server.yaml"), encoding="utf-8").read()
cfg = cfg.replace("impl: qwen3asr", "impl: sensevoice")
cfg = cfg.replace("modelVersion: qwen3-asr-0.6b", "modelVersion: sensevoice-small")
cfg = cfg.replace("supports: [asr.text, asr.timestamps]", "supports: [asr.text]")
cfg = cfg.replace("est_vram_mb: 4700", "est_vram_mb: 1800")
open(os.path.join(DST, "server.yaml"), "w", encoding="utf-8").write(cfg)

# ---------- 3) .env ----------
env_path = os.path.join(DST, ".env")
if not os.path.exists(env_path):
    open(env_path, "w", encoding="utf-8").write(
        "ECHO_IMAGE=echo-backend:0.1.0-cu126\n"
        "ECHO_SERVER_ID=gpu-office-2060s\n"
        "ECHO_JWT_SECRET=%s\n"
        "ECHO_HOST_MODELS_DIR=%s/models\n"
        "ECHO_HOST_TMP_DIR=%s/tmp\n"
        "ECHO_HOST_STATE_DIR=%s/state\n"
        "ECHO_HOST_CACHE_DIR=%s/cache\n"
        "ECHO_HEALTH_SCHEME=http\n"
        "ECHO_MAX_CONCURRENT=2\n" % (secrets.token_hex(32), HOME, HOME, HOME, HOME))

print("== compose: environment 关键行")
for ln in comp.splitlines():
    if any(k in ln for k in ("MODELSCOPE_CACHE", "HOME:", "cache", "image:", "8900", "8901", "read_only", "8900")):
        print("   " + ln.strip())
print("== server.yaml: asr-long 段")
show = False
for ln in cfg.splitlines():
    if "asr-long" in ln:
        show = True
    if show:
        print("   " + ln)
        if "supports:" in ln and show:
            break
print("== .env 已写:", env_path)
print("== 目录:", sorted(os.listdir(DST)))
