#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_installer.py - assemble dist/ECHO-Setup-<version>.exe (phase 1, unsigned).

This script is the ONE definition of how the installer is put together.  It reuses
the existing packaging whitelists instead of listing files again:

  * main code      -> build_kit.build_main_packages() / build-package.ps1 -Profile main
  * runtime        -> build-package.ps1 -Profile component -Component runtime-core
  * sidebar        -> dotnet publish sidebar/echo-sidebar.csproj
  * launcher       -> dotnet publish launcher/ECHO.Launcher (NativeAOT)
  * installer      -> ISCC installer/ECHO.iss

It must run on Windows (Inno's ISCC and NativeAOT need a Windows host).  ``--dry-run``
only prints the commands and is safe on any platform; unit tests use the small pure
helpers below.

No signing is implemented on purpose: phase 1 ships an unsigned internal build and
must never pretend a self-signed certificate is trustworthy.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DIST = ROOT / "dist"
INSTALLER = ROOT / "installer"
ISS = INSTALLER / "ECHO.iss"
SIDEBAR_PROJECT = ROOT / "sidebar" / "echo-sidebar.csproj"
LAUNCHER_PROJECT = ROOT / "launcher" / "ECHO.Launcher" / "ECHO.Launcher.csproj"
SIDEBAR_OUT = Path("echo-core") / "sidebar" / "bin" / "Release" / "net7.0-windows" / "win-x64"
ISCC_NAMES = ("ISCC.exe", "iscc.exe", "ISCC", "iscc")


class BuildError(RuntimeError):
    pass


def say(message: str) -> None:
    print("  " + message, flush=True)


def run(argv, *, cwd: Path | None = None, dry_run: bool = False, timeout: int | None = None):
    printable = " ".join(str(x) for x in argv)
    say("$ " + printable)
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, "", "")
    try:
        proc = subprocess.run(
            [str(x) for x in argv], cwd=str(cwd or ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
    except FileNotFoundError as exc:
        raise BuildError(f"命令不存在：{argv[0]}（{exc}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"命令超时（{timeout}s）：{printable}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-25:] +
                         (proc.stderr or "").splitlines()[-25:])
        raise BuildError(f"命令失败（exit={proc.returncode}）：{printable}\n{tail}")
    return proc


def read_version() -> str:
    """The only source of truth is app/__init__.py:__version__."""
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise BuildError("app/__init__.py 里找不到 __version__")
    return match.group(1).strip()


def numeric_version(version: str) -> str:
    """Inno's VersionInfoVersion must be 1-4 numeric parts."""
    parts = [int(x) for x in re.findall(r"\d+", version)[:4]]
    while len(parts) < 4:
        parts.append(0)
    return ".".join(str(x) for x in parts)


def load_build_kit():
    path = SCRIPTS / "build_kit.py"
    spec = importlib.util.spec_from_file_location("echo_build_kit", path)
    if spec is None or spec.loader is None:
        raise BuildError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def newest(pattern: str, folder: Path = DIST) -> Path | None:
    found = [p for p in folder.glob(pattern) if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def find_iscc(explicit: str = "") -> str:
    if explicit:
        if Path(explicit).is_file():
            return str(Path(explicit).resolve())
        raise BuildError(f"--iscc 指向的文件不存在：{explicit}")

    env = os.environ.get("ISCC", "").strip()
    if env and Path(env).is_file():
        return str(Path(env).resolve())

    roots = [
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    candidates = []
    for root in roots:
        if not root:
            continue
        candidates += [
            Path(root) / "Inno Setup 6" / "ISCC.exe",
            Path(root) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        ]
    for name in ISCC_NAMES:
        which = shutil.which(name)
        if which:
            candidates.append(Path(which))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise BuildError(
        "找不到 ISCC.exe。安装 Inno Setup 6 或传 --iscc <path>；CI 会下载固定版本。")


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def build_code_package(dist: Path) -> Path:
    """Build the code-only installer payload.

    We use a dedicated ``-Profile installer`` instead of ``main``: integration branches
    carry multi-MB evidence screenshots under docs/ that are not product content.  The
    profile keeps the same whitelist/forbidden-content checks and the 20 MB budget, but
    keeps only markdown/text from docs/.
    """
    say("building installer code package (profile=installer) ...")
    run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPTS / "build-package.ps1"),
         "-Profile", "installer", "-OutDir", str(dist)])
    package = newest("ECHO-installer-win-*.zip", dist)
    if package is None:
        raise BuildError(f"build-package.ps1 说成功了，但在 {dist} 里找不到 ECHO-installer-win-*.zip")
    return package


def stage_main(stage: Path, main_zip: Path | None, dry_run: bool) -> Path | None:
    if main_zip is None:
        if dry_run:
            say("would build installer code package (build-package.ps1 -Profile installer)")
            return None
        main_zip = build_code_package(DIST)
    if main_zip is None or not Path(main_zip).is_file():
        raise BuildError("没有安装器代码包（build-package.ps1 -Profile installer）")
    if dry_run:
        say(f"would extract main package: {main_zip}")
        return Path(main_zip)
    kit = load_build_kit()
    code = kit.extract_main_package(Path(main_zip), stage)
    if not (code / "app" / "main.py").is_file():
        raise BuildError(f"主程序包解出来没有 app/main.py：{main_zip}")
    if code.name != "echo-core":
        raise BuildError(f"主程序目录不是 echo-core：{code}")
    return Path(main_zip)


def stage_runtime(stage: Path, runtime_zip: Path | None, dry_run: bool) -> Path | None:
    if runtime_zip is None:
        if dry_run:
            say("would build runtime-core component (build-package.ps1 -Profile component)")
            return None
        say("building runtime-core component (needs uv + network) ...")
        powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(SCRIPTS / "build-package.ps1"),
             "-Profile", "component", "-Component", "runtime-core",
             "-OutDir", str(DIST)])
        runtime_zip = newest("ECHO-component-runtime-core-win-*.zip")
    if runtime_zip is None or not Path(runtime_zip).is_file():
        raise BuildError("没有 runtime-core 组件包")
    if dry_run:
        say(f"would extract runtime core: {runtime_zip}")
        return Path(runtime_zip)

    target = stage / "runtime-core"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(runtime_zip) as zf:
        members = [m for m in zf.infolist() if m.filename.replace("\\", "/").startswith("runtime-core/")]
        if not members:
            raise BuildError(f"组件包里没有 runtime-core/：{runtime_zip}")
        for member in members:
            relative = member.filename.replace("\\", "/")[len("runtime-core/"):]
            if not relative:
                continue
            destination = target / relative
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
    if not (target / "python.exe").is_file() and not (target / "Scripts" / "python.exe").is_file():
        raise BuildError(f"runtime-core 里没有 python.exe：{target}")
    return Path(runtime_zip)


def stage_sidebar(stage: Path, dry_run: bool) -> None:
    out = stage / SIDEBAR_OUT
    say("publishing sidebar (framework-dependent .NET 7) ...")
    run(["dotnet", "publish", str(SIDEBAR_PROJECT), "-c", "Release", "-r", "win-x64",
         "--self-contained", "false", "-o", str(out)], dry_run=dry_run)
    if dry_run:
        return
    if not (out / "echo-sidebar.exe").is_file():
        raise BuildError(f"sidebar 产物不存在：{out / 'echo-sidebar.exe'}")


def stage_launcher(stage: Path, version: str, dry_run: bool) -> None:
    say("publishing launcher (NativeAOT win-x64) ...")
    run(["dotnet", "publish", str(LAUNCHER_PROJECT), "-c", "Release", "-r", "win-x64",
         "-p:PublishAot=true", "-p:Version=" + version,
         "-p:DebugType=None", "-p:DebugSymbols=false",
         "-o", str(stage)], dry_run=dry_run)
    if dry_run:
        return
    if not (stage / "ECHO.exe").is_file():
        raise BuildError(f"启动器产物不存在：{stage / 'ECHO.exe'}")


def compile_installer(stage: Path, version: str, out_dir: Path, iscc: str,
                      dry_run: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [iscc,
            "/DAppVersion=" + version,
            "/DVersionInfoVersion=" + numeric_version(version),
            "/DStageDir=" + str(stage.resolve()),
            "/DOutputDir=" + str(out_dir.resolve()),
            str(ISS)]
    run(argv, dry_run=dry_run)
    target = out_dir / f"ECHO-Setup-{version}.exe"
    if dry_run:
        say(f"would produce: {target}")
        return target
    if not target.is_file():
        raise BuildError(f"ISCC 没有产出 {target}")
    if target.stat().st_size < 20 * 1024 * 1024:
        raise BuildError(f"安装包只有 {target.stat().st_size} 字节，明显不完整：{target}")
    return target


def write_metadata(setup: Path, version: str, stage: Path) -> None:
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    (setup.with_suffix(setup.suffix + ".sha256")).write_text(
        f"{digest}  {setup.name}\n", encoding="utf-8")
    info = {
        "product": "ECHO",
        "version": version,
        "installer": setup.name,
        "size": setup.stat().st_size,
        "sha256": digest,
        "builtAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signed": False,
        "stage": str(stage),
        "note": "Phase 1 internal build. Unsigned on purpose; SmartScreen will warn.",
    }
    (setup.parent / "installer-build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build ECHO-Setup-<version>.exe")
    parser.add_argument("--version", default="", help="default: app/__init__.py")
    parser.add_argument("--out", default=str(DIST))
    parser.add_argument("--stage", default=str(DIST / "installer-stage"))
    parser.add_argument("--main-zip", default="", help="reuse an existing main package")
    parser.add_argument("--runtime-zip", default="", help="reuse an existing runtime-core package")
    parser.add_argument("--iscc", default="", help="path to ISCC.exe")
    parser.add_argument("--skip-main", action="store_true")
    parser.add_argument("--skip-runtime", action="store_true")
    parser.add_argument("--skip-sidebar", action="store_true")
    parser.add_argument("--skip-launcher", action="store_true")
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    version = (args.version or read_version()).strip()
    out_dir = Path(args.out).resolve()
    stage = Path(args.stage).resolve()
    say(f"ECHO installer {version}")
    say(f"stage: {stage}")
    say(f"out:   {out_dir}")

    if stage.exists() and not args.dry_run:
        shutil.rmtree(stage)
    if not args.dry_run:
        stage.mkdir(parents=True, exist_ok=True)

    try:
        if args.skip_main:
            if not args.dry_run and not (stage / "echo-core" / "app" / "main.py").is_file():
                raise BuildError("--skip-main 但 stage 里没有 echo-core/app/main.py")
        else:
            stage_main(stage, Path(args.main_zip) if args.main_zip else None, args.dry_run)

        if args.skip_runtime:
            if not args.dry_run and not (stage / "runtime-core" / "python.exe").is_file() and not (
                    stage / "runtime-core" / "Scripts" / "python.exe").is_file():
                raise BuildError("--skip-runtime 但 stage 里没有 runtime-core/python.exe")
        else:
            stage_runtime(stage, Path(args.runtime_zip) if args.runtime_zip else None, args.dry_run)

        if args.skip_sidebar:
            if not args.dry_run and not (stage / SIDEBAR_OUT / "echo-sidebar.exe").is_file():
                raise BuildError("--skip-sidebar 但 stage 里没有 sidebar 产物")
        else:
            stage_sidebar(stage, args.dry_run)

        if args.skip_launcher:
            if not args.dry_run and not (stage / "ECHO.exe").is_file():
                raise BuildError("--skip-launcher 但 stage 里没有 ECHO.exe")
        else:
            stage_launcher(stage, version, args.dry_run)

        try:
            iscc = find_iscc(args.iscc)
        except BuildError:
            if not args.dry_run:
                raise
            iscc = "ISCC.exe"
        setup = compile_installer(stage, version, out_dir, iscc, args.dry_run)
        if not args.dry_run:
            write_metadata(setup, version, stage)
            say(f"[ok] {setup} ({setup.stat().st_size / (1024 * 1024):.1f} MB)")
            say(f"sha256: {hashlib.sha256(setup.read_bytes()).hexdigest()}")
        if not args.keep_stage and not args.dry_run:
            shutil.rmtree(stage, ignore_errors=True)
        return 0
    except BuildError as exc:
        print(f"\n[fail] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
