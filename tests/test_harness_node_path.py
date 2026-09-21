# -*- coding: utf-8 -*-
"""``harness_proc`` 的 node 探测与 PATH 注入（2026-09-22 同事反馈 B1）。

症状：机器上只有"托管式" node（WorkBuddy 装在自家 binaries 下、不写系统 PATH），
双击桌面快捷方式启动 ECHO 后 harness 起不来，`/api/boot/status` 里显示 failed：
「找不到 npx」。根因是 ``shutil.which`` 读的是**本进程**的 ``os.environ``。

这些用例把"探测 → 注入 → 解析出绝对路径"这条链钉住。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import platform as echo_platform
from app import harness_proc


class _TempNodeMixin(unittest.TestCase):

    def setUp(self):
        self._saved_path = os.environ.get("PATH", "")
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._saved_path))
        self.tmp = tempfile.mkdtemp(prefix="echo-node-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def make_node_dir(self, name="versions/22.22.2-3"):
        d = os.path.join(self.tmp, *name.split("/"))
        os.makedirs(d, exist_ok=True)
        # Windows 上 which() 靠 PATHEXT 找到 npx.cmd；两种都放，跨平台都稳
        for exe in ("npx.cmd", "npx.exe", "npx"):
            p = os.path.join(d, exe)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("@echo off\n")
        return d


class FindNodeDirTests(_TempNodeMixin):

    def test_returns_empty_when_platform_reports_nothing(self):
        with patch.object(echo_platform, "node_dirs", lambda: []):
            self.assertEqual("", harness_proc.find_node_dir())

    def test_survives_a_broken_platform_seam(self):
        def boom():
            raise RuntimeError("no seam")
        with patch.object(echo_platform, "node_dirs", boom):
            self.assertEqual("", harness_proc.find_node_dir())

    def test_finds_the_dir_that_actually_has_npx(self):
        node_dir = self.make_node_dir()
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        with patch.object(echo_platform, "node_dirs", lambda: [empty, node_dir]):
            self.assertEqual(node_dir, harness_proc.find_node_dir(),
                             "要跳过没有 npx 的目录，返回真正有它的那个")


class EnsureNodeOnPathTests(_TempNodeMixin):

    def test_prepends_and_is_idempotent(self):
        node_dir = self.make_node_dir()
        with patch.object(echo_platform, "node_dirs", lambda: [node_dir]):
            self.assertEqual(node_dir, harness_proc.ensure_node_on_path())
            self.assertTrue(os.environ["PATH"].startswith(node_dir + os.pathsep))
            first = os.environ["PATH"]
            self.assertEqual(node_dir, harness_proc.ensure_node_on_path())
            self.assertEqual(first, os.environ["PATH"], "第二次不该重复插入")

    def test_noop_when_node_is_missing(self):
        with patch.object(echo_platform, "node_dirs", lambda: []):
            before = os.environ.get("PATH", "")
            self.assertEqual("", harness_proc.ensure_node_on_path())
            self.assertEqual(before, os.environ.get("PATH"), "找不到就不该动 PATH")

    def test_argv_then_resolves_npx_to_an_absolute_path(self):
        """这是最终目的：注入之后 `_argv()` 能把 npx 解析成真路径（否则 Popen 直接失败）。"""
        node_dir = self.make_node_dir()
        os.environ["PATH"] = ""                      # 模拟"PATH 里没有 node"
        with patch.object(echo_platform, "node_dirs", lambda: [node_dir]), \
                patch.object(harness_proc, "command",
                             lambda: "npx -y @deepseek-ai/dsh web"):
            harness_proc.ensure_node_on_path()
            argv = harness_proc._argv()
        self.assertTrue(os.path.isabs(argv[0]), "应当是绝对路径，而不是裸 npx：%r" % argv[0])
        self.assertEqual(node_dir, os.path.dirname(argv[0]))
        self.assertEqual(["-y", "@deepseek-ai/dsh", "web"], argv[1:])


class PlatformSeamTests(unittest.TestCase):

    def test_node_dirs_is_a_list(self):
        dirs = echo_platform.node_dirs()
        self.assertIsInstance(dirs, list, "接缝必须返回列表（缺失实现时为空）")
        for d in dirs:
            self.assertIsInstance(d, str)
            self.assertTrue(d)

    def test_missing_implementation_returns_empty(self):
        with patch.object(echo_platform, "_platform_fn", lambda name: None):
            self.assertEqual([], echo_platform.node_dirs())


if __name__ == "__main__":
    unittest.main()
