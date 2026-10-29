# -*- coding: utf-8 -*-
"""仪表盘「会议录音」那一块：状态行挪到按钮右侧、与录音时间上下并列（2026-10-19 用户口径）。

## 用户原话与判据

截图里这一块原来是**竖着**排的 —— 按钮下方单独压着一行「ⓘ 已确认：服务端在录音」：

    [ 停止录音 ]
    正在录音：2026-10-19_13-58-42
    ⓘ 已确认：服务端在录音          ← 在按钮下方（要挪走）

用户原话："**pc 端开始录音按钮下面有一行状态，这个挪到按钮右侧跟录音时间上下并列**"。
要改成：

    [ 停止录音 ]      正在录音：2026-10-19_13-58-42
                      ⓘ 已确认：服务端在录音        ← 同一列，时间下面

所以这一组钉四件事：

  1. 找得到那个"确认行"元素（`#meetingActionNote`），且它与录音时间（`#meetingInfo`）
     **在同一个容器里** —— 不是拆成两列；
  2. 它**不再是按钮的兄弟/下方**（不是 `.card-body` 的直接子节点，也不在按钮那一行里）；
  3. 宽屏结构就是「按钮 | 右侧一列（时间 + 确认行）」：列是按钮的**后一个兄弟**，
     列内顺序是 时间 → 确认行（上→下）；
  4. **这一列永远不换行**（2026-10-29 用户口径，见下）。

## 2026-10-29 追加（这次**推翻了**上面第 4 条的旧口径）

用户原话："**仪表盘页面开始录音按钮旁边展示的内容太臃肿了，需要精简，确保不占新行**"。

旧版第 4 条写的是"窄屏**整列**落到按钮下方"（靠 `.card-body.row` 的 `flex-wrap: wrap`）——
实际用起来正好是用户抱怨的样子：手机那场那句长文案一来，整列就掉到按钮下面、**占了新的一行**。
现在反过来钉住两件事：

  * **可见文案只留一句短语**（`refreshDashboard()`）：电脑在录 = `录音中 · HH:MM 开始`、
    手机那场 = `手机在录 · 传完才转写`、出错 = `上次出错`、空闲 = `未在录音`；
    完整信息（文件夹名 / "不是这台电脑" / 错误原话）一律进 `title`（悬停可见）。
  * **列本身不许换行**：`meetingSideColumn()` 给列 `flex: 1 1 0`（假想宽度 0 → `flex-wrap`
    永远不会把它挤到下一行），里面的文字 `nowrap + ellipsis`。这样以后文案再长也只省略，
    不会"把按钮那一行撑成两行"。

**不钉**：状态行的显示条件（`meetingNote()` 那套，一律不改）。

## 为什么"真跑 JS"而不是查字符串

这次要断言的恰恰是**父子/兄弟关系**（"是不是按钮的兄弟""是不是同一个容器"），
字符串在不在完全查不出这件事 —— 旧代码里 `insertAdjacentElement("afterend", note)`
贴的也是同一个 `#meetingActionNote`。所以把 `meetingSideColumn()` / `meetingNote()`
从 `web/app.js` **原样抠出来**交给 node 跑，配一个只够用的迷你 DOM（仓库没有 jsdom），
节点关系在一棵真树上算出来。没有 node 就**响亮失败**（与 `tests/test_js_syntax.py` /
`test_meeting_export_html.py` 同一条约定：静默跳过等于假装查过了）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 仪表盘「开始/停止录音」按钮与它右边那句「正在录音：<folder>」（`web/index.html` 原样）
SIDE_SIG = "function meetingSideColumn("
NOTE_SIG = "function meetingNote("
CONFIRM_TEXT = "已确认：服务端在录音"


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


#: 只够用的迷你 DOM：`meetingSideColumn()` / `meetingNote()` 用到的那几样全都有真实现
#: （父子、前一个兄弟、insertAdjacentElement、closest、contains、isConnected）。
_DOM_STUB = r"""
class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.id = "";
    this.className = "";
    this.children = [];
    this.parentElement = null;
    this._text = "";
    this._html = "";
    this.style = {};
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).indexOf(c) >= 0,
      add: (c) => { if (!self.classList.contains(c)) self.className = (self.className + " " + c).trim(); },
      remove: (c) => { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
    };
  }
  get isConnected() {
    let n = this;
    while (n) { if (n === document.body) return true; n = n.parentElement; }
    return false;
  }
  get previousElementSibling() {
    if (!this.parentElement) return null;
    const i = this.parentElement.children.indexOf(this);
    return i > 0 ? this.parentElement.children[i - 1] : null;
  }
  get nextElementSibling() {
    if (!this.parentElement) return null;
    const i = this.parentElement.children.indexOf(this);
    return (i >= 0 && i < this.parentElement.children.length - 1)
      ? this.parentElement.children[i + 1] : null;
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }
  appendChild(child) {
    if (child.parentElement) child.parentElement.removeChild(child);
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) { this.children.splice(i, 1); child.parentElement = null; }
    return child;
  }
  contains(node) {
    let n = node;
    while (n) { if (n === this) return true; n = n.parentElement; }
    return false;
  }
  closest(sel) {
    let n = this;
    while (n) {
      if (sel.charAt(0) === "." && n.classList.contains(sel.slice(1))) return n;
      if (sel.charAt(0) === "#" && n.id === sel.slice(1)) return n;
      n = n.parentElement;
    }
    return null;
  }
  insertAdjacentElement(pos, el) {
    if (pos === "beforebegin" || pos === "afterend") {
      const p = this.parentElement;
      if (!p) return null;
      if (el.parentElement) el.parentElement.removeChild(el);
      const i = p.children.indexOf(this);
      if (i < 0) return null;
      el.parentElement = p;
      p.children.splice(pos === "beforebegin" ? i : i + 1, 0, el);
      return el;
    }
    if (pos === "afterbegin") {
      if (el.parentElement) el.parentElement.removeChild(el);
      el.parentElement = this;
      this.children.unshift(el);
      return el;
    }
    return this.appendChild(el);
  }
}

function findById(node, id) {
  if (node.id === id) return node;
  for (const c of node.children) { const hit = findById(c, id); if (hit) return hit; }
  return null;
}
function findBySel(node, sel) {
  const id = sel.charAt(0) === "#" ? sel.slice(1) : null;
  const cls = sel.charAt(0) === "." ? sel.slice(1) : null;
  function walk(n) {
    if (id !== null && n.id === id) return n;
    if (cls !== null && n.classList.contains(cls)) return n;
    for (const c of n.children) { const hit = walk(c); if (hit) return hit; }
    return null;
  }
  return walk(node);
}
function countById(node, id) {
  let n = node.id === id ? 1 : 0;
  for (const c of node.children) n += countById(c, id);
  return n;
}

const document = {
  body: new El("body"),
  createElement: (tag) => new El(tag),
  getElementById: (id) => findById(document.body, id),
  querySelector: (sel) => findBySel(document.body, sel),
  querySelectorAll: () => [],
};
function $(sel) { return document.querySelector(sel); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
"""

#: 与 `web/index.html` 同形的骨架：仪表盘（按钮 + 时间）、会议历史的标题栏那颗按钮。
_FIXTURE = r"""
function mk(tag, cls, id, text) {
  const e = new El(tag);
  if (cls) e.className = cls;
  if (id) e.id = id;
  if (text != null) e.textContent = text;
  return e;
}

const dashboardCard = mk("div", "card span-2", "meetingCard");
const dashTitle = mk("div", "card-title", null, "🎙️ 会议录音");
const badge = mk("span", "badge", "meetingBadge", "空闲");
const micLevel = mk("div", "mic-level hidden", "micLevel");
const gotoMeetings = mk("a", "more", "gotoMeetings", "全部 ›");
for (const el of [badge, micLevel, gotoMeetings]) dashTitle.appendChild(el);
dashboardCard.appendChild(dashTitle);
const cardBody = mk("div", "card-body row");
const btnMeeting = mk("button", "btn big", "btnMeeting", "开始录音");
const info = mk("div", "muted", "meetingInfo", "未在录音");
cardBody.appendChild(btnMeeting);
cardBody.appendChild(info);
dashboardCard.appendChild(cardBody);
dashboardCard.appendChild(mk("div", "meeting-list", "recentMeetings"));
document.body.appendChild(dashboardCard);

const historyCard = mk("div", "card");
const title = mk("div", "card-title", null, "会议");
const btnList = mk("button", "btn", "btnMeetingFromList", "开始录音");
title.appendChild(btnList);
historyCard.appendChild(title);
document.body.appendChild(historyCard);

let RESULT = null;
function snap() {
  const note = document.getElementById("meetingActionNote");
  const col = info.parentElement;
  return {
    noteFound: !!note,
    noteHtml: note ? note.innerHTML : "",
    noteText: note ? note.innerHTML.replace(/<[^>]*>/g, "") : "",
    noteCls: note ? note.className : "",
    noteParentId: note && note.parentElement ? note.parentElement.id : null,
    notePrevId: note && note.previousElementSibling ? note.previousElementSibling.id : null,
    infoParentId: col ? col.id : null,
    infoNextId: info.nextElementSibling ? info.nextElementSibling.id : null,
    infoInCardBody: info.parentElement === cardBody,
    infoText: info.textContent,
    sameParent: !!(note && info && note.parentElement === info.parentElement),
    noteInCardBody: !!(note && note.parentElement === cardBody),
    noteIsChildOfCard: !!(note && note.parentElement === dashboardCard),
    noteUnderHistoryTitle: !!(note && note.parentElement === historyCard
                              && note.previousElementSibling === title),
    cardBodyKids: cardBody.children.map((c) => c.id || c.tagName.toLowerCase()),
    cardKids: dashboardCard.children.map((c) => c.id || c.tagName.toLowerCase()),
    titleKids: dashTitle.children.map((c) => c.id || c.tagName.toLowerCase()),
    btnText: btnMeeting.textContent,
    badgeText: badge.textContent,
    micHidden: micLevel.className.indexOf("hidden") >= 0,
    gotoText: gotoMeetings.textContent,
    colKids: col ? col.children.map((c) => c.id || c.tagName.toLowerCase()) : [],
    colPrevId: col && col.previousElementSibling ? col.previousElementSibling.id : null,
    colDisplay: col ? col.style.display : "",
    colDirection: col ? col.style.flexDirection : "",
    colFlex: col ? col.style.flex : "",
    infoWhiteSpace: info.style.whiteSpace,
    infoOverflow: info.style.overflow,
    infoTextOverflow: info.style.textOverflow,
    noteCount: countById(document.body, "meetingActionNote"),
    colCount: countById(document.body, "meetingSideCol"),
  };
}
"""


class _NodeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = _node()
        cls.js = _read("web", "app.js")
        cls.css = _read("web", "app.css")
        cls.html = _read("web", "index.html")
        cls.side = _js_function(cls.js, SIDE_SIG)
        cls.note = _js_function(cls.js, NOTE_SIG)

    def _run(self, body):
        if not self.node:
            self.fail("找不到 node —— 这一组用例没跑（这不等于通过）。"
                      "装一个 node，或把它放到 PATH / %LOCALAPPDATA%\\hermes\\node\\。")
        driver = (_DOM_STUB + "\n" + self.side + "\n" + self.note + "\n"
                  + _FIXTURE + "\n" + body + "\n"
                  + "console.log(JSON.stringify(RESULT));\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(driver)
            try:
                r = subprocess.run([self.node, path], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
            except subprocess.TimeoutExpired:
                self.fail("node 60 秒还没结束 —— 这段布局代码在空转/死循环")
            self.assertEqual(r.returncode, 0,
                             "node 跑页面真代码失败了：\n%s" % (r.stderr or "")[-3000:])
            return json.loads(r.stdout)


class MeetingPanelLayoutTests(_NodeCase):
    """宽屏：按钮 | 右侧一列（时间 → 确认行）。"""

    def test_the_confirmation_line_is_in_the_same_column_as_the_recording_time(self):
        """**核心判据**：确认行与「正在录音：<folder>」在同一个容器里、时间**下面**。"""
        out = self._run(
            'info.textContent = "正在录音：2026-10-19_13-58-42";\n'
            'meetingNote("%s", "info", btnMeeting);\n'
            'RESULT = snap();\n' % CONFIRM_TEXT)
        self.assertTrue(out["noteFound"], "找不到确认行元素 #meetingActionNote")
        self.assertIn(CONFIRM_TEXT, out["noteHtml"], "确认行的文案被改动了：%s" % out["noteHtml"])
        # ① 同一容器：不是拆成两列
        self.assertTrue(out["sameParent"],
                        "确认行与录音时间不在同一个容器里（拆成两列了？）：%r" % out)
        self.assertEqual("meetingSideCol", out["noteParentId"])
        self.assertEqual("meetingSideCol", out["infoParentId"])
        # ② 上下并列：时间在上、确认行在它下一位
        self.assertEqual("meetingActionNote", out["infoNextId"],
                         "确认行不在录音时间**下面**（同一列的下一行）：%r" % out)
        self.assertEqual("meetingInfo", out["notePrevId"])
        self.assertEqual(["meetingInfo", "meetingActionNote"], out["colKids"],
                         "这一列里应当就是「时间 → 确认行」两行：%r" % out["colKids"])
        # ③ 不再压在按钮下方 / 不再是按钮那一行的兄弟
        self.assertFalse(out["noteInCardBody"], "确认行仍然是按钮那一行的直接子节点")
        self.assertFalse(out["noteIsChildOfCard"], "确认行仍然贴在按钮那一块**下方**")
        # ④ 宽屏结构：按钮 | 右侧一列（列紧跟在按钮后面）
        self.assertEqual(["btnMeeting", "meetingSideCol"], out["cardBodyKids"],
                         "按钮那一行里应当是「按钮 + 右侧一列」：%r" % out["cardBodyKids"])
        self.assertEqual("btnMeeting", out["colPrevId"], "那一列不在按钮右侧")
        self.assertEqual("flex", out["colDisplay"])
        self.assertEqual("column", out["colDirection"],
                         "这一列必须**竖排**（时间与确认行上下并列，不是并排两列）")
        # ⑤ 时间那句文案一个字没动
        self.assertIn("正在录音：2026-10-19_13-58-42", out["infoText"])

    def test_the_other_elements_of_the_block_are_untouched(self):
        """别动这一块的其它元素：标题/徽章/电平/「全部」链接都还在，按钮文案不变。"""
        out = self._run(
            'info.textContent = "正在录音：2026-10-19_13-58-42";\n'
            'meetingNote("%s", "info", btnMeeting);\n'
            'RESULT = snap();\n' % CONFIRM_TEXT)
        self.assertEqual("开始录音", out["btnText"], "按钮文案被这次调整改动了")
        self.assertEqual("空闲", out["badgeText"])
        self.assertTrue(out["micHidden"], "电平的默认隐藏状态被动了")
        self.assertEqual("全部 ›", out["gotoText"])
        self.assertEqual(["meetingBadge", "micLevel", "gotoMeetings"], out["titleKids"],
                         "标题行的三个元素（徽章 / 电平 / 全部）被动过")
        self.assertEqual(["div", "div", "recentMeetings"], out["cardKids"],
                         "会议卡的三块（标题 / 控制行 / 近期会议）被动过")
        self.assertEqual(["btnMeeting", "meetingSideCol"], out["cardBodyKids"],
                         "按钮那一行的成员被动了（除「按钮 + 右侧一列」之外不该多/少）")

    def test_repeated_updates_do_not_duplicate_or_move_it(self):
        """点几次/状态刷新几次：列只建一次、确认行只一个，且始终在时间下面。

        这条防的是"每次刷新都搬一次节点"那种抖动（`meetingNote` 每 5 秒的轮询里会被调）。
        """
        out = self._run(
            'info.textContent = "正在录音：2026-10-19_13-58-42";\n'
            'meetingNote("正在停止录音，等 ECHO 回应…", "info", btnMeeting);\n'
            'meetingNote("已取消，仍在录音", "warn", btnMeeting);\n'
            'meetingNote("%s", "info", btnMeeting);\n'
            'RESULT = snap();\n' % CONFIRM_TEXT)
        self.assertEqual(1, out["noteCount"], "确认行被复制出了 %d 个" % out["noteCount"])
        self.assertEqual(1, out["colCount"], "右侧那一列被建了 %d 个" % out["colCount"])
        self.assertEqual(["meetingInfo", "meetingActionNote"], out["colKids"])
        self.assertEqual("meetingActionNote", out["infoNextId"])
        self.assertIn(CONFIRM_TEXT, out["noteHtml"])

    def test_building_the_column_twice_keeps_exactly_one_column(self):
        """`meetingSideColumn()` 是幂等的（首屏调用 + 每次 `meetingNote` 都会走到它）。"""
        out = self._run(
            'meetingSideColumn(btnMeeting);\n'
            'meetingSideColumn(btnMeeting);\n'
            'RESULT = snap();\n')
        self.assertEqual(1, out["colCount"], "同一块里建出了多列")
        self.assertEqual(["btnMeeting", "meetingSideCol"], out["cardBodyKids"])
        self.assertEqual(["meetingInfo"], out["colKids"], "时间应当被挪进这一列（只挪一次）")

    def test_the_column_can_never_be_pushed_to_a_new_line(self):
        """2026-10-29 用户："**确保不占新行**" —— 列的假想宽度必须是 0（`flex: 1 1 0`）。

        只把文案写短是"碰巧放得下"；`flex-wrap` 是照**假想宽度**决定换行的，`auto` 时它
        等于文字最大宽度 —— 长一点整列就掉到按钮下方（2026-10-29 真机现场就是这样）。
        所以这里钉的是**样式**：`flex: 1 1 0`（假想宽度 0 → 永不换行）+ 文字单行省略。
        """
        out = self._run(
            'info.textContent = "手机在录 · 传完才转写";\n'
            'meetingSideColumn(btnMeeting);\n'
            'RESULT = snap();\n')
        self.assertEqual("1 1 0", out["colFlex"],
                         "那一列没拿到 `flex: 1 1 0`：flex-wrap 会把它挤到按钮下面（占新行）")
        self.assertEqual("nowrap", out["infoWhiteSpace"],
                         "状态句必须单行：允许换行就会把按钮那一行撑高")
        self.assertEqual("hidden", out["infoOverflow"],
                         "没有 overflow:hidden 时省略号不生效，长文案照样撑宽")
        self.assertEqual("ellipsis", out["infoTextOverflow"],
                         "放不下要用省略号（完整内容在 title 里）")


class MeetingListButtonTests(_NodeCase):
    """会议列表标题栏那颗按钮旁边没有「这场录音的时间」→ 状态行仍贴标题行下面（不改旧行为）。"""

    def test_the_list_line_stays_under_the_header_and_does_not_steal_the_dashboards_time(self):
        out = self._run(
            'meetingNote("已取消，仍在录音", "warn", btnList);\n'
            'RESULT = snap();\n')
        self.assertTrue(out["noteFound"])
        self.assertTrue(out["noteUnderHistoryTitle"],
                        "会议列表那颗按钮的状态行不在标题行下面：%r" % out)
        self.assertFalse(out["sameParent"], "列表那颗按钮旁边没有时间，不该硬塞进同一列")
        self.assertTrue(out["infoInCardBody"],
                        "仪表盘那句「录音中 · <开始时间>」被列表那条挪走了")


class MeetingPanelSourceTests(unittest.TestCase):
    """源码一级：判据只有一处、文案精简、列不换行（2026-10-29 口径）。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")
        cls.css = _read("web", "app.css")
        cls.html = _read("web", "index.html")

    def test_the_confirm_sentence_is_written_once_and_unchanged(self):
        """「已确认：服务端在录音」只该有一处，且**逐字**没被缩短/改写（用户明确要求）。"""
        self.assertEqual(1, self.js.count('"%s"' % CONFIRM_TEXT),
                         "这句文案要么被改了、要么被复制了一份")
        verify = _js_function(self.js, "async function verifyMeetingState(")
        self.assertIn('"%s"' % CONFIRM_TEXT, verify)
        self.assertIn('meetingNote(done, "info", btn)', verify,
                      "显示条件/通道（info）不许被这次摆放调整改掉")

    def test_the_state_line_follows_the_recording_information(self):
        """摆放的判据只有一处：有「这场录音的时间」才进那一列（源码可读的意图）。"""
        note = _js_function(self.js, NOTE_SIG)
        self.assertIn("meetingSideColumn(anchor)", note,
                      "状态行的落点必须由这一处决定（别再在别处自己 insert）")
        self.assertIn('info.insertAdjacentElement("afterend", note)', note,
                      "进那一列时，它必须排在录音时间**后面**（同一列、时间下面）")
        side = _js_function(self.js, SIDE_SIG)
        self.assertIn('anchor.insertAdjacentElement("afterend", col)', side,
                      "那一列要固定在按钮右侧")
        self.assertIn("col.appendChild(info)", side, "时间要挪进这一列（不是复制一份）")
        self.assertIn("flexDirection = \"column\"", side, "这一列必须竖排")

    def test_the_column_is_pinned_to_the_buttons_line(self):
        """2026-10-29："不占新行" 的判据只能在 `meetingSideColumn()` 里（别散到别处）。"""
        side = _js_function(self.js, SIDE_SIG)
        self.assertIn('col.style.flex = "1 1 0"', side,
                      "列必须 flex-basis 0（假想宽度 0）—— 否则 flex-wrap 会把它挤到下一行")
        self.assertIn('info.style.whiteSpace = "nowrap"', side, "状态句必须单行")
        self.assertIn('info.style.textOverflow = "ellipsis"', side,
                      "放不下要省略号（完整内容在 title 里，别把按钮行撑高）")

    def test_the_visible_line_is_one_short_phrase_and_the_detail_is_in_the_title(self):
        """2026-10-29：按钮旁那句**精简成一句短语**，完整信息进 `title`（悬停可见）。"""
        dash = _js_function(self.js, "async function refreshDashboard(")
        # 手机那场：可见 = 一句短语；"不是这台电脑"的完整说明在 title
        self.assertIn('infoEl.textContent = "手机在录 · 传完才转写"', dash,
                      "手机那场那句太长了（用户 2026-10-29：太臃肿、不许占新行）")
        self.assertIn("手机正在录音（不是这台电脑）", dash,
                      "精简的是可见文字，完整原因仍要在 title 里说清")
        # 电脑在录：可见 = 开始时间（2026-10-19 要的"录音时间"），文件夹名进 title
        self.assertIn("录音中 · ${at} 开始", dash,
                      "电脑在录那句要精简成「录音中 · HH:MM 开始」（别把文件夹名摆出来）")
        self.assertIn("infoEl.title = `正在录音：", dash)
        # 出错：可见只留短标记，原话在 title
        self.assertIn('infoEl.textContent = "上次出错"', dash)
        self.assertIn("infoEl.title = `上次错误：", dash)

    def test_index_html_is_still_the_static_skeleton(self):
        """这次的改动只在 JS 里：静态骨架仍是「按钮 + 时间」两样，行/列都靠 JS 建。"""
        self.assertNotIn("meetingActionNote", self.html)
        self.assertNotIn("meetingSideCol", self.html)
        self.assertEqual(1, self.html.count('id="meetingInfo"'))
        self.assertEqual(1, self.html.count('id="btnMeeting"'))


if __name__ == "__main__":
    unittest.main()
