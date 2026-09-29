# -*- coding: utf-8 -*-
"""能力层的在线转写后端（`asr-provider`，会议转写方案 3）的契约。

**别与 `tests/test_asr_provider.py` 混了**：那一份测的是 P5 的 `providerAsr`
（provider 注册表那条老路，OpenAI 兼容 `/audio/transcriptions`）。这一份测的是
**能力路由**里的第三个后端（`app/capabilities/asr_provider.py`，千问AI平台异步 filetrans）。

这一层的价值全在"**请求长什么样、结果怎么翻、整场怎么切**" —— 三件事用假 opener
就能钉死，不需要真联网（真联网只该在集成测试里做一次）。所以这里一个真请求都不发。

要守的（每条都对应一个真实的失败方式）：

* **`parameters` 必须存在** —— 官方明写：省略它任务能提交、但识别会失败；
* **`oss://` 必须带 `X-DashScope-OssResourceResolve: enable`** —— 否则服务端解析不了；
* **不产出嵌入** —— `provides` 里没有 `speaker.embed`，`diarize()` 的 `speakers` 恒为空
  （"认不了联系人"是产品边界，不许被代码悄悄圆过去）；
* **整场只提交一次** —— 一场会 N 段，N 次上传 N 个任务是这套设计的反面；
  切回各段时时间轴要减掉段起点，而标签（`S0`/`S1`）**保持全局**；
* **失败带分类** —— 限流是 `quota`、密钥错是 `blocked`、超时是 `busy`、认不出来的归 `error`。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.capabilities import asr_provider as ap                            # noqa: E402
from app.capabilities.base import SKIP_REASONS, CapabilityError            # noqa: E402


# ---------------------------------------------------------------- 假传输

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTransport:
    """按 URL 子串分发的一台假服务端，并把每个请求原样记下来。"""

    def __init__(self, routes=None):
        self.routes = list(routes or [])
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "method": req.get_method(),
                              "headers": dict(req.headers), "data": req.data})
        for needle, answer in self.routes:
            if needle in req.full_url:
                if callable(answer):
                    answer = answer(req, self)
                if isinstance(answer, Exception):
                    raise answer
                return _Resp(*answer)
        raise AssertionError("假传输没有这条路由：%s" % req.full_url)

    def urls(self):
        return [r["url"] for r in self.requests]


def _policy_body(upload_dir="dashscope-instant/abc/2026-09-28/x"):
    return {"data": {"upload_dir": upload_dir, "upload_host": "https://oss.example.com/up",
                     "oss_access_key_id": "AK", "signature": "SIG", "policy": "POL",
                     "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true"}}


def _result_body(rows, text=""):
    """`rows` = [(起始秒, 结束秒, 文本, speaker_id 或 None), …]"""
    sentences = []
    for i, (a, b, t, spk) in enumerate(rows):
        row = {"begin_time": int(a * 1000), "end_time": int(b * 1000), "text": t,
               "sentence_id": i + 1}
        if spk is not None:
            row["speaker_id"] = spk
        sentences.append(row)
    return {"transcripts": [{"channel_id": 0,
                            "text": text or "".join(r["text"] for r in sentences),
                            "sentences": sentences}],
            "properties": {"original_sampling_rate": 16000,
                           "original_duration_in_milliseconds": 600000}}


def _write_wav(path, seconds, rate=16000):
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


# ---------------------------------------------------------------- 配置默认值

class DefaultsPointAtTheRightPlatformTests(unittest.TestCase):
    """默认值必须指向**千问AI平台**（2026-09-29 用户纠正 + 对文档核过）。

    这一路早先的默认 base_url 写的是 `dashscope.aliyuncs.com`（阿里云百炼）——
    两者接口形状几乎一样、域名与 API Key **都不通用**，默认值错了的表现是
    "填了密钥却一直失败"，而且报错看起来像密钥问题。所以在这里钉死。
    """

    def test_default_base_url_is_the_qianwen_platform(self):
        self.assertEqual(ap.DEFAULT_BASE_URL, "https://maas.qianwenaiapi.com")

    def test_default_model_is_the_filetrans_one_with_diarization(self):
        """方案 3 要的是"整场异步 + 说话人分离"那一支。

        `qwen3-asr-flash-filetrans` 那支的请求 schema 里**没有** `diarization_enabled`，
        换过去会静默丢掉说话人 —— 所以默认模型也钉住。
        """
        self.assertEqual(ap.DEFAULT_MODEL, "qwen-audio-3.1-asr-flash-filetrans")

    def test_an_empty_setting_falls_back_to_the_platform(self):
        real = ap._setting
        ap._setting = lambda name, default="": ""      # 什么都没配
        try:
            self.assertEqual(ap.config()["baseUrl"], "https://maas.qianwenaiapi.com")
        finally:
            ap._setting = real

    def test_a_configured_url_still_wins(self):
        real = ap._setting
        ap._setting = lambda name, default="": ("https://gw.internal/asr"
                                                if name == "capabilityAsrProviderBaseUrl" else "")
        try:
            self.assertEqual(ap.config()["baseUrl"], "https://gw.internal/asr")
        finally:
            ap._setting = real


# ---------------------------------------------------------------- 结果解析

class ParseResultTests(unittest.TestCase):
    def test_milliseconds_become_seconds_and_turns_come_from_speaker_id(self):
        body = _result_body([(0.1, 3.82, "你好。", 0), (3.82, 6.5, "好的。", 1)])
        text, sentences, turns = ap.parse_result(body)
        self.assertEqual(sentences, [(0.1, 3.82, "你好。"), (3.82, 6.5, "好的。")])
        self.assertEqual(turns, [(0.1, 3.82, "S0"), (3.82, 6.5, "S1")])
        self.assertIn("你好。", text)

    def test_no_speaker_id_means_no_turns(self):
        """**没开分离就不许编说话人** —— 编出来的标签会让下游以为"已经分过了"。"""
        _t, _s, turns = ap.parse_result(_result_body([(0.0, 1.0, "在。", None)]))
        self.assertEqual(turns, [])

    def test_multiple_channels_are_joined_in_order(self):
        text, _s, _t = ap.parse_result({"transcripts": [{"text": "左"}, {"text": "右"}]})
        self.assertEqual(text, "左右")


# ---------------------------------------------------------------- 整场拼接

class ConcatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-cap-asr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.a = _write_wav(os.path.join(self.tmp, "01.wav"), 2.0)
        self.b = _write_wav(os.path.join(self.tmp, "02.wav"), 3.0)

    def test_offsets_are_cumulative(self):
        offs = ap._segment_offsets([self.a, self.b])
        self.assertEqual([os.path.basename(p) for p, _s, _e in offs], ["01.wav", "02.wav"])
        self.assertAlmostEqual(offs[0][1], 0.0)
        self.assertAlmostEqual(offs[0][2], 2.0, delta=0.01)
        self.assertAlmostEqual(offs[1][1], 2.0, delta=0.01)
        self.assertAlmostEqual(offs[1][2], 5.0, delta=0.01)

    def test_concat_keeps_params_and_frames(self):
        dst = os.path.join(self.tmp, "whole.wav")
        ap.concat_wavs([self.a, self.b], dst)
        with wave.open(dst, "rb") as f:
            self.assertEqual((f.getframerate(), f.getnchannels()), (16000, 1))
            self.assertAlmostEqual(f.getnframes() / 16000.0, 5.0, delta=0.01)

    def test_mismatched_segments_are_refused(self):
        """格式不一致**当场拒绝**：拼出一条坏音频比失败更糟（结果会静默错位）。"""
        c = _write_wav(os.path.join(self.tmp, "03.wav"), 1.0, rate=8000)
        with self.assertRaises(CapabilityError) as cm:
            ap.concat_wavs([self.a, c], os.path.join(self.tmp, "bad.wav"))
        self.assertEqual(cm.exception.reason, "unsupported")

    def test_slice_keeps_global_labels_and_shifts_times(self):
        task = ap._MeetingTask(
            offsets=[(self.a, 0.0, 2.0), (self.b, 2.0, 5.0)],
            sentences=[(0.5, 1.5, "第一段"), (2.5, 3.5, "第二段"), (4.0, 4.5, "还在第二段")],
            turns=[(0.5, 1.5, "S0"), (2.5, 3.5, "S1"), (4.0, 4.5, "S0")],
            text="第一段第二段还在第二段")
        text, sents, turns = task.slice_for(self.b)
        self.assertEqual(sents, [(0.5, 1.5, "第二段"), (2.0, 2.5, "还在第二段")])
        self.assertEqual(turns, [(0.5, 1.5, "S1"), (2.0, 2.5, "S0")])
        self.assertEqual(text, "第二段还在第二段")


# ---------------------------------------------------------------- 三道门

class UploadAndSubmitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"baseUrl": "https://maas.qianwenaiapi.com", "apiKey": "sk-test",
                    "model": ap.DEFAULT_MODEL}
        self.tmp = tempfile.mkdtemp(prefix="echo-cap-asr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wav = _write_wav(os.path.join(self.tmp, "01.wav"), 1.0)

    def test_policy_request_shape(self):
        t = FakeTransport([("/api/v1/uploads", (200, _policy_body()))])
        data = ap.get_upload_policy(self.cfg, opener=t)
        self.assertEqual(data["upload_host"], "https://oss.example.com/up")
        url = t.urls()[0]
        self.assertIn("action=getPolicy", url)
        self.assertIn("model=" + ap.DEFAULT_MODEL, url)
        self.assertEqual(t.requests[0]["headers"].get("Authorization"), "Bearer sk-test")

    def test_policy_without_upload_host_is_an_error(self):
        t = FakeTransport([("/api/v1/uploads", (200, {"data": {}}))])
        with self.assertRaises(CapabilityError):
            ap.get_upload_policy(self.cfg, opener=t)

    def test_upload_sends_the_seven_fields_and_returns_oss_url(self):
        t = FakeTransport([("oss.example.com", (200, b""))])
        url = ap.upload_file(_policy_body()["data"], self.wav, opener=t)
        self.assertTrue(url.startswith("oss://dashscope-instant/"))
        self.assertTrue(url.endswith("/01.wav"))
        body = t.requests[0]["data"]
        for field in (b"OSSAccessKeyId", b"Signature", b"policy", b"x-oss-object-acl",
                      b"x-oss-forbid-overwrite", b"key", b"success_action_status",
                      b'filename="01.wav"'):
            self.assertIn(field, body, "multipart 里缺 %r" % field)

    def test_oversize_file_is_refused_before_uploading(self):
        t = FakeTransport([])
        real = ap.MAX_UPLOAD_BYTES
        ap.MAX_UPLOAD_BYTES = 10
        try:
            with self.assertRaises(CapabilityError) as cm:
                ap.upload_file(_policy_body()["data"], self.wav, opener=t)
        finally:
            ap.MAX_UPLOAD_BYTES = real
        self.assertEqual(cm.exception.reason, "unsupported")
        self.assertEqual(cm.exception.code, "payload_too_large")
        self.assertEqual(t.requests, [], "超限时一个字节都不该传")

    def test_the_stricter_of_the_two_limits_wins(self):
        """上限取"凭证里按模型给的那个"和我们的硬上限里**小的那个**（2026-09-29）。

        官方凭证里带 `max_file_size_mb`（按模型给，示例 100MB），而 filetrans 侧能到 2GB。
        只按我们自己的 1GB 判，会在小模型上"传上去才发现不认" —— 那是最费时间的一种失败。
        """
        policy = _policy_body()["data"]
        policy["max_file_size_mb"] = 1          # 模型只让传 1MB
        big = _write_wav(os.path.join(self.tmp, "big.wav"), 50.0)   # ≈1.6MB，超过它
        t = FakeTransport([])
        with self.assertRaises(CapabilityError) as cm:
            ap.upload_file(policy, big, opener=t)
        self.assertEqual(cm.exception.code, "payload_too_large")
        self.assertIn("1 MB", str(cm.exception.detail or cm.exception))
        self.assertEqual(t.requests, [], "超模型上限时一个字节都不该传")

    def test_a_generous_policy_limit_does_not_raise_our_own_ceiling(self):
        """反过来：凭证说能传 4GB，也不许把我们自己的 1GB 上限顶掉（那是内存/时间保护）。"""
        policy = _policy_body()["data"]
        policy["max_file_size_mb"] = 4096
        t = FakeTransport([("oss.example.com", (200, b""))])
        ap.upload_file(policy, self.wav, opener=t)      # 小文件照常传
        self.assertEqual(len(t.requests), 1)

    def test_submit_has_parameters_and_the_two_mandatory_headers(self):
        t = FakeTransport([("/transcription", (200, {"output": {"task_id": "T1"}}))])
        self.assertEqual(ap.submit(self.cfg, "oss://x/01.wav", opener=t), "T1")
        req = t.requests[0]
        # 头名**按文档的驼峰形式发出去**（urllib 默认会 capitalize，见 `_request` 里的说明）
        self.assertEqual(req["headers"].get("X-DashScope-Async"), "enable")
        self.assertEqual(req["headers"].get("X-DashScope-OssResourceResolve"), "enable")
        body = json.loads(req["data"].decode())
        self.assertIn("parameters", body, "官方明写：省略 parameters → 提交成功但识别失败")
        self.assertTrue(body["parameters"], "parameters 不能是空对象以外的形状")
        self.assertEqual(body["parameters"]["channel_id"], [0])
        self.assertTrue(body["parameters"]["diarization_enabled"])
        self.assertEqual(body["input"]["file_urls"], ["oss://x/01.wav"])

    def test_submit_without_key_is_absent(self):
        with self.assertRaises(CapabilityError) as cm:
            ap.submit({"baseUrl": "https://x", "apiKey": "", "model": "m"}, "oss://x")
        self.assertEqual(cm.exception.reason, "absent")

    def test_http_error_codes_map_to_the_authority_vocabulary(self):
        for code, want in (("Throttling", "quota"), ("InvalidApiKey", "blocked"),
                           ("InvalidParameter", "unsupported"), ("Weird", "error")):
            with self.subTest(code=code):
                t = FakeTransport([("/transcription", (400, {"code": code, "message": "x"}))])
                with self.assertRaises(CapabilityError) as cm:
                    ap.submit(self.cfg, "oss://x", opener=t)
                self.assertEqual(cm.exception.reason, want)
                self.assertIn(cm.exception.reason, SKIP_REASONS)


class PollTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"baseUrl": "https://maas.qianwenaiapi.com", "apiKey": "sk",
                    "model": ap.DEFAULT_MODEL}

    def test_polls_until_succeeded(self):
        seq = ["PENDING", "RUNNING", "SUCCEEDED"]

        def answer(req, t):
            seen = len([r for r in t.requests if "/tasks/" in r["url"]])
            state = seq[min(seen, len(seq)) - 1]
            extra = ({"results": [{"transcription_url": "https://r/1.json",
                                   "subtask_status": "SUCCEEDED"}]}
                     if state == "SUCCEEDED" else {})
            return (200, {"output": dict({"task_status": state}, **extra)})

        t = FakeTransport([("/tasks/", answer)])
        out = ap.poll(self.cfg, "T1", interval=0.01, opener=t, sleep=lambda _s: None)
        self.assertEqual(out["task_status"], "SUCCEEDED")
        self.assertEqual(ap._result_url(out), "https://r/1.json")
        self.assertEqual(len([u for u in t.urls() if "/tasks/" in u]), 3)

    def test_timeout_says_busy_and_is_retryable(self):
        t = FakeTransport([("/tasks/", (200, {"output": {"task_status": "RUNNING"}}))])
        with self.assertRaises(CapabilityError) as cm:
            ap.poll(self.cfg, "T1", timeout=0.0, opener=t, sleep=lambda _s: None)
        self.assertEqual(cm.exception.reason, "busy")
        self.assertTrue(cm.exception.retryable)

    def test_failed_subtask_reports_its_own_code(self):
        """子任务失败要把它自己的 code 带出来（`FAILED` 不是错误码，`FILE_DOWNLOAD_FAILED` 才是）。"""
        with self.assertRaises(CapabilityError) as cm:
            ap._result_url({"task_status": "SUCCEEDED",
                            "results": [{"subtask_status": "FAILED",
                                         "code": "FILE_DOWNLOAD_FAILED",
                                         "message": "cannot download"}]})
        self.assertEqual(cm.exception.code, "FILE_DOWNLOAD_FAILED")
        self.assertIn("cannot download", cm.exception.detail)


# ---------------------------------------------------------------- 后端（整场一次）

def _full_transport(rows=None):
    """一台假服务端：三道门 + 结果下载全给上，并记录请求。"""
    rows = rows or [(0.5, 1.5, "第一段", 0), (2.5, 3.5, "第二段", 1), (4.0, 4.5, "还在第二段", 0)]
    t = FakeTransport()

    def query(req, tt):
        return (200, {"output": {"task_status": "SUCCEEDED",
                                 "results": [{"transcription_url": "https://r/res.json",
                                              "subtask_status": "SUCCEEDED"}]}})

    t.routes = [("/api/v1/uploads", (200, _policy_body())),
                ("oss.example.com", (200, b"")),
                ("/transcription", (200, {"output": {"task_id": "T-%d" % len(t.requests)}})),
                ("/tasks/", query),
                ("https://r/res.json", (200, _result_body(rows)))]
    return t


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-cap-asr-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.seg1 = _write_wav(os.path.join(self.tmp, "01.wav"), 2.0)
        self.seg2 = _write_wav(os.path.join(self.tmp, "02.wav"), 3.0)

    def _client(self, transport):
        return ap.DashScopeAsrClient(base_url="https://maas.qianwenaiapi.com",
                                     api_key="sk-test", model=ap.DEFAULT_MODEL,
                                     opener=transport, tmp_dir=self.tmp)

    def test_it_does_not_claim_speaker_embed(self):
        c = self._client(FakeTransport())
        self.assertEqual(c.provides, frozenset({"asr.text", "asr.timestamps", "diarize.turns"}))
        self.assertNotIn("speaker.embed", c.provides)
        self.assertEqual(c.source, "wan")
        self.assertEqual(c.vector_space_id, ap.VECTOR_SPACE_ID)

    def test_one_task_per_meeting_and_per_segment_slicing(self):
        t = _full_transport()
        c = self._client(t)
        first = c.transcribe(self.seg1, want_timestamps=True)
        self.assertEqual(first.text, "第一段")
        self.assertEqual(first.sentences, ((0.5, 1.5, "第一段"),))
        self.assertEqual(first.timestamps, "exact")
        second = c.transcribe(self.seg2)
        self.assertEqual([s[2] for s in second.sentences], ["第二段", "还在第二段"])
        c.diarize(self.seg1)                     # 分离那一槽问的是同一段 → 不该再来一次
        self.assertEqual(len([u for u in t.urls() if u.endswith(ap.SUBMIT_PATH)]), 1,
                         "一场会只该提交一次（N 段 → 1 个任务）")
        self.assertEqual(len([u for u in t.urls() if u.startswith("https://oss.example.com")]), 1,
                         "一场会只该上传一次")

    def test_diarize_returns_global_labels_with_no_embeddings(self):
        c = self._client(_full_transport())
        d2 = c.diarize(self.seg2)
        self.assertEqual([x[2] for x in d2.turns], ["S1", "S0"],
                         "标签是整场的编号，不按段重新编号")
        self.assertEqual(d2.speakers, {}, "不返回嵌入 —— 不许编一个")
        self.assertEqual(d2.vector_space_id, ap.VECTOR_SPACE_ID)
        self.assertEqual([round(x[0], 1) for x in d2.turns], [0.5, 2.0],
                         "时间轴要减掉段起点")

    def test_ready_only_needs_a_key(self):
        self.assertTrue(self._client(FakeTransport()).ready())
        self.assertFalse(ap.DashScopeAsrClient(base_url="https://x", api_key="").ready())

    def test_describe_says_it_cannot_recognize_contacts(self):
        out = self._client(FakeTransport()).describe()
        self.assertIn("认不了联系人", out["note"])
        self.assertTrue(out["hasKey"])

    def test_no_key_means_no_client(self):
        real = ap._setting
        ap._setting = lambda name, default="": ""
        try:
            self.assertIsNone(ap.client_from_settings())
            self.assertFalse(ap.configured())
        finally:
            ap._setting = real

    def test_key_means_a_client(self):
        real = ap._setting
        ap._setting = lambda name, default="": ("sk-x" if name.endswith("ApiKey") else "")
        try:
            self.assertTrue(ap.configured())
            self.assertIsInstance(ap.client_from_settings(), ap.DashScopeAsrClient)
        finally:
            ap._setting = real


class RouterWiringTests(unittest.TestCase):
    """**配了才造**：没填密钥时路由里根本没有这一路（不造空壳）。"""

    def _router_ids(self):
        from unittest.mock import patch as _patch
        from app.capabilities import echo_server
        from app.capabilities.router import build_default_router
        with _patch("app.capabilities.echo_server.client_from_settings", lambda *a, **k: None):
            return {c.backend_id for c in build_default_router().clients()}

    def test_no_key_means_the_route_does_not_exist(self):
        real = ap._setting
        ap._setting = lambda name, default="": ""
        try:
            self.assertNotIn("asr-provider", self._router_ids())
        finally:
            ap._setting = real

    def test_key_means_the_route_exists_and_declares_its_slots(self):
        real = ap._setting
        ap._setting = lambda name, default="": ("sk-x" if name.endswith("ApiKey") else "")
        try:
            ids = self._router_ids()
        finally:
            ap._setting = real
        self.assertIn("asr-provider", ids)


class MeetingLabelTests(unittest.TestCase):
    """**有轮次、没嵌入**时会议那一侧怎么办（在线转写的形状）。

    2026-09-28 之前：`_normalize_diarize` 回空 `labels`，会议那句
    `registry.map(embs, labels)` 给出空映射，紧接着 `key_map[spk]` **KeyError** ——
    也就是说"后端给了说话人时间轴但没有嵌入"这条路一走进来就炸。
    """

    def test_labels_come_from_the_backend_numbering(self):
        from app import meeting
        rows = [(0.0, 1.0, "S0"), (1.0, 2.0, "S1"), (2.0, 3.0, "S0")]
        self.assertEqual(meeting._labels_from_turns(rows),
                         {"S0": "说话人1", "S1": "说话人2"})

    def test_it_accepts_other_label_spellings(self):
        from app import meeting
        self.assertEqual(meeting._labels_from_turns([(0.0, 1.0, "SPEAKER_00")]),
                         {"SPEAKER_00": "说话人1"})

    def test_no_digits_keeps_the_label_instead_of_guessing(self):
        """抽不到数字就原样用 —— **不许按出现顺序猜编号**（猜错的表现是"看着正常、认错人"）。"""
        from app import meeting
        self.assertEqual(meeting._labels_from_turns([(0.0, 1.0, "甲"), (1.0, 2.0, "乙")]),
                         {"甲": "甲", "乙": "乙"})

    def test_empty_is_empty(self):
        from app import meeting
        self.assertEqual(meeting._labels_from_turns([]), {})
        self.assertEqual(meeting._labels_from_turns(None), {})


if __name__ == "__main__":
    unittest.main()
