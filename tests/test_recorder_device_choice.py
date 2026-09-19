# -*- coding: utf-8 -*-
"""麦克风选择的回归保护（issue #15 / PR #14）：

1. 默认输入优先用「系统默认输入」（device=None），不按下标硬取；
2. 兜底遍历跳过虚拟/映射/回环设备（macOS 上打开这类设备会卡死 CoreAudio HAL）；
3. 用户显式指定的设备不受过滤影响；
4. default_input_device() 返回 sd.default.device[0]（pair 是 (输入, 输出)）。

用假 sounddevice 模块替换 sys.modules，避免真去开设备。
"""
import sys
import types
import unittest
from unittest.mock import patch

from app.audio import recorder


def _fake_sd(devices, fail=None, default=(1, 4)):
    """devices=[(idx, name, in_ch)]；fail=打开时抛错的 device 集合。"""
    attempts = []

    class _Stream:
        def __init__(self, device=None, **kw):
            attempts.append(device)
            if fail and device in fail:
                raise RuntimeError("open failed: %r" % (device,))
            self.device = device

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _query_devices(kind=None):
        if kind is None:
            return [{"index": i, "name": n, "max_input_channels": c} for i, n, c in devices]
        return {"index": 1, "name": "麦克风 (ROSE SPEAKFEEL)", "max_input_channels": 2}

    return types.SimpleNamespace(
        default=types.SimpleNamespace(device=list(default)),
        query_devices=_query_devices,
        InputStream=_Stream,
        attempts=attempts,
    )


class MicChoiceTests(unittest.TestCase):
    def setUp(self):
        platform = patch.object(recorder.echo_platform, "isolates_audio_capture",
                                return_value=False)
        platform.start()
        self.addCleanup(platform.stop)
        # _log_record 会写 DB logs 表，测试里不要污染真实库
        self._patch_log = patch.object(recorder, "_log_record", lambda *a, **k: None)
        self._patch_log.start()
        self.addCleanup(self._patch_log.stop)

    def test_mac_does_not_probe_other_devices_on_default_failure(self):
        sd = _fake_sd([(1, "iPhone microphone", 1), (2, "Built-in", 1)], fail={None})
        with patch.object(recorder.echo_platform, "isolates_audio_capture", return_value=True), \
                patch.dict(sys.modules, {"sounddevice": sd}):
            with self.assertRaisesRegex(RuntimeError, "未自动尝试"):
                recorder._open_input(-1)
        self.assertEqual(sd.attempts, [None])

    def test_default_success_does_not_enumerate_devices(self):
        sd = _fake_sd([])
        sd.query_devices = lambda: self.fail("must not enumerate")
        with patch.dict(sys.modules, {"sounddevice": sd}):
            recorder._open_input(-1)

    def test_default_input_first(self):
        sd = _fake_sd([(0, "Microsoft 声音映射器 - Input", 2),
                       (1, "麦克风 (ROSE SPEAKFEEL)", 2)])
        with patch.dict(sys.modules, {"sounddevice": sd}):
            stream = recorder._open_input(-1)
        self.assertEqual(sd.attempts, [None])       # 第一步就交给系统默认输入
        self.assertIsNone(stream.device)

    def test_fallback_skips_virtual_devices(self):
        sd = _fake_sd([(0, "Microsoft 声音映射器 - Input", 2),
                       (1, "麦克风 (ROSE SPEAKFEEL)", 2),
                       (2, "立体声混音 (Realtek(R) Audio)", 2),
                       (3, "OrayVirtualAudioDevice", 2)], fail={None})
        with patch.dict(sys.modules, {"sounddevice": sd}):
            stream = recorder._open_input(-1)
        self.assertEqual(sd.attempts, [None, 1])    # 默认失败 → 直接落到真实麦克风
        self.assertEqual(stream.device, 1)

    def test_explicit_virtual_device_still_used(self):
        sd = _fake_sd([(0, "Microsoft 声音映射器 - Input", 2)])
        with patch.dict(sys.modules, {"sounddevice": sd}):
            stream = recorder._open_input(0)
        self.assertEqual(sd.attempts, [0])          # 显式指定：虚拟设备也照用
        self.assertEqual(stream.device, 0)

    def test_only_virtual_devices_raises_with_hint(self):
        sd = _fake_sd([(0, "Microsoft 声音映射器 - Input", 2),
                       (2, "立体声混音", 2)], fail={None})
        with patch.dict(sys.modules, {"sounddevice": sd}):
            with self.assertRaises(RuntimeError) as cm:
                recorder._open_input(-1)
        self.assertIn("虚拟", str(cm.exception))
        self.assertEqual(sd.attempts, [None])       # 没有去开任何虚拟设备

    def test_default_input_device_is_pair_first(self):
        sd = _fake_sd([(1, "麦克风", 2)], default=(3, 7))
        with patch.dict(sys.modules, {"sounddevice": sd}):
            self.assertEqual(recorder.default_input_device(), 3)

    def test_real_devices_all_fail_still_reports_plain_error(self):
        sd = _fake_sd([(1, "麦克风 (ROSE SPEAKFEEL)", 2)], fail={None, 1})
        with patch.dict(sys.modules, {"sounddevice": sd}):
            with self.assertRaises(RuntimeError) as cm:
                recorder._open_input(-1)
        self.assertEqual(str(cm.exception), "没有可用的输入设备")


if __name__ == "__main__":
    unittest.main()
