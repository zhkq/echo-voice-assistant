# -*- coding: utf-8 -*-
"""app.platform —— 平台接缝的唯一入口（2.0 / D10–D12）

规则（由 ``tests/test_platform_contract.py`` 钉住）：

  **平台差异只允许出现在 ``app/platform/<os>/`` 里。** ``app/`` 下的其它模块一律通过
  本包取平台默认值，不得自己写平台分支，也不得出现平台特征串（如 ``darwin``）。

这是 P1 的切片：目前只承载"环境默认值"（系统数据目录、危险目录前缀）。
P3 会把 ``config.DEFAULTS`` 的平台默认值与 mac 运行时的注入式覆盖一并收拢到这里
（D11/D17：接缝一次性分层做完，P1 只先立起目录与取值入口）。
"""
from __future__ import annotations

import importlib
import os
import sys

#: 已实现的平台目录名。新增平台 = 新增 app/platform/<name>/env.py，并在这里登记。
NAMES = ("win32", "darwin", "linux")


def current() -> str:
    """当前平台名。用 ``os.name`` 判定 Windows，其余看 ``sys.platform``。"""
    if os.name == "nt":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def module(name: str = ""):
    """取某个平台的 env 模块（默认当前平台）。"""
    return importlib.import_module("app.platform.%s.env" % (name or current()))


def defaults() -> dict:
    """当前平台的默认值。取不到就返回空字典——路径解析要能降级到通用值。"""
    try:
        return dict(getattr(module(), "PLATFORM_DEFAULTS", {}) or {})
    except Exception:
        return {}


def dangerous_prefixes() -> list:
    """当前平台下"不该放用户数据"的前缀，``[(前缀, 原因), ...]``（D21 校验用）。"""
    try:
        fn = getattr(module(), "dangerous_prefixes", None)
        return list(fn()) if callable(fn) else []
    except Exception:
        return []


# ---------------------------------------------------------------- 运行时原语（P3 收口）
# 下面这些原语把"业务代码里的平台分支"收进接缝：app/ 的其它模块只调用它们，
# 不得再自己写 os.name / LOCALAPPDATA / platform.system()（由 tests/test_path_seam.py 钉住）。
# 每个原语在 app/platform/<os>/env.py 里各实现一份；缺实现时给安全的默认值。

def _platform_fn(name):
    """取当前平台 env.py 里的函数；没有则 None（不抛，业务侧不必层层 try）。"""
    try:
        return getattr(module(), name, None)
    except Exception:
        return None


def display_name() -> str:
    """系统名（状态文案用）。"""
    fn = _platform_fn("display_name")
    return fn() if callable(fn) else current()


def no_window_creationflags() -> int:
    """起控制台子进程时抑制黑窗的 creationflags（非 Windows = 0）。"""
    fn = _platform_fn("no_window_creationflags")
    return int(fn()) if callable(fn) else 0


def isolates_audio_capture() -> bool:
    """音频采集是否必须隔离到可回收的子进程。

    macOS 的 CoreAudio 可能被虚拟/接力设备（Oray、iPhone 麦克风…）锁死，锁死后
    进程内无法自愈、开麦永久超时并空转占满 CPU（issue #15）。所以 darwin 上采集
    放进独立子进程，卡住可以直接 kill 释放设备；Windows/Linux 直接在进程内采集。
    这是平台分支，按 D12 只允许存在于接缝里。
    """
    return current() == "darwin"


def chromium_candidates():
    """Chromium 系浏览器可执行文件候选（调用方自行 expandvars / 判存在）。"""
    fn = _platform_fn("chromium_candidates")
    return list(fn()) if callable(fn) else []


def agent_cli_candidates():
    """CodeBuddy CLI 由平台专有安装位置带来的候选路径。"""
    fn = _platform_fn("agent_cli_candidates")
    return list(fn()) if callable(fn) else []


def tcp_excluded_port_range_output() -> str:
    """系统 TCP 保留端口段的原始命令输出（解析归 app/ports.py）。"""
    fn = _platform_fn("tcp_excluded_port_range_output")
    return str(fn()) if callable(fn) else ""


# ---------------------------------------------------------------- 运行时替换（P3 剩余搬迁）
# 这一组把 app/ 里最后一批"Windows 专有实现"收进接缝：子进程 flags、进程查询、
# 打开窗口、提示音、离线 TTS、桌面通知、边条可执行文件候选。业务代码只调这里，
# 于是 Windows 与 macOS 的差异只存在于 app/platform/<os>/ 下（D12）。

def detach_gui_kwargs() -> dict:
    """让 **GUI** 子进程脱离父进程组（边条进程）。缺实现时退化为空 dict。"""
    fn = _platform_fn("detach_gui_kwargs")
    return dict(fn()) if callable(fn) else {}


def detach_console_kwargs() -> dict:
    """控制台 helper 脱离父进程组（自我重启脚本）。缺实现时退化为空 dict。"""
    fn = _platform_fn("detach_console_kwargs")
    return dict(fn()) if callable(fn) else {}


def console_shell_argv(script: str):
    """起一个控制台脚本的 argv（Windows=PowerShell / POSIX=/bin/sh）。"""
    fn = _platform_fn("console_shell_argv")
    return list(fn(script)) if callable(fn) else []


def process_running(image_name: str) -> bool:
    """按名字查进程（边条是否已在运行）。查不到 = False，永不抛。"""
    fn = _platform_fn("process_running")
    return bool(fn(image_name)) if callable(fn) else False


def kill_process_tree(pid: int) -> bool:
    """杀掉整棵进程树（独立 harness 的 npx 会套 cmd→node→cmd→node，只杀最外层不够）。

    平台差异收在这里：Windows 用 ``taskkill /T /F``，POSIX 用进程组 SIGTERM。
    永不抛；返回"是否执行了杀动作"。
    """
    fn = _platform_fn("kill_process_tree")
    return bool(fn(int(pid))) if callable(fn) else False


def listening_pid(port: int) -> int:
    """谁在监听本机某个 TCP 端口（找不到 = 0）。用于"端口还在被占"时补一刀。"""
    fn = _platform_fn("listening_pid")
    return int(fn(int(port))) if callable(fn) else 0


def shell_open(target: str, params: str = "") -> bool:
    """用系统 shell 打开 URL / 可执行文件（非阻塞）。"""
    fn = _platform_fn("shell_open")
    return bool(fn(target, params)) if callable(fn) else False


def play_wav_async(path: str) -> bool:
    """异步播放一个 wav 文件（提示音）。"""
    fn = _platform_fn("play_wav_async")
    return bool(fn(path)) if callable(fn) else False


def offline_tts_speak(text: str, timeout: int = 60) -> bool:
    """离线朗读（Windows=SAPI / macOS=say）。"""
    fn = _platform_fn("offline_tts_speak")
    return bool(fn(text, timeout)) if callable(fn) else False


def offline_tts_label() -> str:
    """离线 TTS 的引擎短名（进状态文案：Windows=`sapi`、macOS=`say`）。"""
    fn = _platform_fn("offline_tts_label")
    return str(fn()) if callable(fn) else "sapi"


def offline_tts_display() -> str:
    """离线 TTS 的可读名字（面板文案，如 `Windows 慧慧`）。"""
    fn = _platform_fn("offline_tts_display")
    return str(fn()) if callable(fn) else "SAPI"


def notify(title: str, text: str) -> bool:
    """桌面通知（Windows=气泡 / macOS=osascript）。"""
    fn = _platform_fn("notify")
    return bool(fn(title, text)) if callable(fn) else False


def sidebar_candidates(install_root: str):
    """边条（右缘面板宿主）可执行文件候选；返回空列表 = 该平台没有原生边条，
    调用方应回落到"打开整窗"。"""
    fn = _platform_fn("sidebar_candidates")
    return list(fn(install_root)) if callable(fn) else []


def hotkey_impl():
    """当前平台的全局热键实现模块（``HotkeyListener`` + 常量）。

    Windows = ``app/platform/win32/hotkey.py``（ctypes + 低级键盘钩子）；
    macOS/Linux = ``app/platform/_posix_hotkey.py``（pynput）。
    ``app/hotkey.py`` 是门面，业务代码只 import 那个门面。
    """
    return importlib.import_module("app.platform.%s.hotkey" % current())


def model_install_command(name: str, fallback: str = "") -> str:
    """某个模型的一键安装命令（平台专有脚本；本平台没有就返回 ``fallback``）。

    声明式放在 ``PLATFORM_DEFAULTS["modelInstallCommands"]`` 里（D11 的用法），
    这样"面板上贴给用户跑的那条命令"也不会把平台特征串漏回业务代码。
    """
    table = defaults().get("modelInstallCommands") or {}
    return str(table.get(name) or fallback)


# ---------------------------------------------------------------- 配置默认值（D11）
# 平台差异不只体现在"环境默认值"上，也体现在**配置项的默认值与候选项**上：
#   * macOS 没有 CUDA → device 默认应为 cpu，不该让用户看到"auto 会挑 cuda"的假象；
#   * macOS 的离线朗读是 say，不是 Windows 的 SAPI → ttsEngine 候选项里不该出现 sapi；
#   * macOS 精简依赖不含 funasr → sttModel 默认要落在 whisper 档，否则静默转写失败。
# 这些以前散在 ``mac/run_mac.py`` 的"注入式覆盖 DEFAULTS"里（D17 要收掉的那类）；
# 现在改成**声明式**放在各平台 env.py 的 PLATFORM_DEFAULTS 里，由 app/config.py 消费。
# 声明缺失 = 用 ``config.DEFAULTS`` 里的 Windows 基准值，所以 Windows 行为零变化。

def setting_default(key: str):
    """该平台为某个配置项声明的默认值；没声明返回 ``None``（= 用 Windows 基准值）。"""
    table = defaults().get("settingDefaults") or {}
    return table.get(key)


def setting_options(key: str):
    """该平台为某个配置项声明的候选项（面板下拉用）；没声明返回 ``None``。"""
    table = defaults().get("settingOptions") or {}
    opts = table.get(key)
    return list(opts) if opts else None


def hf_executable(install_root: str) -> str:
    """venv 里的 hf 命令行入口。"""
    fn = _platform_fn("hf_executable")
    if callable(fn):
        return str(fn(install_root))
    return os.path.join(install_root, "venv", "bin", "hf")


def shell_script(hf: str, jobs) -> str:
    """把若干条 argv 渲染成该平台的 shell 下载脚本。"""
    fn = _platform_fn("shell_script")
    return str(fn(hf, jobs)) if callable(fn) else ""


def acquire_named_lock(lock_id: str, lock_path: str):
    """获取内核级单实例锁。返回 (handle, detail)。**失败必须显式上报，不能吞。**"""
    return module().acquire_named_lock(lock_id, lock_path)


def named_lock_held(lock_id: str, lock_path: str) -> bool:
    return bool(module().named_lock_held(lock_id, lock_path))


def release_named_lock(handle) -> None:
    return module().release_named_lock(handle)


# ---------------------------------------------------------------- 清单用的平台标记
# 组件清单（components/*.json 与 app/components.py 的内置清单）要声明"支持哪些平台"，
# 但**不能**在 app/ 的其它地方写出平台特征串（D12 契约）。所以清单统一用中立标记
# （win32 / macos / linux），由本模块负责翻译与版本探测——平台分支只留在接缝里。
MANIFEST_NAMES = {"win32": "win32", "darwin": "macos", "linux": "linux"}
MANIFEST_TO_INTERNAL = {v: k for k, v in MANIFEST_NAMES.items()}


def manifest_name(name: str = "") -> str:
    """内部平台名（= ``current()`` 的取值）→ 清单标记。已是标记则原样返回。"""
    if not name:
        return MANIFEST_NAMES.get(current(), current())
    if name in MANIFEST_TO_INTERNAL:
        return name
    return MANIFEST_NAMES.get(name, name)


def internal_name(token: str) -> str:
    """清单标记 → 内部平台名（``macos`` → ``darwin``）。"""
    return MANIFEST_TO_INTERNAL.get(token, token)


def os_version(name: str = "") -> tuple:
    """系统版本（用于清单的 ``min_os``）。取不到返回空元组 = 不做版本限制。

    ``name`` 收内部平台名；不给则当前平台。
    """
    n = name or current()
    try:
        import platform as _p
        if n == "darwin":
            return tuple(int(x) for x in _p.mac_ver()[0].split(".")[:3] if x.isdigit())
        if n == "win32":
            return tuple(int(x) for x in _p.version().split(".")[:3] if x.isdigit())
    except Exception:
        pass
    return ()


def gpu_info() -> dict:
    """显卡信息（首装向导用：决定推荐哪档转写、要不要显示"用显卡加速"）。

    返回 ``{"vendor","name","vramMb","driver","source"}``；**探测不到就返回空字段，不猜**
    —— 向导据此说"没检测到独立显卡"，而不是编一个型号出来。任何异常都不抛。
    """
    blank = {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": ""}
    fn = _platform_fn("gpu_info")
    if not callable(fn):
        return blank
    try:
        data = dict(fn() or {})
    except Exception:
        return blank
    out = dict(blank)
    for key in blank:
        if key in data:
            out[key] = data[key]
    return out


def node_dirs() -> list:
    """本机**可能**装着 node/npx 的目录（按可信度排序）。没有实现或都没有时返回空列表。

    用途：ECHO 由桌面快捷方式启动时，进程 PATH 里未必有 node（托管式 node 故意不写系统
    PATH），而独立 harness 靠 npx 起 —— 找不到就静默失败（2026-09-22 同事反馈 B1）。
    """
    fn = _platform_fn("node_dirs")
    if not callable(fn):
        return []
    try:
        return [str(d) for d in (fn() or []) if d]
    except Exception:
        return []
