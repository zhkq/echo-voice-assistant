# -*- coding: utf-8 -*-
"""Run the ECHO test suite in parallel shards -- same tests, same verdict, fewer minutes.

WHY THIS EXISTS (2026-10-01): the full gate's unit-test step is single-process and took
~24 minutes of wall time on a 24-core box, because a handful of modules dominate:
`test_settings_wiring` 374s, `test_admin_console` 208s, `test_backend_admin` 81s.
Almost all of that is *waiting* (test HTTP clients, live subprocess spawns, scrypt),
not CPU -- so the fix that needs no test edits is to run modules concurrently.

WHAT IT DOES
  * discovers `tests/test_*.py`;
  * splits them into N shards, greedily balanced by an estimated cost
    (measured weights for the known-heavy modules, file size as the proxy otherwise);
  * runs each shard in its own subprocess via the ordinary
    `python -m unittest -q <module> <module> ...` -- so each imported module sees
    exactly the same entry point the sequential gate uses;
  * prints a per-shard one-liner, full output for any shard that failed, and an
    aggregated summary.

SAFETY (why this is allowed to be parallel at all)
  * every server in the suite binds an **ephemeral port** (`("127.0.0.1", 0)`);
    the `8900/8901` numbers that appear in tests are values rendered into temp
    `server.yaml` files, never bound;
  * each test module isolates its own state in `tempfile.mkdtemp()` -- that is the
    repo's existing convention (see `tests/__init__.py`);
  * workers are capped by default to stay well inside free RAM, since a test process
    that pulls in torch peaks near 0.8 GB.

KNOWN LIMITS -- the two hazards a 6-shard run actually hit (2026-10-01, log kept at
`dist/_parallel_run1.log`). Both fail *only* under concurrency and pass in isolation,
so they are test-isolation gaps, not a problem with sharding itself:
  1. `test_config_compat.test_paths_follow_the_user_value_after_reseeding` -- the path
     layer read the repo's real `data/meetings` while another shard was rewriting
     settings. Sequential: OK; with 6 shards: 1 failure.
  2. `test_backend_proc` (3 x `PortOwnerTests`, 1 x `SpawnTests`) -- `port_owner()`
     reported `pid=0` / the wrong owner under load, so "the port is taken by a
     stranger" came out inverted.
Until those two modules are hardened for concurrency, treat a `-Parallel` failure as
"re-run that module sequentially" -- do not chase it as a product bug. The gate script
keeps parallel **opt-in** for this reason.

USAGE
    python scripts/check-parallel.py                # default workers, quiet
    python scripts/check-parallel.py -j 4           # explicit worker count
    python scripts/check-parallel.py -j 1           # one shard (still a separate process)
    python scripts/check-parallel.py -v             # echo every shard's output
    python scripts/check-parallel.py --modules tests.test_settings_wiring

Exit code 0 = every shard passed; 1 = at least one shard failed (or nothing ran).

ASCII-ONLY ON PURPOSE (same rule as the other scripts here): this file may be read
by Windows PowerShell 5.1, which parses BOM-less files as ANSI/GBK.
"""

import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

#: Measured wall time (seconds) of the modules heavy enough that "balance by file
#: size" would be wrong. Everything else falls back to file size / 900 as a proxy.
#: These numbers are a *hint* for balancing only -- a stale entry costs a few seconds
#: of imbalance, never a wrong verdict.
KNOWN_COST = {
    "test_settings_wiring": 90.0,
    "test_admin_console": 205.0,
    "test_backend_admin": 80.0,
    "test_meeting_speakers_standard": 67.0,
    "test_meeting_capability": 62.0,
    "test_server_contract": 55.0,
    "test_providers": 48.0,
    "test_meeting_compress": 46.0,
    "test_harness_agent": 43.0,
    "test_config_compat": 33.0,
    "test_boot_state": 32.0,
    "test_meeting_import": 27.0,
    "test_capability_admin": 26.0,
    "test_meeting_engine": 22.0,
    "test_agent_settings": 20.0,
    "test_capabilities_contract": 18.0,
    "test_components": 17.0,
    "test_backend_pairing": 16.0,
    "test_summary_provider": 16.0,
    "test_platform_settings": 15.0,
    "test_agent_panel_wiring": 15.0,
    "test_settings_meta": 13.0,
    "test_api_contract": 12.0,
    "test_model_cleanup": 12.0,
    "test_backend_ready": 12.0,
    "test_build_backend_kit": 11.0,
    "test_meeting_retranscribe_guard": 11.0,
    "test_asr_provider": 9.0,
    "test_path_seam": 7.0,
    "test_model_usage": 7.0,
    "test_install_state": 7.0,
    "test_stt_qwen3asr": 6.0,
    "test_admin_password_cli": 6.0,
    "test_modelinfo_ready": 6.0,
    "test_paths": 5.0,
}

#: Floor for every module: interpreter start + app import is ~0.7-1.6s here.
BASE_COST = 1.0

_RAN = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s", re.M)


def _ascii_safe_stdout():
    """Child output is decoded with errors='replace', so it can carry U+FFFD.

    On Windows the runner's own stdout is often cp936, and printing U+FFFD there
    raises UnicodeEncodeError *after* the tests already passed -- that would turn a
    green run red. Reconfigure to UTF-8 with a replacement fallback.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def quiet_libraries():
    """Silence the per-request INFO chatter that made the gate log 0.23 MB.

    `httpx` logs every test request at INFO, and the server's own logger chatters too.
    None of it is needed to know whether the suite passed, and all of it lands in the
    gate log that a human (or an agent) then has to read. Test *results* go to stderr,
    not through logging, so muting here cannot hide a failure.

    Applies to this process and (through `ECHO_GATE_QUIET_LIBS`) to anything it spawns,
    so the sequential path and the shard workers are silenced the same way.
    """
    import logging
    for name in ("httpx", "httpcore", "urllib3", "asyncio", "anyio"):
        logging.getLogger(name).setLevel(logging.WARNING)


# Also honour it when the gate silences its own output (`check-windows.ps1 -Quiet` sets
# this), so `-Quiet` really means "quiet" instead of "same volume, fewer progress lines".
if os.environ.get("ECHO_GATE_QUIET_LIBS"):
    quiet_libraries()


def python_exe():
    """Same interpreter resolution order as scripts/check-windows.ps1."""
    env = os.environ.get("ECHO_PYTHON")
    if env and os.path.isfile(env):
        return env
    candidates = [
        os.path.join(os.path.expanduser("~"), ".echo-venv", "Scripts", "python.exe"),
        os.path.join(ROOT, "runtime-core", "python.exe"),
        os.path.join(ROOT, "runtime-core", "Scripts", "python.exe"),
        os.path.join(ROOT, "venv", "Scripts", "python.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return sys.executable


def discover():
    names = []
    for fn in sorted(os.listdir(TESTS)):
        if fn.startswith("test_") and fn.endswith(".py"):
            names.append("tests." + fn[:-3])
    return names


def cost_of(module):
    short = module.split(".")[-1]
    if short in KNOWN_COST:
        return KNOWN_COST[short]
    path = os.path.join(TESTS, short + ".py")
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return BASE_COST + size / 900.0


def shard(modules, workers):
    """Greedy longest-processing-time-first split: deterministic and load-aware."""
    buckets = [[] for _ in range(workers)]
    load = [0.0] * workers
    for module in sorted(modules, key=cost_of, reverse=True):
        i = load.index(min(load))
        buckets[i].append(module)
        load[i] += cost_of(module)
    return [(b, l) for b, l in zip(buckets, load) if b]


def child_env():
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    # ResourceWarnings from a *passing* suite are noise the gate already tolerates;
    # they were a large share of a 0.23 MB gate log.
    if not env.get("ECHO_GATE_KEEP_WARNINGS"):
        env["PYTHONWARNINGS"] = "ignore::ResourceWarning"
    return env


def run_shard(modules, verbose):
    argv = [python_exe(), "-m", "unittest", "-q"] + list(modules)
    t0 = time.time()
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                          errors="replace", env=child_env())
    secs = time.time() - t0
    out = (proc.stdout or "") + (proc.stderr or "")
    if verbose:
        sys.stdout.write(out)
        sys.stdout.flush()
    return proc.returncode, secs, out


def summarise(out):
    match = _RAN.search(out or "")
    if not match:
        return ""
    return "Ran %s tests in %ss" % (match.group(1), match.group(2))


def main():
    _ascii_safe_stdout()
    parser = argparse.ArgumentParser(description="Run the ECHO test suite in parallel shards.")
    parser.add_argument("-j", "--workers", type=int, default=0,
                        help="shard count (0 = pick from CPU count and free RAM)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every shard's full output, not just failures")
    parser.add_argument("--modules", nargs="*", default=None,
                        help="restrict to these module names (default: discover all)")
    parser.add_argument("--no-quiet", action="store_true",
                        help="keep the libraries' per-request INFO logging (debugging only)")
    args = parser.parse_args()

    if not args.no_quiet:
        quiet_libraries()

    modules = args.modules or discover()
    if not modules:
        print("[check-parallel] no tests discovered under %s" % TESTS)
        return 1

    workers = args.workers or default_workers()
    workers = max(1, min(workers, len(modules)))
    buckets = shard(modules, workers)

    print("[check-parallel] %d modules -> %d shard(s)  interpreter=%s"
          % (len(modules), len(buckets), python_exe()))
    for i, (mods, load) in enumerate(buckets, 1):
        print("  shard %-2d est %5.0fs  %d module(s)" % (i, load, len(mods)))
    sys.stdout.flush()

    t0 = time.time()
    failed = []
    for i, (mods, _load) in enumerate(buckets, 1):
        code, secs, out = run_shard(mods, args.verbose)
        note = summarise(out)
        status = "PASS" if code == 0 else "FAIL"
        print("  [%s] shard %-2d %6.1fs  %s" % (status, i, secs, note), flush=True)
        if code != 0:
            failed.append((i, mods, out))
    total = time.time() - t0

    for i, mods, out in failed:
        print("\n" + "=" * 72)
        print("shard %d FAILED (exit != 0): %s" % (i, " ".join(mods)))
        print("=" * 72)
        print(out)
    if failed:
        print("\n[check-parallel] %d/%d shard(s) FAILED in %.1fs -- do not push."
              % (len(failed), len(buckets), total))
        return 1
    print("[check-parallel] all %d shard(s) passed in %.1fs" % (len(buckets), total))
    return 0


def default_workers():
    """Leave headroom: a shard that imports torch peaks near 0.8 GB."""
    cpu = os.cpu_count() or 4
    workers = max(1, min(8, cpu // 3))
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            by_ram = int((status.ullAvailPhys / (1024 ** 3)) / 0.9)
            workers = max(1, min(workers, by_ram))
    except Exception:
        pass
    return workers


if __name__ == "__main__":
    _ascii_safe_stdout()
    sys.exit(main())
