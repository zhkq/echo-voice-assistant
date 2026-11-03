# -*- coding: utf-8 -*-
"""方案 B 后端委托：delegated JWT + DPoP 设备持有证明。"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from server import auth as auth_mod
from server import delegation
from server import dpop
from server import settings as settings_mod


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class DelegatedTtsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-delegate-")
        self.cfg = settings_mod.load()
        self.cfg.raw["auth"]["enabled"] = True
        self.cfg.raw["auth"]["mode"] = "jwt"
        self.cfg.raw["auth"]["jwt_secret"] = "test-secret-" + "x" * 32
        self.cfg.raw["auth"]["db"] = os.path.join(self.tmp, "auth.db")
        self.store = auth_mod.open_store(self.cfg)
        self.addCleanup(self.store.close)
        self.auth = auth_mod.Auth(self.cfg, self.store)
        self.cid = "echo-pc"
        self.store.upsert_client(
            self.cid, "echo", auth_mod.hash_secret("s", self.cid), "asr tts")
        self.parent = self.store.client(self.cid)
        self.phone = ec.generate_private_key(ec.SECP256R1())
        raw = self.phone.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint)
        self.device_b64 = _b64(raw)
        self.jkt = dpop.thumbprint(self.device_b64)

    def test_delegated_token_is_scoped_to_tts_and_bound_to_device(self):
        token, _ttl = self.auth.issue_delegated(
            self.parent, "phone-1", self.jkt, ttl_s=600)
        who = self.auth.authenticate("Bearer " + token, need_scope="tts")
        self.assertTrue(who["delegated"])
        self.assertEqual("phone:phone-1", who["client_id"])
        self.assertEqual(self.jkt, who["cnf"]["jkt"])
        with self.assertRaises(Exception) as ctx:
            self.auth.authenticate("Bearer " + token, need_scope="asr")
        self.assertEqual(403, ctx.exception.status)

    def _proof(self, token, *, key=None, htu="/v1/tts", htm="POST", ath=True):
        key = key or self.phone
        pub = key.public_key().public_numbers()
        jwk = {
            "kty": "EC", "crv": "P-256",
            "x": _b64(pub.x.to_bytes(32, "big")),
            "y": _b64(pub.y.to_bytes(32, "big")),
        }
        payload = {
            "htm": htm, "htu": htu, "iat": int(time.time()),
            "jti": os.urandom(8).hex(),
        }
        if ath:
            payload["ath"] = _b64(hashlib.sha256(token.encode()).digest())
        return jwt.encode(payload, key, algorithm="ES256",
                          headers={"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk})

    def _request(self, proof):
        return SimpleNamespace(
            method="POST",
            headers={"authorization": "Bearer token", "dpop": proof},
        )

    def test_dpop_proof_must_match_device_and_ath(self):
        token, _ttl = self.auth.issue_delegated(
            self.parent, "phone-1", self.jkt, ttl_s=600)
        req = self._request(self._proof(token))
        claims = dpop.verify(req, token, {"jkt": self.jkt})
        self.assertEqual("POST", claims["htm"])
        # 另一把设备私钥：token 的 cnf 不匹配
        other = ec.generate_private_key(ec.SECP256R1())
        bad = self._request(self._proof(token, key=other))
        with self.assertRaises(Exception):
            dpop.verify(bad, token, {"jkt": self.jkt})
        # ath 不匹配（拿到的 token 不是 proof 绑定的那个）
        bad2 = self._request(self._proof(token, ath=False))
        with self.assertRaises(Exception):
            dpop.verify(bad2, token, {"jkt": self.jkt})

    def test_delegated_token_can_be_revoked_before_ttl(self):
        token, _ttl = self.auth.issue_delegated(
            self.parent, "phone-1", self.jkt, ttl_s=600)
        self.auth.authenticate("Bearer " + token, need_scope="tts")
        delegation.revoke("phone-1")
        with self.assertRaises(Exception) as ctx:
            self.auth.authenticate("Bearer " + token, need_scope="tts")
        self.assertEqual(401, ctx.exception.status)
        delegation.reset()

    def test_revoking_parent_invalidates_delegated_token(self):
        token, _ttl = self.auth.issue_delegated(
            self.parent, "phone-1", self.jkt, ttl_s=600)
        self.auth.cache.revoke(self.cid)
        with self.assertRaises(Exception) as ctx:
            self.auth.authenticate("Bearer " + token, need_scope="tts")
        self.assertIn(ctx.exception.status, (401, 403))


if __name__ == "__main__":
    unittest.main()
