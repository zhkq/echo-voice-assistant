# -*- coding: utf-8 -*-
"""``app/backend_pid.py`` 的用例。

卫生规则（照 ``tests/test_harness_agent.py`` 的套路）
----------------------------------------------------
``backend_pid`` 的 pid 路径走 ``paths.data_root()``，**不受**测试里 patch 的
``db.DATA_DIR`` 约束 —— 所以必须显式把 ``backend_pid._logs_dir`` 指到
``tempfile.mkdtemp()``，否则用例会去读/删**真实**的 ``data/logs/backend.pid``
（2026-09-22 harness 那场事故正是这么来的：单测杀了开发者正在跑的标准版服务）。
本文件**绝不**碰真实 ``data/**``，并有护栏用例钉住这一点。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from app import backend_pid

_TMP_DIR = tempfile.mkdtemp(prefix="echo-backend-pid-test-")
_OLD_LOGS_DIR = backend_pid._logs_dir
#: 打桩**之前**的真实路径：只用来比对"有没有被动过"，不读不写内容以外的东西
_REAL_PID_PATH = backend_pid.pid_path()


def _real_pid_bytes():
    """真实 pid 文件的字节（不存在 -> None）。只读，用来断言它没被改动。"""
    try:
        with open(_REAL_PID_PATH, "rb") as fh:
            return fh.read()
    except Exception:
        return None


def setUpModule():
    backend_pid._logs_dir = lambda: _TMP_DIR


def tearDownModule():
    backend_pid._logs_dir = _OLD_LOGS_DIR
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


def _write_pid(raw):
    with open(backend_pid.pid_path(), "w", encoding="utf-8") as fh:
        fh.write(raw)


class IsolationTests(unittest.TestCase):
    def test_pid_path_is_isolated_from_the_real_one(self):
        """跑测试不会动真实 ``data/logs/backend.pid``（护栏）。"""
        isolated = backend_pid.pid_path()
        self.assertTrue(isolated.startswith(_TMP_DIR), isolated)
        self.assertNotEqual(isolated, _REAL_PID_PATH)
        self.assertFalse(_REAL_PID_PATH.startswith(_TMP_DIR))
        log, err = backend_pid.log_paths()
        self.assertTrue(log.startswith(_TMP_DIR), log)
        self.assertTrue(err.startswith(_TMP_DIR), err)
        self.assertNotEqual(log, err)
        for p in (log, err):
            self.assertFalse(os.path.exists(p), "log_paths() 只该给路径，不许建文件：%s" % p)

        # 真跑一遍读接口，真实的 pid 文件必须一个字节都没变
        before = _real_pid_bytes()
        _write_pid("999999")
        backend_pid.read_pid()
        backend_pid.is_ours_alive()
        self.assertEqual(_real_pid_bytes(), before)


class ReadPidTests(unittest.TestCase):
    def test_missing_or_garbage_pid_file_is_not_fatal(self):
        """文件不存在 / ``abc`` / 空文件 -> ``read_pid()`` 返回 None，且不抛。"""
        path = backend_pid.pid_path()
        if os.path.exists(path):
            os.remove(path)
        self.assertIsNone(backend_pid.read_pid())          # 不存在
        for raw in ("abc", "", "   \n", "12abc", "1.5", "-1", "0"):
            with self.subTest(raw=raw):
                _write_pid(raw)
                self.assertIsNone(backend_pid.read_pid())
        with self.subTest(raw="1234"):
            _write_pid("1234")
            self.assertEqual(backend_pid.read_pid(), 1234)  # 正常值仍读得出来


class StalePidTests(unittest.TestCase):
    def test_a_stale_pid_is_cleaned_and_reported(self):
        """99% 不存在的 pid：判为"不是我们的"，且顺手清掉陈旧 pid 文件并在 note 里说明。"""
        path = backend_pid.pid_path()
        _write_pid("999999")
        self.assertEqual(backend_pid.read_pid(), 999999)
        alive, note = backend_pid.is_ours_alive()
        self.assertFalse(alive, note)
        self.assertIn("陈旧", note)
        self.assertIn("已清掉", note)
        self.assertFalse(os.path.exists(path), note)


class LivePidTests(unittest.TestCase):
    def _kill(self, proc):
        for step in (proc.terminate, proc.kill):
            try:
                if proc.poll() is None:
                    step()
                    proc.wait(timeout=5)
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def test_a_live_pid_is_reported_alive(self):
        """真起一个进程，把它的 pid 写进文件 -> ``is_ours_alive()`` 为 True 且文件留着。"""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self._kill, proc)
        self.assertIsNone(proc.poll(), "子进程没起来，用例没法判活")
        path = backend_pid.pid_path()
        _write_pid(str(proc.pid))
        alive, note = backend_pid.is_ours_alive()
        self.assertTrue(alive, note)
        self.assertIn(str(proc.pid), note)
        self.assertTrue(os.path.exists(path), "活着的 pid 记录不该被清掉")
        # 探活**只能看**：不许把被探的进程打断/杀掉
        self.assertIsNone(proc.poll(), "探活把被测进程弄死了：%s" % note)


if __name__ == "__main__":
    unittest.main()
