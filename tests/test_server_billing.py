# -*- coding: utf-8 -*-
"""套餐 / 订单 / 权益 / 支付 / 闸门（设计 §4.1–§4.6；任务书 T3–T7）。

这一组用例的目标是任务书 §7 的验收自测（除 TTS —— 本仓库当前**没有**
`/v1/tts` 端点，见 `server/README.md` 的说明；字符账本单独测）。
"""
import base64
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient                        # noqa: E402

from server import admin as admin_mod                            # noqa: E402
from server import auth as auth_mod                              # noqa: E402
from server import engines, errors, main as server_main          # noqa: E402
from server import quota as quota_mod                            # noqa: E402
from server import ratelimit                                     # noqa: E402
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

    def register(self, employee_no="E001"):
        r = self.client.post("/v1/account/register",
                             json={"inviteCode": "INVITE-1", "name": "张三",
                                   "employeeNo": employee_no})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def create_paid_client(self, employee_no="E001", plan="monthly"):
        """注册 → 下单 → Mock 付款 → 兑换配对码 → (userToken, clientHeaders, orderId)。"""
        user = self.register(employee_no)
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
            self.store.user_by_employee_no("E001")["user_id"])["paid_until"]
        second = self.client.post("/v1/billing/mock/notify", json=payload)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["duplicate"])
        again = self.store.entitlement(
            self.store.user_by_employee_no("E001")["user_id"])["paid_until"]
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
        uid = self.store.user_by_employee_no("E001")["user_id"]
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
            self.store.entitlement(self.store.user_by_employee_no("E001")["user_id"])
            ["plan_code"], "trial")


class EntitlementGateTests(_BillingCase):
    def test_an_expired_entitlement_returns_402_and_renewal_restores_it(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.store.user_by_employee_no("E001")["user_id"]
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
        uid = self.store.user_by_employee_no("E001")["user_id"]
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
        uid = self.store.user_by_employee_no("E001")["user_id"]
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
            self.store.user_by_employee_no("E001")["user_id"])["status"]
        self.assertEqual(status, "refunded")

    def test_internal_is_never_downgraded_by_a_purchase(self):
        token, headers, _oid = self.create_paid_client()
        uid = self.store.user_by_employee_no("E001")["user_id"]
        self.svc.adjust_entitlement(uid, {"action": "internal"})
        self.assertEqual(self.store.entitlement(uid)["plan_code"], "internal")
        self.assertEqual(self.store.entitlement(uid)["paid_until"], 0.0)
        order_id = self.client.post("/v1/billing/orders", json={"planCode": "monthly"},
                                    headers=self.uh(token)).json()["orderId"]
        self.client.post("/v1/billing/mock/pay/" + order_id, json={})
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
                    user = client.post("/v1/account/register",
                                       json={"inviteCode": "INV", "name": "张三",
                                             "employeeNo": "E001"}).json()
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
        uid = self.store.user_by_employee_no("E001")["user_id"]
        before = self.store.entitlement(uid)["paid_until"]
        ac = self.admin_app()
        r = ac.post("/admin/api/billing/entitlements/%s/adjust" % uid,
                    json={"action": "extend", "days": 30}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        after = self.store.entitlement(uid)["paid_until"]
        self.assertAlmostEqual(after, before + 30 * 86400, delta=5)
        r = ac.post("/admin/api/billing/entitlements/%s/adjust" % uid,
                    json={"action": "freeze"}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        # 冻结后下一个能力请求 402（权益立即失效）
        self.assertEqual(self.asr(cap_headers).status_code, 402)

    def test_the_admin_can_reset_all_devices(self):
        _token, headers, _oid = self.create_paid_client()
        uid = self.store.user_by_employee_no("E001")["user_id"]
        ac = self.admin_app()
        r = ac.post("/admin/api/account/users/%s/reset-devices" % uid,
                    json={"confirm": uid}, headers=self.wh())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.count_devices(uid), 0)
        self.assertEqual(self.asr(headers).status_code, 403)
