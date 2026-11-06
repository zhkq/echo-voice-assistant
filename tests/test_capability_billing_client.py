# -*- coding: utf-8 -*-
"""客户端收费闭环（M2）**本轮不做** —— 这里只钉"将来接的时候不用改服务端"的契约。

任务书 T8 的原话：`tests/test_capability_billing_client.py`（**客户端不上**，这个可以
留空/跳过或只写契约级）。

所以这个文件做三件事：

1. **契约级**：服务端返回的字段名/形状就是「我的」页要读的那些（`plans` / `orders` /
   `entitlement`），用真实的 app 打一遍；
2. **边界**：客户端 `app/`、`web/` 本轮一个字节没改；将来接的时候复用
   `app/capabilities/echo_server.py::EchoServerClient._request`（现有的 TLS pin/重试/
   错误分型），**不另写一套 HTTP 客户端**；
3. **跳过**：M2 的入口（`app/account_admin.py`、`/api/account/*`）不存在时明确 skip，
   而不是假装通过。
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

    def test_the_m2_entry_points_are_not_part_of_this_ticket(self):
        import importlib.util
        if importlib.util.find_spec("app.account_admin") is None:
            self.skipTest("M2 的客户端入口（app/account_admin.py）本轮不做")
        self.fail("客户端入口出现了 —— 本 ticket 的范围是服务端；请确认这是另一个 ticket")
