# -*- coding: utf-8 -*-
"""单实例锁测试（app/single_instance.py）。

背景：重复实例曾以"活着但不监听"的形态留在机器上（2026-09-18 实测 18060 上
有一个在服务、另一个不监听却仍在后台加载 SenseVoice）。根因是用端口探测当
权威判定 —— 探测与 uvicorn 真正 bind 之间有竞态窗口。改成内核级锁之后，
这里把它的语义钉住：

  1. 同一把锁第二次获取必须失败（同进程、以及**跨进程**）；
  2. 释放后可以重新获取；
  3. 不同名字 / 不同数据目录互相隔离（不同端口的两个 ECHO 允许并存）。

用临时目录 + 随机锁名，绝不碰真实运行中的 ECHO 的锁。
"""
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

from app import single_instance as si

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SingleInstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-lock-")
        self.name = "test-%s" % uuid.uuid4().hex[:8]

    def tearDown(self):
        si.release(self.name)
        si.release(self.name + "-b")

    def test_second_acquire_fails_then_succeeds_after_release(self):
        ok, detail = si.acquire(self.name, self.tmp)
        self.assertTrue(ok, f"首次获取应成功：{detail}")

        ok2, detail2 = si.acquire(self.name, self.tmp)
        self.assertFalse(ok2, "同一把锁第二次获取必须失败（否则挡不住重复实例）")
        self.assertIn("已", detail2)

        self.assertTrue(si.release(self.name))
        ok3, detail3 = si.acquire(self.name, self.tmp)
        self.assertTrue(ok3, f"释放后应能重新获取：{detail3}")

    def test_lock_is_visible_to_other_process(self):
        """真正的跨进程断言：父进程持锁时，子进程必须拿不到。"""
        ok, _ = si.acquire(self.name, self.tmp)
        self.assertTrue(ok)
        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "from app.single_instance import acquire;"
            "ok, detail = acquire(r'%s', r'%s');"
            "print('OK' if ok else 'BUSY')" % (ROOT, self.name, self.tmp)
        )
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=60,
                              cwd=ROOT, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"子进程异常：{proc.stderr[:400]}")
        self.assertIn("BUSY", proc.stdout,
                      "父进程持锁时子进程竟然拿到了锁 —— 单实例保护失效")

    def test_is_held_reflects_state(self):
        self.assertFalse(si.is_held(self.name, self.tmp), "未获取时不应报告已持有")
        si.acquire(self.name, self.tmp)
        self.assertTrue(si.is_held(self.name, self.tmp), "获取后应报告已持有")
        si.release(self.name)
        self.assertFalse(si.is_held(self.name, self.tmp), "释放后不应再报告已持有")

    def test_different_names_and_dirs_are_independent(self):
        other = tempfile.mkdtemp(prefix="echo-lock2-")
        self.addCleanup(lambda: si.release(self.name + "-c"))
        ok_a, _ = si.acquire(self.name, self.tmp)
        ok_b, _ = si.acquire(self.name + "-b", self.tmp)      # 不同名字
        ok_c, _ = si.acquire(self.name + "-c", other)         # 不同数据目录
        self.assertTrue(ok_a)
        self.assertTrue(ok_b, "不同名字的锁应互相独立")
        self.assertTrue(ok_c, "不同数据目录应各有一把锁（不同端口的 ECHO 可并存）")


if __name__ == "__main__":
    unittest.main()
