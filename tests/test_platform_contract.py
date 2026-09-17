# -*- coding: utf-8 -*-
"""跨平台契约测试：非 Windows 支持只能待在 mac/ 里，不得侵入 Windows 路径。

背景（2026-09-15 定的约定）
--------------------------
ECHO 的主平台是 Windows：热键、边条、PowerShell 启动链全是
Windows 实现。macOS 支持走 `mac/run_mac.py` 独立入口 —— 它在 `import app.main`
**之前**把 `hotkey_mac` / `mac_runtime` 塞进 `sys.modules`、覆盖 `app.config.DEFAULTS`
并给 TTS/通知打补丁，所以 `app/` 一行都不用改。

这条约定以前只是"注释里的君子协定"：谁顺手把 Mac 逻辑或 Mac 默认值写进 `app/`，
Windows 上就悄悄退化，而没有任何东西会红。本文件把它变成可执行断言：

  1. `app/`、`scripts/` 不得 import `mac` / `hotkey_mac` / `mac_runtime` / `notify_mac` / `tts_mac`;
  2. `app/` 里不得出现 `darwin` 分支（非 Windows 实现只放 mac/）;
  3. 共享 `app/config.py` 的默认值必须是 Windows 值（Mac 覆盖只允许发生在 mac/ 入口）;
  4. Windows 上真实 import `app.main` 后不得加载任何 mac 模块;
  5. mac 入口必须仍然是"注入式"的（存在且使用 sys.modules / DEFAULTS）。

本文件只依赖标准库 + `app.config`（后者只 import os/sqlite），所以 CI 的静态 job
不需要装 torch 之类的重依赖也能跑。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAC_MODULES = ("mac", "run_mac", "hotkey_mac", "mac_runtime", "notify_mac", "tts_mac")


def _py_files(*dirs):
    """遍历给定目录下的 .py 文件（跳过 __pycache__ / build 产物）。"""
    for d in dirs:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in ("__pycache__", "build")]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def _import_lines(path):
    """产出 (行号, 去掉注释后的代码行) —— 只看 import 语句，不看字符串/注释。"""
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f.read().splitlines(), 1):
            yield i, line.split("#", 1)[0].strip()


class SharedCodeIsMacFree(unittest.TestCase):
    def test_app_and_scripts_do_not_import_mac_modules(self):
        bad = []
        for path in _py_files("app", "scripts"):
            for i, code in _import_lines(path):
                for mod in MAC_MODULES:
                    if (code.startswith("import " + mod + " ")
                            or code == "import " + mod
                            or code.startswith("from " + mod + " import ")
                            or code.startswith("from " + mod + ".")):
                        bad.append("%s:%d: %s" % (os.path.relpath(path, ROOT), i, code))
        self.assertEqual(
            bad, [],
            "app/ 与 scripts/ 不得引用 mac 专用模块（Mac 支持必须走 mac/ 独立入口）：\n" + "\n".join(bad))

    def test_app_has_no_darwin_branch(self):
        bad = []
        for path in _py_files("app"):
            for i, code in _import_lines(path):
                if "darwin" in code.lower():
                    bad.append("%s:%d: %s" % (os.path.relpath(path, ROOT), i, code))
        self.assertEqual(
            bad, [],
            "app/ 里不得出现 darwin 分支：非 Windows 实现只能放 mac/，"
            "跨平台差异用 os.name 判断并在两个平台各自验证：\n" + "\n".join(bad))


class SharedDefaultsStayWindows(unittest.TestCase):
    """Mac 入口靠覆盖 DEFAULTS 生效；一旦有人把 Mac 默认值写进共享配置，
    Windows 会静默降级（例如 device 变 cpu 就白扔了 CUDA）。"""

    def test_runtime_defaults_are_windows_values(self):
        import app.config as config
        self.assertEqual(config.DEFAULTS["device"]["value"], "auto")
        self.assertEqual(config.DEFAULTS["sttModel"]["value"], "sensevoice")
        self.assertEqual(config.DEFAULTS["meetingSttModel"]["value"], "sensevoice")


class MacSupportStaysIsolated(unittest.TestCase):
    def test_mac_entry_is_injection_based(self):
        path = os.path.join(ROOT, "mac", "run_mac.py")
        self.assertTrue(os.path.isfile(path), "mac/run_mac.py 必须存在（macOS 唯一入口）")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("sys.modules", src, "mac 入口必须用 sys.modules 注入，而不是改 app/")
        self.assertIn("DEFAULTS", src, "mac 入口必须在运行时覆盖 DEFAULTS")

    @unittest.skipUnless(sys.platform == "win32", "仅在 Windows 上验证真实入口")
    def test_windows_entry_does_not_load_mac_modules(self):
        try:
            import app.main  # noqa: F401
        except ImportError as e:
            # 缺重依赖（torch/sounddevice/funasr…）时跳过，避免把环境问题误报成兼容性问题
            self.skipTest("重依赖缺失，跳过真实导入：%s" % e)
        loaded = sorted(m for m in sys.modules if m in MAC_MODULES or m.startswith("mac."))
        self.assertEqual(loaded, [], "Windows 入口不应加载 mac 模块，却发现了：%s" % loaded)


if __name__ == "__main__":
    unittest.main()
