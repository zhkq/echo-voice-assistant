# -*- coding: utf-8 -*-
"""临时目录收尾机制的守卫（``tests/__init__.py::sweep``）。

为什么值得单独钉住：这个机制**平时看不见** —— 它只在进程退出时生效，所以
"前缀写错""把只删新增的改成全删""顺手把文件也删了"这类改动，本地跑一遍测试是看不出来的：
要么某天才发现 TEMP 里又积了上千个目录，要么反过来 —— 把**运行前就存在**的目录
（可能是别的进程正在用的）删掉。所以这里直接验证契约本身。

2026-09-21 背景：TEMP 里实测积了 1120 个 ``echo-*`` 目录，全部来自用例的
``tempfile.mkdtemp`` 没有清理。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tests as tests_pkg


class SweepContractTests(unittest.TestCase):
    """`sweep` 的三条边界：删新增的、不删既有的、不碰文件。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="echo-sweeptest-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _mkdir(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        return path

    def test_removes_new_echo_dirs_only(self):
        old = self._mkdir("echo-old")
        new = self._mkdir("echo-new")
        unrelated = self._mkdir("keepme")

        removed = tests_pkg.sweep(self.root, {old})

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(new), "本次新增的 echo- 目录应当被删掉")
        self.assertTrue(os.path.isdir(old), "运行前就存在的目录不能删")
        self.assertTrue(os.path.isdir(unrelated), "非 echo- 前缀的目录不能删")

    def test_files_are_never_removed(self):
        """只删目录：文件可能属于同时跑着的别的 ECHO 进程（如 echo-tts-<pid>.mp3）。"""
        path = os.path.join(self.root, "echo-somefile.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")

        removed = tests_pkg.sweep(self.root, set())

        self.assertEqual(removed, 0)
        self.assertTrue(os.path.isfile(path), "文件不在清理范围内")

    def test_snapshot_lists_matching_dirs_only(self):
        matched = self._mkdir("echo-yes")
        plain = self._mkdir("plain")

        snap = tests_pkg._snapshot(self.root)

        self.assertIn(matched, snap)
        self.assertNotIn(plain, snap)


class AtexitWiringTests(unittest.TestCase):
    """机制真的挂在退出钩子上 —— 光测 `sweep` 本身测不出这一点。"""

    def test_a_leaked_dir_is_gone_after_the_process_exits(self):
        code = (
            "import tempfile, tests;"
            "print(tempfile.mkdtemp(prefix='echo-atexit-'))"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, "-c", code], cwd=root,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        leaked = proc.stdout.strip().splitlines()[-1]
        self.addCleanup(shutil.rmtree, leaked, ignore_errors=True)
        self.assertTrue(leaked.replace("\\", "/").split("/")[-1].startswith("echo-atexit-"),
                        f"子进程没按预期输出临时目录: {leaked!r}")
        self.assertFalse(os.path.exists(leaked),
                         "子进程退出后，它漏出来的 echo- 目录应当已被收尾")


if __name__ == "__main__":
    unittest.main()
