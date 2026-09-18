# -*- coding: utf-8 -*-
"""recorder.py — ECHO 录音（sounddevice，16k 单声道 int16）

两类用途：
  record_command()      单次命令录音：开口检测 + 静音自动停止（同类语义）
  MeetingRecorder       会议录音线程：持续录，按 segment_minutes 自动分段，
                        实时输出音量电平（供面板波形）

录音期间任何异常都会抛给调用方；麦克风不可用时不阻塞（返回 None）。
"""
import os
import threading
import time
import wave

import numpy as np

SAMPLE_RATE = 16000


def _open_input(device_id=-1, blocksize=0):
    """打开可用的输入流。

    依次尝试：指定设备 → 系统默认输入 → 遍历全部输入设备；
    只要「能打开」就用（不做环境电平筛选——蓝牙耳机麦克风静音时
    电平极低是正常的，误判会跳过用户实际使用的设备）。

    默认输入用 device=None 交给 PortAudio 选，**不要**用 sd.default.device
    的下标硬取：该 pair 是 (输入, 输出)，早期代码误取 [1]（输出设备）当输入，
    还会把真正的默认输入排除掉，于是去开虚拟/接力设备（Oray、iPhone 麦克风），
    在 macOS 上可能把 CoreAudio HAL 卡死。见 app/audio/wake.py 同样结论。
    """
    import sounddevice as sd

    def _try_open(idx):
        return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                              device=idx, blocksize=blocksize or 0)

    candidates = []
    if device_id is not None and device_id >= 0:
        candidates.append(device_id)
    else:
        candidates.append(None)  # 系统默认输入
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            candidates.append(d["index"])

    seen = set()
    for idx in candidates:
        if idx in seen:
            continue
        seen.add(idx)
        try:
            return _try_open(idx)
        except Exception as e:
            _log_record(idx, 0, False, f"设备打开失败: {str(e)[:60]}")
            continue
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
        with _open_input(device_id) as stream:
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

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)

    def _loop(self):
        seg_samples = int(self.segment_minutes * 60 * SAMPLE_RATE)
        buf = []
        seg_idx = 0
        try:
            with _open_input(self.device_id) as stream:
                self._started.set()          # 输入流已打开：通知 start_meeting 校验通过
                while not self.stop_event.is_set():
                    data, _ = stream.read(int(SAMPLE_RATE * 0.2))
                    a = data.astype(np.float32) / 32768.0
                    self.level = min(1.0, float(np.sqrt(np.mean(a * a))) * 30.0)
                    if self.level_cb:
                        try:
                            self.level_cb(self.level)
                        except Exception:
                            pass
                    buf.append(data)
                    if sum(len(x) for x in buf) >= seg_samples:
                        seg_idx += 1
                        name = f"{seg_idx:02d}.wav"
                        _write_wav(os.path.join(self.folder, name), buf)
                        self.segments.append(name)
                        buf = []
            if buf and sum(len(x) for x in buf) > int(0.5 * SAMPLE_RATE):
                seg_idx += 1
                name = f"{seg_idx:02d}.wav"
                _write_wav(os.path.join(self.folder, name), buf)
                self.segments.append(name)
        except Exception as e:
            self.error = str(e)
            self._started.set()              # 打不开：唤醒等待者，让它拿到 error
            print(f"[meeting] 录音线程异常: {e}")
