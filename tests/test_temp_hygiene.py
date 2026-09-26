# -*- coding: utf-8 -*-
"""临时目录收尾机制的守卫（``tests/__init__.py``）。

为什么值得单独钉住：这个机制**平时看不见** —— 它只在进程退出时生效，所以
"前缀写错""把只删自己的改成全删""顺手把文件也删了"这类改动，本地跑一遍测试是看不出来的：
要么某天才发现 TEMP 里又积了上千个目录，要么反过来 —— 把**别的测试进程正在用**的目录
删掉（2026-09-26 实测踩到过：并发跑第二个测试进程时互相踩，症状是
``no such table: settings``，看着像代码坏了）。

所以现在的契约是**只清本进程登记过的路径**（`register_temp_dir` / `mkdtemp` 替身），
并且这里逐条钉住：只删登记过的、不删别人的、不删文件。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tests as tests_pkg


class OwnedSweepContractTests(unittest.TestCase):
    """`sweep` 的三条边界：删自己登记的、不碰别人的、不碰文件。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="echo-sweeptest-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _mkdir(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        return path

    def test_removes_only_registered_dirs(self):
        mine = self._mkdir("echo-mine")
        not_mine = self._mkdir("echo-not-mine")      # 别的进程建的（我们没登记）
        unrelated = self._mkdir("keepme")

        removed = tests_pkg.sweep({mine})

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(mine), "本进程登记的目录应当被删掉")
        self.assertTrue(os.path.isdir(not_mine),
                        "没登记过的目录一个都不许删 —— 那是别的测试进程正在用的")
        self.assertTrue(os.path.isdir(unrelated), "非 echo- 前缀的目录不在清理范围内")

    def test_another_processes_dir_survives_the_real_exit_hook(self):
        """真契约：**另一个进程**建的临时目录，本进程退出时不许动它。

        这条是那次事故的直接复现 —— 旧实现按"运行期间新增的 echo- 目录"扫，
        于是先退出的进程会把后一个进程的目录删掉。
        """
        survivor = self._mkdir("echo-other-process")
        code = ("import os, sys, tests;"
                "print(tests.sweep())")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run([sys.executable, "-c", code], cwd=root,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isdir(survivor),
                        "另一个测试进程退出时删掉了我建的目录（并发跑必炸）")

    def test_files_are_never_removed(self):
        """只删目录：文件可能属于同时跑着的别的 ECHO 进程（如 echo-tts-<pid>.mp3）。"""
        path = os.path.join(self.root, "echo-somefile.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")

        removed = tests_pkg.sweep({path})

        self.assertEqual(removed, 0)
        self.assertTrue(os.path.isfile(path), "文件不在清理范围内")

    def test_registration_accepts_only_real_dirs(self):
        """登记是幂等的、接受相对路径，并且 `owns()` 说的是实话。"""
        mine = self._mkdir("echo-reg")
        tests_pkg.register_temp_dir(mine)
        tests_pkg.register_temp_dir(mine)              # 幂等
        self.assertTrue(tests_pkg.owns(mine))
        self.assertFalse(tests_pkg.owns(os.path.join(self.root, "nope")))
        self.assertEqual(len([p for p in tests_pkg.tracked_dirs()
                              if p == os.path.normcase(os.path.abspath(mine))]), 1)
        # **只传自己这一份**：默认登记表里还躺着别的用例的目录，动它就是这次要防的互踩
        self.assertEqual(tests_pkg.sweep({mine}), 1)


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
                         "子进程退出后，它自己建（并登记）的 echo- 目录应当已被收尾")

    def test_the_mkdtemp_stand_in_is_installed_at_import(self):
        """用例照原样写 `tempfile.mkdtemp(...)` 就该被登记 —— 这是"不用改 50 处"的前提。"""
        path = tempfile.mkdtemp(prefix="echo-wrapped-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self.assertTrue(tests_pkg.owns(path),
                        "tempfile.mkdtemp 没有被替换成会登记的版本")


class UsageLedgerIsOffInTests(unittest.TestCase):
    """测试进程不许往真实库写模型使用记录（会污染用户的「清理」建议）。"""

    def test_usage_ledger_is_disabled(self):
        from app import model_usage
        self.assertFalse(model_usage.ENABLED,
                         "tests/__init__.py 应当在导入时关掉模型使用账本")
        self.assertFalse(model_usage.note_used("whisper-small"),
                         "关掉之后 note_used() 必须什么都不写")


if __name__ == "__main__":
    unittest.main()
