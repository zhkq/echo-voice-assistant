#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扩展包（**不走容器**）的出包 —— 客户端简化第 3 步 · 批 5。

产物是一棵树（顺带一个 zip），交给同事在**目标机**上解包：

    ECHO-backend-portable-<stamp>/
      ├─ 先读我.md            这份包是什么、怎么起、怎么验
      ├─ manifest.json        包内容 + 变体 + 校验（安装侧据此判断"这是已解开的包"）
      ├─ app/  server/        源码（与容器档同一份）
      ├─ server.yaml.tmpl     模板：listen 127.0.0.1:8900 + admin_listen 127.0.0.1:8901 + local_pair
      ├─ runtime/             **随包 CPython + 依赖**（--runtime-from 给）
      ├─ wheels/              **离线 wheelhouse**（--wheels-from 给，可选）
      └─ scripts/install-*.ps1 / *.sh        把它装到 {echoBase}/backend 的脚本

## 为什么运行时与 wheelhouse 是**显式输入**，而不是"顺手拿开发机的 venv"

实施方案 §7-1 把"扩展包的 Python/torch 从哪来"列为**待拍板**的一项。在拍板之前，
这个脚本**不许**偷偷用开发机上的 `venv/`：venv 里带的是**绝对路径**与**这台机器的 CUDA 版本**，
拿它出的包在同事机器上的典型表现是 `import torch` 崩或 ABI 不符 —— 而那要到"每个 /v1/asr 都 503"
才现形（`AGENTS.md` 记过这个形状）。所以：

* 给了 `--runtime-from` / `--wheels-from` → 照做，并在**出包时**跑那条 ABI 校验（见下）；
* 一个都没给 → **响亮失败**，报错里写清三条出路（带 venv 快照 / 带 wheelhouse / 让目标机自己装）。

## 出包时必须跑的那条 ABI 校验

与 `server/Dockerfile` 构建期同一条判据（`app.backend_env.check_torch_abi`）：
**torch 与 torchaudio 的 CUDA 源标签一致**（都带 `+cu126`），且 `import torchaudio` 通过 ——
**版本号相等不是判据**（PyTorch 2.9 之后 torchaudio 停更）。这条必须在**出包时**炸，
不要留到目标机（那时只表现为"每个 /v1/asr 都 503"，很难查到是 ABI 不符）。

**未验证（照实说）**：这条路的**真机端到端**（干净 Windows + N 卡：解包 → 起 → 配对 → 就绪 →
真音频 `/v1/asr` 出文字）**还没做过** —— 它要么需要先定 §7-1、要么需要一台干净的 N 卡机器。
本文件与它的用例只保证"包的结构、自检与 ABI 闸门"是对的。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import zipfile
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 包名与两个"随包"目录（名字是契约：安装脚本按它拷贝）。
PACKAGE_PREFIX = "ECHO-backend-portable"
RUNTIME_DIR = "runtime"
WHEELS_DIR = "wheels"
SCRIPTS_DIR = "scripts"
MANIFEST = "manifest.json"
READ_ME = "先读我.md"
YAML_TMPL = "server.yaml.tmpl"
#: 源清单：薄包的"另一半"（从哪下、下什么）。写进包里给人看，也是安装侧的依据。
SOURCES = "sources.json"

#: 源码里**必须进包**的顶层项（少一个都不是能跑的包）。
#: 只有这两棵：`app/`（客户端共享的引擎层）与 `server/`（服务端）—— 说明文档由本脚本生成。
SOURCE_ITEMS = ("app", "server")

#: 打包**绝不**带的东西（安全 + 体积）：与容器档同一条纪律。
EXCLUDE_DIRS = {"__pycache__", ".git", ".github", ".idea", ".vscode",
                "data", "models", "dist", "logs", "venv", "runtime-core", "tests", "docs"}


class PackError(RuntimeError):
    """出包失败（**响亮**：报错里要写清缺什么 + 出路）。"""


# ---------------------------------------------------------------- 服务器配置模板

def render_server_yaml(port: int = 8900, admin_port: int = 8901,
                       root: str = "{ECHO_BASE}/backend") -> str:
    """扩展包的 `server.yaml` 模板：**只绑回环** + 本机自配对（与容器档同一套口径）。

    占位符只有 `{ROOT}`（安装脚本把它替换成实际落点）——**不在这里生成 jwt_secret**：
    那是"起本机后端"那一步的活（`app/backend_setup.ensure_secret()`：已有的一律沿用，
    绝不重生成），在出包时就写死一份会在所有人之间**共用同一个密钥**。
    """
    return "\n".join([
        "# ECHO 能力后端（扩展包形态）—— 安装脚本会把这份模板落到 {ROOT}/server.yaml。",
        "# 只服务本机：两个地址都只听回环；同机配对走后端自己写的 local-pair.json。",
        "# jwt_secret 由客户端的「起本机后端」在那台机器上生成并**持久沿用**（不在这里写死）。",
        "server:",
        "  listen: \"127.0.0.1:%d\"" % int(port),
        "  admin_listen: \"127.0.0.1:%d\"" % int(admin_port),
        "  state_root: \"%s/state\"" % root,
        "  local_pair: true",
        "  advertised_host: \"127.0.0.1\"",
        "tmp:",
        "  root: \"%s/tmp\"" % root,
        "models:",
        "  # 指向**客户端那份模型库**（不复制）；安装脚本会按目标机的实际路径替换",
        "  root: \"{MODELS_ROOT}\"",
        "  device: cuda",
        "auth:",
        "  enabled: true",
        "  mode: jwt",
        "  # jwt_secret 留空 —— 由「起本机后端」在那台机器上生成一次并一直沿用",
        "  jwt_secret: \"\"",
        "",
    ])


INSTALL_PS1 = r"""# ECHO 扩展包安装（Windows / PowerShell）。
# 把这份包的内容拷到 {ROOT}，并生成 server.yaml（只绑回环）。
param([string]$Root = "", [string]$ModelsRoot = "")
$ErrorActionPreference = "Stop"
$src = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = Join-Path (Split-Path -Parent $src) "backend" }
if (-not $ModelsRoot) { $ModelsRoot = Join-Path (Split-Path -Parent $src) "models" }
New-Item -ItemType Directory -Force -Path $Root | Out-Null
foreach ($d in @("app", "server", "runtime", "wheels", "scripts")) {
  $from = Join-Path $src $d
  if (Test-Path $from) { Copy-Item -Recurse -Force $from (Join-Path $Root $d) }
}
$tmpl = Get-Content -Raw (Join-Path $src "server.yaml.tmpl")
$tmpl = $tmpl.Replace("{ROOT}", $Root).Replace("{MODELS_ROOT}", $ModelsRoot)
Set-Content -Encoding UTF8 -Path (Join-Path $Root "server.yaml") -Value $tmpl
Write-Host "已装到 $Root" -ForegroundColor Green
Write-Host "下一步：回到 ECHO 面板「能力」页签点「起本机后端」（或「就绪自测」）。"
"""

INSTALL_SH = r"""#!/bin/sh
# ECHO 扩展包安装（Linux / macOS）。
set -e
src="$(cd "$(dirname "$0")/.." && pwd)"
root="${1:-$(dirname "$(dirname "$src")")/backend}"
models="${2:-$(dirname "$(dirname "$src")")/models}"
mkdir -p "$root"
for d in app server runtime wheels scripts; do
  [ -e "$src/$d" ] && cp -R "$src/$d" "$root/"
done
sed -e "s#{ROOT}#$root#g" -e "s#{MODELS_ROOT}#$models#g" \
    "$src/server.yaml.tmpl" > "$root/server.yaml"
echo "已装到 $root"
echo "下一步：回到 ECHO 面板「能力」页签点「起本机后端」（或「就绪自测」）。"
"""


def render_readme(*, variant: str, port: int, admin_port: int, runtime_from: str,
                  wheels_from: str, with_models: bool, thin: bool = False,
                  has_python: bool = False) -> str:
    return "\n".join([
        "# ECHO 能力后端（扩展包形态 / 不走容器）",
        "",
        "这一份是**扩展包**：目标机不需要 Docker，直接以本机进程形态跑。",
        "",
        "| | |",
        "|---|---|",
        "| 形态 | %s |" % ("**薄包**（运行时按国内源现装）" if thin else "厚包（运行时随包）"),
        "| 变体 | %s |" % (variant or "（未指定）"),
        "| 监听 | `127.0.0.1:%d`（能力面） / `127.0.0.1:%d`（管理面）—— **只服务本机** |"
        % (int(port), int(admin_port)),
        "| 运行时 | %s |" % (("随包带：%s" % runtime_from) if runtime_from
                              else ("随包只带解释器（torch 按国内源现装）" if has_python
                                    else "**没带** —— 点「起本机后端」时按国内源现装")),
        "| wheelhouse | %s |" % (wheels_from or "（没带）"),
        "| 权重 | %s |" % ("随包带 `models/`" if with_models else "不带，复用目标机的模型库/ModelScope 缓存"),
        "",
        "## 怎么装",
        "",
        "1. 把整个文件夹解到一个**没有中文、没有空格**的路径下（Windows 上尤其）；",
        "2. 跑 `scripts/install-windows.ps1`（或 `scripts/install-posix.sh`）——",
        "   它把源码/运行时拷到 `{echoBase}/backend` 并生成 `server.yaml`；",
        "3. 回到 ECHO 面板「能力」页签 →「起本机后端」→ 它会再写一遍配置（jwt_secret 只生成一次）"
        "、起进程、读本机配对文件自动配对，最后跑**三层就绪自测**；",
        "4. 自测里的第三层是**一次真音频转写** —— 只有它挡得住"
        "「health 全绿、每个 /v1/asr 都 503」那种状态。",
        "",
        "## 必须自己验的两步（脚本验不了）",
        "",
        "* `runtime/` 里那份 python 的 torch/torchaudio **CUDA 源标签必须一致**"
        "（出包时跑过一次；换机器/换驱动后请再用「就绪自测」验一遍）；",
        "* 起好之后**在另一台机器**上 `curl http://<这台机器的 IP>:%d/v1/health` 必须**连不上**"
        "（只绑回环的判据；连得上说明哪里把端口放开到网段了）。" % int(port),
        "",
        "> ⚠️ **这份包的真机端到端还没验过**（干净 Windows + N 卡：解包 → 起 → 配对 → 就绪 → "
        "真音频出文字）。出包脚本与它的用例只保证结构、自检与 ABI 闸门是对的。",
        "",
    ])


# ---------------------------------------------------------------- 出包

def _copy_source(dst: str) -> List[str]:
    """把源码拷进包（按 `EXCLUDE_DIRS` 过滤）→ 返回相对路径清单。"""
    out: List[str] = []
    for item in SOURCE_ITEMS:
        src = os.path.join(ROOT, item)
        if not os.path.exists(src):
            raise PackError("源码里找不到 %s —— 出包前的树不对（%s）" % (item, ROOT))
        if os.path.isfile(src):
            target = os.path.join(dst, item)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            out.append(item)
            continue
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in files:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, ROOT)
                target = os.path.join(dst, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(full, target)
                out.append(rel.replace("\\", "/"))
    return out


def _copy_tree(src: str, dst: str, what: str) -> List[str]:
    if not src:
        return []
    if not os.path.isdir(src):
        raise PackError("%s 不存在：%s" % (what, src))
    out: List[str] = []
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, src)
            target = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)
            out.append(rel.replace("\\", "/"))
    if not out:
        raise PackError("%s 是空的：%s（空的运行时/仓库等于没带）" % (what, src))
    return out


def abi_check(runtime_from: str) -> Dict[str, object]:
    """出包时的 ABI 闸门（判据与 `server/Dockerfile` 构建期同一条）。"""
    from app import backend_env
    exe = ""
    for rel in (os.path.join("Scripts", "python.exe"), os.path.join("bin", "python3"),
                os.path.join("bin", "python")):
        cand = os.path.join(runtime_from, rel)
        if os.path.isfile(cand):
            exe = cand
            break
    if not exe:
        raise PackError("runtime-from 里没找到解释器（找过 Scripts/python.exe 与 bin/python3）：%s"
                        % runtime_from)
    got = backend_env.check_torch_abi(exe)
    if not got.get("ok"):
        raise PackError("随包运行时的 torch/torchaudio ABI 不符：%s\n"
                        "（判据是**CUDA 源标签一致**：都带 +cu126/+cu118；版本号相等不是判据。"
                        "修法：两者同一条 pip、同一个 index-url 装。）" % got.get("error"))
    return got


def stage(out_dir: str, *, variant: str = "", runtime_from: str = "",
          wheels_from: str = "", models_from: str = "", port: int = 8900,
          admin_port: int = 8901, stamp: str = "",
          python_from: str = "") -> Tuple[str, str, Dict[str, object]]:
    """组出扩展包 → ``(kit_dir, zip_path, info)``。

    **默认出薄包**（用户 2026-09-30 拍板："默认 = 薄包 + 国内可下载"）：
    源码 + 配置模板 + 安装脚本 + 源清单（`sources.json`）+ 可选解释器，
    **几 GB 的 torch 不随包走** —— 目标机点「起本机后端」时由 `app/backend_fetch.py`
    从国内镜像现装（SJTU 的 CUDA 索引 + 清华/阿里 PyPI；可达性 2026-09-30 实测过）。

    给了 `--runtime-from` 就是**厚包**（离线场景：完整运行时随包走，出包时过 ABI 闸门）。
    """
    thin = not runtime_from
    stamp = stamp or time.strftime("%Y%m%d-%H%M")
    name = "%s-%s" % (PACKAGE_PREFIX, stamp)
    kit_dir = os.path.join(out_dir, name)
    if os.path.exists(kit_dir):
        raise PackError("目标目录已存在：%s（换个 stamp 或先删掉）" % kit_dir)
    # **闸门放在建目录之前**：ABI 不符时连半个包都不该留下（否则下次出包要手工清）
    abi: Dict[str, object] = {}
    if runtime_from:
        abi = abi_check(runtime_from)
        if not abi.get("ok"):
            # 双保险：`abi_check()` 自己会抛，但**出包这条路绝不允许**带着未通过的校验往下走
            raise PackError("随包运行时的 ABI 校验没通过：%s" % (abi.get("error") or abi))
    os.makedirs(kit_dir, exist_ok=False)
    info: Dict[str, object] = {"kit": kit_dir, "variant": variant, "files": 0, "thin": thin}

    files = _copy_source(kit_dir)
    if runtime_from:
        files += ["%s/%s" % (RUNTIME_DIR, rel) for rel in
                  _copy_tree(runtime_from, os.path.join(kit_dir, RUNTIME_DIR), "runtime")]
    elif python_from:
        # 薄包也可以**只带解释器**（几十 MB、与卡无关）：torch 仍然按国内源现装。
        files += ["%s/%s" % (RUNTIME_DIR, rel) for rel in
                  _copy_tree(python_from, os.path.join(kit_dir, RUNTIME_DIR), "python")]
    files += ["%s/%s" % (WHEELS_DIR, rel) for rel in
              _copy_tree(wheels_from, os.path.join(kit_dir, WHEELS_DIR), "wheels")] \
        if wheels_from else []
    if models_from:
        files += ["models/%s" % rel for rel in
                  _copy_tree(models_from, os.path.join(kit_dir, "models"), "models")]

    with open(os.path.join(kit_dir, YAML_TMPL), "w", encoding="utf-8") as fh:
        fh.write(render_server_yaml(port=port, admin_port=admin_port))
    scripts = os.path.join(kit_dir, SCRIPTS_DIR)
    os.makedirs(scripts, exist_ok=True)
    with open(os.path.join(scripts, "install-windows.ps1"), "w", encoding="utf-8") as fh:
        fh.write(INSTALL_PS1)
    with open(os.path.join(scripts, "install-posix.sh"), "w", encoding="utf-8") as fh:
        fh.write(INSTALL_SH)
    with open(os.path.join(kit_dir, READ_ME), "w", encoding="utf-8") as fh:
        fh.write(render_readme(variant=variant, port=port, admin_port=admin_port,
                               runtime_from=runtime_from, wheels_from=wheels_from,
                               with_models=bool(models_from), thin=thin,
                               has_python=bool(python_from or runtime_from)))
    # 源清单（薄包的"另一半"）：目标机上 `app/backend_fetch.py` 按它装运行时；
    # 写进包里也是给人看的 —— "这一档从哪下、下什么"不该只活在代码里。
    sources = {
        "mode": "thin" if thin else "thick",
        "variant": variant,
        "pipIndexes": ["https://pypi.tuna.tsinghua.edu.cn/simple",
                       "https://mirrors.aliyun.com/pypi/simple/"],
        "torchIndexTmpl": "https://mirror.sjtu.edu.cn/pytorch-wheels/%s/",
        "torchIndex": "https://mirror.sjtu.edu.cn/pytorch-wheels/%s/" % (variant or "cu126"),
        "note": ("薄包：几 GB 的 torch 不随包走；目标机点「起本机后端」时按这些源现装"
                 "（`app/backend_fetch.py`）。源可达性 2026-09-30 实测。"),
    }
    with open(os.path.join(kit_dir, SOURCES), "w", encoding="utf-8") as fh:
        json.dump(sources, fh, ensure_ascii=False, indent=2)
    files += [YAML_TMPL, READ_ME, SOURCES,
              "%s/install-windows.ps1" % SCRIPTS_DIR, "%s/install-posix.sh" % SCRIPTS_DIR]

    manifest = {
        "package": "backend-portable",
        "mode": "thin" if thin else "thick",
        "variant": variant,
        "stamp": stamp,
        "port": int(port),
        "adminPort": int(admin_port),
        "hasRuntime": bool(runtime_from or python_from),
        "hasWheels": bool(wheels_from),
        "hasModels": bool(models_from),
        "abi": {"ok": bool(abi.get("ok")), "torch": abi.get("torch", ""),
                "torchaudio": abi.get("torchaudio", "")},
        "files": sorted(files),
        "count": len(files),
        "note": ("扩展包（不走容器）：只绑回环 + 本机自配对；jwt_secret 由客户端在那台机器上生成。"
                 + ("薄包：运行时按 `sources.json` 的国内源现装（`app/backend_fetch.py`）。"
                    if thin else "厚包：运行时随包走，出包时已过 ABI 闸门。")),
    }
    with open(os.path.join(kit_dir, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    info["count"] = len(files)
    info["manifest"] = manifest

    zip_path = kit_dir + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, names in os.walk(kit_dir):
            for fn in names:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, kit_dir)
                zf.write(full, os.path.join(name, rel))    # **带顶层目录前缀**（解包得到一个文件夹）
    info["zip"] = zip_path
    info["zipBytes"] = os.path.getsize(zip_path)
    return kit_dir, zip_path, info


def verify(kit_dir: str) -> List[str]:
    """包自检：**缺什么就列什么**（不抛，返回问题清单）。"""
    problems: List[str] = []
    for rel in ("app", "server", YAML_TMPL, READ_ME, MANIFEST,
                os.path.join(SCRIPTS_DIR, "install-windows.ps1"),
                os.path.join(SCRIPTS_DIR, "install-posix.sh")):
        if not os.path.exists(os.path.join(kit_dir, rel)):
            problems.append("包内缺 %s" % rel)
    try:
        with open(os.path.join(kit_dir, MANIFEST), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception as e:
        return problems + ["manifest.json 读不出来：%s" % e]
    if manifest.get("hasRuntime"):
        exe = ""
        for rel in (os.path.join("Scripts", "python.exe"), os.path.join("bin", "python3"),
                    os.path.join("bin", "python")):
            cand = os.path.join(kit_dir, RUNTIME_DIR, rel)
            if os.path.isfile(cand):
                exe = cand
                break
        if not exe:
            problems.append("manifest 说有运行时，但 runtime/ 里没有解释器")
        elif not manifest.get("abi", {}).get("ok"):
            problems.append("manifest 里 abi.ok 不是真 —— 出包时没跑 ABI 校验？")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="出 ECHO 扩展包（不走容器）")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--variant", default="", help="cu126 / cu118（写进 manifest 与说明）")
    ap.add_argument("--runtime-from", default="", help="厚包：随包一份完整运行时（含 Scripts/python.exe 或 bin/python3）")
    ap.add_argument("--python-from", default="", help="薄包可只带解释器（几十 MB、与卡无关）")
    ap.add_argument("--wheels-from", default="", help="离线 wheelhouse 目录（可选）")
    ap.add_argument("--models-from", default="", help="权重目录（可选；不给就不带）")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--admin-port", type=int, default=8901)
    ap.add_argument("--stamp", default="")
    args = ap.parse_args(argv)
    try:
        kit_dir, zip_path, info = stage(args.out, variant=args.variant,
                                        runtime_from=args.runtime_from,
                                        wheels_from=args.wheels_from,
                                        models_from=args.models_from,
                                        port=args.port, admin_port=args.admin_port,
                                        stamp=args.stamp, python_from=args.python_from)
    except PackError as e:
        print("[x] %s" % e, file=sys.stderr)
        return 2
    problems = verify(kit_dir)
    print("出包：%s（%d 个文件，zip %.1f MB）"
          % (zip_path, info["count"], info["zipBytes"] / (1 << 20)))
    if problems:
        print("[!] 自检没过：", file=sys.stderr)
        for p in problems:
            print("    - %s" % p, file=sys.stderr)
        return 3
    print("[ok] 自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
