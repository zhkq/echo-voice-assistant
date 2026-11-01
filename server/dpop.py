# -*- coding: utf-8 -*-
"""DPoP（Demonstrating Proof of Possession）与后端委托 token 的校验。

为什么需要它：手机拿到的 TTS token 会经过 relay、也可能被后端自己看到；**Bearer
token 被谁看到谁就能用**。委托 token 里带 `cnf.jkt`（手机公钥指纹），手机每次请求
再用私钥签一个 DPoP proof，后端验 proof 与指纹一致 —— 只看/转发 token 的人仍然用不了。

这里的 `jkt` **不是 RFC 7638 的 JWK thumbprint**，而是：
`base64url(sha256(手机 P-256 未压缩公钥 65 字节))`。
理由：手机端用 Java 生成公钥，拿 65 字节原生点最直接；没必要为了一个内部字段
实现一遍 JWK canonicalization。字段名仍叫 `jkt`，文档里写清是本系统自定义的
"设备公钥指纹"。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from server import errors

DPOP_ALG = "ES256"
DPOP_TYP = "dpop+jwt"
#: proof 允许的时间偏差 / 重放窗口。
DPOP_MAX_SKEW_S = 60.0
#: 同一个 jti 在这个窗口内不能复用（防重放）。
DPOP_REPLAY_S = 120.0

_replay: Dict[str, float] = {}
_replay_lock = threading.Lock()


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unb64u(text: str) -> bytes:
    value = str(text or "")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def public_key_raw(public_key: ec.EllipticCurvePublicKey) -> bytes:
    return public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def public_key_from_b64(value: str) -> ec.EllipticCurvePublicKey:
    raw = unb64u(value)
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("设备公钥不是 P-256 未压缩点")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def thumbprint(public_key_b64: str) -> str:
    """本系统的设备公钥指纹（见模块头；不是 RFC7638）。"""
    raw = unb64u(public_key_b64)
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("设备公钥不是 P-256 未压缩点")
    return b64u(hashlib.sha256(raw).digest())


def validate_device_public_key(value: str) -> Tuple[str, str]:
    """校验并返回 `(b64, jkt)`；不合法抛 ValueError。"""
    key = public_key_from_b64(value)          # 校验曲线/长度
    raw = public_key_raw(key).hex()
    return b64u(public_key_raw(key)), b64u(hashlib.sha256(bytes.fromhex(raw)).digest())


def _jwk_public_key(jwk: dict) -> ec.EllipticCurvePublicKey:
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("DPoP jwk 不是 P-256")
    x = str(jwk.get("x") or "")
    y = str(jwk.get("y") or "")
    if not x or not y:
        raise ValueError("DPoP jwk 缺 x/y")
    # 0x04 || x || y（各 32 字节）
    raw = b"\x04" + unb64u(x).rjust(32, b"\0") + unb64u(y).rjust(32, b"\0")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def _check_replay(jti: str) -> None:
    if not jti:
        raise errors.unauthorized("DPoP 缺 jti")
    now = time.time()
    with _replay_lock:
        for key, at in list(_replay.items()):
            if now - at > DPOP_REPLAY_S:
                _replay.pop(key, None)
        if jti in _replay:
            raise errors.unauthorized("DPoP 重放")
        _replay[jti] = now


def _auth_token(request) -> str:
    raw = str(request.headers.get("authorization") or "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def verify(request, access_token: str, cnf: dict) -> Dict[str, Any]:
    """验一个 DPoP proof，返回 claims；失败抛 401。

    `cnf` 来自 delegated token 的 `cnf`（至少 `jkt`）。proof 必须：
      * 用 proof header 里的 jwk 签名；
      * jwk 指纹 == token 的 `cnf.jkt`；
      * `htm`/`htu`/`ath` 与本次请求一致；
      * `jti` 不在重放窗口；`iat` 不太旧/太新。
    """
    proof = str(request.headers.get("dpop") or "").strip()
    if not proof:
        raise errors.unauthorized("缺少 DPoP proof")
    try:
        header = jwt.get_unverified_header(proof)
    except Exception:
        raise errors.unauthorized("DPoP proof 格式不对") from None
    if str(header.get("typ") or "") != DPOP_TYP or str(header.get("alg") or "") != DPOP_ALG:
        raise errors.unauthorized("DPoP proof 算法/类型不对")
    jwk = header.get("jwk")
    try:
        pub = _jwk_public_key(jwk or {})
        raw = public_key_raw(pub)
        expected = str((cnf or {}).get("jkt") or "")
        actual = b64u(hashlib.sha256(raw).digest())
        if not expected or actual != expected:
            raise errors.unauthorized("DPoP 公钥与 token 的 cnf 不一致")
        claims = dict(jwt.decode(proof, pub, algorithms=[DPOP_ALG],
                                 options={"verify_aud": False}))
    except errors.EchoError:
        raise
    except Exception:
        raise errors.unauthorized("DPoP proof 签名/内容无效") from None

    now = time.time()
    try:
        iat = float(claims.get("iat") or 0)
    except (TypeError, ValueError):
        iat = 0.0
    if not iat or abs(now - iat) > DPOP_MAX_SKEW_S:
        raise errors.unauthorized("DPoP proof 时间无效")
    if str(claims.get("htm") or "").upper() != str(request.method or "POST").upper():
        raise errors.unauthorized("DPoP htm 不匹配")
    htu = str(claims.get("htu") or "")
    try:
        from urllib.parse import urlsplit
        path = urlsplit(htu).path or htu
    except Exception:
        path = htu
    if path != "/v1/tts":
        raise errors.unauthorized("DPoP htu 不匹配")
    expect_ath = b64u(hashlib.sha256(access_token.encode("ascii")).digest())
    if str(claims.get("ath") or "") != expect_ath:
        raise errors.unauthorized("DPoP ath 不匹配")
    _check_replay(str(claims.get("jti") or ""))
    return claims
