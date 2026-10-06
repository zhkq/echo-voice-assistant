# -*- coding: utf-8 -*-
"""`web/*.js` 的**语法**必须能被真解析器解析（2026-10-06 加）。

## 为什么需要它（当天的事故）

我在 `web/app.js` 里改 `applyTranscribeSettingsVisibility()` 时多打了一个 `}`：

    if (loc) loc.classList.toggle("hidden", !localPaired);
    }}          ← 多这一个

后果是**整个 `web/app.js` 解析失败 → 仪表盘全坏**（用户报的原话："仪表盘坏了"）。
而它一路混过了：Python 测试全绿、`compileall` 全绿、ruff 全绿、门禁五项全绿 ——
因为**没有任何一步在解析 JavaScript**：现有那些读 `.js` 的用例只查"某个字符串在不在"，
不做语法分析。`node --check` 一秒就能逮住它。

## 判据

* 有 node 就用 `node --check`（最准：就是浏览器那个解析器家族）；
* 没有 node（干净机器）→ **跳过并说明**，不当失败 —— 门禁不该因为环境缺 node 而变红，
  但也不能假装查过了。
"""
import glob
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _node() -> str:
    """找一个能用的 node。先 PATH，再 DSH 托管目录，最后常见安装位。"""
    exe = shutil.which("node")
    if exe:
        return exe
    cands = [
        os.path.expanduser(r"~\AppData\Local\hermes\node\node.exe"),
        os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
        r"C:\Program Files\nodejs\node.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


class JsSyntaxTests(unittest.TestCase):
    """"**能编译**不等于**语义没被搬走**"那条教训的 JS 版：语法错了必须当场红。"""

    def test_every_web_js_file_parses(self):
        node = _node()
        # 只查自己写的那些：`vendor/` 下是第三方（mermaid.min.js 3.5 MB，`--check` 要 20 多秒，
        # 而且它坏了也不是我们改坏的）。
        files = sorted(f for f in glob.glob(os.path.join(ROOT, "web", "**", "*.js"), recursive=True)
                       if os.sep + "vendor" + os.sep not in f)
        self.assertTrue(files, "web/ 下一个 .js 都没有 —— 路径判断错了？")
        if not node:
            # **不静默跳过**：这道检查是"别把坏 JS 推出去"的唯一一道（2026-10-06 的
            # `}}` 事故就是这么混过门禁的）。找不到 node 要**说出来**，让人知道漏了一道闸。
            self.fail("找不到 node —— 这道 JS 语法检查没跑（这不等于通过）。"
                      "装一个 node，或把它放到 PATH / %LOCALAPPDATA%\\hermes\\node\\。"
                      "（DSH 桌面版自带 node，通常就在这里）")
        bad = []
        for f in files:
            r = subprocess.run([node, "--check", f], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                rel = os.path.relpath(f, ROOT).replace("\\", "/")
                first = (r.stderr or "").strip().splitlines()
                bad.append("%s\n    %s" % (rel, first[0] if first else "(无输出)"))
        self.assertEqual(bad, [], "web/ 下有 JS 解析不过（浏览器会整片挂掉）：\n" + "\n".join(bad))

    def test_the_guard_actually_catches_a_broken_file(self):
        """守卫自己要被验一次：**造一个坏的必须报错**。

        不然"全绿"可能只是因为 `node --check` 的参数写错、或它压根没被执行
        （`--check` 拼错时 node 会当普通脚本跑，某些情况下也能退 0）。
        """
        node = _node()
        if not node:
            self.skipTest("没有 node")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "broken.js")
            with open(broken, "w", encoding="utf-8") as fh:
                fh.write("function a() {\n  return 1;\n}}\n")     # 多一个 }
            r = subprocess.run([node, "--check", broken], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            self.assertNotEqual(r.returncode, 0, "坏文件的 `node --check` 竟然过了 —— 守卫是假的")
            good = os.path.join(tmp, "good.js")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write("function a() {\n  return 1;\n}\n")
            r2 = subprocess.run([node, "--check", good], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            self.assertEqual(r2.returncode, 0, r2.stderr)
