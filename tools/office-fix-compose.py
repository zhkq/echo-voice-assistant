#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 ~/echo/backend/compose.yaml（幂等、安全）。

上一版把 `build:` 段用贪婪正则删掉了，连带删掉了 image/environment/volumes —— 这份修正：
 1) 原样拷贝交付包里的 compose.yaml（**不删任何段**；本地已有镜像时 compose 不会去 build）
 2) 只做两处**精确插入**：
      * environment 段：MODELSCOPE_CACHE / HOME → /var/echo/cache
        （必须：SenseVoice 的 vad_model="fsmn-vad" 按模型名现拉，只读根会 [Errno 30] → 每个 /v1/asr 都失败）
      * volumes 段：${ECHO_HOST_CACHE_DIR} → /var/echo/cache
 3) 用 `docker compose config --quiet` 自检，失败就还原备份
"""
import os
import re
import shutil
import subprocess
import time

SRC = "/home/zysk/echo/src/compose.yaml"
DST = "/home/zysk/echo/backend/compose.yaml"

txt = open(SRC, encoding="utf-8").read()
assert "services:" in txt and "environment:" in txt and "volumes:" in txt, "源 compose 看着不对"

# ---- 插入 1：environment 里加两行 ----
m = re.search(r"^([ \t]*)ECHO_STATE_ROOT:.*$", txt, re.M)
assert m, "找不到 ECHO_STATE_ROOT 行"
ind = m.group(1)
if "MODELSCOPE_CACHE" not in txt:
    txt = (txt[:m.end()]
           + "\n%s# ModelScope/funasr 的可写缓存：vad 模型按名现拉，只读根会直接失败" % ind
           + "\n%sMODELSCOPE_CACHE: \"/var/echo/cache\"" % ind
           + "\n%sHOME: \"/var/echo/cache\"" % ind
           + txt[m.end():])

# ---- 插入 2：volumes 里加 cache 卷 ----
m2 = re.search(r"^([ \t]*)-[ \t]*\$\{ECHO_HOST_STATE_DIR[^\n]*$", txt, re.M)
assert m2, "找不到 state 卷行"
ind2 = m2.group(1)
if "${ECHO_HOST_CACHE_DIR" not in txt:
    txt = (txt[:m2.end()]
           + "\n%s# 可写缓存（别塞进 tmp 卷：那卷的 files/bytes 是泄漏判据）" % ind2
           + "\n%s- ${ECHO_HOST_CACHE_DIR:-/srv/echo/cache}:/var/echo/cache" % ind2
           + txt[m2.end():])

# ---- 写 + 自检 ----
bak = None
if os.path.exists(DST):
    bak = DST + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(DST, bak)
open(DST, "w", encoding="utf-8").write(txt)

r = subprocess.run(["docker", "compose", "config", "--quiet"],
                   cwd=os.path.dirname(DST), capture_output=True, text=True)
if r.returncode != 0:
    if bak:
        shutil.copy2(bak, DST)
        print("  [warn] 自检失败，已还原 %s" % os.path.basename(bak))
    print("  compose config 报错：%s" % (r.stderr or r.stdout).strip()[:300])
else:
    print("  compose config 自检通过 ✓")

print("== environment / volumes 关键行")
for ln in open(DST, encoding="utf-8"):
    s = ln.rstrip()
    if any(k in s for k in ("MODELSCOPE_CACHE", "HOME:", "/var/echo/cache",
                            "ECHO_STATE_ROOT", "ECHO_TMP_ROOT", "image:", "8900:", "8901:")):
        print("   " + s.strip())
print("== 服务段完整性")
for key in ("services:", "backend:", "image:", "ports:", "environment:", "volumes:", "read_only:", "deploy:"):
    print("   %-14s %s" % (key, "✓" if re.search(r"^[ \t]*%s" % re.escape(key), txt, re.M) else "✗ 缺"))
print("== .env")
for ln in open("/home/zysk/echo/backend/.env", encoding="utf-8"):
    print("   " + ln.strip())
