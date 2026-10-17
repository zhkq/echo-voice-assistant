# -*- coding: utf-8 -*-
"""指令的"要念的那段话"（`speakText`）与**按发起方路由朗读**（2026-10-17 用户真机）。

用户原话（手机发了一条测试命令之后）：

  > **"1、手机端朗读了全部内容，包括详情，应该跟电脑测试一样，只朗读前面的摘要
  >   2、电脑端同步朗读了指令答复，应该改为手机的任务，电脑只执行不朗读"**

这两条其实是同一件事的两面，本文件各钉一组判据：

  ① **单一出处**：电脑本机朗读的那段话 = 回传给手机的 `speakText` ——
     都来自 `assistant.speak_text_for_reply()`（它用的就是电脑一直念的
     `brief_text()`/`conclusion_only()`，在"详情如下："处切开）。
     两端各切一套必然漂；这次就漂在"详情"上（手机把整篇 `reply` 念了出来）。
  ② **谁发起，谁念**：`origin=mobile` → 电脑**只执行、存储、回传**，喇叭一声不响；
     `origin=pc`（含拿不到来源时的既有行为）→ 照旧朗读。

隔离（照 `tests/test_event_origin.py` 的写法）：临时库 + 打桩 DSH / TTS / 通知。
**绝不写真库、不真响一声**：`providers.speak_async` / `providers.speak_text` /
`app.audio.tts.speak` 三处都换成记录器，"有没有念"看记录器的调用次数。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

from app import assistant as assistant_mod                           # noqa: E402
from app import db                                                   # noqa: E402
from app import providers as providers_mod                           # noqa: E402
from app.api import router                                           # noqa: E402
from app.config import settings                                      # noqa: E402
from app.audio import tts as audio_tts                               # noqa: E402

#: 电脑上"电脑测试"看到的那种回复：**一段摘要 + 详情**。
#: 详情故意长到远超 `maxBriefChars`（200），且带一个**只在详情里出现**的记号 ——
#: 这样"念了详情"一定逃不过断言（截断到 200 字也会把记号带进来）。
SUMMARY = "客厅的灯已经开好了，室温二十六度。"
DETAIL_MARK = "灯带亮度百分之七十"
DETAIL = ("详情如下：\n（下面这些是给人看的长内容。" + DETAIL_MARK +
          "，窗帘半开；空调除湿模式二十六度；湿度百分之四十五；"
          "另外还顺手查了明后两天的天气，早晚温差有点大。" * 6 + "）")
REPLY = SUMMARY + "\n" + DETAIL


class _FakeDshClient:
    """DSH 替身：够 `_dispatch` 走完"发出 → 等回复 → 写 `command_done`"。

    刻意**不给** `list_sessions`：`_configured_command_target()` 因此返回 `(None, None)`，
    不必再打桩「命令目标」那两项设置。
    """

    def __init__(self, reply=REPLY):
        self.reply = reply
        self.prompted = []

    def ping(self):
        return True

    def ensure_command_session(self, text):
        return "sid-speak"

    def clear_stuck(self, sid):
        pass

    def prompt(self, sid, text, mode="queue"):
        self.prompted.append(text)

    def wait_for_reply(self, sid, timeout=0, poll=0):
        return (self.reply, True)


class _Spoken:
    """TTS 记录器：**只记不响**（判据就是它的调用次数/参数）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, text, *a, **kw):
        self.calls.append(text)
        return True


class CommandSpeakRoutingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-speakdb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "test.db")
        db.init()
        # 进程级设置缓存**不受**上面的 db 补丁约束：不清就会读到真库的值
        # （比如本机真的开着 apiAuthEnabled，接口就 401 了）。
        settings._drop_cache()

        self.speak_async = _Spoken()
        self.speak_text = _Spoken()
        self._audio_speak = _Spoken()

        self._patchers = [
            mock.patch.object(assistant_mod, "get_client",
                              lambda: self.dsh),
            mock.patch.object(assistant_mod, "_agent_snapshot", lambda: ""),
            mock.patch.object(assistant_mod, "build_env_context", lambda: ""),
            mock.patch.object(assistant_mod, "build_reply_requirement", lambda: ""),
            mock.patch.object(assistant_mod, "notify", lambda *a, **kw: None),
            # 提示音/朗读：assistant 里那两个入口都是 `tts_mod.*`，
            # 换成**不响**的替身（`speak_async` 是 providers 那边的，单独桩）。
            mock.patch.object(assistant_mod, "tts_mod",
                              SimpleNamespace(play_beep=lambda *a, **kw: None,
                                              beep_ok=lambda *a, **kw: None,
                                              speak=lambda *a, **kw: True)),
            mock.patch.object(providers_mod, "speak_async", self.speak_async),
            mock.patch.object(providers_mod, "speak_text", self.speak_text),
            mock.patch.object(audio_tts, "speak", self._audio_speak),
        ]
        for p in self._patchers:
            p.start()

        self.dsh = _FakeDshClient()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

        self.addCleanup(self._clean)

    # ---------------------------------------------------------------- 隔离

    def _clean(self):
        for p in self._patchers:
            p.stop()
        db.DATA_DIR, db.DB_FILE = self._old_db
        # 缓存按**临时库**读满过，交出去之前必须丢掉（与 test_events_feed 同一个理由）；
        # 顺手按真实库重建一次，让交出去的状态与"没跑过本类"一致。
        settings._drop_cache()
        try:
            settings.get("apiAuthEnabled")
        except Exception:                                          # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 小工具

    def _dispatch(self, text, source, reply=REPLY):
        """真跑 `_dispatch`（DSH 与 TTS 都是替身），返回它的布尔结果。"""
        self.dsh = _FakeDshClient(reply)
        with mock.patch.object(assistant_mod, "get_client", lambda: self.dsh):
            return assistant_mod._dispatch(text, source)

    def _commands(self):
        return db.list_commands(limit=50)

    def _one_command(self):
        rows = self._commands()
        self.assertEqual(1, len(rows), "该只有一条指令：%s" % (rows,))
        return rows[0]

    def _events(self, type_):
        out = []
        for row in reversed(db.list_events(limit=200)):
            if row.get("type") != type_:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except Exception:                                      # noqa: BLE001
                payload = {}
            out.append(payload)
        return out

    def _one_event(self, type_):
        rows = self._events(type_)
        self.assertEqual(1, len(rows), "%s 该只有一条：%s" % (type_, rows))
        return rows[0]

    def _api_row(self, cmd_id):
        r = self.client.get("/api/commands")
        self.assertEqual(200, r.status_code, r.text)
        items = r.json()["items"]
        for it in items:
            if it.get("id") == cmd_id:
                return it
        self.fail("`GET /api/commands` 里找不到指令 %s：%s" % (cmd_id, items))

    # ------------------------------------- 1) 手机发起的：执行、存储、回传，但不念

    def test_a_mobile_command_runs_is_stored_and_returns_speak_text_without_pc_tts(self):
        """`origin=mobile`：**电脑喇叭一声不响**，但指令照常执行/存储/回传。

        为什么电脑不该念：这是**手机发起的任务** —— 用户在手机上按的、手机在等结果，
        声音该出在手机上。电脑只是"配合执行和存储"（用户 2026-10-16/17 的一贯口径），
        在电脑的喇叭上念一遍等于把同一条答复播给两个房间听。
        """
        self.assertTrue(self._dispatch("把客厅灯打开", "mobile"))

        # ① TTS **一次都没被调用**（三个入口全查：assistant 直接调的那个 + 音频层）
        self.assertEqual([], self.speak_async.calls, "手机发起的任务，电脑不该朗读")
        self.assertEqual([], self.speak_text.calls, "手机发起的任务，电脑不该朗读")
        self.assertEqual([], self._audio_speak.calls, "音频层也不该真的出声")

        # ② 执行了：命令真的发给了 DSH（prompt 里有原话），状态 done、回复落库
        self.assertEqual(1, len(self.dsh.prompted), "指令要照常发给智能体")
        self.assertIn("把客厅灯打开", self.dsh.prompted[0])
        row = self._one_command()
        self.assertEqual("done", row["status"])
        self.assertEqual(REPLY, row["reply"], "`reply`（给人看的整篇）照常存，一个字不动")
        self.assertEqual(SUMMARY, row["brief"], "`brief` = 当时该念的摘要")

        # ③ 存了：日志里有这条指令的痕迹（来源也写着 mobile）
        logs = " | ".join((r.get("message") or "") for r in db.list_logs(limit=100))
        self.assertIn("命令[mobile]", logs)

        # ④ 回传里带 `speakText`（手机拿它直接念），既有键 `id`/`origin` 原样都在
        ev = self._one_event("command_done")
        self.assertEqual(row["id"], ev.get("id"))
        self.assertEqual("mobile", ev.get("origin"))
        self.assertEqual(SUMMARY, ev.get("speakText"),
                         "手机念的就是电脑本机朗读的那段摘要")
        self.assertNotIn(DETAIL_MARK, ev.get("speakText") or "",
                         "详情是给人看的，不该进 `speakText`")

    # ------------------------------- 2) 电脑发起的：照旧念，且只念摘要（不是全文）

    def test_a_pc_command_still_speaks_and_only_the_summary(self):
        """`origin=pc`：**行为不变**（照旧朗读），但念的只能是摘要，不是全文/详情。

        这条正是用户说的"跟电脑测试一样"：电脑上一直是这样念的 —— 这里把它钉住，
        以后谁改了 `brief_text`/截断规则，手机端会跟着一起变（单一出处），但**不许**变成念全文。
        """
        self.assertTrue(self._dispatch("看看客厅现在什么情况", "web"))

        self.assertEqual([SUMMARY], self.speak_async.calls,
                         "电脑发起 → 照旧朗读，且只念摘要")
        self.assertEqual([], self._audio_speak.calls,
                         "朗读走 providers.speak_async，不该绕过它直接调音频层")
        spoken = self.speak_async.calls[0]
        self.assertNotIn(DETAIL_MARK, spoken, "详情不该被念出来")
        self.assertNotEqual(REPLY, spoken, "念的是摘要，不是整篇回复")
        self.assertEqual("pc", self._api_row(self._one_command()["id"])["origin"],
                         "电脑发起的任务，行上的 origin 该是 pc")

    # ------------------------------------ 3) 单一出处：speakText == 电脑念的那段

    def test_speak_text_is_exactly_what_the_pc_reads(self):
        """同一段回复，手机收到的 `speakText` 与电脑念的**逐字相等**。

        用户原话是"应该**跟电脑测试一样**" —— 判据不是"像摘要"，而是"同一个字符串"。
        这条刻意让两条路都真跑一遍（先 pc 再 mobile，临时库不冲突），再比字符串：
        任何一端偷偷另切一套（哪怕只差一个标点）都会红。
        """
        self.dsh = _FakeDshClient(REPLY)
        with mock.patch.object(assistant_mod, "get_client", lambda: self.dsh):
            self.assertTrue(assistant_mod._dispatch("看看客厅", "web"))
            self.assertTrue(assistant_mod._dispatch("看看客厅", "mobile"))

        pc_spoken = self.speak_async.calls
        self.assertEqual(1, len(pc_spoken), "只有 pc 那条该念，mobile 那条不该念")
        mobile_ev = [ev for ev in self._events("command_done")
                     if ev.get("origin") == "mobile"]
        self.assertEqual(1, len(mobile_ev), mobile_ev)
        self.assertEqual(pc_spoken[0], mobile_ev[0].get("speakText"),
                         "手机念的与电脑念的必须**逐字相同**（同一函数、同一结果）")
        # 而它们都来自那个唯一出处的函数（不是测试自己拼的样例）
        self.assertEqual(assistant_mod.speak_text_for_reply(REPLY),
                         mobile_ev[0].get("speakText"))

    # ------------------------------ 4) 指令回复接口（`GET /api/commands`）也带 speakText

    def test_the_commands_endpoint_hands_the_phone_the_same_speak_text(self):
        """手机轮询用的 `GET /api/commands` 里也得有 `speakText`，且与事件里那份相同。

        手机有两条拿结果的路：事件推送（`command_done`）与轮询指令列表。只补一条的话，
        另一条路仍会让手机去念 `reply`（整篇）—— 那正是这次的事故。既有键
        （`reply`/`brief`/`status`/`source`/`meta`…）一个都不许少/改名。
        """
        self.assertTrue(self._dispatch("把客厅灯打开", "mobile"))
        row = self._one_command()
        api_row = self._api_row(row["id"])

        self.assertEqual(SUMMARY, api_row.get("speakText"))
        self.assertEqual(api_row.get("brief"), api_row.get("speakText"),
                         "历史行的 brief 就是当时该念的那段")
        self.assertEqual(REPLY, api_row.get("reply"), "`reply` 原样还在（给人看的）")
        self.assertEqual("mobile", api_row.get("source"))
        # `origin` 与事件里的那份**同一个判据**：手机靠它实现"谁发起谁念"
        # （只有 mobile 发起的才在手机上念 `speakText`）。
        self.assertEqual("mobile", api_row.get("origin"))
        for key in ("id", "ts", "source", "text", "status", "session_id",
                    "reply", "brief", "duration_ms", "error", "meta"):
            self.assertIn(key, api_row, "既有键 %s 不许少" % key)
        ev = self._one_event("command_done")
        self.assertEqual(ev.get("speakText"), api_row.get("speakText"),
                         "两条回传路径必须是同一段话")

    # ------------------------------------------- 5) 详情绝不进 speakText（长详情样例）

    def test_the_long_detail_never_leaks_into_speak_text(self):
        """塞一段很长的"详情"，`speakText` 里不许出现它的任何记号。

        为什么要单独钉：摘要截断（`maxBriefChars`）只看**长度**，
        一旦有人把"取摘要"改成"整篇截断"，短详情可能侥幸看不出来，长详情一定露馅。
        """
        self.assertTrue(self._dispatch("查一下客厅", "mobile"))
        ev = self._one_event("command_done")
        spoken = ev.get("speakText") or ""
        self.assertTrue(spoken, "摘要不该为空")
        self.assertNotIn(DETAIL_MARK, spoken)
        self.assertNotIn("详情如下", spoken)
        self.assertNotIn("窗帘半开", spoken)
        self.assertEqual(SUMMARY, spoken)
        self.assertLess(len(spoken), len(DETAIL), "念的东西要比详情短得多")

    # ---------------------- 6) 拿不到来源 → 按既有行为（朗读），不悄悄改变电脑行为

    def test_an_unknown_origin_keeps_speaking_like_before(self):
        """认不出来源（None/空/hotkey/API/大写乱写）→ 按既有行为朗读。

        `_command_origin()` 对认不出的来源保守判成 `pc`（"宁可不在手机上响"）。
        这条保证本次改动**没有**让电脑自己发起的指令变成"不念" —— 那是另一种回归。
        """
        for i, source in enumerate((None, "", "   ", "hotkey", "API")):
            self.dsh = _FakeDshClient(REPLY)
            with mock.patch.object(assistant_mod, "get_client", lambda: self.dsh):
                self.assertTrue(assistant_mod._dispatch("第%s条" % i, source),
                                "来源 %r 的指令该照常执行" % (source,))
        self.assertEqual(5, len(self.speak_async.calls),
                         "认不出来源 → 按 pc 的既有行为朗读")
        for ev in self._events("command_done"):
            self.assertEqual("pc", ev.get("origin"))
            self.assertEqual(SUMMARY, ev.get("speakText"))

    # ---------------------- 7) 电脑关了语音简报，手机仍拿得到 speakText（各管各的开关）

    def test_pc_voice_brief_off_does_not_take_speak_text_away_from_the_phone(self):
        """`voiceBrief=false` 只关**电脑喇叭**，不该顺手把手机的 `speakText` 清掉。

        `voiceBrief` 是"这台电脑要不要出声"的开关；手机念不念由手机自己的设置决定。
        把两者绑在一起，用户会得到一个"手机突然不念了"的谜。
        """
        db.set_setting("voiceBrief", False)
        settings._drop_cache()
        self.assertTrue(self._dispatch("看看客厅", "web"))
        self.assertEqual([], self.speak_async.calls, "电脑关了语音简报 → 不念")
        ev = self._one_event("command_done")
        self.assertEqual(SUMMARY, ev.get("speakText"),
                         "手机要念的那段话仍要回传")
        self.assertEqual(SUMMARY, self._api_row(self._one_command()["id"])["speakText"])

    # ---------------------- 8) 老记录（没有 brief，只有整篇 reply）也不念详情

    def test_an_old_row_without_brief_still_yields_the_same_summary(self):
        """老记录只有 `reply`、`brief` 为空 → 接口现算，规则仍是**同一份**。

        升级前落库的行没有 `speakText`；如果这里另写一套"取开头"的逻辑，
        老记录就会立刻和电脑念的不一样（用户手机上听到的是另一段）。所以只许调
        `speak_text_for_reply()`。读接口不该反过来触发朗读。
        """
        cid = db.add_command("老指令", source="mobile", status="done")
        db.update_command(cid, reply=REPLY)                 # 故意不写 brief
        api_row = self._api_row(cid)
        self.assertEqual("", api_row.get("brief"), "前提：这条老记录没有 brief")
        self.assertEqual(SUMMARY, api_row["speakText"])
        self.assertEqual(assistant_mod.speak_text_for_reply(REPLY),
                         api_row["speakText"], "与电脑本机朗读同一函数、同一结果")
        self.assertEqual([], self.speak_async.calls, "只是读接口，不该触发朗读")


if __name__ == "__main__":
    unittest.main()
