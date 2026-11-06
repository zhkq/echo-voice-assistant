# -*- coding: utf-8 -*-
"""账号体系（2026-11-06，issue #15）：**邮箱即账号**。

注册：`email + 邮件验证码 + 密码 + 邀请码`；登录：`email + 密码`；
忘记密码：邮箱验证码 → 重设密码。老的「工号 + 邀请码」注册/登录路径已退役，
`employee_no` 只作为可留空的历史列保留（不再参与任何登录判断）。

## userToken 是**独立域**

client JWT 的 claims 是 `{sub: client_id, scopes, ver}`；userToken 是
`{sub: user_id, typ: "user", ver}`。两边都显式判 `typ`：

* `server/auth.py::Auth.authenticate()` 拒绝 `typ=user`（账号令牌不能调能力端点）；
* 本模块的 `AccountService.authenticate()` 要求 `typ=user`（客户端 JWT 不能下单）。

## 设备绑定码复用 `pairing_codes`

`kind=device-bind`、TTL 10 分钟、一次性（设计 §4.1 末段）。**不新造第二套码**：
兑换仍然走 `/v1/pair`，兑换后由 `BillingService.after_pair()` 记 `user_devices`。
"""
from __future__ import annotations

import math
import re
import secrets
import time
from typing import Any, Dict, Optional

import jwt
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from server import emailer as emailer_mod
from server import errors, ops
from server.auth import JWT_ALG, PairThrottle, decode_token
from server.passwords import hash_password, verify_password

router = APIRouter(prefix="/v1/account")

#: userToken 的有效期默认 30 天（设计 §3.5.4）。可在 `billing.user_token_ttl_s` 改。
DEFAULT_USER_TOKEN_TTL_S = 30 * 24 * 3600

#: 邮箱上限（RFC 5321 的 254 字节口径）与一个保守的 ASCII 格式。
_EMAIL_MAX_LEN = 254
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

#: 未知邮箱也跑一次 scrypt，避免"邮箱存不存在"从耗时上被看出来。
_DUMMY_PASSWORD_HASH = hash_password("echo-dummy-password-for-timing")

#: 邮箱账号的密码长度上限。scrypt 对超长输入照样要算，别让人拿几 MB 的"密码"烧 CPU。
MAX_PASSWORD_LEN = 200
MIN_PASSWORD_LEN = 8


def normalize_email(raw: Any) -> str:
    """归一化成小写并校验；不合法直接 400 `invalid_email`。"""
    text = str(raw or "").strip().lower()
    if not text or len(text) > _EMAIL_MAX_LEN or not _EMAIL_RE.match(text):
        raise errors.invalid_email()
    return text


def password_policy_error(password: Any) -> str:
    """返回中文原因（空串 = 通过）。**只在服务端判**。"""
    text = str(password if password is not None else "")
    if not text:
        return "密码不能为空"
    if len(text) < MIN_PASSWORD_LEN:
        return "密码至少 %d 位" % MIN_PASSWORD_LEN
    if len(text) > MAX_PASSWORD_LEN:
        return "密码最多 %d 位" % MAX_PASSWORD_LEN
    if any(ord(ch) < 32 for ch in text):
        return "密码不能包含控制字符"
    if not any(ch.isalpha() for ch in text) or not any(ch.isdigit() for ch in text):
        return "密码要同时包含字母和数字"
    return ""


#: 自由文本（例如管理面发邀请码时的备注）限长 + 拒绝 HTML 元字符/控制字符。
_TEXT_MAX = 64


def clean_text(raw: Any, what: str = "文本", max_len: int = _TEXT_MAX) -> str:
    value = str(raw or "").strip()
    if len(value) > int(max_len):
        raise errors.bad_request("%s 太长（上限 %d 字）" % (what, int(max_len)))
    for ch in value:
        if ch in '<>&"\'`' or ord(ch) < 32:
            raise errors.bad_request("%s 不能包含 HTML 元字符或控制字符" % what)
    return value


def new_user_id() -> str:
    return "usr-" + secrets.token_hex(6)


def _bearer(authorization: str) -> str:
    raw = str(authorization or "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def device_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """一台设备 → 面板要的 camelCase 视图（**不含任何凭据**）。"""
    return {
        "clientId": str(row.get("client_id") or ""),
        "deviceName": str(row.get("device_name") or ""),
        "boundAt": float(row.get("bound_at") or 0),
        "lastSeen": float(row.get("last_seen") or 0),
        "status": str(row.get("status") or ""),
    }


def _public_user(row: Dict[str, Any]) -> Dict[str, Any]:
    """可以出网的账号视图。**没有 password_hash / 没有 token_version。**"""
    return {
        "userId": str(row.get("user_id") or ""),
        "name": str(row.get("name") or ""),
        # 历史字段：新账号恒为空；保留是为了旧排障页面/客户端不炸。
        "employeeNo": str(row.get("employee_no") or ""),
        "email": str(row.get("email") or ""),
        "emailVerified": bool(float(row.get("email_verified_at") or 0) > 0),
        "phone": str(row.get("phone") or ""),
        "status": str(row.get("status") or ""),
        "createdAt": float(row.get("created_at") or 0),
    }


class AccountService:
    def __init__(self, cfg, store, auth, billing=None, email_sender=None):
        self.cfg = cfg
        self.store = store
        self.auth = auth
        self.billing = billing
        self.login_throttle = PairThrottle(
            max_failures=int(cfg.get("auth.pair_max_failures", 5)),
            window_s=float(cfg.get("auth.pair_window_s", 300)))
        if email_sender is None:
            self.email_sender, self.email_error = emailer_mod.build_sender(cfg)
        else:
            self.email_sender, self.email_error = email_sender, ""

    # ---------------------------------------------------------------- 令牌

    @property
    def key(self) -> str:
        """userToken 用与 client JWT 同一个签名密钥，但 claims 的 `typ` 不同域。

        没配 `auth.jwt_secret` 时**明确报配置错误**，不随机生成：随机密钥会让
        进程一重启所有账号登出，而现象很难联想到"密钥每次都是新的"。
        """
        secret = str(self.cfg.get("auth.jwt_secret", "") or "").strip()
        if not secret:
            raise errors.auth_misconfigured(
                "收费功能需要 userToken，但没配 auth.jwt_secret；"
                "请生成一个（openssl rand -hex 32）后写进配置")
        return secret

    def issue_token(self, user: Dict[str, Any]) -> Dict[str, Any]:
        now = int(time.time())
        ttl = int(self.cfg.get("billing.user_token_ttl_s",
                               DEFAULT_USER_TOKEN_TTL_S) or DEFAULT_USER_TOKEN_TTL_S)
        claims = {
            "sub": str(user.get("user_id") or ""),
            "typ": "user",
            # 撤销靠它：比对 `users.token_version`（logout / 重置密码时 +1）。
            "ver": int(user.get("token_version") or 1),
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_hex(8),
        }
        return {"userToken": jwt.encode(claims, self.key, algorithm=JWT_ALG),
                "expiresAt": now + ttl, "expiresIn": ttl}

    def authenticate(self, authorization: str) -> Dict[str, Any]:
        token = _bearer(authorization)
        if not token:
            raise errors.unauthorized("缺少 userToken")
        claims = decode_token(token, self.key,
                              leeway_s=int(self.cfg.get("auth.clock_skew_s", 60)))
        if str(claims.get("typ") or "") != "user":
            raise errors.unauthorized("这不是账号令牌")
        uid = str(claims.get("sub") or "")
        user = self.store.user(uid)
        if user is None:
            raise errors.unauthorized("账号不存在")
        if str(user.get("status") or "active") != "active":
            raise errors.forbidden("这个账号已停用")
        if int(user.get("token_version") or 1) != int(claims.get("ver") or 0):
            raise errors.unauthorized("登录已失效，请重新登录")
        return user

    def optional(self, authorization: str) -> Optional[Dict[str, Any]]:
        try:
            return self.authenticate(authorization)
        except errors.EchoError:
            return None

    # ---------------------------------------------------------------- 配置

    def _int_cfg(self, path: str, default: int) -> int:
        try:
            return max(1, int(self.cfg.get(path, default)))
        except (TypeError, ValueError):
            return int(default)

    def _email_ready(self) -> None:
        if not bool(self.cfg.get("account.email.enabled", False)):
            raise errors.email_disabled()
        if self.email_sender is None:
            raise errors.email_unconfigured(self.email_error)

    def _check_invite(self, invite_code: str) -> None:
        """只读校验邀请码；真正消费在注册事务里（避免失败时白扣名额）。"""
        if not bool(self.cfg.get("invite.required", True)):
            return
        if not invite_code:
            raise errors.invite_invalid("注册需要邀请码")
        invite = self.store.invite(invite_code)
        if invite is None:
            raise errors.invite_invalid("没有这张邀请码")
        expires_at = float(invite.get("expires_at") or 0)
        if expires_at and expires_at < time.time():
            raise errors.invite_invalid("邀请码已过期")
        if int(invite.get("used") or 0) >= int(invite.get("max_uses") or 0):
            raise errors.invite_invalid("邀请码已用完")

    # ---------------------------------------------------------------- 发码

    def send_code(self, payload: Dict[str, Any], source: str = "") -> Dict[str, Any]:
        """发 6 位验证码。响应不区分"邮箱是否已注册"。"""
        self._email_ready()
        payload = payload or {}
        email = normalize_email(payload.get("email"))
        purpose = str(payload.get("purpose") or "").strip().lower()
        if purpose not in ("register", "reset"):
            raise errors.invalid_purpose("purpose 只能是 register 或 reset")
        now = time.time()
        # 顺手清掉 24 小时前的行；失败不影响本次发码。
        try:
            self.store.prune_email_codes(now - 24 * 3600)
        except Exception:
            pass
        cooldown = max(0.0, float(self.cfg.get("account.email.resend_cooldown_s", 60) or 0))
        latest = self.store.email_code_latest_any(email, purpose)
        if latest is not None:
            elapsed = now - float(latest.get("created_at") or 0)
            if elapsed < cooldown:
                raise errors.rate_limited(
                    max(1, int(math.ceil(cooldown - elapsed))),
                    detail="同一邮箱同一用途 %d 秒内只能发一次" % int(cooldown))
        max_email = self._int_cfg("account.email.max_per_email_hour", 5)
        max_ip = self._int_cfg("account.email.max_per_ip_hour", 20)
        if self.store.email_code_count(email=email, since=now - 3600) >= max_email:
            raise errors.rate_limited(3600, detail="这个邮箱一小时内发码次数已达上限")
        if source and self.store.email_code_count(ip=source, since=now - 3600) >= max_ip:
            raise errors.rate_limited(3600, detail="这个来源一小时内发码次数已达上限")

        if purpose == "reset":
            user = self.store.user_by_email(email)
            if user is None or str(user.get("status") or "active") != "active":
                # 防枚举：不告诉你这个邮箱有没有账号，也不发信。
                return {"ok": True, "purpose": purpose,
                        "expiresIn": self._int_cfg("account.email.code_ttl_s", 600),
                        "cooldownSeconds": int(cooldown)}

        code = "%06d" % secrets.randbelow(1_000_000)
        ttl = self._int_cfg("account.email.code_ttl_s", 600)
        code_id = self.store.email_code_create(
            email=email, purpose=purpose, code_hash=hash_password(code),
            expires_at=now + ttl, ip=source, now=now)
        try:
            self.email_sender.send_code(email=email, code=code, purpose=purpose,
                                        ttl_s=ttl)
        except emailer_mod.EmailSendError as exc:
            try:
                self.store.email_code_invalidate(code_id)
            except Exception:
                pass
            raise errors.email_send_failed(str(exc))
        except Exception:
            try:
                self.store.email_code_invalidate(code_id)
            except Exception:
                pass
            raise errors.email_send_failed("邮件发送异常")
        return {"ok": True, "purpose": purpose, "expiresIn": ttl,
                "cooldownSeconds": int(cooldown)}

    # ---------------------------------------------------------------- 注册

    def _verified_code(self, email: str, purpose: str, code: str) -> Dict[str, Any]:
        """校验验证码并返回仍然有效的行；失败时按原因抛错并记账。"""
        text = str(code or "").strip()
        if not text:
            raise errors.verification_code_invalid("缺少验证码")
        if len(text) != 6 or not text.isdigit():
            raise errors.verification_code_invalid()
        now = time.time()
        max_attempts = self._int_cfg("account.email.max_attempts", 5)
        row = self.store.email_code_latest(email, purpose)
        if row is None:
            raise errors.verification_code_invalid("验证码不存在或已使用")
        if float(row.get("expires_at") or 0) < now:
            raise errors.verification_code_expired()
        if int(row.get("attempts") or 0) >= max_attempts:
            raise errors.verification_code_attempts_exceeded()
        if not verify_password(text, str(row.get("code_hash") or "")):
            attempts = self.store.email_code_attempt(
                int(row["id"]), max_attempts=max_attempts, now=now)
            if attempts >= max_attempts:
                raise errors.verification_code_attempts_exceeded()
            raise errors.verification_code_invalid()
        return row

    def register(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _ = self.key
        self._email_ready()
        payload = payload or {}
        email = normalize_email(payload.get("email"))
        password = str(payload.get("password") or "")
        reason = password_policy_error(password)
        if reason:
            raise errors.password_weak(reason)
        invite_required = bool(self.cfg.get("invite.required", True))
        invite_code = str(payload.get("inviteCode") or payload.get("invite_code") or "").strip().upper()
        name = str(payload.get("name") or "").strip()
        if name:
            name = clean_text(name, "姓名", 64)
        # 邀请码先只读校验：没有邀请码的人不该走到"邮箱是否已注册"那一步。
        self._check_invite(invite_code)
        row = self._verified_code(email, "register", payload.get("code"))
        if self.store.user_by_email(email) is not None:
            raise errors.email_taken()
        user_id = new_user_id()
        password_hash = hash_password(password)
        result = self.store.create_email_account(
            user_id, email=email, password_hash=password_hash, name=name,
            invite_code=invite_code, invite_required=invite_required,
            code_id=int(row["id"]),
            max_attempts=self._int_cfg("account.email.max_attempts", 5),
            now=time.time())
        if result == "email_taken":
            raise errors.email_taken()
        if result == "invite_invalid":
            raise errors.invite_invalid("邀请码已用完")
        if result != "ok":
            raise errors.verification_code_invalid("验证码已失效")
        user = self.store.user(user_id) or {}
        trial = None
        if self.billing is not None and self.billing.enabled:
            try:
                trial = self.billing.grant_trial(user_id)
            except Exception:
                # 试用送不出去**不该**让注册失败：账号已经建好了，管理员可手工补权益。
                trial = None
        issued = self.issue_token(user)
        return {"userId": user_id, "user": _public_user(user), "devices": [],
                "trial": bool(trial is not None), **issued}

    # ---------------------------------------------------------------- 登录

    def login(self, payload: Dict[str, Any], source: str = "") -> Dict[str, Any]:
        _ = self.key
        payload = payload or {}
        self.login_throttle.check(str(source or "unknown"))
        try:
            email = normalize_email(payload.get("email"))
            password = str(payload.get("password") or "")
            # 超过上限的"密码"不可能是合法的：跑一次固定短串的 dummy scrypt 后
            # 直接 401，别拿几 MB 输入烧 CPU。
            if len(password) > MAX_PASSWORD_LEN:
                verify_password("x", _DUMMY_PASSWORD_HASH)
                raise errors.unauthorized("邮箱或密码不对")
            user = self.store.user_by_email(email)
            stored = str(user.get("password_hash") or "") if user else ""
            if not user or not stored:
                # 未知邮箱/无密码的老账号都跑一次 dummy scrypt，响应与耗时都统一。
                verify_password(password, _DUMMY_PASSWORD_HASH)
                raise errors.unauthorized("邮箱或密码不对")
            if not verify_password(password, stored):
                raise errors.unauthorized("邮箱或密码不对")
            if str(user.get("status") or "active") != "active":
                raise errors.forbidden("这个账号已停用")
        except errors.EchoError as exc:
            if exc.status in (400, 401, 403):
                self.login_throttle.failed(str(source or "unknown"))
            raise
        self.login_throttle.succeeded(str(source or "unknown"))
        try:
            self.store.touch_user_login(str(user["user_id"]))
        except Exception:
            pass
        issued = self.issue_token(user)
        return {"userId": str(user["user_id"]), "user": _public_user(user),
                "devices": [device_view(d) for d in
                            self.store.devices(str(user["user_id"]))], **issued}

    # ---------------------------------------------------------------- 重置密码

    def reset_password(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _ = self.key
        self._email_ready()
        payload = payload or {}
        email = normalize_email(payload.get("email"))
        new_password = str(payload.get("newPassword")
                           or payload.get("new_password") or "")
        reason = password_policy_error(new_password)
        if reason:
            raise errors.password_weak(reason)
        row = self._verified_code(email, "reset", payload.get("code"))
        user = self.store.user_by_email(email)
        if user is None or str(user.get("status") or "active") != "active":
            # 不在"邮箱是否存在"上给答案。
            raise errors.verification_code_invalid()
        version = self.store.reset_user_password(
            str(user["user_id"]), hash_password(new_password),
            code_id=int(row["id"]),
            max_attempts=self._int_cfg("account.email.max_attempts", 5),
            now=time.time())
        if version <= 0:
            raise errors.verification_code_invalid("验证码已失效")
        return {"ok": True, "tokenVersion": int(version)}

    # ---------------------------------------------------------------- 我的 / 设备

    def _entitlement_block(self, user: Dict[str, Any]) -> Dict[str, Any]:
        uid = str(user.get("user_id") or "")
        devices = self.store.devices(uid)
        if self.billing is None:
            return {"entitlement": None, "devices": devices}
        return {"entitlement": self.billing.entitlement_summary(uid, devices=len(devices)),
                "devices": [device_view(d) for d in devices]}

    def me(self, user: Dict[str, Any]) -> Dict[str, Any]:
        out = {"user": _public_user(user)}
        # 顶层也摊一份身份字段：客户端「我的」页直接读 `userId/email`。
        out.update(_public_user(user))
        out.update(self._entitlement_block(user))
        return out

    def create_device_code(self, user: Dict[str, Any], client_name: str = "") -> Dict[str, Any]:
        """换机/重装：发一张一次性设备绑定码（10 分钟、只能绑到当前账号）。"""
        if self.billing is not None:
            self.billing.require_entitlement(str(user["user_id"]))
            limit = self.billing.device_limit(str(user["user_id"]))
            if self.store.count_devices(str(user["user_id"])) >= limit:
                raise errors.device_limit_reached(limit)
        ttl = float(self.cfg.get("billing.device_bind_ttl_s", 600) or 600)
        code = self.auth.create_pairing_code(
            created_by="user:" + str(user["user_id"]),
            name=str(client_name or "新设备"),
            scopes=str(self.cfg.get("billing.client_scopes", "") or ""),
            ttl_s=ttl, user_id=str(user["user_id"]), kind="device-bind")
        built = ops.pairing_string(self.cfg, code)
        return {"bindCode": code, "pairUrl": built["url"],
                "expiresAt": time.time() + ttl, "ttlSeconds": int(ttl),
                "devices": self.store.count_devices(str(user["user_id"])),
                "deviceLimit": (self.billing.device_limit(str(user["user_id"]))
                                if self.billing is not None else 0)}

    def unbind(self, user: Dict[str, Any], client_id: str) -> Dict[str, Any]:
        """解绑一台设备：设备行置 `revoked`，客户端**立即失效**（401/403，复用老路径）。"""
        cid = str(client_id or "").strip()
        if not cid:
            raise errors.bad_request("要带 clientId")
        device = self.store.device_for_client(cid)
        if device is None or str(device.get("user_id") or "") != str(user["user_id"]):
            raise errors.EchoError(404, "device_not_found", "没有这台设备",
                                   detail=cid)
        self.store.revoke_device(str(user["user_id"]), cid)
        # 复用既有**禁用**路径（403 `forbidden`：认得出这台设备，但不许用）。
        # 刻意**不推 token_version**：那会让旧令牌变成 401「不知道你是谁」，
        # 而设计 §3.5.10 要的是 403「这台设备已被解绑」。
        try:
            self.auth.set_disabled(cid, True)
        except errors.EchoError:
            pass
        if self.billing is not None:
            self.billing.forget_device(cid)
        return {"ok": True, "clientId": cid}

    def logout(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """退出登录：`users.token_version += 1`，已发出的 userToken 全部失效。"""
        version = self.store.bump_user_token_version(str(user["user_id"]))
        return {"ok": True, "tokenVersion": version}


# ---------------------------------------------------------------- 路由（/v1/account）

def _state(request: Request):
    return request.app.state.echo


def _svc(request: Request) -> AccountService:
    svc = getattr(_state(request), "account", None)
    if svc is None:
        raise errors.auth_misconfigured("服务端没有可用的账号服务")
    return svc


def _user(request: Request) -> Dict[str, Any]:
    return _svc(request).authenticate(request.headers.get("authorization") or "")


def _source_of(request: Request) -> str:
    """限速来源：优先 X-Forwarded-For 第一段，否则 request.client.host。"""
    fwd = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "unknown")


async def _body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


@router.post("/email/send-code")
async def send_code(request: Request):
    """`{email, purpose: register|reset}` → 发 6 位码。响应不区分邮箱是否已注册。"""
    payload = await _body(request)
    return await run_in_threadpool(_svc(request).send_code, payload, _source_of(request))


@router.post("/register")
async def register(request: Request):
    """`{email, code, password, inviteCode?}` → `{userId, userToken, ...}`。**免客户端凭据**。"""
    payload = await _body(request)
    return await run_in_threadpool(_svc(request).register, payload)


@router.post("/password/reset")
async def password_reset(request: Request):
    """`{email, code, newPassword}` → 改密并撤销旧 userToken。"""
    payload = await _body(request)
    return await run_in_threadpool(_svc(request).reset_password, payload)


@router.post("/login")
async def login(request: Request):
    """`{email, password}` → `{userToken, expiresAt, ...}`。**免客户端凭据**。"""
    payload = await _body(request)
    return await run_in_threadpool(_svc(request).login, payload, _source_of(request))


@router.get("/me")
def me(request: Request):
    """账号信息 + 权益摘要 + 已绑设备（面板「我的」页读它）。"""
    return _svc(request).me(_user(request))


@router.post("/devices/code")
async def device_code(request: Request):
    """换机/重装：申请一张一次性设备绑定码。"""
    user = _user(request)
    payload = await _body(request)
    return _svc(request).create_device_code(
        user, str(payload.get("clientName") or payload.get("name") or ""))


@router.post("/devices/unbind")
async def device_unbind(request: Request):
    """解绑一台设备，腾出设备名额（旧客户端立即失效）。"""
    user = _user(request)
    payload = await _body(request)
    return _svc(request).unbind(user, str(payload.get("clientId") or ""))


@router.post("/logout")
def logout(request: Request):
    """退出登录（撤销这个账号已发出的所有 userToken）。"""
    return _svc(request).logout(_user(request))
