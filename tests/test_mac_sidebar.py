"""macOS host integration without requiring AppKit or audio dependencies in CI."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


class MacSidebarTests(unittest.TestCase):
    def setUp(self):
        deps = {name: MagicMock() for name in (
            "app.assistant", "app.audio.wake", "app.config", "app.hotkey", "app.services",
        )}
        path = Path(__file__).resolve().parents[1] / "mac" / "mac_runtime.py"
        spec = importlib.util.spec_from_file_location("sidebar_runtime_test", path)
        self.runtime = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", deps):
            spec.loader.exec_module(self.runtime)
        self.values = {"panelOpenMode": "sidebar", "panelAutoStart": True,
                       "panelStartCollapsed": True, "serverPort": 9123}
        self.runtime.settings = MagicMock()
        self.runtime.settings.get.side_effect = self.values.get

    def test_startup_uses_non_toggling_commands(self):
        with patch.object(self.runtime, "_spawn_sidebar", return_value=True) as spawn:
            self.runtime.autostart_sidebar()
            spawn.assert_called_once_with("collapsed")
            self.values["panelStartCollapsed"] = False
            self.runtime.autostart_sidebar()
            self.assertEqual(spawn.call_args.args, ("expanded",))

    def test_browser_mode_and_disabled_autostart_do_not_spawn(self):
        with patch.object(self.runtime, "_spawn_sidebar") as spawn:
            self.values["panelOpenMode"] = "browser"
            self.runtime.autostart_sidebar()
            self.values["panelOpenMode"] = "sidebar"
            self.values["panelAutoStart"] = False
            self.runtime.autostart_sidebar()
            spawn.assert_not_called()

    def test_missing_binary_falls_back_to_browser(self):
        with patch.object(self.runtime, "_spawn_sidebar", return_value=False), \
                patch.object(self.runtime, "_open_browser", return_value=True) as browser:
            self.assertTrue(self.runtime.toggle_sidebar())
            browser.assert_called_once_with()

    def test_spawn_passes_configured_port_and_detaches(self):
        with patch.object(self.runtime, "sidebar_exe_path", return_value="/tmp/ECHO Sidebar"), \
                patch.object(self.runtime, "_retire_stale_sidebars") as retire, \
                patch.object(self.runtime.os, "makedirs"), \
                patch("builtins.open", unittest.mock.mock_open()), \
                patch.object(self.runtime.subprocess, "Popen") as popen:
            self.assertTrue(self.runtime._spawn_sidebar("toggle"))
            self.assertEqual(popen.call_args.args[0],
                             ["/tmp/ECHO Sidebar", "--port", "9123", "--command", "toggle",
                              "--data", self.runtime.paths.data_root()])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            # 换端口后必须先清掉开在旧端口上的浮动框，否则屏幕上会有两个边条
            retire.assert_called_once_with(9123)

    def test_spawn_prefers_actual_port_from_port_file(self):
        """首选端口被占、ECHO 让位后，浮动框必须按 echo-port.txt 的实际端口打开。"""
        with patch.object(self.runtime, "sidebar_exe_path", return_value="/tmp/ECHO Sidebar"), \
                patch.object(self.runtime, "_actual_port", return_value=8971), \
                patch.object(self.runtime, "_retire_stale_sidebars") as retire, \
                patch.object(self.runtime.os, "makedirs"), \
                patch("builtins.open", unittest.mock.mock_open()), \
                patch.object(self.runtime.subprocess, "Popen") as popen:
            self.assertTrue(self.runtime._spawn_sidebar("collapsed"))
            argv = popen.call_args.args[0]
            self.assertEqual(argv[argv.index("--port") + 1], "8971")
            retire.assert_called_once_with(8971)


if __name__ == "__main__":
    unittest.main()
