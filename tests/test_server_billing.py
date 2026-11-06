# -*- coding: utf-8 -*-
"""套餐 / 订单 / 权益 / 支付 / 闸门（设计 §4.1–§4.6；任务书 T3–T7）。

这一组用例覆盖任务书 §7 的验收自测；主线已含 `/v1/tts`，其收费口径在
`TtsBillingTests` 里单独测。
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient                        # noqa: E402

from server import admin as admin_mod                            # noqa: E402
from server import auth as auth_mod                              # noqa: E402
from server import engines, errors, main as server_main          # noqa: E402
from server import payment as payment_mod                        # noqa: E402
from server import quota as quota_mod                            # noqa: E402
from server import ratelimit                                     # noqa: E402
from server import settings as settings_mod                      # noqa: E402
from tests.account_test_helpers import put_email_code            # noqa: E402
from tests.test_server_contract import _cfg, _fake_loader, _wav_bytes  # noqa: E402


def _billing_cfg(tmp, **over):
    cfg = _cfg(tmp)
    cfg.raw["billing"]["enabled"] = True
    cfg.raw["payment"]["provider"] = "mock"
    cfg.raw["auth"]["enabled"] = True
    cfg.raw["auth"]["mode"] = "jwt"
    cfg.raw["auth"]["jwt_secret"] = "0123456789abcdef0123456789abcdef"
    cfg.raw["auth"]["clock_skew_s"] = 0
    cfg.raw["billing"]["timezone"] = "Asia/Shanghai"
    cfg.raw["server"]["state_root"] = os.path.join(tmp, "state")
    cfg.raw["account"]["email"]["enabled"] = True
    cfg.raw["account"]["email"]["provider"] = "dev"
    for key, value in over.items():
        cfg.raw[key] = value
    return cfg


class _BillingCase(unittest.TestCase):
    def setUp(self):
        # `create_app()` 会装 `app.paths._settings_get` 这个**进程内全局** seam；
        # 跑完必须还原，否则会连累后面 test_config_compat / test_backend_admin
        # 里"路径跟随用户设置"的用例（仓库吃过这个亏，`PathsSeamTests` 会扫）。
        from app import paths
        self._paths_seam = getattr(paths, "_settings_get", None)
        self.addCleanup(self._restore_paths_seam)
        self.tmp = tempfile.mkdtemp(prefix="echo-billing-test-")
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
        self.svc = self.state.billing_svc
        self.store.put_invite("INVITE-1", max_uses=50)

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

    def uh(self, token):
        return {"Authorization": "Bearer " + token}

    def register(self, label="E001"):
        """用邮箱验证码注册一个测试账号（label 只用来生成唯一邮箱）。"""
        email = "%s@example.com" % str(label).lower()
        put_email_code(self.store, email, "register", "123456")
        r = self.client.post("/v1/account/register",
                             json={"email": email, "code": "123456",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def uid(self, label="E001"):
        user = self.store.user_by_email("%s@example.com" % str(label).lower())
        self.assertIsNotNone(user, "测试账号不存在：%s" % label)
        return user["user_id"]

    def create_paid_client(self, label="E001", plan="monthly"):
        """注册 → 下单 → Mock 付款 → 兑换配对码 → (userToken, clientHeaders, orderId)。"""
        user = self.register(label)
        token = user["userToken"]
        r = self.client.post("/v1/billing/orders", json={"planCode": plan},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        order_id = r.json()["orderId"]
        r = self.client.post("/v1/billing/mock/pay/" + order_id, json={})
        self.assertEqual(r.status_code, 200, r.text)
        order = self.client.get("/v1/billing/orders/" + order_id,
                                headers=self.uh(token)).json()
        self.assertTrue(order.get("pairUrl"))
        code = order["pairUrl"].split("code=")[1].split("&")[0]
        r = self.client.post("/v1/pair", json={"code": code, "clientName": "本机"})
        self.assertEqual(r.status_code, 200, r.text)
        paired = r.json()
        raw = base64.b64encode(("%s:%s" % (paired["clientId"],
                                           paired["secret"])).encode()).decode()
        r = self.client.post("/v1/token", headers={"Authorization": "Basic " + raw})
        self.assertEqual(r.status_code, 200, r.text)
        return token, {"Authorization": "Bearer " + r.json()["accessToken"],
                       "Content-Type": "audio/wav"}, order_id

    def asr(self, headers, seconds=0.2):
        return self.client.post("/v1/asr", content=_wav_bytes(seconds=seconds),
                                headers=headers)

    def admin_app(self):
        if self.store.admin("ops") is None:
            self.store.upsert_admin("ops", admin_mod.hash_password("good-pass"))
        app = admin_mod.create_admin_app(self.cfg, self.state)
        client = TestClient(app, base_url="http://127.0.0.1:8901")
        client.__enter__()
        self.addCleanup(lambda: client.__exit__(None, None, None))
        r = client.post("/admin/api/login",
                        json={"username": "ops", "password": "good-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.csrf = r.json()["csrf"]
        return client

    def wh(self, host="127.0.0.1:8901"):
        return {"Content-Type": "application/json",
                "X-ECHO-Admin": "1", "X-CSRF-Token": self.csrf,
                "Origin": "http://" + host, "Host": host}


class CatalogTests(_BillingCase):
    def test_the_monthly_plan_matches_the_task_book(self):
        r = self.client.get("/v1/billing/plans")
        self.assertEqual(r.status_code, 200, r.text)
        plans = {p["code"]: p for p in r.json()["plans"]}
        monthly = plans["monthly"]
        self.assertEqual(monthly["priceCents"], 1000)
        self.assertEqual(monthly["days"], 30)
        self.assertEqual(monthly["dailyAudioMinutes"], 120.0)
        self.assertEqual(monthly["ttsChars"], 50000)
        self.assertEqual(monthly["qps"], 1)
        self.assertEqual(monthly["maxDevices"], 3)
        self.assertEqual(monthly["maxConcurrent"], 1)
        self.assertIn("trial", plans)
        self.assertIn("internal", plans)
        # 官网按 `purchasable` 决定是否显示「立即购买」：付费套餐可下单，
        # 试用/内部账号是邀请制（否则页面会出现一个点了就被 400 的按钮）。
        self.assertTrue(monthly["purchasable"])
        self.assertFalse(plans["trial"]["purchasable"])
        self.assertFalse(plans["internal"]["purchasable"])

    def test_plans_are_not_exposed_when_billing_is_off(self):
        self.cfg.raw["billing"]["enabled"] = False
        r = self.client.get("/v1/billing/plans")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "billing_disabled")

    def test_seeding_does_not_overwrite_admin_edits(self):
        self.store.upsert_plan({"code": "monthly", "name": "改过的月卡",
                                "price_cents": 1200, "days": 30,
                                "daily_audio_minutes": 120, "tts_chars": 50000,
                                "qps": 1, "max_devices": 3, "max_concurrent": 1,
                                "active": 1, "sort": 10})
        self.assertEqual(self.svc.seed(), 0)
        self.assertEqual(self.store.plan("monthly")["price_cents"], 1200)


class OrderTests(_BillingCase):
    def test_creating_an_order_requires_a_user_token(self):
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"})
        self.assertEqual(r.status_code, 401)

    def test_the_amount_comes_from_the_plan_not_from_the_client(self):
        token = self.register()["userToken"]
        r = self.client.post("/v1/billing/orders",
                             json={"planCode": "monthly", "amountCents": 1},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["amountCents"], 1000)
        self.assertTrue(r.json()["payUrl"])

    def test_an_unknown_plan_is_404(self):
        token = self.register()["userToken"]
        r = self.client.post("/v1/billing/orders", json={"planCode": "nope"},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "plan_not_found")

    def test_cancel_is_allowed_before_payment_and_refused_after(self):
        token = self.register()["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        r = self.client.post("/v1/billing/orders/%s/cancel" % order_id,
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "closed")
        # 已关闭的订单不能付
        r = self.client.post("/v1/billing/orders/%s/cancel" % order_id,
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 409)

        other = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                 headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + other, json={})
        r = self.client.post("/v1/billing/orders/%s/cancel" % other,
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "order_state_conflict")

    def test_another_users_order_is_not_visible(self):
        first = self.register("E001")["userToken"]
        second = self.register("E002")["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(first)).json()["orderId"]
        r = self.client.get("/v1/billing/orders/" + order_id, headers=self.uh(second))
        self.assertEqual(r.status_code, 404)


class MockPaymentTests(_BillingCase):
    def test_payment_marks_paid_and_issues_a_pair_url(self):
        token, client_headers, order_id = self.create_paid_client()
        user = self.client.get("/v1/account/me", headers=self.uh(token)).json()
        self.assertEqual(user["entitlement"]["status"], "active")
        self.assertEqual(user["entitlement"]["plan"], "monthly")
        # 首次支付：从**支付时刻**起 +30 天（试用不叠加）。
        paid_until = user["entitlement"]["paidUntil"]
        self.assertAlmostEqual(paid_until, time.time() + 30 * 86400, delta=120)
        # 配对后 capabilities 带上权益摘要
        cap = self.client.get("/v1/capabilities", headers=client_headers).json()
        self.assertEqual(cap["entitlement"]["plan"], "monthly")
        self.assertEqual(cap["entitlement"]["devices"], 1)
        # 没有令牌时该字段是 None（这个端点仍然免凭据）
        self.assertIsNone(self.client.get("/v1/capabilities").json()["entitlement"])

    def test_repeat_notify_with_the_same_trade_no_is_idempotent(self):
        token = self.register()["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        payload = {"outTradeNo": order_id, "tradeNo": "mock-fixed-1"}
        first = self.client.post("/v1/billing/mock/notify", json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["duplicate"])
        paid_until = self.store.entitlement(
            self.uid())["paid_until"]
        second = self.client.post("/v1/billing/mock/notify", json=payload)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["duplicate"])
        again = self.store.entitlement(
            self.uid())["paid_until"]
        self.assertEqual(paid_until, again, "重复回调不该再延一次")

    def test_a_tampered_amount_is_refused(self):
        token = self.register()["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        r = self.client.post("/v1/billing/mock/notify",
                             json={"outTradeNo": order_id, "amountCents": 1})
        self.assertEqual(r.status_code, 400)
        self.assertIn("金额", r.json()["detail"])
        self.assertEqual(self.store.order(order_id)["status"], "paying")

    def test_renewal_stacks_from_paid_until(self):
        token, _headers, _oid = self.create_paid_client()
        uid = self.uid()
        first = self.store.entitlement(uid)["paid_until"]
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers=self.uh(token))
        second_id = r.json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + second_id, json={})
        second = self.store.entitlement(uid)["paid_until"]
        self.assertAlmostEqual(second, first + 30 * 86400, delta=5)
        # 续费**不发码**（已经绑过设备）：订单详情里没有 pairUrl。
        order = self.client.get("/v1/billing/orders/" + second_id,
                                headers=self.uh(token)).json()
        self.assertFalse(order.get("pairUrl"))

    def test_a_closed_trade_does_not_grant_anything(self):
        token = self.register()["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        r = self.client.post("/v1/billing/mock/notify",
                             json={"outTradeNo": order_id, "status": "closed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.order(order_id)["status"], "paying")
        self.assertEqual(
            self.store.entitlement(self.uid())
            ["plan_code"], "trial")


class EntitlementGateTests(_BillingCase):
    def test_an_expired_entitlement_returns_402_and_renewal_restores_it(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        row = dict(self.store.entitlement(uid))
        row["paid_until"] = time.time() - 86400
        self.store.upsert_entitlement(row)
        self.svc.forget_entitlement(uid)
        r = self.asr(headers)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["code"], "entitlement_expired")

        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + order_id, json={})
        r = self.asr(headers)
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_missing_entitlement_returns_402(self):
        """没有权益的账号（例如管理面手工清空 / 支付即注册未完成）。"""
        user = self.store.create_user("usr-nobill", name="无权益", employee_no="E900")
        self.store.upsert_client("cli-nobill", "无权益设备",
                                 auth_mod.hash_secret("s", "cli-nobill"),
                                 scopes="asr")
        self.store.upsert_device(user["user_id"], "cli-nobill")
        self.state.auth.cache.forget("cli-nobill")
        token, _ = auth_mod.issue_token(self.store.client("cli-nobill"),
                                        self.state.auth.key, 3600)
        r = self.asr({"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["code"], "entitlement_missing")

    def test_the_daily_quota_is_counted_per_account(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        row = dict(self.store.entitlement(uid))
        row["daily_audio_minutes"] = 0.01          # 0.6 秒
        row["qps"] = 1000                          # 这一条测日额度，别让 1 QPS 先挡
        self.store.upsert_entitlement(row)
        self.svc.forget_entitlement(uid)
        self.assertEqual(self.asr(headers, seconds=1.0).status_code, 200)
        time.sleep(0.02)                           # 令牌桶 burst=1：1000 QPS 也要隔 1ms
        r = self.asr(headers, seconds=0.2)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "quota_exceeded")
        self.assertTrue(r.headers.get("Retry-After"))

    def test_one_qps_is_enforced_per_account(self):
        _token, headers, _oid = self.create_paid_client()
        first = self.asr(headers)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.asr(headers)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["code"], "rate_limited")
        self.assertEqual(second.headers.get("Retry-After"), "1")

    def test_a_second_device_hits_the_account_level_concurrency_gate(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        with self.svc.concurrency.hold(uid, 1):
            r = self.asr(headers)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "client_busy")
        self.assertIn("另一台设备", r.json()["detail"])

    def test_refund_invalidates_the_entitlement_at_once(self):
        token, headers, order_id = self.create_paid_client()
        r = self.client.post("/v1/billing/orders/" + order_id + "/refund",
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 404, "客户端没有退款入口（只有管理面有）")
        self.svc.refund_order(order_id)
        r = self.asr(headers)
        self.assertEqual(r.status_code, 402)
        status = self.store.entitlement(
            self.uid())["status"]
        self.assertEqual(status, "refunded")

    def test_internal_is_never_downgraded_by_a_purchase(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        self.svc.adjust_entitlement(uid, {"action": "internal"})
        self.assertEqual(self.store.entitlement(uid)["plan_code"], "internal")
        self.assertEqual(self.store.entitlement(uid)["paid_until"], 0.0)
        # internal 不降级、也不该收钱：下单在落库之前就被拒。
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "plan_change_unsupported")
        self.assertEqual(self.store.entitlement(uid)["plan_code"], "internal")
        self.assertEqual(self.asr(headers).status_code, 200)

    def test_expiring_soon_covers_the_last_seven_days(self):
        uid = self.store.create_user("usr-exp", name="快到期", employee_no="E901")["user_id"]
        self.store.upsert_entitlement({
            "user_id": uid, "plan_code": "monthly",
            "paid_until": time.time() + 6 * 86400, "daily_audio_minutes": 120,
            "tts_chars": 50000, "qps": 1, "max_devices": 3, "max_concurrent": 1,
            "status": "active", "order_id": "", "updated_at": time.time()})
        self.svc.forget_entitlement(uid)
        self.assertTrue(self.svc.entitlement_summary(uid)["expiringSoon"])
        self.store.upsert_entitlement({
            "user_id": uid, "plan_code": "monthly",
            "paid_until": time.time() + 8 * 86400, "daily_audio_minutes": 120,
            "tts_chars": 50000, "qps": 1, "max_devices": 3, "max_concurrent": 1,
            "status": "active", "order_id": "", "updated_at": time.time()})
        self.svc.forget_entitlement(uid)
        self.assertFalse(self.svc.entitlement_summary(uid)["expiringSoon"])


class UnitTests(unittest.TestCase):
    def test_the_token_bucket_refuses_the_second_call_in_the_same_second(self):
        clock = [1000.0]
        bucket = ratelimit.TokenBucket(rate=1, burst=1, clock=lambda: clock[0])
        bucket.check("u1")
        with self.assertRaises(errors.EchoError) as ctx:
            bucket.check("u1")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.retry_after, 1)
        clock[0] += 1.01
        bucket.check("u1")

    def test_user_concurrency_releases_on_exception(self):
        c = ratelimit.UserConcurrency()
        try:
            with c.hold("u1", 1):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with c.hold("u1", 1):
            self.assertEqual(c.current("u1"), 1)
        self.assertEqual(c.current("u1"), 0)

    def test_the_tts_char_ledger_enforces_the_daily_limit(self):
        ledger = quota_mod.CharLedger(limit_for=lambda uid: 10)
        ledger.add("u1", 6)
        self.assertEqual(ledger.remaining("u1"), 4)
        with self.assertRaises(errors.EchoError) as ctx:
            ledger.check("u1", add_chars=5)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        ledger.check("u1", add_chars=4)


class AlipayModeTests(unittest.TestCase):
    def test_mock_routes_do_not_exist_and_orders_say_payment_unconfigured(self):
        tmp = tempfile.mkdtemp(prefix="echo-alipay-mode-")
        cfg = _billing_cfg(tmp)
        cfg.raw["payment"]["provider"] = "alipay"
        cfg.raw["alipay"]["app_id"] = ""
        cfg.raw["alipay"]["private_key_path"] = ""
        cfg.raw["alipay"]["public_key_path"] = ""
        from app import paths
        before = getattr(paths, "_settings_get", None)
        try:
            with patch.object(engines, "build_loaders",
                              lambda device="cuda": {"fake": _fake_loader}):
                app = server_main.create_app(cfg)
                with TestClient(app) as client:
                    store = app.state.echo.auth.store
                    store.put_invite("INV", max_uses=5)
                    put_email_code(store, "e001@example.com", "register", "123456")
                    user = client.post("/v1/account/register",
                                       json={"email": "e001@example.com",
                                             "code": "123456",
                                             "password": "Passw0rd",
                                             "inviteCode": "INV"}).json()
                    # Mock 支付端点**不存在**（不是"存在但拒绝"）
                    r = client.post("/v1/billing/mock/pay/ord-x", json={})
                    self.assertEqual(r.status_code, 404)
                    # 下单明确回 503，且不静默走 Mock
                    r = client.post("/v1/billing/orders",
                                    json={"planCode": "monthly"},
                                    headers={"Authorization": "Bearer "
                                             + user["userToken"]})
                    self.assertEqual(r.status_code, 503)
                    self.assertEqual(r.json()["code"], "payment_unconfigured")
        finally:
            if before is None:
                try:
                    delattr(paths, "_settings_get")
                except AttributeError:
                    pass
            else:
                paths._settings_get = before


class AdminBillingTests(_BillingCase):
    def test_refund_from_the_admin_console_invalidates_and_is_audited(self):
        _token, headers, order_id = self.create_paid_client()
        ac = self.admin_app()
        r = ac.get("/admin/api/billing/summary", headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["provider"], "mock")
        r = ac.post("/admin/api/billing/orders/%s/refund" % order_id,
                    json={"confirm": order_id}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "refunded")
        audit = [(a["admin"], a["action"]) for a in self.store.recent_audit(50)]
        self.assertIn(("ops", "billing-refund"), audit)
        self.assertEqual(self.asr(headers).status_code, 402)

    def test_the_admin_can_create_a_plan_and_an_invite(self):
        ac = self.admin_app()
        r = ac.post("/admin/api/billing/plans",
                    json={"code": "quarterly", "name": "季卡", "priceCents": 2700,
                          "days": 90, "dailyAudioMinutes": 120, "ttsChars": 50000,
                          "qps": 1, "maxDevices": 3, "maxConcurrent": 1},
                    headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.plan("quarterly")["price_cents"], 2700)
        r = ac.post("/admin/api/account/invites",
                    json={"maxUses": 5, "note": "给同事"}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        code = r.json()["invite"]["code"]
        self.assertTrue(code)
        self.assertIsNotNone(self.store.invite(code))

    def test_the_admin_can_extend_and_freeze_an_entitlement(self):
        _token, cap_headers, _oid = self.create_paid_client()
        uid = self.uid()
        before = self.store.entitlement(uid)["paid_until"]
        ac = self.admin_app()
        r = ac.post("/admin/api/billing/entitlements/%s/adjust" % uid,
                    json={"action": "extend", "days": 30, "confirm": uid},
                    headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        after = self.store.entitlement(uid)["paid_until"]
        self.assertAlmostEqual(after, before + 30 * 86400, delta=5)
        r = ac.post("/admin/api/billing/entitlements/%s/adjust" % uid,
                    json={"action": "freeze", "confirm": uid}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        # 冻结后下一个能力请求 402（权益立即失效）
        self.assertEqual(self.asr(cap_headers).status_code, 402)

    def test_the_admin_can_reset_all_devices(self):
        _token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        ac = self.admin_app()
        r = ac.post("/admin/api/account/users/%s/reset-devices" % uid,
                    json={"confirm": uid}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.count_devices(uid), 0)
        self.assertEqual(self.asr(headers).status_code, 403)


class TtsBillingTests(_BillingCase):
    """`/v1/tts` 的收费口径（2026-10-31 的 edge-tts 通道 + 2026-11-03 收费）。

    不用真的 edge-tts：patch 掉 `routes.tts_mod.synthesize`，只看闸门与记账。
    """

    def _patch_synth(self):
        from unittest.mock import AsyncMock
        p = patch("server.routes.tts_mod.synthesize",
                  new=AsyncMock(return_value=b"fake-mp3"))
        p.start()
        self.addCleanup(p.stop)

    def _enable_tts(self, cfg):
        cfg.raw.setdefault("tts", {})["enabled"] = True

    def test_tts_chars_are_metered_and_the_daily_cap_is_enforced(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        row = dict(self.store.entitlement(uid))
        row["tts_chars"] = 10
        row["qps"] = 1000
        self.store.upsert_entitlement(row)
        self.svc.forget_entitlement(uid)
        self._patch_synth()
        first = self.client.post("/v1/tts", json={"text": "123456"}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.store.entitlement(uid)["tts_chars"], 10)
        self.assertEqual(self.state.tts_ledger.remaining(uid), 4)
        time.sleep(0.02)
        second = self.client.post("/v1/tts", json={"text": "123456"}, headers=headers)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["code"], "quota_exceeded")
        self.assertEqual(self.state.tts_ledger.remaining(uid), 4)

    def test_tts_uses_the_user_level_one_qps(self):
        _token, headers, _oid = self.create_paid_client()
        self._patch_synth()
        first = self.client.post("/v1/tts", json={"text": "hi"}, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post("/v1/tts", json={"text": "hi"}, headers=headers)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["code"], "rate_limited")
        self.assertEqual(second.headers.get("Retry-After"), "1")

    def test_tts_does_not_consume_the_asr_minute_quota(self):
        _token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        self._patch_synth()
        before = self.state.user_quota.used_minutes(uid)
        r = self.client.post("/v1/tts", json={"text": "hello"}, headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.state.user_quota.used_minutes(uid), before)

    def test_an_expired_account_gets_402_before_spending_tts(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        row = dict(self.store.entitlement(uid))
        row["paid_until"] = time.time() - 86400
        self.store.upsert_entitlement(row)
        self.svc.forget_entitlement(uid)
        self._patch_synth()
        r = self.client.post("/v1/tts", json={"text": "hi"}, headers=headers)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["code"], "entitlement_expired")


class TransactionRegressionTests(_BillingCase):
    """review P1.1：唯一索引失败后必须 rollback，否则下一个 BEGIN IMMEDIATE 500。"""

    def test_duplicate_notify_does_not_poison_the_next_pairing_transaction(self):
        self.assertTrue(self.store.record_notify("trade-1", "ord-1"))
        self.assertFalse(self.store.record_notify("trade-1", "ord-1"))
        self.store.put_pairing_code("hash-1", 60)
        self.assertIsNotNone(self.store.take_pairing_code("hash-1"))

    def test_duplicate_employee_does_not_poison_the_next_invite_transaction(self):
        self.store.create_user("usr-1", name="A", employee_no="E1")
        with self.assertRaises(ValueError):
            self.store.create_user("usr-2", name="B", employee_no="E1")
        self.store.put_invite("AFTER-ROLLBACK")
        self.assertTrue(self.store.consume_invite("AFTER-ROLLBACK"))


class PaymentAtomicityTests(_BillingCase):
    """review P1.2 / P2.1：paid 与权益同事务；重复回调自愈；并发只加一次。"""

    def _register_no_trial(self):
        self.cfg.raw["trial"]["enabled"] = False
        user = self.register()
        return user["userToken"]

    def test_a_failed_payment_does_not_leave_paid_without_entitlement(self):
        token = self._register_no_trial()
        order = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                 headers=self.uh(token)).json()
        order_id = order["orderId"]
        # 模拟"回调时套餐模板被删除"：plan_not_found 在事务之前失败 → 订单保持 paying。
        self.store.delete_plan("monthly")
        r = self.client.post("/v1/billing/mock/pay/" + order_id, json={})
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.store.order(order_id)["status"], "paying")
        self.assertIsNone(self.store.entitlement(
            self.uid()))
        # 恢复套餐后重投同一笔 → 正常入账（不会"永远 duplicate"）。
        self.svc.seed()
        r = self.client.post("/v1/billing/mock/pay/" + order_id, json={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.order(order_id)["status"], "paid")
        self.assertIsNotNone(self.store.entitlement(
            self.uid()))
        self.assertIn("fulfilled", json.loads(self.store.order(order_id)["meta"]))

    def test_duplicate_callback_repairs_a_missing_entitlement(self):
        token = self._register_no_trial()
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        now = time.time()
        self.store.update_order(order_id, status="paid", paid_at=now,
                                alipay_trade_no="trade-repair", meta="{}")
        self.store.record_notify("trade-repair", order_id)
        uid = self.uid()
        self.assertIsNone(self.store.entitlement(uid))
        r = self.client.post("/v1/billing/mock/notify",
                             json={"outTradeNo": order_id, "tradeNo": "trade-repair"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["duplicate"])
        ent = self.store.entitlement(uid)
        self.assertIsNotNone(ent, "重复回调没有自愈缺失的权益")
        self.assertEqual(ent["status"], "active")

    def test_a_non_success_notify_does_not_block_a_later_success(self):
        token = self._register_no_trial()
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        # 先到一条 TRADE_CLOSED，同一 trade_no 落了一行非成功通知。
        r = self.client.post("/v1/billing/mock/notify",
                             json={"outTradeNo": order_id, "tradeNo": "T-EDGE",
                                   "status": "closed"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ignored")
        # 后来同一 trade_no 的成功通知必须能结算，而不是被唯一键永久挡住。
        r = self.client.post("/v1/billing/mock/notify",
                             json={"outTradeNo": order_id, "tradeNo": "T-EDGE"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.order(order_id)["status"], "paid")
        self.assertIsNotNone(self.store.entitlement(
            self.uid()))
        self.assertEqual(self.store.notify_record("T-EDGE")["status"], "paid")

    def test_a_late_payment_on_a_closed_order_is_recorded_for_reconciliation(self):
        token = self._register_no_trial()
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        self.store.update_order(order_id, status="closed")
        r = self.client.post("/v1/billing/mock/notify",
                             json={"outTradeNo": order_id, "tradeNo": "T-LATE"})
        self.assertEqual(r.status_code, 409, r.text)
        rec = self.store.notify_record("T-LATE")
        self.assertIsNotNone(rec, "迟到支付没有留下 alipay_notify 记录")
        self.assertEqual(rec["status"], "conflict")
        self.assertEqual(self.store.order(order_id)["status"], "closed")

    def test_two_threads_with_the_same_trade_extend_only_once(self):
        token = self._register_no_trial()
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        norm = {"tradeNo": "trade-concurrent", "outTradeNo": order_id,
                "amountCents": 1000, "status": "paid", "verified": True}
        errors_seen = []
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                results.append(self.svc.apply_notify(dict(norm)))
            except Exception as exc:                            # pragma: no cover - 失败才看
                errors_seen.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors_seen, [])
        self.assertEqual(len(results), 2)
        uid = self.uid()
        ent = self.store.entitlement(uid)
        self.assertIsNotNone(ent)
        self.assertAlmostEqual(ent["paid_until"], time.time() + 30 * 86400, delta=120)
        self.assertEqual(self.store.order(order_id)["status"], "paid")


class PairCodeCasTests(_BillingCase):
    def test_concurrent_code_install_only_one_wins(self):
        token = self.register()["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        barrier = threading.Barrier(2)
        results = []

        def worker(tag):
            row = {"code_hash": "cas-" + tag, "ttl_s": 600,
                   "created_by": "test", "name": "n", "scopes": "asr",
                   "client_id": "", "user_id": "", "kind": "purchase"}
            barrier.wait()
            results.append(self.store.install_pair_code(order_id, "", row,
                                                        {"pairNonce": tag}))

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len([r for r in results if r.get("ok")]), 1, results)
        self.assertEqual(len(self.store.pairing_codes()), 1)
        self.assertIn(self.store.order(order_id)["pairing_code_id"],
                      {"cas-0", "cas-1"})


class RefundRecomputeTests(_BillingCase):
    def test_refunding_one_of_two_stacked_orders_keeps_the_other(self):
        token, _headers, first = self.create_paid_client()
        second = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                  headers=self.uh(token)).json()["orderId"]
        self.assertEqual(self.client.post("/v1/billing/mock/pay/" + second,
                                          json={}).status_code, 200)
        uid = self.uid()
        self.assertEqual(self.svc.refund_order(first)["status"], "refunded")
        ent = self.store.entitlement(uid)
        self.assertEqual(ent["status"], "active")
        self.assertAlmostEqual(ent["paid_until"], time.time() + 30 * 86400, delta=120)
        self.svc.refund_order(second)
        self.assertEqual(self.store.entitlement(uid)["status"], "refunded")

    def test_refund_reason_is_in_the_order_events(self):
        _token, _headers, order_id = self.create_paid_client()
        self.svc.refund_order(order_id, reason="用户申请")
        events = self.store.order_events(order_id)
        blob = json.dumps(events, ensure_ascii=False)
        self.assertIn("用户申请", blob)
        self.assertIn("refunded", blob)


class RenewalRuleTests(_BillingCase):
    """issue #13：期内/过期/多笔/退款/换套餐/预计到期的唯一口径。"""

    def add_plan(self, code="yearly", days=365, price_cents=12000):
        self.store.upsert_plan({
            "code": code, "name": "年卡", "price_cents": price_cents, "days": days,
            "daily_audio_minutes": 120, "tts_chars": 50000, "qps": 1,
            "max_devices": 3, "max_concurrent": 1, "active": 1, "sort": 30,
        })

    def uid(self, label="E001"):
        user = self.store.user_by_email("%s@example.com" % str(label).lower())
        self.assertIsNotNone(user, "测试账号不存在：%s" % label)
        return user["user_id"]

    def test_early_renewal_projection_matches_the_actual_expiry(self):
        token, _headers, _first = self.create_paid_client()
        uid = self.uid()
        before = self.store.entitlement(uid)["paid_until"]
        created = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                   headers=self.uh(token)).json()
        self.assertEqual(created["renewalDays"], 30)
        self.assertEqual(created["renewalBasis"], "paid_until")
        self.assertAlmostEqual(created["currentPaidUntil"], before, delta=5)
        self.assertAlmostEqual(created["projectedPaidUntil"], before + 30 * 86400,
                               delta=5)
        self.assertIn("期内续费", created["renewalRule"])
        self.client.post("/v1/billing/mock/pay/" + created["orderId"], json={})
        actual = self.store.entitlement(uid)["paid_until"]
        self.assertAlmostEqual(actual, created["projectedPaidUntil"], delta=5,
                               msg="预览与入账必须是同一套口径")
        detail = self.client.get("/v1/billing/orders/" + created["orderId"],
                                 headers=self.uh(token)).json()
        self.assertEqual(detail["status"], "paid")
        self.assertAlmostEqual(detail["projectedPaidUntil"], actual, delta=5,
                               msg="已支付订单的预计到期要等于实际 paid_until")
        self.assertAlmostEqual(detail["currentPaidUntil"], before, delta=5)

    def test_expired_renewal_starts_from_payment_time(self):
        token, _headers, _first = self.create_paid_client()
        uid = self.uid()
        row = dict(self.store.entitlement(uid))
        row["paid_until"] = time.time() - 3 * 86400
        self.store.upsert_entitlement(row)
        self.svc.forget_entitlement(uid)
        created = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                   headers=self.uh(token)).json()
        self.assertEqual(created["renewalBasis"], "payment_time")
        self.assertAlmostEqual(created["projectedPaidUntil"], time.time() + 30 * 86400,
                               delta=120)
        self.client.post("/v1/billing/mock/pay/" + created["orderId"], json={})
        actual = self.store.entitlement(uid)["paid_until"]
        self.assertAlmostEqual(actual, time.time() + 30 * 86400, delta=120)

    def test_three_consecutive_orders_stack_in_payment_order(self):
        token, _headers, _first = self.create_paid_client()
        uid = self.uid()
        first = self.store.entitlement(uid)["paid_until"]
        for _ in range(2):
            order_id = self.client.post(
                "/v1/billing/orders", json={"planCode": "monthly"},
                headers=self.uh(token)).json()["orderId"]
            self.assertEqual(self.client.post(
                "/v1/billing/mock/pay/" + order_id, json={}).status_code, 200)
        self.assertAlmostEqual(self.store.entitlement(uid)["paid_until"],
                               first + 60 * 86400, delta=10)

    def test_refunding_the_second_order_keeps_the_first_term(self):
        token, _headers, first_order = self.create_paid_client()
        uid = self.uid()
        first_paid_until = self.store.entitlement(uid)["paid_until"]
        second = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                  headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + second, json={})
        self.assertEqual(self.store.entitlement(uid)["paid_until"],
                         first_paid_until + 30 * 86400)
        out = self.svc.refund_order(second, reason="退第二笔")
        self.assertEqual(out["status"], "refunded")
        ent = self.store.entitlement(uid)
        self.assertEqual(ent["status"], "active")
        self.assertAlmostEqual(ent["paid_until"], first_paid_until, delta=5)
        self.assertEqual(self.store.order(first_order)["status"], "paid")

    def test_refunding_both_orders_zeroes_the_expiry_and_blocks_access(self):
        token, headers, first_order = self.create_paid_client()
        uid = self.uid()
        second = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                  headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + second, json={})
        self.svc.refund_order(first_order)
        self.svc.refund_order(second)
        ent = self.store.entitlement(uid)
        self.assertEqual(ent["status"], "refunded")
        self.assertEqual(ent["paid_until"], 0.0, "只剩退款单时不能再显示未来到期日")
        self.assertEqual(self.asr(headers).status_code, 402)

    def test_refund_then_renew_starts_from_the_new_payment(self):
        token, _headers, order_id = self.create_paid_client()
        uid = self.uid()
        self.svc.refund_order(order_id)
        before = time.time()
        created = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                   headers=self.uh(token)).json()
        self.assertEqual(created["renewalBasis"], "payment_time")
        self.assertGreaterEqual(created["projectedPaidUntil"], before + 30 * 86400 - 5)
        self.client.post("/v1/billing/mock/pay/" + created["orderId"], json={})
        ent = self.store.entitlement(uid)
        self.assertEqual(ent["status"], "active")
        self.assertAlmostEqual(ent["paid_until"], time.time() + 30 * 86400, delta=120)

    def test_trial_does_not_stack_into_the_first_paid_month(self):
        user = self.register()
        token = user["userToken"]
        uid = self.uid()
        trial_until = self.store.entitlement(uid)["paid_until"]
        self.assertEqual(self.store.entitlement(uid)["plan_code"], "trial")
        created = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                   headers=self.uh(token)).json()
        self.assertEqual(created["renewalBasis"], "payment_time")
        self.assertAlmostEqual(created["projectedPaidUntil"], time.time() + 30 * 86400,
                               delta=120)
        self.assertLess(created["projectedPaidUntil"], trial_until + 30 * 86400)
        self.client.post("/v1/billing/mock/pay/" + created["orderId"], json={})
        ent = self.store.entitlement(uid)
        self.assertEqual(ent["plan_code"], "monthly")
        self.assertAlmostEqual(ent["paid_until"], time.time() + 30 * 86400, delta=120)

    def test_changing_plan_while_active_is_rejected_before_an_order_exists(self):
        token, _headers, _first = self.create_paid_client()
        self.add_plan("yearly")
        before = len(self.store.orders(user_id=self.uid(), limit=1000))
        r = self.client.post("/v1/billing/orders", json={"planCode": "yearly"},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "plan_change_unsupported")
        self.assertIn("暂不支持升级/降级", r.json()["message"])
        self.assertEqual(len(self.store.orders(user_id=self.uid(), limit=1000)), before,
                         "被拒的换套餐不能留下 closed 孤儿订单")
        # 同套餐续费仍然允许。
        same = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                headers=self.uh(token))
        self.assertEqual(same.status_code, 200, same.text)

    def test_an_expired_account_may_buy_a_different_plan(self):
        token, _headers, _first = self.create_paid_client()
        uid = self.uid()
        row = dict(self.store.entitlement(uid))
        row["paid_until"] = time.time() - 86400
        self.store.upsert_entitlement(row)
        self.svc.forget_entitlement(uid)
        self.add_plan("yearly")
        r = self.client.post("/v1/billing/orders", json={"planCode": "yearly"},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.client.post("/v1/billing/mock/pay/" + r.json()["orderId"], json={})
        ent = self.store.entitlement(uid)
        self.assertEqual(ent["plan_code"], "yearly")
        self.assertAlmostEqual(ent["paid_until"], time.time() + 365 * 86400, delta=120)

    def test_internal_accounts_cannot_be_downgraded_by_a_purchase(self):
        token = self.register()["userToken"]
        uid = self.uid()
        self.svc.adjust_entitlement(uid, {"action": "internal"})
        r = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "plan_change_unsupported")
        self.assertIn("内部账号", r.json()["message"])
        self.assertEqual(self.store.entitlement(uid)["plan_code"], "internal")

    def test_order_list_keeps_refunded_and_closed_rows(self):
        token, _headers, paid_order = self.create_paid_client()
        closed = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                  headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/orders/%s/cancel" % closed,
                         headers=self.uh(token))
        refunded = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + refunded, json={})
        self.svc.refund_order(refunded)
        rows = self.client.get("/v1/billing/orders", headers=self.uh(token)).json()["orders"]
        statuses = {row["orderId"]: row["status"] for row in rows}
        self.assertEqual(statuses[paid_order], "paid")
        self.assertEqual(statuses[closed], "closed")
        self.assertEqual(statuses[refunded], "refunded")

    def test_entitlement_summary_carries_the_rules(self):
        token = self.register()["userToken"]
        body = self.client.get("/v1/billing/entitlement", headers=self.uh(token)).json()
        self.assertTrue(body["renewalRule"])
        self.assertTrue(body["planChangeRule"])
        self.assertTrue(body["refundRule"])
        self.assertFalse(body["autoRenew"])
        self.assertEqual(body["renewalDays"], 7)      # 试用套餐天数来自 plans 表


class BillingHardeningTests(_BillingCase):
    def test_a_disabled_user_does_not_fall_back_to_the_legacy_free_path(self):
        _token, headers, _oid = self.create_paid_client()
        uid = self.uid()
        self.store.update_user(uid, status="disabled")
        r = self.asr(headers)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "forbidden")

    def test_expired_unpaid_orders_are_closed(self):
        token = self.register()["userToken"]
        saved = float(self.cfg.get("billing.order_ttl_s", 900))
        self.cfg.raw["billing"]["order_ttl_s"] = -1
        try:
            first = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                     headers=self.uh(token)).json()["orderId"]
            self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                             headers=self.uh(token))
            self.assertEqual(self.store.order(first)["status"], "closed")
        finally:
            self.cfg.raw["billing"]["order_ttl_s"] = saved

    def test_tts_config_key_is_read(self):
        self.cfg.raw["billing"]["daily_tts_chars"] = 7
        self.assertEqual(self.svc.limits("usr-none")["tts_chars"], 7)

    def test_effective_gating_needs_auth(self):
        self.assertTrue(self.svc.effective_gating)
        self.cfg.raw["auth"]["enabled"] = False
        self.assertFalse(self.svc.effective_gating)

    def test_mock_endpoints_refuse_non_loopback_sources(self):
        token = self.register()["userToken"]
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        remote = TestClient(self.app, client=("10.0.0.9", 50000))
        try:
            r = remote.post("/v1/billing/mock/pay/" + order_id, json={})
            self.assertEqual(r.status_code, 403, r.text)
        finally:
            remote.close()
        self.cfg.raw["payment"]["mock_allow_remote"] = True
        remote = TestClient(self.app, client=("10.0.0.9", 50000))
        try:
            r = remote.post("/v1/billing/mock/pay/" + order_id, json={})
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            remote.close()


class PlanMergeTests(_BillingCase):
    def test_putting_only_price_does_not_reset_trial(self):
        ac = self.admin_app()
        r = ac.put("/admin/api/billing/plans/trial",
                   json={"priceCents": 2800}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        plan = self.store.plan("trial")
        self.assertEqual(plan["price_cents"], 2800)
        self.assertEqual(plan["days"], 7)
        self.assertEqual(plan["daily_audio_minutes"], 120.0)
        self.assertEqual(plan["tts_chars"], 50000)
        self.assertEqual(plan["active"], 1)

    def test_plan_change_is_audited_with_before_and_after(self):
        ac = self.admin_app()
        r = ac.put("/admin/api/billing/plans/trial",
                   json={"priceCents": 2800}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        rows = [a for a in self.store.recent_audit(50)
                if a["action"] == "billing-plan-set.diff"]
        self.assertTrue(rows)
        self.assertIn('"before"', rows[0]["target"])
        self.assertIn('"after"', rows[0]["target"])


class PaymentUnitHardeningTests(unittest.TestCase):
    def test_mock_secret_is_random_when_not_configured(self):
        a = payment_mod.MockGateway("")
        b = payment_mod.MockGateway("")
        self.assertTrue(a.generated_secret)
        self.assertNotEqual(a.secret, b.secret)
        form = a.build_form({"order_id": "ord-1", "amount_cents": 1000})
        self.assertEqual(a.verify_notify(form)["status"], "paid")

    def test_alipay_sandbox_maps_the_gateway(self):
        cfg = settings_mod.load()
        cfg.raw["alipay"]["sandbox"] = True
        cfg.raw["alipay"]["gateway"] = payment_mod.ALIPAY_PROD_GATEWAY
        gw = payment_mod.AlipayGateway(cfg)
        self.assertEqual(gw.gateway, payment_mod.ALIPAY_SANDBOX_GATEWAY)
        self.assertIsInstance(gw.seller_id, str)


class Phase3OrderFlowTests(_BillingCase):
    """Phase 3：官网支付页用到的订单列表 / 主动 sync / 支付模式信息。"""

    def _order(self, token, plan="monthly"):
        r = self.client.post("/v1/billing/orders", json={"planCode": plan},
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_plans_expose_payment_mode_without_secrets(self):
        r = self.client.get("/v1/billing/plans")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        payment = data["payment"]
        self.assertEqual(payment["provider"], "mock")
        self.assertTrue(payment["configured"])
        self.assertFalse(payment["sandbox"])
        self.assertIn("serverTime", data)
        self.assertNotIn(self.tmp, json.dumps(data), "套餐接口不能泄漏密钥/路径")

    def test_order_view_carries_plan_name_days_amount_and_sandbox(self):
        token = self.register()["userToken"]
        order = self._order(token)
        self.assertEqual(order["planName"], "月卡")
        self.assertEqual(order["days"], 30)
        self.assertEqual(order["amountCents"], 1000)
        self.assertFalse(order["sandbox"])
        self.assertTrue(order["payUrl"].startswith("mock://pay/"))
        self.assertEqual(order["status"], "paying")

    def test_sync_returns_the_current_order_for_a_paying_order(self):
        token = self.register()["userToken"]
        order = self._order(token)
        r = self.client.post("/v1/billing/orders/%s/sync" % order["orderId"],
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["orderId"], order["orderId"])
        self.assertEqual(body["status"], "paying")
        self.assertEqual(body["amountCents"], 1000)

    def test_sync_after_mock_payment_returns_paid(self):
        token = self.register()["userToken"]
        order = self._order(token)
        r = self.client.post("/v1/billing/mock/pay/" + order["orderId"], json={})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/v1/billing/orders/%s/sync" % order["orderId"],
                             headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "paid")

    def test_sync_marks_a_paid_query_result_and_grants_entitlement(self):
        token = self.register()["userToken"]
        order = self._order(token)
        fake = {
            "status": "paid", "tradeNo": "mock-sync-trade",
            "outTradeNo": order["orderId"], "amountCents": 1000,
            "verified": True, "buyerId": "",
        }
        with patch.object(self.svc.gateway, "query", return_value=fake):
            r = self.client.post("/v1/billing/orders/%s/sync" % order["orderId"],
                                 headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "paid")
        uid = self.uid()
        ent = self.store.entitlement(uid)
        self.assertIsNotNone(ent)
        self.assertEqual(ent["status"], "active")
        self.assertGreater(float(ent["paid_until"]), time.time())

    def test_sync_closes_an_order_when_alipay_says_trade_closed(self):
        token = self.register()["userToken"]
        order = self._order(token)
        fake = {"status": "closed", "tradeNo": "mock-closed-trade",
                "outTradeNo": order["orderId"], "amountCents": 1000,
                "verified": True, "rawStatus": "TRADE_CLOSED"}
        with patch.object(self.svc.gateway, "query", return_value=fake):
            r = self.client.post("/v1/billing/orders/%s/sync" % order["orderId"],
                                 headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "closed")
        self.assertEqual(self.store.order(order["orderId"])["status"], "closed")

    def test_sync_is_scoped_to_the_order_owner(self):
        token = self.register("E001")["userToken"]
        other = self.register("E002")["userToken"]
        order = self._order(token)
        r = self.client.post("/v1/billing/orders/%s/sync" % order["orderId"],
                             headers=self.uh(other))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "order_not_found")

    def test_orders_list_is_scoped_to_the_current_user_and_read_only(self):
        token = self.register("E001")["userToken"]
        other = self.register("E002")["userToken"]
        first = self._order(token)
        second = self._order(token)
        third = self._order(other)
        r = self.client.get("/v1/billing/orders", headers=self.uh(token))
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["orders"]
        self.assertEqual({row["orderId"] for row in rows},
                         {first["orderId"], second["orderId"]})
        for row in rows:
            self.assertNotIn("pairUrl", row, "列表是只读的，不能触发补发码")
        r = self.client.get("/v1/billing/orders", headers=self.uh(other))
        self.assertEqual([row["orderId"] for row in r.json()["orders"]],
                         [third["orderId"]])

    def test_orders_list_requires_a_user_token(self):
        r = self.client.get("/v1/billing/orders")
        self.assertEqual(r.status_code, 401)
