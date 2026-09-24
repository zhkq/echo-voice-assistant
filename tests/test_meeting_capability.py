# -*- coding: utf-8 -*-
"""主链路接线：会议分段转写走能力后端（3.0 施工顺序 step 3 的前半）。

这件事的价值就一句话：**在此之前，装什么后端都不影响会议转写。**
接线之后，"办公本没有 GPU"这件事才有了出路 —— 音频发给有显卡的那台。

## 为什么测的是 `_capability_segment_rows` 而不是 `_transcribe_impl`

第一版我端到端跑 `_transcribe_impl`（造一场"会议"：meta.json + 库 + wav），
结果**污染了后面的测试文件**：它要桩掉 `db`、`meta`、导出、`settings`，
而 `db.DATA_DIR/DB_FILE` 是模块级全局 —— 排在后面的 `test_meeting_rows`
在 Windows 上删不掉自己的临时库（`WinError 32`），还有一次撞出
`database is locked`。**报错出现在别人那里，与本文件看着毫无关系。**

所以把那段逻辑抽成了 `_capability_segment_rows(router, need, wav, cfg, …)` ——
它只依赖那四个参数，测试直接喂假 router + 一个 wav，**一个库都不碰**。
`_transcribe_impl` 里留下的只是"调它 + 记日志 + 写 meta"。

钉三件事：

  1. **没配后端时行为不变** —— 判据是"计划里 `asr.text` 落到本机以外"，
     没配就返回 None，走原来那段本地代码。这是接线敢上线的根据。
  2. **配了后端就真的发出去**，文本按拼装层规则变成逐句时间，档位如实标注。
  3. **后端失败要留痕**，而且**不在段内悄悄回落本地**。
"""
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import (                                   # noqa: E402
    BACKEND_ECHO_SERVER,
    SOURCE_LAN,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    CapabilityRouter,
    Need,
    Provenance,
)
from app.capabilities.assemble import (                          # noqa: E402
    TIMESTAMPS_ALIGNED,
    TIMESTAMPS_ESTIMATED,
    TIMESTAMPS_EXACT,
)

CFG = {"sttLanguage": "zh", "sttModel": "sensevoice"}


class _FakeBackend(CapabilityClient):
    """假的能力后端：记下被调了什么，按脚本回答。"""

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"asr.text", "asr.timestamps"})
    vector_space_id = "ws-fake-v1"

    def __init__(self, sentences=(), text="后端文本。第二句。", fail=None):
        self.calls = []
        self._sentences = list(sentences)
        self._text = text
        self._fail = fail

    def transcribe(self, wav, *, lang="auto", want_timestamps=False, variant="long", **kw):
        self.calls.append((os.path.basename(wav), want_timestamps))
        if self._fail:
            raise CapabilityError(self._fail, "后端故意失败",
                                  backend_id=self.backend_id, slot="asr.text")
        return AsrResult(text=self._text, sentences=tuple(self._sentences),
                         timestamps=TIMESTAMPS_EXACT if self._sentences else "none",
                         provenance=Provenance(self.backend_id, "fake-v1"),
                         audio_seconds=1.0)


class _RowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-capseg-")
        self.wav = os.path.join(self.tmp, "01.wav")
        with wave.open(self.wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            n = 16000
            w.writeframes(struct.pack("<%dh" % n, *([0] * n)))

    def _rows(self, backend, seg_min=10):
        """直接调被抽出来的那段逻辑：一个库、一个 meta 都不需要。"""
        import app.meeting as meeting
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        kinds = {}
        rows, plan, got = meeting._capability_segment_rows(
            router, _need(), self.wav, CFG, 1, seg_min, kinds)
        return rows, plan, got, kinds


def _need():
    return Need(slots=("asr.text", "asr.timestamps"), purpose="meeting")


class SegmentGoesToTheBackendTests(_RowCase):
    """配了后端 → 音频真的发出去，文本按拼装规则变成逐句时间。"""

    def test_full_backend_answer_is_used_as_is(self):
        """后端一次就给全（文本 + 句级时间轴）→ **只调一次**，档位 `exact`。"""
        backend = _FakeBackend(sentences=[(0.0, 0.5, "后端第一句。"),
                                          (0.5, 1.0, "后端第二句。")])
        rows, plan, got, kinds = self._rows(backend)
        self.assertEqual([c[0] for c in backend.calls], ["01.wav"],
                         "后端没被调用（或调了不止一次）")
        self.assertTrue(backend.calls[0][1], "要时间戳时应带上 timestamps=1")
        self.assertEqual(kinds, {TIMESTAMPS_EXACT: 1})
        self.assertEqual([r[3] for r in rows], ["后端第一句。", "后端第二句。"])
        self.assertEqual([r[0] for r in rows], [1, 1], "行里要带段号")

    def test_text_only_backend_is_marked_estimated(self):
        """后端只给文本 → 按字数均摊，**档位如实写着 `estimated`**。

        设计 §4.4 点名要修的就是这个谎："现在这个信息只写在注释里，面板和导出看不出来"。
        """
        backend = _FakeBackend(sentences=[], text="第一句。第二句长一点。")
        rows, _plan, got, kinds = self._rows(backend)
        self.assertEqual(kinds, {TIMESTAMPS_ESTIMATED: 1})
        self.assertEqual(len(rows), 2)
        # 均摊的**总长取真实音频时长**（这段 wav 是 1 秒），而不是配置里的"分段 10 分钟"
        # —— 实际时长更准，配置只是"打算录多长"。第一版我按 600 写，被它挡下来了。
        self.assertAlmostEqual(rows[-1][2], 1.0, places=1)

    def test_configured_segment_length_is_the_fallback_when_duration_is_unknown(self):
        """量不出真实时长（文件不是合法 wav）时才用配置的分段长度兜底。"""
        backend = _FakeBackend(sentences=[], text="第一句。第二句。")
        import app.meeting as meeting
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        kinds = {}
        with patch.object(meeting, "_wav_seconds", lambda p: 0.0):
            rows, _plan, _got = meeting._capability_segment_rows(
                router, _need(), self.wav, CFG, 1, 10, kinds)
        self.assertAlmostEqual(rows[-1][2], 600.0, places=1)

    def test_skeleton_from_the_other_slot_gets_aligned(self):
        """文本没带时间戳，但 `asr.timestamps` 槽能给骨架 → 对齐（`aligned`）。

        这就是把 `asr.text` 与 `asr.timestamps` 分成两个槽的意义：
        文本可以来自只给文本的服务，骨架来自另一个能出时间轴的后端。
        """
        backend = _FakeBackend(sentences=[], text="第一句话。第二句话。")
        calls = {"n": 0}
        real = backend.transcribe

        def scripted(wav, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(wav, **kw)          # asr.text：只给文本
            return AsrResult(text="",            # asr.timestamps：只给骨架
                             sentences=((0.0, 5.0, "第一句话"), (5.0, 12.0, "第二句话")),
                             timestamps=TIMESTAMPS_EXACT,
                             provenance=Provenance(backend.backend_id, "fake-v1"))

        backend.transcribe = scripted
        rows, _plan, _got, kinds = self._rows(backend)
        self.assertEqual(kinds, {TIMESTAMPS_ALIGNED: 1}, "有骨架却没走对齐")
        self.assertEqual(calls["n"], 2, "应当分别问了 asr.text 与 asr.timestamps")
        self.assertGreater(rows[1][1], 5.0, "第二句应当落在骨架说的 5 秒之后")

    def test_plan_records_which_backend_and_why(self):
        """计划里必须有"用了谁 + 为什么" —— 面板排障靠它。"""
        _rows, plan, _got, _kinds = self._rows(_FakeBackend(sentences=[(0, 1, "x")]))
        self.assertEqual(plan["picks"]["asr.text"]["backendId"], BACKEND_ECHO_SERVER)
        self.assertTrue(plan["picks"]["asr.text"]["reason"])


class BackendFailureIsHonestTests(_RowCase):
    """后端失败要**抛出来**（由调用方留痕），**绝不拿空结果冒充成功**。"""

    def test_failure_raises_so_the_caller_can_log_it(self):
        with self.assertRaises(CapabilityError) as ctx:
            self._rows(_FakeBackend(fail="offline"))
        self.assertEqual(ctx.exception.reason, "offline")

    def test_empty_backend_answer_is_not_an_error_but_yields_no_rows(self):
        """后端说"这段没人说话"（空文本、无时间戳）→ **不抛**，但一行都不产出。

        空结果与失败是两件事：前者是安静片段，后者是后端坏了。
        混起来会让"安静片段 → 换后端 → 还是安静"变成一个蠢循环。
        """
        rows, _plan, got, kinds = self._rows(_FakeBackend(sentences=[], text=""))
        self.assertEqual(rows, [])
        self.assertEqual(got.timestamps, "none")


class SessionDecisionTests(unittest.TestCase):
    """**什么情况下才走能力后端** —— 这条决定了"没配后端时行为不变"。"""

    def test_no_backend_configured_means_no_session(self):
        """没配地址 → 返回 None → `_transcribe_impl` 走原来那段本地代码。"""
        import app.meeting as meeting
        from app.config import settings
        with patch.object(settings, "get",
                          lambda k, d=None: "" if k == "capabilityEchoServerUrl" else d), \
             patch.multiple("app.meeting.db", add_log=lambda *a, **k: None):
            self.assertIsNone(meeting._capability_asr_session(CFG))

    def test_configured_and_reachable_backend_produces_a_session(self):
        """配了**且真的可用**的后端 → 给出 session（不是 None）。

        少了这条，"永远返回 None"这种改动不会被任何用例抓住 ——
        下面那些用例都是**注入** session 去测逻辑，"什么情况下有 session" 得单独钉。
        """
        import app.meeting as meeting
        from app.capabilities import CapabilityRouter
        from app.config import settings
        backend = _FakeBackend(sentences=[(0.0, 0.5, "x")])
        router = CapabilityRouter([backend], settings_get=lambda k, d=None: d)
        with patch.object(settings, "get",
                          lambda k, d=None: "http://gpu-01:8900"
                          if k == "capabilityEchoServerUrl" else d), \
             patch("app.capabilities.build_default_router", lambda **kw: router), \
             patch.multiple("app.meeting.db", add_log=lambda *a, **k: None):
            got = meeting._capability_asr_session(CFG)
        self.assertIsNotNone(got, "配了可用后端却拿不到 session —— 那 3.0 等于没接上")
        sess_router, need = got
        self.assertIs(sess_router, router)
        self.assertEqual(sess_router.plan(need).backend_for("asr.text"), BACKEND_ECHO_SERVER)

    def test_configured_but_unreachable_backend_falls_back_LOUDLY(self):
        """配了后端却连不上 → 走本机，但**必须说出来为什么**。

        不说的话：用户以为在用后端，实际在啃本机 CPU，现象只是"转写很慢"
        —— 本机那条路的日志一切正常，查不出所以然。这是接线里最容易"静默退化"的一处。
        """
        import app.meeting as meeting
        from app.config import settings
        logged = []
        real_get = settings.get

        def fake_get(key, default=None):
            if key == "capabilityEchoServerUrl":
                return "http://127.0.0.1:1"        # 配了，但没人监听
            return real_get(key, default)

        with patch.object(settings, "get", fake_get), \
             patch.multiple("app.meeting.db",
                            add_log=lambda level, src, msg: logged.append((level, src, msg))):
            self.assertIsNone(meeting._capability_asr_session(CFG))
        self.assertTrue([m for lv, src, m in logged
                         if lv == "warn" and "仍走本机引擎" in m],
                        "静默退化了，没留下原因：%s" % logged)
        self.assertTrue(any("原因" in m for _lv, _s, m in logged),
                        "日志里要写出 skip 的原因（连不上 / privacy 不允许 …）")


if __name__ == "__main__":
    unittest.main()
