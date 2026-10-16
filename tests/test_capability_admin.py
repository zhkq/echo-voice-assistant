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


class AddressPinEndpointTests(_Isolated):
    """`POST /api/capability/address` 换成 https 时**自动补证书固定**（2026-10-16 真机缺口 1）。

    真机现场：公网入口从 http 换成 https 之后，用产品自己的这个接口改地址**立刻失败**
    （`后端 配的是 https，但本机没有它的证书（缺 certPem）——拒绝连接`，`ready=False`），
    那一晚是手工拿 `pair()` 里逐字同一段补的 pin。这里钉的是**接口自己会补**，
    而且补的过程**不动身份**：clientId/secret 逐字不变、桩上也不会再出现第二条 `/v1/pair`
    （scopes 与 hostId 都挂在服务端那条客户端记录上、认的就是 clientId ——
    "没有再配对一次"就是"它们没变"的判据）。

    后端全是本机桩服务器（`_Stub` / `_HttpsServer`），**不碰公网**。
    """

    def _paired_http(self):
        """先用 http 桩配一次 —— 这就是"改地址之前"的状态。"""
        s = self.stub([(200, _PAIR_OK)])
        ok, msg = capability_admin.pair(s.url, "ABC123")
        self.assertTrue(ok, msg)
        return s

    def _tls(self):
        from tests.test_backend_tls import _HttpsServer
        srv = _HttpsServer()
        self.addCleanup(srv.stop)
        return srv

    def test_http_to_https_pins_the_certificate_and_keeps_the_credentials(self):
        """① http 地址改 https：自动补 pin 成功 → 之后 `ready=True`，凭据逐字不变。"""
        from tests.tls_test_cert import CERT_PEM
        s = self._paired_http()
        before = cred.load()
        srv = self._tls()
        r = self.client.post("/api/capability/address", json={"base_url": srv.url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("证书固定下来", r.json()["message"], "补了证书就要说出来")
        after = cred.load()
        self.assertEqual(after.base_url, srv.url)
        self.assertEqual(after.cert_pem.strip(), CERT_PEM.strip(), "证书没落盘")
        self.assertEqual(after.cert_fingerprint, pairing.fingerprint_of(CERT_PEM))
        self.assertEqual(after.client_id, before.client_id, "clientId 不许变")
        self.assertEqual(after.secret, before.secret, "secret 不许变")
        self.assertEqual(after.paired_at, before.paired_at)
        self.assertEqual([q["path"] for q in s.requests], ["/v1/pair"],
                         "改地址又走了一遍 /v1/pair = 换了 clientId（中继 hostId 会跟着废）")
        # 之后**真的能用**：真 https 握手 + 真固定（ready 顺带走一次 /v1/token）
        self.assertTrue(echo_server.EchoServerClient(base_url=after.base_url).ready(),
                        "补上证书之后应当 ready=True")

    def test_an_https_address_whose_certificate_cannot_be_fetched_fails_loudly(self):
        """② https 地址但抓不到证书 → **不静默**：400 + 具体原因，`ready` 仍是 False。"""
        self._paired_http()
        r = self.client.post("/api/capability/address",
                             json={"base_url": "https://127.0.0.1:1"})   # 没人监听
        self.assertEqual(r.status_code, 400, "抓不到证书不许当成功")
        detail = r.json()["detail"]
        self.assertIn("取证书", detail, detail)      # 原因要指得出是**证书**这件事
        self.assertIn("127.0.0.1:1", detail)
        stored = cred.load()
        self.assertEqual(stored.cert_pem, "", "抓不到证书就不许写任何证书")
        self.assertFalse(echo_server.EchoServerClient(base_url=stored.base_url).ready(),
                         "缺证书时 ready 必须是 False（绝不静默跳过校验）")

    def test_http_to_http_does_not_touch_any_certificate(self):
        """③ http→http：行为不变（一个证书都不去取）。"""
        s = self._paired_http()
        before = cred.load()
        calls = []
        with mock.patch.object(pairing, "fetch_cert_pem",
                               lambda url, **kw: calls.append(url) or "PEM"):
            r = self.client.post("/api/capability/address",
                                 json={"base_url": "http://10.0.0.9:8900"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(calls, [], "http→http 不该去取证书")
        after = cred.load()
        self.assertEqual(after.base_url, "http://10.0.0.9:8900")
        self.assertEqual((after.cert_pem, after.cert_fingerprint),
                         (before.cert_pem, before.cert_fingerprint))
        self.assertEqual((after.client_id, after.secret), (before.client_id, before.secret))

    def test_the_same_address_is_still_a_no_op(self):
        """③ 同地址重复改：行为不变（http 那一档连取都不取）。"""
        self._paired_http()
        before = cred.load()
        calls = []
        with mock.patch.object(pairing, "fetch_cert_pem",
                               lambda url, **kw: calls.append(url) or "PEM"):
            r = self.client.post("/api/capability/address",
                                 json={"base_url": before.base_url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("没变", r.json()["message"])
        self.assertEqual(calls, [], "地址没变就不该去取证书")
        self.assertEqual(cred.load().base_url, before.base_url)

    def test_the_same_https_address_with_its_certificate_is_also_a_no_op(self):
        """③ 同地址重复改（https 且证书已在）：还是 no-op —— 不重复取、不重复写。"""
        self._paired_http()
        srv = self._tls()
        self.assertEqual(self.client.post("/api/capability/address",
                                          json={"base_url": srv.url}).status_code, 200)
        calls = []
        with mock.patch.object(pairing, "fetch_cert_pem",
                               lambda url, **kw: calls.append(url) or "PEM"):
            r = self.client.post("/api/capability/address", json={"base_url": srv.url})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("没变", r.json()["message"])
        self.assertEqual(calls, [], "证书已经在、地址也没变，不该再取一次")


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


class BackendSettingsStayOnTheBackendTests(unittest.TestCase):
    """**设置分家**（2026-09-28，设计 §6.6）：客户端只留"连哪个 + 凭据 + 允许音频去哪"。

    后端自己的设置（监听端口 / 显存预算 / 模型档位 / 配额 / TLS / 管理员）**不许**出现在
    客户端设置里 —— 它们已经有一处权威归属（后端的 `server.yaml` 与管理面）。
    在客户端再放一份的代价是"两处都能改、改完不知道谁生效"，而这正是这个仓库反复踩过的坑
    （同一个设置项三处都能改 → 2026-09-19 那轮合并）。客户端只显示**状态**（后端 `health`
    宣告的那些数字）与一个「打开后端管理面」的入口。
    """

    #: 后端专有的词。**刻意不含 `port`** —— 客户端的 `serverPort`（面板端口）与
    #: `harnessPort`（智能体端口）是它自己的东西，不是后端设置。
    BACKEND_ONLY = ("vram", "admin", "tls", "cert", "quota", "budget", "spec")

    def test_no_backend_side_setting_leaks_into_the_client(self):
        from app.config import DEFAULTS
        bad = sorted(k for k in DEFAULTS
                     if any(w in k.lower() for w in self.BACKEND_ONLY))
        self.assertEqual(bad, [], "客户端设置里出现了后端自己的设置项：%s" % bad)

    def test_the_client_keeps_connection_credentials_and_permission(self):
        """客户端该有的就这三类（外加"会议转写走哪条路"与本地配对文件路径）。"""
        from app.config import DEFAULTS
        for key in ("capabilityEchoServerUrl", "capabilityEchoServerToken",
                    "capabilityEchoServerStaticToken", "capabilityPrivacy",
                    "capabilityLocalPairPath"):
            with self.subTest(key=key):
                self.assertIn(key, DEFAULTS, "客户端少了 %s —— 连接/凭据/许可这三类要齐" % key)


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


class BlockedByPrivacyIsNotUnreachableTests(unittest.TestCase):
    """**被许可挡住 ≠ 连不上**（2026-09-29 用户拍板 A 的配套）。

    会议侧从这一档起会把两种情形记成两个词（`blocked-by-privacy` / `waiting-backend`，
    见 `app/meeting.py` 的 `_policy_blocked_only`），这一层负责把它翻成**两句不同的话**：
    前者等多久都不会来，该做的是去改「允许音频去哪」；后者才是"等它 / 去查那台"。

    说错一个词的代价与 `not-configured` 那条一模一样：用户会去查错的地方
    （去查网络/端口/后端进程，而真答案是许可）。
    """

    def test_the_label_names_the_permission_and_the_fix(self):
        label = capability_admin.TRANSCRIBE_FALLBACK_LABELS["blocked-by-privacy"]
        self.assertIn("允许音频去哪", label)
        self.assertIn("内网", label, "要给出下一步，而不只是描述现象")
        self.assertIn("回环", label, "本机后端不受这条限制 —— 这句必须说，否则用户会以为没救")
        self.assertNotIn("连不上", label, "它不是连不上")

    def test_the_state_is_its_own_bucket(self):
        """`blocked-by-privacy` 是**独立一档**：不加"等待能力后端"前缀（等不来）。"""
        out = capability_admin.execution_summary(None, "", "blocked-by-privacy")
        self.assertEqual(out["state"], "blocked-by-privacy")
        self.assertEqual(out["fallbackReasonLabel"],
                         capability_admin.TRANSCRIBE_FALLBACK_LABELS["blocked-by-privacy"])
        self.assertEqual(out["headline"], out["fallbackReasonLabel"])
        self.assertNotIn(capability_admin.WAITING_BACKEND, out["headline"])

    def test_a_diarize_record_does_not_swallow_the_permission_reason(self):
        """分离那一档排在最前（"这场没有说话人"），但**许可那句仍要跟着出现**。"""
        dia = {"executed": False, "reason": "blocked", "waiting": False}
        out = capability_admin.execution_summary(dia, "", "blocked-by-privacy")
        self.assertEqual(out["state"], "diarize-not-executed")
        self.assertIn("允许音频去哪", out["fallbackReasonLabel"])

    def test_the_reason_does_not_need_a_diarize_record(self):
        """**没有分离记录的会议**（配置里关掉了分离）也要画得出来。

        那句原因本来就在 `meta.json` 里（`transcribeFallbackReason`），而原来这一行
        只由 `diarize.waiting` 决定 —— 于是那种会议的详情页**一个字都不显示**，
        用户只看到"转写失败"，原因得自己去翻 meta。记下来的事实必须看得见。
        """
        out = capability_admin.execution_summary(None, "", "waiting-backend")
        self.assertEqual(out["state"], "waiting-backend")
        self.assertTrue(out["headline"])

    def test_it_still_says_nothing_when_there_is_nothing_to_say(self):
        """反过来：**什么都没记** → `None`（老会议不该凭空多出一行）。"""
        self.assertIsNone(capability_admin.execution_summary(None, "", ""))


class UsedBackendIsNotWaitingForItTests(unittest.TestCase):
    """文字**是能力后端出的** → 那一行不许再说「在等后端」（2026-10-13）。

    用户现场：一场会先"配了后端但连不上"、meta 里留下 `diarize.waiting=True` 与
    `transcribeFallbackReason=waiting-backend`（**同一次失败一起写下的两个字段**）；
    后来配上后端**重转成功**，详情页于是同时显示「分离已执行（ECHO 后端）」与
    「等待能力后端：配置里点名了它，但它现在连不上」—— 转写稿明明就摆在那儿。

    判据只看"这一场的文字是谁出的"：`transcribeEngine == "backend"`。
    **写入侧**同批修了（`meeting._finalize_waiting_flag()`），但盘上的老 meta
    不会因为代码更新而自动重写 —— 所以读侧必须自己兜住，而且**两个字段都要清**
    （只清一个，另一个照样把这一行画成「等待能力后端」）。
    """

    def test_it_is_reported_as_backend_not_as_waiting(self):
        dia = {"executed": True, "reason": "", "waiting": True}
        out = capability_admin.execution_summary(dia, "backend", "waiting-backend")
        self.assertIs(out["waiting"], False, out)
        self.assertEqual(out["waitingLabel"], "")
        self.assertEqual(out["fallbackReason"], "",
                         "文字是后端出的 → 「为什么没走成」这条旧原因必须一起清掉")
        self.assertEqual(out["state"], "backend",
                         "后端出的文字不许再说成「在等后端」：%s" % out)
        self.assertEqual(out["headline"], out["transcribeEngineLabel"])
        self.assertFalse(out["diarize"]["waiting"],
                         "发射出去的那份 diarize 快照也要自洽：%s" % out["diarize"])

    def test_it_does_not_mutate_the_caller_snapshot(self):
        """读的时候**不许改调用方那份** `diarize`（它可能是盘上 meta 的引用）。"""
        dia = {"executed": True, "reason": "", "waiting": True}
        capability_admin.execution_summary(dia, "backend", "waiting-backend")
        self.assertTrue(dia["waiting"], "调用方那份被就地改了：%s" % dia)

    def test_a_waiting_record_survives_when_the_backend_did_not_do_it(self):
        """没走后端的那一轮**不许**被上一条顺手改掉（那是真"在等"）。"""
        dia = {"executed": False, "reason": "offline", "waiting": True}
        out = capability_admin.execution_summary(dia, "", "waiting-backend")
        self.assertIs(out["waiting"], True)
        self.assertEqual(out["waitingLabel"], capability_admin.WAITING_BACKEND)
        self.assertEqual(out["fallbackReason"], "waiting-backend")


class SetBaseUrlTests(unittest.TestCase):
    """**只改地址、不重新配对**（2026-10-13 用户要求）。

    判据三条：① 只换 `base_url`，`client_id` / `secret` / 证书固定**一个字节都不动**
    （动了服务端就会多一条客户端、老凭据作废）；② 没配对过 = 业务失败（回一句人话）；
    ③ 地址没变时不写盘（幂等）。
    """

    def _creds(self):
        from app.capabilities import credentials
        return credentials.BackendCredentials(
            base_url="http://221.216.62.2:8900", client_id="cli-1", secret="sec",
            server_name="gpu-office-2060s", cert_pem="PEM", cert_fingerprint="sha256:aa")

    def test_it_swaps_only_the_address(self):
        from unittest.mock import patch
        from app import capability_admin
        from app.capabilities import credentials
        saved = {}
        with patch.object(credentials, "load", lambda: self._creds()), \
                patch.object(credentials, "save", lambda c: saved.update(c=c)):
            ok, msg = capability_admin.set_base_url("aeros.zetone.cn:8900")
        self.assertTrue(ok, msg)
        c = saved.get("c")
        self.assertIsNotNone(c, "要真的落盘")
        self.assertEqual(c.base_url, "http://aeros.zetone.cn:8900")
        self.assertEqual(c.client_id, "cli-1", "clientId 不许变")
        self.assertEqual(c.secret, "sec", "secret 不许变")
        self.assertEqual(c.cert_pem, "PEM", "证书固定不许被顺手清掉")
        self.assertIn("不用重新配对", msg)

    def test_no_credentials_is_a_business_failure(self):
        from unittest.mock import patch
        from app import capability_admin
        from app.capabilities import credentials
        with patch.object(credentials, "load", lambda: None):
            ok, msg = capability_admin.set_base_url("aeros.zetone.cn:8900")
        self.assertFalse(ok)
        self.assertIn("还没配对", msg)

    def test_a_same_address_is_idempotent(self):
        from unittest.mock import patch
        from app import capability_admin
        from app.capabilities import credentials
        saved = {}
        with patch.object(credentials, "load", lambda: self._creds()), \
                patch.object(credentials, "save", lambda c: saved.update(c=c)):
            ok, msg = capability_admin.set_base_url("http://221.216.62.2:8900")
        self.assertTrue(ok, msg)
        self.assertEqual(saved, {}, "地址没变就不该写盘")
        self.assertIn("没变", msg)


class AddressChangeIsAtomicTests(_Isolated):
    """改地址是"**先抓证书、成功才落地址**"（2026-10-16 晚复盘，A1 缺口 1 的修法版）。

    复盘的原话是"**一次网络抖动就能把一台本来可用的机器改成不可用**"：旧顺序是
    `set_base_url()` 落盘 → 再抓证书补 pin，于是抓证书失败时**地址已经改了、证书没有**
    → `ready=False`。这一组钉的就是那条：失败时**旧地址与旧凭据逐字未变**，
    而且**仍然连得上旧地址**（旧地址的桩还起着）；成功时地址与 pin 一起到位。

    后端全是本机桩（`_Stub` 明文 / `_HttpsServer` 真 https 握手），**不碰公网**。
    """

    def _tls(self):
        from tests.test_backend_tls import _HttpsServer
        srv = _HttpsServer()
        self.addCleanup(srv.stop)
        return srv

    def _paired_http(self):
        s = self.stub([(200, _PAIR_OK)])
        ok, msg = capability_admin.pair(s.url, "ABC123")
        self.assertTrue(ok, msg)
        return s

    def _paired_https(self):
        """先配对到一台**真 https** 后端（带 `fp=` 校验证书）—— 这是"能用的旧状态"。"""
        from tests.tls_test_cert import CERT_PEM
        srv = self._tls()
        ok, msg = capability_admin.pair(srv.url, "ABC123",
                                        cert_fingerprint=pairing.fingerprint_of(CERT_PEM))
        self.assertTrue(ok, msg)
        return srv

    def _snapshot(self):
        """凭据的**每一个字节**（用于"逐字未变"这条判据）。"""
        c = cred.load()
        self.assertIsNotNone(c)
        return {"base_url": c.base_url, "client_id": c.client_id, "secret": c.secret,
                "server_name": c.server_name, "cert_pem": c.cert_pem,
                "cert_fingerprint": c.cert_fingerprint, "paired_at": c.paired_at}

    def test_a_failed_certificate_fetch_changes_nothing_and_the_old_address_still_works(self):
        """① 抓不到证书 → 400，**地址与凭据逐字未变**，旧地址仍然 `ready=True`。"""
        srv = self._paired_https()
        before = self._snapshot()
        r = self.client.post("/api/capability/address",
                             json={"base_url": "https://127.0.0.1:1"})      # 没人监听
        self.assertEqual(r.status_code, 400, "抓不到证书不许当成功")
        detail = r.json()["detail"]
        self.assertIn("取证书", detail, detail)          # 原因要指得出是**证书**这件事
        self.assertIn("127.0.0.1:1", detail)
        self.assertEqual(self._snapshot(), before,
                         "抓证书失败却动了地址/凭据 —— 那正是'一次抖动把可用机器改坏'")
        # 旧地址仍然可用（桩还起着）：这一条才是"保持原来的可用状态"的判据
        self.assertTrue(echo_server.EchoServerClient(base_url=before["base_url"]).ready(),
                        "失败之后旧地址必须仍然可用")
        self.assertTrue(srv.url == before["base_url"])

    def test_a_failed_fetch_never_writes_the_credentials_file(self):
        """① 的加强版：失败时**一次 `save` 都不许发生**（不是"先写再回滚"）。

        "先写后回滚"能通过状态断言（最终值一样），但它留下的是一个真实存在的窗口：
        那一刻磁盘上就是"地址已改、证书没补"的坏状态 —— 进程正好在这时崩掉/被杀，
        用户就永久拿到了那台不可用的机器。所以这条按**写盘次数**判。
        """
        self._paired_https()
        first = cred.load()
        seen = []
        real_save = cred.save
        with mock.patch.object(cred, "save",
                               lambda c: seen.append(c.base_url) or real_save(c)):
            r = self.client.post("/api/capability/address",
                                 json={"base_url": "https://127.0.0.1:1"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(seen, [], "抓证书失败时一个字节都不该写")
        self.assertEqual(cred.load().base_url, first.base_url)

    def test_a_successful_change_lands_the_address_and_the_certificate_together(self):
        """② 成功时**地址与 pin 一起到位**（http → https，还带 `fp=`），身份一个字节不动。"""
        from tests.tls_test_cert import CERT_PEM
        self._paired_http()
        before = self._snapshot()
        srv = self._tls()
        fp = pairing.fingerprint_of(CERT_PEM)
        r = self.client.post("/api/capability/address",
                             json={"base_url": srv.url, "fingerprint": fp})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("证书固定下来", r.json()["message"], "补了证书就要说出来")
        after = self._snapshot()
        self.assertEqual(after["base_url"], srv.url)
        self.assertEqual(after["cert_pem"].strip(), CERT_PEM.strip(), "证书没落盘")
        self.assertEqual(after["cert_fingerprint"], fp)
        self.assertEqual((after["client_id"], after["secret"], after["paired_at"]),
                         (before["client_id"], before["secret"], before["paired_at"]),
                         "换地址 ≠ 换身份：clientId/secret 不许动")
        self.assertTrue(echo_server.EchoServerClient(base_url=after["base_url"]).ready(),
                        "地址与 pin 都到位之后应当 ready=True")

    def test_a_mismatched_fingerprint_is_a_400_and_the_old_state_is_kept(self):
        """③ `fp=` 对不上 → 400（**可能是中间人**）+ 旧状态不变、旧地址仍可用。"""
        from tests.tls_test_cert import OTHER_CERT_PEM
        srv = self._paired_https()
        before = self._snapshot()
        r = self.client.post("/api/capability/address",
                             json={"base_url": srv.url,
                                   "fingerprint": pairing.fingerprint_of(OTHER_CERT_PEM)})
        self.assertEqual(r.status_code, 400, "指纹对不上不许当成功")
        self.assertIn("中间人", r.json()["detail"])
        self.assertEqual(self._snapshot(), before, "指纹对不上却动了地址/凭据")
        self.assertTrue(echo_server.EchoServerClient(base_url=before["base_url"]).ready())

    def test_an_unparseable_address_is_a_400_and_changes_nothing(self):
        """地址本身认不出来时也一样：先报错，**不动旧地址**（不写盘）。"""
        self._paired_https()
        before = self._snapshot()
        r = self.client.post("/api/capability/address", json={"base_url": "ftp://x/y"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
