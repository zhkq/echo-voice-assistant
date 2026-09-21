# -*- coding: utf-8 -*-
"""Linux 平台环境默认值 + 平台原语（非当前交付目标，先把接缝补齐）。

系统数据按 XDG：``$XDG_DATA_HOME/ECHO``，未设置则 ``~/.local/share/ECHO``。
"""
import os

from app.platform import _posix

NAME = "linux"

PLATFORM_DEFAULTS = {
    "dataDir": os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"),
        "ECHO"),
    # ---- 配置项的默认值与候选项（D11）----
    # Linux 不是交付目标，但接缝要齐：离线朗读走 spd-say/espeak（不是 SAPI）；
    # device 保持不覆盖（Linux 机器可能有 NVIDIA GPU，"auto" 语义正确）。
    "settingOptions": {
        "ttsEngine": ["auto", "edge-tts", "espeak", "off"],
    },
}


def dangerous_prefixes():
    return [
        ("/usr", "不能放在系统目录下"),
        ("/etc", "不能放在系统目录下"),
        ("/boot", "不能放在系统目录下"),
        ("/sys", "不能放在系统目录下"),
        ("/proc", "不能放在系统目录下"),
    ]


def display_name() -> str:
    """状态文案里的系统名（与 ``platform.system()`` 在 Linux 上的取值一致）。"""
    return "Linux"


def chromium_candidates():
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ]

# ------------------------------------------------------------------ 展示 / 子进程

def no_window_creationflags() -> int:
    return 0


# ------------------------------------------------------------------ 运行时替换（P3）
# Linux 不是当前交付目标，但接缝要齐（守卫测试会断言三个平台实现同一组原语）。

def detach_gui_kwargs() -> dict:
    return _posix.detach_kwargs()


def detach_console_kwargs() -> dict:
    return _posix.detach_kwargs()


def console_shell_argv(script: str):
    return _posix.console_shell_argv(script)


def process_running(image_name: str) -> bool:
    return _posix.process_running(image_name)


def shell_open(target: str, params: str = "") -> bool:
    return _posix.shell_open(target, params, opener=("xdg-open",))


def play_wav_async(path: str) -> bool:
    """提示音：桌面环境里 paplay/aplay 不一定都装了，逐个试。"""
    return _posix.play_wav_async(path, players=(("paplay",), ("aplay",), ("ffplay", "-nodisp", "-autoexit")))


def offline_tts_speak(text: str, timeout: int = 60) -> bool:
    return _posix.offline_tts_speak(
        text, timeout, engines=(("spd-say",), ("espeak-ng",), ("espeak",)))


def offline_tts_label() -> str:
    """引擎短名（进状态文案；Windows 那边是 `sapi`）。"""
    return "espeak"


def offline_tts_display() -> str:
    return "eSpeak / spd-say"


def notify(title: str, text: str) -> bool:
    """桌面通知：尽量用 ``notify-send``。"""
    import subprocess
    try:
        subprocess.Popen(["notify-send", str(title), str(text)], close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def sidebar_candidates(install_root: str):
    """Linux 上没有原生边条宿主，调用方回落到"打开整窗"。"""
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
    """显卡信息（首装向导用）。与 Windows 同一条路：只问 ``nvidia-smi``，探不到就返回空。

    这里**不用** lspci：它给不出显存与驱动版本，而向导的推荐（"显存 <4 GB 就别装重档"）
    恰恰依赖这两个数。
    """
    import shutil
    import subprocess

    out = {"vendor": "", "name": "", "vramMb": 0, "driver": "", "source": ""}
    exe = shutil.which("nvidia-smi")
    if not exe:
        return out
    try:
        raw = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
    except Exception:
        return out
    rows = [r for r in (raw.stdout or "").splitlines() if r.strip()]
    if not rows:
        return out
    parts = [p.strip() for p in rows[0].split(",")]
    if len(parts) >= 3:
        try:
            vram = int(float(parts[1] or 0))
        except ValueError:
            vram = 0
        out.update({"vendor": "nvidia", "name": parts[0], "vramMb": vram,
                    "driver": parts[2], "source": "nvidia-smi"})
    return out


def node_dirs():
    """可能装着 node/npx 的目录（按可信度排序）。见 win32 同名的说明。"""
    home = os.path.expanduser("~")
    out = ["/usr/local/bin", "/usr/bin", "/bin"]
    nvm = os.path.join(home, ".nvm", "versions", "node")
    try:
        out += [os.path.join(nvm, v, "bin") for v in sorted(os.listdir(nvm), reverse=True)]
    except OSError:
        pass
    out.append(os.path.join(home, ".volta", "bin"))
    return [d for d in out if d and os.path.isdir(d)]
