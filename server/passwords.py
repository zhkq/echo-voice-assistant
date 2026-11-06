# -*- coding: utf-8 -*-
"""口令哈希（scrypt）—— **账号与管理面共用的一份实现**。

`server/admin.py` 需要它，`server/account.py`（邮箱账号）也需要它；而
`server/account.py` 已经被 `server/admin.py` 引用，反过来 import admin 会循环依赖。
所以把纯函数放在这里，两边各自 import。

格式：`scrypt$<salt_b64>$<hash_b64>`。前缀自带算法，将来换 argon2id 时可以按前缀
平滑迁移（旧哈希继续可验，新口令用新算法写）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

#: 这些是"让人算不快"的参数：n=2^14 时一次验证约几十毫秒。
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str, *, salt: str = "") -> str:
    """`scrypt$<salt_b64>$<hash_b64>`。随机 16 字节盐。"""
    raw_salt = base64.b64decode(salt) if salt else os.urandom(16)
    dk = hashlib.scrypt(str(password).encode("utf-8"), salt=raw_salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return "scrypt$%s$%s" % (base64.b64encode(raw_salt).decode(),
                             base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    """**常数时间比较**，且认不出的格式一律 False（不猜）。"""
    text = str(stored or "")
    if not text.startswith("scrypt$"):
        return False
    parts = text.split("$")
    if len(parts) != 3:
        return False
    try:
        raw_salt = base64.b64decode(parts[1])
        want = base64.b64decode(parts[2])
    except Exception:
        return False
    # **空盐 / 空哈希要当场否掉**：`scrypt(dklen=0)` 会抛 `ValueError`，
    # 而"库里的哈希被改坏了"这条路径只该得到 `False`（进不去），不该是一段 traceback。
    if not raw_salt or not want:
        return False
    try:
        dk = hashlib.scrypt(str(password).encode("utf-8"), salt=raw_salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(want))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(dk, want)
