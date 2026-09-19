"""Fault injection only: never access a physical audio device or the user's DB."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import numpy as np

from app.audio import mic, recorder


class Stream:
    device = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, n):
        return np.zeros((n, 1), dtype=np.int16), False


class RecordingFailureTests(unittest.TestCase):
    def test_disconnect_preserves_partial_wav(self):
        stream = Stream()
        stream.read = MagicMock(side_effect=[
            (np.zeros((3200, 1), dtype=np.int16), False),
            RuntimeError("disconnected")])
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(mic, "_new_stream", return_value=stream):
            rec = recorder.MeetingRecorder(folder)
            rec.start()
            rec.thread.join(2)
            self.assertFalse(rec.thread.is_alive())
            self.assertEqual(rec.error, "disconnected")
            self.assertEqual(rec.segments, ["01.wav"])
            with wave.open(os.path.join(folder, "01.wav")) as saved:
                self.assertEqual(saved.getnframes(), 3200)

    def test_stop_reports_live_thread_instead_of_claiming_release(self):
        gate = threading.Event()
        rec = recorder.MeetingRecorder("unused")
        rec.thread = threading.Thread(target=gate.wait)
        rec.thread.start()
        try:
            self.assertFalse(rec.stop(timeout=0.01))
        finally:
            gate.set()
            rec.thread.join(1)
        self.assertTrue(rec.stop(timeout=0.01))

    def test_wav_is_readable_while_recording_before_stream_close(self):
        stream = Stream()
        rec = None
        calls = 0
        with tempfile.TemporaryDirectory() as folder:
            def read(n):
                nonlocal calls
                calls += 1
                if calls == 2:
                    with wave.open(os.path.join(folder, "01.wav")) as saved:
                        self.assertEqual(saved.getnframes(), 3200)
                    rec.stop_event.set()
                return np.zeros((n, 1), dtype=np.int16), False
            stream.read = read
            rec = recorder.MeetingRecorder(folder)
            with patch.object(mic, "_new_stream", return_value=stream):
                rec._loop()
            self.assertIsNone(rec.error)


class MeetingStateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        # Redirect even import-time directory initialization away from real data.
        self.stack.enter_context(patch("app.paths.meetings_root", return_value=self.folder))
        from app import meeting
        self.meeting = meeting
        self.stack.enter_context(patch.object(meeting, "db", MagicMock()))
        meeting.db.get_meeting_by_name.return_value = {"id": 7}
        self.stack.enter_context(patch.dict(meeting._state, {
            "active": True, "folder": self.folder, "recorder": None,
            "started_at": None, "level": 0, "error": ""}))
        self.transcribe = self.stack.enter_context(patch.object(meeting, "_transcribe_meeting"))

    def test_unexpected_failure_updates_status_and_retains_audio(self):
        rec = recorder.MeetingRecorder(self.folder)
        rec.error = "disconnected"
        rec.segments = ["01.wav"]
        recorder._write_wav(os.path.join(self.folder, "01.wav"),
                            np.zeros((3200, 1), dtype=np.int16))
        rec.thread = MagicMock()
        rec.thread.is_alive.return_value = False
        self.meeting._state["recorder"] = rec
        self.meeting._watch_recorder(rec)
        self.assertFalse(self.meeting.meeting_status()["active"])
        self.assertIn("disconnected", self.meeting.meeting_status()["error"])
        self.assertEqual(self.meeting.db.update_meeting.call_args.kwargs["status"], "interrupted")
        self.transcribe.assert_not_called()

    def test_stop_timeout_retains_recorder_and_prevents_new_start(self):
        rec = MagicMock()
        rec.stop.return_value = False
        self.meeting._state["recorder"] = rec
        ok, _ = self.meeting.stop_meeting()
        self.assertFalse(ok)
        self.assertIs(self.meeting._state["recorder"], rec)
        self.assertFalse(self.meeting.start_meeting()[0])
        self.transcribe.assert_not_called()


class WakeOwnershipTests(unittest.TestCase):
    def test_wake_releases_microphone_before_command_callback(self):
        from app.audio.wake import WakeListener
        cfg = {"wakePaused": False, "wakeSilenceFloor": 0}
        listener = WakeListener(lambda key, default=None: cfg.get(key, default))
        detector = MagicMock()
        detector.hit.return_value = True
        callback = MagicMock()

        def on_wake():
            with mic.input_stream():
                callback()
            listener.shutdown()

        listener.on_wake = on_wake
        with patch.object(listener, "_make_detector", return_value=detector), \
                patch.object(mic, "_new_stream", return_value=Stream()):
            listener._run_impl()
        callback.assert_called_once()


class OwnershipTests(unittest.TestCase):
    def test_quarantine_refuses_device_open(self):
        with patch.object(mic, "_quarantined", True), \
                patch.object(mic, "_new_stream") as factory:
            with self.assertRaisesRegex(RuntimeError, "重启"):
                with mic.input_stream():
                    pass
            factory.assert_not_called()

    def test_second_foreground_cannot_open_device(self):
        with patch.object(mic, "_new_stream", return_value=Stream()) as factory:
            with mic.input_stream():
                with self.assertRaises(mic.MicrophoneBusy):
                    with mic.input_stream():
                        pass
            self.assertEqual(factory.call_count, 1)
            with mic.input_stream():
                pass

    def test_failed_open_releases_ownership(self):
        with patch.object(mic, "_new_stream", side_effect=RuntimeError("fail")):
            with self.assertRaises(RuntimeError):
                with mic.input_stream():
                    pass
        with patch.object(mic, "_new_stream", return_value=Stream()):
            with mic.input_stream():
                pass

    def test_background_yields_to_foreground(self):
        ready = threading.Event()
        yielded = threading.Event()

        def background():
            with mic.input_stream(background=True) as stream:
                ready.set()
                mic._yield_requested.wait(2)
                try:
                    stream.read(1)
                except mic.MicrophoneBusy:
                    yielded.set()

        with patch.object(mic, "_new_stream", return_value=Stream()):
            thread = threading.Thread(target=background)
            thread.start()
            try:
                self.assertTrue(ready.wait(2))
                with mic.input_stream():
                    self.assertTrue(yielded.is_set())
            finally:
                thread.join(2)
            self.assertFalse(thread.is_alive())


@unittest.skipIf(os.name == "nt", "macOS isolation uses POSIX descriptor passing")
class IsolationTests(unittest.TestCase):
    def test_real_worker_with_fake_audio_module(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "mic_stub")
        with patch.dict(os.environ, {"PYTHONPATH": fixture}):
            with mic._IsolatedStream(-1, 0) as stream:
                data, overflow = stream.read(3200)
                self.assertEqual(data.shape, (3200, 1))
                self.assertFalse(overflow)
            self.assertIsNotNone(stream.process.poll())

    def child_factory(self, body, children):
        real_popen = subprocess.Popen

        def spawn(argv, **kwargs):
            code = ("import socket,sys,json,time,base64; "
                    "s=socket.socket(fileno=int(sys.argv[1])); " + body)
            child = real_popen([sys.executable, "-c", code, argv[3]], **kwargs)
            children.append(child)
            return child
        return spawn

    def test_stuck_open_is_reaped(self):
        children = []
        with patch.object(mic, "_RESPONSE_TIMEOUT", 0.2), \
                patch.object(mic.subprocess, "Popen", self.child_factory("time.sleep(30)", children)):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                mic._IsolatedStream(-1, 0)
        self.assertIsNotNone(children[0].poll())

    def test_stuck_read_and_close_are_reaped(self):
        children = []
        body = 's.sendall(b\'{"device":1}\\n\'); time.sleep(30)'
        with patch.object(mic, "_RESPONSE_TIMEOUT", 0.2), \
                patch.object(mic.subprocess, "Popen", self.child_factory(body, children)):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                with mic._IsolatedStream(-1, 0) as stream:
                    stream.read(3200)
        self.assertIsNotNone(children[0].poll())

    def test_audio_round_trip_without_hardware(self):
        children = []
        body = ('s.sendall(b\'{"device":1}\\n\'); s.recv(1024); '
                's.sendall((json.dumps({"audio":base64.b64encode(b"\\0"*8).decode(),'
                '"overflow":False})+"\\n").encode()); s.recv(1024)')
        with patch.object(mic.subprocess, "Popen", self.child_factory(body, children)):
            with mic._IsolatedStream(-1, 0) as stream:
                data, overflow = stream.read(4)
                self.assertEqual(data.shape, (4, 1))
                self.assertFalse(overflow)
        self.assertIsNotNone(children[0].poll())


if __name__ == "__main__":
    unittest.main()
