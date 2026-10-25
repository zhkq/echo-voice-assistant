# -*- coding: utf-8 -*-
"""回顾剩下的两个遗留缺陷（2026-10-18，上一轮审计的 `i.` 与「结束回顾」话术）。

## ① `history_list` 当天摘要只取"最新一行"的 `brief` ✗

历史页一天一行，摘要取**当天最新一条有内容的播报**。旧实现写的是
`if item["turns"] == 1 and brief:` —— 只认当天**最新那一轮**：

  * 最新一轮 `brief` 为空（那一轮失败、或技能没按【播报】契约输出）时，
    即便更早几轮**明明播过**，列表也显示空白 —— **空 brief 吃掉了那天的播报** ✗；
  * 用户看到的是"这天好像什么都没说"，而详情页里其实有好几句。

现在从最新往回找**第一条非空** `brief`；全空就如实留空（不退回口述原文、不编）。

## ② 面板「结束回顾」的 toast 恒说"已请求结束这场回顾" ✗

服务端 `POST /api/daily-review/stop` 已经**如实回了** `stopped`
（= `assistant.stop_review()` 的判词：主机上到底有没有停掉一场），
但 `web/app.js` 不读它，两个入口（设置卡 / 仪表盘）都一律说"已请求结束" ✗。
主机上已经没有在跑的那场时，这句话会让用户以为还有一场在等它收尾。

现在按 `stopped` 三态说话：

  * `true`      → "已结束这场回顾"；
  * `false`     → "主机上这场回顾已经不在跑了"；
  * 拿不到字段  → 保留原来那句"已请求结束这场回顾"（宁含糊，不替服务端下结论）。

## 测试卫生

  * 历史那一半用临时库（复用 `tests/test_daily_review._Base`），**不碰真库**；
  * 面板那一半把 `web/app.js` 里的**真函数**抠出来交给 `node` 跑
    （与 `tests/test_waiting_reason_ui.py` / `tests/test_meetings_list_source.py`
    同一套做法：只查源码文本查不出"用户最后看到哪句话"）。
    没有 node 就**响亮失败** —— 静默跳过等于假装查过了。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                                  # noqa: E402
from app import daily_review as dr                                   # noqa: E402
from tests.test_daily_review import _Base                            # noqa: E402


# ===========================================================================
# ① `history_list` 的 brief 回退
# ===========================================================================

class HistoryBriefFallbackTests(_Base):
    """一天的摘要 = **当天最新一条非空**播报；空 brief 不许吃掉更早的。"""

    def _turn(self, text, spoken, ts):
        """记一轮回顾，并把 `ts` 钉成指定的那一刻（避免靠 sleep 猜先后）。"""
        cid = dr._persist(text, {"ok": True, "spoken": spoken})
        db._exec("UPDATE commands SET ts=? WHERE id=?", (ts, cid))   # noqa: SLF001
        return cid

    def test_a_blank_newest_turn_falls_back_to_the_older_brief(self):
        """**核心判据**：最新一轮播报为空、中间那轮有 → 摘要取**中间**那句。

        这正是审计 `i.` 的那个现场：3 轮里最新那轮 `brief` 是空串，
        旧实现 `turns == 1` 直接放弃 → 最早/中间两句明明在库里，列表却是空的。
        """
        self._turn("最早那轮的口述", "最早那句播报", "2026-10-16 08:00:00")
        self._turn("中间那轮的口述", "中间那句播报", "2026-10-16 09:00:00")
        self._turn("最新那轮的口述", "", "2026-10-16 10:00:00")

        item = dr.history_list()["items"][0]
        self.assertEqual("2026-10-16", item["date"])
        self.assertEqual(3, item["turns"], "三轮都在这一天")
        self.assertEqual("中间那句播报", item["brief"],
                         "最新一轮 brief 为空时，必须往回找到中间那条播报")
        self.assertEqual("2026-10-16 10:00:00", item["lastAt"],
                         "`lastAt` 仍是**最新那一轮**的时间（摘要回退不改时间）")

    def test_every_turn_blank_leaves_the_brief_empty(self):
        """整天都没有播报 → **如实留空**：不退回口述原文，也不编一句。

        `text` 三行都非空，正是为了钉住"摘要**不**拿原文顶替播报"。
        """
        self._turn("口述一", "", "2026-10-16 08:00:00")
        self._turn("口述二", "   ", "2026-10-16 09:00:00")
        self._turn("口述三", "", "2026-10-16 10:00:00")

        item = dr.history_list()["items"][0]
        self.assertEqual(3, item["turns"])
        self.assertEqual("", item["brief"], "没有播报就留空，别编、别拿原文顶替")

    def test_a_non_blank_newest_turn_still_wins(self):
        """最新一轮有播报 → 仍取最新那条（回退**只在为空时**发生，别倒过来）。"""
        self._turn("早", "早先那句", "2026-10-16 08:00:00")
        self._turn("晚", "最后那句", "2026-10-16 10:00:00")

        self.assertEqual("最后那句", dr.history_list()["items"][0]["brief"])


# ===========================================================================
# ② 面板「结束回顾」的话术（node 真跑 `web/app.js` 里的函数）
# ===========================================================================

#: 只替**页面之外**的东西（接口 / toast / DOM / 卡片刷新）；被测函数原样跑。
_STOP_STUBS = r"""
let TOASTS = [];
function toast(msg) { TOASTS.push(String(msg)); }
function $(sel) { return null; }
let REPLY = {};
async function post(path, body) { return REPLY; }
function refreshReviewCard() { return Promise.resolve(); }
"""


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _node():
    """找一个能用的 node（与 `tests/test_js_syntax.py` 同一份探测顺序）。"""
    exe = shutil.which("node")
    if exe:
        return exe
    for c in (os.path.expanduser(r"~\AppData\Local\hermes\node\node.exe"),
              os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
              r"C:\Program Files\nodejs\node.exe"):
        if os.path.isfile(c):
            return c
    return ""


def _js_function(src, sig):
    """从 `sig` 起、到下一个**顶格** `}` 为止（本仓库一贯的写法）。"""
    i = src.index(sig)
    return src[i:src.index("\n}", i) + 2]


class StopReviewWordingTests(unittest.TestCase):
    """`stopped` 三态各自说哪句话 —— 跑的是 `doReviewStop()` 这个真入口。"""

    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.app_js = _read("web", "app.js")

    def _run_node(self, driver):
        if not self.node:
            self.fail("找不到 node —— 这一组用例没跑（这不等于通过）。"
                      "装一个 node，或把它放到 PATH / %LOCALAPPDATA%\\hermes\\node\\。")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(driver)
            try:
                r = subprocess.run([self.node, path], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
            except subprocess.TimeoutExpired:
                self.fail("node 60 秒还没结束 —— 被测函数里很可能有死循环")
            self.assertEqual(r.returncode, 0,
                             "node 跑面板真代码失败了：\n%s" % (r.stderr or "")[-3000:])
            return json.loads(r.stdout)

    def _click_stop(self, replies):
        """依次把 `REPLY` 换成服务端回包，真调 `doReviewStop()`，收集每轮的 toast。"""
        entry = _js_function(self.app_js, "async function doReviewStop()")
        driver = (_STOP_STUBS + "\n" + entry + "\n"
                  + "const CASES = " + json.dumps(replies, ensure_ascii=False) + ";\n"
                  + "(async () => {\n"
                  + "  const toasts = [];\n"
                  + "  for (const c of CASES) {\n"
                  + "    REPLY = c;\n"
                  + "    TOASTS = [];\n"
                  + "    await doReviewStop();\n"
                  + "    toasts.push(TOASTS.slice());\n"
                  + "  }\n"
                  + "  console.log(JSON.stringify({ toasts: toasts }));\n"
                  + "})();\n")
        return self._run_node(driver)

    def test_stopped_true_says_it_really_ended(self):
        """`stopped=true` = 刚才真的停了一场 → 说"已结束"，不再说"已请求结束"。"""
        out = self._click_stop([{"ok": True, "stopped": True}])
        msg = out["toasts"][0][0]
        self.assertIn("已结束", msg, "服务端说真的停了，就该说已结束：%s" % msg)
        self.assertNotIn("已请求结束", msg, "不许再说那句含糊话：%s" % msg)
        self.assertNotIn("已经不在跑了", msg)

    def test_stopped_false_says_nothing_was_running(self):
        """`stopped=false` = 主机上根本没有在跑的回顾 → **如实说**，别让用户白等收尾。"""
        out = self._click_stop([{"ok": True, "stopped": False}])
        msg = out["toasts"][0][0]
        self.assertIn("已经不在跑了", msg,
                      "服务端说没停到任何一场，就该如实说主机上已经不在跑：%s" % msg)
        self.assertNotIn("已请求结束", msg, "这句会让用户以为还有一场在收尾：%s" % msg)
        self.assertNotIn("已结束", msg, "没停到任何一场，不能说「已结束」：%s" % msg)

    def test_a_missing_field_keeps_the_old_wording(self):
        """拿不到 `stopped`（老服务端 / 回包异常）→ 保留原来那句，**不替服务端下结论**。

        三态里最容易写错的是这一档：用 `r.stopped === false` 才不会把
        `undefined` / `{}` 当成"没在跑"而多说一句服务端没说过的话。
        """
        out = self._click_stop([{"ok": True}, {}, None])
        for i, msg in enumerate(out["toasts"]):
            with self.subTest(case=i):
                self.assertEqual("已请求结束这场回顾", msg[0],
                                 "拿不到字段就必须保留原话术：%s" % (msg,))

    def test_the_panel_reads_the_server_field_not_a_guess(self):
        """源码一级：话术来自服务端回的 `stopped`（不是面板自己猜"停没停"）。

        两个「结束回顾」入口（设置卡 + 仪表盘）都要读它 —— 只修一个的话，
        另一个入口仍会恒说"已请求结束"。
        """
        entry = _js_function(self.app_js, "async function doReviewStop()")
        self.assertIn("r.stopped === true", entry)
        self.assertIn("r.stopped === false", entry)

        # 设置卡那个按钮（`bindDailyReviewCard` 里的 `act === "stop"` 分支）同一口径。
        i = self.app_js.index('} else if (act === "stop") {')
        j = self.app_js.index('} else if (act === "submit") {', i)
        branch = self.app_js[i:j]
        self.assertIn("r.stopped === true", branch, "设置卡的结束按钮也要读 stopped")
        self.assertIn("r.stopped === false", branch)
        self.assertIn("主机上这场回顾已经不在跑了", branch)

        # 服务端确实在回这个字段（面板读的是事实，不是约定俗成）。
        # 2026-10-25：结束还要一并停准备，所以回包多了 `preStopped`（两个字段分开）。
        api_src = _read("app", "api.py")
        self.assertIn('"stopped": stopped', api_src)
        self.assertIn('"preStopped": pre_stopped', api_src)


# ===========================================================================
# ③ 仪表盘 `history_summary()` 的 `lastBrief` 回退（与 ① 同口径）
# ===========================================================================

class HistorySummaryBriefFallbackTests(_Base):
    """仪表盘那句"最近一次播报" = **最新一条非空**播报；`lastAt` 仍是**最新那一轮**。

    这是与 ① 同族的第二处：`history_list` 修好了，但仪表盘卡片读的是
    `history_summary()`，它原来**只取最新一行的 `brief`** ✗ —— 最新一轮 brief 为空时，
    卡片上"最近一次播报"整块变空，而更早那轮明明播过。

    `lastAt` 与 `lastBrief` 语义不同，**不许一起回退**：
    `lastAt` 答"最近一次回顾发生在什么时候"（要最新那一轮，哪怕它 brief 是空的），
    `lastBrief` 答"最近一次播报说了什么"（往回找到第一条非空的）。
    """

    def _turn(self, text, spoken, ts):
        """记一轮回顾，并把 `ts` 钉成指定的那一刻（与上面 `_turn` 同一做法）。"""
        cid = dr._persist(text, {"ok": True, "spoken": spoken})
        db._exec("UPDATE commands SET ts=? WHERE id=?", (ts, cid))   # noqa: SLF001
        return cid

    def test_a_blank_newest_turn_falls_back_but_keeps_the_newest_last_at(self):
        """**核心判据**：最新一轮 brief 空、前一轮有 → `lastBrief` 取前一轮，`lastAt` 取最新。

        这是仪表盘的现场：最新那轮失败了（brief 为空），旧实现让卡片显示
        "最近一次播报：—"，可上一轮**刚刚播过**。同时 `lastAt` 必须仍是**最新那一轮**
        的时间 —— 那一轮确实发生过了，"最近一次回顾是什么时候"不该被 brief 空不空影响。
        """
        self._turn("早一轮的口述", "早一轮那句播报", "2026-10-18 08:00:00")
        self._turn("最新那轮的口述", "", "2026-10-18 10:00:00")

        s = dr.history_summary()
        self.assertEqual("早一轮那句播报", s["lastBrief"],
                         "最新一轮 brief 为空时，必须往回找到上一轮那句播报")
        self.assertEqual("2026-10-18 10:00:00", s["lastAt"],
                         "`lastAt` 仍是**最新那一轮**的时间（brief 的回退不改时间）")

    def test_every_turn_blank_leaves_last_brief_empty(self):
        """全部为空 → `lastBrief == ""`：**如实留空**，不拿 `text` 顶替、不编。

        三行 `text` 都非空，正是为了钉住"摘要**不**退回口述原文"这条。
        """
        self._turn("口述一", "", "2026-10-18 08:00:00")
        self._turn("口述二", "   ", "2026-10-18 09:00:00")
        self._turn("口述三", "", "2026-10-18 10:00:00")

        s = dr.history_summary()
        self.assertEqual("", s["lastBrief"], "没有播报就留空，别编、别拿原文顶替")
        self.assertNotIn(s["lastBrief"], ("口述一", "口述二", "口述三"),
                         "`text`（口述原文）不许顶进 `lastBrief`")
        self.assertEqual("2026-10-18 10:00:00", s["lastAt"],
                         "全部为空时 `lastAt` 仍是最新那一轮的时间")

    def test_a_non_blank_newest_turn_still_wins(self):
        """最新一轮有播报 → 就是最新那句（回退**只在为空时**发生，别倒过来）。"""
        self._turn("早", "早先那句", "2026-10-18 08:00:00")
        self._turn("晚", "最后那句", "2026-10-18 10:00:00")

        s = dr.history_summary()
        self.assertEqual("最后那句", s["lastBrief"])
        self.assertEqual("2026-10-18 10:00:00", s["lastAt"])


# ===========================================================================
# ④ 仪表盘「最近一行」：时间/状态属于最新一轮，播报可能来自更早一轮
# ===========================================================================

#: 只替**依赖**（HTML 转义工具）；被抠出去交给 node 跑的是 `reviewLastLineHTML()` 本身。
_SUMMARY_STUBS = r"""
function esc(s) { return String(s ?? ""); }
"""


class ReviewLastLineWordingTests(unittest.TestCase):
    """`reviewLastLineHTML()` 用 node 真跑：读的是用户最终看到的那句话。

    服务端 `history_summary()` 里 `lastAt` / `lastStatus` 取**最新那一轮**，
    而 `lastBrief` 是**从最新往回找到的第一条非空播报**（可能来自更早一轮）。
    接口不告诉面板"这条播报是哪一轮的"，所以面板只能把两件事**分开说** ——
    拼成一句就会让用户以为那句播报是 10:00 说的、还被"那次失败了"评价了一次。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.app_js = _read("web", "app.js")

    def _html_for(self, sums):
        if not self.node:
            self.fail("找不到 node —— 这一组用例没跑（这不等于通过）。"
                      "装一个 node，或把它放到 PATH / %LOCALAPPDATA%\\hermes\\node\\。")
        entry = _js_function(self.app_js, "function reviewLastLineHTML(sum)")
        driver = (_SUMMARY_STUBS + "\n" + entry + "\n"
                  + "const CASES = " + json.dumps(sums, ensure_ascii=False) + ";\n"
                  + "console.log(JSON.stringify("
                  + "{ html: CASES.map((c) => reviewLastLineHTML(c)) }));\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(driver)
            try:
                r = subprocess.run([self.node, path], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
            except subprocess.TimeoutExpired:
                self.fail("node 60 秒还没结束 —— 被测函数里很可能有死循环")
        self.assertEqual(r.returncode, 0,
                         "node 跑面板真代码失败了：\n%s" % (r.stderr or "")[-3000:])
        return json.loads(r.stdout)["html"]

    def test_a_blank_newest_brief_is_not_glued_to_the_newest_time(self):
        """**核心判据**：最新一轮失败（它没有播报）+ 更早一轮有播报 →

        时间/状态 与 播报句必须**分开说**，不能再拼成
        "最近一次 10-18 10:00：{上一轮那句播报} 那次失败了"。
        """
        html = self._html_for([{
            "lastAt": "2026-10-18 10:00:00",
            "lastBrief": "上一轮那句播报",
            "lastStatus": "failed",
        }])[0]
        head, sep, tail = html.partition("<br>")
        self.assertTrue(sep, "时间/状态与播报必须分成两句（要能看出不是同一件事）：%s" % html)
        self.assertIn("10-18 10:00", head, "第一句要说是哪一轮回顾：%s" % html)
        self.assertIn("失败", head, "第一句要如实带那一轮的状态：%s" % html)
        self.assertNotIn("上一轮那句播报", head,
                         "播报句不许留在时间/状态那一句里：%s" % html)
        self.assertIn("上一轮那句播报", tail, "第二句才是播报：%s" % html)
        self.assertNotIn("10-18 10:00", tail,
                         "播报那一句不该再重复最新一轮的时间（会被读成属于那一轮）：%s" % html)
        self.assertNotIn("那次失败了", html,
                         "不许再把「那次失败了」挂在播报句后面：%s" % html)
        self.assertNotIn("</span>：上一轮那句播报", html,
                         "旧的那句混合文案必须消失：%s" % html)

    def test_a_non_blank_newest_turn_keeps_the_existing_wording_verbatim(self):
        """最新一轮正常（成功 + 有播报）→ 文案与改动前**逐字相同**（回归钉子）。"""
        html = self._html_for([{
            "lastAt": "2026-10-18 10:00:00",
            "lastBrief": "最后那句播报",
            "lastStatus": "done",
        }])[0]
        self.assertEqual('<span class="dim">最近一次 10-18 10:00</span>：最后那句播报',
                         html, "常用文案不许为极端情况改啰嗦")

    def test_no_brief_at_all_says_that_round_had_none(self):
        """全部播报为空 → 只说回顾的时间/状态，并**如实说**"那一轮没有播报"。"""
        html = self._html_for([{
            "lastAt": "2026-10-18 10:00:00",
            "lastBrief": "",
            "lastStatus": "failed",
        }])[0]
        self.assertIn("10-18 10:00", html)
        self.assertIn("失败", html)
        self.assertIn("没有播报", html, "没有播报就如实说：%s" % html)
        self.assertNotIn("最近一次播报", html, "没有播报就别写「最近一次播报：」：%s" % html)

    def test_no_review_at_all_hides_the_line(self):
        """从没回顾过（接口连时间和播报都没给）→ 整行收起，不说半句空话。"""
        for case in ({}, {"lastAt": "", "lastBrief": "   ", "lastStatus": ""}):
            with self.subTest(case=case):
                self.assertEqual("", self._html_for([case])[0])

    def test_the_card_actually_uses_this_function(self):
        """仪表盘那一行必须真的走这个函数（别只在注释里修），旧话术要从模板里消失。"""
        self.assertIn("reviewLastLineHTML(sum)", self.app_js)
        self.assertNotIn('sbadge warn">那次失败了', self.app_js,
                         "旧的那句混合话术（把「那次失败了」挂在播报后面）必须消失")


if __name__ == "__main__":
    unittest.main()
