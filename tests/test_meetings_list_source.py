# -*- coding: utf-8 -*-
"""`GET /api/meetings` **每一行都要带 `source`** —— 列表与仪表盘不许各说一套。

2026-10-17 真机（用户的原始投诉，两句话其实是一件事）：

    电脑端接受上传期间看到了一条会议显示**录音中** …
    我用手机发起了一次会议录音，**电脑端也变成录音状态了**。

仪表盘那条路已经修好了（`web/app.js::isPhoneMicMeeting` 读
`meeting_status().source`，手机麦那场说「手机录音中」）。但**会议列表的每一行**当时
没有 `source` 可读 —— 它只会照 `status === "recording"` 画「录音中」。于是**同一场会议**
在仪表盘上是"手机在录"、在列表里是"这台电脑在录"，而这台电脑**一支麦都没开**。
用户看到的正是这个不一致（他会照着列表去点「停止录音」，那一步会把手机那场停掉）。

所以本文件钉两条契约：

  1. **服务端**：`/api/meetings` 的每一行都有 `source`，取值只有 `"phone"` / `"pc"`；
     取不到 / 认不出（老会议没有 `meta.json`、导入那场写的是 `source=import`、
     键是空串）一律 `"pc"` —— 保守值宁可少说"手机在录"，不能凭空说。既有键一个不少。
  2. **面板**：列表行**复用** `isPhoneMicMeeting` 判据（不写第二套）：手机那场显示
     「手机录音中」且**不是**「录音中」/「停止录音」；电脑那场（含没有 `source` 的老数据）
     与补这一格之前**逐字一样**。

测试卫生：临时库 + 临时会议目录 + 声卡/转写全部打桩（复用
`tests/test_phone_start_is_phone_source.py` 的 `_Base`），**绝不碰**真实库 / 真实会议目录 /
真实麦克风；面板那一半把 `web/app.js` 的会议列表**整段抽出来交给 node 真跑**
（与 `tests/test_waiting_reason_ui.py` 同一套做法 —— 只查源码文本是查不出"渲染出来是什么"的）。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                               # noqa: E402
from tests.test_phone_start_is_phone_source import _Base        # noqa: E402
from tests.test_waiting_reason_ui import (_APP_STUBS,           # noqa: E402
                                          APP_REGION_END, APP_REGION_START,
                                          _NodeCase)

#: `/api/meetings` 每一行**必须都还在**的键（既有契约，一个都不许改名/删）。
#:
#: 前一段是 `db.list_meetings()` 原样带出来的 `meetings` 表列（`app/db.py` 建表 +
#: 迁移 6 的 `error`），后一段是列表接口自己在循环里补的展示字段（2026-09-10 ~ 2026-09-28）。
#: `tests/test_api_contract.py` 逐条钉着其中几个的**语义**，这里钉的是"一个都没少"。
EXISTING_ROW_KEYS = {
    # 库里的列（一列都不能少：面板/技能按名字读）
    "id", "name", "title", "started_at", "ended_at", "duration_seconds",
    "status", "stt_model", "stt_device", "diarize", "segments", "audio_bytes",
    "notes", "created_at", "error",
    # 列表接口补的展示字段
    "has_summary", "compression", "speakerNames", "transcribing", "timestamps",
}


class MeetingsListSourceTests(_Base):
    """服务端：每一行的 `source` 就是**那一场**的采集源。"""

    # ------------------------------------------------------------ 小工具

    def _list(self):
        r = self.client.get("/api/meetings", headers=self._headers(relay=False))
        self.assertEqual(200, r.status_code, r.text)
        return r.json()["items"]

    def _row(self, mid):
        rows = [it for it in self._list() if it["id"] == mid]
        self.assertEqual(1, len(rows), "列表里找不到 id=%r 那一行" % (mid,))
        return rows[0]

    def _mid_of(self, name):
        m = db.get_meeting_by_name(name)
        self.assertIsNotNone(m, "`start` 回的那个名字在库里找不到：%r" % (name,))
        return m["id"]

    def _fake_meeting(self, name, meta=None):
        """造一场"盘上已经有会议目录"的会议（不经录音，也不碰声卡）。"""
        mid = db.create_meeting(name, started_at="2026-10-01T09:00:00")
        folder = os.path.join(self.tmp, name)
        os.makedirs(folder, exist_ok=True)
        if meta is not None:
            with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
        return mid

    # ------------------------------------------------------------ ① 手机麦

    def test_a_phone_mic_meeting_row_says_phone(self):
        """手机那条路（`source=phone`，电脑不开麦）→ 这一行必须是 `"phone"`。

        这正是真机上"列表说电脑在录"的那一场：服务端 `active` 是真的（等手机传完段），
        所以 `status` 仍是 `recording` —— 面板要靠 `source` 才分得清是谁在录。
        """
        r = self._start(source="phone")                    # 手机麦克风那条路
        self.assertEqual("phone", r["source"])
        mid = self._mid_of(r["message"])
        row = self._row(mid)
        self.assertEqual("phone", row["source"],
                         "手机麦那一场在列表里必须是 phone，否则它会被画成"
                         "「录音中」/「停止录音」：%s" % sorted(row))
        self.assertEqual("recording", row["status"], "这场会还开着（等手机传段）")

    # ------------------------------------------------------------ ② 电脑麦

    def test_a_pc_mic_meeting_row_says_pc(self):
        """本机面板那条老路（不带 source、不带中继标记）→ `"pc"`，回包也逐字不变。"""
        r = self._start_raw({}, relay=False)
        self.assertEqual("pc", r["source"])
        row = self._row(self._mid_of(r["message"]))
        self.assertEqual("pc", row["source"],
                         "电脑自己录的那场绝不能报 phone：面板会把它的按钮置灰、"
                         "用户连停都停不了：%s" % sorted(row))

    # ------------------------------------------------------------ ③ 取不到 / 认不出

    def test_an_old_meeting_without_a_meta_file_says_pc(self):
        """老会议（目录在、`meta.json` 没有）→ `"pc"`（行为与补这一格之前一样）。"""
        mid = self._fake_meeting("2026-10-01_10-00-00", meta=None)
        db.update_meeting(mid, status="transcribed", segments=2, duration_seconds=120)
        self.assertEqual("pc", self._row(mid)["source"])

    def test_a_meta_without_source_or_with_a_non_phone_value_says_pc(self):
        """`meta` 里没有 `source` / 空串 / 认不出的值 → 一律 `"pc"`。

        * 本机麦那场的 `meta` **本来就没有** `source` 这个键（`start_meeting` 只写 config 等）；
        * 导入那场写的是 `source="import"`（`meeting.import_meeting`）—— 它不是"手机麦"，
          但也绝不是 `phone`，所以按 `pc` 这条保守分支走。
        """
        cases = {
            "2026-10-01_11-00-00": {},                       # 本机麦会议的真实形状
            "2026-10-01_12-00-00": {"source": ""},           # 空串 = 取不到
            "2026-10-01_13-00-00": {"source": "import"},     # 导入那场（不是手机麦）
            "2026-10-01_14-00-00": {"source": "PC"},         # 大小写不敏感，仍是电脑
        }
        for name, meta in cases.items():
            with self.subTest(meta=meta):
                mid = self._fake_meeting(name, meta=meta)
                db.update_meeting(mid, status="transcribed", segments=1)
                self.assertEqual("pc", self._row(mid)["source"])

    # ------------------------------------------------------------ ④ 既有键一个不少

    def test_the_row_keeps_every_existing_key(self):
        """`source` 是**新增**的一格：既有键一个都不许改名/删（面板与技能按名字读）。"""
        mid = self._fake_meeting("2026-10-01_15-00-00",
                                 meta={"timestampsKinds": {"estimated": 2}})
        db.update_meeting(mid, status="transcribed", segments=1, duration_seconds=60)
        row = self._row(mid)
        missing = EXISTING_ROW_KEYS - set(row)
        self.assertEqual(set(), missing,
                         "这几个既有键被删/改名了：%s" % sorted(missing))
        # 新键的形状也要钉住：只有两个取值，**不许**留 None 或空串让面板自己猜
        self.assertIn(row["source"], ("phone", "pc"), row["source"])
        # 顺带确认几格既有字段的语义没被这次改动挤掉（与 test_api_contract 对齐）
        self.assertEqual([], row["speakerNames"], "没标过说话人就是空数组（不是 None）")
        self.assertIsNone(row["compression"], "没压过就是 None（面板据此不显示标记）")
        self.assertEqual({"estimated": 2}, row["timestamps"]["kinds"])

    def test_every_row_of_the_page_has_a_source(self):
        """整页**每一行**都要有 `source`（不是只有某一场）—— 面板是按行渲染的。"""
        self._fake_meeting("2026-10-01_16-00-00", meta={"source": "phone"})
        self._fake_meeting("2026-10-01_17-00-00", meta={})
        rows = self._list()
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            with self.subTest(name=row.get("name")):
                self.assertIn("source", row)
                self.assertIn(row["source"], ("phone", "pc"))


class MeetingsListRowPanelTests(_NodeCase):
    """面板：列表行真的按 `source` 分开说（把 `web/app.js` 那一段交给 node 真跑）。"""

    def _render(self, cases):
        region = self.app_js[self.app_js.index(APP_REGION_START):
                            self.app_js.index(APP_REGION_END)]
        driver = (_APP_STUBS + "\n" + region + "\n"
                  + "const CASES = " + json.dumps(cases, ensure_ascii=False) + ";\n"
                  + "const el = { innerHTML: '' };\n"
                  + "const out = [];\n"
                  + "for (const c of CASES) { el.innerHTML = '';"
                  + " renderMeetingItems(el, [c], {}); out.push(el.innerHTML); }\n"
                  + "console.log(JSON.stringify(out));\n")
        return self._run_node(driver)

    @staticmethod
    def _m(status, source=None, **extra):
        row = {"id": 7, "name": "2026-10-17_13-18-27", "title": "周例会",
               "started_at": "2026-10-17T13:18:27", "duration_seconds": 600,
               "segments": 0, "status": status}
        if source is not None:
            row["source"] = source
        row.update(extra)
        return row

    def test_a_phone_row_says_phone_mic_instead_of_recording(self):
        """手机那一场：徽标「手机录音中」，**不是**「录音中」/「停止录音」。

        「没有停止按钮」这条在**列表这一行**上就是"行里不许出现停止入口"（停止按钮只在
        列表头那颗 `#btnMeetingFromList` 上，它读 `/api/meeting/status`、已由
        `tests/test_meeting_phone_panel_state.py` 钉着）。这一行要是照 `status` 画成
        「录音中」，用户就会去点那颗按钮 —— 那一停停的是手机那场会。
        """
        html = self._render([self._m("recording", "phone")])[0]
        self.assertIn(">手机录音中</span>", html,
                      "手机麦那一场要分开说：%s" % html)
        self.assertNotIn(">录音中</span>", html, "不许再说成这台电脑在录")
        self.assertNotIn("停止录音", html, "列表行里不该给这一场一个「停止录音」入口")
        self.assertIn("不是这台电脑", html, "徽标的 title 要说清是谁在录")

    def test_a_pc_row_is_byte_for_byte_the_old_one(self):
        """电脑自己录的那场：还是原来那句「录音中」，没有「手机」字样。"""
        pc = self._render([self._m("recording", "pc")])[0]
        old = self._render([self._m("recording")])[0]      # 老服务端（没有 source）
        self.assertIn(">录音中</span>", pc, pc)
        self.assertNotIn("手机录音中", pc, "电脑在录的那场不许被说成手机在录")
        self.assertEqual(old, pc, "没有 source 的老数据必须与 pc 逐字一样（按 pc 兜底）")
        self.assertNotIn("不是这台电脑", pc)

    def test_a_finished_phone_meeting_is_not_still_recording(self):
        """已经转写完的手机麦会议：说「已转写」，不是「手机录音中」（`source` 不是活动状态）。"""
        html = self._render([self._m("transcribed", "phone")])[0]
        self.assertIn(">已转写</span>", html, html)
        self.assertNotIn("手机录音中", html)

    def test_a_finished_pc_meeting_is_unchanged(self):
        """既有几档状态（转写中/失败/待转写）一个都没被这次改动挤掉。"""
        rows = self._render([self._m("transcribing", "pc"),
                             self._m("error", "pc", error="引擎报错"),
                             self._m("imported", "pc")])
        self.assertIn(">转写中</span>", rows[0])
        self.assertIn(">录音失败</span>", rows[1])
        self.assertIn(">待转写</span>", rows[2])

    def test_the_row_reuses_the_one_judgement(self):
        """源码级钉子：列表行**复用** `isPhoneMicMeeting`，不许在行里另写一套判据。"""
        i = self.app_js.index("function renderMeetingItems(")
        fn = self.app_js[i:self.app_js.index("\n}", i) + 2]
        self.assertIn("isPhoneMicMeeting({", fn, "列表行要复用那唯一一份判据")
        self.assertIn("m.source", fn, "判据的输入是服务端下发的 source")
        # 判据本身仍然只有一份（两处各抄一份迟早会漂）
        self.assertEqual(1, self.app_js.count("function isPhoneMicMeeting("),
                         "`isPhoneMicMeeting` 只许有一份实现")


if __name__ == "__main__":
    unittest.main()
