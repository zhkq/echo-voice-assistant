# -*- coding: utf-8 -*-
"""Windows 平台环境默认值。

放在这里的理由见 ``app/platform/__init__.py``：平台差异只允许出现在本目录下。
Windows 是 ECHO 的主平台，因此这些值与 1.x 的行为**逐字保持**，不惊动老用户。
"""
import os

NAME = "win32"

#: 系统数据根：与 1.x 一致，就在安装目录下（老用户升级后 data 不用搬）
PLATFORM_DEFAULTS = {
    "dataDir": "{ECHO}/data",
}


def dangerous_prefixes():
    """用户不该把数据/模型目录指到这些位置。"""
    out = []
    win = os.environ.get("SystemRoot") or r"C:\Windows"
    out.append((os.path.abspath(win), "不能放在系统目录（%s）下" % win))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        base = os.environ.get(var)
        if base:
            out.append((os.path.abspath(base), "不能放在 %s 下" % base))
    return out
