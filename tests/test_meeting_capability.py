# -*- coding: utf-8 -*-
"""主链路接线：会议分段转写与**说话人分离**走能力后端（3.0 施工顺序 step 3 + step 4）。

这件事的价值就一句话：**在此之前，装什么后端都不影响会议转写。**
接线之后，"办公本没有 GPU"这件事才有了出路 —— 音频发给有显卡的那台。

## 为什么测的是 `_capability_segment_rows` / `_capability_diarize_segment`

第一版我端到端跑 `_transcribe_impl`（造一场"会议"：meta.json + 库 + wav），
结果**污染了后面的测试文件**：它要桩掉 `db`、`meta`、导出、`settings`，
而 `db.DATA_DIR/DB_FILE` 是模块级全局 —— 排在后面的 `test_meeting_rows`
在 Windows 上删不掉自己的临时库（`WinError 32`），还有一次撞出
`database is locked`。**报错出现在别人那里，与本文件看着毫无关系。**

所以把那段逻辑抽成了 `_capability_segment_rows(会话, wav, cfg, …)` 与
`_capability_diarize_segment(会话, wav)` —— 它们只依赖那几个参数，
测试直接喂假 router + 一个 wav，**一个库都不碰**。
`_transcribe_impl` 里留下的只是"调它 + 记日志 + 写 meta"。

钉四件事：

  1. **没配后端时行为不变** —— 判据是"计划里有没有会议槽落到本机以外"，
     没配就返回 None，走原来那段本地代码（含原来的 `diarize_wav_full`）。
     这是接线敢上线的根据。
  2. **配了后端就真的发出去**，文本按拼装层规则变成逐句时间，档位如实标注。
  3. **后端失败要留痕**，而且**不在段内悄悄回落本地**。
  4. **说话人分离与转写同源**（L5）：分离结果来自后端、形状与本机逐字对齐、
     计划里记下 `diarize.turns` 用了谁；走不了时有一条带 `reason` 的 warn。
"""
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db                                            # noqa: E402
from app.capabilities import (                                   # noqa: E402
    BACKEND_ECHO_SERVER,
    BACKEND_LOCAL,
    SKIP_REASONS,
    SOURCE_LAN,
    AsrResult,
    CapabilityClient,
    CapabilityError,
    CapabilityRouter,
    Need,
    Provenance,
)
from app.capabilities import credentials as cred_mod             # noqa: E402
from app.capabilities.assemble import (                          # noqa: E402
    TIMESTAMPS_ALIGNED,
    TIMESTAMPS_ESTIMATED,
    TIMESTAMPS_EXACT,
)
from app.config import settings                                  # noqa: E402

CFG = {"sttLanguage": "zh", "sttModel": "sensevoice"}


class _FakeBackend(CapabilityClient):
    """假的能力后端：记下被调了什么，按脚本回答。

    step 4 起它还要回答 **说话人分离与嵌入**（`diarize.turns` / `speaker.embed`）——
    会议链路的分离现在走能力路由，所以这个替身必须能出与
    `app/audio/diarize.py` **同形状**的结果（`turns` / `speakers` / `dim` / 向量空间）。
    """

    backend_id = BACKEND_ECHO_SERVER
    source = SOURCE_LAN
    provides = frozenset({"asr.text", "asr.timestamps", "diarize.turns",
                          "diarize.embeddings", "speaker.embed"})
    vector_space_id = "ws-fake-v1"

    def __init__(self, sentences=(), text="后端文本。第二句。", fail=None,
                 turns=None, speakers=None, dim=0, fail_diarize=None):
        self.calls = []
        self.diarize_calls = []
        self._sentences = list(sentences)
        self._text = text
        self._fail = fail
        self._fail_diarize = fail_diarize
        # 默认给一个"两个人"的分离结果（形状与 pyannote 一致：
        # 时间轴用 `SPEAKER_xx` 标签，每个标签一条 256 维嵌入）。
        self._turns = ([(0.0, 0.5, "SPEAKER_00"), (0.5, 1.0, "SPEAKER_01")]
                       if turns is None else list(turns))
        self._speakers = ({"SPEAKER_00": [0.1] * 256, "SPEAKER_01": [0.2] * 256}
                          if speakers is None else speakers)
        self._dim = int(dim or 256)

    def transcribe(self, wav, *, lang="auto", want_timestamps=False, variant="long", **kw):
        self.calls.append((os.path.basename(wav), want_timestamps))
        if self._fail:
            raise CapabilityError(self._fail, "后端故意失败",
                                  backend_id=self.backend_id, slot="asr.text")
        return AsrResult(text=self._text, sentences=tuple(self._sentences),
                         timestamps=TIMESTAMPS_EXACT if self._sentences else "none",
                         provenance=Provenance(self.backend_id, "fake-v1"),
                         audio_seconds=1.0)

    def diarize(self, wav, *, max_speakers=None, **kw):
        self.diarize_calls.append(os.path.basename(wav))
        if self._fail_diarize:
            raise CapabilityError(self._fail_diarize, "后端故意失败（分离）",
                                  backend_id=self.backend_id, slot="diarize.turns")
        from app.capabilities import DiarizeResult
        return DiarizeResult(turns=tuple(self._turns),
                             speakers={k: tuple(v) for k, v in self._speakers.items()},
                             dim=self._dim, vector_space_id=self.vector_space_id,
                             provenance=Provenance(self.backend_id, "fake-dia-v1"),
                             audio_seconds=1.0)


#: 这一场会要用到的槽（step 4 起含 `diarize.turns`）。
#: 顺序与 `meeting._session_slots()` 一致：`diarize.turns` 在 `speaker.embed` 之前
#: —— 这是**刻意**的，L5 的向量空间锁沿着槽的顺序推进（先有分离才谈得上认人）。
SESSION_SLOTS = ("asr.text", "asr.timestamps", "diarize.turns", "speaker.embed")


def _need(slots=SESSION_SLOTS):
    return Need(slots=tuple(slots), purpose="meeting")


def _session(router, slots=SESSION_SLOTS):
    """造一个假 Router 的会话。

    **走会议那边真用的那个类**（`meeting._CapabilitySession`），不另写一个替身 ——
    要验的恰恰是"L5 的锁在会议这条路上有没有被带下去"，用替身测等于没测。
    """
    import app.meeting as meeting
    return meeting._CapabilitySession(router, CFG, slots)



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
            _session(router), self.wav, CFG, 1, seg_min, kinds)
        return rows, plan, got, kinds


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
                _session(router), self.wav, CFG, 1, 10, kinds)
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


class _IsolatedState(unittest.TestCase):
    """把"这台机器真实的状态"隔在外面：库、凭据文件、设置缓存。

    为什么 step 4 非做不可：`_capability_asr_session` 现在会把**分离槽**也放进判据，
    而分离槽的默认链**只有 ECHO 后端** —— 一台真配对过的开发机上，
    `build_default_router()` 会造出一个真的 ECHO 后端客户端，
    于是"没配后端"的用例会**当场变红**（而且报的现象像是代码本身有问题）。
    三样都得隔离（与 `tests/test_capability_admin.py` 同一套写法）：

      * `db.DATA_DIR` / `db.DB_FILE` —— `settings.update()` 会写库；
      * `credentials_path`        —— 否则会读到这台机器真配对的 `backend.json`；
      * `settings._cache`         —— 否则读到的是上一个测试类（或真实库）里的值。

    **`settings._cache = None` 这一步不能省**：`settings.update()` 在 `_cache` 是
    None 时才重读库，缓存里带着上一个测试类的值会把 `capabilityEchoServerUrl`
    之类带进来 —— 表现是"没配对却出现了 ECHO 后端"这种完全指不到原因的假象。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-mtgcap-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.tmp
        db.DB_FILE = os.path.join(self.tmp, "cap.db")
        db.init()
        settings._cache = None
        settings.seed_defaults()
        self._cred_file = os.path.join(self.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: self._cred_file)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        db.DATA_DIR, db.DB_FILE = self._old_db
        settings._cache = None

    def _put(self, **values):
        settings.update(dict(values))

    def _logged(self):
        """接住日志：能力层的告警只能这样验（`db.add_log` 会写临时库）。"""
        rows = []
        p = patch("app.meeting.db.add_log",
                  lambda level, src, msg: rows.append((level, src, msg)))
        p.start()
        self.addCleanup(p.stop)
        return rows


class DiarizeRoutingTests(_IsolatedState):
    """step 4：**会议链路的说话人分离走能力路由**（`diarize.turns`）。

    这一组直接调 `_capability_diarize_segment(会话, wav)` —— 与
    `SegmentGoesToTheBackendTests` 同一个理由：不碰 `_transcribe_impl` 那个 350 行的
    大循环（它会连累后面的测试文件）。但它比那一组多测一件事：**返回值要和
    `app/audio/diarize.py` 同形状**，因为会议那边落库/合并/声纹那几段代码是
    照那个形状写的，形状不对就会在"合并"那一步崩，而崩的位置离根因很远。

    本机 `diarize_wav_full` 在这里被打桩成一组**可辨认**的结果（说话人叫 "LOCAL"），
    于是"这一场到底用了谁"是肉眼可辨的，不用去猜。
    """

    def setUp(self):
        super().setUp()
        self.wav = os.path.join(self.tmp, "01.wav")
        with wave.open(self.wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<16000h", *([0] * 16000)))
        self.logged = self._logged()
        # 本机分离：形状与真实现一致（turns / embs / labels）
        p = patch("app.audio.diarize.diarize_wav_full",
                  lambda path, max_speakers=None: ([(0.0, 1.0, "LOCAL")],
                                                   [[0.9, 0.9]], ["LOCAL"]))
        p.start()
        self.addCleanup(p.stop)
        # 本场按"配了 ECHO 后端"起手；单个用例要改的设置自己 `_put()`
        self._put(capabilityEchoServerUrl="http://gpu-01:8900",
                  capabilityEchoServerToken="tok",
                  capabilityDiarizeBackend="auto")

    def _router(self, backend, **settings_map):
        if settings_map:
            self._put(**settings_map)
        # 走**真的**设置读取（不是 lambda 返回默认值）：`_order_for` 要按
        # `capabilityDiarizeBackend` 决定候选顺序 —— 拿替身把设置绕开，
        # "用户点名了谁"这件事就没被验到。
        return CapabilityRouter([backend], settings_get=settings.get)

    def _segment(self, backend, **settings_map):
        """按**会议那边真实的顺序**跑一遍：先开会话（说一次"哪些槽不归能力层"），
        再逐段要分离结果。

        顺序很重要：那句"这一槽走不了能力后端（reason=…）"在开会话时**只说一次**
        （8 段会议连说 8 遍会把日志淹掉），而它是排障要看的第一手信息 ——
        所以这里不能只调 `_capability_diarize_segment`。
        """
        import app.meeting as meeting
        router = self._router(backend, **settings_map)
        cap = _session(router)
        cap.note_local_and_empty()
        return meeting._capability_diarize_segment(cap, self.wav)

    def test_backend_result_is_used_and_recorded_in_the_plan(self):
        """**分离结果确实来自后端**（本机那条路一次都没被调用），且计划里记下了用了谁。

        这条是 step 4 的核心：在此之前，`meeting.py` 的分离那一步**无条件**调
        `diarize_wav_full` —— 面板上配了后端也没用，办公本照样一个说话人都没有。
        """
        import numpy as np
        backend = _FakeBackend()
        turns, embs, labels, plan = self._segment(backend)
        self.assertEqual(backend.diarize_calls, ["01.wav"], "后端没被调用")
        self.assertEqual([(a, b, s) for a, b, s in turns],
                         [(0.0, 0.5, "SPEAKER_00"), (0.5, 1.0, "SPEAKER_01")])
        self.assertEqual(labels, ["SPEAKER_00", "SPEAKER_01"])
        self.assertEqual(getattr(embs, "shape", None), (2, 256),
                         "嵌入必须是 (n, dim) 的数组 —— `SpeakerRegistry` 直接对它做矩阵运算")
        self.assertEqual(embs.dtype, np.float32)
        self.assertAlmostEqual(float(embs[1][0]), 0.2, places=5)
        self.assertEqual(plan["picks"]["diarize.turns"]["backendId"], BACKEND_ECHO_SERVER,
                         "计划里必须记下 diarize.turns 用了谁（面板靠它）")
        self.assertTrue(plan["picks"]["diarize.turns"]["reason"], "还要记下为什么")

    def test_the_shape_matches_the_local_one_so_the_merge_step_cannot_break(self):
        """**形状与本机逐字对齐**：同一段音频，两条路的返回值都能喂给同一段代码。

        会议那边接下来要做的是 `registry.map(embs, labels)` →
        `turns` 改标 → `_assign_speakers` → `voiceprint.identify`。
        这里就把这几步真跑一遍（**不是只看类型**）—— 形状对不上时，
        报错会出现在"合并"那一步，离根因很远。
        """
        import numpy as np
        from app.audio.diarize import SpeakerRegistry
        import app.meeting as meeting

        backend = _FakeBackend(turns=[(0.0, 0.5, "SPEAKER_00"), (0.5, 1.0, "SPEAKER_01")],
                               speakers={"SPEAKER_00": [0.1] * 256,
                                         "SPEAKER_01": [0.2] * 256})
        turns, embs, labels, _plan = self._segment(backend)
        reg = SpeakerRegistry()
        label_map = reg.map(embs, labels)
        self.assertEqual(sorted(label_map), ["SPEAKER_00", "SPEAKER_01"])
        key_map = {pl: "S" + re.sub(r"\D", "", disp) for pl, disp in label_map.items()}
        keyed = [(s, e, key_map[spk]) for s, e, spk in turns]
        rows = meeting._assign_speakers([(1, 0.0, 0.6, "前半句"), (1, 0.6, 1.2, "后半句")],
                                        keyed)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r) == 5 for r in rows), "必须变成 db 要的 5 元组")
        # 声纹那一层也要吃得下（它内部按 (embs[i], labels[i]) 对齐）
        from app import voiceprint
        matcher = voiceprint.VoiceMatcher([(1, "老王", np.asarray(embs[0], dtype=np.float32))])
        got = voiceprint.identify(embs, labels, label_map, matcher)
        self.assertTrue(got, "声纹识别吃不下后端给的形状")
        self.assertTrue(any(m["ok"] for m in got.values()),
                        "嵌入与自己比应当命中：%s" % got)

    def test_no_backend_at_all_still_diarizes_locally(self):
        """**没配后端的机器行为不变**：走原来那段本机 `diarize_wav_full`。

        这是底线（任务里的第 2 条）。注意这里连 `capabilityDiarizeBackend` 都没配
        —— 而且**默认链上没有本机**（§5.1：不做兜底），所以它走到本机那条路的
        理由不是"路由挑中了本机"，而是"这场它压根不该来问能力层"。
        判据在 `_capability_asr_session`（那才是"没配后端时行为不变"的守卫）。
        """
        import app.meeting as meeting
        from app.capabilities import CapabilityRouter
        from app.capabilities.local import LocalCapabilityClient
        from app.capabilities import echo_server
        backend = _FakeBackend()
        with patch.object(settings, "get",
                          lambda k, d=None: "" if k == "capabilityEchoServerUrl" else d), \
             patch.object(echo_server, "_creds", lambda: None):
            self.assertIsNone(meeting._capability_asr_session(dict(CFG, diarize=True)))
        # 就算把会话硬塞过来（模拟"将来判据变了"），这一槽也不会落到本机
        router = CapabilityRouter([backend, LocalCapabilityClient(provides={"diarize.turns"})],
                                  settings_get=settings.get)
        turns, embs, labels, plan = meeting._capability_diarize_segment(
            _session(router), self.wav)
        self.assertEqual(labels, ["SPEAKER_00", "SPEAKER_01"], "这一槽不该落到本机")
        self.assertEqual(backend.diarize_calls, ["01.wav"])

    def test_choosing_local_explicitly_uses_the_local_engine(self):
        """**用户显式选了本机** → 不去打扰后端（"他选的主选" ≠ "自动兜底"）。

        与上一条的区别要看清：上一条是"没配后端"（整场不走能力层），
        这一条是"他主动把这一槽设成 local" —— §5.1 的表格专门把这两种分开，
        而 `_order_for` 里 `explicit != "auto"` 那条就是它的落点。

        注意这里**本机客户端没有注册**（router 里只有一个假后端）：点名 `local`
        而本机没装引擎时，计划里 `diarize.turns` 是空的 —— 那也该走本机那段代码
        （它自己会报"没装 pyannote"），而不是在能力层抛一个 `absent` 就算了
        （那样整场会**一行说话人都没有，也没有任何解释**）。
        """
        import app.meeting as meeting
        backend = _FakeBackend()
        router = self._router(backend, capabilityDiarizeBackend="local")
        cap = _session(router)
        cap.note_local_and_empty()
        turns, _embs, _labels, plan = meeting._capability_diarize_segment(cap, self.wav)
        self.assertEqual(backend.diarize_calls, [], "点名本机了还去问后端")
        self.assertIsNone(turns)          # 调用方据此走本机那段代码
        self.assertIsNone(plan)
        self.assertFalse([m for lv, _s, m in self.logged if lv == "warn"],
                         "点名本机不是降级，不该报 warn：%s" % self.logged)

    def test_unsupported_slot_is_a_warn_with_the_authoritative_reason(self):
        """后端**不支持这一槽** → 一条带 `unsupported` 的 warn，返回值是 None（不是空结果）。

        "不支持"与"失败了"要分清：前者是"这个后端没装分离模型"，后者是"它坏了"。
        两种都写权威词汇（`SKIP_REASONS`），面板与日志才排得下去。
        """
        import app.meeting as meeting
        backend = _FakeBackend()
        backend.provides = frozenset({"asr.text"})          # 只会转写
        turns, _embs, _labels, plan = self._segment(backend)
        self.assertIsNone(turns, "拿不到分离结果就必须是 None —— 不能拿空数组冒充成功")
        self.assertIsNone(plan)
        self.assertEqual(backend.diarize_calls, [], "不支持还去调用它")
        warn = " | ".join(m for lv, _s, m in self.logged if lv == "warn")
        self.assertIn("reason=unsupported", warn, warn)
        self.assertIn("diarize.turns", warn, warn)

    def test_a_broken_backend_is_a_warn_not_a_silent_empty_result(self):
        """后端**这一槽失败** → 同样留一条带原因的 warn（权威词汇之一），不静默。"""
        import app.meeting as meeting
        backend = _FakeBackend(fail_diarize="offline")
        turns, _embs, _labels, plan = self._segment(backend)
        self.assertIsNone(turns)
        self.assertIsNone(plan)
        self.assertEqual(backend.diarize_calls, ["01.wav"], "失败也要留下'调用过'的痕迹")
        warn = " | ".join(m for lv, _s, m in self.logged if lv == "warn")
        self.assertIn("reason=offline", warn, warn)

    def test_privacy_none_blocks_the_backend_and_says_blocked(self):
        """`privacy=none`（不出机）→ 挡住后端，warn 里是 `blocked`。"""
        import app.meeting as meeting
        backend = _FakeBackend()
        router = self._router(backend, capabilityPrivacy="none")
        cap = _session(router)
        cap.note_local_and_empty()
        turns, _embs, _labels, plan = meeting._capability_diarize_segment(cap, self.wav)
        self.assertIsNone(turns, "privacy=none 还把音频发出去了？")
        self.assertIsNone(plan)
        self.assertEqual(backend.diarize_calls, [])
        warn = " | ".join(m for lv, _s, m in self.logged if lv == "warn")
        self.assertIn("reason=blocked", warn, warn)

    def test_a_sparse_backend_answer_does_not_break_the_shape(self):
        """后端只给了部分说话人的嵌入 → **不崩**，也不给出错人的向量。

        服务端的 `speakers` 是字典，契约只保证"标签在本次响应内标识一个说话人"。
        拿一个缺向量的标签去 `np.asarray` 会 raise；而"补齐一个零向量"更糟 ——
        零向量与任何人都不像，但**它会被当成一个真人参与聚类与声纹比对**。
        """
        backend = _FakeBackend(turns=[(0.0, 0.5, "SPEAKER_00")],
                               speakers={"SPEAKER_00": [0.1] * 256})
        turns, embs, labels, _plan = self._segment(backend)
        self.assertEqual(labels, ["SPEAKER_00"])
        self.assertEqual(embs.shape, (1, 256))

    def test_a_backend_answer_without_embeddings_is_shape_legal(self):
        """后端一个嵌入都没给（只有时间轴）→ 仍然要给出**形状合法**的空数组。

        `SpeakerRegistry.map` 对空数组走的是 `n == 0` 分支，而对"不是数组的东西"
        会直接 raise（`np.linalg.norm(..., axis=1)`）。所以这里必须是
        `np.zeros((0, dim))`，不能是 `None` 或 `[]`。
        """
        backend = _FakeBackend(turns=[(0.0, 0.5, "SPEAKER_00")], speakers={})
        turns, embs, labels, _plan = self._segment(backend)
        self.assertEqual(labels, [])
        # 维数**保留**（256，后端声明的），只是没有行 —— 这样下游按 dim 判断时
        # 仍然知道"这个后端是 256 维的"，而不是以为它没有空间。
        self.assertEqual(getattr(embs, "shape", None), (0, 256))
        from app.audio.diarize import SpeakerRegistry
        self.assertEqual(SpeakerRegistry().map(embs, labels), {})

    def test_the_vector_space_lock_survives_the_whole_meeting(self):
        """L5：**同一场会议不许混向量空间** —— 锁由会话带着走到每一段。

        真实场景：本场第一段锁到 `ws-A`，第二段那个后端不可用了，只剩 `ws-B` 的。
        正确行为是**这一段没有说话人**，而不是"换个能用的" —— 换了之后前后半场的嵌入
        不可比，而**不可比不报错**，只会认错人（`test_L5_locked_vector_space_never_
        falls_back_to_another_space` 在路由层钉的是同一条，这里钉的是会议这条缝
        有没有把锁带下去）。
        """
        import app.meeting as meeting
        from app.capabilities import CapabilityRouter
        a = _FakeBackend()
        a.backend_id = "backendA"
        a.vector_space_id = "ws-A"
        b = _FakeBackend()
        b.backend_id = "backendB"
        b.vector_space_id = "ws-B"

        router = CapabilityRouter([a, b], settings_get=settings.get)
        session = _session(router)
        _turns, _embs, _labels, plan = meeting._capability_diarize_segment(session, self.wav)
        self.assertEqual(plan["picks"]["diarize.turns"]["backendId"], "backendA")
        self.assertEqual(session.vector_space_id, "ws-A", "分离之后没有锁住向量空间")

        # 第二段：A 掉了，只剩 B
        session.router = CapabilityRouter([b], settings_get=settings.get)
        turns2, _e2, _l2, _plan2 = meeting._capability_diarize_segment(session, self.wav)
        self.assertIsNone(turns2, "跨空间回退了 —— 会把两个人的向量混着比")
        self.assertEqual(session.vector_space_id, "ws-A", "锁被抹掉了")


class SessionDecisionTests(_IsolatedState):
    """**什么情况下才走能力后端** —— 这条决定了"没配后端时行为不变"。"""

    def test_no_backend_configured_means_no_session(self):
        """没配地址**也没配对** → 返回 None → `_transcribe_impl` 走原来那段本地代码。

        **两处都要打桩**：地址有两个来源，配对凭据那条路现在也会让后端"存在"
        （`client_from_settings()`）。只堵设置那一条的话，这台机器哪天真配了对，
        这条用例就会红 —— 而它报的是"没配后端却开了 session"，指不到真正的原因。
        """
        import app.meeting as meeting
        from app.capabilities import credentials, echo_server
        with patch.object(settings, "get",
                          lambda k, d=None: "" if k == "capabilityEchoServerUrl" else d), \
             patch.object(echo_server, "_creds", lambda: None), \
             patch.multiple("app.meeting.db", add_log=lambda *a, **k: None):
            self.assertIsNone(meeting._capability_asr_session(CFG))
        self.assertTrue(callable(credentials.load))     # 别把整个模块的入口打没了

    def test_a_diarize_only_backend_still_opens_a_session(self):
        """**只有分离那一槽有后端可用**时也要开 session（step 4 的判据扩展）。

        这是真实配置：这台机器的转写链路另有安排（本机引擎够用，或上面那段
        `providerAsr`），而说话人分离**只能**靠 ECHO 后端。老判据（只看 `asr.text`）
        在这台机器上会返回 None，**分离就永远接不上后端**。

        用两个替身把这件事说清楚：一个只会转写、一个只会分离。判据要是还盯着
        `asr.text`（落到只会转写那个），这条就会红。
        """
        import app.meeting as meeting
        from app.capabilities import CapabilityRouter
        asr_only = _FakeBackend()
        asr_only.backend_id = "asr-only"
        asr_only.provides = frozenset({"asr.text"})
        dia_only = _FakeBackend()
        dia_only.backend_id = "dia-only"
        dia_only.provides = frozenset({"diarize.turns"})
        router = CapabilityRouter([asr_only, dia_only], settings_get=lambda k, d=None: d)
        cfg = dict(CFG, diarize=True)
        with patch.object(settings, "get",
                          lambda k, d=None: "http://gpu-01:8900"
                          if k == "capabilityEchoServerUrl" else d), \
             patch("app.capabilities.build_default_router", lambda **kw: router), \
             patch.multiple("app.meeting.db", add_log=lambda *a, **k: None):
            got = meeting._capability_asr_session(cfg)
        self.assertIsNotNone(got, "分离那一槽有可用后端，却拿不到 session")
        plan = got.plan()
        self.assertEqual(plan.backend_for("diarize.turns"), "dia-only")
        self.assertEqual(plan.backend_for("speaker.embed"), "",
                         "没有联系人样本时不该把声纹槽列进计划")

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
        logged = []
        real_get = settings.get

        def fake_get(key, default=None):
            if key == "capabilityEchoServerUrl":
                return "http://127.0.0.1:1"        # 配了，但没人监听
            return real_get(key, default)

        with patch.object(settings, "get", fake_get), \
             patch.multiple("app.meeting.db",
                            add_log=lambda level, src, msg: logged.append((level, src, msg))):
            self.assertIsNone(meeting._capability_asr_session(dict(CFG, diarize=True)))
        self.assertTrue([m for lv, src, m in logged
                         if lv == "warn" and "仍走本机引擎" in m],
                        "静默退化了，没留下原因：%s" % logged)
        self.assertTrue(any("原因" in m for _lv, _s, m in logged),
                        "日志里要写出 skip 的原因（连不上 / privacy 不允许 …）")
        self.assertTrue([m for lv, _s, m in logged
                         if lv == "warn" and "不会标说话人" in m],
                        "分离这一槽没有本机兜底，用户必须被告知：%s" % logged)

    def test_a_plan_that_cannot_use_diarize_says_which_reason(self):
        """配了后端、但计划里**用不上分离** → warn 里要带得出权威原因。

        privacy=none（"不出机"）是最常见的一种：`asr.text` 有本机兜底、照旧转写，
        而 `diarize.turns` 在默认链上**没有本机**（§5.1）—— 这场会一个说话人标签
        都不会有。不说清楚，用户只会看到"分离没生效"。
        """
        import app.meeting as meeting
        logged = self._logged()
        self._put(capabilityEchoServerUrl="http://gpu-01:8900",
                  capabilityPrivacy="none")
        self.assertIsNone(meeting._capability_asr_session(dict(CFG, diarize=True)))
        warn = " | ".join(m for lv, _s, m in logged if lv == "warn")
        self.assertIn("blocked", warn, "warn 里要写得出降级原因（这里是 privacy 挡住）：%s" % warn)
        self.assertTrue(any(r in warn for r in SKIP_REASONS), warn)

    def test_a_paired_only_machine_also_falls_back_LOUDLY(self):
        """**只配对、设置里没填地址**的机器，后端不可用时同样要留一句话。

        这条是 2026-09-24 真机联调后读代码发现的：那句告警原来只在
        `capabilityEchoServerUrl` 非空时打，而"只配对、什么都没配"从 §2.7 起
        就是一种**能用状态** —— 于是那台机器掉了后端会**一声不响**地退回本机引擎，
        正是这条告警要防的那件事（现象只是"转写很慢"，本机那条路的日志一切正常）。
        """
        import app.meeting as meeting
        from app.capabilities import echo_server
        from app.capabilities.credentials import BackendCredentials
        logged = []
        real_get = settings.get
        # 用**真的**凭据类型，不用 SimpleNamespace：能力层会问它 `token_fresh()`，
        # 假对象少一个属性就变成另一条错误路径（第一版就踩了，日志是
        # "能力路由不可用…has no attribute 'token_fresh'"，断言看着像没打日志）。
        dead = BackendCredentials(base_url="http://127.0.0.1:1", client_id="cli-x", secret="s")

        def fake_get(key, default=None):
            if key == "capabilityEchoServerUrl":
                return ""                      # 设置里**没填**
            return real_get(key, default)

        with patch.object(settings, "get", fake_get), \
             patch.object(echo_server, "_creds", lambda: dead), \
             patch.multiple("app.meeting.db",
                            add_log=lambda level, src, msg: logged.append((level, src, msg))):
            self.assertIsNone(meeting._capability_asr_session(CFG))
        self.assertTrue([m for lv, src, m in logged
                         if lv == "warn" and "仍走本机引擎" in m],
                        "只配对没填地址的机器静默退化了：%s" % logged)

    def test_configured_counts_pairing_too(self):
        """`echo_server.configured()`：**设置或配对，任一个都算**。"""
        from app.capabilities import echo_server
        from app.capabilities.credentials import BackendCredentials
        with patch.object(echo_server, "_setting", lambda k, d=None: ""), \
             patch.object(echo_server, "_creds", lambda: None):
            self.assertFalse(echo_server.configured())
        with patch.object(echo_server, "_setting", lambda k, d=None: "http://x:1"), \
             patch.object(echo_server, "_creds", lambda: None):
            self.assertTrue(echo_server.configured())
        with patch.object(echo_server, "_setting", lambda k, d=None: ""), \
             patch.object(echo_server, "_creds",
                          lambda: BackendCredentials(base_url="http://y:2", client_id="c",
                                                     secret="s")):
            self.assertTrue(echo_server.configured(), "只配对也算配了")


class StalePlanMustNotSurviveTests(unittest.TestCase):
    """重转一场会时，**上一场的路由结论不许留下来**（2026-09-24 真机现场抓到）。

    现场：先把 privacy 改成 `none`（于是计划阶段就用不上后端）再重转同会议 ——
    `meta.json` 里的 `capability` 还是上一次那份，详情页写着"转写文本 → ECHO 后端"，
    而这一场其实是**本机**转的。页面说的与实际用的不一致，比不显示更糟。
    """

    def test_a_capability_run_writes_the_plan(self):
        from app.meeting import _apply_capability_meta
        plan = {"picks": {"asr.text": {"backendId": "echo-server"}}}
        meta = _apply_capability_meta({}, plan, {"estimated": 2})
        self.assertEqual(meta["capability"], plan)
        self.assertEqual(meta["timestampsKinds"], {"estimated": 2})

    def test_a_local_run_drops_the_previous_plan(self):
        from app.meeting import _apply_capability_meta
        meta = {"capability": {"picks": {"asr.text": {"backendId": "echo-server"}}},
                "timestampsKinds": {"estimated": 2}, "config": {"sttModel": "sensevoice"}}
        _apply_capability_meta(meta, None, {})
        self.assertNotIn("capability", meta, "上一场的执行计划留下来了 —— 页面会说谎")
        self.assertNotIn("timestampsKinds", meta)
        self.assertEqual(meta["config"], {"sttModel": "sensevoice"}, "别的键不能被误删")

    def test_a_plan_without_timestamp_kinds_is_still_recorded(self):
        """有计划、但这一段没拿到时间轴档位（空文本段）→ 只删时间轴那一项。"""
        from app.meeting import _apply_capability_meta
        plan = {"picks": {}}
        meta = {"timestampsKinds": {"estimated": 2}}
        _apply_capability_meta(meta, plan, {})
        self.assertEqual(meta["capability"], plan)
        self.assertNotIn("timestampsKinds", meta)


class MeetingMetaTests(unittest.TestCase):
    """`meeting.meeting_meta()`：会议详情要读的那份"录音当时"的快照。

    这场会实际用了哪个后端**只在这里**（`meta.json` 的 `capability`），
    所以详情页能不能回答那个问题，全看这个函数读得对不对。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-meeting-meta-")
        p = patch("app.meeting.meetings_dir", lambda: self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, name, payload):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
        return name

    def test_it_reads_back_what_the_recorder_wrote(self):
        import app.meeting as meeting
        name = self._write("2026-09-24_15-03-13", {"capability": {"picks": {}},
                                                  "timestampsKinds": {"exact": 2}})
        got = meeting.meeting_meta(name)
        self.assertEqual(got["timestampsKinds"], {"exact": 2})

    def test_missing_or_broken_file_is_an_empty_dict_not_an_exception(self):
        """读不到 / 内容坏了都返回 `{}`：**一场会的详情页不该因为元数据坏了就打不开**。"""
        import app.meeting as meeting
        self.assertEqual(meeting.meeting_meta("没有这场会"), {})
        self.assertEqual(meeting.meeting_meta(""), {})
        self.assertEqual(meeting.meeting_meta(None), {})
        broken = self._write("坏文件", "{这不是 JSON")
        self.assertEqual(meeting.meeting_meta(broken), {})
        notdict = self._write("不是对象", "[1, 2, 3]")
        self.assertEqual(meeting.meeting_meta(notdict), {})

    def test_a_name_with_separators_cannot_escape_the_meetings_root(self):
        """库里的 name 正常不会带路径分隔符，但这条路径是**读文件** —— 花一行挡住它。"""
        import app.meeting as meeting
        outside = os.path.join(self.tmp, "..", "secret")
        os.makedirs(os.path.dirname(outside), exist_ok=True)
        self.assertEqual(meeting.meeting_meta("../../secret"), {})
        self.assertEqual(meeting.meeting_meta("..\\..\\secret"), {})


if __name__ == "__main__":
    unittest.main()
