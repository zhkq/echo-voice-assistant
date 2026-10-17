# -*- coding: utf-8 -*-
"""出包的互斥锁 + 失败原因可见性（2026-10-17/18 两次"检查全 ok 却 exit=1"之后加的）。

现场（用户 2026-10-17/18 真机流程里连着遇到两次）::

    [fail] build-package.ps1 失败（win-x64, exit=1）：
      [ok]   no forbidden files or content
      [ok]   main package size gate: 16.46 MB <= 20 MB

所有检查都是 ok，然后 exit=1，重跑立刻好。根因不是"偶发"：两个打包同时写**同一个 dist**
时撞的是**同一批输出文件名**（`ECHO-main-<platform>-<版本>-<yyyyMMdd-HHmm>.zip` 只精确到
分钟；kit 目录/zip 用的也是同一个分钟 stamp），后到的那个在
`build-package.ps1` 的 `ZipFile::Open(..., Create)` 上拿到
``The file '…zip' already exists.`` —— 这一步在全部 [ok] 之后，于是屏幕上只剩 [ok] 和 exit=1。

本文件钉住三件事：

1. **两个打包并发时不许静默失败**：要么串行成功（第二个等锁），要么明确说
   "另一个打包正在跑"（带 pid、等了多久）—— 见 `TwoBuildsDoNotTrampleEachOther`；
2. **失败必须带出原错误**（`build-package.ps1` 的真实错误行），见 `FailuresShowTheRealReason`；
3. **单跑照旧正常**，且 `--dist` 真的把包打到那个目录（以前 `-OutDir` 根本没传给 ps1，
   `--dist` 只影响"去哪里找结果"），见 `TheRealPs1StillWorks`。

跑法（别跑整轮 discover）::

    venv\\Scripts\\python.exe -m unittest tests.test_build_package_lock
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
REAL_PS1 = SCRIPTS / "build-package.ps1"
WIN = {
    "key": "win", "label": "win-x64", "kit_prefix": "ECHO-kit",
    "package_platform": None, "readme": "kit-readme-win.md",
    "cmd_template": "kit-install.cmd",
}


def _load_build_kit():
    """把 scripts/build_kit.py 当模块加载（它不在包路径里）。"""
    spec = importlib.util.spec_from_file_location(
        "build_kit_lock_under_test", SCRIPTS / "build_kit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_kit = _load_build_kit()


# --------------------------------------------------------------------- 小工具
def _mini_main_zip(path: Path) -> Path:
    """造一个最小主包（顶层 `ECHO/`，与 build-package.ps1 的 main 档同布局）。

    组 kit 时只认这几样：`ECHO/app/main.py`（代码目录的判据）、`ECHO/manifest.json`、
    以及 zip 根的 `BUILD-INFO.txt` / `SHA256SUMS.txt`。
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ECHO/app/main.py", "# stub app\n")
        zf.writestr("ECHO/manifest.json", '{"platform": "win-x64"}')
        zf.writestr("BUILD-INFO.txt", "git: deadbee\nunpacked: 0.01 MB\n")
        zf.writestr("SHA256SUMS.txt", "")
    return path


_STUB_PS1 = r"""param(
    [string]$Profile = 'main',
    [string]$OutDir = '',
    [string]$Version = '',
    [string]$Component = '',
    [string[]]$Components = @(),
    [string]$Platform = '',
    [switch]$DryRun,
    [int]$LockTimeoutSeconds = 600,
    [int]$LockStaleSeconds = 1800
)
$ErrorActionPreference = 'Stop'
$trace = $env:ECHO_TEST_TRACE
if ($trace) { Add-Content -LiteralPath $trace -Value ("start {0} {1}" -f $PID, [DateTime]::UtcNow.Ticks) }
Start-Sleep -Milliseconds ([int]$env:ECHO_TEST_SLEEP_MS)
Write-Host '  [ok]   no forbidden files or content'
Write-Host '  [ok]   main package size gate: 16.46 MB <= 20 MB'
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$target = Join-Path $OutDir ("ECHO-main-win-x64-2.1.7-" + $stamp + ".zip")
Copy-Item -LiteralPath $env:ECHO_TEST_MAIN_ZIP -Destination $target -Force
if ($trace) { Add-Content -LiteralPath $trace -Value ("end {0} {1}" -f $PID, [DateTime]::UtcNow.Ticks) }
exit 0
"""

_FAILING_PS1 = r"""param(
    [string]$Profile = 'main',
    [string]$OutDir = '',
    [string]$Version = '',
    [string]$Component = '',
    [string[]]$Components = @(),
    [string]$Platform = '',
    [switch]$DryRun,
    [int]$LockTimeoutSeconds = 600,
    [int]$LockStaleSeconds = 1800
)
$ErrorActionPreference = 'Stop'
$trace = $env:ECHO_TEST_TRACE
if ($trace) { Add-Content -LiteralPath $trace -Value ("try {0}" -f $PID) }
Write-Host '  [ok]   no forbidden files or content'
Write-Host '  [ok]   main package size gate: 16.46 MB <= 20 MB'
throw 'boom: injected package failure'
"""


class _TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="echolock-")
        self.tmp = Path(self._tmp.name)
        self.dist = self.tmp / "dist"
        self.main_zip = _mini_main_zip(self.tmp / "main-template.zip")
        self._env_backup = {k: os.environ.get(k) for k in
                            ("ECHO_TEST_TRACE", "ECHO_TEST_SLEEP_MS",
                             "ECHO_TEST_MAIN_ZIP", "ECHO_PACK_LOCK_OWNER")}

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _env(self, name, value):
        os.environ[name] = str(value)


# --------------------------------------------------------- 1. 两个打包并发
class TwoBuildsDoNotTrampleEachOther(_TempTree):
    """两个打包并发：必须**串行成功**（第二个等锁），不许静默 exit=1。

    锁住在出包这一步上，是因为撞的是 `dist` 里的**文件名**：主包名只精确到分钟，
    kit 目录/zip 用的也是同一个分钟 stamp —— 只锁 ps1 那一步不够，组 kit 时两个进程
    照样会互相 `rmtree` 掉对方正在写的目录。
    """

    def test_both_runs_succeed_and_never_overlap(self):
        stub = self.tmp / "stub-ps1.ps1"
        stub.write_text(_STUB_PS1, encoding="ascii")
        trace = self.tmp / "trace.txt"
        self._env("ECHO_TEST_TRACE", trace)
        self._env("ECHO_TEST_SLEEP_MS", "600")
        self._env("ECHO_TEST_MAIN_ZIP", self.main_zip)

        old = build_kit.BUILD_PACKAGE
        build_kit.BUILD_PACKAGE = stub
        results = {}

        def one(name):
            try:
                results[name] = build_kit.do_build(
                    self.dist, [WIN], "20261018-0101", False, False, lock_timeout=120)
            except Exception as exc:                     # noqa: BLE001 - 记下来给人看
                results[name] = exc

        threads = [threading.Thread(target=one, args=(n,)) for n in ("a", "b")]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(180)
        finally:
            build_kit.BUILD_PACKAGE = old

        self.assertEqual(sorted(str(v) for v in results.values()), ["0", "0"],
                         f"两个打包没有都成功（{results}）—— 不许静默失败")
        # 真的串行了：两个 ps1 的区间不能重叠（trace 里不许出现连续两个 start）
        kinds = [line.split()[0] for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(kinds), 4, f"应该正好两次 ps1 调用，实际 {kinds}")
        self.assertNotIn(("start", "start"), list(zip(kinds, kinds[1:])),
                         f"两个打包重叠了（谁都没等锁）：{kinds}")
        # 第二个是等锁之后**重做**的同名 kit，不是半成品
        self.assertTrue((self.dist / "ECHO-kit-20261018-0101.zip").is_file())
        self.assertFalse((self.dist / build_kit.LOCK_NAME).exists(), "锁没释放")

    def test_waiting_times_out_by_naming_the_other_build(self):
        """等不到就必须**明说**在等谁（pid + 等了多久），不是静默 exit=1。"""
        self.dist.mkdir(parents=True, exist_ok=True)
        lock = self.dist / build_kit.LOCK_NAME
        lock.write_text(f"pid={os.getpid()}\ntoken=someoneelse\n"
                        f"started=2026-10-17T23:59:00\n", encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(build_kit.BuildError) as ctx:
            with build_kit.pack_lock(self.dist, timeout=1.0):
                self.fail("不该拿到锁：那把锁是活的")
        message = str(ctx.exception)
        self.assertIn("另一个打包正在跑", message)
        self.assertIn(str(os.getpid()), message)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_lock_left_by_a_dead_process_is_cleaned_up(self):
        """上一个打包崩了留下的锁不能把后面的人堵十分钟（pid 已不在 → 立刻清掉）。"""
        self.dist.mkdir(parents=True, exist_ok=True)
        dead = subprocess.Popen(["cmd", "/c", "exit", "0"])
        dead.wait(timeout=30)
        lock = self.dist / build_kit.LOCK_NAME
        lock.write_text(f"pid={dead.pid}\ntoken=gone\n", encoding="utf-8")
        self.assertFalse(build_kit._pid_alive(dead.pid), "那条 cmd 应该已经退出了")
        with build_kit.pack_lock(self.dist, timeout=5):
            self.assertTrue(lock.exists(), "持锁期间锁文件应该在")
        self.assertFalse(lock.exists(), "放锁之后锁文件应该在")

    def test_pid_probe_does_not_kill_anything(self):
        """`_pid_alive` 只是**问**，不许像 os.kill(pid, 0) 那样把进程杀掉。"""
        victim = subprocess.Popen(["cmd", "/c", "ping -n 6 127.0.0.1 > nul"])
        try:
            self.assertTrue(build_kit._pid_alive(victim.pid))
            self.assertTrue(build_kit._pid_alive(os.getpid()))
            self.assertFalse(build_kit._pid_alive(0))
            self.assertIsNone(victim.poll(), "探活把进程杀了 —— 守卫写成了杀手")
        finally:
            victim.kill()
            victim.wait(timeout=30)


# --------------------------------------------------------- 2. 失败要带原因
class FailuresShowTheRealReason(_TempTree):
    """失败路径必须把 `build-package.ps1` 的**真实错误行**顶到第一屏。"""

    def test_a_failing_ps1_surfaces_its_error_and_is_retried(self):
        stub = self.tmp / "boom-ps1.ps1"
        stub.write_text(_FAILING_PS1, encoding="ascii")
        trace = self.tmp / "trace.txt"
        self._env("ECHO_TEST_TRACE", trace)
        self._env("ECHO_TEST_MAIN_ZIP", self.main_zip)

        old = build_kit.BUILD_PACKAGE
        build_kit.BUILD_PACKAGE = stub
        try:
            with self.assertRaises(build_kit.BuildError) as ctx:
                build_kit.do_build(self.dist, [WIN], "20261018-0101", False, False)
        finally:
            build_kit.BUILD_PACKAGE = old
        message = str(ctx.exception)
        self.assertIn("boom: injected package failure", message,
                      "真实错误行没带出来 —— 这就是本次最该修的『看不出来』")
        self.assertIn("真实原因", message)
        self.assertIn("exit=", message)
        attempts = [ln for ln in trace.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(attempts), 2, "应该带原因重试一次（1 次重试）")

    def test_the_real_ps1_names_the_reason_instead_of_only_exiting(self):
        """真的 build-package.ps1：意外失败必须打一行 `[fail] <原因>`，而不是只有 exit=1。"""
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")           # 让 <它>\dist 建不出来
        code, out = build_kit.run(
            [build_kit.powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", REAL_PS1, "-Profile", "main",
             "-OutDir", str(blocker / "dist")])
        self.assertNotEqual(code, 0, "这条路本来就不可能成功")
        fails = [ln for ln in out.splitlines() if "[fail]" in ln]
        self.assertTrue(fails, f"没有 [fail] 行：\n{out[-1500:]}")
        reason = fails[0].split("[fail]", 1)[1].strip()
        self.assertGreater(len(reason), 8, f"[fail] 后面没写原因：{fails[0]!r}")


# --------------------------------------------------------- 3. 单跑照旧 + --dist
class TheRealPs1StillWorks(_TempTree):
    def test_one_build_lands_in_the_given_dist_and_releases_the_lock(self):
        """单跑正常；而且 `-OutDir` 真的传给了 ps1（`--dist` 以前是个谎）。"""
        repo_dist = build_kit.DIST
        before = set(repo_dist.glob("ECHO-main-win-x64-*.zip")) if repo_dist.is_dir() else set()
        code = build_kit.do_build(self.dist, [WIN], "20261018-0102", False, False)
        self.assertEqual(code, 0)
        built = list(self.dist.glob("ECHO-main-win-x64-*.zip"))
        self.assertEqual(len(built), 1, f"主包没落在 --dist 指的目录里：{list(self.dist.iterdir())}")
        with zipfile.ZipFile(built[0]) as zf:
            self.assertIn("ECHO/manifest.json", zf.namelist())
        after = set(repo_dist.glob("ECHO-main-win-x64-*.zip")) if repo_dist.is_dir() else set()
        self.assertEqual(after - before, set(), "主包漏到了仓库自己的 dist/ 里（-OutDir 没传）")
        self.assertFalse((self.dist / build_kit.LOCK_NAME).exists(), "锁没释放")

    def test_the_script_keeps_its_bom_and_parses(self):
        raw = REAL_PS1.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                        "build-package.ps1 丢了 UTF-8 BOM —— WinPS 5.1 会按 GBK 解析，直接报错")
        text = raw.decode("utf-8")
        self.assertIn(".pack.lock", text, "打包锁被拿掉了？两个打包又会互相踩")
        self.assertIn("ECHO_PACK_LOCK_OWNER", text,
                      "ps1 不再认父进程的锁 —— 父子会自己等自己，等到超时")
        checker = (
            "$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{REAL_PS1}', [ref]$null, [ref]$errs); "
            "if ($errs.Count) { $errs | ForEach-Object { $_.Message }; exit 1 }"
        )
        proc = subprocess.run([build_kit.powershell_exe(), "-NoProfile",
                               "-ExecutionPolicy", "Bypass", "-Command", checker],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        self.assertEqual(proc.returncode, 0,
                         f"Parser.ParseFile 报错（WinPS 5.1 会解析失败）：\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
