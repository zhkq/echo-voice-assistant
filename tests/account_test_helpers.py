# -*- coding: utf-8 -*-
"""账号测试的共用小工具（**只给测试用**）。

邮箱注册需要一条真实验证码；测试不经过邮件，直接把码的 scrypt 哈希写进
`email_codes`，再走真实的 HTTP 注册/重置。这样既覆盖了生产路径（验证哈希、
一次性、尝试次数、事务），又不发真实邮件。
"""
from __future__ import annotations

import time

from server.passwords import hash_password


def put_email_code(store, email: str, purpose: str = "register",
                   code: str = "123456", ttl_s: int = 600,
                   ip: str = "127.0.0.1") -> int:
    now = time.time()
    return store.email_code_create(
        email=str(email).strip().lower(), purpose=purpose,
        code_hash=hash_password(str(code)), expires_at=now + float(ttl_s),
        ip=ip, now=now)
