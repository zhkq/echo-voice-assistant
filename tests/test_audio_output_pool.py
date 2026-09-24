# -*- coding: utf-8 -*-
"""扬声器（播放设备）设备池：有序候选 + 在位判定 + 回退不静默。

为什么要有它：采集那侧早就能"按用途挑设备"（指令用耳机、会议用全向麦），
而播放一直是"系统默认发声"。于是会出现同一台机器上
**麦选了耳机（不想把全场录进来），播报却从会议室音箱出去（把"已发送"念给全场听）**。
两个方向的需求本来就是对称的，所以扬声器也要进同一个池。

这里钉四件事：

  1. **优先级池取第一个在位的** —— 这就是"用优先级最高的在线设备"在播放侧的落点；
  2. **不在位要往下找，找完都没有才回退系统默认，而且写日志**（回退绝不静默）；
  3. **"系统默认"是不传 `device`**，不是 `sd.default.device[1]` 那个会漂的下标
     （AGENTS.md 记过一次同类事故：把二元组的下标取错，在 macOS 上锁死了 CoreAudio）；
  4. 输入那套的既有行为**一点没动**（它刚加固过，不该为了"对称好看"去重构）。

不碰真声卡：`list_output_devices` 在测试里被打桩成一份固定的设备清单。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio import output as out_mod                                # noqa: E402
from app.audio.output import (                                        # noqa: E402
    SYSTEM_DEFAULT,
    find_output_device,
    play_device_kwargs,
    pool_status,
    resolve_output_device,
)

#: 一份假的输出设备清单：两台真扬声器（各出现在两套 host API 里）+ 一个虚拟端点
FAKE_DEVICES = [
    {"index": 3, "name": "扬声器 (Realtek)", "channels": 2,
     "hostapi": "MME", "samplerate": 44100, "virtual": False},
    {"index": 9, "name": "扬声器 (Realtek)", "channels": 2,
     "hostapi": "Windows WASAPI", "samplerate": 44100, "virtual": False},
    {"index": 4, "name": "Bose Speaker", "channels": 2,
     "hostapi": "MME", "samplerate": 48000, "virtual": False},
    {"index": 7, "name": "立体声混音 (Realtek)", "channels": 2,
     "hostapi": "MME", "samplerate": 48000, "virtual": True},
]


def _settings(values):
    """假的设置读取：只认识传进来的那几个键。"""
    return lambda key, default=None: values.get(key, default)


class _PoolCase(unittest.TestCase):
    def setUp(self):
        p = patch.object(out_mod, "list_output_devices_cached",
                         lambda *a, **k: list(FAKE_DEVICES))
        p.start()
        self.addCleanup(p.stop)
        self.logs = []
        q = patch("app.db.add_log",
                  lambda level, src, msg: self.logs.append((level, src, msg)))
        q.start()
        self.addCleanup(q.stop)

    def resolve(self, purpose="command", **values):
        with patch("app.config.settings.get", _settings(values)):
            return resolve_output_device(purpose)

    def resolve_pool(self, pool, purpose="command", **extra):
        values = {"outputDeviceIds": pool}
        values.update(extra)
        return self.resolve(purpose, **values)


class FindDeviceTests(_PoolCase):
    def test_name_is_a_stable_key(self):
        self.assertEqual(find_output_device("Bose Speaker"), 4)

    def test_same_name_in_two_apis_prefers_wasapi(self):
        """同一个扬声器出现在 MME 与 WASAPI 里时选 WASAPI（排名更前）。

        这条与输入侧共用 `rank_hostapi` —— 两边的"哪套 API 更好"是同一个答案。
        """
        self.assertEqual(find_output_device("扬声器 (Realtek)"), 9)

    def test_index_form_still_works_and_negative_means_default(self):
        """老配置里存的是索引（会漂）—— 仍然认，负数 = 系统默认。"""
        self.assertEqual(find_output_device("4"), 4)
        self.assertEqual(find_output_device("-1"), -1)

    def test_unknown_name_is_none_not_an_exception(self):
        self.assertIsNone(find_output_device("不存在的音箱"))


class PoolPriorityTests(_PoolCase):
    def test_takes_the_first_present_candidate(self):
        """**顺序由用户排，取第一个在位的** —— 这就是"优先级池"的全部语义。"""
        self.assertEqual(self.resolve_pool(["Bose Speaker", "扬声器 (Realtek)"]), 4)
        self.assertEqual(self.resolve_pool(["扬声器 (Realtek)", "Bose Speaker"]), 9)

    def test_skips_absent_candidates_and_keeps_looking(self):
        """第一个候选拔了 → **继续往下找**，而不是直接回退系统默认。

        这条是池的意义所在：只有"首选"没有"备选"的话，插拔一次设备就得改配置。
        """
        self.assertEqual(self.resolve_pool(["会议室音箱（已拔）", "Bose Speaker"]), 4)

    def test_whole_pool_absent_falls_back_to_default_LOUDLY(self):
        """池里一个都不在位 → 回退系统默认，**并且写一条日志**。

        不写日志的话：用户以为配了池、其实一直在用默认扬声器，
        现象只是"播报从笔记本出来了"，查不出原因。
        """
        self.assertEqual(self.resolve_pool(["甲（没有）", "乙（也没有）"]), SYSTEM_DEFAULT)
        self.assertTrue([m for _lv, _s, m in self.logs if "优先级池都不在位" in m],
                        "静默回退了：%s" % self.logs)

    def test_explicit_per_purpose_setting_wins_over_the_pool(self):
        """按用途显式指定 > 优先级池（与采集侧同一条优先级）。

        用途分开正是需求本身："指令的确认只让自己听见、会议的播报让全场听见"。
        """
        got = self.resolve("command", commandOutputDeviceId="Bose Speaker",
                           outputDeviceIds=["扬声器 (Realtek)"])
        self.assertEqual(got, 4, "显式指定被池盖掉了")
        got2 = self.resolve("meeting", meetingOutputDeviceId="扬声器 (Realtek)",
                            outputDeviceIds=["Bose Speaker"])
        self.assertEqual(got2, 9)

    def test_purposes_do_not_see_each_others_setting(self):
        """给会议配了音箱，不该影响指令链路（反之亦然）。"""
        self.assertEqual(self.resolve("command", meetingOutputDeviceId="Bose Speaker"),
                         SYSTEM_DEFAULT)

    def test_absent_explicit_setting_warns_and_falls_back(self):
        """显式指定的那台不在位 → 告警 + 回退（不静默）。"""
        got = self.resolve("command", commandOutputDeviceId="早就拔了")
        self.assertEqual(got, SYSTEM_DEFAULT)
        self.assertTrue([m for _lv, _s, m in self.logs if "不在位" in m],
                        "显式指定没找到却没告警：%s" % self.logs)

    def test_pool_accepts_a_comma_string_too(self):
        """设置层可能把列表给成逗号串（面板回传的形态）—— 两种都要认。"""
        self.assertEqual(self.resolve_pool("Bose Speaker, 扬声器 (Realtek)"), 4)

    def test_broken_settings_read_falls_back_to_default(self):
        """读配置炸了 → 回退系统默认。**播放路径不该因为读配置失败就出不了声。**"""
        with patch("app.config.settings.get", side_effect=RuntimeError("boom")):
            self.assertEqual(resolve_output_device("command"), SYSTEM_DEFAULT)


class PlayKwargsTests(_PoolCase):
    def test_system_default_means_OMIT_the_device_argument(self):
        """**"用系统默认"= 不传 `device`。**

        这是本模块最容易写错的一处：传 `sd.default.device[1]` 看着更"明确"，
        但那个下标会随在位设备增减平移，而且 AGENTS.md 记过一次真实事故
        （把 `(输入, 输出)` 的下标取错，在 macOS 上把 CoreAudio HAL 锁死）。
        """
        with patch("app.config.settings.get", _settings({})):
            self.assertEqual(play_device_kwargs("command"), {})

    def test_explicit_device_is_passed_through(self):
        with patch("app.config.settings.get",
                   _settings({"outputDeviceIds": ["Bose Speaker"]})):
            self.assertEqual(play_device_kwargs("command"), {"device": 4})

    def test_pool_status_reports_presence_per_candidate(self):
        """面板要能回答"我配的那台现在在不在"。"""
        with patch("app.config.settings.get",
                   _settings({"outputDeviceIds": ["Bose Speaker", "甲（没有）"]})):
            self.assertEqual(pool_status(),
                             [{"value": "Bose Speaker", "present": True},
                              {"value": "甲（没有）", "present": False}])


class BeepsFollowSystemDefaultTests(_PoolCase):
    """提示音**有意不参与**扬声器设备池（2026-09-24 拍板）。

    Windows 的接缝是 `winsound`、macOS 是 `afplay`，**都没有设备参数** ——
    所以"配了扬声器池"对提示音不生效。这是取舍，不是漏了。

    ## 为什么不能"顺手"改成 `sd.play`（这条用例真正在钉的东西）

    `sounddevice.play()` 的文档：*"It **cannot be used for multiple overlapping
    playbacks**. … Call `stop()` to terminate any currently running invocation"*。
    而**播报也走 `sd.play`**（`_play_wav_data`）。所以提示音一改用它，就变成
    "播报正在念 → 提示音响 → 播报**被当场切断**"，且只在与播报重叠时发生 ——
    最难查的那类时序问题。

    所以这里**行为性地**钉住：一次提示音**绝不许碰 sounddevice 的全局播放态**。
    只写注释拦不住下一个人；钉住"没调用 sd.play"才拦得住。
    """

    def test_beep_uses_the_platform_seam_and_ignores_the_pool(self):
        from app.audio import tts
        with patch("app.config.settings.get",
                   _settings({"outputDeviceIds": ["Bose Speaker"]})), \
             patch.object(tts.echo_platform, "play_wav_async") as seam:
            seam.return_value = True
            ok = tts.play_beep("start")
        self.assertTrue(ok)
        self.assertTrue(seam.called,
                        "提示音应当走平台接缝（跟随系统默认），这是有意的取舍")

    def test_beep_never_touches_sounddevices_global_playback(self):
        """**核心那条**：提示音不许调 `sd.play`，否则会和播报抢全局播放态。

        真跑去调了会怎样：`sd.play` 内部先 `sd.stop()`，把正在念的播报切断。
        这里只要它**被调用**就红 —— 不等到真出 bug 才发现。
        """
        from app.audio import tts
        import sounddevice as sd
        with patch("app.config.settings.get",
                   _settings({"outputDeviceIds": ["Bose Speaker"]})), \
             patch.object(sd, "play") as sd_play, \
             patch.object(tts.echo_platform, "play_wav_async") as seam:
            seam.return_value = True
            tts.play_beep("ok")
        self.assertFalse(sd_play.called,
                         "提示音调了 sounddevice 的 sd.play —— 它会 stop() 掉正在播的语音播报。"
                         "真要给提示音选设备，得单独开一条 sd.OutputStream（见 play_beep 的说明）")

    def test_module_records_the_REAL_reason_not_the_wrong_ones(self):
        """源码里写的理由必须是**真的那条**。

        第一版注释写的是"得自己管异步性、重采样与失败回退" —— 前两条**是错的**：
        `sd.play` 本来就非阻塞（不 wait 即异步），而提示音是 44.1 kHz 的常规率。
        照着错的理由做决定，会把人带到"那就顺手实现一下吧"—— 恰好踩进真正的坑。
        """
        import inspect
        from app.audio import tts
        doc = inspect.getdoc(tts.play_beep) or ""
        self.assertIn("不参与扬声器设备池", doc)
        self.assertIn("都没有设备参数", doc)
        # 真正的隐患：全局播放态 + stop()
        self.assertIn("cannot be used", doc)
        self.assertIn("OutputStream", doc, "要给出正解的方向，别只说'不行'")
        # 反面证据也要留着，否则会被当成"纯没收益"
        self.assertIn("winsound 老 waveOut", doc)
        # 那两条错的理由不许再出现
        self.assertNotIn("拍板，是有意的", doc)


class TtsUsesThePoolTests(_PoolCase):
    """**语音播报走设备池** —— 这是需求真正要的那半。"""

    def test_play_wav_data_omits_device_when_default(self):
        """系统默认时**不传 device**（见 PlayKwargsTests 的说明）。"""
        from app.audio import tts
        seen = {}

        class _FakeSD:
            @staticmethod
            def play(data, sr, **kw):
                seen.update(kw)

            @staticmethod
            def wait():
                pass

        with patch("app.config.settings.get", _settings({})), \
             patch.dict(sys.modules, {"sounddevice": _FakeSD}):
            tts._play_wav_data([0.0], 16000)
        self.assertEqual(seen, {}, "系统默认时不该传 device")

    def test_play_wav_data_passes_the_resolved_device(self):
        """配了池 → 播报走那台（`device=` 有值）。"""
        from app.audio import tts
        seen = {}

        class _FakeSD:
            @staticmethod
            def play(data, sr, **kw):
                seen.update(kw)

            @staticmethod
            def wait():
                pass

        with patch("app.config.settings.get",
                   _settings({"outputDeviceIds": ["Bose Speaker"]})), \
             patch.dict(sys.modules, {"sounddevice": _FakeSD}):
            tts._play_wav_data([0.0], 16000)
        self.assertEqual(seen, {"device": 4}, "配了扬声器池却没传给播放")

    def test_purpose_is_honoured(self):
        """按用途解析：指令的播报可以只走耳机，会议的播报走音箱。"""
        from app.audio import tts
        seen = {}

        class _FakeSD:
            @staticmethod
            def play(data, sr, **kw):
                seen.update(kw)

            @staticmethod
            def wait():
                pass

        values = {"meetingOutputDeviceId": "Bose Speaker",
                  "commandOutputDeviceId": "扬声器 (Realtek)"}
        with patch("app.config.settings.get", _settings(values)), \
             patch.dict(sys.modules, {"sounddevice": _FakeSD}):
            tts._play_wav_data([0.0], 16000, purpose="meeting")
        self.assertEqual(seen, {"device": 4})


if __name__ == "__main__":
    unittest.main()
