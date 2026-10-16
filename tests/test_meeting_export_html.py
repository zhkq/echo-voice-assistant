# -*- coding: utf-8 -*-
"""会议详情页「⬇ 离线HTML」导出的两条口径（2026-10-16 用户要求）。

用户原话：

> 会议纪要离线保存的时候文件名改写一下把标题带上，如果没有完成纪要，就等完成再存

澄清后点名了入口：**"我是说离线html那个按钮"** —— 就是面板会议详情右上角那颗
（`web/meeting.html` 的 `#btnExport` → `downloadOfflineHtml()`）。

## 这个文件为什么**真跑** JS

仓库里读 `web/*.js` 的用例一贯是"查某个字符串在不在"（`tests/test_ia_panel.py` 开篇
就写明了这个约定）。但这次要钉的两件事 —— **文件名怎么拼**、**等待有没有上限** ——
恰好是"字符串在不在"查不出来的：我可以把 `buildExportFileName` 写错成永远返回
`{日期}_会议纪要.html`，字面上 `M.title` 字样仍在，用例照样绿。
所以这里用 `node` **真的调用**页面里那几个函数（`buildExportFileName` / `safeFileStem` /
`meetingDateForFile`），并且**真的跑一遍** `downloadOfflineHtml()`（api/toast/DOM/时钟
换成替身）—— 断言的是"点了按钮到底会不会下载、下载叫什么名字"，不是"源码里有没有这句话"。

`<script>` 那一段是从 `web/meeting.html` **原样抠出来的**（一个字都不手抄）：
手抄的测试只能证明我抄得对不对。

## 没有 node 怎么办

**响亮失败**，与 `tests/test_js_syntax.py` 同一条约定：静默跳过等于"假装查过了"，
而这一组正是唯一的"名字到底对不对 / 会不会无限等"闸门。
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

EXPORT_SECTION = "/* ---------- 离线 HTML 导出 ----------"
# 按钮接的是薄壳 `downloadOfflineHtml()`（防重入，见页面里的注释）；
# 拼文件名、等纪要这些真逻辑都在 `_exportOfflineHtml()` 里。
EXPORT_SIG = "async function _exportOfflineHtml()"
EXPORT_ENTRY_SIG = "async function downloadOfflineHtml()"

# 文件名里**三个平台都不接受**的字符（`/ \ : * ? " < > |`）。用例按它逐字符查结果，
# 所以这张表就是"清理规则"的可执行版本。
ILLEGAL = '/\\:*?"<>|'
CTRL = "\r\n\t\x00\x1f"
# 半个代理对（emoji 被截断切一半）—— 文件名里会显示成乱码方块
LONE_SURROGATE = re.compile(r"[\ud800-\udbff](?![\udc00-\udfff])"
                            r"|(?<![\ud800-\udbff])[\udc00-\udfff]")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _node():
    """找一个能用的 node（与 `tests/test_js_syntax.py` 同一份探测顺序）。"""
    import shutil
    exe = shutil.which("node")
    if exe:
        return exe
    for c in (os.path.expanduser(r"~\AppData\Local\hermes\node\node.exe"),
              os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
              r"C:\Program Files\nodejs\node.exe"):
        if os.path.isfile(c):
            return c
    return ""


def _inline_script(html):
    """页面主脚本正文（`<script>`…最后一个 `</script>`）。

    取**最后一个** `</script>` 是必须的：导出拼出来的 HTML 里有一行
    `'</scr' + 'ipt></body></html>'`（故意拆开，免得把外层脚本截断），
    用 `index()` 会在那儿截住。
    """
    i = html.index("<script>")
    j = html.rindex("</script>")
    return html[i + len("<script>"):j]


def _js_function(src, sig):
    """从 `sig` 起、到下一个**顶格** `}` 为止（本仓库一贯的写法）。"""
    i = src.index(sig)
    return src[i:src.index("\n}", i) + 2]


# ---------------------------------------------------------------------------
# 测试替身：只替**页面之外**的东西（api / toast / DOM / 时钟）。
# 页面自己的代码（buildExportFileName / safeFileStem / waitForExportSummary /
# downloadOfflineHtml）**原样**跑。
# ---------------------------------------------------------------------------
_STUBS = r"""
let TOASTS = [];
function toast(msg) { TOASTS.push(String(msg)); }
let CLICKS = 0, DOWNLOADED = null, LAST_HTML = null;
const document = {
  createElement() {
    return { href: "", download: "",
             click() { CLICKS += 1; DOWNLOADED = this.download; } };
  },
};
const URL = { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} };
// Blob 换成"记下内容"的替身：导出出来的 HTML 是自包含的，用例要当面看一遍
class Blob { constructor(parts) { LAST_HTML = parts.join(""); } }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function mdInline() { return ""; }
function mdToHtml() { return ""; }
function bindMermaidZoom() {}
function openMermaidZoom() {}
// 这几个是导出时**立刻**要用到的页面函数（不是回调里才调的）：
// `fmtHM` 拼 header 的时间、`mergeTurns`/`fmtTime`/`spkColor` 只在有分段时才调（用例里没有分段）
function fmtHM() { return "10 分钟"; }
function fmtTime() { return "00:01"; }
function spkColor() { return "#4f8cff"; }
function mergeTurns(lines) { return lines || []; }
async function fetch() { return { text: async () => "/*mermaid-stub*/" }; }

let M = null;
const NOT_READY = { exists: false, content: "" };
const READY = { exists: true, content: "# 会议纪要\n\n正文在此" };
let SUMMARY_SCRIPT = [];     // 前几次问纪要时按剧本回答（空数组 = 一直用 LAST_SUMMARY）
let LAST_SUMMARY = NOT_READY;
let API_CALLS = 0;

async function api(path) {
  API_CALLS += 1;
  // ⚠️ 这里**不能**用"调太多次就抛错"来当保险：`fetchExportSummary()` 会把读接口的异常
  // 吞成"没好"（那是页面的真实行为，见它的 catch），抛了也传不出来。
  // 防"无限等"的保险放在下面的定时器替身里。
  if (path.indexOf("kind=summary") >= 0) {
    return SUMMARY_SCRIPT.length ? SUMMARY_SCRIPT.shift() : LAST_SUMMARY;
  }
  if (path.indexOf("kind=topics") >= 0) return { exists: true, content: '{"分段":[]}' };
  if (path.indexOf("kind=transcript") >= 0) return { exists: true, content: "转写正文" };
  throw new Error("用例没预期的接口：" + path);
}

// 时钟与定时器换成"立刻走完"的替身（**两条路都要**）：
//   ① 页面在下载后会挂一个 `setTimeout(() => URL.revokeObjectURL(...), 4000)` —— 真跑它，
//      node 会为了这个 4 秒的定时器多活 4 秒（每条用例白等 4 秒）；
//   ② "等纪要"那条路要把 30 秒的窗口走完，真睡就是真等 30 秒。
// 两者都只影响**时间**，不影响被测逻辑 —— 上限判据读的就是 `Date.now()`。
let FAKE_NOW = 0;
let TIMER_CALLS = 0;
Date.now = () => (FAKE_NOW += 2000);
setTimeout = (fn, ms) => {
  TIMER_CALLS += 1;
  // **硬上限**：万一"等纪要"被改成无限循环，这里抛出来让 node 响亮退出
  // （它抛在 `await new Promise(...)` 那一行上，`fetchExportSummary` 的 catch 吞不掉），
  // 而不是把整个门禁挂死。实测过：没有它时，一个"不设上限"的变异能让 node 空转到天荒地老
  // —— 那次是我自己去杀进程才停下来的。
  if (TIMER_CALLS > 60) {
    throw new Error("定时器被排了 60 次以上 —— 等待逻辑没有上限？（用例主动炸掉，免得空转）");
  }
  FAKE_NOW += (ms || 0);
  fn();
};
"""


class _NodeHarness(unittest.TestCase):
    """抠出页面里的真代码 → 交给 node 跑。"""

    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.html = _read("web", "meeting.html")
        cls.src = _inline_script(cls.html)

    # ---------------------------------------------------------------- 取代码
    def _export_code(self):
        """导出那一节（含常量与全部 helper）+ 入口薄壳 + `_exportOfflineHtml` 本体。"""
        sec = self.src[self.src.index(EXPORT_SECTION):self.src.index(EXPORT_ENTRY_SIG)]
        entry = _js_function(self.src, EXPORT_ENTRY_SIG)
        body = _js_function(self.src, EXPORT_SIG)
        return sec + "\n" + entry + "\n" + body + "\n"

    def _run(self, driver):
        if not self.node:
            self.fail("找不到 node —— 离线导出这一组用例没跑（这不等于通过）。"
                      "装一个 node，或把它放到 PATH / %LOCALAPPDATA%\\hermes\\node\\。")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(driver)
            try:
                r = subprocess.run([self.node, path], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
            except subprocess.TimeoutExpired:
                # 页面代码里真出现了"永远等下去"时，用例必须**红**，不能把门禁挂死
                self.fail("node 60 秒还没结束 —— 导出逻辑很可能没有上限（无限等/死循环）")
            self.assertEqual(r.returncode, 0,
                             "node 跑导出的真代码失败了：\n%s" % (r.stderr or "")[-3000:])
            return json.loads(r.stdout)

    def _names(self, cases):
        """真调 `buildExportFileName()`，返回它给的结果。"""
        driver = (_STUBS + "\n" + self._export_code()
                  + "\nconst CASES = " + json.dumps(cases, ensure_ascii=False) + ";\n"
                  + "console.log(JSON.stringify(CASES.map((c) => buildExportFileName(c))));\n")
        return self._run(driver)

    def _click(self, meeting, summary_script=(), last_summary="READY"):
        """真的跑一遍按钮的处理函数，回报"下载了什么 / 提示了什么 / 问了几次接口"。"""
        driver = (
            _STUBS + "\n" + self._export_code()
            + "\n(async () => {\n"
            + "  M = " + json.dumps(meeting, ensure_ascii=False) + ";\n"
            + "  SUMMARY_SCRIPT = " + json.dumps(list(summary_script)) + ".map((b) => (b ? READY : NOT_READY));\n"
            + "  LAST_SUMMARY = " + last_summary + ";\n"
            + "  await downloadOfflineHtml();\n"
            + "  console.log(JSON.stringify({ clicks: CLICKS, downloaded: DOWNLOADED,\n"
            + "     toasts: TOASTS, apiCalls: API_CALLS, html: LAST_HTML,\n"
            + "     fakeNow: FAKE_NOW, cap: EXPORT_SUMMARY_WAIT_MS,\n"
            + "     poll: EXPORT_SUMMARY_POLL_MS }));\n"
            + "})();\n")
        return self._run(driver)

    @staticmethod
    def _meeting(title="Q3 复盘", started_at="2026-10-16T11:37:25", name="2026-10-16_11-37-25"):
        return {"id": 7, "title": title, "started_at": started_at, "name": name,
                "speakers": [], "segments": [], "status": "transcribed",
                "duration_seconds": 600}


class ExportFileNameTests(_NodeHarness):
    """① 文件名 = `{日期}_{标题}.html`（真跑函数，不是查字符串）。"""

    def test_the_title_is_carried_into_the_file_name(self):
        """**文件名是给人看的** → 带标题才认得出来是哪场会（用户这次的核心要求）。"""
        out = self._names([{"started_at": "2026-10-16T11:37:25", "title": "Q3 复盘"},
                           {"started_at": "2026-08-21T10:00:00", "title": "周例会"}])
        self.assertEqual(out[0]["name"], "2026-10-16_Q3 复盘.html")
        self.assertEqual(out[1]["name"], "2026-08-21_周例会.html")
        for row in out:
            with self.subTest(row=row):
                self.assertFalse(row["usedDateFallback"], "有标题时不该走日期兜底")
                # 老写法是 `<会议目录名>_会议纪要.html` —— 目录名那种时间戳等于没写名字
                self.assertNotIn("_11-37-25", row["name"])
                self.assertNotIn("_会议纪要", row["name"])

    def test_illegal_characters_and_overlong_titles_are_cleaned(self):
        """非法字符 / 换行 / 结尾的点与空格 / 超长：都要清掉（用户标题是自由文本）。

        不清的后果不是"名字难看"：`a.download` 里带上 `\\ / : * ? " < > |` 时，
        浏览器轻则把整串换成 `download`、重则落盘失败，而界面上一句提示都没有。
        """
        overlong = "长" * 80
        emoji = "a" + "📌" * 40        # 81 个 UTF-16 单元：按 60 切正好切在第 30 个 emoji 中间
        cases = [
            {"started_at": "2026-10-16T11:37:25", "title": "/ \\ : * ? \" < > |"},
            {"started_at": "2026-10-16T11:37:25", "title": "换行\n在里面\t制表"},
            {"started_at": "2026-10-16T11:37:25", "title": "结尾有点和空格. . "},
            {"started_at": "2026-10-16T11:37:25", "title": overlong},
            {"started_at": "2026-10-16T11:37:25", "title": emoji},
        ]
        out = self._names(cases)
        for row, case in zip(out, cases):
            stem = row["name"][len("2026-10-16_"):-len(".html")]
            with self.subTest(title=case["title"][:20]):
                for ch in ILLEGAL:
                    self.assertNotIn(ch, stem, "非法字符 %r 没被清掉" % ch)
                for ch in CTRL:
                    self.assertNotIn(ch, stem, "控制字符 %r 没被清掉" % ch)
                self.assertFalse(stem.startswith(" "), "开头不该留空格")
                self.assertFalse(stem.endswith(" "), "结尾不该留空格")
                self.assertFalse(stem.endswith("."), "结尾的点在 Windows 上存不下来")
                self.assertTrue(stem, "清完不该什么都不剩")
                self.assertIsNone(LONE_SURROGATE.search(stem),
                                  "截断把 emoji 切成了半个代理对（文件名里会是乱码）")
        # 限长：超长标题要被截断（否则文件名会长到落盘失败）
        self.assertLessEqual(len(out[3]["name"][len("2026-10-16_"):-len(".html")]), 60)
        self.assertFalse(out[3]["usedDateFallback"], "截断之后仍然算「有标题」")
        # 换行/制表压成空格（不是直接删掉，免得两个词黏在一起）
        self.assertEqual(out[1]["name"], "2026-10-16_换行 在里面 制表.html")
        self.assertEqual(out[2]["name"], "2026-10-16_结尾有点和空格.html")

    def test_a_missing_title_is_never_faked_by_the_directory_name(self):
        """标题为空/只有空白 → **不许**拿会议目录名（`M.name`，时间戳）冒充标题。

        那会让文件名"看起来有标题"，其实还是 `2026-08-21_10-00-00` 那串东西 ——
        比不写更坏，因为它撒谎。这里钉住选定的口径 **(b)：日期兜底 +
        界面上如实说明"文件名用了日期"**（见下一个用例查 UI 那句话）。
        """
        dirname = "2026-08-21_10-00-00"
        out = self._names([
            {"started_at": dirname.replace("_", "T", 1), "title": "", "name": dirname},
            {"started_at": "2026-08-21T10:00:00", "title": "   ", "name": dirname},
            {"started_at": "2026-08-21T10:00:00", "title": "\n\t", "name": dirname},
            {"started_at": "2026-08-21T10:00:00", "name": dirname},          # 连字段都没有
        ])
        for row in out:
            with self.subTest(row=row):
                self.assertTrue(row["usedDateFallback"], "没标题必须走日期兜底（口径 b）")
                self.assertEqual(row["name"], "2026-08-21_会议纪要.html")
                self.assertNotIn("10-00-00", row["name"], "目录名被拿来冒充标题了")

    def test_the_date_rule_is_the_same_one_the_backend_uses(self):
        """日期取"这场会那天"，且与后端 `meeting._meeting_date()` **同一规则**。

        为什么要跨语言对一遍：文件名里的日期是给人归档用的（工作日志/归档都按会议
        当天落地）。两边规则一旦不一致，隔天补导出就会出现"日志写在 10-16、文件名却
        是 10-17"，人只会以为归错了场。
        """
        from app.meeting import _meeting_date

        raws = ["2026-10-16T11:37:25", "2026-08-21 10:00:00", "2026-02-31T01:00:00",
                "2026-13-01T00:00:00", "2026-1-5 09:00", "", "不是时间"]
        out = self._names([{"started_at": r, "title": "会议"} for r in raws])
        today = datetime.date.today().strftime("%Y-%m-%d")
        for raw, row in zip(raws, out):
            want = _meeting_date({"started_at": raw})
            with self.subTest(started_at=raw):
                self.assertEqual(row["name"][:10], want,
                                 "两边算出来的会议日期不一致（回退值是今天 %s）" % today)

    def test_a_broken_started_at_still_produces_a_usable_name(self):
        """`started_at` 缺失/为 null 时不许抛异常（历史会议、手工导入都可能没有它）。"""
        today = datetime.date.today().strftime("%Y-%m-%d")
        out = self._names([{}, {"started_at": None, "title": "没有时间的会"}])
        # 这两个的日期都只能回落到"今天"，所以只钉形状与**标题那一半**（日期回退是另一条用例的事）
        self.assertEqual(out[0]["name"], today + "_会议纪要.html")   # 连标题都没有 → 日期兜底
        self.assertEqual(out[1]["name"], today + "_没有时间的会.html")
        for row in out:
            with self.subTest(row=row):
                self.assertRegex(row["name"], r"^\d{4}-\d{2}-\d{2}_.+\.html$")


class ExportSummaryGateTests(_NodeHarness):
    """② 纪要没完成 → **不**触发下载 + 有可见提示 + 有**带上限**的等待。"""

    def test_it_does_not_download_an_unfinished_summary(self):
        """空纪要导出去等于骗人：用户拿到"看着像纪要、打开是空的"HTML，只会以为纪要丢了。

        所以这条钉三件事：**一个字节都不写**（`clicks == 0`）、**界面有话**、
        而且**等得有上限**（假时钟跑完整个窗口后必须自己收手，不是无限轮询）。
        """
        out = self._click(self._meeting(), summary_script=[], last_summary="NOT_READY")
        self.assertEqual(out["clicks"], 0, "纪要没好时**不许**触发下载")
        self.assertIsNone(out["downloaded"], "连 download 属性都不该被设置")
        self.assertIsNone(out["html"], "连 HTML 都不该被拼出来")
        # 可见反馈：等待期间要一直说在等（不是"点了没反应"）
        self.assertTrue(any("正在等它完成" in t for t in out["toasts"]),
                        "等待期间必须有可见提示：%s" % out["toasts"])
        # 到点如实说，并给出下一步（别静默失败）
        last = out["toasts"][-1]
        self.assertIn("还没生成好", last)
        self.assertIn("先不导出", last)
        # 上限：30 秒（30000ms）—— 常量与轮询间隔都在，且轮询次数被上限压住
        self.assertEqual(out["cap"], 30000, "等待上限应当写明是 30 秒")
        self.assertGreater(out["poll"], 0, "轮询间隔要 > 0（0 会变成忙等）")
        self.assertLessEqual(out["fakeNow"], out["cap"] + 2 * out["poll"] + 1000,
                             "等待超过了上限（%s ms）" % out["cap"])
        self.assertLessEqual(out["apiCalls"], out["cap"] // out["poll"] + 2,
                             "问接口的次数超过了上限允许的次数（像是不停地轮询）")

    def test_a_generating_summary_is_waited_for_and_then_exported(self):
        """用户口径是"**等完成再存**"：还没好 → 等（有反馈）→ 好了 → 照样存下来。"""
        out = self._click(self._meeting(title="周例会"),
                          summary_script=[False, False, True], last_summary="READY")
        self.assertEqual(out["clicks"], 1, "纪要后来好了，就应当真的存下来")
        self.assertEqual(out["downloaded"], "2026-10-16_周例会.html")
        self.assertTrue(any("正在等它完成" in t for t in out["toasts"]),
                        "等待期间要有反馈：%s" % out["toasts"])
        self.assertGreaterEqual(out["apiCalls"], 3, "应当真的重试过（不是问一次就放弃）")

    def test_the_export_is_announced_with_the_file_name(self):
        """导出成功要有可见反馈（沿用面板既有 toast/状态行），并且**把文件名念出来** ——
        用户这次要改的正是文件名，不念出来就无从确认改对了。"""
        out = self._click(self._meeting(title="Q3 复盘"))
        self.assertEqual(out["clicks"], 1)
        self.assertTrue(any("已生成 2026-10-16_Q3 复盘.html" in t for t in out["toasts"]),
                        "成功的提示里要带文件名：%s" % out["toasts"])

    def test_the_no_title_case_says_the_date_was_used(self):
        """口径 (b) 的**界面那一半**：用了日期兜底就必须如实说明，不能悄悄换掉名字。"""
        out = self._click(self._meeting(title="", name="2026-10-16_11-37-25"))
        self.assertEqual(out["clicks"], 1, "没标题也要能存（口径 b）")
        self.assertEqual(out["downloaded"], "2026-10-16_会议纪要.html")
        joined = "".join(out["toasts"])
        self.assertIn("还没有标题", joined, "必须如实说'这场还没有标题'：%s" % out["toasts"])
        self.assertIn("文件名用了日期", joined, "必须说明文件名用了日期：%s" % out["toasts"])

    def test_the_exported_html_is_still_self_contained(self):
        """**隐藏 bug 防线**：导出的 HTML 必须是自包含的（离线打开也不能去外面取东西），
        现在就是这样 —— 样式全内联、mermaid 的源码在生成时读进来内嵌。

        这条防的是"顺手把 mermaid 改成 `<script src=...>`"那类回退：文件一旦引用外部
        URL，用户离线打开（这正是这颗按钮存在的理由）就是一张白纸或一堆报错。
        """
        out = self._click(self._meeting(title="Q3 复盘"))
        html = out["html"]
        self.assertIsNotNone(html, "没拼出 HTML？")
        for bad in ('src="http', "src='http", 'href="http', "href='http",
                    "<link", "@import"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, html.lower() if bad == "<link" else html,
                                 "导出的 HTML 引用了外部资源（离线就打不开了）")
        self.assertIn("/*mermaid-stub*/", html,
                      "mermaid 必须是**内嵌源码**（生成时读进来），不是外链")
        self.assertNotIn("/web/vendor", html, "导出文件里不该留下站内资源路径")
        self.assertIn("正文在此", html, "既然等到成稿了，纪要正文就必须真的在里面")

    def test_a_second_click_while_exporting_does_not_save_twice(self):
        """真的点两次（并发）：只许存一份。

        这条是**行为**断言，不是查字符串 —— 没有防重入时，两次点击会各自走完导出，
        `a.click()` 被调两次（浏览器里就是「文件 (1).html」），而用例会拿到 `clicks == 2`。
        """
        driver = (_STUBS + "\n" + self._export_code()
                  + "\n(async () => {\n"
                  + "  M = " + json.dumps(self._meeting(title="Q3 复盘"), ensure_ascii=False) + ";\n"
                  + "  LAST_SUMMARY = READY;\n"
                  + "  await Promise.all([downloadOfflineHtml(), downloadOfflineHtml()]);\n"
                  + "  console.log(JSON.stringify({ clicks: CLICKS, toasts: TOASTS }));\n"
                  + "})();\n")
        out = self._run(driver)
        self.assertEqual(out["clicks"], 1, "同一时刻的两次点击只该存一份")
        self.assertTrue(any("正在导出" in t for t in out["toasts"]),
                        "第二次点击要**看得见地**给出反馈：%s" % out["toasts"])

    def test_a_summary_of_only_whitespace_counts_as_unfinished(self):
        """`summary.md` 里只有空白 = **没成稿**（服务端 `_notify_summary_ready()` 也是
        同一个判据：空白正文不发"纪要好了"）。这里两边口径必须一致，否则会出现
        "手机没收到通知、面板却肯导出"这种自相矛盾。"""
        driver = (_STUBS + "\n" + self._export_code()
                  + "\n(async () => {\n  M = " + json.dumps(self._meeting(), ensure_ascii=False) + ";\n"
                  + "  SUMMARY_SCRIPT = [{ exists: true, content: '   \\n\\t ' }];\n"
                  + "  LAST_SUMMARY = { exists: true, content: '   \\n\\t ' };\n"
                  + "  await downloadOfflineHtml();\n"
                  + "  console.log(JSON.stringify({ clicks: CLICKS, toasts: TOASTS }));\n"
                  + "})();\n")
        out = self._run(driver)
        self.assertEqual(out["clicks"], 0, "只有空白的纪要 = 没好，不许导出")
        self.assertIn("还没生成好", out["toasts"][-1])


class ExportSourceContractTests(_NodeHarness):
    """源码一级的钉子：入口只有一处、等待有上限、文件名只由 `buildExportFileName()` 决定。"""

    def test_the_button_wiring_is_unchanged(self):
        """接口路径与参数**一个都没改**（这是前端导出，后端一行不动）。"""
        for path in ("/api/meetings/${M.id}/file?kind=summary",
                     "/api/meetings/${M.id}/file?kind=topics",
                     "/api/meetings/${M.id}/file?kind=transcript"):
            with self.subTest(path=path):
                self.assertIn(path, self.src)
        self.assertIn('$("#btnExport").addEventListener("click", downloadOfflineHtml)',
                      self.src, "按钮的接线还在原处")

    def test_a_second_click_is_blocked_by_a_thin_entry_shell(self):
        """入口那一层只做一件事：**防重入**（复位必须在 `finally` 里，任何出口都解锁）。

        为什么需要它、为什么是这次才需要：导出从"立刻完成"变成"可能等 30 秒"，用户以为
        没反应就会再点一次 —— 两个并发等待会各存一份同样的文件。旧代码那个窗口只有几十
        毫秒，所以以前不需要这道闸（见页面里的注释）。
        """
        entry = _js_function(self.src, EXPORT_ENTRY_SIG)
        self.assertIn("_exportRunning", entry)
        self.assertIn("_exportOfflineHtml()", entry, "薄壳要委派给本体")
        self.assertIn("finally", entry, "复位要在 finally 里 —— 否则一次异常就把按钮锁死了")
        self.assertIn("正在导出", entry, "第二次点击要有可见反馈，不是静默忽略")

    def test_the_file_name_has_exactly_one_source(self):
        """`a.download` 只许来自 `buildExportFileName()`。

        这条挡的是"在下载那里再拼一次"：两处拼名字迟早不一致（一处清了非法字符、
        另一处没清），而且用户看到的名字与提示里念的名字会对不上。
        """
        fn = _js_function(self.src, EXPORT_SIG)
        self.assertIn("a.download = fname.name;", fn)
        self.assertEqual(fn.count("a.download"), 1, "`a.download` 只该被赋值一次")
        self.assertNotIn("+ \"_会议纪要.html\"", fn, "旧的时间戳写法必须已删掉")

    def test_the_wait_is_bounded_and_has_feedback(self):
        """**有上限**的等待 + 期间有可见反馈（写在导出那一节里，不在 handler 里现编）。"""
        sec = self.src[self.src.index(EXPORT_SECTION):self.src.index(EXPORT_ENTRY_SIG)]
        self.assertRegex(sec, r"const EXPORT_SUMMARY_WAIT_MS = 30000;",
                         "上限要是一个明面上的常量（30 秒）")
        self.assertRegex(sec, r"const EXPORT_SUMMARY_POLL_MS = \d+;", "轮询间隔也要是常量")
        wait = _js_function(sec, "async function waitForExportSummary()")
        self.assertIn("Date.now() - t0", wait, "上限必须是「算出来的」，不是「轮询固定次数」")
        self.assertIn("leftMs <= 0", wait, "到点必须收手（不能无限等）")
        self.assertIn("toast(", wait, "等待期间要有可见反馈")

    def test_the_html_has_no_external_reference_in_the_template(self):
        """模板里也不许出现外链标签（mermaid 是**读进源码再内嵌**的）。"""
        fn = _js_function(self.src, EXPORT_SIG)
        for bad in ("<link", "@import", 'src="http', 'href="http'):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, fn)
        self.assertIn('await (await fetch("/web/vendor/mermaid.min.js")).text()', fn,
                      "mermaid 要在生成时读成文本内嵌（这就是「自包含」的实现方式）")

    def test_the_inline_script_still_parses(self):
        """`web/*.html` 里的内联脚本**没有任何门禁在解析**（`test_js_syntax.py` 只看 `.js`）。

        这个按钮就住在 `meeting.html` 的内联脚本里，一个多余的 `}` 会让整个详情页
        白屏（2026-10-06 在 `app.js` 上正是这么栽的），所以这里补一道。
        """
        if not self.node:
            self.fail("找不到 node —— 内联脚本没被解析过（这不等于通过）。")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "meeting.inline.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.src)
            r = subprocess.run([self.node, "--check", path], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(r.returncode, 0,
                             "web/meeting.html 的内联脚本解析不过（浏览器会整页挂掉）：\n%s"
                             % (r.stderr or "")[:1500])

    def test_the_page_does_not_reference_the_removed_name_rule(self):
        """详情页里不该再有"拿会议目录名当文件名"的旧写法。"""
        fn = _js_function(self.src, EXPORT_SIG)
        self.assertNotIn("(M.name ||", fn, "文件名不该再回落到会议目录名")
        # 页面标题/`DATA.name` 用 `M.title || M.name` 是**展示**，与文件名无关，仍然留着
        self.assertIn("M.title || M.name", fn)
        # 而文件名那一处只吃 `m.title`
        fname = _js_function(self.src, "function buildExportFileName(")
        self.assertIn("m && m.title", fname)
        self.assertNotIn(".name", fname, "文件名里不许出现会议目录名")


if __name__ == "__main__":
    unittest.main()
