# -*- coding: utf-8 -*-
"""版本号一致性测试：`app/__init__.py:__version__` 是唯一权威来源。

背景（2026-09-18）
-----------------
发版前版本号硬编码在 4 处：`app/__init__.py`、`app/api.py`（/api/status）、
`app/main.py`（FastAPI title）、`pyproject.toml`。结果是面板显示的版本与 tag /
交付包名对不上，用户报障时无法确认他跑的是哪一版。

本文件把它变成可执行断言，且**只用标准库 + 读源码文本**（不 import app.api，
以免把 fastapi/assistant 这些重依赖拖进 CI 的 static job）：

  1. `app.__version__` 必须形如 X.Y.Z；
  2. `pyproject.toml` 的 version 必须与它一致；
  3. `app/api.py` / `app/main.py` 里**不得再出现硬编码的版本字面量**（必须引用 __version__）；
  4. `README.md` 里出现的版本号必须与它一致（发版时 README 也要改）。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# 匹配 version="1.2.3" / "version": "1.2.3" / version = "1.2.3"
HARDCODED = re.compile(r"""(?:version\s*[=:]\s*["'])(\d+\.\d+\.\d+)["']""")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class VersionIsSingleSourced(unittest.TestCase):
    def test_version_is_semver(self):
        import app
        self.assertTrue(SEMVER.match(app.__version__),
                        f"app.__version__ 必须是 X.Y.Z：{app.__version__!r}")

    def test_pyproject_matches(self):
        import app
        m = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), re.M)
        self.assertIsNotNone(m, "pyproject.toml 里找不到 version")
        self.assertEqual(m.group(1), app.__version__,
                         "pyproject.toml 的 version 与 app/__init__.py 不一致")

    def test_api_and_main_do_not_hardcode_version(self):
        import app
        for rel in ("app/api.py", "app/main.py"):
            found = HARDCODED.findall(_read(*rel.split("/")))
            self.assertEqual(
                found, [],
                f"{rel} 里出现硬编码版本号 {found}；必须引用 app.__version__"
                f"（当前 {app.__version__}），否则面板版本会与 tag / 交付包对不上")

    def test_readme_mentions_current_version(self):
        import app
        self.assertIn(f"v{app.__version__}", _read("README.md"),
                      f"README.md 里没有提到当前版本 v{app.__version__}（发版时请同步）")


if __name__ == "__main__":
    unittest.main()
