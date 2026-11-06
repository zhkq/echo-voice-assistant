# -*- coding: utf-8 -*-
"""账号 / 邀请码 / 设备绑定（设计 §3.5、§4.2；任务书 T2）。

邮箱账号体系（2026-11-06，issue #15）：注册 = 邮箱 + 验证码 + 密码 + 邀请码；
登录 = 邮箱 + 密码。这个文件保留**设备绑定 / 令牌域隔离**的用例；邮箱注册、
验证码、找回密码的完整矩阵在 `tests/test_server_account_email.py`。
"""
import base64
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient                        # noqa: E402

from server import engines, main as server_main                  # noqa: E402
from tests.account_test_helpers import put_email_code            # noqa: E402
from tests.test_server_contract import _cfg, _fake_loader        # noqa: E402


def _billing_cfg(tmp, **over):
    cfg = _cfg(tmp)
    cfg.raw["billing"]["enabled"] = True
    cfg.raw["payment"]["provider"] = "mock"
    cfg.raw["auth"]["enabled"] = True
    cfg.raw["auth"]["mode"] = "jwt"
    cfg.raw["auth"]["jwt_secret"] = "0123456789abcdef0123456789abcdef"
    cfg.raw["auth"]["clock_skew_s"] = 0
    cfg.raw["server"]["state_root"] = os.path.join(tmp, "state")
    cfg.raw["account"]["email"]["enabled"] = True
    cfg.raw["account"]["email"]["provider"] = "dev"
    for key, value in over.items():
        cfg.raw[key] = value
    return cfg


class _AccountCase(unittest.TestCase):
    def setUp(self):
        # `create_app()` 会装 `app.paths._settings_get` 这个**进程内全局** seam；
        # 跑完必须还原，否则会连累后面 test_config_compat / test_backend_admin
        # 里"路径跟随用户设置"的用例（仓库吃过这个亏，`PathsSeamTests` 会扫）。
        from app import paths
        self._paths_seam = getattr(paths, "_settings_get", None)
        self.addCleanup(self._restore_paths_seam)
        self.tmp = tempfile.mkdtemp(prefix="echo-account-test-")
        self.cfg = _billing_cfg(self.tmp)
        self._patch = patch.object(engines, "build_loaders",
                                   lambda device="cuda": {"fake": _fake_loader})
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.app = server_main.create_app(self.cfg)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self._exit)
        self.state = self.app.state.echo
        self.store = self.state.auth.store

    def _restore_paths_seam(self):
        from app import paths
        if self._paths_seam is None:
            try:
                delattr(paths, "_settings_get")
            except AttributeError:
                pass
        else:
            paths._settings_get = self._paths_seam

    def _exit(self):
        try:
            self.client.__exit__(None, None, None)
        except Exception:
            pass

    # ---- helpers ---------------------------------------------------------

    def invite(self, code="INVITE-1", max_uses=10, expires_at=0):
        self.store.put_invite(code, max_uses=max_uses, expires_at=expires_at)
        return code

    def register(self, email="user@example.com", code="123456",
                 password="Passw0rd", invite_code="INVITE-1", seed=True):
        if seed:
            put_email_code(self.store, email, "register", code)
        return self.client.post("/v1/account/register",
                                json={"email": email, "code": code,
                                      "password": password,
                                      "inviteCode": invite_code})

    def login(self, email="user@example.com", password="Passw0rd"):
        return self.client.post("/v1/account/login",
                                json={"email": email, "password": password})

    def headers(self, token):
        return {"Authorization": "Bearer " + token}

    def pair(self, code, client_name="测试设备"):
        r = self.client.post("/v1/pair", json={"code": code,
                                               "clientName": client_name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def client_token(self, cid, secret):
        raw = base64.b64encode(("%s:%s" % (cid, secret)).encode()).decode()
        r = self.client.post("/v1/token", headers={"Authorization": "Basic " + raw})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["accessToken"]

    def bind_device(self, user_token, name="设备"):
        r = self.client.post("/v1/account/devices/code", json={"clientName": name},
                             headers=self.headers(user_token))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        return body, self.pair(body["bindCode"], name)


class TokenDomainTests(_AccountCase):
    def test_a_client_jwt_cannot_call_the_billing_api(self):
        self.invite()
        body = self.register().json()
        bind, paired = self.bind_device(body["userToken"])
        token = self.client_token(paired["clientId"], paired["secret"])
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers=self.headers(token))
        self.assertEqual(r.status_code, 401)
        self.assertIn("账号令牌", r.json()["detail"])

    def test_a_user_token_cannot_call_capability_endpoints(self):
        self.invite()
        token = self.register().json()["userToken"]
        r = self.client.post("/v1/asr", content=b"x", headers=self.headers(token))
        self.assertEqual(r.status_code, 401)
        self.assertIn("账号令牌", r.json()["detail"])

    def test_an_unknown_token_is_refused_by_both_domains(self):
        r = self.client.get("/v1/account/me",
                            headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(r.status_code, 401)


class DeviceBindingTests(_AccountCase):
    def setUp(self):
        super().setUp()
        self.invite()
        self.body = self.register().json()
        self.token = self.body["userToken"]

    def test_a_bind_code_is_single_use_and_binds_the_device(self):
        issued, paired = self.bind_device(self.token, "第一台")
        self.assertEqual(issued["devices"], 0)
        self.assertIn("echo://pair?", issued["pairUrl"])
        self.assertEqual(self.store.count_devices(self.body["userId"]), 1)
        # 再用一次同一张码 → 401（已消费）
        r = self.client.post("/v1/pair", json={"code": issued["bindCode"]})
        self.assertEqual(r.status_code, 401)

    def test_the_fourth_device_is_refused_until_one_is_unbound(self):
        pairs = []
        for i in range(3):
            _issued, paired = self.bind_device(self.token, "设备%d" % i)
            pairs.append(paired)
        self.assertEqual(self.store.count_devices(self.body["userId"]), 3)
        # 解绑前先把它手上的令牌换出来（解绑后换令牌会被 403 挡住 —— 这正是目的）。
        old_token = self.client_token(pairs[0]["clientId"], pairs[0]["secret"])
        r = self.client.post("/v1/account/devices/code",
                             json={"clientName": "第四台"},
                             headers=self.headers(self.token))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "device_limit_reached")

        # 解绑第一台 → 第四台可以绑；旧客户端立即失效。
        r = self.client.post("/v1/account/devices/unbind",
                             json={"clientId": pairs[0]["clientId"]},
                             headers=self.headers(self.token))
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/v1/asr", content=b"x",
                             headers=self.headers(old_token))
        self.assertEqual(r.status_code, 403)
        _issued, new_pair = self.bind_device(self.token, "第四台")
        self.assertEqual(self.store.count_devices(self.body["userId"]), 3)
        self.assertTrue(new_pair["clientId"].startswith("cli-"))

    def test_a_purchase_code_binds_the_device_too(self):
        """支付成功发的码也走同一条 `after_pair`（任务书 §5.5 时序 A）。"""
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers=self.headers(self.token))
        self.assertEqual(r.status_code, 200, r.text)
        order_id = r.json()["orderId"]
        r = self.client.post("/v1/billing/mock/pay/" + order_id, json={})
        self.assertEqual(r.status_code, 200, r.text)
        order = self.client.get("/v1/billing/orders/" + order_id,
                                headers=self.headers(self.token)).json()
        code = order["pairUrl"].split("code=")[1].split("&")[0]
        paired = self.pair(code, "买了月卡的本机")
        self.assertEqual(self.store.count_devices(self.body["userId"]), 1)
        self.assertEqual(self.store.device_for_client(paired["clientId"])["user_id"],
                         self.body["userId"])
