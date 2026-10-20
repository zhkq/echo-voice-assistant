# -*- coding: utf-8 -*-
"""supervisor 自愈 Startup 快捷方式 + 日志轮转 / 心跳去重（2026-10-19 真机事故）。

现场
----
1. Startup 里的 ``ECHO startup.lnk`` **不见了** → 登录后没人拉起 ECHO；
   而 supervisor 日志 1.36 MB，每 6 秒一行 ``active '...' healthy ...``。

要钉住的契约
------------
* 「快捷方式该指向什么」（wscript.exe + ``~/.echo-switch/echo-switch-startup.vbs``）
  只有 **一处真相**：``echo-instance-lib.ps1`` 的 ``Get-EchoStartupShortcutSpec``；
  ``install-switcher.ps1`` 与 ``echo-supervisor.ps1`` 都走它，源码里不许再抄一份。
* supervisor 启动时 + 每 30 分钟的 tick 里检查；缺失 / 指错 → 重写，
  指向正确 → **一个字都不动**（幂等）。
* 日志 > 2 MB → 轮转成 ``.1``（只留一份，旧的 ``.1`` 被覆盖）；
  心跳：状态变化立即写 + 每 10 分钟一条摘要 + 其余 tick 保持安静；
  重要事件（启动/退出/异常/自愈/启动实例）**永远不去重**。

纪律
----
快捷方式用例一律在**临时 Startup 目录**里跑（``-StartupDir`` / ``-SwitchDir``），
并且 ``StartupShortcutSelfHealTests`` 在 setUp/tearDown 里比对**真实** Startup
快捷方式的 mtime —— 用例碰了真机就是红。绝不写真实 Startup、绝不起停 supervisor。

⚠️ 本机端点防护会**杀掉创建独立 `.lnk` 的进程**（2026-10-19 实测：非 Start Menu
位置一写 `.lnk`，PowerShell 直接消失，文件约 1 秒后也被删）。所以用例把
``Read-EchoShortcutLink`` / ``Write-EchoShortcutLink`` 这两个薄 I/O 函数换成
"同路径的文本替身"，缺失/指错/重建/幂等全部按真实逻辑跑；生产那份真 COM 实现
由 ``ShortcutSingleTruthTests`` 的源码断言钉住，不会悄悄烂掉。
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
LIB = SCRIPTS / "echo-instance-lib.ps1"
SUPERVISOR = SCRIPTS / "echo-supervisor.ps1"
INSTALLER = SCRIPTS / "install-switcher.ps1"

MAX_BYTES = 2 * 1024 * 1024

RESULT_WRITER = (
    # Deliver the result on STDOUT, not through a temp file: on this box a
    # fresh standalone .lnk (and occasionally the neighbouring scratch files)
    # is removed by real-time protection a second or so after it is written,
    # and a missing out.json made this suite flaky for a reason that has
    # nothing to do with the supervisor.
    "$json = $result | ConvertTo-Json -Depth 6 -Compress\n"
    "Write-Output ('ECHO_RESULT:' + $json)\n"
)
RESULT_MARKER = "ECHO_RESULT:"


def _powershell():
    for name in ("pwsh", "powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("找不到 PowerShell")


def _run_ps(work, name, body, env_extra=None):
    """Run a generated ASCII-only .ps1 that dot-sources the shared lib."""
    script = Path(work) / (name + ".ps1")
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        ". '%s'\n" % _psq(LIB) + body,
        encoding="utf-8-sig")
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        # 超时给得宽一点：这类用例真的起 powershell.exe，而门禁常与录音/安卓构建同时跑，
        # 120 秒在忙机子上会偶发超时（2026-10-20 实测一次：跑满 120 秒被判失败，单跑 8 秒就过）。
        capture_output=True, env=env, timeout=300)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise AssertionError(
            "PowerShell 退出码 %s\nstdout:\n%s\nstderr:\n%s" %
            (proc.returncode, stdout, stderr))
    payload = None
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            payload = line[len(RESULT_MARKER):]
    if payload is None:
        raise AssertionError(
            "PowerShell 没有输出结果行\nstdout:\n%s\nstderr:\n%s" %
            (stdout, stderr))
    return json.loads(payload)


def _read(path):
    with io.open(str(path), encoding="utf-8") as fh:
        return fh.read()


def _strip_comment_lines(text):
    """去掉整行注释（PowerShell 的 ``#``）。注释里提到名字不算"第二份真相"。"""
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


def _psq(path):
    """PowerShell 单引号字符串转义。"""
    return str(path).replace("'", "''")


def _same_path(a, b):
    return (os.path.normcase(os.path.abspath(a)) ==
            os.path.normcase(os.path.abspath(b)))


def _real_startup_link():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / \
        "Programs" / "Startup" / "ECHO startup.lnk"


# ===========================================================================
# 1. 同一处真相（源码断言）
# ===========================================================================
class ShortcutSingleTruthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = _read(LIB)
        cls.sup = _read(SUPERVISOR)
        cls.install = _read(INSTALLER)

    def test_the_expected_link_is_defined_exactly_once(self):
        """链接名 + wscript + vbs 的真理只在 lib 里出现；两个调用方不许抄。"""
        self.assertIn("function Get-EchoStartupShortcutSpec", self.lib)
        self.assertEqual(
            _strip_comment_lines(self.lib).count("'ECHO startup.lnk'"), 1,
            "链接名只许在 Get-EchoStartupShortcutSpec 里定义一次")
        for name, text in (("install-switcher.ps1", self.install),
                           ("echo-supervisor.ps1", self.sup)):
            code = _strip_comment_lines(text)
            self.assertNotIn("ECHO startup.lnk", code,
                             "%s 里又硬编码了链接名 —— 真相只在 lib" % name)
            self.assertNotIn("System32\\wscript.exe", code,
                             "%s 里又硬编码了 wscript 目标 —— 真相只在 lib" % name)

    def test_installer_writes_the_shared_spec(self):
        self.assertIn("Get-EchoStartupShortcutSpec", self.install)
        self.assertIn("$lnk.TargetPath = $spec.TargetPath", self.install)
        self.assertIn("$lnk.Arguments = $spec.Arguments", self.install)
        self.assertIn("$lnk.WorkingDirectory = $spec.WorkingDirectory", self.install)
        # 安装脚本必须 dot-source 那份 lib，否则拿不到同一个函数
        self.assertIn("echo-instance-lib.ps1", self.install)

    def test_supervisor_checks_and_restores_through_the_same_spec(self):
        self.assertIn("Get-EchoStartupShortcutState", self.sup)
        self.assertIn("Restore-EchoStartupShortcut", self.sup)
        self.assertNotIn("CreateShortcut", self.sup,
                         "supervisor 不许自己写 .lnk —— 走 Restore-EchoStartupShortcut")

    def test_the_real_shortcut_io_is_pinned_to_windows_com(self):
        """生产路径必须真的用 WScript.Shell 读写 .lnk（测试替身只在用例里）。"""
        code = _strip_comment_lines(self.lib)
        self.assertIn("function Read-EchoShortcutLink", code)
        self.assertIn("function Write-EchoShortcutLink", code)
        self.assertEqual(code.count("New-Object -ComObject WScript.Shell"), 2,
                         "只允许 Read/Write-EchoShortcutLink 这两个 I/O 函数碰 COM")
        self.assertIn("$lnk.TargetPath = $Spec.TargetPath", code)
        self.assertIn("$lnk.Arguments = $Spec.Arguments", code)
        self.assertIn("$lnk.Save()", code)
        self.assertIn("Read-EchoShortcutLink -Path $spec.LinkPath", self.lib)
        self.assertIn("Write-EchoShortcutLink -Spec $state.Spec", self.lib)

    def test_it_checks_at_startup_and_every_30_minutes(self):
        self.assertIn("$script:StartupCheckMinutes = 30", self.sup)
        self.assertEqual(self.sup.count("Invoke-StartupShortcutCheck -DryRun:$DryRun"), 2,
                         "一处启动时检查 + 一处 tick 里的定时检查")
        self.assertIn("$script:NextStartupCheck", self.sup)

    def test_a_restore_is_logged_as_a_warning_with_the_reason(self):
        self.assertIn("startup shortcut restored ($($st.Reason))", self.sup)
        self.assertIn("'WARN'", self.sup)


# ===========================================================================
# 2. 日志轮转（源码接线 + 真跑临时文件）
# ===========================================================================
class LogWiringTests(unittest.TestCase):
    def test_suplog_rotates_before_appending(self):
        sup = _read(SUPERVISOR)
        rot = sup.index("Invoke-EchoLogRotation -Path $script:LogPath")
        append = sup.index("Add-Content -Path $script:LogPath")
        self.assertLess(rot, append, "必须先轮转再追加，否则单个超大行能突破 2 MB")

    def test_only_heartbeat_lines_are_deferred_to_the_deduper(self):
        sup = _read(SUPERVISOR)
        self.assertEqual(sup.count('SupHeartbeat "'), 2,
                         "只有 healthy / booting 两行走进心跳去重")
        for event in ("is down - starting",
                      "is up on port",
                      "failed to come up",
                      "startup shortcut restored",
                      "===== echo-supervisor start",
                      "===== echo-supervisor stop"):
            self.assertIn(event, sup)
            self.assertNotIn("SupHeartbeat \"%s" % event, sup,
                             "重要事件 %r 不许走心跳去重" % event)


@unittest.skipUnless(os.name == "nt", "PowerShell 只在 Windows 上跑")
class LogRotationTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="echo-rotate-"))
        self.log = self.work / "sup.log"

    def tearDown(self):
        shutil.rmtree(str(self.work), ignore_errors=True)

    def _rotate(self, body_extra):
        return _run_ps(self.work, "rotate",
                       "$p = '%s'\n%s\n%s" % (_psq(self.log), body_extra,
                                              RESULT_WRITER))

    def test_a_log_past_2mb_is_rotated_keeping_one_backup(self):
        payload = "A" * (MAX_BYTES + 4096)
        self.log.write_text(payload, encoding="ascii")
        data = self._rotate(
            "$rotated = Invoke-EchoLogRotation -Path $p -MaxBytes 2097152\n"
            "$bak = $p + '.1'\n"
            "$bakLen = 0\n"
            "if (Test-Path -LiteralPath $bak) { $bakLen = (Get-Item -LiteralPath $bak).Length }\n"
            "$again = Invoke-EchoLogRotation -Path $p -MaxBytes 2097152\n"
            "$result = [pscustomobject]@{ rotated=$rotated; logExists=(Test-Path -LiteralPath $p); "
            "bakExists=(Test-Path -LiteralPath $bak); bakLen=$bakLen; again=$again }")
        self.assertTrue(data["rotated"], "超过 2 MB 必须轮转")
        self.assertFalse(data["logExists"], "轮转后原日志应已挪走（下一行会新建）")
        self.assertTrue(data["bakExists"])
        self.assertEqual(data["bakLen"], len(payload), "备份必须是完整的那份旧日志")
        self.assertFalse(data["again"], "没有日志时再调一次应当是 no-op")

    def test_a_small_log_is_left_alone(self):
        self.log.write_bytes(b"small log\n")
        bak = Path(str(self.log) + ".1")
        bak.write_bytes(b"old backup\n")
        data = self._rotate(
            "$rotated = Invoke-EchoLogRotation -Path $p -MaxBytes 2097152\n"
            "$result = [pscustomobject]@{ rotated=$rotated; "
            "logText=[System.IO.File]::ReadAllText($p); "
            "bakText=[System.IO.File]::ReadAllText($p + '.1') }")
        self.assertFalse(data["rotated"])
        self.assertEqual(data["logText"], "small log\n")
        self.assertEqual(data["bakText"], "old backup\n",
                         "没轮转就不许碰旧的 .1")

    def test_rotating_again_overwrites_the_old_backup(self):
        bak = Path(str(self.log) + ".1")
        bak.write_bytes(b"old backup\n")
        payload = "B" * (MAX_BYTES + 123)
        self.log.write_text(payload, encoding="ascii")
        data = self._rotate(
            "$rotated = Invoke-EchoLogRotation -Path $p -MaxBytes 2097152\n"
            "$bak = $p + '.1'\n"
            "$result = [pscustomobject]@{ rotated=$rotated; "
            "bakLen=(Get-Item -LiteralPath $bak).Length; "
            "first=[System.IO.File]::ReadAllText($bak).Substring(0,1) }")
        self.assertTrue(data["rotated"])
        self.assertEqual(data["bakLen"], len(payload), "只保留一份：旧的 .1 要被覆盖")
        self.assertEqual(data["first"], "B")


# ===========================================================================
# 3. 心跳去重 + 10 分钟摘要
# ===========================================================================
@unittest.skipUnless(os.name == "nt", "PowerShell 只在 Windows 上跑")
class HeartbeatDedupeTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="echo-heartbeat-"))

    def tearDown(self):
        shutil.rmtree(str(self.work), ignore_errors=True)

    def _decide(self):
        body = (
            "$t0 = [datetime]::Parse('2026-10-19T10:00:00')\n"
            "$key = 'healthy|port=18060|echo=1|router=1|supervisor=1'\n"
            "$state = $null\n"
            "$decisions = @()\n"
            "$times = @($t0, $t0.AddSeconds(6), $t0.AddSeconds(12), $t0.AddMinutes(9),\n"
            "           $t0.AddMinutes(10), $t0.AddMinutes(10).AddSeconds(6))\n"
            "foreach ($now in $times) {\n"
            "    $d = Get-EchoHeartbeatDecision -State $state -Key $key -Now $now -SummaryMinutes 10\n"
            "    $state = $d.State\n"
            "    $decisions += [pscustomobject]@{ kind=$d.Kind; minutes=$d.Minutes }\n"
            "}\n"
            "$now2 = $t0.AddMinutes(11)\n"
            "$d2 = Get-EchoHeartbeatDecision -State $state -Key ($key -replace 'router=1', 'router=0') "
            "-Now $now2 -SummaryMinutes 10\n"
            "$result = [pscustomobject]@{ decisions = @($decisions); "
            "change = [pscustomobject]@{ kind=$d2.Kind; minutes=$d2.Minutes } }\n"
            "%s" % RESULT_WRITER
        )
        return _run_ps(self.work, "heartbeat", body)

    def test_repeats_are_silent_until_the_ten_minute_summary(self):
        data = self._decide()
        kinds = [d["kind"] for d in data["decisions"]]
        self.assertEqual(
            kinds, ["change", "none", "none", "none", "summary", "none"],
            "重复的心跳只写第一条，10 分钟写一条摘要，其余保持安静")
        self.assertEqual(data["decisions"][4]["minutes"], 10,
                         "摘要要能说出「已连续 N 分钟」")
        self.assertEqual(data["change"]["kind"], "change",
                         "状态真的变了要立即写")

    def test_switching_back_to_the_same_state_logs_again(self):
        """healthy → booting → healthy（计数相同）也必须再写一次，不能被去重吃掉。"""
        body = (
            "$t = [datetime]::Parse('2026-10-19T10:00:00')\n"
            "$s = $null\n"
            "$a = Get-EchoHeartbeatDecision -State $s -Key 'healthy|x' -Now $t\n"
            "$s = $a.State\n"
            "$b = Get-EchoHeartbeatDecision -State $s -Key 'booting|x' -Now $t.AddSeconds(6)\n"
            "$s = $b.State\n"
            "$c = Get-EchoHeartbeatDecision -State $s -Key 'healthy|x' -Now $t.AddSeconds(12)\n"
            "$result = [pscustomobject]@{ a=$a.Kind; b=$b.Kind; c=$c.Kind }\n"
            "%s" % RESULT_WRITER
        )
        data = _run_ps(self.work, "again", body)
        self.assertEqual([data["a"], data["b"], data["c"]],
                         ["change", "change", "change"])


# ===========================================================================
# 4. 快捷方式自愈（在临时目录里真跑，绝不碰真实 Startup）
# ===========================================================================
@unittest.skipUnless(os.name == "nt", "PowerShell 只在 Windows 上跑")
class StartupShortcutSelfHealTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="echo-selfheal-"))
        self.startup = self.work / "Startup"
        self.switch = self.work / "echo-switch"
        self.startup.mkdir()
        self.switch.mkdir()
        # The real ~/.echo-switch always carries this launcher; having it here
        # keeps VbsExists true (the supervisor warns when it is absent).
        (self.switch / "echo-switch-startup.vbs").write_bytes(b"' test stub\r\n")
        self.real = _real_startup_link()
        self.real_before = self.real.stat().st_mtime_ns if self.real.exists() else None
        self._n = 0

    def tearDown(self):
        try:
            after = self.real.stat().st_mtime_ns if self.real.exists() else None
            self.assertEqual(
                self.real_before, after,
                "用例碰了真实 Startup 里的快捷方式（%s）—— 绝不允许" % self.real)
        finally:
            shutil.rmtree(str(self.work), ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def _run(self, lines, env_extra=None):
        self._n += 1
        body = (
            "$StartupDir = '%s'\n"
            "$SwitchDir = '%s'\n"
            # NEVER write a real .lnk here: this box's endpoint protection kills
            # a process that creates a standalone .lnk outside the Start Menu
            # (2026-10-19, reproduced: powershell vanishes, the file is deleted
            # a second later). These two overrides keep the exact same path -
            # `<temp Startup>\ECHO startup.lnk` in the temp Startup folder - but
            # store a tiny text stand-in, so missing/wrong/rebuild/idempotence
            # all run for real. The real COM implementation is pinned by
            # ShortcutSingleTruthTests so it cannot silently rot.
            "function Read-EchoShortcutLink {\n"
            "    param([string]$Path)\n"
            "    $lines = [System.IO.File]::ReadAllLines($Path)\n"
            "    return [pscustomobject]@{ TargetPath = $lines[0]; Arguments = $lines[1] }\n"
            "}\n"
            "function Write-EchoShortcutLink {\n"
            "    param($Spec)\n"
            "    $enc = New-Object System.Text.UTF8Encoding($false)\n"
            "    [System.IO.File]::WriteAllText($Spec.LinkPath, "
            "($Spec.TargetPath + \"`n\" + $Spec.Arguments + \"`n\"), $enc)\n"
            "}\n"
            "%s\n"
            "%s" % (_psq(self.startup), _psq(self.switch),
                    "\n".join(lines), RESULT_WRITER)
        )
        return _run_ps(self.work, "step%d" % self._n, body, env_extra)

    def _heal(self, setup=None):
        """check -> restore -> check, all in ONE PowerShell process.

        The *logic* is real (same temp Startup path, same decision function,
        same restore function, idempotence checked via the file's mtime); only
        the .lnk serializer is the text stand-in from ``_run``, because writing
        a real shortcut here gets the process killed by endpoint protection.
        """
        lines = list(setup or [])
        lines += [
            "$a = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$r = Restore-EchoStartupShortcut -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$b = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$actual = Read-EchoShortcutLink -Path $b.Spec.LinkPath",
            "$result = [pscustomobject]@{",
            "    beforeNeeded = $a.Needed;",
            "    beforeReason = $a.Reason;",
            "    changed = $r.Changed;",
            "    afterNeeded = $b.Needed;",
            "    linkExists = (Test-Path -LiteralPath $b.Spec.LinkPath);",
            "    target = [string]$actual.TargetPath;",
            "    arguments = [string]$actual.Arguments;",
            "    specTarget = $b.Spec.TargetPath;",
            "    specVbs = $b.Spec.VbsPath;",
            "}",
        ]
        return self._run(lines)

    def _plant_wrong_link(self, target_expr, arguments_expr):
        """Lines that first put a wrong link in place (same process as _heal)."""
        return [
            "$enc0 = New-Object System.Text.UTF8Encoding($false)",
            "$l0 = Join-Path $StartupDir 'ECHO startup.lnk'",
            "[System.IO.File]::WriteAllText($l0, (%s + \"`n\" + %s + \"`n\"), $enc0)" %
            (target_expr, arguments_expr),
        ]

    def _assert_repaired(self, data, reason):
        self.assertTrue(data["beforeNeeded"], "缺失/指错时必须报告需要重建")
        self.assertEqual(data["beforeReason"], reason)
        self.assertTrue(data["changed"], "报告需要重建就必须真的重建")
        self.assertFalse(data["afterNeeded"], "重建后再检查应当已经正确")
        self.assertTrue(data["linkExists"])
        self.assertTrue(_same_path(data["target"], data["specTarget"]),
                        "目标必须是 wscript.exe：%r" % data["target"])
        self.assertIn(data["specVbs"].lower(), data["arguments"].lower(),
                      "参数必须指向 .echo-switch 里的启动 vbs")
        self.assertTrue(_same_path(
            data["specVbs"], str(self.switch / "echo-switch-startup.vbs")))

    # -- cases -----------------------------------------------------------
    def test_missing_shortcut_is_reported_and_rebuilt(self):
        self.assertFalse((self.startup / "ECHO startup.lnk").exists())
        self._assert_repaired(self._heal(), "missing")

    def test_wrong_target_is_rebuilt(self):
        data = self._heal(self._plant_wrong_link(
            r"(Join-Path $env:SystemRoot 'System32\notepad.exe')", "''"))
        self._assert_repaired(data, "wrong-target")

    def test_wrong_arguments_are_rebuilt(self):
        other = self.work / "some-other.vbs"
        data = self._heal(self._plant_wrong_link(
            r"(Join-Path $env:SystemRoot 'System32\wscript.exe')",
            '"%s"' % other))
        self._assert_repaired(data, "wrong-arguments")

    def test_a_correct_shortcut_is_never_touched(self):
        first = self._run([
            "$a = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$r1 = Restore-EchoStartupShortcut -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$before = (Get-Item -LiteralPath $a.Spec.LinkPath).LastWriteTimeUtc.Ticks",
            "Start-Sleep -Milliseconds 50",
            "$r2 = Restore-EchoStartupShortcut -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$after = (Get-Item -LiteralPath $a.Spec.LinkPath).LastWriteTimeUtc.Ticks",
            "$b = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$result = [pscustomobject]@{ firstChanged=$r1.Changed; "
            "secondChanged=$r2.Changed; secondWouldChange=$r2.WouldChange; "
            "before=$before; after=$after; needed=$b.Needed }",
        ])
        self.assertTrue(first["firstChanged"])
        self.assertFalse(first["secondChanged"], "已经正确就不许再写")
        self.assertFalse(first["secondWouldChange"])
        self.assertEqual(first["before"], first["after"],
                         "指向正确的 .lnk 一个字都不许动（幂等）")
        self.assertFalse(first["needed"])

    def test_dry_run_reports_without_writing(self):
        data = self._run([
            "$a = Get-EchoStartupShortcutState -StartupDir $StartupDir -SwitchDir $SwitchDir",
            "$r = Restore-EchoStartupShortcut -StartupDir $StartupDir -SwitchDir $SwitchDir -DryRun",
            "$result = [pscustomobject]@{ needed=$a.Needed; reason=$a.Reason; "
            "wouldChange=$r.WouldChange; changed=$r.Changed; "
            "linkExists=(Test-Path -LiteralPath $a.Spec.LinkPath) }",
        ])
        self.assertTrue(data["needed"])
        self.assertTrue(data["wouldChange"])
        self.assertFalse(data["changed"])
        self.assertFalse(data["linkExists"], "-DryRun 不许留下任何文件")

    def test_redirected_profile_falls_back_to_the_roaming_startup_folder(self):
        """USERPROFILE 被重定向时 GetFolderPath('Startup') 返回空串（2026-10-19 冒烟逮住）。

        这时必须退到 ``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``，
        而不是拿空路径去 Join-Path / Test-Path（那会让状态对象整个炸掉）。
        """
        fake_home = self.work / "fake-home"
        fake_roaming = self.work / "fake-roaming"
        fake_home.mkdir()
        (fake_roaming / "Microsoft" / "Windows" / "Start Menu" /
         "Programs" / "Startup").mkdir(parents=True)
        data = self._run([
            "$a = Get-EchoStartupShortcutState",
            "$result = [pscustomobject]@{ needed=$a.Needed; reason=$a.Reason; "
            "link=$a.Spec.LinkPath; vbs=$a.Spec.VbsPath }",
        ], env_extra={"USERPROFILE": str(fake_home),
                      "APPDATA": str(fake_roaming)})
        expected = (fake_roaming / "Microsoft" / "Windows" / "Start Menu" /
                    "Programs" / "Startup" / "ECHO startup.lnk")
        self.assertTrue(data["needed"])
        self.assertEqual(data["reason"], "missing")
        self.assertTrue(_same_path(data["link"], str(expected)),
                        "应落到重定向后的 APPDATA Startup：%r" % data["link"])
        self.assertTrue(_same_path(
            data["vbs"], str(fake_home / ".echo-switch" / "echo-switch-startup.vbs")))


if __name__ == "__main__":
    unittest.main()
