# -*- coding: utf-8 -*-
"""ECHO 侧方案 B 的端到端配对核心（**不依赖后端转发保密**）。

这一层只做三件事：

1. **ECHO 长期身份密钥**（P-256）——手机通过屏幕二维码拿到公钥；
2. **一次性 pairing ticket**——把 ECHO 公钥、relay 地址、nonce、6 位码、后端 TLS
   指纹打包给手机；二维码是信任根，不经过后端；
3. **手机 claim 的 ECIES 解密/设备签名校验**——手机用 ECHO 公钥加密配对码，
   后端 relay 只能看到密文；ECHO 用身份私钥解出，校验手机设备公钥签名，
   再返回加密的 token。

## 为什么用 P-256 而不是 X25519

App 的 minSdk 是 26，Android 的 `KeyPairGenerator("EC")` / ECDH / ECDSA 从 API 23
就有；X25519 在旧 Android 上要额外库。P-256 足够，且 Python `cryptography` 与 Java
都原生支持。

## 文件

`{DATA}/echo-e2e-identity.json` 只存**加密后的私钥**（Windows DPAPI / POSIX 0600），
公钥可公开；`{DATA}/echo-e2e-devices.json` 存已配对设备的公钥（用于撤销/审计）。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

IDENTITY_VERSION = 1
#: 二维码 ticket 的版本（协议字段变化时必须升；手机按版本拒绝旧票）。
TICKET_VERSION = 2
#: 配对 claim 的版本。
CLAIM_VERSION = 2

_TICKET_LOCK = threading.Lock()
#: 当前有效的 ticket nonce（内存；与 phone_pair 的 6 位码同寿命）。
_TICKET: Dict[str, Any] = {"nonce": "", "expires": 0.0, "code": ""}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    value = str(text or "")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- 身份密钥

def _data_path(name: str) -> str:
    from app import paths
    return os.path.join(paths.data_root(), name)


def _identity_path() -> str:
    return _data_path("echo-e2e-identity.json")


def _devices_path() -> str:
    return _data_path("echo-e2e-devices.json")


def _seal(raw: bytes) -> dict:
    from app import platform
    blob = platform.protect_secret(bytes(raw))
    kind = platform.protect_secret_kind()
    if kind != "plain" and blob == raw:
        kind = "plain"
    return {"enc": kind, "value": _b64(blob)}


def _open(envelope: Any) -> bytes:
    from app import platform
    if not isinstance(envelope, dict):
        raise ValueError("identity envelope 格式不对")
    blob = _unb64(str(envelope.get("value") or ""))
    kind = str(envelope.get("enc") or "")
    if kind == "plain":
        return blob
    if kind == "dpapi":
        return platform.unprotect_secret(blob)
    raise ValueError("不认识的 identity enc: %r" % kind)


def _new_private() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pub_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _pub_b64(key: ec.EllipticCurvePublicKey) -> str:
    return _b64(_pub_bytes(key))


def _pub_from_b64(value: str) -> ec.EllipticCurvePublicKey:
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    return _ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), _unb64(value))


def _save_identity(key: ec.EllipticCurvePrivateKey) -> None:
    from app import platform
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    payload = {
        "v": IDENTITY_VERSION,
        "alg": "P-256",
        "createdAt": int(time.time()),
        "private": _seal(pem),
        "public": _pub_b64(key.public_key()),
    }
    path = _identity_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    platform.restrict_file(tmp)
    os.replace(tmp, path)
    platform.restrict_file(path)


def load_identity() -> Tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    """读 ECHO 身份密钥；没有就生成一次。**坏文件不静默覆盖**，抛错让人看见。"""
    path = _identity_path()
    if not os.path.isfile(path):
        key = _new_private()
        _save_identity(key)
        return key, key.public_key()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if int(data.get("v") or 0) != IDENTITY_VERSION:
        raise ValueError("identity 版本不支持：%r" % data.get("v"))
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(_open(data.get("private")), password=None)
    return key, key.public_key()


def identity_public() -> str:
    _key, pub = load_identity()
    return _pub_b64(pub)


# ---------------------------------------------------------------- ticket

def new_ticket(*, code: str, relay_url: str, host_id: str,
               backend: Optional[dict] = None, ttl_s: int = 300) -> dict:
    """生成 QR ticket。二维码里含 6 位码（手机扫码后不需要再手输）。"""
    key, pub = load_identity()
    now = time.time()
    nonce = secrets.token_urlsafe(18)
    payload = {
        "v": TICKET_VERSION,
        "host": str(host_id or ""),
        "relay": str(relay_url or ""),
        "pub": _pub_b64(pub),
        "nonce": nonce,
        "exp": int(now + max(30, int(ttl_s))),
        "code": str(code or ""),
        "backend": dict(backend or {}),
    }
    # 自签名：二维码本身是信任根；签名用于手机校验票没被篡改/手工抄错。
    sig = key.sign(_canonical(payload), ec.ECDSA(hashes.SHA256()))
    payload["sig"] = _b64(sig)
    ticket = _b64(_canonical(payload))
    with _TICKET_LOCK:
        _TICKET.update(nonce=nonce, expires=float(payload["exp"]), code=str(code or ""))
    return {"ticket": ticket, "payload": payload}


def verify_ticket(ticket: str) -> dict:
    """手机侧/测试用：只验票的结构与自签名，不验“这是哪台 ECHO”。"""
    payload = json.loads(_unb64(ticket).decode("utf-8"))
    if int(payload.get("v") or 0) != TICKET_VERSION:
        raise ValueError("ticket 版本不支持")
    sig = _unb64(str(payload.pop("sig") or ""))
    pub = _pub_from_b64(str(payload.get("pub") or ""))
    pub.verify(sig, _canonical(payload), ec.ECDSA(hashes.SHA256()))
    if float(payload.get("exp") or 0) < time.time():
        raise ValueError("ticket 已过期")
    return payload


def pending_nonce() -> str:
    with _TICKET_LOCK:
        if float(_TICKET.get("expires") or 0) < time.time():
            return ""
        return str(_TICKET.get("nonce") or "")


# ---------------------------------------------------------------- 加解密

def _derive(shared: bytes, *, nonce: str, kind: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=str(nonce).encode("utf-8"),
        info=("echo-pair-v2:" + kind).encode("ascii"),
    ).derive(shared)


def decrypt_claim(body: Dict[str, Any]) -> Tuple[dict, bytes]:
    """解手机 claim → `(plaintext, response_key)`。

    手机用 ECHO 公钥做 ECDH；ECHO 用身份私钥 + 手机临时公钥得到同一 shared secret。
    返回的 response_key 用于把 token 加密回手机。
    """
    if int(body.get("v") or 0) != CLAIM_VERSION:
        raise ValueError("claim 版本不支持")
    nonce = str(body.get("nonce") or "")
    if not nonce or not secrets.compare_digest(nonce, pending_nonce()):
        raise ValueError("配对 nonce 已过期，请重新扫码")
    eph = _pub_from_b64(str(body.get("ephPub") or ""))
    key, _pub = load_identity()
    shared = key.exchange(ec.ECDH(), eph)
    aad = _canonical({"v": CLAIM_VERSION, "nonce": nonce})
    plain = AESGCM(_derive(shared, nonce=nonce, kind="claim")).decrypt(
        _unb64(str(body.get("iv") or "")),
        _unb64(str(body.get("ciphertext") or "")),
        aad,
    )
    data = json.loads(plain.decode("utf-8"))
    # 手机设备公钥 + 对 transcript 的签名：证明它真的持有设备私钥。
    device_pub = _pub_from_b64(str(data.get("devicePublicKey") or ""))
    sig = _unb64(str(data.get("signature") or ""))
    transcript = _canonical({
        "v": CLAIM_VERSION, "nonce": nonce,
        "ephPub": str(body.get("ephPub") or ""),
        "devicePublicKey": str(data.get("devicePublicKey") or ""),
    })
    device_pub.verify(sig, transcript, ec.ECDSA(hashes.SHA256()))
    return data, _derive(shared, nonce=nonce, kind="response")


def encrypt_response(key: bytes, payload: dict) -> dict:
    iv = os.urandom(12)
    aad = b"echo-pair-response-v2"
    cipher = AESGCM(key).encrypt(iv, _canonical(payload), aad)
    return {"v": CLAIM_VERSION, "iv": _b64(iv), "ciphertext": _b64(cipher)}


# ---------------------------------------------------------------- 设备记录

def remember_device(device_id: str, public_key: str, name: str) -> None:
    """记下已配对设备公钥（**不含令牌**；撤销时用它识别设备）。"""
    path = _devices_path()
    data: Dict[str, Any] = {}
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh) or {}
    except Exception:
        data = {}
    devices = data.setdefault("devices", {})
    devices[str(device_id)] = {
        "publicKey": str(public_key),
        "name": str(name or "mobile"),
        "pairedAt": int(time.time()),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def list_devices() -> list:
    """已配对设备（公钥记录；不含令牌）。面板只展示 id/name/时间。"""
    try:
        with open(_devices_path(), encoding="utf-8") as fh:
            data = json.load(fh) or {}
        out = []
        for device_id, value in (data.get("devices") or {}).items():
            if not isinstance(value, dict):
                continue
            out.append({
                "id": str(device_id),
                "name": str(value.get("name") or "mobile"),
                "pairedAt": int(value.get("pairedAt") or 0),
            })
        return sorted(out, key=lambda x: x.get("pairedAt") or 0, reverse=True)
    except Exception:
        return []


def device_id_for(public_key: str) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(_unb64(public_key))
    return _b64(digest.finalize())[:32]


# ---------------------------------------------------------------- 常规 API 的 E2E 信封

#: E2E 信封允许的时钟偏差/寿命（秒）；手机与 ECHO 都走 NTP，5 分钟足够。
E2E_MAX_SKEW_S = 300
#: 已用过的信封 id（防 relay/中间人重放同一请求）。**单进程内存**，
#: 与现有 relay worker 的部署形态一致；重启后丢缓存可接受（AES-GCM 仍防篡改）。
_seen_ids: Dict[str, float] = {}
_seen_lock = threading.Lock()


def _check_replay(inner: Dict[str, Any]) -> None:
    try:
        ts = int(inner.get("ts") or 0)
    except (TypeError, ValueError):
        raise ValueError("E2E 信封缺少时间戳") from None
    now = time.time()
    if abs(now - ts) > E2E_MAX_SKEW_S:
        raise ValueError("E2E 信封已过期或时钟偏差过大")
    rid = str(inner.get("id") or "").strip()
    if not rid or len(rid) > 128:
        raise ValueError("E2E 信封缺少一次性 id")
    with _seen_lock:
        exp = _seen_ids.get(rid)
        if exp is not None and exp >= now:
            raise ValueError("E2E 信封重放")
        for k, v in list(_seen_ids.items()):
            if v < now:
                _seen_ids.pop(k, None)
        _seen_ids[rid] = now + E2E_MAX_SKEW_S * 2


def forget_device(device_id: str) -> bool:
    """撤销一台设备的 E2E 身份（之后它的 E2E 信封/配对票据都不能再解）。"""
    path = _devices_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        devices = data.get("devices") or {}
        if str(device_id) not in devices:
            return False
        devices.pop(str(device_id), None)
        data["devices"] = devices
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def load_device(device_id: str) -> Optional[dict]:
    """读已配对设备（只含公钥/名字/时间，不含 token）。"""
    try:
        with open(_devices_path(), encoding="utf-8") as fh:
            data = json.load(fh) or {}
        value = (data.get("devices") or {}).get(str(device_id))
        return dict(value) if isinstance(value, dict) else None
    except Exception:
        return None


def _session_key(device_public_key: str, device_id: str) -> bytes:
    """ECHO 用身份私钥 + 手机设备公钥派生静态 E2E 会话键。

    手机侧用 `SK_P` + `PK_E` 派生同一个值。静态原因是"每次请求都做 ECDH"没有必要；
    每次消息随机 IV + AES-GCM 保证唯一性。设备撤销后删设备记录即可。
    """
    key, _pub = load_identity()
    phone_pub = _pub_from_b64(device_public_key)
    shared = key.exchange(ec.ECDH(), phone_pub)
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=b"echo-e2e-session-v2",
        info=("device:" + str(device_id)).encode("utf-8"),
    ).derive(shared)


def open_envelope(envelope: Dict[str, Any]) -> Tuple[dict, str]:
    """解密手机→ECHO 的常规 API 信封，返回 `(inner, device_id)`。

    `inner` = `{method,path,query,headers,body(base64)}`；`headers` 只允许
    Authorization / Content-Type，防止 E2E 变成“任意头注入”。
    """
    if int(envelope.get("v") or 0) != 2:
        raise ValueError("E2E envelope 版本不支持")
    device_id = str(envelope.get("device") or "")
    device = load_device(device_id)
    if not device or not device.get("publicKey"):
        raise ValueError("设备未配对或已被撤销")
    key = _session_key(str(device["publicKey"]), device_id)
    plain = AESGCM(key).decrypt(
        _unb64(str(envelope.get("iv") or "")),
        _unb64(str(envelope.get("ciphertext") or "")),
        b"echo-e2e-v2",
    )
    inner = json.loads(plain.decode("utf-8"))
    if not isinstance(inner, dict):
        raise ValueError("E2E inner 不是对象")
    _check_replay(inner)
    return inner, device_id


def seal_envelope(device_id: str, payload: Dict[str, Any]) -> dict:
    """加密 ECHO→手机的响应信封。"""
    device = load_device(device_id)
    if not device or not device.get("publicKey"):
        raise ValueError("设备未配对或已被撤销")
    key = _session_key(str(device["publicKey"]), device_id)
    iv = os.urandom(12)
    cipher = AESGCM(key).encrypt(iv, _canonical(payload), b"echo-e2e-v2")
    return {"v": 2, "device": device_id, "iv": _b64(iv),
            "ciphertext": _b64(cipher)}


def open_response(envelope: Dict[str, Any], device_id: str,
                  device_public_key: str) -> dict:
    """手机侧/测试用：解 ECHO 响应信封。"""
    key = _session_key(device_public_key, device_id)
    plain = AESGCM(key).decrypt(
        _unb64(str(envelope.get("iv") or "")),
        _unb64(str(envelope.get("ciphertext") or "")),
        b"echo-e2e-v2",
    )
    return json.loads(plain.decode("utf-8"))

