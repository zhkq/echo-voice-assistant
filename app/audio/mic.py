"""Shared microphone ownership; isolate macOS CoreAudio calls from the server.

Never kill a Python thread that may own CoreAudio locks. On macOS all device
operations live in a disposable, lightweight child process instead.
"""
import base64
from contextlib import contextmanager
import json
import socket
import subprocess
import sys
import threading

import numpy as np

from app import platform as echo_platform

_lock = threading.Lock()
_foreground = threading.Lock()
_yield_requested = threading.Event()
_RESPONSE_TIMEOUT = 5
_quarantined = False


class MicrophoneBusy(RuntimeError):
    pass


class _IsolatedStream:
    def __init__(self, device_id, blocksize):
        self.process = None
        self.sock = None
        self.reader = None
        parent, child = socket.socketpair()
        self.sock = parent
        parent.settimeout(_RESPONSE_TIMEOUT)
        try:
            from app.paths import echo_root
            self.process = subprocess.Popen(
                [sys.executable, "-m", "app.audio.mic", str(child.fileno()),
                 str(device_id), str(blocksize)], cwd=echo_root(),
                pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            child.close()
            self.reader = parent.makefile("rb")
            self.device = self._response()["device"]
        except BaseException:
            child.close()
            self.close()
            raise

    def _response(self):
        try:
            line = self.reader.readline()
        except (TimeoutError, OSError) as exc:
            raise RuntimeError("麦克风响应超时，已隔离异常设备；请检查输入设备后重试") from exc
        if not line:
            raise RuntimeError("麦克风子进程已退出")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def __enter__(self):
        return self

    def read(self, frames):
        self.sock.sendall((json.dumps({"frames": frames}) + "\n").encode())
        result = self._response()
        data = np.frombuffer(base64.b64decode(result["audio"]), dtype=np.int16)
        return data.reshape(-1, 1).copy(), result["overflow"]

    def close(self):
        global _quarantined
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.reader is not None:
            self.reader.close()
        if self.sock is not None:
            self.sock.close()
        if self.process is not None:
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired as exc:
                        _quarantined = True
                        raise RuntimeError("音频子进程无法退出，已禁止再次开麦，请重启 ECHO") from exc

    def __exit__(self, *args):
        self.close()


def _new_stream(device_id, blocksize):
    if echo_platform.isolates_audio_capture():
        return _IsolatedStream(device_id, blocksize)
    from app.audio.recorder import _open_input
    return _open_input(device_id, blocksize)


class _Lease:
    def __init__(self, stream, background):
        self.stream = stream
        self.background = background
        self.device = stream.device

    def read(self, frames):
        if self.background and _yield_requested.is_set():
            raise MicrophoneBusy("唤醒监听让出麦克风")
        return self.stream.read(frames)


@contextmanager
def input_stream(device_id=-1, blocksize=0, *, background=False):
    """Only one capture owns the mic. Foreground capture preempts wake listening."""
    foreground = False
    acquired = False
    try:
        if _quarantined:
            raise RuntimeError("音频子进程未释放，请重启 ECHO 后再使用麦克风")
        if not background:
            foreground = _foreground.acquire(blocking=False)
            if not foreground:
                raise MicrophoneBusy("麦克风正在录音或测试，请先结束当前操作")
            _yield_requested.set()
        elif _yield_requested.is_set():
            raise MicrophoneBusy("麦克风正在使用")
        acquired = _lock.acquire(timeout=2 if not background else 0)
        if not acquired:
            raise MicrophoneBusy("麦克风尚未释放，请稍后重试")
        if _quarantined:
            raise RuntimeError("音频子进程未释放，请重启 ECHO 后再使用麦克风")
        with _new_stream(device_id, blocksize) as stream:
            yield _Lease(stream, background)
    finally:
        if acquired:
            _lock.release()
        if foreground:
            _yield_requested.clear()
            _foreground.release()


def _worker(fd, device_id, blocksize):
    from app.audio.recorder import _open_input
    sock = socket.socket(fileno=fd)

    def reply(value):
        sock.sendall((json.dumps(value) + "\n").encode())

    try:
        with sock.makefile("rb") as requests, _open_input(device_id, blocksize) as stream:
            reply({"device": int(stream.device)})
            for line in requests:
                frames = int(json.loads(line)["frames"])
                if not 0 < frames <= 16000:
                    raise ValueError("invalid audio frame count")
                data, overflow = stream.read(frames)
                reply({"audio": base64.b64encode(data.tobytes()).decode(),
                       "overflow": bool(overflow)})
    except Exception as exc:
        try:
            reply({"error": str(exc)})
        except OSError:
            pass
    finally:
        sock.close()


if __name__ == "__main__":
    _worker(*(int(arg) for arg in sys.argv[1:]))
