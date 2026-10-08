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

## 两种方言（**同一份脚本，两条产出**）

### A. 组件合集（旧方言，`--components` / `--python-from`）

`install.ps1` 的 `-ComponentDir` 认的那一套。产出两样：

1. **解开的目录** `<out>/<pkg>/…`（`runtime-core/`、`models/sherpa-onnx-streaming/`）——
   `install.ps1` 的最后一条兜底就是"给我一个含 `runtime-core\\` 的目录"，**这条路不依赖 zip 内部约定**，
   所以它是最稳的交付形态：`-ComponentDir <out>/<pkg>` 即可。
2. **zip** `<out>/ECHO-离线组件合集-<stamp>.zip`（带顶层目录前缀）——给"要拷到别的机器"的场景。

### B. `bundle\\` 载荷（新方言，`--bundle`）

`scripts/install-all.ps1` 走的那一套（它把 `<KitRoot>\bundle` 自动探测出来，见该脚本
`Resolve-KitLayout` 的离线载荷段）。契约**逐行**照 install-all 的三个消费点写：

    <kit>\bundle\
      wheels\                              必需（-Offline 硬检查这个目录在不在）
                                           → pip install --no-index --find-links bundle\wheels -r requirements-core.txt
      models\sherpa-onnx-streaming\        模型，原样复制到 <安装根>\models\（唤醒主路 + 指令兜底）
      models\sensevoice-onnx\              指令转写的**默认**引擎（int8 ONNX，约 230 MB）
      models\wakeword\                     唤醒词 —— **默认不打**（wakeEnabled 出厂就是关的；
                                           启用唤醒时在面板里下；见 DEFAULT_BUNDLE_COMPONENTS）
      runtime\python-3.11.9-embed-amd64.zip  第④级兜底建运行时（tar 解压，名字必须一字不差）
      runtime\get-pip.py                     配套：python get-pip.py --no-index --find-links wheels

**为什么需要它**：kit 里带了 `bundle\\`，同事机器上 `install-all.ps1` 就自动走离线 ——
装的时候**一个字节都不下载**（Python 运行时/依赖/模型全在包里）。这条正是"同事拿到一个包、
双击、只问安装位置"的地基。wheels 与嵌入包那几件的实现**不在这份脚本里**，复用
`scripts/build_min_kit.py`（`pip download` 的版本钉法、`bundle/` 的校验清单都只有一份）。

## 两条纪律（这个脚本自己守着）

* **只打规格里声明过的组件**：id 不在 `offline-pack.json` 里 → 直接报错（不然会产出"清单里没有"的东西，
  而 `tests/test_default_profile.py` 正是钉"没有孤儿组件"的）；
* **`runtime-core` 不许含 torch**：`requirements-core.txt` 的铁律 L1（默认档零 torch），
  装完**实测** `importlib.util.find_spec("torch") is None`，不满足就失败 —— 这条是整个默认档
  "装得上、起得快"的地基。

用法：
    python scripts/build_offline_pack.py                     # 默认 runtime-core + stt-sherpa（组件合集）
    python scripts/build_offline_pack.py --components runtime-core
    python scripts/build_offline_pack.py --python-from <uv 的 CPython 目录>

    python scripts/build_offline_pack.py --bundle            # 出 bundle\ 载荷（给 install-all -Offline）
    python scripts/build_offline_pack.py --bundle --into dist/ECHO-kit-20260930-1234
                                                             # 直接写进 kit 根（不打 zip、不套目录）
    python scripts/build_offline_pack.py --bundle --no-runtime --no-models
                                                             # 只出 wheels（调试/小包）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(ROOT, "components", "offline-pack.json")
REQUIREMENTS = "requirements-core.txt"          # 规格里 runtime-core 用的那份
DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: kit 的 `manifest.json.requiredComponents` 就是这两个 —— 默认打它俩，装完即自足。
#: 2026-10-08：指令转写的默认引擎改成 `stt-sensevoice-onnx`，所以它也得进默认档
#: （否则新装机上默认引擎没有模型，`transcribe_ex` 会回落到 sherpa —— 能跑但叠字多）。
DEFAULT_COMPONENTS = ("runtime-core", "stt-sherpa", "stt-sensevoice-onnx")
PACKAGE_PREFIX = "ECHO-离线组件合集"
#: 装完必须能 import 的这些（少一个都说明这份运行时不能用）
IMPORT_CHECK = ("fastapi, uvicorn, pydantic, yaml, numpy, sounddevice, soundfile, soxr,"
                " sherpa_onnx, modelscope, httpx")

# ---- bundle 方言（契约见模块头 B）------------------------------------------------
#: bundle 的包名前缀（`--into` 之外，产出 `<out>/ECHO-bundle-<stamp>/bundle/…`）。
BUNDLE_PREFIX = "ECHO-bundle"
#: 默认打进 bundle 的组件 id。
#: **`wake-kws` 故意不在默认档**（2026-10-08 用户定）：语音唤醒 `wakeEnabled` 默认就是
#: **关**的，而这份唤醒模型 39.7 MB —— 关着的功能不该占离线包的体积。
#: 启用唤醒时在面板「设置 → 模型」里下载它（目录条目 `kws`）。
DEFAULT_BUNDLE_COMPONENTS = ("runtime-core", "stt-sherpa", "stt-sensevoice-onnx")
#: 模型类组件里"缺了就跳过"的（**`stt-sherpa` 与 `stt-sensevoice-onnx` 都是必需的**：
#: 前者是唤醒主路 + 指令兜底，后者是默认转写引擎；两个都缺就没法转了）。
OPTIONAL_MODEL_COMPONENTS = ()
#: 嵌入包与 get-pip 的**名字必须与 install-all.ps1 里写的一字不差**（它按名字找）：
#: `$zip = Join-Path $script:Bundle 'runtime\python-3.11.9-embed-amd64.zip'`。
EMBED_NAME = "python-3.11.9-embed-amd64.zip"
EMBED_URL = "https://www.python.org/ftp/python/3.11.9/" + EMBED_NAME
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
#: bundle 自检要求"必须在"的 wheel（少一个，目标机上那一步就一定失败）：
#: 前五个是默认档跑起来要的，最后四个是**离线把 pip 装上**要的（嵌入包不带 pip/ensurepip）。
#:
#: 2026-10-08 补 `pypinyin`：它是**唤醒的拼音容错层**要用的（`wake.py::_pinyin`），
#: 而它**从来没进过任何依赖清单** —— 结果客户机上导入失败被 `except` 静默吞掉，
#: 表现为"开发机能唤醒、客户机唤不醒"。光把它写进 `requirements-core.txt` 只盖住
#: "在线装/薄包"那条路，**离线装是从这个 wheelhouse 拿包**的，所以必须在这里也钉住：
#: 少了它，出包时**当场失败**，而不是等用户在客户机上发现唤不醒。
BUNDLE_REQUIRED_WHEELS = ("fastapi", "uvicorn", "sherpa-onnx", "modelscope", "soundfile",
                          "pypinyin",
                          "pip", "setuptools", "wheel", "packaging")
#: 模型目录里必须有的三件 onnx + 词表（判据与 build_min_kit.verify_kit 一致）。
MODEL_REQUIRED_FILES = (("encoder", ".onnx"), ("decoder", ".onnx"), ("joiner", ".onnx"))



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


# =============================================================== bundle 方言（B）
# wheels / 模型 / 嵌入包那三件的**实现只有一份**（`scripts/build_min_kit.py`）——
# 这里只做三件事：按 `components/offline-pack.json` 决定打哪些、把产出摆成 install-all
# 认的那个布局、以及**出包时就把"目标机能不能离线装上"验一遍**。
#
# 为什么不各写一份：`pip download` 必须用 **cp311** 的解释器（目标运行时是包里的
# python.org 3.11 嵌入包，用 3.12/3.13 的 pip 会拿到 cp312/cp313 的二进制轮子，
# 到目标机就是"下得到、import 不了"），而"离线把 pip 装上要哪几个 wheel"
# （BOOTSTRAP_WHEELS）也是那边定过的。抄一份 = 两处真相。

_HELPERS = None
#: 本脚本自己所在目录。**不要用 ROOT 定位同目录的脚本**：ROOT 是可被打桩的
#: （用例把它指到假仓库树），而 `build_min_kit.py` 是随仓库走的真实文件。
SELF_DIR = os.path.dirname(os.path.abspath(__file__))


def helpers():
    """懒加载 `scripts/build_min_kit.py`（只在 bundle 方言下才需要它）。"""
    global _HELPERS
    if _HELPERS is None:
        path = os.path.join(SELF_DIR, "build_min_kit.py")
        spec = importlib.util.spec_from_file_location("echo_build_min_kit", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _HELPERS = module
    return _HELPERS


def pick_python(override: str) -> List[str]:
    """挑一个能下 **cp311** wheel 的解释器（判据在 min_kit 里，别在这儿复制）。"""
    return helpers().pick_python(override)


def bundle_specs(from_env: bool) -> Tuple[List[str], List[str]]:
    """bundle 要下的 wheel 清单：**默认档（sherpa）那一路**。

    引擎→依赖的映射从技能脚本 `echo-install-components.ps1` 的 `$ENGINE_MAP` 解析出来
    （`min_kit.spec_list`），所以"引擎依赖变了但 bundle 没跟上"这件事不会静默发生。
    """
    return helpers().spec_list(["sherpa"], from_env)


def pinned_specs(specs: List[str], py: List[str]) -> List[str]:
    return helpers().pinned_specs(specs, py)


def download_wheels(py: List[str], specs: List[str], dest: str, cache: str) -> List:
    return helpers().download_wheels(py, specs, Path(dest), Path(cache))


def copy_model(src, dest) -> None:
    return helpers().copy_model(Path(src), Path(dest))


def fetch(url: str, dest) -> None:
    return helpers().fetch(url, Path(dest))


def bundle_model_items(spec: Dict[str, dict], wanted: Iterable[str]) -> List[Tuple[str, str, str]]:
    """从规格里算出要拷哪些模型目录 → `[(组件 id, 仓库内相对路径, 包内相对路径)]`。

    ⚠️ 目标机的 `install-all.ps1` 是**按 `bundle\\models\\<名字>` 找模型**的
    （`sherpa-onnx-streaming` / `wakeword`）——所以 `to` 必须原样摆过去，不能改名。
    """
    out: List[Tuple[str, str, str]] = []
    for cid in wanted:
        pack = spec[cid]["pack"]
        if str(pack.get("kind") or "") != "files":
            continue
        for item in pack.get("items") or []:
            out.append((str(cid), str(item["from"]).replace("\\", "/"),
                        str(item["to"]).replace("\\", "/")))
    return out


def offline_resolve_check(py: List[str], wheels_dir: str) -> Tuple[bool, str]:
    """**真的拿 pip 解一遍**：`--no-index --find-links <wheels> --dry-run -r requirements-core.txt`。

    为什么值得多花这十几秒：wheelhouse 里缺一个包，在**出包时**是一句清楚的报错；
    到了目标机上却是"离线装到一半失败"（那时同事已经在等，而且看不出缺的是哪个）。
    `--dry-run` 只解析、不安装、不联网。
    """
    argv = list(py) + ["-m", "pip", "install", "--dry-run", "--no-index",
                       "--find-links", wheels_dir, "--disable-pip-version-check",
                       "-r", os.path.join(ROOT, REQUIREMENTS)]
    code, out = _run(argv, timeout=1200)
    return code == 0, out


def build_bundle(bundle_dir: str, spec: Dict[str, dict], wanted: Iterable[str], *,
                 python_from: str = "", wheels_from_env: bool = False,
                 with_models: bool = True, with_runtime: bool = True,
                 cache_root: str = "", resolve_check: bool = True) -> Dict[str, object]:
    """把 `bundle\\` 载荷摆好 → 返回本次产出清单（也写进 `bundle/BUNDLE-INFO.txt`）。"""
    if os.path.exists(bundle_dir):
        # 重出时**只清这一个目录**（`--into` 指向的是别人的 kit，别动它别的部分）
        shutil.rmtree(bundle_dir)
    os.makedirs(bundle_dir)
    info: Dict[str, object] = {"dir": bundle_dir, "wheels": [], "models": [], "runtime": [],
                              "why": [], "pipPython": "", "resolveCheck": ""}

    # ① wheels：必需（install-all 的 -Offline 硬检查这个目录）
    py = pick_python(python_from)
    info["pipPython"] = " ".join(py)
    log("  [bundle] 下 wheel 用的 Python：%s（必须是 3.11：目标运行时就是 cp311）" % " ".join(py))
    specs, why = bundle_specs(wheels_from_env)
    if wheels_from_env:
        specs = pinned_specs(specs, py)
        why.append("--wheels-from-env：按本机已装版本钉住（更贴近实测过的组合）")
    info["why"] = why
    info["specs"] = specs
    for line in why:
        log("    · %s" % line)
    cache = cache_root or os.path.join(ROOT, "dist", "_offline-cache")
    wheels = download_wheels(py, specs, os.path.join(bundle_dir, "wheels"), cache)
    info["wheels"] = [(os.path.basename(str(p)), os.path.getsize(str(p))) for p in wheels]
    log("  [bundle] wheels：%d 个 / %.1f MB"
        % (len(info["wheels"]), sum(s for _n, s in info["wheels"]) / (1 << 20)))

    # ② models：原样复制（本机已经有 —— **不重新下载**）
    if with_models:
        for cid, src_rel, to_rel in bundle_model_items(spec, wanted):
            src = os.path.join(ROOT, *src_rel.split("/"))
            if not os.path.isdir(src):
                if cid in OPTIONAL_MODEL_COMPONENTS:
                    log("  [bundle] 跳过可选组件 %s：本机没有 %s（唤醒词没有就自动跳过）"
                        % (cid, src_rel))
                    continue
                raise PackError("组件 %s 要的模型不在仓库里：%s（先在面板里下好它）"
                                % (cid, src))
            dst = os.path.join(bundle_dir, *to_rel.split("/"))
            copy_model(src, dst)
            mb = _size_mb(dst)
            info["models"].append((to_rel, mb))
            log("  [bundle] 模型 %-34s %7.2f MB" % (to_rel, mb))
    else:
        log("  [bundle] --no-models：不打模型（目标机会去面板里自己下，需要网络）")

    # ③ runtime：python.org 嵌入包 + get-pip.py（第④级兜底建运行时；名字必须一字不差）
    if with_runtime:
        rt = os.path.join(bundle_dir, "runtime")
        os.makedirs(rt, exist_ok=True)
        for url, name in ((EMBED_URL, EMBED_NAME), (GETPIP_URL, "get-pip.py")):
            src = os.path.join(cache, name)
            if not os.path.isfile(src):
                fetch(url, src)
            shutil.copyfile(src, os.path.join(rt, name))
            size = os.path.getsize(os.path.join(rt, name))
            info["runtime"].append((name, size))
            log("  [bundle] 运行时兜底 %-38s %7.2f MB" % (name, size / (1 << 20)))
    else:
        log("  [bundle] --no-runtime：不打嵌入包与 get-pip.py（目标机没 Python 就装不了）")

    # ④ **出包时就把"目标机能不能离线装上"验一遍**（缺包在这里炸，不留到同事那边）
    if resolve_check:
        ok, out = offline_resolve_check(py, os.path.join(bundle_dir, "wheels"))
        info["resolveCheck"] = "ok" if ok else "failed"
        if not ok:
            tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-12:])
            raise PackError("wheelhouse 里缺东西 —— 离线解析 requirements-core.txt 就失败了：\n%s"
                            % tail)
        log("  [bundle] 离线解析自检：pip --no-index --dry-run -r %s ✅" % REQUIREMENTS)

    lines = ["# bundle 清单（build_offline_pack.py --bundle 生成；install-all.ps1 按它离线装）",
             "generated : %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "pip python: %s" % info["pipPython"],
             "components: %s" % ", ".join(wanted),
             "resolve   : %s" % (info["resolveCheck"] or "skipped"),
             "",
             "契约（scripts/install-all.ps1 的三个消费点，改名就装不上）：",
             "  wheels\\                             pip install --no-index --find-links bundle\\wheels",
             "  models\\sherpa-onnx-streaming\\       复制到 <安装根>\\models\\",
             "  models\\wakeword\\                    -Wake 才要；没有就自动跳过",
             "  runtime\\%s  第④级兜底建运行时" % EMBED_NAME,
             "  runtime\\get-pip.py                   配套（--no-index --find-links wheels）",
             ""]
    for line in why:
        lines.append("why: %s" % line)
    lines.append("")
    lines.append("[wheels] %d 个，合计 %.1f MB" % (len(info["wheels"]),
                                                  sum(s for _n, s in info["wheels"]) / (1 << 20)))
    for name, size in sorted(info["wheels"], key=lambda x: -x[1]):
        lines.append("  %8.2f MB  %s" % (size / (1 << 20), name))
    lines.append("")
    lines.append("[models] 合计 %.1f MB" % sum(mb for _r, mb in info["models"]))
    for rel, mb in info["models"]:
        lines.append("  %8.2f MB  %s" % (mb, rel))
    lines.append("")
    lines.append("[runtime] 合计 %.1f MB" % (sum(s for _n, s in info["runtime"]) / (1 << 20)))
    for name, size in info["runtime"]:
        lines.append("  %8.2f MB  %s" % (size / (1 << 20), name))
    with open(os.path.join(bundle_dir, "BUNDLE-INFO.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return info


def verify_bundle(bundle_dir: str, *, want_models: bool = True,
                  want_runtime: bool = True) -> List[str]:
    """bundle 的结构自检（**不抛**，返回问题清单）——判据与目标机那几个消费点一一对应。"""
    problems: List[str] = []
    wheels_dir = os.path.join(bundle_dir, "wheels")
    wheels = sorted(fn for fn in (os.listdir(wheels_dir) if os.path.isdir(wheels_dir) else [])
                    if fn.lower().endswith(".whl"))
    if not wheels:
        problems.append("bundle\\wheels 里没有 wheel（-Offline 硬检查这个目录）")
    names = [fn.lower().replace("_", "-") for fn in wheels]
    for need in BUNDLE_REQUIRED_WHEELS:
        norm = need.replace("_", "-").lower()
        if not any(fn.startswith(norm) for fn in names):
            problems.append("bundle\\wheels 里缺 %s" % need)
    if want_models:
        # 2026-10-08：默认档现在有**两个**转写模型，判据各不相同 ——
        #   * sherpa 流式：encoder*/decoder*/joiner* 三个 onnx + tokens.txt（唤醒主路 + 指令兜底）
        #   * sensevoice-onnx：model*.onnx + tokens.txt + silero_vad.onnx（指令默认引擎）
        # 少任一份都要在**出包时**就报出来：等装到同事机器上才发现"默认引擎没模型"，
        # 表现是识别质量悄悄退回流式的叠字，最难查。
        d = os.path.join(bundle_dir, "models", "sherpa-onnx-streaming")
        if not os.path.isdir(d):
            problems.append("bundle\\models 里没有 sherpa-onnx-streaming"
                            "（语音唤醒的主路 + 指令转写兜底都靠它）")
        else:
            have = os.listdir(d)
            for pre, suf in MODEL_REQUIRED_FILES:
                if not any(fn.startswith(pre) and fn.endswith(suf) for fn in have):
                    problems.append("模型目录缺 %s*%s" % (pre, suf))
            if "tokens.txt" not in have:
                problems.append("模型目录缺 tokens.txt")
        d2 = os.path.join(bundle_dir, "models", "sensevoice-onnx")
        if not os.path.isdir(d2):
            problems.append("bundle\\models 里没有 sensevoice-onnx"
                            "（默认转写引擎，缺了会静默回落到流式 sherpa）")
        else:
            have2 = os.listdir(d2)
            if not any(fn.startswith("model") and fn.endswith(".onnx") for fn in have2):
                problems.append("sensevoice-onnx 目录缺 model*.onnx")
            for need in ("tokens.txt", "silero_vad.onnx"):
                if need not in have2:
                    # VAD 缺了不致命（退回整段识别），但长音频会退化成几个字 —— 要报
                    problems.append("sensevoice-onnx 目录缺 %s" % need)
    if want_runtime:
        rt = os.path.join(bundle_dir, "runtime")
        for name in (EMBED_NAME, "get-pip.py"):
            if not os.path.isfile(os.path.join(rt, name)):
                problems.append("bundle\\runtime 里缺 %s" % name)
    if not os.path.isfile(os.path.join(bundle_dir, "BUNDLE-INFO.txt")):
        problems.append("bundle 根没有 BUNDLE-INFO.txt（载荷清单）")
    return problems


def run_bundle(args, spec: Dict[str, dict]) -> int:
    """`--bundle` 这条路的入口（组件合集那条路完全不受影响）。"""
    wanted = [c.strip() for c in args.components.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in spec]
    if unknown:
        raise PackError("这些 id 不在 components/offline-pack.json 里：%s（规格里只有 %s）"
                        % (unknown, sorted(spec)))
    has_runtime = any(str(spec[c]["pack"].get("kind")) == "runtime" for c in wanted)
    if not has_runtime:
        log("  [!] 选的组件里没有 runtime 类（wheels 还是要的：pip 得能离线装上）")

    pkg_dir = ""
    pkg_name = "%s-%s" % (BUNDLE_PREFIX, args.stamp)
    if args.into:
        root = os.path.abspath(args.into)
        if not os.path.isdir(root):
            raise PackError("--into 指向的目录不存在：%s（先 python scripts/build_kit.py 出 kit）"
                            % root)
        bundle_dir = os.path.join(root, "bundle")
        log("=== 出 bundle 载荷（直接写进 kit 根，不打 zip）：%s ===" % bundle_dir)
    else:
        pkg_dir = os.path.join(args.out, pkg_name)
        if os.path.exists(pkg_dir):
            raise PackError("目标已存在：%s（换个 stamp 或先删掉）" % pkg_dir)
        os.makedirs(pkg_dir, exist_ok=False)
        bundle_dir = os.path.join(pkg_dir, "bundle")
        log("=== 出 bundle 载荷：%s/bundle ===" % pkg_name)
    log("  组件：%s" % "、".join(wanted))

    t0 = time.time()
    info = build_bundle(bundle_dir, spec, wanted, python_from=args.python_from,
                        wheels_from_env=args.wheels_from_env,
                        with_models=not args.no_models, with_runtime=not args.no_runtime,
                        resolve_check=not args.no_resolve_check)
    total = _size_mb(bundle_dir)
    log("  合计 %.1f MB（用时 %.0f 秒）" % (total, time.time() - t0))

    problems = verify_bundle(bundle_dir, want_models=not args.no_models,
                             want_runtime=not args.no_runtime)
    if problems:
        raise PackError("bundle 自检没过（这个包别发）：\n  - " + "\n  - ".join(problems))
    log("  [ok] bundle 自检通过")

    zip_path = ""
    if pkg_dir and not args.no_zip:
        zip_path = pkg_dir + ".zip"
        make_zip(pkg_dir, zip_path, pkg_name)
        log("  zip：%s（%.1f MB，sha256 %s）"
            % (zip_path, os.path.getsize(zip_path) / (1 << 20), sha256(zip_path)[:16]))

    log("\n=== 目标机怎么用 ===")
    if args.into:
        log("  ① bundle 已在 kit 根：%s" % bundle_dir)
        log("  ② 同事双击 kit 根那个 .bat —— 它会自动发现 bundle\\ 并走 -Offline"
            "（装的时候不下载运行时/依赖/模型；DSH 标准版仍联网装）")
    else:
        log("  ① 把 zip 解开，把里面的 bundle\\ 整个拷到 kit 根（与 先读我.md 同级）")
        log("  ② 或直接重出到 kit 里：python scripts/build_offline_pack.py --bundle --into <kit 目录>")
        log("  （%s）" % (zip_path or pkg_dir))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="出 ECHO 离线组件合集 / bundle 载荷（D23）")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d-%H%M"))
    ap.add_argument("--components", default=",".join(DEFAULT_COMPONENTS),
                    help="逗号分隔的组件 id（必须在 components/offline-pack.json 里）")
    ap.add_argument("--python-from", default="", help="可重定位 CPython 目录（默认找 uv 托管那份）")
    ap.add_argument("--pip-index", default=DEFAULT_INDEX)
    ap.add_argument("--no-zip", action="store_true", help="只出解开的目录，不打 zip")
    # ---- bundle 方言（B）：给 install-all.ps1 的 -Offline 用 ----
    ap.add_argument("--bundle", action="store_true",
                    help="出 bundle\\ 载荷（wheels + models + runtime；kit 里带上它就自动离线装）")
    ap.add_argument("--into", default="",
                    help="--bundle 专用：直接写进 <dir>\\bundle（不套顶层目录、不打 zip）")
    ap.add_argument("--wheels-from-env", action="store_true",
                    help="--bundle 专用：wheel 按本机已装版本钉住")
    ap.add_argument("--no-models", action="store_true", help="--bundle 专用：不打模型")
    ap.add_argument("--no-runtime", action="store_true",
                    help="--bundle 专用：不打嵌入包与 get-pip.py")
    ap.add_argument("--no-resolve-check", action="store_true",
                    help="--bundle 专用：跳过 pip --dry-run 的离线解析自检")
    args = ap.parse_args(argv)

    spec = load_spec()
    if args.bundle:
        # bundle 默认多一件可选组件（唤醒词）：本机没有模型时会**跳过并说清**，不算失败
        if args.components == ",".join(DEFAULT_COMPONENTS):
            args.components = ",".join(DEFAULT_BUNDLE_COMPONENTS)
        return run_bundle(args, spec)

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
