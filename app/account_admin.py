# -*- coding: utf-8 -*-
"""账号登录 → 自动绑定设备（issue #23，客户端侧）。

对面板/向导只暴露三件事：**登录/注册**、**账号会话落盘**、**登录态绑定设备**。
它**不实现能力调用** —— 绑定成功后的东西还是 `{DATA}/backend.json`，
由 `app/capabilities/echo_server.py` 照旧使用；旧 `/v1/pair` 手动配对也照旧存在。

## 两个文件，两个边界

| 文件 | 内容 | 保护 |
|---|---|---|
| `{DATA}/account.json` | `userToken` + 账号服务地址 | 整个 JSON 走 `credentials.seal_text`（DPAPI / 0600） |
| `{DATA}/backend.json` | `clientId` + `clientSecret` | 现有配对凭据信封（**不改**） |
| `{DATA}/device-id` | 稳定设备 id（不是秘密） | 0600；删了就等于换一台设备 |

`userToken` **只用于账号 API**（登录态、绑定、设备列表、解绑、退出），
**绝不写进 `backend.json`**，也绝不拿去调能力端点 —— 服务端两侧都显式拒绝。

## 不自动回退配对码

`bind_device()` 的 401 只翻成"登录已失效，请重新登录"；
`/v1/account/devices/code` 与 `/v1/pair` 只在用户显式点"手动配对"时才被调用。

## 为什么放在客户端进程里

密码/令牌不经过浏览器；TLS 证书固定（`pairing.pinned_context`）只在进程里有；
也不会有 CORS。面板/向导只调本地 `/api/account/*`。
"""
from __future__ import annotations

import os
import secrets
import socket
import time
from typing import Any, Dict, Optional

from app import platform
from app.capabilities import credentials as cred
from app.capabilities import pairing
from app.config import settings

#: 账号/设备绑定文件的文件名（与 `backend.json` 同级）。
FILENAME = "account.json"
DEVICE_ID_FILENAME = "device-id"

#: 官方账号服务地址（HTTPS）。可被隐藏设置 `accountServerUrl` 覆盖（自建/开发环境）。
DEFAULT_SERVER_URL = "https://echoserver.zhkq.online:8443"

#: 设备 id 的形状（与服务端 `_DEVICE_ID_RE` 对齐）。
DEVICE_ID_LEN = 32

#: 这些错误码在客户端有**专门的一句话**；其余沿用服务端 detail。
_AUTH_CODES = {"unauthorized", "unauthenticated", "token_expired"}


class AccountError(Exception):
    """账号/绑定失败。`message` 是能直接显示给用户的一句话。

    `status`/`code` 保留服务端的分类：本地 API 用它回 HTTP 状态；界面按 `code`
    区分"去登录 / 去续费 / 去解绑"三种下一步。**不继承 `CapabilityError`** ——
    它不进能力路由，也不参与后端降级。
    """

    def __init__(self, message: str, *, code: str = "", status: int = 0,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.message = str(message)
        self.code = str(code or "")
        self.status = int(status or 0)
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.message


def _translate(err: pairing.PairingError) -> AccountError:
    """服务端/网络错误 → 用户能照着做的一句话。"""
    code = str(getattr(err, "code", "") or "")
    status = int(getattr(err, "status", 0) or 0)
    detail = str(getattr(err, "message", "") or "")
    if status == 401 or code in _AUTH_CODES:
        return AccountError("登录已失效，请重新登录", code=code or "unauthorized",
                            status=401)
    if status == 402 or code.startswith("entitlement_"):
        return AccountError("账号没有有效权益（或已到期），请续费后再绑定设备",
                            code=code or "entitlement_missing", status=402)
    if code == "device_limit_reached":
        return AccountError("设备数已满，请先在账户页解绑一台旧设备",
                            code=code, status=403)
    if code == "device_bound_to_other_account":
        return AccountError("这台设备已绑定到另一个账号，请先在原账号解绑",
                            code=code, status=409)
    if code == "client_not_yours":
        return AccountError("这条设备凭据属于另一个账号，不能认领",
                            code=code, status=409)
    if code == "device_mismatch":
        return AccountError("这条设备凭据对应的不是这台设备，请先解绑旧设备再重新绑定",
                            code=code, status=409)
    if status == 429 or code == "rate_limited":
        wait = int(getattr(err, "retry_after", 0) or 0)
        message = "操作太频繁，请稍后再试" if not wait else "操作太频繁，请 %d 秒后再试" % wait
        return AccountError(message, code=code or "rate_limited", status=429,
                            retry_after=wait or None)
    if code == "billing_disabled":
        return AccountError("该服务端未开通账号计费", code=code, status=404)
    if status == 404:
        # 旧服务端没有 `/v1/account/devices/bind`（或路径被反代挡了）：
        # **明确说"版本较旧，请手动配对"**，而不是把 404 说成"账号服务请求失败"。
        return AccountError("服务端还不支持登录自动绑定（版本较旧），请手动配对",
                            code=code or "not_found", status=404)
    if code == "auth_misconfigured":
        return AccountError("服务端鉴权配置不完整，请联系管理员", code=code, status=503)
    return AccountError(detail or "账号服务请求失败", code=code, status=status or 400)


def _check(server_url: str = "") -> str:
    """规整账号服务地址：参数 → 隐藏设置 → 官方默认。"""
    raw = str(server_url or settings.get("accountServerUrl", "") or
              DEFAULT_SERVER_URL).strip()
    try:
        url = pairing.normalize_base_url(raw)
    except pairing.PairingError as exc:
        raise AccountError(exc.message, code=exc.code or "bad_request", status=400) from None
    if not url:
        raise AccountError("还没配置账号服务地址", code="bad_request", status=400)
    return url


def _context_for(url: str):
    """https 时尽量用已固定的证书；没有就交给系统 CA（首次登录还没 pin 过）。"""
    if not pairing.is_https(url):
        return None
    existing = cred.load()
    if not existing or not existing.cert_pem:
        return None
    try:
        if pairing.normalize_base_url(existing.base_url) != pairing.normalize_base_url(url):
            return None
    except Exception:
        return None
    try:
        return pairing.pinned_context(existing.cert_pem)
    except pairing.PairingError:
        return None


def _call(method: str, url: str, path: str, payload: Optional[dict] = None,
          *, token: str = "", timeout: float = pairing.PAIR_TIMEOUT_S,
          pin: bool = True) -> Dict[str, Any]:
    """调账号 API。`pin=False` 用于登录/注册/发码/重置 —— 那几步还没绑定过，
    只能用系统 CA；**bind 会用 `pin_certificate` 首次固定证书**（与 `/v1/pair` 一致）。"""
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + str(token)
    context = _context_for(url) if pin else None
    try:
        return pairing.request_json(method, url + path, payload, headers=headers,
                                    timeout=timeout, context=context)
    except pairing.PairingError as exc:
        raise _translate(exc) from None


def _save_session(server_url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    user = body.get("user") if isinstance(body.get("user"), dict) else {}
    token = str(body.get("userToken") or "")
    if not token:
        raise AccountError("服务端没有返回登录令牌，请稍后再试",
                           code="bad_response", status=502)
    payload = {
        "serverUrl": server_url,
        "userId": str(body.get("userId") or user.get("userId") or ""),
        "email": str(user.get("email") or body.get("email") or ""),
        "userToken": token,
        "expiresAt": int(body.get("expiresAt") or 0),
        "savedAt": time.time(),
    }
    try:
        cred.write_sealed_json(FILENAME, payload)
    except Exception as exc:
        raise AccountError("登录成功但保存到本机失败：%s" % exc,
                           code="storage_error", status=500) from None
    return payload


def load_session() -> Optional[Dict[str, Any]]:
    """读账号会话。没有/坏了返回 None（与 `credentials.load` 同一纪律，不抛）。"""
    data = cred.read_sealed_json(FILENAME)
    if not isinstance(data, dict):
        return None
    if not str(data.get("userToken") or "") or not str(data.get("serverUrl") or ""):
        return None
    return data


def clear_session() -> bool:
    path = cred.data_path(FILENAME)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def _required() -> Dict[str, Any]:
    session = load_session()
    if session is None:
        raise AccountError("请先登录账号", code="not_signed_in", status=401)
    return session


def session_view() -> Dict[str, Any]:
    """给界面看的一份会话视图（**没有 userToken**）。"""
    session = load_session()
    if session is None:
        return {"signedIn": False, "userId": "", "email": "",
                "serverUrl": "", "expiresAt": 0, "expired": False}
    expires_at = int(session.get("expiresAt") or 0)
    expired = bool(expires_at and expires_at < time.time())
    return {
        "signedIn": not expired,
        "userId": str(session.get("userId") or ""),
        "email": str(session.get("email") or ""),
        "serverUrl": str(session.get("serverUrl") or ""),
        "expiresAt": expires_at,
        "expired": expired,
    }


# ---------------------------------------------------------------- 账号 API 代理

def login(email: str, password: str, server_url: str = "") -> Dict[str, Any]:
    url = _check(server_url)
    body = _call("POST", url, "/v1/account/login",
                 {"email": str(email or ""), "password": str(password or "")},
                 pin=False)
    _save_session(url, body)
    return {"userId": str(body.get("userId") or ""),
            "user": body.get("user") or {},
            "devices": body.get("devices") or [],
            "expiresAt": int(body.get("expiresAt") or 0),
            "serverUrl": url}


def register(email: str, code: str, password: str, invite_code: str = "",
             name: str = "", server_url: str = "") -> Dict[str, Any]:
    url = _check(server_url)
    body = _call("POST", url, "/v1/account/register",
                 {"email": str(email or ""), "code": str(code or ""),
                  "password": str(password or ""), "inviteCode": str(invite_code or ""),
                  "name": str(name or "")},
                 pin=False)
    _save_session(url, body)
    return {"userId": str(body.get("userId") or ""),
            "user": body.get("user") or {},
            "devices": body.get("devices") or [],
            "expiresAt": int(body.get("expiresAt") or 0),
            "serverUrl": url}


def send_code(email: str, purpose: str = "register", server_url: str = "") -> Dict[str, Any]:
    url = _check(server_url)
    return _call("POST", url, "/v1/account/email/send-code",
                 {"email": str(email or ""), "purpose": str(purpose or "")},
                 pin=False)


def reset_password(email: str, code: str, new_password: str,
                   server_url: str = "") -> Dict[str, Any]:
    url = _check(server_url)
    return _call("POST", url, "/v1/account/password/reset",
                 {"email": str(email or ""), "code": str(code or ""),
                  "newPassword": str(new_password or "")},
                 pin=False)


def me() -> Dict[str, Any]:
    session = _required()
    body = _call("GET", str(session["serverUrl"]), "/v1/account/me",
                 token=str(session["userToken"]))
    body.setdefault("devices", [])
    return body


def logout() -> Dict[str, Any]:
    """退出登录：尽力撤销服务端的 userToken，**无论如何都清本机会话**。"""
    session = load_session()
    try:
        if session is not None:
            _call("POST", str(session["serverUrl"]), "/v1/account/logout",
                  token=str(session["userToken"]))
    except AccountError:
        pass
    clear_session()
    return {"ok": True}


# ---------------------------------------------------------------- 设备绑定

def device_id() -> str:
    """稳定设备 id（32 hex）。读不到/坏了就生成一个新的并原子写盘。

    它不是秘密（服务端按它做幂等），但要求稳定：重装/升级保留数据目录时不变；
    删掉数据目录 = 换一台设备，旧设备要由用户在账户页解绑。
    """
    path = cred.data_path(DEVICE_ID_FILENAME)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if 12 <= len(text) <= 64 and all(
                    ch.isalnum() or ch in "_-" for ch in text):
                return text
    except Exception:
        pass
    value = secrets.token_hex(DEVICE_ID_LEN // 2)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        platform.restrict_file(tmp)
        os.replace(tmp, path)
        platform.restrict_file(path)
    except Exception:
        # 写不进去也让它用这一次：下一次启动会重新生成（服务端会多一台设备）。
        pass
    return value


def _hostname() -> str:
    try:
        return str(socket.gethostname() or "")
    except Exception:
        return ""


def _pair_view(creds: cred.BackendCredentials) -> Dict[str, Any]:
    return {
        "paired": True,
        "clientId": str(getattr(creds, "client_id", "") or ""),
        "baseUrl": str(getattr(creds, "base_url", "") or ""),
        "serverName": str(getattr(creds, "server_name", "") or ""),
        "certFingerprint": str(getattr(creds, "cert_fingerprint", "") or ""),
        "pairedAt": float(getattr(creds, "paired_at", 0) or 0),
    }


def bind_device(force: bool = False, *, client_name: str = "",
                device_name: str = "", platform_name: str = "") -> Dict[str, Any]:
    """登录态绑定本机设备（幂等）。已有 `backend.json` 时默认**跳过**。

    `force=True` 时重新绑定/认领：会把当前 `backend.json.client_id` 作为
    `existingClientId` 传给服务端（仅当服务端确认它属于当前账号才复用），
    然后覆盖本机凭据。**不自动改任何能力路由/隐私设置** —— 那是向导/面板的事。
    """
    session = _required()
    existing = cred.load()
    if existing is not None and existing.client_id and existing.secret and not force:
        return {"ok": True, "skipped": True, "reason": "本机已有后端凭据，不重复绑定",
                "pair": _pair_view(existing), "deviceId": device_id()}

    host = _hostname()
    # 只取一次：如果 `{DATA}` 不可写，第二次生成会是一个**不同的** id，
    # 而服务端已经按第一次的 id 绑好了（响应里报错的那个值会骗人）。
    did = device_id()
    try:
        creds = pairing.bind_with_account(
            str(session["serverUrl"]), str(session["userToken"]),
            device_id=did,
            client_name=str(client_name or host or "我的 ECHO"),
            device_name=str(device_name or host),
            platform=str(platform_name or platform.current()),
            existing_client_id=(str(existing.client_id) if force and existing else ""),
        )
    except pairing.PairingError as exc:
        raise _translate(exc) from None
    return {"ok": True, "skipped": False, "pair": _pair_view(creds),
            "deviceId": did}


def unbind_device(client_id: str) -> Dict[str, Any]:
    """解绑一台设备（默认本机）。解绑本机时同时删掉本机 `backend.json`。"""
    session = _required()
    cid = str(client_id or "").strip()
    if not cid:
        existing = cred.load()
        cid = str(getattr(existing, "client_id", "") or "") if existing else ""
    if not cid:
        raise AccountError("要带 clientId", code="bad_request", status=400)
    body = _call("POST", str(session["serverUrl"]), "/v1/account/devices/unbind",
                 {"clientId": cid}, token=str(session["userToken"]))
    own = cred.load()
    cleared = False
    if own is not None and str(own.client_id or "") == cid:
        cred.clear()
        cleared = True
    return {"ok": True, "clientId": cid, "cleared": cleared,
            "server": body if isinstance(body, dict) else {}}
