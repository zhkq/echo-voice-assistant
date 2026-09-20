# -*- coding: utf-8 -*-
"""热键/媒体键 + 运行时的 characterization 测试（§13.8 安全网第 2 项，P3 前置）

"characterization" = 把**当前行为**钉住，而不是断言"应该怎样"。原因很实际：
P3 要把 `app/hotkey.py`、`app/runtime.py` 里的平台专有实现（ctypes/Windows 钩子/
ShellExecute/creationflags）搬进 `app/platform/<os>/` 接缝。搬之前必须先知道
"现在的行为是什么样"，否则搬完只剩下"看起来还能用"。

本文件钉住的四类行为（都是历史上真出过问题或真会被踩的地方）：

  1. `parse_hotkey_combo`：哪些写法合法、修饰键怎么拼、无效写法返回什么；
  2. `MEDIA_KEYS` 的键码表与 `config.triggerKeys` 选项的对应（两处漂移 = 媒体键失灵）；
  3. `HotkeyListener`：注册哪些配置键、注册失败怎么处理、低级钩子里媒体键命中后
     `on_trigger` + 拦截（`consumeMediaKey`）的先后与返回值；
  4. `runtime._hotkey_cb` 的分派矩阵 + 边条的启动参数/跳过原因 + 打开面板的去抖；
  5. `runtime.start/stop_hotkey|wake` 的幂等与状态上报。

⚠️ P3 搬迁后：Windows 实现在 ``app/platform/win32/hotkey.py``，``app/hotkey.py`` 变成
**门面**（按平台转发）。本文件里带 ``skip_without_hotkey`` 的用例针对 Windows 实现
（ctypes 钩子那套），非 Windows 上整组跳过；门面本身另有一组用例（``FacadeTests``）。
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                         # noqa: E402
from app import runtime                                     # noqa: E402
from app import hotkey as hotkey_facade                     # noqa: E402
from app.config import DEFAULTS, settings                   # noqa: E402

try:                       # 非 Windows：ctypes.windll 不存在，Windows 实现整组跳过
    from app.platform.win32 import hotkey
except Exception:          # pragma: no cover - 平台差异
    hotkey = None

skip_without_hotkey = unittest.skipIf(
    hotkey is None, "Windows 热键实现在 app/platform/win32/hotkey.py（非 Windows 跳过）")


class _FakeUser32:
    """替身 user32：记录 RegisterHotKey / CallNextHookEx 的调用。"""

    CHAIN_SENTINEL = 42

    def __init__(self, register_ok=True):
        self.register_ok = register_ok
        self.registered = []          # [(hid, mods, vk)]
        self.chained = 0

    def RegisterHotKey(self, hwnd, hid, mods, vk):
        self.registered.append((hid, mods, vk))
        return 1 if self.register_ok else 0

    def CallNextHookEx(self, hook, nCode, wParam, lParam):
        self.chained += 1
        return self.CHAIN_SENTINEL


def _patch_settings_get(mapping, default=None):
    """用 patch 替掉单例 settings.get（不污染其它用例）。"""
    def _get(key, dflt=None):
        if key in mapping:
            return mapping[key]
        return dflt if dflt is not None else (default.get(key) if default else None)
    return patch.object(settings, "get", side_effect=_get)


def _patch_active_port(port):
    """把「实际监听端口」钉死——**凡是用例要断言 URL 里的端口，就必须带上这个**。

    `runtime` 开面板/边条取的是 `ports.active_port(settings.serverPort)`：**活动**端口，
    不是配置端口（ECHO 让位后 `echo-port.txt` 才是权威，理由见 `app/ports.py::active_port`
    里的 2026-09-20 事故）。只打 `settings.get` 会漏到跑测试这台机器的
    `data/echo-port.txt`：开发机上写着 18060，于是断言 8970 的用例变红（2026-09-21 真实
    拦到 3 个），而恰好断言 18060 的用例只是**碰巧**过 —— 那是环境泄漏，不是被测行为。
    """
    return patch.object(runtime.ports, "active_port", return_value=port)


class FacadeTests(unittest.TestCase):
    """`app/hotkey.py` 是门面：必须转发到当前平台的实现（P3 契约）。"""

    def test_facade_points_at_the_platform_implementation(self):
        from app import platform as echo_platform
        impl = echo_platform.hotkey_impl()
        self.assertIs(hotkey_facade.impl, impl)
        self.assertIs(hotkey_facade.HotkeyListener, impl.HotkeyListener)

    def test_facade_is_importable_on_every_platform(self):
        """门面不许在 import 期就依赖 Windows（这是 1.x 的结构性缺陷）。"""
        self.assertTrue(callable(hotkey_facade.HotkeyListener))
        self.assertTrue(callable(hotkey_facade.parse_hotkey_combo))

    @unittest.skipUnless(hotkey is not None, "仅在 Windows 上比较 Windows 实现")
    def test_windows_facade_matches_the_implementation(self):
        self.assertIs(hotkey_facade.parse_hotkey_combo, hotkey.parse_hotkey_combo)
        self.assertEqual(hotkey_facade.MEDIA_KEYS, hotkey.MEDIA_KEYS)
        self.assertEqual(hotkey_facade.NAMED_KEYS, hotkey.NAMED_KEYS)

    def test_facade_does_not_leak_windows_only_constants(self):
        """门面只暴露跨平台数据面：Windows 的键码/消息常量不许出现在这里。

        （守卫测试 test_path_seam 的 WINDOWS_API 规则会拦；这条是行为侧的对照。）
        """
        for name in ("MOD_NOREPEAT", "WM_KEYDOWN", "KBDLLHOOKSTRUCT", "user32"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(hotkey_facade, name))

    def test_seam_resolves_a_different_implementation_per_platform(self):
        """S8 的实测形态：不再靠 sys.modules 注入，接缝自己按平台选实现。

        在 Windows 上把 `echo_platform.current()` 换成 darwin/linux 后，
        `hotkey_impl()` 必须给出 POSIX 实现（pynput 的 HotkeyListener）——这正是
        P3 要替换掉的 ``mac/run_mac.py`` 注入式入口所做的事，现在由接缝承担。
        """
        from unittest.mock import patch as _patch

        from app import platform as echo_platform
        from app.platform import _posix_hotkey

        win_impl = echo_platform.hotkey_impl()
        self.assertTrue(win_impl.__name__.endswith("win32.hotkey"),
                        "Windows 上应解析到 win32 实现，实际 %s" % win_impl.__name__)
        for name in ("darwin", "linux"):
            with self.subTest(platform=name):
                with _patch.object(echo_platform, "current", lambda n=name: n):
                    impl = echo_platform.hotkey_impl()
                    self.assertTrue(impl.__name__.endswith("%s.hotkey" % name))
                    self.assertIs(impl.HotkeyListener, _posix_hotkey.HotkeyListener)


@skip_without_hotkey
class HotkeyComboParseTests(unittest.TestCase):
    def test_ctrl_alt_letter(self):
        mods, vk = hotkey.parse_hotkey_combo("Ctrl+Alt+V")
        self.assertEqual(mods, hotkey.MOD_CONTROL | hotkey.MOD_ALT | hotkey.MOD_NOREPEAT)
        self.assertEqual(vk, ord("V"))

    def test_case_and_space_insensitive(self):
        for combo in ("ctrl+alt+v", "CTRL + ALT + v", "  Ctrl+Alt+V  ", "ctrl+alt+V"):
            with self.subTest(combo=combo):
                self.assertEqual(hotkey.parse_hotkey_combo(combo),
                                 hotkey.parse_hotkey_combo("Ctrl+Alt+V"))

    def test_shift_and_win(self):
        mods, vk = hotkey.parse_hotkey_combo("Ctrl+Shift+E")
        self.assertEqual(mods, (hotkey.MOD_CONTROL | hotkey.MOD_SHIFT | hotkey.MOD_NOREPEAT))
        self.assertEqual(vk, ord("E"))
        mods, vk = hotkey.parse_hotkey_combo("Win+Space")
        self.assertEqual(mods, hotkey.MOD_WIN | hotkey.MOD_NOREPEAT)
        self.assertEqual(vk, hotkey.NAMED_KEYS["SPACE"])

    def test_digits_and_named_keys(self):
        self.assertEqual(hotkey.parse_hotkey_combo("Ctrl+1")[1], ord("1"))
        self.assertEqual(hotkey.parse_hotkey_combo("Ctrl+F12")[1], 0x7B)
        self.assertEqual(hotkey.parse_hotkey_combo("Ctrl+Enter")[1], 0x0D)
        self.assertEqual(hotkey.parse_hotkey_combo("Ctrl+Esc")[1], 0x1B)

    def test_unknown_or_empty_modifier_is_rejected(self):
        """不认识的修饰键必须拒绝：静默忽略会退化成"全局裸键"热键。

        修正前 `"Meta+V"` / `"+V"` 会解析成只剩 MOD_NOREPEAT 的 V —— 装上去
        会把系统里所有 V 键都吞掉（而单写 `"V"` 早就被拒了，判据原本不一致）。
        """
        for combo in ("Meta+V", "+V", "Ctrl++V", "Ctrl+Meta+V"):
            with self.subTest(combo=combo):
                self.assertIsNone(hotkey.parse_hotkey_combo(combo))

    def test_invalid_combos_return_none(self):
        for combo in (None, "", "V", "Ctrl", "Ctrl+", "Ctrl+NoSuchKey", "+V", "F13+Ctrl"):
            with self.subTest(combo=combo):
                self.assertIsNone(hotkey.parse_hotkey_combo(combo))

    def test_named_key_aliases_map_to_the_same_vk(self):
        for alias in (("ENTER", "RETURN"), ("ESC", "ESCAPE"), ("PGUP", "PAGEUP"),
                      ("PGDN", "PAGEDOWN")):
            with self.subTest(alias=alias):
                self.assertEqual(hotkey.NAMED_KEYS[alias[0]], hotkey.NAMED_KEYS[alias[1]])

    def test_function_keys_cover_f1_to_f24(self):
        for i in range(1, 25):
            self.assertEqual(hotkey.NAMED_KEYS["F%d" % i], 0x70 + (i - 1))


@skip_without_hotkey
class MediaKeyTableTests(unittest.TestCase):
    def test_media_key_codes_are_pinned(self):
        """这些虚拟键码是 Windows 约定值，改了就等于媒体键全废。"""
        self.assertEqual(hotkey.MEDIA_KEYS, {
            "vol_up": 0xAF, "vol_down": 0xAE, "vol_mute": 0xAD,
            "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
        })

    def test_every_configurable_trigger_key_exists_in_the_table(self):
        """配置里能勾的每个媒体键都必须有实现，否则勾了没反应（静默失灵）。"""
        for name in DEFAULTS["triggerKeys"]["options"]:
            with self.subTest(name=name):
                self.assertIn(name, hotkey.MEDIA_KEYS)


@skip_without_hotkey
class HotkeyRegistrationTests(unittest.TestCase):
    """`_register_hotkeys()`：读哪些配置键、失败怎么处理。"""

    def setUp(self):
        self._log = patch.object(db, "add_log", lambda *a, **k: None)
        self._log.start()
        self.addCleanup(self._log.stop)

    def test_registers_the_three_configured_hotkeys_in_order(self):
        fake = _FakeUser32()
        hk = hotkey.HotkeyListener(lambda k, d=None: d)
        values = {"wakeHotkey": "Ctrl+Alt+C",
                  "fallbackHotkey": "Ctrl+Alt+V",
                  "panelHotkey": "Ctrl+Shift+E"}
        with patch.object(hotkey, "user32", fake), \
                patch.object(hk, "settings_get", lambda k, d=None: values.get(k, d)):
            regs = hk._register_hotkeys()
        self.assertEqual([k for k, _ in regs],
                         ["wakeHotkey", "fallbackHotkey", "panelHotkey"])
        self.assertEqual([m for _, m in regs],
                         ["Ctrl+Alt+C", "Ctrl+Alt+V", "Ctrl+Shift+E"])
        self.assertEqual(len(fake.registered), 3)
        self.assertEqual([hid for hid, _, _ in fake.registered], [1, 2, 3])

    def test_failed_registration_is_skipped_and_does_not_raise(self):
        """组合键被别的程序占用是常见情况：必须只跳过它，不影响其它键。"""
        fake = _FakeUser32(register_ok=False)
        hk = hotkey.HotkeyListener(lambda k, d=None: d)
        values = {"wakeHotkey": "Ctrl+Alt+C", "panelHotkey": "Ctrl+Shift+E"}
        with patch.object(hotkey, "user32", fake), \
                patch.object(hk, "settings_get", lambda k, d=None: values.get(k, d)):
            regs = hk._register_hotkeys()
        self.assertEqual(regs, [])
        self.assertEqual(len(fake.registered), 2, "失败了也要真的试过")
        self.assertEqual(hk._hotkey_ids, {})

    def test_invalid_combo_never_reaches_the_os(self):
        fake = _FakeUser32()
        hk = hotkey.HotkeyListener(lambda k, d=None: d)
        values = {"wakeHotkey": "not-a-combo", "fallbackHotkey": "", "panelHotkey": None}
        with patch.object(hotkey, "user32", fake), \
                patch.object(hk, "settings_get", lambda k, d=None: values.get(k, d)):
            self.assertEqual(hk._register_hotkeys(), [])
        self.assertEqual(fake.registered, [])

    def test_missing_settings_gives_empty_result(self):
        fake = _FakeUser32()
        hk = hotkey.HotkeyListener(lambda k, d=None: d)
        with patch.object(hotkey, "user32", fake), \
                patch.object(hk, "settings_get", lambda k, d=None: d):
            self.assertEqual(hk._register_hotkeys(), [])


@skip_without_hotkey
class HookProcTests(unittest.TestCase):
    """低级键盘钩子回调：媒体键的判定、派发与拦截。

    这是 §27 那条"媒体键到底有没有被吞掉/有没有派发"的代码路径，之前完全没有测试。
    """

    def setUp(self):
        self._log = patch.object(db, "add_log", lambda *a, **k: None)
        self._log.start()
        self.addCleanup(self._log.stop)
        self.fake = _FakeUser32()
        self.patched = patch.object(hotkey, "user32", self.fake)
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def _call(self, vk, wparam=None, settings_map=None, ncode=0):
        seen = []
        hk = hotkey.HotkeyListener(
            lambda k, d=None: (settings_map or {}).get(k, d),
            on_trigger=lambda s, d: seen.append((s, d)))
        proc = hk._make_hook_proc()
        kbd = hotkey.KBDLLHOOKSTRUCT()
        kbd.vkCode = vk
        import ctypes
        rc = proc(ncode, hotkey.WM_KEYDOWN if wparam is None else wparam,
                  ctypes.addressof(kbd))
        return rc, seen

    def test_media_key_triggers_and_is_consumed(self):
        rc, seen = self._call(hotkey.MEDIA_KEYS["play_pause"],
                              settings_map={"consumeMediaKey": True})
        self.assertEqual(seen, [("mediakey", "play_pause")])
        self.assertEqual(rc, 1, "consumeMediaKey=True 时返回 1 = 不向系统透传")

    def test_media_key_passes_through_when_not_consuming(self):
        rc, seen = self._call(hotkey.MEDIA_KEYS["vol_up"],
                              settings_map={"consumeMediaKey": False})
        self.assertEqual(seen, [("mediakey", "vol_up")])
        self.assertEqual(rc, self.fake.CHAIN_SENTINEL, "不拦就必须把事件链下去")
        self.assertEqual(self.fake.chained, 1)

    def test_consume_defaults_to_true_when_setting_missing(self):
        rc, seen = self._call(hotkey.MEDIA_KEYS["next"], settings_map={})
        self.assertEqual(seen, [("mediakey", "next")])
        self.assertEqual(rc, 1)

    def test_ordinary_keys_are_chained_without_trigger(self):
        rc, seen = self._call(0x41, settings_map={"consumeMediaKey": True})   # 'A'
        self.assertEqual(seen, [])
        self.assertEqual(rc, self.fake.CHAIN_SENTINEL)

    def test_keyup_does_not_trigger(self):
        rc, seen = self._call(hotkey.MEDIA_KEYS["play_pause"], wparam=hotkey.WM_KEYUP)
        self.assertEqual(seen, [])
        self.assertEqual(rc, self.fake.CHAIN_SENTINEL)

    def test_negative_ncode_is_passed_through_immediately(self):
        """nCode<0 时按 Windows 约定不得处理事件（也不该去解引用 lParam）。"""
        rc, seen = self._call(hotkey.MEDIA_KEYS["play_pause"], ncode=-1)
        self.assertEqual(seen, [])
        self.assertEqual(rc, self.fake.CHAIN_SENTINEL)

    def test_trigger_exception_is_swallowed_and_key_still_consumed(self):
        """当前行为：on_trigger 抛异常被静默吞掉（§27 的待办：加日志）。"""
        hk = hotkey.HotkeyListener(lambda k, d=None: True,
                                   on_trigger=lambda s, d: (_ for _ in ()).throw(RuntimeError("boom")))
        proc = hk._make_hook_proc()
        kbd = hotkey.KBDLLHOOKSTRUCT()
        kbd.vkCode = hotkey.MEDIA_KEYS["play_pause"]
        import ctypes
        rc = proc(0, hotkey.WM_KEYDOWN, ctypes.addressof(kbd))
        self.assertEqual(rc, 1)


class HotkeyCallbackDispatchTests(unittest.TestCase):
    """`runtime._hotkey_cb` 的分派矩阵（纯逻辑，不碰 Windows API）。"""

    def setUp(self):
        self.capture = patch.object(runtime.assistant, "capture")
        self.toggle = patch.object(runtime, "toggle_sidebar")
        self.open_win = patch.object(runtime, "open_panel_window")
        self.m_capture = self.capture.start()
        self.m_toggle = self.toggle.start()
        self.m_open = self.open_win.start()
        for m in (self.capture, self.toggle, self.open_win):
            self.addCleanup(m.stop)

    def _with_settings(self, **values):
        base = {"panelOpenMode": "sidebar", "triggerKeys": ["vol_up"]}
        base.update(values)
        return _patch_settings_get(base)

    def test_panel_hotkey_uses_sidebar_by_default(self):
        with self._with_settings():
            runtime._hotkey_cb("hotkey", "panelHotkey")
        self.m_toggle.assert_called_once_with()
        self.m_open.assert_not_called()
        self.m_capture.assert_not_called()

    def test_panel_hotkey_opens_full_window_when_configured(self):
        for mode in ("app", "browser"):
            with self.subTest(mode=mode):
                self.m_open.reset_mock()
                with self._with_settings(panelOpenMode=mode):
                    runtime._hotkey_cb("hotkey", "panelHotkey")
                self.m_open.assert_called_once_with()
                self.m_toggle.assert_not_called()

    def test_wake_and_fallback_hotkeys_start_a_capture(self):
        for key in ("wakeHotkey", "fallbackHotkey"):
            with self.subTest(key=key):
                self.m_capture.reset_mock()
                with self._with_settings():
                    runtime._hotkey_cb("hotkey", key)
                self.m_capture.assert_called_once_with("hotkey")

    def test_trigger_key_starts_a_capture(self):
        with self._with_settings(triggerKeys=["vol_up", "play_pause"]):
            runtime._hotkey_cb("mediakey", "play_pause")
        self.m_capture.assert_called_once_with("mediakey")

    def test_media_key_outside_trigger_keys_is_silently_ignored(self):
        """**当前行为**：不在 triggerKeys 里的媒体键什么都不做、也不报错。

        §27 排查"媒体键没反应"时，正是这条静默分支让日志里一片空白。
        钉住它，是为了 P3/后续加固时能一眼看出行为变了没有（加日志 ≠ 改行为）。
        """
        with self._with_settings(triggerKeys=["vol_up"]):
            runtime._hotkey_cb("mediakey", "next")
        self.m_capture.assert_not_called()

    def test_empty_or_missing_trigger_keys_falls_back_to_vol_up(self):
        with self._with_settings(triggerKeys=[]):
            runtime._hotkey_cb("mediakey", "vol_up")
        self.m_capture.assert_called_once_with("mediakey")

    def test_unknown_source_is_ignored(self):
        with self._with_settings():
            runtime._hotkey_cb("who-knows", "whatever")
        self.m_capture.assert_not_called()
        self.m_toggle.assert_not_called()
        self.m_open.assert_not_called()


class SidebarLifecycleTests(unittest.TestCase):
    """边条：可执行文件探测、启动参数、自动显示的跳过原因。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-sidebar-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _make_exe(self, *rel):
        path = os.path.join(self.tmp, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")
        return path

    def test_release_build_wins_over_debug(self):
        rel = ("sidebar", "bin")
        debug = self._make_exe(*rel, "Debug", "net7.0-windows", "win-x64", "echo-sidebar.exe")
        with patch.object(runtime, "BASE_DIR", self.tmp):
            self.assertEqual(runtime.sidebar_exe_path(), debug)
            release = self._make_exe(*rel, "Release", "net7.0-windows", "win-x64",
                                     "echo-sidebar.exe")
            self.assertEqual(runtime.sidebar_exe_path(), release)

    def test_missing_exe_returns_none(self):
        with patch.object(runtime, "BASE_DIR", self.tmp):
            self.assertIsNone(runtime.sidebar_exe_path())

    def test_spawn_arguments_and_flags_are_pinned(self):
        exe = self._make_exe("sidebar", "bin", "Release", "net7.0-windows", "win-x64",
                             "echo-sidebar.exe")
        # 首选端口 8970、活动端口 8971：边条必须开在**活动**端口上（见 _patch_active_port）。
        with patch.object(runtime, "BASE_DIR", self.tmp), \
                patch.object(runtime, "sidebar_exe_path", return_value=exe), \
                patch.object(runtime.subprocess, "Popen") as popen, \
                _patch_settings_get({"serverPort": 8970}), \
                _patch_active_port(8971):
            self.assertTrue(runtime._spawn_sidebar(collapsed=True))
        args, kwargs = popen.call_args
        argv = args[0]
        self.assertEqual(argv[0], exe)
        self.assertIn("--url=http://127.0.0.1:8971/", argv)
        self.assertIn("--width=450", argv)
        self.assertIn("--collapsed", argv)
        self.assertEqual(kwargs["cwd"], os.path.dirname(exe))
        self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x00000200)
        self.assertTrue(kwargs["close_fds"])

    def test_spawn_without_exe_is_false(self):
        with patch.object(runtime, "sidebar_exe_path", return_value=None):
            self.assertFalse(runtime._spawn_sidebar())

    def test_autostart_skip_reasons(self):
        exe = self._make_exe("sidebar", "bin", "Release", "net7.0-windows", "win-x64",
                             "echo-sidebar.exe")
        cases = [
            ({"panelOpenMode": "app", "panelAutoStart": True}, "skip: panelOpenMode != sidebar"),
            ({"panelOpenMode": "sidebar", "panelAutoStart": False}, "skip: panelAutoStart=False"),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                with _patch_settings_get(values):
                    self.assertEqual(runtime.autostart_sidebar(), expected)

        # 边条已在运行 → 不打扰（否则会把用户展开的面板收起来）
        with patch.object(runtime, "_sidebar_running", return_value=True), \
                _patch_settings_get({"panelOpenMode": "sidebar", "panelAutoStart": True}):
            self.assertEqual(runtime.autostart_sidebar(), "skip: already running")

        # 边条没编译出来 → 不报错，只是跳过
        with patch.object(runtime, "_sidebar_running", return_value=False), \
                patch.object(runtime, "sidebar_exe_path", return_value=None), \
                _patch_settings_get({"panelOpenMode": "sidebar", "panelAutoStart": True}):
            self.assertEqual(runtime.autostart_sidebar(), "skip: sidebar exe not built")

    def test_autostart_spawns_collapsed_by_default(self):
        with patch.object(runtime, "_sidebar_running", return_value=False), \
                patch.object(runtime, "sidebar_exe_path", return_value="X:\\exe"), \
                patch.object(runtime, "_spawn_sidebar", return_value=True) as spawn, \
                _patch_settings_get({"panelOpenMode": "sidebar", "panelAutoStart": True}):
            self.assertEqual(runtime.autostart_sidebar(), "started: collapsed=True")
        spawn.assert_called_once_with(collapsed=True)

    def test_autostart_reports_errors_instead_of_raising(self):
        with patch.object(runtime, "_sidebar_running", side_effect=RuntimeError("boom")), \
                _patch_settings_get({"panelOpenMode": "sidebar", "panelAutoStart": True}):
            self.assertTrue(runtime.autostart_sidebar().startswith("error:"))

    def test_toggle_falls_back_to_full_window_without_exe(self):
        with patch.object(runtime, "sidebar_exe_path", return_value=None), \
                patch.object(runtime, "open_panel_window", return_value=True) as open_win:
            self.assertTrue(runtime.toggle_sidebar())
        open_win.assert_called_once_with()


class PanelWindowTests(unittest.TestCase):
    """打开整窗：URL 用**活动**端口（echo-port.txt 权威，不是配置里的首选端口）、1.5 秒去抖、
    优先 Chromium --app。"""

    def setUp(self):
        runtime._panel_last_open = 0.0
        self.addCleanup(setattr, runtime, "_panel_last_open", 0.0)

    def _fake_ctypes(self, calls):
        def _exec(hwnd, op, target, params, cwd, show):
            calls.append({"op": op, "target": target, "params": params, "show": show})
            return 1
        return types.SimpleNamespace(
            windll=types.SimpleNamespace(shell32=types.SimpleNamespace(ShellExecuteW=_exec)))

    def test_uses_active_port_and_default_browser(self):
        calls = []
        with patch.dict(sys.modules, {"ctypes": self._fake_ctypes(calls)}), \
                patch.object(runtime.echo_platform, "chromium_candidates", return_value=[]), \
                _patch_settings_get({"serverPort": 18060, "panelOpenMode": "app"}), \
                _patch_active_port(18060):
            self.assertTrue(runtime.open_panel_window())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target"], "http://127.0.0.1:18060/")
        self.assertIsNone(calls[0]["params"])

    def test_url_uses_the_active_port_not_the_configured_one(self):
        """让位场景（2026-09-20 事故）：ECHO 从 8970 让位到 8971，面板若仍按配置的 8970
        打开，页面会渲染出来但所有请求失败（点按钮报 "Load failed"）。"""
        exe = os.path.join(tempfile.gettempdir(), "echo-fake-chrome-reloc.exe")
        with open(exe, "wb") as fh:
            fh.write(b"x")
        self.addCleanup(os.remove, exe)
        calls = []
        with patch.dict(sys.modules, {"ctypes": self._fake_ctypes(calls)}), \
                patch.object(runtime.echo_platform, "chromium_candidates", return_value=[exe]), \
                _patch_settings_get({"serverPort": 8970, "panelOpenMode": "app"}), \
                _patch_active_port(8971):
            self.assertTrue(runtime.open_panel_window())
        self.assertEqual(calls[0]["target"], exe)
        self.assertEqual(calls[0]["params"], "--app=http://127.0.0.1:8971/")

    def test_second_call_within_debounce_is_ignored(self):
        calls = []
        with patch.dict(sys.modules, {"ctypes": self._fake_ctypes(calls)}), \
                patch.object(runtime.echo_platform, "chromium_candidates", return_value=[]), \
                _patch_settings_get({"serverPort": 18060, "panelOpenMode": "app"}), \
                _patch_active_port(18060):
            self.assertTrue(runtime.open_panel_window())
            self.assertFalse(runtime.open_panel_window(), "1.5 秒内的重复触发要忽略")
        self.assertEqual(len(calls), 1)

    def test_chromium_app_mode_uses_app_flag(self):
        calls = []
        exe = os.path.join(tempfile.gettempdir(), "echo-fake-chrome.exe")
        with open(exe, "wb") as fh:
            fh.write(b"x")
        self.addCleanup(os.remove, exe)
        try:
            with patch.dict(sys.modules, {"ctypes": self._fake_ctypes(calls)}), \
                    patch.object(runtime.echo_platform, "chromium_candidates",
                                 return_value=[exe]), \
                    _patch_settings_get({"serverPort": 8970, "panelOpenMode": "app"}), \
                    _patch_active_port(8970):
                self.assertTrue(runtime.open_panel_window())
            self.assertEqual(calls[0]["target"], exe)
            self.assertEqual(calls[0]["params"], "--app=http://127.0.0.1:8970/")
        finally:
            pass

    def test_browser_mode_ignores_chromium_candidates(self):
        calls = []
        with patch.dict(sys.modules, {"ctypes": self._fake_ctypes(calls)}), \
                patch.object(runtime.echo_platform, "chromium_candidates",
                             return_value=["X:\\chrome.exe"]), \
                _patch_settings_get({"serverPort": 8970, "panelOpenMode": "browser"}), \
                _patch_active_port(8970):
            self.assertTrue(runtime.open_panel_window())
        self.assertEqual(calls[0]["target"], "http://127.0.0.1:8970/")


class ListenerLifecycleTests(unittest.TestCase):
    """`start_hotkey/stop_hotkey/start_wake/stop_wake`：幂等 + 状态上报。"""

    def setUp(self):
        self._log = patch.object(db, "add_log", lambda *a, **k: None)
        self._log.start()
        self.addCleanup(self._log.stop)
        runtime._hotkey = None
        runtime._wake = None
        self.addCleanup(setattr, runtime, "_hotkey", None)
        self.addCleanup(setattr, runtime, "_wake", None)

    class _FakeListener:
        def __init__(self, *a, **kw):
            self.started = False
            self.shutdown_called = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started and not self.shutdown_called

        def shutdown(self):
            self.shutdown_called = True

    def test_hotkey_start_is_idempotent(self):
        with patch.object(runtime, "HotkeyListener", self._FakeListener), \
                patch.object(runtime.services, "report_hotkey") as report:
            ok, msg = runtime.start_hotkey()
            self.assertTrue(ok)
            self.assertEqual(msg, "热键监听已启动")
            ok2, msg2 = runtime.start_hotkey()
            self.assertTrue(ok2)
            self.assertEqual(msg2, "热键监听已在运行")
        self.assertEqual(report.call_count, 1, "已在运行不该重复上报")

    def test_hotkey_stop_reports_offline_even_when_never_started(self):
        with patch.object(runtime.services, "report_hotkey") as report:
            self.assertEqual(runtime.stop_hotkey(), (True, "热键监听已停止"))
        report.assert_called_once_with("offline", "已停止")

    def test_stop_shuts_the_listener_down(self):
        with patch.object(runtime, "HotkeyListener", self._FakeListener), \
                patch.object(runtime.services, "report_hotkey"):
            runtime.start_hotkey()
            listener = runtime._hotkey
            runtime.stop_hotkey()
        self.assertTrue(listener.shutdown_called)
        self.assertIsNone(runtime._hotkey)

    def test_wake_is_gated_by_config(self):
        with patch.object(runtime, "WakeListener", self._FakeListener), \
                patch.object(runtime.services, "report_wake"):
            with _patch_settings_get({"wakeEnabled": False}):
                results = runtime.start_all()
            self.assertEqual(len(results), 1, "未启用唤醒时只启动热键")
            self.assertIsNone(runtime._wake)

            with _patch_settings_get({"wakeEnabled": True}):
                results = runtime.start_all()
            self.assertEqual(len(results), 2)
            self.assertIsNotNone(runtime._wake)

    def test_wake_disabled_reports_disabled_state(self):
        with patch.object(runtime, "WakeListener", self._FakeListener), \
                patch.object(runtime.services, "report_wake") as report, \
                patch.object(runtime.services, "report_hotkey"):
            with _patch_settings_get({"wakeEnabled": False}):
                runtime.start_all()
        self.assertIn(("disabled", "未启用"), [c.args for c in report.call_args_list])


if __name__ == "__main__":
    unittest.main()
