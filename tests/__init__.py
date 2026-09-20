"""ECHO 自动化测试。

临时目录收尾（2026-09-21）
==========================

仓库里的用例普遍用 ``tempfile.mkdtemp(prefix="echo-...")`` 造隔离目录，但多数
``tearDown`` 只恢复补丁、不删目录：全量跑一次漏几十个，实测 TEMP 里积到了 **1120 个**
（``test_wizard.py`` 一个文件就贡献 628 个）。

与其改 50 处调用点，这里统一在**进程退出时**扫尾：只删「本次运行期间新增的、
``echo-`` 前缀的**目录**」。两条自我约束都是为了不误伤：

1. **只删目录，不动文件** —— 文件可能属于同时跑着的别的 ECHO 进程
   （``app/audio/tts.py`` 会写 ``echo-tts-<pid>.mp3``、``app/api.py`` 会写上传中转文件），
   删掉会打到别人。
2. **只删运行前不存在的**（导入本模块时先拍快照）—— 运行前就在的东西一律不碰。

覆盖不到的两种情况，都是明知且可接受：进程被**强杀**时 ``atexit`` 不执行（正常结束、
断言失败退出、KeyboardInterrupt 都会执行）；裸 ``discover -s tests``（不加 ``-t .``）会把
用例当顶层模块导入、不加载本模块，收尾也就不生效 —— 那种跑法本来就不符合仓库的包结构约定
（门禁用的是 ``discover -s tests -t .``）。
"""
import atexit
import os
import shutil
import tempfile

#: 测试用临时目录的前缀。要清理新前缀时加在这里，别在 sweep 里另写一套判断。
TEMP_PREFIXES = ("echo-",)


def _snapshot(root):
    """列出 root 下匹配测试前缀的**目录**（绝对路径）。

    只看目录：文件不在清理范围内（见模块文档第 1 条）。
    """
    try:
        names = os.listdir(root)
    except OSError:
        return set()
    found = set()
    for name in names:
        if not name.startswith(TEMP_PREFIXES):
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            found.add(path)
    return found


_BEFORE = _snapshot(tempfile.gettempdir())


def sweep(root=None, before=None):
    """删掉 root 下「新增的 echo- 目录」，返回实际删掉的个数。

    ``root`` 省略时用 ``tempfile.gettempdir()``，``before`` 省略时用导入时拍的快照。
    两者都抽成参数，是为了让用例能直接验证这个契约（见 ``tests/test_temp_hygiene.py``）。
    """
    root = root or tempfile.gettempdir()
    if before is None:
        before = _BEFORE
    removed = 0
    for path in _snapshot(root) - set(before):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
    return removed


atexit.register(sweep)
