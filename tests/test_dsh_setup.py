# -*- coding: utf-8 -*-
"""DSH 首次向导的检测/安装状态机（2026-11-03 一套 DSH）。

这里不碰真实下载：`_run_script` 被替身接管，只验证
  * `-NoDownload` 不能把"目录在"当成"已安装"；
  * 安装脚本没返回 CLI 时状态是 failed，不是 done；
  * 装好之后写的是 agentBackend=dsh / dshAutoStart=true，并且服务没在线时不能报成功。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import dsh_setup                                      # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-dsh-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.script = os.path.join(self.tmp, ".dsh", "skills", "echo-install", "scripts",
                                   "harness-install-desktop.ps1")
        os.makedirs(os.path.dirname(self.script), exist_ok=True)
        from pathlib import Path
        Path(self.script).write_text("", encoding="utf-8")
        patches = [
            patch.object(dsh_setup.paths, "echo_root", lambda: self.tmp),
            patch.object(dsh_setup.paths, "data_root", lambda: os.path.join(self.tmp, "data")),
            patch.object(dsh_setup.echo_platform, "current", lambda: "win32"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with dsh_setup._LOCK:
            dsh_setup._JOB.update({
                "state": "idle", "phase": "", "message": "", "detail": "",
                "installed": False, "ready": False, "cli": "", "version": "",
                "error": "", "startedAt": "", "finishedAt": "", "log": "",
            })
        dsh_setup._VERSION_CACHE.clear()


class ParseOutputTests(_Base):
    def test_command_file_wins_over_stdout(self):
        command_file = os.path.join(self.tmp, "cmd.txt")
        version_file = command_file + ".version"
        from pathlib import Path
        Path(command_file).write_text('"C:\\逻辑 路径\\dsh.cmd" web', encoding="utf-8")
        Path(version_file).write_text("0.2.1-rc.5", encoding="utf-8")
        got = dsh_setup._parse_output(
            'HARNESS_COMMAND="C:\\wrong\\dsh.cmd" web\nDSH_VERSION=9.9.9\n',
            command_file, version_file)
        self.assertIn("逻辑", got["command"])
        self.assertEqual(got["version"], "0.2.1-rc.5")

    def test_stdout_is_the_fallback(self):
        got = dsh_setup._parse_output(
            'HARNESS_COMMAND="C:\\x\\dsh.cmd" web\nDSH_VERSION=0.2.1\n',
            os.path.join(self.tmp, "missing.txt"),
            os.path.join(self.tmp, "missing.txt"))
        self.assertTrue(got["command"].endswith("web"))
        self.assertEqual(got["version"], "0.2.1")


class StatusTests(_Base):
    def test_missing_cli_is_not_installed(self):
        with patch("app.harness_proc.find_desktop_cli", return_value=""), \
                patch.object(dsh_setup, "_online_and_desktop", return_value=(False, False, "")):
            got = dsh_setup.status()
        self.assertTrue(got["supported"])
        self.assertFalse(got["installed"])
        self.assertFalse(got["ready"])
        self.assertIn("还没检测到", got["message"])

    def test_cli_that_cannot_run_version_is_not_installed(self):
        with patch("app.harness_proc.find_desktop_cli", return_value=r"C:\fake\dsh.cmd"), \
                patch.object(dsh_setup, "_cli_version", return_value=""), \
                patch.object(dsh_setup, "_online_and_desktop", return_value=(False, False, "")):
            got = dsh_setup.status()
        self.assertFalse(got["installed"], "目录/文件存在不算已安装")
        self.assertIn("--version", got["message"])


class DetectTests(_Base):
    def test_failed_detection_is_reported_honestly(self):
        proc = subprocess.CompletedProcess([], 1, stdout="", stderr="not installed\n")
        with patch.object(dsh_setup, "_run_script", return_value=proc), \
                patch.object(dsh_setup, "_parse_output", return_value={"command": "", "version": ""}), \
                patch.object(dsh_setup, "_tail", return_value="detail"):
            got = dsh_setup.detect()
        self.assertFalse(got["ok"])
        self.assertIn("未检测到", got["error"])

    def test_detection_returns_the_real_command(self):
        proc = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        parsed = {"command": '"C:\\Program Files\\DeepSeek Harness\\dsh.cmd" web',
                  "version": "0.2.1-rc.5", "installDir": ""}
        with patch.object(dsh_setup, "_run_script", return_value=proc), \
                patch.object(dsh_setup, "_parse_output", return_value=parsed):
            got = dsh_setup.detect()
        self.assertTrue(got["ok"])
        self.assertEqual(got["version"], "0.2.1-rc.5")
        self.assertTrue(got["installed"])


class InstallWorkerTests(_Base):
    def test_no_command_from_installer_marks_failed(self):
        proc = subprocess.CompletedProcess([], 1, stdout="", stderr="download failed\n")
        with patch.object(dsh_setup, "_run_script", return_value=proc), \
                patch.object(dsh_setup, "_parse_output", return_value={"command": "", "version": ""}), \
                patch.object(dsh_setup, "_tail", return_value="detail"):
            dsh_setup._install_worker()
        job = dsh_setup.state()
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["installed"])
        self.assertIn("DSH 安装失败", job["error"])

    def test_installed_but_offline_is_not_success(self):
        proc = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        parsed = {"command": '"C:\\dsh.cmd" web', "version": "0.2.1", "installDir": ""}
        fake_harness = type("H", (), {
            "ensure_running": staticmethod(lambda: (False, "not ready")),
            "online": staticmethod(lambda timeout=0.8: False),
            "desktop_mode": staticmethod(lambda timeout=0.8: False),
        })()
        with patch.object(dsh_setup, "_run_script", return_value=proc), \
                patch.object(dsh_setup, "_parse_output", return_value=parsed), \
                patch.object(dsh_setup, "_harness", return_value=fake_harness), \
                patch.object(dsh_setup, "settings") as fake_settings, \
                patch("app.settings_effects.apply", return_value=[
                    {"scope": "agent", "ok": False, "detail": "dsh 没起来"}]), \
                patch.object(dsh_setup, "_online_and_desktop", return_value=(False, False, "")):
            fake_settings.update.return_value = ["agentBackend", "dshAutoStart"]
            dsh_setup._install_worker()
        job = dsh_setup.state()
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["installed"], "程序确实装上了")
        self.assertFalse(job["ready"], "服务没在线就不能说 ready")

    def test_installed_and_online_is_success(self):
        proc = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        parsed = {"command": '"C:\\dsh.cmd" web', "version": "0.2.1", "installDir": ""}
        fake_harness = type("H", (), {
            "ensure_running": staticmethod(lambda: (True, "dsh online")),
            "online": staticmethod(lambda timeout=0.8: True),
            "desktop_mode": staticmethod(lambda timeout=0.8: False),
        })()
        with patch.object(dsh_setup, "_run_script", return_value=proc), \
                patch.object(dsh_setup, "_parse_output", return_value=parsed), \
                patch.object(dsh_setup, "_harness", return_value=fake_harness), \
                patch.object(dsh_setup, "settings") as fake_settings, \
                patch("app.settings_effects.apply", return_value=[
                    {"scope": "agent", "ok": True, "detail": "dsh online"}]), \
                patch.object(dsh_setup, "_online_and_desktop", return_value=(True, False, "")):
            fake_settings.update.return_value = ["agentBackend", "dshAutoStart"]
            dsh_setup._install_worker()
        job = dsh_setup.state()
        self.assertEqual(job["state"], "done")
        self.assertTrue(job["installed"])
        self.assertTrue(job["ready"])
        fake_settings.update.assert_called_once()
        values = fake_settings.update.call_args[0][0]
        self.assertEqual(values["agentBackend"], "dsh")
        self.assertTrue(values["dshAutoStart"])
        self.assertNotIn("harnessCommand", values)
        self.assertNotIn("agentHarnessEnabled", values)


if __name__ == "__main__":
    unittest.main()
