# -*- coding: utf-8 -*-
"""tts.py — ECHO 语音合成与提示音（Windows）

引擎：
  edge-tts  微软在线合成（自然女声），需能访问 speech.platform.bing.com
  sapi      Windows 自带 System.Speech（离线，中文语音）
  auto      优先 edge-tts，失败自动降级 sapi
  off       不播报

播放：edge-tts 产出 mp3 → soundfile 解码（libsndfile 内置 mp3 支持），
      失败回退 ffmpeg 转 wav；最终用 sounddevice 播放（不依赖控制台）。

提示音：assets/beeps/*.wav，winsound 异步播放。
"""
import atexit
import os
import subprocess
import threading
import tempfile
import time

from app import paths

# 提示音 wav 随**代码**走（assets/ 是安装目录的一部分），所以取安装根而不是数据根；
# 来源统一由路径层给（含 ECHO_ROOT 覆盖），本模块不再自己推导。
BASE_DIR = paths.echo_root()
BEEPS_DIR = os.path.join(BASE_DIR, "assets", "beeps")

_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
_edge_broken = False
_lock = threading.Lock()


def play_beep(name):
    """播放 assets/beeps/<name>.wav（start/done/ok/err…）。"""
    wav = os.path.join(BEEPS_DIR, name + ".wav")
    if not os.path.isfile(wav):
        return
    try:
        import winsound
        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        pass


def _play_wav_data(data, sr):
    try:
        import sounddevice as sd
        sd.play(data, sr)
        sd.wait()
    except Exception:
        pass


def _play_media_file(path):
    try:
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32")
        _play_wav_data(data, sr)
        return True
    except Exception:
        pass
    # 回退：ffmpeg 转 wav（系统已装 ffmpeg）
    try:
        out = path + ".wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, out],
                       timeout=30, creationflags=0x08000000)
        if os.path.isfile(out):
            import soundfile as sf
            data, sr = sf.read(out, dtype="float32")
            _play_wav_data(data, sr)
            os.remove(out)
            return True
    except Exception:
        pass
    return False


def _speak_edge(text, timeout=30):
    """edge-tts 合成并播放；失败返回 False。

    用 asyncio.wait_for 限制整体耗时（edge 服务不可达时 6 秒内快速
    失败回退 SAPI，避免"空等 20 秒"）。
    """
    import edge_tts
    mp3 = os.path.join(tempfile.gettempdir(), f"echo-tts-{os.getpid()}.mp3")
    try:
        import asyncio
        async def _gen():
            com = edge_tts.Communicate(text, _EDGE_VOICE)
            await com.save(mp3)
        asyncio.run(asyncio.wait_for(_gen(), timeout=min(timeout, 6)))
        if os.path.isfile(mp3) and os.path.getsize(mp3) > 0:
            return _play_media_file(mp3)
        return False
    except Exception:
        return False
    finally:
        try:
            if os.path.isfile(mp3):
                os.remove(mp3)
        except Exception:
            pass


# ---- 常驻 SAPI（避免每句话都新起 powershell 子进程的 ~1s 开销）----
_sapi_lock = threading.Lock()
_sapi_proc = None
_sapi_q = None

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


def _sapi_reader(proc, q):
    try:
        for line in proc.stdout:
            q.put(line.strip())
    except Exception:
        pass


def _sapi_kill():
    global _sapi_proc
    p = _sapi_proc
    _sapi_proc = None
    if p is not None:
        try:
            p.kill()
        except Exception:
            pass


atexit.register(_sapi_kill)


def _sapi_ensure():
    global _sapi_proc, _sapi_q
    if _sapi_proc is not None and _sapi_proc.poll() is None:
        return _sapi_proc, _sapi_q
    import queue
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SAPI_PS],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", creationflags=0x08000000)
    q = queue.Queue()
    threading.Thread(target=_sapi_reader, args=(proc, q), daemon=True).start()
    _sapi_proc, _sapi_q = proc, q
    return proc, q


def _speak_sapi_persistent(text):
    with _sapi_lock:
        try:
            proc, q = _sapi_ensure()
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            try:
                q.get(timeout=60)   # 收到 ACK 表示朗读完成
                return True
            except Exception:
                _sapi_kill()
                return False
        except Exception:
            _sapi_kill()
            return False


def _speak_sapi_once(text):
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
            stdin=subprocess.PIPE, creationflags=0x08000000)
        p.communicate(text.encode("utf-8"), timeout=60)
        return p.returncode == 0
    except Exception:
        return False


def _speak_sapi(text):
    """Windows 离线 SAPI（中文语音）：常驻进程优先，失败回退一次性子进程。

    注意编码：stdin 以 UTF-8 传递，PowerShell 里显式设置 InputEncoding=UTF8，
    否则中文会被按 GBK 解码成乱码（现象：英文正常、中文乱码）。
    """
    if _speak_sapi_persistent(text):
        return True
    return _speak_sapi_once(text)


def speak(text, engine="auto", timeout=60):
    """朗读文本（阻塞，最长 timeout 秒）。engine: auto|edge-tts|sapi|off"""
    global _edge_broken
    if not text:
        return False
    if engine == "off":
        return False
    if engine == "sapi" or (engine == "auto" and _edge_broken):
        return _speak_sapi(text)
    if engine == "auto" and probe_online() is False:
        # 在线 TTS 探针不可用 → 直接本地 SAPI（跳过 edge 等待，保证效率）
        return _speak_sapi(text)
    ok = _speak_edge(text, timeout)
    if ok:
        return True
    _edge_broken = True
    if engine != "edge-tts":
        return _speak_sapi(text)
    return False


# ---------------------------------------------------------------- 在线 TTS 探针

_EDGE_HOST = "speech.platform.bing.com"
_edge_ok = None          # None=未探测 / True / False
_edge_ok_lock = threading.Lock()
_edge_probe_ts = 0.0
_PROBE_TTL = 300         # 探针结果有效期（秒）


def probe_online(force=False):
    """探测在线 TTS（edge-tts）可达性。

    用 TCP 连接 speech.platform.bing.com:443（3 秒超时）判断；
    结果缓存 _PROBE_TTL 秒，force=True 强制重测。
    返回 True/False。
    """
    global _edge_ok, _edge_probe_ts
    now = time.monotonic()
    if not force and _edge_ok is not None and now - _edge_probe_ts < _PROBE_TTL:
        return _edge_ok
    ok = False
    try:
        import socket
        s = socket.create_connection((_EDGE_HOST, 443), timeout=3)
        s.close()
        ok = True
    except Exception:
        ok = False
    with _edge_ok_lock:
        _edge_ok = ok
        _edge_probe_ts = time.monotonic()
    return ok


def tts_online_status():
    """面板状态用：返回 {'online': bool|None, 'engine': 'edge-tts'|'sapi', 'detail': str}"""
    ok = probe_online()
    if ok is True:
        return {"online": True, "engine": "edge-tts", "detail": "edge-tts 在线"}
    return {"online": False, "engine": "sapi", "detail": "sapi 本地（在线不可用）"}


def speak_async(text, engine="auto"):
    """后台线程朗读，不阻塞调用方。"""
    threading.Thread(target=speak, args=(text, engine), daemon=True).start()


def beep_ok():
    play_beep("ok")
    import time
    time.sleep(0.08)
    play_beep("ok2")
