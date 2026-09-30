#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_offline_pack.py —— 出**离线组件合集**（REFACTOR-PLAN D23 的兜底件）。

## 这是什么、为什么需要它

主包（D22）**不带运行时也不带模型**，装的时候 `install.ps1` 要去网上找 Python（三级降级：
uv → py -3.11 → python.org 嵌入包）、再 pip 装依赖、再下模型。这套在**能上网**的机器上没问题，
但每一次安装都要等那几分钟，而且失败模式很多（镜像、代理、版本漂移 —— 装到什么版本看当时 registry）。

**离线组件合集**把这些**提前到出包时**做掉：字节数一样（甚至更少，少了重复下载），
换来的却是**目标机装的时候 0 下载、几秒完事、版本钉死**。这就是"用几百兆空间换快速简单安装"。

规格**早就写好了**，在 `components/offline-pack.json` 里（每个组件：`kind`、`dest`、
`approx_mb`，`runtime-core` 连"怎么做"都逐条写着）；消费方也早就写好了（`scripts\\install.ps1`
按"越专用越先"找 `ECHO-组件-runtime-core-*.zip` → `ECHO-离线组件合集-*.zip` → `ECHO-offline-*.zip`
→ **解开的 `runtime-core\\` 目录**）。**唯独没有"出这个包"的脚本** —— 这个文件就是它。

## 产出两样（都是有意的）

1. **解开的目录** `<out>/<pkg>/…`（`runtime-core/`、`models/sherpa-onnx-streaming/`）——
   `install.ps1` 的最后一条兜底就是"给我一个含 `runtime-core\\` 的目录"，**这条路不依赖 zip 内部约定**，
   所以它是最稳的交付形态：`-ComponentDir <out>/<pkg>` 即可。
2. **zip** `<out>/ECHO-离线组件合集-<stamp>.zip`（带顶层目录前缀）——给"要拷到别的机器"的场景。

## 两条纪律（这个脚本自己守着）

* **只打规格里声明过的组件**：id 不在 `offline-pack.json` 里 → 直接报错（不然会产出"清单里没有"的东西，
  而 `tests/test_default_profile.py` 正是钉"没有孤儿组件"的）；
* **`runtime-core` 不许含 torch**：`requirements-core.txt` 的铁律 L1（默认档零 torch），
  装完**实测** `importlib.util.find_spec("torch") is None`，不满足就失败 —— 这条是整个默认档
  "装得上、起得快"的地基。

用法：
    python scripts/build_offline_pack.py                     # 默认 runtime-core + stt-sherpa
    python scripts/build_offline_pack.py --components runtime-core
    python scripts/build_offline_pack.py --python-from <uv 的 CPython 目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(ROOT, "components", "offline-pack.json")
REQUIREMENTS = "requirements-core.txt"          # 规格里 runtime-core 用的那份
DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: kit 的 `manifest.json.requiredComponents` 就是这两个 —— 默认打它俩，装完即自足。
DEFAULT_COMPONENTS = ("runtime-core", "stt-sherpa")
PACKAGE_PREFIX = "ECHO-离线组件合集"
#: 装完必须能 import 的这些（少一个都说明这份运行时不能用）
IMPORT_CHECK = ("fastapi, uvicorn, pydantic, yaml, numpy, sounddevice, soundfile, soxr,"
                " sherpa_onnx, modelscope, httpx")


class PackError(RuntimeError):
    """出包失败（**响亮**：把子进程原文带出来）。"""


def log(msg: str) -> None:
    print(msg, flush=True)


def load_spec() -> Dict[str, dict]:
    with open(SPEC_PATH, encoding="utf-8") as fh:
        items = json.load(fh)
    return {str(c["id"]): c for c in items}


def find_uv_python() -> str:
    """找 uv 托管的那份 standalone CPython 3.11（**可重定位**，不依赖目标机 Python）。"""
    base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "uv", "python")
    if not os.path.isdir(base):
        return ""
    cands = []
    for name in os.listdir(base):
        d = os.path.join(base, name)
        if name.startswith("cpython-3.11") and os.path.isfile(os.path.join(d, "python.exe")):
            cands.append(d)
    return max(cands, key=os.path.getmtime) if cands else ""


def _run(argv: List[str], *, timeout: float = 3600.0) -> Tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _size_mb(path: str) -> float:
    total = 0
    for base, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(base, fn))
            except OSError:
                pass
    return total / (1 << 20)


def build_runtime_core(pack: dict, dest: str, python_from: str, index: str) -> None:
    """照规格做 `runtime-core`：复制可重定位 CPython → 删 EXTERNALLY-MANAGED → 装基础依赖 → 自检。"""
    if not python_from or not os.path.isfile(os.path.join(python_from, "python.exe")):
        raise PackError("找不到可重定位的 CPython（--python-from 指错了？）：%s" % python_from)
    log("  [runtime-core] 复制 %s → %s" % (python_from, dest))
    shutil.copytree(python_from, dest)
    # 规格里明写的一步：uv 会放一个 EXTERNALLY-MANAGED 标记，pip 见到它会拒绝装东西
    for rel in ("Lib/EXTERNALLY-MANAGED", "Scripts/EXTERNALLY-MANAGED"):
        marker = os.path.join(dest, *rel.split("/"))
        if os.path.exists(marker):
            os.remove(marker)
    py = os.path.join(dest, "python.exe")
    code, out = _run([py, "-m", "pip", "--version"])
    if code != 0:
        raise PackError("这份 CPython 里 pip 不可用：\n%s" % out)
    req = os.path.join(ROOT, REQUIREMENTS)
    log("  [runtime-core] pip install -r %s（源：%s）" % (REQUIREMENTS, index))
    code, out = _run([py, "-m", "pip", "install", "--no-warn-script-location",
                      "--disable-pip-version-check", "-r", req, "-i", index])
    if code != 0:
        tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-12:])
        raise PackError("装 %s 失败（pip 退出码 %d）：\n%s" % (REQUIREMENTS, code, tail))
    # 自检①：该 import 的都能 import
    code, out = _run([py, "-c", "import %s; print('imports ok')" % IMPORT_CHECK], timeout=600)
    if code != 0:
        raise PackError("装完了但 import 自检不过：\n%s" % out[-1200:])
    # 自检②（**铁律 L1**）：默认档不许有 torch
    code, _out = _run([py, "-c",
                       "import importlib.util as u, sys; sys.exit(3 if u.find_spec('torch')"
                       " else 0)"])
    if code != 0:
        raise PackError("runtime-core 里出现了 torch —— 默认档的铁律 L1 不许（%s）" % REQUIREMENTS)


def build_files_component(pack: dict, root: str) -> List[Tuple[str, str, float]]:
    """`kind: files` 的组件：按 `items` 的 from→to 复制（from 相对仓库，to 相对包根）。"""
    done: List[Tuple[str, str, float]] = []
    for item in pack.get("items") or []:
        src = os.path.join(ROOT, *str(item["from"]).replace("/", os.sep).split(os.sep))
        dst = os.path.join(root, *str(item["to"]).replace("/", os.sep).split(os.sep))
        if not os.path.isdir(src):
            raise PackError("组件要的文件不在仓库里：%s（%s）" % (item["from"], src))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        mb = _size_mb(dst)
        log("  文件 %-42s → %-42s %6.1f MB" % (item["from"], item["to"], mb))
        done.append((str(item["from"]), str(item["to"]), mb))
    return done


def make_zip(root: str, zip_path: str, top: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(root):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, root)
                zf.write(full, os.path.join(top, rel))     # **带顶层前缀**：解开是一个文件夹


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="出 ECHO 离线组件合集（D23）")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M"))
    ap.add_argument("--components", default=",".join(DEFAULT_COMPONENTS),
                    help="逗号分隔的组件 id（必须在 components/offline-pack.json 里）")
    ap.add_argument("--python-from", default="", help="可重定位 CPython 目录（默认找 uv 托管那份）")
    ap.add_argument("--pip-index", default=DEFAULT_INDEX)
    ap.add_argument("--no-zip", action="store_true", help="只出解开的目录，不打 zip")
    args = ap.parse_args(argv)

    spec = load_spec()
    wanted = [c.strip() for c in args.components.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in spec]
    if unknown:
        raise PackError("这些 id 不在 components/offline-pack.json 里：%s（规格里只有 %s）"
                        % (unknown, sorted(spec)))

    pkg_name = "%s-%s" % (PACKAGE_PREFIX, args.stamp)
    root = os.path.join(args.out, pkg_name)
    if os.path.exists(root):
        raise PackError("目标已存在：%s（换个 stamp 或先删掉）" % root)
    os.makedirs(root, exist_ok=False)
    log("=== 出离线组件合集：%s ===" % pkg_name)
    log("  组件：%s" % "、".join(wanted))

    python_from = args.python_from or find_uv_python()
    t0 = time.time()
    sizes: Dict[str, float] = {}
    for cid in wanted:
        pack = spec[cid]["pack"]
        kind = str(pack.get("kind") or "")
        dest_rel = str(pack.get("dest") or cid)
        if kind == "runtime":
            dest = os.path.join(root, *dest_rel.replace("/", os.sep).split(os.sep))
            build_runtime_core(pack, dest, python_from, args.pip_index)
            sizes[cid] = _size_mb(dest)
        elif kind == "files":
            items = build_files_component(pack, root)
            sizes[cid] = round(sum(i[2] for i in items), 1)
        else:
            raise PackError("还不认识的组件类型 kind=%r（id=%s）" % (kind, cid))

    total = _size_mb(root)
    log("  合计 %.1f MB（用时 %.0f 秒）" % (total, time.time() - t0))
    log("  解开目录（`install.ps1 -ComponentDir` 可直接指它）：%s" % root)

    zip_path = ""
    if not args.no_zip:
        zip_path = os.path.join(args.out, pkg_name + ".zip")
        make_zip(root, zip_path, pkg_name)
        log("  zip：%s（%.1f MB，sha256 %s）"
            % (zip_path, os.path.getsize(zip_path) / (1 << 20), sha256(zip_path)[:16]))

    log("\n=== 目标机怎么用 ===")
    log("  ① 把这个目录或 zip 放到安装根旁边（下面的 -ComponentDir 指过去）：%s" % root)
    log("  ② powershell -NoProfile -ExecutionPolicy Bypass -File "
        "'<安装根>\\echo-core\\scripts\\install.ps1' -DestDir '<安装根>' -Silent "
        "-ComponentDir '%s'" % root)
    log("     → install.ps1 会**优先**用离线包，跳过 uv / python.org / pip 那一整套动作")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackError as e:
        print("[x] %s" % e, file=sys.stderr)
        raise SystemExit(2)
