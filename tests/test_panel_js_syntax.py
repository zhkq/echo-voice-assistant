# -*- coding: utf-8 -*-
"""面板 JS 必须能被 JS 引擎解析。

2026-11-03 真机事故：`web/app.js` 里一处字符串拼接少写了 `+`
（`"a" "b"` → SyntaxError: Unexpected string），整个 app.js 不执行：
面板静态 HTML 出来了，但页签点不动（点「设置」没反应）、仪表盘卡片永远停在
"读取中…"，而所有 /api 接口都正常。单测之前只测后端，没人解析过这个文件，
于是坏代码一路打进了稳定版和交付包。
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_FILES = [
    os.path.join(ROOT, "web", "app.js"),
]


class PanelJsSyntaxTests(unittest.TestCase):
    def test_every_panel_js_file_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed; JS syntax check needs node --check")
        for path in JS_FILES:
            self.assertTrue(os.path.isfile(path), path)
            proc = subprocess.run(
                [node, "--check", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(
                0, proc.returncode,
                "web/app.js 有语法错误（面板会整体不执行）：\n" +
                (proc.stderr or proc.stdout)[:2000],
            )


if __name__ == "__main__":
    unittest.main()
