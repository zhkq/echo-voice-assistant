# -*- coding: utf-8 -*-
"""把"新卡（cu126）"那套模型打成可分发的 zip。

容器里的模型根是**只读挂载**，所以：
* 目录名必须是 HF 缓存约定：`hub/models--<owner>--<name>/snapshots/<rev>/`
  （`app/audio/stt.py` 的 `_resolve_hf_cache` 按这个名字找；ModelScope 缓存里那份叫
  `Qwen--Qwen3-ASR-0.6B`，少了 `models--` 前缀，所以要在这里改名）
* **必须把软链实体化**（HF 缓存里 snapshots/* 多是指向 ../../blobs/* 的软链）——
  软链一旦落在只读挂载里就会指向挂载点外面，等于"文件不存在"。

用法：python tools/pack-models-cu126.py [--out dist] [--stamp YYYYmmdd-HHMM]
产物：<out>/ECHO-models-cu126-<stamp>.zip（ZIP_STORED，safetensors 压不动，省 CPU）
"""
import argparse
import os
import time
import zipfile

HOME = os.path.expanduser("~")
# 包内路径 -> 源目录
PLAN = [
    ("hub/models--Qwen--Qwen3-ASR-0.6B",
     os.path.join(HOME, ".cache", "modelscope", "models", "Qwen--Qwen3-ASR-0.6B")),
    ("hub/models--Qwen--Qwen3-ForcedAligner-0.6B",
     os.path.join(r"C:\echo-dev", "models", "hub", "models--Qwen--Qwen3-ForcedAligner-0.6B")),
    ("sensevoice", os.path.join(r"C:\echo-dev", "models", "sensevoice")),
    ("pyannote", os.path.join(r"C:\echo-dev", "models", "pyannote")),
]

README = """# ECHO 模型包（新卡 / cu126 用）

解到**宿主**的模型根（容器只读挂载它），保持这个布局：

    /srv/echo/models/
      hub/models--Qwen--Qwen3-ASR-0.6B/snapshots/<rev>/...
      hub/models--Qwen--Qwen3-ForcedAligner-0.6B/snapshots/<rev>/...
      sensevoice/...
      pyannote/...

一条命令（在交付包目录里，宿主上）：

    sudo mkdir -p /srv/echo/models
    sudo unzip -q ECHO-models-cu126-*.zip -d /srv/echo/models
    sudo chown -R "$USER" /srv/echo

**为什么强制对齐器不能少**：缺了它，服务端仍会宣告 `supports: [asr.timestamps]`，
但实际给不出句级时间戳 —— 那是说假话（`/v1/health` 照样绿）。摆完自检：

    ls /srv/echo/models/hub/models--Qwen--Qwen3-ForcedAligner-0.6B/snapshots/*/ | head
    ls /srv/echo/models/hub/models--Qwen--Qwen3-ASR-0.6B/snapshots/*/ | head

旧卡（≤7.0）不要下这份：那台只能跑 SenseVoice，pyannote/qwen3asr 都装不上，见 cu118 交付包。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(r"C:\echo-dev", "dist"))
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M"))
    a = ap.parse_args()

    for _, src in PLAN:
        if not os.path.isdir(src):
            print("  [fail] 缺源目录：%s" % src)
            return 1
    zip_path = os.path.join(a.out, "ECHO-models-cu126-%s.zip" % a.stamp)
    os.makedirs(a.out, exist_ok=True)

    total_files = 0
    total_bytes = 0
    per_model = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        root = "ECHO-models-cu126-%s" % a.stamp
        z.writestr(root + "/模型包-说明.md", README)
        for dest, src in PLAN:
            n = 0
            b = 0
            for dirpath, _dirs, files in os.walk(src):
                for f in files:
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, src)
                    # 实体化软链：读真实内容写进 zip（只读挂载下软链会失效）
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    z.write(full, arcname="%s/%s/%s" % (root, dest, rel.replace(os.sep, "/")))
                    n += 1
                    b += size
            per_model.append((dest, n, b))
            total_files += n
            total_bytes += b
            print("  %-46s %5d 文件  %8.1f MB" % (dest, n, b / 1048576.0))
        print("  合计 %d 文件 / %.2f GB" % (total_files, total_bytes / 1073741824.0))
    print("  产物：%s（%.2f GB）" % (zip_path, os.path.getsize(zip_path) / 1073741824.0))
    for dest, n, b in per_model:
        print("    %-46s %5d 文件 %8.1f MB" % (dest, n, b / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
