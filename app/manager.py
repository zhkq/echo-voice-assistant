# -*- coding: utf-8 -*-
r"""manager.py — ECHO 进程管理（DSH Desktop 探测 + 自身 pid）

DSH Desktop 2.x 是 ECHO 的执行底层，由桌面客户端自己托管（Web GUI 与 API
同端口，默认 http://127.0.0.1:43120），ECHO 不再拉起/终止 DSH 进程：
  * 启动/就绪：探测 43120 的 /api/session.list（带签名 Cookie）
  * 停止：无操作（桌面版由用户/开机自启管理），仅清理失效 pid 文件
  * 状态：端口/API 探活

旧的"spawn node <dsh>/lib/bin.js web"方式（0.1.1-rc.2，独立 3080 服务）已随
DSH Desktop 升级弃用：其会话与桌面版不互通。
"""
import os

from app import paths as _paths
from app.dsh import get_client

BASE_DIR = _paths.echo_root()
# 数据根：与 db.py 一致，改由路径层解析（安装根可覆盖 + 分平台默认值）。仍是模块级
# 常量（现有测试用"给模块属性赋值"隔离数据目录；不要用模块 __getattr__，
# PEP 562 对模块内部裸名字无效）。
DATA_DIR = _paths.data_root()
DSH_PID_FILE = os.path.join(DATA_DIR, "dsh.pid")
ECHO_PID_FILE = os.path.join(DATA_DIR, "echo.pid")



def dsh_ready():
    """DSH Desktop API 是否可访问（两层认证通过才算）。"""
    try:
        return get_client().ping()
    except Exception:
        return False


def dsh_start():
    """DSH Desktop 由用户/自启管理，此处只探测并给出提示。返回 (ok, message)。"""
    if dsh_ready():
        return True, "DSH Desktop 已在运行"
    return False, ("DSH Desktop 未运行或未开放本机访问。请启动 DSH Desktop"
                   "（设置 → 常规：普通浏览器访问=开启，或确认 ~/.dsh/settings.yaml"
                   " 的 dsh-desktop.mode=compatibility 且 openBrowser=true），"
                   "然后重试")


def dsh_stop():
    """桌面版不由 ECHO 停止；仅清理失效的旧 pid 文件。返回 (ok, message)。"""
    try:
        if os.path.isfile(DSH_PID_FILE):
            os.remove(DSH_PID_FILE)
    except OSError:
        pass
    return True, "DSH Desktop 由桌面客户端托管，ECHO 不参与启停（旧 pid 已清理）"


def echo_write_pid():
    _write_pid(ECHO_PID_FILE, os.getpid())


def echo_stop_self():
    """停止当前 ECHO 服务（供面板按钮调用）。"""
    try:
        os._exit(0)
    except SystemExit:
        raise


def _write_pid(path, pid):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        pass
