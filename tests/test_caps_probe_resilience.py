# -*- coding: utf-8 -*-
"""「**一次探测超时 ≠ 后端不可用**」—— 2026-10-17 真机事故的护栏用例。

## 现场（用户 2026-10-17 报告）

用户停掉一场会议后，面板报「后端不可用…等它恢复后可重新转写」（`waiting-backend`）；
**48 秒后他手动点「重新转写」就成功了**。而那台后端**从头到尾都在线**
（容器 uptime 16.2 小时、https 一直 200）。

## 已核对的根因链（本文件逐段钉住）

1. `app/meeting.py:2499` 每轮转写**新建 router** → `build_default_router()`；
2. → `app/capabilities/router.py:519` 里 `c.refresh()` → `app/capabilities/echo_server.py`
   现场 `GET /v1/capabilities`，**超时 15 秒**（事故当时；现在是 `CAPS_TIMEOUT_S`）；
3. 失败时旧代码 `self.provides = frozenset()` → 于是"这台后端什么都不提供"；
4. `router.plan()` 的 `client.supports(slot)` 全落空 → 会议没有任何槽落到后端
   → `app/meeting.py:2535` 记 `waiting-backend`；
5. 时间完全吻合：13:18:54 停止 → 13:19:09 报错（正好 15 秒）。

## 这一组要钉死的判据

| 情形 | 期望 |
| --- | --- |
| 第 1 次探测超时、第 2 次成功 | **不是** `waiting-backend`；会议照常转写；日志里有那次失败 |
| 探测一直失败（且有上次已知能力、后端还活着） | 沿用上次能力，照常转写；日志写明"沿用" |
| 探测一直失败（明确连不上：健康探针也失败） | **仍如实报** `waiting-backend`（不许吞） |
| 探测成功但能力集为空（它真的不提供） | **仍如实报**（与"探测失败沿用旧值"区分开：`empty` ≠ `unknown`） |
| 能力探测失败 + `privacy=none` 挡住内网后端 | 仍是 `blocked-by-privacy`（"连得上但不许发"的判据不变） |

隔离（照 `tests/test_meeting_waiting_backend.py` 的写法）：临时库 + 临时会议目录 +
临时凭据文件 + **桩服务器**（一个真后端都不打）+ 进程级能力缓存每条用例清空。
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                                       # noqa: E402
from app import meeting as meeting_mod                                   # noqa: E402
from app.capabilities import credentials as cred_mod                     # noqa: E402
from app.capabilities import echo_server                                 # noqa: E402
from app.capabilities.base import CapabilityError                        # noqa: E402
from app.capabilities.echo_server import EchoServerClient                # noqa: E402
from app.capabilities.router import CapabilityRouter, Need               # noqa: E402
from app.config import settings                                          # noqa: E402

#: 一台"只会转写"的后端会宣告的能力（`asr.long` 是服务端自己的词汇，
#: 客户端只认交集里的 `asr.text` —— 正好顺带验一次那条映射没被改坏）。
CAPS_ASR_ONLY = {
    "server": {"id": "gpu-01", "version": "1.2.3"},
    "slots": {"asr.text": {"model": "qwen3asr"}, "asr.long": {"model": "qwen3asr"}},
    "models": [{"id": "qwen3asr", "vectorSpaceId": ""}],
    "limits": {"maxUploadMB": 512},
}

#: 探测成功、但**它明确不提供**任何我们认得的槽。
CAPS_EMPTY = {"server": {"id": "gpu-01"}, "slots": {}, "models": [], "limits": {}}


def _write_wav(path, seconds=1.0):
    """一段**真的** wav（16k 单声道 int16，与 recorder.py 的产出同格式）。"""
    n = int(16000 * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % n, *([0] * n)))


def _client(url="http://gpu-01:8900"):
    """一个**不读这台机器真实设置/凭据**的 ECHO 后端客户端（构造时不联网）。"""
    with patch.object(echo_server, "_setting", lambda k, d=None: ""):
        return EchoServerClient(base_url=url, creds=False)


# ================================================================ ① 客户端：探测策略

class ProbePolicyTests(unittest.TestCase):
    """`EchoServerClient.refresh()` 的判据（打桩 `_request`，一个真后端都不打）。

    这一层盯的是"客户端自己怎么判"：重试、沿用、超时值、状态词。
    下面 `MeetingStillTranscribesTests` 那一层再从会议链路端到端验一遍。
    """

    def setUp(self):
        echo_server.reset_caps_cache()
        self.addCleanup(echo_server.reset_caps_cache)
        self.logs = []
        p = patch.object(echo_server, "_log_probe",
                         lambda level, msg: self.logs.append((level, msg)))
        p.start()
        self.addCleanup(p.stop)
        # 重试间隔在用例里没必要真等（它是给生产用的 0.4 秒）
        p = patch.object(echo_server, "CAPS_RETRY_DELAY_S", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def test_the_probe_timeout_is_not_the_inference_timeout(self):
        """探测用的是**自己那一档**短超时，不是上传/推理那 15 秒起的数字。

        真机现场就是这条 15 秒：一份静态元数据卡了整 15 秒，然后被判定成"后端不可用"。
        """
        self.assertLess(echo_server.CAPS_TIMEOUT_S, 15.0)
        self.assertLess(echo_server.CAPS_TIMEOUT_S, echo_server.UPLOAD_TIMEOUT_S)
        self.assertLess(echo_server.CAPS_TIMEOUT_S, echo_server.INFER_TIMEOUT_S)
        self.assertGreaterEqual(echo_server.CAPS_ATTEMPTS, 2,
                                "至少要有一次快速重试：一次超时不足以判定不可用")

    def test_one_timeout_then_success_is_not_a_failure(self):
        """判据 ①：第 1 次超时、第 2 次成功 → 探测算成功，**不**清空能力集。"""
        c = _client()
        calls = []

        def fake(method, path, **kw):
            calls.append((method, path, kw.get("timeout")))
            if len(calls) == 1:
                raise CapabilityError("offline", "连不上 http://gpu-01:8900（timed out）")
            return 200, CAPS_ASR_ONLY

        with patch.object(c, "_request", side_effect=fake):
            self.assertTrue(c.refresh(force=True))

        self.assertEqual(2, len(calls), "应当**重试一次**再判定")
        self.assertEqual(echo_server.CAPS_TIMEOUT_S, calls[0][2],
                         "探测请求用的必须是探测那一档超时")
        self.assertEqual({"asr.text"}, set(c.provides))
        self.assertEqual(echo_server.CAPS_STATE_OK, c.caps_state)
        self.assertEqual("", c.caps_error)
        # 日志里必须有那次失败：真机上那 15 秒一个字节都没留下，根因只能靠推断
        self.assertTrue([m for _lv, m in self.logs if "第 1/2 次失败" in m], self.logs)

    def test_a_failed_probe_keeps_the_last_known_capabilities(self):
        """判据 ②：探测失败**不许**把 `provides` 清空 —— 沿用上次成功拿到的那一份。"""
        c = _client()
        with patch.object(c, "_request", return_value=(200, CAPS_ASR_ONLY)):
            self.assertTrue(c.refresh(force=True))
        self.assertEqual({"asr.text"}, set(c.provides))

        with patch.object(c, "_request",
                          side_effect=CapabilityError("offline", "timed out")), \
                patch.object(c, "_health_ok", return_value=True):
            self.assertFalse(c.refresh(force=True))

        self.assertEqual({"asr.text"}, set(c.provides),
                         "探测失败把能力集清空 —— 这正是真机上那次 waiting-backend 的成因")
        self.assertTrue(c.supports("asr.text"),
                        "后端还活着（健康探针通）时这一轮要照常派活")
        d = c.describe()
        self.assertEqual(echo_server.CAPS_STATE_STALE, d["capsState"])
        self.assertTrue(d["capsUsable"])
        self.assertTrue(d["capsError"], "失败原因必须能从 /api/capability 看到")
        self.assertIn("沿用", d["capsError"], d["capsError"])
        self.assertTrue([m for _lv, m in self.logs if "沿用上次已知能力" in m], self.logs)

    def test_a_dead_backend_is_not_dispatched_on_stale_capabilities(self):
        """判据 ③：沿用是有前提的 —— 健康探针也失败时，**这一轮不派活**。

        不设这条前提，`provides` 就会"永远沿用"，于是**永远不报**：
        一场 9 段的会议会逐段去撞推理超时（900 秒/段），用户等不到"待转写"。
        """
        c = _client()
        with patch.object(c, "_request", return_value=(200, CAPS_ASR_ONLY)):
            self.assertTrue(c.refresh(force=True))
        with patch.object(c, "_request",
                          side_effect=CapabilityError("offline", "连不上")), \
                patch.object(c, "_health_ok", return_value=False):
            self.assertFalse(c.refresh(force=True))

        self.assertEqual(echo_server.CAPS_STATE_OFFLINE, c.caps_state)
        self.assertFalse(c.supports("asr.text"), "连不上的后端不许拿旧能力去接活")
        # `provides` 仍保留（面板要如实显示"它上次说自己能干什么"），但不派活
        self.assertEqual({"asr.text"}, set(c.provides))

        plan = CapabilityRouter([c], settings_get=lambda k, d=None: d).plan(
            Need(slots=("asr.text",), purpose="meeting", privacy="lan"))
        self.assertEqual("", plan.backend_for("asr.text"))
        skip = [s for s in plan.skipped
                if s.slot == "asr.text" and s.backend_id == "echo-server"][0]
        self.assertEqual("offline", skip.reason,
                         "连不上要报 offline（面板翻成「连不上」），不是 unsupported")
        self.assertIn("用不上它", skip.detail)
        self.assertIn("能力探测失败", skip.detail, "失败原因要带进计划里")

    def test_a_first_probe_failure_is_unknown_not_empty(self):
        """判据 ④前半：**首次就失败** → `unknown`（未知），不是"它不提供"。"""
        c = _client()
        with patch.object(c, "_request",
                          side_effect=CapabilityError("offline", "timed out")):
            self.assertFalse(c.refresh(force=True))

        d = c.describe()
        self.assertEqual(echo_server.CAPS_STATE_UNKNOWN, d["capsState"])
        self.assertFalse(d["capsUsable"])
        self.assertEqual([], d["provides"], "从没成功过时确实没有能力集")
        self.assertIn("首次探测就失败", d["capsError"], d["capsError"])
        self.assertNotEqual(echo_server.CAPS_STATE_EMPTY, d["capsState"],
                            "「没问到」不能被说成「它明确不提供」")

    def test_a_healthy_probe_with_no_slots_is_empty_not_unknown(self):
        """判据 ④后半：探测**成功**、能力集为空 → `empty`（它明确不提供）。

        这条与上一条是**两个不同的结论**：一个是"没问到"，一个是"问到了、它就是没有"。
        混成一个，用户就会被引去查网络（或反过来去换后端）。
        """
        c = _client()
        with patch.object(c, "_request", return_value=(200, CAPS_EMPTY)):
            self.assertTrue(c.refresh(force=True))

        self.assertEqual(echo_server.CAPS_STATE_EMPTY, c.caps_state)
        self.assertEqual("", c.caps_error, "它答话了，没有失败原因")
        plan = CapabilityRouter([c], settings_get=lambda k, d=None: d).plan(
            Need(slots=("asr.text",), purpose="meeting", privacy="lan"))
        self.assertEqual("", plan.backend_for("asr.text"))
        skip = [s for s in plan.skipped
                if s.slot == "asr.text" and s.backend_id == "echo-server"][0]
        self.assertEqual("unsupported", skip.reason)
        self.assertIn("什么都不提供", skip.detail)

    def test_the_last_known_capabilities_expire(self):
        """判据 ⑤：上次成功**太旧**就不再沿用（否则等于"永远不报"）。

        上限本身也是判据：它必须存在且有限。这条用例把"上次成功"改成远超上限，
        探测再失败时状态必须回到 `unknown`、且不派活。
        """
        self.assertGreater(echo_server.CAPS_STALE_MAX_S, 0.0)
        self.assertLess(echo_server.CAPS_STALE_MAX_S, 24 * 3600.0,
                        "上限不该长到「永远沿用」的程度")
        c = _client()
        with patch.object(c, "_request", return_value=(200, CAPS_ASR_ONLY)):
            self.assertTrue(c.refresh(force=True))
        echo_server._caps_cache_put(
            c._caps_key(),
            dict(c._snapshot(), at=time.time() - echo_server.CAPS_STALE_MAX_S - 60.0))

        with patch.object(c, "_request",
                          side_effect=CapabilityError("offline", "timed out")):
            self.assertFalse(c.refresh(force=True))

        self.assertEqual(echo_server.CAPS_STATE_UNKNOWN, c.caps_state)
        self.assertFalse(c.supports("asr.text"))
        self.assertIn("沿用上限", c.caps_error, c.caps_error)


# ================================================================ ② 桩服务器

class _Handler(BaseHTTPRequestHandler):
    """按 `server.responder(method, path, seen)` 回答；可选"先睡一会儿"模拟超时。"""

    def do_GET(self):                                            # noqa: N802
        self._handle("GET")

    def do_POST(self):                                           # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)          # 必须抽干，否则客户端会看到连接被重置
        self._handle("POST")

    def _handle(self, method):
        path = self.path.split("?")[0]
        seen = self.server.requests
        seen.append((method, path))
        spec = self.server.responder(method, path, seen) or {}
        if spec.get("sleep"):
            time.sleep(spec["sleep"])        # 故意拖过客户端超时 → 客户端侧是真超时
        data = json.dumps(spec.get("body") or {}, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(int(spec.get("status") or 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                             # 客户端已经超时走开（正是要模拟的那件事）

    def log_message(self, *a):                                   # 安静
        pass


class _Stub:
    """一个只活在用例里的后端（回环端口 + 后台线程，跑完就关）。"""

    def __init__(self, responder):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.responder = responder
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def requests(self):
        return self.httpd.requests

    def count(self, path):
        return sum(1 for _m, p in self.requests if p == path)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _hit(seen, path):
    return sum(1 for _m, p in seen if p == path)


# ================================================================ ③ 会议链路（端到端）

class _MeetingCase(unittest.TestCase):
    """一场真会议（目录 + meta.json + 库记录 + 真 wav）+ 全套隔离 + 一个桩后端。

    这里**不**打桩 `build_default_router()`：要走的就是"每轮新建 router → 现场探测
    capabilities"那条真路径，否则这条用例证明不了任何事。
    """

    SEGS = 2

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-capsprobe-")
        self.dbdir = tempfile.mkdtemp(prefix="echo-capsprobedb-")
        self._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = self.dbdir
        db.DB_FILE = os.path.join(self.dbdir, "test.db")
        db.init()
        settings._cache = None

        self.meetings_root = os.path.join(self.tmp, "meetings")
        os.makedirs(self.meetings_root, exist_ok=True)
        p = patch.object(meeting_mod, "meetings_dir", lambda: self.meetings_root)
        p.start()
        self.addCleanup(p.stop)

        # 这台机器真配对过也不能漏进来：凭据文件指到临时目录。
        self._cred = os.path.join(self.tmp, "backend.json")
        p = patch.object(cred_mod, "credentials_path", lambda: self._cred)
        p.start()
        self.addCleanup(p.stop)
        # 手填令牌同样不许漏（否则请求会带上 Authorization，桩还得认它）
        p = patch.object(echo_server, "_token_from_settings", lambda: "")
        p.start()
        self.addCleanup(p.stop)

        # 用例不许出声、也不许真去生成纪要（那会去连 DSH）。
        for target, kw in ((meeting_mod.tts_mod, "play_beep"),
                           (meeting_mod.tts_mod, "beep_ok")):
            p = patch.object(target, kw, lambda *a, **k: None)
            p.start()
            self.addCleanup(p.stop)
        for fn in ("request_summary", "request_topic_segments"):
            p = patch.object(meeting_mod, fn, lambda *a, **k: True)
            p.start()
            self.addCleanup(p.stop)

        # 重试间隔与探测超时都调小：判据不变，只是别让用例真等十几秒。
        for name, value in (("CAPS_RETRY_DELAY_S", 0.02), ("CAPS_TIMEOUT_S", 0.25)):
            p = patch.object(echo_server, name, value)
            p.start()
            self.addCleanup(p.stop)

        settings.update({"meetingAutoSummarize": False})
        settings._cache = None
        meeting_mod._state.update(active=False, folder=None, recorder=None,
                                  started_at=None, level=0.0, error="",
                                  paused=False, pauses=[], source="pc", seg_count=0)
        echo_server.reset_caps_cache()

        def _clean():
            meeting_mod._state.update(active=False, folder=None, recorder=None,
                                      paused=False, pauses=[], source="pc", seg_count=0)
            db.DATA_DIR, db.DB_FILE = self._old_db
            settings._cache = None
            echo_server.reset_caps_cache()
            shutil.rmtree(self.tmp, ignore_errors=True)
            shutil.rmtree(self.dbdir, ignore_errors=True)

        self.addCleanup(_clean)

    # ---------------------------------------------------------------- 小工具

    def stub(self, responder):
        s = _Stub(responder)
        self.addCleanup(s.stop)
        return s

    def use_backend(self, url):
        settings.update({"capabilityEchoServerUrl": url, "capabilityPrivacy": "lan"})
        settings._cache = None

    def make_meeting(self, name="2026-10-17_13-18-00", segs=SEGS):
        folder = os.path.join(self.meetings_root, name)
        os.makedirs(folder, exist_ok=True)
        names = ["%02d.wav" % (i + 1) for i in range(segs)]
        for seg in names:
            _write_wav(os.path.join(folder, seg), 1.0)
        meta = {"segments": list(names),
                "start": "2026-10-17T13:08:00",
                "end": "2026-10-17T13:18:54",
                "durationSeconds": float(len(names)),
                "config": {"sttModel": "small", "sttDevice": "auto", "diarize": True},
                "pauses": []}
        with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        mid = db.create_meeting(name, started_at=meta["start"], stt_model="small",
                                stt_device="auto", diarize=1)
        db.update_meeting(mid, segments=len(names),
                          duration_seconds=meta["durationSeconds"])
        return mid, folder

    def meta(self, folder):
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def capability_logs(self):
        """真实落库的能力层日志（不是打桩接住的）—— 判据要求日志里查得到。"""
        return "\n".join(str(r.get("message") or "")
                         for r in db.list_logs(limit=200, source="capability"))


def _responder(caps_status=200, caps_body=None, caps_sleep_at=(),
               health_status=200, asr_text="后端文本。"):
    """一个可配的桩后端：`/v1/capabilities`、`/v1/health`、`/v1/asr`。

    `caps_sleep_at`：**第几次（1 起）** capabilities 请求先睡一会儿（拖过客户端超时）。
    必须逐次点名，不能写成"从第 N 次起"：那样重试也会睡，用例就变成"两次都超时"了
    （两种情形各有用例，别混）。
    """
    body = CAPS_ASR_ONLY if caps_body is None else caps_body
    sleep_at = set(int(x) for x in (caps_sleep_at or ()))

    def responder(method, path, seen):
        if path == "/v1/capabilities":
            if _hit(seen, path) in sleep_at:
                return {"status": caps_status, "body": body, "sleep": 0.8}
            return {"status": caps_status, "body": body}
        if path == "/v1/health":
            return {"status": health_status,
                    "body": {"ok": True, "server": {"id": "gpu-01"}}
                    if health_status == 200 else {"code": "server_error"}}
        if path == "/v1/asr":
            return {"status": 200, "body": {"text": asr_text, "sentences": [],
                                            "timestamps": "none"}}
        return {"status": 404, "body": {"code": "not_found"}}

    return responder


class MeetingStillTranscribesTests(_MeetingCase):
    """真机那种情形：探测那一下没成，但后端一直在线 → 会议必须照常转写。"""

    def test_a_single_probe_timeout_is_retried_and_the_meeting_transcribes(self):
        """判据 ①：**第 1 次探测超时、第 2 次成功** → 不判 waiting-backend、照常转写。"""
        stub = self.stub(_responder(caps_sleep_at=(1,)))
        self.use_backend(stub.url)
        mid, folder = self.make_meeting()

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("transcribed", code,
                         "一次探测超时不许把这一场打成「待转写」\n%s\n%s"
                         % (self.capability_logs(), stub.requests))
        row = db.get_meeting(mid)
        self.assertEqual("transcribed", row["status"], row)
        self.assertEqual(["后端文本。", "后端文本。"],
                         [ln["text"] for ln in db.get_lines(mid)])
        meta = self.meta(folder)
        self.assertNotEqual("waiting-backend", meta.get("transcribeFallbackReason"),
                            "后端一直在线，不许报「在等后端」")
        self.assertEqual("", meta.get("transcribeFallbackReason"))
        self.assertEqual("backend", meta.get("transcribeEngine"))
        # 探测确实重试过（不是"第一次就成功"这种空转）
        self.assertEqual(2, stub.count("/v1/capabilities"))
        # 那次失败必须留在日志里
        self.assertIn("能力探测第 1/2 次失败", self.capability_logs())

    def test_a_persistent_probe_timeout_still_uses_the_last_known_capabilities(self):
        """判据 ②：探测**连续**失败，但健康探针说后端活着 → 沿用上次已知能力照常转写。

        这是真机那条链的精确复现：capabilities 一直超时（两次都超），
        而 `/v1/health`（https 一直 200）证明后端在线 —— 这时判「后端不可用」就是误报。
        """
        stub = self.stub(_responder(caps_sleep_at=(2, 3)))
        # 预热：先成功探测一次（=上一轮转写/面板刚看过），把"上次已知能力"放进进程缓存
        warm = _client(stub.url)
        with patch.object(echo_server, "_setting", lambda k, d=None: ""):
            self.assertTrue(warm.refresh(force=True), warm.caps_error)
        self.use_backend(stub.url)
        mid, folder = self.make_meeting("2026-10-17_13-20-00")

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("transcribed", code)
        row = db.get_meeting(mid)
        self.assertEqual("transcribed", row["status"], row)
        self.assertEqual(["后端文本。", "后端文本。"],
                         [ln["text"] for ln in db.get_lines(mid)])
        meta = self.meta(folder)
        self.assertEqual("", meta.get("transcribeFallbackReason"))
        self.assertEqual("backend", meta.get("transcribeEngine"))
        # 两次探测都超时（一次 + 重试），并且真的问了健康探针
        self.assertGreaterEqual(stub.count("/v1/capabilities"), 3,
                                "预热 1 次 + 这一轮 2 次（首次+重试）")
        self.assertGreaterEqual(stub.count("/v1/health"), 1,
                                "沿用旧能力之前必须先确认后端还活着")
        logs = self.capability_logs()
        self.assertIn("能力探测第 2/2 次失败", logs, logs)
        self.assertIn("沿用上次已知能力", logs, logs)


class GenuinelyUnavailableIsStillReportedTests(_MeetingCase):
    """反向判据：真的不可用/真的不提供时，**必须如实报**（不许为了"不误报"变成"永远不说"）。"""

    def test_a_dead_backend_is_still_reported_as_waiting_backend(self):
        """判据 ③：探测一直失败、健康探针也失败 → 仍 `waiting-backend`，且**不派活**。"""
        stub = self.stub(_responder(caps_status=500, health_status=500))
        self.use_backend(stub.url)
        mid, folder = self.make_meeting("2026-10-17_13-30-00")

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("waiting", code)
        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertIn("后端暂不可用", row["error"] or "")
        self.assertEqual("waiting-backend",
                         self.meta(folder).get("transcribeFallbackReason"))
        self.assertEqual(0, stub.count("/v1/asr"),
                         "连不上的后端不许被派活（否则每段都要等满推理超时）")
        logs = self.capability_logs()
        self.assertIn("echo-server(offline)", logs,
                      "日志要说清是「连不上」，不是「不支持」：%s" % logs)

    def test_a_probe_that_works_but_declares_nothing_is_still_reported(self):
        """判据 ④：探测**成功**但能力集为空 → 仍如实报（且与"沿用旧值"分得开）。"""
        stub = self.stub(_responder(caps_body=CAPS_EMPTY))
        self.use_backend(stub.url)
        mid, folder = self.make_meeting("2026-10-17_13-40-00")

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("waiting", code)
        row = db.get_meeting(mid)
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING, row["status"], row)
        self.assertEqual("waiting-backend",
                         self.meta(folder).get("transcribeFallbackReason"))
        self.assertEqual(0, stub.count("/v1/asr"))
        logs = self.capability_logs()
        self.assertIn("echo-server(unsupported)", logs,
                      "「它明确不提供」要按 unsupported 记，不是 offline：%s" % logs)
        self.assertNotIn("能力探测第", logs,
                         "探测成功了，不该有失败日志：%s" % logs)

    def test_privacy_blocked_is_still_blocked_when_the_probe_failed(self):
        """判据 ⑤：`blocked-by-privacy` 的判据不变 —— "连得上但不许发"仍是它。

        这里**故意让能力探测失败**（caps 500）并让地址算作内网：如果"探测失败"被误当成
        `blocked`，或者 privacy 的判据被这次改动挪了位置，这一场就会变成
        `waiting-backend`（"等它恢复"）—— 而真相是"去改「允许音频去哪」"，等一辈子都不会来。
        """
        stub = self.stub(_responder(caps_status=500, health_status=500))
        # 桩在回环上，但本用例要模拟"内网那台 GPU 机"：只改判据来源，不改实现
        p = patch.object(echo_server, "_is_loopback", lambda url: False)
        p.start()
        self.addCleanup(p.stop)
        settings.update({"capabilityEchoServerUrl": stub.url,
                         "capabilityPrivacy": "none"})
        settings._cache = None
        mid, folder = self.make_meeting("2026-10-17_13-50-00")

        code = meeting_mod._transcribe_impl(folder)

        self.assertEqual("waiting", code)
        meta = self.meta(folder)
        self.assertEqual("blocked-by-privacy", meta.get("transcribeFallbackReason"),
                         "「连得上但不许发」不许被说成「在等后端」")
        self.assertEqual(meeting_mod.MEETING_STATUS_WAITING,
                         db.get_meeting(mid)["status"])
        self.assertIn("「允许音频去哪」", db.get_meeting(mid)["error"] or "")
        self.assertEqual(0, stub.count("/v1/asr"), "许可挡住时一个字节都不许发")


# ================================================================ ④ 面板可见

class TheFailureIsVisibleInTheCapabilityViewTests(_MeetingCase):
    """判据要求：失败原因必须能从**日志或 `/api/capability`** 看到（这里是后者）。"""

    def test_the_capability_view_says_why_the_probe_failed(self):
        from app import capability_admin

        stub = self.stub(_responder(caps_status=500, health_status=500))
        self.use_backend(stub.url)

        v = capability_admin.view(force=True)
        row = next(b for b in v["backends"] if b["backendId"] == "echo-server")
        self.assertEqual("unknown", row["capsState"],
                         "首次探测就失败 → capsState 必须是 unknown（不是「它不提供」）")
        self.assertFalse(row["capsUsable"])
        self.assertIn("能力探测失败", row["capsError"])
        self.assertGreaterEqual(row["capsAttempts"], 2, "面板也要能看出重试过")


if __name__ == "__main__":
    unittest.main()
