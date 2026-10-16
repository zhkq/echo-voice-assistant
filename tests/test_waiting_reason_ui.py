# -*- coding: utf-8 -*-
"""补完「待转写 / 纪要生成中」这个故事的两半（用户 2026-10-16 口径）。

## 这一组用例钉的是**两件**事，它们同属一个故事：**状态要能指向"下一步做什么"**

### A. 「待转写」必须把**为什么**说出来（此前面板一处都不渲染 `meetings.error`）

后端（提交 `7abd6c72`）已经把"录音完成但转写没成"落成 `imported`（面板译作「待转写」），
原因人话写在 `meetings.error`、原因词在 `meta.transcribeFallbackReason`。但面板上：

  * 会议列表/仪表盘只显示徽标「待转写」三个字 —— 既不知道**为什么**没转，
    也不知道**下一步做什么**（去起后端？去配后端？去改「允许音频去哪」？）；
  * 详情页（`web/meeting.html`）**一处都不渲染 `M.error`**。

只显示"待转写"等于把用户丢在半路。这一半钉住：

  1. `imported` + 有 `error` → 列表条**真的渲染后端那句原话**，用**黄**（`--yellow`）不是红；
  2. `imported` **无** `error` → **不渲染**（不编一句"原因未知"占地方）；
  3. `transcribed` / `error` 的行为**不变**（红字那条不许被改坏）；
  4. `imported` 的徽标仍是 `idle`（不染红），并且带 `title` 释义；
  5. 详情页出现 `<div … data-testid="waiting-reason">` 且有内容 —— 连**没有能力快照**
     的待转写会议也要能显示（那一场恰恰最需要它）。

### B. 「纪要生成中」必须是一个**真的**可读状态（此前压根没有这个信号）

同一轮的另一半：`app/meeting.py::regenerate_summary()` 在 `request_summary()` 派发后台
线程后**立刻** `finish_summary_run(run_id, "done")` → `summary_runs.status` 永远看不到
pending ✗ → 面板只能写"还没生成好（**也可能还没发起生成**）"，于是**压根没任务**的那种
也要白等 30 秒 ✗（等一个不存在的任务 = 骗人）。

现在 `meeting.summary_state()` 给出 `ready` / `running` / `none` 三档，`GET
/api/meetings/{id}/file?kind=summary` 把它带给面板，「⬇ 离线HTML」据此：

  * `running` → "纪要**正在生成**，等它完成…"（说得准确）；
  * `none`    → **立刻**说"这场还没有在跑的纪要生成任务（还没发起过，或上一次没成稿）"
                + 下一步（去点「重新生成纪要」）—— **一次都不等**；
  * 失败/超时/异常/进程崩溃留下的运行**一律收口**（不会永远 pending，也就不会让
    面板永远说"正在生成"）。

## 为什么这个文件里既有"真跑 JS"又有后端用例

它们是**同一个用户可见行为**的两端：面板那句话是**根据服务端判词**说的，只钉一头
（只钉 JS 文案、或只钉后端字段）都证明不了"用户看到的是真的"。跑法也是同一条命令
（见任务给定的门禁组合），所以放在一起。

  * 面板那半用 `node` **真跑**页面函数（`renderMeetingItems` / `renderCapability` /
    `waitForExportSummary`）：字符串在不在查不出"黄还是红""到底等了没有"——
    我可以把 `--yellow` 写成 `--red` 而字面上仍然有那句话。`<script>` 是从
    `web/meeting.html` **原样抠出来**的（一个字都不手抄：手抄的用例只证明我抄得对）。
    没有 node 就**响亮失败**（与 `tests/test_js_syntax.py` / `test_meeting_export_html.py`
    同一条约定：静默跳过等于假装查过了）。
  * 后端那半用临时库 + 临时会议目录（照 `tests/test_meeting_waiting_backend.py` 的写法），
    **一个字节都不写进真库**；DSH/LLM 全部打桩（本机正跑着 ECHO，不许碰真的）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db                                                       # noqa: E402
from app import meeting                                                  # noqa: E402
from app.config import settings                                          # noqa: E402

#: 后端 `_waiting_text()` 真会写进 `meetings.error` 的那种句子（用例里逐字用它，
#: 这样"面板显示的是后端原话"才是可断言的事实，而不是我自己编的一句）。
WAITING_TEXT = ("录音已完成，音频已保存；等转写后端可用后可重新转写"
                "（原因：后端暂不可用）")

EXPORT_SECTION = "/* ---------- 离线 HTML 导出 ----------"
EXPORT_SIG = "async function _exportOfflineHtml()"
EXPORT_ENTRY_SIG = "async function downloadOfflineHtml()"

#: 会议列表那一段（`meetingBadgeCls` + 两个文案表 + `renderMeetingItems`）——
#: 从 `let _meetings` 到 `pollTranscribe` 之间**全是**这一段（原样抠）。
APP_REGION_START = "let _meetings = [];"
APP_REGION_END = "async function pollTranscribe() {"


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
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


def _inline_script(html):
    """页面主脚本正文（`<script>`…最后一个 `</script>`）。

    取**最后一个** `</script>` 是必须的：导出拼出来的 HTML 里有一行
    `'</scr' + 'ipt></body></html>'`（故意拆开），用 `index()` 会在那儿截住。
    """
    i = html.index("<script>")
    j = html.rindex("</script>")
    return html[i + len("<script>"):j]


class _NodeCase(unittest.TestCase):
    """把页面里的真代码抠出来交给 node 跑的公共部分（失败就**响亮**失败）。"""

    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.app_js = _read("web", "app.js")
        cls.html = _read("web", "meeting.html")
        cls.src = _inline_script(cls.html)

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
                # 页面代码里真出现"永远等下去"时用例必须**红**，不能把门禁挂死
                self.fail("node 60 秒还没结束 —— 等待逻辑很可能没有上限（无限等/死循环）")
            self.assertEqual(r.returncode, 0,
                             "node 跑页面真代码失败了：\n%s" % (r.stderr or "")[-3000:])
            return json.loads(r.stdout)


# ===========================================================================
# A① 会议列表：`imported` + error → 渲染后端那句原话（黄，不是红）
# ===========================================================================

#: 只替**页面之外**的东西：`esc`/`fmtHM`/`$$`（DOM）用替身，被测的两个函数原样跑。
_APP_STUBS = r"""
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtHM() { return "10 分钟"; }
const STATUS_TEXT = {};
const $$ = () => [];
function openMeetingDetail() {}
"""


class MeetingListWaitingReasonTests(_NodeCase):
    """列表条目（仪表盘"最近几场"与「历史 → 会议历史」共用同一次渲染）。"""

    def _render(self, cases, full=False):
        """真调 `renderMeetingItems()`，返回每条渲染出来的 HTML。"""
        region = self.app_js[self.app_js.index(APP_REGION_START):
                             self.app_js.index(APP_REGION_END)]
        driver = (_APP_STUBS + "\n" + region + "\n"
                  + "const CASES = " + json.dumps(cases, ensure_ascii=False) + ";\n"
                  + "const OPTS = {full: " + ("true" if full else "false") + "};\n"
                  + "const el = { innerHTML: '' };\n"
                  + "const out = [];\n"
                  + "for (const c of CASES) { el.innerHTML = '';"
                  + " renderMeetingItems(el, [c], OPTS); out.push(el.innerHTML); }\n"
                  + "console.log(JSON.stringify({ rows: out,\n"
                  + "  cls: { imported: meetingBadgeCls('imported'),"
                  + "         error: meetingBadgeCls('error'),"
                  + "         transcribed: meetingBadgeCls('transcribed'),"
                  + "         transcribing: meetingBadgeCls('transcribing') } }));\n")
        return self._run_node(driver)

    @staticmethod
    def _m(status, error=None, **extra):
        row = {"id": 7, "name": "2026-10-16_10-00-00", "title": "周例会",
               "started_at": "2026-10-16T10:00:00", "duration_seconds": 600,
               "segments": 3, "status": status}
        if error is not None:
            row["error"] = error
        row.update(extra)
        return row

    def test_the_backend_sentence_is_shown_for_a_waiting_meeting(self):
        """A 的核心判据：`imported` + `error` → 面板**真的**渲染后端那句原话。

        只显示徽标「待转写」三个字等于把用户丢在半路：他不知道**为什么**没转、
        也就不知道**下一步做什么** —— 这句话就是那个答案。
        """
        out = self._render([self._m("imported", WAITING_TEXT)])
        html = out["rows"][0]
        self.assertIn('data-testid="waiting-reason"', html, "待转写那一行没渲染：%s" % html)
        self.assertIn("录音已完成，音频已保存", html, "后端那句原话没显示出来")
        self.assertIn("后端暂不可用", html, "原因词的那半也要在（它才是「下一步」的依据）")
        # **黄，不是红**：这不是故障（音频在、后端回来就能转），红字会让人以为录音丢了
        self.assertIn("var(--yellow)", html, "待转写必须用黄字：%s" % html)
        self.assertNotIn("var(--red)", html, "待转写**不许**染红 —— 那正是这一轮要修的谎")
        self.assertNotIn("badge error", html, "徽标也不许是红的")

    def test_a_waiting_meeting_without_a_reason_renders_nothing_extra(self):
        """`imported` 但后端没记原因（空串）→ **不渲染空行**，也不编一句"原因未知"。"""
        for row in (self._m("imported", ""), self._m("imported", "   "), self._m("imported")):
            with self.subTest(row=row):
                html = self._render([row])["rows"][0]
                self.assertNotIn("waiting-reason", html, "没原因就不该有那一行：%s" % html)
                self.assertNotIn("var(--yellow)", html)
                self.assertIn("待转写", html, "徽标本身还要在")

    def test_the_older_statuses_are_unchanged(self):
        """`transcribed` 什么都不显示；`error` 仍是红字 + 后端原话（别把既有渲染改坏）。"""
        out = self._render([self._m("transcribed"), self._m("error", "转写引擎报错：CUDA OOM"),
                            self._m("error", "")])
        # 已转写：那一行**不存在**（`m-meta` 那行本来就有，是"开始时间 · 时长 · 段数"，
        # 所以判据只能看 waiting-reason / 颜色，不能看 `m-meta` 这个类名本身）
        self.assertNotIn("waiting-reason", out["rows"][0], out["rows"][0])
        self.assertNotIn("var(--yellow)", out["rows"][0])
        self.assertNotIn("var(--red)", out["rows"][0], "已转写的不该有红字原因行")
        self.assertIn("var(--red)", out["rows"][1], "error 仍然是红的")
        self.assertIn("CUDA OOM", out["rows"][1], "error 仍显示引擎原话")
        self.assertNotIn("var(--yellow)", out["rows"][1], "error 不是「待转写」那种黄")
        self.assertIn("原因没有记下来", out["rows"][2], "error 没原因时仍有那句兜底提示")

    def test_the_imported_badge_is_idle_and_explains_itself(self):
        """徽标：`imported` → `idle`（不染红）+ `title` 释义（说清"录音在、转写没成"）。"""
        out = self._render([self._m("imported", WAITING_TEXT)])
        self.assertEqual("idle", out["cls"]["imported"], "待转写的徽标必须是 idle")
        self.assertEqual("error", out["cls"]["error"], "error 那一档不受影响")
        self.assertEqual("idle", out["cls"]["transcribed"])
        self.assertEqual("transcribing", out["cls"]["transcribing"], "在转的那档不受影响")
        html = out["rows"][0]
        self.assertIn("录音已完成、音频已保存；转写还没成", html,
                      "徽标要带 title 释义，把状态指向下一步：%s" % html)
        self.assertIn('title="', html)

    def test_the_source_keeps_one_place_for_that_sentence(self):
        """源码一级的钉子：那句原话只有**后端**一个来源（面板不许自己编原因）。

        这里查的是"渲染出来的那句 **就是** 后端那句"（`esc(errText)`），而不是查某个词在不在 ——
        函数上方的注释里特意留着当年那句硬编码的教训（"麦克风没打开"），查词会把它误判成代码。
        """
        fn = _js_function(self.app_js, "function renderMeetingItems(")
        self.assertIn('m.status === "imported" && errText', fn,
                      "判据要明确：imported 且有 error")
        self.assertIn('style="color:var(--yellow)">⏳ ${esc(errText)}</div>', fn,
                      "黄字那一行显示的必须**就是**后端那句（转义后原样）")


# ===========================================================================
# A② 详情页：能力/运行那块要带上"为什么没转"
# ===========================================================================

_CAP_STUBS = r"""
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
let M = null;
let BOX = { className: "", innerHTML: "" };
function $(sel) { return BOX; }
"""


class MeetingDetailWaitingReasonTests(_NodeCase):
    """`web/meeting.html` 的 `renderCapability()`：详情页第一眼就能看到"为什么 + 怎么办"。"""

    def _render_cap(self, meeting):
        fn = _js_function(self.src, "function renderCapability()")
        driver = (_CAP_STUBS + "\n" + fn + "\n"
                  + "M = " + json.dumps(meeting, ensure_ascii=False) + ";\n"
                  + "BOX = { className: '', innerHTML: '' };\n"
                  + "renderCapability();\n"
                  + "console.log(JSON.stringify({ cls: BOX.className, html: BOX.innerHTML }));\n")
        return self._run_node(driver)

    def test_it_shows_the_reason_even_without_a_capability_snapshot(self):
        """**最硬的那一种**：待转写的会议连能力快照都没有 —— 那一行照样要出来。

        `M.capability` 是录音时写进 meta.json 的"用了谁"快照，而"没转成"的那一场根本
        没走到那一步。所以这一行必须在"没有能力信息就整块隐藏"的早退**之前**算出来，
        否则"最需要它的那一场偏偏不显示"。
        """
        out = self._render_cap({"id": 7, "status": "imported", "error": WAITING_TEXT,
                                "capability": None})
        self.assertIn('data-testid="waiting-reason"', out["html"], out["html"])
        self.assertIn("录音已完成，音频已保存", out["html"], "必须是后端那句原话")
        self.assertIn("cap-line warn", out["html"], "用既有的 warn（黄）样式")
        self.assertEqual("cap-box", out["cls"], "有话说时整块不许是 hidden")
        self.assertNotIn("hidden", out["cls"])

    def test_it_also_shows_when_a_capability_snapshot_exists(self):
        """有能力快照时，那一行与"用了谁"一起显示（那一行**不许**被能力块挤掉）。"""
        out = self._render_cap({
            "id": 7, "status": "imported", "error": WAITING_TEXT,
            "capability": {"picks": [{"slotLabel": "转写", "backendLabel": "本机"}],
                           "skipped": [], "run": None, "diarize": None,
                           "timestampsLabel": ""}})
        self.assertIn('data-testid="waiting-reason"', out["html"], out["html"])
        self.assertIn("录音已完成，音频已保存", out["html"])
        self.assertIn("本机", out["html"], "能力那一行也还在（不是二选一）")
        self.assertEqual("cap-box", out["cls"])

    def test_a_normal_meeting_is_unchanged(self):
        """已转写、也没有能力信息 → 整块仍然隐藏（别为了这一行把空盒子放出来）。"""
        out = self._render_cap({"id": 7, "status": "transcribed", "error": "",
                                "capability": None})
        self.assertEqual("", out["html"], "没有可显示的就不该有内容")
        self.assertEqual("hidden", out["cls"])

    def test_the_style_and_the_judgement_are_written_down(self):
        """源码一级：判据只有一处（`status === "imported" && error`），且它是黄的。"""
        self.assertIn('M.status === "imported"', self.src)
        self.assertIn('data-testid="waiting-reason"', self.src)
        css = self.html[self.html.index(".cap-box .cap-line.warn"):]
        self.assertIn("var(--yellow)", css[:120], "那一行必须是黄的（不是故障，也不是正常）")


# ===========================================================================
# B① 后端：`summary_state()` 说真话（有任务在跑 / 压根没有 / 已成稿）
# ===========================================================================

class _SummaryCase(unittest.TestCase):
    """临时库 + 临时会议目录 + 打桩的纪要会话（一个字节都不写进真库）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-summarystate-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-summarystatedb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        settings._cache = None
        self.root = os.path.join(self.tmp, "meetings")
        os.makedirs(self.root, exist_ok=True)
        p = patch.object(meeting, "meetings_dir", lambda: self.root)
        p.start()
        self.addCleanup(p.stop)

        def _clean():
            # 进程级的簿记（标记 / 队列）必须清干净：本文件里每个用例都重建临时库，
            # 会议 id 会**重复从 1 开始** —— 留着上一轮的 run id，下一轮就可能误认。
            with meeting._summarizing["lock"]:
                meeting._summarizing["set"].clear()
            with meeting._summary_run_queue["lock"]:
                meeting._summary_run_queue["by_meeting"].clear()
            db.DATA_DIR, db.DB_FILE = self._old_db
            settings._cache = None
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    # ---------------------------------------------------------------- 小工具
    def _meeting(self, name="2026-10-16_10-00-00", status="transcribed"):
        folder = os.path.join(self.root, name)
        os.makedirs(folder, exist_ok=True)
        mid = db.create_meeting(name, started_at="2026-10-16T10:00:00")
        db.update_meeting(mid, status=status, segments=1, duration_seconds=60.0)
        with open(os.path.join(folder, "transcript.md"), "w", encoding="utf-8") as fh:
            fh.write("# 会议转写\n\n[00.00.01] 张三：这场会讨论纪要状态。\n")
        return mid, folder

    def _wait(self, cond, timeout=8.0):
        """等后台线程跑到位（**别用 sleep 猜**：等的是那个条件本身）。"""
        deadline = time.time() + timeout
        while time.time() < deadline and not cond():
            time.sleep(0.01)
        return cond()

    def _run_status(self, mid):
        runs = db.get_summary_runs(mid)
        return (runs[0]["status"] if runs else "")

    def _spawn_with(self, mid, folder, client):
        """在**打桩的纪要会话**里跑一次 agent 路径（`summary.md`）。"""
        with patch.object(meeting, "get_client", lambda: client), \
                patch.object(meeting, "_summary_session", lambda c, m: "sid"):
            meeting._spawn_summary_waiter(mid, folder, "summary.md", "写纪要", "纪要")


class _GateClient:
    """纪要会话替身：`wait_for_reply` 卡在一个 Event 上，直到用例放行。

    这样"正在生成"那一刻是**确定性**的（不靠 sleep 猜窗口），而"超时/异常"两条路
    也能稳定复现（回空串 = 等回复超时；`prompt` 抛异常 = 会话坏了）。
    """

    def __init__(self, reply="", gate=None, prompt_boom=None):
        self.reply = reply
        self.gate = gate
        self.prompt_boom = prompt_boom

    def clear_stuck(self, sid):
        return None

    def prompt(self, sid, text, mode=""):
        if self.prompt_boom is not None:
            raise self.prompt_boom

    def wait_for_reply(self, sid, timeout=0, poll=0):
        if self.gate is not None:
            self.gate.wait(timeout=10)
        return (self.reply, True)


REAL_MINUTES = ("# 会议纪要\n\n## 一、背景\n\n"
                "本次会议确认了纪要状态字段的口径与落点，并核对了两条生成路径的收口纪律。\n\n"
                "## 二、结论\n\n纪要生成期间对外可读的状态是 running，成稿后是 ready。\n")


class SummaryStateTests(_SummaryCase):
    """`meeting.summary_state()`：三档各说各的实话，`summary_runs` 也**如实**。"""

    def test_nothing_running_and_nothing_written_is_none(self):
        """没任务、没成稿 → `none`（面板据此**立刻**说话，不许白等 30 秒）。"""
        mid, _folder = self._meeting()
        self.assertEqual("none", meeting.summary_state(mid, "2026-10-16_10-00-00"))

    def test_a_live_thread_reads_running_with_a_pending_run(self):
        """**核心**：派发了、还没成稿 → 状态 `running`，且库里那行是 **pending**。

        "pending" 这半边正是背景里那个 ✗：旧代码在派发之后立刻记 done，
        `summary_runs.status` 永远看不到 pending（"派发了但还没成稿"读不到）。
        """
        mid, folder = self._meeting()
        gate = threading.Event()
        client = _GateClient(reply=REAL_MINUTES, gate=gate)
        run_id = db.add_summary_run(mid, "")
        meeting._register_summary_run(mid, run_id)

        self._spawn_with(mid, folder, client)
        # 起线程那一帧就已经登记（不留"任务在跑、面板却说没有"的空档）
        self.assertEqual("running", meeting.summary_state(mid, "2026-10-16_10-00-00"))
        self.assertTrue(self._wait(lambda: self._run_status(mid) == "pending"),
                        "库里这行必须是 pending（旧代码在派发后立刻记 done：%r）"
                        % self._run_status(mid))
        gate.set()
        self.assertTrue(self._wait(lambda: meeting.summary_state(mid) == "ready"),
                        "纪要写出来了就该是 ready")
        # 收口在**写盘之后**的 finally 里（所以这里要等它一步，而不是当场断言）：
        # 两个信号之间那几毫秒里"成稿了、运行还写着 pending"是可以接受的（面板已经能用
        # 那份纪要了），**永远**停在 pending 才是错的。
        self.assertTrue(self._wait(lambda: self._run_status(mid) == "done"),
                        "成稿 → 运行应当收口成 done，实际：%r" % self._run_status(mid))
        self.assertTrue(os.path.isfile(os.path.join(folder, "summary.md")))

    def test_a_timeout_closes_the_run_instead_of_leaving_it_pending(self):
        """等回复超时（`reply` 为空）→ 运行收口成 `failed`，**不会**永远 pending。

        永远 pending 的后果很具体：库里/面板上永远写着"生成中"，而线程早就没了。
        """
        mid, folder = self._meeting()
        run_id = db.add_summary_run(mid, "")
        meeting._register_summary_run(mid, run_id)

        self._spawn_with(mid, folder, _GateClient(reply=""))
        self.assertTrue(self._wait(lambda: self._run_status(mid) == "failed"),
                        "超时那条路没把运行收口：%r" % self._run_status(mid))
        self.assertTrue(self._wait(lambda: meeting.summary_state(mid) == "none"),
                        "线程没了 → 状态必须回到 none（不能永远说「正在生成」）")

    def test_an_exception_in_the_writer_also_closes_it(self):
        """会话坏掉（`prompt` 抛异常）→ 同样收口（异常路径不许漏）。"""
        mid, folder = self._meeting()
        run_id = db.add_summary_run(mid, "")
        meeting._register_summary_run(mid, run_id)

        self._spawn_with(mid, folder, _GateClient(prompt_boom=RuntimeError("会话挂了")))
        self.assertTrue(self._wait(lambda: self._run_status(mid) == "failed"),
                        "异常路径没把运行收口：%r" % self._run_status(mid))
        self.assertTrue(self._wait(lambda: meeting.summary_state(mid) == "none"))

    def test_a_pending_row_alone_never_reads_as_running(self):
        """库里一条 pending **自己**不等于"在跑" —— 判据必须是"本进程真有线程"。

        这条挡的是"拿跨进程活下来的痕迹当现在的事实"：进程崩过之后那行还在，
        拿它当 running 就会让面板对着一个不存在的任务一直等下去。
        """
        mid, folder = self._meeting()
        db.add_summary_run(mid, "")
        self.assertEqual("none", meeting.summary_state(mid))
        # 成稿判据也不看库：写一份**只有空白**的 summary.md 不算 ready（与导出口径一致）
        with open(os.path.join(folder, "summary.md"), "w", encoding="utf-8") as fh:
            fh.write("   \n\t\n")
        self.assertEqual("none", meeting.summary_state(mid), "空白纪要 = 没成稿")

    def test_boot_recovery_closes_residue(self):
        """进程崩过留下的 pending：启动恢复把它收口（用户不会看到永远的"生成中"）。"""
        mid, _folder = self._meeting()
        run_id = db.add_summary_run(mid, "")
        self.assertEqual("pending", self._run_status(mid))

        meeting.recover_orphaned_meetings()

        self.assertEqual("failed", self._run_status(mid),
                         "启动恢复必须把残留的「生成中」收口（run=%s）" % run_id)
        self.assertEqual("none", meeting.summary_state(mid))

    def test_regenerate_no_longer_marks_the_run_done_immediately(self):
        """公共入口：`regenerate_summary()` 派发之后**不许**立刻记 done（原来那个 ✗）。

        没有任务在跑（派发被替身吞掉了）时，状态同时必须是 `none` —— 两件事一起钉：
        运行真的 pending（"派发了还没成稿"读得到），而"没有人跑"时状态也说没有。
        """
        mid, _folder = self._meeting()
        with patch.object(meeting, "request_summary", lambda *a, **k: True), \
                patch.object(meeting, "request_topic_segments", lambda *a, **k: True):
            ok, _msg = meeting.regenerate_summary(mid)
        self.assertTrue(ok)
        self.assertEqual("pending", self._run_status(mid),
                         "派发之后就该是 pending（旧代码这里永远是 done）")
        self.assertEqual("none", meeting.summary_state(mid),
                         "没有线程在跑 → 不许说 running（面板会白等）")

    def test_a_dispatch_that_never_happened_is_failed_not_pending(self):
        """压根没派发出去（两条路都不可用）→ 运行记 `failed`，别挂一条 pending 骗人。"""
        mid, _folder = self._meeting()
        with patch.object(meeting, "request_summary", lambda *a, **k: False), \
                patch.object(meeting, "request_topic_segments", lambda *a, **k: False):
            ok, _msg = meeting.regenerate_summary(mid)
        self.assertFalse(ok)
        self.assertEqual("failed", self._run_status(mid))


class SummaryStateOverHttpTests(_SummaryCase):
    """面板读的就是这条接口：`summaryState` 必须**跟着这次读**回到面板。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI                                        # noqa: E402
        from fastapi.testclient import TestClient                          # noqa: E402
        from app.api import router                                          # noqa: E402
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_no_job_no_summary(self):
        mid, _folder = self._meeting()
        r = self.client.get("/api/meetings/%d/file?kind=summary" % mid)
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({"exists": False, "content": "", "summaryState": "none"}, r.json())

    def test_a_running_job_says_running(self):
        """"有一个线程在为这一场写纪要" → 面板才敢说"正在生成"（判据来自服务端）。"""
        mid, _folder = self._meeting()
        meeting._begin_summarizing(mid)
        self.addCleanup(lambda: meeting._end_summarizing(mid))
        body = self.client.get("/api/meetings/%d/file?kind=summary" % mid).json()
        self.assertEqual("running", body["summaryState"], body)

    def test_a_written_summary_says_ready(self):
        mid, folder = self._meeting()
        with open(os.path.join(folder, "summary.md"), "w", encoding="utf-8") as fh:
            fh.write("# 纪要\n\n正文。\n")
        body = self.client.get("/api/meetings/%d/file?kind=summary" % mid).json()
        self.assertTrue(body["exists"])
        self.assertEqual("ready", body["summaryState"], body)

    def test_other_kinds_do_not_get_the_field(self):
        """转写/分段那两条没有这个字段（别给它们添噪声 —— 它只讲纪要那一件事）。"""
        mid, _folder = self._meeting()
        for kind in ("transcript", "topics"):
            with self.subTest(kind=kind):
                body = self.client.get("/api/meetings/%d/file?kind=%s" % (mid, kind)).json()
                self.assertNotIn("summaryState", body)


# ===========================================================================
# B② 面板：话术准确（在跑 / 没在跑）且**不**白等
# ===========================================================================

#: 导出那一段的替身：只替**页面之外**的东西（api / toast / DOM / 时钟）。
_EXPORT_STUBS = r"""
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
class Blob { constructor(parts) { LAST_HTML = parts.join(""); } }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function mdInline() { return ""; }
function mdToHtml() { return ""; }
function bindMermaidZoom() {}
function openMermaidZoom() {}
function fmtHM() { return "10 分钟"; }
function fmtTime() { return "00:01"; }
function spkColor() { return "#4f8cff"; }
function mergeTurns(lines) { return lines || []; }
async function fetch() { return { text: async () => "/*mermaid-stub*/" }; }

let M = null;
let API_CALLS = 0;
//: 每次问纪要时按剧本回答（数组用完后一直用 LAST_SUMMARY）。
//: `summaryState` 就是服务端 `meeting.summary_state()` 的判词（用例按需给）。
let SUMMARY_SCRIPT = [];
let LAST_SUMMARY = { exists: false, content: "", summaryState: "none" };
function scripted(v) {
  // 剧本项写成 "s:<state>" 或 "ready"
  if (v === "ready") return { exists: true, content: "# 会议纪要\n\n正文在此", summaryState: "ready" };
  return { exists: false, content: "", summaryState: String(v).replace(/^s:/, "") };
}
async function api(path) {
  API_CALLS += 1;
  if (path.indexOf("kind=summary") >= 0) {
    const next = SUMMARY_SCRIPT.length ? SUMMARY_SCRIPT.shift() : null;
    return next === null ? LAST_SUMMARY : scripted(next);
  }
  if (path.indexOf("kind=topics") >= 0) return { exists: true, content: '{"分段":[]}' };
  if (path.indexOf("kind=transcript") >= 0) return { exists: true, content: "转写正文" };
  throw new Error("用例没预期的接口：" + path);
}

// 时钟与定时器换成"立刻走完"的替身（真睡 30 秒就是真等 30 秒）。
// 判据读的就是 `Date.now()`：**没等待**时它一步都不该走。
let FAKE_NOW = 0;
let TIMER_CALLS = 0;
Date.now = () => FAKE_NOW;
setTimeout = (fn, ms) => {
  TIMER_CALLS += 1;
  if (TIMER_CALLS > 60) {
    throw new Error("定时器被排了 60 次以上 —— 等待逻辑没有上限？（用例主动炸掉，免得空转）");
  }
  FAKE_NOW += (ms || 0);
  fn();
};
"""


class ExportWaitingWordingTests(_NodeCase):
    """「⬇ 离线HTML」等纪要时说的话：任务在跑 → "正在生成"；没任务 → **一次都不等**。"""

    def _export_code(self):
        sec = self.src[self.src.index(EXPORT_SECTION):self.src.index(EXPORT_ENTRY_SIG)]
        entry = _js_function(self.src, EXPORT_ENTRY_SIG)
        body = _js_function(self.src, EXPORT_SIG)
        return sec + "\n" + entry + "\n" + body + "\n"

    def _click(self, meeting, summary_script=(), last_summary="s:none"):
        driver = (_EXPORT_STUBS + "\n" + self._export_code()
                  + "\n(async () => {\n"
                  + "  M = " + json.dumps(meeting, ensure_ascii=False) + ";\n"
                  + "  SUMMARY_SCRIPT = " + json.dumps(list(summary_script), ensure_ascii=False) + ";\n"
                  + "  LAST_SUMMARY = scripted(" + json.dumps(last_summary) + ");\n"
                  + "  await downloadOfflineHtml();\n"
                  + "  console.log(JSON.stringify({ clicks: CLICKS, downloaded: DOWNLOADED,\n"
                  + "     toasts: TOASTS, apiCalls: API_CALLS, fakeNow: FAKE_NOW,\n"
                  + "     timers: TIMER_CALLS, html: LAST_HTML, cap: EXPORT_SUMMARY_WAIT_MS,\n"
                  + "     poll: EXPORT_SUMMARY_POLL_MS }));\n"
                  + "})();\n")
        return self._run_node(driver)

    @staticmethod
    def _meeting(title="周例会"):
        return {"id": 7, "title": title, "started_at": "2026-10-16T11:37:25",
                "name": "2026-10-16_11-37-25", "speakers": [], "segments": [],
                "status": "transcribed", "duration_seconds": 600}

    def test_no_job_at_all_does_not_wait_a_single_second(self):
        """**核心判据**：压根没有在跑的生成任务 → 立刻如实说 + 给出下一步，**零等待**。

        这条正是"没有任务"与"任务在跑"必须分开的证明：旧代码在这里要白等 30 秒
        （`fakeNow` 会走到 30000、定时器被排 20 次），而那 30 秒等的是一个**不存在的任务**。
        """
        out = self._click(self._meeting(), summary_script=[], last_summary="s:none")
        self.assertEqual(0, out["timers"], "没有任务时**一次都不该**排定时器（那就是白等）")
        self.assertEqual(0, out["fakeNow"], "时钟一步都不该走：%s" % out["fakeNow"])
        self.assertEqual(0, out["clicks"], "没好就不许导出（空纪要导出去等于骗人）")
        last = out["toasts"][-1]
        self.assertIn("没有在跑的纪要生成任务", last)
        self.assertIn("还没发起过", last, "要说清「可能压根没发起过」这一种：%s" % last)
        self.assertIn("重新生成纪要", last, "要给出下一步（去哪发起）：%s" % last)
        self.assertIn("先不导出", last)

    def test_a_running_job_is_waited_for_with_accurate_wording(self):
        """有**真的**生成中状态 → 说的话是"纪要正在生成"，而不是含糊的"还没生成好"。"""
        out = self._click(self._meeting(title="Q3 复盘"), summary_script=[],
                          last_summary="s:running")
        self.assertTrue(any("纪要正在生成" in t for t in out["toasts"]),
                        "有 running 状态就该说「正在生成」：%s" % out["toasts"])
        self.assertTrue(any("正在生成，等它完成" in t for t in out["toasts"]))
        self.assertEqual(0, out["clicks"], "等满 30 秒还没好 → 仍然不导出")
        self.assertIn("还没生成好", out["toasts"][-1])
        self.assertIn("先不导出", out["toasts"][-1])
        # 有上限：等的是 30 秒那个窗口，不是无限轮询
        self.assertEqual(30000, out["cap"])
        self.assertLessEqual(out["fakeNow"], out["cap"] + 2 * out["poll"] + 1000)
        self.assertLessEqual(out["apiCalls"], out["cap"] // out["poll"] + 2)

    def test_it_exports_when_the_running_job_finishes(self):
        """在跑的任务后来成稿了 → 照样存下来（"等完成再存"这条口径不变）。"""
        out = self._click(self._meeting(title="周例会"),
                          summary_script=["s:running", "s:running", "ready"],
                          last_summary="s:running")
        self.assertEqual(1, out["clicks"], "成稿后就该真的存下来：%s" % out["toasts"])
        self.assertEqual("2026-10-16_周例会.html", out["downloaded"])
        self.assertIn("正文在此", out["html"])
        self.assertTrue(any("纪要正在生成" in t for t in out["toasts"]))

    def test_a_job_that_dies_mid_wait_stops_the_wait_immediately(self):
        """轮询中任务**收口**了（失败/异常）→ 立刻收手并如实说，不把 30 秒等满。

        "等一个已经结束的任务"和"等一个不存在的任务"是同一种骗人，只是晚一点发现。
        """
        out = self._click(self._meeting(), summary_script=["s:running", "s:none"],
                          last_summary="s:running")
        self.assertLess(out["fakeNow"], out["cap"],
                        "任务都没了还等满 30 秒？fakeNow=%s" % out["fakeNow"])
        last = out["toasts"][-1]
        self.assertIn("没有在跑的纪要生成任务", last, last)
        self.assertEqual(0, out["clicks"])

    def test_an_unknown_state_keeps_the_old_conservative_wording(self):
        """读不到状态（老服务端/替身）→ 退回**原来那句**含糊话，不替服务端断言"正在生成"。

        说错原因会把人引去等一个根本没在跑的任务 —— 宁可含糊，不可乱说。
        """
        out = self._click(self._meeting(), summary_script=[], last_summary="s:")
        self.assertTrue(any("正在等它完成" in t for t in out["toasts"]), out["toasts"])
        self.assertFalse(any("纪要正在生成" in t for t in out["toasts"]),
                         "读不到状态就**不许**说「正在生成」：%s" % out["toasts"])
        self.assertEqual(0, out["clicks"])

    def test_the_state_comes_from_the_server_not_from_the_panel(self):
        """源码一级：状态读的是服务端字段，面板不自己编一套判据。"""
        fn = _js_function(self.src, "async function fetchExportSummary()")
        self.assertIn("summaryState", fn, "状态要跟着读文件接口回来")
        wait = _js_function(self.src, "async function waitForExportSummary()")
        self.assertIn('first.state === "none"', wait, "没任务必须立刻返回（0 等待）")
        export = _js_function(self.src, EXPORT_SIG)
        self.assertIn('sumFile.state === "none"', export, "出口那句要按状态分开说")
        api_src = _read("app", "api.py")
        self.assertIn('"summaryState": meeting.summary_state(', api_src,
                      "服务端要真的下发这个字段（面板不能自己在编）")
        meeting_src = _read("app", "meeting.py")
        self.assertIn("def summary_state(", meeting_src,
                      "判据只有一处：meeting.summary_state()")
        self.assertIn("def _close_orphan_summary_runs(", meeting_src,
                      "残留的「生成中」必须有收口的地方")


if __name__ == "__main__":
    unittest.main()
