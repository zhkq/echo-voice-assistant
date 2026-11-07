# -*- coding: utf-8 -*-
"""客户端收费/账号闭环的**服务端契约**（M2；issue #23 起客户端入口已存在）。

这个文件做两件事：

1. **契约级**：服务端返回的字段名/形状就是「我的」页要读的那些（`plans` / `orders` /
   `entitlement`），用真实的 app 打一遍；
2. **复用边界**：客户端账号入口（`app/account_admin.py`）只复用现有能力客户端的
   TLS pin / HTTP / 错误模型（`pairing.request_json`），**不另写一套 HTTP 客户端**。
   客户端登录/绑定的行为用例在 `tests/test_account_admin.py`。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_server_billing import _BillingCase                      # noqa: E402


class ServerContractForTheFutureClientTests(_BillingCase):
    def test_plans_shape_the_panel_will_read(self):
        body = self.client.get("/v1/billing/plans").json()
        self.assertIsInstance(body["plans"], list)
        for plan in body["plans"]:
            for key in ("code", "name", "priceCents", "days", "dailyAudioMinutes",
                        "ttsChars", "qps", "maxDevices", "maxConcurrent"):
                self.assertIn(key, plan)

    def test_order_shape_and_the_poll_until_paid_flow(self):
        token = self.register()["userToken"]
        headers = {"Authorization": "Bearer " + token}
        created = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                   headers=headers).json()
        for key in ("orderId", "amountCents", "payUrl", "expiresAt", "status",
                    "currentPaidUntil", "projectedPaidUntil", "renewalDays",
                    "renewalBasis", "renewalRule", "planChangeRule", "refundRule",
                    "autoRenew"):
            self.assertIn(key, created)
        self.assertEqual(created["status"], "paying")
        self.client.post("/v1/billing/mock/pay/" + created["orderId"], json={})
        paid = self.client.get("/v1/billing/orders/" + created["orderId"],
                               headers=headers).json()
        self.assertEqual(paid["status"], "paid")
        self.assertTrue(paid.get("pairUrl", "").startswith("echo://pair?"))

    def test_entitlement_shape_the_panel_will_read(self):
        token = self.register()["userToken"]
        body = self.client.get("/v1/billing/entitlement",
                               headers={"Authorization": "Bearer " + token}).json()
        for key in ("status", "plan", "paidUntil", "dailyMinutes", "ttsCharsLeft",
                    "qps", "maxDevices", "maxConcurrent", "devices", "expiringSoon",
                    "renewalRule", "planChangeRule", "refundRule", "autoRenew"):
            self.assertIn(key, body)


class ClientIsUntouchedTests(unittest.TestCase):
    def test_the_client_capability_client_still_exists_for_reuse(self):
        """将来接 M2 时复用它（证书固定 / 重试 / 错误分型都在里面）。"""
        from app.capabilities.echo_server import EchoServerClient
        self.assertTrue(callable(getattr(EchoServerClient, "_request", None)))

    def test_the_client_account_entry_exists_now(self):
        """issue #23 起客户端账号入口就在 `app/account_admin.py`（M2 的客户端侧）。

        它复用现有能力客户端的 TLS pin / 错误模型（`pairing.request_json`），
        不另写一套 HTTP 客户端。
        """
        from app import account_admin
        self.assertTrue(callable(getattr(account_admin, "login", None)))
        self.assertTrue(callable(getattr(account_admin, "bind_device", None)))
        self.assertTrue(callable(getattr(account_admin, "me", None)))
        self.assertTrue(callable(getattr(account_admin, "unbind_device", None)))
