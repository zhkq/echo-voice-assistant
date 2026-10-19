# -*- coding: utf-8 -*-
"""会议纪要（`web/meeting.html` 的「纪要」页签）**渲染失败必须有退路**（2026-10-19 加）。

## 为什么加这一组（当天真事故）

用户原话（2026-10-19 晚）：

> 刚才转写的纪要还是有个渲染失败的额

那场是 `2026-10-19_13-58-42`（`meeting_note.md` 264,139 字节 / 3500 行）。用面板自带的
`web/vendor/mermaid.min.js`（mermaid 11.17.2）把那份纪要里的 8 张图**逐张真 render**，
抓到原始错误：

    TypeError: Cannot read properties of undefined (reading 'endTime')
        at compileTask (mermaid.min.js:1475)
        at compileTasks ...

根因是 **gantt 的标题写成了 `title: 文本`**（带冒号）：`mermaid.parse()` 能过，
但渲染阶段直接抛错，页面上那张图只剩一句 `Syntax error in text`（英文、没有源码、没有原因）。
生成端（`app/meeting.py` 的提示词）只说过"timeline 的标题冒号不可省"，模型把它套到了
gantt 上，而清洗器（`_sanitize_mermaid`）当时只处理 graph/flowchart/timeline。

## 两条判据（缺一不可）

1. **修生成端**：`_sanitize_mermaid` 现在会把 gantt 的 `title:` 改成 `title `；
   提示词也写明了 gantt 与 timeline 的冒号规则**相反**。
2. **修渲染端 + 留退路**：旧文件**不许要求用户重跑** —— 面板在显示前自己兜底
   （`mdToHtml` 里的 gantt 归一化），并且任何渲染异常都降级成「可见提示 + `<pre>` 原文」；
   一张图失败只降级那一张。**绝不允许白屏/半截**。

## 这一组怎么跑

和 `tests/test_meeting_export_html.py` 同一个路数：把页面里的真函数**原样抠出来**交给
node 跑（断言的是"点了以后页面到底产出什么"，不是"源码里有没有这句话"）。最后一条用
**无头 Edge + 仓库自带的 mermaid** 真渲染一遍 —— gantt 这个坑 parse 查不出来，只有 render 能查。

没有 node / Edge 时**响亮失败**（与 `tests/test_js_syntax.py`、`tests/test_mermaid_renders.py`
同一条约定：静默跳过等于"假装查过了"）。
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import meeting  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
MERMAID_JS = os.path.join(ROOT, "web", "vendor", "mermaid.min.js")
_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: 与页面里 `MD_SEGMENT_LIMIT` 同一个数（用例自己给，避免"页面改了用例跟着改"变糊）
SEGMENT_LIMIT = 20000


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _node():
    """找一个能用的 node（与 `tests/test_js_syntax.py` 同一份探测顺序）。"""
    exe = shutil.which("node")
    if exe:
        return exe
    for c in (os.path.expanduser(r"~\AppData\Local\hermes\node\node.exe"),
              os.path.expanduser(r"~\AppData\Local\nodejs\node.exe"),
              os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
              r"C:\Program Files\nodejs\node.exe"):
        if os.path.isfile(c):
            return c
    return ""


def _edge():
    for p in _EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def _inline_script(page_html):
    """页面主脚本正文（第一个 `<script>` 到**最后一个** `</script>`）。

    取最后一个 `</script>` 是必须的：导出那段会拼出 `'</scr' + 'ipt>…'`，
    用 `index()` 会在那儿截住（`tests/test_meeting_export_html.py` 里同样的坑）。
    """
    i = page_html.index("<script>")
    j = page_html.rindex("</script>")
    return page_html[i + len("<script>"):j]


def _js_function(src, sig):
    """从 `sig` 起、到下一个**顶格** `}` 为止（本仓库一贯的写法）。"""
    i = src.index(sig)
    return src[i:src.index("\n}", i) + 2]


def _mermaid_blocks(md):
    """取 markdown 里所有 ```mermaid 块的源码。"""
    out, i, lines = [], 0, md.split("\n")
    while i < len(lines):
        if lines[i].strip().startswith("```"):
            lang = lines[i].strip()[3:].strip()
            i += 1
            body = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            if lang == "mermaid":
                out.append("\n".join(body))
            i += 1
        else:
            i += 1
    return out


def _giant_note():
    """~50 KB / 3000+ 行、与那一场真纪要**同构**的纪要（不含任何真实会议内容）。

    为什么不用真文件：那是内部会议内容，不该进公开仓库；而这里要钉的是**结构**
    （长文 + 多张表 + 多张图 + 那张带 `title:` 的 gantt），结构对了判据就成立。
    尺寸按用户许可裁剪到 ~50 KB（真件 264 KB / 3500 行）。
    """
    head = [
        "# 会议纪要｜大体量样例",
        "",
        "> **会议时间**：2026-10-19 13:58",
        "> **主持人**：某人",
        "",
        "---",
        "",
        "## 核心结论（速览）",
        "",
    ]
    body = []
    for sec in range(1, 101):
        body += [
            "## 第 %d 节 议题与结论" % sec,
            "",
            "### %d.1 背景" % sec,
            "",
            "- 这一节是第 %d 段的要点，用来把篇幅堆到与真纪要同一个量级（3500 行 / 264 KB）。" % sec,
            "- 要点二：表格、图表、段落都要覆盖到，才能验证分段渲染不会切坏结构。",
            "- 要点三：这一行只是占位文字，不含任何真实会议内容。",
            "",
            "| 序号 | 事项 | 负责人 |",
            "|---|---|---|",
            "| 1 | 需求确认（第 %d 节） | 甲 |" % sec,
            "| 2 | 方案评审（第 %d 节） | 乙 |" % sec,
            "",
            "### %d.2 小结" % sec,
            "",
            "1. 第一条小结",
            "2. 第二条小结",
            "",
        ]
        if sec == 1:
            body += [
                "```mermaid",
                "gantt",
                "    title: 后续工作安排（2026年10月—11月）",
                "    dateFormat YYYY-MM-DD",
                "    axisFormat %m.%d",
                "    section 梳理与成图",
                "    整理会议结论与模板 :a1, 2026-10-20, 2d",
                "    section 汇报",
                "    10月底汇报材料定稿 :b1, 2026-10-27, 4d",
                "```",
                "",
            ]
        if sec == 2:
            body += [
                "```mermaid",
                "flowchart TD",
                '    A["开始"] --> B["结束"]',
                "```",
                "",
            ]
    tail = ["## 附：待确认事项", "", "- 待确认一", "- 待确认二", ""]
    return "\n".join(head + body + tail)


class _PageFunctions(unittest.TestCase):
    """把页面里那组纯函数**原样**抠出来，交给 node 跑。"""

    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.src = _inline_script(_read(ROOT, "web", "meeting.html"))
        parts = [
            # esc 是页面里的一行箭头函数（行内字符串带分号，不能按 `;` 截）
            [ln.strip() for ln in cls.src.splitlines()
             if ln.strip().startswith("const esc =")][0],
            _js_function(cls.src, "function mdInline(s) {"),
            _js_function(cls.src, "function mdToHtml(md) {"),
            _js_function(cls.src, "function mdSplitSegments(md, limit) {"),
            _js_function(cls.src, "function mdFallback(md, err, why) {"),
            _js_function(cls.src, "function mdSafe(md, render) {"),
        ]
        cls.funcs = "\n".join(parts) + "\n"

    # ------------------------------------------------------------------ 工具
    def _drive(self, code, *args):
        if not self.node:
            self.fail("找不到 node —— 纪要渲染这一组没跑（这不等于通过）。"
                      "装一个 node，或放到 PATH / %LOCALAPPDATA%\\hermes\\node\\。")
        driver = ("const fs = require('fs');\n" + self.funcs + "\n" + code + "\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(driver)
            r = subprocess.run([self.node, path] + list(args), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120)
        if r.returncode != 0:
            self.fail("node 跑挂了（页面函数本身坏了？）：\n%s\n%s"
                      % (r.stderr[-2000:], r.stdout[-1000:]))
        line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("RESULT ")]
        self.assertTrue(line, "探针没输出 RESULT（页面函数没执行？）：%s" % (r.stdout[-1000:],))
        return json.loads(line[-1][len("RESULT "):])

    # ------------------------------------------------------------------ 用例
    def test_missing_table_separator_degrades_to_text_without_raising(self):
        """表格缺分隔行 → **不抛异常**、降级成普通段落、内容**看得见**。

        2026-10-19 现场的"最典型翻车形状"就是这个：`| a | b |` 后面没有 `|---|`。
        正确行为是当普通段落画出来（用户能读），不是整篇失败。
        """
        out = self._drive(r"""
const md = fs.readFileSync(process.argv[2], "utf8");
const r = mdSafe(md);
const out = {
  ok: r.ok,
  tables: (r.html.match(/<table/g) || []).length,
  hasHeader: r.html.indexOf("序号") >= 0,
  hasRow1: r.html.indexOf("需求确认") >= 0,
  hasRow2: r.html.indexOf("方案评审") >= 0,
  hasTail: r.html.indexOf("上面这块之后的内容") >= 0,
  fallback: r.html.indexOf('data-testid="summary-fallback"') >= 0,
  bytes: r.html.length,
};
console.log("RESULT " + JSON.stringify(out));
""", os.path.join(FIXTURES, "meeting_note_bad_table.md"))
        self.assertTrue(out["ok"], "坏表格竟然让渲染抛异常（应该有退路、不抛）：%s" % out)
        self.assertEqual(out["tables"], 0, "缺分隔行的那块**不该**被当成表格：%s" % out)
        self.assertTrue(out["hasHeader"] and out["hasRow1"] and out["hasRow2"],
                        "表格里的文字必须仍然看得见：%s" % out)
        self.assertTrue(out["hasTail"], "表格后面的内容也必须照常渲染：%s" % out)
        self.assertFalse(out["fallback"], "这一份不该走整篇降级（它是可渲染的）：%s" % out)

    def test_clean_note_renders_exactly_as_before(self):
        """干净 markdown → 输出与以前**逐字节一致**（不回归）。

        这条是"别为了兜底把正常排版改坏"的闸：期望值就放在
        `tests/fixtures/meeting_note_clean.expected.html`，改了 mdToHtml 的排版就会红。
        """
        out = self._drive(r"""
const md = fs.readFileSync(process.argv[2], "utf8");
const expected = fs.readFileSync(process.argv[3], "utf8");
const r = mdSafe(md);
const out = { ok: r.ok, equal: r.html === expected,
              got: r.html.length, want: expected.length,
              firstDiff: (() => { for (let i = 0; i < Math.min(r.html.length, expected.length); i++) {
                if (r.html[i] !== expected[i]) return i; } return -1; })() };
console.log("RESULT " + JSON.stringify(out));
""", os.path.join(FIXTURES, "meeting_note_clean.md"),
            os.path.join(FIXTURES, "meeting_note_clean.expected.html"))
        self.assertTrue(out["ok"], "干净纪要都渲染不了？%s" % out)
        self.assertTrue(out["equal"],
                        "干净纪要的渲染结果变了（不回归的判据）：firstDiff=%s got=%s want=%s"
                        % (out["firstDiff"], out["got"], out["want"]))

    def test_gantt_title_colon_is_normalized_for_display_only(self):
        """面板显示前会把 gantt 的 `title:` 改成 `title `（旧文件不用重跑）。

        这一条钉的正是 2026-10-19 那个根因：文件里是 `title: …`，但发给 mermaid 之前
        必须变成 `title …`；timeline 的冒号**不许**跟着改（它是合法的、而且是必需的）。
        """
        out = self._drive(r"""
const md = fs.readFileSync(process.argv[2], "utf8");
const html = mdToHtml(md);
const blocks = [];
const re = /<pre class="mermaid">([\s\S]*?)<\/pre>/g;
let m;
while ((m = re.exec(html)) !== null) blocks.push(m[1]);
const out = { n: blocks.length, blocks };
console.log("RESULT " + JSON.stringify(out));
""", os.path.join(FIXTURES, "meeting_note_gantt.md"))
        self.assertEqual(out["n"], 2, "样例里应当有两张图（gantt + timeline）：%s" % out)
        gantt, timeline = out["blocks"]
        self.assertIn("title 后续工作安排", gantt,
                      "gantt 的标题必须去掉冒号：%r" % gantt)
        self.assertNotIn("title:", gantt, "gantt 里不该再有 `title:`：%r" % gantt)
        self.assertIn("title: 时间线", timeline,
                      "timeline 的冒号是合法的，不许一起改掉：%r" % timeline)

    def test_render_error_falls_back_to_pre_with_a_visible_notice(self):
        """渲染抛错/渲染成空 → **有 `data-testid` 提示 + `<pre>` 原文**，绝不空白。"""
        out = self._drive(r"""
const a = mdSafe("原文一\n第二行", function () { throw new Error("boom"); });
const b = mdSafe("原文二", function () { return ""; });
const c = mdSafe("", mdToHtml);
const out = {
  a: { ok: a.ok, len: a.html.length,
       notice: a.html.indexOf('data-testid="summary-fallback"') >= 0,
       pre: a.html.indexOf('<pre class="sm-raw">原文一\n第二行</pre>') >= 0,
       why: a.html.indexOf("boom") >= 0 },
  b: { ok: b.ok, len: b.html.length, notice: b.html.indexOf('summary-fallback') >= 0 },
  c: { ok: c.ok, len: c.html.length },
};
console.log("RESULT " + JSON.stringify(out));
""")
        self.assertFalse(out["a"]["ok"], "抛异常时必须标记为降级：%s" % out)
        self.assertGreater(out["a"]["len"], 0, "降级结果不能是空白：%s" % out)
        self.assertTrue(out["a"]["notice"], "降级必须给**可见提示**：%s" % out)
        self.assertTrue(out["a"]["pre"], "降级必须把 markdown 原文放进 `<pre>`：%s" % out)
        self.assertTrue(out["a"]["why"], "原因要带上（用户才知道发生了什么）：%s" % out)
        self.assertFalse(out["b"]["ok"], "渲染成空也算失败（空 HTML 对用户就是白屏）")
        self.assertTrue(out["b"]["notice"], "渲染成空也要给可见提示：%s" % out)
        self.assertTrue(out["c"]["ok"], "空输入不该被当成失败（本来就没有内容）")

    def test_big_note_is_segmented_and_never_blank(self):
        """~50 KB / 3000+ 行（同构于那份 264 KB 的真纪要）→ 分段渲染，成功或**有提示地**降级。"""
        note = _giant_note()
        self.assertGreater(len(note.encode("utf-8")), 45000,
                           "样例太小，压不出分段渲染（真件 264 KB）")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "giant.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(note)
            out = self._drive(r"""
const md = fs.readFileSync(process.argv[2], "utf8");
const LIMIT = %d;
const segs = mdSplitSegments(md, LIMIT);
let html = "", anyFail = false;
for (const s of segs) { const r = mdSafe(s); if (!r.ok) anyFail = true; html += r.html; }
const plain = html.replace(/<[^>]*>/g, "");
const out = {
  bytes: Buffer.byteLength(md), lines: md.split("\n").length, chars: md.length,
  segs: segs.length, rejoin: segs.join("\n") === md,
  fencesEven: segs.every((s) => ((s.match(/^```/gm) || []).length %% 2) === 0),
  overLimit: segs.map((s) => s.length).filter((n) => n > LIMIT * 2).length,
  ok: !anyFail,
  fallback: html.indexOf('data-testid="summary-fallback"') >= 0,
  preFallback: html.indexOf('<pre class="sm-raw">') >= 0,
  tables: (html.match(/<table/g) || []).length,
  diagrams: (html.match(/pre class="mermaid"/g) || []).length,
  textChars: plain.replace(/\s/g, "").length,
  headVisible: plain.indexOf("大体量样例") >= 0,
  tailVisible: plain.indexOf("待确认二") >= 0,
};
console.log("RESULT " + JSON.stringify(out));
""" % SEGMENT_LIMIT, path)
        self.assertTrue(out["rejoin"], "分段之后再拼回去必须与原文逐字节一致：%s" % out)
        self.assertTrue(out["fencesEven"], "分段把某个代码围栏切开了（会导致后半段整片错）：%s" % out)
        self.assertGreaterEqual(out["segs"], 2, "50 KB 的纪要应当被切成 ≥2 段：%s" % out)
        self.assertEqual(out["overLimit"], 0, "有段远超上限（分段没生效）：%s" % out)
        self.assertGreater(out["textChars"], 10000, "正文没了（白屏）：%s" % out)
        self.assertTrue(out["headVisible"] and out["tailVisible"],
                        "分段后开头/结尾都必须看得见：%s" % out)
        self.assertGreater(out["tables"], 10, "表格丢了（分段把表格切坏了？）：%s" % out)
        self.assertEqual(out["diagrams"], 2, "图表的数量不对：%s" % out)
        # 判据（用户口径）：**渲染成功**，或降级成 `<pre>` 且**有可见提示** —— 不许空白
        if not out["ok"]:
            self.assertTrue(out["fallback"] and out["preFallback"],
                            "降级了却没有可见提示/原文：%s" % out)

    def test_segments_never_split_a_fence(self):
        """围栏里的 `## ` 不能被当成标题切开（切了会让后半段整片变成代码块）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fence.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("## 甲\n\n" + ("填字" * 40 + "\n") * 3
                         + "```text\n## 这是代码里的井号\n" + ("行" * 50 + "\n") * 30 + "```\n"
                         + "## 乙\n\n结尾\n")
            out = self._drive(r"""
const md = fs.readFileSync(process.argv[2], "utf8");
const segs = mdSplitSegments(md, 500);
const out = {
  segs: segs.length,
  rejoin: segs.join("\n") === md,
  even: segs.every((s) => ((s.match(/^```/gm) || []).length % 2) === 0),
  hasFenceHeading: segs.some((s) => s.indexOf("## 这是代码里的井号") >= 0),
};
console.log("RESULT " + JSON.stringify(out));
""", path)
        self.assertGreaterEqual(out["segs"], 2, "样例应当被切开（否则这条没测到东西）")
        self.assertTrue(out["rejoin"], "拼回去要一致：%s" % out)
        self.assertTrue(out["even"], "有段把 ``` 切开了：%s" % out)
        self.assertTrue(out["hasFenceHeading"], "围栏内容不该被切丢：%s" % out)


class SanitizerGanttTests(unittest.TestCase):
    """生成端：`_sanitize_mermaid` 必须把 gantt 的标题冒号去掉（timeline 的不许动）。"""

    @staticmethod
    def _mm(body):
        fence = "`" * 3
        return fence + "mermaid\n" + body + "\n" + fence

    def test_gantt_title_colon_is_stripped(self):
        out = meeting._sanitize_mermaid(self._mm(
            "gantt\n    title: 后续工作安排\n    dateFormat YYYY-MM-DD\n"
            "    section 甲\n    任务 :a1, 2026-10-20, 2d"))
        self.assertIn("    title 后续工作安排", out)
        self.assertNotIn("title:", out)

    def test_timeline_title_colon_is_kept(self):
        out = meeting._sanitize_mermaid(self._mm(
            "timeline\n    title: 时间线\n    2026-10-20 : 开始"))
        self.assertIn("title: 时间线", out)

    def test_the_prompt_tells_the_model_the_two_rules_are_opposite(self):
        """提示词里必须写明"gantt 与 timeline 相反"—— 否则模型还会套错。"""
        text = meeting._SUMMARY_REQUIREMENTS
        self.assertIn("gantt", text)
        self.assertIn("不能带冒号", text)
        self.assertIn("timeline", text)


class DiagramsReallyRenderTests(unittest.TestCase):
    """最后一道：**真 render**（不是 parse）—— gantt 的坑 parse 查不出来。

    做法与 `tests/test_mermaid_renders.py` 相同：仓库自带的 `mermaid.min.js` + 无头 Edge，
    `file://` 打开，不碰面板、不碰数据。
    """

    PAGE = """<!doctype html><meta charset="utf-8"><title>t</title>
<script type="application/json" id="blocks">%s</script>
<script src="mermaid.min.js"></script><div id="RESULT">pending</div>
<script>(async () => {
  const bs = JSON.parse(document.getElementById("blocks").textContent);
  const res = [];
  mermaid.initialize({startOnLoad:false, theme:"dark", securityLevel:"strict"});
  for (let i = 0; i < bs.length; i++) {
    try {
      const r = await mermaid.render("blk-" + i + "-" + Date.now(), bs[i]);
      const svg = r.svg || "";
      // 注意：不能用 svg.includes("error-icon") —— mermaid 的 CSS 里本来就有 .error-icon 规则
      const bad = svg.indexOf("Syntax error in text") >= 0
        || /class="[^"]*error-icon/.test(svg);
      res.push({i, ok: !bad, err: bad ? "rendered an error diagram" : ""});
    } catch (e) {
      res.push({i, ok: false, err: String((e && (e.str || e.message)) || e)});
    }
  }
  document.getElementById("RESULT").textContent = JSON.stringify(res);
})();</script>"""

    def test_panel_and_sanitized_diagrams_really_render(self):
        edge = _edge()
        if not edge:
            self.fail("没找到 Edge —— 这道'真渲染'检查没跑（这不等于通过）。"
                      "Windows 自带 Edge；找过：%s" % list(_EDGE_CANDIDATES))
        self.assertTrue(os.path.isfile(MERMAID_JS), "仓库里没有 web/vendor/mermaid.min.js")

        src = _read(FIXTURES, "meeting_note_gantt.md")
        blocks = {
            # ① 生成端修好：清洗后的块
            "sanitized": _mermaid_blocks(meeting._sanitize_mermaid(src)),
            # ② 渲染端兜底：面板 `mdToHtml` 吐出来的 `<pre class="mermaid">` 内容
            #    （等于"旧文件不重跑也能显示"的证据）
            "panel": self._panel_blocks(src),
        }
        self.assertTrue(all(blocks.values()), "样例里没找到图：%s" % blocks)
        self.assertNotIn("title:", blocks["sanitized"][0],
                         "清洗后的 gantt 里还留着冒号：%r" % blocks["sanitized"][0])
        self.assertNotIn("title:", blocks["panel"][0],
                         "面板显示前的归一化没生效：%r" % blocks["panel"][0])

        results = {}
        for kind, bs in blocks.items():
            results[kind] = self._render(edge, bs)
        bad = ["%s -> #%d %s" % (kind, x["i"], x.get("err", ""))
               for kind, res in results.items() for x in res if not x["ok"]]
        self.assertEqual(bad, [],
                         "清洗/面板兜底之后图还是渲染不出来（真 render 才算数）：\n"
                         + "\n".join(bad))

    def _panel_blocks(self, md):
        """用页面里的真 `mdToHtml` 渲染一遍，再把它吐出的 mermaid 源码取回来。"""
        node = _node()
        if not node:
            self.fail("找不到 node —— 取不到面板渲染后的 mermaid 源码（这不等于通过）")
        src = _inline_script(_read(ROOT, "web", "meeting.html"))
        funcs = "\n".join([
            [ln.strip() for ln in src.splitlines()
             if ln.strip().startswith("const esc =")][0],
            _js_function(src, "function mdInline(s) {"),
            _js_function(src, "function mdToHtml(md) {"),
        ])
        driver = ("const fs = require('fs');\n" + funcs + r"""
const html = mdToHtml(fs.readFileSync(process.argv[2], "utf8"));
const out = [];
const re = /<pre class="mermaid">([\s\S]*?)<\/pre>/g;
let m;
while ((m = re.exec(html)) !== null) out.push(m[1]);
console.log("RESULT " + JSON.stringify(out));
""")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(driver)
            r = subprocess.run([node, path, os.path.join(FIXTURES, "meeting_note_gantt.md")],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")][-1]
        return [html.unescape(x) for x in json.loads(line[len("RESULT "):])]

    def _render(self, edge, blocks):
        tmp = tempfile.mkdtemp(prefix="echo-mmrender-")
        try:
            shutil.copyfile(MERMAID_JS, os.path.join(tmp, "mermaid.min.js"))
            payload = json.dumps(blocks, ensure_ascii=False).replace("</", "<\\/")
            page = os.path.join(tmp, "probe.html")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write(self.PAGE % payload)
            profile = tempfile.mkdtemp(prefix="echo-edge-")
            try:
                r = subprocess.run(
                    [edge, "--headless=new", "--disable-gpu", "--no-first-run",
                     "--user-data-dir=" + profile, "--virtual-time-budget=30000",
                     "--dump-dom", "file:///" + page.replace("\\", "/")],
                    capture_output=True, timeout=300)
                dom = (r.stdout or b"").decode("utf-8", "replace")
            finally:
                shutil.rmtree(profile, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertGreater(len(dom), 200, "无头 Edge 没吐出 DOM")
        m = re.search(r'id="RESULT">(.*?)</div>', dom, re.S)
        self.assertIsNotNone(m, "探针页没写结果（页面没跑起来？）：%s" % dom[:400])
        raw = html.unescape(m.group(1))
        self.assertNotIn("pending", raw, "探针页的脚本没跑完：%s" % raw[:200])
        return json.loads(raw)


if __name__ == "__main__":
    unittest.main()
