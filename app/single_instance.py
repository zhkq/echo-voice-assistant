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

平台原语在 `app/platform/<os>/env.py` 里（D12：业务代码不得自己写 `os.name` 分支）：
Windows 走命名互斥量，Linux/macOS 走 `app/platform/_posix.py` 的 flock。

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

from app import paths
from app import platform as echo_platform

_LOCK = threading.Lock()
_HELD = {}          # name -> 句柄（Windows: HANDLE(int) / POSIX: fd）


def _lock_id(name, data_dir):
    """按数据目录派生稳定的锁名（同一目录 → 同一把锁）。"""
    key = os.path.normcase(os.path.abspath(data_dir))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return "ECHO-%s-%s" % (name, digest)


def _lock_file(data_dir, name):
    return os.path.join(data_dir, "%s.lock" % name)


# ---------------------------------------------------------------- 对外接口
def acquire(name="echo", data_dir=None):
    """尝试独占名为 `name` 的锁。返回 (ok, detail)。

    ok=True 时锁由本进程持有，直到进程退出或显式 release()。同一进程重复 acquire
    同一把锁会返回 False（不会自己跟自己抢成功），便于测试与防御性调用。
    """
    data_dir = data_dir or paths.data_root()
    lid = _lock_id(name, data_dir)
    with _LOCK:
        if name in _HELD:
            return False, "本进程已持有该锁（%s）" % lid
        handle, detail = echo_platform.acquire_named_lock(lid, _lock_file(data_dir, name))
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
    echo_platform.release_named_lock(handle)
    return True


def is_held(name="echo", data_dir=None):
    """该锁当前**是否存在**（含本进程自己持有）。只探测，不获取。"""
    data_dir = data_dir or paths.data_root()
    return echo_platform.named_lock_held(_lock_id(name, data_dir),
                                         _lock_file(data_dir, name))
