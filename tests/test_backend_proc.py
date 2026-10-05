# -*- coding: utf-8 -*-
"""``app/backend_proc.py``（批 1b：起 / 停 / 端口占用者）的用例。

卫生规则（照 ``tests/test_backend_pid.py`` 与 ``tests/test_harness_agent.py`` 的套路）
-------------------------------------------------------------------------------
``backend_proc`` 的 pid / 日志路径走 ``paths.data_root()``、后端目录走 ``paths.backend_root()``
—— 两者**都不受**测试里 patch 的 ``db.DATA_DIR`` 约束，所以必须显式打桩：

* ``backend_pid._logs_dir`` → 临时目录（pid 文件与两份日志都在里面）；
* ``backend_proc.backend_root`` → 临时目录（"起后端"的 cwd）。

**绝不**用真实的 8900 / 8901：端口相关用例一律用 ``127.0.0.1:0`` 现取的临时端口，
否则会撞上开发机上真在跑的后端（或反过来把它当成"别人占着"而误判）。
本文件有护栏用例钉住"真实 ``data/logs/backend.pid`` 一个字节都没动"。
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock

from app import backend_pid, backend_proc
from app import db
from app.config import settings

_TMP_DIR = tempfile.mkdtemp(prefix="echo-backend-proc-test-")
_TMP_BACKEND = tempfile.mkdtemp(prefix="echo-backend-root-test-")
_OLD_LOGS_DIR = backend_pid._logs_dir
_OLD_BACKEND_ROOT = backend_proc.backend_root
#: 打桩**之前**的真实 pid 路径：只用来比对"有没有被动过"
_REAL_PID_PATH = backend_pid.pid_path()
#: 同样是打桩之前的**真实库**路径与状态（只读比对，绝不写）
_REAL_DB_PATH = os.path.join(db.DATA_DIR, db.DB_FILE) if not os.path.isabs(db.DB_FILE) \
    else db.DB_FILE
_OLD_DB = None


def _real_pid_bytes():
    try:
        with open(_REAL_PID_PATH, "rb") as fh:
            return fh.read()
    except Exception:
        return None


def _real_rows_with(text: str) -> int:
    """真实库里有多少行 logs 提到 `text`（库/表不存在都算 0，**永不抛**）。

    为什么按内容判：真实库的 `(大小, mtime)` 会被**任何**无关写入改掉（用户正在用 ECHO
    就会），那样这条护栏会变成"看运气"；"这条路径写的那行日志有没有落到真库"才是它的本意。
    """
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % _REAL_DB_PATH.replace("\\", "/"), uri=True)
        try:
            return int(conn.execute("select count(*) from logs where message like ?",
                                    ("%" + text + "%",)).fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def _real_db_stat():
    """真实库的 (大小, mtime_ns)；不存在 -> None。**只 stat，不读不写**。"""
    try:
        st = os.stat(_REAL_DB_PATH)
        return (st.st_size, st.st_mtime_ns)
    except Exception:
        return None


def setUpModule():
    """隔离三样：pid/日志**文件**、后端目录、以及**数据库**。

    ⚠️ 第三样是 2026-09-30 补的，补之前这里有个真事故：`backend_proc.stop()` 成功时会
    写一条日志（`db.add_log("info", "backend", "已停止 ECHO 起的后端…")`），而本文件原来
    只把 pid / 日志**文件**的路径指到临时目录 —— 于是每跑一次用例就往**真实的
    `data/echo.db`** 里插一行"已停止 ECHO 起的后端（用例）pid=…"（实测累计 55 行，
    而那 55 行说的都是**这台机器上从未发生过的停后端动作**）。
    这与 `AGENTS.md` 记的 harness 事故是同一类：**测试不许碰真实状态**；
    护栏用例 `IsolationTests::test_the_real_database_is_not_written` 现在钉住这一点。
    """
    global _OLD_DB
    backend_pid._logs_dir = lambda: _TMP_DIR
    backend_proc.backend_root = lambda: _TMP_BACKEND
    _OLD_DB = (db.DATA_DIR, db.DB_FILE)
    db.DATA_DIR = _TMP_DIR
    db.DB_FILE = os.path.join(_TMP_DIR, "backend-proc.db")
    settings._cache = None            # 缓存里可能还留着真实库的值
    db.init()


def tearDownModule():
    global _OLD_DB
    backend_pid._logs_dir = _OLD_LOGS_DIR
    backend_proc.backend_root = _OLD_BACKEND_ROOT
    if _OLD_DB:
        db.DATA_DIR, db.DB_FILE = _OLD_DB
    settings._cache = None
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
    shutil.rmtree(_TMP_BACKEND, ignore_errors=True)


def _clear_pid_file():
    if os.path.exists(backend_pid.pid_path()):
        os.remove(backend_pid.pid_path())


def _kill(proc):
    for step in (proc.terminate, proc.kill):
        try:
            if proc.poll() is None:
                step()
                proc.wait(timeout=5)
        except Exception:
            pass


def _sleep_proc():
    """一个真实的、与后端形状类似的长命子进程（用它代替真后端）。"""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _listening_socket():
    """占一个**临时**端口（现取现用，绝不碰 8900/8901）。返回 ``(sock, port)``。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    return sock, sock.getsockname()[1]


class IsolationTests(unittest.TestCase):
    def test_paths_are_isolated_from_the_real_ones(self):
        """跑测试不动真实 ``data/logs/backend.pid``、也不在真实后端目录里起东西。"""
        self.assertTrue(backend_pid.pid_path().startswith(_TMP_DIR))
        self.assertNotEqual(backend_pid.pid_path(), _REAL_PID_PATH)
        self.assertFalse(_REAL_PID_PATH.startswith(_TMP_DIR))
        self.assertEqual(backend_proc.backend_root(), _TMP_BACKEND)
        self.assertNotEqual(os.path.abspath(backend_proc.backend_root()),
                            os.path.abspath(_OLD_BACKEND_ROOT()))

        before = _real_pid_bytes()
        _clear_pid_file()
        backend_proc.stop(reason="护栏用例", ports=())
        backend_proc.port_check(())          # 空端口表：只是走一遍，绝不碰真端口
        self.assertEqual(_real_pid_bytes(), before)

    def test_the_real_database_is_not_written(self):
        """**库也要隔离**（2026-09-30 补的护栏）：`stop()` 成功时会写一条日志。

        补这条之前，本文件每跑一次就往真实 `data/echo.db` 里插一行
        "已停止 ECHO 起的后端（用例）pid=…"（累计 55 行）。这条用例同时钉两件事：
          * 日志**真的写了**（写进被隔离的那个库）—— 不能靠"把日志关掉"来通过；
          * 真实库的 (大小, mtime) 一个字节没变。
        """
        proc = _sleep_proc()
        self.addCleanup(_kill, proc)
        self.assertTrue(backend_pid.write_pid(proc.pid))
        ok, detail = backend_proc.stop(reason="护栏用例", ports=())
        self.assertTrue(ok, detail)
        # ① 日志写进了**被隔离**的库（证明这条路径确实会写库，而不是被静音了）
        import sqlite3
        conn = sqlite3.connect(db.DB_FILE)
        try:
            count = conn.execute("select count(*) from logs where source='backend'"
                                 ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(count, 0, "隔离库里应该有那条日志（否则这条护栏没有意义）")
        # ② 真实库没被**这条路径**写进去 —— 按**内容**判，不按 (大小, mtime)：
        #    2026-10-05 实测：开发机上 ECHO 正被真实使用（语音指令 / 录音 / 纪要）时，
        #    真实库本来就在被写，按 mtime 比会**冤枉**这条用例（门禁连红两次，
        #    查真库 logs 的时间戳才确认写它的是用户自己的操作）。
        self.assertEqual(0, _real_rows_with("护栏用例"),
                         "用例写进真实 data/echo.db 了（%s）" % _REAL_DB_PATH)


class InterpreterLayoutTests(unittest.TestCase):
    """`python_exe()` 要认**三种**解释器布局（2026-09-30 补第一种）。

    薄包"只带解释器"那一档塞进来的就是**独立 CPython**（uv 托管的那份 / 官方 embeddable 包）——
    它把 `python.exe` 直接放在 `runtime/` 根下。补之前只认 venv 的 `Scripts/python.exe`，
    于是"薄包明明带了 Python，面板却说没有解释器"（这是写完薄包才发现的口子）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-pyexe-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(backend_proc, "backend_root", lambda: self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def _touch(self, rel):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w", encoding="utf-8").close()
        return full

    def test_it_accepts_the_standalone_cpython_layout(self):
        exe = self._touch(os.path.join("runtime", "python.exe"))
        self.assertEqual(backend_proc.python_exe(), exe)

    def test_it_accepts_the_venv_layouts(self):
        exe = self._touch(os.path.join("runtime", "Scripts", "python.exe"))
        self.assertEqual(backend_proc.python_exe(), exe)
        os.remove(exe)
        exe = self._touch(os.path.join("runtime", "bin", "python3"))
        self.assertEqual(backend_proc.python_exe(), exe)

    def test_an_empty_runtime_is_not_an_interpreter(self):
        os.makedirs(os.path.join(self.tmp, "runtime"), exist_ok=True)
        self.assertEqual(backend_proc.python_exe(), "")


class StopOnlyKillsWhatEchoStartedTests(unittest.TestCase):
    def setUp(self):
        _clear_pid_file()

    def tearDown(self):
        _clear_pid_file()

    def test_stop_only_kills_what_echo_started(self):
        """没有 pid 记录 → **不动**任何进程；记上之后 → 停掉它并清掉记录。"""
        proc = _sleep_proc()
        self.addCleanup(_kill, proc)
        self.assertIsNone(proc.poll(), "子进程没起来，用例没法判")

        ok, detail = backend_proc.stop(reason="用例", ports=())
        self.assertTrue(ok, detail)
        self.assertIn("没有可停的后端", detail)
        self.assertIsNone(proc.poll(), "没记录时不该动任何进程：%s" % detail)
        self.assertFalse(os.path.exists(backend_pid.pid_path()))

        # 记上归属 → 这一次才是"我们的"
        self.assertTrue(backend_pid.write_pid(proc.pid))
        ok, detail = backend_proc.stop(reason="用例", ports=())
        self.assertTrue(ok, detail)
        self.assertIn(str(proc.pid), detail)
        for _ in range(50):                  # 杀树是异步的，等它真的落地
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(proc.poll(), "记了归属就该真的停掉：%s" % detail)
        self.assertFalse(os.path.exists(backend_pid.pid_path()),
                         "停成功后 pid 记录必须被清掉")

    def test_a_stale_record_is_reported_and_not_killed(self):
        """陈旧记录（那个 pid 早没了）→ 如实说"陈旧"，不猜一个来停。"""
        proc = _sleep_proc()
        self.addCleanup(_kill, proc)
        self.assertTrue(backend_pid.write_pid(999999))   # 这个 pid 不是我们的子进程
        ok, detail = backend_proc.stop(reason="用例", ports=())
        self.assertTrue(ok, detail)
        self.assertIn("陈旧", detail)
        self.assertIsNone(proc.poll(), "陈旧记录时不许动别的进程")
        self.assertFalse(os.path.exists(backend_pid.pid_path()))


class PortOwnerTests(unittest.TestCase):
    def setUp(self):
        _clear_pid_file()
        self.sock, self.port = _listening_socket()
        self.addCleanup(self.sock.close)

    def tearDown(self):
        _clear_pid_file()

    def test_port_conflict_names_the_owner(self):
        """端口被**别人**占着 → 不起，并说出占用者是谁（端口 + pid + 名字）。"""
        ok, detail = backend_proc.port_check((self.port,))
        self.assertFalse(ok, detail)
        self.assertIn(str(self.port), detail)
        self.assertIn(str(os.getpid()), detail, "必须说出占用者的 pid：%s" % detail)
        self.assertIn("占着", detail)
        owner = backend_proc.port_owner(self.port)
        self.assertEqual(owner["pid"], os.getpid(), owner)
        self.assertIn(str(self.port), backend_proc.describe_owner(owner))

    def test_our_own_backend_on_the_port_is_not_a_conflict(self):
        """占用者**就是 pid 记录里那条** → 不冲突（已经在跑，调用方直接进入配对）。"""
        self.assertTrue(backend_pid.write_pid(os.getpid()))
        ok, detail = backend_proc.port_check((self.port,))
        self.assertTrue(ok, detail)
        self.assertIn("已经在跑", detail)
        self.assertIn(str(self.port), detail)

    def test_describe_owner_says_free_when_nobody_listens(self):
        """没人监听 → ``port_owner`` 给 ``pid=0``，人话是"空着"。"""
        owner = backend_proc.port_owner(self.port)
        self.assertEqual(owner["pid"], os.getpid())      # 这个端口我们自己占着
        self.assertIn("空着", backend_proc.describe_owner({"port": self.port, "pid": 0}))


class SpawnTests(unittest.TestCase):
    def setUp(self):
        _clear_pid_file()

    def tearDown(self):
        # 用例结束时把"我们起的"也收干净（哪怕断言失败）
        try:
            backend_proc.stop(reason="用例清理", ports=())
        except Exception:
            pass
        _clear_pid_file()

    def _spawn_sleeper(self):
        return backend_proc.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                                  cwd=_TMP_BACKEND, ports=())

    def test_spawn_records_the_pid_and_stop_ends_it(self):
        """起 → pid 落盘（可读、活着）→ 停 → 进程没了、记录清了。"""
        ok, detail = self._spawn_sleeper()
        self.assertTrue(ok, detail)
        pid = backend_pid.read_pid()
        self.assertTrue(pid and pid > 0, "%s / %s" % (detail, backend_pid.pid_path()))
        alive, note = backend_pid.is_ours_alive()
        self.assertTrue(alive, note)
        log, err = backend_pid.log_paths()
        self.assertTrue(os.path.exists(log), "启动横幅该写进 %s" % log)
        for path in (log, err):
            self.assertTrue(os.path.dirname(path) == _TMP_DIR, path)

        ok, detail = backend_proc.stop(reason="用例", ports=())
        self.assertTrue(ok, detail)
        self.assertIsNone(backend_pid.read_pid(), "停完 pid 记录必须清掉")

    def test_spawn_refuses_a_second_one(self):
        """pid 记录里那条还活着 → 不起第二个（同机 1:1）。"""
        ok, detail = self._spawn_sleeper()
        self.assertTrue(ok, detail)
        first = backend_pid.read_pid()
        ok2, detail2 = self._spawn_sleeper()
        self.assertFalse(ok2, detail2)
        self.assertIn("已经在跑", detail2)
        self.assertEqual(backend_pid.read_pid(), first, "不该多出一个进程")

    def test_spawn_refuses_when_the_port_is_taken_by_a_stranger(self):
        """端口被别人占着 → 一个进程都不起，并把占用者说出来。"""
        sock, port = _listening_socket()
        self.addCleanup(sock.close)
        ok, detail = backend_proc.spawn(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=_TMP_BACKEND, ports=(port,))
        self.assertFalse(ok, detail)
        self.assertIn(str(port), detail)
        self.assertIn(str(os.getpid()), detail)
        self.assertIsNone(backend_pid.read_pid(), "没起起来就不该留 pid 记录")

    def test_spawn_says_so_when_the_interpreter_is_missing(self):
        """找不到解释器 → 人话（而不是一个 Popen 的 traceback）。"""
        ok, detail = backend_proc.spawn(["definitely-not-here-echo-test"], ports=())
        self.assertFalse(ok, detail)
        self.assertIn("找不到", detail)


if __name__ == "__main__":
    unittest.main()
