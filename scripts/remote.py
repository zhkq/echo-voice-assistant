# -*- coding: utf-8 -*-
"""在测试机上执行命令（密码走环境变量，不写进命令行/仓库）。

用法：
    set ECHO_TEST_HOST=10.100.0.24
    set ECHO_TEST_USER=zysk
    set ECHO_TEST_PASS=...
    python scripts/remote.py "uname -a" "nvidia-smi -L"

为什么单独一个脚本：`ssh.exe` 不能非交互传密码，而**密码不该出现在命令行里**
（会进 PowerShell 历史、进程列表、日志）。这里从环境变量读，且不回显。
"""
import os
import sys

import paramiko


def connect():
    host = os.environ.get("ECHO_TEST_HOST", "")
    user = os.environ.get("ECHO_TEST_USER", "")
    pwd = os.environ.get("ECHO_TEST_PASS", "")
    if not (host and user and pwd):
        raise SystemExit("缺 ECHO_TEST_HOST / ECHO_TEST_USER / ECHO_TEST_PASS")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=user, password=pwd, timeout=15,
              allow_agent=False, look_for_keys=False)
    return c


def run(c, cmd, timeout=60, sudo=False):
    """跑一条命令。`sudo=True` 时用 `sudo -S` 并把密码从 **stdin** 喂进去。

    为什么不写成 `echo PASS | sudo -S ...`：那样密码会出现在**远端命令行**上
    （`ps` 看得见、也可能进 shell 历史）。stdin 只有我们和 sudo 知道。
    """
    if sudo:
        pwd = os.environ.get("ECHO_TEST_PASS", "")
        cmd = "sudo -S -p '' bash -lc %s" % _shquote(cmd)
    _in, out, err = c.exec_command(cmd, timeout=timeout)
    if sudo:
        _in.write(pwd + "\n")
        _in.flush()
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    return rc, o, e


def _shquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main(argv):
    # 约定：命令前面加 `sudo:` 就用 sudo 跑（例：python scripts/remote.py "sudo:docker images"）
    cmds = argv or ["uname -a"]
    c = connect()
    try:
        for cmd in cmds:
            use_sudo = cmd.startswith("sudo:")
            if use_sudo:
                cmd = cmd[5:].strip()
            print("$ %s%s" % ("[sudo] " if use_sudo else "", cmd))
            rc, o, e = run(c, cmd, sudo=use_sudo)
            if o.strip():
                print(o.rstrip())
            if e.strip():
                print("[stderr] " + e.rstrip())
            if rc:
                print("[exit %s]" % rc)
            print()
    finally:
        c.close()


if __name__ == "__main__":
    main(sys.argv[1:])
