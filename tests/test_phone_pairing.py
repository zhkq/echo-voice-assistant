# -*- coding: utf-8 -*-
"""手机 / 手表触点：配对码 · 局域网守卫 · 鉴权联动 · 接口契约（2026-10-09）

设计：`docs/手机触点-App设计.md`（阶段 0/1）、`docs/手表触点-可行性分析-Watch4Pro.md` §4。
本文件钉四件事，每件都能单独判"坏没坏"：

  1. **配对码**（`app/phone_pair.py`）：一次性 / 5 分钟寿命 / **用掉即删** / 错多了锁，
     而且**锁着的时候连正确的码也不放过**（这一条最容易写反）。
  2. **局域网守卫不是"放行一切"**（`app/netguard.py`）：Host 必须是**本机自己的地址**
     **且** 对端不是公网 —— 两条都判。默认档（loopback）行为与以前**一字不差**。
  3. **联动**（`app/config.py::Settings._couple_bind_mode`）：`serverBindMode=lan` ⇒
     `apiAuthEnabled=true`，且 **lan 期间关不掉鉴权**；同一次请求里"切回 loopback + 关鉴权"可以。
  4. **接口**：发码只许本机；兑换是唯一不带令牌的接口；"网上来的调用"拒收清单。

**一条安全纪律（照 AGENTS.md 的事故写的）**：拒收清单那四条里有
`POST /api/control/echo/stop` 与 `POST /api/system/restart` —— 如果直接拿**真路由**测，
逻辑一旦有洞，**用例自己就会把开发机上正在跑的 ECHO 停掉**。
所以这里用"**同路径的桩应用**"测依赖本身，另加一条"桩的路径与真路由一致"的防腐烂断言。

隔离：DB/设置缓存全部指向临时目录；不起服务、不开麦、不碰真实 data/。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import app.db as db                                              # noqa: E402
from app import netguard, phone_pair                             # noqa: E402
from app.config import settings                                  # noqa: E402

#: 测试用的"本机局域网地址"（**patch 掉真地址枚举**：结论不该随这台机器装了几块网卡而变）
FAKE_LAN = "192.168.1.5"
#: 内网里**别的**机器（必须继续被拒）
OTHER_LAN = "10.9.9.9"
OTHER_LAN_URL = "http://%s:8970" % OTHER_LAN


def _drop_cache():
    settings._cache = None


class _Isolated(unittest.TestCase):
    """临时库 + 设置缓存复位（每个用例类一份，互不污染）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="echo-phone-")
        cls._old = (db.DATA_DIR, db.DB_FILE)
        db.DATA_DIR = cls.tmp
        db.DB_FILE = os.path.join(cls.tmp, "test.db")
        db.init()
        settings.seed_defaults()
        _drop_cache()

    @classmethod
    def tearDownClass(cls):
        db.DATA_DIR, db.DB_FILE = cls._old
        _drop_cache()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        phone_pair.reset()
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
        self.addCleanup(netguard.forget_own_names)
        # 每个用例都从"最安全的档"开始：loopback + 关鉴权
        settings.update({"serverBindMode": "loopback", "apiAuthEnabled": False})
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()

    def _lan_mode(self):
        """切到局域网档（联动会把鉴权打开）。"""
        settings.update({"serverBindMode": "lan"})
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()


# ================================================================ 1. 配对码
class PairCodeTests(_Isolated):
    def test_a_code_can_be_claimed_exactly_once(self):
        issued = phone_pair.issue()
        self.assertEqual(len(issued["code"]), phone_pair.DIGITS)
        self.assertTrue(issued["code"].isdigit())
        self.assertTrue(phone_pair.pending(), "生成之后面板要能看到它")
        ok, why = phone_pair.claim(issued["code"])
        self.assertTrue(ok, why)
        self.assertEqual(phone_pair.pending(), {}, "用掉即删（不是标记已用）")
        again, why2 = phone_pair.claim(issued["code"])
        self.assertFalse(again, "同一张码不许兑换第二次")
        self.assertIn("过期", why2)

    def test_a_wrong_code_never_unlocks_and_never_mints(self):
        issued = phone_pair.issue()
        wrong = "0" * phone_pair.DIGITS if issued["code"] != "0" * phone_pair.DIGITS else "1" * phone_pair.DIGITS
        ok, why = phone_pair.claim(wrong)
        self.assertFalse(ok)
        self.assertIn("不对", why)
        self.assertTrue(phone_pair.pending(), "错一次不该把码作废（用户会手抖打错）")

    def test_the_code_expires(self):
        phone_pair.issue(ttl=0)
        ok, why = phone_pair.claim("000000")
        self.assertFalse(ok)
        self.assertIn("过期", why)

    def test_issuing_again_replaces_the_previous_code(self):
        first = phone_pair.issue()["code"]
        second = phone_pair.issue()["code"]
        self.assertTrue(phone_pair.claim(second)[0])
        # 旧的**已经不存在**（不是"还能用"）—— 面板上只会显示最新那张
        self.assertEqual(phone_pair.pending(), {})

    def test_brute_force_is_locked_out_and_the_lock_beats_the_right_code(self):
        """错满 `MAX_FAILURES` 次就锁；**锁着的时候正确的码也不放过**。

        这条是本文件最要紧的一条：如果锁只挡错误码，6 位码在局域网上是可以暴力试穿的
        （攻击者连错到极限，只要某一次撞上就直接过关）。
        """
        issued = phone_pair.issue()
        for _ in range(phone_pair.MAX_FAILURES):
            phone_pair.claim("000000")
        self.assertGreater(phone_pair.locked_for(), 0, "错满次数必须锁")
        ok, why = phone_pair.claim(issued["code"])
        self.assertFalse(ok, "锁着的时候**正确的码也不许过**")
        self.assertIn("频繁", why)

    def test_reset_clears_everything(self):
        phone_pair.issue()
        for _ in range(phone_pair.MAX_FAILURES):
            phone_pair.claim("000000")
        phone_pair.reset()
        self.assertEqual(phone_pair.pending(), {})
        self.assertEqual(phone_pair.locked_for(), 0)


# ================================================================ 2. 局域网守卫
class LanGuardTests(_Isolated):
    def test_loopback_judgement_is_unchanged_by_lan_mode(self):
        """`is_loopback_host` 是安全判据，**不许**被局域网档放宽（放宽=把两个概念混成一个）。"""
        for host in ("127.0.0.1:8970", "localhost", "[::1]:8970"):
            self.assertTrue(netguard.is_loopback_host(host), host)
        for host in ("192.168.1.5:8970", "10.9.9.9:8970", "example.com", ""):
            self.assertFalse(netguard.is_loopback_host(host), host)

    def test_host_must_be_one_of_our_own_addresses(self):
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            self.assertTrue(netguard.is_lan_host("%s:8970" % FAKE_LAN))
            self.assertFalse(netguard.is_lan_host("%s:8970" % OTHER_LAN),
                             "内网里**别的**机器不是本机")
            self.assertFalse(netguard.is_lan_host("example.com:8970"))
            self.assertFalse(netguard.is_lan_host("127.0.0.1:8970"),
                             "回环由 is_loopback_host 负责，不走这条")

    def test_is_lan_host_is_mode_agnostic_but_the_guard_is_not(self):
        """`is_lan_host` 只回答"是不是**本机自己的**地址"，**档位由守卫判**。

        这个分工是故意的：判据（这是我自己的地址吗）与策略（这台机器现在允许谁访问）
        分开，策略只有一处（`local_only_guard`），不会在别处被"顺手放宽"。
        "默认档下即使 Host 是自己的局域网地址也不放行"由 `GuardMiddlewareTests` 钉着。
        """
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self.assertTrue(netguard.is_lan_host("%s:8970" % FAKE_LAN))
            self.assertFalse(netguard.is_lan_host("%s:8970" % OTHER_LAN))

    def test_origin_rules(self):
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            # 默认档：只认回环来源
            self.assertTrue(netguard.is_allowed_origin("http://127.0.0.1:8970"))
            self.assertFalse(netguard.is_allowed_origin("http://%s:8970" % FAKE_LAN))
            self._lan_mode()
            # 局域网档：自己那两个地址可以（面板经局域网打开时要用），别的内网机器与公网不行
            self.assertTrue(netguard.is_allowed_origin("http://%s:8970" % FAKE_LAN))
            self.assertTrue(netguard.is_allowed_origin("http://127.0.0.1:8970"))
            self.assertFalse(netguard.is_allowed_origin(OTHER_LAN_URL))
            self.assertFalse(netguard.is_allowed_origin("https://example.com"))
            self.assertFalse(netguard.is_allowed_origin("null"))

    def test_peer_judgements_point_in_opposite_directions(self):
        """`is_public_peer` 判不出就不拒（宁放不误杀）；`is_loopback_peer` 判不出就**不给豁免**。"""
        class _Client:
            def __init__(self, host):
                self.host = host

        self.assertTrue(netguard.is_public_peer(_Client("8.8.8.8")))
        self.assertFalse(netguard.is_public_peer(_Client("192.168.1.50")))
        self.assertFalse(netguard.is_public_peer(_Client("testclient")))
        self.assertTrue(netguard.is_loopback_peer("127.0.0.1"))
        self.assertFalse(netguard.is_loopback_peer("192.168.1.50"))
        self.assertFalse(netguard.is_loopback_peer("testclient"))


class GuardMiddlewareTests(_Isolated):
    """把 `local_only_guard` 装进一个最小应用，逐格打真请求。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI
        app = FastAPI()
        netguard.install(app)

        @app.get("/ping")
        def ping():
            return {"ok": True}

        cls.app = app

    def _client(self, host, peer):
        from fastapi.testclient import TestClient
        return TestClient(self.app, base_url="http://%s/" % host, client=(peer, 51234))

    def test_default_mode_refuses_the_lan_address_even_from_a_lan_peer(self):
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            r = self._client("%s:8970" % FAKE_LAN, "192.168.1.50").get("/ping")
            self.assertEqual(r.status_code, 403, "默认档不许从局域网访问")

    def test_lan_mode_allows_own_address_from_private_peer(self):
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            r = self._client("%s:8970" % FAKE_LAN, "192.168.1.50").get("/ping")
            self.assertEqual(r.status_code, 200, r.text)

    def test_lan_mode_still_refuses_other_machines_and_public_hosts(self):
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            for host in ("%s:8970" % OTHER_LAN, "example.com:8970"):
                with self.subTest(host=host):
                    r = self._client(host, "192.168.1.50").get("/ping")
                    self.assertEqual(r.status_code, 403, host)

    def test_lan_mode_refuses_a_public_peer_even_with_a_valid_host(self):
        """对端是公网 → 403（第二条判据；少这一条就是"公网也能连"）。"""
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            r = self._client("%s:8970" % FAKE_LAN, "8.8.8.8").get("/ping")
            self.assertEqual(r.status_code, 403, "Host 是自己的地址也救不了公网对端")

    def test_lan_mode_refuses_a_cross_site_origin(self):
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            r = self._client("%s:8970" % FAKE_LAN, "192.168.1.50").get(
                "/ping", headers={"Origin": "https://evil.example"})
            self.assertEqual(r.status_code, 403)
            r2 = self._client("%s:8970" % FAKE_LAN, "192.168.1.50").get(
                "/ping", headers={"Origin": OTHER_LAN_URL})
            self.assertEqual(r2.status_code, 403, "内网里**别的**页面也不行")


# ================================================================ 3. 绑定档 ⇒ 鉴权 联动
class BindModeCouplingTests(_Isolated):
    def test_lan_switches_authentication_on(self):
        settings.update({"serverBindMode": "lan"})
        self.assertTrue(settings.get("apiAuthEnabled"),
                        "开局域网必须**自动**打开鉴权（局域网上没有令牌=谁都能下命令）")

    def test_authentication_cannot_be_switched_off_while_lan(self):
        self._lan_mode()
        settings.update({"apiAuthEnabled": False})
        self.assertTrue(settings.get("apiAuthEnabled"), "lan 期间关不掉鉴权")

    def test_going_back_to_loopback_and_switching_auth_off_in_one_request_works(self):
        """否则就"关不掉了" —— 用户会被自己的安全设置锁住。"""
        self._lan_mode()
        settings.update({"serverBindMode": "loopback", "apiAuthEnabled": False})
        self.assertEqual(settings.get("serverBindMode"), "loopback")
        self.assertFalse(settings.get("apiAuthEnabled"))

    def test_an_unknown_mode_is_refused_not_written(self):
        settings.update({"serverBindMode": "wan"})
        self.assertEqual(settings.get("serverBindMode"), "loopback", "取值不认识就拒收")


# ================================================================ 4. 接口契约
class _ApiBase(_Isolated):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import router
        app = FastAPI()
        netguard.install(app)
        app.include_router(router)
        cls.app = app
        cls.TestClient = TestClient

    def _client(self, host="127.0.0.1", peer="127.0.0.1"):
        return self.TestClient(self.app, base_url="http://%s/" % host, client=(peer, 51234))


class PairEndpointTests(_ApiBase):
    def _patched_lan(self):
        return patch("app.netguard.local_addresses", lambda: [FAKE_LAN])

    def test_state_endpoint_is_read_only(self):
        r = self._client().get("/api/pair/phone")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["bindMode"], "loopback")
        self.assertEqual(body["pending"], {})
        self.assertIn("addresses", body)

    def test_issuing_a_code_needs_lan_mode_and_says_why(self):
        with self._patched_lan():
            netguard.forget_own_names()
            r = self._client().post("/api/pair/phone")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("允许局域网访问", r.json()["detail"])
        self.assertEqual(phone_pair.pending(), {}, "拒了就不该留下半张码")

    def test_an_issued_code_can_be_claimed_and_used_over_the_network(self):
        with self._patched_lan():
            netguard.forget_own_names()
            self._lan_mode()
            r = self._client().post("/api/pair/phone")
            self.assertEqual(r.status_code, 200, r.text)
            issued = r.json()
            self.assertEqual(len(issued["code"]), phone_pair.DIGITS)
            self.assertEqual(issued["baseUrl"], "http://%s:8970" % FAKE_LAN)
            self.assertIn("code=%s" % issued["code"], issued["pairString"])
            self.assertIn("host=%s:8970" % FAKE_LAN, issued["pairString"])

            # 设备**从网络**上来兑换（对端是内网别的机器、Host 是本机地址）
            lan = self._client(host="%s:8970" % FAKE_LAN, peer="192.168.1.50")
            claim = lan.post("/api/pair/phone/claim",
                             json={"code": issued["code"], "name": "mate60"})
            self.assertEqual(claim.status_code, 200, claim.text)
            token = claim.json()["token"]
            self.assertTrue(token.startswith("echo_"))

            # 拿到的令牌能用：`/api/status`（设计 §8 阶段 1 的验收判据）
            ok = lan.get("/api/status", headers={"Authorization": "Bearer %s" % token})
            self.assertEqual(ok.status_code, 200, ok.text)
            # 同一把令牌不能再用第二次兑换（码已删）
            self.assertEqual(lan.post("/api/pair/phone/claim",
                                      json={"code": issued["code"]}).status_code, 403)

    def test_claim_is_refused_without_a_valid_code(self):
        with self._patched_lan():
            netguard.forget_own_names()
            self._lan_mode()
            lan = self._client(host="%s:8970" % FAKE_LAN, peer="192.168.1.50")
            r = lan.post("/api/pair/phone/claim", json={"code": "000000"})
            self.assertEqual(r.status_code, 403, r.text)

    def test_issuing_a_code_from_the_network_is_refused(self):
        """**网上来的请求即使带着有效令牌也不给新码** —— 否则一把泄露的令牌能无限复制凭据。"""
        with self._patched_lan():
            netguard.forget_own_names()
            self._lan_mode()
            lan = self._client(host="%s:8970" % FAKE_LAN, peer="192.168.1.50")
            issued = self._client().post("/api/pair/phone").json()
            claimed = lan.post("/api/pair/phone/claim", json={"code": issued["code"]}).json()
            r = lan.post("/api/pair/phone", headers={"Authorization": "Bearer %s" % claimed["token"]})
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("本机", r.json()["detail"])

    def test_anonymous_network_calls_still_need_a_token(self):
        with self._patched_lan():
            netguard.forget_own_names()
            self._lan_mode()
            lan = self._client(host="%s:8970" % FAKE_LAN, peer="192.168.1.50")
            self.assertEqual(lan.get("/api/settings").status_code, 401)

    def test_the_local_panel_is_exempt_from_the_token(self):
        """lan 档强制打开鉴权之后，**本地面板必须照样能用** —— 否则连"生成配对码"都点不动。"""
        with self._patched_lan():
            netguard.forget_own_names()
            self._lan_mode()
            local = self._client(host="127.0.0.1", peer="127.0.0.1")
            r = local.get("/api/settings")
            self.assertEqual(r.status_code, 200, "本机回环调用不该要令牌")
            self.assertEqual(local.post("/api/pair/phone").status_code, 200)


class NetworkDenyListTests(_ApiBase):
    """"网上来的调用"拒收清单。

    **故意用同路径的桩应用**（见文件头那句安全纪律）：拿真路由测的话，
    逻辑一有洞，用例就把开发机上的 ECHO 停掉了。
    """

    #: 桩要复刻的路径（与 `app/api.py::_NETWORK_DENIED` 一一对应）
    DENIED = (("POST", "/api/control/echo/stop"),
              ("POST", "/api/system/restart"),
              ("DELETE", "/api/meetings/9"),
              ("POST", "/api/meetings/clean-short"),
              ("PUT", "/api/settings"),
              ("POST", "/api/settings/reset"))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi import Depends, FastAPI
        from app.api import optional_auth
        app = FastAPI()
        netguard.install(app)
        cls.calls = []

        def _record():
            cls.calls.append(1)
            return {"ok": True}

        for method, path in cls.DENIED:
            app.add_api_route(path, _record, methods=[method],
                              dependencies=[Depends(optional_auth)])
        app.add_api_route("/api/status", _record, methods=["GET"],
                          dependencies=[Depends(optional_auth)])
        cls.app = app

    def test_the_stub_paths_are_the_real_ones_and_guarded(self):
        """防腐烂：桩的路径必须真的在真路由里，**而且真路由确实挂了 `optional_auth`**。

        少任何一条，这条用例就在测一个虚构的接口：
        路径漂移 → 测的是没人用的地址；依赖缺失 → 拒收清单在真链路上根本不生效。
        （路径里有 `{mid}` 这种参数写法，所以按 FastAPI 的 `path_regex` 匹配，
        不搞字符串相等。）
        """
        from app.api import optional_auth, router as real_router
        for method, path in self.DENIED:
            with self.subTest(path=path):
                hits = [r for r in real_router.routes
                        if method in (getattr(r, "methods", None) or set())
                        and r.path_regex.match(path)]
                self.assertTrue(hits, "%s %s 不在真路由里（清单漂移了）" % (method, path))
                self.assertTrue(
                    any(getattr(d, "call", None) is optional_auth
                        for r in hits for d in r.dependant.dependencies),
                    "%s 没挂 optional_auth —— 拒收清单在真链路上会绕过去" % path)

    def test_network_callers_cannot_stop_restart_delete_or_reconfigure(self):
        self.calls.clear()          # `cls.calls` 是类级记录，逐个用例清（否则互相污染）
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            lan = self._client(host="%s:8970" % FAKE_LAN, peer="192.168.1.50")
            # 令牌**直接建**（不走兑换）：本用例测的是拒收清单，
            # 兑换那条链由 `PairEndpointTests` 覆盖，别把两个判据缠在一起
            token = db.add_api_key_row("test-device", ["device"])["token"]
            head = {"Authorization": "Bearer %s" % token}
            for method, path in self.DENIED:
                with self.subTest(path=path):
                    r = lan.request(method, path, headers=head, json={})
                    self.assertEqual(r.status_code, 403, "%s 必须被拒：%s" % (path, r.text))
                    self.assertIn("本机面板不受影响", r.json()["detail"])
            self.assertEqual(self.calls, [], "被拒的请求**根本不该进处理器**")
            # 对照组：同样这把令牌调允许的动作要能过（否则"全拒"也能让上面的断言通过）
            self.assertEqual(lan.get("/api/status", headers=head).status_code, 200)

    def test_local_callers_are_not_hit_by_the_deny_list(self):
        """本机面板不受清单影响（它就是用户自己）。"""
        self.calls.clear()
        with patch("app.netguard.local_addresses", lambda: [FAKE_LAN]):
            netguard.forget_own_names()
            self._lan_mode()
            local = self._client(host="127.0.0.1", peer="127.0.0.1")
            self.assertEqual(local.post("/api/control/echo/stop").status_code, 200)
            self.assertEqual(local.put("/api/settings", json={"values": {}}).status_code, 200)
            self.assertEqual(len(self.calls), 2)


class PhonePanelWiringTests(unittest.TestCase):
    """面板接线（**静态断言**：门禁里没有浏览器，只能钉"接上了没有"）。

    这三条挡的都是**安静的空白**（与 `tests/test_capability_panel.py` 同一套理由）：
      * `$("#phoneCardHost")` 拼错一个字母 → 那张卡里什么都没有；
      * 两个新设置项没进落点表 → 它们会漂到「未归类」兜底卡里，用户看到的是"这张卡没做完"；
      * 按钮没走委托 → 卡片每次重绘后"点了没反应"。
    """

    @classmethod
    def setUpClass(cls):
        def _read(name):
            with open(os.path.join(_ROOT, "web", name), encoding="utf-8") as fh:
                return fh.read()
        cls.js = _read("app.js")
        cls.css = _read("app.css")

    def test_every_phone_id_the_js_reaches_for_is_created_somewhere(self):
        import re
        reached = set(re.findall(r'\$\("#(phone[A-Za-z0-9]*)"\)', self.js))
        self.assertTrue(reached, "一个手机卡的 id 都没引用到？")
        created = set(re.findall(r'id="([^"]+)"', self.js))
        missing = sorted(i for i in reached if i not in created)
        self.assertEqual(missing, [], "JS 引用了没人创建的面板 id：%s" % missing)

    def test_the_phone_card_claims_both_new_settings(self):
        """两个新键必须落在**这张卡**的落点表里（否则会漂进「未归类」）。"""
        self.assertIn('id: "phone"', self.js, "SET_CARDS 里没有手机卡")
        panel = self.js[self.js.index('id: "phone"'):]
        panel = panel[:panel.index("dynAfter")] if "dynAfter" in panel else panel[:2000]
        for key in ("serverBindMode", "serverLanHost"):
            self.assertIn('"%s"' % key, panel,
                          "%s 没进「手机 / 手表」卡的落点表（会漂到「未归类」）" % key)

    def test_the_card_buttons_are_wired_by_delegation(self):
        for attr in ("data-phone-issue", "data-phone-revoke", "data-phone-refresh"):
            self.assertIn(attr, self.js, "%s 没有被渲染出来" % attr)
        for attr in ("issue", "revoke", "refresh"):
            self.assertIn('closest("[data-phone-%s]")' % attr, self.js,
                          "%s 按钮没有接上（重绘后会点了没反应）" % attr)

    def test_the_pairing_code_is_visually_loud(self):
        """6 位码是要拿到另一台设备上手输的 —— 样式必须真给它字号（别只写个类名）。"""
        self.assertIn(".phone-code", self.css)
        block = self.css[self.css.index(".phone-code {"):]
        self.assertIn("font-size", block[:block.index("}")])


if __name__ == "__main__":
    unittest.main()
