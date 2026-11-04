# -*- coding: utf-8 -*-
"""账号 / 邀请码 / 设备绑定（设计 §3.5、§4.2）。**权益归人，设备只是登录端。**

## userToken 是**独立域**

client JWT 的 claims 是 `{sub: client_id, scopes, ver}`；userToken 是
`{sub: user_id, typ: "user", ver}`。两边都显式判 `typ`：

* `server/auth.py::Auth.authenticate()` 拒绝 `typ=user`（账号令牌不能调能力端点）；
* 本模块的 `AccountService.authenticate()` 要求 `typ=user`（客户端 JWT 不能下单）。

**不靠"sub 前缀不同"这种间接理由**：两个域的令牌由两处各自钉死，改坏一处会红。

## 登录方式（本轮口径）

设计 §3.5.3 的 A 案：**邀请码 + 姓名 + 工号**。没有密码 —— 内部 10 元月卡场景，
邀请码就是"谁能注册/登录"的门。`invite.required=false` 时退化成"工号即可"，
只适合完全内部的部署。

## 设备绑定码复用 `pairing_codes`

`kind=device-bind`、TTL 10 分钟、一次性（设计 §4.1 末段）。**不新造第二套码**：
兑换仍然走 `/v1/pair`，兑换后由 `BillingService.after_pair()` 记 `user_devices`。
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional

import jwt
from fastapi import APIRouter, Request

from server import errors, ops
from server.auth import JWT_ALG, PairThrottle, decode_token

router = APIRouter(prefix="/v1/account")

#: userToken 的有效期默认 30 天（设计 §3.5.4）。可在 `billing.user_token_ttl_s` 改。
DEFAULT_USER_TOKEN_TTL_S = 30 * 24 * 3600

#: 工号允许的字符：字母 / 数字 / 下划线 / 短横线 / 点。**不要中文**（它在 URL 与
#: 表格里都要好复制；姓名才是中文）。
_EMPLOYEE_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")

#: 姓名/备注这类**会出网**的自由文本：限长 + 拒绝 HTML 元字符/控制字符。
#: 管理面的表格已经只允许 `raw()` 显式插入，但输入侧再收一道，避免"某天有人
#: 又加了一个不过 esc 的渲染点"时直接变成存储型 XSS。
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
    """可以出网的账号视图。**没有 token_version / 没有任何哈希**。"""
    return {
        "userId": str(row.get("user_id") or ""),
        "name": str(row.get("name") or ""),
        "employeeNo": str(row.get("employee_no") or ""),
        "phone": str(row.get("phone") or ""),
        "status": str(row.get("status") or ""),
        "createdAt": float(row.get("created_at") or 0),
    }


class AccountService:
    def __init__(self, cfg, store, auth, billing=None):
        self.cfg = cfg
        self.store = store
        self.auth = auth
        self.billing = billing
        # 登录是**免凭据**入口（工号 + 邀请码），必须和 /v1/pair 一样有失败退避。
        self.login_throttle = PairThrottle(
            max_failures=int(cfg.get("auth.pair_max_failures", 5)),
            window_s=float(cfg.get("auth.pair_window_s", 300)))

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
            # 撤销靠它：比对 `users.token_version`（logout / 重置设备时 +1）。
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

    # ---------------------------------------------------------------- 注册 / 登录

    def _valid_login_invite(self, code: str) -> None:
        """登录时只验"这张邀请码存在且没过期"，**不消耗次数**。

        注册时已经消耗过名额；登录再消耗的话，一次性邀请码的用户第二次就登不进来。
        `invite.required=false` 时不看这一项。
        """
        invite = self.store.invite(code)
        if invite is None:
            raise errors.invite_invalid("没有这张邀请码")
        expires_at = float(invite.get("expires_at") or 0)
        if expires_at and expires_at < time.time():
            raise errors.invite_invalid("邀请码已过期")

    def register(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 先确认 token 签得出来（没配 jwt_secret 就 503），**再**建账号 ——
        # 否则会留下一个"注册成功但拿不到令牌"的账号，用户只能找管理员删。
        _ = self.key
        payload = payload or {}
        name = clean_text(payload.get("name"), "姓名", 64)
        employee_no = str(payload.get("employeeNo") or payload.get("employee_no") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        invite_code = str(payload.get("inviteCode") or payload.get("invite_code") or "").strip()
        if not name:
            raise errors.bad_request("要填姓名")
        if not employee_no:
            raise errors.bad_request("要填工号")
        if not set(employee_no) <= _EMPLOYEE_CHARS or len(employee_no) > 64:
            raise errors.bad_request("工号只能包含字母/数字/下划线/短横线/点（最多 64 位）")
        invite_required = bool(self.cfg.get("invite.required", True))
        if invite_required:
            invite = self.store.invite(invite_code)
            if invite is None:
                raise errors.invite_invalid("没有这张邀请码")
            expires_at = float(invite.get("expires_at") or 0)
            if expires_at and expires_at < time.time():
                raise errors.invite_invalid("邀请码已过期")
            if int(invite.get("used") or 0) >= int(invite.get("max_uses") or 0):
                raise errors.invite_invalid("邀请码已用完")
        if self.store.user_by_employee_no(employee_no) is not None:
            raise errors.employee_no_taken()
        user_id = new_user_id()
        try:
            user = self.store.create_user(user_id, name=name, employee_no=employee_no,
                                          phone=phone)
        except ValueError:
            raise errors.employee_no_taken()
        if invite_required and not self.store.consume_invite(invite_code):
            # 并发下名额刚好被抢光：把刚建的账号回滚掉，别留下一个"没通过邀请"的号。
            self.store.delete_user(user_id)
            raise errors.invite_invalid("邀请码已用完")
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

    def login(self, payload: Dict[str, Any], source: str = "") -> Dict[str, Any]:
        _ = self.key
        payload = payload or {}
        # 与 `/v1/pair` 同形的失败退避：同一来源连续错到阈值就 429（5 分钟窗口）。
        self.login_throttle.check(str(source or "unknown"))
        try:
            employee_no = str(payload.get("employeeNo")
                              or payload.get("employee_no") or "").strip()
            if not employee_no:
                raise errors.bad_request("要填工号")
            user = self.store.user_by_employee_no(employee_no)
            if user is None:
                # 不区分"没这个工号"与"邀请码不对"：那等于送一个枚举工号的接口。
                raise errors.unauthorized("工号或邀请码不对")
            # `invite.required=true` 时**登录也强制**邀请码（不是只有注册）。
            # 邀请码不消耗次数；只验存在且未过期。客户端/协议不变。
            invite_code = str(payload.get("inviteCode") or payload.get("invite_code") or "")
            if bool(self.cfg.get("invite.required", True)):
                if not invite_code:
                    raise errors.invite_invalid("登录需要邀请码")
                self._valid_login_invite(invite_code)
            elif invite_code:
                self._valid_login_invite(invite_code)
            if str(user.get("status") or "active") != "active":
                raise errors.forbidden("这个账号已停用")
        except errors.EchoError as exc:
            if exc.status in (400, 401, 403):
                self.login_throttle.failed(str(source or "unknown"))
            raise
        self.login_throttle.succeeded(str(source or "unknown"))
        issued = self.issue_token(user)
        return {"userId": str(user["user_id"]), "user": _public_user(user),
                "devices": [device_view(d) for d in
                            self.store.devices(str(user["user_id"]))], **issued}

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
        # 顶层也摊一份身份字段：客户端「我的」页直接读 `userId/name/employeeNo`，
        # 不必先解一层 `user`（两处都给，避免以后有一边改了另一边漂）。
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


@router.post("/register")
async def register(request: Request):
    """邀请码 + 姓名 + 工号 → `{userId, userToken, expiresAt}`。**免客户端凭据**。"""
    return _svc(request).register(await _body(request))


@router.post("/login")
async def login(request: Request):
    """工号 (+ 邀请码) → `{userToken, expiresAt, ...}`。**免客户端凭据**。"""
    return _svc(request).login(await _body(request), _source_of(request))


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
