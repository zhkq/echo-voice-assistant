#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_kit.py - 出交付包：主包（build-package.ps1）+ 组装 kit + 核验 + 过期检查。

为什么要有它
------------
2026-09-22 同事要测安装，dist/ 里的包却比源码旧了 9 小时 —— 13:30-15:00 的修复
（含「标准版本地永久安装」）根本没进包。根因是**组 kit 一直是手工活**：`scripts/`
里没有对应脚本，靠人记步骤、还要从 dist 里翻上一代 kit 捡 `先读我.md`。手工活必然
漂移，而且那次踩的两个坑都是**静默**的：

  * 组装脚本存成无 BOM 的 UTF-8，PowerShell 5.1 按 ANSI 读 → 中文文件名被弄坏，
    `先读我.md` 根本没拷进去、BUILD-INFO 追加成乱码，却不报错；
  * zip 条目少了 `<kit>/` 顶层前缀 → 解包出来是一堆散文件，而不是「一个文件夹」。

用 Python 写这份脚本本身就是对第一个坑的规避（.py 无 BOM/编码之忧），第二个坑
由 `make_zip()` 统一加前缀来消除。

两个平台都走 `-Profile main`（2026-09-22 统一，见 `--check` 的说明）：裹 `ECHO/`、
带 `components/`，`manifest.json` 的 `componentManifests` 才与包内实际文件对得上。
（`public` 档曾经声明 `components/offline-pack.json` 却没装进去 —— 一句写在交付清单
里的假话，而 manifest 正是安装流程用来判断「这是已解开的包」的文件。）

模式
----
    python scripts/build_kit.py                  # 出两个主包 + 组两个 kit + 核验
    python scripts/build_kit.py --check          # 只报告：dist 的 kit 与源码一致吗（不写盘）
    python scripts/build_kit.py --kits-only      # 复用 dist 里已有的主包，只重组 kit
    python scripts/build_kit.py --platforms win
    python scripts/build_kit.py --stamp 20260922-2100
    python scripts/build_kit.py --no-verify

退出码
------
    0  正常；或 --check 判定「一致」
    2  --check 判定「过期」（需重出）
    3  --check 判定「还没有包」
    1  出错
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path

# 中文输出在 Windows 上被「管道捕获」时会按本地码页（GBK）编码，落地即乱码。
# 强制 UTF-8：无论直接跑，还是被 gh-push.ps1 / check-windows.ps1 捕获，都读得懂。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DELIVERY = ROOT / "delivery"
SKILL_DIR = ROOT / ".dsh" / "skills" / "echo-install"
BUILD_PACKAGE = ROOT / "scripts" / "build-package.ps1"

#: kit 里给人的那份说明叫这个名字（中文名是刻意的：同事一眼知道先读它）
KIT_README = "先读我.md"

#: 交付矩阵。**两个平台都是 main profile** —— 平台差异只体现在 `-Platform`（改包名、
#: manifest 的 platform，并顺带排除 mac 的 `sidebar/bin/`）与用哪份 `先读我.md`。
PLATFORMS = (
    {
        "key": "win",
        "label": "win-x64",
        "kit_prefix": "ECHO-kit",
        "package_platform": None,               # 不传 -Platform，由构建机推断
        "readme": "kit-readme-win.md",
    },
    {
        "key": "macos",
        "label": "macos-universal",
        "kit_prefix": "ECHO-kit-macos",
        "package_platform": "macos-universal",
        "readme": "kit-readme-mac.md",
    },
)

EXIT_OK = 0
EXIT_STALE = 2
EXIT_ABSENT = 3
EXIT_ERROR = 1


class BuildError(RuntimeError):
    """脚本自己能解释清楚的失败，直接打给人看。"""


# --------------------------------------------------------------------------- 小工具

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def powershell_exe() -> str:
    """优先 pwsh（7+，按 UTF-8 读脚本），退回 Windows PowerShell。

    这里只调用仓库自己的 .ps1；两者都能跑。用 pwsh 时中文输出也不会乱码。
    """
    for name in ("pwsh", "powershell.exe", "powershell"):
        if shutil.which(name):
            return name
    raise BuildError("找不到 PowerShell（pwsh / powershell 都不在 PATH 里）")


def run(argv, cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [str(a) for a in argv],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",      # PowerShell 5.1 的中文输出可能不是 UTF-8；我们只解析 ASCII
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def git_short() -> str:
    code, out = run(["git", "rev-parse", "--short", "HEAD"])
    return out.strip() if code == 0 else ""


def git_dirty() -> bool:
    code, out = run(["git", "status", "--porcelain"])
    return code == 0 and bool(out.strip())


# --------------------------------------------------------------- 主包 / kit 的定位

def find_kits(dist: Path, prefix: str) -> list[Path]:
    """按名字找 kit 目录，并把它和 `ECHO-kit-macos-*` 区分开。

    为什么不用简单的 startswith：`ECHO-kit` 是 `ECHO-kit-macos` 的前缀，用 startswith
    会让 win 的选择器把 mac 的 kit 也算进去（真踩过）。
    """
    if not dist.is_dir():
        return []
    out = []
    for item in dist.iterdir():
        if not item.is_dir():
            continue
        if prefix == "ECHO-kit":
            if re.fullmatch(r"ECHO-kit-\d{8}-\d{4}", item.name):
                out.append(item)
        elif re.fullmatch(re.escape(prefix) + r"-\d{8}-\d{4}", item.name):
            out.append(item)
    return sorted(out, key=lambda p: p.name)


def newest_kit(dist: Path, plat: dict) -> Path | None:
    kits = find_kits(dist, plat["kit_prefix"])
    return kits[-1] if kits else None


def newest_main_package(dist: Path, plat: dict) -> Path | None:
    pat = re.compile(r"^ECHO-main-" + re.escape(plat["label"]) + r"-\d.*\.zip$")
    found = [p for p in dist.glob("*.zip") if pat.match(p.name)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


# ------------------------------------------------------------------- 读包内的元数据

def read_sums(kit: Path) -> dict[str, str]:
    """读 kit 根的 SHA256SUMS.txt → {仓库相对路径: sha256}。

    这就是「包里到底装了什么」的权威清单（build-package.ps1 逐文件写的），
    所以过期检查以它为准，而不是靠时间戳猜。
    """
    path = kit / "SHA256SUMS.txt"
    if not path.is_file():
        return {}
    sums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, rel = line.partition("  ")
        if rel.strip():
            sums[rel.strip()] = digest.strip().lower()
    return sums


def read_build_info(kit: Path) -> dict[str, str]:
    path = kit / "BUILD-INFO.txt"
    info: dict[str, str] = {}
    if not path.is_file():
        return info
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith(" "):
            info[key.strip()] = value.strip()
    return info


def packed_summary(plat: dict) -> tuple[int, float, dict[str, int]]:
    """问 build-package.ps1「现在这棵树会打出什么」——文件数、MB、以及按顶层目录的分布。

    为什么不在这里自己复刻白名单：白名单和禁止路径规则长在 build-package.ps1 里，
    复刻一份就是两处真相，迟早漂移。`-DryRun` 只做检查不写盘，正好是我们要的。
    """
    argv = [powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", BUILD_PACKAGE, "-Profile", "main", "-DryRun"]
    if plat["package_platform"]:
        argv += ["-Platform", plat["package_platform"]]
    code, out = run(argv)
    if code != 0:
        raise BuildError(f"build-package.ps1 -DryRun 失败（exit={code}）：\n{out[-2000:]}")
    m = re.search(r"selected (\d+) files, ([\d.]+) MB unpacked", out)
    if not m:
        raise BuildError("看不懂 build-package.ps1 -DryRun 的输出（'selected N files' 没找到）")
    per_dir = {name: int(n) for name, n in
               re.findall(r"^\s+(\S+)\s+(\d+) files\s+[\d.]+ KB\s*$", out, re.M)}
    return int(m.group(1)), float(m.group(2)), per_dir


# ----------------------------------------------------------------------- 核验

def compare_skill(kit: Path) -> list[str]:
    """kit 里的 `echo-install/` 必须与仓库 `.dsh/skills/echo-install/` 一字不差。

    这是最要紧的一致：同事就是照这份技能装的。历史上有过「包里的技能比仓库旧」。
    """
    packed = kit / "echo-install"
    if not packed.is_dir():
        return ["kit 里没有 echo-install/ 目录"]
    problems = []
    want = {p.relative_to(SKILL_DIR).as_posix(): p
            for p in SKILL_DIR.rglob("*") if p.is_file()}
    have = {p.relative_to(packed).as_posix(): p
            for p in packed.rglob("*") if p.is_file()}
    for rel, src in sorted(want.items()):
        if rel not in have:
            problems.append(f"echo-install/ 缺文件: {rel}")
        elif sha256_file(have[rel]) != sha256_file(src):
            problems.append(f"echo-install/ 与仓库不一致: {rel}")
    for rel in sorted(set(have) - set(want)):
        problems.append(f"echo-install/ 多出文件: {rel}")
    return problems


def compare_manifest(kit: Path) -> list[str]:
    """manifest.json 声明的东西必须真的在包里。

    `public` 档曾经声明 `components/offline-pack.json` 而包里没有它 —— 交付清单里的
    假话，而 manifest 正是安装流程用来判断「这是已解开的包」的文件。
    """
    path = kit / "ECHO" / "manifest.json"
    if not path.is_file():
        return ["包里没有 ECHO/manifest.json"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest.json 读不出来: {exc}"]
    problems = []
    for rel in manifest.get("componentManifests") or []:
        if not (kit / "ECHO" / rel).is_file():
            problems.append(f"manifest.json 声明了 components 清单但包内没有: {rel}")
    return problems


def compare_to_source(kit: Path, plat: dict) -> tuple[list[str], list[str]]:
    """把 kit 与当前源码比。返回 (problems, notes)。

    problems 非空 = 过期。判据三路：内容哈希（改了什么）、文件数（多了什么）、
    按目录的分布（是哪一块多了），外加 git 短哈希作参考。
    """
    problems: list[str] = []
    notes: list[str] = []

    sums = read_sums(kit)
    if not sums:
        return [f"{kit.name} 里没有可用的 SHA256SUMS.txt"], notes

    changed, missing = [], []
    for rel, digest in sorted(sums.items()):
        target = ROOT / rel
        if not target.is_file():
            missing.append(rel)
        elif sha256_file(target) != digest:
            changed.append(rel)
    if changed:
        problems.append(f"{len(changed)} 个文件打包后被改过: " + _preview(changed))
    if missing:
        problems.append(f"{len(missing)} 个文件已从仓库删除: " + _preview(missing))

    # 新增文件：与 build-package 现在会打出来的清单比。内容哈希抓不到「新增」，
    # 所以要靠文件数，并按顶层目录定位是多出来的是哪一块。
    try:
        count, _mb, per_dir = packed_summary(plat)
    except BuildError as exc:
        problems.append(str(exc))
        per_dir, count = {}, len(sums)
    if count != len(sums):
        kit_per_dir = Counter(rel.split("/")[0] for rel in sums)
        grew = [f"{d} +{n - kit_per_dir.get(d, 0)}"
                for d, n in sorted(per_dir.items()) if n > kit_per_dir.get(d, 0)]
        shrank = [f"{d} -{kit_per_dir.get(d, 0) - n}"
                  for d, n in sorted(per_dir.items()) if n < kit_per_dir.get(d, 0)]
        detail = ", ".join(grew + shrank) or "分布看不出差异"
        problems.append(f"打包后文件数变了：现在会打 {count} 个，包里是 {len(sums)} 个（{detail}）")

    info = read_build_info(kit)
    packed_git = (info.get("git") or "").split("@")[-1].strip().split()[0] if info.get("git") else ""
    head = git_short()
    if packed_git and head:
        if packed_git == head:
            notes.append(f"git {head}（与 HEAD 相同）")
        else:
            notes.append(f"包出自 git {packed_git}，当前 HEAD {head}")
    if git_dirty():
        notes.append("工作区有未提交改动")

    problems += compare_skill(kit)
    problems += compare_manifest(kit)
    return problems, notes


def _preview(items: list[str], limit: int = 6) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" …（共 {len(items)} 个）" if len(items) > limit else "")


def verify_kit(kit: Path, plat: dict) -> list[str]:
    """出完包后的自检：结构 + 内容。返回问题列表（空 = 通过）。"""
    problems = []
    if not (kit / "ECHO").is_dir():
        problems.append("kit 里没有 ECHO/")
    if not (kit / KIT_README).is_file():
        problems.append(f"kit 里没有 {KIT_README}")
    if not (kit / "BUILD-INFO.txt").is_file():
        problems.append("kit 根目录没有 BUILD-INFO.txt")
    if not (kit / "SHA256SUMS.txt").is_file():
        problems.append("kit 根目录没有 SHA256SUMS.txt")
    problems += compare_skill(kit)
    problems += compare_manifest(kit)
    verify_dirs = [p for p in (kit / "ECHO").iterdir() if p.is_dir()] if (kit / "ECHO").is_dir() else []
    if not verify_dirs:
        problems.append("ECHO/ 是空的")
    return problems


# ----------------------------------------------------------------------- 出包

def build_main_packages(plats: list[dict], dist: Path) -> dict[str, Path]:
    built: dict[str, Path] = {}
    for plat in plats:
        argv = [powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", BUILD_PACKAGE, "-Profile", "main"]
        if plat["package_platform"]:
            argv += ["-Platform", plat["package_platform"]]
        print(f"  [main ] building {plat['label']} ...", flush=True)
        code, out = run(argv)
        if code != 0:
            raise BuildError(f"build-package.ps1 失败（{plat['label']}, exit={code}）：\n{out[-3000:]}")
        pkg = newest_main_package(dist, plat)
        if pkg is None:
            raise BuildError(f"build-package.ps1 说成功了，但在 {dist} 里找不到 "
                             f"ECHO-main-{plat['label']}-*.zip")
        size_mb = pkg.stat().st_size / (1 << 20)
        print(f"           -> {pkg.name}  ({size_mb:.2f} MB)")
        built[plat["key"]] = pkg
    return built


def make_zip(src: Path, zip_path: Path, prefix: str) -> None:
    """把 src 的内容打进 zip，**每条目都带 `<prefix>/` 顶层前缀**。

    为什么必须带前缀：`先读我.md` 让同事「把这个文件夹整个交给助手」，解包出来就得是
    一个文件夹。少了前缀会散成一堆文件（2026-09-22 真踩到，且不报错）。
    """
    dirs = sorted((p for p in src.rglob("*") if p.is_dir()), key=lambda p: p.as_posix())
    files = sorted((p for p in src.rglob("*") if p.is_file()), key=lambda p: p.as_posix())
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in dirs:
            rel = d.relative_to(src).as_posix()
            info = zipfile.ZipInfo(prefix + "/" + rel + "/", date_time=_dos_time(d))
            info.external_attr = 0o40755 << 16
            zf.writestr(info, b"")
        for f in files:
            rel = f.relative_to(src).as_posix()
            zf.write(f, prefix + "/" + rel)


def _dos_time(path: Path) -> tuple[int, int, int, int, int, int]:
    t = time.localtime(path.stat().st_mtime)
    year = max(t.tm_year, 1980)
    return (year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def assemble_kit(plat: dict, main_zip: Path, stamp: str, dist: Path) -> tuple[Path, Path]:
    kit_dir = dist / f"{plat['kit_prefix']}-{stamp}"
    if kit_dir.exists():
        shutil.rmtree(kit_dir)
    kit_dir.mkdir(parents=True)

    with zipfile.ZipFile(main_zip) as zf:
        zf.extractall(kit_dir)

    skill_dst = kit_dir / "echo-install"
    if skill_dst.exists():
        shutil.rmtree(skill_dst)
    shutil.copytree(SKILL_DIR, skill_dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    readme = DELIVERY / plat["readme"]
    if not readme.is_file():
        raise BuildError(f"缺少 {KIT_README} 模板：{readme}")
    shutil.copyfile(readme, kit_dir / KIT_README)

    short = git_short()
    block = [
        "",
        "kit contents (this archive)",
        f"  ECHO/           unpacked main package (profile=main) @ {short}",
        "  echo-install/   echo-install skill (SKILL.md + scripts/*.ps1|.sh)",
        f"  {KIT_README}       三句话说明（给同事看的）",
    ]
    build_info = kit_dir / "BUILD-INFO.txt"
    if not build_info.is_file():
        raise BuildError(f"主包里没有 BUILD-INFO.txt：{main_zip.name}")
    body = build_info.read_text(encoding="utf-8", errors="replace").rstrip("\n")
    build_info.write_text(body + "\n" + "\n".join(block) + "\n", encoding="utf-8")

    zip_path = dist / f"{plat['kit_prefix']}-{stamp}.zip"
    make_zip(kit_dir, zip_path, kit_dir.name)
    return kit_dir, zip_path


# ----------------------------------------------------------------------- 两个模式

def do_check(dist: Path, plats: list[dict]) -> int:
    print("=== delivery packages: is dist/ still current? ===")
    worst = EXIT_OK
    for plat in plats:
        kit = newest_kit(dist, plat)
        if kit is None:
            print(f"  {plat['key']:<5} (no kit in {dist})  -- 还没出过包")
            worst = max(worst, EXIT_ABSENT)
            continue
        problems, notes = compare_to_source(kit, plat)
        tag = "STALE" if problems else "CURRENT"
        print(f"  {plat['key']:<5} {kit.name:<32} {tag}" +
              (f"   [{'; '.join(notes)}]" if notes else ""))
        for problem in problems:
            print(f"        - {problem}")
        if problems:
            worst = max(worst, EXIT_STALE)
    if worst == EXIT_OK:
        print("\n[ok] dist/ 与当前源码一致，不用重出。")
    elif worst == EXIT_STALE:
        print(f"\n[!] 有 kit 已过期 —— 重出：python scripts/{Path(__file__).name}")
    else:
        print(f"\n[i] dist/ 里还没有 kit；要出包：python scripts/{Path(__file__).name}")
    return worst


def do_build(dist: Path, plats: list[dict], stamp: str, kits_only: bool,
             verify: bool) -> int:
    dist.mkdir(parents=True, exist_ok=True)
    if kits_only:
        mains: dict[str, Path] = {}
        for plat in plats:
            pkg = newest_main_package(dist, plat)
            if pkg is None:
                raise BuildError(f"--kits-only 但 dist 里没有 ECHO-main-{plat['label']}-*.zip")
            mains[plat["key"]] = pkg
            print(f"  [reuse] {plat['label']}: {pkg.name}")
    else:
        mains = build_main_packages(plats, dist)

    print(f"\n  stamp={stamp}  git={git_short()}{' (dirty)' if git_dirty() else ''}")
    results = []
    for plat in plats:
        kit_dir, zip_path = assemble_kit(plat, mains[plat["key"]], stamp, dist)
        size_mb = zip_path.stat().st_size / (1 << 20)
        print(f"  [kit  ] {zip_path.name}  ({size_mb:.2f} MB)")
        results.append((plat, kit_dir, zip_path))

    if verify:
        print("\n=== verify ===")
        failed = False
        for plat, kit_dir, _zip in results:
            problems = verify_kit(kit_dir, plat)
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
    for _plat, _kit_dir, zip_path in results:
        print(f"    {zip_path}")
    return EXIT_OK


def parse_platforms(spec: str) -> list[dict]:
    if not spec:
        return list(PLATFORMS)
    wanted = [x.strip() for x in spec.split(",") if x.strip()]
    picked = [p for p in PLATFORMS if p["key"] in wanted]
    unknown = [x for x in wanted if x not in {p["key"] for p in PLATFORMS}]
    if unknown:
        raise BuildError(f"不认识的平台：{', '.join(unknown)}"
                         f"（可选：{', '.join(p['key'] for p in PLATFORMS)}）")
    return picked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="出 ECHO 交付包（主包 + kit）并检查 dist/ 是否过期")
    ap.add_argument("--check", action="store_true",
                    help="只报告 dist/ 里的 kit 与当前源码是否一致（不写任何东西）")
    ap.add_argument("--kits-only", action="store_true",
                    help="复用 dist/ 里已有的主包，只重新组装 kit")
    ap.add_argument("--platforms", default="",
                    help="逗号分隔：win,macos（默认两个都出）")
    ap.add_argument("--stamp", default="",
                    help="覆盖时间戳（默认 now，形如 20260922-2100）")
    ap.add_argument("--no-verify", action="store_true", help="跳过出包后的自检")
    ap.add_argument("--dist", default="", help="输出目录（默认 <repo>/dist）")
    args = ap.parse_args(argv)

    dist = Path(args.dist).resolve() if args.dist else DIST
    try:
        plats = parse_platforms(args.platforms)
    except BuildError as exc:
        print(f"[fail] {exc}")
        return EXIT_ERROR
    if args.check:
        return do_check(dist, plats)
    stamp = args.stamp or time.strftime("%Y%m%d-%H%M")
    print(f"=== build-kit (platforms={','.join(p['key'] for p in plats)}, "
          f"kitsOnly={args.kits_only}) ===")
    try:
        return do_build(dist, plats, stamp, args.kits_only, not args.no_verify)
    except BuildError as exc:
        print(f"\n[fail] {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
