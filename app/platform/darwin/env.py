# -*- coding: utf-8 -*-
"""macOS 平台环境默认值（D18）。

系统数据放 ``~/Library/Application Support/ECHO``：``.app`` 内部不可写、也不该写，
而且签名封条不允许往 bundle 里塞运行时数据。

注意：mac 侧还有一层"注入式入口"（``mac/run_mac.py``）负责热键/边条/TTS 等运行时替换，
那是 P3 才收拢的；本文件目前只提供环境默认值。
"""
import os

NAME = "darwin"

PLATFORM_DEFAULTS = {
    "dataDir": os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ECHO"),
}


def dangerous_prefixes():
    return [
        ("/System", "不能放在系统目录下"),
        ("/Library", "不能放在系统目录下"),
        ("/Applications", "不能放在应用程序目录下"),
        ("/usr", "不能放在系统目录下"),
    ]
