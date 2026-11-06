# -*- coding: utf-8 -*-
"""邮箱验证码发送：`dev`（写日志）与 `smtp`（真发信）。

## 为什么要有这一层

账号流程只依赖一个很小的接口（`send_code`），测试注入 fake、本地用 dev、
生产用 smtp —— 三种实现互不影响。**不静默回退**：`enabled=true` 但 provider/
from/host/凭据没配好时，账号端点明确回 503 `email_unconfigured`，不会偷偷用
dev 把验证码写进生产日志。

## 凭据

密码**只从环境变量或只读文件读**（`settings.smtp_password()`），不写 YAML、
不进 SQLite、不进 `cfg.raw`；本模块也不把密码/验证码写进异常文本。
"""
from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional, Tuple

from server import settings as settings_mod

logger = logging.getLogger("echo.account.email")

SUBJECT = "ECHO 邮箱验证码"


class EmailSendError(Exception):
    """邮件发送失败。文本里**不带**验证码/密码，只给可读原因。"""


def _purpose_text(purpose: str) -> str:
    return "重置密码" if str(purpose or "") == "reset" else "注册账号"


def _body(code: str, purpose: str, ttl_s: int) -> str:
    minutes = max(1, int(round(int(ttl_s or 600) / 60.0)))
    return (
        "你正在%s ECHO 账号，验证码是：\n\n"
        "    %s\n\n"
        "这个验证码 %d 分钟内有效，只能使用一次。\n"
        "如果不是你本人操作，请忽略这封邮件；你的账号不会被改动。\n"
        "（这封邮件由系统自动发送，请勿回复。）\n"
    ) % (_purpose_text(purpose), str(code), minutes)


class DevEmailSender:
    """把验证码写到服务端日志，供本地/CI 联调。**不要用于生产。**"""

    name = "dev"

    def __init__(self) -> None:
        self.logger = logger

    def send_code(self, *, email: str, code: str, purpose: str, ttl_s: int) -> None:
        # WARNING 级别：默认日志就能看到，又不会混在调试噪声里。
        self.logger.warning(
            "[account.email][dev] email=%s purpose=%s code=%s expires_in=%ss "
            "（dev provider 只会把验证码写日志，不能用于生产）",
            str(email or ""), str(purpose or ""), str(code or ""), int(ttl_s or 600))


class SmtpEmailSender:
    """标准库 smtplib 发信；超时/重试都有上限，认证失败不重试。"""

    name = "smtp"

    def __init__(self, cfg) -> None:
        self.host = str(cfg.get("account.email.smtp.host", "") or "").strip()
        try:
            self.port = int(cfg.get("account.email.smtp.port", 465) or 465)
        except (TypeError, ValueError):
            self.port = 465
        self.mode = str(cfg.get("account.email.smtp.mode", "ssl") or "ssl").strip().lower()
        self.username = str(cfg.get("account.email.smtp.username", "") or "").strip()
        self.password = settings_mod.smtp_password(cfg)
        self.sender = str(cfg.get("account.email.from", "") or "").strip()
        self.sender_name = str(cfg.get("account.email.from_name", "ECHO") or "ECHO").strip()
        try:
            self.timeout_s = max(1.0, float(cfg.get("account.email.send_timeout_s", 10) or 10))
        except (TypeError, ValueError):
            self.timeout_s = 10.0
        try:
            self.retries = max(0, int(cfg.get("account.email.send_retries", 1) or 0))
        except (TypeError, ValueError):
            self.retries = 1
        if not self.host:
            raise ValueError("account.email.smtp.host 为空")
        if not self.sender:
            raise ValueError("account.email.from 为空")
        if self.mode not in ("ssl", "starttls", "plain"):
            raise ValueError("account.email.smtp.mode 只支持 ssl|starttls|plain")
        if self.mode == "plain" and self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("plain 模式只允许本机 SMTP；远程请用 ssl/starttls")
        if self.username and not self.password:
            raise ValueError("配了 SMTP 用户名但读不到密码（环境变量或 password_file）")

    def _connect(self):
        context = ssl.create_default_context()
        if self.mode == "ssl":
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout_s,
                                    context=context)
        conn = smtplib.SMTP(self.host, self.port, timeout=self.timeout_s)
        if self.mode == "starttls":
            conn.ehlo()
            conn.starttls(context=context)
            conn.ehlo()
        return conn

    def _send_once(self, message: EmailMessage) -> None:
        conn = self._connect()
        try:
            if self.username:
                conn.login(self.username, self.password)
            conn.send_message(message)
        finally:
            try:
                conn.quit()
            except Exception:
                pass

    def send_code(self, *, email: str, code: str, purpose: str, ttl_s: int) -> None:
        message = EmailMessage()
        message["Subject"] = SUBJECT
        message["From"] = formataddr((self.sender_name, self.sender))
        message["To"] = str(email or "")
        message.set_content(_body(code, purpose, ttl_s))
        last = None
        for attempt in range(self.retries + 1):
            try:
                self._send_once(message)
                return
            except smtplib.SMTPAuthenticationError as exc:
                # 认证失败重试没有意义；不要把用户名/密码带进异常文本。
                raise EmailSendError("SMTP 认证失败") from exc
            except (smtplib.SMTPException, OSError, socket.timeout) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(0.5)
        raise EmailSendError("SMTP 发送失败：%s" % type(last).__name__ if last
                            else "SMTP 发送失败")


def build_sender(cfg) -> Tuple[Optional[object], str]:
    """按配置造 sender。返回 `(sender, 错误说明)`；错误说明非空 = 不能发信。"""
    if not bool(cfg.get("account.email.enabled", False)):
        return None, "邮箱注册/发码未启用（account.email.enabled=false）"
    provider = str(cfg.get("account.email.provider", "dev") or "dev").strip().lower()
    if provider == "dev":
        logger.warning("account.email.provider=dev：验证码会写到服务端日志，"
                       "只用于本地/CI，不要用于生产")
        return DevEmailSender(), ""
    if provider == "smtp":
        try:
            return SmtpEmailSender(cfg), ""
        except ValueError as exc:
            return None, str(exc)
    if provider == "api":
        return None, "provider=api 本轮未实现；请用 dev 或 smtp"
    return None, "未知的 account.email.provider：%s" % provider
