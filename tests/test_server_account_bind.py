# -*- coding: utf-8 -*-
"""登录态直接绑定设备（issue #23）：`POST /v1/account/devices/bind`。

这里只钉**服务端契约**：凭据形状、幂等、设备上限、权益/冻结闸门、解绑立即失效、
旧配对兼容、认领旧凭据、限流与错误码。客户端侧在 `tests/test_account_admin.py`。

旧配对（`/v1/account/devices/code` + `/v1/pair`）的完整用例仍在
`tests/test_server_account.py::DeviceBindingTests` —— 本文件只额外确认两条路能并存。
"""
import base64
import time

from tests.test_server_account import _AccountCase


def _dev(i: int) -> str:
    """一个合法的 16 位 deviceId（12–64 位 URL 安全字符）。"""
    return "dev-%012d" % int(i)


class BindDeviceTests(_AccountCase):
    def setUp(self):
        super().setUp()
        self.invite()
        body = self.register().json()
        self.token = body["userToken"]
        self.uid = body["userId"]
        self.state.account.bind_bucket.reset()

    # ---- helpers ---------------------------------------------------------

    def bind(self, token=None, device_id=None, **extra):
        payload = {
            "deviceId": device_id or _dev(1),
            "clientName": "测试设备",
            "deviceName": "DESKTOP-TEST",
            "platform": "windows",
        }
        payload.update(extra)
        return self.client.post("/v1/account/devices/bind", json=payload,
                                headers=self.headers(token or self.token))

    def token_response(self, client_id, secret):
        raw = base64.b64encode(("%s:%s" % (client_id, secret)).encode()).decode()
        return self.client.post("/v1/token",
                                headers={"Authorization": "Basic " + raw})

    def asr(self, token, body=b"x"):
        return self.client.post("/v1/asr", content=body,
                                headers={"Authorization": "Bearer " + token,
                                         "Content-Type": "audio/wav"})

    # ---- 成功路径 --------------------------------------------------------

    def test_bind_returns_device_credentials_and_a_working_short_token(self):
        r = self.bind()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for key in ("clientId", "clientSecret", "accessToken", "expiresIn",
                    "scopes", "serverName", "protocol", "rebound", "device",
                    "devices", "deviceLimit"):
            self.assertIn(key, body)
        self.assertTrue(body["clientId"].startswith("cli-"))
        self.assertTrue(body["clientSecret"])
        self.assertTrue(body["accessToken"])
        self.assertFalse(body["rebound"])
        self.assertEqual(body["devices"], 1)
        self.assertEqual(body["deviceLimit"], 3)
        self.assertEqual(body["device"]["platform"], "windows")
        self.assertEqual(body["device"]["deviceName"], "DESKTOP-TEST")
        self.assertIn("asr", body["scopes"])
        # 服务端只存哈希：返回的明文不能落库。
        row = self.store.client(body["clientId"])
        self.assertNotEqual(row.get("secret_hash"), body["clientSecret"])

        # 客户端凭据能换令牌，也能过能力鉴权（音频不合法没关系，**不是 401/403**）。
        token = self.token_response(body["clientId"], body["clientSecret"])
        self.assertEqual(token.status_code, 200, token.text)
        access = token.json()["accessToken"]
        self.assertNotIn(self.asr(access).status_code, (401, 403))

        # 账号页能看到这台设备。
        me = self.client.get("/v1/account/me", headers=self.headers(self.token)).json()
        self.assertEqual(len(me["devices"]), 1)
        self.assertEqual(me["devices"][0]["clientId"], body["clientId"])
        self.assertEqual(me["devices"][0]["platform"], "windows")
        self.assertGreater(me["devices"][0]["lastSeen"], 0)

    def test_rebind_same_device_is_idempotent_and_rotates_the_secret(self):
        first = self.bind().json()
        second = self.bind().json()
        self.assertEqual(second["clientId"], first["clientId"])
        self.assertTrue(second["rebound"])
        self.assertEqual(second["devices"], 1)
        self.assertNotEqual(second["clientSecret"], first["clientSecret"])

        # 旧的短期令牌失效（token_version +1）。
        self.assertEqual(self.asr(first["accessToken"]).status_code, 401)
        # 旧 secret 也换不到令牌；新 secret 可以。
        self.assertEqual(self.token_response(first["clientId"],
                                             first["clientSecret"]).status_code, 401)
        self.assertEqual(self.token_response(first["clientId"],
                                             second["clientSecret"]).status_code, 200)

    def test_the_fourth_device_is_refused_and_does_not_consume_a_slot(self):
        for i in range(3):
            self.assertEqual(self.bind(device_id=_dev(i)).status_code, 200)
        r = self.bind(device_id=_dev(9))
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "device_limit_reached")
        self.assertEqual(self.store.count_devices(self.uid), 3)

    def test_same_device_on_another_account_is_a_conflict(self):
        self.assertEqual(self.bind(device_id=_dev(7)).status_code, 200)
        other = self.register(email="other@example.com").json()
        r = self.bind(token=other["userToken"], device_id=_dev(7))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "device_bound_to_other_account")

    def test_bind_requires_a_user_token_and_rejects_a_client_token(self):
        r = self.client.post("/v1/account/devices/bind",
                             json={"deviceId": _dev(1)})
        self.assertEqual(r.status_code, 401)
        # 先绑一台拿到客户端 JWT，再拿它冒充账号令牌。
        bound = self.bind().json()
        r = self.client.post("/v1/account/devices/bind",
                             json={"deviceId": _dev(2)},
                             headers={"Authorization": "Bearer " + bound["accessToken"]})
        self.assertEqual(r.status_code, 401)
        self.assertIn("账号令牌", r.json()["detail"])

    def test_missing_or_malformed_device_id_is_a_400(self):
        r = self.client.post("/v1/account/devices/bind", json={},
                             headers=self.headers(self.token))
        self.assertEqual(r.status_code, 400)
        r = self.bind(device_id="x")
        self.assertEqual(r.status_code, 400)
        r = self.bind(platform="WINDOWS!")
        self.assertEqual(r.status_code, 400)

    # ---- 闸门 ------------------------------------------------------------

    def test_no_entitlement_is_refused_before_a_credential_is_created(self):
        self.store.set_entitlement_status(self.uid, "inactive")
        r = self.bind()
        self.assertEqual(r.status_code, 402, r.text)
        self.assertEqual(r.json()["code"], "entitlement_inactive")
        self.assertEqual(self.store.count_devices(self.uid), 0)
        self.assertEqual(self.store.clients(), [])

    def test_an_expired_entitlement_is_refused_with_402(self):
        row = self.store.entitlement(self.uid)
        row["paid_until"] = time.time() - 10
        self.store.upsert_entitlement(row)
        r = self.bind()
        self.assertEqual(r.status_code, 402, r.text)
        self.assertEqual(r.json()["code"], "entitlement_expired")

    def test_a_frozen_account_cannot_bind_and_its_device_loses_access(self):
        bound = self.bind().json()
        self.store.update_user(self.uid, status="disabled")
        r = self.bind()
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.asr(bound["accessToken"]).status_code, 403)

    def test_bind_is_rate_limited_per_account(self):
        self.state.account.bind_bucket.burst = 1.0
        self.state.account.bind_bucket.rate = 1.0 / 60.0
        self.state.account.bind_bucket.reset()
        self.assertEqual(self.bind(device_id=_dev(1)).status_code, 200)
        r = self.bind(device_id=_dev(2))
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()["code"], "rate_limited")

    def test_billing_disabled_is_404_and_non_jwt_auth_is_503(self):
        self.cfg.raw["billing"]["enabled"] = False
        r = self.bind()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "billing_disabled")
        self.cfg.raw["billing"]["enabled"] = True
        self.cfg.raw["auth"]["mode"] = "token"
        r = self.bind()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "auth_misconfigured")

    # ---- 解绑 / 旧路 / 认领 ---------------------------------------------

    def test_unbind_revokes_immediately_and_the_same_device_can_rebind(self):
        first = self.bind().json()
        self.assertNotIn(self.asr(first["accessToken"]).status_code, (401, 403))
        r = self.client.post("/v1/account/devices/unbind",
                             json={"clientId": first["clientId"]},
                             headers=self.headers(self.token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.asr(first["accessToken"]).status_code, 403)

        again = self.bind().json()
        self.assertEqual(again["devices"], 1)
        self.assertFalse(again["rebound"])
        self.assertNotIn(self.asr(again["accessToken"]).status_code, (401, 403))

    def test_the_old_pairing_path_still_works_alongside_bind(self):
        issued, paired = self.bind_device(self.token, "旧路设备")
        self.assertEqual(self.store.count_devices(self.uid), 1)
        self.assertEqual(self.store.device_for_client(paired["clientId"])["user_id"],
                         self.uid)
        # 旧路设备没有 deviceId；新路再绑一台，两条路互不影响。
        new = self.bind(device_id=_dev(3)).json()
        self.assertEqual(new["devices"], 2)
        me = self.client.get("/v1/account/me", headers=self.headers(self.token)).json()
        self.assertEqual(len(me["devices"]), 2)

    def test_existing_client_id_is_adopted_without_consuming_a_second_slot(self):
        _issued, paired = self.bind_device(self.token, "旧配对")
        r = self.bind(device_id=_dev(5), existingClientId=paired["clientId"])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["clientId"], paired["clientId"])
        self.assertTrue(body["rebound"])
        self.assertEqual(body["devices"], 1)
        row = self.store.device_for_client(paired["clientId"])
        self.assertEqual(row["device_id"], _dev(5))

    def test_existing_client_id_must_match_the_same_device(self):
        """认领只允许同一台设备（或还没有 deviceId 的历史行），不能拿别的设备顶替。"""
        first = self.bind(device_id=_dev(5)).json()
        r = self.bind(device_id=_dev(6), existingClientId=first["clientId"])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "device_mismatch")
        row = self.store.device_for_client(first["clientId"])
        self.assertEqual(row["device_id"], _dev(5))
        self.assertEqual(self.store.count_devices(self.uid), 1)

    def test_another_accounts_client_cannot_be_claimed(self):
        _issued, paired = self.bind_device(self.token, "别人的旧配对")
        other = self.register(email="other@example.com").json()
        r = self.bind(token=other["userToken"], device_id=_dev(6),
                      existingClientId=paired["clientId"])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "client_not_yours")

    def test_an_unknown_existing_client_is_ignored_and_a_new_one_is_created(self):
        r = self.bind(device_id=_dev(8), existingClientId="cli-ffffff")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["rebound"])
        self.assertEqual(r.json()["devices"], 1)

    def test_old_pairing_the_same_client_does_not_clear_the_device_id(self):
        """旧路重新配对同一 client 时，`after_pair` 传的是空 deviceId —— 不能把它抹掉。

        否则"先登录即绑、后手动配对一次"会悄悄丢失幂等身份，下一次 bind 会新建一台设备。
        """
        bound = self.bind(device_id=_dev(4)).json()
        code = self.state.account.auth.create_pairing_code(
            created_by="user:" + self.uid, name="旧路重配", scopes="",
            ttl_s=600, user_id=self.uid, kind="device-bind",
            client_id=bound["clientId"])
        paired = self.pair(code, "旧路重配")
        self.assertEqual(paired["clientId"], bound["clientId"])
        row = self.store.device_for_client(bound["clientId"])
        self.assertEqual(row["device_id"], _dev(4))
        self.assertEqual(self.store.count_devices(self.uid), 1)
