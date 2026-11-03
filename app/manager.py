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
    """DSH **Desktop** API 是否可访问（两层认证通过才算）。

    这里刻意指名桌面版适配器（`get_desktop_client()`）：本函数回答的是"桌面版这个进程
    在不在"，与用户当前选了哪个智能体无关（选独立 harness 时也该照实报告桌面版状态）。
    """
    try:
        from app.dsh import get_desktop_client
        return get_desktop_client().ping()
    except Exception:
        return False


def dsh_start():
    """**一套 DSH** 的启动入口：桌面版在跑就直接用它，否则由 ECHO 起无头 dsh。

    2026-11-03 用户拍板：只保留一套 DSH。桌面版 GUI **不由 ECHO 启动/关闭**
    （那是用户自己的窗口），但它没开时 ECHO 会把同一份程序（桌面端自带的 dsh）
    以无头方式拉起来，家目录同样是 `~/.dsh`。返回 ``(ok, message)``。
    """
    if dsh_ready():
        return True, "DSH 桌面版已在运行（%s）" % _desktop_url()
    try:
        from app import harness_proc
        return harness_proc.ensure_running()
    except Exception as exc:
        return False, "启动 DSH 失败：%s: %s" % (type(exc).__name__, exc)


def _desktop_url():
    try:
        from app import harness_proc
        return harness_proc.desktop_url()
    except Exception:
        return "http://127.0.0.1:43120"


def agent_ready():
    """**当前选中的**智能体是否可用（桌面版或标准版 harness，谁选中就问谁）。

    这和 `dsh_ready()` 是**两个不同的问题**，别合并：

    | 函数 | 回答的问题 | 什么时候用 |
    |---|---|---|
    | `dsh_ready()` | "DSH **桌面版**这个进程在不在" | 桌面版自己的启停探测（`dsh_start`）、
      boot 里那个"桌面版"组件行 —— 用户选了标准版时它**照实报"跳过"**才是对的 |
    | `agent_ready()` | "**我配置的那个**智能体能不能干活" | 要不要把活交给智能体
      （会议纪要 / 分段 / 归档） |

    **来由**（2026-10-08 真实事故，历史）：纪要当年会在智能体不可用时退回"直连 LLM"，
    而那个判据误问了 `dsh_ready()`，于是用户选了标准版 harness、桌面版没开时，
    纪要与分段**全部绕开智能体**。用户原话："会议纪要分段应该发给配置好的智能体，
    也就是 dsh 标准版进行呀"。

    2026-10-22 起直连 LLM 已**彻底删除**：`agent_ready()` 不再决定"走哪条路"，
    而是决定"能不能做"——不可用就明确失败（`meeting.request_summary` 返回 False），
    不再静默降级。

    探测失败一律按"不可用"处理（宁可明确报不可用，也不要拿着一份坏配置去发活）。
    """
    try:
        from app.dsh import get_client
        return bool(get_client().ping())
    except Exception:
        return False


def dsh_stop():
    """停止 **ECHO 自己拉起的** 无头 dsh（桌面版 GUI 不归 ECHO 停）。返回 (ok, message)。"""
    ok, msg = True, ""
    try:
        from app import harness_proc
        ok, msg = harness_proc.stop(reason="面板/接口手动停止 DSH")
    except Exception as exc:
        ok, msg = False, "停止 DSH 失败：%s: %s" % (type(exc).__name__, exc)
    try:
        if os.path.isfile(DSH_PID_FILE):
            os.remove(DSH_PID_FILE)
    except OSError:
        pass
    return ok, msg or "DSH 已停止"


def echo_write_pid():
    _write_pid(ECHO_PID_FILE, os.getpid())


def echo_stop_self(keep_backend: bool = False):
    """停止当前 ECHO 服务（面板「关机」/ `POST /api/control/echo/stop` 的定时器都走这里）。

    **退出前先安排完整停止**：`scripts/stop.ps1` 会先停 watchdog / switch supervisor，
    再停 ECHO、边条和（默认）本机后端 —— 否则 watchdog 几秒后又把 ECHO 拉起来。
    升级路径会传 ``keep_backend=True``（后端配对/配置不能因为升级被停掉）。
    """
    try:
        from app import runtime as _runtime
        # 2026-10-31：**先安排完整停止**（watchdog/supervisor + ECHO + 边条 + 后端
        # 全由 scripts/stop.ps1 负责）。原来只 os._exit 自己，watchdog 几秒后把
        # ECHO 又拉起来，用户看到的是"关完又自动打开"。
        _runtime.schedule_full_stop(keep_backend=keep_backend)
        _runtime.quit_sidebar()
        # 兜底：旧边条不认 quit / 管道没接上 / 屏幕上是升级前遗留的进程时，
        # 3 秒后由脱离 ECHO 的清理进程按本棵树路径强制结束它。正常边条 2 秒就关了，
        # 这里跑过去时已经没有进程可杀；只匹配 <BASE_DIR>\sidebar\，不误杀别的树。
        _runtime.schedule_sidebar_force_close()
    except Exception as e:  # noqa: BLE001 —— 停止动作的最后一步，任何异常都不许挡住退出
        print(f"[service] 通知边条退出失败（忽略，继续停服务）: {e}")
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
