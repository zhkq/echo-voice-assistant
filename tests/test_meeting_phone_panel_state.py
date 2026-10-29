# -*- coding: utf-8 -*-
"""**「录音中」不等于「这台电脑在录」** —— 手机麦会议期间电脑面板的状态（2026-10-17 真机）。

用户原话：

    电脑端接受上传期间看到了一条会议显示**录音中**，然后**开始录音的按钮也变成录制中**了。

现场是真的：手机按段上传期间，服务端 `meeting.active` **确实是 true** —— 那是"这场会
还开着，等手机把段传完"，是对的。错的是**电脑面板**把它读成"这台电脑在录音"：
麦克风电平、徽章、"停止录音"按钮全都照着 `active` 翻，可这台电脑**一支麦都没开**。

本文件钉两件事：

  1. **服务端**：`meeting_status()["source"]` 是面板的唯一判据，所以它必须**逐场重设** ——
     上一场是手机麦（`source=phone`）时，**紧接着电脑自己录的那一场**不许继续报 `phone`
     （真机上这就是"电脑在录、面板却把它显示成手机在录、连停都停不了"）；
  2. **面板**（`web/app.js`，源码级判据，本仓库一贯做法）：
     * `phone + active` → 徽章"手机录音中"、说明"手机正在录音（不是这台电脑）"、
       **不显示**麦克风电平、电脑那颗「开始录音」**置灰**并在 `title` 里说清"只有一条录音槽，
       等手机结束/传完才能开新的一场"（服务端那条拒绝仍然在，是双保险）；
       *2026-10-29 起*：按钮旁**可见**的那句精简成「手机在录 · 传完才转写」（用户："太臃肿了，
       需要精简，确保不占新行"），完整说明仍在 `title` 里 —— 判据没变，只是不再铺在界面上；
     * `pc + active` → 与以前**逐字相同**：按钮「停止录音」+ 危险色。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_phone_start_is_phone_source import CID, _Base       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class SourceIsResetForTheNextMeetingTests(_Base):
    """服务端：`source` 是**每一场**的采集源，不许从上一场粘过来。"""

    def test_a_pc_meeting_after_a_phone_meeting_reports_source_pc(self):
        """先手机麦录一场并收尾，再在电脑上开一场 → 第二场必须报 `source=pc`。

        真机现场（2026-10-17 晚）：用户的手机麦会议结束后，面板上下一场电脑录的会仍然
        显示"手机正在录音（不是这台电脑）"，于是「开始录音」被置灰、想停也点不了 ——
        根因是 `_state["source"]` 只在 `start_meeting_phone()` 里被设成 `phone`，
        `start_meeting()`（电脑麦）**从没把它写回来**，内存状态跨会议复用就粘住了。
        """
        # ① 手机麦那场：建会 → 传一段 → 收尾
        phone = self._start(source="phone", client_id=CID)
        self.assertTrue(phone["ok"], phone)
        self.assertEqual("phone", self._status()["source"])
        self.assertEqual(200, self._segment(1, client_id=CID).status_code)
        r = self.client.post("/api/meeting/stop", params={"clientId": CID},
                             headers=self._headers())
        self.assertEqual(200, r.status_code, r.text)
        self.assertTrue(r.json()["ok"], r.json())
        self.assertFalse(self._status()["active"], "收尾之后不该还在录")

        # ② 电脑自己录的那一场（面板那条路：不带中继标记、不带 source）
        # 会议目录名按**秒**命名（`%Y-%m-%d_%H-%M-%S`），两场挨太近会撞 `UNIQUE(meetings.name)`
        # —— 那是用例的时序假象，不是被测行为：跨过一秒再开第二场。
        time.sleep(1.1)
        pc = self._start_raw({}, relay=False)
        self.assertEqual("pc", pc["source"], "面板开的那场用的就是电脑的麦")
        self.assertEqual(1, len(self.recorder_calls), "电脑那条路才该真的开麦")
        st = self._status()
        self.assertTrue(st["active"])
        self.assertEqual("pc", st["source"],
                         "电脑**正在录**却报 source=phone：面板会显示成'手机在录'并把"
                         "「开始录音」置灰（真机上就是这一条把用户卡住的）")

    def test_a_phone_meeting_reports_phone_while_it_is_still_uploading(self):
        """反向的一半：手机麦那场（还在等上传）必须报 `phone` —— 别把这条判据修没了。"""
        self._start(source="phone", client_id=CID)
        st = self._status()
        self.assertTrue(st["active"], "上传期间这场会就是 active（那是**正常**的）")
        self.assertEqual("phone", st["source"])


class PanelDoesNotPretendTheComputerIsRecordingTests(unittest.TestCase):
    """面板（`web/app.js`）：手机麦那一场要显示"手机在录"，而且不许动电脑那颗按钮。"""

    @classmethod
    def setUpClass(cls):
        cls.js = _read("web", "app.js")

    @staticmethod
    def _js_function(src, sig):
        """从 `sig` 起、到下一个**顶格** `}` 为止（与 `tests/test_ia_panel.py` 同一写法）。"""
        i = src.index(sig)
        return src[i:src.index("\n}", i) + 2]

    # ---------------------------------------------------------------- ① 判据只看 source
    def test_the_judgement_is_meeting_source_not_active_alone(self):
        """`isPhoneMicMeeting` 必须同时看 `active` 与 `source == "phone"`。"""
        fn = self._js_function(self.js, "function isPhoneMicMeeting(")
        self.assertIn("m.active", fn, "不 active 就不是'正在录'")
        self.assertIn('String(m.source || "pc").toLowerCase() === "phone"', fn,
                      "判据是服务端 meeting_status().source，不是本地猜")
        # 缺 source 的老服务端 → 按 pc 处理（行为与以前一致，不许因此把按钮置灰）
        self.assertIn('|| "pc"', fn)

    def test_the_hint_says_why_and_what_to_do(self):
        """置灰时那句话要**说清原因与出路**（"等手机结束/传完" + "只有一条录音槽"）。"""
        i = self.js.index("const PHONE_MIC_BUTTON_TITLE =")
        hint = self.js[i:self.js.index(";", i) + 1]
        self.assertIn("不是这台电脑", hint)
        self.assertIn("一条录音槽", hint, "服务端只有一条录音槽 —— 这是置灰的理由")
        self.assertIn("等手机结束/传完", hint, "要给出下一步（什么时候才能开新的）")

    # ---------------------------------------------------------------- ② 仪表盘
    def test_the_dashboard_badge_says_phone_not_pc(self):
        dash = self._js_function(self.js, "async function refreshDashboard(")
        self.assertIn("const phoneMic = isPhoneMicMeeting(meeting);", dash)
        self.assertIn("const pcRecording = !!(meeting.active && !phoneMic);", dash)
        self.assertIn('"手机录音中"', dash, "徽章要分开说：手机在录，不是这台电脑")
        # 2026-10-29：按钮旁那句**精简成一句短语**（用户："太臃肿了…确保不占新行"）；
        # 完整说明（"不是这台电脑" / 文件夹名 / 传完才转写）进 `title`，悬停可见。
        self.assertIn('infoEl.textContent = "手机在录 · 传完才转写"', dash,
                      "按钮旁那句要精简（2026-10-29 用户要求），不能是原来那串长话")
        self.assertIn("手机正在录音（不是这台电脑）", dash,
                      "精简的是**可见文字**；完整原因仍要在 title 里说清")
        # `active` 不许再单独决定"录音中"（那正是这次的面板状态错）
        self.assertNotIn('mb.textContent = meeting.active ? "录音中" : "空闲";', dash)

    def test_the_pc_start_button_never_becomes_stop_while_the_phone_records(self):
        """手机麦期间：按钮文案仍是「开始录音」、**置灰** + `title` 说明原因。"""
        dash = self._js_function(self.js, "async function refreshDashboard(")
        self.assertIn('$("#btnMeeting").textContent = pcRecording ? "停止录音" : "开始录音";',
                      dash, "只有**电脑自己在录**才该是「停止录音」")
        self.assertIn('$("#btnMeeting").disabled = phoneMic;', dash,
                      "手机在录时电脑那颗按钮必须置灰（点了也只会被服务端拒）")
        self.assertIn('$("#btnMeeting").title = phoneMic ? PHONE_MIC_BUTTON_TITLE', dash)

    def test_the_pc_recording_case_is_unchanged(self):
        """电脑**自己**在录（source=pc）时照旧：按钮=「停止录音」+ 危险色，且不置灰。"""
        dash = self._js_function(self.js, "async function refreshDashboard(")
        self.assertIn('const pcRecording = !!(meeting.active && !phoneMic);', dash)
        self.assertIn('$("#btnMeeting").className = "btn big " + (pcRecording ? "danger" : "");',
                      dash)
        self.assertIn('$("#btnMeeting").disabled = phoneMic;', dash,
                      "pc 那一场 phoneMic 恒为 false → 不会误置灰")

    def test_the_level_meter_follows_the_computers_own_microphone(self):
        """手机那一场没有本地录音流：电平条不许显示（否则就是"电脑在录"的第二个假象）。"""
        dash = self._js_function(self.js, "async function refreshDashboard(")
        self.assertIn('micLevel.classList.toggle("hidden", !pcRecording)', dash)
        self.assertIn("if (pcRecording || listening) {", dash)
        self.assertNotIn('micLevel.classList.toggle("hidden", !st.meeting.active)', dash)

    def test_the_topbar_status_also_distinguishes_phone(self):
        fn = self._js_function(self.js, "function liveState(")
        self.assertIn("isPhoneMicMeeting(st.meeting)", fn)
        self.assertIn("手机录音中", fn)
        self.assertIn("手机正在录音（不是这台电脑）", fn)

    # ---------------------------------------------------------------- ③ 会议列表那颗按钮
    def test_the_list_button_uses_the_same_one_judgement(self):
        """两处入口共用同一个判据（两套逻辑迟早漂 —— 与「点了永远看得出来」同一条纪律）。"""
        fn = self._js_function(self.js, "function renderMeetingListBtn(")
        self.assertIn("isPhoneMicMeeting({ active: active, source: source })", fn)
        self.assertIn("btn.disabled = phoneMic;", fn)
        self.assertIn('btn.textContent = pcRecording ? "停止录音" : "开始录音";', fn)
        # `source` 必须从状态接口传进来（只传 active 就会画错）
        hdr = self._js_function(self.js, "async function refreshMeetingHeader(")
        self.assertIn("renderMeetingListBtn(!!st.active, st.source);", hdr)


if __name__ == "__main__":
    unittest.main()
