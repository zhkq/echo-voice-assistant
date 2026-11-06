# -*- coding: utf-8 -*-
"""邮件发送层：dev / smtp / 配置读取（issue #15）。不发真实邮件。"""
import os
import smtplib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import emailer, settings                               # noqa: E402


def _cfg(enabled=True, provider="smtp"):
    cfg = settings.load()
    section = cfg.raw.setdefault("account", {}).setdefault("email", {})
    section.update({
        "enabled": enabled,
        "provider": provider,
        "from": "no-reply@example.com",
        "from_name": "ECHO Test",
        "send_timeout_s": 10,
        "send_retries": 1,
    })
    section.setdefault("smtp", {}).update({
        "host": "smtp.example.com",
        "port": 465,
        "mode": "ssl",
        "username": "mailer@example.com",
        "password_env": "ECHO_SMTP_PASSWORD",
        "password_file": "",
    })
    return cfg


class BuildSenderTests(unittest.TestCase):
    def test_disabled_returns_no_sender(self):
        sender, error = emailer.build_sender(_cfg(enabled=False))
        self.assertIsNone(sender)
        self.assertIn("enabled=false", error)

    def test_dev_provider_returns_dev(self):
        sender, error = emailer.build_sender(_cfg(provider="dev"))
        self.assertIsInstance(sender, emailer.DevEmailSender)
        self.assertEqual(error, "")

    def test_api_provider_is_explicitly_unimplemented(self):
        sender, error = emailer.build_sender(_cfg(provider="api"))
        self.assertIsNone(sender)
        self.assertIn("api", error)

    def test_smtp_without_from_or_host_is_unconfigured(self):
        cfg = _cfg()
        cfg.raw["account"]["email"]["from"] = ""
        sender, error = emailer.build_sender(cfg)
        self.assertIsNone(sender)
        self.assertIn("from", error)
        cfg = _cfg()
        cfg.raw["account"]["email"]["smtp"]["host"] = ""
        sender, error = emailer.build_sender(cfg)
        self.assertIsNone(sender)
        self.assertIn("host", error)

    def test_smtp_with_username_but_no_password_is_unconfigured(self):
        cfg = _cfg()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ECHO_SMTP_PASSWORD", None)
            sender, error = emailer.build_sender(cfg)
        self.assertIsNone(sender)
        self.assertIn("密码", error)


class OfficialSenderConfigTests(unittest.TestCase):
    """官方发件邮箱口径（2026-11-06）：nbz_echo@163.com / smtp.163.com。"""

    def test_example_config_prefills_the_official_sender_without_a_secret(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "server", "echo-server.example.yaml")
        with patch.dict(os.environ, {}, clear=True):
            cfg = settings.load(path)
        self.assertEqual(cfg.get("account.email.provider"), "smtp")
        self.assertEqual(cfg.get("account.email.from"), "nbz_echo@163.com")
        self.assertEqual(cfg.get("account.email.from_name"), "ECHO")
        smtp = cfg.get("account.email.smtp") or {}
        self.assertEqual(smtp.get("host"), "smtp.163.com")
        self.assertEqual(smtp.get("port"), 465)
        self.assertEqual(smtp.get("mode"), "ssl")
        self.assertEqual(smtp.get("username"), "nbz_echo@163.com")
        # 授权码只能来自 env/secret 文件；示例配置里不许出现真实密码。
        self.assertNotIn("password", smtp)
        self.assertEqual(smtp.get("password_env"), "ECHO_SMTP_PASSWORD")
        self.assertEqual(smtp.get("password_file"), "")
        # 官方生产模板默认启用；没有授权码时发送端会明确 503，不会静默发信。
        self.assertTrue(cfg.get("account.email.enabled"))


class DevSenderTests(unittest.TestCase):
    def test_dev_writes_the_code_to_the_log(self):
        sender = emailer.DevEmailSender()
        with self.assertLogs("echo.account.email", level="WARNING") as logs:
            sender.send_code(email="a@example.com", code="123456",
                             purpose="register", ttl_s=600)
        joined = "\n".join(logs.output)
        self.assertIn("a@example.com", joined)
        self.assertIn("code=123456", joined)
        self.assertIn("purpose=register", joined)


class _Conn:
    def __init__(self, fail_times=0, auth_fail=False):
        self.fail_times = fail_times
        self.auth_fail = auth_fail
        self.sent = []
        self.logins = 0
        self.quit_called = False

    def login(self, username, password):
        self.logins += 1
        if self.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.password = password

    def send_message(self, message):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset")
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


class SmtpSenderTests(unittest.TestCase):
    def test_smtp_sends_a_message_and_retries_a_transient_failure(self):
        cfg = _cfg()
        conn = _Conn(fail_times=1)
        with patch.dict(os.environ, {"ECHO_SMTP_PASSWORD": "secret-not-in-log"}, clear=False), \
                patch("server.emailer.smtplib.SMTP_SSL", return_value=conn) as ssl_cls, \
                patch("server.emailer.time.sleep", return_value=None):
            sender = emailer.SmtpEmailSender(cfg)
            sender.send_code(email="a@example.com", code="654321",
                             purpose="reset", ttl_s=600)
        self.assertEqual(ssl_cls.call_count, 2)         # 每次尝试重新连
        self.assertEqual(conn.logins, 2)                 # 两次尝试都走 login
        self.assertEqual(len(conn.sent), 1)              # 第二次成功
        message = conn.sent[0]
        self.assertEqual(message["To"], "a@example.com")
        self.assertIn("654321", message.get_content())
        self.assertIn("重置密码", message.get_content())
        self.assertNotIn("secret-not-in-log", message.as_string())

    def test_auth_failure_is_not_retried(self):
        cfg = _cfg()
        conn = _Conn(auth_fail=True)
        with patch.dict(os.environ, {"ECHO_SMTP_PASSWORD": "x"}, clear=False), \
                patch("server.emailer.smtplib.SMTP_SSL", return_value=conn) as ssl_cls:
            sender = emailer.SmtpEmailSender(cfg)
            with self.assertRaises(emailer.EmailSendError) as ctx:
                sender.send_code(email="a@example.com", code="123456",
                                 purpose="register", ttl_s=600)
        self.assertIn("认证失败", str(ctx.exception))
        self.assertEqual(ssl_cls.call_count, 1)
        self.assertEqual(conn.logins, 1)
        self.assertEqual(conn.sent, [])

    def test_retries_are_bounded(self):
        cfg = _cfg()
        cfg.raw["account"]["email"]["send_retries"] = 1
        conn = _Conn(fail_times=99)
        with patch.dict(os.environ, {"ECHO_SMTP_PASSWORD": "x"}, clear=False), \
                patch("server.emailer.smtplib.SMTP_SSL", return_value=conn), \
                patch("server.emailer.time.sleep", return_value=None):
            sender = emailer.SmtpEmailSender(cfg)
            with self.assertRaises(emailer.EmailSendError):
                sender.send_code(email="a@example.com", code="123456",
                                 purpose="register", ttl_s=600)
        # 1 次正常 + 1 次重试；不会无限重造连接。
        self.assertEqual(conn.logins, 2)
        self.assertEqual(conn.fail_times, 97)

    def test_password_can_come_from_a_file(self):
        cfg = _cfg()
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
            fh.write("from-file\n")
            path = fh.name
        self.addCleanup(lambda: os.unlink(path))
        cfg.raw["account"]["email"]["smtp"]["password_file"] = path
        self.assertEqual(settings.smtp_password(cfg), "from-file")


if __name__ == "__main__":
    unittest.main()
