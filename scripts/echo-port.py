# -*- coding: utf-8 -*-
"""echo-port.py — ECHO 面板端口的**唯一解析器**（供 PowerShell 脚本调用）。

为什么要有它（2026-10-06）
==========================

同一个端口被三个地方各推一遍，顺序还各不相同，于是"从面板按重启"会出现这种现场：

    日志里是 "resolved ECHO port=18060"，而 ECHO 实际在 8970

根因是**环境变量被当成第一优先**：`ECHO_PORT` 会从任何父进程（终端、计划任务、
开发机上的持久用户变量）继承进来，而它可能指着**另一棵树**或者一个早已废弃的端口。
它压过了 `<数据根>/data/echo-port.txt` —— 而那个文件才是 ECHO **启动时自己写的实际端口**。

判定顺序（本文件是唯一事实源）
==============================

1. **配置 `serverPort`**（库 `<数据根>/data/echo.db` 的 settings 表）
   —— 用户的显式选择，权威。面板上的「ECHO 面板端口」就是它。
2. **`<数据根>/data/echo-port.txt`**
   —— 首选端口被占或落在 Windows 保留段时，``app/main.py`` 会让位并把**实际**端口写回这里。
   优先级在配置之下是本设计的有意取舍：配置是"我要它听哪儿"，端口文件是"它实际听了哪儿"；
   两者不一致时，ECHO 下次启动仍会尝试配置值（主循环里 `port_check` 会如实说出被占）。
3. **环境变量 `ECHO_PORT`**（**仅兜底**）：库与端口文件都读不到时（例如数据目录还没建、
   或从别处注入）才用它。保留它是因为安装器 `install-all.ps1` 会**显式钉住**自己装的那棵树。
4. 出厂默认 **8970**。

用法（PowerShell）：``$port = & $py scripts\echo-port.py --data-root <数据根>``
退出码恒为 0，只打印一个整数 —— 调用方不需要处理错误分支。
"""
import argparse
import json
import os
import sqlite3
import sys

DEFAULT_PORT = 8970
PORT_FILE_NAME = "echo-port.txt"


def _candidate_roots(data_root: str):
    """把调用方给的路径展开成"可能含 echo.db / echo-port.txt 的目录"列表。

    为什么要容忍两种传法（2026-10-06 实测）：`paths.data_root()` 给的是**含 echo.db 的那一层**
    （dev = ``C:\\echo-dev\\data``），而脚本里算出来的 `$base` 是**安装根**
    （``C:\\echo-dev``）。两者都有人传，猜错就会静默回落到默认端口 —— 正是本次要消灭的那类 bug。
    """
    out = []
    for d in (data_root, os.path.join(data_root or "", "data")):
        if d and d not in out:
            out.append(d)
    return out


def _from_config(data_root: str) -> int:
    """读配置里的 `serverPort`；读不到 = 0。"""
    for root in _candidate_roots(data_root):
        db = os.path.join(root, "echo.db")
        if not os.path.isfile(db):
            continue
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
            try:
                row = conn.execute("SELECT value FROM settings WHERE key=?",
                                   ("serverPort",)).fetchone()
            finally:
                conn.close()
            if not row or row[0] is None:
                continue
            raw = row[0]
            # 库里存的是 JSON（"18060" 或 18060）
            try:
                val = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                val = raw
            port = int(val)
            if 0 < port <= 65535:
                return port
        except Exception:
            continue
    return 0


def _from_port_file(data_root: str) -> int:
    """读 `echo-port.txt`（ECHO 启动时写的实际端口）；读不到 = 0。"""
    for root in _candidate_roots(data_root):
        for rel in (PORT_FILE_NAME, os.path.join("data", PORT_FILE_NAME)):
            path = os.path.join(root, rel)
            try:
                with open(path, "r", encoding="utf-8-sig") as fh:
                    text = (fh.read() or "").strip()
                port = int(text)
                if 0 < port <= 65535:
                    return port
            except Exception:
                continue
    return 0


def resolve(data_root: str) -> int:
    """按 §判定顺序 返回端口。**永不抛**。"""
    port = _from_config(data_root or "")
    if port:
        return port
    port = _from_port_file(data_root or "")
    if port:
        return port
    try:
        port = int(str(os.environ.get("ECHO_PORT") or "").strip())
        if 0 < port <= 65535:
            return port
    except Exception:
        pass
    return DEFAULT_PORT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ECHO 面板端口的唯一解析器")
    ap.add_argument("--data-root", default="", help="ECHO 的数据根（含 echo.db / data\\echo-port.txt）")
    ap.add_argument("--explain", action="store_true", help="把判定过程打到 stderr（排障用）")
    args = ap.parse_args(argv)

    sys.stdout.write("%d\n" % resolve(args.data_root))
    if args.explain:
        sys.stderr.write(
            "data_root=%r config=%d port_file=%d env=%r -> %d\n"
            % (args.data_root, _from_config(args.data_root),
               _from_port_file(args.data_root), os.environ.get("ECHO_PORT"),
               resolve(args.data_root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
