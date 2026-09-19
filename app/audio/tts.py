# -*- coding: utf-8 -*-
"""tts.py — ECHO 语音合成与提示音（跨平台；平台专有实现收在 app/platform/）

引擎（名称沿用 1.x 的配置值，不动配置兼容性）：
  edge-tts  微软在线合成（自然女声），需能访问 speech.platform.bing.com
  sapi      离线合成：Windows = System.Speech（中文音色），macOS = say
  auto      优先 edge-tts，失败自动降级离线引擎
  off       不播报

播放：edge-tts 产出 mp3 → soundfile 解码（libsndfile 内置 mp3 支持），
      失败回退 ffmpeg 转 wav；最终用 sounddevice 播放（不依赖控制台）。

提示音：assets/beeps/*.wav，播放实现由平台接缝给（Windows = winsound 异步播放）。
  实测记录（2026-09-19）：五个提示音文件都正常；但 **90 ms 的 done.wav 会被这条
  老通路（MME/waveOut）吞掉**，而 120 ms 以上的都能听到 —— 详见 docs/2.0-PROGRESS §34。
"""
import atexit
import os
import subprocess
import threading
import tempfile
import time

from app import paths
from app import platform as echo_platform

# 提示音 wav 随**代码**走（assets/ 是安装目录的一部分），所以取安装根而不是数据根；
# 来源统一由路径层给（含 ECHO_ROOT 覆盖），本模块不再自己推导。
BASE_DIR = paths.echo_root()
BEEPS_DIR = os.path.join(BASE_DIR, "assets", "beeps")

_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
_edge_broken = False
_lock = threading.Lock()


def play_beep(name):
    """播放 assets/beeps/<name>.wav（start/done/ok/err…）。返回是否**成功播出**。

    播放实现是平台差异（Windows=winsound 异步、macOS=afplay），收在接缝里。

    ⚠️ 2026-09-19 起不再静默：原实现"文件不存在直接 return、异常 pass"，
    表现和"设备没声音"完全一样，导致"提示音到底响没响"查了两天。现在：
      * 文件不存在 / 播放失败（接缝返回 False）都写一条 ``debug`` 日志（source=tts）；
      * 返回值透出给调用方（探针据此区分"播了没响"与"根本没播"）。
    仍然**不抛异常** —— 提示音永远不该影响录音主流程。
    """
    wav = os.path.join(BEEPS_DIR, name + ".wav")
    if not os.path.isfile(wav):
        _log_beep(name, wav, "文件不存在")
        return False
    try:
        ok = bool(echo_platform.play_wav_async(wav))
    except Exception as e:
        _log_beep(name, wav, "播放异常: %s" % e)
        return False
    if not ok:
        _log_beep(name, wav, "播放失败（接缝返回 False）")
    return ok


def _log_beep(name, path, why):
    """提示音诊断日志（写 DB logs 表；失败也无所谓）。"""
    try:
        import app.db as db
        db.add_log("debug", "tts", "提示音 %s：%s（%s）" % (name, why, path))
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
                       timeout=30, creationflags=echo_platform.no_window_creationflags())
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


# ---- 离线 TTS（Windows=SAPI / macOS=say）----
# 实现整段收在接缝里（P3 剩余搬迁）：常驻子进程、编码、Voice 探测都是平台专有细节。
def _speak_offline(text):
    """离线朗读；失败返回 False（永不抛）。"""
    return echo_platform.offline_tts_speak(text)


def _offline_engine_ids():
    """"离线引擎"的配置值集合。

    ``sapi`` 是 1.x 的配置值（Windows 的 System.Speech），库里可能存着，必须继续认；
    macOS/Linux 的离线引擎 id 由接缝给（``say`` / ``espeak``），面板下拉里出现的也是它。
    所以判断"用户选了离线引擎"要同时认这两个来源，否则在 mac 上选 say 会先走一遍在线合成。
    """
    ids = {"sapi"}
    try:
        label = str(echo_platform.offline_tts_label() or "").strip()
        if label:
            ids.add(label)
    except Exception:
        pass
    return ids


def speak(text, engine="auto", timeout=60):
    """朗读文本（阻塞，最长 timeout 秒）。engine: auto|edge-tts|<离线引擎>|off

    ``<离线引擎>`` = Windows ``sapi`` / macOS ``say`` / Linux ``espeak``（由接缝给），
    命名保持不变以免动配置兼容性。
    """
    global _edge_broken
    if not text:
        return False
    if engine == "off":
        return False
    offline = _offline_engine_ids()
    if engine in offline or (engine == "auto" and _edge_broken):
        return _speak_offline(text)
    if engine == "auto" and probe_online() is False:
        # 在线 TTS 探针不可用 → 直接本地离线合成（跳过 edge 等待，保证效率）
        return _speak_offline(text)
    ok = _speak_edge(text, timeout)
    if ok:
        return True
    _edge_broken = True
    if engine != "edge-tts":
        return _speak_offline(text)
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
    """面板状态用：返回 {'online': bool, 'engine': str, 'detail': str}

    ``engine`` 在 Windows 上仍是 ``sapi``（与 1.x 相同）；离线实现的名字由接缝给，
    macOS 上会是 ``say``。
    """
    ok = probe_online()
    if ok is True:
        return {"online": True, "engine": "edge-tts", "detail": "edge-tts 在线"}
    return {"online": False, "engine": echo_platform.offline_tts_label(),
            "detail": "%s 本地（在线不可用）" % echo_platform.offline_tts_label()}


def speak_async(text, engine="auto"):
    """后台线程朗读，不阻塞调用方。"""
    threading.Thread(target=speak, args=(text, engine), daemon=True).start()


def beep_ok():
    """「已发送」确认音：ok → ok2 两连音。

    注意间隔：异步播放通路里**下一次 PlaySound 会打断上一次**，所以间隔必须 ≥ 第一个音的
    时长，否则 ok 会被 ok2 拦腰截断（原来固定 0.08s，而提示音加长到 0.22s 之后就是把
    ok 切掉了 3/4）。现在按"文件实际时长 + 余量"来等。
    """
    play_beep("ok")
    time.sleep(_beep_seconds("ok") + 0.04)
    play_beep("ok2")


def _beep_seconds(name, default=0.22):
    """提示音时长（读文件头；读不到就用默认值）。"""
    try:
        import wave
        with wave.open(os.path.join(BEEPS_DIR, name + ".wav"), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return default
