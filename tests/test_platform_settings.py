# -*- coding: utf-8 -*-
"""D11：平台声明的配置默认值 / 候选项（声明式，取代入口处的 monkeypatch）

背景
----
平台差异不只是"环境默认值"（`dataDir` 那种），还体现在**配置项本身**：

  * macOS 没有 CUDA → `device` 默认该是 `cpu`；
  * macOS 的离线朗读是 `say`，不是 Windows 的 SAPI → `ttsEngine` 候选项里不该出现 `sapi`；
  * macOS 精简依赖不含 funasr → `sttModel` 默认要落在 whisper 档（否则静默转写失败）。

这些原来散在 `mac/run_mac.py` 的"注入式覆盖 DEFAULTS"里（D17 要收掉的那类）。
现在声明式放在各平台 `env.py` 的 `PLATFORM_DEFAULTS`，由 `app/config.py` 消费。

本文件钉住的四件事
------------------
1. 三个平台都**声明了**该声明的（且没有互相抄错的键，例如 mac 上不该有 `sapi`）；
2. **Windows 行为零变化**：没声明 = 用 `config.DEFAULTS` 的基准值；
3. 换平台声明后，`seed_defaults()` 写进库的值与面板候选项都跟着变；
4. 用户显式改过的值仍然优先（D20/D21 的纪律，不能被平台默认值顶掉）。

做法：把 `app.platform.defaults` 换成某个平台的 `PLATFORM_DEFAULTS`（与
`tests/test_paths.py` 验证 mac 数据根时同一手法），无需真机。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                          # noqa: E402
from app import platform as echo_platform                     # noqa: E402
from app.config import DEFAULTS, Settings, settings           # noqa: E402
from app.config import platform_default, platform_options     # noqa: E402
from app.platform import darwin, linux, win32                 # noqa: E402


def _expected_default(key):
    """当前平台下 ``key`` 的有效默认值（= ``seed_defaults()`` 应当写进库的值）。

    ``config.DEFAULTS`` 是 **Windows 基准值**，不是"当前平台的默认值"：mac/linux 的
    ``PLATFORM_DEFAULTS`` 会覆盖其中几项（device=cpu、sttModel=base…）。所以断言必须
    走平台接缝，写死基准值会让用例在 CI 的 mac/linux runner 上必红（2026-09-20 修）。
    """
    return platform_default(key, DEFAULTS[key]["value"])


def _declared_default(mod, key):
    """把 ``mod`` 的平台声明当作"当前平台"时，``key`` 的有效默认值。"""
    declared = (mod.PLATFORM_DEFAULTS.get("settingDefaults") or {}).get(key)
    return DEFAULTS[key]["value"] if declared is None else declared


class PlatformDeclarations(unittest.TestCase):
    def test_facade_exposes_the_two_lookups(self):
        self.assertTrue(callable(echo_platform.setting_default))
        self.assertTrue(callable(echo_platform.setting_options))
        # 没声明的键返回 None（= 用基准值），不是抛异常
        self.assertIsNone(echo_platform.setting_default("noSuchKey"))
        self.assertIsNone(echo_platform.setting_options("noSuchKey"))

    def test_windows_declares_the_reference_values(self):
        """Windows 是基准平台：显式声明出来，三个平台才一眼可比。"""
        self.assertIsNone(win32.PLATFORM_DEFAULTS.get("settingDefaults"),
                          "Windows 不需要覆盖任何默认值（它就是基准）")
        opts = win32.PLATFORM_DEFAULTS["settingOptions"]["ttsEngine"]
        self.assertIn("sapi", opts)
        self.assertNotIn("say", opts)
        self.assertEqual(opts, DEFAULTS["ttsEngine"]["options"],
                         "Windows 声明的候选项必须与 config.DEFAULTS 一致")

    def test_macos_declares_cpu_and_whisper_and_say(self):
        d = darwin.PLATFORM_DEFAULTS["settingDefaults"]
        self.assertEqual(d["device"], "cpu", "Mac 没有 CUDA")
        self.assertEqual(d["sttModel"], "base", "mac 精简依赖不含 funasr")
        self.assertEqual(d["meetingSttModel"], "small")
        opts = darwin.PLATFORM_DEFAULTS["settingOptions"]["ttsEngine"]
        self.assertIn("say", opts)
        self.assertNotIn("sapi", opts, "mac 上不该出现 Windows 的 SAPI 选项")

    def test_linux_declares_espeak(self):
        opts = linux.PLATFORM_DEFAULTS["settingOptions"]["ttsEngine"]
        self.assertIn("espeak", opts)
        self.assertNotIn("sapi", opts)
        # Linux 机器可能有 GPU，device 不做覆盖（沿用 auto）
        self.assertNotIn("device", linux.PLATFORM_DEFAULTS.get("settingDefaults") or {})

    def test_every_platform_offers_auto_and_off(self):
        for name, mod in (("win32", win32), ("darwin", darwin), ("linux", linux)):
            with self.subTest(platform=name):
                opts = mod.PLATFORM_DEFAULTS["settingOptions"]["ttsEngine"]
                self.assertEqual(opts[0], "auto")
                self.assertEqual(opts[-1], "off")

    def test_declared_keys_all_exist_in_defaults(self):
        """声明了 config 里不存在的键 = 静默无效（写错了没人会知道）。"""
        for name, mod in (("win32", win32), ("darwin", darwin), ("linux", linux)):
            for key in (mod.PLATFORM_DEFAULTS.get("settingDefaults") or {}):
                with self.subTest(platform=name, key=key):
                    self.assertIn(key, DEFAULTS)
            for key in (mod.PLATFORM_DEFAULTS.get("settingOptions") or {}):
                with self.subTest(platform=name, key=key):
                    self.assertIn(key, DEFAULTS)


class ConfigConsumesTheDeclarations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-platform-settings-")
        cls._old = (db.DATA_DIR, db.DB_FILE)

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        settings._cache = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "%s.db" % self._testMethodName)
        for suffix in ("", "-wal", "-shm"):
            p = db.DB_FILE + suffix
            if os.path.exists(p):
                os.remove(p)
        db.init()
        settings._cache = None
        self.addCleanup(setattr, settings, "_cache", None)

    def _as_platform(self, mod):
        """把接缝的 defaults() 换成某个平台的声明（与 test_paths 同一手法）。"""
        return patch.object(echo_platform, "defaults", lambda: dict(mod.PLATFORM_DEFAULTS))

    def _stored(self, key):
        return db.get_setting(key)

    def _row(self, key):
        return {r["key"]: r for r in db.all_settings()}[key]

    def test_current_platform_defaults_apply(self):
        """本机平台跑：写进库的就是**本平台**的有效默认值。

        Windows 上有效默认值 == ``DEFAULTS`` 基准值，所以这条在 Windows 依然等价于
        "写进去的就是基准值，行为零变化"；但在 mac/linux 上基准值不对（device=cpu 等），
        断言必须走平台接缝。
        """
        settings.seed_defaults()
        for key in ("device", "sttModel", "ttsEngine"):
            self.assertEqual(self._stored(key), _expected_default(key),
                             "%s 应写入本平台默认值" % key)
        self.assertEqual(self._row("ttsEngine")["options"],
                         platform_options("ttsEngine", DEFAULTS["ttsEngine"]["options"]))
        self.assertEqual(settings.get("device"), _expected_default("device"))

    def test_macos_declarations_reach_the_database_and_the_panel(self):
        with self._as_platform(darwin):
            settings.seed_defaults()
            self.assertEqual(self._stored("device"), "cpu")
            self.assertEqual(self._stored("sttModel"), "base")
            self.assertEqual(self._stored("meetingSttModel"), "small")
            self.assertEqual(self._row("ttsEngine")["options"],
                             ["auto", "edge-tts", "say", "off"],
                             "面板下拉的候选项也要跟着平台走")
            settings._cache = None
            self.assertEqual(settings.get("device"), "cpu")
            self.assertEqual(settings.get("sttModel"), "base")

    def test_existing_database_gets_platform_options_on_reseed(self):
        """老库（键已存在）重新 seed 时，元数据（候选项）也要按平台刷新。"""
        settings.seed_defaults()                       # 先按本平台默认值建库
        before = self._stored("device")                # 库里已有的值
        # 挑一个 device 默认值与 before **不同**的声明来 patch："已有值不被平台默认值
        # 顶掉"这条断言才在 win/mac/linux 三种 runner 上都有效力（若 patch 成与本平台
        # 同值的声明，断言会退化成恒真）。
        other = next(m for m in (win32, darwin, linux)
                     if _declared_default(m, "device") != before)
        with self._as_platform(other):
            settings.seed_defaults()                   # 再按 other 平台的声明 seed
            self.assertEqual(self._row("ttsEngine")["options"],
                             list(other.PLATFORM_DEFAULTS["settingOptions"]["ttsEngine"]))
            # 但**已有值**不能被平台默认值改写（用户改过就得留着）
            self.assertEqual(self._stored("device"), before)

    def test_user_value_beats_platform_default(self):
        with self._as_platform(darwin):
            settings.update({"device": "cuda", "sttModel": "medium"})
            settings.seed_defaults()
            self.assertEqual(self._stored("device"), "cuda")
            self.assertEqual(self._stored("sttModel"), "medium")

    def test_reset_uses_the_platform_default(self):
        with self._as_platform(darwin):
            settings.seed_defaults()
            settings.update({"device": "auto"})
            settings.reset("device")
            self.assertEqual(self._stored("device"), "cpu",
                             "重置要回到本平台默认值，而不是硬编码的 Windows 值")

    def test_new_settings_instance_reads_platform_defaults(self):
        with self._as_platform(darwin):
            settings.seed_defaults()
            other = Settings()
            self.assertEqual(other.get("device"), "cpu")
            self.assertEqual(other.get("ttsEngine"), "auto")


class OfflineEngineIds(unittest.TestCase):
    """`speak()` 必须认得本平台的离线引擎 id（mac=say），否则选了它还会先走在线合成。"""

    def setUp(self):
        from app.audio import tts
        self.tts = tts
        self.edge = patch.object(tts, "_speak_edge", return_value=True)
        self.offline = patch.object(tts, "_speak_offline", return_value=True)
        self.m_edge = self.edge.start()
        self.m_offline = self.offline.start()
        self.addCleanup(self.edge.stop)
        self.addCleanup(self.offline.stop)

    def test_platform_offline_id_routes_offline(self):
        with patch.object(self.tts.echo_platform, "offline_tts_label", lambda: "say"):
            self.assertTrue(self.tts.speak("你好", "say"))
        self.m_offline.assert_called_once()
        self.m_edge.assert_not_called()

    def test_windows_value_still_works(self):
        """1.x 的 `sapi` 必须继续认（库里可能存着这个值）。"""
        with patch.object(self.tts.echo_platform, "offline_tts_label", lambda: "say"):
            self.assertTrue(self.tts.speak("你好", "sapi"))
        self.m_offline.assert_called_once()
        self.m_edge.assert_not_called()

    def test_engine_off_speaks_nothing(self):
        self.assertFalse(self.tts.speak("你好", "off"))
        self.m_offline.assert_not_called()
        self.m_edge.assert_not_called()

    def test_online_engine_still_goes_online(self):
        self.assertTrue(self.tts.speak("你好", "edge-tts"))
        self.m_edge.assert_called_once()
        self.m_offline.assert_not_called()

    def test_tts_online_status_reports_the_platform_engine(self):
        with patch.object(self.tts, "probe_online", return_value=False), \
                patch.object(self.tts.echo_platform, "offline_tts_label", lambda: "say"):
            st = self.tts.tts_online_status()
        self.assertEqual(st["engine"], "say")
        self.assertIn("say", st["detail"])


if __name__ == "__main__":
    unittest.main()
