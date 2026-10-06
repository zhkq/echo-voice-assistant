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


class HtmlStructureTests(unittest.TestCase):
    """`web/*.html` 的标签必须配平（2026-10-06 加，与 JS 那条同一个由来）。

    当天在 `#capRouteCard` 上连栽两次：`<div id="capLocalSettings">` 被错手塞进了
    「解除配对」按钮里、还套在 `#capPairSettings` 内 —— 于是"本机后端"那一组
    **两种状态下都显示不出来**，而 Python 测试、ruff、门禁**全绿**（没人解析 HTML）。
    浏览器拿到错配的标签会"尽量猜"，于是坏的是**别的地方**，最难查。

    判据只用标准库（`html.parser`）：开闭标签必须成对、不能交叉。**VOID 元素不算开标签。**
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def _check(self, text):
        import html.parser

        void = self.VOID

        class P(html.parser.HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.stack, self.err = [], []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append((tag, self.getpos()[0]))

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack:
                    self.err.append("第%d行多一个 </%s>" % (self.getpos()[0], tag))
                    return
                top, ln = self.stack.pop()
                if top != tag:
                    self.err.append("第%d行 </%s> 与第%d行的 <%s> 不匹配"
                                    % (self.getpos()[0], tag, ln, top))

        p = P()
        p.feed(text)
        return p.err, [t for t, _ in p.stack]

    def test_every_web_html_is_well_formed(self):
        files = sorted(glob.glob(os.path.join(ROOT, "web", "**", "*.html"), recursive=True))
        self.assertTrue(files, "web/ 下没有 .html —— 路径判断错了？")
        bad = []
        for f in files:
            with open(f, encoding="utf-8") as fh:
                err, unclosed = self._check(fh.read())
            if err or unclosed:
                bad.append("%s\n    %s%s" % (
                    os.path.relpath(f, ROOT).replace("\\", "/"),
                    err[:3], ("未闭合: %s" % unclosed[:5]) if unclosed else ""))
        self.assertEqual(bad, [], "web/ 下有 HTML 标签不配平（浏览器会猜，坏在别处）：\n"
                         + "\n".join(bad))

    def test_the_html_guard_catches_the_real_bug(self):
        """守卫要能逮住当天那个真 bug：`<div>` 塞进 `<button>` 里、`</div>` 错位。"""
        broken = ('<div class="card"><div class="card-body">\n'
                  '  <button id="x">解除配对<div id="local">\n'
                  '  </button>\n'
                  '  </div>\n'                     # 少了一个 </div>（local 那个）
                  '  <div id="settings"></div>\n'
                  '</div>\n')
        err, unclosed = self._check(broken)
        self.assertTrue(err or unclosed, "错配的 HTML 竟然被判为配平 —— 守卫是假的")
        good = ('<div class="card"><div class="card-body">\n'
                '  <button id="x">解除配对</button>\n'
                '  <div id="local"></div>\n'
                '  <div id="settings"></div>\n'
                '</div></div>\n')
        err2, un2 = self._check(good)
        self.assertEqual((err2, un2), ([], []), "好的 HTML 被判成坏了：%s %s" % (err2, un2))


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
