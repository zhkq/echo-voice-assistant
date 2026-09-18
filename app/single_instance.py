# -*- coding: utf-8 -*-
"""single_instance.py — 进程级单实例锁（内核持有，进程退出即释放）

为什么不用端口探测
------------------
原来的做法是"连一下端口，通了就说明已有实例"（`app/main.py` 里那段 socket 探测）。
它有两个致命问题：

1. **有竞态窗口**：探测与 uvicorn 真正 bind 之间隔着一段时间。两个实例同时启动时
   会双双探测到"端口空闲"，然后双双继续往下跑 —— 2026-09-18 实测就是如此：
   18060 上有一个在服务，另有一个**不监听却活着**，还在后台加载 SenseVoice
   （CPU 空转、显存/内存白占），任何基于端口的探活都会被它误导。
2. **探测失败不代表没事**：端口被非 ECHO 程序占用时同样"探测成功"，消息会误导。

锁由操作系统持有：Windows 是命名互斥量（进程退出/崩溃时内核自动释放），
POSIX 是 `flock` 的文件锁（fd 关闭时释放）。所以既没有竞态窗口，也不会留下
需要人工清理的陈旧锁文件。

用法
----
    from app.single_instance import acquire
    ok, detail = acquire("echo", data_dir)
    if not ok:
        sys.exit(0)          # 已有实例，本进程是冗余的

锁按 **数据目录** 区分：同一个 data 目录只允许一个实例（两个进程共用一份
`echo.db` 是不允许的），不同数据目录/不同端口的两个 ECHO 可以并存。
"""
import hashlib
import os
import threading

_LOCK = threading.Lock()
_HELD = {}          # name -> 句柄（Windows: HANDLE(int) / POSIX: fd）

ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000


def _lock_id(name, data_dir):
    """按数据目录派生稳定的锁名（同一目录 → 同一把锁）。"""
    key = os.path.normcase(os.path.abspath(data_dir))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return "ECHO-%s-%s" % (name, digest)


def _lock_file(data_dir, name):
    return os.path.join(data_dir, "%s.lock" % name)


# ---------------------------------------------------------------- Windows
def _win_kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _win_acquire(lid):
    import ctypes
    k32 = _win_kernel32()
    handle = k32.CreateMutexW(None, True, "Local\\" + lid)
    err = ctypes.get_last_error()      # 必须紧跟调用读取
    if not handle:
        return None, "CreateMutexW 失败（err=%s）" % err
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return None, "已有同名实例在运行（%s）" % lid
    return handle, lid


def _win_held(lid):
    k32 = _win_kernel32()
    handle = k32.OpenMutexW(SYNCHRONIZE, False, "Local\\" + lid)
    if handle:
        k32.CloseHandle(handle)
        return True
    return False


def _win_release(handle):
    _win_kernel32().CloseHandle(handle)


# ---------------------------------------------------------------- POSIX
def _posix_acquire(path):
    import fcntl
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None, "已有同名实例在运行（%s）" % path
    try:                       # 写入 pid 便于排查（锁本身不依赖文件内容）
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
    return fd, path


def _posix_held(path):
    import fcntl
    if not os.path.isfile(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True            # 别人持着
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _posix_release(fd):
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------- 对外接口
def acquire(name="echo", data_dir=None):
    """尝试独占名为 `name` 的锁。返回 (ok, detail)。

    ok=True 时锁由本进程持有，直到进程退出或显式 release()。同一进程重复 acquire
    同一把锁会返回 False（不会自己跟自己抢成功），便于测试与防御性调用。
    """
    data_dir = data_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    lid = _lock_id(name, data_dir)
    with _LOCK:
        if name in _HELD:
            return False, "本进程已持有该锁（%s）" % lid
        if os.name == "nt":
            handle, detail = _win_acquire(lid)
        else:
            handle, detail = _posix_acquire(_lock_file(data_dir, name))
        if handle is None:
            return False, detail
        _HELD[name] = handle
        return True, detail


def release(name="echo"):
    """释放锁（正常退出不需要调用，内核会自动释放）。"""
    with _LOCK:
        handle = _HELD.pop(name, None)
    if handle is None:
        return False
    if os.name == "nt":
        _win_release(handle)
    else:
        _posix_release(handle)
    return True


def is_held(name="echo", data_dir=None):
    """该锁当前**是否存在**（含本进程自己持有）。只探测，不获取。"""
    data_dir = data_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    if os.name == "nt":
        return _win_held(_lock_id(name, data_dir))
    return _posix_held(_lock_file(data_dir, name))
