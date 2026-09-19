# -*- coding: utf-8 -*-
"""recorder.py — ECHO 录音（sounddevice，16k 单声道 int16）

两类用途：
  record_command()      单次命令录音：开口检测 + 静音自动停止（同类语义）
  MeetingRecorder       会议录音线程：持续录，按 segment_minutes 自动分段，
                        实时输出音量电平（供面板波形）

命令录音失败返回 False；会议录音失败保存已收到的音频并记录 error。
"""
import os
import threading
import time
import wave

import numpy as np
from app import platform as echo_platform
from app.audio.mic import input_stream

SAMPLE_RATE = 16000

# 名字里带这些词的，基本都是"虚拟 / 映射 / 回环 / 汇总"设备：**兜底遍历**时不主动去开它们。
# 为什么：macOS 上打开这类设备可能把 CoreAudio HAL 卡死（issue #15：麦克风永久超时 + CPU 400%），
# Windows 上则常见"录到静音或系统声音"（本机 idx0 就是「Microsoft 声音映射器」）。
# 注意：只过滤兜底遍历；用户显式指定的设备、以及系统默认输入都不受此限制。
_VIRTUAL_HINTS = ("声音映射器", "sound mapper", "映射器", "立体声混音", "stereo mix",
                  "virtual", "虚拟", "voicemeeter", "vb-audio", "cable", "loopback",
                  "blackhole", "soundflower", "aggregate", "汇总", "oray",
                  "iphone", "continuity", "接力")


def _is_virtual_device(name):
    """按设备名粗判虚拟/映射/回环/汇总设备（只用于兜底遍历的跳过）。"""
    low = (name or "").lower()
    return any(h in low for h in _VIRTUAL_HINTS)


def _open_input(device_id=-1, blocksize=0):
    """打开可用的输入流。

    macOS 只尝试指定设备或系统默认输入，不遍历其它设备。
    其它平台在首选失败后遍历非虚拟输入设备；
    只要「能打开」就用（不做环境电平筛选——蓝牙耳机麦克风静音时
    电平极低是正常的，误判会跳过用户实际使用的设备）。

    默认输入用 device=None 交给 PortAudio 选，**不要**用 sd.default.device
    的下标硬取：该 pair 是 (输入, 输出)，早期代码误取 [1]（输出设备）当输入，
    还会把真正的默认输入排除掉，于是去开虚拟/接力设备（Oray、iPhone 麦克风），
    在 macOS 上可能把 CoreAudio HAL 卡死。见 app/audio/wake.py 同样结论。

    兜底遍历会跳过虚拟/映射/回环设备（见 _VIRTUAL_HINTS）：issue #15 就是被这类设备
    卡死的；显式指定与系统默认两条路径不跳过（尊重用户/系统的选择）。
    """
    import sounddevice as sd

    def _try_open(idx):
        return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                              device=idx, blocksize=blocksize or 0)

    # macOS must never probe unrelated devices automatically: even opening a
    # Continuity/virtual device can wedge CoreAudio. Explicit choices still work.
    first = device_id if device_id is not None and device_id >= 0 else None
    try:
        return _try_open(first)
    except Exception as exc:
        if echo_platform.isolates_audio_capture():
            raise RuntimeError("无法打开所选麦克风，请在系统设置中检查输入设备和权限；"
                               "未自动尝试其他设备：" + str(exc)) from exc

    candidates = []
    if device_id is not None and device_id >= 0:
        candidates.append(device_id)          # 显式指定：即使是虚拟设备也照用
    else:
        candidates.append(None)               # 系统默认输入（尊重系统设置）
    skipped_virtual = []
    for d in sd.query_devices():
        if d["max_input_channels"] <= 0:
            continue
        if _is_virtual_device(d.get("name")):
            skipped_virtual.append(d["index"])
            continue
        candidates.append(d["index"])
    if skipped_virtual:
        _log_record(-1, 0.0, False,
                    f"兜底遍历跳过 {len(skipped_virtual)} 个虚拟/映射设备: {skipped_virtual[:6]}")

    seen = {first}
    for idx in candidates:
        if idx in seen:
            continue
        seen.add(idx)
        try:
            return _try_open(idx)
        except Exception as e:
            _log_record(idx, 0, False, f"设备打开失败: {str(e)[:60]}")
            continue
    if skipped_virtual:
        raise RuntimeError(
            f"没有可用的输入设备（已跳过 {len(skipped_virtual)} 个虚拟/映射设备：{skipped_virtual[:4]}）")
    raise RuntimeError("没有可用的输入设备")


def list_input_devices():
    import sounddevice as sd
    return [{"index": d["index"], "name": d["name"], "channels": d["max_input_channels"]}
            for d in sd.query_devices() if d["max_input_channels"] > 0]


def default_input_device():
    import sounddevice as sd
    try:
        return sd.default.device[0]  # pair=(输入, 输出)，[0] 才是默认输入
    except Exception:
        return None


def _write_wav(path, frames, sr=SAMPLE_RATE):
    data = np.concatenate(frames) if isinstance(frames, list) else frames
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.ascontiguousarray(data).tobytes())


def record_command(out_path, max_ms=30000, silence_threshold=0.012,
                   hangover_ms=1100, no_speech_abort_ms=4000,
                   device_id=-1, level_cb=None, stop_event=None):
    """单次命令录音：检测到声音开始，静音 hangover 后停止。

    返回 True=录到有效音频；False=无语音/被中断。out_path 写入 16k 单声道 wav。
    level_cb(level01) 每帧回调（面板波形）；stop_event 置位立即停止。
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    max_frames = int(max_ms / 1000 * SAMPLE_RATE)
    hangover_frames = int(hangover_ms / 1000 * SAMPLE_RATE)
    abort_frames = int(no_speech_abort_ms / 1000 * SAMPLE_RATE)
    frames = []
    talking = False
    silent_run = 0
    max_rms = 0.0
    device_used = None
    try:
        with input_stream(device_id) as stream:
            device_used = stream.device
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False
                data, _ = stream.read(int(SAMPLE_RATE * 0.1))
                a = data.astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(a * a)))
                max_rms = max(max_rms, rms)
                if level_cb:
                    try:
                        level_cb(min(1.0, rms * 30.0))
                    except Exception:
                        pass
                if not talking:
                    if rms > silence_threshold:
                        talking = True
                        silent_run = 0
                        frames.append(data.copy())
                    else:
                        silent_run += len(data)
                        if silent_run > abort_frames:
                            _log_record(device_used, max_rms, talking, "开口前无语音放弃")
                            return False   # 开口前太久没声音 → 放弃
                else:
                    frames.append(data.copy())
                    if rms <= silence_threshold:
                        silent_run += len(data)
                        if silent_run > hangover_frames or sum(len(f) for f in frames) > max_frames:
                            break
                    else:
                        silent_run = 0
                    if sum(len(f) for f in frames) > max_frames:
                        break
        total_samples = sum(len(f) for f in frames)
        if total_samples < int(0.15 * SAMPLE_RATE):
            _log_record(device_used, max_rms, talking,
                        f"录音过短 {total_samples}样本/{(total_samples/16000):.2f}s")
            return False
        _write_wav(out_path, frames)
        _log_record(device_used, max_rms, talking,
                    f"录音完成 {(total_samples / SAMPLE_RATE):.1f}s")
        return True
    except Exception as e:
        print(f"[recorder] 录音失败: {e}")
        _log_record(device_used, max_rms, talking, f"录音异常: {e}")
        return False


def _log_record(device, max_rms, talking, msg):
    """录音诊断日志（写 DB logs 表）。"""
    try:
        import app.db as db
        db.add_log("debug", "recorder",
                   f"{msg} | 设备={device} | 峰值RMS={max_rms:.4f} | talking={talking}")
    except Exception:
        pass


class MeetingRecorder:
    """会议录音线程：按 segment_minutes 自动分段写 wav，实时电平回调。"""

    def __init__(self, folder, segment_minutes=10, device_id=-1, level_cb=None):
        self.folder = folder
        self.segment_minutes = segment_minutes
        self.device_id = device_id
        self.level_cb = level_cb
        self.stop_event = threading.Event()
        self.thread = None
        self.level = 0.0
        self.segments = []
        self.started_at = None
        # 录音线程是否已成功打开输入流 / 失败原因（供 start 后同步校验，
        # 避免"麦打不开但界面显示录音中、最后留下空会议"）
        self.error = None
        self._started = threading.Event()

    def start(self):
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def wait_started(self, timeout=5.0):
        """等录音线程真正打开输入流；成功返回 True，打不开（或超时）返回 False。"""
        self._started.wait(timeout)
        return self._started.is_set() and not self.error

    def stop(self, timeout=8):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=timeout)
            return not self.thread.is_alive()
        return True

    def _loop(self):
        seg_samples = int(self.segment_minutes * 60 * SAMPLE_RATE)
        seg_idx = 0
        writer = None
        audio_file = None
        samples = 0

        def close_segment():
            nonlocal writer, audio_file
            try:
                if writer is not None:
                    writer.close()
            finally:
                writer = None
                if audio_file is not None:
                    audio_file.close()
                    audio_file = None

        try:
            with input_stream(self.device_id) as stream:
                self._started.set()          # 输入流已打开：通知 start_meeting 校验通过
                while not self.stop_event.is_set():
                    data, overflow = stream.read(int(SAMPLE_RATE * 0.2))
                    if overflow:
                        _log_record(stream.device, 0, True, "音频输入溢出，部分采样可能丢失")
                    a = data.astype(np.float32) / 32768.0
                    self.level = min(1.0, float(np.sqrt(np.mean(a * a))) * 30.0)
                    if self.level_cb:
                        try:
                            self.level_cb(self.level)
                        except Exception:
                            pass
                    if writer is None:
                        seg_idx += 1
                        name = f"{seg_idx:02d}.wav"
                        audio_file = open(os.path.join(self.folder, name), "wb")
                        writer = wave.open(audio_file, "wb")
                        writer.setnchannels(1)
                        writer.setsampwidth(2)
                        writer.setframerate(SAMPLE_RATE)
                        samples = 0
                    # Each block updates the WAV header, avoiding a whole segment
                    # held only in memory. Finalize before closing the device.
                    writer.writeframes(np.ascontiguousarray(data).tobytes())
                    audio_file.flush()
                    if name not in self.segments:
                        self.segments.append(name)
                    samples += len(data)
                    if samples >= seg_samples:
                        close_segment()
        except Exception as e:
            self.error = str(e)
            self._started.set()              # 打不开：唤醒等待者，让它拿到 error
            print(f"[meeting] 录音线程异常: {e}")
        finally:
            try:
                close_segment()
            except Exception as exc:
                self.error = self.error or f"音频保存失败：{exc}"
            self.level = 0.0
