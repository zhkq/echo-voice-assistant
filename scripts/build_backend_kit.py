#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_backend_kit.py - 出「后端交付包」：两个变体各一个 zip。

    dist/ECHO-backend-kit-cu126-<stamp>.zip     新卡（Turing 及以后，sm_75+）
    dist/ECHO-backend-kit-cu118-<stamp>.zip     老卡（Maxwell/Pascal/Volta，sm_<=7.0）

为什么要有它
------------
后端要分享给**另一个人的显卡环境**：同事的卡可能是 2080（Turing）、也可能是 1070
（Pascal），而这两类卡的**镜像不一样**（torch 从 cu126 还是 cu118 装 —— 装错的那个
镜像照样构建成功、只是看不到这块 GPU）。于是交付物不是"一个后端"，而是**两个按卡分流的
交付目录** + 一份给非开发者看的 `先读我.md`。

组包这件事在本仓库有前科（2026-09-22：`dist/` 里的 kit 比源码旧了 9 小时，13:30-15:00
的修复一个都没进包，根因是"组 kit 一直是手工活"）。所以这里照 `scripts/build_kit.py` /
`scripts/build_min_kit.py` 的规矩来：**包只由脚本出**，模板在 git 里（`delivery/`），
每个条目带 `<kit 名>/` 顶层前缀（少了它解包出来是一堆散文件，而且**不报错**）。

包结构（两个变体只差 `compose.yaml` / `.env.example` / `server.yaml` / `先读我.md`）::

    ECHO-backend-kit-cu126-<stamp>/
      ├─ 先读我.md          ← delivery/backend-cu126/先读我.md（给同事的中文说明）
      ├─ compose.yaml       ← 自包含的编排（image tag、GPU 直通、8900/8901 发布、两个卷）
      ├─ .env.example       ← 密钥 / 卷路径模板
      ├─ server.yaml        ← 该变体的 models.specs（`--config` 挂进容器；**没有**对应环境变量）
      ├─ server/  app/      ← **只带后端要的两层**（Dockerfile 就是 COPY 这两层）
      ├─ scripts/prepare-backend.sh, scripts/smoke-echo-backend.py
      ├─ BUILD-INFO.txt     ← 变体 / 时间 / git 短哈希 / 镜像 tag / torch 源
      └─ SHA256SUMS.txt     ← 每个条目的 sha256（也是 --check 的判据）

**明确不带**（安全与体积）：`data/`、`models/`、`__pycache__/`、`.git/`、`dist/`、
任何密钥与证书（`*.env` / `*.key` / `*.pem` / `*.crt` / `settings.yaml` / `.credentials`）。
模型不随镜像也不随包 —— 权重从宿主只读挂载进容器（`先读我.md` 第 4 节）。

用法
----
    python scripts/build_backend_kit.py                    # 两个变体都出
    python scripts/build_backend_kit.py --only cu118       # 只出老卡那个
    python scripts/build_backend_kit.py --out dist --stamp 20260928-0130
    python scripts/build_backend_kit.py --check            # 只报告 dist 是否过期（不写盘）
    python scripts/build_backend_kit.py --no-verify

退出码
------
    0  正常；或 --check 判定「一致」
    2  --check 判定「过期」（改过 / 新增 / 删除过文件）
    3  --check 判定「还没有包」
    1  出错

契约由 `tests/test_build_backend_kit.py` 钉住（两个目录都在、compose 的端口与 GPU 直通、
两份说明的关键结论、zip 的顶层前缀、排除项、SHA256SUMS）。
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import sys
import time
from pathlib import Path

# 中文输出在 Windows 上被「管道捕获」时会按本地码页（GBK）编码，落地即乱码 —— 强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DIST = ROOT / "dist"
DELIVERY = ROOT / "delivery"

#: 包名前缀。zip 里每个条目都带 `<kit 名>/`（= 目录名 = 这个前缀 + 变体 + 时间戳）。
KIT_PREFIX = "ECHO-backend-kit"
#: 给人的那份说明叫这个名字（中文名是刻意的：同事一眼知道先读它）
KIT_README = "先读我.md"

#: 变体矩阵。两个变体**只差**四份交付目录里的文件 + 镜像 tag + torch 源，
#: server/ 与 app/ 是同一份源码（torch 从哪个 CUDA 源装是**构建期参数**，不是另一份代码）。
#: `key` 同时决定 zip 名、镜像 tag 与目录名，所以它是这三处的唯一事实源。
VARIANTS = (
    {
        "key": "cu126",
        "delivery": "backend-cu126",
        "label": "新卡（Turing 及以后，sm_75+；Ampere/Ada/Blackwell 最佳）",
        "image": "echo-backend:0.1.0-cu126",
        "torch_index": "https://download.pytorch.org/whl/cu126",
        "torch_version": "",            # 该源上最新的一版
        # 「含权重」档要拷哪几棵子树（相对模型根）。**名字就是容器里找的那个路径**，
        # HF 缓存那种长名（`hub/models--Qwen--Qwen3-ASR-0.6B`）别改写。
        "models": ("hub/models--Qwen--Qwen3-ASR-0.6B",
                   "hub/models--Qwen--Qwen3-ForcedAligner-0.6B",
                   "sensevoice",
                   "pyannote"),
    },
    {
        "key": "cu118",
        "delivery": "backend-cu118",
        "label": "老卡（Maxwell/Pascal/Volta，sm_<=7.0）",
        "image": "echo-backend:0.1.0-cu118",
        "torch_index": "https://download.pytorch.org/whl/cu118",
        "torch_version": "2.7.1",       # cu118 源上带 Pascal(sm_61) 的最后一版，**必须钉**
        # 老卡**只要 SenseVoice**：镜像里没有 qwen-asr（会被 torch 版本校验拦住），
        # 也没有 pyannote（4.x 要 torch>=2.8）。拷多了只是白占几个 GB。
        "models": ("sensevoice",),
    },
)

#: 「含权重」档在包根留的说明文件名（自检与 `--check` 都靠它判断"这个包带没带权重"）。
MODELS_NOTE = "MODELS-INCLUDED.txt"

#: 含权重包超过这个体积就**提醒一声**（不是失败：权重本来就大）。
MODELS_WARN_BYTES = 2 * 1024 ** 3

#: 「后端要的两层」——与 `server/Dockerfile` 的 `COPY server/ app/` 对齐，不多不少。
BACKEND_TREES = ("server", "app")
#: 从仓库里单独点名的两个脚本（不是整层 scripts/：那是给客户端的，后端不需要）
BACKEND_FILES = ("scripts/prepare-backend.sh", "scripts/smoke-echo-backend.py")
#: 每个变体的交付目录里**必须**有的模板（缺一个就别出包）。
#: **这张表同时是两个变体的"不许漂"契约**：`tests/test_build_backend_kit.py` 直接派生它，
#: 于是"某个变体多出一份只发给自己的文档"这种事会在出包与用例两处一起报错
#: （2026-09-28：cu126 多了 `给同事Agent-构建说明.md` 而 cu118 没有，
#:  那条红就是这么来的 —— 修法是两边都补上，而不是把判据放宽）。
DELIVERY_REQUIRED = (KIT_README, "compose.yaml", ".env.example", "server.yaml",
                     "给同事Agent-构建说明.md")

#: 永远不打包的目录名（安全 + 体积）。`data/` 与 `models/` 是**业务数据与几 GB 权重**；
#: `__pycache__/` `.git/` `dist/` 是构建垃圾与产物。
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".github", ".idea", ".vscode",
    "data", "models", "dist", "logs",
    "node_modules", "venv", ".venv", ".ruff_cache", ".pytest_cache", ".mypy_cache", "obj",
}
#: 永远不打包的后缀：编译产物 / 库文件 / 私钥与证书 / 密钥环
EXCLUDE_SUFFIXES = (
    ".pyc", ".pyo", ".pyd", ".db", ".db-wal", ".db-shm", ".pid", ".log",
    ".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".jks", ".keystore", ".bak", ".orig",
)
#: 永远不打包的文件名（`.env` 与它的兄弟；`*.example` 是模板，要留）
EXCLUDE_NAMES = {".env", ".credentials", "settings.yaml", "id_rsa", "id_ed25519"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STALE = 2
EXIT_ABSENT = 3


class BuildError(RuntimeError):
    """脚本自己能解释清楚的失败，直接打给人看。"""


# --------------------------------------------------------------------- 复用 build_kit
def load_build_kit():
    """把 `scripts/build_kit.py` 当模块加载 —— zip 前缀规则与 git 信息只留一份。

    `make_zip()` 的 "<prefix>/ 顶层前缀"是本仓库踩过坑的约定（少了它解包散落一堆文件，
    而且**不报错**），`sha256_file()` / `git_short()` / `git_dirty()` 同理。
    再写一份就是两处真相，迟早漂移。
    """
    path = SCRIPTS / "build_kit.py"
    spec = importlib.util.spec_from_file_location("build_kit_shared", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- 排除规则
def is_excluded(rel_parts: tuple[str, ...], name: str, *, allow_models: bool = False) -> bool:
    """这个文件该不该进包（判据只此一处，打包与自检都走它）。

    `allow_models=True` 只放行**目录名那一层的 `models`** ——「含权重」档才用得上它；
    后缀类排除（`.db` / `.log` / `.pem` …）在权重目录里**照旧生效**，
    免得把缓存里的垃圾或密钥一起打进去。
    """
    dirs = rel_parts[:-1]
    if allow_models and dirs and dirs[0] == "models":
        dirs = dirs[1:]
    if any(part in EXCLUDE_DIRS for part in dirs):
        return True
    low = name.lower()
    if low in EXCLUDE_NAMES:
        return True
    # `.env` 的兄弟一律不带，但 `.env.example` 这类模板要留（同事拿它当填写模板）
    if low.startswith(".env.") and not low.endswith(".example"):
        return True
    if low.endswith(".env") and low != ".env.example":
        return True
    return low.endswith(EXCLUDE_SUFFIXES)


# --------------------------------------------------------------------- 打包清单
def planned_model_files(variant: dict, models_from: Path) -> dict[str, Path]:
    """**「含权重」档**要拷进包的文件：`models/<变体声明的那几棵子树>/**`。

    缺一棵就**失败**，不做"能拷多少拷多少"：含权重包的全部意义是拿来即用，
    少一棵子树的表现是"装完了某个引擎加载失败" —— 那种故障要到真机上才现形，
    而真机在同事手上。

    键带 `models/` 前缀（容器里按同一路径找：`ECHO_HOST_MODELS_DIR` 指向包内 `models/`）。
    """
    out: dict[str, Path] = {}
    wanted = tuple(variant.get("models") or ())
    missing = [rel for rel in wanted if not (models_from / rel).is_dir()]
    if missing:
        raise BuildError(
            "「含权重」档要求模型根里这几棵子树都在，缺：%s\n"
            "        模型根：%s\n"
            "        变体 %s 需要：%s\n"
            "        先把权重放齐（见 delivery/%s/先读我.md 第 4 节），"
            "或者出**纯代码包**（不加 --with-models）。"
            % (", ".join(missing), models_from, variant["key"], ", ".join(wanted),
               variant["delivery"]))
    for rel in wanted:
        src = models_from / rel
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            rel_inside = path.relative_to(models_from).as_posix()
            # 权重目录里**照旧**走后缀/文件名排除（缓存垃圾、密钥、证书一个都不带）
            if is_excluded(("models",) + path.relative_to(models_from).parts,
                           path.name, allow_models=True):
                continue
            out["models/" + rel_inside] = path
    if not out:
        raise BuildError("模型根 %s 下这几棵子树里一个文件都没有：%s"
                         % (models_from, ", ".join(wanted)))
    return out


def planned_files(variant: dict, models_from: Path | None = None) -> dict[str, Path]:
    """这个变体现在会打出哪些文件：`{包内相对路径: 仓库里的源文件}`。

    **唯一的事实源** —— 打包（stage）与过期检查（--check）都用它，所以两者不可能漂。
    `models_from` 给了就额外把**权重**按变体声明的子树拷进来（「含权重」档）。
    """
    ddir = DELIVERY / variant["delivery"]
    if not ddir.is_dir():
        raise BuildError(f"缺交付目录 {ddir}（模板必须在 git 里，不能从 dist 里捡）")
    missing = [n for n in DELIVERY_REQUIRED if not (ddir / n).is_file()]
    if missing:
        raise BuildError(f"{variant['delivery']}/ 里缺：{', '.join(missing)}")

    out: dict[str, Path] = {}
    # ① 交付目录里的模板（先读我.md / 给同事Agent-构建说明.md / compose.yaml / …）→ 包根
    for path in sorted(ddir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(ddir).parts
        if is_excluded(rel_parts, path.name):
            continue
        out[path.relative_to(ddir).as_posix()] = path
    # ② 后端要的两层源码 → 同名路径
    for tree in BACKEND_TREES:
        base = ROOT / tree
        if not base.is_dir():
            raise BuildError(f"缺目录 {base} —— 后端镜像就是 COPY 它")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = (tree,) + path.relative_to(base).parts
            if is_excluded(rel_parts, path.name):
                continue
            out[path.relative_to(ROOT).as_posix()] = path
    # ③ 两个脚本
    for rel in BACKEND_FILES:
        path = ROOT / rel
        if not path.is_file():
            raise BuildError(f"缺 {rel}")
        out[rel] = path
    # ④ 「含权重」档：把变体声明的那几棵模型子树拷进来（默认不走这一步）
    if models_from is not None:
        out.update(planned_model_files(variant, Path(models_from)))
    return out


def sums_text(files: dict[str, Path], build_kit) -> str:
    """写 `SHA256SUMS.txt`：`<sha256>  <包内相对路径>`（与 build-package.ps1 同格式）。"""
    lines = [f"{build_kit.sha256_file(files[rel])}  {rel}" for rel in sorted(files)]
    return "\n".join(lines) + "\n"


def parse_sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        digest, _, rel = line.strip().partition("  ")
        if rel.strip():
            out[rel.strip()] = digest.strip().lower()
    return out


def build_info_text(variant: dict, stamp: str, files: dict[str, Path], build_kit,
                    *, models_bytes: int = 0) -> str:
    short = build_kit.git_short() or "unknown"
    dirty = "（工作区有未提交改动）" if build_kit.git_dirty() else ""
    per_tree: dict[str, int] = {}
    for rel in files:
        per_tree[rel.split("/")[0]] = per_tree.get(rel.split("/")[0], 0) + 1
    pinned = (f"（钉 torch=={variant['torch_version']}）" if variant["torch_version"]
              else "（该源上最新的一版）")
    has_models = bool(models_bytes) or any(r.startswith("models/") for r in files)
    lines = [
        "# ECHO 能力后端交付包 —— this archive",
        f"variant      : {variant['key']} —— {variant['label']}",
        f"built        : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"stamp        : {stamp}",
        f"git          : {short}{dirty}",
        f"image        : {variant['image']}",
        f"torch source : {variant['torch_index']} {pinned}   （构建期参数，不是运行时开关）",
        "builder      : scripts/build_backend_kit.py",
        "",
        "contents（只带后端要的两层 + 交付目录里的模板）:",
        f"  {KIT_README} / 给同事Agent-构建说明.md / compose.yaml / .env.example / server.yaml",
        f"  server/    {per_tree.get('server', 0)} 个文件",
        f"  app/       {per_tree.get('app', 0)} 个文件",
        "  scripts/prepare-backend.sh, scripts/smoke-echo-backend.py",
        "",
    ]
    if has_models:
        lines += [
            f"**含权重**（{per_tree.get('models', 0)} 个文件，约 {models_bytes / (1 << 20):.0f} MB）:",
            "  models/ 就在包根 —— 把它挂给容器（ECHO_HOST_MODELS_DIR 指过来）即可，",
            f"  不用再去 ModelScope 下。清单与摆法见同目录 {MODELS_NOTE}。",
        ]
    else:
        lines += [
            "**不含权重**（纯代码包）: 模型不随镜像也不随包，",
            "  权重从宿主**只读挂载**进容器（见 先读我.md 第 4 节）。",
        ]
    lines += [
        "",
        "不含（刻意的）: data/ __pycache__/ .git/ dist/ 以及任何密钥与证书。",
        f"装法：先读 {KIT_README}（中文，给非开发者）；逐文件校验见同目录 SHA256SUMS.txt。",
    ]
    return "\n".join(lines) + "\n"


def models_note_text(variant: dict, files: dict[str, Path], models_bytes: int) -> str:
    """「含权重」档包根那份说明：里面是什么、怎么挂、别做什么。"""
    got = sorted({"/".join(r.split("/")[:2]) for r in files if r.startswith("models/")})
    return "\n".join([
        "# 这个包**含模型权重**（不是纯代码包）",
        "",
        f"变体      : {variant['key']} —— {variant['label']}",
        f"权重体积  : 约 {models_bytes / (1 << 20):.0f} MB（{len([r for r in files if r.startswith('models/')])} 个文件）",
        "",
        "包内这几棵子树：",
        *(f"  models/{name}/" for name in got),
        "",
        "## 怎么用（与 先读我.md 第 4 节的差别只有一步）",
        "",
        "权重**就在包根的 `models/`**，所以第 4 节里「从源机 rsync / 从 ModelScope 下」",
        "这一步可以跳过 —— 把 `ECHO_HOST_MODELS_DIR` 指向**解包后的 `models/`** 即可：",
        "",
        "```bash",
        f"# 在 .env 里（或直接 export）：",
        f"ECHO_HOST_MODELS_DIR=$(pwd)/models      # 解包目录下的 models/",
        "docker compose up -d",
        "```",
        "",
        "## 别做的两件事",
        "",
        "1. **别把 `models/` 拷进镜像** —— 它是只读挂载进来的（换权重不必重出镜像）。",
        "2. **别删包里的 `SHA256SUMS.txt`** —— 权重有几个 GB，传输坏了不会报错，",
        "   只会在加载时报一个看不懂的错。校验一遍：`sha256sum -c SHA256SUMS.txt`。",
        "",
        "> 本变体需要哪几个引擎由 `server.yaml` 的 `models.specs` 决定；",
        "> 权重与它不匹配时的表现是「某个 spec 一直 model_failed」，`/v1/health` 的 `models` 看得见。",
        "",
    ])


# --------------------------------------------------------------------- 出包
def stage(variant: dict, stamp: str, out: Path, build_kit,
          models_from: Path | None = None) -> tuple[Path, Path]:
    files = planned_files(variant, models_from)
    kit_dir = out / f"{KIT_PREFIX}-{variant['key']}-{stamp}"
    if kit_dir.exists():
        shutil.rmtree(kit_dir)
    kit_dir.mkdir(parents=True)
    for rel, src in sorted(files.items()):
        dst = kit_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    models_bytes = sum(src.stat().st_size for rel, src in files.items()
                       if rel.startswith("models/"))
    (kit_dir / "SHA256SUMS.txt").write_text(sums_text(files, build_kit), encoding="utf-8")
    (kit_dir / "BUILD-INFO.txt").write_text(
        build_info_text(variant, stamp, files, build_kit, models_bytes=models_bytes),
        encoding="utf-8")
    if models_bytes:
        # 「含权重」档多一份说明（怎么挂、别做什么）。它**不进 SHA256SUMS**：
        # 那份清单列的是"从仓库拷来的文件"，生成物一向不进去（BUILD-INFO 也不进）。
        (kit_dir / MODELS_NOTE).write_text(
            models_note_text(variant, files, models_bytes), encoding="utf-8")
    # 顶层前缀由 build_kit.make_zip 统一加（少前缀 = 解包散落，这个坑不报错）
    zip_path = out / (kit_dir.name + ".zip")
    build_kit.make_zip(kit_dir, zip_path, kit_dir.name)
    return kit_dir, zip_path


# --------------------------------------------------------------------- 自检
def check_compose(text: str, variant: dict) -> list[str]:
    """compose.yaml 的三条硬事实（挂错端口 = 管理面或能力面直接不通）。

    这个检查同时钉在 `tests/test_build_backend_kit.py` 里（那边是独立实现的护栏）；
    这里再跑一遍是为了**出包时就响亮地失败**，而不是等同事解包之后。
    """
    problems: list[str] = []
    try:
        import yaml
    except ImportError:                                  # pragma: no cover - 环境缺 PyYAML
        return ["本机没有 PyYAML，跳过了 compose.yaml 的结构检查"]
    try:
        doc = yaml.safe_load(text) or {}
    except Exception as exc:                             # noqa: BLE001 - 报给人看
        return [f"compose.yaml 解析不了：{exc}"]
    svc = (doc.get("services") or {}).get("backend")
    if not isinstance(svc, dict):
        return ["compose.yaml 里没有 services.backend"]
    image = str(svc.get("image") or "")
    if variant["key"] not in image:
        problems.append(f"镜像 tag 里没有变体名 {variant['key']}：{image}")
    ports = [str(p) for p in (svc.get("ports") or [])]
    if not any(p.startswith("8900:8900") for p in ports):
        problems.append("8900（能力面）没有发布到所有网卡")
    if not any(p.startswith("127.0.0.1:8901:8901") for p in ports):
        problems.append("8901（管理面）没有只发布到宿主回环")
    if any(p.startswith("8901:") and not p.startswith("127.0.0.1:") for p in ports):
        problems.append("8901 被发布到了所有网卡 —— 管理面等于对网段敞开")
    env = svc.get("environment") or {}
    if str(env.get("ECHO_ADMIN_LISTEN") or "") != "0.0.0.0:8901":
        problems.append("ECHO_ADMIN_LISTEN 不是 0.0.0.0:8901（容器里绑回环 = 宿主进不来）")
    devices = (((svc.get("deploy") or {}).get("resources") or {})
               .get("reservations") or {}).get("devices")
    if not devices:
        problems.append("没有 GPU 直通段（deploy.resources.reservations.devices）")
    if svc.get("read_only") is not True:
        problems.append("根文件系统不是 read_only")
    return problems


def verify_kit(kit_dir: Path, variant: dict, build_kit,
               models_from: Path | None = None) -> list[str]:
    """出完包后的自检。返回问题列表（空 = 通过）。

    `models_from` 给了就按「含权重」档来查：那份说明要在、变体声明的每棵子树都要有文件。
    """
    with_models = models_from is not None
    problems: list[str] = []
    required = DELIVERY_REQUIRED + ("BUILD-INFO.txt", "SHA256SUMS.txt")
    if with_models:
        required += (MODELS_NOTE,)
    for name in required:
        if not (kit_dir / name).is_file():
            problems.append(f"包根缺 {name}")
    for rel in ("server/Dockerfile", "server/requirements.txt", "server/main.py",
                "app/audio/stt.py"):
        if not (kit_dir / rel).is_file():
            problems.append(f"缺 {rel}（后端跑不起来）")
    # 排除项：包内**一个都不许有**（这条是安全线，别放松）；含权重档只放行 models/ 顶层
    for path in kit_dir.rglob("*"):
        rel_parts = path.relative_to(kit_dir).parts
        if path.is_file() and is_excluded(rel_parts, path.name, allow_models=with_models):
            problems.append(f"包里出现了该排除的文件：{path.relative_to(kit_dir).as_posix()}")
        if path.is_dir() and path.name in EXCLUDE_DIRS:
            if not (with_models and rel_parts[:1] == ("models",)):
                problems.append(f"包里出现了该排除的目录：{path.relative_to(kit_dir).as_posix()}")
    if with_models:
        for rel in (variant.get("models") or ()):
            sub = kit_dir / "models" / rel
            files_inside = [p for p in sub.rglob("*") if p.is_file()] if sub.is_dir() else []
            if not files_inside:
                problems.append(f"含权重档里 models/{rel}/ 是空的或不存在（变体声明要它）")
    problems += check_compose((kit_dir / "compose.yaml").read_text(encoding="utf-8"), variant)
    # 清单与内容必须对得上（否则 SHA256SUMS 是一句空话）
    files = planned_files(variant, models_from)
    sums = parse_sums((kit_dir / "SHA256SUMS.txt").read_text(encoding="utf-8"))
    if sorted(sums) != sorted(files):
        problems.append("SHA256SUMS.txt 与本次打包清单不一致")
    else:
        for rel, src in files.items():
            if build_kit.sha256_file(kit_dir / rel) != build_kit.sha256_file(src):
                problems.append(f"包里的 {rel} 与仓库源文件不一致")
    return problems


def check_zip(zip_path: Path, variant: dict, *, with_models: bool = False) -> list[str]:
    """zip 层的两条硬事实：条目前缀 + 不许出现被排除的路径。"""
    import zipfile
    problems: list[str] = []
    prefix = zip_path.stem + "/"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        bad = [n for n in names if not n.startswith(prefix)]
        if bad:
            problems.append(f"有 {len(bad)} 个条目没带顶层前缀 {prefix}：{bad[:3]}")
        for name in names:
            rel = name[len(prefix):].rstrip("/")
            if not rel:
                continue
            parts = rel.split("/")
            if is_excluded(tuple(parts), parts[-1], allow_models=with_models):
                problems.append(f"zip 里有被排除的条目：{name}")
        if prefix + "SHA256SUMS.txt" not in names:
            problems.append("zip 里没有 SHA256SUMS.txt")
        if prefix + KIT_README not in names:
            problems.append(f"zip 里没有 {KIT_README}")
    return problems


# --------------------------------------------------------------------- --check
def newest_zip(out: Path, variant: dict) -> Path | None:
    """这个变体最新那个 zip（按名字排，<前缀>-<变体>-YYYYMMDD-HHMM.zip）。"""
    if not out.is_dir():
        return None
    pat = re.compile(re.escape(f"{KIT_PREFIX}-{variant['key']}") + r"-\d{8}-\d{4}\.zip$")
    found = [p for p in out.glob("*.zip") if pat.match(p.name)]
    return sorted(found, key=lambda p: p.name)[-1] if found else None


def read_zip_text(zip_path: Path, member: str) -> str:
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return zf.read(member).decode("utf-8", errors="replace")
    except (KeyError, OSError):
        return ""


def compare_to_source(zip_path: Path, variant: dict, build_kit) -> tuple[list[str], str]:
    """包里的清单 vs 现在的源码。返回 (problems, note)。

    判据是 `SHA256SUMS.txt` 里的哈希 —— 它就是"包里到底装了什么"的权威清单，
    所以比时间戳猜靠谱。三路：改了 / 新增了 / 删掉了。

    **含权重包里的 `models/**` 不参与这条比较**：那是"payload"，不是源码 ——
    它本来就允许与当前机器上的权重不同（出包那台机器上放着就算数）。
    """
    files = planned_files(variant)
    all_sums = parse_sums(read_zip_text(zip_path, zip_path.stem + "/SHA256SUMS.txt"))
    if not all_sums:
        return [f"{zip_path.name} 里没有可用的 SHA256SUMS.txt"], ""
    model_entries = [rel for rel in all_sums if rel.startswith("models/")]
    sums = {rel: sha for rel, sha in all_sums.items() if not rel.startswith("models/")}
    changed = [rel for rel in sorted(files)
               if rel in sums and build_kit.sha256_file(files[rel]) != sums[rel]]
    added = [rel for rel in sorted(files) if rel not in sums]
    removed = [rel for rel in sorted(sums) if rel not in files]
    problems: list[str] = []
    if changed:
        problems.append(f"{len(changed)} 个文件打包后被改过：" + ", ".join(changed[:6])
                        + (" …" if len(changed) > 6 else ""))
    if added:
        problems.append(f"{len(added)} 个文件是打包后新增的：" + ", ".join(added[:6])
                        + (" …" if len(added) > 6 else ""))
    if removed:
        problems.append(f"{len(removed)} 个文件已从仓库删除：" + ", ".join(removed[:6])
                        + (" …" if len(removed) > 6 else ""))
    note = ""
    if model_entries:
        note = f"含权重（{len(model_entries)} 个文件，不计入源码一致性）"
    info = read_zip_text(zip_path, zip_path.stem + "/BUILD-INFO.txt")
    m = re.search(r"^git\s*:\s*([0-9a-f]+)", info, re.M)
    head = build_kit.git_short()
    if m and head:
        git_note = (f"git {head}（与 HEAD 相同）" if m.group(1) == head
                    else f"包出自 git {m.group(1)}，当前 HEAD {head}")
        note = f"{note}；{git_note}" if note else git_note
    return problems, note


def do_check(out: Path, variants: list[dict], build_kit) -> int:
    print("=== backend delivery kits: is dist/ still current? ===")
    worst = EXIT_OK
    for variant in variants:
        zip_path = newest_zip(out, variant)
        if zip_path is None:
            print(f"  {variant['key']:<6} (no zip in {out})  -- 还没出过包")
            worst = max(worst, EXIT_ABSENT)
            continue
        problems, note = compare_to_source(zip_path, variant, build_kit)
        tag = "STALE" if problems else "CURRENT"
        print(f"  {variant['key']:<6} {zip_path.name:<40} {tag}"
              + (f"   [{note}]" if note else ""))
        for problem in problems:
            print(f"        - {problem}")
        if problems:
            worst = max(worst, EXIT_STALE)
    if worst == EXIT_OK:
        print("\n[ok] dist/ 与当前源码一致，不用重出。")
    elif worst == EXIT_STALE:
        print(f"\n[!] 有包已过期 —— 重出：python scripts/{Path(__file__).name}")
    else:
        print(f"\n[i] dist/ 里还没有后端包；要出包：python scripts/{Path(__file__).name}")
    return worst


# --------------------------------------------------------------------- 入口
def parse_variants(spec: str) -> list[dict]:
    if not spec:
        return list(VARIANTS)
    wanted = [x.strip() for x in spec.split(",") if x.strip()]
    picked = [v for v in VARIANTS if v["key"] in wanted]
    unknown = [x for x in wanted if x not in {v["key"] for v in VARIANTS}]
    if unknown:
        raise BuildError(f"不认识的变体：{', '.join(unknown)}"
                         f"（可选：{', '.join(v['key'] for v in VARIANTS)}）")
    if not picked:
        raise BuildError("--only 没选中任何变体")
    return picked


def do_build(out: Path, variants: list[dict], stamp: str, verify: bool, build_kit,
             models_from: Path | None = None) -> int:
    out.mkdir(parents=True, exist_ok=True)
    tier = "含权重" if models_from is not None else "纯代码"
    print(f"=== build-backend-kit (variants={','.join(v['key'] for v in variants)}, "
          f"stamp={stamp}, 档位={tier}) ===")
    print(f"  git : {build_kit.git_short() or '?'}"
          f"{' (dirty：工作区有未提交改动)' if build_kit.git_dirty() else ''}")
    if models_from is not None:
        print(f"  models: {models_from}")
    results = []
    for variant in variants:
        files = planned_files(variant, models_from)
        kit_dir, zip_path = stage(variant, stamp, out, build_kit, models_from)
        size_mb = zip_path.stat().st_size / (1 << 20)
        weights = len([r for r in files if r.startswith("models/")])
        extra = f" + {weights} 个权重文件" if weights else ""
        print(f"  [kit  ] {zip_path.name}  ({size_mb:.2f} MB, {len(files)} 个源文件{extra})")
        if zip_path.stat().st_size > MODELS_WARN_BYTES:
            print(f"         [!] 超过 {MODELS_WARN_BYTES / (1 << 30):.0f} GB —— "
                  f"拷给同事时注意盘符与传输方式（USB/内网 rsync 都比聊天工具稳）")
        results.append((variant, kit_dir, zip_path))

    if verify:
        print("\n=== verify ===")
        failed = False
        for variant, kit_dir, zip_path in results:
            problems = verify_kit(kit_dir, variant, build_kit, models_from)
            problems += check_zip(zip_path, variant, with_models=models_from is not None)
            if problems:
                failed = True
                print(f"  [FAIL] {kit_dir.name}")
                for problem in problems:
                    print(f"        - {problem}")
            else:
                print(f"  [ok]   {kit_dir.name}")
        if failed:
            raise BuildError("自检没过 —— 上面这些包别发")

    print("\n  created:")
    for _variant, _kit_dir, zip_path in results:
        print(f"    {zip_path}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="出 ECHO 后端交付包（按显卡分流的 cu126 / cu118 两个 zip）")
    ap.add_argument("--check", action="store_true",
                    help="只报告 dist/ 里的包与当前源码是否一致（不写任何东西）")
    ap.add_argument("--only", default="",
                    help="逗号分隔：cu126,cu118（默认两个都出）")
    ap.add_argument("--out", default="", help="输出目录（默认 <repo>/dist）")
    ap.add_argument("--stamp", default="", help="覆盖时间戳（默认 now，形如 20260928-0130）")
    ap.add_argument("--no-verify", action="store_true", help="跳过出包后的自检")
    ap.add_argument("--with-models", action="store_true",
                    help="**含权重档**：把变体声明的那几棵模型子树打进包（默认出纯代码包）。"
                         "权重不齐会**直接失败**，不做「能拷多少拷多少」")
    ap.add_argument("--models-from", default="",
                    help="模型根（默认 <repo>/models）。只与 --with-models 一起用；"
                         "出包测试/换机时指到别处")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve() if args.out else DIST
    try:
        variants = parse_variants(args.only)
        build_kit = load_build_kit()
        if args.check:
            return do_check(out, variants, build_kit)
        models_from = None
        if args.with_models or args.models_from:
            models_from = Path(args.models_from).resolve() if args.models_from else (ROOT / "models")
            if not models_from.is_dir():
                raise BuildError(f"模型根不存在：{models_from}（含权重档要它）")
        stamp = args.stamp or time.strftime("%Y%m%d-%H%M")
        return do_build(out, variants, stamp, not args.no_verify, build_kit, models_from)
    except BuildError as exc:
        print(f"\n[fail] {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
