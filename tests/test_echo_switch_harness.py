# -*- coding: utf-8 -*-
"""切换实例时，harness 必须跟着**它自己的那棵树**走（2026-10-31 真机 bug）。

3.0 布局里代码根是 ``<安装根>\\echo-core``，而 harness 在上一级
``<安装根>\\dsh\\app``。切换脚本原来只拿代码根去匹配进程命令行，于是
stable 的 harness 永远匹配不上：切到 dev 后它继续占着 43199，dev 启动时
如实报「另一棵树的 harness」，但旧进程没人收，最后 dev 又复用了 stable
的 DSH_HOME（会话/工作区 id 全对不上）。

这里不启动任何进程，只把假的进程对象喂给 `Get-EchoInstanceProcs`，
钉住「代码根 + data root 都能认出 harness」。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "echo-instance-lib.ps1"


def _powershell() -> str:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return ""


@unittest.skipUnless(os.name == "nt", "Windows process/path matching only")
class SwitchHarnessOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ps = _powershell()
        if not cls.ps:
            raise unittest.SkipTest("找不到 PowerShell")
        if not LIB.is_file():
            raise unittest.SkipTest("缺 echo-instance-lib.ps1: %s" % LIB)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="echo-switch-"))
        # 3.0 layout: code root is <install>\echo-core, data root is <install>.
        self.install = self.tmp / "ECHO"
        self.code = self.install / "echo-core"
        (self.code / "scripts").mkdir(parents=True)
        (self.install / "dsh" / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _classify(self, root: Path, cmdline: str):
        script = (
            ". '%s'; "
            "$p=[pscustomobject]@{ProcessId=4242;Name='node.exe';ParentProcessId=1;"
            "CommandLine='%s';ExecutablePath='C:\\node.exe'}; "
            "Get-EchoInstanceProcs '%s' @($p) | ConvertTo-Json -Compress"
            % (LIB, cmdline, root)
        )
        proc = subprocess.run([self.ps, "-NoProfile", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        self.assertEqual(0, proc.returncode, proc.stderr)
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
        self.assertTrue(lines, "没有输出 JSON：\n%s\n%s" % (proc.stdout, proc.stderr))
        data = json.loads(lines[-1])
        return data if isinstance(data, list) else [data]

    def test_stable_harness_is_matched_through_the_data_root(self):
        cmdline = str(self.install / "dsh" / "app" / "node_modules" /
                      "@deepseek-ai" / "dsh" / "lib" / "bin.js") + " web --port 43199"
        hits = self._classify(self.code, cmdline)
        kinds = [(h.get("Kind"), h.get("PID")) for h in hits]
        self.assertIn(("harness", 4242), kinds, kinds)

    def test_dev_harness_still_matched_through_the_code_root(self):
        cmdline = str(self.install / "harness" / "dsh" / "node_modules" /
                      "@deepseek-ai" / "dsh" / "lib" / "bin.js") + " web"
        hits = self._classify(self.code, cmdline)
        kinds = [(h.get("Kind"), h.get("PID")) for h in hits]
        self.assertIn(("harness", 4242), kinds, kinds)


if __name__ == "__main__":
    unittest.main()
