# -*- coding: utf-8 -*-
"""安装/停止链路的 2026-10-31 回归：桌面启停脚本 + 关闭不被 watchdog 拉起。

三件事都是真机反馈：
  1. 安装包要在用户桌面提供「启动 ECHO.cmd / 停止 ECHO.cmd」；
  2. 停止 ECHO 时必须先停 watchdog / switch supervisor，否则关完几秒又被拉起；
  3. 边条 ⏻ 走 `manager.echo_stop_self()`，它必须安排完整停止，而不是只退出自己。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP_HELPER = ROOT / "scripts" / "install-desktop-shortcut.ps1"
STOP = ROOT / "scripts" / "stop.ps1"
RESTART = ROOT / "scripts" / "restart-echo.ps1"
RUNTIME = ROOT / "app" / "runtime.py"
MANAGER = ROOT / "app" / "manager.py"


def _powershell() -> str:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return ""


@unittest.skipUnless(os.name == "nt", "Windows desktop/process semantics only")
class DesktopStartStopScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ps = _powershell()
        if not cls.ps:
            raise unittest.SkipTest("找不到 PowerShell")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-desktop-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra: str):
        proc = subprocess.run(
            [self.ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DESKTOP_HELPER),
             "-DesktopDir", str(self.tmp), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc

    def test_start_and_stop_cmd_are_created(self):
        self._run()
        start = self.tmp / "启动 ECHO.cmd"
        stop = self.tmp / "停止 ECHO.cmd"
        self.assertTrue(start.is_file(), sorted(p.name for p in self.tmp.iterdir()))
        self.assertTrue(stop.is_file(), sorted(p.name for p in self.tmp.iterdir()))
        self.assertIn("start.ps1", start.read_text(encoding="ascii"))
        self.assertIn("-Background", start.read_text(encoding="ascii"))
        self.assertIn("stop.ps1", stop.read_text(encoding="ascii"))

    def test_start_stop_only_does_not_touch_the_shortcut(self):
        self._run("-StartStopOnly")
        self.assertTrue((self.tmp / "启动 ECHO.cmd").is_file())
        self.assertTrue((self.tmp / "停止 ECHO.cmd").is_file())
        self.assertFalse((self.tmp / "ECHO 个人助理.lnk").exists(),
                         "升级模式不该覆盖用户的桌面快捷方式")

    def test_install_all_refreshes_desktop_start_stop_on_upgrade(self):
        """升级路径会跳过快捷方式/自启，但仍必须补桌面启停脚本。"""
        text = (ROOT / "scripts" / "install-all.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("install-desktop-shortcut.ps1", text)
        self.assertIn("-StartStopOnly", text)
        self.assertIn("$NoShortcuts", text)

    def test_remove_takes_all_three_entries_away(self):
        self._run()
        self._run("-Remove")
        self.assertFalse((self.tmp / "启动 ECHO.cmd").exists())
        self.assertFalse((self.tmp / "停止 ECHO.cmd").exists())
        self.assertFalse((self.tmp / "ECHO 个人助理.lnk").exists())


class StopSupervisorWiringTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows subprocess flags only")
    def test_schedule_full_stop_is_console_safe_and_can_keep_backend(self):
        """PowerShell 不能用 DETACHED_PROCESS（WinError 786）；升级要能 -KeepBackend。"""
        import subprocess
        from unittest.mock import patch
        from app import runtime

        with patch.object(runtime.subprocess, "Popen") as popen:
            self.assertTrue(runtime.schedule_full_stop(keep_backend=True))
        args, kwargs = popen.call_args
        cmd = args[0]
        self.assertTrue(any("stop.ps1" in str(x) for x in cmd), cmd)
        self.assertIn("-Quiet", cmd)
        self.assertIn("-KeepBackend", cmd)
        flags = kwargs.get("creationflags", 0)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & subprocess.DETACHED_PROCESS,
                         "DETACHED_PROCESS 会让 powershell 被策略拦掉（WinError 786）")

    def test_stop_leaves_supervisor_alone_only_when_asked(self):
        text = STOP.read_text(encoding="utf-8")
        self.assertIn("[switch]$KeepSupervisor", text)
        self.assertIn("function Stop-EchoSupervisors", text)
        self.assertIn("echo-supervisor.ps1", text)
        self.assertIn("startup.ps1", text)
        self.assertIn("Stop-EchoSupervisors", text)

    def test_restart_keeps_the_supervisor(self):
        text = RESTART.read_text(encoding="utf-8")
        self.assertIn("-KeepSupervisor", text)

    def test_sidebar_close_path_schedules_a_full_stop(self):
        runtime = RUNTIME.read_text(encoding="utf-8")
        manager = MANAGER.read_text(encoding="utf-8")
        self.assertIn("def schedule_full_stop", runtime)
        self.assertIn("stop.ps1", runtime)
        self.assertIn("schedule_full_stop(", manager)
        # 完整停止必须先于 os._exit（否则脚本还没起来 ECHO 就死了）
        self.assertLess(manager.index("schedule_full_stop("), manager.index("os._exit(0)"))


if __name__ == "__main__":
    unittest.main()
