# -*- coding: utf-8 -*-
"""「能力路由」页签背后的接口与整理逻辑（`app/capability_admin.py` + `/api/capability/*`）。

这一层要回答的是**人能看懂的那几个问题**：
"我配了没有？"、"这台后端能干什么？"、"开会转写现在选的哪个后端？"。
所以用例盯的也是这几件事，而不是内部结构。

两处**必须钉死**的地方：

  * **页签不许自己算"会选中谁"** —— 那个答案只能由 `router.plan` 给出（会议主链路用的
    就是它）。这里只显示事实，`view()` 里任何一处又算一遍都是隐患。
  * **secret 绝不出现在这个接口的任何字节里** —— 它是给浏览器渲染的，而浏览器那侧
    （截图、控制台、发给人的报错）都不该看到后端凭据。

后端的"能干什么"是**探测出来的**（本机看装了哪些引擎、远程问 `/v1/capabilities`），
所以远程那一半这里用桩服务器；配对成功的落盘效果要真的验（读凭据文件）。
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                 # noqa: E402
from fastapi.testclient import TestClient                   # noqa: E402

import app.db as db                                         # noqa: E402
from app import capability_admin                            # noqa: E402
from app.api import router as api_router                     # noqa: E402
from app.capabilities import credentials as cred             # noqa: E402
from app.capabilities import echo_server, pairing            # noqa: E402
from app.capabilities import router as cap_router            # noqa: E402
from app.config import DEFAULTS, settings                    # noqa: E402

# 桩服务器与那段"配对成功"的应答都在配对那批用例里。**故意复用而不是再写一份**：
# 两个文件各写一份桩，就又多了一处"同一件事算两遍"，而它们迟早会不一样。
from tests.test_backend_pairing import _PAIR_OK, _Stub  # noqa: E402


class _Isolated(unittest.TestCase):
    """隔离三样东西：库、凭据文件、以及本机可能填着的后端令牌。

    少隔离任何一样，用例都会去动这台机器真实的 `data/` —— 那是用户正在用的东西。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-capadmin-")
        cls._old_db = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        # 先清掉配置缓存再播种：缓存里可能还留着**上一个测试类**（或这台机器真实库）
        # 的值，而那会把 `capabilityEchoServerUrl` 之类带进来 —— 表现是"没配对却出现了
        # ECHO 后端"这种完全指不到原因的假象。
        settings._cache = None
        settings.seed_defaults()
        settings.update({"capabilityEchoServerUrl": "",
                         "capabilityEchoServerToken": "",
                         "capabilityEchoServerStaticToken": ""})
        cls.client = TestClient(_app())

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old_db
        settings._cache = None

    @property
    def _cred_file(self):
        return os.path.join(self.tmp, "backend.json")

    def setUp(self):
        # 每个用例都从一个"这台机器没配过后端"的状态开始。
        # **配对用例会把凭据写到 `self.tmp` 里，而这个目录是整个类共用的** ——
        # 不清的话，按字母序排在后面的用例会看到前一个用例配好的凭据
        # （实测：`test_view_has_everything_the_tab_needs` 因此报"还没配对呢"为假）。
        for p in (self._cred_file, self._cred_file + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass
        p = mock.patch.object(cred, "credentials_path", lambda: self._cred_file)
        p.start()
        self.addCleanup(p.stop)
        # 本机设置里万一填着令牌，会让"配对凭据"那条路被整个绕过 —— 用例要的是后者
        p2 = mock.patch.object(echo_server, "_token_from_settings", lambda: "")
        p2.start()
        self.addCleanup(p2.stop)

    def stub(self, script):
        s = _Stub(script)
        self.addCleanup(s.stop)
        return s


def _app():
    app = FastAPI()
    app.include_router(api_router)
    return app


class ViewTests(_Isolated):
    def test_view_has_everything_the_tab_needs(self):
        v = capability_admin.view()
        json.dumps(v, ensure_ascii=False)          # 面板要能直接下发
        for key in ("pair", "privacy", "backends", "choices", "backendLabels",
                    "slotLabels"):
            self.assertIn(key, v)
        self.assertTrue(v["backends"], "至少要看得见本机后端")
        self.assertFalse(v["pair"]["paired"], "还没配对呢")

    def test_the_local_backend_is_always_listed(self):
        ids = [b["backendId"] for b in capability_admin.view()["backends"]]
        self.assertIn("local", ids)

    def test_every_backend_row_carries_a_label_and_its_slots(self):
        """面板上不能出现 `asr.timestamps` 这种内部词汇 —— 人看的是"句级时间轴"。"""
        for b in capability_admin.view()["backends"]:
            self.assertTrue(b["label"], b)
            self.assertNotEqual(b["label"], b["backendId"], "没翻成中文：%s" % b)
            for s in b["slotsLabeled"]:
                self.assertNotEqual(s["label"], s["slot"])

    def test_the_echo_backend_is_absent_until_it_is_configured_or_paired(self):
        """两个来源都没有时**不造这个后端** —— 造一个必然失败的只会让人以为它坏了。"""
        ids = [b["backendId"] for b in capability_admin.view()["backends"]]
        self.assertNotIn("echo-server", ids)

    def test_merely_pairing_is_enough_for_the_backend_to_show_up(self):
        """**配对就够了**（地址随凭据一起来）。

        这条钉的是一个真缺口：`build_default_router` 原来只看 `capabilityEchoServerUrl`，
        于是"只配对、什么都没配"的机器根本不会把后端交给路由 ——
        面板显示配好了，会议却还是走本机。
        """
        s = self.stub([(200, _PAIR_OK)])
        assert capability_admin.pair(s.url, "ABC123")[0]
        ids = [b["backendId"] for b in capability_admin.view()["backends"]]
        self.assertIn("echo-server", ids)

    def test_the_view_never_carries_the_secret(self):
        s = self.stub([(200, _PAIR_OK)])
        capability_admin.pair(s.url, "ABC123")
        text = json.dumps(capability_admin.view(), ensure_ascii=False)
        self.assertNotIn(_PAIR_OK["secret"], text)

    def test_a_dead_backend_does_not_break_the_view(self):
        """后端连不上时，页签要照样能开 —— 那一格显示"连不上"，不是整页 500。"""
        s = self.stub([(200, _PAIR_OK)])
        capability_admin.pair(s.url, "ABC123")
        s.stop()                                   # 后端没了
        v = capability_admin.view(force=True)
        row = next(b for b in v["backends"] if b["backendId"] == "echo-server")
        self.assertIn(row["ready"], (False, None))
        self.assertTrue(row["capsError"], "连不上就要说为什么")


class SlotMappingTests(unittest.TestCase):
    """`SETTING_SLOTS`（键 → 槽）与 `router._setting_key_for`（槽 → 键）必须是**同一份**。

    两处各写一遍是这套东西里最容易漂的地方：面板上改了设置，路由却按另一个键去读，
    表现是"改了没用"。所以这里双向对一遍。
    """

    def test_both_directions_agree(self):
        for key, slots in capability_admin.SETTING_SLOTS.items():
            for slot in slots:
                with self.subTest(key=key, slot=slot):
                    self.assertEqual(cap_router._setting_key_for(slot), key)

    def test_every_slot_with_a_setting_is_covered(self):
        """反过来：路由认得的槽，面板上都得有一个地方能改它（否则那个设置项没人能设）。"""
        for slot in ("asr.text", "asr.timestamps", "diarize.turns",
                     "diarize.embeddings", "speaker.embed"):
            key = cap_router._setting_key_for(slot)
            self.assertTrue(key, "%s 没有对应的设置项" % slot)
            self.assertIn(slot, capability_admin.SETTING_SLOTS.get(key, ()),
                          "%s 指向 %s，但 %s 里没列它" % (slot, key, key))

    def test_every_choice_value_has_a_sentence(self):
        """设置里能选的值，界面上都得有一句人话（否则面板会显示成 `echo-server`）。

        **从 `DEFAULTS` 直接读**，不走 `settings.all()` —— 后者会把 `hidden=True` 的项
        整行剔掉，而能力路由这批设置**全是 hidden**（它们由这个页签承载，不作为普通表单行）。
        用 `all()` 的话这里会拿到空列表，用例就成了空转的绿灯。
        """
        for key in capability_admin.SETTING_SLOTS:
            for value in DEFAULTS[key].get("options") or []:
                with self.subTest(key=key, value=value):
                    self.assertIn(value, capability_admin.CHOICE_LABELS)


class EndpointTests(_Isolated):
    def test_get_capability(self):
        r = self.client.get("/api/capability")
        self.assertEqual(r.status_code, 200)
        self.assertIn("backends", r.json())

    def test_probe_is_post_only_and_returns_the_same_shape(self):
        self.assertEqual(self.client.post("/api/capability/probe").status_code, 200)
        self.assertEqual(self.client.get("/api/capability/probe").status_code, 405)

    def test_pair_with_a_bad_address_is_a_400_with_a_sentence(self):
        r = self.client.post("/api/capability/pair",
                             json={"base_url": "10.100.0.24:9", "code": "ABC"})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertIn("连不上", detail)
        self.assertNotIn("Traceback", detail)

    def test_pair_without_a_code_is_a_400(self):
        r = self.client.post("/api/capability/pair",
                             json={"base_url": "http://127.0.0.1:1", "code": ""})
        self.assertEqual(r.status_code, 400)

    def test_pair_succeeds_and_the_credentials_really_land_on_disk(self):
        s = self.stub([(200, _PAIR_OK)])
        r = self.client.post("/api/capability/pair",
                             json={"base_url": s.url, "code": "abc-123",
                                   "client_name": "我的办公本"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["pair"]["paired"])
        self.assertEqual(body["pair"]["clientId"], _PAIR_OK["clientId"])
        stored = cred.load()
        self.assertIsNotNone(stored, "接口说配好了，盘上却没有凭据")
        self.assertEqual(stored.secret, _PAIR_OK["secret"])
        # 顺手确认配对码规整过（抄成小写也能用）
        sent = json.loads(s.requests[0]["body"].decode("utf-8"))
        self.assertEqual(sent["code"], "ABC-123")

    def test_a_rejected_code_comes_back_as_one_readable_line(self):
        s = self.stub([(401, {"code": "unauthorized", "message": "配对码无效、已被使用"})])
        r = self.client.post("/api/capability/pair",
                             json={"base_url": s.url, "code": "NOPE"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("配对码无效", r.json()["detail"])
        self.assertIsNone(cred.load(), "失败了却把凭据写下去了")

    def test_unpair(self):
        s = self.stub([(200, _PAIR_OK)])
        self.client.post("/api/capability/pair", json={"base_url": s.url, "code": "ABC"})
        r = self.client.post("/api/capability/unpair")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["pair"]["paired"])
        self.assertIsNone(cred.load())

    def test_the_api_never_returns_the_secret(self):
        s = self.stub([(200, _PAIR_OK)])
        r = self.client.post("/api/capability/pair", json={"base_url": s.url, "code": "ABC"})
        self.assertNotIn(_PAIR_OK["secret"], r.text)
        self.assertNotIn(_PAIR_OK["secret"], self.client.get("/api/capability").text)


class PairingFingerprintTests(_Isolated):
    """配对串里的 `fp=` 要真的通到接口上（设计 §7.5 ①）。

    `pairing.pair(..., cert_fingerprint=)` 的能力由 `test_backend_tls` 钉着；
    这里钉的是**它有没有被打通到 HTTP 接口** —— 加这个字段之前，`PairBackendIn`
    根本没有它，于是界面上的"配对串带指纹"无处可填，那一层防中间人等于没接上
    （`pairing.py` 里那 25 条用例照样全绿，因为没人从这条路走）。
    """

    def _tls(self):
        from tests.test_backend_tls import _HttpsServer
        srv = _HttpsServer()
        self.addCleanup(srv.stop)
        return srv

    def test_a_matching_fingerprint_pairs_and_pins(self):
        from tests.tls_test_cert import CERT_PEM
        srv = self._tls()
        r = self.client.post("/api/capability/pair",
                             json={"base_url": srv.url, "code": "abc",
                                   "fingerprint": pairing.fingerprint_of(CERT_PEM)})
        self.assertEqual(r.status_code, 200, r.text)
        stored = cred.load()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.cert_fingerprint, pairing.fingerprint_of(CERT_PEM))

    def test_a_mismatched_fingerprint_is_refused_with_one_line(self):
        from tests.tls_test_cert import OTHER_CERT_PEM
        srv = self._tls()
        r = self.client.post("/api/capability/pair",
                             json={"base_url": srv.url, "code": "abc",
                                   "fingerprint": pairing.fingerprint_of(OTHER_CERT_PEM)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("中间人", r.json()["detail"])
        self.assertIsNone(cred.load(), "指纹对不上却把凭据写下去了")

    def test_without_a_fingerprint_it_is_still_tofu(self):
        """老路不许坏：不填 `fp=` = 第一次见谁信谁（证书照样固定下来给下次用）。"""
        from tests.tls_test_cert import CERT_PEM
        srv = self._tls()
        r = self.client.post("/api/capability/pair",
                             json={"base_url": srv.url, "code": "abc"})
        self.assertEqual(r.status_code, 200, r.text)
        stored = cred.load()
        self.assertEqual(stored.cert_fingerprint, pairing.fingerprint_of(CERT_PEM))


class MeetingDetailWiringTests(_Isolated):
    """会议详情接口要把**录音时那份执行计划**带出来（3.0 的最后一环）。

    这里只钉"接口带没带、翻没翻"：`meeting.get_meeting_detail()` 与 `meeting_meta()`
    各自的行为由 `tests/test_meeting_capability.py` 钉着，两处不重复测同一件事。
    """

    def _detail(self, meta):
        from app import meeting
        row = {"id": 1, "name": "2026-09-24_15-03-13", "segments": 2,
               "status": "transcribed", "duration_seconds": 600}
        with mock.patch.object(meeting, "get_meeting_detail", lambda mid: dict(row)), \
             mock.patch.object(meeting, "build_segments", lambda mid: []), \
             mock.patch.object(meeting, "meeting_meta", lambda name: meta):
            return self.client.get("/api/meetings/1")

    def test_it_carries_the_recorded_plan_with_labels(self):
        from app.capabilities.router import Pick, Plan, Skipped
        plan = Plan(picks={"asr.text": Pick("asr.text", "echo-server", "")},
                    skipped=[Skipped("asr.text", "local", "absent", "没装")]).as_dict()
        r = self._detail({"capability": plan, "timestampsKinds": {"exact": 2}})
        self.assertEqual(r.status_code, 200, r.text)
        cap = r.json()["capability"]
        self.assertEqual(cap["picks"][0]["backendLabel"], "ECHO 后端")
        self.assertEqual(cap["picks"][0]["slotLabel"], "转写文本")
        self.assertIn("不在位", cap["skipped"][0]["reasonLabel"])
        self.assertEqual(cap["timestampsKinds"], {"exact": 2})

    def test_a_meeting_without_a_recorded_plan_says_none(self):
        """老会议、或者走本机回退那条路：没有这段信息就是 `None`（不是空壳）。"""
        r = self._detail({})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["capability"])

    def test_a_corrupt_meta_json_does_not_break_the_detail_page(self):
        """`meta.json` 被手改坏 → 详情页照样打得开（只是没有那段信息）。"""
        r = self._detail({"capability": "这不是字典", "timestampsKinds": "也不是"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["capability"])


class PlanSummaryTests(unittest.TestCase):
    """`plan_summary()`：把会议里记下的执行计划翻成人话。

    输入是**录音当时的快照**（`meta.json` 里的 `Plan.as_dict()`），所以用例直接用
    那个函数生成输入，而不是手写一个"我以为的形状" —— 手写的迟早与真形状漂开，
    然后翻译静默出错（面板照常渲染，只是显示的东西不对）。
    """

    def _plan(self):
        from app.capabilities.router import Pick, Plan, Skipped
        return Plan(picks={"asr.text": Pick("asr.text", "echo-server", "优先"),
                           "diarize.turns": Pick("diarize.turns", "local", "")},
                    skipped=[Skipped("asr.text", "local", "absent", "没装 whisper"),
                             Skipped("diarize.turns", "echo-server", "blocked", "privacy=lan")],
                    candidates={"asr.text": ["echo-server"]},
                    vector_space_id="ws-x", notes=["第一次尝试失败，换了后端"]).as_dict()

    def test_it_labels_slots_backends_and_reasons(self):
        out = capability_admin.plan_summary(self._plan(), {"exact": 3, "estimated": 1})
        self.assertIsNotNone(out)
        picked = {p["slot"]: p for p in out["picks"]}
        self.assertEqual(picked["asr.text"]["backendLabel"], "ECHO 后端")
        self.assertEqual(picked["diarize.turns"]["backendLabel"], "本机")
        self.assertNotEqual(picked["asr.text"]["slotLabel"], "asr.text", "槽没翻成中文")
        reasons = {s["backendId"]: s for s in out["skipped"]}
        self.assertIn("不在位", reasons["local"]["reasonLabel"])
        self.assertIn("策略", reasons["echo-server"]["reasonLabel"])
        self.assertEqual(out["timestampsKinds"], {"exact": 3, "estimated": 1})
        self.assertIn("精确", out["timestampsLabel"])
        self.assertIn("估算", out["timestampsLabel"])

    def test_it_keeps_the_details_the_panel_may_need(self):
        """`candidates` / `notes` / `vectorSpaceId` 原样带上（服务端不预判哪些值得留）。"""
        out = capability_admin.plan_summary(self._plan(), None)
        self.assertEqual(out["candidates"], {"asr.text": ["echo-server"]})
        self.assertEqual(out["notes"], ["第一次尝试失败，换了后端"])
        self.assertEqual(out["vectorSpaceId"], "ws-x")

    def test_unknown_reason_falls_back_to_the_raw_token(self):
        """**不许猜翻译**：认不出的原因原样显示，让人能拿去搜。"""
        plan = {"picks": {}, "skipped": [{"slot": "asr.text", "backendId": "local",
                                          "reason": "weird-new-reason", "detail": ""}]}
        out = capability_admin.plan_summary(plan, None)
        self.assertEqual(out["skipped"][0]["reasonLabel"], "weird-new-reason")

    def test_unknown_slot_and_backend_fall_back_too(self):
        """认不出的槽/后端原样显示 —— 但**别拿真存在的槽当"不认识的"**：
        `asr.streaming` 在 `SLOT_LABELS` 里是有中文的（"流式转写"），
        第一版用例拿它当未知槽，断言直接被打回来。"""
        plan = {"picks": {"asr.weird-thing": {"backendId": "brand-new-backend"}},
                "skipped": []}
        out = capability_admin.plan_summary(plan, None)
        self.assertEqual(out["picks"][0]["slotLabel"], "asr.weird-thing")
        self.assertEqual(out["picks"][0]["backendLabel"], "brand-new-backend")

    def test_every_authoritative_reason_has_a_sentence(self):
        """权威十词**每一个**都要有中文 —— 少一个，界面上就会出现英文 token。

        防的是"加了新错误码/新原因，却忘了配文案"：词汇表在 `base.SKIP_REASONS`，
        翻译在这里，两处必须一起长。
        """
        from app.capabilities.base import SKIP_REASONS
        missing = sorted(set(SKIP_REASONS) - set(capability_admin.REASON_LABELS))
        self.assertEqual(missing, [], "这些降级原因没有中文文案：%s" % missing)

    def test_every_timestamps_kind_has_a_sentence(self):
        from app.capabilities import assemble
        kinds = (assemble.TIMESTAMPS_EXACT, assemble.TIMESTAMPS_ALIGNED,
                 assemble.TIMESTAMPS_ESTIMATED, assemble.TIMESTAMPS_NONE)
        missing = [k for k in kinds if k not in capability_admin.TIMESTAMPS_LABELS]
        self.assertEqual(missing, [], "这些档位没有中文文案：%s" % missing)

    def test_nothing_recorded_is_none_not_an_empty_shell(self):
        """老会议 / 走本机回退那条路没有这段信息 → `None`，不是空壳。

        空壳会让面板渲染出"用了谁：无"，看着像出了问题。
        """
        self.assertIsNone(capability_admin.plan_summary(None, None))
        self.assertIsNone(capability_admin.plan_summary({}, {}))
        self.assertIsNone(capability_admin.plan_summary({"picks": {}, "skipped": []}, None))

    def test_it_never_raises_on_a_hand_edited_file(self):
        """`meta.json` 是盘上的文件，可能被人手改坏 —— 翻译**不许炸**（炸了就是详情页打不开）。"""
        for junk in ({"picks": "not-a-dict"}, {"skipped": [None, 1, "x"]},
                     {"picks": {"asr.text": "b"}}, {"skipped": "nope"},
                     {"picks": {"asr.text": {"backendId": None}}}):
            with self.subTest(junk=junk):
                capability_admin.plan_summary(junk, {"exact": "not-a-number"})


if __name__ == "__main__":
    unittest.main()
