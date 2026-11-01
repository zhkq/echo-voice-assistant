# -*- coding: utf-8 -*-
"""方案 B 配对核心（ECHO 侧）：身份密钥 / 二维码 ticket / 加密 claim。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import e2e
from app import paths


def _b64(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _derive(shared, nonce, kind="claim"):
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=nonce.encode(), info=("echo-pair-v2:" + kind).encode()
                ).derive(shared)


class E2EPairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="echo-e2e-")
        self._p = patch.object(paths, "data_root", lambda: self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_identity_is_stable_and_public_is_not_private(self):
        a = e2e.identity_public()
        b = e2e.identity_public()
        self.assertEqual(a, b)
        with open(os.path.join(self.tmp, "echo-e2e-identity.json"), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("PRIVATE", raw)
        self.assertIn('"enc"', raw)

    def test_ticket_contains_echo_pub_and_verifies(self):
        key, pub = e2e.load_identity()
        t = e2e.new_ticket(code="123456", relay_url="https://backend/v1/relay/h/",
                          host_id="h")
        payload = e2e.verify_ticket(t["ticket"])
        self.assertEqual(payload["pub"], e2e.identity_public())
        self.assertEqual(payload["code"], "123456")
        self.assertEqual(payload["nonce"], t["payload"]["nonce"])
        self.assertEqual(e2e.pending_nonce(), t["payload"]["nonce"])

    def _encrypted_claim(self, code="123456", nonce=None, name="pixel"):
        _echo_key, echo_pub = e2e.load_identity()
        phone = ec.generate_private_key(ec.SECP256R1())
        phone_pub = phone.public_key().public_bytes(
            __import__("cryptography.hazmat.primitives.serialization",
                       fromlist=["Encoding"]).Encoding.X962,
            __import__("cryptography.hazmat.primitives.serialization",
                       fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint)
        phone_pub_b64 = _b64(phone_pub)
        transcript = _canonical({
            "v": 2, "nonce": nonce, "ephPub": _b64(phone.public_key().public_bytes(
                __import__("cryptography.hazmat.primitives.serialization",
                           fromlist=["Encoding"]).Encoding.X962,
                __import__("cryptography.hazmat.primitives.serialization",
                           fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint)),
            "devicePublicKey": phone_pub_b64,
        })
        # 用手机静态私钥对 transcript 签名（设备持有证明）。
        sig = phone.sign(transcript, ec.ECDSA(hashes.SHA256()))
        shared = phone.exchange(ec.ECDH(), echo_pub)
        key = _derive(shared, nonce)
        aad = _canonical({"v": 2, "nonce": nonce})
        iv = os.urandom(12)
        cipher = AESGCM(key).encrypt(iv, _canonical({
            "code": code, "deviceName": name, "devicePublicKey": phone_pub_b64,
            "signature": _b64(sig),
        }), aad)
        return {
            "v": 2,
            "nonce": nonce,
            "ephPub": _b64(phone.public_key().public_bytes(
                __import__("cryptography.hazmat.primitives.serialization",
                           fromlist=["Encoding"]).Encoding.X962,
                __import__("cryptography.hazmat.primitives.serialization",
                           fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint)),
            "iv": _b64(iv),
            "ciphertext": _b64(cipher),
        }

    def test_claim_round_trip_and_response_encryption(self):
        t = e2e.new_ticket(code="123456", relay_url="https://backend/v1/relay/h/",
                          host_id="h")
        nonce = t["payload"]["nonce"]
        body = self._encrypted_claim(nonce=nonce)
        data, response_key = e2e.decrypt_claim(body)
        self.assertEqual("123456", data["code"])
        self.assertEqual("pixel", data["deviceName"])
        self.assertTrue(data["devicePublicKey"])
        # 响应只有持同一 ECDH shared secret 的手机能解
        env = e2e.encrypt_response(response_key, {"ok": True, "token": "secret"})
        # 用响应 key 解（这里模拟手机；手机端由同一 shared secret 派生）
        plain = AESGCM(response_key).decrypt(
            __import__("base64").urlsafe_b64decode(env["iv"] + "=="),
            __import__("base64").urlsafe_b64decode(env["ciphertext"] + "=="),
            b"echo-pair-response-v2")
        self.assertEqual({"ok": True, "token": "secret"}, json.loads(plain))

    def test_wrong_nonce_is_rejected(self):
        e2e.new_ticket(code="123456", relay_url="https://backend/v1/relay/h/",
                       host_id="h")
        with self.assertRaises(Exception):
            e2e.decrypt_claim(self._encrypted_claim(nonce="wrong"))

    def test_device_id_is_stable_and_not_the_key(self):
        _key, pub = e2e.load_identity()
        pub_b64 = _b64(pub.public_bytes(
            __import__("cryptography.hazmat.primitives.serialization",
                       fromlist=["Encoding"]).Encoding.X962,
            __import__("cryptography.hazmat.primitives.serialization",
                       fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint))
        a = e2e.device_id_for(pub_b64)
        b = e2e.device_id_for(pub_b64)
        self.assertEqual(a, b)
        self.assertNotEqual(a, pub_b64)

    def test_regular_api_e2e_envelope_round_trip(self):
        """手机↔ECHO 常规请求：ECHO 能解、响应能加密；未配对设备被拒。"""
        phone = ec.generate_private_key(ec.SECP256R1())
        phone_pub = _b64(phone.public_key().public_bytes(
            __import__("cryptography.hazmat.primitives.serialization",
                       fromlist=["Encoding"]).Encoding.X962,
            __import__("cryptography.hazmat.primitives.serialization",
                       fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint))
        device_id = e2e.device_id_for(phone_pub)
        e2e.remember_device(device_id, phone_pub, "pixel")
        inner = {"id": "req-1", "ts": int(time.time()), "method": "GET",
                 "path": "api/status", "query": "",
                 "headers": {"authorization": "Bearer phone-token"}, "body": ""}
        env = e2e.seal_envelope(device_id, inner)
        got, got_id = e2e.open_envelope(env)
        self.assertEqual(device_id, got_id)
        self.assertEqual("api/status", got["path"])
        # 同一信封第二次必须拒绝（防 relay/中间人重放）
        with self.assertRaises(Exception):
            e2e.open_envelope(env)
        resp = e2e.seal_envelope(device_id, {"status": 200, "body": "eA=="})
        self.assertEqual({"status": 200, "body": "eA=="},
                         e2e.open_response(resp, device_id, phone_pub))
        # 没配对的设备不能用 E2E
        with self.assertRaises(Exception):
            e2e.open_envelope({"v": 2, "device": "nope", "iv": "x", "ciphertext": "y"})
        # 设备身份列表可读；撤销后不再可读
        ids = {d["id"] for d in e2e.list_devices()}
        self.assertIn(device_id, ids)
        self.assertTrue(e2e.forget_device(device_id))
        self.assertNotIn(device_id, {d["id"] for d in e2e.list_devices()})


if __name__ == "__main__":
    unittest.main()
