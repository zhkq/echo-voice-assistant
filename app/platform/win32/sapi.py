# -*- coding: utf-8 -*-
"""Windows 离线 TTS（SAPI / System.Speech）实现 —— 平台专有，收在接缝里。

从 ``app/audio/tts.py`` 原样搬来（P3 剩余搬迁）：那里的其它部分（引擎选择、
edge-tts 在线合成、soundfile/sounddevice 播放）都是跨平台的，只有这段
"起 PowerShell 调 System.Speech" 是 Windows 专有。

为什么用常驻子进程：每句话都新起一次 powershell 要 ~1s 开销（老用户能听出来），
所以开一个常驻进程、用 stdin 逐句喂文本、读一行 ``ACK`` 当"读完了"。

编码坑（已在 1.x 踩过）：stdin 以 UTF-8 传递，PowerShell 里显式设
``[Console]::InputEncoding = UTF8``，否则中文被按 GBK 解码成乱码
（现象：英文正常、中文乱码）。
"""
import atexit
import subprocess
import threading

# 常驻进程句柄 / ACK 队列
_lock = threading.Lock()
_proc = None
_queue = None

_SAPI_PS = (
    "Add-Type -AssemblyName System.Speech; "
    "[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false); "
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$zh = @($s.GetInstalledVoices() | Where-Object { $_.Enabled -and "
    "$_.VoiceInfo.Culture.Name -like 'zh*' })[0]; "
    "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }; "
    "$s.Rate = 1; "
    "while (($line = [Console]::In.ReadLine()) -ne $null) { "
    "$s.Speak($line); [Console]::Out.WriteLine('ACK'); [Console]::Out.Flush() }"
)


def no_window_creationflags():
    from app.platform.win32.env import no_window_creationflags as _f
    return _f()


def _reader(proc, q):
    try:
        for line in proc.stdout:
            q.put(line.strip())
    except Exception:
        pass


def kill():
    """杀掉常驻 SAPI 进程（进程退出时由 atexit 调用）。"""
    global _proc
    p = _proc
    _proc = None
    if p is not None:
        try:
            p.kill()
        except Exception:
            pass


atexit.register(kill)


def _ensure():
    global _proc, _queue
    if _proc is not None and _proc.poll() is None:
        return _proc, _queue
    import queue
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_PS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", creationflags=no_window_creationflags())
    q = queue.Queue()
    threading.Thread(target=_reader, args=(proc, q), daemon=True).start()
    _proc, _queue = proc, q
    return proc, q


def _speak_persistent(text):
    with _lock:
        try:
            proc, q = _ensure()
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            try:
                q.get(timeout=60)   # 收到 ACK 表示朗读完成
                return True
            except Exception:
                kill()
                return False
        except Exception:
            kill()
            return False


def _speak_once(text):
    """一次性 PowerShell SAPI（常驻进程失败时的兜底）。"""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false); "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$zh = @($s.GetInstalledVoices() | Where-Object { $_.Enabled -and "
        "$_.VoiceInfo.Culture.Name -like 'zh*' })[0]; "
        "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }; "
        "$s.Rate = 1; $s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
    )
    try:
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            stdin=subprocess.PIPE, creationflags=no_window_creationflags())
        p.communicate(text.encode("utf-8"), timeout=60)
        return p.returncode == 0
    except Exception:
        return False


def speak(text, timeout=60):
    """朗读文本：常驻进程优先，失败回退一次性子进程。永不抛异常。"""
    if not (text or "").strip():
        return False
    if _speak_persistent(text):
        return True
    return _speak_once(text)


def label():
    """面板/日志里的引擎名（与 1.x 文案一致）。"""
    return "Windows 慧慧"
