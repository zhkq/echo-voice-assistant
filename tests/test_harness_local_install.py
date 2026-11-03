# -*- coding: utf-8 -*-
"""B5 的产品侧兜底 + 排障可读性（同事 2026-09-22 反馈）。

两块：

1. **本地永久安装优先**：安装技能把 `@deepseek-ai/dsh` 装到 `<安装目录>/harness/dsh`，
   而设置里可能还留着出厂的 `npx -y …`（升级上来的机器、或技能没写成功）。
   `harness_proc.command()` 在"用户没自己改过命令"时自动改走本地入口 ——
   npx 冷启动 2 分 10 秒 vs 本地 9 秒。**用户写过的命令一个字都不许动**。
2. **`harness.log` 每次启动写分隔行**：日志是追加模式，没有分隔时多段 traceback 堆在一起，
   看着像"同一个进程反复重启"。
"""

import io
import os
import tempfile
import types
import unittest
from unittest.mock import patch

import app.harness_proc as hp


def _make_tree(root, rel, content=b"// entry\n"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(content)
    return p


class LocalEntryTests(unittest.TestCase):
    REL = hp.LOCAL_ENTRY_REL

    def test_detects_installed_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            want = _make_tree(tmp, self.REL)
            with patch.object(hp.paths, "echo_root", lambda: tmp):
                self.assertEqual(hp.local_entry(), want)

    def test_empty_entry_is_ignored(self):
        """同事踩过"目录在、文件被截断"：空文件不能当成装好了。"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, self.REL, b"")
            with patch.object(hp.paths, "echo_root", lambda: tmp):
                self.assertEqual(hp.local_entry(), "")

    def test_missing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hp.paths, "echo_root", lambda: tmp):
                self.assertEqual(hp.local_entry(), "")

    def test_the_mac_installers_layout_is_found_too(self):
        """3.0 布局 + 技能把本体装在 `<根>/harness/dsh`（macOS 那份 .sh 的旧落点）也要认。

        不认的后果不是"报错"而是**静默变慢**：装好的入口找不到 → 退回 npx 冷启动 1-2 分钟。
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "ECHO")
            code = os.path.join(base, "echo-core")
            os.makedirs(code)
            installed = _make_tree(base, self.REL)          # <根>/harness/dsh/...
            with patch.object(hp.paths, "echo_root", lambda: code), \
                 patch.object(hp.paths, "echo_base", lambda: base), \
                 patch.object(hp.paths, "dsh_root",
                              lambda: os.path.join(base, "dsh", "app")):
                self.assertEqual(hp.local_entry(), installed)

    def test_the_new_layout_wins_when_both_exist(self):
        """`<根>/dsh/app` 与 `<根>/harness/dsh` 都在时，先用新布局那份（升级上来的机器）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "ECHO")
            code = os.path.join(base, "echo-core")
            os.makedirs(code)
            want = _make_tree(base, hp.LOCAL_ENTRY_REL_BASE)   # <根>/dsh/app/...
            _make_tree(base, self.REL)                          # <根>/harness/dsh/...
            with patch.object(hp.paths, "echo_root", lambda: code), \
                 patch.object(hp.paths, "echo_base", lambda: base), \
                 patch.object(hp.paths, "dsh_root",
                              lambda: os.path.join(base, "dsh", "app")):
                self.assertEqual(hp.local_entry(), want)


class CommandSelectionTests(unittest.TestCase):
    REL = hp.LOCAL_ENTRY_REL

    def _cmd(self, setting_value, root, node="/usr/bin/node", desktop_cli=""):
        with patch.object(hp.paths, "echo_root", lambda: root), \
             patch.object(hp.shutil, "which", lambda n: node if n == "node" else None), \
             patch.object(hp, "find_node_dir", lambda: ""), \
             patch.object(hp, "find_desktop_cli", lambda: desktop_cli), \
             patch.object(hp.settings, "get", lambda k, d=None: setting_value if k == "harnessCommand" else d):
            return hp.command()

    def test_the_official_desktop_cli_wins(self):
        """2026-11-03 一套 DSH：程序来源是官方桌面端自带的 dsh（升级跟着桌面端）。"""
        with tempfile.TemporaryDirectory() as tmp:
            entry = _make_tree(tmp, self.REL)          # 本地那份也在，但桌面端优先
            got = self._cmd(hp.DEFAULT_COMMAND, tmp,
                            desktop_cli=r"C:\App\resources\runtime\cli\bin\dsh.cmd")
            self.assertIn("dsh.cmd", got)
            self.assertIn("web", got)
            self.assertNotIn(entry, got)

    def test_default_npx_switches_to_local_when_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(hp.is_default_command("npx -y @deepseek-ai/dsh web --port 1"))
            entry = _make_tree(tmp, self.REL)
            got = self._cmd(hp.DEFAULT_COMMAND, tmp)
            self.assertEqual(got, '"%s" "%s" web' % ("/usr/bin/node", entry))

    def test_no_local_entry_keeps_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._cmd(hp.DEFAULT_COMMAND, tmp), hp.DEFAULT_COMMAND)

    def test_custom_command_is_never_overridden(self):
        """用户自己写的命令（哪怕也提到 npx）一律不动 —— 这是他明确表态过的选择。"""
        custom = "npx -y @deepseek-ai/dsh web --port 43222 --no-open"
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, self.REL)
            self.assertEqual(self._cmd(custom, tmp), custom)

    def test_no_node_keeps_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, self.REL)
            self.assertEqual(self._cmd(hp.DEFAULT_COMMAND, tmp, node=None), hp.DEFAULT_COMMAND)

    def test_default_recognition(self):
        self.assertTrue(hp.is_default_command(""))
        self.assertTrue(hp.is_default_command(None))
        self.assertTrue(hp.is_default_command(hp.DEFAULT_COMMAND))
        self.assertTrue(hp.is_default_command("  npx -y @deepseek-ai/dsh web  "))
        self.assertFalse(hp.is_default_command("npx -y @deepseek-ai/dsh web --port 1"))


class LogSeparatorTests(unittest.TestCase):
    def test_each_start_writes_a_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = types.SimpleNamespace(pid=4321, stdout=io.BytesIO(b""))
            with patch.object(hp.paths, "data_root", lambda: tmp):
                hp._read_output(proc)
                hp._read_output(proc)
            log = os.path.join(tmp, "logs", "harness.log")
            with open(log, encoding="utf-8") as f:
                text = f.read()
        self.assertEqual(text.count("===== harness 启动"), 2)   # 两次启动 = 两条分隔
        self.assertIn("pid=4321", text)


if __name__ == "__main__":
    unittest.main()
