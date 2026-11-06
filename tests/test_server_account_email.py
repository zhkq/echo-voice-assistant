# -*- coding: utf-8 -*-
"""邮箱即账号（2026-11-06，issue #15）：发码 / 注册 / 登录 / 重置密码。

这里覆盖的是**邮箱账号**的完整矩阵；设备绑定与令牌域隔离在
`tests/test_server_account.py`。测试不经过真实邮件：注册码直接写哈希，发码流程用
`dev` provider 的日志验证。
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import emailer as emailer_mod                        # noqa: E402
from server.store import Store                                   # noqa: E402
from tests.account_test_helpers import put_email_code            # noqa: E402
from tests.test_server_account import _AccountCase                # noqa: E402


class SendCodeTests(_AccountCase):
    def test_dev_provider_logs_the_code_and_persists_only_a_hash(self):
        with self.assertLogs("echo.account.email", level="WARNING") as logs:
            r = self.client.post("/v1/account/email/send-code",
                                 json={"email": "User@Example.COM",
                                       "purpose": "register"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["purpose"], "register")
        self.assertEqual(body["expiresIn"], 600)
        joined = "\n".join(logs.output)
        self.assertIn("user@example.com", joined)
        self.assertIn("purpose=register", joined)
        # 明文只出现在 dev 日志里；库里只有 scrypt 哈希。
        row = self.store.email_code_latest("user@example.com", "register")
        self.assertIsNotNone(row)
        self.assertTrue(str(row["code_hash"]).startswith("scrypt$"))
        self.assertNotIn("123456", str(row["code_hash"]))

    def test_the_second_send_within_sixty_seconds_is_rate_limited(self):
        first = self.client.post("/v1/account/email/send-code",
                                 json={"email": "a@example.com",
                                       "purpose": "register"})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post("/v1/account/email/send-code",
                                  json={"email": "a@example.com",
                                        "purpose": "register"})
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["code"], "rate_limited")
        self.assertIn("Retry-After", second.headers)

    def test_each_email_has_an_hourly_cap(self):
        self.cfg.raw["account"]["email"]["resend_cooldown_s"] = 0
        self.cfg.raw["account"]["email"]["max_per_email_hour"] = 2
        for _ in range(2):
            self.assertEqual(self.client.post(
                "/v1/account/email/send-code",
                json={"email": "cap@example.com", "purpose": "register"}).status_code, 200)
        third = self.client.post("/v1/account/email/send-code",
                                 json={"email": "cap@example.com", "purpose": "register"})
        self.assertEqual(third.status_code, 429, third.text)
        self.assertEqual(third.json()["code"], "rate_limited")

    def test_each_ip_has_an_hourly_cap(self):
        self.cfg.raw["account"]["email"]["resend_cooldown_s"] = 0
        self.cfg.raw["account"]["email"]["max_per_email_hour"] = 100
        self.cfg.raw["account"]["email"]["max_per_ip_hour"] = 2
        for addr in ("one@example.com", "two@example.com"):
            self.assertEqual(self.client.post(
                "/v1/account/email/send-code",
                json={"email": addr, "purpose": "register"}).status_code, 200)
        third = self.client.post("/v1/account/email/send-code",
                                 json={"email": "three@example.com",
                                       "purpose": "register"})
        self.assertEqual(third.status_code, 429, third.text)

    def test_invalid_email_and_purpose_are_rejected(self):
        r = self.client.post("/v1/account/email/send-code",
                             json={"email": "not-an-email", "purpose": "register"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid_email")
        r = self.client.post("/v1/account/email/send-code",
                             json={"email": "a@example.com", "purpose": "login"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid_purpose")

    def test_reset_for_an_unknown_email_does_not_reveal_existence(self):
        r = self.client.post("/v1/account/email/send-code",
                             json={"email": "nobody@example.com", "purpose": "reset"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.store.email_code_latest("nobody@example.com", "reset"))

    def test_a_failed_send_leaves_no_valid_code(self):
        class Boom:
            def send_code(self, **_kwargs):
                raise emailer_mod.EmailSendError("smtp down")
        self.state.account.email_sender = Boom()
        r = self.client.post("/v1/account/email/send-code",
                             json={"email": "fail@example.com", "purpose": "register"})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()["code"], "email_send_failed")
        self.assertIsNone(self.store.email_code_latest("fail@example.com", "register"))

    def test_disabled_and_unconfigured_are_distinct_503(self):
        self.cfg.raw["account"]["email"]["enabled"] = False
        r = self.client.post("/v1/account/email/send-code",
                             json={"email": "a@example.com", "purpose": "register"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "email_disabled")
        self.cfg.raw["account"]["email"]["enabled"] = True
        self.state.account.email_sender = None
        self.state.account.email_error = "provider=api 未实现"
        r = self.client.post("/v1/account/email/send-code",
                             json={"email": "a@example.com", "purpose": "register"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["code"], "email_unconfigured")


class RegisterTests(_AccountCase):
    def test_register_with_a_valid_code_invite_and_password(self):
        self.invite()
        r = self.register(email="User@Example.COM")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["userId"].startswith("usr-"))
        self.assertTrue(body["userToken"])
        self.assertEqual(body["user"]["email"], "user@example.com")
        self.assertTrue(body["user"]["emailVerified"])
        self.assertEqual(body["user"]["employeeNo"], "")
        self.assertTrue(body["trial"])
        user = self.store.user_by_email("user@example.com")
        self.assertIsNotNone(user)
        self.assertTrue(float(user["email_verified_at"]) > 0)
        self.assertTrue(str(user["password_hash"]).startswith("scrypt$"))
        # 邮箱账号登录不再需要邀请码
        login = self.login(email="user@example.com")
        self.assertEqual(login.status_code, 200, login.text)
        self.assertTrue(login.json()["userToken"])

    def test_an_invite_is_required_by_default(self):
        r = self.register(invite_code="")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["code"], "invite_invalid")
        self.assertIsNone(self.store.user_by_email("user@example.com"))

    def test_wrong_expired_and_used_up_invites_are_refused(self):
        r = self.register(invite_code="NOPE")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "invite_invalid")

        self.store.put_invite("OLD", max_uses=1, expires_at=time.time() - 10)
        r = self.register(email="old@example.com", invite_code="OLD")
        self.assertEqual(r.status_code, 403)

        self.store.put_invite("ONE", max_uses=1)
        self.assertEqual(self.register(email="one@example.com",
                                       invite_code="ONE").status_code, 200)
        r = self.register(email="two@example.com", invite_code="ONE")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "invite_invalid")

    def test_invite_can_be_optional_when_config_says_so(self):
        self.cfg.raw["invite"]["required"] = False
        r = self.register(invite_code="")
        self.assertEqual(r.status_code, 200, r.text)

    def test_email_is_normalized_and_unique(self):
        self.invite()
        self.assertEqual(self.register(email="Case@Example.com").status_code, 200)
        put_email_code(self.store, "case@example.com", "register", "123456")
        r = self.register(email="CASE@example.com")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "email_taken")
        self.assertEqual(len([u for u in self.store.users()
                              if u["email"] == "case@example.com"]), 1)

    def test_wrong_and_expired_codes_are_refused_without_creating_a_user(self):
        self.invite()
        put_email_code(self.store, "u@example.com", "register", "123456")
        r = self.client.post("/v1/account/register",
                             json={"email": "u@example.com", "code": "000000",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_invalid")
        self.assertIsNone(self.store.user_by_email("u@example.com"))

        put_email_code(self.store, "e@example.com", "register", "123456", ttl_s=-1)
        r = self.client.post("/v1/account/register",
                             json={"email": "e@example.com", "code": "123456",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_expired")

    def test_five_wrong_attempts_invalidate_the_code(self):
        self.invite()
        put_email_code(self.store, "u@example.com", "register", "123456")
        for _ in range(4):
            r = self.client.post("/v1/account/register",
                                 json={"email": "u@example.com", "code": "000000",
                                       "password": "Passw0rd", "inviteCode": "INVITE-1"})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(r.json()["code"], "verification_code_invalid")
        r = self.client.post("/v1/account/register",
                             json={"email": "u@example.com", "code": "000000",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["code"], "verification_code_attempts_exceeded")
        # 正确码也不行了
        r = self.client.post("/v1/account/register",
                             json={"email": "u@example.com", "code": "123456",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_purpose_is_isolated_and_a_code_is_single_use(self):
        self.invite()
        put_email_code(self.store, "u@example.com", "reset", "123456")
        r = self.client.post("/v1/account/register",
                             json={"email": "u@example.com", "code": "123456",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_invalid")

        self.assertEqual(self.register(email="once@example.com").status_code, 200)
        r = self.client.post("/v1/account/register",
                             json={"email": "once@example.com", "code": "123456",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_invalid")

    def test_weak_passwords_are_refused(self):
        self.invite()
        for password in ("short1", "allletters", "12345678"):
            r = self.register(email="weak@example.com", password=password)
            self.assertEqual(r.status_code, 400, (password, r.text))
            self.assertEqual(r.json()["code"], "password_weak")

    def test_name_is_optional_and_stored_when_given(self):
        self.invite()
        put_email_code(self.store, "named@example.com", "register", "123456")
        r = self.client.post("/v1/account/register",
                             json={"email": "named@example.com", "code": "123456",
                                   "password": "Passw0rd", "inviteCode": "INVITE-1",
                                   "name": "张三"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["user"]["name"], "张三")
        self.assertEqual(self.store.user_by_email("named@example.com")["name"], "张三")

    def test_legacy_employee_payload_no_longer_registers(self):
        r = self.client.post("/v1/account/register",
                             json={"inviteCode": "INVITE-1", "name": "张三",
                                   "employeeNo": "E001"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid_email")


class LoginTests(_AccountCase):
    def setUp(self):
        super().setUp()
        self.invite()
        self.body = self.register().json()

    def test_email_login_does_not_need_an_invite_and_updates_last_login(self):
        before = self.store.user_by_email("user@example.com")["last_login"]
        r = self.login()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(float(self.store.user_by_email(
            "user@example.com")["last_login"]), float(before))
        self.assertGreater(float(self.store.user_by_email(
            "user@example.com")["last_login"]), 0)

    def test_wrong_password_and_unknown_email_share_one_generic_error(self):
        a = self.login(password="WrongPass1")
        b = self.login(email="nobody@example.com", password="WrongPass1")
        self.assertEqual(a.status_code, 401)
        self.assertEqual(b.status_code, 401)
        self.assertEqual(a.json()["code"], "unauthorized")
        self.assertEqual(b.json()["code"], "unauthorized")
        self.assertEqual(a.json()["detail"], b.json()["detail"])

    def test_legacy_employee_login_no_longer_works(self):
        r = self.client.post("/v1/account/login",
                             json={"employeeNo": "E001", "inviteCode": "INVITE-1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid_email")

    def test_disabled_account_is_forbidden_only_after_the_password_is_right(self):
        uid = self.store.user_by_email("user@example.com")["user_id"]
        self.store.update_user(uid, status="disabled")
        r = self.login(password="WrongPass1")
        self.assertEqual(r.status_code, 401)
        r = self.login()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "forbidden")

    def test_login_is_throttled_after_five_failures(self):
        for _ in range(5):
            r = self.login(password="WrongPass1")
            self.assertEqual(r.status_code, 401, r.text)
        r = self.login()
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(r.json()["code"], "rate_limited")
        self.assertIn("Retry-After", r.headers)


class ResetPasswordTests(_AccountCase):
    def setUp(self):
        super().setUp()
        self.invite()
        self.body = self.register().json()
        self.old_token = self.body["userToken"]

    def test_reset_changes_the_password_and_revokes_old_tokens(self):
        put_email_code(self.store, "user@example.com", "reset", "654321")
        r = self.client.post("/v1/account/password/reset",
                             json={"email": "user@example.com", "code": "654321",
                                   "newPassword": "NewPassw0rd"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ok"], True)
        self.assertEqual(self.login(password="Passw0rd").status_code, 401)
        self.assertEqual(self.login(password="NewPassw0rd").status_code, 200)
        r = self.client.get("/v1/account/me",
                            headers=self.headers(self.old_token))
        self.assertEqual(r.status_code, 401)

    def test_reset_rejects_wrong_expired_and_used_codes(self):
        put_email_code(self.store, "user@example.com", "reset", "654321")
        r = self.client.post("/v1/account/password/reset",
                             json={"email": "user@example.com", "code": "000000",
                                   "newPassword": "NewPassw0rd"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_invalid")

        put_email_code(self.store, "user@example.com", "reset", "654321", ttl_s=-1)
        r = self.client.post("/v1/account/password/reset",
                             json={"email": "user@example.com", "code": "654321",
                                   "newPassword": "NewPassw0rd"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_expired")

        put_email_code(self.store, "user@example.com", "reset", "654321")
        self.assertEqual(self.client.post(
            "/v1/account/password/reset",
            json={"email": "user@example.com", "code": "654321",
                  "newPassword": "NewPassw0rd"}).status_code, 200)
        # 同一个码再用一次
        r = self.client.post("/v1/account/password/reset",
                             json={"email": "user@example.com", "code": "654321",
                                   "newPassword": "AnotherPass1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_invalid")

    def test_reset_rejects_weak_passwords(self):
        put_email_code(self.store, "user@example.com", "reset", "654321")
        r = self.client.post("/v1/account/password/reset",
                             json={"email": "user@example.com", "code": "654321",
                                   "newPassword": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "password_weak")

    def test_reset_for_an_unknown_email_is_generic(self):
        put_email_code(self.store, "nobody@example.com", "reset", "654321")
        r = self.client.post("/v1/account/password/reset",
                             json={"email": "nobody@example.com", "code": "654321",
                                   "newPassword": "NewPassw0rd"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "verification_code_invalid")


class MigrationTests(unittest.TestCase):
    """老库（`users` 还没有 email/password_hash）打开后自动补列 + 建唯一索引。"""

    def test_an_old_users_table_is_migrated_without_losing_rows(self):
        tmp = tempfile.mkdtemp(prefix="echo-email-migrate-")
        path = os.path.join(tmp, "auth.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
            " employee_no TEXT NOT NULL DEFAULT '', invite_code TEXT NOT NULL DEFAULT '',"
            " phone TEXT NOT NULL DEFAULT '', alipay_open_id TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'active', token_version INTEGER NOT NULL DEFAULT 1,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        raw.execute("INSERT INTO users (user_id, name, employee_no, created_at, updated_at)"
                    " VALUES ('usr-old', '老账号', 'OLD1', 1, 1)")
        raw.commit()
        raw.close()

        store = Store(path)
        try:
            cols = store.columns()["users"]
            for name in ("email", "email_verified_at", "password_hash", "last_login"):
                self.assertIn(name, cols)
            old = store.user("usr-old")
            self.assertIsNotNone(old)
            self.assertEqual(old["email"], "")
            # 空邮箱不参与唯一索引：两个老账号都能存在。
            store.create_user("usr-old-2", name="另一个老账号")
            self.assertIsNone(store.user_by_email(""))
            # 新邮箱可以正常建，且重复会撞唯一索引。
            store.create_user("usr-new", email="a@example.com", password_hash="x")
            with self.assertRaises(ValueError):
                store.create_user("usr-new-2", email="a@example.com")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
