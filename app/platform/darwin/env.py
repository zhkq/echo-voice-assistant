# -*- coding: utf-8 -*-
"""macOS 平台环境默认值 + 平台原语（D18、D12）。

系统数据放 ``~/Library/Application Support/ECHO``：``.app`` 内部不可写、也不该写，
而且签名封条不允许往 bundle 里塞运行时数据。

注意：mac 侧还有一层"注入式入口"（``mac/run_mac.py``）负责热键/边条/TTS 等运行时替换，
那是 P3 才收拢的；本文件负责"环境默认值 + 平台原语"，业务代码只通过
``app/platform/__init__.py`` 取用。
"""
import os

from app.platform import _posix

NAME = "darwin"

PLATFORM_DEFAULTS = {
    "dataDir": os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ECHO"),
    # ---- 配置项的默认值与候选项（D11）----
    # 这些值原来散在 ``mac/run_mac.py`` 的"注入式覆盖 DEFAULTS"里（D17 要收掉的那类），
    # 现改为声明式，由 app/config.py 在 seed/reset/get 时消费（Windows 行为不受影响）。
    "settingDefaults": {
        # Mac 没有 CUDA：别让 "auto" 给人一种"会挑到 cuda"的错觉
        "device": "cpu",
        # mac 的精简依赖（mac/requirements-mac.txt）不含 funasr → 默认必须落在 whisper 档，
        # 否则 sensevoice/qwen3asr 会静默转写失败
        "sttModel": "base",
        "meetingSttModel": "small",
    },
    "settingOptions": {
        # 离线朗读在 macOS 上是 say，不是 Windows 的 SAPI
        "ttsEngine": ["auto", "edge-tts", "say", "off"],
    },
}


def dangerous_prefixes():
    return [
        ("/System", "不能放在系统目录下"),
        ("/Library", "不能放在系统目录下"),
        ("/Applications", "不能放在应用程序目录下"),
        ("/usr", "不能放在系统目录下"),
    ]


def display_name() -> str:
    """状态文案里的系统名（与 ``platform.system()`` 在 macOS 上的取值一致）。"""
    return "Darwin"


def chromium_candidates():
    return [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]

# ------------------------------------------------------------------ 展示 / 子进程

def no_window_creationflags() -> int:
    return 0


# ------------------------------------------------------------------ 运行时替换（P3）
# ⚠️ 未在 macOS 上实测（缺机器，见 REFACTOR-PLAN §9.1 的 S7/S9/S10）：契约与 Windows
# 一致、永不抛异常；能不能真跑起来要等实测。

def detach_gui_kwargs() -> dict:
    return _posix.detach_kwargs()


def detach_console_kwargs() -> dict:
    return _posix.detach_kwargs()


def console_shell_argv(script: str):
    return _posix.console_shell_argv(script)


def process_running(image_name: str) -> bool:
    return _posix.process_running(image_name)


def shell_open(target: str, params: str = "") -> bool:
    return _posix.shell_open(target, params, opener=("open",))


def play_wav_async(path: str) -> bool:
    """提示音：macOS 自带 ``afplay``。"""
    return _posix.play_wav_async(path, players=(("afplay",),))


def offline_tts_speak(text: str, timeout: int = 60) -> bool:
    """离线 TTS：macOS 自带 ``say``（中文音色取决于系统已装语音）。"""
    return _posix.offline_tts_speak(text, timeout, engines=(("say",),))


def offline_tts_label() -> str:
    """引擎短名（进状态文案；Windows 那边是 `sapi`）。"""
    return "say"


def offline_tts_display() -> str:
    return "macOS say"


def notify(title: str, text: str) -> bool:
    """桌面通知：``osascript display notification``。"""
    import subprocess
    esc = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')   # noqa: E731
    script = 'display notification "%s" with title "%s"' % (esc(text), esc(title))
    try:
        subprocess.Popen(["osascript", "-e", script], close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def sidebar_candidates(install_root: str):
    """mac 原生边条宿主属于 P3 未完成部分（D19 常驻 helper），暂不提供候选：
    返回空表会让调用方回落到"打开整窗面板"（``shell_open``），功能可用。"""
    return []


def agent_cli_candidates():
    """CodeBuddy CLI 的内置候选：本平台没有，返回空表。"""
    return []


def tcp_excluded_port_range_output() -> str:
    """本平台没有 Windows 那套 TCP 保留端口段，返回空串。"""
    return ""


# ------------------------------------------------------------------ HuggingFace 下载脚本

def hf_executable(install_root: str) -> str:
    return os.path.join(install_root, "venv", "bin", "hf")


def shell_script(hf: str, jobs) -> str:
    """把若干条 argv 渲染成 POSIX sh 下载脚本。"""
    import shlex
    lines = []
    for argv in jobs:
        lines.append("HF_ENDPOINT=https://huggingface.co HF_HUB_OFFLINE=0 "
                     + shlex.join([str(x) for x in argv]))
    return " &&\n".join(lines)


# ------------------------------------------------------------------ 单实例锁（flock）

def acquire_named_lock(lock_id, lock_path):
    return _posix.acquire(lock_path)


def named_lock_held(lock_id, lock_path) -> bool:
    return _posix.held(lock_path)


def release_named_lock(handle):
    return _posix.release(handle)


def gpu_info():
    """显卡信息（首装向导用）。

    macOS 上 ECHO 目前**没有**可用的加速路径（CUDA 组件只声明 win32/linux），
    所以这里如实返回"未知"，不编造型号 —— 向导据此不显示"用显卡加速"那一步。
    """
    return {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": ""}


def node_dirs():
    """可能装着 node/npx 的目录（按可信度排序）。见 win32 同名的说明。"""
    home = os.path.expanduser("~")
    out = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
    nvm = os.path.join(home, ".nvm", "versions", "node")
    try:
        out += [os.path.join(nvm, v, "bin") for v in sorted(os.listdir(nvm), reverse=True)]
    except OSError:
        pass
    out.append(os.path.join(home, ".volta", "bin"))
    return [d for d in out if d and os.path.isdir(d)]


# ---------------------------------------------------------------- 秘密保护（凭据落盘）
# POSIX 共享实现：落盘靠文件权限（见 _posix.py 里那段取舍说明）。

def protect_secret_kind() -> str:
    return _posix.protect_secret_kind()


def protect_secret(data: bytes) -> bytes:
    return _posix.protect_secret(data)


def unprotect_secret(blob: bytes) -> bytes:
    return _posix.unprotect_secret(blob)


def restrict_file(path: str) -> None:
    return _posix.restrict_file(path)
