"""ECHO 自动化测试。

临时目录收尾（2026-09-21 引入，2026-09-26 改成**只清本进程自己建的**）
=====================================================================

仓库里的用例普遍用 ``tempfile.mkdtemp(prefix="echo-...")`` 造隔离目录，但多数
``tearDown`` 只恢复补丁、不删目录：全量跑一次漏几十个，实测 TEMP 里积到了 **1120 个**
（``test_wizard.py`` 一个文件就贡献 628 个）。

第一版的做法是"在进程退出时删掉「本次运行期间新增的 ``echo-`` 前缀目录」"。它单独跑没问题，
**并发跑第二个测试进程时互相踩**：A 进程把 B 进程刚建好、正在用的临时目录当成"本次新增的"
删掉，B 那边的表现是 ``no such table: settings``（库文件连目录一起没了）——2026-09-26
实测踩到过，而且这种故障看起来像"代码坏了"，查起来极费时间。

现在的做法（**只清自己登记过的路径**）：

1. `tempfile.mkdtemp` 在**本包导入时**被换成会登记的版本（`_tracking_mkdtemp`）——
   用例一行都不用改，凡是走 ``tempfile.mkdtemp`` 建出来的目录都记在 `_OWNED` 里；
2. 进程退出时只 ``rmtree`` 这些登记过的目录，**别的一律不碰**：另一个测试进程的目录
   不在我们的登记表里，就永远不会被我们删掉；
3. 需要手工登记的（少数直接 ``os.makedirs`` 的用例）用 `register_temp_dir()`。

为什么不是"按 pid 命名"：那要求改 50 处调用点（``prefix="echo-<pid>-..."``），
而登记表的覆盖面一样、还不用改调用点。判据仍然是"删目录不删文件"——
文件可能属于同时跑着的别的 ECHO 进程（``app/audio/tts.py`` 写 ``echo-tts-<pid>.mp3``、
``app/api.py`` 写上传中转文件），删掉会打到别人。

覆盖不到的情况都是明知且可接受的：进程被**强杀**时 ``atexit`` 不执行；
裸 ``discover -s tests``（不加 ``-t .``）会把用例当顶层模块导入、不加载本模块，
收尾也就不生效 —— 那种跑法本来就不符合仓库的包结构约定（门禁用的是 ``discover -s tests -t .``）。

顺带一条纪律（2026-09-26）：**本包导入时关掉模型使用账本**
（``app.model_usage.ENABLED``）。用例会真的调到 ``stt.transcribe_ex()`` /
``diarize_wav_full()``，而它们会往"碰巧配着的那个库"写真实的使用记录 ——
不关的话，跑一次测试就能让用户面板上的「清理」把某个模型看成"刚用过"，
甚至污染 `data/echo.db`。要验证账本本身的用例自己把它打开。
"""
import atexit
import os
import shutil
import tempfile
import threading

_OWNED = set()                      # 本进程建的临时目录（绝对路径，normcase 过）
_OWNED_LOCK = threading.Lock()

_original_mkdtemp = tempfile.mkdtemp


def register_temp_dir(path):
    """登记一个"本进程建的、退出时要删"的目录；返回原路径。

    只有目录才登记（文件不在清理范围内，见模块文档）。
    """
    if not path:
        return path
    key = os.path.normcase(os.path.abspath(str(path)))
    with _OWNED_LOCK:
        _OWNED.add(key)
    return path


def owns(path):
    """这个目录是不是本进程登记过的。"""
    if not path:
        return False
    return os.path.normcase(os.path.abspath(str(path))) in _OWNED


def tracked_dirs():
    """已登记目录的快照（用例用它验证契约，别去读私有集合）。"""
    with _OWNED_LOCK:
        return sorted(_OWNED)


def _tracking_mkdtemp(*args, **kwargs):
    """`tempfile.mkdtemp` 的替身：建完就登记（用例照原样写就行）。"""
    return register_temp_dir(_original_mkdtemp(*args, **kwargs))


#: 从这一刻起，凡是走 `tempfile.mkdtemp()` 建的目录都会被本进程收尾。
#: 用例模块是在本包 `__init__` 之后才导入的，所以它们拿到的都是这个替身。
tempfile.mkdtemp = _tracking_mkdtemp


def sweep(owned=None):
    """删掉**登记过的**临时目录，返回实际删掉的个数。

    `owned` 省略时用本进程的登记表；用例可以传一份自己的集合来验证契约
    （`tests/test_temp_hygiene.py` 就是这么钉住"别人的目录一个都不许碰"的）。
    """
    if owned is None:
        with _OWNED_LOCK:
            targets = list(_OWNED)
    else:
        targets = [os.path.normcase(os.path.abspath(str(p))) for p in owned]
    removed = 0
    for path in targets:
        if not os.path.isdir(path):
            with _OWNED_LOCK:
                _OWNED.discard(path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
            with _OWNED_LOCK:
                _OWNED.discard(path)
    return removed


# 模型使用账本：用例不许往真实库里写使用记录（见模块文档末段）。
try:                                                    # pragma: no cover - 环保
    from app import model_usage as _model_usage
    _model_usage.ENABLED = False
except Exception:
    pass


atexit.register(sweep)
