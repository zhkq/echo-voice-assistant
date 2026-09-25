#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_min_kit.py - 出「离线最小包」：dist\\ECHO-kit-min-<stamp>.zip

为什么要它
----------
用户那边的安装现在慢在**每一步都联网**（pip 装依赖、下模型）**加** agent 逐步确认。
`scripts/build_kit.py` 出的 kit 解决的是"包比源码旧"和"手工组包漂移"，但**不含离线载荷** ——
每台新机器都得重下 100+ MB 依赖和 189 MB 模型。

这个脚本出的是一个**离线最小实例**：主程序 + 安装技能 + 依赖 wheel + 流式模型 + 运行时兜底，
一条命令（`装我.cmd` / `install-offline.ps1`）装完，**全程不联网、也不绕 agent**。

包结构（与 build_kit 的 kit 同规矩：zip 里每个条目都带 `<kit 名>/` 顶层前缀）::

    ECHO-kit-min-<stamp>/
      ├─ ECHO\\…                              主包（复用 build_kit/build-package 的白名单，不手工列文件）
      ├─ echo-install\\…                      安装技能（保留：agent 那条路继续可用）
      ├─ bundle\\wheels\\…                    离线 wheel（pip --no-index --find-links）
      ├─ bundle\\runtime\\…                   兜底 CPython 3.11 嵌入包 + get-pip.py
      ├─ bundle\\models\\sherpa-onnx-streaming\\…  流式模型（从本机 models\\ 直接复制）
      ├─ 装我.cmd / install-offline.ps1 / 先读我.md
      └─ BUILD-INFO.txt / SHA256SUMS.txt（主包元数据，随主包解出来）

不做的事（刻意的）
------------------
* **不打 torch**（最小档的定义就是不含它，铁律 L1 —— components/profiles.json）；
* **不打 DSH / harness**（`@deepseek-ai/dsh` 是私有 npm 包，许可上不能随包分发）；
  所以离线路径的 `-Agent` 默认是 `none`，需要智能体时仍走联网/缓存那条路。
* **不打 CUDA / funasr / pyannote / whisper 系模型**：那些是"离线增强档"，按需在面板里下。

用法
----
    python scripts/build_min_kit.py                  # 出主包 + 组离线最小包 + 自检
    python scripts/build_min_kit.py --reuse-main     # 复用 dist 里已有的主包（快）
    python scripts/build_min_kit.py --stamp 20260925-2200
    python scripts/build_min_kit.py --engines sherpa,whisper-base
    python scripts/build_min_kit.py --no-runtime     # 不打 python.org 嵌入包（小一点，但裸机装不了）
    python scripts/build_min_kit.py --no-models      # 不打模型（只出 wheel 包）
    python scripts/build_min_kit.py --wheels-from-env  # 用本机已装环境的版本号下 wheel

退出码：0 正常；1 出错。
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

# 中文输出在 Windows 上被「管道捕获」时会按本地码页编码，落地即乱码 —— 强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DIST = ROOT / "dist"
DELIVERY = ROOT / "delivery"
OFFLINE_TPL = DELIVERY / "offline"
SKILL_DIR = ROOT / ".dsh" / "skills" / "echo-install"
COMPONENTS_PS1 = SKILL_DIR / "scripts" / "echo-install-components.ps1"
REQUIREMENTS_CORE = ROOT / "requirements-core.txt"
MODELS_DIR = ROOT / "models"

KIT_PREFIX = "ECHO-kit-min"
KIT_README = "先读我.md"
EMBED_NAME = "python-3.11.9-embed-amd64.zip"
EMBED_URL = "https://www.python.org/ftp/python/3.11.9/" + EMBED_NAME
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
#: 目标运行时是包里的嵌入包（3.11），wheel 必须是 cp311 —— 用别的解释器下会拿到错版本
TARGET_PY = (3, 11)
#: 模型下载客户端（modelscope）：**打进 wheel** 而不是"离线时跳过" —— 见 README/报告的说明。
#: 它同时是 requirements-core.txt 的一行、也是 sherpa 档 ENGINE_MAP 的一项，
#: 所以这里不额外硬编码包名，只把它们从两个权威来源里取出来。
#: 只有这几个是为了"离线把 pip 装上"（嵌入包不带 pip / ensurepip）。
BOOTSTRAP_WHEELS = ("pip", "setuptools", "wheel", "packaging")
#: 最小档的定义：这些**不许**出现在包里（有 tests/test_default_profile.py 守着同一份事实）
NEVER_IN_MIN = ("torch", "torchvision", "torchaudio", "funasr", "pyannote.audio",
                "speechbrain", "transformers", "faster-whisper", "ctranslate2",
                "onnxruntime-gpu")

EXIT_OK = 0
EXIT_ERROR = 1


class BuildError(RuntimeError):
    """脚本自己能解释清楚的失败。"""


# --------------------------------------------------------------------- 复用 build_kit
def load_build_kit():
    """把 scripts/build_kit.py 当模块加载 —— 主包白名单/组装/zip 前缀规则只留一份。

    为什么不复制一份：`build-package.ps1` 才是"包里有哪些文件"的唯一事实源（白名单长在
    它里面），`build_kit.py` 是调用它的唯一入口；这里再写一遍就是两处真相，迟早漂移。
    """
    path = SCRIPTS / "build_kit.py"
    spec = importlib.util.spec_from_file_location("build_kit_shared", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def win_platform(build_kit) -> dict:
    plat = next((p for p in build_kit.PLATFORMS if p["key"] == "win"), None)
    if plat is None:
        raise BuildError("build_kit.PLATFORMS 里没有 win —— 平台矩阵改了？")
    return plat


# --------------------------------------------------------------------- 依赖清单
_PS1_ENGINE_RE = re.compile(
    r"^\s*'([^']+)'\s*=\s*@\{\s*pip\s*=\s*@\(([^)]*)\)", re.M)


def engine_pip_map() -> dict[str, list[str]]:
    """从技能里的组件安装器解析 $ENGINE_MAP 的 pip 依赖（**唯一实现**在那儿）。

    为什么不在这里手抄一份包名表：引擎→依赖的映射有权威出处（技能脚本 + app/install_state.py），
    手抄一份就是第三个真相。格式变了这里会**响亮地失败**（解析不出来就报错），不会静默漏包。
    """
    text = COMPONENTS_PS1.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for engine, pip_part in _PS1_ENGINE_RE.findall(text):
        out[engine] = re.findall(r"'([^']+)'", pip_part)
    if not out:
        raise BuildError("从 echo-install-components.ps1 里一条 $ENGINE_MAP 都没解析出来 —— "
                         "引擎表格式变了？")
    return out


def requirements_core_names() -> list[str]:
    names = []
    for line in REQUIREMENTS_CORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    if not names:
        raise BuildError(f"{REQUIREMENTS_CORE} 里一条依赖都没有")
    return names


def spec_list(engines: list[str], from_env: bool) -> tuple[list[str], list[str]]:
    """算出要下的 wheel 清单。返回 (requirements 行, 来源说明)。"""
    table = engine_pip_map()
    specs: list[str] = []
    why: list[str] = []
    for line in requirements_core_names():
        specs.append(line)
    why.append(f"requirements-core.txt（{len(specs)} 行：面板/服务骨架 + sherpa-onnx + modelscope）")
    for e in engines:
        if e not in table:
            raise BuildError(f"不认识的引擎：{e}（可选：{', '.join(sorted(table))}）")
        before = len(specs)
        specs += [p for p in table[e] if p not in specs]
        why.append(f"ENGINE_MAP['{e}'] 的 pip 依赖（+{len(specs) - before}：{' '.join(table[e])}）")
    # 任务书点名要覆盖的三件（sherpa 档的转写依赖）；requirements-core 里已有 soundfile /
    # sherpa-onnx / modelscope，onnxruntime 是 sherpa-onnx 在部分平台上的运行期依赖，
    # 显式带上（面板的组件清单也把它列在 stt 行）。
    for extra in ("onnxruntime",):
        if not any(s.split("=")[0].split(">")[0].split("[")[0].strip().lower() == extra for s in specs):
            specs.append(extra)
            why.append(f"显式带上 {extra}（sherpa 档的运行期依赖）")
    for b in BOOTSTRAP_WHEELS:
        if not any(s.split("=")[0].split(">")[0].split("[")[0].strip().lower() == b for s in specs):
            specs.append(b)
            why.append(f"离线装 pip 用：{b}（嵌入包不带 pip/ensurepip）")
    if from_env:
        why.append("--wheels-from-env：用本机已装版本号钉住（避免拿到未验证的新版本）")
    return specs, why


def pinned_specs(specs: list[str], py: list[str]) -> list[str]:
    """把 requirements 行按本机已装版本钉住（可选路径）。

    为什么要这个开关：`pip download` 不钉版本会拿"当下最新"（例如 numpy 2.5），而 kit 是
    给**同一批测试机**用的、也跟本机实测过的组合对得上会稳一点。不钉也能装，只是多一层不确定。
    """
    # 注意**保住 extras**（`uvicorn[standard]` 的方括号不能丢）：`uvicorn==x` 不会装
    # httptools/watchfiles/websockets 那几件，离线装出来的服务会缺东西。
    code = ("import importlib.metadata as m,re,sys\n"
            "for s in sys.argv[1:]:\n"
            "    n=re.split(r'[=<>!\\[]', s)[0].strip()\n"
            "    ex=re.search(r'\\[[^\\]]*\\]', s)\n"
            "    try: print(n+(ex.group(0) if ex else '')+'=='+m.version(n))\n"
            "    except Exception: print(s)\n")
    proc = subprocess.run(py + ["-c", code] + specs, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise BuildError("钉版本失败（本机环境读不到？）：\n" + (proc.stdout or "") + (proc.stderr or ""))
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


# --------------------------------------------------------------------- 找 Python 3.11
def _py_version(argv: list[str]) -> tuple[int, int] | None:
    try:
        proc = subprocess.run(argv + ["-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return None
    try:
        major, minor = out[-1].split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def _has_pip(argv: list[str]) -> bool:
    proc = subprocess.run(argv + ["-m", "pip", "--version"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode == 0


def pick_python(override: str) -> list[str]:
    """挑一个**能下 cp311 wheel** 的 pip 解释器。

    目标运行时是包里的 CPython 3.11（python.org 嵌入包）：用 3.12/3.13 的 pip 下 wheel 会
    拿到 cp312/cp313 的二进制轮子（numpy / onnxruntime / sherpa-onnx 都是二进制的），
    装到目标机上就是"下得到、import 不了"。所以这里**必须**是 3.11。
    """
    cands: list[list[str]] = []
    if override:
        cands.append([override])
    if sys.version_info[:2] == TARGET_PY:
        cands.append([sys.executable])
    for name in ("py", "python3.11", "python"):
        exe = shutil.which(name)
        if not exe:
            continue
        cands.append([exe, "-3.11"] if name == "py" else [exe])
    tried = []
    for argv in cands:
        ver = _py_version(argv)
        if ver != TARGET_PY:
            tried.append(f"{' '.join(argv)} -> {ver}")
            continue
        if not _has_pip(argv):
            tried.append(f"{' '.join(argv)} -> 没有 pip")
            continue
        print(f"  [py   ] 用 {' '.join(argv)} 下 wheel（Python {ver[0]}.{ver[1]}）")
        return argv
    raise BuildError("找不到 Python 3.11 的 pip（wheel 必须是 cp311，否则目标机 import 不了）。\n"
                     "  试过：" + "; ".join(tried) + "\n"
                     "  用 --python <3.11 解释器路径> 指定一个。")


# --------------------------------------------------------------------- 下载/复制载荷
def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  [dl   ] {url}")
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)
    print(f"          -> {dest.name}  ({dest.stat().st_size / (1 << 20):.2f} MB)")


def download_wheels(py: list[str], specs: list[str], dest: Path, cache: Path) -> list[Path]:
    """下 wheel 到 `dest`（先落到**跨次复用**的 `cache`，再拷进 kit）。

    `pip download` 对已经下过的同名文件会直接跳过（"File was already downloaded"），
    重出包时就不用再走一遍网络 —— 这正是任务书要的"优先用本机缓存，别重新下载"。
    缓存**不能**放在 kit 目录里（那个目录每次重出都会被删掉）。
    """
    cache.mkdir(parents=True, exist_ok=True)
    argv = py + ["-m", "pip", "download", "--dest", str(cache), "--only-binary=:all:",
                 "--disable-pip-version-check"] + specs
    print("  [pip  ] " + " ".join(argv))
    proc = subprocess.run(argv, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise BuildError(f"pip download 失败（exit={proc.returncode}）—— 看上面是哪一行")
    dest.mkdir(parents=True, exist_ok=True)
    for whl in sorted(cache.glob("*.whl")):
        shutil.copyfile(whl, dest / whl.name)
    return sorted(dest.glob("*.whl"))


def copy_model(src: Path, dest: Path) -> None:
    if not src.is_dir():
        raise BuildError(f"本机没有这个模型目录：{src}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / (1 << 20)
    print(f"  [model] {src.name} -> {dest.parent.name}/{dest.name}  ({mb:.2f} MB)")


def _tree_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1 << 20)


# --------------------------------------------------------------------- 组装
def stage_kit(args, build_kit) -> tuple[Path, dict]:
    plat = win_platform(build_kit)
    stamp = args.stamp
    dist = Path(args.dist).resolve() if args.dist else DIST
    dist.mkdir(parents=True, exist_ok=True)

    # ① 主包（白名单/排除规则都在 build-package.ps1 里；这里只是调用它）
    if args.reuse_main:
        main_zip = build_kit.newest_main_package(dist, plat)
        if main_zip is None:
            raise BuildError(f"--reuse-main 但 dist 里没有 ECHO-main-{plat['label']}-*.zip")
        print(f"  [main ] 复用 {main_zip.name}")
    else:
        main_zip = build_kit.build_main_packages([plat], dist)[plat["key"]]
    stats: dict = {"main_zip": main_zip.name, "main_mb": main_zip.stat().st_size / (1 << 20)}

    kit_dir = dist / f"{KIT_PREFIX}-{stamp}"
    if kit_dir.exists():
        shutil.rmtree(kit_dir)
    kit_dir.mkdir(parents=True)
    with zipfile.ZipFile(main_zip) as zf:
        zf.extractall(kit_dir)
    if not (kit_dir / "ECHO" / "app" / "main.py").is_file():
        raise BuildError(f"主包里没有 ECHO/app/main.py：{main_zip.name}")

    # ② 安装技能（原路径保留：agent 那条路继续可用）
    skill_dst = kit_dir / "echo-install"
    shutil.copytree(SKILL_DIR, skill_dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # ③ 离线载荷：wheel
    specs, why = spec_list(args.engines, args.wheels_from_env)
    py = pick_python(args.python)
    if args.wheels_from_env:
        specs = pinned_specs(specs, py)
    wheels_dir = kit_dir / "bundle" / "wheels"
    wheels = download_wheels(py, specs, wheels_dir, dist / "_offline-cache" / "wheels")
    stats["wheels"] = [(p.name, p.stat().st_size) for p in wheels]
    stats["wheels_mb"] = sum(s for _n, s in stats["wheels"]) / (1 << 20)
    stats["spec_why"] = why
    stats["pip_python"] = " ".join(py)

    # ④ 离线载荷：模型（本机已有，直接复制 —— 不重新下载）
    models_staged = []
    for mid, rel in (("sherpa", "sherpa-onnx-streaming"),):
        src = MODELS_DIR / rel
        if not src.is_dir():
            if args.allow_missing_models:
                print(f"  [model] 缺 {src} —— 跳过（--allow-missing-models）")
                continue
            raise BuildError(f"本机没有模型 {src}（离线最小包必须有它）。"
                             f"用 --allow-missing-models 跳过，或先在面板里下好这个模型。")
        copy_model(src, kit_dir / "bundle" / "models" / rel)
        models_staged.append(rel)
    stats["models"] = [(rel, _tree_mb(kit_dir / "bundle" / "models" / rel)) for rel in models_staged]

    # ⑤ 离线载荷：运行时兜底（python.org 嵌入包 + get-pip.py）
    runtime_files = []
    if args.no_runtime:
        print("  [rt   ] --no-runtime：不打 CPython 嵌入包（裸机没 Python 就装不了）")
    else:
        cache = dist / "_offline-cache"
        embed = cache / EMBED_NAME
        getpip = cache / "get-pip.py"
        if not embed.is_file():
            fetch(EMBED_URL, embed)
        if not getpip.is_file():
            fetch(GETPIP_URL, getpip)
        rt_dst = kit_dir / "bundle" / "runtime"
        rt_dst.mkdir(parents=True, exist_ok=True)
        for src in (embed, getpip):
            shutil.copyfile(src, rt_dst / src.name)
            runtime_files.append((src.name, (rt_dst / src.name).stat().st_size))
    stats["runtime"] = [(n, s) for n, s in runtime_files]

    # ⑥ 一条命令的入口 + 说明
    for name in ("装我.cmd", "install-offline.ps1"):
        src = OFFLINE_TPL / name
        if not src.is_file():
            raise BuildError(f"缺模板 {src}")
        shutil.copyfile(src, kit_dir / name)
    readme = DELIVERY / "kit-readme-offline.md"
    if not readme.is_file():
        raise BuildError(f"缺模板 {readme}")
    shutil.copyfile(readme, kit_dir / KIT_README)

    # ⑦ 载荷清单（人看得懂的那种；排障/对账用）
    lines = ["# bundle 清单（build_min_kit.py 生成）",
             f"generated : {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"pip python: {stats['pip_python']}",
             f"engines   : {', '.join(args.engines)}",
             ""]
    lines.append(f"[wheels] {len(wheels)} 个，合计 {stats['wheels_mb']:.1f} MB")
    for name, size in sorted(stats["wheels"], key=lambda x: -x[1]):
        lines.append(f"  {size / (1 << 20):8.2f} MB  {name}")
    lines.append("")
    lines.append(f"[models] 合计 {sum(s for _n, s in stats['models']):.1f} MB")
    for rel, mb in stats["models"]:
        lines.append(f"  {mb:8.2f} MB  {rel}")
    lines.append("")
    lines.append(f"[runtime] 合计 {sum(s for _n, s in stats['runtime']) / (1 << 20):.1f} MB")
    for n, s in stats["runtime"]:
        lines.append(f"  {s / (1 << 20):8.2f} MB  {n}")
    (kit_dir / "bundle" / "BUNDLE-INFO.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ⑧ BUILD-INFO：主包那份 + 本包的说明（与 build_kit 同一手法）
    short = build_kit.git_short()
    build_info = kit_dir / "BUILD-INFO.txt"
    if not build_info.is_file():
        raise BuildError(f"主包里没有 BUILD-INFO.txt：{main_zip.name}")
    block = [
        "",
        "kit contents (this archive)",
        f"  ECHO/           unpacked main package (profile=main) @ {short}",
        "  echo-install/   echo-install skill (agent path, still works offline)",
        f"  bundle/wheels/  {len(wheels)} offline wheels (pip --no-index --find-links)",
        f"  bundle/models/  {', '.join(models_staged) or '(none)'}",
        f"  bundle/runtime/ {', '.join(n for n, _s in stats['runtime']) or '(none)'}",
        "  装我.cmd        double click me (offline, one command)",
        "  install-offline.ps1  what 装我.cmd calls",
        f"  {KIT_README}       three sentences for the colleague",
        "",
        "NOTE: no DSH/harness in this kit (@deepseek-ai/dsh is a private npm package and",
        "      must not be redistributed). Offline installs use -Agent none by default.",
    ]
    body = build_info.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    build_info.write_text(body + "\n" + "\n".join(block) + "\n", encoding="utf-8")
    return kit_dir, stats


def verify_kit(kit_dir: Path, build_kit, args) -> list[str]:
    problems = []
    if not (kit_dir / "ECHO" / "app" / "main.py").is_file():
        problems.append("kit 里没有 ECHO/app/main.py")
    if not (kit_dir / "ECHO" / "scripts" / "install-all.ps1").is_file():
        problems.append("主包里没有 ECHO/scripts/install-all.ps1 —— 装我.cmd 会找不到入口")
    for name in ("装我.cmd", "install-offline.ps1", KIT_README, "BUILD-INFO.txt"):
        if not (kit_dir / name).is_file():
            problems.append(f"kit 根缺少 {name}")
    problems += build_kit.compare_skill(kit_dir)      # 技能必须与仓库一字不差（复用）
    problems += build_kit.compare_manifest(kit_dir)   # manifest 声明的东西必须真的在（复用）
    wheels = sorted((kit_dir / "bundle" / "wheels").glob("*.whl"))
    if not wheels:
        problems.append("bundle/wheels 里没有 wheel")
    for w in wheels:
        norm = w.name.lower().replace("_", "-")
        for never in NEVER_IN_MIN:
            if norm.startswith(never.replace(".", "-") + "-"):
                problems.append(f"最小档不许带 {never}：{w.name}")
    for need in ("fastapi", "uvicorn", "sherpa-onnx", "modelscope", "soundfile"):
        # wheel 文件名里 `_` 与 `-` 混用（sherpa_onnx-1.13.8-…whl）—— 两边都归一化再比。
        norm = need.replace("_", "-").lower()
        if not any(w.name.lower().replace("_", "-").startswith(norm) for w in wheels):
            problems.append(f"bundle/wheels 里缺 {need}")
    if not args.allow_missing_models:
        d = kit_dir / "bundle" / "models" / "sherpa-onnx-streaming"
        if not d.is_dir():
            problems.append("bundle/models 里没有流式模型 sherpa-onnx-streaming")
        else:
            names = [p.name for p in d.iterdir()]
            for pre, suf in (("encoder", ".onnx"), ("decoder", ".onnx"), ("joiner", ".onnx")):
                if not any(n.startswith(pre) and n.endswith(suf) for n in names):
                    problems.append(f"模型目录缺 {pre}*{suf}")
            if "tokens.txt" not in names:
                problems.append("模型目录缺 tokens.txt")
    if not args.no_runtime:
        rt = kit_dir / "bundle" / "runtime"
        for name in (EMBED_NAME, "get-pip.py"):
            if not (rt / name).is_file():
                problems.append(f"bundle/runtime 里缺 {name}（裸机没 Python 就装不了）")
    # 不许出现 DSH/harness 载荷
    for p in kit_dir.rglob("*"):
        low = p.name.lower()
        if low in ("node_modules",) or low.startswith("@deepseek-ai"):
            problems.append(f"kit 里不该有 DSH 载荷：{p.relative_to(kit_dir)}")
    return problems


def print_summary(kit_dir: Path, zip_path: Path, stats: dict) -> None:
    print("\n=== 离线最小包 ===")
    print(f"  kit 目录 : {kit_dir}")
    print(f"  zip      : {zip_path}")
    print(f"  zip 体积 : {zip_path.stat().st_size / (1 << 20):.2f} MB")
    print(f"  主包     : {stats['main_mb']:.2f} MB（{stats['main_zip']}）")
    print(f"  wheels   : {len(stats['wheels'])} 个 / {stats['wheels_mb']:.2f} MB")
    print(f"  模型     : {sum(s for _n, s in stats['models']):.2f} MB"
          f"（{', '.join(rel for rel, _mb in stats['models']) or '无'}）")
    print(f"  运行时   : {sum(s for _n, s in stats['runtime']) / (1 << 20):.2f} MB")
    print("\n  wheel 最大的 12 个：")
    for name, size in sorted(stats["wheels"], key=lambda x: -x[1])[:12]:
        print(f"    {size / (1 << 20):8.2f} MB  {name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出 ECHO 离线最小包（dist\\ECHO-kit-min-<stamp>.zip）")
    ap.add_argument("--stamp", default="", help="时间戳（默认 now，形如 20260925-2200）")
    ap.add_argument("--dist", default="", help="输出目录（默认 <repo>/dist）")
    ap.add_argument("--reuse-main", action="store_true", help="复用 dist 里已有的主包，不重出")
    ap.add_argument("--engines", default="sherpa", help="把哪些引擎的 pip 依赖打进 wheel（默认 sherpa）")
    ap.add_argument("--python", default="", help="下 wheel 用的 Python 3.11 解释器（默认自动找）")
    ap.add_argument("--wheels-from-env", action="store_true",
                    help="用本机已装环境的版本号钉住 wheel（更贴近实测过的组合）")
    ap.add_argument("--no-runtime", action="store_true", help="不打 python.org 嵌入包")
    ap.add_argument("--no-models", action="store_true", help="不打模型（只出 wheel 包）")
    ap.add_argument("--no-verify", action="store_true", help="跳过组装后的自检")
    args = ap.parse_args(argv)
    args.engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    args.allow_missing_models = bool(args.no_models)
    args.stamp = args.stamp or time.strftime("%Y%m%d-%H%M")

    print(f"=== build-min-kit (stamp={args.stamp}, engines={','.join(args.engines)}) ===")
    try:
        build_kit = load_build_kit()
        kit_dir, stats = stage_kit(args, build_kit)
        if not args.no_verify:
            print("\n=== verify ===")
            problems = verify_kit(kit_dir, build_kit, args)
            if problems:
                print(f"  [FAIL] {kit_dir.name}")
                for p in problems:
                    print(f"        - {p}")
                raise BuildError("自检没过 —— 这个包别发")
            print(f"  [ok]   {kit_dir.name}")
        dist = kit_dir.parent
        zip_path = dist / (kit_dir.name + ".zip")
        print("\n  [zip  ] 打包中（wheels/模型本身就是压缩过的，这一步只是走一遍 deflate）...")
        build_kit.make_zip(kit_dir, zip_path, kit_dir.name)   # 复用：条目一律带 <kit>/ 前缀
        print_summary(kit_dir, zip_path, stats)
        return EXIT_OK
    except BuildError as exc:
        print(f"\n[fail] {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
