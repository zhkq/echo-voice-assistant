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

# ---------------------------------------------------------------- 采集互斥（按设备分片）
#
# 2026-09-23（D1）：原来这里是三个**全局单例**（_lock / _foreground / _yield_requested），
# 于是"会议用全向麦 + 指令用耳机"这种**配了两个麦**的机器也只能串行 —— 第二个用途被
# 全局锁挡住。AGENTS.md 里那条待办说的就是这件事：
#   "想让'会议用全向麦 + 指令用耳机'真正并行，得让不同设备各自持有流"。
#
# 改成**按设备分片**之后：
#   * **不同设备互不影响** → 两条泳道真并行（会议 / 指令+唤醒）；
#   * **同一设备仍然独占** → 物理上本来就只能有一个采集，这是正确行为。
# 同一设备内部仍然分前后台：**前台抢占后台**（唤醒监听让出）。
#
# 键用**设备名**而不是索引：索引会随在位设备增减整体平移（见 recorder.find_input_device）。
# 系统默认（-1）要**解析成它当前指向的那个设备**，否则"会议用系统默认、指令用那台具体
# 设备"会在实际是同一个物理设备时拿到两把不同的锁 —— 那就等于没互斥。
_dev_guard = threading.Lock()
_devices = {}          # device_key -> _DeviceState
_RESPONSE_TIMEOUT = 5
#: 保持**全局**：子进程杀不掉、HAL 卡死是**进程级**故障，不是某个设备的事。
_quarantined = False


class _DeviceState:
    """单个设备的采集状态。

    `lock` 与 `foreground` 是**两把**锁，沿用原来的分工：
      * `lock`       —— 采集互斥；前台拿它时最多等 2 s（等后台让出）
      * `foreground` —— 前台之间**立即**互斥（不等待）：第二个前台直接拒，
                        不该让它干等 2 s 才报错
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.foreground = threading.Lock()
        self.yield_requested = threading.Event()


class MicrophoneBusy(RuntimeError):
    pass


def device_key(device_id=-1):
    """device_id → 稳定的设备键（**用名字**；索引会漂）。"""
    try:
        idx = int(device_id)
    except (TypeError, ValueError):
        idx = -1
    if idx < 0:
        # 系统默认 → 解析出它当前指向哪个设备，好与"显式指定同一设备"共用一把锁。
        # 用 recorder.default_input_device()（它内部就是 sd.default.device[0]）——
        # 注意：这里只是**为了给锁起名**，绝不把这个索引交给 open()；
        # 打开仍然照旧传 device=None/-1 交给 PortAudio 自己选（AGENTS.md 的铁律）。
        try:
            from app.audio import recorder
            idx = recorder.default_input_device()
        except Exception:
            idx = None
        if idx is None or int(idx) < 0:
            return "default"
    try:
        from app.audio import recorder
        for d in recorder.list_input_devices_cached():
            if int(d.get("index", -1)) == int(idx):
                name = str(d.get("name") or "").strip()
                if name:
                    return "name:" + name
    except Exception:
        pass
    return "idx:%d" % int(idx)


def _state(device_id=-1):
    """取（必要时新建）该设备的状态。返回 `(state, key)`。"""
    key = device_key(device_id)
    with _dev_guard:
        st = _devices.get(key)
        if st is None:
            st = _devices[key] = _DeviceState()
        return st, key


def _yield_requested_for(device_id=-1):
    """该设备"要求后台让出"的标志 —— 后台采集在 `read()` 里查它。

    名字带下划线：只该被本模块与测试用，调用方不该直接操作它。
    """
    return _state(device_id)[0].yield_requested


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
    def __init__(self, stream, background, state):
        self.stream = stream
        self.background = background
        self.state = state
        self.device = stream.device

    def read(self, frames):
        if self.background and self.state.yield_requested.is_set():
            raise MicrophoneBusy("唤醒监听让出麦克风")
        return self.stream.read(frames)


@contextmanager
def input_stream(device_id=-1, blocksize=0, *, background=False):
    """采集麦克风。

    互斥**按设备**（2026-09-23 D1）：同一设备上同时只有一个采集，
    **不同设备可以并行**（会议用全向麦 + 指令用耳机）。

    同一设备内部仍然分前后台：**前台采集抢占后台**（唤醒监听让出）。
    """
    st, _key = _state(device_id)
    foreground = False
    acquired = False
    try:
        if _quarantined:
            raise RuntimeError("音频子进程未释放，请重启 ECHO 后再使用麦克风")
        if not background:
            foreground = st.foreground.acquire(blocking=False)
            if not foreground:
                raise MicrophoneBusy("该麦克风正在录音或测试，请先结束当前操作")
            st.yield_requested.set()
        elif st.yield_requested.is_set():
            raise MicrophoneBusy("该麦克风正在使用")
        acquired = st.lock.acquire(timeout=2 if not background else 0)
        if not acquired:
            raise MicrophoneBusy("该麦克风尚未释放，请稍后重试")
        if _quarantined:
            raise RuntimeError("音频子进程未释放，请重启 ECHO 后再使用麦克风")
        with _new_stream(device_id, blocksize) as stream:
            yield _Lease(stream, background, st)
    finally:
        if acquired:
            st.lock.release()
        if foreground:
            st.yield_requested.clear()
            st.foreground.release()


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
