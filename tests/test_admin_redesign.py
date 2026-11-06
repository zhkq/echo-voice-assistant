# -*- coding: utf-8 -*-
"""管理面重设计（issue #16）新增行为：dashboard / 搜索分页 / 冻结恢复 / 单设备解绑。

这些用例只碰**临时库 + 假引擎**，不调真实模型/网关/邮件。用户邮箱按 issue #15
的 `users.email` 模拟：库里还没有这一列时先在临时库补上（生产等 #15 落地）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import auth as auth_mod                       # noqa: E402
from server import payment as payment_mod                 # noqa: E402
from tests.test_admin_console import _AdminCase, _wav_bytes  # noqa: E402


class _RedesignCase(_AdminCase):
    """在 `_AdminCase` 上补三件测试专用的小工具。"""

    def enable_billing(self):
        self.cfg.raw["billing"]["enabled"] = True
        self.cfg.raw["payment"]["provider"] = "mock"
        svc = self.state.billing_svc
        svc.gateway = payment_mod.MockGateway("admin-redesign-test")
        svc.seed()

    def add_email_column(self, user_id: str, email: str) -> None:
        store = self.state.auth.store
        with store._lock:
            cols = [row[1] for row in store._db.execute("PRAGMA table_info(users)").fetchall()]
            if "email" not in cols:
                store._db.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            store._db.execute("UPDATE users SET email=? WHERE user_id=?", (email, user_id))
            store._db.commit()

    def make_user(self, user_id="usr-1", name="张三", email="", status="active"):
        store = self.state.auth.store
        store.create_user(user_id, name=name, status=status)
        if email:
            self.add_email_column(user_id, email)
        return store.user(user_id)

    def give_entitlement(self, user_id, plan="monthly", days=30):
        now = time.time()
        self.state.auth.store.upsert_entitlement({
            "user_id": user_id, "plan_code": plan,
            "paid_until": now + days * 86400,
            "daily_audio_minutes": 120, "tts_chars": 50000, "qps": 1,
            "max_devices": 3, "max_concurrent": 1,
            "status": "active", "order_id": "ord-test",
            "updated_at": now,
        })

    def bind_device(self, user_id, client_id="cli-1"):
        store = self.state.auth.store
        store.upsert_client(client_id, "测试机",
                            auth_mod.hash_secret("s", client_id), scopes="asr")
        store.upsert_device(user_id, client_id, "测试机")
        self.state.auth.cache.forget(client_id)

    def make_order(self, order_id, user_id, *, status="paid", created=None):
        now = time.time()
        self.state.auth.store.create_order({
            "order_id": order_id, "plan_code": "monthly", "amount_cents": 1000,
            "status": status, "user_id": user_id, "client_id": "",
            "pairing_code_id": "", "alipay_trade_no": "",
            "created_at": created if created is not None else now,
            "paid_at": now if status == "paid" else 0,
            "expires_at": now + 900, "meta": "",
        })


class DashboardTests(_RedesignCase):
    def test_dashboard_shape_and_it_hides_secrets(self):
        self.make_user("usr-1", "张三", "zhangsan@example.com")
        self.give_entitlement("usr-1", days=5)
        self.login()
        r = self.ac.get("/admin/api/dashboard")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for key in ("revenueCentsToday", "ordersToday", "activeUsers24h",
                    "usersTotal", "entitlementsActive", "expiringIn7Days",
                    "conflicts", "unverifiedNotifies", "recentOrders",
                    "recentUsers", "expiring", "billing", "server"):
            self.assertIn(key, body)
        self.assertEqual(body["usersTotal"], 1)
        self.assertEqual(body["entitlementsActive"], 1)
        self.assertEqual(body["expiringIn7Days"], 1)
        blob = r.text.lower()
        for bad in ("private_key", "app_id", "mock_secret", "password"):
            self.assertNotIn(bad, blob)


class OrderSearchTests(_RedesignCase):
    def test_order_search_by_order_id_and_user_email(self):
        self.enable_billing()
        user = self.make_user("usr-1", "张三", "zhangsan@example.com")
        self.give_entitlement("usr-1")
        self.make_order("ord-alpha", "usr-1")
        self.make_order("ord-beta", "usr-1", status="created")
        other = self.make_user("usr-2", "李四", "lisi@example.com")
        self.make_order("ord-gamma", "usr-2")
        self.login()
        r = self.ac.get("/admin/api/billing/orders?q=ord-alpha")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["orders"][0]["orderId"], "ord-alpha")
        r = self.ac.get("/admin/api/billing/orders?q=lisi@example.com")
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["orders"][0]["orderId"], "ord-gamma")
        r = self.ac.get("/admin/api/billing/orders?status=created")
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["orders"][0]["orderId"], "ord-beta")

    def test_order_pagination_returns_total_and_page(self):
        self.enable_billing()
        self.make_user("usr-1", "张三", "zhangsan@example.com")
        self.give_entitlement("usr-1")
        base = int(time.time()) - 1000
        for i in range(25):
            self.make_order("ord-%02d" % i, "usr-1", created=base + i)
        self.login()
        r = self.ac.get("/admin/api/billing/orders?limit=10&offset=20")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 25)
        self.assertEqual(len(body["orders"]), 5)
        self.assertEqual(body["offset"], 20)


class UserSearchTests(_RedesignCase):
    def test_user_search_by_email_when_the_column_exists(self):
        self.make_user("usr-1", "张三", "zhangsan@example.com")
        self.make_user("usr-2", "李四", "lisi@example.com")
        self.login()
        r = self.ac.get("/admin/api/account/users?q=zhangsan@example.com")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["users"][0]["email"], "zhangsan@example.com")

    def test_user_search_does_not_match_employee_number(self):
        """2026-11-06 用户口径：不再为工号搜索做兼容。"""
        store = self.state.auth.store
        store.create_user("usr-1", name="张三", employee_no="E001")
        self.login()
        r = self.ac.get("/admin/api/account/users?q=E001")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 0)


class AccountWriteTests(_RedesignCase):
    def _setup_bound_user(self):
        self.enable_billing()
        self.make_user("usr-1", "张三", "zhangsan@example.com")
        self.give_entitlement("usr-1")
        self.bind_device("usr-1", "cli-1")
        self.login()

    def test_freeze_stops_the_bound_device_and_restore_resumes(self):
        self._setup_bound_user()
        r = self.wpost("/admin/api/account/users/usr-1/status",
                       {"status": "disabled", "confirm": "usr-1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.state.auth.store.user("usr-1")["status"], "disabled")
        r = self.client.post("/v1/asr", content=_wav_bytes(seconds=1.0),
                             headers=self.cap_headers("cli-1"))
        self.assertEqual(r.status_code, 403, r.text)
        r = self.wpost("/admin/api/account/users/usr-1/status",
                       {"status": "active", "confirm": "usr-1"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/v1/asr", content=_wav_bytes(seconds=1.0),
                             headers=self.cap_headers("cli-1"))
        self.assertEqual(r.status_code, 200, r.text)

    def test_single_device_revoke_leaves_the_account_alone(self):
        self._setup_bound_user()
        r = self.wpost("/admin/api/account/users/usr-1/devices/cli-1/revoke",
                       {"confirm": "cli-1"})
        self.assertEqual(r.status_code, 200, r.text)
        devices = self.state.auth.store.devices("usr-1", include_revoked=True)
        self.assertEqual(devices[0]["status"], "revoked")
        self.assertEqual(self.state.auth.store.user("usr-1")["status"], "active")
        r = self.client.post("/v1/asr", content=_wav_bytes(seconds=1.0),
                             headers=self.cap_headers("cli-1"))
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_freeze_and_device_revoke_are_audited(self):
        self._setup_bound_user()
        self.wpost("/admin/api/account/users/usr-1/status",
                   {"status": "disabled", "confirm": "usr-1"})
        self.wpost("/admin/api/account/users/usr-1/devices/cli-1/revoke",
                   {"confirm": "cli-1"})
        actions = {a for _who, a, _t in self.audit_rows()}
        self.assertIn("account-user-status", actions)
        self.assertIn("account-device-revoke", actions)


class SystemAndAuditTests(_RedesignCase):
    def test_system_status_is_read_only_and_has_no_secrets(self):
        self.login()
        r = self.ac.get("/admin/api/system")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("billing", body)
        self.assertIn("server", body)
        blob = r.text.lower()
        for bad in ("private_key", "app_id", "mock_secret", "password"):
            self.assertNotIn(bad, blob)

    def test_audit_endpoint_filters_failed_actions(self):
        self.login()
        # 缺 confirm 的冻结会失败并留下 `.failed` 审计。
        r = self.wpost("/admin/api/account/users/usr-nope/status",
                       {"status": "disabled"})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.ac.get("/admin/api/audit?result=failed&action=account-user-status")
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["rows"]
        self.assertTrue(any(a["action"].endswith(".failed") for a in rows), rows)


class SyncTests(_RedesignCase):
    def test_sync_is_a_registered_write_and_audits(self):
        self.enable_billing()
        self.make_user("usr-1", "张三", "zhangsan@example.com")
        self.give_entitlement("usr-1")
        self.make_order("ord-1", "usr-1", status="created")
        self.login()
        r = self.wpost("/admin/api/billing/orders/ord-1/sync", {})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        actions = {a for _who, a, _t in self.audit_rows()}
        self.assertIn("billing-order-sync", actions)
