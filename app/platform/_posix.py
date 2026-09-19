# -*- coding: utf-8 -*-
"""POSIX（Linux / macOS）单实例锁原语：``flock`` 文件锁。

放在 ``app/platform/`` 下的**共享**实现，由 ``linux/env.py`` 与 ``darwin/env.py`` 转发——
两个平台的锁语义完全相同，没必要抄两份。Windows 那套是命名互斥量，见 win32/env.py。

锁由 fd 持有：进程退出（含崩溃）时内核自动释放，不留需要人工清理的陈旧锁文件。
"""
import os


def acquire(path):
    """尝试独占 ``path``。返回 ``(fd, detail)``；已被别处持有时 fd 为 None。"""
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


def held(path) -> bool:
    """该锁当前**是否存在**（含本进程自己持有）。只探测，不获取。"""
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


def release(fd):
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
